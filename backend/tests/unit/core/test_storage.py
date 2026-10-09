"""存储装配（组合根）测试。

镜像同构：``app/core/storage.py`` → ``tests/unit/core/test_storage.py``。

**服务器档**（默认）：装配会连库并校验 schema，所以这些用例需要
``KYLAB_TEST_DATABASE_URL``（未配置则跳过）。

**本机档**（M2 §4.1）：标了 ``local`` 的用例只碰 SQLite 与文件系统——它们要证明的
恰恰是"断 NAS 的机器上，本机后端照样装配得起来"（不标就是整批跳过，而那正是 M2
最该测的那条路）。
"""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.storage import (
    LOCAL_DB_NAME,
    STORAGE_SUBDIRS,
    build_stores,
    close_stores,
    get_stores,
    reset_stores,
)
from app.models.enums import DataSourceKind, DocumentStage
from app.storage.base import DocumentRecord, KnowledgeBaseRecord, StorageError
from app.storage.postgres_impl.connection import Database
from app.storage.postgres_impl.schema import MIGRATIONS, SCHEMA_VERSION, current_version
from app.storage.split_impl import (
    SEARCH_UNAVAILABLE_MESSAGE,
    UNBOUND_MESSAGE,
    KnowledgeBaseUnavailable,
    RemoteMetaStore,
    RouterMetaStore,
)
from app.storage.sqlite_impl.meta_store import SqliteMetaStore


def _query(settings: Settings, sql: str):  # type: ignore[no-untyped-def]
    """直连测试库取一个标量——这几条用例要验的正是装配本身，所以不走仓储。"""
    db = Database(settings.database_url or "")
    db.open()
    try:
        with db.read() as conn:
            row = conn.execute(sql).fetchone()
            return next(iter(row.values())) if row else None
    finally:
        db.close()


@pytest.fixture
def settings(pg_database, tmp_path) -> Settings:
    """指向临时测试库与临时数据目录，绝不碰开发库/仓库目录。"""
    if pg_database is None:
        pytest.skip("需要 PostgreSQL 测试库：请设置 KYLAB_TEST_DATABASE_URL")
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=pg_database.dsn,
    )


@pytest.fixture(autouse=True)
def _close_pools():
    """用例会直接调 build_stores，连接池得还回去。"""
    yield
    close_stores()


def test_missing_database_url_fails_with_a_useful_message(tmp_path) -> None:
    """没配连接串要**启动即失败**并说清填什么，而不是静默退回别的实现。"""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path / "data", database_url=None
    )
    with pytest.raises(RuntimeError, match="KYLAB_DATABASE_URL"):
        build_stores(settings)


def test_build_stores_creates_object_directories(settings: Settings) -> None:
    build_stores(settings)

    for subdir in STORAGE_SUBDIRS:
        assert (Path(settings.data_dir) / subdir).is_dir()


def test_build_stores_applies_baseline_schema(settings: Settings) -> None:
    stores = build_stores(settings)

    assert isinstance(stores.meta, object)
    db = Database(settings.database_url or "")
    db.open()
    try:
        assert current_version(db) == SCHEMA_VERSION
    finally:
        db.close()


def test_build_stores_is_idempotent(settings: Settings) -> None:
    """每次启动都会调用，必须能重复执行而不出错、不重复应用基线。"""
    build_stores(settings)
    close_stores()
    build_stores(settings)

    applied = _query(settings, "select count(*) as n from schema_migrations")
    # 基线一条 + 每条增量一条；幂等意味着**再启动一次不会多出来**
    assert applied == 1 + len(MIGRATIONS), "基线与增量各只应记录一次"


def test_stores_are_functionally_wired(settings: Settings) -> None:
    """四个仓储确实能协同工作（元数据 + 向量 + 全文 + 对象）。"""
    stores = build_stores(settings)
    stores.meta.create_knowledge_base(
        KnowledgeBaseRecord(
            id="kb_1", name="库", embedding_model_id="BAAI/bge-m3", embedding_dim=4
        )
    )
    stores.meta.create_document(
        DocumentRecord(
            id="doc_1",
            knowledge_base_id="kb_1",
            name="a.md",
            source_kind=DataSourceKind.UPLOAD,
            content_hash="h1",
            stage=DocumentStage.UPLOADED,
        )
    )
    stores.vectors.ensure_partition("kb_1", dim=4)
    stores.vectors.upsert_vectors("kb_1", items=[("c1", [1.0, 0.0, 0.0, 0.0])])
    path = stores.objects.write("markdown/doc_1.md", "# 标题".encode())

    assert stores.meta.get_knowledge_base("kb_1") is not None
    assert stores.vectors.declared_dim("kb_1") == 4
    assert stores.objects.read(path) == "# 标题".encode()


def test_get_stores_is_cached_and_resettable(monkeypatch, pg_database) -> None:
    if pg_database is None:
        pytest.skip("需要 PostgreSQL 测试库：请设置 KYLAB_TEST_DATABASE_URL")
    monkeypatch.setenv("KYLAB_DATABASE_URL", pg_database.dsn)
    reset_stores()
    try:
        first = get_stores()
        assert get_stores() is first  # 进程级单例：两次取到同一份装配

        reset_stores()
        assert get_stores() is not first  # 重置后重新装配（必须持有引用才能比较）
    finally:
        reset_stores()


# ------------------------------------------------------------------ 本机档（M2 §4.1）


def _local_settings(tmp_path: Path, **overrides) -> Settings:  # type: ignore[no-untyped-def]
    """本机档设置：``_env_file=None`` 保证开发机的 ``.env`` 不会漏进来。"""
    return Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=tmp_path / "data", **overrides
    )


@pytest.mark.local
def test_local_deployment_fills_every_field(tmp_path: Path) -> None:
    """§7 阶段 2 的完成判据：本机档下 bundle 五个字段齐备，元数据真的落在本机库里。

    "齐备"要经得起用：所以这里不只断言"对象不是 None"，还真的写进再读出来——
    一个装配成功但一调就炸的 bundle，与没有装配是一回事。
    """
    settings = _local_settings(tmp_path)
    stores = build_stores(settings)

    assert isinstance(stores.meta, RouterMetaStore)
    assert isinstance(stores.meta.local, SqliteMetaStore)
    # KB 侧 M3 阶段 4 起是"真实现"那个类（reader 由服务层后挂，见下面那条用例）
    assert isinstance(stores.meta.kb, RemoteMetaStore)
    assert all(
        getattr(stores, field) is not None
        for field in ("vectors", "fulltext", "objects", "tabular")
    )
    # 导入台账（阶段 5）与知识库快照（M4）都走**同一个实例**——同一个 `Database`，
    # 也就是"写锁是进程内一把"那条纪律站得住的地方（见 `_build_local_stores` 的说明）；
    # 服务器档这两个字段恒为 None，所以这里只在本机档断言
    assert stores.ledger is stores.meta.local
    assert stores.kb_cache is stores.meta.local

    # 本机域走通了：写一个设置再读回来（经 router，与 services 走的是同一条路）
    stores.meta.set_setting("chat.mode", "build")
    assert stores.meta.get_setting("chat.mode") == "build"

    data_dir = Path(settings.data_dir)
    assert (data_dir / LOCAL_DB_NAME).is_file()
    for subdir in STORAGE_SUBDIRS:
        assert (data_dir / subdir).is_dir()


@pytest.mark.local
def test_local_deployment_puts_the_database_where_it_is_told(tmp_path: Path) -> None:
    """``KYLAB_LOCAL_DB`` 指到别处时库就落别处（默认才是 ``<data_dir>/kylab.db``）。"""
    target = tmp_path / "elsewhere" / "kylab.db"
    settings = _local_settings(tmp_path, local_db=target)
    build_stores(settings)

    assert target.is_file()
    assert not (Path(settings.data_dir) / LOCAL_DB_NAME).exists()


@pytest.mark.local
def test_local_deployment_keeps_files_on_this_machine(tmp_path: Path) -> None:
    """本机档的对象存储**固定**是数据目录：配了 S3 也不走（S3 是服务器的事）。

    这条是有意为之的取舍（§4.1 那张表），所以要有用例写着"本机档就是不动 S3 配置"——
    否则某天有人顺手把 ``_build_object_store`` 接进来，谁也不会注意到本机档开始
    往一个它不该够到的桶里写文件了。
    """
    settings = _local_settings(
        tmp_path, s3_endpoint="http://minio:9000", s3_access_key="k", s3_secret_key="s"
    )
    stores = build_stores(settings)

    path = stores.objects.write("markdown/doc_1.md", "# 标题".encode())
    assert (Path(settings.data_dir) / path).is_file()


@pytest.mark.local
def test_local_deployment_reports_the_kb_domain_as_unavailable(tmp_path: Path) -> None:
    """本机档那两个"没有这个能力"的口径，**逐句**钉住（503 那句话的来源）。

    两句话不一样，也不该一样：

    - **检索**（``vectors`` / ``fulltext`` / ``tabular``）是 §2.2 的结论——**检索留在
      本机之外**，本机没有向量与全文索引，别把它改成"经 NAS 的半吊子"（那句话里
      写着"检索在 NAS 知识库"）；
    - **KB 侧的库元数据读**由 ``RemoteMetaStore`` 承担，而它要的 reader 是**服务层
      装配时后挂**的（阶段 4）：只调 ``build_stores`` 时它还没接上，给的是"知识库提供者
      还没接上（组合根未装配）"——不是 ``AttributeError``，更不是空结果。
    """
    stores = build_stores(_local_settings(tmp_path))

    with pytest.raises(KnowledgeBaseUnavailable) as kb_error:
        stores.meta.list_knowledge_bases()
    assert str(kb_error.value) == UNBOUND_MESSAGE

    with pytest.raises(KnowledgeBaseUnavailable):
        stores.vectors.list_partitions()
    with pytest.raises(KnowledgeBaseUnavailable) as search_error:
        stores.fulltext.search(query="会话", top_k=3)
    assert str(search_error.value) == SEARCH_UNAVAILABLE_MESSAGE
    with pytest.raises(KnowledgeBaseUnavailable):
        stores.tabular.list_tables()

    assert issubclass(KnowledgeBaseUnavailable, StorageError)


@pytest.mark.local
def test_local_deployment_refuses_database_url_even_when_validation_is_bypassed(
    tmp_path: Path,
) -> None:
    """正门在 ``Settings`` 的校验器上（见 ``test_config.py``）；这条守绕过去的那条路。

    两个真相源同时在场时"会话写哪儿"取决于哪段代码先读哪个字段，而失败形态是
    **数据被写进另一个库**——宁可起不来。
    """
    settings = _local_settings(tmp_path)
    settings.database_url = "postgresql://kylab:secret@nas:5432/kylab"  # 绕过校验器

    with pytest.raises(RuntimeError, match="KYLAB_DATABASE_URL"):
        build_stores(settings)


@pytest.mark.local
def test_get_stores_is_cached_in_local_deployment(monkeypatch, tmp_path: Path) -> None:
    """本机档同样走 ``lru_cache`` 单例：档位只能在进程启动最早定下（§4.1）。"""
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    reset_stores()
    try:
        first = get_stores()
        assert isinstance(first.meta, RouterMetaStore)
        assert get_stores() is first

        reset_stores()
        assert get_stores() is not first
    finally:
        reset_stores()
