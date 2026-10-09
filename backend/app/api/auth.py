"""请求级鉴权依赖（《架构设计 v0.2》§3.2）。

两种凭据，一个入口：

- **登录会话**（``kylab_st_`` 前缀）：Web 控制台用，用户名 + 密码换来；
- **API Key**：给外部程序用，绑定知识库范围 + 只读/读写。

为什么"进控制台"必须是一种比 API Key 更高的身份，而不是同一把钥匙的另一个权限档：
设置页就是 embedding / LLM 密钥的落点，而 ``base_url`` 可改——给了外部 API Key
就等于给了它一条"把你的 Bearer Token 转发到我的服务器"的路。所以管理员身份只能
来自登录会话里的角色。

三处刻意的决定：

1. **鉴权永远生效**（v0.11 取消控制台令牌与"没配凭据就放行"）。
   第一次打开时唯一能调的是 ``GET /auth/status`` 与 ``POST /auth/setup``——
   先建管理员账号，再谈别的；在此之前任何业务请求都 401。
   上一版在"还没配任何凭据"时把请求放行成本机控制台，而"还没配"与"忘了配"
   从代码上无法区分，等于"无账号即裸奔"。
2. **只在 ``/api/v1`` 下生效**。健康探针（``/health``）与前端静态资源不鉴权：
   前者是容器编排用来判断存活的，带上鉴权会让 readiness 探针误判。
3. **失败快、文案钝**。缺凭据与凭据无效都回 401，且不区分原因（见 api_key.py）。
4. **本机档短路**（M2 §4.1，2026-10-01）：`deployment=local` 时 `current_caller` 直接
   返回"本机主人"，不看请求头。理由见函数自身的说明——**第一条只对服务器档成立**，
   本机档本来就没有账号体系可验（`users` / `sessions` 不在本机库里）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header

from app.core.config import Settings, get_settings
from app.core.exceptions import ForbiddenError, ServiceUnavailableError, UnauthorizedError
from app.core.services import KbServices, Services, get_kb_services
from app.models.enums import ApiKeyPermission
from app.services.api_key import LOCAL_CALLER, READ, WRITE, Caller, resolve_caller
from app.services.auth import URL_SIGNING_SECRET_SETTING

logger = logging.getLogger(__name__)

__all__ = [
    "AdminDep",
    "CallerDep",
    "ReadDep",
    "WriteDep",
    "check_kb_scope",
    "current_caller",
]


def _bearer(authorization: str | None) -> str | None:
    """从 ``Authorization: Bearer <token>`` 里取令牌。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def current_caller(
    services: Annotated[KbServices, Depends(get_kb_services)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Caller:
    """解析调用主体。**没有凭据就是 401**，不存在"未启用鉴权"这条支路。

    **唯一一条例外是本机档**（M2 §4.1、v0.3 §8-1「本机运行时不设门禁」）：
    桌面壳起的边车只监听 127.0.0.1、跑在用户自己的机器上，能打到那个端口的就是这台
    机器的主人，所以直接短路成"本机主人"（见 `api_key.LOCAL_CALLER`）。
    它**不看 `Authorization`** ✗：本机档没有账号体系（`users` / `sessions` 两张表都不在
    本机库里），带着服务器那把令牌打本机也不该被当成成员——那不是本机的身份。

    服务器档一个字没改：缺凭据仍然 401，照样走 `resolve_caller`。
    """
    if settings.deployment == "local":
        return LOCAL_CALLER
    token = _bearer(authorization)
    if token is None:
        raise UnauthorizedError(
            "缺少凭据：请在请求头带上 Authorization: Bearer <会话令牌或 API Key>"
        )

    # 分流与校验都在服务层 `resolve_caller` 里：**MCP 走同一份判定**。
    # 两处各写一份的话，"同一串令牌在一个入口是管理员、在另一个是匿名"
    # 这类不一致不会有任何测试能同时看到。
    return resolve_caller(services, token)


CallerDep = Annotated[Caller, Depends(current_caller)]


def _check(
    services: KbServices,
    caller: Caller,
    *,
    need: ApiKeyPermission,
    kb_ids: list[str] | None = None,
) -> None:
    services.api_keys.check_access(caller, need=need, kb_ids=kb_ids)


def require_read(
    services: Annotated[KbServices, Depends(get_kb_services)], caller: CallerDep
) -> Caller:
    """只读端点。"""
    _check(services, caller, need=READ)
    return caller


def require_write(
    services: Annotated[KbServices, Depends(get_kb_services)], caller: CallerDep
) -> Caller:
    """写端点（上传、建库、删除、改设置）。"""
    _check(services, caller, need=WRITE)
    return caller


def require_admin(caller: CallerDep) -> Caller:
    """管理员专属端点（设置页、模型注册、API Key 管理、用户管理）。

    **必须是管理员会话**，不能只要求"读写权限"：
    这些端点里是 embedding / rerank / LLM 的密钥与外部服务地址，而地址可改——
    给了外部 API Key 就等于给它一条转发凭据的路。
    """
    if not caller.is_admin:
        raise ForbiddenError(
            "该操作需要管理员权限：外部 API Key 不能读写服务端凭据配置"
            "（否则改掉 base_url 就能截获你的 API Key）"
        )
    return caller


ReadDep = Annotated[Caller, Depends(require_read)]
WriteDep = Annotated[Caller, Depends(require_write)]
AdminDep = Annotated[Caller, Depends(require_admin)]


def check_kb_scope(
    services: KbServices,
    caller: Caller,
    kb_ids: list[str] | None,
    *,
    need: ApiKeyPermission = READ,
) -> None:
    """带库范围的作用域判定。

    不做成依赖，是因为它需要**已解析的请求体或路径参数**，而依赖拿不到；
    硬要拿就得把 body 模型也声明成依赖，签名会重复一遍。

    ``need``：写端点必须传 ``WRITE``——成员拿到的分享可能是只读档，
    只按"看得见"判定会让只读分享变成可写（回归测试钉在 test_visibility_api）。
    """
    services.api_keys.check_access(caller, need=need, kb_ids=kb_ids)


def signing_secret(settings: Settings, services: KbServices | Services) -> str | None:
    """下载签名用的密钥：专用密钥（库 / 环境变量），**没有别的来源**。

    都为 None 时返回 None，表示不允许签发——那条路径上没有任何凭据体系能提供
    "这条链接只该给他"的依据，与其发一条永远有效的链接，不如让调用方走需要鉴权的
    常规接口（API 层据此拒绝）。

    首次 ``POST /auth/setup`` 会生成一条落库，**本机档则在组合根生成一次**
    （见 ``services/auth.ensure_url_signing_secret`` 与 ``core/services.py`` 那一段；
    本机档没有初始化流程，不在装配时补这一下这条键就永远是空的）。

    **形参收两个根**（2026-10-08 拆组合根）：它只取共享底座的 `services.runtime`，
    而调用它的既有 KB 侧（文档下载）也有 Agent 侧（会话/笔记的图片与产物下载），
    两边给各自手上的根都对。写成 `KbServices | Services` 是为了让这条"只碰共享件"
    的事实留在签名上，而不是逼调用方多写一次 `.kb`。
    """
    try:
        stored = services.runtime.get(URL_SIGNING_SECRET_SETTING)
    except Exception:
        logger.warning("读取签名密钥失败，退回环境变量", exc_info=True)
        stored = None
    return stored or settings.url_signing_secret


def signing_secret_or_raise(settings: Settings, services: KbServices | Services) -> str:
    """要签名密钥；**没有就抛 503**（不是 401）。

    与 :func:`signing_secret` 的分工：那个返回 ``None`` 让调用方自己决定（有一处**故意**
    不报错：预览时没有密钥就退化成"只能下载"，见 ``api/v1/documents.py`` 的 binary 那条）；
    这个给"必须签发 / 必须校验"的那些端点用。形参同样收两个根，理由见上面那条。

    **为什么不是 401**（桌面壳里实测到的那次故障）：前端把 401 当"登录失效"并跳登录页
    ——那是设计。而"这台机器还没有下载签名密钥"根本与用户的凭据无关，是**我们这边没配好**：
    本机档（边车）没有初始化流程，密钥要么由组合根生成、要么这台机器真的没有可用的库。
    踢到登录页只会让人以为账号出了问题，而正确的下一步是"补配置 / 稍后重试"。
    503 正是这个语义（``ServiceUnavailableError``：这个部署现在没有这个能力）。
    """
    secret = signing_secret(settings, services)
    if not secret:
        raise ServiceUnavailableError(
            "尚未配置下载签名密钥：请在环境里配置 KYLAB_URL_SIGNING_SECRET，"
            "或让这台机器正常启动一次（本机档会在装配时生成一条并落库；"
            "服务器档由首次初始化生成）"
        )
    return secret
