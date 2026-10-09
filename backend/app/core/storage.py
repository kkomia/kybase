"""存储装配（组合根）。

**这里是唯一允许 import 具体实现的地方**（测试夹具另算）。
`services/` 只依赖 `storage/base.py` 的接口，实现由本模块在启动时装配注入——
`scripts/check_layering.py` 的 `L2` 规则会守住这条边界。

**两个部署档，一个开关**（M2 §4.1，``KYLAB_DEPLOYMENT``）：

- **服务器档**（默认，一行行为都不变）：PostgreSQL（元数据 + 向量 + 全文）+
  对象存储 + DuckDB（表格副本）；``database_url`` 未配置时**直接启动失败**——
  留一条"没配就悄悄退回本地文件"的后路，只会让部署问题变成运行期怪现象。
- **本机档**（``KYLAB_DEPLOYMENT=local``，桌面壳的边车进程）：会话 / 消息 / 事件 /
  产物 / 笔记 / 设置 / 工作区 / 定时任务 / MCP / 模型注册 / 用量落 ``<data_dir>/kylab.db``
  （``sqlite_impl/``），向量 / 全文 / 表格三个仓储换成"不可用"实现，知识库那半的元数据
  读转给 ``split_impl/remote_meta.py`` 的 ``RemoteMetaStore``（知识库在 NAS 上，
  M3 阶段 4 已接；它的 reader 由服务层装配那一步后挂），对象存储固定本地目录。

三个存储各管一段（架构 §8）：

- **元数据**（``postgres_impl/`` 或 ``sqlite_impl/`` + ``split_impl/``）：本档的元数据家当。
- **对象存储**（``s3_impl/`` 或 ``local_impl/``）：原件、Markdown 产物、图片。
- **DuckDB**（``duckdb_impl/``）：表格型文档的结构化副本，与主库物理分离。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import Settings, get_settings
from app.storage.base import FullTextStore, MetaStore, ObjectStore, StoreBundle, VectorStore

if TYPE_CHECKING:  # 只为类型标注：**模块级不 import 具体后端**（见下面那段"为什么惰性"）
    from app.storage.postgres_impl.connection import Database as PgDatabase
    from app.storage.sqlite_impl.connection import Database as SqliteDatabase

__all__ = [
    "LOCAL_DB_NAME",
    "STORAGE_SUBDIRS",
    "build_stores",
    "close_stores",
    "get_stores",
    "reset_stores",
]

# ---------------------------------------------------------------- 为什么具体后端是**惰性导入**
#
# 这个模块是"**唯一允许 import 具体实现**"的地方（模块头那句话），而它自己会被
# **客户端那条链**（`app.sidecar` → `agent_tools` → … → `app.core.storage`）带着走 ✗。
# 模块级 import 三个后端，等于让客户端运行时凭空背上：
#
# - `postgres_impl` → **psycopg**（约 9.5 MB，含二进制扩展）
# - `s3_impl` → **boto3 + botocore**（约 21.8 MB）
# - `duckdb_impl` → **duckdb**（约 35.6 MB）
#
# 而按已定的裁定「**客户端永不直连 PG / S3 / DuckDB**」（客户端那条装配路径是
# `sidecar.Clients` / `LocalServices`），这些后端**客户端一个都用不到** ✗ ——
# 它们只是被导入链顺带拽进来的（与 `services/retrieval/coverage.py::_jieba` 同一类问题）。
#
# 所以：**按需导入** ✓ —— 只有真正要装配后端时（`build_stores`，服务器启动那条路 ✓）
# 才 import 它们。**行为不变**：`build_stores` 之前是"导入模块时"就会炸缺包，
# 现在是"调用 build_stores 时"炸 —— 而唯一调用点就是启动时的组合根 ✓（失败时机一样 ✓）。
#
# **本机档再加一条更硬的口径**（M2 §7 阶段 2 的完成判据）：`KYLAB_DEPLOYMENT=local` 那条
# 路上，上面这三份**一个都不许进 `sys.modules`**——它们一个都用不到，而客户端运行时
# 的体积是按闭包算的。所以分流发生在 import 之前（`_build_local_stores` 里只有标准库
# 与本地模块），并且 `tests/unit/storage/test_split_impl.py` 有一条**子进程断言**钉着它：
# 在干净解释器里跑一次本机档 `build_stores()`，然后查 `sys.modules` 里有没有它们。

ORIGINALS_DIR = "originals"
MARKDOWN_DIR = "markdown"
IMAGES_DIR = "images"
STORAGE_SUBDIRS = (ORIGINALS_DIR, MARKDOWN_DIR, IMAGES_DIR)
"""本地对象存储的目录规约。走 S3 时对象在桶里，不涉及这些目录。"""

LOCAL_DB_NAME = "kylab.db"
"""本机库文件名（``<data_dir>/kylab.db``，M2 §1.1）。"""

_S3_REQUIRED = ("s3_endpoint", "s3_access_key", "s3_secret_key")

_OPEN_DATABASES: list[PgDatabase] = []
"""已打开的 PG 连接池。进程级资源，关停时由 ``close_stores`` 统一释放。"""

_OPEN_LOCAL_DATABASES: list[SqliteDatabase] = []
"""已打开的本机 SQLite 句柄（服务器档下恒为空）。同样是进程级资源。"""


def _build_object_store(settings: Settings, data_dir: Path) -> ObjectStore:
    """按配置选对象存储：配了 S3 端点就走 S3，否则落到本地目录。

    这是个**可以独立于数据库切换**的组件：文件与元数据本来就是两套东西。

    配了端点但缺凭据 → 启动即失败。半配状态（有端点没钥匙）如果不能立刻报错，
    就会变成"上传时才发现"。
    """
    if not settings.s3_endpoint:
        # 惰性：本地实现没有第三方依赖，但同一条链上另一支是 S3（boto3+botocore 约 21.8 MB）
        from app.storage.local_impl.object_store import LocalObjectStore

        store = LocalObjectStore(data_dir)
        for subdir in STORAGE_SUBDIRS:
            (data_dir / subdir).mkdir(parents=True, exist_ok=True)
        return store

    missing = [name for name in _S3_REQUIRED if not getattr(settings, name)]
    if missing:
        raise RuntimeError(
            f"配置了 KYLAB_S3_ENDPOINT 但缺少 {missing}；"
            "要么补齐凭据，要么清空端点以使用本地文件系统"
        )

    from app.storage.s3_impl.object_store import S3ObjectStore, build_client

    client = build_client(
        endpoint=settings.s3_endpoint,
        access_key=settings.s3_access_key or "",
        secret_key=settings.s3_secret_key or "",
        region=settings.s3_region,
        secure=settings.s3_secure,
    )
    # 构造时校验桶可达（head_bucket）：配错了要在启动时炸
    return S3ObjectStore(client, bucket=settings.s3_bucket, prefix=settings.s3_prefix)


def _build_pg_stores(
    dsn: str, *, slow_query_ms: int
) -> tuple[MetaStore, VectorStore, FullTextStore]:
    """按 ``database_url`` 装配 PostgreSQL 的三个仓储。

    启动即校验：连不上、缺 pgvector、schema 版本不对，都在这里抛出去——
    失败要发生在启动时，而不是第一个请求或第一次上传。

    连接池要显式收（``close_stores``）：它是进程级资源，进程退出前不还回去，
    反复 build/reset（测试、配置热更）会把连接攒起来。
    """
    from app.storage.postgres_impl.connection import Database as PgDatabase
    from app.storage.postgres_impl.fulltext_store import PostgresFullTextStore
    from app.storage.postgres_impl.meta_store import PostgresMetaStore
    from app.storage.postgres_impl.schema import prepare as prepare_pg_schema
    from app.storage.postgres_impl.vector_store import PostgresVectorStore

    database = PgDatabase(dsn)
    try:
        database.open()
        prepare_pg_schema(database)
    except BaseException:
        database.close()
        raise
    _OPEN_DATABASES.append(database)
    return (
        PostgresMetaStore(database),
        PostgresVectorStore(database),
        PostgresFullTextStore(database, slow_query_ms=slow_query_ms),
    )


def _build_local_stores(settings: Settings, data_dir: Path) -> StoreBundle:
    """本机档：SQLite 元数据 + 三个"不可用" + 本地目录对象存储（M2 §4.1 那张表）。

    **这一支里一个服务器后端都不 import**：没有 psycopg / boto3 / duckdb（见上面那段
    "为什么惰性"）。所以它也不走 ``_build_object_store()``——那个函数会在配了
    ``KYLAB_S3_ENDPOINT`` 时 import boto3，而本机档的对象存储**固定**是数据目录
    （S3 是服务器的事；本机的"文件区"就该在用户自己的盘上，跟着库一起备份）。

    启动即校验的气质与服务器档一致：SQLite 版本不够（``STRICT`` 表要 ≥3.37）、
    schema 版本比应用新、库里有别人在写，都在这里说清楚（见 ``sqlite_impl/schema.py``）。

    **多出来的第六个字段 ``ledger``**（阶段 5）：导入台账（``imports`` / ``import_items``）
    是本机独有的，服务器档恒为 ``None``（那条纪律与理由写在 ``base.StoreBundle.ledger``）。
    **第七个字段 ``kb_cache``**（M4 阶段 1）同一条口径：知识库元数据快照（``kb_meta_cache``）
    也只有本机有，服务器档恒为 ``None``（理由写在 ``base.StoreBundle.kb_cache``）。
    **第八个字段 ``snapshot``**（M5 阶段 2）还是同一条口径：快照打包与读回只有本机有
    （服务器档的库就是它自己，没有"把自己打成一份便携的包"这条动作），服务器档恒为
    ``None``（理由写在 ``base.StoreBundle.snapshot``）。
    **第九个字段 ``backup_queue``**（M5 阶段 3）：待传队列（``backup_snapshots``）同样只有
    本机有（服务器档自己就是备份的目的地，没有"把一份快照排队传出去"这条动作），
    服务器档恒为 ``None``（理由写在 ``base.StoreBundle.backup_queue``）。
    **第十个字段 ``eraser``**（M5 阶段 6 收尾）：安全擦除（``secure_delete`` → ``VACUUM``
    → 收 WAL）擦的是**本机那个库文件**，服务器档的数据在 PG 里、也没有系统钥匙串那一整套
    动作，所以同样只有本机有、服务器档恒为 ``None``（理由写在 ``base.StoreBundle.eraser``）。
    """
    from app.storage.local_impl.object_store import LocalObjectStore
    from app.storage.split_impl import (
        RemoteMetaStore,
        RouterMetaStore,
        UnavailableFullTextStore,
        UnavailableTabularStore,
        UnavailableVectorStore,
    )
    from app.storage.sqlite_impl.connection import Database as SqliteDatabase
    from app.storage.sqlite_impl.meta_store import SqliteMetaStore
    from app.storage.sqlite_impl.schema import prepare as prepare_sqlite_schema

    db_path = Path(settings.local_db) if settings.local_db else data_dir / LOCAL_DB_NAME
    database = SqliteDatabase(db_path)
    try:
        prepare_sqlite_schema(database)
    except BaseException:
        database.close()
        raise
    _OPEN_LOCAL_DATABASES.append(database)

    for subdir in STORAGE_SUBDIRS:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)

    # **同一个实例六处用**（阶段 5 起两处，M4 再添一处，M5 再添三处）：``meta`` 走它做本机域
    # 读写，``ledger`` 走它做导入台账与"一条会话整体写入"，``kb_cache`` 走它做知识库元数据
    # 快照，``snapshot`` 走它做快照打包与读回，``backup_queue`` 走它做待传队列，``eraser``
    # 走它做安全擦除。六个 SqliteMetaStore 指向同一个库文件也能跑，但那会造出六条连接集合
    # 与六个对象——而"写锁是进程内一把"这条纪律是对着 `Database` 说的，不是对着仓储对象说的。
    # 同一个实例没有这个问题。
    local_store = SqliteMetaStore(database)

    return StoreBundle(
        # 本机域走 SQLite；KB 域的库元数据读转给 `RemoteMetaStore`（打 NAS 窄 API）。
        # 它**构造时不带 reader**：存储先于服务层装配，而 reader 要的是"能随设置改地址"
        # 的运行期能力（只有服务层有）——所以由 `core/services.py` 在装配期后挂一次
        # （那个类自己的说明写了完整理由）。没挂上之前调用它，抛的是
        # "知识库提供者还没接上（组合根未装配）"那句，不是 AttributeError。
        meta=RouterMetaStore(local=local_store, kb=RemoteMetaStore()),
        vectors=UnavailableVectorStore(),
        fulltext=UnavailableFullTextStore(),
        objects=LocalObjectStore(data_dir),
        tabular=UnavailableTabularStore(),
        # 导入台账是本机独有的（服务器档那个字段恒为 None，见 base.StoreBundle.ledger）
        ledger=local_store,
        # 知识库元数据快照同理（M4 §3.3）：缓存层服务 KB 域的读路径，但数据主人是本机，
        # 写者只有本机后端一个（M4 §2.3）。服务器档不缓存 KB 元数据——那是它自己的家当。
        kb_cache=local_store,
        # 快照打包与读回同理（M5 §2.3 / §2.4）：本机库里的明文凭据要靠它擦掉，
        # 服务器档没有这条动作（NAS 侧那一半是收包，见 api/v1/backup.py）。
        snapshot=local_store,
        # 待传队列同理（M5 §3.1）：断网入队、联网补传的那张表；服务器档自己就是备份的
        # 目的地，没有"排队往别处传"这条动作。
        backup_queue=local_store,
        # 安全擦除同理（M5 阶段 6 收尾）：擦的是**本机那个库文件**（主库 + -wal + -shm），
        # 服务器档的数据在 PG 里、也没有系统钥匙串那一整套动作（R14）。
        # 谁用它：`services/credentials.py` 清完明文之后的收尾那一下。
        eraser=local_store,
    )


def build_stores(settings: Settings | None = None) -> StoreBundle:
    """按配置建库、迁移、准备目录，并装配五个仓储。

    档位由 ``settings.deployment`` 定（见模块头的两个部署档），**幂等**：可安全地在
    每次启动时调用。

    返回 :class:`StoreBundle`（字段类型全为接口）。具体的连接句柄刻意不外泄——
    一旦交出去，调用方就会顺手拿它写 SQL，Repository 抽象就白做了。
    """
    resolved = settings or get_settings()
    # 两个真相源的兜底（正门在 ``Settings`` 的校验器上，这句是给绕过校验构造出来的
    # Settings 收口用的）：本机档宁可起不来，也不能把会话写进"另一个库"。
    if resolved.deployment == "local" and resolved.database_url:
        raise RuntimeError(
            "本机档（KYLAB_DEPLOYMENT=local）不接受 KYLAB_DATABASE_URL："
            "本机档的元数据落本机 SQLite，配了连接串等于同时声明了两个数据源"
        )

    data_dir = Path(resolved.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if resolved.deployment == "local":
        return _build_local_stores(resolved, data_dir)

    if not resolved.database_url:
        raise RuntimeError(
            "服务器档未配置 KYLAB_DATABASE_URL：元数据与向量/全文都在 PostgreSQL 上。示例："
            "postgresql://用户:口令@主机:5432/库名"
            "（桌面端本机档请设 KYLAB_DEPLOYMENT=local，会话落本机 SQLite）"
        )

    object_store = _build_object_store(resolved, data_dir)
    meta, vectors, fulltext = _build_pg_stores(
        resolved.database_url, slow_query_ms=resolved.slow_query_ms
    )

    # DuckDB（约 35.6 MB）同理惰性：只有真要用"表格型文档的结构化副本"时才 import
    from app.storage.duckdb_impl.tabular_store import DuckDbTabularStore

    return StoreBundle(
        meta=meta,
        vectors=vectors,
        fulltext=fulltext,
        objects=object_store,
        # 表格副本单独一个 DuckDB 文件：列式库适合按行列定位的查询，
        # 而且与主库物理分离，不会出现"扫一份大表把 API 拖慢"
        tabular=DuckDbTabularStore(data_dir / "tabular.duckdb"),
    )


@lru_cache
def get_stores() -> StoreBundle:
    """进程级单例，供依赖注入使用（与 ``get_settings`` 同构）。"""
    return build_stores()


def close_stores() -> None:
    """释放进程级存储资源（PG 连接池 / 本机 SQLite 句柄）。关停时调用。

    两边都收：档位是进程级的，而"反复 build/reset"（测试、配置热更）会把连接攒起来。
    SQLite 那边关库时还会做一次 WAL 检查点，少关一次就多留一份 ``-wal`` 尾巴。
    """
    for database in _OPEN_DATABASES:
        database.close()
    _OPEN_DATABASES.clear()
    for database in _OPEN_LOCAL_DATABASES:
        database.close()
    _OPEN_LOCAL_DATABASES.clear()


def reset_stores() -> None:
    """清掉单例缓存。测试与配置热更新时使用。"""
    close_stores()
    get_stores.cache_clear()
