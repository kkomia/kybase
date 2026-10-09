"""任务中心的批量取消（v24）。

用户看到的场景：几十条 pending 堵在队列里（一篇大 PDF 把单消费者占满），
而唯一的刹车是"逐篇取消文档"。这里钉住新入口的语义与权限。

三种任务要分开对待，这是本功能最要紧的一点：
- ``pending`` → 直接撤销，文档留在原阶段（**可逆**：还能重新入队）；
- ``running`` 且挂文档 → 连文档一起置 canceled（worker 在下一个阶段边界停手）；
- ``running`` 但没挂文档（数据源 / Wiki）→ 明确拒绝，不给"看起来取消了"的假象。
"""

from __future__ import annotations

import io

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


@pytest.fixture
def kb_id(client: TestClient) -> str:
    response = client.post("/api/v1/knowledge-bases", json={"name": "任务取消库"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client: TestClient, kb_id: str, name: str) -> tuple[str, str]:
    """上传并排一个摄入任务（测试里 worker 是关的，所以任务停在 pending）。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (name, io.BytesIO(f"# {name}\n\n正文。\n".encode()), "text/markdown")},
        params={"start": "true"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    return body["document"]["id"], body["task_id"]


def _tasks(client: TestClient) -> list[dict]:
    return client.get("/api/v1/tasks").json()["items"]


def _set_state(task_id: str, state: str) -> None:
    """把任务直接改成某个状态：worker 在测试里不跑，"正在跑"的状态只能这样造。"""
    from app.core.config import get_settings

    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute(
            "UPDATE tasks SET state = %s, lease_owner = 'test-owner' WHERE id = %s",
            (state, task_id),
        )


def test_cancel_a_named_pending_task(client: TestClient, kb_id: str) -> None:
    document_id, task_id = _upload(client, kb_id, "排队中.md")

    response = client.post("/api/v1/tasks/cancel", json={"task_ids": [task_id]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (1, 0)
    assert _tasks(client)[0]["state"] == "canceled"
    # 文档留在原阶段：这不是"取消文档"，撤下来之后还能重新入队
    detail = client.get(f"/api/v1/documents/{document_id}").json()
    assert detail["stage"] == "uploaded"


def test_cancel_all_pending_leaves_running_alone(client: TestClient, kb_id: str) -> None:
    """一键清空排队：只动 pending，正在跑的那条不在默认范围内。"""
    _upload(client, kb_id, "排队一.md")
    _, second = _upload(client, kb_id, "排队二.md")
    _set_state(second, "running")

    response = client.post("/api/v1/tasks/cancel", json={})

    assert response.status_code == 200, response.text
    assert response.json()["succeeded"] == 1
    states = {task["id"]: task["state"] for task in _tasks(client)}
    assert states[second] == "running"


def test_cancel_a_running_task_also_cancels_its_document(client: TestClient, kb_id: str) -> None:
    """正在跑的任务光改任务行没用：worker 手上那次不会因此停手。

    所以必须把文档一起置 canceled——摄入在下一个阶段边界看见就停。
    """
    document_id, task_id = _upload(client, kb_id, "正在跑.md")
    _set_state(task_id, "running")

    response = client.post("/api/v1/tasks/cancel", json={"task_ids": [task_id]})

    assert response.json()["succeeded"] == 1
    assert client.get(f"/api/v1/documents/{document_id}").json()["stage"] == "canceled"
    assert client.get(f"/api/v1/documents/{document_id}").json()["error"] == "已取消"


def test_cancel_state_all_includes_running(client: TestClient, kb_id: str) -> None:
    _, task_id = _upload(client, kb_id, "全都要撤.md")
    _set_state(task_id, "running")

    response = client.post("/api/v1/tasks/cancel", json={"state": "all"})

    assert response.json()["succeeded"] == 1


def test_finished_and_missing_tasks_report_why(client: TestClient, kb_id: str) -> None:
    """撤一条已经结束的、或不存在的不该静默成功——界面要能说出原因。"""
    _, task_id = _upload(client, kb_id, "已经好了.md")
    _set_state(task_id, "succeeded")

    body = client.post(
        "/api/v1/tasks/cancel", json={"task_ids": [task_id, "task_nope"]}
    ).json()

    assert (body["succeeded"], body["failed"]) == (0, 2)
    reasons = {item["task_id"]: item["error"] for item in body["items"]}
    assert "已结束" in reasons[task_id]
    assert reasons["task_nope"] == "任务不存在"


def test_running_task_without_document_is_refused(client: TestClient) -> None:
    """数据源 / Wiki 这类任务没有可中断的阶段，如实拒绝而不是给个假象。"""
    from app.core.config import get_settings

    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute(
            "INSERT INTO tasks (id, kind, state, payload, attempts, max_attempts,"
            " created_at, updated_at) VALUES (%s, %s, %s, %s, 0, 5, now(), now())",
            ("task_wiki_run", "wiki", "running", '{"kb_id": "kb_x"}'),
        )

    body = client.post("/api/v1/tasks/cancel", json={"task_ids": ["task_wiki_run"]}).json()

    assert body["failed"] == 1
    assert "没有可中断的阶段" in body["items"][0]["error"]


def test_readonly_key_cannot_cancel(client: TestClient, kb_id: str) -> None:
    _, task_id = _upload(client, kb_id, "只读不能撤.md")
    issued = client.post(
        "/api/v1/api-keys",
        json={"name": "只读", "permission": "readonly", "knowledge_base_ids": []},
    ).json()

    response = client.post(
        "/api/v1/tasks/cancel",
        json={"task_ids": [task_id]},
        headers={"Authorization": f"Bearer {issued['token']}"},
    )

    assert response.status_code == 403
    assert _tasks(client)[0]["state"] == "pending"
