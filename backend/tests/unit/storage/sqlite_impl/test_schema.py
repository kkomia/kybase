"""``sqlite_impl/schema.py`` 的单元测试：STRICT、索引、迁移与迁移前自动备份。

镜像同构：``app/storage/sqlite_impl/schema.py``
→ ``tests/unit/storage/sqlite_impl/test_schema.py``。

这一组用例对应实施方案 §6.4 的第二条：「STRICT 生效（插入错类型必须报错）、
索引存在、迁移+自动备份、版本高于应用报错」。四条都在这一个文件里，
因为它们答的是同一个问题——**库自己有没有守住它声称的那套纪律**。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.storage.sqlite_impl import schema as schema_module
from app.storage.sqlite_impl.connection import Database
from app.storage.sqlite_impl.schema import (
    BASELINE_VERSION,
    KEY_CREATED_AT,
    KEY_LAST_BACKUP,
    KEY_MIGRATION_LOG,
    MIGRATION_V2_KB_META_CACHE,
    MIGRATION_V3_BACKUP_SNAPSHOTS,
    MIGRATIONS,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    Migration,
    SchemaError,
    current_version,
    ensure_schema,
    prepare,
)

pytestmark = pytest.mark.local

#: 本机库里应该有哪 19 张表（§1.3 逐行列出的那份清单 + M4 的快照表 + M5 的队列表）。
#: **注意**：实施方案的标题写"18 张"，但那份清单逐行数是 17 张；这里按**清单**守，
#: 于是"哪天真的少了一张或多了一张"会立刻红，而不是被一个错误的数字掩盖。
#: 第 18 张 ``kb_meta_cache``（M4）与第 19 张 ``backup_snapshots``（M5 阶段 3）都
#: **不来自基线**（``schema.sql`` 一个字没改），而是增量迁移 v2 / v3 建的，所以它们与
#: 下面 ``test_baseline_version_literal_matches_the_constant`` 那条"基线还是 v1"的断言
#: 并不矛盾。
EXPECTED_TABLES = frozenset(
    {
        "schema_metadata",
        "app_settings",
        "workspaces",
        "conversations",
        "chat_messages",
        "session_events",
        "conversation_artifacts",
        "note_folders",
        "notes",
        "note_tags",
        "scheduled_tasks",
        "mcp_servers",
        "model_providers",
        "model_registry",
        "usage_events",
        "imports",
        "import_items",
        "kb_meta_cache",
        "backup_snapshots",
    }
)

#: 规范 3 点名的覆盖索引（§1.4-3），逐条**按原文**列出来。
EXPECTED_INDEXES = frozenset(
    {
        "idx_conversations_recent",
        "idx_conversations_workspace",
        "idx_conversations_archived",
        "idx_chat_messages_conversation",
        "idx_conversation_artifacts_conv",
        "idx_note_folders_owner",
        "idx_notes_owner",
        "idx_scheduled_tasks_due",
        # M5 阶段 3：待传队列的两种读形状各一条（队列：state + 到期；最近：created_at DESC）
        "idx_backup_snapshots_queue",
        "idx_backup_snapshots_recent",
    }
)


#: 逐表的毫秒时间列（§1.2 的机械翻译结果）。多一个少一个都说明映射错了。
EXPECTED_MS_COLUMNS: dict[str, frozenset[str]] = {
    "app_settings": frozenset({"updated_at_ms"}),
    "workspaces": frozenset({"created_at_ms", "updated_at_ms", "archived_at_ms"}),
    "conversations": frozenset({"created_at_ms", "updated_at_ms", "archived_at_ms"}),
    "chat_messages": frozenset({"created_at_ms"}),
    "session_events": frozenset({"created_at_ms"}),
    "conversation_artifacts": frozenset({"created_at_ms"}),
    "note_folders": frozenset({"created_at_ms", "updated_at_ms"}),
    "notes": frozenset({"created_at_ms", "updated_at_ms"}),
    "scheduled_tasks": frozenset(
        {
            "run_at_ms",
            "next_run_at_ms",
            "last_run_at_ms",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "mcp_servers": frozenset({"created_at_ms", "updated_at_ms"}),
    "model_providers": frozenset({"created_at_ms", "updated_at_ms"}),
    "model_registry": frozenset({"created_at_ms", "updated_at_ms"}),
    "usage_events": frozenset({"created_at_ms", "duration_ms"}),
    "imports": frozenset({"since_ms", "created_at_ms", "updated_at_ms"}),
    "import_items": frozenset({"source_updated_at_ms", "local_updated_at_ms", "created_at_ms"}),
    # 快照那对**不许混**的时间戳（M4 §3.1）：内容什么时候看到的 / 最近一次确认
    "kb_meta_cache": frozenset({"fetched_at_ms", "checked_at_ms"}),
    # 待传队列（M5 §3.1）：入队时刻 + 两个可空时间（下次可试 / 传成）
    "backup_snapshots": frozenset({"created_at_ms", "next_attempt_at_ms", "uploaded_at_ms"}),
    # schema_metadata 只有 key/value：版本是文本，建库时间在它的行值里（不是列）
    "schema_metadata": frozenset(),
}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "kylab.db")
    prepare(db)
    yield db
    db.close()


def _objects(conn: sqlite3.Connection, kind: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    ).fetchall()
    return {row["name"]: row["sql"] or "" for row in rows}


# ------------------------------------------------------------------ 基线


def test_fresh_file_has_no_version_then_baseline_lands(tmp_path: Path) -> None:
    """三种版本状态要分清（``current_version`` 的 None / 0 / n）。

    建完基线**立刻**把增量补上，所以空库走完 ``ensure_schema`` 是 ``SCHEMA_VERSION``
    （M4 之前那个值是 1，因为那时 ``MIGRATIONS`` 还是空的——现在它的判据改成
    "最后一条迁移 == 应用期望的版本"，这样再加迁移时这条用例不用跟着改数字）。
    """
    db = Database(tmp_path / "kylab.db")
    db.open()
    assert current_version(db) is None
    assert ensure_schema(db) == SCHEMA_VERSION
    assert current_version(db) == SCHEMA_VERSION
    # 基线**冻在 v1**：老库（用户机器上那份）只能靠增量升级，改基线等于让它们升不上来
    assert BASELINE_VERSION == 1
    assert MIGRATIONS[-1].version == SCHEMA_VERSION
    assert [item.version for item in MIGRATIONS] == list(
        range(BASELINE_VERSION + 1, SCHEMA_VERSION + 1)
    ), "增量必须逐级连号，中间不许缺（缺一级就有一批库升不上来）"
    db.close()


def test_baseline_version_literal_matches_the_constant() -> None:
    """版本号在 ``schema.sql`` 里是**字面量**（与 DDL 同一事务），必须与应用常量相等。

    顺带钉住 M4 §3.2 那一条：本机库**已经发过版**，所以新表只能走增量——基线文件与
    ``BASELINE_VERSION`` 一个字不改（用例读的就是磁盘上那份 DDL）。
    """
    from app.storage.sqlite_impl.schema import SCHEMA_PATH

    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    assert f"VALUES ('version', '{BASELINE_VERSION}')" in ddl
    assert SCHEMA_VERSION > BASELINE_VERSION, "M4 起增量不为空：基线必须冻着不动"


def test_baseline_creates_exactly_the_local_tables(database: Database) -> None:
    with database.read() as conn:
        tables = set(_objects(conn, "table"))
    assert tables == EXPECTED_TABLES
    assert len(tables) == 19


def test_every_table_is_strict(database: Database) -> None:
    """规范 1：全部量 STRICT。少了 STRICT，列类型就只是注释。"""
    with database.read() as conn:
        sqls = _objects(conn, "table")
    not_strict = sorted(name for name, sql in sqls.items() if "STRICT" not in sql.upper())
    assert not_strict == [], f"这些表没写 STRICT：{not_strict}"


def test_strict_rejects_a_wrong_type(database: Database) -> None:
    """STRICT 生效的判据：**不可无损转换**的值必须当场报错。

    用 "把 TEXT 塞进 INTEGER 列"：反过来（整数塞进 TEXT 列）是**无损转换**，
    SQLite 会照收——这一点值得记住，别拿它当反例。
    """
    with (
        pytest.raises(sqlite3.IntegrityError, match="cannot store TEXT value"),
        database.session() as conn,
    ):
        conn.execute(
            "INSERT INTO conversations"
            " (id, title, kb_ids, pinned, context_summary, created_at_ms, updated_at_ms)"
            " VALUES ('c1', '标题', '[]', 'not-a-bool', '', 1, 1)"
        )


def test_json_columns_are_checked(database: Database) -> None:
    """规范 1 的后半：JSON 列补 ``CHECK (json_valid(...))``。"""
    with pytest.raises(sqlite3.IntegrityError, match="json_valid"), database.session() as conn:
        conn.execute(
            "INSERT INTO conversations"
            " (id, title, kb_ids, pinned, context_summary, created_at_ms, updated_at_ms)"
            " VALUES ('c1', '标题', '不是 JSON', 0, '', 1, 1)"
        )


def test_timestamp_columns_use_ms_and_no_bare_at(database: Database) -> None:
    """类型映射的机械翻译（§1.2）：``timestamptz`` → ``INTEGER`` + ``_ms`` 后缀。

    两条判据：① 没有任何列是 PG 那种裸 ``_at`` 名（漏改一处就会有一个）；
    ② 逐表列出**应该**有的毫秒列，多一个少一个都红。
    """
    with database.read() as conn:
        actual = {
            table: {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for table in _objects(conn, "table")
        }
    bare = sorted(
        f"{table}.{column}"
        for table, columns in actual.items()
        for column in columns
        if column.endswith("_at") and not column.endswith("_ms")
    )
    assert bare == [], f"这些时间列是 PG 的裸名，漏了 _ms：{bare}"
    for table, expected in EXPECTED_MS_COLUMNS.items():
        found = {column for column in actual[table] if column.endswith("_ms")}
        assert found == set(expected), f"{table} 的毫秒列对不上：{found} != {set(expected)}"


def test_required_indexes_exist(database: Database) -> None:
    """规范 3：列表查询的覆盖索引一条都不能少。"""
    with database.read() as conn:
        indexes = set(_objects(conn, "index"))
        # 部分索引（`WHERE enabled`）在 sqlite_master 里的 DDL 带着条件，逐条核一眼
        scheduled = _objects(conn, "index")["idx_scheduled_tasks_due"]
    assert indexes >= EXPECTED_INDEXES
    assert "WHERE enabled" in scheduled

    # 索引确实被查询规划器用上了（建了但用不上 = 白建）
    plan = _explain(
        database, "SELECT * FROM conversations ORDER BY pinned DESC, updated_at_ms DESC"
    )
    assert "idx_conversations_recent" in plan


def test_unique_constraints_the_code_depends_on(database: Database) -> None:
    """几处唯一约束不是装饰：``ConflictError`` 与"同级重名挡住"都指着它们。"""
    with database.session() as conn:
        conn.execute(
            "INSERT INTO model_providers (id, kind, name, created_at_ms, updated_at_ms)"
            " VALUES ('p1', 'llm', '供应商', 1, 1)"
        )
        conn.execute(
            "INSERT INTO model_registry (id, provider_id, model_id, created_at_ms, updated_at_ms)"
            " VALUES ('m1', 'p1', 'deepseek-chat', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError), database.session() as conn:
        conn.execute(
            "INSERT INTO model_registry (id, provider_id, model_id, created_at_ms,"
            " updated_at_ms) VALUES ('m2', 'p1', 'deepseek-chat', 1, 1)"
        )

    with database.session() as conn:
        conn.execute(
            "INSERT INTO note_folders (id, user_id, name, created_at_ms, updated_at_ms)"
            " VALUES ('f1', 'u1', '工作', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError), database.session() as conn:
        # user_id / parent_id 都是 NULL 的"根级同名文件夹"同样要挡住
        conn.execute(
            "INSERT INTO note_folders (id, user_id, name, created_at_ms, updated_at_ms)"
            " VALUES ('f2', 'u1', '工作', 1, 1)"
        )


def test_version_lives_only_in_schema_metadata(database: Database) -> None:
    """规范 2：版本号**只有一处**——不另写 ``PRAGMA user_version``。"""
    with database.read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 0
        row = conn.execute("SELECT value FROM schema_metadata WHERE key = 'version'").fetchone()
        created = conn.execute(
            "SELECT value FROM schema_metadata WHERE key = ?", (KEY_CREATED_AT,)
        ).fetchone()
    assert row["value"] == str(SCHEMA_VERSION)
    assert int(created["value"]) > 0


def test_ensure_schema_is_idempotent(database: Database) -> None:
    assert ensure_schema(database) == SCHEMA_VERSION
    assert ensure_schema(database) == SCHEMA_VERSION


# ------------------------------------------------------------------ 迁移


#: 一条**合成迁移**：只在测试里存在。用它验证迁移机制本身：备份、同事务写版本、
#: 失败回滚、日志。
#:
#: 版本号取 ``SCHEMA_VERSION + 1`` 而**不是写死的 2**（M4 起真实的 ``MIGRATIONS``
#: 已经占了 v2）：合成迁移必须比"库里现在的版本"更高，否则 ``_apply_migrations``
#: 会认为没有待补的迁移，这几条用例就变成"什么都没验"的空跑。
SYNTHETIC = Migration(
    version=SCHEMA_VERSION + 1,
    description="测试用：加一张探针表",
    statements=(
        "CREATE TABLE probe (id TEXT PRIMARY KEY) STRICT",
        "CREATE INDEX idx_probe ON probe (id)",
    ),
)
BROKEN = Migration(
    version=SCHEMA_VERSION + 1,
    description="测试用：第二条语句是坏 SQL",
    statements=(
        "CREATE TABLE half_baked (id TEXT PRIMARY KEY) STRICT",
        "THIS IS NOT SQL",
    ),
)


@pytest.fixture
def upgradeable(database: Database, monkeypatch) -> Database:
    """把 ``MIGRATIONS`` 换成合成的那条，并让应用期望的版本跟上。"""
    monkeypatch.setattr(schema_module, "MIGRATIONS", (SYNTHETIC,))
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", SYNTHETIC.version)
    return database


def test_migration_applies_and_records_everything(upgradeable: Database) -> None:
    assert ensure_schema(upgradeable) == SYNTHETIC.version
    with upgradeable.read() as conn:
        assert "probe" in _objects(conn, "table")
        log = json.loads(
            conn.execute(
                "SELECT value FROM schema_metadata WHERE key = ?", (KEY_MIGRATION_LOG,)
            ).fetchone()["value"]
        )
    # 日志里已经有**真实的那几条**（`prepare` 建库时就迁过 v2），合成那条追加在末尾——
    # 每应用一条就追加一条，顺序即应用顺序（`MIGRATIONS` 是本模块导入的**原始**元组，
    # `upgradeable` 改的是 `schema_module` 上那个名字）
    assert [entry["version"] for entry in log] == [
        *[item.version for item in MIGRATIONS],
        SYNTHETIC.version,
    ]
    assert log[-1]["description"] == SYNTHETIC.description
    assert log[-1]["at_ms"] > 0


def test_migration_backs_up_the_whole_database_first(upgradeable: Database) -> None:
    """规范化要求：迁移前自动备份，路径写回 ``schema_metadata.last_backup``。

    备份是**迁移前**的库——所以它里面没有 ``probe`` 表（而主库有）。
    这正是"迁移跑错了还能退回去"的那个东西。
    """
    ensure_schema(upgradeable)
    with upgradeable.read() as conn:
        recorded = conn.execute(
            "SELECT value FROM schema_metadata WHERE key = ?", (KEY_LAST_BACKUP,)
        ).fetchone()["value"]
    backup = Path(recorded)
    assert backup.exists()
    assert backup.name == upgradeable.path.name
    # 目录名带上版本区间，一眼看得出"这份是升到哪一步之前的"——
    # 起点是**库当时的版本**（基线 + 已应用的增量），不是基线
    assert f"v{SCHEMA_VERSION}→v{SYNTHETIC.version}" in backup.parent.name

    with sqlite3.connect(backup) as snapshot:
        names = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master")}
        version = snapshot.execute(
            "SELECT value FROM schema_metadata WHERE key = 'version'"
        ).fetchone()[0]
    assert "probe" not in names
    assert version == str(SCHEMA_VERSION)


def test_failed_migration_rolls_back_and_keeps_the_version(database: Database, monkeypatch) -> None:
    """失败时**表与版本一起回滚**，并指出备份在哪——不给半截状态。"""
    monkeypatch.setattr(schema_module, "MIGRATIONS", (BROKEN,))
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", BROKEN.version)
    with pytest.raises(SchemaError) as info:
        ensure_schema(database)
    assert "备份" in str(info.value)
    # 回滚到**失败前那一刻**的版本（基线 + 已应用的增量），不是基线本身
    assert current_version(database) == SCHEMA_VERSION
    with database.read() as conn:
        assert "half_baked" not in _objects(conn, "table")


def test_version_newer_than_the_app_is_refused(database: Database, monkeypatch) -> None:
    """库被更新版应用升过级 → 报错，**不降级**（旧代码写新结构 = 静默损坏）。"""
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", SCHEMA_VERSION - 1)
    with pytest.raises(SchemaError, match="高于本应用已知的"):
        ensure_schema(database)


def test_broken_version_record_is_reported(database: Database) -> None:
    with database.session() as conn:
        conn.execute("UPDATE schema_metadata SET value = '不是数字' WHERE key = 'version'")
    with pytest.raises(SchemaError, match="不是十进制版本号"):
        current_version(database)


def test_baseline_failure_points_at_the_file(tmp_path: Path, monkeypatch) -> None:
    """基线 DDL 本身坏掉时要报一句能查的话（而不是一个裸 sqlite3 错误）。"""
    broken = tmp_path / "broken.sql"
    broken.write_text("CREATE TABLE ok (id TEXT PRIMARY KEY) STRICT;\nNOT SQL AT ALL;\n", "utf-8")
    monkeypatch.setattr(schema_module, "SCHEMA_PATH", broken)
    db = Database(tmp_path / "kylab.db")
    db.open()
    with pytest.raises(SchemaError, match="基线 schema 执行失败"):
        ensure_schema(db)
    db.close()


def _explain(database: Database, sql: str) -> str:
    with database.read() as conn:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    return " | ".join(str(row["detail"]) for row in rows)


# ------------------------------------------------------------------ v3：备份待传队列（M5）


def _stop_at_v2(tmp_path: Path) -> Database:
    """造一份**停在 v2** 的库：基线 + 第一条增量 + 一条升级前的会话。

    这就是用户机器上那份库在拿到 v3 之前的样子。本机库**已经发过版**（M4 起就有增量迁移），
    所以"旧库升上来"这条纪律必须一条条迁移地验——不能靠"拿基线建库再补全部"那一条糊过去。
    """
    db = Database(tmp_path / "kylab.db")
    db.open()
    db.script(SCHEMA_PATH.read_text(encoding="utf-8"))
    with db.session() as conn:
        for statement in MIGRATION_V2_KB_META_CACHE.statements:
            conn.execute(statement)
        conn.execute("UPDATE schema_metadata SET value = ? WHERE key = 'version'", ("2",))
        conn.execute(
            "INSERT INTO schema_metadata (key, value) VALUES (?, ?)",
            (
                KEY_MIGRATION_LOG,
                json.dumps([{"version": 2, "description": "v2", "at_ms": 1}], ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO conversations (id, title, kb_ids, pinned, context_summary,"
            " created_at_ms, updated_at_ms)"
            " VALUES ('c_old', '升级前的会话', '[]', 0, '', 1, 1)"
        )
    return db


def test_a_v2_database_upgrades_to_v3_once(tmp_path: Path) -> None:
    """v2 → v3：队列表与两条索引建出来、版本记到 3、迁移日志追加一条、旧行一条不丢。"""
    db = _stop_at_v2(tmp_path)
    try:
        assert current_version(db) == 2

        assert ensure_schema(db) == SCHEMA_VERSION == 3

        with db.read() as conn:
            tables = set(_objects(conn, "table"))
            indexes = set(_objects(conn, "index"))
            log = json.loads(
                conn.execute(
                    "SELECT value FROM schema_metadata WHERE key = ?", (KEY_MIGRATION_LOG,)
                ).fetchone()["value"]
            )
            recorded = conn.execute(
                "SELECT value FROM schema_metadata WHERE key = 'version'"
            ).fetchone()["value"]
            kept = conn.execute("SELECT title FROM conversations WHERE id = 'c_old'").fetchone()[
                "title"
            ]
        assert "backup_snapshots" in tables
        assert {"idx_backup_snapshots_queue", "idx_backup_snapshots_recent"} <= indexes
        assert recorded == str(SCHEMA_VERSION)
        assert [entry["version"] for entry in log] == [2, MIGRATION_V3_BACKUP_SNAPSHOTS.version]
        assert kept == "升级前的会话"
    finally:
        db.close()


def test_a_migration_backup_survives_each_step_of_the_upgrade(tmp_path: Path) -> None:
    """v1 → v3 留**两份**备份：``-v1→v3``（升级前，v1 状态）与 ``-v2→v3``（上一步之前，v2 状态）。

    目录名的读法是"**从库当时的版本** → **这次升级的目标版本**"。同一次升级里每条迁移各留
    一份，于是"退回到升级前"与"退回到上一步之前"两个落点都拿得到——那个中间态
    （有 ``kb_meta_cache``、还没有 ``backup_snapshots``）只在这一份里存在过。
    """
    db = Database(tmp_path / "kylab.db")
    db.open()
    db.script(SCHEMA_PATH.read_text(encoding="utf-8"))  # 停在 v1
    try:
        ensure_schema(db)

        folders = sorted((tmp_path / "migration-backup").iterdir())
        ranges = [item.name.split("-", 1)[1] for item in folders]
        assert ranges == [
            f"v{BASELINE_VERSION}→v{SCHEMA_VERSION}",
            f"v2→v{SCHEMA_VERSION}",
        ], f"每一次迁移都该留一份（读到 {ranges}）"

        pre_upgrade = _backup_state(folders[0])
        mid_upgrade = _backup_state(folders[1])
        assert pre_upgrade["version"] == str(BASELINE_VERSION)
        assert "kb_meta_cache" not in pre_upgrade["tables"]
        assert "backup_snapshots" not in pre_upgrade["tables"]
        assert mid_upgrade["version"] == "2"
        assert "kb_meta_cache" in mid_upgrade["tables"]
        assert "backup_snapshots" not in mid_upgrade["tables"]

        with db.read() as conn:
            recorded = conn.execute(
                "SELECT value FROM schema_metadata WHERE key = ?", (KEY_LAST_BACKUP,)
            ).fetchone()["value"]
        assert Path(recorded) == folders[-1] / db.path.name, "last_backup 指向最后那一份"
    finally:
        db.close()


def test_repeated_ensure_schema_neither_migrates_nor_backs_up_again(tmp_path: Path) -> None:
    """幂等：第二次 ``ensure_schema`` 既不建表也不再多留备份（反复启动不会攒一堆备份）。"""
    db = Database(tmp_path / "kylab.db")
    db.open()
    try:
        ensure_schema(db)
        backup_dir = tmp_path / "migration-backup"
        first = sorted(item.name for item in backup_dir.iterdir())

        assert ensure_schema(db) == SCHEMA_VERSION

        assert sorted(item.name for item in backup_dir.iterdir()) == first
    finally:
        db.close()


def test_the_two_queue_read_shapes_use_their_indexes(database: Database) -> None:
    """两条索引各对着一种读形状（规范 3："建了但用不上 = 白建"）。

    队列那一问（还等着传的、到点的、最旧在前）与服务层真正下推的那条 ORDER BY 逐字一致；
    最近几份那一问同理。改这两条查询时索引会立刻不匹配——那就是这条用例的作用。
    """
    due = _explain(
        database,
        "SELECT * FROM backup_snapshots"
        " WHERE state IN ('pending', 'uploading', 'failed')"
        "   AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= 1)"
        " ORDER BY created_at_ms ASC, id ASC LIMIT 3",
    )
    recent = _explain(
        database,
        "SELECT * FROM backup_snapshots ORDER BY created_at_ms DESC, id DESC LIMIT 20",
    )

    assert "idx_backup_snapshots_queue" in due
    assert "idx_backup_snapshots_recent" in recent


def _backup_state(folder: Path) -> dict[str, object]:
    """读一份迁移前备份：版本、表名、拿得到那条会话吗。"""
    with sqlite3.connect(folder / "kylab.db") as copy:
        copy.row_factory = sqlite3.Row
        names = {row[0] for row in copy.execute("SELECT name FROM sqlite_master")}
        row = copy.execute("SELECT value FROM schema_metadata WHERE key = 'version'").fetchone()
        kept = copy.execute("SELECT title FROM conversations WHERE id = 'c_old'").fetchone()
    return {"version": str(row[0]), "tables": names, "kept": None if kept is None else kept[0]}
