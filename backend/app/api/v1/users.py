"""使用者名册与账号管理端点（调研报告 G6；v10 起升级为账号体系）。

**名册不参与鉴权**：伪造一个名字不会获得任何权限，只会让归属记错。
所以读端点用 ``require_read``、写端点用 ``require_admin``——
名册是控制台级配置（和 API Key 同一档），而不是靠名字本身做安全边界。

v10 起管理员可以在这里**开通账号**（带 username/password 的创建）、
重置密码、禁用启用——这些动作走 ``AuthService``（口令哈希与会话吊销都在那）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.auth import require_admin, require_read, signing_secret
from app.api.v1.schemas import (
    UserCreateIn,
    UserDisabledIn,
    UserListOut,
    UserOut,
    UserPasswordIn,
)
from app.core.config import get_settings
from app.core.exceptions import InvalidRequestError
from app.core.services import Services, get_services
from app.models.enums import UserRole
from app.services.api_key import Caller
from app.services.users import OPERATOR_HEADER

router = APIRouter(prefix="/users", tags=["users"])


def _out(services: Services, record) -> UserOut:  # type: ignore[no-untyped-def]
    avatar_url, _expires = services.avatars.url_for(
        record, secret=signing_secret(get_settings(), services)
    )
    return UserOut(
        avatar_url=avatar_url,
        id=record.id,
        name=record.name,
        note=record.note,
        username=record.username,
        role=record.role.value,
        disabled=record.disabled,
        created_at=record.created_at,
        document_count=services.users.count_documents(record.id),
    )


@router.get("", response_model=UserListOut, summary="使用者名册")
def list_users(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_read)],
) -> UserListOut:
    return UserListOut(
        items=[_out(services, item) for item in services.users.list()],
        header=OPERATOR_HEADER,
    )


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="添加使用者 / 开通账号（带 username 即账号）",
)
def create_user(
    payload: UserCreateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> UserOut:
    if payload.username:
        # 开通账号必须有初始密码：不设密码的账号等于开着门的空房子
        if not payload.password:
            raise InvalidRequestError("开通账号需要设置初始密码")
        record = services.auth.create_account(
            name=payload.name,
            username=payload.username,
            password=payload.password,
            role=UserRole(payload.role),
            note=payload.note,
        )
        return _out(services, record)
    return _out(services, services.users.create(name=payload.name, note=payload.note))


@router.put(
    "/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="重置密码（吊销其全部会话）",
)
def reset_password(
    user_id: str,
    payload: UserPasswordIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> None:
    services.auth.reset_password(user_id, payload.password)


@router.put(
    "/{user_id}/disabled",
    response_model=UserOut,
    summary="禁用 / 启用账号（禁用即吊销全部会话）",
)
def set_disabled(
    user_id: str,
    payload: UserDisabledIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> UserOut:
    services.auth.set_disabled(user_id, payload.disabled)
    return _out(services, services.users.get(user_id))


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除使用者（其文档保留，归属置空）",
)
def delete_user(
    user_id: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> None:
    services.users.delete(user_id)
