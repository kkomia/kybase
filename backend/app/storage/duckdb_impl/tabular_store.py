"""``TabularStore`` 的 DuckDB 实现（M2 / T2.11）。

**为什么是列式库而不是又一张 SQLite 表**：见 ``storage/base.py`` 里
``TabularStore`` 的说明——按行列定位的查询、与主库物理分离。

**关于 SQL 拼接（flake8-bandit S608）**：表名与列名**不能参数化**——
SQL 的参数占位符只用于值，标识符必须拼进语句文本。所以这里的做法是
**白名单校验（``_require_safe``）+ 标识符引号转义（``_quote``）**，
并对这三条语句显式豁免 S608。把标识符也当成"拼接就是不安全"来回避，
只会逼出更绕、更容易出错的写法。

**一切列都是 VARCHAR。** 这是刻意的：推断类型会把 ``007`` 变成 ``7``、
把长数字转成科学计数法、把日期格式改掉，而这些都是**数据本身的损失**，
且用户很难发现。表格进知识库是为了能被检索和核对，不是做统计计算——
保真比类型重要。（需要类型时在查询层转，那时用户自己知道该转成什么。）
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.storage.base import SAFE_KEY_CHARS, TabularStore

if TYPE_CHECKING:
    # 只在注解里用到（``duckdb.DuckDBPyConnection`` ✓）：`from __future__ import annotations`
    # 之下注解不求值 ✓，所以这一句**不进运行时**、也就不进客户端的导入闭包 ✓。
    import duckdb

#: 惰性拿到的 duckdb（**故意不是模块级导入** ✗）。
_DUCKDB: Any = None


def _duckdb() -> Any:
    """第一次建连接时才导入 duckdb（模块级导入会让**客户端运行时**凭空多背一份 ✗）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 ✓，而"表格副本"（DuckDB 文件）
    只有**服务器**的导出/表格那条链会碰 ✓ —— 边车（客户端运行时）跑循环 + 工具 + 沙箱 ✓，
    从不落表格副本。模块级 ``import duckdb`` 的后果实测过（P4-3，2026-09-29）：
    ``dist\\sidecar-runtime`` 里少一份 duckdb 就 ``ModuleNotFoundError`` ✗。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，duckdb 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
    """
    global _DUCKDB
    if _DUCKDB is None:
        import duckdb

        _DUCKDB = duckdb
    return _DUCKDB

__all__ = ["DuckDbTabularStore"]

logger = logging.getLogger(__name__)

#: 行号列名。**带前缀且不可能与用户列重名**：用户的表里也可能有一列叫"行号"，
#: 撞上会让写入直接失败（或更糟——静默覆盖掉用户的数据）。
_ORDINAL = "__kylab_row"


class DuckDbTabularStore(TabularStore):
    """用一个独立的 DuckDB 文件存所有表格副本。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        #: 连接**延迟到第一次真正用时**才建。
        #
        # 为什么必须延迟（实测踩到）：DuckDB 一个文件只允许一个写进程，
        # 而 `build_stores()` 是"启动就建全部仓储"。于是**只要后端在跑**，
        # 任何另一个进程（`kylab-mcp` 的 stdio 模式、脚本、测试）一启动就炸在
        # `Cannot open file ... File is already open in ... python.exe`。
        # 而 stdio MCP 服务的**标准用法就是被客户端当子进程拉起**——
        # 也就是说：不延迟的话，这个功能在"后端也在跑"这个最常见的前提下根本用不了。
        #
        # 单连接仍然够用：表格副本是写入少、读取少的旁路数据，
        # 而 DuckDB 的多连接要走同样的文件锁，收益不抵复杂度。
        self._conn: duckdb.DuckDBPyConnection | None = None
        #: **单连接 = 单线程用**。DuckDB 的连接不是线程安全的（它有自己的执行状态），
        #: 而工具循环会把同一批里的几次导出**并发**跑（v0.27）——两个线程同时用
        #: 同一个连接，轻则报错重则串了结果。所以这里串行化：读写都进这把锁。
        #:
        #: 用 ``RLock`` 而不是 ``Lock``：``read_rows`` 内部要调 ``columns`` →
        #: ``table_exists``，几个公开方法自己会互相调用，普通锁会**自己把自己锁死**。
        #: 表格副本是导出路径上的旁路数据，锁的争用远小于它旁边那次真正的 IO。
        self._lock = threading.RLock()

    def _connection(self) -> duckdb.DuckDBPyConnection:
        """取连接，第一次调用时才真正打开文件。"""
        if self._conn is None:
            self._conn = _duckdb().connect(str(self._path))
            logger.info("表格副本库就位：%s", self._path)
        return self._conn

    # ------------------------------------------------------------------ 写

    def write_table(
        self, *, table: str, columns: Sequence[str], rows: Sequence[Sequence[str]]
    ) -> int:
        _require_safe(table)
        if not columns:
            return 0

        with self._lock:
            # DROP + CREATE 而不是 CREATE OR REPLACE：后者在列数变化时报错，
            # 而"重跑时列变了"是正常情况（用户改了源文件）
            self._connection().execute(f'DROP TABLE IF EXISTS "{table}"')
            # 先建表再 INSERT，并显式声明 VARCHAR——避免 DuckDB 自己推断类型
            column_defs = ", ".join(f"{_quote(name)} VARCHAR" for name in columns)
            self._connection().execute(f'CREATE TABLE "{table}" ({_ORDINAL} BIGINT, {column_defs})')

            if rows:
                placeholders = ", ".join("?" for _ in range(len(columns) + 1))
                payload = [
                    [index, *[_text(cell) for cell in row[: len(columns)]]]
                    for index, row in enumerate(rows)
                ]
                self._connection().executemany(
                    f'INSERT INTO "{table}" VALUES ({placeholders})',  # noqa: S608
                    payload,
                )

        return len(rows)

    def drop_table(self, table: str) -> None:
        _require_safe(table)
        with self._lock:
            self._connection().execute(f'DROP TABLE IF EXISTS "{table}"')

    # ------------------------------------------------------------------ 读

    def table_exists(self, table: str) -> bool:
        _require_safe(table)
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table])
                .fetchone()
            )
        return row is not None

    def columns(self, table: str) -> list[str]:
        _require_safe(table)
        with self._lock:
            if not self.table_exists(table):
                return []
            described = self._connection().execute(f'DESCRIBE "{table}"').fetchall()
        # 第一列是内部行号，不属于用户看到的数据
        return [row[0] for row in described if row[0] != _ORDINAL]

    def row_count(self, table: str) -> int:
        _require_safe(table)
        with self._lock:
            if not self.table_exists(table):
                return 0
            row = (
                self._connection()
                .execute(
                    f'SELECT COUNT(*) FROM "{table}"'  # noqa: S608
                )
                .fetchone()
            )
        return int(row[0]) if row else 0

    def read_rows(self, table: str, *, limit: int = 50, offset: int = 0) -> list[list[str]]:
        _require_safe(table)
        with self._lock:
            names = self.columns(table)
            if not names:
                return []
            selected = ", ".join(_quote(name) for name in names)
            # **按内部行号排序**：DuckDB 不保证无 ORDER BY 的行序，
            # 而用户说的"第 3 行"必须与源文件一致
            rows = (
                self._connection()
                .execute(
                    f'SELECT {selected} FROM "{table}" ORDER BY {_ORDINAL} LIMIT ? OFFSET ?',  # noqa: S608
                    [limit, offset],
                )
                .fetchall()
            )
        return [[_text(cell) for cell in row] for row in rows]

    def list_tables(self) -> list[str]:
        with self._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'main' ORDER BY table_name"
                )
                .fetchall()
            )
        return [str(row[0]) for row in rows]

    def run_select(self, sql: str, *, max_rows: int) -> tuple[list[str], list[list[str]]]:
        """跑一条只读 SQL（校验在服务层，见协议里的说明）。

        两处刻意的做法：

        - **外面再包一层 LIMIT**：模型写的查询可能没有 LIMIT（"这个月一共多少"
          这类聚合本来也不需要），而"把整张表捞进内存再丢掉"是白付的代价。
          包一层还有第二个好处——它让 LIMIT 在 DuckDB 的流水线里能提前停下；
        - **列名从 ``description`` 取**：聚合列的名字是引擎算出来的
          （``sum(金额)`` / ``count(*)``），我们自己拼不出来。

        "有没有被截断"由调用方按"回来的是不是满的"判断（见 ``services/tabular``）——
        多取一条来探边界会让"最多回 N 行"这句话变成 N+1 行，那正是第一版踩的坑。
        """
        limit = max(1, int(max_rows))
        with self._lock:
            cursor = self._connection().execute(
                f"SELECT * FROM ({sql}) AS __kylab_q LIMIT {limit}"  # noqa: S608
            )
            columns = [item[0] for item in (cursor.description or ())]
            rows = cursor.fetchall()
        return columns, [[_text(cell) for cell in row] for row in rows]


    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        # 没打开过就不用关（延迟打开之后这是常态：多数进程从没碰过表格副本）
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def _quote(name: str) -> str:
    """包住列名。

    列名来自用户的表头，可能含空格、中文、甚至引号。双引号里把 ``"`` 转义成 ``""``
    是 SQL 标准做法——不转义的话，一个列名叫 ``a"b`` 的表会让整条 DDL 语法出错。
    """
    return '"' + name.replace('"', '""') + '"'


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _require_safe(table: str) -> None:
    """表名白名单校验。

    表名就是 ``document_id``，而它来自上游（用户上传时生成的）。
    即便目前它只可能是 ``doc_xxx``，这里仍然显式校验：
    **表名会进 SQL 文本，不能只依赖上游的格式**——那是把安全性
    押在"别处不会变"上。``SAFE_KEY_CHARS`` 与对象存储同一套字符集。
    """
    if not table or any(char not in SAFE_KEY_CHARS for char in table):
        raise ValueError(f"非法表名：{table!r}")
