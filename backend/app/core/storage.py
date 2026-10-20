"""存储装配（组合根）。

**这里是唯一允许 import 具体实现的地方**（测试夹具另算）。
`services/` 只依赖 `storage/base.py` 的接口，实现由本模块在启动时装配注入——
`scripts/check_layering.py` 的 `L2` 规则会守住这条边界。

形态只有一个：**PostgreSQL（元数据 + 向量 + 全文）+ 对象存储 + DuckDB（表格副本）**；
``database_url`` 未配置时**直接启动失败**——留一条"没配就悄悄退回本地文件"的后路，
只会让部署问题变成运行期怪现象。（原先的「本机档」及其 SQLite / 分流仓储已随
Agent 侧代码一起拆除，v0.1.0 瘦身。）

三个存储各管一段（架构 §8）：

- **元数据**（``postgres_impl/``）：知识库、文档、切块、Wiki、账号……
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

__all__ = [
    "STORAGE_SUBDIRS",
    "build_stores",
    "close_stores",
    "get_stores",
    "reset_stores",
]

# ---------------------------------------------------------------- 为什么具体后端是**惰性导入**
#
# 三条各带一个重依赖：
#
# - `postgres_impl` → **psycopg**（约 9.5 MB，含二进制扩展）
# - `s3_impl` → **boto3 + botocore**（约 21.8 MB）
# - `duckdb_impl` → **duckdb**（约 35.6 MB）
#
# 所以：**按需导入** √ —— 只有真正要装配后端时（`build_stores`，启动那条路 √）
# 才 import 它们。**行为不变**：之前是"导入模块时"就会炸缺包，现在是"调用
# build_stores 时"炸 —— 而唯一调用点就是启动时的组合根 √（失败时机一样 √）。

ORIGINALS_DIR = "originals"
MARKDOWN_DIR = "markdown"
IMAGES_DIR = "images"
STORAGE_SUBDIRS = (ORIGINALS_DIR, MARKDOWN_DIR, IMAGES_DIR)
"""本地对象存储的目录规约。走 S3 时对象在桶里，不涉及这些目录。"""

_S3_REQUIRED = ("s3_endpoint", "s3_access_key", "s3_secret_key")

_OPEN_DATABASES: list[PgDatabase] = []
"""已打开的 PG 连接池。进程级资源，关停时由 ``close_stores`` 统一释放。"""


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


def build_stores(settings: Settings | None = None) -> StoreBundle:
    """按配置建库、迁移、准备目录，并装配五个仓储。

    **幂等**：可安全地在每次启动时调用。

    返回 :class:`StoreBundle`（字段类型全为接口）。具体的连接句柄刻意不外泄——
    一旦交出去，调用方就会顺手拿它写 SQL，Repository 抽象就白做了。
    """
    resolved = settings or get_settings()
    data_dir = Path(resolved.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if not resolved.database_url:
        raise RuntimeError(
            "未配置 KYLAB_DATABASE_URL：元数据与向量/全文都在 PostgreSQL 上。示例："
            "postgresql://用户:口令@主机:5432/库名"
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
    """释放进程级存储资源（PG 连接池）。关停时调用。

    "反复 build/reset"（测试、配置热更）会把连接攒起来，收一次是必要的。
    """
    for database in _OPEN_DATABASES:
        database.close()
    _OPEN_DATABASES.clear()


def reset_stores() -> None:
    """清掉单例缓存。测试与配置热更新时使用。"""
    close_stores()
    get_stores.cache_clear()
