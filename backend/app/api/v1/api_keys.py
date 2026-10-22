"""API Key 管理端点（ ）。

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
from app.core.exceptions import InvalidRequestError
from app.core.security import API_KEY_PREFIX
from app.core.services import Services, get_services
from app.services.api_key import Caller
from app.services.tools import TOOL_NAMES

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
        "allowed_tool_names": list(record.allowed_tool_names),
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "prefix": f"{API_KEY_PREFIX}{record.key_prefix}…" if record.key_prefix else "…",
    }


def _validate_tool_names(names: list[str]) -> None:
    """工具名必须在 :data:`TOOL_NAMES` 里——**非法值要拒绝，不能存下来**。

    存一个拼错的名字，后果不是"这把钥匙少了一个工具"，而是"用户以为授权成功了、
    实际上那个工具一次都调不动"——而报错要到使用时才出现，中间还隔着一次配置。
    所以在这里拒，并把**允许的名字全列出来**（用户据此能直接改对）。

    这条校验只能在协议层做：``services/api_key.py`` import 不了 ``TOOL_NAMES``
    （``services/tools.py`` 要 import 它，那样会成环）。
    """
    unknown = [name for name in names if name not in TOOL_NAMES]
    if unknown:
        raise InvalidRequestError(
            f"未知的工具名：{'、'.join(unknown)}（可用：{'、'.join(TOOL_NAMES)}）"
        )


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
    _validate_tool_names(payload.allowed_tool_names)
    issued = services.api_keys.create(
        name=payload.name,
        permission=payload.permission,
        knowledge_base_ids=payload.knowledge_base_ids,
        allowed_tool_names=payload.allowed_tool_names,
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
