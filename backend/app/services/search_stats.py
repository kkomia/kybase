"""检索命中趋势（v0.61）：按知识库记下每次检索的候选与留存，再折成曲线。

## 为什么另起一张表

``usage_events(kind='search')`` 只记了一个整数：这次检索**返回了几条**。
它没有库这一维，于是两件用户真正会问的事都答不出来：

- "这个库的命中在涨还是在跌"（KB 卡片上那条迷你趋势）——一次检索可能跨好几个库，
  落在一行上的那个数归给谁都不对；
- "这几天有多少次是「资料里没有」"（``fit`` 的分布）——用量表没有放它的地方。

``search_events`` 补的就是这两样：**一行 = 一次检索 × 一个库**，外加这次检索整体的
契合度判定（``distribution.fit``）。

## 两处口径要说清楚

**``candidates`` 与 ``kept`` 不是一回事**：前者是分布收敛**之前**这个库贡献的条数
（``summarize`` 看到的那一批），后者是收敛之后**剩下**的条数——见
``storage/base.SearchEventRecord`` 的说明。只记一个数的话，"给 12 条留 2 条"
（召回还行、只是被收敛剪掉了）与"给 2 条留 2 条"（压根没捞到东西）
在曲线上长得一模一样，而那两件事的下一步动作完全不同。

**``kept`` 记的是"收敛留下了多少"，不是"这一页给了几条"**（v0.63）：一次检索可以翻页
（``offset`` / ``next_offset``），而趋势要回答的是"这个库的命中在涨还是在跌"——
照页数记的话，同一个主题翻三页就把那一天记成三倍，曲线立刻变成"翻页次数曲线"。

**``searches`` 是"库被检索了几次"，不是"发生过几次检索"**：一次跨两库的检索会写两行，
所以「概览」那条全局曲线上的 ``searches`` 是各库之和（= 窗口内的行数）。
表里没有 search id（那要另加一列，而这一版不需要它），硬凑一个"去重后的检索次数"
只能靠时间戳相等去猜——不如把口径说成它实际是的那一个。

## 按本地日分桶、聚合留在 Python

与 ``services/usage.py``、``services/stats.py`` 同一套：``created_at`` 按 UTC 存，
而用户在界面上看的「今天」是本地的今天（``local_day`` 那一段注释记着这个坑）。
所以查询只做时间下推，分桶留在这里——本地部署的量（一次检索几行）全读回来聚合够用。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

# **复用** ``stats.local_day``（返回 ``date``）而不是再抄第三份：
# ``usage.local_day`` 是同一口径的另一份（返回 ``str``，给 JSON 键用），
# 而这里要的正是 date 对象（响应里的 ``day`` 是 ``date``）。口径这件事只该有一处：
# 抄第三份的那天起，"本地日"就有了三个会各自漂的实现。
from app.services.stats import local_day
from app.storage.base import SearchEventRecord, StoreBundle

__all__ = [
    "SEARCH_HIT_RETENTION_DAYS",
    "KbSearchHits",
    "SearchHitPoint",
    "SearchHitsSummary",
    "SearchStatsService",
]

logger = logging.getLogger(__name__)

#: 检索命中记录的保留天数。
#:
#: **刻意不跟用量表共用**（``usage.USAGE_RETENTION_DAYS = 180``）：控制台能看的窗口上限是
#: **365 天**（``api/v1/stats.MAX_WINDOW_DAYS``），照 180 天清会让"近一年"的后半段
#: 整段变空——而且是**慢慢**变空，看起来像"最近没人检索"，比报错更难查。
#: 400 = 365 + 35 天余量：多出来的一个多月用来兜住窗口边界（窗口按**本地日**算，
#: 与 UTC 最多差 14 小时，跨年那一刻最后一天的数据不该被判过期）。
SEARCH_HIT_RETENTION_DAYS = 400


@dataclass(frozen=True, slots=True)
class SearchHitPoint:
    """某一天、某个库（或全局）的命中量。"""

    day: date
    candidates: int
    """分布收敛之前这个库贡献的条数。"""
    kept: int
    """收敛之后剩下多少条（v0.63 起不是"某一页返回的条数"）。"""


@dataclass(frozen=True, slots=True)
class KbSearchHits:
    """单个知识库在窗口内的命中情况 + 它那条**稀疏**曲线。"""

    kb_id: str
    name: str
    candidates: int
    kept: int
    searches: int
    """这个库在窗口内被检索了几次（= 它的行数）。"""
    series: list[SearchHitPoint] = field(default_factory=list)
    """只有真的有过记录的那些天。**不补零**：补零是画图的事，
    而"哪天没有记录"与"那天记了 0"在数据里本来就该分开（前者根本没有那一行）。"""


@dataclass(frozen=True, slots=True)
class SearchHitsSummary:
    """``GET /stats/search-hits`` 要的全部数字。"""

    days: int
    candidates: int
    kept: int
    searches: int
    by_kb: list[KbSearchHits] = field(default_factory=list)
    """**只含窗口内真有记录的库**：一次都没被检索过的库不出现在这里——
    它没有曲线可画，出现只会让图例多出一堆平线。"""


class SearchStatsService:
    """写一次检索的命中记录、读回一条趋势。

    写侧是给 ``RetrievalService`` 的回调（``search_recorder``），
    读侧给 ``/stats/search-hits`` 与概览页的活跃度曲线。
    """

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 记

    def record(
        self,
        *,
        hits_by_kb: Mapping[str, tuple[int, int]],
        fit: str,
        duration_ms: int = 0,
    ) -> None:
        """把一次检索的按库命中写成若干行。

        ``hits_by_kb`` 的形状是 ``{kb_id: (candidates, kept)}``，**并集由检索层算好**
        （``request.kb_ids ∪ 有候选的 ∪ 有留存的``）：只有它知道"这次检索了哪些库"，
        而"检索了但一条没命中"必须留下 candidates=0 的一行——否则"这个库最近没人查"
        与"查了但查不到"在曲线上无法区分，而那正是这份数据要回答的一半问题。

        **空映射直接不写**：不指定库的"全库检索"、且一条都没命中时就是这样，
        没有任何一个库可以被归因。写一行 ``kb_id=''`` 会让趋势图上多出一条点不进去、
        也没有名字的曲线。

        ``fit`` 是**整批**的判定（同一批行的这一列相同）。
        ``duration_ms`` 不落库——同一次检索的耗时已经在 ``usage_events``（kind=search）里，
        这里再存一份就是同一事实的第二份真相；它只进日志，方便把慢检索与写失败对上。
        """
        if not hits_by_kb:
            return
        # **一次检索共用一个时间戳**：这几行本来就是同一件事的几行，同戳让它们
        # 在按时间排的列表里挨在一起（也省得逐行取 now()）。它不是"检索 id"，
        # 别拿它去数次数——见模块头对 ``searches`` 口径的说明。
        stamp = datetime.now(UTC)
        records = [
            SearchEventRecord(
                id=f"se_{uuid.uuid4().hex[:12]}",
                kb_id=kb_id,
                candidates=max(0, int(candidates)),
                kept=max(0, int(kept)),
                fit=fit or "unknown",
                created_at=stamp,
            )
            for kb_id, (candidates, kept) in hits_by_kb.items()
        ]
        try:
            self._stores.usage.record_search_events(records)
        except Exception:
            # 与用量同一条纪律：**统计失败绝不能反过来影响检索本身**
            # （调用方那边还有一层兜底，这里这层是为了让直接用本服务的场景也安全）
            logger.exception("检索命中记录失败（不影响本次检索）：%d 个库", len(records))
            return
        logger.debug("记下检索命中：%d 个库，耗时 %d ms，契合度 %s", len(records), duration_ms, fit)

    # ------------------------------------------------------------------ 汇总

    def hits(self, *, days: int = 30, kb_ids: list[str] | None = None) -> SearchHitsSummary:
        """窗口内按库的命中趋势。

        ``kb_ids``（成员视角）：只统计这些库，与 ``StatsService.dashboard`` 同一套
        ——成员的驾驶舱不能露出别人的库有几个、叫什么，更不能露出它们被搜了多少次。
        """
        window_start = datetime.now(UTC) - timedelta(days=days)
        events = self._stores.usage.list_search_events(since=window_start)
        visible = None if kb_ids is None else set(kb_ids)
        names = {kb.id: kb.name for kb in self._stores.knowledge_bases.list_knowledge_bases()}

        totals = [0, 0, 0]  # candidates / kept / searches
        per_kb: dict[str, list] = {}
        for event in events:
            if event.created_at is None:
                continue
            if visible is not None and event.kb_id not in visible:
                continue
            day = local_day(event.created_at)
            row = per_kb.get(event.kb_id)
            if row is None:
                row = per_kb[event.kb_id] = [0, 0, 0, {}]
            for index, value in enumerate((event.candidates, event.kept, 1)):
                row[index] += value
                totals[index] += value
            day_row = row[3].setdefault(day, [0, 0])
            day_row[0] += event.candidates
            day_row[1] += event.kept

        by_kb = [
            KbSearchHits(
                kb_id=kb_id,
                # 查不到名字时退回 id 而不是空串：库删了它的行会被级联删掉，
                # 走到这里说明是别的原因（比如名字表读失败），这时 id 至少还能对上
                name=names.get(kb_id, kb_id),
                candidates=row[0],
                kept=row[1],
                searches=row[2],
                series=[
                    SearchHitPoint(day=day, candidates=values[0], kept=values[1])
                    for day, values in sorted(row[3].items())
                ],
            )
            for kb_id, row in sorted(per_kb.items())
        ]
        return SearchHitsSummary(
            days=days,
            candidates=totals[0],
            kept=totals[1],
            searches=totals[2],
            by_kb=by_kb,
        )

    # ------------------------------------------------------------------ 维护

    def purge_expired(self, *, days: int = SEARCH_HIT_RETENTION_DAYS) -> int:
        """清掉超出保留期的命中记录（挂在 worker 的空闲分支上）。"""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = self._stores.usage.purge_search_events_before(cutoff)
        if removed:
            logger.info("清理了 %d 条过期检索命中记录", removed)
        return removed
