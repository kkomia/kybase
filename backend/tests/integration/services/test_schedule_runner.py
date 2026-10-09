"""定时任务的执行链路（v0.33）：调度侧入队 → worker 认领 → 跑一轮 → 结果落会话。

镜像同构：``app/services/schedule_runner.py`` + ``app/workers/queue_worker.py`` 的
``_schedule_loop`` / ``TaskKind.SCHEDULED`` 分支 → 本文件。

要钉住的是**这条链路为什么这么接**：

1. **调度只入队，不跑问答**：到点那一刻做的事必须很轻（它跑在一条独立循环上），
   否则一个慢问答会把"下一次到点"也堵住；
2. **认领是 CAS**：同一批里两个 worker 扫到同一条时只有一个能认领——
   "跑两次"的代价不是重复一次查询，是重复一整轮问答（真花钱）；
3. **结果落进一条会话，形状与对话页一致**（含过程快照），于是"上周它跑了什么、
   结论是什么"就是翻会话记录——不另造一套运行历史；
4. **三种结局都写回状态**（ok / degraded / failed）：漏写一种就会让用户看到
   一条停在"上次跑成没跑成"空白的任务。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import NotFoundError
from app.core.services import get_services
from app.models.enums import TaskKind, TaskState
from app.services import cron as cron_service
from app.services.schedule_runner import run_scheduled_task
from app.services.schedules import ScheduleService
from app.storage.base import ScheduledTaskRecord, StoreBundle, TaskRecord
from tests.conftest import install_fake_chat, search_tool_call


@pytest.fixture
def schedules(bundle: StoreBundle) -> ScheduleService:
    return ScheduleService(bundle)


def _due(services: ScheduleService, **overrides: object) -> ScheduledTaskRecord:  # type: ignore[no-untyped-def]
    payload = {
        "name": "早报",
        "prompt": "把昨天的构建日志汇总成三条结论",
        "kind": "cron",
        "cron": "0 9 * * *",
    }
    payload.update(overrides)
    return services.create(**payload)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 到点入队


def test_a_task_due_now_is_enqueued_once(store, schedules: ScheduleService) -> None:  # type: ignore[no-untyped-def]
    """到点 → 入队一条 SCHEDULED 任务，并把下次时间推到下一回。"""
    record = _due(schedules)
    # 把它挪到"刚刚该跑"（不真的等 9 点）
    store.update_scheduled_task(_due_now(store, record))

    assert schedules.enqueue_due() == 1
    tasks = [item for item in store.list_tasks(None) if item.kind is TaskKind.SCHEDULED]
    assert len(tasks) == 1
    assert tasks[0].payload["scheduled_id"] == record.id
    assert tasks[0].state is TaskState.PENDING

    after = store.get_scheduled_task(record.id)
    assert after is not None
    assert after.next_run_at is not None
    assert after.next_run_at > datetime.now(UTC)
    # 再扫一次**不会**重复入队：下次时间已经推到未来了
    assert schedules.enqueue_due() == 0


def test_two_workers_do_not_both_claim_it(store, schedules: ScheduleService) -> None:  # type: ignore[no-untyped-def]
    """**CAS**：同一批两个消费者扫到同一条时，只有一个认领得到。"""
    record = _due(schedules)
    store.update_scheduled_task(_due_now(store, record))
    stale = store.get_scheduled_task(record.id)
    assert stale is not None

    # 第一个 worker 认领成功
    assert store.arm_scheduled_task(
        record.id,
        expected_next_run_at=stale.next_run_at,
        next_run_at=datetime.now(UTC) + timedelta(days=1),
        enabled=True,
    )
    # 第二个拿着**过期的** next_run_at 再认领 → 失败（它看到的是刚才那一份）
    assert not store.arm_scheduled_task(
        record.id,
        expected_next_run_at=stale.next_run_at,
        next_run_at=datetime.now(UTC) + timedelta(days=2),
        enabled=True,
    )


def test_one_shot_disables_itself_after_firing(store, schedules: ScheduleService) -> None:  # type: ignore[no-untyped-def]
    record = schedules.create(
        name="一次",
        prompt="跑一次就好",
        kind="once",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.update_scheduled_task(_due_now(store, record))
    assert schedules.enqueue_due() == 1
    after = store.get_scheduled_task(record.id)
    assert after is not None and after.enabled is False
    assert schedules.enqueue_due() == 0


def test_a_disabled_task_never_fires(store, schedules: ScheduleService) -> None:  # type: ignore[no-untyped-def]
    record = _due(schedules)
    schedules.set_enabled(record.id, owner_id=None, enabled=False)
    assert schedules.enqueue_due() == 0


def test_worker_runs_the_scheduled_task(store, schedules: ScheduleService) -> None:  # type: ignore[no-untyped-def]
    """worker 认领到 SCHEDULED 任务时调用那个回调（组合根接到 ``run_scheduled_task``）。"""
    from app.workers.queue_worker import TaskWorker

    seen: list[str] = []
    worker = TaskWorker(
        store_bundle_of(store),
        FakeIngest(),
        owner="w-schedule",
        poll_interval=0.01,
        run_scheduled=seen.append,
    )
    store.enqueue_task(
        TaskRecord(
            id="task_sched1",
            kind=TaskKind.SCHEDULED,
            state=TaskState.PENDING,
            payload={"scheduled_id": "sched_x"},
        )
    )
    assert asyncio.run(worker.run_once()) is True
    assert seen == ["sched_x"]


# ------------------------------------------------------------------ 跑一轮


def test_run_writes_the_answer_into_its_own_conversation(store, schedules):  # type: ignore[no-untyped-def]
    """跑一轮 = 那条会话里的一轮问答（含出处与过程快照），并写回状态。"""
    answer = "根据 [1]：昨天一共 3 次构建失败。[1]"
    install_fake_chat(answer, script=[search_tool_call("构建日志")])
    record = _due(schedules)

    text = run_scheduled_task(get_services(), record.id)
    assert text == answer

    after = store.get_scheduled_task(record.id)
    assert after is not None
    assert after.last_status == "ok"
    assert after.run_count == 1
    assert after.conversation_id

    messages = store.list_messages(after.conversation_id)
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[0].content == record.prompt
    assert messages[1].content == answer
    # 过程快照与对话页同一份形状（回看时那段过程面板才画得出来）
    assert messages[1].steps
    assert all({"phase", "label", "status"} <= set(step) for step in messages[1].steps)


def test_second_run_appends_to_the_same_conversation(store, schedules):  # type: ignore[no-untyped-def]
    install_fake_chat("第一轮")
    record = _due(schedules)
    run_scheduled_task(get_services(), record.id)
    first = store.get_scheduled_task(record.id)

    install_fake_chat("第二轮")
    run_scheduled_task(get_services(), record.id)
    second = store.get_scheduled_task(record.id)

    assert second is not None and first is not None
    assert second.conversation_id == first.conversation_id
    assert second.run_count == 2
    assert len(store.list_messages(second.conversation_id)) == 4


def test_a_failure_is_recorded_and_raised(store, schedules):  # type: ignore[no-untyped-def]
    """失败要**既写状态又抛出去**：写状态是给用户看的，抛出去是让队列按策略重试。"""
    install_fake_chat("不会用到", error=RuntimeError("上游 500"))
    record = _due(schedules)
    with pytest.raises(RuntimeError, match="500"):
        run_scheduled_task(get_services(), record.id)
    after = store.get_scheduled_task(record.id)
    assert after is not None
    assert after.last_status == "failed"
    assert "500" in after.last_error


def test_an_empty_answer_is_a_failure_not_a_silent_success(store, schedules):  # type: ignore[no-untyped-def]
    """一个字都没吐出来时**不落库、也不报成功**——半截记录会让回看的人以为它答了空的。"""
    install_fake_chat("")
    record = _due(schedules)
    with pytest.raises(RuntimeError, match="没有产出正文"):
        run_scheduled_task(get_services(), record.id)
    after = store.get_scheduled_task(record.id)
    assert after is not None
    assert after.last_status == "failed"
    assert store.list_messages(after.conversation_id or "") == []


def test_unknown_task_id_raises(schedules) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        run_scheduled_task(get_services(), "sched_nope")


# ------------------------------------------------------------------ 小工具


def _due_now(store, record: ScheduledTaskRecord):  # type: ignore[no-untyped-def]
    """把某条记录的 next_run_at 直接设成**刚刚该跑**（测试里不真的等到 9 点）。"""
    fresh = store.get_scheduled_task(record.id)
    assert fresh is not None
    fresh.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    return fresh


def store_bundle_of(store):  # type: ignore[no-untyped-def]
    """给 worker 用的最小 bundle：它只碰任务表。"""

    class _Bundle:
        meta = store

    return _Bundle()


class FakeIngest:
    """worker 需要一个摄入服务，而这条用例不跑摄入。"""

    def ingest(self, document_id: str) -> None:  # pragma: no cover - 不该被调用
        raise AssertionError("SCHEDULED 任务不该走摄入")

    def generate_questions(self, document_id: str) -> None:  # pragma: no cover
        raise AssertionError("SCHEDULED 任务不该走出题")


def test_cron_next_run_stays_in_the_future() -> None:
    """兜底一条：算出来的下次时间一定在未来（否则会立刻再触发一次）。"""
    now = datetime.now()
    assert cron_service.next_after("*/5 * * * *", now) > now
