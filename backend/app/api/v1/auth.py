"""认证端点：账号引导、登录与会话。

**为什么"进控制台"只能靠账号**：设置页与密钥管理里是服务端的密钥与地址，
必须是一种比 API Key 更高的身份。v0.11 之前这个身份还有第二种来源——控制台令牌
（一个由服务端生成、明文存库的字符串），现在整条取消：它和账号体系并存时，
"我以为我是谁"与"后端认为我是谁"会分叉，而且它本身没有归属、无法审计。

于是入口只剩两个：

- ``POST /auth/setup``：**一次性**创建管理员（只在还没有任何账号时开放），
  同时认领无主老数据、生成下载签名密钥；
- ``POST /auth/login``：换会话令牌（滑动续期），之后一切凭据都从它派生。

``GET /auth/status`` 不鉴权：前端靠它判断"该显示首次设置还是登录"。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field

from app.api.auth import CallerDep, signing_secret
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.services import Services, get_services
from app.services.auth import MIN_PASSWORD_CHARS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthStatusOut(BaseModel):
    """前端据此决定显示"首次设置管理员"还是"登录"。"""

    needs_setup: bool
    """还没有任何可登录账号。为真时控制台进入首次设置向导。"""


# ---------------------------------------------------------------------- 账号（v10）


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD_CHARS)
    name: str | None = Field(default=None, max_length=32)
    """显示名；留空用用户名。"""


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class AccountOut(BaseModel):
    id: str
    username: str
    name: str
    role: str
    avatar_url: str = ""
    """头像链接（签名 URL，v0.29）。空 = 没有头像 → 界面用名字生成默认头像。

    **为什么是签名链接而不是裸路径**：``<img src>`` 带不了 Authorization 头，
    而用户 id 是可枚举的（``user_xxx``）——不签名的话，一个 id 就能把别人的脸
    拉下来（见 ``services/avatars.py``）。"""


class LoginOut(BaseModel):
    """登录/初始化成功。``token`` 只在这一次响应里出现。"""

    token: str
    user: AccountOut


def _account_out(services: Services, user) -> AccountOut:  # type: ignore[no-untyped-def]
    url, _expires = services.avatars.url_for(user, secret=signing_secret(get_settings(), services))
    return AccountOut(
        id=user.id,
        username=user.username or "",
        name=user.name,
        role=user.role.value,
        avatar_url=url,
    )


@router.get("/status", response_model=AuthStatusOut, summary="认证状态（是否需初始化）")
def status(services: Annotated[Services, Depends(get_services)]) -> AuthStatusOut:
    """**不鉴权**：前端要靠它判断该显示首次设置界面还是登录界面。

    只回一个布尔值，不透露任何可用信息。
    """
    try:
        needs_setup = not services.auth.has_accounts()
    except Exception:
        # 存储层抽风时按"已有账号"处理：误开初始化入口是后门，
        # 误关只是让用户走登录页（他本来就有账号）
        logger.warning("检查账号状态失败，按已有账号处理", exc_info=True)
        needs_setup = False
    return AuthStatusOut(needs_setup=needs_setup)


@router.post("/setup", response_model=LoginOut, summary="首次初始化：创建管理员账号")
def setup(
    payload: SetupIn,
    services: Annotated[Services, Depends(get_services)],
) -> LoginOut:
    """仅当**还没有任何账号**时开放（服务层把关），首个账号即管理员，
    无主老数据（v10 之前的库与会话）认领给它。"""
    result = services.auth.setup(
        username=payload.username, password=payload.password, name=payload.name
    )
    return LoginOut(token=result.token, user=_account_out(services, result.user))


@router.post("/login", response_model=LoginOut, summary="登录（用户名 + 密码）")
def login(
    payload: LoginIn,
    services: Annotated[Services, Depends(get_services)],
) -> LoginOut:
    """换一条会话令牌（7 天滑动续期）。连续失败会被限流（服务层）。"""
    result = services.auth.login(username=payload.username, password=payload.password)
    return LoginOut(token=result.token, user=_account_out(services, result.user))


@router.post("/logout", status_code=204, summary="退出登录（吊销当前会话）")
def logout(
    caller: CallerDep,
    services: Annotated[Services, Depends(get_services)],
) -> None:
    # 只有会话能"退出"：API Key 要走它自己的撤销端点
    if caller.session_id is None:
        raise UnauthorizedError("当前凭据不是登录会话，无需退出")
    services.auth.logout(caller.session_id)


@router.get("/me", response_model=AccountOut, summary="当前登录账号")
def me(
    caller: CallerDep,
    services: Annotated[Services, Depends(get_services)],
) -> AccountOut:
    """前端启动时用它恢复身份。API Key 通道没有账号，回 401。"""
    if caller.user is None:
        raise UnauthorizedError("当前凭据不是登录会话")
    return _account_out(services, caller.user)


# ---------------------------------------------------------------------- 头像（v0.29）
#
# 三件事都只作用于**自己**：头像是个人的，管理员改别人的头像没有正当理由
# （名册那边的用户管理是另一件事：停用、改密、删账号）。
#
# 图片本体不走这里，走 `GET /avatars/{user_id}`（带签名的链接，见 services/avatars.py）——
# `<img src>` 带不了 Authorization 头。


@router.post("/avatar", response_model=AccountOut, summary="换一张头像（上传图片）")
async def upload_avatar(
    file: Annotated[UploadFile, File(description="图片；前端会先缩到 256px 再传")],
    caller: CallerDep,
    services: Annotated[Services, Depends(get_services)],
) -> AccountOut:
    """只认图片（按**魔数**认，不看声明的 content-type）。返回更新后的账号。"""
    if caller.user is None:
        raise UnauthorizedError("当前凭据不是登录会话，不能设置头像")
    services.avatars.save(caller.user.id, await file.read())
    fresh = services.users.get(caller.user.id)
    return _account_out(services, fresh)


@router.delete("/avatar", response_model=AccountOut, summary="去掉头像")
def clear_avatar(
    caller: CallerDep,
    services: Annotated[Services, Depends(get_services)],
) -> AccountOut:
    """回到"用名字生成的默认头像"。没有头像时也成功——它要的是结果，不是过程。"""
    if caller.user is None:
        raise UnauthorizedError("当前凭据不是登录会话，不能设置头像")
    services.avatars.clear(caller.user.id)
    return _account_out(services, services.users.get(caller.user.id))


class PasswordChangeIn(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=MIN_PASSWORD_CHARS)


class PasswordChangeOut(BaseModel):
    revoked_sessions: int
    """改密后被吊销的其他会话数（当前这条保住）。"""


@router.post("/password", response_model=PasswordChangeOut, summary="修改自己的密码")
def change_password(
    payload: PasswordChangeIn,
    caller: CallerDep,
    services: Annotated[Services, Depends(get_services)],
) -> PasswordChangeOut:
    if caller.user is None or caller.session_id is None:
        raise UnauthorizedError("当前凭据不是登录会话，无法改密")
    revoked = services.auth.change_password(
        caller.user,
        old_password=payload.old_password,
        new_password=payload.new_password,
        keep_session_id=caller.session_id,
    )
    return PasswordChangeOut(revoked_sessions=revoked)
