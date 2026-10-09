"""负载面板端点（§12.115）。

用户看到的场景是"后台怎么这么慢"，而答案在任务列表里找不到——它可能在 CPU、
在并发槽位、在云端额度。这个端点把那些数一次给全。

用例钉住三件事：

1. **形状**：硬件 / 队列 / 额度三块都在，且都是数（前端直接画，不做二次推导）。
2. **队列口径**：排队的任务出现在 ``queue.pending`` 与按类型分布里。
3. **权限与 ``/tasks/health`` 同档**：成员看到的是 403——这是运维面，不是成员面。
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


def _upload(client: TestClient, kb_id: str, name: str) -> str:
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (name, io.BytesIO(f"# {name}\n\n正文。\n".encode()), "text/markdown")},
        params={"start": "true"},
    )
    assert response.status_code == 202, response.text
    return response.json()["document"]["id"]


def test_load_reports_all_three_blocks(client: TestClient) -> None:
    """三块缺一块，面板就得靠前端去猜；所以形状本身就是断言对象。"""
    body = client.get("/api/v1/tasks/load").json()

    assert set(body) == {"hardware", "queue", "quota", "sampled_at"}
    assert body["hardware"]["cpu_count"] >= 1
    assert body["hardware"]["memory_total_bytes"] > 0
    assert 0 <= body["hardware"]["memory_percent"] <= 100
    assert body["queue"]["slots"] >= 1
    assert body["quota"]["daily_quota"] > 0
    assert body["quota"]["parser_name"] == "MinerUCloudParser"


def test_queued_documents_show_up_in_the_depth(client: TestClient) -> None:
    """排队中的任务要能被数出来——"队列有多深"是"还要等多久"的唯一依据。"""
    kb = client.post("/api/v1/knowledge-bases", json={"name": "负载库"}).json()
    _upload(client, kb["id"], "排队一.md")
    _upload(client, kb["id"], "排队二.md")

    queue = client.get("/api/v1/tasks/load").json()["queue"]

    assert queue["pending"] == 2
    # 按类型分布：这两条是摄入任务（probe 之后那一环），不是出题
    assert queue["pending_by_kind"] == {"parse": 2}
    assert queue["oldest_pending_seconds"] is not None


def test_cpu_is_unknown_until_two_samples_are_far_enough_apart(client: TestClient) -> None:
    """CPU% 要两次采样之差，所以**连着问两次可能都还没有数**。

    这不是"有时没数"的 bug，是刻意的：间隔不够时编一个 0% 出来会被读成
    "机器很空闲"。隔过采样间隔再问，就必须有数。
    """
    first = client.get("/api/v1/tasks/load").json()["hardware"]["cpu_percent"]
    assert first is None or isinstance(first, float)

    time.sleep(0.3)  # > MIN_CPU_SAMPLE_GAP
    second = client.get("/api/v1/tasks/load").json()["hardware"]["cpu_percent"]

    assert isinstance(second, float)
    assert 0.0 <= second <= 100.0


def test_member_gets_403(client: TestClient) -> None:
    """与 ``/tasks/health`` 同一档：机器资源与运维参数不给成员看。

    成员账号直接落库造（与 ``test_visibility_api`` 同一套做法）——这里测的是**权限**，
    不是开通流程。
    """
    from app.core.security import hash_password
    from app.core.services import get_services
    from app.models.enums import UserRole
    from app.storage.base import UserRecord

    # 直连存储只为造账号（与 test_visibility_api 同一做法）：这里测权限，不测开通流程
    get_services().auth._stores.meta.create_user(
        UserRecord(
            id="user_load_member",
            name="成员",
            username="load_member",
            password_hash=hash_password("member pass 123"),
            role=UserRole.MEMBER,
        )
    )
    token = client.post(
        "/api/v1/auth/login", json={"username": "load_member", "password": "member pass 123"}
    ).json()["token"]

    response = client.get("/api/v1/tasks/load", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
