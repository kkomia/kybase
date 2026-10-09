"""知识库内目录端点（v13）。

**为什么目录是知识库的属性而不是全局的**：目录回答"这个库里的文件怎么分类"，
跨库共用一套目录只会让它变成第二个知识库层级。所以路径挂在
``/knowledge-bases/{kb_id}/folders`` 下，成员/密钥的库范围判定沿用既有那一套。

写操作一律 ``require_write`` + ``check_kb_scope(need=WRITE)``——只读分享的成员
看得到目录，但不该能建/改/删，也不该能把文档挪来挪去（那会改掉 owner 的库结构）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.auth import check_kb_scope, require_read, require_write
from app.api.v1.documents import document_out
from app.api.v1.schemas import (
    DocumentFolderIn,
    DocumentOut,
    FolderCreateIn,
    FolderListOut,
    FolderOut,
    FolderRenameIn,
)
from app.core.services import Services, get_services
from app.services.api_key import WRITE, Caller

router = APIRouter(tags=["folders"])


def _folder_out(record, counts: dict[str, int]) -> FolderOut:  # type: ignore[no-untyped-def]
    return FolderOut(
        id=record.id,
        kb_id=record.kb_id,
        name=record.name,
        document_count=counts.get(record.id, 0),
        created_at=record.created_at,
    )


@router.get(
    "/knowledge-bases/{kb_id}/folders",
    response_model=FolderListOut,
    summary="知识库的目录列表（含每个目录的文档数）",
)
def list_folders(
    kb_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> FolderListOut:
    check_kb_scope(services, caller, [kb_id])
    counts = services.folders.counts(kb_id)
    return FolderListOut(
        items=[_folder_out(item, counts) for item in services.folders.list(kb_id)]
    )


@router.post(
    "/knowledge-bases/{kb_id}/folders",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建目录",
)
def create_folder(
    kb_id: str,
    payload: FolderCreateIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> FolderOut:
    check_kb_scope(services, caller, [kb_id], need=WRITE)
    record = services.folders.create(kb_id, payload.name)
    return _folder_out(record, services.folders.counts(kb_id))


@router.patch("/folders/{folder_id}", response_model=FolderOut, summary="重命名目录")
def rename_folder(
    folder_id: str,
    payload: FolderRenameIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> FolderOut:
    folder = services.folders.get(folder_id)
    check_kb_scope(services, caller, [folder.kb_id], need=WRITE)
    record = services.folders.rename(folder_id, payload.name)
    return _folder_out(record, services.folders.counts(record.kb_id))


@router.delete(
    "/folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除目录（非空则拒绝）",
)
def delete_folder(
    folder_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> None:
    folder = services.folders.get(folder_id)
    check_kb_scope(services, caller, [folder.kb_id], need=WRITE)
    services.folders.delete(folder_id)


@router.patch(
    "/documents/{document_id}/folder",
    response_model=DocumentOut,
    summary="把文档移进目录 / 移回根",
)
def move_document(
    document_id: str,
    payload: DocumentFolderIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> DocumentOut:
    """成员拿到只读分享时不该能移动文档——那会改掉 owner 的库结构。"""
    document = services.documents.get(document_id)
    check_kb_scope(services, caller, [document.knowledge_base_id], need=WRITE)
    services.folders.move_document(document_id, payload.folder_id)
    return document_out(services, services.documents.get(document_id))
