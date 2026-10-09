"""知识库内目录：服务的建/列/改名/删与移动（v13）。

镜像同构：``app/services/folder.py`` → 本文件。

要紧的三条：**同库重名要被拒**（否则树里两个同名目录无法区分）、
**非空目录不许删**（悄悄把文件挪回根比"删不掉"更让人困惑）、
**目录必须属于同一个库**（跨库挂载会让文档在按目录查时凭空消失）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.models.enums import DataSourceKind, DocumentStage
from app.services.folder import FOLDER_NAME_MAX_CHARS, FolderService
from app.storage.base import DocumentRecord, KnowledgeBaseRecord


@pytest.fixture
def service(bundle) -> FolderService:  # type: ignore[no-untyped-def]
    return FolderService(bundle)


def _document(store, kb_id: str, document_id: str = "doc_1") -> DocumentRecord:  # type: ignore[no-untyped-def]
    return store.create_document(
        DocumentRecord(
            id=document_id,
            knowledge_base_id=kb_id,
            name=f"{document_id}.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash=f"hash-{document_id}",
            stage=DocumentStage.UPLOADED,
        )
    )


def test_create_and_list_sorted_by_name(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    service.create(kb.id, "合同")
    service.create(kb.id, "annual")
    service.create(kb.id, "发票")

    names = [item.name for item in service.list(kb.id)]

    # 大小写不敏感按名字排（人找目录是按名字找）
    assert names == ["annual", "发票", "合同"]


def test_duplicate_name_is_rejected(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    service.create(kb.id, "合同")

    with pytest.raises(ConflictError, match="已存在"):
        service.create(kb.id, "合同")


def test_blank_and_overlong_names_are_rejected(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidRequestError, match="不能为空"):
        service.create(kb.id, "   ")
    with pytest.raises(InvalidRequestError, match="不能超过"):
        service.create(kb.id, "长" * (FOLDER_NAME_MAX_CHARS + 1))


def test_name_whitespace_is_flattened(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    created = service.create(kb.id, "  2026   年\n合同 ")
    assert created.name == "2026 年 合同"


def test_create_on_missing_kb_is_404(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError, match="知识库不存在"):
        service.create("kb_nope", "合同")


def test_rename_detects_conflict_but_allows_same_name(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    first = service.create(kb.id, "合同")
    service.create(kb.id, "发票")

    # 改成自己原来的名字应当允许（幂等保存）
    assert service.rename(first.id, "合同").name == "合同"

    with pytest.raises(ConflictError, match="已存在"):
        service.rename(first.id, "发票")


def test_counts_only_containers_with_documents(service: FolderService, kb, store) -> None:  # type: ignore[no-untyped-def]
    folder = service.create(kb.id, "合同")
    empty = service.create(kb.id, "发票")
    _document(store, kb.id, "doc_a")
    _document(store, kb.id, "doc_b")
    service.move_document("doc_a", folder.id)

    counts = service.counts(kb.id)

    assert counts == {folder.id: 1}
    assert empty.id not in counts


def test_delete_empty_folder(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    folder = service.create(kb.id, "合同")

    service.delete(folder.id)

    assert service.list(kb.id) == []


def test_delete_non_empty_folder_is_refused_with_the_count(
    service: FolderService, kb, store
) -> None:  # type: ignore[no-untyped-def]
    """非空拒绝：消息里要有"还有几篇"，用户才知道下一步做什么。"""
    folder = service.create(kb.id, "合同")
    _document(store, kb.id, "doc_a")
    service.move_document("doc_a", folder.id)

    with pytest.raises(ConflictError, match="还有 1 篇"):
        service.delete(folder.id)


def test_move_document_to_folder_and_back_to_root(
    service: FolderService, kb, store
) -> None:  # type: ignore[no-untyped-def]
    folder = service.create(kb.id, "合同")
    _document(store, kb.id, "doc_a")

    service.move_document("doc_a", folder.id)
    assert store.get_document("doc_a").folder_id == folder.id

    service.move_document("doc_a", None)
    assert store.get_document("doc_a").folder_id is None


def test_move_rejects_a_folder_from_another_kb(
    service: FolderService, kb, store
) -> None:  # type: ignore[no-untyped-def]
    # 目录不能跨库挂载，否则文档按目录查时会"消失"
    other_kb = store.create_knowledge_base(
        KnowledgeBaseRecord(
            id="kb_other", name="别的库", embedding_model_id="m", embedding_dim=8
        )
    )
    foreign = service.create(other_kb.id, "别人的目录")
    _document(store, kb.id, "doc_a")

    with pytest.raises(InvalidRequestError, match="不属于"):
        service.move_document("doc_a", foreign.id)


def test_move_missing_document_is_404(service: FolderService, kb) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError, match="文档不存在"):
        service.move_document("doc_nope", None)
