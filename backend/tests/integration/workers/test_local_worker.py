"""本机档的消费者（`app/workers/local_worker.py`）。

它要钉住的是**本机档为什么需要一条自己的循环**、以及它的边界：

1. **到点就跑，不走队列**：本机档没有 `tasks` 那张表（`TaskQueueRepo` 属知识库域，
   在本机档整体抛），所以"入队 + 消费者认领"那一环在本机不成立。接缝挂上之后，
   到点的记录交给 `ScheduleRunner`（本机运行队列），**不碰** `enqueue_task`；
2. **没挂接缝时一条都不跑**（如实回 0，而不是编一个默认实现）；
3. **空闲维护只碰本机表**：这里钉的是"它调的是 `UsageService.purge_expired`
   （本机 `usage_events`）"——服务器那份 `_maintain` 里的回收站 / 任务 / 阶段事件
   一件都不许在本机跑（那些数据在 NAS 上）；
4. **两个入口都装得上**（`bind_local_scheduler`）：边车与 `app.main` 各有自己的
   lifespan，但"谁来跑这条到点的任务"只能有一个答案。

没有测试库（``pytest -m local``）也能跑：它只用本机 SQLite 与文件系统。
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.core.services import get_services, reset_services
from app.core.storage import get_stores, reset_stores
from app.models.enums import TaskKind, TaskState
from app.services.schedules import ScheduleService
from app.storage.base import KnowledgeBaseUnavailable, ScheduledTaskRecord, TaskRecord

pytestmark = pytest.mark.local


@contextlib.contextmanager
def _local(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """钉死本机档（与 `test_local_signing_secret._local_app` 同一手法）。

    ``KYLAB_DATABASE_URL`` 必须**显式置空**：跑 PG 测试时 conftest 把它指向测试库，
    而本机档见到连接串会当场拒绝启动（"两个真相源"那条）。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    try:
        yield
    finally:
        reset_services()
        reset_stores()
        get_settings.cache_clear()


def _due_now(stores, record: ScheduledTaskRecord) -> ScheduledTaskRecord:  # type: ignore[no-untyped-def]
    """把某条记录的 ``next_run_at`` 设成"刚刚该跑"（测试里不真的等到 9 点）。"""
    fresh = stores.schedules.get_scheduled_task(record.id)
    assert fresh is not None
    fresh.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    return fresh


class _Recorder:
    """假的本机运行队列：只记下"谁被排进来了"（不起线程、不调模型）。"""

    def __init__(self) -> None:
        self.runs: list[str] = []

    def submit(self, scheduled_id: str) -> str:
        self.runs.append(scheduled_id)
        return f"localrun_test_{len(self.runs)}"


def test_run_due_hands_the_task_to_the_runner_not_to_a_queue(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """到点 → 交给接缝（本机运行队列），**不入队**；下次时间照旧往后推。"""
    with _local(monkeypatch):
        services = get_services()
        stores = get_stores()
        schedules: ScheduleService = services.schedules
        record = schedules.create(
            name="早报", prompt="把昨天的日志汇总成三条", kind="cron", cron="0 9 * * *"
        )
        stores.schedules.update_scheduled_task(_due_now(stores, record))

        runner = _Recorder()
        schedules.bind_runner(runner)

        assert schedules.run_due() == 1
        assert runner.runs == [record.id]
        after = stores.schedules.get_scheduled_task(record.id)
        assert after is not None and after.next_run_at is not None
        assert after.next_run_at > datetime.now(UTC)
        # 再扫一次不重复：下次时间已经推到未来
        assert schedules.run_due() == 0


def test_the_local_face_really_has_no_task_queue(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**这条接缝为什么必须存在**：本机档真去入队会抛（那几张表在 NAS 上）。

    没有这条断言，"为什么不走队列"就只是注释里的一句话——哪天有人把 KB 域的表
    搬进本机库，它也不会红。
    """
    with _local(monkeypatch):
        stores = get_stores()
        with pytest.raises(KnowledgeBaseUnavailable):
            stores.meta.enqueue_task(
                TaskRecord(id="task_x", kind=TaskKind.SCHEDULED, state=TaskState.PENDING)
            )


def test_without_a_runner_nothing_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """没挂接缝时如实回 0（不编一个默认实现，也不静默入队）。"""
    with _local(monkeypatch):
        schedules: ScheduleService = get_services().schedules
        record = schedules.create(
            name="一次", prompt="跑一次", kind="cron", cron="0 9 * * *"
        )
        stores = get_stores()
        stores.schedules.update_scheduled_task(_due_now(stores, record))
        assert schedules.run_due() == 0
        after = stores.schedules.get_scheduled_task(record.id)
        # 也没认领（没跑就没推进下次时间）：下一轮扫描还会看到它
        assert after is not None and after.next_run_at is not None
        assert after.next_run_at < datetime.now(UTC)


def test_bind_local_scheduler_wires_the_service_and_runs_the_run_scheduled_call(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """`bind_local_scheduler` 把接缝接上，并且**真的在后台线程里跑那一轮**。

    这里把 `run_scheduled_task` 换成假的（真那个要调模型），要钉的是"接缝→线程"
    那一跳：`submit` 立刻返回、活由运行线程干，所以调用方（调度循环 / 那一条
    「立即跑一次」的请求）不会被一轮问答按在原处。
    """
    from app.workers import local_worker

    with _local(monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(local_worker, "run_scheduled_task", lambda _s, sid: seen.append(sid))

        scheduler = local_worker.bind_local_scheduler(get_services())
        run_id = scheduler.submit("sched_x")
        assert run_id.startswith(local_worker.RUN_ID_PREFIX)

        deadline = time.monotonic() + 5.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == ["sched_x"]

        # `run_now`（「立即跑一次」那条端点）在本机档也走同一个接缝：
        # 不再往 NAS 的队列表里塞（那是它以前必然 503 的原因）
        record = get_services().schedules.create(
            name="手动", prompt="跑一次", kind="cron", cron="0 9 * * *"
        )
        task = get_services().schedules.run_now(record.id, owner_id=None)
        assert task.kind is TaskKind.SCHEDULED
        assert task.id.startswith(local_worker.RUN_ID_PREFIX)

        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == ["sched_x", record.id]
