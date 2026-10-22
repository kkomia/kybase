"""存储装配（组合根）测试。

镜像同构：``app/core/storage.py`` → ``tests/unit/core/test_storage.py``。

装配会连库并校验 schema，所以这些用例需要 ``KYLAB_TEST_DATABASE_URL``
（未配置则跳过）。（原先那些标 ``local`` 的旧版用例已随旧版客户端一起拆除；
``local`` 标记本身还留着——它是分层规则里的一个档，别的用例仍在用。）
"""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.storage import (
    STORAGE_SUBDIRS,
    build_stores,
    close_stores,
    get_stores,
    reset_stores,
)
from app.models.enums import DataSourceKind, DocumentStage
from app.storage.base import DocumentRecord, KnowledgeBaseRecord
from app.storage.postgres_impl.connection import Database
from app.storage.postgres_impl.schema import MIGRATIONS, SCHEMA_VERSION, current_version


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


# ------------------------------------------------------------------ 旧版（M2 4.1）

