"""MCP 的调用者身份：把凭据换成 ``Caller``，并让工具层能拿到它。

**为什么必须有这一层**：MCP 此前**完全没有身份**——``call_tool`` 的签名里没有调用者，
``_list_knowledge_bases`` 直接调 ``services.knowledge_bases.list_all()``，
``_search`` 留空库列表就等于查全部。实测（另起一个 HTTP 实例、不带任何请求头）
确认：无凭据即可列出真实知识库，而同一进程还挂着 ``delete_document``。
在"每个用户接自己的库"的个人助手形态下，这是不能接受的。

三种调用形态统一成一个 ``Caller``，**判定复用服务层已有的 ``ApiKeyService``**，
不另写一套（两套判定必然相漂，而"看得见但写不动"的错判正是越权洞的形状）：

- **HTTP**：请求头 ``Authorization: Bearer <API Key>``，每个请求各自解析；
- **stdio**：那条传输没有请求头（SDK 的 ``ctx.headers`` 在 stdio 下是 ``None``），
  凭据由客户端拉起本进程时放进环境变量 ``KYLAB_MCP_KEY``；
- **两者都没有 → 一律拒绝**，不做匿名放行。安全判定的默认值必须是"不通过"。

桥接手段用 ``ContextVar``：中间件在消息进入时 set，工具层在 ``call_tool`` 里 get。
之所以不把 Caller 塞进 ``ctx``——本项目的工具函数签名是从 JSON Schema 现造的
（见 ``server.py`` 的说明），拿不到 SDK 的 ``Context`` 对象。
``ContextVar`` 按任务隔离，并发请求之间不会串。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from app.core.exceptions import UnauthorizedError
from app.core.services import Services
from app.services.api_key import Caller, resolve_caller

__all__ = [
    "MCP_KEY_ENV",
    "CallerMiddleware",
    "current_caller",
    "inbound_headers",
    "token_from_env",
    "token_from_headers",
]

#: stdio 传输的凭据来源。客户端在 MCP 配置里写进 ``env`` 即可，
#: 与 HTTP 那侧的 ``Authorization`` 头用的是同一把 API Key。
MCP_KEY_ENV = "KYLAB_MCP_KEY"

_BEARER = "bearer "

_caller: ContextVar[Caller | None] = ContextVar("kylab_mcp_caller", default=None)


def current_caller() -> Caller:
    """取当前请求的调用者；取不到就拒绝。

    **刻意不返回 ``None`` 让调用方自己判**：那等于把"记得判"变成每个工具的纪律，
    而漏判不会报错、只会静默放行——这类洞是最难发现的一种。
    """
    caller = _caller.get()
    if caller is None:
        raise UnauthorizedError(
            "这次 MCP 调用没有身份。HTTP 请在请求头带上 "
            "Authorization: Bearer <API Key>；stdio 请在客户端的环境变量里设置 "
            f"{MCP_KEY_ENV}。Key 可在「设置 → API 密钥」新建。"
        )
    return caller


def token_from_headers(headers: Mapping[str, str] | None) -> str | None:
    """从请求头取 Bearer 令牌。

    头名**大小写不敏感**（HTTP 规定），所以逐个比对而不是直接取 ``headers["Authorization"]``
    ——后者在实测里会因为客户端写成小写而取不到，表现为"配了 Key 却说没身份"。
    """
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() != "authorization":
            continue
        text = (value or "").strip()
        if not text.lower().startswith(_BEARER):
            return None
        token = text[len(_BEARER) :].strip()
        return token or None
    return None


def token_from_env() -> str | None:
    """stdio 的凭据来自环境变量——那条传输没有请求头可读。"""
    value = (os.environ.get(MCP_KEY_ENV) or "").strip()
    return value or None


def inbound_headers(ctx: Any) -> Mapping[str, str] | None:
    """取入站请求头。

    **两个来源都要试**——这是实测得出的结论，不是保险起见：
    SDK 的 ``ctx.headers`` 在**有会话的 Streamable HTTP 路径上是 ``None``**。
    那条路径构造 ``TransportContext`` 时根本没填 headers
    （只有 2026-07-28 的单次交换路径填了，见 SDK 的 ``_streamable_http_modern.py``），
    而 ``ctx.request`` 是原生的 Starlette ``Request``，两条路径上都有。

    踩过一次才写下来：SDK 文档说 ``ctx.headers`` 是"stdio 下为 None"，
    读起来像是"HTTP 下一定有"——实际不是。当时的现象是"明明配了 Key 却报没有身份"，
    而中间件的调试输出里 headers 一直是 None。
    """
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers:
        return headers
    return getattr(ctx, "headers", None)


class CallerMiddleware:
    """每个入站消息解析一次凭据，把 ``Caller`` 放进 ContextVar。

    **为什么无凭据时不在这里拒绝**：这条中间件包着 ``initialize``。
    握手阶段就抛错会让客户端连不上、也列不出有哪些工具，排障时一点线索都没有。
    所以无凭据时**放行到工具层**——握手与 ``tools/list`` 正常，
    而每一个真正的工具调用都会在 ``call_tool`` 里被 ``current_caller()`` 拒掉。

    凭据**存在但无效**则在这里立刻拒绝：那是配置错了，越早说清越好——
    而不是让每个工具各报一次"没有身份"，把人引到错误的方向。
    """

    def __init__(self, services: Services) -> None:
        self._services = services

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        token = token_from_headers(inbound_headers(ctx)) or token_from_env()
        if token is None:
            return await call_next(ctx)

        caller = resolve_caller(self._services, token)
        reset = _caller.set(caller)
        try:
            return await call_next(ctx)
        finally:
            # 必须 reset：连接是长连，不还原会把身份留给下一个请求
            _caller.reset(reset)
