"""存储维护端点（v17）。

镜像同构：``app/api/v1/maintenance.py`` → 本文件。

钉住三件事：
1. 删知识库会**连向量分区表一起丢掉**——原先只删行，表永远留着（实测在真实库里
   发现过一个无主分区 `vec_kb_2319aa4125df`，而分区只要写过第一个向量就占 4MB）；
2. 「整理存储」只动无主数据，能回收空闲页；
3. 这两个端点都是管理员专属（与 `/tasks/health` 同一档）。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from tests.conftest import ADMIN_PASSWORD
from tests.conftest import admin_client as admin_session

MARKDOWN = "# 眼轴\n\n眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一。\n"
MEMBER_PASSWORD = "member pass 123"


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


def _drain_worker() -> None:
    import asyncio

    worker = get_services().worker

    async def drain() -> None:
        while await worker.run_once():
            pass

    asyncio.run(drain())


def _stores():  # type: ignore[no-untyped-def]
    """直达存储：本文件要断言的是"表还在不在"，那是存储层的事实。"""
    return get_services().auth._stores


def _create_kb(client: TestClient, name: str = "维护测试库") -> str:
    return client.post("/api/v1/knowledge-bases", json={"name": name}).json()["id"]


def _index_one(client: TestClient, kb_id: str) -> None:
    """上传并跑到 indexed：**有向量才会建向量分区**，这是本文件的前提。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "true"},
    )
    assert response.status_code == 202, response.text
    _drain_worker()


def test_overview_reports_space_and_partitions(client: TestClient) -> None:
    kb_id = _create_kb(client)
    _index_one(client, kb_id)

    body = client.get("/api/v1/maintenance/storage").json()

    assert body["partitions"] == 1
    assert body["orphans"] == []
    # 文件 = 有效数据 + 可回收空闲页：两个数字拆开，用户才知道"删了没变小"是为什么
    assert body["file_bytes"] > 0
    assert body["data_bytes"] + body["free_bytes"] == body["file_bytes"]


def test_deleting_a_kb_drops_its_vector_partition(client: TestClient) -> None:
    """回归用例：删库必须把分区表一起丢掉。

    原先只按文档清向量行，`vec_kb_*` 表本身留在库里——而 sqlite-vec 的分区
    只要写过第一个向量就占一个 4MB 块，于是"已删掉的库"继续占着磁盘。
    """
    kb_id = _create_kb(client)
    _index_one(client, kb_id)
    assert client.get("/api/v1/maintenance/storage").json()["partitions"] == 1

    response = client.delete(f"/api/v1/knowledge-bases/{kb_id}")

    assert response.status_code == 200, response.text
    after = client.get("/api/v1/maintenance/storage").json()
    assert after["partitions"] == 0
    assert after["orphans"] == []


def test_compact_drops_legacy_orphan_partitions(client: TestClient) -> None:
    """早期版本留下的孤儿分区：概览要能看见，「整理存储」要能清掉。

    造法就是直接建一个"没有对应知识库"的分区——真实库里正是这么留下来的。
    """
    _stores().vectors.ensure_partition("kb_orphan_x", dim=8)

    overview = client.get("/api/v1/maintenance/storage").json()
    assert overview["orphans"] == ["kb_orphan_x"]

    compacted = client.post("/api/v1/maintenance/compact").json()

    assert compacted["orphans"] == []
    assert compacted["partitions"] == 0
    # 再查一次：整理的结果落了盘，不只是响应里的数字
    assert client.get("/api/v1/maintenance/storage").json()["orphans"] == []


def test_compact_keeps_owned_partitions(client: TestClient) -> None:
    """整理**只动无主数据**：有主的向量一个不碰（否则它就成了危险按钮）。"""
    kb_id = _create_kb(client)
    _index_one(client, kb_id)

    compacted = client.post("/api/v1/maintenance/compact").json()

    assert compacted["partitions"] == 1
    assert compacted["orphans"] == []
    assert _stores().vectors.declared_dim(kb_id) is not None


def test_admin_only(client: TestClient) -> None:
    """普通成员看不到存储视角，更不能触发 VACUUM（它会重写整个库文件）。"""
    from app.models.enums import UserRole
    from app.services.auth import hash_password
    from app.storage.base import UserRecord

    _stores().meta.create_user(
        UserRecord(
            id="user_member",
            name="成员",
            username="member",
            password_hash=hash_password(MEMBER_PASSWORD),
            role=UserRole.MEMBER,
        )
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "member", "password": MEMBER_PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/maintenance/storage", headers=headers).status_code == 403
    assert client.post("/api/v1/maintenance/compact", headers=headers).status_code == 403
    assert ADMIN_PASSWORD  # 管理员侧的前提由 client fixture 保证
