"""PG schema 装配的集成测试（需要真实 PostgreSQL，默认跳过）。

**为什么必须从空库开始**：schema 基线里含 ``CREATE EXTENSION IF NOT EXISTS vector``，
所以"空库 → 建 schema → 扩展就位"是一条真实存在的启动路径。而开发服务器上的库
早就装好扩展了，那条路径在真机上**看不出来**——顺序写反（先查扩展再建 schema）
一样能跑通。这条用例专门造一个空库来跑它。

运行方式（指向任意一个带 pgvector 的 PG，必须是超级用户以免 CREATE EXTENSION 被拒）：

    KYLAB_TEST_DATABASE_URL="postgresql://USER:PASSWORD@host:5432/postgres" \
      pytest tests/integration/storage/test_postgres_schema.py -q

**这里曾经写的是一组真实的开发库凭据**（用户名、口令、端口都是真的）——
示例里放真口令等于把口令提交进了仓库，而"示例"正是最容易被复制粘贴到别处的东西。
用占位符：读的人只需要看出**形状**（谁:什么@哪:哪/哪个库）。
"""

from __future__ import annotations

import os
import re
import uuid

import psycopg
import pytest
from psycopg import sql

from app.storage.postgres_impl.connection import Database
from app.storage.postgres_impl.schema import (
    MIGRATIONS,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    SchemaError,
    current_version,
    ensure_schema,
    prepare,
)

ADMIN_DSN = os.environ.get("KYLAB_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_DSN,
    reason="未提供 KYLAB_TEST_DATABASE_URL，跳过需要真实 PostgreSQL 的集成测试",
)


def _declared_tables() -> set[str]:
    """schema.sql 里定义的业务表名。

    **不写死数量**：不同 PG 镜像会往 ``template1`` 里预装扩展（paradedb 就带了
    PostGIS），于是新库会自带一些与本项目无关的表。断言"本项目声明的表都在"，
    既不依赖镜像，也不会因为镜像多带东西而误报。
    """
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return set(re.findall(r"CREATE TABLE\s+(\w+)", body))


def _missing_tables(db: Database) -> set[str]:
    with db.read() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' and table_type = 'BASE TABLE'"
        ).fetchall()
    present = {row["table_name"] for row in rows}
    return _declared_tables() - present


@pytest.fixture
def fresh_db() -> Database:
    """在真实 PG 上造一个**不含任何扩展**的空库，用完删掉。"""
    assert ADMIN_DSN is not None
    name = f"kylab_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))

    dsn = psycopg.conninfo.make_conninfo(ADMIN_DSN, dbname=name)
    db = Database(dsn)
    db.open()
    try:
        yield db
    finally:
        db.close()
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )


def test_current_version_is_none_before_schema(fresh_db: Database) -> None:
    """空库还没有 schema_migrations 表——不能因此报错，要能识别成 None。"""
    assert current_version(fresh_db) is None


def test_prepare_bootstraps_a_fresh_database(fresh_db: Database) -> None:
    """空库上 prepare 必须能一次建成：建表 + 装扩展 + 补齐增量，而不是先抱怨缺扩展。"""
    assert prepare(fresh_db) == SCHEMA_VERSION
    assert _missing_tables(fresh_db) == set(), "schema.sql 声明的表应全部建成"

    with fresh_db.read() as conn:
        row = conn.execute(
            "select extname from pg_extension where extname = 'vector'"
        ).fetchone()
    assert row is not None, "prepare 之后 pgvector 应当已启用"


def test_prepare_is_idempotent(fresh_db: Database) -> None:
    """启动会反复调用：第二次不能因为"表已存在"而失败。"""
    first = prepare(fresh_db)
    second = prepare(fresh_db)
    assert first == second == SCHEMA_VERSION
    assert _missing_tables(fresh_db) == set()


def test_version_older_than_baseline_is_rejected(fresh_db: Database) -> None:
    """库的 schema 比应用旧 → 拒绝启动（而不是带着缺失的列继续跑）。"""
    prepare(fresh_db)
    with fresh_db.session() as conn:
        conn.execute("delete from schema_migrations")

    with pytest.raises(SchemaError, match="低于应用要求的基线"):
        ensure_schema(fresh_db)


def test_version_newer_than_schema_version_is_rejected(fresh_db: Database) -> None:
    """库被更新版应用升过级 → 同样拒绝，避免旧代码往新结构里写。"""
    prepare(fresh_db)
    with fresh_db.session() as conn:
        conn.execute(
            "insert into schema_migrations (version, description) values (%s, %s)",
            # "来自未来"的定义随 SCHEMA_VERSION 走：比它再高一级才算库被升过级
            (SCHEMA_VERSION + 1, "未来版本"),
        )

    with pytest.raises(SchemaError, match="高于本应用已知的"):
        ensure_schema(fresh_db)


def test_migration_to_the_latest_version_runs_on_an_older_database(
    fresh_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**在一条旧库上升级**，而不是只在空库上一次建完（P0-2 的迁移用例）。

    空库那条路（``prepare``）会走 schema.sql 基线 + 全部增量，于是"某条增量写错了"
    很难被发现——它可能只在**已经有数据的库**上才炸（列名撞了、约束重名）。
    所以这里先把库停在上一个版本，再让它升级，并核对新表真的建出来了。

    这也是"只增不改"纪律的守卫：把 ``MIGRATIONS`` 截到上一版再放回去，
    任何"回头去改已发布那条"的做法都会在这条用例里露出来。
    """
    from app.storage.postgres_impl import schema as schema_module

    previous = SCHEMA_VERSION - 1
    older = tuple(item for item in MIGRATIONS if item.version <= previous)
    monkeypatch.setattr(schema_module, "MIGRATIONS", older)
    monkeypatch.setattr(schema_module, "SCHEMA_VERSION", previous)
    assert prepare(fresh_db) == previous, "先造一个上一版的库"

    monkeypatch.undo()
    assert ensure_schema(fresh_db) == SCHEMA_VERSION, "升级应当只补缺的那几条"

    with fresh_db.read() as conn:
        columns = conn.execute(
            "select column_name, data_type from information_schema.columns"
            " where table_name = 'session_events' order by ordinal_position"
        ).fetchall()
        unique = conn.execute(
            "select conname from pg_constraint"
            " where conrelid = 'session_events'::regclass and contype = 'u'"
        ).fetchall()
    assert [row["column_name"] for row in columns] == [
        "id",
        "conversation_id",
        "seq",
        "kind",
        "payload",
        "created_at",
    ]
    assert [row["conname"] for row in unique] == ["uq_session_events_seq"], (
        "会话内 seq 的唯一约束是「只追加」的机械保证"
    )
