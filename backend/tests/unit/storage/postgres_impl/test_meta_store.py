"""``PostgresMetaStore`` 的接口一致性测试。

镜像同构：``app/storage/postgres_impl/meta_store.py``
→ ``tests/unit/storage/postgres_impl/test_meta_store.py``。

这里只守"接口是否被完整实现"，行为验证在
``tests/integration/storage/test_meta_store.py``（那套用例双后端跑过）。
"""

import pytest

from app.storage.base import MetaStore
from app.storage.postgres_impl.meta_store import PostgresMetaStore


def test_implements_meta_store_interface() -> None:
    assert issubclass(PostgresMetaStore, MetaStore)


def test_no_abstract_methods_left() -> None:
    """接口新增方法后这里会立刻变红，提醒实现方补齐——而不是等到运行时报 TypeError。"""
    assert PostgresMetaStore.__abstractmethods__ == frozenset()


def test_is_instantiable_with_a_database(pg_stores) -> None:
    if pg_stores is None:
        pytest.skip("需要 KYLAB_TEST_DATABASE_URL 才能验证可实例化")
    assert PostgresMetaStore(pg_stores) is not None
