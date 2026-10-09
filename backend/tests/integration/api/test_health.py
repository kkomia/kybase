"""API 端到端：存活探针与路径版本化（集成测试）。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["api_version"] == "v1"
    assert body["app"] == "kylab"


def test_every_route_is_versioned(client: TestClient) -> None:
    """所有对外路径必须带版本前缀 ``/api/v1``（工程规范 §3.2）。"""
    paths = [route.path for route in app.routes if getattr(route, "methods", None)]
    assert paths, "未注册任何路由"
    unversioned = [path for path in paths if not path.startswith("/api/v1")]
    assert not unversioned, f"以下路径缺少版本前缀: {unversioned}"
