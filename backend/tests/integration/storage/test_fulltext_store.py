"""``FullTextStore`` 的行为测试（集成）。

覆盖架构 5 的全文召回路径：chunk 写入索引、中文可检索、按库收敛、与图片锚点回填。

需要 PostgreSQL 测试库（``KYLAB_TEST_DATABASE_URL``），未配置则整文件跳过。
"""

from app.models.enums import DataSourceKind, DocumentStage
from app.storage.base import (
    ChunkRecord,
    DocumentRecord,
    FullTextStore,
    KnowledgeBaseRecord,
    MetaStore,
    SearchFilter,
)

_ORDINAL_PAGE: object = object()
"""``_chunk`` 的 ``page`` 默认值：按 ``ordinal + 1`` 填（既有用例的摆法）。

**默认值不能是 ``None``**：``None`` 在这份类型里是个**有意义的取值**（"这一块没有
页码"），拿它当"没给"就再也摆不出"页码为空的块"——而那正是页码过滤要钉住的一半。
"""


def _chunk(chunk_id: str, ordinal: int, text: str, *, document_id: str = "doc_1",
           kb_id: str = "kb_1", image_ids: tuple[str, ...] = (),
           page: int | object | None = _ORDINAL_PAGE,
           heading: str | None = "第3章 > 3.2节") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        knowledge_base_id=kb_id,
        part_id=None,
        ordinal=ordinal,
        text=text,
        content_hash=f"h-{chunk_id}",
        heading_path=heading,
        page=ordinal + 1 if page is _ORDINAL_PAGE else page,  # type: ignore[arg-type]
        image_ids=image_ids,
    )


def _seed(store: MetaStore, fulltext: FullTextStore, chunks: list[ChunkRecord]) -> None:
    store.replace_chunks(chunks[0].document_id, chunks)
    fulltext.index_chunks(chunks)


def test_chinese_query_finds_chunk(store: MetaStore, fulltext_store: FullTextStore,
                                   kb, document) -> None:
    _seed(store, fulltext_store, [
        _chunk("c1", 0, "本系统提供向量检索与全文检索的混合召回能力"),
        _chunk("c2", 1, "部署形态以 Docker Compose 为主，支持局域网访问"),
    ])

    hits = fulltext_store.search(query="向量检索", top_k=5)
    assert [hit.chunk_id for hit in hits] == ["c1"]
    assert hits[0].source == "fulltext"
    assert hits[0].heading_path == "第3章 > 3.2节"
    assert hits[0].page == 1


def test_scores_are_positive_and_sorted_desc(store: MetaStore,
                                             fulltext_store: FullTextStore, kb,
                                             document) -> None:
    """bm25 越小越相关，实现里取了负值，统一成"分越高越相关"。"""
    _seed(store, fulltext_store, [
        _chunk("c1", 0, "检索 检索 检索 检索"),
        _chunk("c2", 1, "检索 之外 还有 很多 别的 词语 填充 内容"),
    ])

    hits = fulltext_store.search(query="检索", top_k=5)
    assert len(hits) == 2
    assert all(hit.score > 0 for hit in hits)
    assert hits[0].score >= hits[1].score


def test_kb_filter_scopes_results(store: MetaStore, fulltext_store: FullTextStore,
                                  kb) -> None:
    """架构 5：元数据过滤要能按知识库收敛。"""
    store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_2", name="第二库", embedding_model_id="m", embedding_dim=4)
    )
    store.create_document(
        DocumentRecord(id="doc_2", knowledge_base_id="kb_2", name="b.md",
                       source_kind=DataSourceKind.UPLOAD, content_hash="hash-2",
                       stage=DocumentStage.UPLOADED)
    )
    _seed(store, fulltext_store, [
        _chunk("c1", 0, "知识库检索说明", kb_id="kb_2", document_id="doc_2"),
    ])

    assert fulltext_store.search(query="检索", top_k=5, kb_id="kb_2") != []
    assert fulltext_store.search(query="检索", top_k=5, kb_id="kb_1") == []


def test_reindexing_same_chunk_does_not_duplicate(store: MetaStore,
                                                  fulltext_store: FullTextStore, kb,
                                                  document) -> None:
    chunk = _chunk("c1", 0, "重复索引测试")
    _seed(store, fulltext_store, [chunk])
    fulltext_store.index_chunks([chunk])
    fulltext_store.index_chunks([chunk])

    hits = fulltext_store.search(query="重复索引", top_k=10)
    assert [hit.chunk_id for hit in hits] == ["c1"]


def test_delete_chunks_removes_from_index(store: MetaStore,
                                          fulltext_store: FullTextStore, kb,
                                          document) -> None:
    _seed(store, fulltext_store, [_chunk("c1", 0, "待删除内容"), _chunk("c2", 1, "保留内容")])

    assert fulltext_store.delete_chunks(["c1"]) == 1
    assert fulltext_store.search(query="删除", top_k=5) == []
    assert fulltext_store.search(query="保留", top_k=5) != []
    assert fulltext_store.delete_chunks([]) == 0


def test_image_anchors_are_returned_with_hits(store: MetaStore,
                                              fulltext_store: FullTextStore, kb,
                                              document) -> None:
    """架构 7：命中含图段落时要把图片引用带出来。"""
    _seed(store, fulltext_store, [_chunk("c1", 0, "如图所示，架构分层", image_ids=("img_1",))])

    hits = fulltext_store.search(query="架构分层", top_k=5)
    assert tuple(hits[0].image_ids) == ("img_1",)


def test_special_characters_do_not_break_match(store: MetaStore,
                                               fulltext_store: FullTextStore, kb,
                                               document) -> None:
    """用户输入里的 FTS5 语法字符必须被安全处理，不能抛 OperationalError。"""
    _seed(store, fulltext_store, [_chunk("c1", 0, "C++ 与向量检索")])

    assert fulltext_store.search(query='C++ "向量"*', top_k=5) is not None
    assert fulltext_store.search(query="   ", top_k=5) == []
    assert fulltext_store.search(query="向量", top_k=0) == []


def test_empty_index_returns_no_hits(fulltext_store: FullTextStore) -> None:
    assert fulltext_store.search(query="任何词", top_k=5) == []
    assert fulltext_store.index_chunks([]) is None


# --------------------------------------------------------------------- 过滤下推（v0.64）
#
# 过滤**在 SQL 里生效**，而且 ``top_k`` 个名额只在合条件的块里分——这一层是
# "只查某本书某章"能不能用的分界线：取满 ``top_k`` 再由调用方丢的话，别的书会把名额
# 占满，那本书即使有几百个块也一条都回不来（见 ``SearchFilter`` 的说明）。


def test_filter_conditions_are_applied_in_sql(store: MetaStore,
                                              fulltext_store: FullTextStore, kb,
                                              document) -> None:
    """文档 / 页码 / 标题三个条件都在 SQL 里裁，且 ``top_k`` 按**过滤后**的集合算。"""
    store.create_document(
        DocumentRecord(id="doc_2", knowledge_base_id="kb_1", name="别的书.md",
                       source_kind=DataSourceKind.UPLOAD, content_hash="hash-filter",
                       stage=DocumentStage.UPLOADED)
    )
    _seed(store, fulltext_store, [
        _chunk("c_med_5", 0, "检索 检索 检索 与 医学 手册", page=5, heading="第3章 > 3.1节"),
        _chunk("c_med_10", 1, "检索 检索 检索 与 医学 手册", page=10, heading="第3章 > 3.2节"),
        _chunk("c_med_none", 2, "检索 检索 检索 与 医学 手册", page=None, heading=None),
    ])
    # 别的书：词面密度更高（不设限时它会占满 top_k 名额），而且**没有页码**
    _seed(store, fulltext_store, [
        _chunk("c_ops_7", 0, "检索 检索 检索 检索 检索 检索", document_id="doc_2", page=None,
               heading="Chapter 7"),
    ])

    def found(**filter_kwargs: object) -> set[str]:
        hits = fulltext_store.search(
            query="检索", top_k=10, filter=SearchFilter(**filter_kwargs)  # type: ignore[arg-type]
        )
        return {hit.chunk_id for hit in hits}

    # 不设限时名额被词面密度更高的那段占走
    assert [hit.chunk_id for hit in fulltext_store.search(query="检索", top_k=1)] == ["c_ops_7"]

    # 只要这本书：名额在它自己的三段里分
    filtered = fulltext_store.search(
        query="检索", top_k=2, filter=SearchFilter(document_ids=["doc_1"])
    )
    assert len(filtered) == 2
    assert {hit.document_id for hit in filtered} == {"doc_1"}

    # 页码：含边界；**页码为空的块被排除**（`c_med_none` 与别的书那段都不出现）
    assert found(page_from=5, page_to=5) == {"c_med_5"}
    assert found(page_from=5) == {"c_med_5", "c_med_10"}
    assert found(page_to=5) == {"c_med_5"}
    assert found(page_from=5, page_to=10) == {"c_med_5", "c_med_10"}
    assert found(page_from=100) == set()

    # 标题：大小写不敏感的子串；标题为空的块被排除
    assert found(heading_contains="3.2") == {"c_med_10"}
    assert found(heading_contains="chapter") == {"c_ops_7"}
    assert found(heading_contains="CHAPTER 7") == {"c_ops_7"}
    assert found(heading_contains="没这一章") == set()

    # 空条件 = 不过滤（与"没传"逐字一致）
    assert fulltext_store.search(
        query="检索", top_k=10, filter=SearchFilter()
    ) == fulltext_store.search(query="检索", top_k=10)
