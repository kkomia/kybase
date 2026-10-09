"""领域异常到 HTTP 响应的映射（集成）。

这是前端依赖的错误信封契约：``{"code": ..., "message": ...}``，
且 HTTP 状态码必须与异常类型对应。

**旁路也要覆盖**：FastAPI 自带的请求校验错误与 Starlette 的 ``HTTPException``
（未匹配路由、手工抛出）默认是 ``{"detail": ...}``，形状完全不同。对接方按 ``code``
分支时那两种会拿到 ``undefined``，所以它们一并注册成同一信封，这里逐条钉住。
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    KylabError,
    NotFoundError,
    PayloadTooLargeError,
    UnauthorizedError,
    UnsupportedContentError,
    UpstreamError,
    register_exception_handlers,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError("知识库不存在")

    @app.get("/conflict")
    async def _conflict() -> None:
        raise ConflictError("检测到相同文件")

    @app.get("/invalid")
    async def _invalid() -> None:
        raise InvalidRequestError("top_k 必须大于 0")

    @app.get("/generic")
    async def _generic() -> None:
        raise KylabError()

    @app.get("/too-large")
    async def _too_large() -> None:
        raise PayloadTooLargeError("文件超过 10MB 上限")

    @app.get("/unauthorized")
    async def _unauthorized() -> None:
        raise UnauthorizedError()

    @app.get("/forbidden")
    async def _forbidden() -> None:
        raise ForbiddenError("不能碰别人的库")

    @app.get("/unsupported")
    async def _unsupported() -> None:
        raise UnsupportedContentError("该文档还没有 Markdown 产物")

    @app.get("/upstream")
    async def _upstream() -> None:
        raise UpstreamError("解析节点返回 500")

    @app.get("/http-exception")
    async def _http_exception() -> None:
        # 模拟"没走领域异常"的旁路（Starlette 手工抛出）
        raise HTTPException(status_code=418, detail="我是茶壶")

    @app.get("/needs-query")
    async def _needs_query(q: int) -> dict[str, int]:
        return {"q": q}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "status", "code", "message"),
    [
        ("/not-found", 404, "not_found", "知识库不存在"),
        ("/conflict", 409, "conflict", "检测到相同文件"),
        ("/invalid", 422, "invalid_request", "top_k 必须大于 0"),
        ("/generic", 500, "internal_error", "服务内部错误"),
        ("/too-large", 413, "payload_too_large", "文件超过 10MB 上限"),
        ("/unauthorized", 401, "unauthorized", "缺少或无效的凭据"),
        ("/forbidden", 403, "forbidden", "不能碰别人的库"),
        ("/unsupported", 409, "unsupported_content", "该文档还没有 Markdown 产物"),
        ("/upstream", 502, "upstream_error", "解析节点返回 500"),
        # 旁路：Starlette 的 HTTPException 也要折成同一形状
        ("/http-exception", 418, "internal_error", "我是茶壶"),
    ],
)
def test_error_envelope(
    client: TestClient, path: str, status: int, code: str, message: str
) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert response.json() == {"code": code, "message": message}


def test_unauthorized_carries_www_authenticate(client: TestClient) -> None:
    """RFC 7235：401 必须带 WWW-Authenticate，否则客户端不知道用哪种方案。"""
    response = client.get("/unauthorized")
    assert response.headers.get("www-authenticate") == 'Bearer realm="kylab"'


def test_validation_error_uses_the_same_envelope(client: TestClient) -> None:
    """请求体/查询参数校验失败原本是 ``{"detail": [...]}``，现在也是同一信封。"""
    response = client.get("/needs-query", params={"q": "不是数字"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    # message 要能定位到字段（只给一句"参数不合法"等于没给）
    assert "q" in body["message"]


def test_unknown_route_uses_the_same_envelope(client: TestClient) -> None:
    """未匹配到路由的 404 也走统一信封——它同样是调用方会遇到的响应。"""
    response = client.get("/no-such-route")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
