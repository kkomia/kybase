"""数据生命周期：影响清单、级联删除、回收站（M6 / T6.3、T6.4）。

镜像同构：``app/services/lifecycle.py`` → 本文件。

**这是计划里欠得最久的一块**：存储层从 M1 就有回收站，但接口层连一个删除端点
都没有，于是回收站永远是空的、"保留 7 天"从未被验证过。用例围着那条链路转：
影响清单要准 → 删完索引与向量要干净 → 原文要能恢复 → 到期要连**磁盘文件**一起清。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.chunking import content_hash_of
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.lifecycle import TRASH_RETENTION_DAYS, LifecycleService
from app.storage.base import ChunkRecord, DocumentStage

DIM = 16

#: 播种时写进对象存储的原文。挂在 StoreBundle 上不行——
#: 它是 ``frozen=True, slots=True`` 的 dataclass，赋新属性会直接 TypeError（踩过）
ORIGINAL_BYTES = "这是原文内容，用于验证回收站恢复。".encode()


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=DIM)


@pytest.fixture
def lifecycle(bundle, embedder) -> LifecycleService:  # type: ignore[no-untyped-def]
    return LifecycleService(bundle)


@pytest.fixture
def seeded(bundle, embedder, document, kb):  # type: ignore[no-untyped-def]
    """一份已摄入的文档：原文在对象存储、切块在元数据与索引、向量在分区里。"""
    bundle.vectors.ensure_partition(kb.id, dim=DIM)

    path = bundle.objects.write("originals/test-doc.txt", ORIGINAL_BYTES)
    # 文档行已经由 document fixture 建好，这里补上原文路径与切块
    bundle.meta.set_setting(f"document.{document.id}.original_path", path)

    records = [
        ChunkRecord(
            chunk_id=f"{document.id}#00000",
            document_id=document.id,
            knowledge_base_id=kb.id,
            part_id=None,
            ordinal=0,
            text="第一块正文",
            content_hash=content_hash_of("第一块正文"),
        ),
        ChunkRecord(
            chunk_id=f"{document.id}#00001",
            document_id=document.id,
            knowledge_base_id=kb.id,
            part_id=None,
            ordinal=1,
            text="第二块正文",
            content_hash=content_hash_of("第二块正文"),
        ),
    ]
    bundle.meta.replace_chunks(document.id, records)
    bundle.fulltext.index_chunks(records)
    bundle.vectors.upsert_vectors(
        kb.id, items=[(r.chunk_id, embedder.embed([r.text])[0]) for r in records]
    )
    return bundle


# --------------------------------------------------------------------- 影响清单


def test_impact_of_document(lifecycle: LifecycleService, seeded, document) -> None:  # type: ignore[no-untyped-def]
    report = lifecycle.impact_of_document(document.id)

    assert report.kind == "document"
    assert report.name == document.name
    assert report.documents == 1
    assert report.chunks == 2
    assert report.restorable is True, "文档级删除应当可恢复"


def test_impact_of_knowledge_base_counts_everything(
    lifecycle: LifecycleService, seeded, kb, document
) -> None:  # type: ignore[no-untyped-def]
    """数字要具体——那是二次确认能有意义的前提。"""
    report = lifecycle.impact_of_knowledge_base(kb.id)

    assert report.kind == "knowledge_base"
    assert report.name == kb.name
    assert report.documents == 1
    assert report.chunks == 2
    assert report.document_names == [document.name], "要给样本文件名让人确认没删错库"


def test_knowledge_base_delete_is_not_restorable(
    lifecycle: LifecycleService, seeded, kb
) -> None:  # type: ignore[no-untyped-def]
    """知识库级删除涉及多份文档、多个对象与一个向量分区，
    恢复要重建的东西太多——**做一半的恢复比没有更危险**，所以明确标成不可恢复。"""
    assert lifecycle.impact_of_knowledge_base(kb.id).restorable is False


def test_impact_of_missing_targets_raise(lifecycle: LifecycleService) -> None:
    with pytest.raises(NotFoundError):
        lifecycle.impact_of_document("doc_不存在")
    with pytest.raises(NotFoundError):
        lifecycle.impact_of_knowledge_base("kb_不存在")


# --------------------------------------------------------------------- 删除文档


def test_delete_document_clears_index_and_vectors(
    lifecycle: LifecycleService, seeded, document, embedder, kb
) -> None:  # type: ignore[no-untyped-def]
    """**索引与向量必须立即清掉。**

    留着向量会让"已删除的文档仍能被检索到"——那是最难查的一类状态。
    """
    lifecycle.delete_document(document.id)

    with pytest.raises(NotFoundError):
        lifecycle.impact_of_document(document.id)
    hits = seeded.vectors.search(kb.id, query_vector=[0.0] * DIM, top_k=10)
    assert hits == [], "向量没清干净"


def test_delete_document_puts_original_in_trash(
    lifecycle: LifecycleService, seeded, document
) -> None:  # type: ignore[no-untyped-def]
    """原文是**不可再生**的（用户得重新上传），所以冷备而不是直接删。"""
    entry = lifecycle.delete_document(document.id)

    assert entry.document_id == document.id
    assert [item.id for item in lifecycle.list_trash()] == [entry.id]
    # 到期时间约在 7 天后
    delta = entry.expires_at - datetime.now(UTC)
    assert timedelta(days=TRASH_RETENTION_DAYS - 1) < delta <= timedelta(
        days=TRASH_RETENTION_DAYS
    )


def test_delete_document_without_original_still_deletes(
    lifecycle: LifecycleService, bundle, document, kb
) -> None:  # type: ignore[no-untyped-def]
    """原文本来就不在时不该报错，也**不该记一条永远恢复不了的回收站条目**。"""
    lifecycle.delete_document(document.id)

    assert lifecycle.list_trash() == []
    with pytest.raises(NotFoundError):
        lifecycle.impact_of_document(document.id)


def test_delete_missing_document_raises(lifecycle: LifecycleService) -> None:
    with pytest.raises(NotFoundError):
        lifecycle.delete_document("doc_不存在")


# --------------------------------------------------------------------- 恢复


def test_restore_brings_back_the_original(
    lifecycle: LifecycleService, seeded, document, bundle
) -> None:  # type: ignore[no-untyped-def]
    """恢复要**真的把原文找回来**，而不是只回一个 id。"""
    entry = lifecycle.delete_document(document.id)

    new_id, _ = lifecycle.restore(entry.id)

    restored = bundle.meta.get_document(new_id)
    assert restored is not None
    assert restored.name == document.name, "名字应当保留"
    assert restored.knowledge_base_id == document.knowledge_base_id, "要放回原来的库"
    path = bundle.meta.get_setting(f"document.{new_id}.original_path")
    assert path and bundle.objects.read(path) == ORIGINAL_BYTES, "原文内容不一致"


def test_restore_uses_a_new_id(
    lifecycle: LifecycleService, seeded, document
) -> None:  # type: ignore[no-untyped-def]
    """用新 id 而不是复用旧 id：旧 id 可能已经进了会话引用、任务记录、
    甚至用户收藏的链接，复用会让那些残留指向一个"内容一样但历史不同"的文档。"""
    entry = lifecycle.delete_document(document.id)

    new_id, _ = lifecycle.restore(entry.id)

    assert new_id != document.id


def test_restore_leaves_the_document_unindexed(
    lifecycle: LifecycleService, seeded, document, kb
) -> None:  # type: ignore[no-untyped-def]
    """**恢复的是"骨架 + 原文"，不是整个状态。**

    切块与向量在删除时就清掉了，所以恢复出来必须回到「已上传」——
    否则界面上会显示一个"已索引但没有切块"的文档，那是自相矛盾的状态。
    要重新摄入才有检索能力。
    """
    entry = lifecycle.delete_document(document.id)

    new_id, _ = lifecycle.restore(entry.id)

    assert seeded.meta.get_document(new_id).stage == DocumentStage.UPLOADED  # type: ignore[union-attr]
    assert seeded.meta.count_chunks(new_id) == 0


def test_restore_clears_the_trash_entry(
    lifecycle: LifecycleService, seeded, document
) -> None:  # type: ignore[no-untyped-def]
    entry = lifecycle.delete_document(document.id)

    lifecycle.restore(entry.id)

    assert lifecycle.list_trash() == [], "恢复后条目应当消失，否则回收站会重复列出"


def test_restore_missing_entry_raises(lifecycle: LifecycleService, seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        lifecycle.restore("trash_不存在")


def test_restore_after_objects_gone_is_an_explicit_error(
    lifecycle: LifecycleService, seeded, document, bundle
) -> None:  # type: ignore[no-untyped-def]
    """原文被清掉时要**明确报错**，而不是恢复出一个空文档。"""
    entry = lifecycle.delete_document(document.id)
    bundle.objects.delete(entry.storage_path)

    with pytest.raises(InvalidRequestError) as excinfo:
        lifecycle.restore(entry.id)
    assert "原文已不在" in str(excinfo.value)


# --------------------------------------------------------------------- 清理


def test_drop_trash_removes_the_file_too(
    lifecycle: LifecycleService, seeded, document, bundle
) -> None:  # type: ignore[no-untyped-def]
    """彻底删除要连磁盘上的原文一起删——只删数据库行会把对象存储
    变成只增不减的垃圾场，而用户以为"点了删除就没了"。"""
    entry = lifecycle.delete_document(document.id)
    assert bundle.objects.exists(entry.storage_path)

    lifecycle.drop_trash(entry.id)

    assert lifecycle.list_trash() == []
    assert not bundle.objects.exists(entry.storage_path), "磁盘上的原文还在"


def test_drop_missing_entry_raises(lifecycle: LifecycleService) -> None:
    with pytest.raises(NotFoundError):
        lifecycle.drop_trash("trash_不存在")


def test_purge_expired_removes_old_entries_and_files(
    lifecycle: LifecycleService, seeded, document, bundle
) -> None:  # type: ignore[no-untyped-def]
    """到期清理同样要删磁盘文件。**这是"保留 7 天"这条承诺的兑现处。**"""
    entry = lifecycle.delete_document(document.id)
    # 把到期时间提前，模拟 7 天过去（用存储层的 setter，不写裸 SQL）
    bundle.meta.set_trash_expiry(entry.id, datetime.now(UTC) - timedelta(days=1))

    removed = lifecycle.purge_expired_trash()

    assert removed == 1
    assert lifecycle.list_trash() == []
    assert not bundle.objects.exists(entry.storage_path), "到期后磁盘上的原文还在"


def test_purge_keeps_unexpired_entries(
    lifecycle: LifecycleService, seeded, document
) -> None:  # type: ignore[no-untyped-def]
    lifecycle.delete_document(document.id)

    assert lifecycle.purge_expired_trash() == 0
    assert len(lifecycle.list_trash()) == 1


# --------------------------------------------------------------------- 删除知识库


def test_delete_knowledge_base_removes_everything(
    lifecycle: LifecycleService, seeded, kb, document, bundle
) -> None:  # type: ignore[no-untyped-def]
    report = lifecycle.delete_knowledge_base(kb.id)

    assert report.documents == 1
    assert bundle.meta.get_knowledge_base(kb.id) is None
    assert bundle.meta.list_documents(kb.id) == []
    # 知识库级删除**不进回收站**（不可恢复）
    assert lifecycle.list_trash() == []


def test_delete_knowledge_base_missing_raises(lifecycle: LifecycleService) -> None:
    with pytest.raises(NotFoundError):
        lifecycle.delete_knowledge_base("kb_不存在")
