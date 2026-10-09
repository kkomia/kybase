"""可观测性：把"卡住"从感觉变成判据（M7 / T7.4）。

**要解决的问题**：任务列表原先只有状态标签（pending / running / failed）。
但"running"这个标签本身说明不了任何事——一个跑了 5 秒的和一个卡了两小时的
看起来完全一样。用户盯着页面的那几分钟里，他真正想知道的是
**"它是还在干活，还是已经死了"**。

三个判据，都基于**已有数据**，不引入新的簿记：

| 判据 | 含义 | 怎么算 |
|------|------|--------|
| `running` | 正常执行中 | 状态 RUNNING 且租约未过期 |
| `stalled` | **可能卡住了** | 状态 RUNNING 但租约已过期——没有 worker 在给它续约 |
| `overdue` | 该跑没跑 | PENDING 且 `next_run_at` 已经过去很久 |

**为什么用租约过期判"卡住"而不是用耗时阈值**：租约是 worker 主动续的（心跳），
过期就意味着"没人认领这个任务了"——这是**确定的事实**。
而"跑了超过 N 分钟"只是启发式：一份 500MB 的 PDF 解析半小时是正常的，
用耗时阈值会一直误报，误报多了用户就不看了。

**为什么阈值取 3 倍心跳**：太小会把"GC 停顿一下"报成卡住；太大就等于没报。
租约时长由配置决定，所以这里按它的倍数算，而不是写死一个秒数。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models.enums import TaskState
from app.storage.base import StoreBundle, TaskRecord

__all__ = ["ObservabilityService", "TaskHealth"]

logger = logging.getLogger(__name__)

#: PENDING 任务超过这个时间还没被领取，就认为"该跑没跑"。
#: 正常情况下一秒内就会被领走——排队不该排十分钟。
OVERDUE_AFTER = timedelta(minutes=10)


@dataclass(slots=True)
class TaskHealth:
    """一个任务的健康判据。"""

    task_id: str
    status: str
    """``running`` / ``stalled`` / ``overdue`` / ``idle`` / ``done``。"""
    label: str
    """给人看的中文短标签。"""
    detail: str = ""
    """一句解释——光有标签用户还得猜，尤其是"卡住"这种判断。"""

    @property
    def is_problem(self) -> bool:
        return self.status in ("stalled", "overdue")


class ObservabilityService:
    """任务健康与运行态概览。"""

    def __init__(self, stores: StoreBundle, *, worker_lease_seconds: int = 60) -> None:
        self._stores = stores
        self._lease_seconds = max(1, worker_lease_seconds)

    # ------------------------------------------------------------------ 单任务

    def assess(self, task: TaskRecord, *, now: datetime | None = None) -> TaskHealth:
        moment = now or datetime.now(UTC)

        if task.state is TaskState.RUNNING:
            if task.lease_expires_at is not None and _aware(task.lease_expires_at) < moment:
                return TaskHealth(
                    task_id=task.id,
                    status="stalled",
                    label="可能卡住",
                    detail=(
                        "执行租约已过期，说明没有 worker 在续约——"
                        "进程可能已经挂掉。重启服务后它会被自动回收重跑"
                    ),
                )
            return TaskHealth(task_id=task.id, status="running", label="执行中")

        if task.state is TaskState.PENDING:
            # next_run_at 为空说明是刚入队的（还没被调度器写过），不算逾期
            if task.next_run_at is not None:
                due = _aware(task.next_run_at)
                if moment - due > OVERDUE_AFTER:
                    waited = _humanize(moment - due)
                    return TaskHealth(
                        task_id=task.id,
                        status="overdue",
                        label="长时间未执行",
                        detail=(
                            f"已等待 {waited} 仍未被领取。"
                            "如果服务没有常驻消费线程（KYLAB_RUN_WORKER），"
                            "任务不会自己开始"
                        ),
                    )
            return TaskHealth(task_id=task.id, status="idle", label="排队中")

        if task.state is TaskState.SUCCEEDED:
            return TaskHealth(task_id=task.id, status="done", label="已完成")
        if task.state is TaskState.CANCELED:
            return TaskHealth(task_id=task.id, status="done", label="已取消")
        return TaskHealth(
            task_id=task.id,
            status="done",
            label="已失败",
            detail=task.error or "",
        )

    # ------------------------------------------------------------------ 概览

    def overview(self, *, now: datetime | None = None) -> dict[str, object]:
        """运行态总览：给驾驶舱与健康检查用。"""
        moment = now or datetime.now(UTC)
        tasks = self._stores.meta.list_tasks()
        healths = [self.assess(task, now=moment) for task in tasks]

        problems = [item for item in healths if item.is_problem]
        if problems:
            # 有卡住的任务就值得在日志里留痕——它通常意味着进程崩过或没起 worker
            logger.warning(
                "有 %d 个任务状态异常：%s",
                len(problems),
                "、".join(f"{item.label}({item.task_id})" for item in problems[:5]),
            )

        return {
            "total": len(tasks),
            "running": sum(1 for item in healths if item.status == "running"),
            "queued": sum(1 for item in healths if item.status == "idle"),
            "stalled": sum(1 for item in healths if item.status == "stalled"),
            "overdue": sum(1 for item in healths if item.status == "overdue"),
            "problems": [
                {
                    "task_id": item.task_id,
                    "status": item.status,
                    "label": item.label,
                    "detail": item.detail,
                }
                for item in problems[:20]
            ],
        }


def _aware(moment: datetime) -> datetime:
    """统一成带时区的时间。

    SQLite 存的是字符串，读回来可能是 naive 的——直接与 ``datetime.now(UTC)``
    比较会抛 ``TypeError: can't compare offset-naive and offset-aware``。
    按 UTC 补上时区（本项目的所有时间戳本来就是 UTC 存的）。
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"
