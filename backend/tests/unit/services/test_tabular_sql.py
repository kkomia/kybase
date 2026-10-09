"""表格副本的只读 SQL 闸（v0.33）。

镜像同构：``app/services/tabular_sql.py`` → 本文件。

这一层是"给模型一条 SQL 通路"与"别把 DuckDB 交出去"之间的那道墙，
所以用例按**三类硬约束**分组：

1. **单条只读语句**：多条语句、写语句、DDL 一律拒；
2. **不许碰外部世界**：能读写文件、装扩展、连别的库的那些（``read_csv`` / ``ATTACH`` /
   ``COPY`` / ``INSTALL``）一律拒——DuckDB 的能力远不止查表，这是它与 SQLite 最不一样的地方；
3. **表名必须在允许范围内**：这是"越权读表"的答案（模型只能看见这一轮允许的库里的表）。

另外两条边界是**不能误伤**的：字符串字面量里的关键字（``SELECT 'DROP'``）与注释里的
关键字都不该被当成违规——判定跑在剥掉它们之后的骨架上。误伤的代价是模型反复改一条
本来就合法的查询，而它不知道为什么被拒。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.tabular_sql import check_tables, identifiers, validate_select

# ------------------------------------------------------------------ 放行


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM doc_abc",
        "select 金额, count(*) from doc_abc group by 金额 order by 2 desc",
        "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
        "DESCRIBE doc_abc",
        "EXPLAIN SELECT * FROM doc_abc",
        "SELECT * FROM doc_abc WHERE 名称 = '打折' AND 金额 > 100",
        "SELECT 'DROP TABLE x' AS 说明 FROM doc_abc",
        "SELECT * FROM doc_abc -- 这是注释，里面提到 DROP",
        "/* 提一句 ATTACH 也不行 */ SELECT 1 FROM doc_abc",
    ],
)
def test_legitimate_reads_pass(sql: str) -> None:
    assert validate_select(sql)


def test_trailing_semicolon_and_comment_are_trimmed() -> None:
    """``SELECT ...; -- 说明`` 是模型很常见的写法，不该被读成"两条语句"。"""
    assert validate_select("  SELECT 1 FROM doc_a; -- 说明") == "SELECT 1 FROM doc_a"
    assert validate_select("SELECT 1 FROM doc_a;\n") == "SELECT 1 FROM doc_a"


def test_identifiers_come_from_the_skeleton() -> None:
    """标识符取自剥掉字符串后的骨架：字符串里的词不算（否则 ``'doc_x'`` 会被当表名）。"""
    assert "drop" not in identifiers("SELECT 'drop' FROM doc_a")
    assert "doc_a" in identifiers("SELECT 'drop' FROM doc_a")


# ------------------------------------------------------------------ 拒：语句形态


# 这些字符串就是"用户/模型写出来的 SQL"，bandit 看到拼接就报；
# 本文件测的正是"这些写法会不会被拦下"
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE doc_abc",
        "DELETE FROM doc_abc",
        "INSERT INTO doc_abc VALUES (1)",
        "UPDATE doc_abc SET a = 1",
        "CREATE TABLE t AS SELECT 1",
        "ALTER TABLE doc_abc ADD COLUMN x int",
        "TRUNCATE doc_abc",
        "SET memory_limit = '1GB'",
        "PRAGMA database_list",
        "BEGIN",
        "CALL some_function()",
        "SELECT 1; DROP TABLE doc_abc",
    ],
)
def test_non_read_statements_are_rejected(sql: str) -> None:
    with pytest.raises(InvalidRequestError):
        validate_select(sql)


def test_empty_sql_is_rejected() -> None:
    with pytest.raises(InvalidRequestError, match="缺少参数"):
        validate_select("   ")


def test_overlong_sql_is_rejected() -> None:
    """拼一条超长 SQL（**被测的就是"把 SQL 拼长"**，bandit 那条 S608 在这里是误报）。"""
    overlong = "SELECT * FROM doc_a WHERE x IN (" + ",".join(["1"] * 3000) + ")"  # noqa: S608
    with pytest.raises(InvalidRequestError, match="太长"):
        validate_select(overlong)


# ------------------------------------------------------------------ 拒：外部世界


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
        "SELECT * FROM glob('/**')",
        "ATTACH 'other.db' AS other",
        "COPY (SELECT 1) TO '/tmp/x.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM sqlite_scan('x.db', 't')",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM duckdb_settings()",
    ],
)
def test_touching_the_outside_world_is_rejected(sql: str) -> None:
    with pytest.raises(InvalidRequestError):
        validate_select(sql)


def test_the_reason_names_what_was_refused() -> None:
    """报错要点名（模型据此换一条路），而不是笼统一句"不合法"。"""
    with pytest.raises(InvalidRequestError, match="READ_CSV"):
        validate_select("SELECT * FROM read_csv('x.csv')")
    with pytest.raises(InvalidRequestError, match="一次只能跑一条语句"):
        validate_select("SELECT 1; SELECT 2")


# ------------------------------------------------------------------ 表范围


def test_tables_outside_the_scope_are_rejected() -> None:
    with pytest.raises(InvalidRequestError, match="不在这一轮能查的范围内"):
        check_tables(
            "SELECT * FROM doc_secret", known={"doc_secret", "doc_mine"}, allowed={"doc_mine"}
        )


def test_unknown_table_is_left_to_the_engine() -> None:
    """拼错的表名不该在这一层被拒——DuckDB 那句"表不存在"是更精确的报错。"""
    check_tables("SELECT * FROM doc_typo", known={"doc_mine"}, allowed={"doc_mine"})


def test_join_across_scopes_is_rejected() -> None:
    """JOIN 里夹带一张别的库的表，是最容易被忽略的一种越权。"""
    with pytest.raises(InvalidRequestError, match="doc_other"):
        check_tables(
            "SELECT * FROM doc_mine m JOIN doc_other o ON m.id = o.id",
            known={"doc_mine", "doc_other"},
            allowed={"doc_mine"},
        )


def test_strings_that_look_like_table_names_do_not_count() -> None:
    """``WHERE name = 'doc_other'`` 只是数据，不是"查了那张表"。"""
    check_tables(
        "SELECT * FROM doc_mine WHERE name = 'doc_other'",
        known={"doc_mine", "doc_other"},
        allowed={"doc_mine"},
    )
