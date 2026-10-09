"""知识库 Wiki 的生成与落库（v24）。

镜像同构：``app/services/wiki.py`` → 本文件。

要紧的四条：

1. **页面必须带得出处的编号**：正文里的 `[n]` 与 `wiki_page_sources` 一一对应，
   越界编号要被清洗掉（留着就是"看着严谨、其实点不出处"）；
2. **模型不听话也要出东西**：规划输出解析不出来时回落到按文档名分主题，
   不能整次生成白跑；
3. **不重复解析、不重新切块**：资料来自检索，只读现有块；
4. **失败要吵**：没开 Wiki 形态、库里没有已索引文档，都抛可读错误（任务记 failed），
   而不是"成功但什么都没有"。
"""

from __future__ import annotations

from app.models.enums import DataSourceKind, DocumentStage
from app.services.wiki import (
    WikiService,
    _demote_dead_links,
    _parse_topics,
    _sanitize_citations,
)
from app.storage.base import DocumentRecord, KnowledgeBaseRecord


class FakeChat:
    """假的对话模型：按提示词内容分流（规划 / 总览 / 正文），记录调用次数。"""

    def __init__(self, plan: str | None = None, citations: int = 2) -> None:
        self.plan = plan
        self.citations = citations
        self.calls: list[str] = []

    def ask_raw(self, messages, *, model_pk=None):  # type: ignore[no-untyped-def]
        prompt = messages[-1].content
        self.calls.append(prompt)
        if "文档清单" in prompt:
            if self.plan is not None:
                return self.plan
            return "1. 近视防控 | 讲怎么防\n2. 白内障手术 | 讲怎么做\n3. 干眼治疗 | 讲怎么治"
        if "总览" in prompt:
            return "这个库收录了眼科相关指南。\n\n## 主题\n\n- [[近视防控]]：怎么防"
        if "###片段" in prompt:
            # 故意多写一个越界编号 [99]，验证清洗
            marks = "".join(f"[{index}]" for index in range(1, self.citations + 1))
            return f"## 要点\n\n这是有出处的结论{marks}，还有一个越界引用[99]。"
        return "## 小节\n\n泛泛而谈。"


class FakeHit:
    def __init__(self, index: int) -> None:
        self.chunk_id = f"chunk_{index}"
        self.document_id = "doc_1"
        self.text = f"第 {index} 段原文，讲的是眼轴与近视。"
        self.heading_path = "第一章 > 1.1"
        self.page = index


class FakeRetrieval:
    """只实现 ``search``：返回固定几条命中。"""

    def __init__(self, per_query: int = 2, empty: bool = False) -> None:
        self.per_query = per_query
        self.empty = empty
        self.queries: list[str] = []

    def search(self, request):  # type: ignore[no-untyped-def]
        self.queries.append(request.query)

        class Response:
            hits = [] if self.empty else [FakeHit(i) for i in range(1, self.per_query + 1)]

        return Response()


def _kb(bundle, kb_id: str = "kb_wiki", *, enabled: bool = True) -> KnowledgeBaseRecord:
    return bundle.meta.create_knowledge_base(
        KnowledgeBaseRecord(
            id=kb_id,
            name="眼科资料",
            embedding_model_id="m",
            embedding_dim=8,
            wiki_enabled=enabled,
        )
    )


def _doc(bundle, kb_id: str, document_id: str = "doc_1", stage=DocumentStage.INDEXED) -> None:
    bundle.meta.create_document(
        DocumentRecord(
            id=document_id,
            knowledge_base_id=kb_id,
            name=f"{document_id}.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash=f"hash-{document_id}",
            stage=stage,
        )
    )


# --------------------------------------------------------------------- 纯函数


def test_parse_topics_takes_title_pipe_brief_and_strips_numbering() -> None:
    raw = "1. 近视防控 | 讲怎么防\n- 白内障手术｜全角竖线\n\n**干眼治疗**\n太长" + "字" * 40
    topics = _parse_topics(raw, limit=5)

    assert topics == [
        ("近视防控", "讲怎么防"),
        ("白内障手术", "全角竖线"),
        ("干眼治疗", ""),
    ]


def test_parse_topics_respects_the_cap() -> None:
    raw = "\n".join(f"主题{i} | 说明" for i in range(20))
    assert len(_parse_topics(raw, limit=3)) == 3


def test_sanitize_citations_drops_out_of_range_numbers() -> None:
    cleaned = _sanitize_citations("有效[1] 有效[2] 越界[3] 零[0]", total=2)

    assert cleaned == "有效[1] 有效[2] 越界 零"


def test_demote_dead_links_only_keeps_alive_targets() -> None:
    md = "- [[近视防控]]：在\n- [[不存在]]：不在"
    assert _demote_dead_links(md, {"近视防控"}) == "- [[近视防控]]：在\n- 不存在：不在"


# --------------------------------------------------------------------- 生成


def test_generate_writes_overview_topic_pages_and_sources(bundle) -> None:
    _kb(bundle)
    _doc(bundle, "kb_wiki")
    chat = FakeChat()
    retrieval = FakeRetrieval(per_query=2)
    service = WikiService(bundle, chat=chat, retrieval=retrieval)

    count = service.generate("kb_wiki")

    pages = service.pages("kb_wiki")
    assert count == len(pages) == 4  # 总览 + 3 个主题
    overview, *topics = pages
    assert overview.level == 0 and overview.parent_id is None
    assert overview.title == "眼科资料总览"
    assert all(page.parent_id == overview.id and page.level == 1 for page in topics)
    assert [page.title for page in topics] == ["近视防控", "白内障手术", "干眼治疗"]
    # 每个主题页都检索了一次（资料来自现有块，不重新解析）
    assert len(retrieval.queries) == 3
    # 每页 2 条出处，编号从 1 开始且与正文里的 [n] 对得上
    for page in topics:
        sources = service.sources(page.id)
        assert [item.index for item in sources] == [1, 2]
        assert all(item.document_id == "doc_1" for item in sources)
        assert "[1]" in page.content_md and "[2]" in page.content_md
        # 越界编号被清掉，正文里不再有 [99]
        assert "[99]" not in page.content_md
        # 出处的章节/页码是生成时抄下的快照
        assert sources[0].heading_path == "第一章 > 1.1" and sources[0].page == 1
    # 总览页没有出处（它是导航页），但有站内链接
    assert service.sources(overview.id) == []
    assert "[[近视防控]]" in overview.content_md


def test_generate_replaces_the_previous_pages(bundle) -> None:
    """整库重建：第二次生成不该留下上一版多出来的页面。"""
    _kb(bundle)
    _doc(bundle, "kb_wiki")
    first = WikiService(bundle, chat=FakeChat(), retrieval=FakeRetrieval())
    first.generate("kb_wiki")
    before = len(first.pages("kb_wiki"))

    shorter = WikiService(
        bundle, chat=FakeChat(plan="1. 只有一个主题 | 说明"), retrieval=FakeRetrieval()
    )
    shorter.generate("kb_wiki")

    pages = shorter.pages("kb_wiki")
    assert len(pages) == 2 == before - 2
    assert [page.title for page in pages] == ["眼科资料总览", "只有一个主题"]
    # 旧页面的出处跟着一起没了（不留孤儿）
    assert shorter.stats("kb_wiki")[0] == 2


def test_generate_falls_back_when_the_plan_is_unparsable(bundle) -> None:
    """规划输出是一坨没有竖线的自由文本时，按文档名分主题——**不能整次白跑**。"""
    _kb(bundle)
    _doc(bundle, "kb_wiki", "doc_a")
    _doc(bundle, "kb_wiki", "doc_b")
    service = WikiService(
        bundle,
        chat=FakeChat(plan="我不知道该怎么分，这批资料看起来都差不多。"),
        retrieval=FakeRetrieval(per_query=1),
    )

    service.generate("kb_wiki")

    titles = [page.title for page in service.pages("kb_wiki")]
    assert "doc_a" in titles and "doc_b" in titles


def test_generate_skips_topics_without_any_source(bundle) -> None:
    """主题检索不到原文就不成页：宁可少一页，也不写一篇没有出处的文章。"""
    _kb(bundle)
    _doc(bundle, "kb_wiki")
    service = WikiService(bundle, chat=FakeChat(), retrieval=FakeRetrieval(empty=True))

    count = service.generate("kb_wiki")

    assert count == 1  # 只剩总览页
    assert [page.level for page in service.pages("kb_wiki")] == [0]
    # 总览里指向被跳过主题的链接退化成纯文本（不是点了没反应的死链）
    assert "[[近视防控]]" not in service.pages("kb_wiki")[0].content_md


def test_generate_requires_the_flag_and_indexed_documents(bundle) -> None:
    import pytest

    from app.core.exceptions import InvalidRequestError

    _kb(bundle, enabled=False)
    _doc(bundle, "kb_wiki")
    service = WikiService(bundle, chat=FakeChat(), retrieval=FakeRetrieval())
    with pytest.raises(InvalidRequestError, match="Wiki"):
        service.generate("kb_wiki")

    bundle.meta.set_knowledge_base_wiki("kb_wiki", enabled=True)
    bundle.meta.update_document_stage("doc_1", DocumentStage.UPLOADED)
    with pytest.raises(InvalidRequestError, match="已索引"):
        service.generate("kb_wiki")


# --------------------------------------------------------------------- 入队与状态


def test_enqueue_is_idempotent_and_checks_the_flag(bundle) -> None:
    import pytest

    from app.core.exceptions import ConflictError

    _kb(bundle)
    service = WikiService(bundle, chat=FakeChat(), retrieval=FakeRetrieval())

    first = service.enqueue("kb_wiki")
    assert service.enqueue("kb_wiki").id == first.id  # 连点不堆任务
    assert first.payload == {"kb_id": "kb_wiki"}
    assert first.document_id is None

    bundle.meta.set_knowledge_base_wiki("kb_wiki", enabled=False)
    with pytest.raises(ConflictError, match="Wiki"):
        service.enqueue("kb_wiki")


def test_status_walks_idle_generating_ready(bundle) -> None:
    _kb(bundle)
    _doc(bundle, "kb_wiki")
    service = WikiService(bundle, chat=FakeChat(), retrieval=FakeRetrieval())

    assert service.status("kb_wiki") == ("idle", None)
    service.enqueue("kb_wiki")
    assert service.status("kb_wiki")[0] == "generating"
    # 生成一次（任务留在 pending，但已有页面——页面优先，说明"上一版还在"）
    service.generate("kb_wiki")
    bundle.meta.update_document_stage("doc_1", DocumentStage.INDEXED)
    assert service.status("kb_wiki")[0] == "generating"
