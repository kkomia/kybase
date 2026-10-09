"""知识库分享端点（v10）。

**谁能管**：库的 owner 或管理员。注意这里**不用** ``check_kb_scope``——
它只回答"你能不能看到这个库"，而管理分享要求"你是不是它的主人"：
被分享者（哪怕 write 档）能看到库，但不能把库再授给别人。

授权对象按 **username** 给出而不是 user id：成员没有权限列全量名册
（/users 是控制台级），界面让用户敲对方的登录名，名册全貌不外泄。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_read, require_write
from app.core.services import Services, get_services
from app.models.enums import SharePermission
from app.services.api_key import Caller
from app.services.share import ShareView

router = APIRouter(prefix="/knowledge-bases/{kb_id}/shares", tags=["shares"])


class ShareOut(BaseModel):
    user_id: str
    username: str
    name: str
    permission: SharePermission
    created_at: str | None


class ShareListOut(BaseModel):
    items: list[ShareOut]


class ShareGrantIn(BaseModel):
    username: str
    permission: SharePermission = SharePermission.READ


def _out(view: ShareView) -> ShareOut:
    return ShareOut(
        user_id=view.user_id,
        username=view.username,
        name=view.name,
        permission=view.permission,
        created_at=view.created_at.isoformat() if view.created_at else None,
    )


def _actor(caller: Caller) -> tuple[str | None, bool]:
    """（操作者账号 id, 是否管理员）。API Key 通道：actor None + is_admin 视角色而定。"""
    return (caller.user.id if caller.user else None), caller.is_admin


@router.get("", response_model=ShareListOut, summary="库的分享列表")
def list_shares(
    kb_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ShareListOut:
    actor_id, is_admin = _actor(caller)
    views = services.shares.list_for_kb(kb_id=kb_id, actor_id=actor_id, is_admin=is_admin)
    return ShareListOut(items=[_out(view) for view in views])


@router.put("", response_model=ShareOut, summary="分享/调整档位（按登录名）")
def grant_share(
    kb_id: str,
    payload: ShareGrantIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ShareOut:
    actor_id, is_admin = _actor(caller)
    view = services.shares.grant(
        kb_id=kb_id,
        username=payload.username,
        permission=payload.permission,
        actor_id=actor_id,
        is_admin=is_admin,
    )
    return _out(view)


@router.delete("/{user_id}", status_code=204, summary="收回分享")
def revoke_share(
    kb_id: str,
    user_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    actor_id, is_admin = _actor(caller)
    services.shares.revoke(kb_id=kb_id, user_id=user_id, actor_id=actor_id, is_admin=is_admin)
