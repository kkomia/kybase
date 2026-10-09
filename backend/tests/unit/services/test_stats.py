"""统计聚合的单元测试。

镜像同构：``app/services/stats.py`` → ``tests/unit/services/test_stats.py``。

仪表盘上每个数字都对应一个决策，算错了没人会发现——所以这里逐个口径钉住。
"""

from datetime import UTC, datetime, timedelta

from app.models.enums import DataSourceKind, DocumentStage, TaskKind, TaskState
from app.services.stats import StatsService, local_day
from app.storage.base import DocumentRecord, KnowledgeBaseRecord, TaskRecord


def _kb(store, kb_id: str = "kb_1", name: str = "手册"):
    return store.create_knowledge_base(
        KnowledgeBaseRecord(
            id=kb_id, name=name, embedding_model_id="BAAI/bge-m3", embedding_dim=1024
        )
    )


def _doc(
    store,
    doc_id: str,
    kb_id: str,
    *,
    stage=DocumentStage.INDEXED,
    days_ago: int = 0,
    name="a.pdf",
    size=100,
    created: datetime | None = None,
):
    stamp = created if created is not None else datetime.now(UTC) - timedelta(days=days_ago)
    return store.create_document(
        DocumentRecord(
            id=doc_id,
            knowledge_base_id=kb_id,
            name=name,
            source_kind=DataSourceKind.UPLOAD,
            content_hash=f"hash-{doc_id}",
            stage=stage,
            size_bytes=size,
            created_at=stamp,
            updated_at=stamp,
        )
    )


def test_recent_documents_counts_by_local_day(store, bundle) -> None:
    """窗口边界按**本地日**算，不是 UTC 日。

    构造"本地今天 00:30"这个时刻——在 UTC+8 它落在 UTC 的昨天。窗口取 1 天
    （``since`` = 今天）时，按 UTC 日会把它算到窗口外，于是界面显示"近 1 天入库 0"
    而用户明明刚上传；热力图同一屏用的却是本地日，两处口径不一致会自相矛盾。
    这就是 `local_day` 注释里记过的那个坑（只在凌晨那几小时出现，白天开发看不到）。
    """
    _kb(store)
    local_early = datetime.now().astimezone().replace(hour=0, minute=30, second=0, microsecond=0)
    _doc(store, "doc_1", "kb_1", created=local_early.astimezone(UTC))

    stats = StatsService(bundle).dashboard(window_days=1)

    assert stats.recent_documents == 1
    assert stats.activity[-1].documents == 1  # 热力图上也是今天


def test_dashboard_counts_scale(store, bundle) -> None:
    _kb(store)
    _doc(store, "doc_1", "kb_1", size=200)
    _doc(store, "doc_2", "kb_1", stage=DocumentStage.FAILED, size=300)

    stats = StatsService(bundle).dashboard()

    assert stats.total_knowledge_bases == 1
    assert stats.total_documents == 2
    assert stats.indexed_documents == 1
    assert stats.failed_documents == 1
    assert stats.storage_bytes == 500


def test_activity_fills_empty_days(store, bundle) -> None:
    """日历热力图靠连续的格子表达节奏，缺天会让图形错位。"""
    _kb(store)
    _doc(store, "doc_1", "kb_1", days_ago=3)
    _doc(store, "doc_2", "kb_1", days_ago=3)
    _doc(store, "doc_3", "kb_1", days_ago=0)

    stats = StatsService(bundle).dashboard(window_days=7)

    assert len(stats.activity) == 7  # 一天都不缺
    by_day = {point.day: point for point in stats.activity}
    assert sum(point.documents for point in stats.activity) == 3
    assert stats.activity[-1].documents == 1  # 今天
    assert stats.activity[-4].documents == 2  # 三天前
    assert by_day  # 字典化不报错


def test_activity_window_excludes_older_documents(store, bundle) -> None:
    _kb(store)
    _doc(store, "doc_old", "kb_1", days_ago=60)
    _doc(store, "doc_new", "kb_1", days_ago=1)

    stats = StatsService(bundle).dashboard(window_days=7)

    assert sum(point.documents for point in stats.activity) == 1
    assert stats.recent_documents == 1  # 窗口外的既不入图也不入"近期"


def test_activity_buckets_by_local_day_not_utc_day() -> None:
    """归日必须按**本地**日历日，否则刚上传的文档会被算到昨天。

    这条是实测踩出来的：凌晨（本地日期与 UTC 日期不同）跑测试时，
    「今天入库」恒为 0——窗口用 ``date.today()``（本地），而分桶用
    ``stamp.date()``（时间按 UTC 存）。两者在 UTC+8 每天有 8 小时不重合。

    断言直接打在 ``local_day`` 上而不是整条 dashboard 上：这样它在任何时区、
    任何时刻都稳定，不必靠"恰好在某个钟点跑"才复现。
    """
    # UTC 的 23:30 —— 在东八区已经是第二天
    stamp = datetime(2026, 9, 10, 23, 30, tzinfo=UTC)

    assert local_day(stamp) == stamp.astimezone().date()
    if stamp.astimezone().utcoffset() != timedelta(0):
        assert local_day(stamp) != stamp.date(), "还在按 UTC 归日，跨零点这段会算错"


def test_local_day_treats_naive_timestamps_as_utc() -> None:
    """存储层写的是 naive UTC，折算时不能再当成本地时间（会多算一个时差）。"""
    naive = datetime(2026, 9, 10, 23, 30)
    aware = datetime(2026, 9, 10, 23, 30, tzinfo=UTC)

    assert local_day(naive) == local_day(aware)


def test_knowledge_base_rows_carry_scale_and_last_activity(store, bundle) -> None:
    _kb(store, "kb_1", "手册")
    _kb(store, "kb_2", "归档")
    _doc(store, "doc_1", "kb_1", days_ago=5)
    _doc(store, "doc_2", "kb_1", days_ago=1)

    stats = StatsService(bundle).dashboard()
    rows = {row.id: row for row in stats.knowledge_bases}

    assert rows["kb_1"].documents == 2
    assert rows["kb_2"].documents == 0
    assert rows["kb_2"].last_activity is None
    assert rows["kb_1"].last_activity is not None


def test_breakdowns_cover_stage_and_suffix(store, bundle) -> None:
    _kb(store)
    _doc(store, "doc_1", "kb_1", name="指南.pdf")
    _doc(store, "doc_2", "kb_1", name="笔记.md", stage=DocumentStage.FAILED)
    _doc(store, "doc_3", "kb_1", name="无后缀")

    stats = StatsService(bundle).dashboard()

    assert stats.by_stage == {"indexed": 2, "failed": 1}
    assert stats.by_suffix[".pdf"] == 1
    assert stats.by_suffix[".md"] == 1
    assert stats.by_suffix["无后缀"] == 1


def test_task_counters_split_running_and_failed(store, bundle) -> None:
    _kb(store)
    _doc(store, "doc_1", "kb_1")
    now = datetime.now(UTC)
    for index, state in enumerate(
        [TaskState.PENDING, TaskState.RUNNING, TaskState.FAILED, TaskState.SUCCEEDED]
    ):
        store.enqueue_task(
            TaskRecord(
                id=f"task_{index}",
                kind=TaskKind.PARSE,
                state=state,
                document_id="doc_1",
                created_at=now,
                updated_at=now,
            )
        )

    stats = StatsService(bundle).dashboard()

    assert stats.running_tasks == 2
    assert stats.failed_tasks == 1


def test_empty_database_returns_zeros_not_errors(bundle) -> None:
    """首次启动、一个库都没有时，驾驶舱要显示 0 而不是崩。"""
    stats = StatsService(bundle).dashboard(window_days=7)

    assert stats.total_knowledge_bases == 0
    assert stats.total_documents == 0
    assert sum(point.documents for point in stats.activity) == 0
    assert len(stats.activity) == 7
