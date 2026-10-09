"""知识库 Wiki：把库里**已录入的内容**整理成一套带原文出处的百科式页面（v24）。

调研结论（`docs/归档/调研/Wiki生成调研-v0.1.md`）里，通用文档没有代码库那种天然骨架，
页面树只能靠"聚类 + 分层摘要"造出来（RAPTOR / GraphRAG 社区报告那条线）。
本版走的是最小可用的一条：**先规划主题 → 每主题用现成检索捞最相关的原文 →
逐页写作并强制带 `[n]` 出处**。它对应报告里的 P0+P1 折中：

- **骨架**来自一次规划调用（而不是向量聚类）——省掉聚类参数，也让人能看懂
  "为什么会有这几个页面"；
- **内容**来自 `RetrievalService`，复用现成的混合检索与已有的 chunk，
  **不重新解析、不重新切块**；
- **出处**是硬要求：每个页面的正文里 `[n]` 指回 `wiki_page_sources`，
  点开能落到原文那一段。没有出处的自动 Wiki 没人敢信，而 kylab 恰好有现成的
  引用体系（报告 §2 的"④溯源"）。

三条明确的取舍：

1. **不做实体抽取/知识图谱**：那是 GraphRAG 路线，成本是"每个文档被模型读好几遍"，
   报告也点名"连同路线项目都在绕开"。本版不建实体页，只出主题页。
2. **用全局默认对话模型**，不给库加"Wiki 模型"设置：WeKnora 的提示词与模型都在
   代码里、不暴露给用户；少一个设置就少一处"配错了没人知道"。真需要按库选模型时，
   结构上只是把 `model_pk` 换成库字段的事。
3. **整库一次性重建**（`replace_wiki_pages` 先删后插），不做增量。增量要
   `wiki_page_sources` + `content_hash` 判脏再逐页重算，是报告 §3.4 的下一步；
   本版把出处一并存下来，就是为了那天不用改数据模型。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.models.enums import TaskKind, TaskState
from app.services.chat import ChatService
from app.services.llm import ChatMessage
from app.services.retrieval.service import RetrievalService
from app.services.retrieval.types import RetrievalQuery
from app.storage.base import (
    DocumentRecord,
    KnowledgeBaseRecord,
    StoreBundle,
    TaskRecord,
    WikiPageRecord,
    WikiSourceRecord,
)

__all__ = ["MAX_TOPICS", "MIN_TOPICS", "WikiService"]

logger = logging.getLogger(__name__)

#: 规划出几个主题页。太少覆盖不了库内容，太多则每页的资料被摊薄、成本也线性涨。
MIN_TOPICS = 3
MAX_TOPICS = 8

#: 每页检索几条原文。8 条 × 约 500 字 ≈ 4K 字，主流模型都吃得下；
#: 也给"每个要点都能标出处"留出足够的编号空间。
CHUNKS_PER_PAGE = 8

#: 喂给模型的片段截断长度。写页面要看到"这段在讲什么"，不需要全文。
_SNIPPET_CHARS = 600

#: 规划时最多列多少篇文档名。文档名本身就是很有信息量的骨架（尤其是带章节名的）。
_PLAN_DOC_SAMPLE = 60

_OVERVIEW_SUFFIX = "总览"

_ENUMERATOR = re.compile(r"^\s*(?:[-*•]+|\d+\s*[.、)．]\s*)")
_CITE = re.compile(r"\[(\d+)\]")

_PLAN_SYSTEM = (
    "你是一个知识库编辑。用户会给你一个知识库里的文档名清单，"
    "请把内容归纳成若干主题，作为这个知识库 Wiki 的目录。"
)
_PLAN_INSTRUCTION = (
    "把这些文档归纳成 {n} 个主题（每个主题是 Wiki 里的一页）。要求：\n"
    "- 主题要能覆盖这批资料的主要方面，彼此不重叠；\n"
    "- 标题用名词短语，不超过 20 个字；不要写「其他」「杂项」这种兜底主题；\n"
    "- 输出格式必须照做：**每行一个主题**，写作 `标题 | 一句话说明`，"
    "不要编号、不要额外的解释或汇总。"
)

_WRITE_SYSTEM = (
    "你是一个严谨的知识库编辑。你只为给出的资料片段写内容，绝不允许引入资料里没有的事实。"
)
_WRITE_INSTRUCTION = (
    "请把下面的资料片段写成一篇百科式页面，标题是「{title}」。要求：\n"
    "- 开篇用 2–4 句话说明这个主题是什么，**不要重复标题、不要写「本文」**；\n"
    "- 用 `## ` 分成 2–4 个小节，小节里先写成段的散文，再按需列要点；\n"
    "- **每一句来自资料的结论，句末都要标出处编号**，例如：高度近视需定期查眼底[2]；\n"
    "- 只使用片段里确有的信息；资料不足就写少一点，不要编；\n"
    "- 关键术语第一次出现时用 **加粗**；\n"
    "- 直接输出页面正文（Markdown），不要写标题行、不要写「参考来源」（出处由系统渲染）。"
)
_OVERVIEW_INSTRUCTION = (
    "请为这个知识库写一页 Wiki 总览，标题是「{title}」。要求：\n"
    "- 开篇 2–4 句说明这个库大致收录了什么；\n"
    "- 用 `## 主题` 列一节，把下面这些主题页**逐个写成一行**："
    "`- [[主题标题]]：一句话说明`；\n"
    "- 题目里的标题**必须与给定主题一字不差**，系统要靠它做站内链接；\n"
    "- 不要写「参考来源」，出处由系统渲染。"
)


class WikiService:
    """规划主题 + 逐页写作 + 落库出处。生成走任务队列，失败不影响摄入主链路。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        chat: ChatService,
        retrieval: RetrievalService,
    ) -> None:
        self._stores = stores
        self._chat = chat
        self._retrieval = retrieval

    # ------------------------------------------------------------------ 读

    def pages(self, kb_id: str) -> list[WikiPageRecord]:
        return self._stores.meta.list_wiki_pages(kb_id)

    def page(self, page_id: str) -> WikiPageRecord:
        record = self._stores.meta.get_wiki_page(page_id)
        if record is None:
            raise NotFoundError(f"Wiki 页面不存在：{page_id}")
        return record

    def sources(self, page_id: str) -> list[WikiSourceRecord]:
        return self._stores.meta.list_wiki_sources(page_id)

    def stats(self, kb_id: str) -> tuple[int, datetime | None]:
        """``(页面数, 最近生成时间)``。接口层不该自己去够存储（分层纪律）。"""
        return self._stores.meta.wiki_stats(kb_id)

    def status(self, kb_id: str) -> tuple[str, str | None]:
        """``(生成状态, 上次失败原因)``。

        **没有单独的状态列**：它完全由"有没有在跑的任务"和"有没有页面"推出来，
        少一份需要同步的状态就少一处会不同步的地方。
        """
        tasks = [
            task
            for task in self._stores.meta.list_tasks()
            if task.kind is TaskKind.WIKI and str(task.payload.get("kb_id") or "") == kb_id
        ]
        running = [t for t in tasks if t.state in (TaskState.PENDING, TaskState.RUNNING)]
        if running:
            return "generating", None
        count, _ = self._stores.meta.wiki_stats(kb_id)
        if count > 0:
            return "ready", None
        failed = [t for t in tasks if t.state is TaskState.FAILED]
        if failed:
            return "failed", failed[-1].error
        return "idle", None

    # ------------------------------------------------------------------ 入队

    def enqueue(self, kb_id: str) -> TaskRecord:
        """把"重建这个库的 Wiki"排进队列。幂等：已在队列里就返回那一个。"""
        kb = self._require_kb(kb_id)
        if not kb.wiki_enabled:
            raise ConflictError("这个知识库没有开启 Wiki 形态；先到设置里打开")
        for task in self._stores.meta.list_tasks():
            if (
                task.kind is TaskKind.WIKI
                and task.state in (TaskState.PENDING, TaskState.RUNNING)
                and str(task.payload.get("kb_id") or "") == kb_id
            ):
                return task
        return self._stores.meta.enqueue_task(
            TaskRecord(
                id=f"task_{uuid.uuid4().hex[:12]}",
                kind=TaskKind.WIKI,
                state=TaskState.PENDING,
                payload={"kb_id": kb_id},
                document_id=None,
            )
        )

    def clear(self, kb_id: str) -> None:
        self._require_kb(kb_id)
        self._stores.meta.replace_wiki_pages(kb_id, [], [])

    # ------------------------------------------------------------------ 生成

    def generate(self, kb_id: str) -> int:
        """重建这个库的 Wiki，返回生成的页面数。**由 worker 调用**。

        失败会抛出（任务记 failed 并带原因）：这是用户显式点的动作，
        "静默成功但什么都没生成"是最坏的结果。
        """
        kb = self._require_kb(kb_id)
        if not kb.wiki_enabled:
            raise InvalidRequestError("这个知识库没有开启 Wiki 形态")
        documents = [
            record
            for record in self._stores.meta.list_documents(kb_id)
            if record.stage.value == "indexed"
        ]
        if not documents:
            raise InvalidRequestError("库里还没有已索引完成的文档，没有可整理的资料")

        topics = self._plan(kb, documents)
        if not topics:
            raise InvalidRequestError("没能从这批文档里归纳出主题，换个库或先补充内容再试")

        now = datetime.now(UTC)
        # 页面 id **不能用 `#` 分隔**：它要出现在 `GET /wiki/pages/{id}` 的路径里，
        # 而 `#` 是 URL 的片段分隔符——请求会被截断成前缀，然后报一个
        # 与真正原因毫不相干的 404（chunk_id 也踩过同一个坑，见 api/documents.ts）。
        overview_id = f"wpage_{kb_id}_overview"
        pages: list[WikiPageRecord] = []
        sources: list[WikiSourceRecord] = []

        # ---- 总览页：只做导航（主题清单 + 站内链接），由主题规划的结果写成
        overview_body = self._write_overview(kb, topics)
        pages.append(
            WikiPageRecord(
                id=overview_id,
                kb_id=kb_id,
                parent_id=None,
                level=0,
                ord=0,
                slug="overview",
                title=f"{kb.name}{_OVERVIEW_SUFFIX}",
                brief=f"共 {len(topics)} 个主题，覆盖 {len(documents)} 篇文档。",
                content_md=overview_body,
                status="ready",
                generated_at=now,
            )
        )

        for index, (title, brief) in enumerate(topics):
            hits = self._retrieve(kb_id, title, brief)
            if not hits:
                logger.info("主题「%s」没有检索到原文，跳过这一页", title)
                continue
            body = self._write_page(title, brief, hits)
            page_id = f"wpage_{kb_id}_{index:02d}"
            pages.append(
                WikiPageRecord(
                    id=page_id,
                    kb_id=kb_id,
                    parent_id=overview_id,
                    level=1,
                    ord=index,
                    slug=f"topic-{index:02d}",
                    title=title,
                    brief=brief,
                    content_md=body,
                    status="ready",
                    generated_at=now,
                )
            )
            for rank, hit in enumerate(hits, start=1):
                sources.append(
                    WikiSourceRecord(
                        page_id=page_id,
                        chunk_id=str(hit.chunk_id),
                        document_id=str(hit.document_id),
                        rank=rank,
                        index=rank,
                        heading_path=hit.heading_path,
                        page=hit.page,
                    )
                )

        # 总览页的 `[[标题]]` 链接只对**真的生成了的页面**有意义：被跳过的主题
        # 会让链接指向不存在的页。这里按最终页表把死链退化成纯文本。
        alive = {page.title for page in pages}
        pages[0].content_md = _demote_dead_links(overview_body, alive)

        self._stores.meta.replace_wiki_pages(kb_id, pages, sources)
        logger.info("知识库 %s 的 Wiki 已重建：%d 页", kb_id, len(pages))
        return len(pages)

    # ------------------------------------------------------------------ 内部

    def _require_kb(self, kb_id: str) -> KnowledgeBaseRecord:
        kb = self._stores.meta.get_knowledge_base(kb_id)
        if kb is None:
            raise NotFoundError(f"知识库不存在：{kb_id}")
        return kb

    def _plan(
        self, kb: KnowledgeBaseRecord, documents: list[DocumentRecord]
    ) -> list[tuple[str, str]]:
        """让模型把文档名归纳成主题清单，返回 ``[(标题, 说明)]``。

        解析不出任何主题时**回落**到"按文档名取前几个"——规划是一次自由文本调用，
        模型完全可能不按格式输出；宁可页少一点、糙一点，也不能让整次生成白跑。
        """
        wanted = max(MIN_TOPICS, min(MAX_TOPICS, len(documents)))
        names = "\n".join(f"- {record.name}" for record in documents[:_PLAN_DOC_SAMPLE])
        prompt = (
            f"知识库「{kb.name}」的文档清单（共 {len(documents)} 篇）：\n{names}\n\n"
            + _PLAN_INSTRUCTION.format(n=wanted)
        )
        try:
            raw = self._chat.ask_raw(
                [
                    ChatMessage(role="system", content=_PLAN_SYSTEM),
                    ChatMessage(role="user", content=prompt),
                ],
                model_pk=None,
            )
        except Exception:
            logger.warning("Wiki 主题规划失败，回落到按文档名分主题", exc_info=True)
            return _fallback_topics(documents, MAX_TOPICS)

        topics = _parse_topics(raw, MAX_TOPICS)
        return topics or _fallback_topics(documents, MAX_TOPICS)

    def _retrieve(self, kb_id: str, title: str, brief: str) -> list:
        """主题页的资料：直接用现成的混合检索，**不另起一套采样**。"""
        query = f"{title} {brief}".strip()
        try:
            response = self._retrieval.search(
                RetrievalQuery(
                    query=query,
                    kb_ids=[kb_id],
                    top_k=CHUNKS_PER_PAGE,
                    candidate_k=max(CHUNKS_PER_PAGE * 4, 30),
                )
            )
        except Exception:
            logger.warning("主题「%s」的检索失败，跳过这一页", title, exc_info=True)
            return []
        return list(response.hits)

    def _write_page(self, title: str, brief: str, hits: list) -> str:
        snippets = "\n".join(
            f"###片段{index}\n{_snippet(hit)}" for index, hit in enumerate(hits, start=1)
        )
        ask = _WRITE_INSTRUCTION.format(title=title)
        if brief:
            ask = f"这一页的大意（供你把握重点）：{brief}\n{ask}"
        raw = self._chat.ask_raw(
            [
                ChatMessage(role="system", content=_WRITE_SYSTEM),
                ChatMessage(role="user", content=f"资料片段：\n{snippets}\n\n{ask}"),
            ],
            model_pk=None,
        )
        return _sanitize_citations(raw, len(hits))

    def _write_overview(self, kb: KnowledgeBaseRecord, topics: list[tuple[str, str]]) -> str:
        listing = "\n".join(f"- {title}：{brief}" for title, brief in topics)
        prompt = f"知识库「{kb.name}」的主题清单：\n{listing}\n\n" + _OVERVIEW_INSTRUCTION.format(
            title=f"{kb.name}{_OVERVIEW_SUFFIX}"
        )
        try:
            raw = self._chat.ask_raw(
                [
                    ChatMessage(role="system", content=_WRITE_SYSTEM),
                    ChatMessage(role="user", content=prompt),
                ],
                model_pk=None,
            )
        except Exception:
            # 总览页挂了不该带走整个 Wiki：退化成一份纯目录
            logger.warning("Wiki 总览页生成失败，退化成纯目录", exc_info=True)
            return "## 主题\n\n" + listing
        return raw.strip()


# --------------------------------------------------------------------- 纯函数


def _snippet(hit) -> str:  # type: ignore[no-untyped-def]
    where = hit.heading_path or (f"第 {hit.page} 页" if hit.page else "")
    body = " ".join(str(hit.text).split())[:_SNIPPET_CHARS]
    return f"[{where}] {body}" if where else body


def _parse_topics(raw: str, limit: int) -> list[tuple[str, str]]:
    """把规划输出解析成 ``[(标题, 说明)]``。

    **保守清洗**：只认 `标题 | 说明`（竖线可能写成全角），丢了说明也认标题行；
    去编号、去引号、丢掉过长或过短的行。解析不出来就返回空，由调用方回落。

    **以句末标点结尾、又没有说明的行不当标题**：模型不按格式输出时，整段话往往
    是一句散文（"这批资料看起来都差不多。"），拿它当页面标题会生成一页莫名其妙
    的东西；这种情形宁可回落到按文档名分主题。
    """
    topics: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        text = _ENUMERATOR.sub("", line).strip().strip("\"'“”‘’")
        if not text or text.startswith("#"):
            continue
        title, _, brief = text.replace("｜", "|").partition("|")
        title = title.strip().strip("*`").strip()
        brief = brief.strip()
        if not title or len(title) > 30 or title in seen:
            continue
        if not brief and title[-1] in "。！？!?.;；":
            continue
        seen.add(title)
        topics.append((title, brief))
        if len(topics) >= limit:
            break
    return topics


def _fallback_topics(documents: list[DocumentRecord], limit: int) -> list[tuple[str, str]]:
    """模型没给出可解析的主题时：按文档名取前几篇，一篇一个主题。

    粗，但**页面标题与文档名对得上**，用户一看就知道这页在讲哪份资料；
    比"生成失败、什么都没有"强。
    """
    topics: list[tuple[str, str]] = []
    for record in documents[:limit]:
        name = record.name.rsplit("/", 1)[-1]
        name = re.sub(r"\.(md|markdown|pdf|docx?|txt|csv|xlsx?|pptx?)$", "", name, flags=re.I)
        if name and name not in {title for title, _ in topics}:
            topics.append((name[:30], "以这份文档为线索整理。"))
    return topics


def _sanitize_citations(raw: str, total: int) -> str:
    """把越界的 `[n]` 去掉。

    模型偶尔会引用不存在的编号（或把资料里的年份写成 `[2024]`）。越界编号在页面上
    点不出出处，留着就是"看起来很严谨、实际指不到东西"——比不标还糟。编号从 1 开始，
    所以 `[0]` 也一并去掉。
    """
    text = _CITE.sub(lambda match: match.group(0) if 1 <= int(match.group(1)) <= total else "", raw)
    return text.strip()


def _demote_dead_links(markdown: str, alive_titles: set[str]) -> str:
    """把指向不存在页面的 `[[标题]]` 退化成纯文本（标题本身）。

    总览页是按"规划出的主题"写的，但个别主题可能因为检索不到原文而没有成页；
    留着 `[[ ]]` 就是一个点了没反应的链接。
    """

    def replace(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        if target in alive_titles:
            return match.group(0)
        return target

    return re.sub(r"\[\[([^\]]+)\]\]", replace, markdown)
