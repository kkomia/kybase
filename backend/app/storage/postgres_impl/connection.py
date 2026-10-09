"""PostgreSQL 连接管理（v0.12 存储切换）。

与 ``sqlite_impl/connection.py`` 的对应关系，以及**有意的差异**：

- ``read()`` / ``session()`` 语义保持一致，``meta_store`` 等方法体可以近乎逐行平移
  （占位符从 ``?`` 换成 ``%s`` 是唯一的机械改动）。
- 连接来自**连接池**：SQLite 那版是"每次操作新建、用完即关"，那在 PG 上不可行
  ——建连接要握手/鉴权，成本高一个量级。
- 事务交给 psycopg3 的 ``conn.transaction()``：正常退出提交、异常回滚，
  比手写 ``BEGIN/COMMIT/ROLLBACK`` 更不容易漏分支。
- 去掉了 WAL / busy_timeout / foreign_keys 三件 SQLite 专属的事：
  并发由 MVCC 承担，外键在 PG 里**始终强制**（不再是要靠 PRAGMA 打开的开关）。

**为什么仍然是同步的**：现有 ``services/`` 全部是同步代码（SQLite 那版在 async 路由里
也是直接同步调用）。保持同步意味着切换存储只改 ``storage/`` 一层；
换成 async 驱动会把改动面扩散到每个 service 与 API 路由，
那是本次迁移不必要承担的风险。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 只在注解里用到（``Iterator[Connection]`` ✓）：`from __future__ import annotations`
    # 之下注解不求值 ✓，所以这一句**不进运行时**、也就不进客户端的导入闭包 ✓。
    from psycopg import Connection

#: 惰性拿到的 (ConnectionPool, dict_row)（**故意不是模块级导入** ✗）。
_POOL_PIECES: tuple[Any, Any] | None = None


def _pool_pieces() -> tuple[Any, Any]:
    """第一次**建连接池**时才导入 psycopg / psycopg_pool（模块级导入会多背一份 ✗）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 ✓，而"连 PostgreSQL"只有
    **服务器**那条链会做 ✓ —— 边车（客户端运行时）只跑循环 + 工具 + 沙箱 ✓，
    它的会话权威与库都在**远端**（走 HTTP ✓，不走 psycopg）。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，psycopg 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。
    """
    global _POOL_PIECES
    if _POOL_PIECES is None:
        from psycopg.rows import dict_row as _dict_row
        from psycopg_pool import ConnectionPool as _ConnectionPool

        _POOL_PIECES = (_ConnectionPool, _dict_row)
    return _POOL_PIECES

DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 20
"""连接池上限。

**为什么是 20 而不是 10**：协议层改成同步端点之后，Starlette 用 40 个线程跑它们
（见 scripts/check_layering.py 的 A1 规则），也就是说并发请求最多能有 40 个同时在
向存储要连接。池子只有 10 时，多出来的线程只是从"等事件循环"换成"等池子"——
那仍然是串行，只是换了个地方排队。20 是折中：够覆盖典型并发，又不至于把 PG 的
``max_connections``（默认 100）吃光，毕竟还要给 worker、psql、备份留位置。
"""
DEFAULT_OPEN_TIMEOUT_S = 10.0


class Database:
    """连接池句柄：与 SQLite 版同名，但底层是池而不是"每次新建"。

    生命周期是显式的——``open()`` 在装配时校验连通性（失败即启动失败，
    而不是等到第一个请求），``close()`` 在关停时释放。
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = DEFAULT_POOL_MIN,
        max_size: int = DEFAULT_POOL_MAX,
    ) -> None:
        self._dsn = dsn
        ConnectionPool, dict_row = _pool_pieces()
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            # 行以 dict 形式返回：现存储层到处是 row["列名"]，dict 比元组少一层心智负担
            kwargs={"row_factory": dict_row},
            open=False,
        )

    @property
    def dsn(self) -> str:
        """连接串。**脱敏后**才可用于日志——生产环境日志里不该出现口令。"""
        return self._dsn

    def open(self, *, timeout: float = DEFAULT_OPEN_TIMEOUT_S) -> None:
        """打开连接池并**等到真的连上**。

        ``wait=True`` 是关键：它让"库连不上"在启动时就炸出来，
        而不是变成一个所有请求都 500 的、看起来像业务 bug 的问题。
        """
        self._pool.open(wait=True, timeout=timeout)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def read(self) -> Iterator[Connection]:
        """只读场景：借用连接，退出即归还。"""
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def session(self) -> Iterator[Connection]:
        """在显式事务中执行一段操作；异常则回滚。

        对应 SQLite 版的 ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``：
        "状态推进 + 任务入队"这类多写操作要么全成要么全不成。
        """
        with self._pool.connection() as conn, conn.transaction():
            yield conn
