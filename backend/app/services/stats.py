"""统计聚合（仪表盘的数据源）。

设计口径：

- **只统计真实存在的数据**：文档、切块、任务三类都有时间戳，能出日/月趋势；
  调用量（对话 / 向量化 / 检索）走 ``usage_events``，见 ``/stats/usage``——
  检索原先只在日志里，v17 起也落库了（``kind="search"``），所以"检索量"
  现在是**数出来的**，不是估的；
- **切块按所属文档的创建时间归日**：`chunks` 表没有自己的时间戳，
  但"某天入库的文档带来多少块"才是用户关心的口径，这个近似是准的；
- **查询下推、聚合留在 Python**：这一版把原来的 `K+1` 次查询（按库逐个取全量文档
  + 一次巨型 `IN` + 全表任务）换成**两条投影查询**
  （`list_document_stats` / `list_task_stats`：只取聚合要的那几列，
  文档那条还用一条 `LEFT JOIN` 把切块数一起带出来）。
  分布统计（按阶段 / 后缀 / 来源）与按日序列仍在 Python 里算——
  **没有把 `GROUP BY 日` 下推到 SQL**，原因是"日"是**本地日历日**：
  下推就得把应用时区的偏移传进 SQL，而 `local_day()` 的注释记着这个口径踩过的坑
  （UTC 日期与本地日期在 UTC+8 有 8 小时不重合，界面会显示"今天入库 0"）。
  口径只留一处比少一次往返重要。
  剩余代价是 Python 侧对几列做 `Counter`，一万篇文档是毫秒级。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from app.storage.base import KnowledgeBaseRecord, StoreBundle

__all__ = [
    "ActivityPoint",
    "DashboardStats",
    "KbStatRow",
    "StatsService",
    "local_day",
]

#: 活跃度的默认观察窗口（天）：约四个月的日历热力图
DEFAULT_WINDOW_DAYS = 120


@dataclass(frozen=True, slots=True)
class ActivityPoint:
    """一天的活跃度。三个计数分开给，前端可以切换看哪个维度。"""

    day: date
    documents: int
    chunks: int
    tasks: int


@dataclass(frozen=True, slots=True)
class KbStatRow:
    """单个知识库的规模。"""

    id: str
    name: str
    embedding_model_id: str
    embedding_dim: int
    documents: int
    chunks: int
    last_activity: datetime | None


@dataclass(slots=True)
class DashboardStats:
    """仪表盘要的全部数字。"""

    generated_at: datetime
    window_days: int
    total_knowledge_bases: int
    total_documents: int
    total_chunks: int
    indexed_documents: int
    failed_documents: int
    running_tasks: int
    failed_tasks: int
    storage_bytes: int
    recent_documents: int
    """观察窗口内新增的文档数。"""
    activity: list[ActivityPoint] = field(default_factory=list)
    by_stage: dict[str, int] = field(default_factory=dict)
    by_suffix: dict[str, int] = field(default_factory=dict)
    by_source_kind: dict[str, int] = field(default_factory=dict)
    knowledge_bases: list[KbStatRow] = field(default_factory=list)


class StatsService:
    """把散在几张表里的数字聚合成仪表盘要的形状。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    def dashboard(
        self, *, window_days: int = DEFAULT_WINDOW_DAYS, kb_ids: list[str] | None = None
    ) -> DashboardStats:
        """聚合成仪表盘要的形状。

        ``kb_ids``（v10 成员视角）：只统计这些库。成员的驾驶舱不能露出
        别人的库有几个、叫什么——那不叫统计，叫泄露。
        """
        meta = self._stores.meta
        today = date.today()
        since = today - timedelta(days=window_days - 1)

        knowledge_bases = meta.list_knowledge_bases()
        if kb_ids is not None:
            visible = set(kb_ids)
            knowledge_bases = [kb for kb in knowledge_bases if kb.id in visible]

        # **两条投影查询**取代原来的 K+1 次（按库逐个取全量文档、再换一次巨型 IN）
        # 与"全表任务"：见 storage/base.py 里两个方法的说明。
        #
        # 走**窄视图**（`documents` / `tasks`）而不是 `meta`：这一节只碰这两个域，
        # 签名里就看得出来——这是拆 MetaStore 的第一步（见 storage/repositories.py）。
        # `list_knowledge_bases` 仍在 `meta` 上：知识库域还没切出来。
        documents = self._stores.documents.list_document_stats(
            [kb.id for kb in knowledge_bases]
        )
        # 任务没有直接挂库：经 document 绕一道。没有文档的任务（数据源拉取）
        # 在成员视角下隐藏——它属于管理员关心的全局运维面
        tasks = self._stores.tasks.list_task_stats(
            None if kb_ids is None else [doc.id for doc in documents]
        )

        activity = _build_activity(documents, tasks, since=since, today=today)

        return DashboardStats(
            generated_at=datetime.now(),
            window_days=window_days,
            total_knowledge_bases=len(knowledge_bases),
            total_documents=len(documents),
            total_chunks=sum(doc.chunks for doc in documents),
            indexed_documents=sum(1 for doc in documents if doc.stage in ("indexed", "enriched")),
            failed_documents=sum(1 for doc in documents if doc.stage == "failed"),
            running_tasks=sum(1 for task in tasks if task.state in ("pending", "running")),
            failed_tasks=sum(1 for task in tasks if task.state == "failed"),
            storage_bytes=sum(doc.size_bytes for doc in documents),
            # 用 `local_day` 而不是 `created_at.date()`：后者是 UTC 日期，
            # 在 UTC+8 的早上会把"今天入库的"算成昨天——那正是这个模块
            # 记过的坑（见 `local_day`），而热力图同一屏里用的是本地日，
            # 两处口径不一致时界面会自相矛盾（"近 7 天入库 3" 而热力图上今天为空）
            recent_documents=sum(
                1
                for doc in documents
                if doc.created_at is not None and local_day(doc.created_at) >= since
            ),
            activity=activity,
            by_stage=_count_by(documents, lambda d: d.stage),
            by_suffix=_count_by(documents, lambda d: _suffix_of(d.name)),
            by_source_kind=_count_by(documents, lambda d: d.source_kind),
            knowledge_bases=[_kb_row(kb, documents) for kb in knowledge_bases],
        )


def _kb_row(
    kb: KnowledgeBaseRecord,
    documents: list,
) -> KbStatRow:
    mine = [doc for doc in documents if doc.knowledge_base_id == kb.id]
    stamps = [doc.updated_at for doc in mine if doc.updated_at]
    return KbStatRow(
        id=kb.id,
        name=kb.name,
        embedding_model_id=kb.embedding_model_id,
        embedding_dim=kb.embedding_dim,
        documents=len(mine),
        chunks=sum(doc.chunks for doc in mine),
        last_activity=max(stamps) if stamps else None,
    )


def local_day(stamp: datetime) -> date:
    """把时间戳折算成**本地日历日**。

    为什么不能直接用 ``stamp.date()``：时间戳是按 UTC 存的，而用户在界面上看的
    「今天」是本地的今天。两者在 UTC+8 有 8 小时不重合——实测在这段窗口里，
    刚上传的文档会被归到昨天，仪表盘显示「近 N 天入库 0」而库里明明有今天的新文档。
    这个 bug 只在本地日期与 UTC 日期不同的那几个小时里出现，所以白天开发时看不到。

    实现上先把 naive 时间当作 UTC（存储层就是这么写的），再转本地时区取日期。
    用固定偏移而不是 tzlocal，是因为本机与部署机都在同一台/同一时区。
    """
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone().date()


def _build_activity(
    documents: list,
    tasks: list,
    *,
    since: date,
    today: date,
) -> list[ActivityPoint]:
    """按天聚合，**补齐没有数据的日子**。

    缺口不能省：日历热力图靠"连续的格子"表达节奏，缺天会让图形错位。
    """
    documents_by_day: Counter[date] = Counter()
    chunks_by_day: Counter[date] = Counter()
    for doc in documents:
        if doc.created_at is None:
            continue
        day = local_day(doc.created_at)
        if day < since:
            continue
        documents_by_day[day] += 1
        chunks_by_day[day] += doc.chunks

    tasks_by_day: Counter[date] = Counter()
    for task in tasks:
        stamp = task.updated_at or task.created_at
        if stamp is None:
            continue
        day = local_day(stamp)
        if day < since:
            continue
        tasks_by_day[day] += 1

    points: list[ActivityPoint] = []
    cursor = since
    while cursor <= today:
        points.append(
            ActivityPoint(
                day=cursor,
                documents=documents_by_day.get(cursor, 0),
                chunks=chunks_by_day.get(cursor, 0),
                tasks=tasks_by_day.get(cursor, 0),
            )
        )
        cursor += timedelta(days=1)
    return points


def _count_by(rows: list, key) -> dict[str, int]:  # type: ignore[no-untyped-def]
    counter: Counter[str] = Counter(key(row) for row in rows)
    return dict(counter.most_common())


def _suffix_of(name: str) -> str:
    lowered = name.lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > 0 else "无后缀"
