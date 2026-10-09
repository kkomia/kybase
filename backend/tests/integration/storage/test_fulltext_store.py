"""``FullTextStore`` 的行为测试（集成）。

覆盖架构 §5 的全文召回路径：chunk 写入索引、中文可检索、按库收敛、与图片锚点回填。

需要 PostgreSQL 测试库（``KYLAB_TEST_DATABASE_URL``），未配置则整文件跳过。
"""

from app.models.enums import DataSourceKind, DocumentStage
from app.storage.base import (
    ChunkRecord,
    DocumentRecord,
    FullTextStore,
    KnowledgeBaseRecord,
    MetaStore,
)


def _chunk(chunk_id: str, ordinal: int, text: str, *, document_id: str = "doc_1",
           kb_id: str = "kb_1", image_ids: tuple[str, ...] = ()) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        knowledge_base_id=kb_id,
        part_id=None,
        ordinal=ordinal,
        text=text,
        content_hash=f"h-{chunk_id}",
        heading_path="第3章 > 3.2节",
        page=ordinal + 1,
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
    """架构 §5：元数据过滤要能按知识库收敛。"""
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
    """架构 §7：命中含图段落时要把图片引用带出来。"""
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
