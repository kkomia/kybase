"""下载签名密钥的取用（《架构设计 v0.2》§3.2）。

签发与校验本身在 ``app/core/signing.py``（纯函数，只认密钥）；**"密钥从哪儿来"**
是这里的事——库里那条（首次 ``POST /auth/setup`` 生成）优先，其次环境变量
``KYLAB_URL_SIGNING_SECRET``。

**为什么住在 services/ 而不是 api/**（v0.61 搬家）：服务层也要签发链接——
``get_document_content`` 这个 MCP 工具会把一条绝对下载地址一并回给调用方，
而它住在 ``services/tools.py``。协议层与业务层都得用同一份实现，那就只能放
``services/``：反过来的话工具要么 import ``app.api.auth``（分层规则 L5 直接红），
要么把"密钥从哪儿取"再抄一遍——两处判定必然相漂，而漂出来的形态是**一条签不出
来的链接**或一份漏判的凭据。

``app/api/auth.py`` 仍然 re-export 这两个名字（``signing_secret`` /
``signing_secret_or_raise``）：端点原先就从那里 import，行为一字未改。
"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.core.services import Services
from app.services.auth import URL_SIGNING_SECRET_SETTING

__all__ = ["signing_secret", "signing_secret_or_raise"]

logger = logging.getLogger(__name__)


def signing_secret(settings: Settings, services: Services) -> str | None:
    """下载签名用的密钥：专用密钥（库 / 环境变量），**没有别的来源**。

    都为 None 时返回 None，表示不允许签发——那条路径上没有任何凭据体系能提供
    "这条链接只该给他"的依据，与其发一条永远有效的链接，不如让调用方走需要鉴权的
    常规接口（API 层据此拒绝）。

    首次 ``POST /auth/setup`` 会生成一条落库。
    """
    try:
        stored = services.runtime.get(URL_SIGNING_SECRET_SETTING)
    except Exception:
        logger.warning("读取签名密钥失败，退回环境变量", exc_info=True)
        stored = None
    return stored or settings.url_signing_secret


def signing_secret_or_raise(settings: Settings, services: Services) -> str:
    """要签名密钥；**没有就抛 503**（不是 401）。

    与 :func:`signing_secret` 的分工：那个返回 ``None`` 让调用方自己决定（有一处**故意**
    不报错：预览时没有密钥就退化成"只能下载"，见 ``api/v1/documents.py`` 的 binary 那条）；
    这个给"必须签发 / 必须校验"的那些端点用。

    **为什么不是 401**：前端把 401 当"登录失效"并跳登录页——那是设计。而"还没有下载
    签名密钥"根本与用户的凭据无关，是**我们这边没配好**（``POST /auth/setup`` 还没跑过）。
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
