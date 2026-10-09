"""向量分区的建表语句（不连数据库：用一个**记账的假连接**看它到底发了什么 SQL）。

镜像同构：``app/storage/postgres_impl/vector_store.py``
→ ``tests/unit/storage/postgres_impl/test_vector_store.py``。

为什么这条值得单列：维度 >2000 时**不发** `CREATE INDEX ... hnsw` 是一个"少了什么"
的判断，只在真库上验会很难看出"是没建索引还是建失败被吞了"。这里把 SQL 原样抓下来，
一条一条断言（真库那一侧另有 `tests/integration/storage/test_vector_store.py` 钉"能不能用"）。
"""

from __future__ import annotations

from typing import Any

from app.storage.postgres_impl.vector_store import HNSW_MAX_DIM, PostgresVectorStore


class _Result:
    """假查询结果：分区不存在（维度读不到）→ `fetchone()` 给 None。"""

    def fetchone(self) -> None:
        return None


class _Conn:
    """假连接：只记账，不执行。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> _Result:
        self.statements.append(" ".join(str(sql).split()))
        return _Result()


class _Session:
    """`Database.session()` 的替身：上下文里交出那一个假连接。"""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Conn:
        return self._conn

    def __exit__(self, *args: object) -> bool:
        return False


class _Database:
    def __init__(self) -> None:
        self.conn = _Conn()

    def session(self) -> _Session:
        return _Session(self.conn)

    def read(self) -> _Session:
        return _Session(self.conn)


def _store() -> tuple[PostgresVectorStore, _Conn]:
    database = _Database()
    return PostgresVectorStore(database), database.conn  # type: ignore[arg-type]


def _created_index(statements: list[str]) -> bool:
    return any("using hnsw" in item.lower() for item in statements)


def test_hnsw_index_is_created_at_and_below_the_limit() -> None:
    """≤2000 维照旧建 HNSW（既有行为一位不变），而且**没有**要告诉用户的话。"""
    for dim in (4, 1024, HNSW_MAX_DIM):
        store, conn = _store()

        advisory = store.ensure_partition("kb_1", dim=dim)

        assert advisory is None, dim
        assert _created_index(conn.statements), dim
        # 表与索引都按这个维度建
        assert any(f"vector({dim})" in item for item in conn.statements), dim


def test_above_the_limit_skips_the_index_and_says_so() -> None:
    """>2000 维：**不建索引**（建了会直接失败），并把代价如实说出来。

    实测原文（真库）：``column cannot have more than 2000 dimensions for hnsw index``。
    处理是"照建照用、只是精确检索"，所以这里既不许报错、也不许假装索引建好了。
    """
    store, conn = _store()

    advisory = store.ensure_partition("kb_1", dim=2048)

    assert not _created_index(conn.statements), "2048 维不该发 CREATE INDEX ... hnsw"
    assert any("vector(2048)" in item for item in conn.statements), "表本身还是要建"
    assert advisory is not None
    # 那句话必须说清三件事：为什么、代价是什么、怎么换回索引
    assert "2048" in advisory
    assert str(HNSW_MAX_DIM) in advisory
    assert "没有向量索引" in advisory
    assert "变慢" in advisory
    assert "1024" in advisory


def test_the_sentence_only_appears_for_dimensions_above_the_limit() -> None:
    """那句话**只在 >2000 维时**出现（不然它就成了噪声，用户会开始无视它）。"""
    store, _ = _store()
    assert store.ensure_partition("kb_ok", dim=HNSW_MAX_DIM) is None

    store, _ = _store()
    assert store.ensure_partition("kb_big", dim=HNSW_MAX_DIM + 1) is not None


def test_zero_dimension_is_still_rejected() -> None:
    store, _ = _store()

    try:
        store.ensure_partition("kb_1", dim=0)
    except ValueError as exc:
        assert "正整数" in str(exc)
    else:  # pragma: no cover - 没抛就是回归
        raise AssertionError("维度为 0 应当直接拒绝")
