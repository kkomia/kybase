"""``sqlite_impl/connection.py`` 的单元测试：PRAGMA 口径、写锁、事务边界。

镜像同构：``app/storage/sqlite_impl/connection.py``
→ ``tests/unit/storage/sqlite_impl/test_connection.py``。

标 ``local``：整套只用本机 SQLite 与临时目录，**不需要 PostgreSQL**——
"断 NAS 的机器上本机后端能跑"是 M2 要证明的事，这些用例就是它的一半。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.storage.sqlite_impl.connection import (
    BUSY_TIMEOUT_MS,
    MIN_SQLITE_VERSION,
    Database,
    SqliteVersionError,
)
from app.storage.sqlite_impl.schema import prepare

pytestmark = pytest.mark.local


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "kylab.db")
    # 与装配点的顺序一致：先 open（PRAGMA + 版本自检），再建 schema
    prepare(db)
    yield db
    db.close()


def test_pr_baseline_is_what_the_plan_says(database: Database) -> None:
    """五条 PRAGMA 口径（§1.5）：WAL / busy_timeout / foreign_keys / synchronous / checkpoint。"""
    conn = database.connection()
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == BUSY_TIMEOUT_MS
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    # synchronous=NORMAL 是代号 1；WAL 下它等价于"提交不落 fsync"
    assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1
    assert int(conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]) == 1000


def test_foreign_keys_are_really_enforced(database: Database) -> None:
    """外键**开着**才让 schema 里那几条 ON DELETE 语义成立。

    这条用例是有来由的：``PRAGMA`` 落在未提交事务里会被**静默忽略**，
    所以 ``_connect`` 必须用 ``isolation_level=None``——把它改回去，
    下面这一句就不再报错，而删文件夹不再把笔记退回未归档。
    """
    with database.session() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, kb_ids, pinned, context_summary,"
            " created_at_ms, updated_at_ms) VALUES ('c1', '', '[]', 0, '', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError), database.session() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, conversation_id, role, content, created_at_ms)"
            " VALUES ('m1', 'nope', 'user', 'x', 1)"
        )


def test_session_commits_and_rolls_back(database: Database) -> None:
    with database.session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('a', '1', 1)"
        )
    assert database.connection().execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 1

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), database.session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('b', '2', 2)"
        )
        raise Boom
    assert database.connection().execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 1


def test_nested_session_is_rejected_rather_than_silently_swallowing(
    database: Database,
) -> None:
    """嵌套会让内层的 ROLLBACK 掀掉外层事务，所以必须当场拦下（见 ``session()``）。"""
    with database.session(), pytest.raises(RuntimeError, match="不可嵌套"), database.session():
        pass  # pragma: no cover - 进不来


def test_read_waits_for_nothing_while_a_writer_holds_the_lock(database: Database) -> None:
    """WAL 的关键承诺：**1 写 N 读**。

    写事务开着的时候，另一条线程的 ``read()`` 必须能立刻拿到数据——这正是
    "一轮对话在写、用户同时翻另一条会话"那个场景。写锁只串行化写。
    """
    with database.session() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('k', 'v', 1)")

    holding = threading.Event()
    release = threading.Event()
    seen: list[str] = []
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            with database.session() as conn:
                conn.execute("UPDATE app_settings SET value = 'v2' WHERE key = 'k'")
                holding.set()
                assert release.wait(5)
        except BaseException as exc:  # pragma: no cover - 失败时把异常带回主线程
            failures.append(exc)

    def reader() -> None:
        try:
            assert holding.wait(5)
            started = time.perf_counter()
            with database.read() as conn:
                row = conn.execute("SELECT value FROM app_settings WHERE key = 'k'").fetchone()
            seen.append(f"{row['value']}:{time.perf_counter() - started:.3f}")
        except BaseException as exc:  # pragma: no cover - 同上
            failures.append(exc)
        finally:
            release.set()

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not failures
    assert len(seen) == 1
    # 读到的是**写事务提交前的旧值**（未提交的写对别的连接不可见），且几乎立刻返回
    value, elapsed = seen[0].split(":")
    assert value == "v"
    assert float(elapsed) < 1.0


def test_writes_are_serialized_across_threads(database: Database) -> None:
    """40 个线程同时写也不该出错：写锁把它们排成队，``BEGIN IMMEDIATE`` 不会撞 BUSY。"""
    with database.session() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('n', '0', 1)")

    def bump() -> None:
        for _ in range(5):
            with database.session() as conn:
                conn.execute(
                    "UPDATE app_settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT),"
                    " updated_at_ms = max(updated_at_ms + 1, 1) WHERE key = 'n'"
                )

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    value = database.connection().execute(
        "SELECT value FROM app_settings WHERE key = 'n'"
    ).fetchone()[0]
    assert value == "40"


def test_low_sqlite_version_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    """版本低于 3.37 时**明确报错**，不降级成非 STRICT 表（§1.1）。"""
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.36.0")
    db = Database(tmp_path / "kylab.db")
    with pytest.raises(SqliteVersionError, match="STRICT"):
        db.open()
    assert MIN_SQLITE_VERSION >= (3, 37)


def test_script_runs_atomically(database: Database) -> None:
    """``script()`` 是"整段 DDL 要么全成、要么全不成"——半截 schema 是最难查的状态。"""
    with pytest.raises(sqlite3.Error):
        database.script(
            "CREATE TABLE ok_one (id TEXT PRIMARY KEY) STRICT;\n"
            "CREATE TABLE ok_two (id TEXT PRIMARY KEY, n INTEGER NOT NULL) STRICT;\n"
            "THIS IS NOT SQL;"
        )
    names = {
        row[0]
        for row in database.connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "ok_one" not in names
    assert "ok_two" not in names


def test_maintenance_connection_is_usable_outside_transactions(database: Database) -> None:
    """``VACUUM`` 不能在事务里跑；``maintenance()`` 借的就是一条不在事务里的连接。"""
    with database.maintenance() as conn:
        conn.execute("CREATE TABLE t_probe (id INTEGER PRIMARY KEY) STRICT")
        conn.execute("VACUUM")
        conn.execute("DROP TABLE t_probe")


def test_close_checkpoints_the_wal_into_the_main_file(database: Database) -> None:
    """"关掉应用之后 ``kylab.db`` 是完整的、``-wal`` 是空的"（可整份备份）。

    兜底检查点之后 SQLite 会把 ``-wal`` 直接删掉（内容已全部并回主库），
    所以"空"有两种成立形态：文件不在，或者大小是 0。
    """
    with database.session() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at_ms) VALUES ('k', 'v', 1)")
    wal = Path(f"{database.path}-wal")
    assert wal.exists() and wal.stat().st_size > 0
    database.close()
    assert not wal.exists() or wal.stat().st_size == 0
    # 并回主库之后，另开一条连接仍然读得到那条数据
    reopened = sqlite3.connect(database.path)
    try:
        assert reopened.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == 1
    finally:
        reopened.close()
