"""可观测性：任务健康判据（M7 / T7.4）。

镜像同构：``app/services/observability.py`` → 本文件。

**这一层的价值全在"判据是否可靠"上**：把还在正常跑的任务报成"卡住"，
用户看几次就不看了；而漏报真正卡住的任务，等于这个功能不存在。
所以用例围着两个方向转：**该报的要报，不该报的不能报**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import TaskKind, TaskState
from app.services.observability import OVERDUE_AFTER, ObservabilityService
from app.storage.base import TaskRecord

LEASE = 60


@pytest.fixture
def obs(bundle) -> ObservabilityService:  # type: ignore[no-untyped-def]
    return ObservabilityService(bundle, worker_lease_seconds=LEASE)


def _task(**overrides) -> TaskRecord:  # type: ignore[no-untyped-def]
    base = {
        "id": "task_1",
        "kind": TaskKind.PARSE,
        "state": TaskState.PENDING,
    }
    base.update(overrides)
    return TaskRecord(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- 正常态


def test_running_with_a_live_lease_is_healthy(obs: ObservabilityService) -> None:
    """**这条是防误报的关键**：租约没过期的运行中任务必须判成正常。

    判错了用户就开始无视这个标记，然后真正卡住的那次也没人看。
    """
    now = datetime.now(UTC)
    task = _task(state=TaskState.RUNNING, lease_expires_at=now + timedelta(seconds=30))

    health = obs.assess(task, now=now)

    assert health.status == "running"
    assert health.is_problem is False


def test_pending_without_a_due_time_is_just_queued(obs: ObservabilityService) -> None:
    """刚入队的任务 ``next_run_at`` 还是空的（调度器还没写过），不算逾期。"""
    health = obs.assess(_task(state=TaskState.PENDING), now=datetime.now(UTC))

    assert health.status == "idle"
    assert health.is_problem is False


def test_recently_scheduled_pending_is_queued(obs: ObservabilityService) -> None:
    now = datetime.now(UTC)
    task = _task(state=TaskState.PENDING, next_run_at=now + timedelta(seconds=5))

    assert obs.assess(task, now=now).status == "idle"


def test_succeeded_and_canceled_are_done(obs: ObservabilityService) -> None:
    now = datetime.now(UTC)
    assert obs.assess(_task(state=TaskState.SUCCEEDED), now=now).status == "done"
    assert obs.assess(_task(state=TaskState.CANCELED), now=now).status == "done"


def test_failed_carries_the_error(obs: ObservabilityService) -> None:
    health = obs.assess(
        _task(state=TaskState.FAILED, error="解析失败：文件损坏"), now=datetime.now(UTC)
    )

    assert health.status == "done"
    assert "文件损坏" in health.detail


# --------------------------------------------------------------------- 卡住


def test_running_with_an_expired_lease_is_stalled(obs: ObservabilityService) -> None:
    """**判据用的是租约过期，不是耗时阈值。**

    租约是 worker 主动续的；过期就意味着"没人认领这个任务了"——这是确定的事实。
    而"跑了超过 N 分钟"只是启发式：一份 500MB 的 PDF 解析半小时是正常的，
    用耗时阈值会一直误报。
    """
    now = datetime.now(UTC)
    task = _task(state=TaskState.RUNNING, lease_expires_at=now - timedelta(seconds=1))

    health = obs.assess(task, now=now)

    assert health.status == "stalled"
    assert health.is_problem is True
    assert "租约已过期" in health.detail


def test_long_running_but_renewed_is_not_stalled(obs: ObservabilityService) -> None:
    """跑很久但心跳正常 = 还在干活。这条防的是"大文件被误报成卡住"。"""
    now = datetime.now(UTC)
    # 一小时前创建，但租约刚刚续过
    task = _task(
        state=TaskState.RUNNING,
        created_at=now - timedelta(hours=1),
        lease_expires_at=now + timedelta(seconds=LEASE),
    )

    assert obs.assess(task, now=now).status == "running"


def test_running_without_a_lease_is_not_stalled(obs: ObservabilityService) -> None:
    """租约为空说明刚领取、还没来得及写——不能据此判卡住。"""
    assert obs.assess(_task(state=TaskState.RUNNING), now=datetime.now(UTC)).status == "running"


# --------------------------------------------------------------------- 逾期


def test_pending_way_past_its_due_time_is_overdue(obs: ObservabilityService) -> None:
    """**"任务一直排队"最常见的原因是内嵌消费线程没开**，
    所以这条的 detail 必须把那个原因说出来，而不是只报"逾期"。
    """
    now = datetime.now(UTC)
    task = _task(state=TaskState.PENDING, next_run_at=now - OVERDUE_AFTER - timedelta(minutes=1))

    health = obs.assess(task, now=now)

    assert health.status == "overdue"
    assert health.is_problem is True
    assert "KYLAB_RUN_WORKER" in health.detail


def test_pending_slightly_late_is_not_overdue(obs: ObservabilityService) -> None:
    """重试退避会让 next_run_at 落在几十秒后，那不是"没人管"。"""
    now = datetime.now(UTC)
    task = _task(state=TaskState.PENDING, next_run_at=now - timedelta(seconds=30))

    assert obs.assess(task, now=now).status == "idle"


# --------------------------------------------------------------------- 时区


def test_naive_timestamps_do_not_blow_up(obs: ObservabilityService) -> None:
    """SQLite 存字符串、读回来可能是 naive 的时间。

    直接与 ``datetime.now(UTC)`` 比较会抛
    ``TypeError: can't compare offset-naive and offset-aware``——
    那会让整个任务列表 500，而原因只是一个时区标记。

    用 2020 年这种**确定在过去**的时刻，而不是写某个近期日期：
    否则这条用例会随挂钟漂移，某一天突然开始失败（踩过）。
    """
    naive_past = datetime(2020, 1, 1, 0, 0, 0)  # 无 tzinfo，但确定已过去
    task = _task(state=TaskState.RUNNING, lease_expires_at=naive_past)

    health = obs.assess(task, now=datetime.now(UTC))

    assert health.status == "stalled"


def test_naive_future_timestamp_is_still_healthy(obs: ObservabilityService) -> None:
    """naive 时间被当作 UTC 解读；很远的未来仍算"租约有效"。"""
    naive_future = datetime(2099, 1, 1, 0, 0, 0)
    task = _task(state=TaskState.RUNNING, lease_expires_at=naive_future)

    assert obs.assess(task, now=datetime.now(UTC)).status == "running"


# --------------------------------------------------------------------- 总览


def test_overview_counts_by_status(obs: ObservabilityService, bundle) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    for task in (
        [
            _task(id="t1", state=TaskState.RUNNING, lease_expires_at=now + timedelta(seconds=30)),
            _task(id="t2", state=TaskState.RUNNING, lease_expires_at=now - timedelta(seconds=1)),
            _task(id="t3", state=TaskState.PENDING),
            _task(id="t4", state=TaskState.PENDING, next_run_at=now - OVERDUE_AFTER * 2),
        ]
    ):
        bundle.meta.enqueue_task(task)

    overview = obs.overview(now=now)

    assert overview["total"] == 4
    assert overview["running"] == 1
    assert overview["queued"] == 1
    assert overview["stalled"] == 1
    assert overview["overdue"] == 1


def test_overview_lists_only_problems(obs: ObservabilityService, bundle) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    bundle.meta.enqueue_task(_task(id="t1", state=TaskState.PENDING))
    bundle.meta.enqueue_task(
        _task(id="t2", state=TaskState.RUNNING, lease_expires_at=now - timedelta(seconds=1))
    )

    overview = obs.overview(now=now)

    problems = overview["problems"]
    assert len(problems) == 1
    assert problems[0]["task_id"] == "t2"
    assert problems[0]["label"] == "可能卡住"


def test_overview_on_empty_queue(obs: ObservabilityService) -> None:
    """一条任务都没有时不能炸——新部署就是这个状态。"""
    overview = obs.overview()

    assert overview["total"] == 0
    assert overview["problems"] == []


def test_overview_caps_the_problem_list(obs: ObservabilityService, bundle) -> None:  # type: ignore[no-untyped-def]
    """问题列表封顶 20 条：界面不需要滚一屏同一个毛病，日志也没必要。"""
    now = datetime.now(UTC)
    for index in range(30):
        bundle.meta.enqueue_task(
            _task(
                id=f"t{index:02d}",
                state=TaskState.RUNNING,
                lease_expires_at=now - timedelta(seconds=1),
            )
        )

    assert len(obs.overview(now=now)["problems"]) == 20
