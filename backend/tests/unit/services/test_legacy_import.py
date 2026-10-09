"""旧会话导入与回滚（M2 §6.3 的验收用例，`@pytest.mark.local`）。

这条用例要证明的是方案 §3.1 那三句话**逐条成立**，而且**不需要 PostgreSQL**
（`local` 这个 marker 的意义，见 `conftest.py`）：

1. **幂等**：重跑同一批 → 全部 ``skipped``、**一行未写**；
2. **覆盖策略 `replace_if_local_untouched`**：本机在导入之后又改过 → 跳过并如实列出；
   本机那条不是我们导进来的 → 也不覆盖；
3. **可回滚**：``created`` 且没再动过 → 删；``replaced`` 且没再动过 → 用快照恢复；
   本机改过的一律保留并如实报数。

## 假的是什么、真的是什么

| 件 | 这一轮的形态 |
| --- | --- |
| 源端（NAS） | 假的：``httpx.MockTransport`` 按 ``limit``/``offset`` 分页吐同一套 NDJSON |
| 线格式 | **真的**：两头的编解码都用 `conversation_export`（不是手写几行 JSON） |
| 本机库 | **真的**：``build_stores()`` 装出来的本机档（SQLite，落在 ``tmp_path``） |
| 导入器 | **真的**：`LegacyImporter`（台账、快照、失败记数、逐页拉取全走真实现） |

线格式用同一套编解码而不是手写：手写的那份迟早与产品漂掉，而"用例绿着、
契约已经变了"正是这类协议测试最典型的死法。
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.storage import build_stores, reset_stores
from app.services import legacy_import as module
from app.services.conversation_export import (
    MEDIA_TYPE,
    encode_conversation,
    footer_line,
    header_line,
    read_transfer,
)
from app.services.legacy_import import ROLLBACK_DIRNAME, LegacyImporter
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
    SessionEventRecord,
    StoreBundle,
    WorkspaceRecord,
)
from app.storage.sqlite_impl.connection import Database

pytestmark = pytest.mark.local

NAS = "http://nas.test/api/v1"
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
CONTENT_TABLES = (
    "conversations",
    "chat_messages",
    "session_events",
    "conversation_artifacts",
)


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def bundle(tmp_path: Path) -> Iterator[StoreBundle]:
    """本机档的真装配（与 `_build_local_stores` 同一条路，连带证明 `ledger` 接上了）。"""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=tmp_path / "data"
    )
    stores = build_stores(settings)
    assert stores.ledger is not None, "本机档的导入台账必须接上（阶段 5）"
    yield stores
    reset_stores()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """另一条连接，只为**直读表**（"一行未写"这种事得绕过仓储才看得见）。"""
    handle = Database(tmp_path / "data" / "kylab.db", allow_multiple_instances=True)
    handle.open()
    yield handle
    handle.close()


def conversation(
    index: int, *, updated_at: datetime | None = None, **overrides: object
) -> ConversationRecord:
    """一条源端会话（id 固定，方便用例指名道姓地断言）。"""
    kwargs: dict[str, object] = {
        "id": f"conv_{index:04d}",
        "title": f"旧会话 {index}",
        "kb_ids": ("kb_1",),
        "owner_id": "usr_owner",
        "pinned": index == 1,
        "created_at": T0 + timedelta(minutes=index),
        "updated_at": updated_at or (T0 + timedelta(minutes=index)),
    }
    kwargs.update(overrides)
    return ConversationRecord(**kwargs)  # type: ignore[arg-type]


def long_conversation(index: int, *, turns: int = 20) -> ConversationTransfer:
    """一条**长会话**：`turns` 轮问答 + 事件 + 产物 + 摘要（导入要保住的东西全在）。

    第一条回答带一个附件、这条会话有一份产物——R4 那个"未随导入的文件引用数"
    就是数它们（产物 1 + 附件 1）。
    """
    record = conversation(index)
    messages: list[ChatMessageRecord] = []
    events: list[SessionEventRecord] = []
    for turn in range(turns):
        moment = T0 + timedelta(minutes=index, seconds=turn * 2)
        messages.append(
            ChatMessageRecord(
                id=f"msg_{index}_{turn}_q",
                conversation_id=record.id,
                role="user",
                content=f"第 {turn} 问：这台机器上的旧会话要怎么导进来？" + "补充" * 20,
                created_at=moment,
            )
        )
        messages.append(
            ChatMessageRecord(
                id=f"msg_{index}_{turn}_a",
                conversation_id=record.id,
                role="assistant",
                content=f"第 {turn} 答：" + "正文" * 80,
                sources=({"index": 1, "document_name": "指南.pdf"},),
                steps=({"tool": "search", "status": "done"},),
                thinking=f"第 {turn} 轮的推理",
                attachments=({"key": "art_1", "name": "附件.pdf"},) if turn == 0 else (),
                created_at=moment + timedelta(seconds=1),
            )
        )
        events.append(
            SessionEventRecord(
                conversation_id=record.id,
                seq=turn * 2 + 1,
                kind="turn/start",
                payload={"query": f"第 {turn} 问"},
                created_at=moment,
            )
        )
        events.append(
            SessionEventRecord(
                conversation_id=record.id,
                seq=turn * 2 + 2,
                kind="turn/complete",
                payload={"status": "ok"},
                created_at=moment + timedelta(seconds=1),
            )
        )
    return ConversationTransfer(
        conversation=record,
        summary="这是压缩过的上下文摘要。" * 10,
        summary_upto=messages[-2].id,
        messages=messages,
        events=events,
        artifacts=[
            ConversationArtifactRecord(
                id=f"art_{index}",
                conversation_id=record.id,
                name="报告.docx",
                format="docx",
                size_bytes=2048,
                storage=ARTIFACT_IN_OBJECTS,
                location=f"conversations/{record.id}/art_{index}.docx",
                created_at=T0 + timedelta(minutes=index, seconds=1),
            )
        ],
    )


def fake_nas(
    transfers: Sequence[ConversationTransfer],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """假 NAS：按 ``limit``/``offset`` 分页吐同一套 NDJSON（首行/末行都在）。

    第二项是收到的请求（用来核对分页游标与 ``since`` 真的进了查询参数）。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.endswith("/conversations/export"), request.url
        limit = int(request.url.params.get("limit", "200"))
        offset = int(request.url.params.get("offset", "0"))
        page = list(transfers)[offset : offset + limit]
        lines = [header_line(count=len(page))]
        messages = events = artifacts = 0
        for transfer in page:
            lines.extend(encode_conversation(transfer))
            messages += len(transfer.messages)
            events += len(transfer.events)
            artifacts += len(transfer.artifacts)
        lines.append(
            footer_line(
                conversations=len(page), messages=messages, events=events, artifacts=artifacts
            )
        )
        return httpx.Response(
            200, content="".join(lines).encode("utf-8"), headers={"content-type": MEDIA_TYPE}
        )

    return httpx.MockTransport(handler), seen


def make_importer(
    bundle: StoreBundle,
    transfers: Sequence[ConversationTransfer],
    tmp_path: Path,
    *,
    page_size: int = 200,
    since: datetime | None = None,
) -> tuple[LegacyImporter, list[httpx.Request]]:
    """一个连着假 NAS 的导入器（真的走 HTTP 那一层：分页、逐行解析都是真实现）。"""
    transport, seen = fake_nas(transfers)
    importer = LegacyImporter(
        bundle,
        data_dir=tmp_path / "data",
        source=NAS,
        token="t",
        since=since,
        page_size=page_size,
        transport=transport,
    )
    return importer, seen


def content_rows(db: Database) -> dict[str, list[tuple]]:
    """四张内容表的全量快照（"一行未写"的机械判据）。"""
    with db.read() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608
            for table in CONTENT_TABLES
        }


# ------------------------------------------------------------------ ① 导进来：与源一致


def test_imports_three_conversations_and_matches_the_source(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """3 条会话（含一条长会话）导进来之后：**行数 / 顺序 / 摘要 / seq 与源逐字一致**。"""
    long_one = long_conversation(2)
    transfers = [
        ConversationTransfer(conversation=conversation(1)),
        long_one,
        ConversationTransfer(conversation=conversation(3)),
    ]
    importer, _seen = make_importer(bundle, transfers, tmp_path)

    report = importer.run()

    assert report.state == "done", report.error
    assert report.counts["created"] == 3
    assert report.counts["messages"] == len(long_one.messages)
    assert report.counts["events"] == len(long_one.events)
    assert report.counts["file_references"] == 2  # 1 份产物 + 1 个消息附件（R4 那个数）

    for expected in transfers:
        stored = bundle.meta.get_conversation(expected.conversation.id)
        assert stored is not None, expected.conversation.id
        # 用**同一套读法**读本地：导进来的东西必须原样读得回来
        local = read_transfer(bundle, stored)
        assert local.conversation.title == expected.conversation.title
        assert local.conversation.pinned == expected.conversation.pinned
        assert local.conversation.updated_at == expected.conversation.updated_at
        assert local.summary == expected.summary
        assert local.summary_upto == expected.summary_upto
        assert [item.id for item in local.messages] == [item.id for item in expected.messages]
        assert [item.content for item in local.messages] == [
            item.content for item in expected.messages
        ]
        assert [item.seq for item in local.events] == [item.seq for item in expected.events]
        assert [item.kind for item in local.events] == [item.kind for item in expected.events]
        assert [item.payload for item in local.events] == [item.payload for item in expected.events]
        assert [item.location for item in local.artifacts] == [
            item.location for item in expected.artifacts
        ]


def test_a_line_with_unicode_separators_is_not_split(bundle: StoreBundle, tmp_path: Path) -> None:
    """正文里的 **U+2028 / U+2029 / 制表符** 不能把一行 JSON 切开（真机抓到的 bug）。

    现场（2026-10-01 真机验收，302 条真会话）：一条 24463 字符的行被读成
    12543 + 11920 两半，导入当场失败「导出流里有一行不是 JSON」。
    根因不在导出端（按字节切 4846 + 3063 行一行不差），而在**读端**用了
    ``httpx.Response.iter_lines()``：它按
    [W3C 的换行口径](https://www.w3.org/TR/newline) 把 ``U+2028`` / ``U+2029``
    也当换行，而这两个字符在 JSON 字符串里**可以合法地原样出现**
    （``ensure_ascii=False`` 不转义它们；从网页复制来的正文里一抓一大把）。
    """
    separators = "行分隔\u2028段落分隔\u2029制表\t回车\r结尾"
    record = conversation(7, title=f"标题带分隔符{separators}")
    transfer = ConversationTransfer(
        conversation=record,
        summary=f"摘要也带{separators}",
        summary_upto=None,
        messages=[
            ChatMessageRecord(
                id="msg_7_1",
                conversation_id=record.id,
                role="assistant",
                content=f"正文里的分隔符必须原样回来：{separators}",
                created_at=T0 + timedelta(minutes=7),
            )
        ],
    )
    importer, _seen = make_importer(bundle, [transfer], tmp_path)

    report = importer.run()

    assert report.state == "done", report.error
    assert report.counts["created"] == 1
    stored = bundle.meta.get_conversation(record.id)
    assert stored is not None
    local = read_transfer(bundle, stored)
    assert local.conversation.title == record.title
    assert local.summary == transfer.summary
    assert [item.content for item in local.messages] == [
        "正文里的分隔符必须原样回来：" + separators
    ]


def test_pages_are_followed_page_by_page(bundle: StoreBundle, tmp_path: Path) -> None:
    """``page_size=1``：三条会话走三页，offset 逐页推进（分页游标真在动）。"""
    transfers = [ConversationTransfer(conversation=conversation(index)) for index in (1, 2, 3)]
    importer, seen = make_importer(bundle, transfers, tmp_path, page_size=1)

    report = importer.run()

    assert report.counts["created"] == 3
    # 第四页空着，但"上一页收满了"这条判据要求再问一次才知道到头
    assert [int(item.url.params["offset"]) for item in seen] == [0, 1, 2, 3]
    assert {int(item.url.params["limit"]) for item in seen} == {1}


def test_the_import_keeps_one_thousand_messages_per_second(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """R3 的量化目标：**≥1000 消息/秒**（一个会话一个事务 + ``executemany``）。

    门限给得很宽（本机实测在几万条每秒的量级）：这条断言要抓的是"有人把批量写
    拆成了逐条 execute"这类**数量级**的回退，不是 CI 抖动。
    """
    turns = 60
    transfer = long_conversation(9, turns=turns)
    importer, _seen = make_importer(bundle, [transfer], tmp_path)

    started = time.monotonic()
    report = importer.run()
    elapsed = max(time.monotonic() - started, 1e-6)

    assert report.state == "done", report.error
    assert report.counts["messages"] == turns * 2
    throughput = report.counts["messages"] / elapsed
    assert throughput >= 1000, f"只有 {throughput:.0f} 条消息/秒（目标 ≥1000）"


# ------------------------------------------------------------------ ② 幂等：一行未写


def test_rerunning_the_same_batch_skips_everything_and_writes_nothing(
    bundle: StoreBundle, db: Database, tmp_path: Path
) -> None:
    """重跑同一批 → 全部 ``skipped`` 且**一行未写**（内容表一行都不许动）。"""
    transfers = [
        ConversationTransfer(conversation=conversation(1)),
        long_conversation(2),
        ConversationTransfer(conversation=conversation(3)),
    ]
    importer, _seen = make_importer(bundle, transfers, tmp_path)
    first = importer.run()
    assert first.state == "done"

    before = content_rows(db)
    again = importer.run()

    assert again.state == "done"
    assert again.counts["skipped"] == 3
    assert (again.counts["created"], again.counts["replaced"]) == (0, 0)
    assert [item["reason"] for item in again.counts["skipped_items"]] == [
        module.REASON_ALREADY_IMPORTED
    ] * 3
    assert content_rows(db) == before, "重跑改了内容表"
    assert bundle.ledger is not None
    # 台账：这一批一条都没记（命中的那些不记新账），上一批那三条还在
    assert bundle.ledger.list_import_items(again.batch_id) == []
    assert len(bundle.ledger.list_import_items(first.batch_id)) == 3


# ------------------------------------------------------------------ ③ 覆盖策略


def test_local_change_after_the_import_is_never_overwritten(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """``replace_if_local_untouched``：本机在导入之后又改过 → **跳过并如实列出**。

    这条是整条纪律的核心：用户在本机说过的话，绝不被一次导入静默盖掉。
    """
    first = ConversationTransfer(conversation=conversation(1))
    importer, _seen = make_importer(bundle, [first], tmp_path)
    assert importer.run().state == "done"

    # 本机改这条会话（改名 + 推 updated_at，与真界面上的动作同一条路径）
    bundle.meta.rename_conversation(first.conversation.id, "本机改过的标题")
    bundle.meta.touch_conversation(first.conversation.id)
    changed = bundle.meta.get_conversation(first.conversation.id)
    assert changed is not None

    # 源端也更新了同一会话（updated_at 比原来大）
    newer = ConversationTransfer(
        conversation=conversation(1, updated_at=T0 + timedelta(days=1), title="源端的新标题")
    )
    importer, _seen = make_importer(bundle, [newer], tmp_path)
    report = importer.run()

    assert report.counts["skipped"] == 1 and report.counts["replaced"] == 0
    skipped = report.counts["skipped_items"][0]
    assert skipped["reason"] == module.REASON_LOCAL_NEWER
    assert "没覆盖" in skipped["detail"]
    stored = bundle.meta.get_conversation(first.conversation.id)
    assert stored is not None
    assert stored.title == "本机改过的标题", "本机的改动被覆盖了"
    assert stored.updated_at == changed.updated_at


def test_a_local_conversation_we_did_not_import_is_left_alone(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """本机有这条会话，但它**不是我们导进来的**（没有台账）→ 也不覆盖。"""
    bundle.meta.create_conversation(
        ConversationRecord(id="conv_0042", title="本机自己建的", updated_at=T0)
    )
    source = ConversationTransfer(conversation=conversation(42))
    importer, _seen = make_importer(bundle, [source], tmp_path)

    report = importer.run()

    assert report.counts["skipped"] == 1
    assert report.counts["skipped_items"][0]["reason"] == module.REASON_NOT_OURS
    stored = bundle.meta.get_conversation("conv_0042")
    assert stored is not None and stored.title == "本机自己建的"


def test_plan_is_a_dry_run(bundle: StoreBundle, db: Database, tmp_path: Path) -> None:
    """``plan()`` 一个字节都不写（``--dry-run`` 与端点的 ``dry_run=true`` 走它）。"""
    transfers = [ConversationTransfer(conversation=conversation(1)), long_conversation(2)]
    importer, _seen = make_importer(bundle, transfers, tmp_path)

    plan = importer.plan()

    assert len(plan.created) == 2 and not plan.replaced and not plan.skipped
    assert plan.scanned == 2
    assert plan.counts()["created"] == 2
    assert plan.file_references == 2  # 产物 1 + 附件 1
    assert content_rows(db)["conversations"] == []
    assert bundle.ledger is not None and bundle.ledger.list_import_batches() == []


# ------------------------------------------------------------------ ④ 回滚


def test_rollback_deletes_created_and_restores_replaced(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """回滚两条规则：``created`` 没再动过 → 删；``replaced`` 没再动过 → 用快照恢复。"""
    first = ConversationTransfer(conversation=conversation(1), summary="第一版摘要")
    second = ConversationTransfer(conversation=conversation(2))
    importer, _seen = make_importer(bundle, [first, second], tmp_path)
    first_batch = importer.run()
    assert first_batch.state == "done"

    # 源端更新了 ①，本机没动过 → 这一条会被替换，替换前**必须**存下快照
    updated = ConversationTransfer(
        conversation=conversation(1, updated_at=T0 + timedelta(days=2), title="源端第二版"),
        summary="第二版摘要",
    )
    importer, _seen = make_importer(bundle, [updated], tmp_path)
    second_batch = importer.run()
    assert second_batch.counts["replaced"] == 1 and second_batch.counts["snapshots"] == 1
    snapshot = tmp_path / "data" / ROLLBACK_DIRNAME / second_batch.batch_id / "conv_0001.ndjson"
    assert snapshot.is_file(), "被替换的会话没有存快照"

    stored = bundle.meta.get_conversation("conv_0001")
    assert stored is not None and stored.title == "源端第二版"

    rollback = importer.rollback(second_batch.batch_id)

    assert rollback.state == "rolled_back"
    assert rollback.counts["restored"] == 1 and rollback.counts["deleted"] == 0
    restored = bundle.meta.get_conversation("conv_0001")
    assert restored is not None
    assert restored.title == "旧会话 1", "回滚没有回到导入前那一版"
    assert restored.updated_at == first.conversation.updated_at
    assert bundle.meta.get_conversation_summary("conv_0001")[0] == "第一版摘要"
    assert bundle.ledger is not None
    batch_row = bundle.ledger.get_import_batch(second_batch.batch_id)
    assert batch_row is not None and batch_row.state == "rolled_back"

    # 再回滚第一批：两条都是 created 且此刻都没被本机动过 → 都删掉
    first_rollback = importer.rollback(first_batch.batch_id)
    assert first_rollback.counts["deleted"] == 2
    assert bundle.meta.get_conversation("conv_0001") is None
    assert bundle.meta.get_conversation("conv_0002") is None


def test_rollback_keeps_what_the_user_changed_here(bundle: StoreBundle, tmp_path: Path) -> None:
    """**本机改过的一律保留并如实报数**（回滚不是把用户在本机说过的话抹掉）。"""
    transfers = [
        ConversationTransfer(conversation=conversation(1)),
        ConversationTransfer(conversation=conversation(2)),
    ]
    importer, _seen = make_importer(bundle, transfers, tmp_path)
    batch = importer.run()

    bundle.meta.touch_conversation("conv_0002")
    bundle.meta.rename_conversation("conv_0002", "本机续聊过")

    rollback = importer.rollback(batch.batch_id)

    assert rollback.counts["deleted"] == 1
    assert rollback.counts["kept"] == 1
    kept = rollback.counts["kept_items"][0]
    assert kept["conversation_id"] == "conv_0002"
    assert kept["reason"] == module.REASON_LOCAL_NEWER
    assert "保留" in kept["detail"]
    assert bundle.meta.get_conversation("conv_0001") is None
    survivor = bundle.meta.get_conversation("conv_0002")
    assert survivor is not None and survivor.title == "本机续聊过"


def test_rollback_reports_a_missing_snapshot_instead_of_pretending(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """快照被人删了 → **如实报**（``no_snapshot``），不装作恢复过。"""
    first = ConversationTransfer(conversation=conversation(1), summary="第一版")
    importer, _seen = make_importer(bundle, [first], tmp_path)
    importer.run()
    updated = ConversationTransfer(conversation=conversation(1, updated_at=T0 + timedelta(days=3)))
    importer, _seen = make_importer(bundle, [updated], tmp_path)
    batch = importer.run()
    (tmp_path / "data" / ROLLBACK_DIRNAME / batch.batch_id / "conv_0001.ndjson").unlink()

    rollback = importer.rollback(batch.batch_id)

    assert rollback.counts["no_snapshot"] == 1
    assert rollback.counts["restored"] == 0
    stored = bundle.meta.get_conversation("conv_0001")
    assert stored is not None and stored.title == "旧会话 1"


def test_after_a_rollback_the_same_source_can_be_imported_again(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """回滚之后再导一次**能成**：被回滚过的那条台账不算"已经导过"。"""
    transfers = [ConversationTransfer(conversation=conversation(1))]
    importer, _seen = make_importer(bundle, transfers, tmp_path)
    first = importer.run()
    importer.rollback(first.batch_id)
    assert bundle.meta.get_conversation("conv_0001") is None

    again = importer.run()

    assert again.counts["created"] == 1 and again.counts["skipped"] == 0
    assert bundle.meta.get_conversation("conv_0001") is not None


def test_a_state_change_from_another_process_is_not_masked(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """**另一个进程**（CLI）回滚了批次 A 之后，这个进程里的小缓存不许再拿旧状态说话。

    场景是真实的：界面（这个进程）导过一次、又重跑过一次（第二次会把批次 A 的状态
    记进缓存），而 CLI 在**另一个进程**里回滚了 A——那次写入这个进程的缓存看不见。
    没有"每次操作开始清缓存"的话，第三次导入会全部 ``skipped``，
    而本机一条会话都没有（**静默少数据**，这条链上最坏的结果）。

    两个导入器实例就是两个进程在这件事上的最小模型：缓存不共享，库共享。
    """
    transfers = [ConversationTransfer(conversation=conversation(1))]
    app_side, _seen = make_importer(bundle, transfers, tmp_path)
    first = app_side.run()
    assert first.counts["created"] == 1
    second = app_side.run()  # 第二次：全 skipped（这一步把批次 A 的状态记进缓存）
    assert second.counts["skipped"] == 1

    cli_side, _seen = make_importer(bundle, transfers, tmp_path)  # "另一个进程"
    cli_side.rollback(first.batch_id)
    assert bundle.meta.get_conversation("conv_0001") is None

    again = app_side.run()

    assert again.counts["created"] == 1, again.counts
    assert bundle.meta.get_conversation("conv_0001") is not None


# ------------------------------------------------------------------ ⑤ 断流与坏流


def test_a_truncated_stream_fails_the_batch_and_can_be_resumed(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """没有末行的流 = 半份数据 → 批次 ``failed``，而**已写入的那几条有台账**，重跑续上。"""
    transfers = [ConversationTransfer(conversation=conversation(index)) for index in (1, 2, 3)]

    def truncated(request: httpx.Request) -> httpx.Response:
        page = list(transfers)[: int(request.url.params.get("limit", "200"))]
        lines = [header_line(count=len(page))]
        for index, transfer in enumerate(page):
            if index == 2:  # 第三条发到一半就断
                break
            lines.extend(encode_conversation(transfer))
        return httpx.Response(200, content="".join(lines).encode("utf-8"))

    importer = LegacyImporter(
        bundle,
        data_dir=tmp_path / "data",
        source=NAS,
        transport=httpx.MockTransport(truncated),
    )
    report = importer.run()

    assert report.state == "failed"
    assert "末行" in report.error
    assert report.counts["created"] == 2  # 前两条已经写进去了（一个会话一个事务）
    assert bundle.ledger is not None
    assert len(bundle.ledger.list_import_items(report.batch_id)) == 2

    # 重跑：前两条命中幂等键跳过，第三条补上（R1 的"按会话幂等重跑"）
    resumed_importer, _seen = make_importer(bundle, transfers, tmp_path)
    resumed = resumed_importer.run()
    assert resumed.counts["created"] == 1 and resumed.counts["skipped"] == 2
    assert bundle.meta.get_conversation("conv_0003") is not None


def test_an_unknown_export_version_is_refused(bundle: StoreBundle, tmp_path: Path) -> None:
    """版本不认识 → **当场拒绝**（猜着读一份改了形状的流，等于往库里写半截数据）。"""

    def future(request: httpx.Request) -> httpx.Response:
        body = '{"type":"header","export_version":99,"count":0}\n'
        return httpx.Response(200, content=body.encode("utf-8"))

    importer = LegacyImporter(
        bundle, data_dir=tmp_path / "data", source=NAS, transport=httpx.MockTransport(future)
    )
    report = importer.run()

    assert report.state == "failed"
    assert "版本" in report.error
    assert bundle.ledger is not None
    batch_row = bundle.ledger.get_import_batch(report.batch_id)
    assert batch_row is not None and batch_row.state == "failed"


def test_a_rejected_request_says_so(bundle: StoreBundle, tmp_path: Path) -> None:
    """被拒（4xx）与连不上是两件事：句子里要说得出来（排障第一步就不一样）。"""

    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"token expired")

    importer = LegacyImporter(
        bundle, data_dir=tmp_path / "data", source=NAS, transport=httpx.MockTransport(rejected)
    )
    report = importer.run()

    assert report.state == "failed"
    assert "被拒" in report.error and "401" in report.error


def test_since_is_sent_to_the_source(bundle: StoreBundle, tmp_path: Path) -> None:
    """``since`` 原样进查询参数（增量导入靠它；严格大于的语义在源端那一侧）。"""
    since = T0 + timedelta(days=1)
    transfers = [ConversationTransfer(conversation=conversation(1))]
    importer, seen = make_importer(bundle, transfers, tmp_path, since=since)

    importer.run()

    assert seen, "一次请求都没发"
    assert seen[0].url.params["since"] == since.isoformat()


def test_a_workspace_that_did_not_come_over_is_dropped_and_counted(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """源端挂着 NAS 的工作区 → 置空 + 报数，**不是整批红**（真机抓到的第二个坑）。

    现场（2026-10-01 真机验收，302 条真会话）：`conversations.workspace_id` 在本机库上
    是外键，而工作区不随导入过来 → 导到第 3 条整批 `failed`，错误只有一句
    `FOREIGN KEY constraint failed`。
    """
    record = conversation(9, workspace_id="ws_on_the_nas")
    importer, _seen = make_importer(
        bundle, [ConversationTransfer(conversation=record)], tmp_path
    )

    plan = importer.plan()
    assert plan.unresolved_workspaces == 1

    report = importer.run()

    assert report.state == "done", report.error
    assert report.counts["created"] == 1
    assert report.counts["unresolved_workspaces"] == 1
    stored = bundle.meta.get_conversation(record.id)
    assert stored is not None
    # 会话本身一条不少，掉的是"归属"（同时**不许**凭空造一条工作区出来）
    assert stored.workspace_id is None
    assert bundle.meta.get_workspace("ws_on_the_nas") is None


def test_a_workspace_that_exists_here_keeps_its_binding(
    bundle: StoreBundle, tmp_path: Path
) -> None:
    """本机**有**这条工作区（同一台机器上本来就有）→ 引用原样留着，一个数都不报。"""
    bundle.meta.create_workspace(
        WorkspaceRecord(
            id="ws_local",
            owner_id="usr_owner",
            name="本机项目",
            root_path=str(tmp_path / "root"),
            created_at=T0,
            updated_at=T0,
        )
    )
    record = conversation(10, workspace_id="ws_local")
    importer, _seen = make_importer(
        bundle, [ConversationTransfer(conversation=record)], tmp_path
    )

    report = importer.run()

    assert report.state == "done", report.error
    assert report.counts["unresolved_workspaces"] == 0
    stored = bundle.meta.get_conversation(record.id)
    assert stored is not None and stored.workspace_id == "ws_local"


def test_the_batch_survives_the_process_for_polling(bundle: StoreBundle, tmp_path: Path) -> None:
    """进度写进库里：另一个进程（界面或 CLI）查得到**同一份账**。"""
    transfers = [ConversationTransfer(conversation=conversation(1))]
    importer, _seen = make_importer(bundle, transfers, tmp_path)
    report = importer.run()

    assert bundle.ledger is not None
    batch = bundle.ledger.get_import_batch(report.batch_id)
    assert batch is not None
    assert batch.state == "done" and batch.counts["created"] == 1
    assert batch.source == NAS
    assert [item.id for item in bundle.ledger.list_import_batches()] == [report.batch_id]


# ------------------------------------------------------------------ ⑥ CLI


def read_cli_database(data_dir: Path) -> tuple[list[str], int]:
    """另开一条连接读 CLI 建出来的那个库：``(会话标题, 台账批次数)``。"""
    handle = Database(data_dir / "kylab.db", allow_multiple_instances=True)
    handle.open()
    try:
        with handle.read() as conn:
            titles = [row["title"] for row in conn.execute("SELECT title FROM conversations")]
            batches = conn.execute("SELECT count(*) AS n FROM imports").fetchone()["n"]
    finally:
        handle.close()
    return titles, int(batches)


def test_cli_imports_into_the_data_dir_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m app.services.legacy_import --server … --data-dir …`` 真能用。

    断言的正是 CLI 的两条承诺：① 库落在 ``--data-dir``（不是 cwd 下的 ``./data``）；
    ② 它与端点**共用同一个导入器**（台账、快照、报告的形状完全一样）。
    """
    data_dir = tmp_path / "cli-data"
    monkeypatch.setenv("KYLAB_DATA_DIR", str(data_dir))
    transfers = [ConversationTransfer(conversation=conversation(1))]
    transport, seen = fake_nas(transfers)
    try:
        code = module.main(
            ["--server", NAS, "--token", "t", "--data-dir", str(data_dir), "--quiet"],
            transport=transport,
        )
    finally:
        get_settings.cache_clear()
        reset_stores()

    assert code == 0
    assert seen, "CLI 没有去拉源端"
    assert (data_dir / "kylab.db").is_file(), "库没落在 --data-dir 下"
    titles, batches = read_cli_database(data_dir)
    assert titles == ["旧会话 1"]
    assert batches == 1


def test_cli_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run``：报告里说得清清楚楚，库里一个字节都没写。"""
    data_dir = tmp_path / "cli-dry"
    monkeypatch.setenv("KYLAB_DATA_DIR", str(data_dir))
    transfers = [ConversationTransfer(conversation=conversation(1))]
    transport, _seen = fake_nas(transfers)
    try:
        code = module.main(
            ["--server", NAS, "--data-dir", str(data_dir), "--dry-run", "--quiet"],
            transport=transport,
        )
    finally:
        get_settings.cache_clear()
        reset_stores()

    assert code == 0
    titles, batches = read_cli_database(data_dir)
    assert titles == [] and batches == 0


def test_cli_reports_an_unreachable_source_as_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连不上 → 一句话 + 退出码 1（不甩 traceback：这个命令是给排障的人用的）。"""

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    data_dir = tmp_path / "cli-dead"
    monkeypatch.setenv("KYLAB_DATA_DIR", str(data_dir))
    try:
        code = module.main(
            ["--server", NAS, "--data-dir", str(data_dir), "--dry-run"],
            transport=httpx.MockTransport(dead),
        )
    finally:
        get_settings.cache_clear()
        reset_stores()
    assert code == 1


def test_since_accepts_a_plain_date() -> None:
    """``--since 2026-10-01`` 也认（按 UTC 当天零点）——**按 UTC 而不是本机时区**。"""
    assert module._parse_since("") is None
    assert module._parse_since("2026-10-01") == datetime(2026, 10, 1, tzinfo=UTC)
    assert module._parse_since("2026-10-01T08:00:00+08:00") == datetime(
        2026, 10, 1, 0, 0, tzinfo=UTC
    )
