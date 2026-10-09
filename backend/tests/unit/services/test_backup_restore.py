"""按点恢复（M5 阶段 5，方案 §3.4 的 11 步）。

镜像同构：``app/services/backup_restore.py`` → ``tests/unit/services/test_backup_restore.py``。

## 真的是什么、假的是什么

| 件 | 这一轮的形态 |
| --- | --- |
| 源端（NAS） | 假的：一个只回"清单 + 包体"的替身（**一个字节都不上网络**） |
| 那台机器的包 | **真的**：`BackupSnapshotService.create` 打出来的（真布局、真清单、真擦洗） |
| 本机库 | **真的**：本机档 `build_stores()`（真导入台账、真擦洗副本、真打包器） |
| 恢复器 | **真的**：`BackupRestorer`（下载 / 解包 / 导入 / 记忆 / 设置 / 产物落位全走真实现） |
| 会话那条链 | **真的**：`LegacyImporter`（升级前的幂等键、三条覆盖规则、一次会话一个事务） |

包用真打包器打、本机库用真装配，是为了让"恢复回来的东西与源逐字一致"这件事**可验**：
两边都不是手写形状，将来布局变了这条用例会当场红，而不是绿着而契约已经漂了。

## 五条安全栏各由哪条用例钉

| 安全栏 | 用例 |
| --- | --- |
| 1 sha256 不符即中止、不落位 | ``test_a_package_that_fails_its_digest_aborts_untouched`` |
| 2 解包路径不越界 | ``test_a_package_member_that_escapes_the_staging_dir_is_refused`` |
| 3 冲突不覆盖 | ``test_artifact_bytes_that_differ_are_reported_not_overwritten`` |
| 4 凭据一个都不写 | ``test_a_credential_in_the_package_is_never_written`` |
| 5 ``pre_restore`` 不入队 | ``test_the_pre_restore_lands_locally_and_is_never_queued`` |
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.storage import build_stores, reset_stores
from app.services import backup_restore as module
from app.services.backup_restore import (
    RESTORE_BACKUP_DIRNAME,
    RESTORE_STAGING_DIRNAME,
    BackupRestoreError,
    BackupRestorer,
    SnapshotFileSource,
)
from app.services.backup_snapshot import MEMBER_DB, BackupSnapshotService, SnapshotResult
from app.services.conversation_export import read_transfer
from app.services.legacy_import import (
    REASON_ALREADY_IMPORTED,
    REASON_LOCAL_NEWER,
    REASON_NOT_OURS,
    TransferSource,
)
from app.services.remote_clients import RemoteUnavailableError
from app.services.runtime_config import SECRET_KEYS, RuntimeConfigService
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ChatMessageRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
    ModelProviderRecord,
    SessionEventRecord,
    SnapshotDbView,
)

pytestmark = pytest.mark.local

DEVICE = "dev-restore-0001"
SEGMENT = "2026-10-05T08-03-00Z-ab12cd34"
SOURCE = f"backup://{DEVICE}/{SEGMENT}"
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

PLAIN_KEY = "llm.temperature"
"""一个**普通**设置：恢复该把它补上（本机没有时）。"""

SECRET_KEY = "web.search_api_key"
assert SECRET_KEY in SECRET_KEYS, "这条用例要挑一个真在 SECRET_KEYS 里的键，别手抄一个"
SECRET_SENTINEL = "tavily_SENTINEL_restore_8c1f"
MODEL_SENTINEL = "kylab_sk_SENTINEL_model_restore"

ARTIFACT_KEY = "conversations/conv_0001/report.docx"
ARTIFACT_BYTES = b"object-side artifact bytes"

CONTENT_TABLES = (
    "conversations",
    "chat_messages",
    "session_events",
    "conversation_artifacts",
    "app_settings",
    "imports",
    "backup_snapshots",
)


# ------------------------------------------------------------------ 夹具


@pytest.fixture(autouse=True)
def _clean_stores() -> Iterator[None]:
    """每个用例前后把进程级存储缓存清掉（本机档的库落在各自 ``tmp_path`` 下）。"""
    reset_stores()
    yield
    reset_stores()


def _settings(data_dir: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=data_dir
    )


def _conversation(index: int, *, title: str = "") -> ConversationRecord:
    return ConversationRecord(
        id=f"conv_{index:04d}",
        title=title or f"旧会话 {index}",
        kb_ids=("kb_1",),
        owner_id="usr_owner",
        pinned=index == 1,
        created_at=T0 + timedelta(minutes=index),
        updated_at=T0 + timedelta(minutes=index),
    )


def _transfer(index: int) -> ConversationTransfer:
    """一条有正文、有事件、有产物的会话（"恢复要保住的东西"全在）。"""
    record = _conversation(index)
    messages = [
        ChatMessageRecord(
            id=f"msg_{index}_q",
            conversation_id=record.id,
            role="user",
            content=f"第 {index} 条会话的问题",
            created_at=T0 + timedelta(minutes=index),
        ),
        ChatMessageRecord(
            id=f"msg_{index}_a",
            conversation_id=record.id,
            role="assistant",
            content=f"第 {index} 条会话的回答" + "正文" * 40,
            sources=({"index": 1, "document_name": "指南.pdf"},),
            steps=({"tool": "search", "status": "done"},),
            thinking="这一轮的推理",
            created_at=T0 + timedelta(minutes=index, seconds=1),
        ),
    ]
    return ConversationTransfer(
        conversation=record,
        summary=f"第 {index} 条会话的压缩摘要",
        summary_upto=messages[-1].id,
        messages=messages,
        events=[
            SessionEventRecord(
                conversation_id=record.id,
                seq=1,
                kind="turn/start",
                payload={"query": f"第 {index} 问"},
                created_at=T0 + timedelta(minutes=index),
            )
        ],
        artifacts=[
            ConversationArtifactRecord(
                id=f"art_{index}",
                conversation_id=record.id,
                name="报告.docx",
                format="docx",
                size_bytes=len(ARTIFACT_BYTES),
                storage=ARTIFACT_IN_OBJECTS,
                location=ARTIFACT_KEY if index == 1 else f"conversations/{record.id}/报告.docx",
                created_at=T0 + timedelta(minutes=index, seconds=2),
            )
        ],
    )


def _seed_source(stores: Any, data_dir: Path, *, count: int = 2) -> list[ConversationTransfer]:
    """把"那台机器上的东西"种进源库：会话 + 记忆 + 设置 + 模型供应商 + 一份产物字节。"""
    transfers = []
    for index in range(1, count + 1):
        transfer = _transfer(index)
        stores.ledger.write_imported_conversation(transfer)
        transfers.append(transfer)
    artifact = data_dir / ARTIFACT_KEY
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(ARTIFACT_BYTES)
    memory = data_dir / "memory" / "usr_owner"
    (memory / "mem_metadata").mkdir(parents=True, exist_ok=True)
    (memory / "MEMORY.md").write_text("# 记忆\n这台机器上学到的东西。\n", encoding="utf-8")
    (memory / "mem_metadata" / "index.json").write_text('{"rows": 1}', encoding="utf-8")
    stores.meta.set_setting(PLAIN_KEY, "0.42")
    stores.meta.set_setting(SECRET_KEY, SECRET_SENTINEL)
    stores.meta.create_model_provider(
        ModelProviderRecord(
            id="mp_1",
            kind="openai",
            name="供应商甲",
            base_url="https://api.example.com/v1",
            api_key=MODEL_SENTINEL,
            created_at=T0,
            updated_at=T0,
        )
    )
    return transfers


def _packer(stores: Any, data_dir: Path) -> BackupSnapshotService:
    return BackupSnapshotService(
        stores=stores,
        data_dir=data_dir,
        device_id=DEVICE,
        runtime_config=RuntimeConfigService(stores, _settings(data_dir)),
    )


SOURCE_DATA_DIRNAME = "source"
"""那台机器（源端）的数据目录名——``source`` 夹具建的就是它。"""


@dataclass
class Source:
    """那台机器的现场：库 + 数据目录 + **已经打好的那一份包**。

    用例可以在这里"再打一份"（改了源库之后要第二份包时用）：按点恢复这条链要比的
    往往是"源端新一版 vs 本机改过"——那需要两份内容不同的包，而不是一份。
    """

    stores: Any
    data_dir: Path
    result: SnapshotResult

    @property
    def blob_path(self) -> Path:
        return Path(self.result.blob_path)

    def pack_again(self, name: str = "second") -> SnapshotResult:
        """照现在的源库再打一份（落在另一个目录，免得与第一份混在一起）。"""
        return _packer(self.stores, self.data_dir).create(
            into=self.data_dir / "backup" / name, kind="manual"
        )


@pytest.fixture
def source(tmp_path: Path) -> Iterator[Source]:
    """**一份真的快照** + 打它的那台机器（真布局 / 真清单 / 真擦洗）。"""
    data_dir = tmp_path / SOURCE_DATA_DIRNAME
    stores = build_stores(_settings(data_dir))
    assert stores.snapshot is not None and stores.ledger is not None
    _seed_source(stores, data_dir)
    result = _packer(stores, data_dir).create(into=data_dir / "backup" / "pending", kind="manual")
    yield Source(stores=stores, data_dir=data_dir, result=result)


@pytest.fixture
def source_db(source: Source) -> Path:
    """源库文件（打包器读的那一份）。"""
    path = source.data_dir / "kylab.db"
    assert path.is_file()
    return path


class FakeProvider:
    """一个**假提供者**：清单与包体都从本地取（一个字节都不上网络）。

    只实现恢复这条链真正用到的那三个方法（``status`` / ``manifest`` / ``download_snapshot``）
    ——这与 ``BackupProviderClient`` 的窄公共面一致，所以服务层哪天多用了一个方法，
    这里会当场 ``AttributeError`` 而不是静默通过。
    """

    def __init__(self, result: SnapshotResult) -> None:
        self.manifest_calls = 0
        self.downloads = 0
        self.unavailable = ""
        self.digest_mismatch = False
        self.serve(result)

    def serve(self, result: SnapshotResult) -> None:
        """换成另一份包（用例打第二份快照时用）。"""
        self.payload: dict[str, Any] = json.loads(result.manifest_bytes)
        self.blob = Path(result.blob_path).read_bytes()

    def status(self, refresh: bool = False) -> Any:
        return SimpleNamespace(
            available=not self.unavailable,
            reason=self.unavailable,
            state="ready" if not self.unavailable else "unavailable",
            checked_at=T0,
            base_url="http://nas.test/api/v1",
        )

    def manifest(self, device_id: str, snapshot_id: str) -> dict[str, Any] | None:
        self.manifest_calls += 1
        if self.unavailable:
            return None
        payload = json.loads(json.dumps(self.payload))
        payload["snapshot_id"] = snapshot_id
        return payload

    def download_snapshot(
        self, device_id: str, snapshot_id: str, dest: str | Path
    ) -> dict[str, Any]:
        self.downloads += 1
        if self.digest_mismatch:
            # 真实现是"落盘之后发现摘要不符 → 把那半份删掉再抛"（``download_snapshot``
            # 自己的用例钉着那两下）；这里替身直接抛，正是它留给调用方的净效果。
            raise RemoteUnavailableError(
                "下载快照：落盘的字节与服务端声明的 sha256 不符——这一份已经删掉，绝不落位"
            )
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.blob)
        return {"verified": True, "bytes": len(self.blob)}


@pytest.fixture
def provider(source: Source) -> FakeProvider:
    return FakeProvider(source.result)


@pytest.fixture
def machine(tmp_path: Path, provider: FakeProvider) -> Iterator[tuple[Any, BackupRestorer, Path]]:
    """**另一台机器**（恢复的目的地）：干净的本机档 + 真打包器 + 假提供者。"""
    data_dir = tmp_path / "target"
    stores = build_stores(_settings(data_dir))
    restorer = BackupRestorer(
        stores=stores,
        data_dir=data_dir,
        provider=provider,
        snapshotter=_packer(stores, data_dir),
        runtime_config=RuntimeConfigService(stores, _settings(data_dir)),
    )
    yield stores, restorer, data_dir


# ------------------------------------------------------------------ 判据小工具


def _tables(data_dir: Path) -> dict[str, list[tuple]]:
    """直读那几张表（"一个字节都没写"这种事得绕过仓储才看得见）。"""
    with sqlite3.connect(data_dir / "kylab.db") as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608
            for table in CONTENT_TABLES
        }


def _memory_tree(data_dir: Path) -> dict[str, bytes]:
    root = data_dir / "memory"
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _summary(data_dir: Path) -> dict[str, Any]:
    """这台机器此刻的事实摘要（预演前后必须一字不差）。"""
    return {"tables": _tables(data_dir), "memory": _memory_tree(data_dir)}


def _content_first(tables: dict[str, list[tuple]]) -> dict[str, list[tuple]]:
    """除台账以外的表。

    台账那一张**单独看**：失败**也要**留一行（那是"这次恢复发生过"的记录，见 ``_finish``），
    而其余每一张表"一个字节都没写"才是一条失败路径该有的样子。
    """
    return {name: rows for name, rows in tables.items() if name != "imports"}


def _reasons(counts: dict[str, Any]) -> dict[str, str]:
    return {item["conversation_id"]: item["reason"] for item in counts["skipped_items"]}


def _hostile_archive(path: Path, member: tarfile.TarInfo | tuple[str, str]) -> Path:
    """手工打一份**恶意包**：``db/kylab.db`` 照常放着，另一个成员想跑到外面去。

    ``member`` 给 ``(名字, 类型)`` 或一个现成的 ``TarInfo``（链接那两档要它）。
    """
    with tarfile.open(path, "w:gz") as archive:
        db_member = tarfile.TarInfo(MEMBER_DB)
        db_member.size = 4
        archive.addfile(db_member, io.BytesIO(b"junk"))
        if isinstance(member, tuple):
            name, kind = member
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
                return path
            if kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = MEMBER_DB
                archive.addfile(info)
                return path
            info.size = 5
            info.name = name
            archive.addfile(info, io.BytesIO(b"evil!"))
            return path
        archive.addfile(member)
        return path


# ------------------------------------------------------------------ ① 预演：一个字节都不写


def test_the_plan_answers_before_anything_is_written(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider, tmp_path: Path
) -> None:
    """dry-run 那份报告四件事逐条，且**本机库 / 记忆 / 产物一个字节都没动**（方案第 2 步）。

    它**会**下载并解包（"这一条会怎么处理"要与本机台账逐条比，必须读包里的库）——
    所以还要断言包真的取回来了（否则这一条就退化成"什么都没做也算过"）。
    """
    _stores, restorer, data_dir = machine
    before = _summary(data_dir)

    plan = restorer.plan(DEVICE, SEGMENT)

    assert provider.manifest_calls == 1 and provider.downloads == 1, "清单与包都取了"
    assert [item["conversation_id"] for item in plan.created] == ["conv_0002", "conv_0001"]
    assert plan.replaced == [] and plan.skipped == []
    assert plan.source == SOURCE
    assert plan.package["schema_version"] >= 1
    assert plan.package["counts"]["conversations"] == 2
    assert plan.artifacts["in_package"] == 1 and plan.artifacts["object"] == 1
    assert plan.artifacts["workspace"] == 0
    assert plan.memory["in_package"] == 2 and plan.memory["will_copy"] == 2
    assert PLAIN_KEY in plan.settings["will_fill"]
    assert SECRET_KEY not in plan.settings["will_fill"], "快照里本来就没有它（擦洗过）"
    assert any("供应商甲" in line for line in plan.credentials_to_configure)
    assert any("NAS" in line for line in plan.credentials_to_configure)
    assert plan.as_dict()["counts"] == {"created": 2, "replaced": 0, "skipped": 0}

    assert _summary(data_dir) == before, "预演不碰本机：库 / 记忆逐字节相同"
    staging = data_dir / RESTORE_STAGING_DIRNAME / SEGMENT
    assert (staging / MEMBER_DB).is_file(), "包确实下载 + 解包到暂存区了"
    assert (staging / "memory" / "usr_owner" / "MEMORY.md").is_file()
    assert not (data_dir / ARTIFACT_KEY).exists(), "产物字节没有落位"


def test_a_preview_writes_no_ledger_row(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """预演不产批次（它是"看"，不是"跑"）：台账里一行都不该多出来。"""
    stores, restorer, _data_dir = machine

    restorer.plan(DEVICE, SEGMENT)

    assert stores.ledger.list_import_batches(limit=10) == []


# ------------------------------------------------------------------ ② 干净机器：全量重建


def test_a_clean_machine_is_rebuilt_completely(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """一台空机器恢复完：**会话 / 消息 / 事件 / 产物 / 记忆 / 设置逐字与源一致**。

    判据用的是两条**真读法**：会话那条走 ``conversation_export.read_transfer``
    （与导出、回滚同一个读），资产那几条直接比字节。
    """
    stores, restorer, data_dir = machine
    batch_id = restorer.begin(DEVICE, SEGMENT)
    assert stores.ledger.get_import_batch(batch_id).state == "planned"

    report = restorer.restore(DEVICE, SEGMENT, batch_id=batch_id)

    assert provider.downloads == 1, "真恢复自己下一次（预演才复用 staging）"
    assert report.state == "done", report.error
    assert report.batch_id == batch_id
    counts = report.counts
    assert counts["created"] == 2 and counts["messages"] == 4 and counts["events"] == 2
    assert counts["artifacts"] == 2
    section = counts["restore"]
    assert section["artifacts"]["restored"] == 1
    assert section["artifacts"]["identical"] == 0 and section["artifacts"]["conflicts"] == 0
    assert section["memory"]["copied"] == 2 and section["memory"]["skipped_existing"] == 0
    assert PLAIN_KEY in section["settings"]["filled"]
    assert section["staging"] == str(data_dir / RESTORE_STAGING_DIRNAME / SEGMENT)
    assert section["seconds"] >= 0
    assert report.credentials_to_configure == section["credentials_to_configure"]

    # 会话那一条链读回来的东西与源逐字一致（同一套读法，见 conversation_export）
    for index in (1, 2):
        expected = _transfer(index)
        local = read_transfer(stores, stores.meta.get_conversation(expected.conversation.id))
        assert local.conversation.title == expected.conversation.title
        assert local.conversation.pinned == expected.conversation.pinned
        assert local.conversation.updated_at == expected.conversation.updated_at
        assert local.summary == expected.summary and local.summary_upto == expected.summary_upto
        assert [item.id for item in local.messages] == [item.id for item in expected.messages]
        assert [item.content for item in local.messages] == [
            item.content for item in expected.messages
        ]
        assert [item.thinking for item in local.messages] == [
            item.thinking for item in expected.messages
        ]
        assert [item.seq for item in local.events] == [item.seq for item in expected.events]
        assert [item.payload for item in local.events] == [item.payload for item in expected.events]
        assert [item.location for item in local.artifacts] == [
            item.location for item in expected.artifacts
        ]
        assert [item.storage for item in local.artifacts] == [
            item.storage for item in expected.artifacts
        ]

    # 记忆与产物字节真的落位了（与源**逐字节**相同）
    landed = data_dir / "memory" / "usr_owner" / "MEMORY.md"
    assert landed.read_text(encoding="utf-8").startswith("# 记忆")
    assert (data_dir / "memory" / "usr_owner" / "mem_metadata" / "index.json").is_file()
    assert (data_dir / ARTIFACT_KEY).read_bytes() == ARTIFACT_BYTES
    # 设置：普通键补上、凭据键一个字都没写
    assert stores.meta.get_setting(PLAIN_KEY) == "0.42"
    assert stores.meta.get_setting(SECRET_KEY) is None

    # 台账里**只有一条**（恢复就是"用快照当来源的一次导入"，进度与回滚复用那两个端点）
    rows = stores.ledger.list_import_batches(limit=10)
    assert len(rows) == 1
    assert rows[0].id == batch_id and rows[0].source == SOURCE and rows[0].state == "done"
    assert rows[0].counts["restore"]["artifacts"]["restored"] == 1


# ------------------------------------------------------------------ ③ 本机改过：保留 + 如实报


def test_a_second_restore_keeps_what_the_user_changed_here(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider, source: Source
) -> None:
    """源端新一版 + 本机改过 → ``local_newer``；源端没变的 → ``already_imported``。

    两条都不覆盖，正是"恢复不会把用户在本机说过的话抹掉"那条纪律的判据
    （与 M2 导入同一条规则）。**两档同时在场**才说明规则是按条判的，不是一刀切：
    要走到 ``local_newer`` 那一档，源端必须也变了（否则先命中幂等键）。
    """
    stores, restorer, data_dir = machine
    first = restorer.restore(DEVICE, SEGMENT)
    assert first.counts["created"] == 2

    stores.meta.touch_conversation("conv_0001")  # 用户在本机又聊了一轮
    source.stores.meta.touch_conversation("conv_0001")  # 那台机器上这条也更新了
    provider.serve(source.pack_again())  # 于是有第二份包（内容与第一份不同）

    again = restorer.restore(DEVICE, SEGMENT)

    assert again.state == "done", again.error
    assert again.counts["created"] == 0 and again.counts["replaced"] == 0
    assert _reasons(again.counts) == {
        "conv_0001": REASON_LOCAL_NEWER,
        "conv_0002": REASON_ALREADY_IMPORTED,
    }
    assert stores.meta.get_conversation("conv_0001").title == "旧会话 1"
    # 重跑是常态：记忆与产物那一遍如实报"已经在本地了"
    section = again.counts["restore"]
    assert section["artifacts"]["identical"] == 1 and section["artifacts"]["restored"] == 0
    assert section["memory"]["skipped_existing"] == 2 and section["memory"]["copied"] == 0
    assert PLAIN_KEY in section["settings"]["kept_local"]
    assert (data_dir / ARTIFACT_KEY).read_bytes() == ARTIFACT_BYTES


def test_a_conversation_that_is_not_ours_is_never_overwritten(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """本机那条**不是我们导进来的**（台账里没有它）→ ``not_ours`` + 原样保留。"""
    stores, restorer, _data_dir = machine
    own = _conversation(1, title="本机自己的标题")
    stores.meta.create_conversation(own)

    report = restorer.restore(DEVICE, SEGMENT)

    assert report.state == "done", report.error
    assert report.counts["created"] == 1, "只有 conv_0002 进来了"
    assert _reasons(report.counts) == {"conv_0001": REASON_NOT_OURS}
    stored = stores.meta.get_conversation("conv_0001")
    assert stored is not None and stored.title == "本机自己的标题"


def test_local_settings_and_memory_are_only_filled_when_missing(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """设置**只补本机没有的键**、记忆**默认只补不覆盖**——两条都是"本机是权威"。

    ``overwrite_memory=True`` 时才覆盖（那是用户明确勾的动作）。
    """
    stores, restorer, data_dir = machine
    stores.meta.set_setting(PLAIN_KEY, "0.99")  # 本机自己改过这一项
    memory = data_dir / "memory" / "usr_owner"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "MEMORY.md").write_text("本机改过的记忆", encoding="utf-8")

    kept = restorer.restore(DEVICE, SEGMENT)

    section = kept.counts["restore"]
    assert stores.meta.get_setting(PLAIN_KEY) == "0.99", "本机改过的设置不覆盖"
    assert PLAIN_KEY in section["settings"]["kept_local"]
    assert section["memory"]["skipped_files"] == ["usr_owner/MEMORY.md"]
    assert (memory / "MEMORY.md").read_text(encoding="utf-8") == "本机改过的记忆"
    assert (memory / "mem_metadata" / "index.json").is_file(), "缺的那一份照补"

    overwritten = restorer.restore(DEVICE, SEGMENT, overwrite_memory=True)

    memory_section = overwritten.counts["restore"]["memory"]
    assert memory_section["copied"] == 2 and memory_section["skipped_existing"] == 0
    assert "usr_owner/MEMORY.md" in memory_section["copied_files"]
    assert (memory / "MEMORY.md").read_text(encoding="utf-8").startswith("# 记忆")


# ------------------------------------------------------------------ ④ 三条安全栏


def test_artifact_bytes_that_differ_are_reported_not_overwritten(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """同 Key 不同内容 → **如实报冲突并跳过**（绝不静默覆盖，与 NAS 侧那条 409 同源）。"""
    _stores, restorer, data_dir = machine
    target = data_dir / ARTIFACT_KEY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("本机自己改过的字节".encode())

    report = restorer.restore(DEVICE, SEGMENT)

    section = report.counts["restore"]["artifacts"]
    assert section["conflicts"] == 1 and section["conflict_keys"] == [ARTIFACT_KEY]
    assert section["restored"] == 0 and section["identical"] == 0
    assert target.read_bytes() == "本机自己改过的字节".encode()
    assert report.state == "done", "冲突不是失败：会话那一段照常恢复"


def test_a_package_that_fails_its_digest_aborts_untouched(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """安全栏 1：下载对不上摘要 → **中止**，本机库 / 记忆 / 兜底一个字节都没写。"""
    stores, restorer, data_dir = machine
    before = _summary(data_dir)
    provider.digest_mismatch = True

    report = restorer.restore(DEVICE, SEGMENT)

    assert report.state == "failed"
    assert "sha256" in report.error
    assert _content_first(_tables(data_dir)) == _content_first(before["tables"])
    assert not (data_dir / RESTORE_BACKUP_DIRNAME).exists(), "兜底都没打（顺序：下载在校验里）"
    staging = data_dir / RESTORE_STAGING_DIRNAME / SEGMENT
    assert not (staging / MEMBER_DB).exists(), "没落位"
    row = stores.ledger.get_import_batch(report.batch_id)
    assert row is not None and row.state == "failed" and "sha256" in row.error


@pytest.mark.parametrize(
    ("member", "fragment"),
    [
        (("../../escape.txt", "file"), "跑到外面去"),
        (("/abs.txt", "file"), "不是安全的相对路径"),
        (("a\\b.txt", "file"), "不是安全的相对路径"),
        (("link.txt", "symlink"), "不是普通文件的成员"),
        (("link.txt", "hardlink"), "不是普通文件的成员"),
    ],
)
def test_a_package_member_that_escapes_the_staging_dir_is_refused(
    machine: tuple[Any, BackupRestorer, Path],
    provider: FakeProvider,
    tmp_path: Path,
    member: tuple[str, str],
    fragment: str,
) -> None:
    """安全栏 2：绝对路径 / ``..`` / 反斜杠 / 链接一律拒，**且真的什么都没落出去**。

    解包一次都不调 ``extractall``（它把"成员名怎么写"整件事交给 tar），逐成员自己写、
    自己核；``plan()`` 与 ``restore()`` 两条路都要走这道门。
    """
    _stores, restorer, data_dir = machine
    hostile = _hostile_archive(tmp_path / "hostile.tar.gz", member)
    provider.blob = hostile.read_bytes()

    with pytest.raises(BackupRestoreError) as raised:
        restorer.plan(DEVICE, SEGMENT)
    assert fragment in str(raised.value)

    report = restorer.restore(DEVICE, SEGMENT)
    assert report.state == "failed" and fragment in report.error
    assert list(tmp_path.rglob("escape.txt")) == []
    assert list(tmp_path.rglob("abs.txt")) == []
    assert not (data_dir / RESTORE_BACKUP_DIRNAME).exists()


def test_a_credential_in_the_package_is_never_written(
    tmp_path: Path, source_db: Path, provider: FakeProvider
) -> None:
    """安全栏 4：包里的库**没擦洗过**时，凭据类键仍然一个都不写（防漂那道门）。

    现场是怎么造出来的：把源库**照文件拷一份**（不擦洗）塞进 ``db/kylab.db``——
    正是"用户手工塞了一份库进快照"那种来路不明的包。擦洗本该在打包那一层做过，
    所以这一道是**第二道**；它必须自己站得住。

    拷贝用 SQLite 的在线备份 API（不是 ``cp``）：源库还在被那个进程用着，
    照文件拷会漏掉 WAL 里那几帧。拷出来的那份再改成 ``journal_mode=delete``——
    一份"只有一个文件"的库才读得开（``SnapshotSource`` 那份契约要的就是它）。
    """
    unscrubbed = tmp_path / "unscrubbed.db"
    with sqlite3.connect(source_db) as src, sqlite3.connect(unscrubbed) as dst:
        src.backup(dst)
        dst.commit()
        dst.execute("PRAGMA journal_mode = DELETE")
    hostile = tmp_path / "unscrubbed.tar.gz"
    with tarfile.open(hostile, "w:gz") as archive:
        archive.add(unscrubbed, arcname=MEMBER_DB)
    provider.blob = hostile.read_bytes()

    data_dir = tmp_path / "target"
    stores = build_stores(_settings(data_dir))
    restorer = BackupRestorer(
        stores=stores,
        data_dir=data_dir,
        provider=provider,
        snapshotter=_packer(stores, data_dir),
        runtime_config=RuntimeConfigService(stores, _settings(data_dir)),
    )

    plan = restorer.plan(DEVICE, SEGMENT)
    assert SECRET_KEY in plan.settings["excluded"]

    report = restorer.restore(DEVICE, SEGMENT)

    assert report.state == "done", report.error
    assert SECRET_KEY in report.counts["restore"]["settings"]["excluded"]
    assert stores.meta.get_setting(SECRET_KEY) is None, "凭据要重填，不是从包里搬回来"
    assert stores.meta.get_setting(PLAIN_KEY) == "0.42", "无关的键照旧补上"


def test_the_pre_restore_snapshot_lands_locally_and_is_never_queued(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """安全栏 5：恢复前那份兜底**只落本机**（``restore-backup/<ts>/``）且**不入队**。

    它是"按错了"的第一道兜底，不是一份要传出去的备份——队列那一侧也有一条显式的拒绝，
    两处同一条边界（见 ``backup_queue``）。
    """
    stores, restorer, data_dir = machine

    report = restorer.restore(DEVICE, SEGMENT)
    snapshot_id = report.counts["restore"]["pre_restore_snapshot"]

    root = data_dir / RESTORE_BACKUP_DIRNAME
    landed = list(root.glob("*/*.tar.gz"))
    assert len(landed) == 1 and landed[0].name == f"{snapshot_id}.tar.gz"
    assert len(list(root.glob("*"))) == 1, "兜底按时刻分目录，一次恢复只落一份"
    assert not (data_dir / "backup" / "pending").exists(), "不是队列那个落点"
    assert stores.backup_queue.list_backup_snapshots(limit=50) == [], "队列里没有它"
    assert stores.backup_queue.get_backup_snapshot(snapshot_id) is None


def test_the_restore_fails_as_a_record_when_the_provider_is_unavailable(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """取不到清单时**不抛**：写一条 ``failed`` 台账并返回报告（后台线程那条路要的）。

    端点那一侧只有 dry-run 会往外抛（那种情况由端点翻成 502）——真恢复永远留一份记录。
    """
    stores, restorer, _data_dir = machine
    provider.unavailable = "连不上 NAS（用例）"

    report = restorer.restore(DEVICE, SEGMENT)

    assert report.state == "failed" and "连不上 NAS" in report.error
    row = stores.ledger.get_import_batch(report.batch_id)
    assert row is not None and row.state == "failed" and "连不上 NAS" in row.error


# ------------------------------------------------------------------ ⑤ 接缝那一侧


def test_snapshot_file_source_is_just_another_transfer_source(
    tmp_path: Path, machine: tuple[Any, BackupRestorer, Path], source: Source
) -> None:
    """``SnapshotFileSource`` 满足 ``TransferSource``：逐条吐的来源因此可以换。

    顺带钉住 ``since`` 那一条口径（**严格晚于**，与导出端点同一句）：按点恢复这条路
    永远不传它（恢复要的是"当时全量"），留着是为了协议完整。
    """
    stores, _restorer, _data_dir = machine
    db_path = tmp_path / "unpacked" / "kylab.db"
    db_path.parent.mkdir(parents=True)
    with tarfile.open(source.blob_path, "r:gz") as archive:
        db_path.write_bytes(archive.extractfile(archive.getmember(MEMBER_DB)).read())

    reader = SnapshotFileSource(db_path=db_path, snapshot=stores.snapshot, source=SOURCE)

    assert isinstance(reader, TransferSource)
    assert reader.source == SOURCE
    assert [item.conversation.id for item in reader.transfers()] == ["conv_0002", "conv_0001"]
    windowed = list(reader.transfers(since=T0 + timedelta(minutes=1)))
    assert [item.conversation.id for item in windowed] == ["conv_0002"], "边界那一条不算"
    assert list(reader.transfers(since=datetime(2030, 1, 1, tzinfo=UTC))) == []
    first = next(reader.transfers())
    assert next(item.content for item in first.messages).startswith("第 2 条会话")
    assert first.artifacts[0].location.startswith("conversations/")


def test_the_read_surface_matches_what_the_storage_layer_reads(
    tmp_path: Path, machine: tuple[Any, BackupRestorer, Path], source: Source
) -> None:
    """两层读面的一致：``read_snapshot_db`` 与 ``iter_snapshot_transfers`` 同一份事实。

    （逐会话全量那条读面的逐格判据在 ``test_backup_archive.py``；这里只钉"恢复这条链
    看到的两份事实对得上"——计数与逐条读出来的条数必须一致。）
    """
    stores, _restorer, _data_dir = machine
    staging = stores.snapshot
    assert staging is not None
    db_path = _unpacked_db(tmp_path, source)
    view = staging.read_snapshot_db(db_path)
    transfers = list(staging.iter_snapshot_transfers(db_path))

    assert view.counts["conversations"] == len(transfers) == 2
    assert view.counts["messages"] == sum(len(item.messages) for item in transfers)
    assert view.counts["session_events"] == sum(len(item.events) for item in transfers)
    assert view.counts["artifacts"] == sum(len(item.artifacts) for item in transfers)
    assert view.counts["settings"] == len(view.settings)
    assert view.model_provider_names == ("供应商甲",)
    assert SECRET_KEY not in view.settings, "擦洗过：凭据不在快照库里"


def _unpacked_db(tmp_path: Path, source: Source) -> Path:
    """把包里的 ``db/kylab.db`` 抽出来放一个临时路径（只为上面那条对照）。"""
    target = tmp_path / "unpacked" / "kylab.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source.blob_path, "r:gz") as archive:
        target.write_bytes(archive.extractfile(archive.getmember(MEMBER_DB)).read())
    return target


def test_a_plan_that_cannot_read_the_package_says_so(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """包不成形（不是 tar）→ 说人话地失败（CLI / 端点把这句话原样给用户）。"""
    _stores, restorer, _data_dir = machine
    provider.blob = b"not a tarball at all"

    with pytest.raises(BackupRestoreError) as raised:
        restorer.plan(DEVICE, SEGMENT)
    assert "包读不开" in str(raised.value)

    report = restorer.restore(DEVICE, SEGMENT)
    assert report.state == "failed" and "包读不开" in report.error


def test_the_staging_dir_keeps_only_the_current_package(
    machine: tuple[Any, BackupRestorer, Path], provider: FakeProvider
) -> None:
    """暂存区**一次一份**：上一次中断留下的目录会被清掉（别让它把盘吃满）。"""
    _stores, restorer, data_dir = machine
    root = data_dir / RESTORE_STAGING_DIRNAME
    leftover = root / "2026-01-01T00-00-00Z-old"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "junk.bin").write_bytes(b"x" * 32)

    restorer.plan(DEVICE, SEGMENT)

    assert not leftover.exists()
    assert [item.name for item in root.iterdir()] == [SEGMENT]


def test_the_settings_guard_is_the_constant_itself(
    machine: tuple[Any, BackupRestorer, Path],
) -> None:
    """安全栏 4 的判据**引用常量本身**：新增一个 SECRET_KEY 或前缀，这里自动跟上。

    （逐个键手抄一份名单是这类判据最典型的死法：名单与常量迟早分叉，而分叉的那一天
    就是"凭据被写进本机库"的那一天。）
    """
    _stores, restorer, _data_dir = machine
    view = SnapshotDbView(
        schema_version=1,
        settings=dict.fromkeys(sorted(SECRET_KEYS), "secret")
        | {"provider.backup.base_url": "http://x", "backup.enabled": "1", PLAIN_KEY: "0.42"},
    )

    section = restorer._restore_settings(view)

    assert section["filled"] == [PLAIN_KEY]
    assert sorted(section["excluded"]) == sorted(
        [*SECRET_KEYS, "provider.backup.base_url", "backup.enabled"]
    )


# ------------------------------------------------------------------ ⑥ CLI
#
# CLI 那条路与端点那条路**同一套读**，只有装配不同（自己读环境变量建库、自己取地址与
# 令牌）。三条判据：``--help`` 里那条"干净的路"、``--dry-run`` 打得出报告、真恢复
# **不需要调用方先建批次**（CLI 那条路没人替它建行——这正是它最容易坏的地方）。


def _cli_restorer(
    tmp_path: Path, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, BackupRestorer]:
    """把 CLI 的装配换成"连着一个假提供者的那一份"（终端之外的判据不变）。"""
    data_dir = tmp_path / "cli"
    stores = build_stores(_settings(data_dir))
    restorer = BackupRestorer(
        stores=stores,
        data_dir=data_dir,
        provider=provider,
        snapshotter=_packer(stores, data_dir),
        runtime_config=RuntimeConfigService(stores, _settings(data_dir)),
    )
    monkeypatch.setattr(module, "_build", lambda *args, **kwargs: restorer)
    return stores, restorer


def test_the_cli_help_writes_the_whole_machine_rollback_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` 里那三步（停边车 → 挪库 → 再恢复）：那条"干净的路"要写在用户看得到的地方。"""
    with pytest.raises(SystemExit) as stopped:
        module.main(["--help"])

    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "停掉边车" in out
    assert "restore-backup/<ts>/" in out
    assert "全量重建" in out, "第三步（库空着再恢复）是那条路的关键"
    assert "不要在本机后端跑着的时候换库文件" in out


def test_the_cli_dry_run_prints_the_report(
    tmp_path: Path,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` 把报告打到 stdout（人不看端点也能先预演一遍）。"""
    _stores, _restorer = _cli_restorer(tmp_path, provider, monkeypatch)

    code = module.main(
        [
            "--data-dir",
            str(tmp_path / "cli"),
            "--device-id",
            DEVICE,
            "--snapshot",
            f"{DEVICE}/{SEGMENT}",
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert '"conv_0001"' in out and '"conv_0002"' in out
    assert "只看不写：会新建 2 / 替换 0 / 跳过 0" in out
    assert "恢复后要重配" in out, "凭据那几行要打到终端上（R13）"


def test_the_cli_restores_without_anyone_creating_the_batch_first(
    tmp_path: Path,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真恢复：**批次行由 CLI 自己建**（端点那条路是 `begin()` 先建的，这条路没人替它建）。

    这是它最容易坏的地方：收尾那一步是 UPDATE，行不存在就写不进去——失败得越早越会撞上。
    """
    stores, _restorer = _cli_restorer(tmp_path, provider, monkeypatch)

    code = module.main(
        [
            "--data-dir",
            str(tmp_path / "cli"),
            "--device-id",
            DEVICE,
            "--snapshot",
            f"{DEVICE}/{SEGMENT}",
        ]
    )

    assert code == 0, capsys.readouterr().err
    assert stores.meta.get_conversation("conv_0002") is not None, "会话真的进来了"
    rows = stores.ledger.list_import_batches(limit=10)
    assert len(rows) == 1 and rows[0].state == "done" and rows[0].source == SOURCE
    assert "恢复后要重配" in capsys.readouterr().out


def test_the_cli_refuses_a_snapshot_argument_that_is_not_two_parts(
    tmp_path: Path,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--snapshot`` 写法不对 → 退出码 1 + 一句"该怎么写"（不是栈回溯）。"""
    _cli_restorer(tmp_path, provider, monkeypatch)

    code = module.main(
        ["--data-dir", str(tmp_path / "cli"), "--device-id", DEVICE, "--snapshot", "只有一段"]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "恢复没能进行" in err and "--list" in err
