"""导入端点（`/local/import*`）的整条链（M2 §6.3，`@pytest.mark.local`）。

这条用例要回答的是"**界面点了那个按钮会发生什么**"：本机后端（真的 app）+
假 NAS（``httpx.MockTransport``，绝不打真网络）+ 真的本机库（SQLite，落在 ``tmp_path``）。
它**不需要 PostgreSQL**：本机档的路由表（`local_router`）把会话那一面整个挂在本机库上。

覆盖：``dry_run`` 只看不写 → 开批次（后台跑）→ 轮询到 ``done`` → 会话真的在列表与详情里
→ ``/local/status`` 的两笔账（未完成的批次、未随导入的文件引用数）→ 回滚 → 会话没了。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.services import get_services, reset_services
from app.core.storage import reset_stores
from app.services.conversation_export import (
    MEDIA_TYPE,
    encode_conversation,
    footer_line,
    header_line,
)
from app.storage.base import (
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
    SessionEventRecord,
)

pytestmark = pytest.mark.local

NAS = "http://nas.test/api/v1"
T0 = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
POLL_TIMEOUT = 30.0


def _transfer(index: int, *, messages: int) -> ConversationTransfer:
    record = ConversationRecord(
        id=f"conv_nas{index:04d}",
        title=f"NAS 上的旧会话 {index}",
        owner_id="usr_owner",
        created_at=T0 + timedelta(minutes=index),
        updated_at=T0 + timedelta(minutes=index, seconds=30),
    )
    return ConversationTransfer(
        conversation=record,
        summary=f"{record.title} 的压缩摘要",
        summary_upto=f"msg_{index}_0",
        messages=[
            ChatMessageRecord(
                id=f"msg_{index}_{position}",
                conversation_id=record.id,
                role="user" if position % 2 == 0 else "assistant",
                content=f"第 {position} 条",
                # 第一条带一个附件：R4 那个"未随导入的文件引用数"要数它
                attachments=({"key": "art_1", "name": "附件.pdf"},) if position == 0 else (),
                created_at=T0 + timedelta(minutes=index, seconds=position),
            )
            for position in range(messages)
        ],
        events=[
            SessionEventRecord(
                conversation_id=record.id,
                seq=position + 1,
                kind="turn/complete",
                payload={"status": "ok"},
                created_at=T0 + timedelta(minutes=index, seconds=position),
            )
            for position in range(messages)
        ],
        artifacts=[
            ConversationArtifactRecord(
                id=f"art_{index}",
                conversation_id=record.id,
                name="报告.docx",
                format="docx",
                size_bytes=2048,
                # **location 保原样**：它指向 NAS 上的 key（本机没有那份文件）
                location=f"conversations/{record.id}/art_{index}.docx",
                created_at=T0 + timedelta(minutes=index, seconds=1),
            )
        ],
    )


@pytest.fixture
def nas() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """假 NAS（两条会话，第二条长一些）。"""
    seen: list[httpx.Request] = []
    transfers = [_transfer(1, messages=2), _transfer(2, messages=6)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.endswith("/conversations/export"), request.url
        limit = int(request.url.params.get("limit", "200"))
        offset = int(request.url.params.get("offset", "0"))
        page = transfers[offset : offset + limit]
        lines = [header_line(count=len(page))]
        messages = events = 0
        for transfer in page:
            lines.extend(encode_conversation(transfer))
            messages += len(transfer.messages)
            events += len(transfer.events)
        lines.append(
            footer_line(conversations=len(page), messages=messages, events=events, artifacts=0)
        )
        return httpx.Response(200, content="".join(lines).encode("utf-8"))

    return httpx.MockTransport(handler), seen


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: tuple[httpx.MockTransport, list]
) -> Iterator[TestClient]:
    """本机档的真 app + 假 NAS（把假传输塞进装配好的导入器里）。"""
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    # 本机档的远端两头：来源基址给假 NAS，令牌走同一个入口（不经过 HTTP 请求体）
    monkeypatch.setenv("KYLAB_SERVER_URL", NAS)
    monkeypatch.setenv("KYLAB_TOKEN", "t")
    get_settings.cache_clear()
    reset_services()
    reset_stores()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        importer = get_services().legacy_import
        assert importer is not None, "本机档必须装配出导入器"
        assert importer.source == NAS
        importer.transport = nas[0]  # 假传输：这条用例一次真网络都不打
        yield test_client
    reset_services()
    reset_stores()
    get_settings.cache_clear()


def wait_for_batch(client: TestClient, batch_id: str) -> dict:
    """轮询到批次跑完（或超时后如实失败——超时本身就是一条要看见的信息）。"""
    deadline = time.monotonic() + POLL_TIMEOUT
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/local/import/{batch_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] in ("done", "failed", "rolled_back"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"批次 {batch_id} 在 {POLL_TIMEOUT}s 内没跑完：{last}")


def test_dry_run_reports_without_writing(client: TestClient) -> None:
    """``dry_run=true``：说清会新建/替换/跳过什么，且**一条会话都不写**。"""
    response = client.post("/api/v1/local/import", json={"dry_run": True})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["dry_run"] is True and body["batch_id"] == ""
    assert body["counts"]["created"] == 2
    assert body["counts"]["scanned"] == 2
    assert client.get("/api/v1/conversations").json()["items"] == []


def test_the_whole_import_flow(client: TestClient, nas: tuple[httpx.MockTransport, list]) -> None:
    """开批次 → 轮询 → 会话在列表与详情里 → ``/local/status`` 的两笔账。"""
    started = client.post("/api/v1/local/import", json={})

    assert started.status_code == 202, started.text
    batch_id = started.json()["batch_id"]
    assert batch_id.startswith("imp_")

    finished = wait_for_batch(client, batch_id)

    assert finished["state"] == "done", finished
    assert finished["counts"]["created"] == 2
    assert finished["counts"]["messages"] == 8
    assert finished["source"] == NAS
    # 真的从假 NAS 上拉的（不是本地凭空造出来的）
    assert nas[1], "导入器一次请求都没发"
    assert nas[1][0].headers["authorization"] == "Bearer t"

    # 列表与详情（走的是本机后端那几条端点，与本机档的界面同一条路）
    listed = client.get("/api/v1/conversations").json()["items"]
    assert sorted(item["id"] for item in listed) == ["conv_nas0001", "conv_nas0002"]
    detail = client.get("/api/v1/conversations/conv_nas0002").json()
    assert len(detail["messages"]) == 6
    assert detail["messages"][0]["content"] == "第 0 条"
    # 事件日志（`session_events` 真的落了，seq 从 1 起）
    events = client.get("/api/v1/conversations/conv_nas0002/events").json()
    assert [item["seq"] for item in events["items"]] == list(range(1, 7))

    # /local/status 的两笔账：没跑完的批次（这条链上 0）与**未随导入的文件引用数**
    # （2 条会话各 1 份产物 + 1 个消息附件 = 4；那些字节还在 NAS 上，R4）
    status = client.get("/api/v1/local/status").json()
    assert status["unfinished_imports"] == 0
    assert status["unimported_file_references"] == 4
    assert [item["batch_id"] for item in status["imports"]] == [batch_id]
    assert status["imports"][0]["state"] == "done"


def test_a_second_run_skips_everything(client: TestClient) -> None:
    """重跑：全部命中幂等键 → ``skipped``，列表里还是那两条。"""
    first = client.post("/api/v1/local/import", json={}).json()
    wait_for_batch(client, first["batch_id"])

    second = client.post("/api/v1/local/import", json={}).json()
    again = wait_for_batch(client, second["batch_id"])

    assert again["counts"]["skipped"] == 2
    assert again["counts"]["created"] == 0
    listed = client.get("/api/v1/conversations").json()["items"]
    assert sorted(item["id"] for item in listed) == ["conv_nas0001", "conv_nas0002"]


def test_rollback_removes_what_the_batch_created(client: TestClient) -> None:
    """回滚：批次创建的那些会话被删掉，状态记 ``rolled_back``，再次导入又能来一遍。"""
    started = client.post("/api/v1/local/import", json={}).json()
    wait_for_batch(client, started["batch_id"])

    rollback = client.post(f"/api/v1/local/import/{started['batch_id']}/rollback")

    assert rollback.status_code == 200, rollback.text
    body = rollback.json()
    assert body["state"] == "rolled_back"
    assert body["counts"]["deleted"] == 2
    assert client.get("/api/v1/conversations").json()["items"] == []

    # 回滚过的那批台账**不算"已经导过"**：再导一次能成
    again = client.post("/api/v1/local/import", json={}).json()
    finished = wait_for_batch(client, again["batch_id"])
    assert finished["counts"]["created"] == 2


def test_unknown_batch_is_a_404(client: TestClient) -> None:
    """查一个不存在的批次 → 404（不是空对象：那会让界面以为"还没开始"）。"""
    response = client.get("/api/v1/local/import/imp_000000000000")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_broken_source_fails_the_batch_with_a_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源端坏了 → 批次 ``failed`` + 一句能读懂的原因（不是静默空跑）。"""

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    importer = get_services().legacy_import
    assert importer is not None
    importer.transport = httpx.MockTransport(broken)

    started = client.post("/api/v1/local/import", json={}).json()
    finished = wait_for_batch(client, started["batch_id"])

    assert finished["state"] == "failed"
    assert "导出端点出错了" in finished["error"]
    # 失败也留了一条账：界面看得到"这次没成、为什么"
    assert client.get("/api/v1/local/import/" + started["batch_id"]).json()["error"]


def test_startup_reports_an_unfinished_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """上次导入被杀在半路 → **启动时如实报一句**（R1），而 ``/local/status`` 报同一笔账。

    落法：库里预置一个 ``state=running`` 的批次（那正是"进程被杀了，台账没来得及收尾"
    的样子），再起应用。**不自动重试**：重跑是用户的决定（来源可能都不在了），
    这里只把"有几笔账没结、怎么续"说出来。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    monkeypatch.setenv("KYLAB_SERVER_URL", NAS)
    get_settings.cache_clear()
    reset_services()
    reset_stores()

    from app.core.storage import build_stores

    stores = build_stores(get_settings())
    assert stores.ledger is not None
    stores.ledger.start_import_batch("imp_dead000000", source=NAS)
    stores.ledger.set_import_state("imp_dead000000", "running")
    reset_stores()  # 关掉这条连接：接下来由应用自己开

    from app.main import create_app

    with (
        caplog.at_level(logging.WARNING, logger="app.main"),
        TestClient(create_app()) as app_client,
    ):
        status = app_client.get("/api/v1/local/status").json()

    assert status["unfinished_imports"] == 1
    assert status["imports"][0]["batch_id"] == "imp_dead000000"
    messages = [record.getMessage() for record in caplog.records]
    assert any("没跑完" in message and "imp_dead000000" in message for message in messages), (
        messages
    )
    reset_services()
    reset_stores()
    get_settings.cache_clear()


def test_the_local_deployment_reports_its_own_limits(client: TestClient) -> None:
    """``/local/status`` 如实写着这一档的边界（文件引用只留 key，本体在 NAS 上）。"""
    status = client.get("/api/v1/local/status").json()

    assert status["deployment"] == "local"
    assert status["database"].endswith("kylab.db")
    assert status["database_exists"] is True
    assert "NAS" in status["note"]
    assert MEDIA_TYPE  # 导出流的媒体类型在本模块被引用（契约常量不会漂成两个名字）
    assert get_settings().data_dir  # 数据目录来自引导配置，不是 cwd
