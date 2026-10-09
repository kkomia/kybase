"""API Key 管理端点（M4 T4.4）。

**这些端点只认管理员**，不认 API Key：能签发钥匙的接口如果也能被钥匙打开，
那任何一把泄露的只读密钥都能给自己再发一把读写密钥——提权一步到位。

响应里绝不出现 ``key_hash``，明文也只在创建那一次出现（见 schemas 的说明）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.auth import require_admin
from app.api.v1.schemas import (
    ApiKeyCreateIn,
    ApiKeyIssuedOut,
    ApiKeyListOut,
    ApiKeyOut,
)
from app.core.security import API_KEY_PREFIX
from app.core.services import Services, get_services
from app.services.api_key import Caller

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _to_out(record) -> dict:  # type: ignore[no-untyped-def]
    """记录 → 响应字典。

    **列表与创建响应用同一套字段**，只有创建时额外多一个明文 ``token``。
    前缀来自库里存的 ``key_prefix``（创建时写入），所以列表里也能分辨是哪把。
    """
    return {
        "id": record.id,
        "name": record.name,
        "permission": record.permission,
        "knowledge_base_ids": list(record.knowledge_base_ids),
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "prefix": f"{API_KEY_PREFIX}{record.key_prefix}…" if record.key_prefix else "…",
    }


@router.get("", response_model=ApiKeyListOut, summary="API Key 列表")
def list_api_keys(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> ApiKeyListOut:
    return ApiKeyListOut(items=[ApiKeyOut(**_to_out(item)) for item in services.api_keys.list()])


@router.post(
    "",
    response_model=ApiKeyIssuedOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建 API Key（明文只在此响应出现）",
)
def create_api_key(
    payload: ApiKeyCreateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> ApiKeyIssuedOut:
    issued = services.api_keys.create(
        name=payload.name,
        permission=payload.permission,
        knowledge_base_ids=payload.knowledge_base_ids,
    )
    return ApiKeyIssuedOut(**_to_out(issued.record), token=issued.token)


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="撤销 API Key",
)
def revoke_api_key(
    key_id: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> None:
    services.api_keys.revoke(key_id)
