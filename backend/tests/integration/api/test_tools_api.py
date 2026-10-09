"""工具目录端点 ``GET /tools``（v0.61）。

镜像同构：``app/api/v1/tools.py`` + ``app/api/v1/schemas.py`` 的 ``Tool*`` / ``SkillOut``
那一段 → 本文件。

要证的三件事：

1. **要凭据**（与 ``/api/v1`` 下的其余端点同一条口径）；
2. **每个工具一行**，且 ``items`` 的顺序就是 ``TOOL_NAMES`` 的登记顺序——界面摆出来
   的顺序与 MCP 客户端看到的应当一致（两边本来就是同一张表）；
3. **给人看的字段不为空**（``label`` / ``summary``）：它们直接渲染到屏幕上，
   空串就是界面上一个说不出是什么的条目。

``mcp_endpoint`` 单独三条：它**只有配了 ``KYLAB_MCP_PUBLIC_URL`` 才有**（没配时回
``None``，而不是拿 REST 基址拼一条端口错的相对地址——那条会被当成能连的地址去试）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.tools import TOOL_NAMES
from tests.conftest import admin_client as admin_session

#: 一条像样的 MCP 接入地址（局域网里那台机器，**另一个端口**）。**带尾斜杠**：
#: 用户很可能这么填，而摆出去的地址不该带着它。
MCP_URL = "http://192.168.31.18:8765/mcp/"


@pytest.fixture
def locked():
    """已初始化管理员、请求自带会话凭据的客户端（与 ``test_auth_api.py`` 同名同义）。"""
    with admin_session() as test_client:
        yield test_client


def test_catalogue_requires_credentials() -> None:
    """没有凭据就拿不到目录——这一条要的是"缺凭据"分支，所以刻意不带会话头。"""
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as anonymous:
        assert anonymous.get("/api/v1/tools").status_code == 401


def test_catalogue_lists_every_tool_in_registration_order(locked: TestClient) -> None:
    """一个工具一行、顺序跟着登记顺序，且每条都有非空的名字与说明。"""
    body = locked.get("/api/v1/tools").json()

    assert [item["name"] for item in body["items"]] == list(TOOL_NAMES)
    for item in body["items"]:
        assert item["label"].strip(), item["name"]
        assert item["summary"].strip(), item["name"]
        assert isinstance(item["requires_write"], bool), item["name"]


def test_catalogue_params_come_from_the_tool_schema(locked: TestClient) -> None:
    """参数**由 ``tool_definitions()`` 推出来**，不是这里另写的一份。

    验 ``search``：它的必填参数只有 ``query``，可选那几个（``top_k`` 之类）也都在。
    两处各写一份参数表必然相漂，而漂出来的形态是"界面提示的参数与实现要的对不上"。
    """
    body = locked.get("/api/v1/tools").json()
    search = next(item for item in body["items"] if item["name"] == "search")
    params = {param["name"]: param for param in search["params"]}

    assert "query" in params
    assert params["query"]["required"] is True
    assert params["top_k"]["required"] is False

    upload = next(item for item in body["items"] if item["name"] == "upload_document")
    assert {param["name"] for param in upload["params"]} == {
        "knowledge_base_id",
        "filename",
        "content_base64",
    }
    assert all(param["required"] for param in upload["params"])


def test_skill_is_the_bundled_one(locked: TestClient) -> None:
    """技能那一块报的是仓库里那份的身份（名字 / 路径 / 两条传输）。"""
    skill = locked.get("/api/v1/tools").json()["skill"]

    assert skill["name"] == "kylab-knowledge-base"
    assert skill["path"] == "skills/kylab-knowledge-base"
    assert skill["transports"] == ["mcp", "rest"]
    assert skill["summary"].strip()


def test_mcp_endpoint_is_none_without_a_public_url(locked: TestClient, monkeypatch) -> None:
    """没配 MCP 接入地址时**整个字段是 ``None``**——拼出来的地址会被当成能连的去试。"""
    from app.core.config import get_settings

    monkeypatch.setenv("KYLAB_MCP_PUBLIC_URL", "")
    get_settings.cache_clear()

    assert locked.get("/api/v1/tools").json()["mcp_endpoint"] is None


def test_mcp_endpoint_is_the_configured_url(monkeypatch) -> None:
    """配了就原样给出（去掉尾斜杠），且**不会被拼上 ``/mcp``**——它本身就是整条。"""
    from app.core.config import get_settings

    monkeypatch.setenv("KYLAB_MCP_PUBLIC_URL", MCP_URL)
    get_settings.cache_clear()
    try:
        with admin_session() as client:
            body = client.get("/api/v1/tools").json()
    finally:
        get_settings.cache_clear()

    assert body["mcp_endpoint"] == "http://192.168.31.18:8765/mcp"


def test_mcp_endpoint_does_not_reuse_the_rest_base(monkeypatch) -> None:
    """只配 REST 基址**不会**让 MCP 地址冒出来：那是另一个监听，端口不同。"""
    from app.core.config import get_settings

    monkeypatch.setenv("KYLAB_PUBLIC_BASE_URL", "http://192.168.31.18:8081")
    monkeypatch.setenv("KYLAB_MCP_PUBLIC_URL", "")
    get_settings.cache_clear()
    try:
        with admin_session() as client:
            body = client.get("/api/v1/tools").json()
    finally:
        get_settings.cache_clear()

    assert body["mcp_endpoint"] is None


def test_catalogue_is_not_narrowed_by_a_keys_tool_scope(locked: TestClient) -> None:
    """限了工具范围的钥匙**照样看得到全量目录**。

    目录回答的是"这个服务有什么"，不是"你能用什么"——后者由调用那一刻的
    ``check_tool`` 判。这里把这条边界钉住：若哪天有人顺手按钥匙范围收窄目录，
    界面就没法告诉用户"你还可以勾哪些"。
    """
    issued = locked.post(
        "/api/v1/api-keys",
        json={
            "name": "只给检索的钥匙",
            "permission": "readonly",
            "allowed_tool_names": ["search"],
        },
    ).json()

    body = locked.get(
        "/api/v1/tools", headers={"Authorization": f"Bearer {issued['token']}"}
    ).json()

    assert [item["name"] for item in body["items"]] == list(TOOL_NAMES)


def test_catalogue_carries_the_same_fingerprint_as_the_mcp_server(locked: TestClient) -> None:
    """目录里的 ``surface_fingerprint`` 与 MCP 那侧**必须是同一个数**（v0.62）。

    这个数的用处就是"两边一比"：控制台显示它、客户端从 ``serverInfo.version`` 里
    读到它，用户据此判断这次部署的契约有没有变过。两处各算一遍迟早对不上——
    所以这里直接钉"就是 ``services.tools.surface_fingerprint()`` 的输出"，
    并顺手验它确实出现在 MCP 的版本串里。
    """
    from app.core.config import get_settings
    from app.mcp_server.server import build_server
    from app.services.tools import surface_fingerprint

    fingerprint = surface_fingerprint()
    body = locked.get("/api/v1/tools").json()

    assert body["surface_fingerprint"] == fingerprint
    assert build_server().version == f"{get_settings().app_version}+mcp.{fingerprint}"
