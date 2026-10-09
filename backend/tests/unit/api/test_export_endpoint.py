"""导出端点的契约（M2 §6.3 第三条，`@pytest.mark.local`）。

`GET /api/v1/conversations/export` 是本机导入器**唯一**的源端入口，所以它的形状
（首行 / 末行 / 流式 / 归属）就是那份契约本身。这条用例钉住四件事：

1. **首行**：``header`` + ``export_version`` + 这一页扫过几条会话；
2. **末行**：``footer`` 的四个数与实际发出去的块数一致（**末行在 = 这一页完整**，
   导入器就是靠它判断"传输有没有被掐断"）；
3. **流式**：一次只有一条会话在内存里（拿到首行时还没去读任何一条会话的正文）；
4. **归属**：成员通道只导自己的（与列表同一个判据，复用 `conversations._caller_owner`）。

跑在本机档上（``KYLAB_DEPLOYMENT=local``）：导出这条路走的是**同一个服务实现**，
两个档位共用它（服务器档导给本机导入器，本机档导自己那批），所以本机档能跑就等于
这条契约成立——而它**不需要 PostgreSQL**。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.api.v1.conversations import export_conversations
from app.core.config import get_settings
from app.core.services import reset_services
from app.core.storage import get_stores, reset_stores
from app.models.enums import UserRole
from app.services.api_key import Caller
from app.services.conversation_export import EXPORT_VERSION, MEDIA_TYPE, parse_line
from app.storage.base import (
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    SessionEventRecord,
    UserRecord,
)

pytestmark = pytest.mark.local

T0 = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """本机档的真应用（`main.create_app`）——导出端点两个档位都挂着，这里走本机档。"""
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    get_settings.cache_clear()
    reset_services()
    reset_stores()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    reset_services()
    reset_stores()
    get_settings.cache_clear()


def seed(owner_id: str | None, index: int, *, messages: int = 2) -> ConversationRecord:
    """往本机库塞一条会话（消息 / 事件 / 产物 / 摘要都带上）。

    **直写存储**而不是走 HTTP：导出要验的是"这一条会话的每个字节都过线"，
    而"怎么造出一条带事件与产物的会话"是另一个话题（`test_local_backend.py` 覆盖它）。
    """
    meta = get_stores().meta
    record = ConversationRecord(
        id=f"conv_exp{index:04d}",
        title=f"导出用例 {index}",
        kb_ids=("kb_1",),
        owner_id=owner_id,
        pinned=index == 1,
        created_at=T0 + timedelta(minutes=index),
        updated_at=T0 + timedelta(minutes=index, seconds=30),
    )
    meta.create_conversation(record)
    meta.append_turn(
        messages=[
            ChatMessageRecord(
                id=f"msg_{index}_{position}",
                conversation_id=record.id,
                role="user" if position % 2 == 0 else "assistant",
                content=f"第 {position} 条消息（{record.title}）",
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
    )
    meta.create_artifact(
        ConversationArtifactRecord(
            id=f"art_{index}",
            conversation_id=record.id,
            name="报告.docx",
            format="docx",
            location=f"conversations/{record.id}/art_{index}.docx",
            created_at=T0 + timedelta(minutes=index),
        )
    )
    meta.set_conversation_summary(record.id, f"这是 {record.title} 的压缩摘要", f"msg_{index}_0")
    return record


def lines_of(response: Any) -> list[dict[str, Any]]:
    """响应体 → 一行一个 JSON（**按线上形状解析**，不猜内部对象）。

    信封那一层摊成 ``block``（块类型）与 ``conversation_id``，``data`` 里的字段平铺进来——
    注意**不能叫 ``kind``**：事件块的正文里本来就有 ``kind``（``turn/complete``），
    同名会把信封那一层盖掉（第一版这条用例就是这么错了一次）。
    """
    parsed: list[dict[str, Any]] = []
    for raw in response.text.splitlines():
        event = parse_line(raw)
        if event is not None:
            parsed.append(
                {"block": event.kind, "conversation_id": event.conversation_id, **event.data}
            )
    return parsed


def body_of(response: StreamingResponse) -> str:
    """把 ``StreamingResponse`` 的流读完。

    它是**异步**迭代器（Starlette 把同步生成器包进了线程池），所以同步用例里用一次
    ``asyncio.run`` 读完就够——不改产品代码去迎合测试。
    """

    async def drain() -> str:
        chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
        # 生成器吐的是 `str`（一行一个 JSON），Starlette 只是把它丢进线程池迭代，
        # 所以这里按 str 拼——用 bytes 拼会在第一个分片就 TypeError
        return "".join(
            chunk if isinstance(chunk, str) else chunk.decode("utf-8") for chunk in chunks
        )

    return asyncio.run(drain())


def test_the_stream_starts_with_a_header_and_ends_with_a_footer(client: TestClient) -> None:
    """首行是 header（版本 + 这一页几条），末行是 footer（四个数对得上）。"""
    seed(None, 1, messages=3)
    seed(None, 2, messages=2)

    response = client.get("/api/v1/conversations/export")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(MEDIA_TYPE)
    rows = lines_of(response)
    assert rows[0]["block"] == "header"
    assert rows[0]["export_version"] == EXPORT_VERSION
    assert rows[0]["count"] == 2
    assert "generated_at" in rows[0]
    footer = rows[-1]
    assert footer["block"] == "footer"
    assert footer["conversations"] == 2
    assert footer["messages"] == 5
    assert footer["events"] == 5
    assert footer["artifacts"] == 2


def test_every_block_of_a_conversation_goes_over_the_wire(client: TestClient) -> None:
    """一条会话的**每一块**都在：会话（含摘要两列）/ 消息 / 事件（含 seq）/ 产物。"""
    record = seed(None, 7, messages=2)

    response = client.get("/api/v1/conversations/export")
    rows = lines_of(response)

    blocks = [row["block"] for row in rows]
    assert blocks[0] == "header" and blocks[-1] == "footer"
    # 顺序固定：会话 → 消息 → 事件 → 产物（解的人靠它一条一条攒）
    assert blocks[1:7] == [
        "conversation",
        "message",
        "message",
        "event",
        "event",
        "artifact",
    ]
    head = rows[1]
    assert head["id"] == record.id
    assert head["title"] == record.title
    assert head["pinned"] == record.pinned
    # **那两个不进 API 的列必须过线**（§1.3）：丢了它们，导进来的长期会话续聊会从头重算
    assert head["context_summary"] == "这是 导出用例 7 的压缩摘要"
    assert head["summary_upto"] == "msg_7_0"
    assert head["updated_at_ms"] == int(record.updated_at.timestamp() * 1000)  # type: ignore[union-attr]
    assert [row["seq"] for row in rows if row["block"] == "event"] == [1, 2]
    assert [row["id"] for row in rows if row["block"] == "message"] == ["msg_7_0", "msg_7_1"]
    artifact = next(row for row in rows if row["block"] == "artifact")
    # 产物记录照发、location 保原 key（文件本体不随导入过来：R4）
    assert artifact["location"] == f"conversations/{record.id}/art_7.docx"


def test_paging_walks_the_same_set(client: TestClient) -> None:
    """``limit`` / ``offset`` 是稳定的游标：两页拼起来正好是全集，且不重不漏。"""
    seed(None, 1)
    seed(None, 2)
    seed(None, 3)

    first = lines_of(client.get("/api/v1/conversations/export?limit=2&offset=0"))
    second = lines_of(client.get("/api/v1/conversations/export?limit=2&offset=2"))

    assert first[0]["count"] == 2 and second[0]["count"] == 1
    ids = [row["id"] for row in (*first, *second) if row["block"] == "conversation"]
    assert sorted(ids) == ["conv_exp0001", "conv_exp0002", "conv_exp0003"]


def test_since_only_takes_the_newer_ones(client: TestClient) -> None:
    """``since`` 用**严格大于**：把上次那批的时间点原样传进来不该再导一遍。"""
    old = seed(None, 1)
    seed(None, 2)

    exact = lines_of(
        client.get(
            "/api/v1/conversations/export",
            params={"since": old.updated_at.isoformat()},  # type: ignore[union-attr]
        )
    )
    ids = [row["id"] for row in exact if row["block"] == "conversation"]

    assert "conv_exp0001" not in ids
    assert "conv_exp0002" in ids


def test_the_stream_is_lazy_about_conversation_bodies(client: TestClient) -> None:
    """**流式**：拿到首行时还没有去读任何一条会话的正文（一次只有一条在内存里）。

    判据用"读会话正文"这个动作本身（``ConversationService.messages`` 被调了几次），
    因为它正是内存上界的那一处：一条长会话的正文可以很大，攒一页再发等于把整页搬进内存。
    """
    from app.core.services import get_services

    seed(None, 1)
    seed(None, 2)
    service = get_services().conversation_export
    reads = 0
    # **数存储层那一次读**（`read_transfer` 走的是 `stores.meta.list_messages`）：
    # 它正是内存上界的那一处——一条长会话的正文可以很大。
    meta = service._stores.meta
    original = meta.list_messages

    def counted(conversation_id: str):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return original(conversation_id)

    meta.list_messages = counted  # type: ignore[method-assign]
    try:
        stream = service.stream(owner_id=None)
        first = next(stream)
        assert parse_line(first).kind == "header"  # type: ignore[union-attr]
        assert reads == 0, "还没读完首行就去读会话正文了（那不是流式）"
        # 再要一行：这时才轮到第一条会话
        next(stream)
        assert reads == 1
        # 把剩下的读完：一共读了两条（各一次），不是"一次读完全部"
        list(stream)
        assert reads == 2
    finally:
        meta.list_messages = original  # type: ignore[method-assign]


def test_a_member_channel_only_exports_their_own(client: TestClient) -> None:
    """归属：成员通道只导自己的（与列表复用同一个 ``_caller_owner`` 判据）。

    这条**直接调端点函数**并给一个成员身份：本机档的鉴权是短路成"本机主人"的
    （admin），走 HTTP 永远拿不到成员通道，而"成员只看得到自己的"恰恰是导出端点
    在服务器档上最要紧的那条纪律（导别人的会话 = 把别人的对话打包带走）。
    """
    from app.core.services import get_services

    mine = seed("usr_me", 11)
    seed("usr_someone_else", 12)
    member = Caller(user=UserRecord(id="usr_me", name="我", role=UserRole.MEMBER))

    response = export_conversations(
        services=get_services(), caller=member, limit=200, offset=0, since=None
    )
    rows = [parse_line(raw) for raw in body_of(response).splitlines()]
    ids = [event.data["id"] for event in rows if event and event.kind == "conversation"]

    assert ids == [mine.id], "成员通道导出了不属于自己的会话"


def test_an_admin_channel_exports_everything(client: TestClient) -> None:
    """管理员/本机主人不过滤（``_caller_owner`` 返回 ``None``）——两个档位同一口径。"""
    from app.core.services import get_services

    seed("usr_a", 21)
    seed("usr_b", 22)

    response = export_conversations(
        services=get_services(),
        caller=Caller(
            is_admin=True, user=UserRecord(id="usr_admin", name="管理员", role=UserRole.ADMIN)
        ),
        limit=200,
        offset=0,
        since=None,
    )
    ids = [
        event.data["id"]
        for event in (parse_line(raw) for raw in body_of(response).splitlines())
        if event and event.kind == "conversation"
    ]

    assert sorted(ids) == ["conv_exp0021", "conv_exp0022"]


def test_archived_conversations_are_exported_too(client: TestClient) -> None:
    """归档的会话**也要导**（归档不是删除；漏掉它们等于用户导完发现少了那批）。"""
    seed(None, 1)
    archived = seed(None, 2)
    get_stores().meta.set_conversation_archived(archived.id, True)

    rows = lines_of(client.get("/api/v1/conversations/export"))
    ids = [row["id"] for row in rows if row["block"] == "conversation"]

    assert sorted(ids) == ["conv_exp0001", "conv_exp0002"]
