"""MCP 的身份解析（``app/mcp_server/auth.py``）。

**为什么单独测**：收口的判定在服务层（``ApiKeyService``，已有自己的用例），
这里测的是**凭据怎么从两条传输里取出来**——这一段出错的表现是"配了 Key 却报没身份"，
或者更糟："没配 Key 却放行了"。前者是排障体验，后者是越权。

纯函数部分**不依赖数据库**：取令牌是字符串处理，用 PG 之类的重依赖去测它
只会让这一层在 CI 里被跳过（本仓库的 PG 用例没有维护库时会 skip）。
中间件那一侧用一个替身 services——它验的是"取到令牌后怎么放、怎么还原、
拿不到时是否放行到工具层"，而不是 ``authenticate`` 本身。
"""

from __future__ import annotations

import pytest

from app.mcp_server.auth import (
    MCP_KEY_ENV,
    CallerMiddleware,
    current_caller,
    inbound_headers,
    token_from_env,
    token_from_headers,
)
from app.services.api_key import Caller

TOKEN = "klb_" + "x" * 40


# --------------------------------------------------------------------- 请求头


def test_reads_bearer_token() -> None:
    assert token_from_headers({"Authorization": f"Bearer {TOKEN}"}) == TOKEN


def test_header_name_is_case_insensitive() -> None:
    """**HTTP 规定头名大小写不敏感**，所以不能直接取 ``headers["Authorization"]``。

    实测踩过：客户端写成小写时取不到值，表现为"明明配了 Key 却说没有身份"，
    而错误信息还会把人引向"Key 无效"这个错误方向。
    """
    assert token_from_headers({"authorization": f"Bearer {TOKEN}"}) == TOKEN
    assert token_from_headers({"AUTHORIZATION": f"Bearer {TOKEN}"}) == TOKEN


def test_bearer_prefix_is_case_insensitive_and_trimmed() -> None:
    assert token_from_headers({"Authorization": f"  bearer   {TOKEN}  "}) == TOKEN


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer   "},
        {"Authorization": f"Basic {TOKEN}"},
        {"X-Other": f"Bearer {TOKEN}"},
    ],
)
def test_non_bearer_credentials_yield_no_token(headers: dict[str, str] | None) -> None:
    """认不出的凭据一律当作"没带"——**不能猜**，猜错就是放行。"""
    assert token_from_headers(headers) is None


# --------------------------------------------------------------------- 环境变量


def test_reads_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_KEY_ENV, TOKEN)
    assert token_from_env() == TOKEN


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_env_is_no_token(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(MCP_KEY_ENV, value)
    assert token_from_env() is None


def test_missing_env_is_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MCP_KEY_ENV, raising=False)
    assert token_from_env() is None


# --------------------------------------------------------------------- 当前主体


def test_current_caller_raises_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到身份就拒绝，**不返回 None**——见 auth.py 的说明。"""
    from app.core.exceptions import UnauthorizedError

    monkeypatch.delenv(MCP_KEY_ENV, raising=False)
    with pytest.raises(UnauthorizedError):
        current_caller()


class _StubKeys:
    """替身：只认一把令牌，其余交给 ``authenticate`` 的语义（抛 401）。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def authenticate(self, token: str) -> Caller:
        from app.core.exceptions import UnauthorizedError

        self.seen.append(token)
        if token != TOKEN:
            raise UnauthorizedError("API Key 无效或已被撤销")
        return Caller(is_admin=True)


class _StubServices:
    def __init__(self) -> None:
        self.api_keys = _StubKeys()


class _Ctx:
    def __init__(self, headers: dict[str, str] | None) -> None:
        self.headers = headers


@pytest.mark.asyncio
async def test_middleware_sets_and_restores_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """中间件在请求期间把 Caller 放进 ContextVar，**请求结束必须还原**。

    连接是长连：不还原就会把上一个人的身份留给下一个请求——
    那是最典型的串号。
    """
    monkeypatch.delenv(MCP_KEY_ENV, raising=False)
    services = _StubServices()
    middleware = CallerMiddleware(services)  # type: ignore[arg-type]
    seen: list[Caller] = []

    async def call_next(_ctx: object) -> str:
        seen.append(current_caller())
        return "ok"

    assert await middleware(_Ctx({"Authorization": f"Bearer {TOKEN}"}), call_next) == "ok"
    assert seen[0].is_admin is True
    assert services.api_keys.seen == [TOKEN]

    from app.core.exceptions import UnauthorizedError

    with pytest.raises(UnauthorizedError):
        current_caller()


@pytest.mark.asyncio
async def test_middleware_lets_handshake_through_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**无凭据不在这里拒绝**：这条中间件包着 ``initialize``，
    在那里抛错会让客户端连不上、也列不出工具，排障时没有线索。
    放行到工具层，由 ``call_tool`` 的 ``current_caller()`` 拒绝。
    """
    monkeypatch.delenv(MCP_KEY_ENV, raising=False)
    middleware = CallerMiddleware(_StubServices())  # type: ignore[arg-type]

    async def call_next(_ctx: object) -> str:
        return "handshake"

    assert await middleware(_Ctx(None), call_next) == "handshake"


@pytest.mark.asyncio
async def test_middleware_rejects_invalid_credentials_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据**存在但无效**要立刻拒——那是配置错了，越早说清越好，
    而不是让每个工具各报一次"没有身份"，把人引向错误的方向。
    """
    from app.core.exceptions import UnauthorizedError

    monkeypatch.delenv(MCP_KEY_ENV, raising=False)
    middleware = CallerMiddleware(_StubServices())  # type: ignore[arg-type]

    async def call_next(_ctx: object) -> str:  # pragma: no cover - 不该被调用
        raise AssertionError("无效凭据不应进入处理链")

    with pytest.raises(UnauthorizedError):
        await middleware(_Ctx({"Authorization": "Bearer 错的"}), call_next)


@pytest.mark.asyncio
async def test_stdio_uses_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio 没有请求头（SDK 的 ``ctx.headers`` 是 None），凭据只能来自环境变量。"""
    monkeypatch.setenv(MCP_KEY_ENV, TOKEN)
    middleware = CallerMiddleware(_StubServices())  # type: ignore[arg-type]
    seen: list[Caller] = []

    async def call_next(_ctx: object) -> str:
        seen.append(current_caller())
        return "ok"

    await middleware(_Ctx(None), call_next)
    assert seen[0].is_admin is True


# --------------------------------------------------------------- 入站请求头的来源


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _CtxWithRequest:
    """模拟真实 HTTP 路径：``ctx.headers`` 为空，头挂在 ``ctx.request`` 上。"""

    def __init__(self, headers: dict[str, str] | None) -> None:
        self.headers = None
        self.request = _Request(headers) if headers is not None else None


def test_inbound_headers_prefers_the_raw_request() -> None:
    """**回归用例**：实时 HTTP 路径上 ``ctx.headers`` 一直是 None。

    SDK 在有会话的 Streamable HTTP 路径构造 ``TransportContext`` 时没填 headers
    （只有单次交换路径填了），而 ``ctx.request`` 是原生 Starlette ``Request``，
    两条路径上都有。当时的症状是"配了 Key 却报没有身份"——
    如果哪天有人"顺手简化"成只读 ``ctx.headers``，这条会立刻失败。
    """
    headers = {"authorization": f"Bearer {TOKEN}"}
    assert inbound_headers(_CtxWithRequest(headers)) == headers
    assert token_from_headers(inbound_headers(_CtxWithRequest(headers))) == TOKEN


def test_inbound_headers_falls_back_to_ctx_headers() -> None:
    """没有原生请求时退回 ``ctx.headers``——另一条传输可能只有它。"""
    assert inbound_headers(_Ctx({"Authorization": "Bearer x"})) == {"Authorization": "Bearer x"}


def test_inbound_headers_without_any_source_is_none() -> None:
    assert inbound_headers(_CtxWithRequest(None)) is None
    assert inbound_headers(_Ctx(None)) is None


@pytest.mark.asyncio
async def test_middleware_reads_credentials_from_the_raw_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中间件要在**真实形状**的 ctx 上工作：头只在 ``ctx.request`` 里。

    只测 ``_Ctx``（``ctx.headers`` 有值）会漏掉这条路径——那正是收口第一次
    上线时失效的原因，端到端探针立刻发现了，但单元测试当时是绿的。
    """
    monkeypatch.delenv(MCP_KEY_ENV, raising=False)
    middleware = CallerMiddleware(_StubServices())  # type: ignore[arg-type]
    seen: list[Caller] = []

    async def call_next(_ctx: object) -> str:
        seen.append(current_caller())
        return "ok"

    ctx = _CtxWithRequest({"Authorization": f"Bearer {TOKEN}"})
    assert await middleware(ctx, call_next) == "ok"
    assert seen[0].is_admin is True
