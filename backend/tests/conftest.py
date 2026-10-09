"""后端测试共享夹具。

工程规范 §5.2：集成测试用**独立临时的库**，禁止触碰开发库。v0.12 起存储是
PostgreSQL，所以测试库是临时建的 PG 数据库（见 ``pg_database``）；
设置 ``KYLAB_TEST_DATABASE_URL``（指向维护库，如 …/postgres）即启用。
**标了 ``local`` 的用例不受这条约束**（M2 阶段 0）：它们只依赖本机 SQLite 与
文件系统，没有 PG 也能跑——见下面 ``isolated_data_dir`` 那段说明。

各测试目录都放了 ``__init__.py``：工程规范 §5.1 要求测试文件与被测模块镜像同构，
于是 ``unit`` 与 ``integration`` 下会出现同名 ``test_<模块>.py``；
不加包的话 pytest 会因 basename 冲突而报 "import file mismatch"。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql as pgsql

from app.core.config import get_settings
from app.core.services import reset_services
from app.core.storage import reset_stores
from app.models.enums import DataSourceKind, DocumentStage
from app.services.model_registry import ModelRegistryService
from app.services.runtime_config import RuntimeConfigService
from app.storage.base import (
    DocumentRecord,
    FullTextStore,
    KnowledgeBaseRecord,
    MetaStore,
    StoreBundle,
    VectorStore,
)
from app.storage.duckdb_impl.tabular_store import DuckDbTabularStore
from app.storage.local_impl.object_store import LocalObjectStore
from app.storage.postgres_impl.fulltext_store import PostgresFullTextStore
from app.storage.postgres_impl.meta_store import PostgresMetaStore
from app.storage.postgres_impl.vector_store import PostgresVectorStore

DEFAULT_MODEL_ID = "BAAI/bge-m3"
DEFAULT_DIM = 1024

#: 提供它就打开"PG 后端的集成测试"。值是**维护库**的连接串（如 …/postgres），
#: 测试会用它建一个临时库、跑完删掉。指向带 pgvector 的 PG；建扩展需要超级用户。
PG_TEST_DSN_ENV = "KYLAB_TEST_DATABASE_URL"

#: 集成测试的管理员账号。**v0.11 起 /api/v1 一律要凭据**，所以每个 API 测试
#: 都要先走一遍产品上第一次打开的真实路径：setup 建管理员 → 拿会话令牌。
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "correct horse battery"


def login_admin(client) -> str:  # type: ignore[no-untyped-def]
    """首次初始化管理员并把会话令牌塞进 ``client.headers``，返回令牌。

    已经初始化过就退化成登录（同一个账号），所以对"每个测试一个临时库"与
    "同一个库跑多条用例"两种用法都成立。
    """
    from app.main import create_app  # noqa: F401  （保持与调用方一致的导入习惯）

    response = client.post(
        "/api/v1/auth/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "name": "管理员"},
    )
    if response.status_code == 409:
        # 已经建过管理员：改成登录
        response = client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


@contextmanager
def admin_client():  # type: ignore[no-untyped-def]
    """带管理员会话凭据的 TestClient（上下文管理器，跑完整 lifespan）。

    想测"没有凭据会怎样"的用例请直接用裸 ``TestClient(create_app())``——
    那正是它们要覆盖的分支。
    """
    from app.main import create_app

    with TestClient(create_app()) as client:
        login_admin(client)
        yield client


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch, pg_database, request):
    """全局兜底：任何测试都不许把运行期数据写进仓库或**真实开发库**。

    起因：`build_stores()` 默认用 ``./data``，一旦某个测试忘了指临时目录，
    就会在 `backend/data/` 建出运行时文件（被 .gitignore 挡住所以不易发现，
    但会污染本地状态、干扰后续手工验证）。

    **数据库同理**：跑 PG 测试时若不同时把应用的 ``KYLAB_DATABASE_URL`` 指到
    临时测试库，``create_app()`` 会连上真名那个库（通常就是开发库）并把测试数据
    写进去。所以这里把它改指临时库。

    **没有测试库就跳过——但 ``local`` 标记的用例不跳过**：v0.12 起存储只有
    PostgreSQL，没有测试库时静默退回别的实现只会掩盖"这套用例其实没跑"，所以宁可
    显式跳过（门禁脚本另有检查，缺 DSN 时不会让它悄悄变绿）。而 ``local`` 标记的用例
    只读配置、只拼装响应、不连任何库（如 `test_provider_handshake.py` 与
    `test_config.py`）——在缺 PG 的机器上跳过它们，等于把"协议与配置"那批断言整批跳过。
    所以这条宽限是**按标记**开的，不是把门整体放倒。

    同时关掉内嵌任务消费者：测试要手动驱动 worker，才能对时序下断言。
    """
    if pg_database is None and not request.node.get_closest_marker("local"):
        pytest.skip("需要 PostgreSQL 测试库：请设置 KYLAB_TEST_DATABASE_URL")

    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_RUN_WORKER", "false")
    if pg_database is None:
        # 标 local 的用例不得碰任何 PG：把引导级连接串压成空串（环境变量优先于 .env，
        # 与下面 S3 那三个同一手法），于是"误连开发库"在这条路上不可能发生。
        monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    else:
        monkeypatch.setenv("KYLAB_DATABASE_URL", pg_database.dsn)
        # 应用层用例（走 create_app）不经过 pg_stores 夹具，得在这里清一次库，
        # 否则上一个用例建的管理员/知识库会漏到下一个用例里
        _reset_database(pg_database)
    # **测试绝不碰真实对象存储**：本机若配了 KYLAB_S3_*（比如为了手工验证），
    # build_stores() 会真的往那个桶里写。这里一律清掉，需要对象存储的用例
    # 自己用 KYLAB_TEST_S3_* 显式构造（见 test_s3_object_store.py）。
    #
    # **必须 setenv 成空串，不能 delenv**：`Settings` 会读 `backend/.env` 文件，
    # 删掉环境变量之后同名值会从文件里"复活"（实测：本机 .env 配了 MinIO，
    # 于是走的是 S3 而不是本地实现，两个 lifespan 用例因此红）。环境变量优先于
    # .env，设成空串才真的把它压下去——库连接串用的是同一套办法（见上）。
    for name in ("KYLAB_S3_ENDPOINT", "KYLAB_S3_ACCESS_KEY", "KYLAB_S3_SECRET_KEY"):
        monkeypatch.setenv(name, "")
    # 测试要一条**不联网**的向量化链路：显式打开开发用确定性嵌入。
    # v0.8 起它不再是"没配就自动兜底"，必须有人主动开——测试就是那个"人"。
    monkeypatch.setenv("KYLAB_DEV_EMBEDDING", "true")
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    yield
    reset_services()
    reset_stores()
    get_settings.cache_clear()


# ---------------------------------------------------------------- PostgreSQL 测试库
#
# v0.12 起存储只有 PostgreSQL（SQLite 已退役），所以仓储夹具**必须**有测试库。
# 未设置 ``KYLAB_TEST_DATABASE_URL`` 时依赖仓储的用例会**跳过**（而不是静默退回
# 别的实现——那只会掩盖"这套用例其实没跑"）。


_RESET_SQL = """
do $$
declare
    r record;
    targets text;
begin
    -- 动态向量分区：只能 drop（TRUNCATE 清不掉"表"本身），索引随表一起走
    for r in
        select c.relname as name
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = current_schema()
           and c.relkind = 'r'
           and starts_with(c.relname, 'vec_')
    loop
        execute format('drop table if exists %I', r.name);
    end loop;

    -- 固定业务表：一条 TRUNCATE 全清；**保留版本表**，清掉它应用会认为
    -- 库比基线还旧（current_version 返回 0），于是每个用例都起不来
    select string_agg(format('%I', table_name), ', ') into targets
      from information_schema.tables
     where table_schema = current_schema()
       and table_type = 'BASE TABLE'
       and table_name <> 'schema_migrations';
    if targets is not null then
        execute 'truncate table ' || targets || ' restart identity cascade';
    end if;
end $$;
"""
"""清库用**一条**语句完成。

拆成"查分区表 / 查全部表 / TRUNCATE"三次往返在本地 PG 上无所谓，
但在跨网络的 PG 上（测试机连远端库）每次往返都是实打实的延迟，
乘以每个用例一次就变成了分钟级浪费——压成一次是纯赚。
"""


def _reset_database(db) -> None:  # type: ignore[no-untyped-def]
    """清空一个 PG 库，让用例之间互不影响。"""
    with db.session() as conn:
        conn.execute(_RESET_SQL)


@pytest.fixture(scope="session")
def pg_database() -> Iterator[object | None]:
    """一个**独立的临时 PG 库**（建好 schema），跑完删除；未配置则 None。

    用独立库而不是开发库：测试要清空整库，绝不能落到开发数据上。
    """
    dsn = os.environ.get(PG_TEST_DSN_ENV)
    if not dsn:
        yield None
        return

    from app.storage.postgres_impl.connection import Database as PgDatabase
    from app.storage.postgres_impl.schema import prepare

    name = f"kylab_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(pgsql.SQL("create database {}").format(pgsql.Identifier(name)))

    db = PgDatabase(psycopg.conninfo.make_conninfo(dsn, dbname=name))
    db.open()
    try:
        prepare(db)
        yield db
    finally:
        db.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                pgsql.SQL("drop database if exists {} with (force)").format(
                    pgsql.Identifier(name)
                )
            )


@pytest.fixture
def pg_stores(pg_database) -> Iterator[object | None]:
    """清空 PG 库（用例隔离）后把连接交出去；未配置 PG 时为 None。

    **只在这里清库一次**，各仓储夹具共用它。做成一堆"各自清库"的夹具会出问题：
    一个用例同时要几个仓储时，后构造的那个会把先建的数据清掉。
    """
    if pg_database is None:
        yield None
        return
    _reset_database(pg_database)
    yield pg_database


def _require_pg(pg_stores, what: str):  # type: ignore[no-untyped-def]
    if pg_stores is None:
        pytest.skip(f"{what} 需要 PostgreSQL 测试库：请设置 KYLAB_TEST_DATABASE_URL")
    return pg_stores


@pytest.fixture
def store(pg_stores) -> MetaStore:
    """元数据仓储（PG）。"""
    return PostgresMetaStore(_require_pg(pg_stores, "元数据仓储"))


@pytest.fixture
def vector_store(pg_stores) -> VectorStore:
    """向量仓储（pgvector）。"""
    return PostgresVectorStore(_require_pg(pg_stores, "向量仓储"))


@pytest.fixture
def fulltext_store(pg_stores) -> FullTextStore:
    """全文仓储（tsvector）。"""
    return PostgresFullTextStore(_require_pg(pg_stores, "全文仓储"))


@pytest.fixture
def object_store(tmp_path) -> LocalObjectStore:
    """对象存储根目录用临时目录，绝不写进仓库的 data/。"""
    return LocalObjectStore(tmp_path / "data")


@pytest.fixture
def tabular_store(tmp_path) -> DuckDbTabularStore:
    """表格副本库用临时文件，绝不写进仓库的 data/。"""
    return DuckDbTabularStore(tmp_path / "tabular.duckdb")


@pytest.fixture
def bundle(
    store: MetaStore,
    vector_store: VectorStore,
    fulltext_store: FullTextStore,
    object_store: LocalObjectStore,
    tabular_store: DuckDbTabularStore,
) -> StoreBundle:
    """五个仓储的装配（与组合根同构，但不碰磁盘上的开发库）。

    直接复用上面那几个夹具：这样"用哪个后端"只有一处决定，也不会出现
    同一用例里两个不同的库。
    """
    return StoreBundle(
        meta=store,
        vectors=vector_store,
        fulltext=fulltext_store,
        objects=object_store,
        tabular=tabular_store,
    )


@pytest.fixture
def runtime(bundle: StoreBundle) -> RuntimeConfigService:
    """运行期配置（行为参数）读写器。

    不传 Settings（``.env`` 引导值）——单测要的是"只有代码默认值"这个干净起点，
    需要测引导优先级时再显式构造带 Settings 的实例。

    **带上注册表**：与组合根同构（v0.8 起模型身份只从注册表取），
    这样用例可以直接用下面的 ``bind_slot`` 造出"模型已配好"的状态。
    """
    return RuntimeConfigService(bundle, registry=ModelRegistryService(bundle))


#: 用途 → 供应商类别。注册供应商时要选一个类别，测试里按用途推出来就够了。
_KIND_BY_SLOT = {"chat": "llm", "embedding": "embedding", "rerank": "rerank"}


def bind_model(
    registry: ModelRegistryService,
    slot: str,
    *,
    model_id: str,
    capabilities: list[str],
    dim: int | None = None,
    api_key: str = "sk-fake",
    base_url: str = "https://api.example.com/v1"
    ):  # type: ignore[no-untyped-def]
    """登记一个模型并绑到某个用途——测试里"模型已配好"的唯一入口。

    v0.8 起模型身份（地址 / 密钥 / 模型名 / 维度）只来自注册表，
    所以"配好了没"不能再靠往 ``app_settings`` 里写 ``llm.api_key`` 来伪造。
    """
    provider = registry.create_provider(
        kind=_KIND_BY_SLOT[slot], name="测试供应商", base_url=base_url, api_key=api_key
    )
    model = registry.register_model(
        provider_id=provider.id,
        model_id=model_id,
        dim=dim,
        capabilities=capabilities
    )
    registry.bind(slot, model.id)
    return model


@pytest.fixture
def bind_slot(bundle: StoreBundle):  # type: ignore[no-untyped-def]
    """``bind_model`` 的夹具形态：省掉每次自己造 ``ModelRegistryService``。"""

    def _bind(slot: str, **kwargs: object):  # type: ignore[no-untyped-def]
        return bind_model(ModelRegistryService(bundle), slot, **kwargs)  # type: ignore[arg-type]

    return _bind


@pytest.fixture
def kb(store: MetaStore) -> KnowledgeBaseRecord:
    return store.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_1", name="默认库", embedding_model_id=DEFAULT_MODEL_ID,
                            embedding_dim=DEFAULT_DIM)
    )


@pytest.fixture
def document(store: MetaStore, kb: KnowledgeBaseRecord) -> DocumentRecord:
    return store.create_document(
        DocumentRecord(
            id="doc_1",
            knowledge_base_id=kb.id,
            name="示例.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="hash-1",
            stage=DocumentStage.UPLOADED,
            size_bytes=128
    )
    )
