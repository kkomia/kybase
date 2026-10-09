"""上传接口的幂等键行为（M4 T4.3，HTTP 层）。

镜像同构：``app/api/v1/documents.py`` 的上传分支 → 本文件。

服务层判定已在 ``tests/unit/services/test_idempotency.py`` 覆盖，
这里只验**接进接口之后**真的生效：同键重放不再入库、同键换内容被拒。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from app.models.enums import DocumentStage
from tests.conftest import admin_client as admin_session

MARKDOWN = "# 设计\n\n用于验证幂等键的正文。\n".encode()
HEADERS = {"Idempotency-Key": "client-retry-1"}


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:

        yield test_client



@pytest.fixture
def kb_id(client: TestClient) -> str:
    response = client.post("/api/v1/knowledge-bases", json={"name": "幂等测试库"})
    assert response.status_code == 201
    return response.json()["id"]


def _upload(
    client: TestClient,
    kb_id: str,
    *,
    content: bytes = MARKDOWN,
    headers=None,  # type: ignore[no-untyped-def]
    start: bool = False
    ):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(content), "text/markdown")},
        params={"start": str(start).lower()},
        headers=headers or {}
    )


def _document_count(client: TestClient, kb_id: str) -> int:
    response = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents")
    return len(response.json()["items"])


def test_without_idempotency_key_behaviour_is_unchanged(client: TestClient, kb_id: str) -> None:
    """不提供键时接口照旧工作——键是可选的，见 upload_document 的说明。"""
    assert _upload(client, kb_id).status_code == 202
    assert _document_count(client, kb_id) == 1


def test_replay_with_same_key_does_not_create_a_second_document(
    client: TestClient, kb_id: str
) -> None:
    """**这是幂等键要解决的真实场景**：上传成功但响应丢了，客户端重试。

    重试必须拿到与首次**完全相同**的响应（同一个 document id、同一个 task id），
    否则客户端会认为这是一份新文档。
    """
    first = _upload(client, kb_id, headers=HEADERS, start=True)
    assert first.status_code == 202
    first_body = first.json()

    second = _upload(client, kb_id, headers=HEADERS, start=True)
    assert second.status_code == 202
    assert second.json() == first_body, "重放必须回上次那份响应"

    # 关键：库里仍然只有一份文档
    assert _document_count(client, kb_id) == 1


def test_same_key_with_different_content_is_rejected(client: TestClient, kb_id: str) -> None:
    """同键换内容必须 409。

    静默当成重放回上次的 document_id，会让客户端以为自己传了新文件——
    这是最坏的一种"成功"。
    """
    assert _upload(client, kb_id, headers=HEADERS).status_code == 202

    different = _upload(
        client, kb_id, content="# 另一份完全不同的内容\n".encode(), headers=HEADERS
    )
    assert different.status_code == 409
    assert "不同的请求内容" in different.json()["message"]


def test_same_key_different_kb_is_rejected(client: TestClient, kb_id: str) -> None:
    """指纹覆盖 kb_id：同键传到另一个库也算"不同请求"。"""
    other = client.post("/api/v1/knowledge-bases", json={"name": "另一个库"}).json()["id"]
    assert _upload(client, kb_id, headers=HEADERS).status_code == 202

    assert _upload(client, other, headers=HEADERS).status_code == 409


def test_different_keys_both_succeed(client: TestClient, kb_id: str) -> None:
    """不同键各自入库——幂等键不该把正常的不同请求也拦下。

    注意内容相同，所以这里同时验证了"内容 hash 去重仍然生效"：
    第二次会命中 is_duplicate，而**不是**被幂等层拦住。
    """
    first = _upload(client, kb_id, headers={"Idempotency-Key": "k-a"})
    second = _upload(client, kb_id, headers={"Idempotency-Key": "k-b"})

    assert first.status_code == 202
    assert second.status_code == 202
    # 内容 hash 去重让第二份没有新增文档
    assert second.json()["is_duplicate"] is True
    assert _document_count(client, kb_id) == 1


def test_replay_works_even_after_the_task_started(client: TestClient, kb_id: str) -> None:
    """带着 start=true 真正入队后重放，也不能多出任务。

    这条覆盖的是"响应里带 task_id"的那条分支——如果重放路径只回 document
    而丢掉 task_id，客户端的后续轮询就断了。
    """
    first = _upload(client, kb_id, headers={"Idempotency-Key": "k-task"}, start=True).json()
    assert first["task_id"], "首次上传应当产生任务"

    second = _upload(client, kb_id, headers={"Idempotency-Key": "k-task"}, start=True).json()
    assert second["task_id"] == first["task_id"]

    tasks = client.get("/api/v1/tasks").json()["items"]
    assert len(tasks) == 1, "重放不该产生第二个任务"


def test_purge_removes_expired_keys_and_keeps_fresh_ones(client: TestClient, kb_id: str) -> None:
    """过期清理要真的删掉旧键、留下新键。

    没有这一步 ``idempotency_keys`` 会随每次上传无限增长——这类表不会报错，
    只会在几个月后变成"某个晚上数据库突然大了一截"。
    """
    from datetime import UTC, datetime, timedelta

    services = get_services()
    _upload(client, kb_id, headers={"Idempotency-Key": "fresh"})

    # 造一个 3 天前的旧键（保留期是 24 小时）
    services.idempotency.begin("stale", "hash-stale")
    assert services.idempotency.purge_expired(now=datetime.now(UTC)) == 0, "新键不该被删"

    assert services.idempotency.purge_expired(
        now=datetime.now(UTC) + timedelta(days=3)
    ) >= 1

    assert services.idempotency.begin("fresh", "hash-fresh") is not None
    # 旧键已清空，可以重新占住
    assert not services.idempotency.begin("stale", "hash-stale").is_replay


def test_uploaded_document_is_still_stored_normally(client: TestClient, kb_id: str) -> None:
    """幂等层不该改变成功路径的语义。"""
    body = _upload(client, kb_id, headers=HEADERS).json()
    assert body["document"]["name"] == "a.md"
    assert body["document"]["stage"] == DocumentStage.UPLOADED.value
    assert body["is_duplicate"] is False


def test_key_is_released_when_the_business_step_fails(
    client: TestClient, kb_id: str, monkeypatch
) -> None:
    """业务中途失败要放掉键，否则重试永远拿到"正在处理中"。

    这比直接报错更糟：它看起来像"再等等就好"，而实际上什么都没在处理。
    """
    from app.api.v1 import documents as documents_module

    boom = {"count": 0}
    real = documents_module._do_upload

    def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
        boom["count"] += 1
        if boom["count"] == 1:
            raise RuntimeError("模拟入库过程中的存储抖动")
        return real(*args, **kwargs)

    monkeypatch.setattr(documents_module, "_do_upload", flaky)

    headers = {"Idempotency-Key": "will-fail-once"}
    with pytest.raises(RuntimeError):
        _upload(client, kb_id, headers=headers)

    # 键已被放掉，所以重试能真正跑起来，而不是 409
    retry = _upload(client, kb_id, headers=headers)
    assert retry.status_code == 202, retry.text
    assert _document_count(client, kb_id) == 1


def test_release_does_not_drop_a_completed_key(client: TestClient, kb_id: str) -> None:
    """已经成功过的键不能被"释放"掉，否则同一个键能再跑一遍业务。"""
    headers = {"Idempotency-Key": "already-done"}
    assert _upload(client, kb_id, headers=headers).status_code == 202

    get_services().idempotency.release("already-done")

    # 键仍在：重放应当继续生效，而不是重新入库
    replay = _upload(client, kb_id, headers=headers)
    assert replay.status_code == 202
    assert _document_count(client, kb_id) == 1
