"""本机库自检（运维入口）。

用途：断 NAS 排障、桌面端"库在哪/通不通/schema 对不对"这类问题上跑一条命令，
不必先起整个应用。也是启动路径上调用的同一套校验（``prepare``），
所以这里通过就意味着本机后端能起来。

    python -m app.storage.sqlite_impl.validate --data-dir <数据目录>
    python -m app.storage.sqlite_impl.validate                # 取 KYLAB_DATA_DIR

缺 ``--data-dir`` 且没配 ``KYLAB_DATA_DIR`` 时用 ``./data``（与 ``Settings`` 的
默认一致），并**把它打印出来**——"我刚才查的是哪个库"不该靠猜。

与 ``postgres_impl/validate.py`` 的差异：那边查扩展（pgvector）与距离运算，
这边查 ``STRICT`` 表是否真的生效、``foreign_keys`` 开没开、WAL 是不是立住了——
三件都是"这个库能不能被当成本机权威库用"的硬条件。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from app.core.config import get_settings
from app.storage.sqlite_impl.connection import MIN_SQLITE_VERSION, Database
from app.storage.sqlite_impl.schema import SCHEMA_VERSION, current_version, ensure_schema

#: 本机库文件的名字（`Settings` 与装配点都按它拼路径）。
DB_FILENAME = "kylab.db"

_TABLE_COUNT_SQL = """
select count(*) as n
  from sqlite_master
 where type = 'table' and name not like 'sqlite_%'
"""

_INDEX_COUNT_SQL = """
select count(*) as n
  from sqlite_master
 where type = 'index' and name not like 'sqlite_%'
"""

#: 知识库元数据快照表（M4 §3.1）的用量：多少行、多少字节。
#:
#: 方案 §3.4-4 点名要把它报出来——这张表装的是**从 NAS 抄下来的库名与文档名**，
#: 用户与排障的人都有权知道"本机留了多少"。字节按 ``payload`` 的 UTF-8 长度算，
#: 与淘汰时那把尺子**逐字一致**（``sqlite_impl/meta_store.py::_prune_kb_meta_cache``）：
#: 报出来的数与淘汰时的数若不是一个口径，"还剩多少"这句话就没人敢信。
_CACHE_USAGE_SQL = """
select count(*) as n,
       coalesce(sum(length(cast(payload as blob))), 0) as payload_bytes
  from kb_meta_cache
"""


def _data_dir(argv: list[str]) -> Path:
    """``--data-dir <路径>`` 优先，其次 ``KYLAB_DATA_DIR``，最后 ``./data``。"""
    if "--data-dir" in argv:
        index = argv.index("--data-dir")
        if index + 1 < len(argv):
            return Path(argv[index + 1])
        raise SystemExit("--data-dir 后面要跟一个路径")
    configured = get_settings().data_dir
    return Path(configured) if configured else Path("./data")


def _table_count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    data_dir = _data_dir(args)
    db_path = data_dir / DB_FILENAME

    print(f"目标：{db_path}")

    version = sqlite3.sqlite_version
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        need = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        print(f"[FAIL] 内置 SQLite {version} 低于 {need}，STRICT 表不可用")
        return 1
    print(f"[OK  ] SQLite 版本 {version}（STRICT 表可用）")

    db = Database(db_path)
    try:
        try:
            db.open()
        except Exception as exc:
            print(f"[FAIL] 打开/初始化失败：{exc}")
            return 1

        with db.read() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if str(mode).lower() != "wal":
            print(f"[FAIL] journal_mode = {mode}（期望 wal：并发写要靠它）")
            return 1
        print("[OK  ] journal_mode = wal（1 写 N 读）")
        if not foreign_keys:
            print("[FAIL] foreign_keys 没打开：删文件夹/删工作区的那几条级联语义会失效")
            return 1
        print("[OK  ] foreign_keys = ON")

        try:
            schema_version = ensure_schema(db)
        except Exception as exc:
            print(f"[FAIL] schema 无法满足应用要求：{exc}")
            return 1
        if schema_version != SCHEMA_VERSION:
            print(f"[FAIL] schema 版本 {schema_version}，应用期望 {SCHEMA_VERSION}")
            return 1
        print(f"[OK  ] schema 版本 = {schema_version}")

        with db.read() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = _table_count(conn, _TABLE_COUNT_SQL)
            indexes = _table_count(conn, _INDEX_COUNT_SQL)
        if str(integrity) != "ok":
            print(f"[FAIL] integrity_check：{integrity}")
            return 1
        print("[OK  ] integrity_check = ok")
        print(f"[OK  ] 本机表 {tables} 张、索引 {indexes} 条")

        with db.read() as conn:
            cache_rows, cache_bytes = conn.execute(_CACHE_USAGE_SQL).fetchone()
        print(f"[OK  ] 知识库元数据快照 {cache_rows} 行、{cache_bytes} 字节")

        # 借一次真实的写往返确认写路径可用（PRAGMA 只说明"读得动"）
        with db.session() as conn:
            conn.execute(
                "INSERT INTO schema_metadata (key, value) VALUES ('validate_probe', '1')"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
            )
        print("[OK  ] 写事务可用（BEGIN IMMEDIATE + 提交）")

        stats = {"version": current_version(db)}
    finally:
        db.close()

    print(f"\n结论：本机库可用（schema 版本 v{stats['version']}，文件 {db_path}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
