"""``kb_meta_cache``（M4 阶段 1）的迁移、键空间、上限与超龄用例。

镜像同构：``app/storage/sqlite_impl/meta_store.py`` 的快照那一段
→ ``tests/unit/storage/sqlite_impl/test_kb_cache.py``。

这一组用例答的是阶段 1 的四件事（方案 §7 阶段 1 的完成判据）：

1. **新库落到 v2、v1 老库能升且迁移前有整库备份**（§3.2，风险 R7）；
2. **键空间**：两个地址下同 id 的库互不覆盖、两个库的文档列表视图互不串（R2）；
3. **三级上限 + LRU**：单条 / 每地址行数 / 全局总量，淘汰一律按 ``fetched_at``（R4）；
4. **超龄当没有、坏 payload 拒、purge 三档粒度、stats 报数**（§3.4）。

**不在这里测的**（阶段 2 起的事）：谁去 NAS 取、什么时候该再验证、payload 进缓存前要剥
哪些字段——那些是 ``services/kb_cache.py`` 的判据。这个文件只管"本机这张表怎么存取"。

时间戳一律取**相对于现在**（``_ago()``）而不是写死的日历时间：超龄那条判据问的正是
"离现在多久"，写死日历会让这组用例随日子过去慢慢变红；真要在"恰好 30 天"这种边界上
判，就把时钟冻住（``monkeypatch`` 掉 ``meta_store._now``，见下面两条）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.storage.base import (
    MAX_PAYLOAD_BYTES,
    MAX_ROWS_PER_PROVIDER,
    MAX_TOTAL_BYTES,
    SNAPSHOT_MAX_AGE_SECONDS,
    KbMetaCache,
    KbMetaCacheRecord,
)
from app.storage.sqlite_impl import meta_store as meta_store_module
from app.storage.sqlite_impl.connection import Database
from app.storage.sqlite_impl.meta_store import SqliteMetaStore
from app.storage.sqlite_impl.schema import (
    BASELINE_VERSION,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    current_version,
    ensure_schema,
    prepare,
)
from app.storage.sqlite_impl.validate import main as validate_main

pytestmark = pytest.mark.local

#: 一台"家里的 NAS"（归一化后的 base_url）。另一台见 ``OFFICE``。
HOME = "https://nas-home.local/kylab/api/v1"
OFFICE = "https://nas-office.local/kylab/api/v1"


def _ago(**kwargs: float) -> datetime:
    """相对现在往前推一点，并且**抹掉微秒**。

    抹微秒不是洁癖：库里的时间戳只到毫秒（列名 ``_ms``），写进去再读回来必然与
    "带微秒的那个 datetime" 不等——用例要比的是**存下来的那个值**，所以生成时对齐。
    """
    return (datetime.now(UTC) - timedelta(**kwargs)).replace(microsecond=0)


def _record(**overrides: object) -> KbMetaCacheRecord:
    """一条可以只改几个字段的快照记录（默认是一份"刚看到的库详情"）。"""
    values: dict[str, object] = {
        "provider": HOME,
        "resource": "kb_detail",
        "scope_key": "kb_1",
        "payload": '{"id": "kb_1", "name": "资料库"}',
        "version": "sha256:aaaa",
        "source": "revalidate",
        "fetched_at": _ago(minutes=5),
        "checked_at": _ago(minutes=5),
    }
    values.update(overrides)
    return KbMetaCacheRecord(**values)  # type: ignore[arg-type]


def _raw_rows(database: Database) -> list[sqlite3.Row]:
    """直接读表（绕开仓储）：验"行真被删了/真没写进去"这类判据时只能看库。"""
    with database.read() as conn:
        return conn.execute("SELECT * FROM kb_meta_cache").fetchall()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "kylab.db")
    prepare(db)
    yield db
    db.close()


@pytest.fixture
def store(database: Database) -> SqliteMetaStore:
    return SqliteMetaStore(database)


# ------------------------------------------------------------------ 迁移与基线


def test_the_store_satisfies_the_kb_meta_cache_protocol(store: SqliteMetaStore) -> None:
    """装上去的那个实现真的满足协议（``runtime_checkable`` 的意义就在这一句）。"""
    assert isinstance(store, KbMetaCache)


def test_a_fresh_library_lands_on_v2_with_the_cache_table(database: Database) -> None:
    """空库 → 建 v1 基线 → 立刻迁到 v2（真机首次启动走的就是这条路）。

    真机桌面目录下**还没有** ``kylab.db``（方案 §3.2 核过），所以"建 v1 + 即迁 v2"
    是第一条真实路径，它与"v1 老库升级"必须落到同一个结构上。
    """
    assert current_version(database) == SCHEMA_VERSION
    with database.read() as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'kb_meta_cache'"
        ).fetchone()["sql"]
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'kb_meta_cache'"
            )
            # 三列主键由 `sqlite_autoindex_kb_meta_cache_1` 兜着（STRICT 表上的复合主键
            # 就是这么实现的），这里只核**我们自己建的**那两条。
            if not row["name"].startswith("sqlite_autoindex")
        }
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(kb_meta_cache)")]
        primary_key = [
            row["name"] for row in conn.execute("PRAGMA table_info(kb_meta_cache)") if row["pk"]
        ]
    assert "STRICT" in ddl.upper()
    assert indexes == {"idx_kb_meta_cache_prune", "idx_kb_meta_cache_scope"}
    # **三列主键就是键空间**：地址隔离靠 provider，资源内隔离靠 scope_key
    assert primary_key == ["provider", "resource", "scope_key"]
    assert columns == [
        "provider",
        "resource",
        "scope_key",
        "payload",
        "version",
        "etag",
        "last_modified",
        "source",
        "identity",
        "fetched_at_ms",
        "checked_at_ms",
        "stale",
        "last_error",
    ]


def test_a_v1_library_upgrades_with_a_backup(tmp_path: Path) -> None:
    """v1 老库升级：只加表、旧行一条不少、迁移前的整库备份在（§3.2 / R7）。

    造 v1 库的方式是**直接跑那份冻结的基线 DDL**（``ensure_schema`` 会顺手迁到 v2，
    而这里要的正是"迁移之前"那一刻）：用户机器上那份库就长这样。
    """
    db = Database(tmp_path / "kylab.db")
    db.open()
    db.script(SCHEMA_PATH.read_text(encoding="utf-8"))
    with db.session() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, kb_ids, pinned, context_summary,"
            " created_at_ms, updated_at_ms) VALUES ('c_old', '升级前的会话', '[]', 0, '', 1, 1)"
        )
    assert current_version(db) == BASELINE_VERSION
    with db.read() as conn:
        assert "kb_meta_cache" not in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert ensure_schema(db) == SCHEMA_VERSION

    # 新表在、旧行一条不少（本机库装的是用户自己的会话，升级不许丢）
    with db.read() as conn:
        assert "kb_meta_cache" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert (
            conn.execute("SELECT title FROM conversations WHERE id = 'c_old'").fetchone()["title"]
            == "升级前的会话"
        )

    # 迁移前的整库备份在，而且它是**迁移之前**那一份（没有缓存表、版本还是 v1）
    backups = sorted(
        (tmp_path / "migration-backup").glob(f"*-v{BASELINE_VERSION}→v{SCHEMA_VERSION}")
    )
    assert len(backups) == 1, f"迁移前备份不见了：{backups}"
    snapshot = backups[0] / "kylab.db"
    assert snapshot.is_file()
    with sqlite3.connect(snapshot) as copy:
        names = {row[0] for row in copy.execute("SELECT name FROM sqlite_master")}
        recorded = copy.execute(
            "SELECT value FROM schema_metadata WHERE key = 'version'"
        ).fetchone()[0]
        kept = copy.execute("SELECT title FROM conversations WHERE id = 'c_old'").fetchone()[0]
    assert "kb_meta_cache" not in names
    assert recorded == str(BASELINE_VERSION)
    assert kept == "升级前的会话"
    db.close()


# ------------------------------------------------------------------ 键空间（R2）


def test_two_addresses_do_not_overwrite_each_other(store: SqliteMetaStore) -> None:
    """同一台机器可能配了"家里的 NAS / 单位的 NAS"：**按地址隔离，互不覆盖**（§5）。"""
    store.put_kb_meta_cache(_record(provider=HOME, payload='{"name": "家里的库"}'))
    store.put_kb_meta_cache(_record(provider=OFFICE, payload='{"name": "单位的库"}'))

    assert store.kb_meta_cache_stats().rows == 2
    home = store.get_kb_meta_cache(HOME, "kb_detail", "kb_1")
    office = store.get_kb_meta_cache(OFFICE, "kb_detail", "kb_1")
    assert home is not None and home.payload == '{"name": "家里的库"}'
    assert office is not None and office.payload == '{"name": "单位的库"}'


def test_two_libraries_document_lists_do_not_mix(store: SqliteMetaStore) -> None:
    """文档列表按 ``<kb_id>|<视图指纹>`` 分键：不同库、不同页都是**不同的行**。"""
    root_page_1 = "kb_1|folder:root|page:1|size:20"
    root_page_2 = "kb_1|folder:root|page:2|size:20"
    other_kb = "kb_2|folder:root|page:1|size:20"
    store.put_kb_meta_cache(
        _record(resource="doc_list", scope_key=root_page_1, payload='{"items": ["a"]}')
    )
    store.put_kb_meta_cache(
        _record(resource="doc_list", scope_key=other_kb, payload='{"items": ["b"]}')
    )
    store.put_kb_meta_cache(
        _record(resource="doc_list", scope_key=root_page_2, payload='{"items": ["c"]}')
    )

    assert store.kb_meta_cache_stats().rows == 3
    page_1 = store.get_kb_meta_cache(HOME, "doc_list", root_page_1)
    assert page_1 is not None and page_1.payload == '{"items": ["a"]}'
    second_kb = store.get_kb_meta_cache(HOME, "doc_list", other_kb)
    assert second_kb is not None and second_kb.payload == '{"items": ["b"]}'


def test_the_same_key_is_an_upsert_not_a_second_row(store: SqliteMetaStore) -> None:
    """同一个键再写一次是**覆盖**（三列主键），不是又落一行。"""
    store.put_kb_meta_cache(
        _record(payload='{"name": "旧"}', version="sha256:old", identity="api_key")
    )
    store.put_kb_meta_cache(
        _record(payload='{"name": "新"}', version="sha256:new", identity="session")
    )

    rows = store.kb_meta_cache_stats()
    assert rows.rows == 1
    row = store.get_kb_meta_cache(HOME, "kb_detail", "kb_1")
    assert row is not None
    assert row.payload == '{"name": "新"}' and row.version == "sha256:new"
    assert row.identity == "session"


# ------------------------------------------------------------------ 只推确认 / 删行 / 报数


def test_touch_only_advances_the_confirmation(store: SqliteMetaStore) -> None:
    """``touch`` 只动 ``checked_at`` / ``stale`` / ``last_error``——**两个时间戳不混**。"""
    fetched = _ago(hours=2)
    store.put_kb_meta_cache(
        _record(payload='{"name": "没变的库"}', fetched_at=fetched, checked_at=fetched)
    )

    confirmed = _ago(minutes=1)
    assert (
        store.touch_kb_meta_cache(
            HOME, "kb_detail", "kb_1", checked_at=confirmed, stale=True, last_error="连不上"
        )
        is True
    )
    row = store.get_kb_meta_cache(HOME, "kb_detail", "kb_1")
    assert row is not None
    assert row.checked_at == confirmed and row.stale is True
    assert row.last_error == "连不上"
    # payload / version / fetched_at 一个字都没动：这一步的语义是"确认过，还是那份"
    assert row.payload == '{"name": "没变的库"}' and row.version == "sha256:aaaa"
    assert row.fetched_at == fetched

    # 下一次成功的再验证把失败标记清掉（stale / last_error 回到"没问题"）
    assert store.touch_kb_meta_cache(HOME, "kb_detail", "kb_1", checked_at=_ago()) is True
    clean = store.get_kb_meta_cache(HOME, "kb_detail", "kb_1")
    assert clean is not None and clean.stale is False and clean.last_error == ""

    # 行已经不在了 → False（不是异常，调用方据此改走 put）
    assert store.touch_kb_meta_cache(HOME, "kb_detail", "没有这个键", checked_at=_ago()) is False


def test_drop_removes_exactly_one_row(store: SqliteMetaStore) -> None:
    store.put_kb_meta_cache(_record(scope_key="kb_1"))
    store.put_kb_meta_cache(_record(scope_key="kb_2"))

    assert store.drop_kb_meta_cache(HOME, "kb_detail", "kb_1") == 1
    assert store.drop_kb_meta_cache(HOME, "kb_detail", "kb_1") == 0  # 再删一次是 0，不是错误
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_1") is None
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_2") is not None


def test_stats_counts_rows_bytes_and_both_ends(store: SqliteMetaStore) -> None:
    """报数以 ``CAST(payload AS BLOB)`` 为准（UTF-8 字节）——与淘汰时同一把尺子。"""
    older, newer = _ago(hours=3), _ago(minutes=1)
    payloads = ['{"a": 1}', '{"名": "中文"}']
    store.put_kb_meta_cache(
        _record(scope_key="kb_1", payload=payloads[0], fetched_at=older, checked_at=older)
    )
    store.put_kb_meta_cache(
        _record(scope_key="kb_2", payload=payloads[1], fetched_at=newer, checked_at=newer)
    )

    stats = store.kb_meta_cache_stats()
    assert stats.rows == 2
    assert stats.payload_bytes == sum(len(item.encode("utf-8")) for item in payloads)
    assert stats.oldest_fetched_at == older and stats.newest_fetched_at == newer
    # 按地址报数（另一台地址是空的）
    assert store.kb_meta_cache_stats(provider=OFFICE).rows == 0
    assert store.kb_meta_cache_stats(provider=HOME).rows == 2


def test_an_empty_table_reports_zeroes_instead_of_none(store: SqliteMetaStore) -> None:
    """空表也是**一份读数**（0 行 0 字节），不是"没有答案"——界面那句"还没留过"要用它。"""
    stats = store.kb_meta_cache_stats()
    assert (stats.rows, stats.payload_bytes) == (0, 0)
    assert stats.oldest_fetched_at is None and stats.newest_fetched_at is None


# ------------------------------------------------------------------ 上限与 LRU（R4）


def test_the_limits_and_the_age_are_the_numbers_the_plan_decided() -> None:
    """D-E 批准的那三个数字 + 30 天，钉在这里（真机跑一轮要调就一起调）。"""
    assert MAX_ROWS_PER_PROVIDER == 500
    assert MAX_PAYLOAD_BYTES == 2 * 1024 * 1024
    assert MAX_TOTAL_BYTES == 64 * 1024 * 1024
    assert SNAPSHOT_MAX_AGE_SECONDS == 30 * 24 * 60 * 60


def test_a_payload_over_the_single_entry_cap_is_not_cached(
    store: SqliteMetaStore, caplog: pytest.LogCaptureFixture
) -> None:
    """单条超 2 MiB → **不缓存**并如实记一条日志（§3.4-2），调用方那条链不受影响。"""
    oversize = '{"name": "' + "x" * MAX_PAYLOAD_BYTES + '"}'
    with caplog.at_level(logging.WARNING, logger="app.storage.sqlite_impl.meta_store"):
        assert store.put_kb_meta_cache(_record(payload=oversize)) is False
    assert store.kb_meta_cache_stats().rows == 0
    assert "跳过不缓存" in caplog.text


def test_a_payload_exactly_at_the_cap_is_still_cached(store: SqliteMetaStore) -> None:
    """判据是"**超过**"上限：恰好等于的那一份照收（边界方向不能反）。"""
    # `{"a": "` 是 7 字节、收尾 `"}` 是 2 字节，填到正好 MAX_PAYLOAD_BYTES
    exact = '{"a": "' + "x" * (MAX_PAYLOAD_BYTES - 9) + '"}'
    assert len(exact.encode("utf-8")) == MAX_PAYLOAD_BYTES
    assert store.put_kb_meta_cache(_record(payload=exact)) is True
    assert store.kb_meta_cache_stats().rows == 1


def test_the_per_provider_row_cap_evicts_the_oldest_by_fetched_at(
    store: SqliteMetaStore,
) -> None:
    """每地址 500 行封顶，淘汰的是 ``fetched_at`` **最旧**的那一份（不是最先写进来的）。

    用例把"最旧"那一行**最后**写进去：如果实现按写入顺序淘汰，被扔的会是第 0 号；
    按 ``fetched_at`` 淘汰才会扔刚写进来的这一行。
    """
    base = _ago(hours=2)
    for index in range(MAX_ROWS_PER_PROVIDER):
        moment = base + timedelta(seconds=index)
        store.put_kb_meta_cache(
            _record(scope_key=f"kb_{index:04d}", fetched_at=moment, checked_at=moment)
        )
    store.put_kb_meta_cache(_record(provider=OFFICE, scope_key="kb_1"))  # 另一台地址，不该被牵连

    assert store.kb_meta_cache_stats(provider=HOME).rows == MAX_ROWS_PER_PROVIDER

    stale = _ago(days=1)
    store.put_kb_meta_cache(_record(scope_key="kb_ancient", fetched_at=stale, checked_at=stale))

    assert store.kb_meta_cache_stats(provider=HOME).rows == MAX_ROWS_PER_PROVIDER
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_ancient") is None, "最旧的没被淘汰"
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_0000") is not None
    assert store.get_kb_meta_cache(OFFICE, "kb_detail", "kb_1") is not None, "别的地址被牵连了"


def test_the_total_budget_evicts_the_oldest_rows(
    store: SqliteMetaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全局总量超了也按 ``fetched_at`` 淘汰。

    把上限压到 4 KiB 来跑（真实值是 64 MiB）：造 64 MiB 数据只是让用例慢几十秒，
    而判据（"从最新往回累计到装不下为止，更旧的全扔"）一模一样。
    """
    monkeypatch.setattr(meta_store_module, "MAX_TOTAL_BYTES", 4096)
    body = '{"a": "' + "x" * 3000 + '"}'
    assert len(body.encode("utf-8")) == 3009

    for index in range(3):
        moment = _ago(hours=3 - index)
        store.put_kb_meta_cache(
            _record(scope_key=f"kb_{index}", payload=body, fetched_at=moment, checked_at=moment)
        )

    stats = store.kb_meta_cache_stats()
    assert stats.rows == 1, "装不下就该只剩最新那一份"
    assert stats.payload_bytes <= 4096
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_2") is not None
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_0") is None


def test_a_rejected_payload_never_triggers_the_budget_prune(
    store: SqliteMetaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被单条上限拒掉的那一份**没有落库**，于是也不该收总量——否则会连带扔好数据。"""
    monkeypatch.setattr(meta_store_module, "MAX_TOTAL_BYTES", 4096)
    kept = _ago(hours=1)
    store.put_kb_meta_cache(
        _record(scope_key="kb_keep", payload='{"a": 1}', fetched_at=kept, checked_at=kept)
    )

    oversize = '{"a": "' + "x" * MAX_PAYLOAD_BYTES + '"}'  # 超单条 2 MiB：当场被拒
    assert store.put_kb_meta_cache(_record(scope_key="kb_huge", payload=oversize)) is False
    assert store.kb_meta_cache_stats().rows == 1
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_keep") is not None


# ------------------------------------------------------------------ 超龄（§3.4-3）


def test_an_unconfirmed_snapshot_reads_as_missing_and_the_row_goes(
    store: SqliteMetaStore, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 30 天没被确认 → **当没有**，并把那行删掉（§3.4-3 / §4.2）。

    时钟往后推 31 天来验（不是把签名改成 31 天前写的）：这样"写进去的那一刻"仍是
    真实的，判据落在**读**这一侧。
    """
    store.put_kb_meta_cache(_record())
    assert len(_raw_rows(database)) == 1

    later = datetime.now(UTC) + timedelta(days=31)
    monkeypatch.setattr(meta_store_module, "_now", lambda: later)
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_1") is None
    assert _raw_rows(database) == [], "判成'没有'的那一行还留在库里"


def test_the_age_is_thirty_days_and_it_is_judged_on_the_last_confirmation(
    store: SqliteMetaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两条边界：① 恰好 30 天还算数（判据是"超过"）；② 判据是 ``checked_at``。

    ② 是**刻意**的：内容一年前看到、昨天确认过的快照照旧可读——它恰恰是"被确认过的
    那一份"。若按 ``fetched_at`` 判，一份天天确认、内容稳定的快照会在第 30 天被判死。
    界面那句"上次更新于 X"读的仍然是 ``fetched_at``，两个时间戳不混。
    """
    frozen = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(meta_store_module, "_now", lambda: frozen)

    store.put_kb_meta_cache(
        _record(
            scope_key="kb_edge",
            fetched_at=frozen - timedelta(days=30),
            checked_at=frozen - timedelta(days=30),
        )
    )
    assert store.get_kb_meta_cache(HOME, "kb_detail", "kb_edge") is not None

    store.put_kb_meta_cache(
        _record(
            scope_key="kb_stable",
            fetched_at=frozen - timedelta(days=365),
            checked_at=frozen - timedelta(days=1),
        )
    )
    stable = store.get_kb_meta_cache(HOME, "kb_detail", "kb_stable")
    assert stable is not None
    assert stable.fetched_at < stable.checked_at


def test_the_prune_sweeps_expired_rows_on_the_next_write(
    store: SqliteMetaStore, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超龄的行不必等到被读：下一次写就把它们清掉（它们读出来也是"没有"）。"""
    store.put_kb_meta_cache(_record(scope_key="kb_1"))
    store.put_kb_meta_cache(_record(scope_key="kb_2"))

    later = datetime.now(UTC) + timedelta(days=31)
    monkeypatch.setattr(meta_store_module, "_now", lambda: later)
    # 这一份是"在 later 那一刻看到的"（不这么写的话，它自己也会被判成超龄而清掉）
    store.put_kb_meta_cache(_record(scope_key="kb_fresh", fetched_at=later, checked_at=later))

    assert [row["scope_key"] for row in _raw_rows(database)] == ["kb_fresh"]


# ------------------------------------------------------------------ 坏 payload / 清理


def test_a_bad_payload_is_refused(store: SqliteMetaStore, database: Database) -> None:
    """坏 payload 拒之门外：仓储给一句能读的话，列上的 ``CHECK`` 是最后一道。"""
    with pytest.raises(ValueError, match="合法 JSON 文本"):
        store.put_kb_meta_cache(_record(payload="这不是 JSON"))
    assert store.kb_meta_cache_stats().rows == 0

    # 绕过仓储直接写也进不去（"存进来的必然是 JSON"这句话由 schema 守着）
    with pytest.raises(sqlite3.IntegrityError, match="json_valid"), database.session() as conn:
        conn.execute(
            "INSERT INTO kb_meta_cache"
            " (provider, resource, scope_key, payload, version, source, fetched_at_ms,"
            "  checked_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (HOME, "kb_detail", "kb_1", "也不是 JSON", "sha256:x", "revalidate", 1, 1),
        )


def test_purge_has_three_granularities(store: SqliteMetaStore) -> None:
    """三档粒度：按库 / 按地址 / 全清（``DELETE /local/kb-cache`` 那三个入口）。"""
    view_1 = "kb_1|folder:root|page:1|size:20"
    view_2 = "kb_1|folder:root|page:2|size:20"
    store.put_kb_meta_cache(_record(resource="kb_detail", scope_key="kb_1"))
    store.put_kb_meta_cache(_record(resource="folders", scope_key="kb_1"))
    store.put_kb_meta_cache(_record(resource="doc_list", scope_key=view_1))
    store.put_kb_meta_cache(_record(resource="doc_list", scope_key=view_2))
    store.put_kb_meta_cache(_record(provider=OFFICE, resource="kb_detail", scope_key="kb_1"))
    assert store.kb_meta_cache_stats().rows == 5

    # ① 按库清：这一页那个库的几个视图（前缀带分隔符，由调用方拼）
    assert store.purge_kb_meta_cache(provider=HOME, resource="doc_list", scope_prefix="kb_1|") == 2
    assert store.purge_kb_meta_cache(provider=HOME, resource="kb_detail", scope_key="kb_1") == 1
    # ② 按地址清（换了一台 NAS / 设置里那颗「清除」）
    assert store.purge_kb_meta_cache(provider=HOME) == 1  # 只剩 folders 那条
    # ③ 全清
    assert store.purge_kb_meta_cache() == 1
    assert store.kb_meta_cache_stats().rows == 0
    # 清空之后再清是 0，不是错误
    assert store.purge_kb_meta_cache() == 0


def test_the_prefix_purge_escapes_like_wildcards(store: SqliteMetaStore) -> None:
    """库 id 里的下划线是**字面量**：``kb_1|`` 不该把 ``kbx1|`` 那条一起清掉。"""
    store.put_kb_meta_cache(_record(resource="doc_list", scope_key="kb_1|page:1"))
    store.put_kb_meta_cache(_record(resource="doc_list", scope_key="kbx1|page:1"))

    assert store.purge_kb_meta_cache(resource="doc_list", scope_prefix="kb_1|") == 1
    assert store.get_kb_meta_cache(HOME, "doc_list", "kbx1|page:1") is not None


def test_validate_reports_how_much_is_kept(
    store: SqliteMetaStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """运维入口报的是**真数**（§3.4-4）：写两行之后那句就是 2 行、17 字节。"""
    store.put_kb_meta_cache(_record(scope_key="kb_1", payload='{"a": 1}'))
    store.put_kb_meta_cache(_record(scope_key="kb_2", payload='{"b": 22}'))

    # 借的是同一个数据目录（用例的 `database` fixture 就建在 tmp_path 下）
    assert validate_main(["--data-dir", str(tmp_path)]) == 0
    assert "知识库元数据快照 2 行、17 字节" in capsys.readouterr().out
