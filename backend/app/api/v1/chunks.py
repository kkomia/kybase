"""切块人工干预端点（调研报告 G3）。

三个动作：**改正文**（并重新向量化）、**禁用/恢复**、**删除**。
为什么单独一组：它们都在操作"单个块"这个资源，而 `/documents/{id}/chunks`
是"某文档的块列表"——父子资源分开更清楚，也避免 documents.py 继续膨胀。

**两套寻址，都在这里支持**：

1. ``/chunks/{chunk_id}`` —— 通用标识。**注意 chunk_id 里含 ``#``**
   （形如 ``doc_xxx#00000``，来自切分器），那是 URL 的片段分隔符：
   客户端必须先做百分号编码（``%23``），否则从 ``#`` 起被截断，
   请求会退化成一个畸形路径（实测踩到：404 说"文档不存在"，让人以为是路由写错了）。
2. ``/documents/{document_id}/chunks/by-ordinal/{ordinal}`` —— **推荐**。
   界面本来就同时持有文档 ID 与块序号，这两个参数都是 URL 安全的，
   不必让每个调用方都知道"chunk_id 里有特殊字符"这件事。

**权限**：读写。三个动作都会改变检索结果，只读密钥不该能做。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.auth import READ, WRITE, check_kb_scope, require_read, require_write
from app.api.v1.schemas import ChunkOut, ChunkToggleIn, ChunkUpdateIn
from app.core.exceptions import NotFoundError
from app.core.services import Services, get_services
from app.models.enums import ApiKeyPermission
from app.services.api_key import Caller

router = APIRouter(tags=["chunks"])


def _guard_by_chunk(
    services: Services, caller: Caller, chunk_id: str, *, need: ApiKeyPermission = READ
) -> None:
    """按块所属的知识库做范围判定。

    与文档端点同一个理由：密钥范围绑在知识库上，而这里只拿到 chunk_id，
    所以要先取出块、再判它属于哪个库。**顺序不能反**——反了会在块不存在时
    回 403 而不是 404，等于告诉越权探测者"这个 id 存在但你无权看"。
    """
    record = services.chunks.get(chunk_id)
    check_kb_scope(services, caller, [record.knowledge_base_id], need=need)


def _guard_by_ordinal(
    services: Services,
    caller: Caller,
    document_id: str,
    ordinal: int,
    *,
    need: ApiKeyPermission = READ,
) -> str:
    """按（文档, 序号）定位并判范围，返回 chunk_id。

    先判文档的库范围再找块：文档不存在时给 404、越界时给 403，
    两者不能在同一个响应里混起来。
    """
    document = services.documents.get(document_id)
    check_kb_scope(services, caller, [document.knowledge_base_id], need=need)
    for record in services.documents.list_chunks(document_id):
        if record.ordinal == ordinal:
            return record.chunk_id
    raise NotFoundError(f"文档 {document_id} 没有第 {ordinal} 块")


# --------------------------------------------------------------------- 按块 ID


@router.get("/chunks/{chunk_id}", response_model=ChunkOut, summary="切块详情")
def get_chunk(
    chunk_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ChunkOut:
    _guard_by_chunk(services, caller, chunk_id)
    return ChunkOut.model_validate(services.chunks.get(chunk_id))


@router.patch("/chunks/{chunk_id}", response_model=ChunkOut, summary="修改切块正文（会重新向量化）")
def update_chunk(
    chunk_id: str,
    payload: ChunkUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ChunkOut:
    """改正文并重新 embedding。

    三个动作里**唯一有副作用代价**的：要再调一次向量模型。
    所以界面上它应当是显式保存，而不是边打字边存。
    """
    _guard_by_chunk(services, caller, chunk_id, need=WRITE)
    return ChunkOut.model_validate(services.chunks.update_text(chunk_id, payload.text))


@router.put(
    "/chunks/{chunk_id}/disabled", response_model=ChunkOut, summary="禁用 / 恢复切块"
)
def toggle_chunk(
    chunk_id: str,
    payload: ChunkToggleIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ChunkOut:
    """禁用或恢复。

    用 ``PUT`` 而不是 ``POST``：这是幂等的状态设置——重复禁用同一个块结果一样，
    不会累积副作用。
    """
    _guard_by_chunk(services, caller, chunk_id, need=WRITE)
    return ChunkOut.model_validate(
        services.chunks.set_disabled(chunk_id, disabled=payload.disabled)
    )


@router.delete(
    "/chunks/{chunk_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除切块（连同索引与向量）",
)
def delete_chunk(
    chunk_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    _guard_by_chunk(services, caller, chunk_id, need=WRITE)
    services.chunks.delete(chunk_id)


# --------------------------------------------------------------------- 按（文档, 序号）


@router.get(
    "/documents/{document_id}/chunks/by-ordinal/{ordinal}",
    response_model=ChunkOut,
    summary="按文档与序号取切块（推荐：URL 安全）",
)
def get_chunk_by_ordinal(
    document_id: str,
    ordinal: int,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ChunkOut:
    chunk_id = _guard_by_ordinal(services, caller, document_id, ordinal)
    return ChunkOut.model_validate(services.chunks.get(chunk_id))


@router.patch(
    "/documents/{document_id}/chunks/by-ordinal/{ordinal}",
    response_model=ChunkOut,
    summary="按文档与序号改正文",
)
def update_chunk_by_ordinal(
    document_id: str,
    ordinal: int,
    payload: ChunkUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ChunkOut:
    chunk_id = _guard_by_ordinal(services, caller, document_id, ordinal, need=WRITE)
    return ChunkOut.model_validate(services.chunks.update_text(chunk_id, payload.text))


@router.put(
    "/documents/{document_id}/chunks/by-ordinal/{ordinal}/disabled",
    response_model=ChunkOut,
    summary="按文档与序号禁用 / 恢复",
)
def toggle_chunk_by_ordinal(
    document_id: str,
    ordinal: int,
    payload: ChunkToggleIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ChunkOut:
    chunk_id = _guard_by_ordinal(services, caller, document_id, ordinal, need=WRITE)
    return ChunkOut.model_validate(
        services.chunks.set_disabled(chunk_id, disabled=payload.disabled)
    )


@router.delete(
    "/documents/{document_id}/chunks/by-ordinal/{ordinal}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="按文档与序号删除切块",
)
def delete_chunk_by_ordinal(
    document_id: str,
    ordinal: int,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    chunk_id = _guard_by_ordinal(services, caller, document_id, ordinal, need=WRITE)
    services.chunks.delete(chunk_id)

