"""``app/storage/base.py`` 的单元测试。

镜像同构：``app/storage/base.py`` → ``tests/unit/storage/test_base.py``。
重点是守住"接口契约"本身：四个接口必须保持抽象、必须覆盖架构承诺的操作。
"""

import pytest

from app.models.enums import DataSourceKind, DocumentStage
from app.storage.base import (
    ChunkRecord,
    DocumentRecord,
    FullTextStore,
    KnowledgeBaseRecord,
    MetaStore,
    ObjectStore,
    VectorStore,
)

INTERFACES = (MetaStore, VectorStore, FullTextStore, ObjectStore)


@pytest.mark.parametrize("interface", INTERFACES)
def test_interfaces_cannot_be_instantiated(interface: type) -> None:
    """抽象基类不可直接实例化——实现必须显式补齐全部方法。"""
    with pytest.raises(TypeError):
        interface()  # type: ignore[abstract]


def test_vector_store_declares_partition_operations() -> None:
    """向量表按知识库分区、维度在建分区时确定（架构 §8.3 + M1 决策 D4）。"""
    expected = {"ensure_partition", "upsert_vectors", "delete_vectors", "search", "drop_partition"}
    assert expected <= VectorStore.__abstractmethods__


def test_meta_store_covers_architecture_commitments() -> None:
    """这些方法名对应架构里的关键承诺，缺一个就意味着某条承诺无处落地。"""
    required = {
        "get_document_by_hash",  # §6.3 文件 hash 去重
        "replace_chunks",  # §6.1 chunk 级增量更新
        "reclaim_expired_tasks",  # §4 断点续跑 / 超时回收
        "add_to_trash",  # §6.2 回收站 7 天
        "purge_expired_trash",
        "update_knowledge_base_embedding",  # §6.4 模型切换校验
    }
    assert required <= MetaStore.__abstractmethods__


def test_fulltext_and_object_store_minimum_contract() -> None:
    assert {"index_chunks", "delete_chunks", "search"} <= FullTextStore.__abstractmethods__
    assert {"write", "read", "exists", "move_to_trash"} <= ObjectStore.__abstractmethods__


def test_knowledge_base_carries_embedding_identity() -> None:
    """维度随库记录，而非全局常量——D4 的落地方式。"""
    kb = KnowledgeBaseRecord(
        id="kb_1", name="测试库", embedding_model_id="BAAI/bge-m3", embedding_dim=1024
    )
    assert kb.embedding_dim == 1024
    assert kb.chunk_strategy == "fixed"


def test_chunk_record_defaults_to_no_images() -> None:
    chunk = ChunkRecord(
        chunk_id="c1",
        document_id="d1",
        knowledge_base_id="kb_1",
        part_id=None,
        ordinal=0,
        text="正文",
        content_hash="h1",
    )
    assert tuple(chunk.image_ids) == ()
    assert chunk.page is None


def test_document_record_requires_stage() -> None:
    document = DocumentRecord(
        id="d1",
        knowledge_base_id="kb_1",
        name="a.pdf",
        source_kind=DataSourceKind.UPLOAD,
        content_hash="h1",
        stage=DocumentStage.UPLOADED,
    )
    assert document.stage is DocumentStage.UPLOADED
    assert document.is_split is False
