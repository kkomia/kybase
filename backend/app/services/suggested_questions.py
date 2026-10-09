"""推荐问题：**入库时为每个分段生成**，空状态再从中取（v23）。

它是同一件事的两端：

- **写端**（`generate_for_chunks`）：文档切完块之后，让对话模型为**每一段**写出
  「这段可能被问什么」。问题存进 `chunks.questions`，并**并进该段的检索文本**
  （见 `ChunkRecord.index_text`）——向量与全文索引都带上问题的用词，
  于是用户换一种问法也能命中同一段。这是这个功能存在的全部理由：**提升召回**。
- **读端**（`list_questions`）：对话页空状态那排胶囊，直接从库里已存的问题里取。

**读端不再调模型**（v23 之前是每次进空状态现场生成一次）。既然入库时已经为本库的
每一段出了题，再单独花一次模型调用"猜"一批问题就是浪费——而且那样猜出来的问题
与分段无关，答不上来的情况反而更多。

三条边界（沿用 v12 定下的）：

1. **失败不报错**：写端失败只是这一段没有题（摄入照常完成），读端取不到就由前端
   回退静态样例。推荐问题只是引导，不该让"上传"或"打开对话页"变成错误页。
2. **成本有上限**：一次请求最多带 `_CHUNKS_PER_CALL` 段、每段最多 `MAX_QUESTIONS` 条；
   整篇文档按这个批量切分，不逐段调一次。
3. **写端只在库上开着时发生**：开关默认关（生成发生在上传之后，要花钱）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from app.services.chat import ChatService
from app.services.llm import ChatMessage
from app.storage.base import ChunkRecord, StoreBundle

__all__ = [
    "DEFAULT_BATCH_CONCURRENCY",
    "DEFAULT_LIMIT",
    "DEFAULT_QUESTIONS_PER_CHUNK",
    "MAX_QUESTIONS",
    "MIN_QUESTIONS",
    "PROMPT_MAX_CHARS",
    "SuggestedQuestionsService",
]

logger = logging.getLogger(__name__)

#: 每个分段生成几条。1 条常常覆盖不了不同问法，超过 5 条边际收益很低、还稀释原文向量。
MIN_QUESTIONS = 1
MAX_QUESTIONS = 5
DEFAULT_QUESTIONS_PER_CHUNK = 3

#: 空状态一次最多显示几条（读端）。
DEFAULT_LIMIT = 6

#: 自定义出题提示词的长度上限。它是给模型的自然语言指令，写太长只是浪费 token。
PROMPT_MAX_CHARS = 2000

#: 一次请求带几段。8 段 × 约 512 字 ≈ 4K 字，主流模型都吃得下；
#: 再多会让提示词变长，也更难要求模型按段对齐输出。
_CHUNKS_PER_CALL = 8

#: 同时发几批（默认 4，可用 ``KYLAB_QUESTIONS_CONCURRENCY`` 调）。
#:
#: **为什么这件事很要紧**：出题是整条摄入链路里最慢的一步，而它原先**严格串行**——
#: 一份 400 段左右的文档有 50 批，每批一次模型调用（思考型模型 1~2 分钟），
#: 于是单单这一步就是 50~100 分钟，还把所有排队文档一起堵在后面（§12.113 实测）。
#: 这些调用是在**等远端**，不是在本机算：并发几批不会多花 CPU，
#: 只是把等待重叠起来，墙上时间按倍数下降。
#:
#: 上限取 4 而不是更大：模型的 RPM/TPM 限额是真实的约束，撞上去只会拿到 429，
#: 那时重试的代价比省下的时间更大。批次之间互不依赖（每批自带片段编号），
#: 失败的那一批照旧只丢它自己那几段。
DEFAULT_BATCH_CONCURRENCY = 4
#: 每段截多长。出题不需要细节，看到这段在讲什么就够。
_SNIPPET_CHARS = 500
#: 读端抽多少块来找问题。抽块本身很便宜（一条 SQL），所以抽得比写端宽——
#: 老文档多半还没出过题，抽太少会"看着有文档却一条问题都没有"。
_READ_SAMPLE_CHUNKS = 40

_SYSTEM_PROMPT = (
    "你是一个知识库助手。用户会给你资料里的若干片段，"
    "请为每一段分别写出用户可能提出的问题。只输出问题本身，不要答案、不要解释。"
)
_INSTRUCTION = (
    "为上面每一段各写 {n} 个中文问题。要求：具体、能靠这一段的内容回答，"
    "尽量贴近用户真实的问法（同一段可以从不同角度问）。\n"
    "输出格式必须照做：每段以 `###片段N` 单独一行开头（N 是上面的片段编号，"
    "从 1 开始），紧接着每行写一个问题，不要编号、不要引号、不要额外说明。"
)

#: 分段块头：`###片段3` / `### 片段3` / `##片段3`。宽松匹配——模型很少一字不差。
_BLOCK_HEADER = re.compile(r"^\s*#{2,4}\s*片段\s*(\d+)\s*$")

#: 去掉行首的编号/项目符号（`1.` `-` `•` `1、` `1)`）。**只吃前缀**，
#: 不能用一个 lstrip(chars) 把"2024 年的…"这种以数字开头的问题也削掉。
_ENUMERATOR = re.compile(r"^\s*(?:[-*•]+|\d+\s*[.、)．]\s*)")


class SuggestedQuestionsService:
    """写端（入库出题）与读端（空状态取题）都收在这里。"""

    def __init__(
        self,
        stores: StoreBundle,
        chat: ChatService,
        *,
        batch_concurrency: int = DEFAULT_BATCH_CONCURRENCY,
    ) -> None:
        self._stores = stores
        self._chat = chat
        # 至少 1：0 会让"出题"静默变成什么都不做（并发度写成 0 的配置错误
        # 不该表现成"这个功能不见了"，而应该退化成串行）
        self._batch_concurrency = max(1, int(batch_concurrency))

    # ------------------------------------------------------------------ 写端：入库出题

    def generate_for_chunks(
        self,
        chunks: Sequence[ChunkRecord],
        *,
        model_pk: str | None = None,
        count: int = DEFAULT_QUESTIONS_PER_CHUNK,
        prompt: str = "",
    ) -> dict[str, list[str]]:
        """为每个分段生成问题，返回 ``{chunk_id: [问题]}``。

        **从不抛异常**：这是摄入链路上的一步旁路，出题失败只该让这一段没有题，
        不该让整篇文档 failed（`IngestService` 里任何未捕获的异常都会被 worker
        当成可重试失败，最终把文档置为 failed）。所以逐批 try/except，
        失败的批次直接少几条题。

        一次请求带 `_CHUNKS_PER_CALL` 段，而不是每段一次调用——一份 100 段的文档
        那就是 100 次请求，贵得离谱。批与批之间**并发**发（见
        ``DEFAULT_BATCH_CONCURRENCY``）：它们是在等远端，串行等于把等待叠起来。
        """
        if not chunks:
            return {}
        per_chunk = max(MIN_QUESTIONS, min(count, MAX_QUESTIONS))
        batches = _batched(chunks, _CHUNKS_PER_CALL)
        generated: dict[str, list[str]] = {}
        if len(batches) <= 1 or self._batch_concurrency <= 1:
            # 一批（或配置成串行）时不必开线程池：省掉一次调度，
            # 串行这条路径也仍然是默认之外的显式选择
            for batch in batches:
                generated.update(
                    self._generate_batch(batch, model_pk=model_pk, count=per_chunk, prompt=prompt)
                )
            return generated

        # `map` 保持顺序，结果与串行完全一致（同一个文档反复跑得到同样的题序）。
        # `_generate_batch` 自带 try/except：某一批失败只丢它那几段，
        # **不会**把整篇文档带成 failed（这条边界是 v23 定下的，并发不改变它）。
        with ThreadPoolExecutor(
            max_workers=min(self._batch_concurrency, len(batches)),
            thread_name_prefix="questions",
        ) as pool:
            for produced in pool.map(
                lambda batch: self._generate_batch(
                    batch, model_pk=model_pk, count=per_chunk, prompt=prompt
                ),
                batches,
            ):
                generated.update(produced)
        return generated

    def _generate_batch(
        self,
        batch: Sequence[ChunkRecord],
        *,
        model_pk: str | None,
        count: int,
        prompt: str,
    ) -> dict[str, list[str]]:
        try:
            raw = self._chat.ask_raw(
                [
                    ChatMessage(role="system", content=_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=_build_prompt(batch, count, prompt)),
                ],
                model_pk=model_pk,
            )
        except Exception:
            logger.warning("分段问题生成失败（%d 段没有出题）", len(batch), exc_info=True)
            return {}
        return _parse_blocks(raw, batch, count)

    # ------------------------------------------------------------------ 读端：空状态取题

    def list_questions(
        self, *, kb_ids: Sequence[str], limit: int = DEFAULT_LIMIT
    ) -> list[str]:
        """从库里**已存的分段问题**里取几条（不调模型）。

        抽块是随机的（`sample_chunks`），所以每次进空状态看到的问题会换一批——
        这是有意的：它只是"你可以这样问"的引导，不是一份固定清单。
        没有任何已存问题（库的功能关着、或文档还没重新摄入）时返回空列表，
        由前端回退到内置静态样例。

        **抽样只在"有题的块"里做**（`with_questions_only=True`）：出题是逐文档补的，
        绝大多数块没有题；在全库随机抽会经常一条都抽不到，界面就退回静态样例——
        看起来像"功能没生效"（v24 用户反馈）。
        """
        if not kb_ids or limit <= 0:
            return []
        chunks = self._stores.meta.sample_chunks(
            list(kb_ids), limit=_READ_SAMPLE_CHUNKS, with_questions_only=True
        )
        picked: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            for question in chunk.questions:
                text = question.strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                picked.append(text)
                if len(picked) >= limit:
                    return picked
        return picked


# --------------------------------------------------------------------- 纯函数


def _batched(items: Sequence[ChunkRecord], size: int) -> list[list[ChunkRecord]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def _snippet(chunk: ChunkRecord) -> str:
    where = chunk.heading_path or (f"第 {chunk.page} 页" if chunk.page else "")
    body = " ".join(chunk.text.split())[:_SNIPPET_CHARS]
    return f"[{where}] {body}" if where else body


def _build_prompt(chunks: Sequence[ChunkRecord], limit: int, instruction: str = "") -> str:
    """资料片段（按 `###片段N` 编号）+ 出题指令。

    ``instruction`` 为库上的自定义提示词时**替换内置那一句**，片段照旧附在前面——
    自定义的是"怎么出题"，而"依据哪些片段"是系统必须给的东西，不该让用户去拼。
    句子里的 ``{n}`` 会替换成条数；用户写了别的大括号（正则、JSON 示例）时
    ``format`` 会抛，那就原样发出去，不因为一次格式化失败让出题整个失败。
    """
    material = "\n".join(
        f"###片段{index}\n{_snippet(chunk)}" for index, chunk in enumerate(chunks, start=1)
    )
    template = instruction or _INSTRUCTION
    try:
        ask = template.format(n=limit)
    except (KeyError, IndexError, ValueError):
        ask = template
    return f"资料片段：\n{material}\n\n{ask}"


def _parse_blocks(
    raw: str, chunks: Sequence[ChunkRecord], limit: int
) -> dict[str, list[str]]:
    """把模型的输出按 `###片段N` 拆开，落到各自的 chunk_id 上。

    **宽容两条**：
    1. 编号对不上（比如模型从 0 开始、或跳号）的块直接丢掉——宁可少几段有题，
       也不能把 A 段的问题写到 B 段上（那会让检索把用户带到完全无关的段落）。
    2. 只带了一段时，整个输出就当成那一段的题——模型经常"贴心"地省掉块头，
       这时候严格解析会一条都拿不到。
    """
    if not chunks:
        return {}
    lines = raw.splitlines()
    # 只有一段**且模型没写块头**时，整份输出都算它的——模型经常"贴心"地省掉
    # `###片段1`。但若写了块头就仍走按块解析：否则块头本身会被当成一行问题。
    if len(chunks) == 1 and not any(_BLOCK_HEADER.match(line) for line in lines):
        questions = _parse_questions(raw, limit)
        return {chunks[0].chunk_id: questions} if questions else {}

    blocks: dict[int, list[str]] = {}
    current: int | None = None
    for line in lines:
        header = _BLOCK_HEADER.match(line)
        if header:
            current = int(header.group(1))
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(line)

    out: dict[str, list[str]] = {}
    for index, lines in blocks.items():
        if not 1 <= index <= len(chunks):
            continue
        questions = _parse_questions("\n".join(lines), limit)
        if questions:
            out[chunks[index - 1].chunk_id] = questions
    return out


def _parse_questions(raw: str, limit: int) -> list[str]:
    """把模型输出切成问题列表。

    模型经常不听话：加编号、加引号、前面还有一句"以下是几个问题"。这里只做
    **保守清洗**——去编号、去包裹的引号、丢掉过长或过短的行，绝不替它"改写"。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        text = _ENUMERATOR.sub("", line).strip().strip("\"'“”‘’")
        if not text or len(text) > 100:
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out
