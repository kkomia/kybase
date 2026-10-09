"""另外三个仓储的接口一致性测试（M1 T1.7）。

一个接口的每个实现都在下面的清单里登记，漏登记就等于漏掉这条守卫。

行为验证在 ``tests/integration/storage/``，这里只守"接口是否被完整实现"。
新增接口方法时这里会立刻变红，提醒实现方补齐。
"""

import pytest

from app.storage.base import FullTextStore, MetaStore, ObjectStore, TabularStore, VectorStore
from app.storage.postgres_impl.fulltext_store import PostgresFullTextStore
from app.storage.postgres_impl.vector_store import PostgresVectorStore
from app.storage.s3_impl.object_store import S3ObjectStore
from app.storage.split_impl import (
    RouterMetaStore,
    UnavailableFullTextStore,
    UnavailableTabularStore,
    UnavailableVectorStore,
)

#: 同一接口的每个实现都要在这里登记。新增实现忘了登记时，
#: "抽象方法有没有漏实现"这条守卫就会漏掉它——所以这份清单要跟着实现一起长。
#:
#: M2 阶段 2 登记了四个：``RouterMetaStore`` 是 `MetaStore` 的**分档**实现（本机域走
#: SQLite、KB 域转给 KB 侧，见 ``app/storage/split_impl/router.py``）；三个
#: ``Unavailable*`` 是本机档里"这个能力在 NAS 上"那一半的完整实现（每个方法都抛）。
#: ``UnavailableMetaStore`` / ``SqliteMetaStore`` / ``RemoteMetaStore``（M3 阶段 4）
#: **不在这个清单里**：它们各自只覆盖 MetaStore 的一半（KB 域 / 本机域），不是完整的接口
#: 实现——"是否完整"由 ``tests/unit/storage/test_split_impl.py`` 逐名核对。
IMPLEMENTATIONS = (
    (PostgresVectorStore, VectorStore),
    (PostgresFullTextStore, FullTextStore),
    (S3ObjectStore, ObjectStore),
    (RouterMetaStore, MetaStore),
    (UnavailableVectorStore, VectorStore),
    (UnavailableFullTextStore, FullTextStore),
    (UnavailableTabularStore, TabularStore),
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
