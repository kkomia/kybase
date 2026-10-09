"""检索命中趋势（v0.61）。

镜像同构：``app/services/search_stats.py`` → 本文件。

四条要紧的性质：

1. **一行 = 一次检索 × 一个库**，且"被检索了但一条没命中"也要有一行
   （``candidates=0``）——不然"没人查过"与"查了答不上来"在曲线上长得一样；
2. **``candidates``（收敛前）与 ``kept``（收敛后）是两个数**，混成一个就再也
   分不出"召回不行"与"收敛切太狠"；
3. **按本地日历日分桶**，与用量、今日入库同一口径（UTC 分桶会让 UTC+8 的用户
   每天 08:00 之前的数据算到前一天）；
4. **统计失败绝不影响业务**——用户已经拿到检索结果，因为写一行统计而报错是本末倒置。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.search_stats import SEARCH_HIT_RETENTION_DAYS, SearchStatsService
from app.storage.base import KnowledgeBaseRecord, SearchEventRecord


@pytest.fixture
def search_stats(bundle) -> SearchStatsService:  # type: ignore[no-untyped-def]
    return SearchStatsService(bundle)


def _kb(store, kb_id: str, name: str) -> None:
    store.create_knowledge_base(
        KnowledgeBaseRecord(
            id=kb_id, name=name, embedding_model_id="BAAI/bge-m3", embedding_dim=1024
        )
    )


def _row(
    store,
    event_id: str,
    kb_id: str,
    *,
    candidates: int,
    kept: int,
    fit: str = "weak",
    created_at: datetime | None = None,
) -> None:
    store.record_search_events(
        [
            SearchEventRecord(
                id=event_id,
                kb_id=kb_id,
                candidates=candidates,
                kept=kept,
                fit=fit,
                created_at=created_at,
            )
        ]
    )


# --------------------------------------------------------------------- 记录


def test_record_writes_one_row_per_knowledge_base(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """一次检索跨两个库 → 两行；**一条没命中的那个库也有一行**（候选 0）。

    库记录得先存在：``kb_id`` 是**外键**（删库要连历史命中一起清，见存储层用例），
    所以"先建库再记"本来就是真实写侧的次序。
    """
    _kb(bundle.meta, "kb_1", "技术库")
    _kb(bundle.meta, "kb_2", "运维库")
    search_stats.record(hits_by_kb={"kb_1": (12, 3), "kb_2": (0, 0)}, fit="weak")

    # 两层排序：同一批记录**共用一个时间戳**（见 record 的说明），所以 created_at
    # 分不出先后——按 kb_id 排一下，断言的是"两行都在、值各自落对"
    rows = sorted(
        (row.kb_id, row.candidates, row.kept, row.fit) for row in bundle.meta.list_search_events()
    )
    assert rows == [
        ("kb_1", 12, 3, "weak"),
        ("kb_2", 0, 0, "weak"),
    ], "被检索了但没捞到东西的库不能从数据里消失"


def test_record_keeps_the_pre_and_post_cut_counts_apart(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """``candidates`` 是收敛**前**、``kept`` 是收敛**后**——两个数各存一份。

    只留一个的话，"给 12 条留 2 条"（召回还行、只是被收敛剪掉了）与
    "给 2 条留 2 条"（压根没捞到东西）在曲线上完全一样。
    """
    _kb(bundle.meta, "kb_1", "技术库")
    search_stats.record(hits_by_kb={"kb_1": (12, 2)}, fit="strong")

    row = bundle.meta.list_search_events()[0]
    assert (row.candidates, row.kept) == (12, 2)


def test_record_with_no_knowledge_base_to_attribute_writes_nothing(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """不指定库的"全库检索"、且一条都没命中：没有可以被归因的库，什么都不写。

    写一行 ``kb_id=''`` 会让趋势图上多出一条点不进去、也没有名字的曲线。
    """
    search_stats.record(hits_by_kb={}, fit="none")

    assert bundle.meta.list_search_events() == []


def test_record_never_raises_even_if_the_store_fails() -> None:
    """**统计失败绝不能影响业务**：用户已经拿到检索结果。"""

    class BrokenUsage:
        def record_search_events(self, records):  # type: ignore[no-untyped-def]
            raise RuntimeError("磁盘满了")

    service = SearchStatsService(type("S", (), {"usage": BrokenUsage()})())  # type: ignore[arg-type]

    service.record(hits_by_kb={"kb_1": (3, 1)}, fit="weak")  # 不该抛


def test_record_clamps_negative_counts(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """计数落库前夹到非负：负数画在曲线上是"凭空少了几条"。"""
    _kb(bundle.meta, "kb_1", "技术库")
    search_stats.record(hits_by_kb={"kb_1": (-3, -1)}, fit="weak")

    row = bundle.meta.list_search_events()[0]
    assert (row.candidates, row.kept) == (0, 0)


# --------------------------------------------------------------------- 汇总


def test_hits_totals_add_up_across_knowledge_bases(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    _kb(bundle.meta, "kb_1", "技术库")
    _kb(bundle.meta, "kb_2", "运维库")
    _row(bundle.meta, "se_1", "kb_1", candidates=5, kept=2)
    _row(bundle.meta, "se_2", "kb_1", candidates=4, kept=1)
    _row(bundle.meta, "se_3", "kb_2", candidates=0, kept=0, fit="none")

    summary = search_stats.hits(days=7)

    assert (summary.candidates, summary.kept, summary.searches) == (9, 3, 3)
    by_kb = {item.kb_id: item for item in summary.by_kb}
    assert by_kb["kb_1"].name == "技术库"
    assert (by_kb["kb_1"].candidates, by_kb["kb_1"].kept, by_kb["kb_1"].searches) == (9, 3, 2)
    # "被检索了但一条没命中"的库**要在**这里：candidates=0 也是记录
    assert by_kb["kb_2"].searches == 1
    assert by_kb["kb_2"].candidates == 0


def test_hits_omits_knowledge_bases_without_rows(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """从没被检索过的库不进 ``by_kb``——它没有曲线可画，出现只会让图例多一条平线。"""
    _kb(bundle.meta, "kb_1", "技术库")
    _kb(bundle.meta, "kb_2", "没搜过的库")
    _row(bundle.meta, "se_1", "kb_1", candidates=1, kept=1)

    assert [item.kb_id for item in search_stats.hits(days=7).by_kb] == ["kb_1"]


def test_hits_buckets_by_local_day(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """归日按**本地日历日**：凌晨本地那一格在 UTC 日里是"昨天"。

    与用量、今日入库同一口径——两处口径不一致时界面会自相矛盾
    （"近 7 天有检索"而热力图上今天为空）。
    """
    _kb(bundle.meta, "kb_1", "技术库")
    local_early = datetime.now().astimezone().replace(hour=0, minute=30, second=0, microsecond=0)
    _row(bundle.meta, "se_1", "kb_1", candidates=3, kept=1, created_at=local_early.astimezone(UTC))

    series = search_stats.hits(days=1).by_kb[0].series

    assert [point.day for point in series] == [local_early.date()]
    assert len(series) == 1, "同一天的两条要合成一个点（这里只有一条）"


def test_hits_merges_rows_of_the_same_day(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    _kb(bundle.meta, "kb_1", "技术库")
    now = datetime.now(UTC)
    _row(bundle.meta, "se_1", "kb_1", candidates=2, kept=1, created_at=now)
    _row(bundle.meta, "se_2", "kb_1", candidates=5, kept=2, created_at=now)

    point = search_stats.hits(days=1).by_kb[0].series[0]

    assert (point.candidates, point.kept) == (7, 3)


def test_hits_respects_the_window(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """窗口外的记录不能算进来，否则"最近 7 天"画的是全部历史。"""
    _kb(bundle.meta, "kb_1", "技术库")
    _row(
        bundle.meta,
        "se_old",
        "kb_1",
        candidates=999,
        kept=99,
        created_at=datetime.now(UTC) - timedelta(days=40),
    )
    _row(bundle.meta, "se_new", "kb_1", candidates=3, kept=1)

    assert search_stats.hits(days=7).candidates == 3
    assert search_stats.hits(days=90).candidates == 1002


def test_hits_narrows_to_the_visible_knowledge_bases(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """成员视角：别人的库不进窗口，**连总数都不进**——跨库汇总也是私有信息。"""
    _kb(bundle.meta, "kb_mine", "我的库")
    _kb(bundle.meta, "kb_other", "别人的库")
    _row(bundle.meta, "se_1", "kb_mine", candidates=4, kept=1)
    _row(bundle.meta, "se_2", "kb_other", candidates=7, kept=3)

    summary = search_stats.hits(days=7, kb_ids=["kb_mine"])

    assert summary.candidates == 4
    assert [item.kb_id for item in summary.by_kb] == ["kb_mine"]


def test_hits_of_an_empty_history_is_all_zeros(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """一次检索都没有时不能炸，界面要能正常渲染空态度。"""
    summary = search_stats.hits(days=7)

    assert (summary.candidates, summary.kept, summary.searches) == (0, 0, 0)
    assert summary.by_kb == []


# --------------------------------------------------------------------- 清理


def test_purge_expired_keeps_the_console_window_intact(search_stats, bundle) -> None:  # type: ignore[no-untyped-def]
    """保留期必须**长过控制台能看的最长窗口**（否则曲线会慢慢变空）。

    这里顺带钉住那个常量本身：它小于 365（``api/v1/stats.MAX_WINDOW_DAYS`` 那个上限）
    就意味着"近一年"这条曲线有一段时间是假的——而且是慢慢变假，没人会报错。
    """
    assert SEARCH_HIT_RETENTION_DAYS > 365

    _kb(bundle.meta, "kb_1", "技术库")
    _row(
        bundle.meta,
        "se_old",
        "kb_1",
        candidates=1,
        kept=1,
        created_at=datetime.now(UTC) - timedelta(days=SEARCH_HIT_RETENTION_DAYS + 10),
    )
    _row(bundle.meta, "se_new", "kb_1", candidates=1, kept=1)

    removed = search_stats.purge_expired()

    assert removed == 1
    assert [row.id for row in bundle.meta.list_search_events()] == ["se_new"]
