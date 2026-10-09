"""插件包端点的集成测试（v0.43）。

镜像同构：``app/api/v1/plugins.py`` → 本文件。

覆盖四件事：

1. **目录即市场**：把插件目录放进 ``<data_dir>/plugins/``，列表里就该出现它，
   并且要说得出"它提供了什么"；
2. **加载失败的照样列出**（带着原因）——静默藏掉会让用户以为插件没装上；
3. **启停写状态不写目录**，而且只有管理员能做；
4. 未知名字是 404（不是 500，也不是静默成功）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


def _write_plugin(directory, name: str, **manifest: object):  # type: ignore[no-untyped-def]
    """在真数据目录的 ``plugins/`` 下放一个插件（与用户手动放是同一条路）。"""
    target = get_services().plugins.user_dir / directory
    target.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"name": name}
    payload.update(manifest)
    (target / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
    return target


# --------------------------------------------------------------------- 列表


def test_lists_a_plugin_with_what_it_provides(client: TestClient) -> None:
    plugin = _write_plugin(
        "pack",
        "pack",
        version="1.2.0",
        description="示例插件",
        tools=[{"name": "导出", "description": "导出文档", "entry": "src/export.py"}],
    )
    (plugin / "commands").mkdir()
    (plugin / "commands" / "compact.md").write_text("# 压缩\n", encoding="utf-8")

    body = client.get("/api/v1/plugins").json()
    item = body["items"][0]

    assert body["total"] == 1
    assert body["enabled"] == 1
    assert body["failed"] == 0
    assert item["name"] == "pack"
    assert item["version"] == "1.2.0"
    assert item["source"] == "user"
    assert item["loaded"] is True
    assert item["enabled"] is True
    assert item["kinds"] == ["command", "tool"]
    assert [component["name"] for component in item["components"]] == ["compact", "导出"]


def test_list_says_where_the_market_is(client: TestClient) -> None:
    """两条发现源要回给界面：用户得看得见该把目录放到哪儿。"""
    body = client.get("/api/v1/plugins").json()

    assert body["user_dir"].endswith("plugins")
    assert body["builtin_dir"]  # 仓库布局下是 <仓库根>/plugins（存在与否都不影响这条）


def test_failed_plugin_is_still_listed_with_reason(client: TestClient) -> None:
    """缺 ``name`` 的插件不加载，但**要在列表里出现并说明原因**（照 DSH）。"""
    _write_plugin("broken", **{"name": ""})

    body = client.get("/api/v1/plugins").json()

    assert body["total"] == 1
    assert body["failed"] == 1
    assert body["items"][0]["loaded"] is False
    assert "缺 name" in body["items"][0]["error"]


def test_escaping_component_is_a_reason_not_a_crash(client: TestClient) -> None:
    _write_plugin("pack", "pack", tools=[{"name": "越界", "entry": "../../secret.txt"}])

    item = client.get("/api/v1/plugins").json()["items"][0]

    assert item["loaded"] is False
    assert "逃逸插件根" in item["error"]


def test_empty_market_is_empty_not_error(client: TestClient) -> None:
    body = client.get("/api/v1/plugins").json()

    assert body["items"] == []
    assert body["total"] == 0


# --------------------------------------------------------------------- 启停


def test_disable_then_enable_round_trip(client: TestClient) -> None:
    plugin = _write_plugin("pack", "pack")
    before = sorted(item.name for item in plugin.rglob("*"))

    disabled = client.post("/api/v1/plugins/pack/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["blocked"] is False  # 用户放的插件没有屏蔽一说
    assert client.get("/api/v1/plugins").json()["items"][0]["enabled"] is False
    assert client.get("/api/v1/plugins").json()["enabled"] == 0

    enabled = client.post("/api/v1/plugins/pack/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    # **插件目录一个字节都没动**（状态与内容分离）
    assert sorted(item.name for item in plugin.rglob("*")) == before


def test_disabling_a_builtin_records_a_block_and_keeps_it_listed(
    client: TestClient, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """内置插件被停用 = 记一条**屏蔽**（照 ZCode），它仍然在列表里看得见。"""
    builtin = tmp_path / "builtin"
    (builtin / "inner-pack").mkdir(parents=True)
    (builtin / "inner-pack" / "plugin.json").write_text(
        json.dumps({"name": "inner-pack"}), encoding="utf-8"
    )
    # 换掉内置目录要**重建服务**：路径在组合根构造时就定下来了
    monkeypatch.setenv("KYLAB_PLUGINS_DIR", str(builtin))
    get_services.cache_clear()
    try:
        item = client.post("/api/v1/plugins/inner-pack/disable").json()

        assert item["source"] == "builtin"
        assert item["blocked"] is True
        assert item["enabled"] is False
        assert (builtin / "inner-pack" / "plugin.json").is_file()  # 文件还在
        listed = client.get("/api/v1/plugins").json()
        assert [entry["name"] for entry in listed["items"]] == ["inner-pack"]
    finally:
        get_services.cache_clear()


def test_unknown_plugin_is_404(client: TestClient) -> None:
    response = client.post("/api/v1/plugins/nope/enable")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "nope" in response.json()["message"]


# --------------------------------------------------------------------- 权限


def test_list_needs_credentials() -> None:
    from app.main import create_app

    with TestClient(create_app()) as bare:
        assert bare.get("/api/v1/plugins").status_code == 401


def test_enable_and_disable_need_admin() -> None:
    """停用/启用是"会不会有代码被执行"的那一档，与 MCP、技能市场同一档。"""
    from app.core.security import hash_password
    from app.main import create_app
    from app.models.enums import UserRole
    from app.storage.base import UserRecord

    with TestClient(create_app()) as client:
        client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": "correct horse battery"}
        )
        get_services().auth._stores.meta.create_user(  # 测试直达存储造账号（同可见性那组）
            UserRecord(
                id="user_member",
                name="成员",
                username="member",
                password_hash=hash_password("member pass 123"),
                role=UserRole.MEMBER,
            )
        )
        token = client.post(
            "/api/v1/auth/login", json={"username": "member", "password": "member pass 123"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert client.get("/api/v1/plugins", headers=headers).status_code == 200
        assert client.post("/api/v1/plugins/pack/enable", headers=headers).status_code == 403
        assert client.post("/api/v1/plugins/pack/disable", headers=headers).status_code == 403
