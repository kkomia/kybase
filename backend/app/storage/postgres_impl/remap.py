"""一次性运维动作：把库从 kylab 合体期的**交错迁移序列**重写成 KB 独立序列。

## 这是什么、什么时候跑

2026-10-20 的数据分家把 Agent 域的表与迁移从本服务里删掉了：基线不再建
``conversations`` / ``chat_messages`` / ``notes`` / ``note_tags``，迁移序列也从
"KB 与 Agent 交错连号"（v2..v19）重编成 KB 自己的连号（v2..v8）。

**新库用不到这个模块**：空库启动时会按新基线一次建好。
只有**分家之前就存在的那台库**才需要跑一次——它的表与字段都齐（KB 迁移早就
应用过），差的只是 ``schema_migrations`` 里那串旧版本号。应用启动时检测到旧头
（``LEGACY_INTERLEAVED_HEAD``）会明确拒绝并指向这里，不会带着错版本号继续跑。

## 用法

在 ``backend/`` 下（或容器里的 ``/app``，即镜像的 WORKDIR）：

```sh
python -m app.storage.postgres_impl.remap                 # dry-run：只打印计划
python -m app.storage.postgres_impl.remap --apply         # 真的重写 schema_migrations
python -m app.storage.postgres_impl.remap --apply --drop-agent-tables --yes
python -m app.storage.postgres_impl.remap --apply --purge-legacy-tasks --yes
```

DSN 取 ``--dsn`` 或环境变量 ``KYLAB_DATABASE_URL``（与应用的引导级配置同一个）。

NAS 上升级的标准姿势（应用还起不来时 ``exec`` 没有容器可用，用 ``run``）：

```sh
cd /vol1/1000/docker/kybase/app
docker compose run --rm --no-deps backend \\
    python -m app.storage.postgres_impl.remap --apply
```

## 三个可选的清理动作

- ``--drop-agent-tables``：drop 分家后不再属于本服务的 10 张表（会话 / 笔记 /
  工作区 / 定时任务 / 外部 MCP 登记 / 会话事件 / 会话产物）。**数据会没**——
  dry-run 会先把每张表的行数打出来；不带 ``--apply --yes`` 不会真删。
- ``--purge-legacy-tasks``：清掉 ``tasks`` 队列里 ``memory`` / ``scheduled`` 两种
  kind 的历史行。它们属于 Agent 的定时任务/记忆，分家后没有消费者，被领到只会
  "明确失败"；清掉是为了别让队列里留一堆永远失败的行。
- 两个动作都**幂等**：表已经不存在、行已经清过，再跑一遍是空操作。

退出码：``0`` = 完成或无需操作；``1`` = 拒绝执行（库状态不符，原因已打印）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from app.storage.postgres_impl.connection import Database
from app.storage.postgres_impl.schema import (
    LEGACY_INTERLEAVED_HEAD,
    SCHEMA_VERSION,
    current_version,
    legacy_agent_versions,
    legacy_remap_plan,
)

#: 分家之后不再属于本服务的表（顺序不重要，drop 都带 CASCADE）。
AGENT_TABLES = (
    "conversation_artifacts",
    "session_events",
    "chat_messages",
    "scheduled_tasks",
    "mcp_servers",
    "workspaces",
    "note_tags",
    "note_folders",
    "notes",
    "conversations",
)

#: 分家后不再有消费者的任务类型（见 ``models/enums.TaskKind`` 的说明）。
LEGACY_TASK_KINDS = ("memory", "scheduled")

#: 分家后仍必须存在的对象：``(类型, 名字)``。
#:
#: **这一份是"这台库真的在 KB 头部"的核对清单**，不是从 SQL 文本里猜的：
#: 版本号重写本身不验证物理结构，写错会让应用带着"以为已经到 8"的认知启动。
#: 七条 KB 迁移各挑一个可观察对象（表 / 索引 / 列），漏一条就拒绝执行。
REQUIRED_OBJECTS: tuple[tuple[str, str], ...] = (
    ("table", "document_stage_events"),
    ("index", "idx_parse_results_parser_created"),
    ("index", "idx_document_stage_events_entered"),
    ("index", "idx_tasks_state_kind"),
    ("column", "documents.summary"),
    ("column", "knowledge_bases.system_prompt"),
    ("column", "users.avatar_key"),
    ("column", "api_keys.allowed_tool_names"),
    ("table", "search_events"),
)


def _describe(version: int) -> str:
    """新序列里某一版的说明（给 dry-run 的人看的）。"""
    from app.storage.postgres_impl.schema import MIGRATIONS

    for item in MIGRATIONS:
        if item.version == version:
            return item.description
    return "基线"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.storage.postgres_impl.remap",
        description="把 kylab 合体期的交错迁移序列重写成 KB 独立序列（一次性运维动作）。",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("KYLAB_DATABASE_URL"),
        help="PostgreSQL 连接串；默认取环境变量 KYLAB_DATABASE_URL",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写入（默认是 dry-run，只打印计划与核对结果）",
    )
    parser.add_argument(
        "--drop-agent-tables",
        action="store_true",
        help="同时 drop 分家后不再属于本服务的 10 张表（需要 --apply 与 --yes）",
    )
    parser.add_argument(
        "--purge-legacy-tasks",
        action="store_true",
        help="同时清掉 tasks 队列里 memory / scheduled 两种历史行（需要 --apply 与 --yes）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行会丢数据的动作（--drop-agent-tables / --purge-legacy-tasks）",
    )
    return parser.parse_args(argv)


def _current_rows(db: Database) -> list[dict[str, Any]]:
    with db.read() as conn:
        return list(
            conn.execute(
                "select version, description, applied_at from schema_migrations"
                " order by version"
            )
        )


def _missing_objects(db: Database) -> list[str]:
    """清单里的对象缺了哪些（空 = 这台库确实在 KB 头部）。"""
    missing: list[str] = []
    with db.read() as conn:
        for kind, name in REQUIRED_OBJECTS:
            if kind == "column":
                table, column = name.split(".", 1)
                row = conn.execute(
                    "select 1 as hit from information_schema.columns"
                    " where table_schema = current_schema()"
                    "   and table_name = %s and column_name = %s",
                    (table, column),
                ).fetchone()
            else:
                row = conn.execute("select to_regclass(%s) as hit", (name,)).fetchone()
                if row is not None and row["hit"] is None:
                    row = None
            if row is None:
                missing.append(f"{kind} {name}")
    return missing


def _table_rows(db: Database, table: str) -> int | None:
    """表存在时返回行数；不存在返回 ``None``（drop 的 dry-run 用）。"""
    with db.read() as conn:
        exists = conn.execute("select to_regclass(%s) as hit", (table,)).fetchone()
        if exists is None or exists["hit"] is None:
            return None
        row = conn.execute(f"select count(*) as n from {table}").fetchone()  # noqa: S608
        return int(row["n"]) if row is not None else 0


def _legacy_task_rows(db: Database) -> int:
    placeholders = ", ".join(["%s"] * len(LEGACY_TASK_KINDS))
    with db.read() as conn:
        row = conn.execute(
            f"select count(*) as n from tasks where kind in ({placeholders})",  # noqa: S608
            LEGACY_TASK_KINDS,
        ).fetchone()
    return int(row["n"]) if row is not None else 0


def _rewrite_versions(db: Database, rows: list[dict[str, Any]]) -> None:
    """把 schema_migrations 重写成新序列（保留 applied_at）。"""
    plan = legacy_remap_plan()
    descriptions = {int(row["version"]): str(row["description"]) for row in rows}
    applied = {int(row["version"]): row["applied_at"] for row in rows}
    with db.session() as conn:
        conn.execute("delete from schema_migrations")
        for old in sorted(plan):
            new = plan[old]
            conn.execute(
                "insert into schema_migrations (version, description, applied_at)"
                " values (%s, %s, %s)",
                (new, descriptions.get(old, _describe(new)), applied.get(old)),
            )


def _drop_agent_tables(db: Database) -> None:
    with db.session() as conn:
        # 依赖顺序不用管：CASCADE 会带走外键引用方（都是 Agent 侧的表之间）
        for table in AGENT_TABLES:
            conn.execute(f"drop table if exists {table} cascade")


def _purge_legacy_tasks(db: Database) -> None:
    placeholders = ", ".join(["%s"] * len(LEGACY_TASK_KINDS))
    with db.session() as conn:
        conn.execute(
            f"delete from tasks where kind in ({placeholders})",  # noqa: S608
            LEGACY_TASK_KINDS,
        )


def _confirm(flag: bool, what: str) -> bool:
    if flag:
        return True
    print(f"拒绝执行：{what} 会丢数据，需要显式加 --yes 确认。")
    return False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.dsn:
        print("缺少 DSN：给 --dsn 或设置 KYLAB_DATABASE_URL。", file=sys.stderr)
        return 1

    db = Database(str(args.dsn))
    db.open()
    try:
        version = current_version(db)
        print(f"当前 schema 版本：{version}")
        if version is None:
            print("这是一个空库：不需要 remap，直接启动应用会按新基线建库。")
            return 0
        if version == SCHEMA_VERSION:
            print(f"已经在新序列上（v{SCHEMA_VERSION}），无需重写版本号。")
        elif version == LEGACY_INTERLEAVED_HEAD:
            rows = _current_rows(db)
            if not args.apply:
                print(
                    f"\n检测到 kylab 合体期的交错序列头 v{LEGACY_INTERLEAVED_HEAD}"
                    f"（新序列头是 v{SCHEMA_VERSION}）。重写计划（旧 -> 新）："
                )
                for old, new in sorted(legacy_remap_plan().items()):
                    print(f"  {old:>2} -> {new:<2}  {_describe(new)}")
                print(f"\n删掉的旧版本行（只属于 Agent 域）：{sorted(legacy_agent_versions())}")
                print("\n这是 dry-run。确认无误后加 --apply 真正写入。")
                return 0
            missing = _missing_objects(db)
            if missing:
                print(
                    "拒绝执行：这台库缺少下面这些 KB 对象，"
                    "不能当成「已到 KB 头部」：",
                    file=sys.stderr,
                )
                for item in missing:
                    print(f"  - {item}", file=sys.stderr)
                return 1
            expected = set(legacy_remap_plan()) | set(legacy_agent_versions())
            actual = {int(row["version"]) for row in rows}
            if actual != expected:
                print(
                    "拒绝执行：schema_migrations 里的版本集合和旧序列对不上——"
                    f"多出 {sorted(actual - expected) or '无'}，"
                    f"缺少 {sorted(expected - actual) or '无'}。",
                    file=sys.stderr,
                )
                return 1
            _rewrite_versions(db, rows)
            print(f"已重写版本号：{LEGACY_INTERLEAVED_HEAD} -> {SCHEMA_VERSION}")
            print(f"现在版本：{current_version(db)}")
        else:
            print(
                f"拒绝执行：版本 {version} 既不是旧序列头 {LEGACY_INTERLEAVED_HEAD}，"
                f"也不是新序列头 {SCHEMA_VERSION}。请人工确认这台库的状态。",
                file=sys.stderr,
            )
            return 1

        # ---- 可选清理动作（都要 --apply + --yes）----
        if args.drop_agent_tables:
            if not args.apply:
                print("\n--drop-agent-tables 需要同时给 --apply；dry-run 下只列行数：")
                for table in AGENT_TABLES:
                    count = _table_rows(db, table)
                    if count is not None:
                        print(f"  {table}: {count} 行")
                return 0
            if not _confirm(args.yes, "drop 这 10 张 Agent 表"):
                return 1
            _drop_agent_tables(db)
            print(f"已 drop {len(AGENT_TABLES)} 张 Agent 表。")

        if args.purge_legacy_tasks:
            if not args.apply:
                pending = _legacy_task_rows(db)
                print(
                    "\n--purge-legacy-tasks 需要同时给 --apply；"
                    f"dry-run 下：待清 {pending} 行。"
                )
                return 0
            if not _confirm(args.yes, "清掉 tasks 里的历史 memory / scheduled 行"):
                return 1
            _purge_legacy_tasks(db)
            print("已清掉 tasks 队列里的历史 Agent 任务行。")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
