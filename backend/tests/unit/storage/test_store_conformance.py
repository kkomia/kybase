"""另外三个仓储的接口一致性测试（M1 T1.7）。

一个接口的每个实现都在下面的清单里登记，漏登记就等于漏掉这条守卫。

行为验证在 ``tests/integration/storage/``，这里只守"接口是否被完整实现"。
新增接口方法时这里会立刻变红，提醒实现方补齐。
"""

import pytest

from app.storage.base import FullTextStore, MetaStore, ObjectStore, TabularStore, VectorStore
from app.storage.duckdb_impl.tabular_store import DuckDbTabularStore
from app.storage.postgres_impl.fulltext_store import PostgresFullTextStore
from app.storage.postgres_impl.meta_store import PostgresMetaStore
from app.storage.postgres_impl.vector_store import PostgresVectorStore
from app.storage.s3_impl.object_store import S3ObjectStore

#: 同一接口的每个实现都要在这里登记。新增实现忘了登记时，
#: "抽象方法有没有漏实现"这条守卫就会漏掉它——所以这份清单要跟着实现一起长。
#:
#: v0.1.0 瘦身后只剩这些：本机档那一整套（``RouterMetaStore`` / ``SqliteMetaStore``
#: 与三个 ``Unavailable*``）已随边车一起拆除，这一份服务只有服务器形态。
IMPLEMENTATIONS = (
    (PostgresMetaStore, MetaStore),
    (PostgresVectorStore, VectorStore),
    (PostgresFullTextStore, FullTextStore),
    (S3ObjectStore, ObjectStore),
    (DuckDbTabularStore, TabularStore),
)


@pytest.mark.parametrize(("implementation", "interface"), IMPLEMENTATIONS)
def test_implements_its_interface(implementation: type, interface: type) -> None:
    assert issubclass(implementation, interface)


@pytest.mark.parametrize(("implementation", "interface"), IMPLEMENTATIONS)
def test_no_abstract_methods_left(implementation: type, interface: type) -> None:
    assert implementation.__abstractmethods__ == frozenset()


def test_choose_implementation_by_interface_not_by_class() -> None:
    """组合根里应当只出现接口类型；实现类名不该泄漏到 services 层。"""
    from app.storage.base import StoreBundle

    annotations = StoreBundle.__annotations__
    assert annotations["meta"] is not None
    assert all(
        "_impl" not in str(annotation) for annotation in annotations.values()
    ), "StoreBundle 的字段类型不应是具体实现"
