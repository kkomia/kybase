"""schema 分家后的静态契约（**不需要 PostgreSQL**）。

2026-10-20 的数据分家把 Agent 侧的迁移与表从本服务删掉了：迁移序列从
"KB 与 Agent 交错连号"（v2..v19）重编成 KB 自己的连号（v2..v8），基线也不再建
``conversations`` / ``chat_messages`` / ``notes`` / ``note_tags``。

这份用例守三件**不需要连库就能抓**的事：

1. 迁移序列真的是连号，且每条都记着自己的旧编号（一次性运维脚本靠它）；
2. 旧交错序列 → 新序列的映射与"哪些旧版本只属于 Agent"是钉死的（remap 的口径，
   写错了会把线上库重写成错误版本号）；
3. 基线 schema.sql 里不再声明任何 Agent 表（改回去会让新库又长出一套别人的表）。
"""

from __future__ import annotations

import re

import pytest

from app.storage.postgres_impl.schema import (
    LEGACY_INTERLEAVED_HEAD,
    MIGRATIONS,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    legacy_agent_versions,
    legacy_remap_plan,
)

#: 这一组只读常量与 schema.sql 文本，不连库——显式标 `local`，
#: 于是没有 PG 测试库的机器上也会真跑（否则会被 conftest 整组跳过）。
pytestmark = pytest.mark.local

#: 分家前的旧交错序列：KB 迁移（保留，重编）与 Agent 迁移（删掉）的旧编号。
OLD_KB_VERSIONS = (2, 3, 4, 8, 11, 18, 19)
OLD_AGENT_VERSIONS = {5, 6, 7, 9, 10, 12, 13, 14, 15, 16, 17}


def test_the_kb_sequence_is_contiguous_and_starts_after_the_baseline() -> None:
    assert SCHEMA_VERSION == 8, "分家后的 KB 独立序列头（基线 1 + 七条 KB 迁移）"
    assert [item.version for item in MIGRATIONS] == list(range(2, SCHEMA_VERSION + 1))


def test_every_migration_records_its_legacy_number() -> None:
    legacy = [item.legacy_version for item in MIGRATIONS]
    assert None not in legacy, "每条迁移都要记旧编号——一次性运维脚本靠它做映射"
    assert len(set(legacy)) == len(legacy), "旧编号不许重复"
    assert tuple(legacy) == OLD_KB_VERSIONS


def test_legacy_head_is_the_kylab_interleaved_head() -> None:
    assert LEGACY_INTERLEAVED_HEAD == 19
    assert max(legacy_remap_plan()) == LEGACY_INTERLEAVED_HEAD


def test_legacy_remap_plan_is_pinned() -> None:
    """旧 → 新的映射是**线上库升级的唯一依据**，所以逐条钉死。"""
    assert legacy_remap_plan() == {
        1: 1,
        2: 2,
        3: 3,
        4: 4,
        8: 5,
        11: 6,
        18: 7,
        19: 8,
    }


def test_agent_versions_are_exactly_the_removed_ones() -> None:
    assert legacy_agent_versions() == OLD_AGENT_VERSIONS
    assert set(legacy_remap_plan()) | legacy_agent_versions() == set(
        range(1, LEGACY_INTERLEAVED_HEAD + 1)
    ), "旧序列 1..19 必须被「保留」与「删除」两类完整覆盖，不留缺口"


def _declared_tables() -> set[str]:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return set(re.findall(r"CREATE TABLE\s+(\w+)", body))


def test_the_baseline_no_longer_declares_agent_tables() -> None:
    tables = _declared_tables()
    agent_tables = {"conversations", "chat_messages", "notes", "note_tags"}
    assert tables & agent_tables == set(), (
        f"基线里还留着 Agent 侧的表：{sorted(tables & agent_tables)}"
    )
    # KB 本体的表当然要在（防止"删对了别人的、也删掉了自己的"）
    assert {"knowledge_bases", "documents", "chunks", "wiki_pages", "tasks"} <= tables
