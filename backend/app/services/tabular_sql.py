"""表格副本的**只读 SQL 闸**（v0.33）。

`services/tabular.py` 原来只有"按文档读第 N 行"这一条受控查询（架构 §5.2 把
"开放的 SQL 旁路"列为缓做）。但"这个月一共花了多少"这类**聚合**问题，分页读行
答不出来——RAG 对聚合天生弱（它给最相关的几行，而正确答案要求**所有**行）。
所以这里补一条 SQL：**只读、单语句、表范围受调用者可见范围约束**。

`docs/设计/Agent-工作区与能力层设计-v0.1.md` 提到的 D5 没有被推翻：
用户仍然没有"自定义 SQL 的开放入口"（那要求注入防护、资源配额、越权读表一整套），
这一层是**给模型用的一条受控通路**——它有三个硬约束（下面三段），
而其中"表范围"那一约束就是"越权读表"的答案：模型只能看见这一轮允许的库里的表。

三条硬约束：

1. **必须是单条只读语句**（``_skeleton`` 剥掉注释与字符串字面量后按词法判）；
2. **不许碰外部世界**（``read_csv`` / ``ATTACH`` / ``COPY`` / ``INSTALL`` 这类
   能读写文件与联网的东西一律拒掉——DuckDB 的能力远不止查表，这是它与 SQLite
   最不一样的地方）；
3. **表名必须在这一轮允许的范围内**（见 :func:`check_tables`）。

**为什么要剥字符串与注释再判**（而不是直接对原文做关键词匹配）：``SELECT 'DROP'``
里那个 DROP 只是数据，把它当关键字拒掉会让模型莫名其妙；反过来，注释里藏一个
``--`` 之后接关键字也不该被当成"藏了东西"——注释本来就不执行。
所以判定跑在**剥完之后的骨架**上，而真正执行的是**原文**（骨架只用于判定）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.core.exceptions import InvalidRequestError

__all__ = [
    "ALLOWED_STARTERS",
    "MAX_SQL_CHARS",
    "check_tables",
    "identifiers",
    "validate_select",
]

#: SQL 文本上限。一条给模型写的分析查询不该有一万字符；超了通常是它把整张表
#: 的 VALUES 拼进来了（那该用 export_table，不是 SELECT）。
MAX_SQL_CHARS = 4000

#: 允许的起始关键字。**白名单**而不是黑名单：DuckDB 的语句类型比我们想得到的多，
#: 而"只放行这几类"是唯一不会随版本漂移的写法。
ALLOWED_STARTERS = frozenset(
    {
        "select",
        "with",
        "describe",
        "desc",
        "show",
        "explain",
        "summarize",
        "from",
        "values",
        "pivot",
        "unpivot",
        "table",
    }
)

#: 一律拒绝的关键字。分三类，理由各不同：
#:
#: - **写**：``insert`` / ``update`` / ``delete`` / ``drop`` / ``create`` / ``alter``…
#:   表格副本是入库管线的产物，重跑一次摄入就会重建；让模型在里头改数据，
#:   会让"检索到的行"与"结构化副本里的行"变成两份互相矛盾的事实；
#: - **外部世界**：``attach`` / ``copy`` / ``install`` / ``load`` / ``export`` / ``import``
#:   能读写文件、装扩展、连别的库——那是越狱（DuckDB 里 ``COPY (SELECT 1) TO '/tmp/x'``
#:   就是一次磁盘写）；
#: - **会话控制**：``set`` / ``reset`` / ``pragma`` / ``begin`` / ``use`` / ``call``…
#:   改连接级设置会影响**共享这条连接**的其它调用（摄入也在用它）。
_FORBIDDEN = frozenset(
    {
        # 写
        "insert",
        "update",
        "delete",
        "drop",
        "create",
        "alter",
        "truncate",
        "replace",
        "merge",
        "upsert",
        # 外部世界
        "attach",
        "detach",
        "copy",
        "install",
        "load",
        "export",
        "import",
        "checkpoint",
        "vacuum",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_parquet",
        "read_ndjson",
        "read_ndjson_auto",
        "read_text",
        "read_blob",
        "parquet_scan",
        "csv_scan",
        "json_scan",
        "glob",
        "sniff_csv",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "iceberg_scan",
        "delta_scan",
        "httpfs",
        # 会话控制
        "set",
        "reset",
        "pragma",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "use",
        "call",
        "grant",
        "revoke",
        "transaction",
    }
)

#: 一律拒绝的标识符前缀。``duckdb_*`` 是内部函数、``information_schema`` /
#: ``pg_catalog`` 是元数据目录——后者本身不泄露内容，但它能把"库里有哪些文档"
#: 列出来，而"有哪些表"已经由 list_tables 按范围给过了。
_FORBIDDEN_PREFIXES = ("duckdb_", "pragma_", "sqlite_", "pg_", "information_schema", "temp.")

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r"'(?:''|[^'])*'")
#: 尾部的注释与分号。**必须先剥掉再判"有没有第二个分号"**：
#: ``SELECT ...; -- 说明`` 是模型很常见的写法（分号收句、后面跟一句解释），
#: 不剥的话那一个分号会被当成"这是两条语句"而拒掉一条完全合法的查询。
_TRAILING = re.compile(r"(--[^\n]*|/\*.*?\*/|;)\s*\Z", re.DOTALL)


def validate_select(sql: str) -> str:
    """校验并规范化一条只读 SQL，返回**可以执行的那一份**（去掉尾部注释与 ``;``）。

    报错一律说清"为什么不行 + 该怎么办"：模型据此能自己改对，
    而"SQL 不合法"这种话它只能靠猜。
    """
    text = (sql or "").strip()
    if not text:
        raise InvalidRequestError("缺少参数：sql")
    if len(text) > MAX_SQL_CHARS:
        raise InvalidRequestError(f"SQL 太长（{len(text)} 字符，上限 {MAX_SQL_CHARS}）")
    previous = None
    while previous != text:
        previous = text
        text = _TRAILING.sub("", text).rstrip()
    if not text:
        raise InvalidRequestError("这不是一条 SQL")

    skeleton = _skeleton(text)
    if ";" in skeleton:
        raise InvalidRequestError("一次只能跑一条语句（检测到多个分号）。想跑几步就分几次调用")
    tokens = [item.lower() for item in _WORD.findall(skeleton)]
    if not tokens:
        raise InvalidRequestError("这不是一条 SQL")
    if tokens[0] not in ALLOWED_STARTERS:
        raise InvalidRequestError(
            "只允许只读查询（SELECT / WITH / DESCRIBE / SHOW / EXPLAIN），"
            f"收到的是 {tokens[0].upper()}。表格副本是只读的，要改数据请重新摄入源文件"
        )
    banned = sorted({item for item in tokens if item in _FORBIDDEN})
    if banned:
        raise InvalidRequestError(
            f"这条 SQL 里有不允许的用法：{'、'.join(item.upper() for item in banned)}。"
            "只能查已经入库的表格副本，不能读写文件、装扩展或改连接设置"
        )
    prefixed = sorted(
        {item for item in tokens if any(item.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES)}
    )
    if prefixed:
        raise InvalidRequestError(
            f"这条 SQL 碰了不该碰的东西：{'、'.join(prefixed)}（内部函数或元数据目录）"
        )
    return text


def identifiers(sql: str) -> list[str]:
    """SQL 里的标识符（剥掉字符串与注释后的词）。**用于表名范围判定**。"""
    return _WORD.findall(_skeleton(sql))


def check_tables(sql: str, *, known: Iterable[str], allowed: Iterable[str]) -> None:
    """表名必须落在允许的范围内。

    判定方式：把 SQL 里的标识符与**库里真实存在的表名**取交集，交出来的那些
    必须全部在允许集合里。这样既能挡住"查了别的库的表"，也能挡住拼错的表名
    （拼错的会落到 DuckDB 那句"表不存在"，也是一条能读懂的报错）。

    为什么不做 SQL 解析：DuckDB 的语法面比"我们想解析的那一小块"大得多
    （CTE、子查询、PIVOT、表函数…），一个半吊子解析器给出的"安全"是假的。
    而"标识符里出现过的真实表名"是一条**不会漏**的判据：不出现表名就查不到它的数据。
    """
    known_set = set(known)
    allowed_set = set(allowed)
    referenced = sorted({item for item in identifiers(sql) if item in known_set})
    outside = [item for item in referenced if item not in allowed_set]
    if outside:
        usable = "、".join(sorted(allowed_set)) or "（这一轮没有可查的表格）"
        raise InvalidRequestError(
            f"这些表不在这一轮能查的范围内：{'、'.join(outside)}。能查的是：{usable}。"
            "要查别的文档，先把它的入库范围调对（或让对方在会话上勾上那个知识库）"
        )


def _skeleton(sql: str) -> str:
    """剥掉注释与字符串字面量，剩下的骨架用于判定。

    顺序要紧：先剥块注释、再剥行注释、最后剥字符串——反过来会把
    ``'--'`` 这种字符串里的内容当成注释的开头，于是把它后面**真正**的语句
    一起吃掉（那正是"藏一条语句"的经典写法）。
    """
    text = _BLOCK_COMMENT.sub(" ", sql)
    text = _LINE_COMMENT.sub(" ", text)
    return _STRING.sub("''", text)
