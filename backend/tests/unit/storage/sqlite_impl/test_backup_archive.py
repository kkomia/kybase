"""``backup_archive`` 的擦洗与读面判据（M5 阶段 2，方案 §2.4 / §3.5）。

镜像同构：``app/storage/sqlite_impl/backup_archive.py``
→ ``tests/unit/storage/sqlite_impl/test_backup_archive.py``。

**这一份用例守的是全场最要命的那件事：快照里绝不放秘密**（方案 §2.4 / 风险 R1）。
三条判据按强度排：

1. **副本的原始字节里没有哨兵串**（最强的那个：不是"列被清了"，而是"字节里查不到"）；
2. **同一份库如果不擦洗，那些哨兵串就在副本字节里**——这一条是变异验证：
   它证明第 1 条真的查得出东西，而不是在查一份本来就没有秘密的库；
3. **逐条擦洗**：``model_providers.api_key`` 清空、``mcp_servers.env`` / ``headers`` 清成
   ``{}``、``app_settings`` 里 ``SECRET_KEYS`` ∪ 三个前缀的行删掉，且无关的键**必须留下**
   （判据松了会漏、紧了会误伤，两个方向都要有断言）。

第三个判据的清单**从常量取**（``SECRET_KEYS`` / ``SNAPSHOT_EXCLUDED_SETTING_PREFIXES``）：
将来新增一个 SECRET_KEY，``test_every_secret_setting_is_deleted`` 自动多一条用例——
这正是方案 §2.4"引用常量本身、不手抄"那条要求在用例这一侧的形态。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.auth import URL_SIGNING_SECRET_SETTING
from app.services.runtime_config import SECRET_KEYS
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ARTIFACT_IN_WORKSPACE,
    SNAPSHOT_EXCLUDED_SETTING_PREFIXES,
    SnapshotFormatError,
    SnapshotRedaction,
    StorageError,
)
from app.storage.sqlite_impl import backup_archive
from app.storage.sqlite_impl.backup_archive import (
    dump_scrubbed_db,
    iter_snapshot_transfers,
    local_schema_version,
    read_snapshot_db,
)
from app.storage.sqlite_impl.connection import Database
from app.storage.sqlite_impl.schema import SCHEMA_VERSION, prepare

pytestmark = pytest.mark.local

#: 三类明文凭据各用一个**互不相同**的哨兵串：grep 判据要能指认是谁漏的。
MODEL_SENTINEL = "kylab_sk_SENTINELmodel7f3a91bc"
MCP_SENTINEL = "mcp_SENTINELtoken91bc7de2"
SETTING_SENTINEL = "tavily_SENTINELsearch7de2a1f0"
PREFIX_SENTINEL = "provider_SENTINELfamily4c8e"

#: 本机自己的**签名材料**（``auth.url_signing_secret``）另用一个哨兵串：
#: 它与其他几类不是一回事——那是本机档在组合根生成的一条（见
#: ``services/auth.ensure_url_signing_secret``），**换一台机器重新生成更好**
#: （签出去的链接本来就该重签），所以它同样不该跟着快照走。
SIGNING_SENTINEL = "signing_SENTINELurlsecret4e7d"

ALL_SENTINELS = (
    MODEL_SENTINEL,
    MCP_SENTINEL,
    SETTING_SENTINEL,
    PREFIX_SENTINEL,
    SIGNING_SENTINEL,
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "kylab.db")
    prepare(db)
    yield db
    db.close()


def _seed_secrets(db: Database) -> None:
    """把本机库里那三处明文凭据都种进去（照 ``schema.sql`` 的列与 CHECK）。"""
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
        for key in sorted(SECRET_KEYS):
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, ?, 1)",
                (key, SETTING_SENTINEL),
            )
        for prefix in SNAPSHOT_EXCLUDED_SETTING_PREFIXES:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, ?, 1)",
                (f"{prefix}demo_key", PREFIX_SENTINEL),
            )
        # 真实的那条键（名字从常量取，不手抄）：它是 `auth.` 这一族的第一个成员，
        # 而"整族排除"这条纪律要在这个**真名**上也成立（合成的 `auth.demo_key` 只证明
        # 前缀匹配，证明不了"这条真的会被洗掉"）。
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, ?, 1)",
            (URL_SIGNING_SECRET_SETTING, SIGNING_SENTINEL),
        )


def _copy_path(tmp_path: Path) -> Path:
    """副本的落点：**必须是另一个目录**——``tmp_path/kylab.db`` 是夹具那个源库自己。"""
    return tmp_path / "dump" / "kylab.db"


def _setting_keys(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [str(row[0]) for row in conn.execute("SELECT key FROM app_settings ORDER BY key")]


def _copy_bytes(db: Database, path: Path) -> bytes:
    """照文件拷（**不擦洗**）：只用来做变异验证，不属于任何产品路径。"""
    with sqlite3.connect(path) as sink:
        db.connection().backup(sink)
    return path.read_bytes()


# ------------------------------------------------------------------ 副本的形状


def test_dump_leaves_a_single_file_without_wal(database: Database, tmp_path: Path) -> None:
    """副本是**一个**文件：没有 ``-wal`` / ``-shm``，也不是 WAL 模式（方案 §2.4-4）。

    不是 WAL 这一点比"没有 -wal 文件"更强：WAL 库要有 ``-shm`` 才能读，而这份副本将来会
    被**只读打开**（阶段 5 的按点恢复读的就是它）——一个"必须能写才能读"的副本在恢复
    现场就是个坑。
    """
    dest = _copy_path(tmp_path)
    report = dump_scrubbed_db(database, dest)

    assert sorted(item.name for item in dest.parent.iterdir()) == ["kylab.db"]
    assert report.path == dest
    assert report.bytes == dest.stat().st_size
    with sqlite3.connect(dest) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"


def test_dump_over_an_existing_copy_cleans_the_leftovers(
    database: Database, tmp_path: Path
) -> None:
    """同一条路径打第二次也能跑（先清掉上一次可能留下的 ``-wal`` / ``-shm``）。"""
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)
    dest.with_name("kylab.db-wal").write_bytes(b"leftover")
    dump_scrubbed_db(database, dest)

    assert sorted(item.name for item in dest.parent.iterdir()) == ["kylab.db"]


def test_dump_refuses_to_write_over_the_source(database: Database) -> None:
    """副本不许落在**源库自己的路径**上：那条路会先把源库删掉（换目录是调用方的事）。

    这条拦的是"写错一个参数"，而代价是用户的整库——所以它值一条用例，而不是靠注释。
    """
    with pytest.raises(StorageError) as excinfo:
        dump_scrubbed_db(database, database.path)

    assert str(database.path) in str(excinfo.value)
    with database.read() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM schema_metadata").fetchone()["n"] > 0


# ------------------------------------------------------------------ 最强的那条：字节级


def test_no_plaintext_survives_in_the_copy_bytes(database: Database, tmp_path: Path) -> None:
    """**最强判据**：副本的原始字节里，四个哨兵串一个都查不到。

    为什么必须是字节级而不是"查那一列是不是空"：SQLite 的 UPDATE / DELETE **不覆盖旧页
    内容**，明文会残留在空闲页里（风险 R11）。只查列的话，一份"列看着干净、空闲页里躺着
    密钥"的副本会轻松通过——而它一旦上传，秘密就出去了。
    """
    _seed_secrets(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)

    raw = dest.read_bytes()
    for sentinel in ALL_SENTINELS:
        assert sentinel.encode() not in raw, f"副本字节里还有 {sentinel}"


def test_an_unscrubbed_copy_would_contain_them(database: Database, tmp_path: Path) -> None:
    """**变异验证**：同一份库不擦洗，哨兵串就在副本字节里。

    没有这一条，上面那条 grep 可能是"查一个本来就没有秘密的库"——假绿。它同时钉住
    "哨兵串真的写进了库、而且真的在能被 grep 到的位置上"。
    """
    _seed_secrets(database)
    raw = _copy_bytes(database, tmp_path / "raw.db")

    assert MODEL_SENTINEL.encode() in raw
    assert SETTING_SENTINEL.encode() in raw
    # 五个哨兵一个都不能少（含那条签名密钥）：**变异验证**要的正是"这份副本里
    # 确实有那些字"，否则上面那条"擦完 0 命中"可能只是查错了地方。
    for sentinel in ALL_SENTINELS:
        assert sentinel.encode() in raw, f"{sentinel} 不在未擦洗的副本里"


def test_the_source_database_is_untouched(database: Database, tmp_path: Path) -> None:
    """擦洗做在**副本**上：源库一个字节不动（方案 §2.4 的开头那句）。"""
    _seed_secrets(database)
    dump_scrubbed_db(database, _copy_path(tmp_path))

    with database.read() as conn:
        api_key = conn.execute("SELECT api_key FROM model_providers").fetchone()["api_key"]
        mcp = conn.execute("SELECT env, headers FROM mcp_servers").fetchone()
    assert api_key == MODEL_SENTINEL
    assert MCP_SENTINEL in mcp["env"] and MCP_SENTINEL in mcp["headers"]
    assert _setting_keys(database.path) == sorted(
        [
            *SECRET_KEYS,
            *(f"{prefix}demo_key" for prefix in SNAPSHOT_EXCLUDED_SETTING_PREFIXES),
            # 真实的那条签名密钥（`auth.` 这一族的第一个成员）也原样留着
            URL_SIGNING_SECRET_SETTING,
        ]
    )


# ------------------------------------------------------------------ 逐条擦洗


def test_model_provider_api_key_is_emptied_and_reported(database: Database, tmp_path: Path) -> None:
    """``model_providers.api_key`` → ``''``（行留着：恢复后用户还要重配这家供应商）。"""
    _seed_secrets(database)
    dest = _copy_path(tmp_path)
    report = dump_scrubbed_db(database, dest)

    with sqlite3.connect(dest) as conn:
        row = conn.execute("SELECT id, api_key FROM model_providers").fetchone()
    assert row[0] == "mp_1"
    assert row[1] == ""
    assert SnapshotRedaction(table="model_providers", column="api_key", rows=1) in report.redacted


def test_mcp_env_and_headers_are_emptied_and_reported(database: Database, tmp_path: Path) -> None:
    """``mcp_servers.env`` / ``headers`` → ``'{}'``（两列一条报告，照方案 §2.3 的形状）。"""
    _seed_secrets(database)
    dest = _copy_path(tmp_path)
    report = dump_scrubbed_db(database, dest)

    with sqlite3.connect(dest) as conn:
        row = conn.execute("SELECT id, env, headers FROM mcp_servers").fetchone()
    assert row[0] == "mcp_1"
    assert row[1] == "{}" and row[2] == "{}"
    assert SnapshotRedaction(table="mcp_servers", column="env,headers", rows=1) in report.redacted


@pytest.mark.parametrize("key", sorted(SECRET_KEYS))
def test_every_secret_setting_is_deleted(key: str, database: Database, tmp_path: Path) -> None:
    """``SECRET_KEYS`` 里的**每一个**键都会被删掉（清单从常量取，不手抄）。

    参数化在 ``SECRET_KEYS`` 上：将来加一个密钥键，这里自动多一条——判据的"机械防漂"
    因此有两层（实现引用常量 + 用例遍历常量）。
    """
    with database.session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, ?, 1)",
            (key, SETTING_SENTINEL),
        )
    dest = _copy_path(tmp_path)
    report = dump_scrubbed_db(database, dest)

    assert key not in _setting_keys(dest)
    assert SnapshotRedaction(table="app_settings", key=key, rows=1) in report.redacted


@pytest.mark.parametrize("prefix", SNAPSHOT_EXCLUDED_SETTING_PREFIXES)
def test_every_excluded_prefix_family_is_deleted(
    prefix: str, database: Database, tmp_path: Path
) -> None:
    """三个前缀族**整族**删掉，而"只像前缀"的键必须留下（判据不许宽到误伤）。

    ``provider_not_prefixed`` 是刻意造的邻居：它含 ``provider`` 但不含那个点。
    判据按"前缀 + 点"下刀，它就该原样留着——判据宽一格，用户的行为设置就会在恢复后
    莫名其妙地少几个键。
    """
    neighbour = f"{prefix.rstrip('.')}_not_prefixed"
    with database.session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, ?, 1)",
            (f"{prefix}demo_key", PREFIX_SENTINEL),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES (?, 'ok', 1)",
            (neighbour,),
        )
    dest = _copy_path(tmp_path)
    report = dump_scrubbed_db(database, dest)

    keys = _setting_keys(dest)
    assert f"{prefix}demo_key" not in keys
    assert neighbour in keys
    assert (
        SnapshotRedaction(table="app_settings", key=f"{prefix}demo_key", rows=1) in report.redacted
    )


def test_unrelated_settings_paths_survive(database: Database, tmp_path: Path) -> None:
    """白名单外的设置与凭据入口**一行不动**（擦洗不是"清库"）。"""
    with database.session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('ui.theme', 'dark', 1)"
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms)"
            " VALUES ('memory.workspace', 'memory', 1)"
        )
        conn.execute(
            "INSERT INTO model_providers"
            " (id, kind, name, base_url, api_key, created_at_ms, updated_at_ms)"
            " VALUES ('mp_2', 'openai', '没配钥匙的那家', 'https://api.example.com', '', 1, 1)"
        )
    dest = _copy_path(tmp_path)
    report = dump_scrubbed_db(database, dest)

    assert _setting_keys(dest) == ["memory.workspace", "ui.theme"]
    assert report.redacted == ()
    """一个空列、一个无关键都不该出现在"洗掉了什么"里——报多了同样是假话。"""


# ------------------------------------------------------------------ 读数


def test_counts_and_schema_version_are_reported(database: Database, tmp_path: Path) -> None:
    """``counts`` 是库侧事实的行数：manifest 的 ``counts`` 段直接用它们。"""
    with database.session() as conn:
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at_ms, updated_at_ms)"
            " VALUES ('ws_1', '项目', '/tmp/ws', 1, 1)"
        )
        conn.execute(
            "INSERT INTO conversations (id, title, created_at_ms, updated_at_ms)"
            " VALUES ('c_1', '一次对话', 1, 2), ('c_2', '又一次', 3, 4)"
        )
        conn.execute(
            "INSERT INTO chat_messages (id, conversation_id, role, content, created_at_ms)"
            " VALUES ('m_1', 'c_1', 'user', '你好', 1), ('m_2', 'c_1', 'assistant', '在', 2),"
            "        ('m_3', 'c_2', 'user', '再问', 3)"
        )
        conn.execute(
            "INSERT INTO session_events (conversation_id, seq, kind, created_at_ms)"
            " VALUES ('c_1', 0, 'turn', 1), ('c_1', 1, 'turn', 2)"
        )
        conn.execute(
            "INSERT INTO notes (id, title, created_at_ms, updated_at_ms)"
            " VALUES ('n_1', '一条笔记', 1, 1)"
        )
        conn.execute(
            "INSERT INTO scheduled_tasks (id, name, prompt, kind, created_at_ms, updated_at_ms)"
            " VALUES ('t_1', '每天', '写日报', 'cron', 1, 1)"
        )
        conn.execute(
            "INSERT INTO conversation_artifacts"
            " (id, conversation_id, name, format, size_bytes, storage, location, created_at_ms)"
            " VALUES ('a_1', 'c_1', '报告.docx', 'docx', 12, 'object',"
            "         'conversations/c_1/a_1.docx', 1)"
        )
    report = dump_scrubbed_db(database, _copy_path(tmp_path))

    assert report.counts == {
        "conversations": 2,
        "messages": 3,
        "session_events": 2,
        "notes": 1,
        "workspaces": 1,
        "scheduled_tasks": 1,
        "artifacts": 1,
        "settings": 0,
    }
    assert report.schema_version == SCHEMA_VERSION


# ------------------------------------------------------------------ 读面


def _seed_for_reading(db: Database) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at_ms, updated_at_ms)"
            " VALUES ('c_1', '一次对话', 1, 5), ('c_2', '又一次', 2, 9)"
        )
        conn.execute(
            "INSERT INTO chat_messages (id, conversation_id, role, content, created_at_ms)"
            " VALUES ('m_1', 'c_1', 'user', '你好', 1)"
        )
        conn.execute(
            "INSERT INTO conversation_artifacts"
            " (id, conversation_id, name, format, size_bytes, storage, location, created_at_ms)"
            " VALUES ('a_1', 'c_1', '报告.docx', 'docx', 12, 'object',"
            "         'conversations/c_1/a_1.docx', 1),"
            "        ('a_2', 'c_2', '大报告.pptx', 'pptx', 34, 'workspace',"
            "         'E:/项目/大报告.pptx', 2)"
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('ui.theme', 'dark', 1)"
        )


def test_read_snapshot_db_reports_what_is_in_the_package(
    database: Database, tmp_path: Path
) -> None:
    """读面回四项：会话（含条数）/ 产物 Key / 设置键 / 计数（阶段 5 的恢复输入）。"""
    _seed_for_reading(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)
    view = read_snapshot_db(dest)

    assert view.schema_version == SCHEMA_VERSION
    assert view.counts["conversations"] == 2
    assert [(item.id, item.title, item.messages) for item in view.conversations] == [
        ("c_2", "又一次", 0),
        ("c_1", "一次对话", 1),
    ]
    """最近更新的在前（与产品里那份会话列表同一顺序）。"""
    assert [(item.id, item.storage, item.location) for item in view.artifacts] == [
        ("a_1", "object", "conversations/c_1/a_1.docx"),
        ("a_2", "workspace", "E:/项目/大报告.pptx"),
    ]
    assert view.settings_keys == ("ui.theme",)


def test_read_snapshot_db_does_not_touch_the_snapshot(database: Database, tmp_path: Path) -> None:
    """只读打开：读一份快照**不产生** ``-journal`` / ``-wal`` / ``-shm``。

    快照是观察对象。读它留下一个 ``-journal`` 意味着"这次读有可能改过它"——
    而阶段 5 是在用户的恢复现场读它的，那里最不该出现"半截写"的可能。
    """
    _seed_for_reading(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)
    before = sorted(item.name for item in dest.parent.iterdir())

    read_snapshot_db(dest)

    assert sorted(item.name for item in dest.parent.iterdir()) == before


def test_read_snapshot_db_refuses_a_newer_schema(database: Database, tmp_path: Path) -> None:
    """快照里的库版本比本机新 → **当场拒绝**（方案 §2.3 的兼容判据，不猜着读）。"""
    _seed_for_reading(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)
    with sqlite3.connect(dest) as conn:
        conn.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'version'",
            (str(SCHEMA_VERSION + 1),),
        )

    with pytest.raises(SnapshotFormatError) as excinfo:
        read_snapshot_db(dest)
    assert str(SCHEMA_VERSION + 1) in str(excinfo.value)


def test_read_snapshot_db_refuses_a_library_without_a_version(
    database: Database, tmp_path: Path
) -> None:
    """没有版本号的库（不是本应用打的 / 被打断了）同样拒绝——"读到 0"不等于"就当 v1 用"。"""
    dest = _copy_path(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(dest) as conn:
        conn.execute("CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    with pytest.raises(SnapshotFormatError):
        read_snapshot_db(dest)


# ---------------------------------------------------- 逐会话全量（阶段 5 加的那条读面）
#
# 这一节是**按点恢复**的输入：``iter_snapshot_transfers`` 逐条吐 ``ConversationTransfer``，
# 导入器因此不需要知道"这一批是从 NAS 拉的还是从包里读的"。判据有三类：
#
# ① 与 ``read_snapshot_db`` 同一份事实（计数能对上）；② 三条子表的顺序**逐字照存储层
# 那三条 ``ORDER BY``**（顺序错了，导进来的会话"哪句话在前"就错了）；③ 该拒的拒
# （版本不认识），该只读的只读（读完不留 ``-wal``）。


def _seed_for_transfers(db: Database) -> None:
    """种两条会话：时间顺序与插入顺序**刻意不一致**（否则 SQL 排没排看不出来）。"""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO conversations"
            " (id, title, kb_ids, owner_id, model_pk, thinking, thinking_effort, pinned,"
            "  context_summary, summary_upto, archived_at_ms, created_at_ms, updated_at_ms)"
            " VALUES ('c_1', '先写的', '[\"kb_1\", \"kb_2\"]', 'usr_1', 'mp_1::gpt', 1, 'high', 1,"
            "         '压过的上下文', 'm_1b', NULL, 10, 20),"
            "        ('c_2', '后写的', '[]', NULL, NULL, NULL, NULL, 0,"
            "         '', NULL, NULL, 30, 40)"
        )
        # 插入顺序是 b 先、a 后，而时间戳是 a 在前、b 在后（同一毫秒里也照 rowid）
        conn.execute(
            "INSERT INTO chat_messages"
            " (id, conversation_id, role, content, sources, steps, thinking, attachments,"
            "  created_at_ms)"
            " VALUES ('m_1b', 'c_1', 'assistant', '后一句', '[]', '[]', '', '[]', 21),"
            "        ('m_1a', 'c_1', 'user', '前一句', '[{\"index\": 1}]',"
            '         \'[{"tool": "search"}]\', \'想过\', \'[{"key": "k"}]\', 21),'
            "        ('m_1c', 'c_1', 'user', '更晚的一句', '[]', '[]', '', '[]', 99)"
        )
        # 事件按 seq 排：插入顺序 2 → 1 → 3
        conn.execute(
            "INSERT INTO session_events (conversation_id, seq, kind, payload, created_at_ms)"
            " VALUES ('c_1', 2, 'turn/complete', '{\"status\": \"ok\"}', 5),"
            "        ('c_1', 1, 'turn/start', '{\"query\": \"你好\"}', 9),"
            "        ('c_1', 3, 'note', '{}', 1)"
        )
        conn.execute(
            "INSERT INTO conversation_artifacts"
            " (id, conversation_id, name, format, size_bytes, storage, location,"
            "  workspace_id, created_at_ms)"
            " VALUES ('a_2', 'c_1', '晚的.docx', 'docx', 2, 'object', 'conversations/c_1/b.docx',"
            "         NULL, 8),"
            "        ('a_1', 'c_1', '早的.pptx', 'pptx', 1, 'workspace', 'E:/项目/早.pptx',"
            "         'ws_1', 7),"
            "        ('a_3', 'c_2', '别的.docx', 'docx', 3, 'object', 'conversations/c_2/c.docx',"
            "         NULL, 9)"
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms)"
            " VALUES ('ui.theme', 'dark', 1), ('llm.temperature', '0.3', 1)"
        )
        conn.execute(
            "INSERT INTO model_providers (id, kind, name, base_url, api_key, created_at_ms,"
            " updated_at_ms) VALUES ('mp_1', 'openai', '供应商甲', 'https://x', '', 1, 1)"
        )


def test_iter_snapshot_transfers_reads_the_whole_conversation(
    database: Database, tmp_path: Path
) -> None:
    """逐会话全量：会话那几个字段 / 摘要上界 / 消息 / 事件 / 产物**逐条读回来**。

    与导入那条链看到的形状同一种：``ConversationTransfer``（导入器只认它）。
    """
    _seed_for_transfers(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)

    transfers = {item.conversation.id: item for item in iter_snapshot_transfers(dest)}

    assert sorted(transfers) == ["c_1", "c_2"], "会话按 updated_at_ms DESC"
    first = transfers["c_1"]
    assert first.conversation.title == "先写的"
    assert tuple(first.conversation.kb_ids) == ("kb_1", "kb_2")
    assert first.conversation.owner_id == "usr_1"
    assert first.conversation.model_pk == "mp_1::gpt"
    assert first.conversation.thinking is True, "三态列：1 是 True，不是「跟随默认」"
    assert first.conversation.thinking_effort == "high"
    assert first.conversation.pinned is True
    assert first.conversation.archived_at is None
    assert first.conversation.created_at is not None
    assert first.summary == "压过的上下文" and first.summary_upto == "m_1b"
    # 消息：created_at_ms 升序，同一毫秒里按 rowid（先插入的在前）
    assert [item.id for item in first.messages] == ["m_1b", "m_1a", "m_1c"]
    assert first.messages[1].sources == ({"index": 1},)
    assert first.messages[1].steps == ({"tool": "search"},)
    assert first.messages[1].thinking == "想过"
    assert first.messages[1].attachments == ({"key": "k"},)
    # 事件：**按 seq**（时间戳在这是乱的：5 / 9 / 1）
    assert [item.seq for item in first.events] == [1, 2, 3]
    assert [item.kind for item in first.events] == ["turn/start", "turn/complete", "note"]
    assert first.events[0].payload == {"query": "你好"}
    assert first.events[0].id is not None
    # 产物：created_at_ms 升序（工作区那一份**保原样**：location 是那台机器上的路径）
    assert [item.id for item in first.artifacts] == ["a_1", "a_2"]
    assert first.artifacts[0].storage == ARTIFACT_IN_WORKSPACE
    assert first.artifacts[0].location == "E:/项目/早.pptx"
    assert first.artifacts[1].storage == ARTIFACT_IN_OBJECTS
    # 第二条会话是空的：一条消息都没有时也该读回来（不是"跳过这条"）
    assert transfers["c_2"].messages == [] and transfers["c_2"].events == []
    assert transfers["c_2"].conversation.thinking is None, "NULL 保持三态里的「跟随默认」"


def test_both_read_surfaces_agree_on_the_same_facts(database: Database, tmp_path: Path) -> None:
    """两条读面（清单视图 / 逐会话全量）对同一份快照给出**一致**的事实。

    它们服务两个动作（预演与真恢复），而"预演说会新建 2 条，真恢复却只进来 1 条"是最坏的
    一种不一致——所以计数与逐条读出来的条数在这里对齐。
    """
    _seed_for_transfers(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)

    view = read_snapshot_db(dest)
    transfers = list(iter_snapshot_transfers(dest))

    assert view.counts["conversations"] == len(transfers) == 2
    assert view.counts["messages"] == sum(len(item.messages) for item in transfers)
    assert view.counts["session_events"] == sum(len(item.events) for item in transfers)
    assert view.counts["artifacts"] == sum(len(item.artifacts) for item in transfers)
    assert view.counts["settings"] == len(view.settings) == len(view.settings_keys)
    assert [item.id for item in view.conversations] == [
        item.conversation.id for item in transfers
    ], "会话清单与逐条读的顺序同一条（updated_at_ms DESC, id）"
    assert view.settings_keys == tuple(view.settings), "两份设置读面同源"
    assert view.model_provider_names == ("供应商甲",), "恢复后要重配的模型凭据按名字说"


def test_iter_snapshot_transfers_is_read_only(database: Database, tmp_path: Path) -> None:
    """只读打开：读完一份快照不留 ``-journal`` / ``-wal`` / ``-shm``（与清单视图同一条）。"""
    _seed_for_transfers(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)
    before = sorted(item.name for item in dest.parent.iterdir())

    list(iter_snapshot_transfers(dest))

    assert sorted(item.name for item in dest.parent.iterdir()) == before


def test_iter_snapshot_transfers_refuses_a_newer_schema(database: Database, tmp_path: Path) -> None:
    """版本比本机新 → **当场拒绝**（与 ``read_snapshot_db`` 同一个判据，不猜着读）。"""
    _seed_for_transfers(database)
    dest = _copy_path(tmp_path)
    dump_scrubbed_db(database, dest)
    with sqlite3.connect(dest) as conn:
        conn.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'version'",
            (str(SCHEMA_VERSION + 1),),
        )

    with pytest.raises(SnapshotFormatError):
        list(iter_snapshot_transfers(dest))


def test_local_schema_version_is_the_schema_constant_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``local_schema_version`` 报的就是 ``SCHEMA_VERSION`` 那个常量（**不另抄一个数**）。

    判据是"改常量它跟着变"：手抄一个数的那种写法在这里会当场红——而它红得很值，
    因为"打包时写进 manifest 的版本"与"读快照时判据用的版本"分叉意味着**新库被旧码读**。
    """
    assert local_schema_version() == SCHEMA_VERSION

    monkeypatch.setattr(backup_archive, "SCHEMA_VERSION", SCHEMA_VERSION + 7)

    assert local_schema_version() == SCHEMA_VERSION + 7
