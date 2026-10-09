"""文档摘要：入库时给每篇文档写一段紧凑摘要（v25）。

**它存在的唯一理由是省 token**（用户明确要求，参考 WeKnora 的摘要机制）。
问题出在问答的上下文装配上：命中 6 段资料时，每段都被补成"它所在的那一小节"
（默认 1800 字）——一次提问光资料就上万字（约 7k token），而其中大部分与问题无关。

摘要改变的是这笔账：

| | 没有摘要 | 有摘要 |
|---|---|---|
| 资料块 | 6 段 × 1800 字 ≈ 10800 字 | 6 段 × 1000 字 ≈ 6000 字 + 每篇文档一行摘要 |
| 模型知道的背景 | 只看得到命中的那几段 | 知道"这几段来自一篇讲什么的文档" |

于是**总 token 降下来、答案的方位感反而更好**——这正是用户那条"van den Bosch
2017 综述的主题是什么"答不好的原因：命中的是某篇文档参考文献里的一行转述，
模型既不知道这篇文档整体在讲什么，也没有被要求区分"转述"与"结论"。

**为什么是"每篇一次"而不是"每段一次"**：每段一次是 WeKnora 那种更细的做法，
但那是 N 次模型调用（一份 400 段的文档就是 400 次）。这里成本必须可控：
一篇文档一次调用、输入只取少量采样片段，摘要长度也封顶。省下的 token 必须
大于生成它的 token，否则这个机制就是负收益——`SUMMARY_MAX_CHARS` 与
`_SAMPLE_CHUNKS` 都是照这条约束定的。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from app.services.chat import ChatService
from app.services.llm import ChatMessage
from app.storage.base import ChunkRecord, StoreBundle

__all__ = ["DEFAULT_BACKFILL_BATCH", "SUMMARY_MAX_CHARS", "DocumentSummaryService"]

logger = logging.getLogger(__name__)

#: 摘要字符上限。**它是一次问答里要复用的东西**，所以必须短：超过这个长度，
#: "带摘要"就不再比"带片段"省。约三句话。
SUMMARY_MAX_CHARS = 220

#: 补漏时一次处理几篇（见 ``summarize_missing``）。
DEFAULT_BACKFILL_BATCH = 2

#: 采样几个片段做输入：首段（标题+摘要通常在这里）、中段、尾段。再多输入就贵了。
_SAMPLE_CHUNKS = 5
#: 每个采样片段的截断长度。
_SAMPLE_CHARS = 600
#: 摘要提示词的输出约束（写进提示词，并与上面上限一致）。
_OUTPUT_HINT = f"不超过 {SUMMARY_MAX_CHARS} 字"

_SYSTEM_PROMPT = (
    "你在为一篇已入库的文档写**检索用摘要**。读者是后续的问答模型："
    "它只会看到这份摘要和几个零散片段，需要靠摘要判断"
    "「这篇文档整体在讲什么、里面能查到什么」。\n"
    "要求：\n"
    "1. 只写这些片段里能确认的内容，不确定就不写，**不要编造标题、作者、结论**；\n"
    "2. 写成三句话：① 这篇文档讲什么主题（是论文/报告/规范/手册要说清）；"
    "② 覆盖哪些要点或方法；③ 能回答哪类问题。\n"
    "3. 不要复述片段原文，不要罗列章节编号，不要写「本文档」以外的套话；\n"
    f"4. 中文，{_OUTPUT_HINT}，直接给摘要正文，不要任何前缀或标题。"
)


class DocumentSummaryService:
    """给文档生成摘要。**从不抛异常**：与分段出题同一条边界。"""

    def __init__(
        self,
        stores: StoreBundle,
        chat: ChatService,
        *,
        enabled: Callable[[], bool] | None = None,
        batch_size: int = DEFAULT_BACKFILL_BATCH,
    ) -> None:
        self._stores = stores
        self._chat = chat
        # 开关走运行期配置（设置页可改）：关掉时**一次模型调用都不发**。
        # 不给（测试）就当作开着——测的是"怎么生成"，不是"开关怎么读"。
        self._enabled = enabled or (lambda: True)
        #: 每次补漏处理几篇。**它跑在 worker 的空闲分支上**，一次做太多会把
        #: 空闲时间占满、也把配额用光；一小批一小批地收敛更稳。
        self._batch_size = max(1, batch_size)

    def summarize(self, document_id: str) -> str:
        """生成并落库这篇文档的摘要；返回摘要（失败返回空串）。

        **失败只记日志**：摘要是旁路能力（问答没有它照样能答，只是多花 token），
        绝不能让一篇文档因为"摘要没写出来"变成 failed。
        """
        if not self._enabled():
            return ""
        document = self._stores.meta.get_document(document_id)
        if document is None:
            return ""
        chunks = list(self._stores.meta.iter_chunks(document_id))
        if not chunks:
            return ""

        try:
            summary = self._ask(document.name, chunks)
        except Exception:
            logger.warning("文档摘要生成失败（%s）", document_id, exc_info=True)
            return ""
        if not summary:
            return ""
        self._stores.meta.update_document_summary(document_id, summary)
        return summary

    def _ask(self, name: str, chunks: Sequence[ChunkRecord]) -> str:
        samples = _sample(chunks)
        body = "\n\n".join(
            f"[片段 {index}]\n{chunk.text[:_SAMPLE_CHARS]}"
            for index, chunk in enumerate(samples, 1)
        )
        # 文件名可作线索，但**明确标注它也可能只是系统里的题录名**：学术 PDF 的
        # 文件名常常是"作者_年份_标题"这种拼接，直接让模型照抄会写出假标题
        raw = self._chat.ask_raw(
            [
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=(
                        f"文档在系统里的名字（仅供参考，可能不准确）：{name}\n\n"
                        f"以下是这篇文档的若干片段（不是全文）：\n\n{body}"
                    ),
                ),
            ]
        )
        return _clean(raw)


    def summarize_missing(self, *, limit: int | None = None) -> int:
        """给**还没有摘要的已索引文档**补摘要，返回补了几篇。

        **为什么需要它**：摘要是这一版才有的机制，库里已有的文档（用户那 20 篇）
        不会重新入库，于是它们永远享受不到"省 token"这件事——而省 token 恰恰是
        这个机制存在的理由。补漏挂在 worker 的空闲分支上（见 `core/services.py`），
        一小批一小批地补，不需要用户点任何东西。

        **只补已索引的**：没跑完的文档块还没定稿，摘要写出来就得重写。
        按入库时间从早到晚补：老文档更可能已经被反复检索过。
        """
        if not self._enabled():
            return 0
        size = max(1, limit or self._batch_size)
        done = 0
        for document in self._stores.meta.list_documents_without_summary(limit=size):
            if self.summarize(document.id):
                done += 1
        return done


def _sample(chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    """跨整篇均匀取几个片段：开头（含标题/摘要）、中间、结尾各来一点。

    **不能只取前几段**：学术 PDF 的前几段常常是标题页与目录，什么也说明不了；
    而只取末尾又会拿到参考文献列表（用户那次答不好的问题正是踩在这上面）。
    """
    total = len(chunks)
    if total <= _SAMPLE_CHUNKS:
        return list(chunks)
    step = total // _SAMPLE_CHUNKS
    picked = [chunks[min(index * step, total - 1)] for index in range(_SAMPLE_CHUNKS)]
    # 保证尾巴也在：结论/局限通常落在最后几段
    tail = chunks[-1]
    if picked[-1].chunk_id != tail.chunk_id:
        picked[-1] = tail
    return picked


def _clean(raw: str) -> str:
    """清掉模型爱加的前缀与包裹，并压到上限。"""
    text = " ".join((raw or "").split())
    for prefix in ("摘要：", "摘要:", "文档摘要：", "文档摘要:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text[:SUMMARY_MAX_CHARS].strip()
