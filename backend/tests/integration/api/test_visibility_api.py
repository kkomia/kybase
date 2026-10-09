"""成员数据隔离的 HTTP 层行为（v10：私有 + 可分享）。

镜像同构：``app/api/v1/knowledge_bases.py`` / ``conversations.py`` / ``chat.py`` /
``tasks.py`` / ``stats.py`` 里的成员分支 → 本文件。

**这份用例钉的是"私有"两个字**：成员登录后，别人的知识库、文档任务、会话、
驾驶舱统计都不能露。每一条都先用管理员造出"别人的数据"，再拿成员的会话去够——
不先造出别人的数据，"看不到"就可能是"根本没有"（会骗人的绿）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.models.enums import UserRole
from app.storage.base import UserRecord

ADMIN_PASSWORD = "correct horse battery"
MEMBER_PASSWORD = "member pass 123"


@pytest.fixture
def two_users(monkeypatch):
    """一个管理员（经 setup）+ 一个普通成员（直接落库）的客户端。

    成员开通端点是步骤 5 的事；这里直接写库造账号，测的是**隔离**不是开通流程。
    """
    from app.core.config import get_settings
    from app.core.services import get_services
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        admin = client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": ADMIN_PASSWORD}
        ).json()
        # 通过组合根拿到存储：测试与 app 同进程同单例（conftest 的 reset_services 保证）
        meta = get_services().auth._stores.meta  # 测试需要直达存储造账号（开通端点是步骤 5）
        meta.create_user(
            UserRecord(
                id="user_member",
                name="成员",
                username="member",
                password_hash=hash_password(MEMBER_PASSWORD),
                role=UserRole.MEMBER
    )
        )
        member = client.post(
            "/api/v1/auth/login", json={"username": "member", "password": MEMBER_PASSWORD}
        ).json()
        yield client, admin, member
    get_settings.cache_clear()


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 知识库


def test_member_sees_only_own_knowledge_bases(two_users) -> None:  # type: ignore[no-untyped-def]
    client, admin, member = two_users
    client.post("/api/v1/knowledge-bases", json={"name": "管理员的库"}, headers=_as(admin["token"]))

    # 成员眼里是空的
    mine = client.get("/api/v1/knowledge-bases", headers=_as(member["token"])).json()["items"]
    assert mine == []

    # 成员自己建一个：归属自己，管理员照样看得见（is_admin 不受限）
    created = client.post(
        "/api/v1/knowledge-bases", json={"name": "成员的库"}, headers=_as(member["token"])
    )
    assert created.status_code == 201
    names = {
        item["name"]
        for item in client.get("/api/v1/knowledge-bases", headers=_as(admin["token"])).json()[
            "items"
        ]
    }
    assert names == {"管理员的库", "成员的库"}


def test_member_cannot_read_others_kb_detail(two_users) -> None:  # type: ignore[no-untyped-def]
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "机密库"}, headers=_as(admin["token"])
    ).json()

    response = client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=_as(member["token"]))
    assert response.status_code == 403


def test_member_cannot_upload_into_others_kb(two_users) -> None:  # type: ignore[no-untyped-def]
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "机密库"}, headers=_as(admin["token"])
    ).json()

    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("偷渡.md", b"# hello", "text/markdown")},
        headers=_as(member["token"])
    )
    assert upload.status_code == 403


# 摘掉的：会话那三条（2026-10-05）
#
# 原用例名与判据：
#
# - ``test_conversations_are_private``（成员列表里看不到别人的会话，详情/改名/删除
#   越主一律 404，自己的会话照常）；
# - ``test_member_cannot_chat_with_others_conversation``（拿别人的会话 id 提问 = 把整段
#   历史读走，必须 404）；
# - ``test_member_cannot_branch_from_others_conversation``（分叉 = 把整段历史复制一份，
#   所以越权比"看"更严重，必须 404，而且成员名下不该多出新会话）。
#
# **为什么在本机档没有意义**：**会话面只在本机档**（NAS 网页端退役，服务器档那张表里
# 只剩 `/conversations/export`），而本机档的主体恒为"本机主人"——不挂 `/auth/*` 与
# `/users`，`users` / `sessions` 两张表也不在本机库里（见 `api/v1/router.py` 里
# "明确不挂"那一段）。于是"别人的会话"这件事在两个档里都不存在可观察的形态：
# 服务器档没有会话端点，本机档没有第二个身份。
#
# 归属判定本身（`ConversationService.get_for_owner` 与
# `chat.py::_require_visible_conversation` 那份口径）**一个字没改**，
# 由 `tests/unit/services/test_conversation.py` 按服务层覆盖。


# --------------------------------------------------------------------- 任务与统计


def test_member_tasks_and_dashboard_are_scoped(two_users) -> None:  # type: ignore[no-untyped-def]
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "管理员的库"}, headers=_as(admin["token"])
    ).json()
    client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("私有.md", b"# secret", "text/markdown")},
        headers=_as(admin["token"])
    )

    # 成员的任务列表里没有别人文档的任务
    member_tasks = client.get("/api/v1/tasks", headers=_as(member["token"])).json()["items"]
    assert member_tasks == []
    admin_tasks = client.get("/api/v1/tasks", headers=_as(admin["token"])).json()["items"]
    assert len(admin_tasks) >= 1

    # 驾驶舱：成员看到的库数是 0，管理员是 1
    assert (
        client.get("/api/v1/stats/dashboard", headers=_as(member["token"])).json()[
            "total_knowledge_bases"
        ]
        == 0
    )
    assert (
        client.get("/api/v1/stats/dashboard", headers=_as(admin["token"])).json()[
            "total_knowledge_bases"
        ]
        == 1
    )

    # 运行态总览是管理员视角
    assert client.get("/api/v1/tasks/health", headers=_as(member["token"])).status_code == 403


def test_member_cannot_touch_console_endpoints(two_users) -> None:  # type: ignore[no-untyped-def]
    """密钥管理/用户名册对成员是 403（与外部 API Key 同一档待遇）。

    原来这一条里还有 ``/api/v1/settings``：它随 NAS 网页端退役**只在本机档**了
    （2026-10-05），服务器档上那是 404 而不是 403，所以从这张表里去掉了——
    管理员档那一半由 `api/v1/settings.py` 上的 ``WriteDep`` 自己保证，
    而它现在只挂在"没有第二个身份"的那一档上（见 `router.py` 里 `local_router` 那一段）。
    """
    client, _admin, member = two_users
    for method, path in (
        ("get", "/api/v1/api-keys"),
        ("post", "/api/v1/api-keys")
    ):
        response = getattr(client, method)(path, headers=_as(member["token"]))
        assert response.status_code == 403, f"{method} {path} 对成员放行了"


# --------------------------------------------------------------------- 正向路径（防"会骗人的绿"）
#
# 上面的用例只断言"成员看不到/够不着"。如果实现错写成"成员永远为空"，
# 那些断言照样绿——所以必须补"成员自己的东西照常工作"的正向用例。


def test_member_sees_own_task_and_dashboard_counts(two_users) -> None:  # type: ignore[no-untyped-def]
    client, _admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "成员的库"}, headers=_as(member["token"])
    ).json()
    client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("自己的.md", b"# mine", "text/markdown")},
        headers=_as(member["token"])
    )

    member_tasks = client.get("/api/v1/tasks", headers=_as(member["token"])).json()["items"]
    assert len(member_tasks) >= 1, "成员自己上传的任务必须可见"
    dashboard = client.get("/api/v1/stats/dashboard", headers=_as(member["token"])).json()
    assert dashboard["total_knowledge_bases"] == 1


# 摘掉的两条（2026-10-05）：``test_member_chats_with_own_conversation``（成员带自己的
# conversation_id 调 ``/chat`` 应正常）与
# ``test_admin_session_sees_conversations_from_other_channels``（管理员用网页会话要
# 看得到跨账号建的会话）——两条都打在**已经退役的会话链路**上（``/chat`` 与
# ``/conversations`` 都不在服务器档，而这一份是服务器档、带账号的那一份）。
# 本机档的对话由**边车**的 ``/turn*`` 承接，会话面的正向路径由
# `tests/integration/api/test_conversation_api.py`（本机档）覆盖。


# --------------------------------------------------------------------- 分享（v10）


def test_share_read_then_write_then_revoke(two_users) -> None:  # type: ignore[no-untyped-def]
    """完整的分享生命周期：授读 → 成员能看不能写 → 升档 → 能写 → 收回 → 回到不可见。"""
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "家庭相册"}, headers=_as(admin["token"])
    ).json()
    client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("相册.md", b"# photos", "text/markdown")},
        headers=_as(admin["token"])
    )
    member_h = _as(member["token"])

    # 授读：成员列表里出现这个库，详情能看，上传被挡
    granted = client.put(
        f"/api/v1/knowledge-bases/{kb['id']}/shares",
        json={"username": "member", "permission": "read"},
        headers=_as(admin["token"])
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["username"] == "member"

    listing = client.get("/api/v1/knowledge-bases", headers=member_h).json()["items"]
    names = [item["name"] for item in listing]
    assert names == ["家庭相册"]
    assert client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=member_h).status_code == 200
    assert (
        client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents",
            files={"file": ("偷传.md", b"# x", "text/markdown")},
            headers=member_h
    ).status_code
        == 403
    )

    # 升档为写：成员可以上传了（上传是异步摄入，受理回 202）
    client.put(
        f"/api/v1/knowledge-bases/{kb['id']}/shares",
        json={"username": "member", "permission": "write"},
        headers=_as(admin["token"])
    )
    assert (
        client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents",
            files={"file": ("成员补充.md", b"# ok", "text/markdown")},
            headers=member_h
    ).status_code
        == 202
    )

    # 收回：回到不可见
    shares = client.get(f"/api/v1/knowledge-bases/{kb['id']}/shares", headers=_as(admin["token"]))
    target = shares.json()["items"][0]["user_id"]
    assert (
        client.delete(
            f"/api/v1/knowledge-bases/{kb['id']}/shares/{target}", headers=_as(admin["token"])
        ).status_code
        == 204
    )
    assert client.get("/api/v1/knowledge-bases", headers=member_h).json()["items"] == []


def test_member_cannot_manage_shares_of_shared_kb(two_users) -> None:  # type: ignore[no-untyped-def]
    """被分享者（write 档）不能把库再授给别人：权限扩散止于 owner。"""
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "共享库"}, headers=_as(admin["token"])
    ).json()
    client.put(
        f"/api/v1/knowledge-bases/{kb['id']}/shares",
        json={"username": "member", "permission": "write"},
        headers=_as(admin["token"])
    )

    assert (
        client.put(
            f"/api/v1/knowledge-bases/{kb['id']}/shares",
            json={"username": "admin", "permission": "read"},
            headers=_as(member["token"])
    ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/knowledge-bases/{kb['id']}/shares", headers=_as(member["token"])
        ).status_code
        == 403
    )


def test_can_manage_flag_matches_share_rights(two_users) -> None:  # type: ignore[no-untyped-def]
    """``can_manage`` 是界面显示「分享」入口的依据，必须与"能不能管分享"一致。

    三档都覆盖：自己建的库 true、管理员看别人的库 true、被分享者看别人的库 false。
    最后一条最关键——否则界面上会出现一个点了必然 403 的按钮。
    """
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "家庭相册"}, headers=_as(admin["token"])
    ).json()
    admin_h, member_h = _as(admin["token"]), _as(member["token"])

    # 管理员（is_admin）看自己建的库：可管
    items = client.get("/api/v1/knowledge-bases", headers=admin_h).json()["items"]
    assert items[0]["can_manage"] is True

    # 授读之后成员能看到，但管不动分享
    client.put(
        f"/api/v1/knowledge-bases/{kb['id']}/shares",
        json={"username": "member", "permission": "read"},
        headers=admin_h
    )
    shared = client.get("/api/v1/knowledge-bases", headers=member_h).json()["items"]
    assert [item["name"] for item in shared] == ["家庭相册"]
    assert shared[0]["can_manage"] is False
    assert shared[0]["can_write"] is False
    detail = client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=member_h).json()
    assert detail["can_manage"] is False
    assert detail["can_write"] is False

    # 升到写档：看得见也写得动，但仍然管不了分享（权限扩散止于 owner）
    client.put(
        f"/api/v1/knowledge-bases/{kb['id']}/shares",
        json={"username": "member", "permission": "write"},
        headers=admin_h
    )
    upgraded = client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=member_h).json()
    assert upgraded["can_write"] is True
    assert upgraded["can_manage"] is False

    # 成员自己建的库：可管（详情与列表口径一致）
    own = client.post(
        "/api/v1/knowledge-bases", json={"name": "我的库"}, headers=member_h
    ).json()
    assert own["can_manage"] is True
    assert own["can_write"] is True
    assert (
        client.get(f"/api/v1/knowledge-bases/{own['id']}", headers=member_h).json()["can_manage"]
        is True
    )


def test_member_cannot_touch_trash(two_users) -> None:  # type: ignore[no-untyped-def]
    """回收站是控制台专属：里面有所有人删过什么的元信息，成员一律 403。"""
    client, admin, member = two_users
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "管理员的库"}, headers=_as(admin["token"])
    ).json()
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("待删.md", b"# t", "text/markdown")},
        headers=_as(admin["token"])
    ).json()
    trashed = client.delete(
        f"/api/v1/documents/{upload['document']['id']}", headers=_as(admin["token"])
    ).json()

    member_h = _as(member["token"])
    assert client.get("/api/v1/trash", headers=member_h).status_code == 403
    assert (
        client.post(f"/api/v1/trash/{trashed['id']}/restore", headers=member_h).status_code == 403
    )
    assert client.delete(f"/api/v1/trash/{trashed['id']}", headers=member_h).status_code == 403
