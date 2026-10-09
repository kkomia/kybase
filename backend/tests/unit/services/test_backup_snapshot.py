"""本机侧快照打包的验收用例（M5 阶段 2，方案 §2.2 / §2.3 / §2.4）。

镜像同构：``app/services/backup_snapshot.py`` → ``tests/unit/services/test_backup_snapshot.py``。

**这一份用例守三件事**：

1. **包里没有秘密**（方案 §2.4 / R1）：造一份含哨兵串的本机库 → 打快照 →
   在**包解开后的原始字节**里 grep → 0 命中。这条判据的"擦洗很彻底吗"那一半在
   ``tests/unit/storage/sqlite_impl/test_backup_archive.py``（VACUUM 之后的空闲页），
   这里守的是**端到端**那一半：从真实装配出来的一条链（``build_stores`` → 打包）走下来，
   哨兵串在包里、在每一个成员里都查不到。
2. **manifest 逐字段**（方案 §2.3）：字段集合、计数、被跳过项、被洗掉的项、
   兼容判据（``format_version`` 只增 / ``schema_version`` 高于本机拒绝）。
3. **三条"选择性"判据**（方案 §2.2）：位置判据自动备、工作区判据默认关、
   额度判据超限**逐条**进 ``skipped``——两条额度都用 ``monkeypatch`` 调小来测
   （真的造 64 MiB / 512 MiB 的文件只会让用例变慢，判据的位置一点没变）。

**假的是什么、真的是什么**：本机库、擦洗、打包、清单、tar.gz **全是真的**
（``build_stores`` 装出来的本机档 + 真的 ``tarfile``），没有替身。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tracemalloc
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import InvalidRequestError
from app.core.storage import build_stores, reset_stores
from app.services import backup_snapshot as module
from app.services.auth import URL_SIGNING_SECRET_SETTING
from app.services.backup_snapshot import (
    COUNT_KEYS,
    FORMAT_NAME,
    FORMAT_VERSION,
    INCLUDE_WORKSPACE_KEY,
    MANIFEST_NAME,
    MEMBER_DB,
    MEMBER_FILES,
    MEMBER_MEMORY,
    MEMBER_WORKSPACE_FILES,
    SKIP_REASONS,
    SNAPSHOT_KINDS,
    BackupSnapshotService,
    parse_manifest,
    read_archive_manifest,
)
from app.services.runtime_config import SECRET_KEYS, RuntimeConfigService
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ARTIFACT_IN_WORKSPACE,
    SnapshotFormatError,
    StorageError,
    StoreBundle,
)
from app.storage.sqlite_impl.connection import Database
from app.storage.sqlite_impl.schema import SCHEMA_VERSION

pytestmark = pytest.mark.local

DEVICE_ID = "3f1c8b2e-0a4d-4a77-9d55-2c6a1b7e9f01"
SNAPSHOT_AT = datetime(2026, 10, 5, 8, 3, 0, tzinfo=UTC)
"""固定时刻：``snapshot_id`` 里那两段时间戳因此可断言（方案 §1.2 的命名规则）。"""

MODEL_SENTINEL = "kylab_sk_SENTINELmodel7f3a91bc"
MCP_SENTINEL = "mcp_SENTINELtoken91bc7de2"
SETTING_SENTINEL = "tavily_SENTINELsearch7de2a1f0"

#: 本机自己的签名材料（``auth.url_signing_secret``）：它也不该跟着快照走
#: （M5 纪律的直接推论——本机档在组合根生成它，换机器重新生成更好）。
SIGNING_SENTINEL = "signing_SENTINELurlsecret4e7d"

ALL_SENTINELS = (MODEL_SENTINEL, MCP_SENTINEL, SETTING_SENTINEL, SIGNING_SENTINEL)

TOP_LEVEL_KEYS = {
    "format",
    "format_version",
    "snapshot_id",
    "device_id",
    "device_name",
    "created_at",
    "kind",
    "app_version",
    "schema_version",
    "platform",
    "blob",
    "counts",
    "included",
    "skipped",
    "redacted",
    "encryption",
}
"""manifest 的字段集合（方案 §2.3 逐字段）：多一个少一个都算改契约。"""


# ------------------------------------------------------------------ 夹具与帮手


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[StoreBundle]:
    """本机档的真装配（与 ``_build_local_stores`` 同一条路，连带证明 ``snapshot`` 接上了）。"""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=tmp_path / "data"
    )
    bundle = build_stores(settings)
    assert bundle.snapshot is not None, "本机档的快照能力必须接上（阶段 2）"
    yield bundle
    reset_stores()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """另一条连接，只为**直写 / 直读表**（造夹具数据、绕过仓储看库里到底有什么）。"""
    handle = Database(tmp_path / "data" / "kylab.db", allow_multiple_instances=True)
    handle.open()
    yield handle
    handle.close()


@pytest.fixture
def service(stores: StoreBundle, data_dir: Path, tmp_path: Path) -> BackupSnapshotService:
    return BackupSnapshotService(
        stores=stores,
        data_dir=data_dir,
        device_id=DEVICE_ID,
        device_name="小又的笔记本",
        app_version="0.1.1",
        platform_name="win32",
    )


def seed_conversation(db: Database, *, conversation_id: str = "c_1", messages: int = 1) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at_ms, updated_at_ms)"
            " VALUES (?, ?, 1, ?)",
            (conversation_id, f"会话 {conversation_id}", 1000 + messages),
        )
        for index in range(messages):
            conn.execute(
                "INSERT INTO chat_messages (id, conversation_id, role, content, created_at_ms)"
                " VALUES (?, ?, 'user', ?, ?)",
                (f"m_{conversation_id}_{index}", conversation_id, f"第 {index} 句", index + 1),
            )
        conn.execute(
            "INSERT INTO session_events (conversation_id, seq, kind, created_at_ms)"
            " VALUES (?, 0, 'turn', 1)",
            (conversation_id,),
        )


def seed_object_artifact(
    db: Database,
    data_dir: Path,
    *,
    artifact_id: str = "a_1",
    conversation_id: str = "c_1",
    key: str = "conversations/c_1/a_1.docx",
    content: bytes = b"report-bytes",
    name: str = "报告.docx",
) -> Path:
    """一份落在对象存储里的产物：库里一条记录 + 盘上一个**真文件**。"""
    path = data_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    with db.session() as conn:
        conn.execute(
            "INSERT INTO conversation_artifacts"
            " (id, conversation_id, name, format, size_bytes, storage, location, created_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                artifact_id,
                conversation_id,
                name,
                name.rsplit(".", 1)[-1],
                len(content),
                ARTIFACT_IN_OBJECTS,
                key,
            ),
        )
    return path


def seed_workspace_artifact(
    db: Database, *, path: Path, artifact_id: str = "a_ws", name: str = "大报告.pptx"
) -> None:
    """一份落在工作区真实目录里的产物（绝对路径进库，方案 §2.2-2 那一档）。"""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at_ms, updated_at_ms)"
            " VALUES ('ws_1', '项目', ?, 1, 1)",
            (str(path.parent),),
        )
        conn.execute(
            "INSERT INTO conversation_artifacts"
            " (id, conversation_id, name, format, size_bytes, storage, location,"
            "  workspace_id, created_at_ms)"
            " VALUES (?, 'c_1', ?, 'pptx', ?, ?, ?, 'ws_1', 2)",
            (artifact_id, name, path.stat().st_size, ARTIFACT_IN_WORKSPACE, str(path)),
        )


def seed_memory(data_dir: Path) -> None:
    """两份记忆文件（``memory/<账号>/…`` 与共享桶根上的那一份）。"""
    (data_dir / "memory" / "u1").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory" / "u1" / "MEMORY.md").write_text("# 记忆\n", encoding="utf-8")
    (data_dir / "memory" / "SOUL.md").write_text("# 人设\n", encoding="utf-8")


def seed_secrets(db: Database) -> None:
    """三处明文凭据各一份（与 ``test_backup_archive`` 同一组哨兵串）。"""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO model_providers"
            " (id, kind, name, base_url, api_key, created_at_ms, updated_at_ms)"
            " VALUES ('mp_1', 'openai', '供应商', 'https://api.example.com', ?, 1, 1)",
            (MODEL_SENTINEL,),
        )
        conn.execute(
            "INSERT INTO mcp_servers"
            " (id, name, transport, target, env, headers, created_at_ms, updated_at_ms)"
            " VALUES ('mcp_1', '外部服务', 'stdio', 'npx', ?, ?, 1, 1)",
            (
                json.dumps({"TOKEN": MCP_SENTINEL}),
                json.dumps({"Authorization": f"Bearer {MCP_SENTINEL}"}),
            ),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms)"
            " VALUES ('web.search_api_key', ?, 1)",
            (SETTING_SENTINEL,),
        )
        # 那条**真实**的签名密钥（键名从常量取，不手抄）：它属于 `auth.` 那一族，
        # 而"整族不进快照"这条纪律要在这个真名上也成立。
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, ?, 1)",
            (URL_SIGNING_SECRET_SETTING, SIGNING_SENTINEL),
        )


def members_of(blob_path: Path) -> dict[str, bytes]:
    """包解开后的**每个成员的原始字节**（判据按它 grep 哨兵串）。"""
    with tarfile.open(blob_path) as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


def member_names(blob_path: Path) -> list[str]:
    with tarfile.open(blob_path) as archive:
        return [member.name for member in archive.getmembers()]


def manifest_of(blob_path: Path) -> dict[str, object]:
    payload = json.loads(members_of(blob_path)[MANIFEST_NAME])
    assert isinstance(payload, dict)
    return payload


def body_digest(members: Mapping[str, bytes]) -> tuple[str, int]:
    """快照体（除 manifest 之外的成员）的摘要与字节数——照它的定义重算一遍。"""
    digest = hashlib.sha256()
    total = 0
    for name, data in members.items():
        if name == MANIFEST_NAME:
            continue
        digest.update(data)
        total += len(data)
    return digest.hexdigest(), total


# ------------------------------------------------------------------ manifest 逐字段


def test_manifest_carries_every_field_of_the_contract(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """§2.3 的字段一个不少，取值对得上（身份 / 时刻 / 计数 / 包含 / 跳过 / 洗掉 / 加密）。"""
    seed_conversation(db, messages=3)
    seed_memory(data_dir)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    payload = manifest_of(result.blob_path)

    assert set(payload) == TOP_LEVEL_KEYS
    assert payload["format"] == FORMAT_NAME
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["device_id"] == DEVICE_ID
    assert payload["device_name"] == "小又的笔记本"
    assert payload["created_at"] == "2026-10-05T08:03:00Z"
    assert payload["kind"] == "manual"
    assert payload["app_version"] == "0.1.1"
    assert payload["platform"] == "win32"
    assert payload["encryption"] == "none"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["snapshot_id"] == result.snapshot_id

    assert set(payload["counts"]) == set(COUNT_KEYS)
    assert payload["counts"] == {
        "conversations": 1,
        "messages": 3,
        "session_events": 1,
        "notes": 0,
        "workspaces": 0,
        "scheduled_tasks": 0,
        "artifacts": 0,
        "memory_files": 2,
    }
    assert payload["included"] == {
        "db": True,
        "memory": True,
        "settings": True,
        "artifacts_object": 0,
        "artifacts_workspace": 0,
    }
    assert payload["skipped"] == []
    assert payload["redacted"] == []


def test_manifest_bytes_are_the_first_member_verbatim(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """包里的第一个成员**逐字节**等于另传那一份 ``manifest.json``（一个真相，两份载体）。"""
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    members = members_of(result.blob_path)

    assert next(iter(members)) == MANIFEST_NAME
    assert members[MANIFEST_NAME] == result.manifest_bytes


def test_members_are_in_the_contract_order(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """成员顺序：manifest → db → memory → files（方案 §2.3，manifest 必须第一）。"""
    seed_conversation(db)
    seed_object_artifact(db, data_dir)
    seed_memory(data_dir)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    assert member_names(result.blob_path) == [
        MANIFEST_NAME,
        MEMBER_DB,
        f"{MEMBER_MEMORY}/SOUL.md",
        f"{MEMBER_MEMORY}/u1/MEMORY.md",
        f"{MEMBER_FILES}/conversations/c_1/a_1.docx",
    ]


def test_the_archive_is_digested_while_it_is_written(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """两个摘要各司其职：``blob.sha256`` 是快照体，``blob_sha256`` 是归档文件自己。"""
    seed_conversation(db)
    seed_object_artifact(db, data_dir, content=b"x" * 4096)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    members = members_of(result.blob_path)
    digest, size = body_digest(members)

    assert result.manifest.blob.sha256 == digest
    assert result.manifest.blob.bytes == size
    assert result.snapshot_id.endswith(digest[:8])
    assert result.blob_sha256 == hashlib.sha256(result.blob_path.read_bytes()).hexdigest()
    assert result.blob_bytes == result.blob_path.stat().st_size


def test_the_snapshot_id_follows_the_naming_rule(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """``<device>-<ts>-<hash8>``，且路径里那段时刻没有冒号（Windows 与 URL 都不接受）。"""
    seed_conversation(db)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    assert result.snapshot_id.startswith(f"{DEVICE_ID}-2026-10-05T08-03-00Z-")
    assert ":" not in result.snapshot_id
    assert len(result.snapshot_id.rsplit("-", 1)[-1]) == 8
    assert result.blob_path.name == f"{result.snapshot_id}.tar.gz"


def test_two_identical_snapshots_in_the_same_second_share_an_id(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """同内容 + 同一秒 → 同一个 id（内容寻址那一条）；换个时刻就是另一份。"""
    first = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    second = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    later = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT.replace(minute=4))

    assert first.snapshot_id == second.snapshot_id
    assert first.blob_path == second.blob_path
    assert later.snapshot_id != first.snapshot_id


def test_contents_changed_means_another_id(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """内容变了，同一秒也是另一份（"解决同秒冲突"那一条）。"""
    seed_conversation(db, conversation_id="c_1")
    first = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    seed_conversation(db, conversation_id="c_2")
    second = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    assert second.snapshot_id != first.snapshot_id


# ------------------------------------------------------------------ 哨兵串：包解开后 0 命中


def test_no_sentinel_survives_in_the_package(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """**端到端最强判据**：哨兵串在整包字节里、在每个成员里都查不到。

    半边是"库里确实有那些秘密"（先断言源库里还查得到），另半边才是"包里没有"——
    只断言后一半的话，一条把库打空的实现也能通过。
    """
    seed_secrets(db)
    seed_conversation(db)
    seed_object_artifact(db, data_dir)
    seed_memory(data_dir)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    with db.read() as conn:
        stored = conn.execute("SELECT api_key FROM model_providers").fetchone()["api_key"]
    assert stored == MODEL_SENTINEL, "源库里的明文凭据必须原样留着（擦洗只动副本）"

    archive_bytes = result.blob_path.read_bytes()
    members = members_of(result.blob_path)
    for sentinel in ALL_SENTINELS:
        assert sentinel.encode() not in archive_bytes, f"包里还有 {sentinel}"
        for name, data in members.items():
            assert sentinel.encode() not in data, f"成员 {name} 里还有 {sentinel}"


def test_redacted_section_lists_what_was_washed(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """``redacted`` 如实列出洗掉的四条（用户要知道"恢复后要重配什么"）。"""
    seed_secrets(db)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    assert manifest_of(result.blob_path)["redacted"] == [
        {"table": "model_providers", "column": "api_key", "rows": 1},
        {"table": "mcp_servers", "column": "env,headers", "rows": 1},
        # `auth.` 那一族：签名密钥被洗掉要**如实报**（它在恢复之后会重新生成，
        # 但用户有权知道"包里没带它"）。两条设置按键名排序（报告那一层就是这么攒的）。
        {"table": "app_settings", "key": URL_SIGNING_SECRET_SETTING, "rows": 1},
        {"table": "app_settings", "key": "web.search_api_key", "rows": 1},
    ]
    assert "web.search_api_key" in SECRET_KEYS, "这条判据的前提：它是 SECRET_KEYS 里的一员"
    assert URL_SIGNING_SECRET_SETTING.startswith("auth."), "这条判据的前提：它属于 auth. 那一族"


# ------------------------------------------------------------------ 三条"选择性"判据


def test_object_artifacts_are_always_included(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """位置判据：落在对象存储里的产物**一律备**（不需要任何开关）。"""
    seed_conversation(db)
    seed_object_artifact(db, data_dir, content=b"report-bytes")
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["included"]["artifacts_object"] == 1
    assert members_of(result.blob_path)[f"{MEMBER_FILES}/conversations/c_1/a_1.docx"] == (
        b"report-bytes"
    )


def test_workspace_artifacts_stay_out_by_default(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """工作区判据：默认**不备**，而且逐条进 ``skipped``（用户要看得见"哪些没备"）。"""
    workspace_file = tmp_path / "项目" / "大报告.pptx"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(b"pptx-bytes")
    seed_conversation(db)
    seed_workspace_artifact(db, path=workspace_file)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["included"]["artifacts_workspace"] == 0
    assert payload["skipped"] == [
        {
            "name": "大报告.pptx",
            "size_bytes": len(b"pptx-bytes"),
            "reason": "workspace_not_included",
        }
    ]
    assert all(
        not name.startswith(MEMBER_WORKSPACE_FILES) for name in member_names(result.blob_path)
    )


def test_workspace_artifacts_are_included_when_the_switch_is_on(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """开关打开之后按同一套额度备（成员名用**产物 id + 文件名**，不是那台机器的绝对路径）。"""
    workspace_file = tmp_path / "项目" / "大报告.pptx"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(b"pptx-bytes")
    seed_conversation(db)
    seed_workspace_artifact(db, path=workspace_file)

    runtime = RuntimeConfigService(stores)
    runtime.set({INCLUDE_WORKSPACE_KEY: "1"})
    service_on = BackupSnapshotService(
        stores=stores, data_dir=tmp_path / "data", device_id=DEVICE_ID, runtime_config=runtime
    )
    result = service_on.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["included"]["artifacts_workspace"] == 1
    assert payload["skipped"] == []
    members = members_of(result.blob_path)
    assert members[f"{MEMBER_WORKSPACE_FILES}/a_ws/大报告.pptx"] == b"pptx-bytes"
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)
    """包的清单里不许出现**这台机器的绝对路径**：它要发到 NAS 上去。"""


def test_an_oversized_artifact_lands_in_skipped(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """额度判据之一：单文件超限 → 那一条进 ``skipped``（reason=too_large），其余照备。"""
    monkeypatch.setattr(module, "BACKUP_ARTIFACT_MAX_BYTES", 8)
    seed_conversation(db)
    seed_object_artifact(db, data_dir, artifact_id="a_big", key="conversations/c_1/a_big.bin")
    seed_object_artifact(
        db,
        data_dir,
        artifact_id="a_small",
        key="conversations/c_1/a_small.txt",
        content=b"ok",
        name="小.txt",
    )
    big_path = data_dir / "conversations/c_1/a_big.bin"
    big_path.write_bytes(b"x" * 9)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["skipped"] == [
        {"name": "conversations/c_1/a_big.bin", "size_bytes": 9, "reason": "too_large"}
    ]
    assert payload["included"]["artifacts_object"] == 1
    assert [name for name in member_names(result.blob_path) if name.startswith(MEMBER_FILES)] == [
        f"{MEMBER_FILES}/conversations/c_1/a_small.txt"
    ]


def test_the_total_budget_is_reported_per_item(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """额度判据之二：总量超限 → **逐条**进 ``skipped``（reason=total_budget），装得下的照备。

    顺序是"按产物的顺序往后装"：先到的那份占住额度，后面的报 ``total_budget``——
    用户看到的是"哪几份没进去"，而不是一句"超过上限了"。
    """
    monkeypatch.setattr(module, "BACKUP_ARTIFACTS_TOTAL_BYTES", 10)
    seed_conversation(db)
    seed_object_artifact(
        db,
        data_dir,
        artifact_id="a_1",
        key="conversations/c_1/a_1.txt",
        content=b"y" * 6,
        name="一.txt",
    )
    seed_object_artifact(
        db,
        data_dir,
        artifact_id="a_2",
        key="conversations/c_1/a_2.txt",
        content=b"z" * 6,
        name="二.txt",
    )
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["included"]["artifacts_object"] == 1
    assert payload["skipped"] == [
        {"name": "conversations/c_1/a_2.txt", "size_bytes": 6, "reason": "total_budget"}
    ]
    assert {item["reason"] for item in payload["skipped"]} <= set(SKIP_REASONS)


def test_a_missing_artifact_is_reported_instead_of_failing_the_snapshot(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """库里记着、盘上没了：如实报 ``missing``，快照照打（一份坏记录不该让备份打不出来）。"""
    seed_conversation(db)
    seed_object_artifact(db, data_dir)
    (data_dir / "conversations/c_1/a_1.docx").unlink()
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["skipped"] == [
        {
            "name": "conversations/c_1/a_1.docx",
            "size_bytes": len(b"report-bytes"),
            "reason": "missing",
        }
    ]
    assert "missing" in SKIP_REASONS


def test_an_unsafe_object_key_is_refused_not_written(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """Key 不是安全的相对路径（``..`` / 绝对路径 / 反斜杠）→ 宁可报 ``unsafe_location``。

    写进包就是一个越界的成员名（解开时能落到包外）。这条判据的方向是"宁可少备一份"。
    """
    seed_conversation(db)
    with db.session() as conn:
        conn.execute(
            "INSERT INTO conversation_artifacts"
            " (id, conversation_id, name, format, size_bytes, storage, location, created_at_ms)"
            " VALUES ('a_bad', 'c_1', '越界.txt', 'txt', 3, ?, '../../escape.txt', 1)",
            (ARTIFACT_IN_OBJECTS,),
        )
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["skipped"] == [
        {"name": "越界.txt", "size_bytes": 3, "reason": "unsafe_location"}
    ]
    assert all("escape" not in name for name in member_names(result.blob_path))


def test_memory_symlinks_are_not_followed(
    stores: StoreBundle, data_dir: Path, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """记忆目录里的符号链接**不跟**（它可能指向工作区，那条"永不上传"不该被绕过去）。"""
    outside = data_dir / "workspaces" / "项目" / "秘密.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("工作区里的东西", encoding="utf-8")
    link = data_dir / "memory" / "u1" / "捷径.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - 平台不给建链接
        pytest.skip("这台机器不允许建符号链接（Windows 需要开发者模式或管理员）")

    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["skipped"] == [
        {"name": f"{MEMBER_MEMORY}/u1/捷径.md", "size_bytes": 0, "reason": "symlink"}
    ]
    assert all("捷径" not in name for name in member_names(result.blob_path))


def test_an_empty_memory_directory_reports_zero(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """记忆目录不存在（从没用过记忆）时：``memory_files: 0``、``included.memory: false``。"""
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    payload = manifest_of(result.blob_path)
    assert payload["counts"]["memory_files"] == 0
    assert payload["included"]["memory"] is False


# ------------------------------------------------------------------ 流式与内存


def test_a_large_artifact_is_packed_without_loading_it_into_memory(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
) -> None:
    """打包是流式的（方案 R10）：32 MiB 的产物**不进 Python 内存**，峰值内存粗量可控。

    判据是"峰值远小于产物本身"：真把整份读进 ``bytes`` 的实现，峰值会立刻超过 32 MiB。
    这份产物是可压缩的（一块重复字节），免得把用例的时间花在压缩率上。
    """
    seed_conversation(db)
    payload_size = 32 * 1024 * 1024
    seed_object_artifact(
        db,
        data_dir,
        key="conversations/c_1/a_big.bin",
        content=b"\0" * (1024 * 1024),
        name="大文件.bin",
    )
    big = data_dir / "conversations/c_1/a_big.bin"
    with big.open("wb") as handle:
        handle.write(b"\0" * payload_size)

    tracemalloc.start()
    try:
        result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < payload_size // 4, f"打包峰值内存 {peak} 说明产物被整份读进了内存"
    assert member_names(result.blob_path)[-1] == f"{MEMBER_FILES}/conversations/c_1/a_big.bin"


def test_a_changed_body_aborts_the_package(
    stores: StoreBundle,
    db: Database,
    data_dir: Path,
    service: BackupSnapshotService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写包时复核快照体（R10 那一遍预读的结论）：对不上 → **删掉那份包并报错**。

    模拟的是"两遍读之间内容变了"：把预读那一步换成一组对不上的数（真实现里它来自
    文件的真实字节），于是写包那一遍必然复核失败。留一份"自己的清单对不上自己"的包在
    盘上，比报错糟得多——读它的人会以为拿到的是完好的东西。
    """
    seed_conversation(db)
    monkeypatch.setattr(module, "_digest_members", lambda _members: ("0" * 64, 0))

    with pytest.raises(StorageError):
        service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    assert list((tmp_path / "out").glob("*.tar.gz")) == []


def test_the_work_directory_is_asserted_to_hold_only_the_copy(
    stores: StoreBundle, tmp_path: Path
) -> None:
    """打包前那条断言（方案 §2.4-4）：副本旁边多出一个 ``-wal`` 就**当场停**。

    它守的是"照文件拷会少一截"那类事故的形状。用一层薄包装造出那个形状——
    真实现不会产生它（``test_backup_archive`` 那条"只有一个文件"钉着）。
    """

    class _StrayWal:
        """把真实现包一层，dump 之后顺手在副本旁边放一个 ``-wal``。

        **四个方法都要转发**（``SnapshotSource`` 那份读面在 M5 阶段 5 涨到了三个：
        读一份库 / 逐会话读全量 / 本机 schema 版本）：这一层是"薄包装"，协议涨一个它就得
        跟一个——不跟的话它当场不再满足那个 ``runtime_checkable`` 协议，而报出来的错
        会是"这个部署没有快照面"（离真正的原因很远）。
        """

        def __init__(self, inner: object) -> None:
            self._inner = inner

        def dump_scrubbed_db(self, dest: Path):  # type: ignore[no-untyped-def]
            report = self._inner.dump_scrubbed_db(dest)  # type: ignore[attr-defined]
            Path(f"{dest}-wal").write_bytes(b"")
            return report

        def read_snapshot_db(self, db_path: Path):  # type: ignore[no-untyped-def]
            return self._inner.read_snapshot_db(db_path)  # type: ignore[attr-defined]

        def iter_snapshot_transfers(self, db_path: Path):  # type: ignore[no-untyped-def]
            return self._inner.iter_snapshot_transfers(db_path)  # type: ignore[attr-defined]

        def local_schema_version(self) -> int:
            return int(self._inner.local_schema_version())  # type: ignore[attr-defined]

    assert stores.snapshot is not None
    wrapped = StoreBundle(
        meta=stores.meta,
        vectors=stores.vectors,
        fulltext=stores.fulltext,
        objects=stores.objects,
        tabular=stores.tabular,
        snapshot=_StrayWal(stores.snapshot),  # type: ignore[arg-type]
    )
    service = BackupSnapshotService(stores=wrapped, data_dir=tmp_path / "data", device_id=DEVICE_ID)

    with pytest.raises(StorageError) as excinfo:
        service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    assert "kylab.db-wal" in str(excinfo.value)


# ------------------------------------------------------------------ 兼容判据与拒绝


def test_manifest_round_trips_through_the_reader(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """写出来的清单，读者**逐字段**读得回来（``parse_manifest`` 与 ``to_bytes`` 对称）。"""
    seed_conversation(db)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    parsed = parse_manifest(result.manifest_bytes, local_schema_version=SCHEMA_VERSION)

    assert parsed == result.manifest
    assert read_archive_manifest(result.blob_path, local_schema_version=SCHEMA_VERSION).kind == (
        "manual"
    )


def test_parse_manifest_refuses_a_newer_format_version(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """``format_version`` 高于本机 → 拒绝（不认识就当场说，绝不硬按旧结构读）。"""
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    payload = manifest_of(result.blob_path)
    payload["format_version"] = FORMAT_VERSION + 1

    with pytest.raises(SnapshotFormatError) as excinfo:
        parse_manifest(json.dumps(payload), local_schema_version=SCHEMA_VERSION)
    assert str(FORMAT_VERSION + 1) in str(excinfo.value)


def test_parse_manifest_refuses_a_newer_schema_version(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """``schema_version`` 高于本机的 ``SCHEMA_VERSION`` → **拒绝恢复**（方案 §2.3）。"""
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    payload = manifest_of(result.blob_path)
    payload["schema_version"] = SCHEMA_VERSION + 1

    with pytest.raises(SnapshotFormatError):
        parse_manifest(json.dumps(payload), local_schema_version=SCHEMA_VERSION)


def test_parse_manifest_refuses_a_foreign_package(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """不是我们的包（``format`` 不对）→ 拒绝；顺手钉住"缺必备字段也拒绝"。"""
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    payload = manifest_of(result.blob_path)
    payload["format"] = "someone-else-backup"

    with pytest.raises(SnapshotFormatError):
        parse_manifest(json.dumps(payload), local_schema_version=SCHEMA_VERSION)

    del payload["blob"]
    with pytest.raises(SnapshotFormatError):
        parse_manifest(json.dumps(payload), local_schema_version=SCHEMA_VERSION)


def test_parse_manifest_tolerates_unknown_new_fields(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """多出来的**新字段原样忽略**：那是"对方多写了一点"，不是"本机不认识这个格式"。"""
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    payload = manifest_of(result.blob_path)
    payload["future_field"] = {"anything": True}

    parsed = parse_manifest(json.dumps(payload), local_schema_version=SCHEMA_VERSION)
    assert parsed.snapshot_id == result.snapshot_id


def test_read_archive_manifest_requires_the_manifest_first(
    stores: StoreBundle, tmp_path: Path
) -> None:
    """第一个成员不是 ``manifest.json`` 的包**读不了**（方案 §2.3 的那条纪律）。"""
    other = tmp_path / "other.tar.gz"
    with tarfile.open(other, "w:gz") as archive:
        data = b"not a manifest"
        info = tarfile.TarInfo(MEMBER_DB)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))

    with pytest.raises(SnapshotFormatError):
        read_archive_manifest(other, local_schema_version=SCHEMA_VERSION)


def test_without_a_device_identity_the_snapshot_is_refused(
    stores: StoreBundle, tmp_path: Path
) -> None:
    """R12：没有设备身份就**如实拒**并说下一步，绝不编一个 id。"""
    service = BackupSnapshotService(stores=stores, data_dir=tmp_path / "data", device_id="")

    with pytest.raises(InvalidRequestError) as excinfo:
        service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    assert "设备身份" in str(excinfo.value)
    assert not list((tmp_path / "out").glob("*.tar.gz"))


def test_an_unknown_kind_is_refused(
    stores: StoreBundle, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """``kind`` 的词表就那三个（与 ``backup_snapshots`` 表的 CHECK 同一个集合）。"""
    with pytest.raises(InvalidRequestError):
        service.create(into=tmp_path / "out", kind="whenever", created_at=SNAPSHOT_AT)
    assert "auto" in SNAPSHOT_KINDS


def test_the_service_refuses_to_exist_without_the_snapshot_capability(
    stores: StoreBundle, tmp_path: Path
) -> None:
    """服务器档（``StoreBundle.snapshot`` 恒为 ``None``）：这个服务**构造时就该拒**。"""
    server_side = StoreBundle(
        meta=stores.meta,
        vectors=stores.vectors,
        fulltext=stores.fulltext,
        objects=stores.objects,
        tabular=stores.tabular,
    )

    with pytest.raises(RuntimeError) as excinfo:
        BackupSnapshotService(stores=server_side, data_dir=tmp_path / "data", device_id=DEVICE_ID)
    assert "快照" in str(excinfo.value)


def test_the_three_local_only_capabilities_share_one_instance(stores: StoreBundle) -> None:
    """``meta`` / ``ledger`` / ``kb_cache`` / ``snapshot`` 是**同一个**实例（写锁只有一把）。"""
    assert stores.snapshot is not None
    assert stores.snapshot is stores.ledger
    assert stores.snapshot is stores.kb_cache


def test_kinds_and_reasons_are_the_documented_vocabulary() -> None:
    """词表本身钉住（方案 §2.2 / §2.3）：改它是有意识的动作，不是顺手加一个字。"""
    assert SNAPSHOT_KINDS == ("manual", "auto", "pre_restore")
    assert SKIP_REASONS[:3] == ("too_large", "total_budget", "workspace_not_included")
    assert COUNT_KEYS == (
        "conversations",
        "messages",
        "session_events",
        "notes",
        "workspaces",
        "scheduled_tasks",
        "artifacts",
        "memory_files",
    )


def test_the_snapshot_keeps_a_readable_database(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """包里的那份库是**完整的**：解出来能被读面读出会话与产物（阶段 5 的入口就这么开）。"""
    seed_conversation(db, messages=2)
    seed_object_artifact(db, tmp_path / "data")
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(result.blob_path) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            target = unpacked / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())

    view = stores.snapshot.read_snapshot_db(unpacked / MEMBER_DB)  # type: ignore[union-attr]
    assert view.counts["conversations"] == 1
    assert [item.id for item in view.conversations] == ["c_1"]
    assert [item.location for item in view.artifacts] == ["conversations/c_1/a_1.docx"]


def test_the_blob_is_a_gzip_tar_in_plain_names(
    stores: StoreBundle, db: Database, service: BackupSnapshotService, tmp_path: Path
) -> None:
    """成员名是 POSIX 相对路径、没有绝对路径与前缀（"包"这个形状的两个前提）。"""
    seed_conversation(db)
    result = service.create(into=tmp_path / "out", created_at=SNAPSHOT_AT)

    names: Sequence[str] = member_names(result.blob_path)
    assert all(not name.startswith(("/", "\\")) and ":" not in name for name in names)
    assert all("\\" not in name for name in names)
