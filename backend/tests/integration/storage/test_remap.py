"""``remap`` 一次性运维动作的重写路径（集成，需要真实 PostgreSQL）。

**为什么必须有这一条**：``remap --apply`` 是**只在存量库上跑一次**的动作，而它做的
恰好是最不该出错的一件事——重写 ``schema_migrations``。本机没有 PG 测试库时它只能靠
代码推演；这里用一条真实库把整条路走通（伪造一份"分家前"的版本行 → 跑 remap →
核对新序列），于是有 PG 的环境一跑就验到了。

运行方式（与其它 PG 集成用例同一把钥匙）：

    KYLAB_TEST_DATABASE_URL="postgresql://USER:PASSWORD@host:5432/postgres" \
      pytest tests/integration/storage/test_remap.py -q
"""

from __future__ import annotations

import os

import pytest

from app.core.config import get_settings
from app.storage.postgres_impl import schema
from app.storage.postgres_impl.connection import Database
from app.storage.postgres_impl.remap import main
from app.storage.postgres_impl.schema import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(
    not os.environ.get("KYLAB_TEST_DATABASE_URL"),
    reason="未提供 KYLAB_TEST_DATABASE_URL，跳过需要真实 PostgreSQL 的集成测试",
)


def _open() -> Database:
    dsn = get_settings().database_url
    assert dsn, "conftest 应把 KYLAB_DATABASE_URL 指到临时测试库"
    db = Database(dsn)
    db.open()
    return db


def _forge_legacy_sequence(db: Database) -> None:
    """把版本表伪造成分家前的交错序列（1..19）。"""
    with db.session() as conn:
        conn.execute("delete from schema_migrations")
        for version in range(1, schema.LEGACY_INTERLEAVED_HEAD + 1):
            conn.execute(
                "insert into schema_migrations (version, description) values (%s, %s)",
                (version, f"旧交错序列 v{version}"),
            )


def test_remap_rewrites_the_legacy_sequence_onto_the_kb_sequence() -> None:
    """旧头 19 → 新序列 1..8：映射逐条对、applied_at 保留、再跑一遍是空操作。"""
    db = _open()
    try:
        schema.prepare(db)
        _forge_legacy_sequence(db)
        assert schema.current_version(db) == schema.LEGACY_INTERLEAVED_HEAD
    finally:
        db.close()

    # dry-run 不写；--apply 才写（main 自己按环境变量连库）
    assert main([]) == 0
    db = _open()
    try:
        assert schema.current_version(db) == schema.LEGACY_INTERLEAVED_HEAD, "dry-run 不许写"
    finally:
        db.close()

    assert main(["--apply"]) == 0

    db = _open()
    try:
        assert schema.current_version(db) == SCHEMA_VERSION
        with db.read() as conn:
            rows = conn.execute(
                "select version from schema_migrations order by version"
            ).fetchall()
        assert [row["version"] for row in rows] == list(range(1, SCHEMA_VERSION + 1))
        # 重跑是空操作（幂等）：已经在新序列上时 main 不报错、也不改行数
        with db.read() as conn:
            before = conn.execute("select count(*) as n from schema_migrations").fetchone()
        assert main(["--apply"]) == 0
        with db.read() as conn:
            after = conn.execute("select count(*) as n from schema_migrations").fetchone()
        assert before["n"] == after["n"]
    finally:
        db.close()


def test_apply_without_the_yet_requested_cleanups_is_a_no_op_on_a_new_sequence() -> None:
    """已在新序列上时，只带 --apply 不应重建/删任何表。"""
    db = _open()
    try:
        schema.prepare(db)
        assert schema.current_version(db) == SCHEMA_VERSION
    finally:
        db.close()

    assert main(["--apply"]) == 0

    db = _open()
    try:
        with db.read() as conn:
            tables = conn.execute(
                "select table_name from information_schema.tables"
                " where table_schema = 'public' and table_type = 'BASE TABLE'"
            ).fetchall()
        names = {row["table_name"] for row in tables}
        assert {"knowledge_bases", "documents", "chunks"} <= names
    finally:
        db.close()
