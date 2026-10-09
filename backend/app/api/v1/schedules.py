"""定时任务端点（v0.33，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.6）。

五件事：列、建、改、删、**立即跑一次**。最后一个不是装饰——新建一条"每天 9 点"的
任务之后，用户最想确认的是"它到底会跑成什么样"，而等到明天早上才知道太晚了。

**它属于任务中心那一页**（前端是一个分段），因为"到点要跑的事"与"正在跑的事"
是同一件事的两个时间态；分成两页会让"我那个定时任务跑了吗"变成要跳页找的问题。

执行那条路（``services/schedule_runner.py``）在这一层只被**间接触发**：
「立即跑一次」把一条 ``TaskKind.SCHEDULED`` 任务放进队列，由 worker 去跑
——所以这个端点立刻返回、不阻塞在几分钟的问答上。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.auth import require_read, require_write
from app.api.v1.schemas import (
    ScheduledTaskCreateIn,
    ScheduledTaskListOut,
    ScheduledTaskOut,
    ScheduledTaskRunOut,
    ScheduledTaskUpdateIn,
)
from app.core.caller import Caller
from app.core.services import Services, get_services
from app.services.schedules import timezone_name

router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled-tasks"])


def _out(services: Services, record) -> ScheduledTaskOut:  # type: ignore[no-untyped-def]
    item = ScheduledTaskOut.model_validate(record)
    # 人话那一段由服务层给（cron 的解析只有一份，就在那儿）
    item.schedule_text = services.schedules.next_run_text(record)
    return item


@router.get("", response_model=ScheduledTaskListOut, summary="定时任务列表")
def list_scheduled_tasks(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ScheduledTaskListOut:
    """成员只看自己的；管理员会话与 API Key 通道看全部（与工作区/能力同一口径）。"""
    records = services.schedules.list(owner_id=caller.owner_id)
    return ScheduledTaskListOut(
        items=[_out(services, item) for item in records],
        timezone=timezone_name(),
    )


@router.post("", response_model=ScheduledTaskOut, summary="新建定时任务")
def create_scheduled_task(
    payload: ScheduledTaskCreateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ScheduledTaskOut:
    """建一条。时间字段的校验（cron 语法、过去的时刻、字段范围）由服务层做，
    报错直接回给用户看——所以措辞是照着"怎么改对"写的。"""
    record = services.schedules.create(
        name=payload.name,
        prompt=payload.prompt,
        kind=payload.kind,
        cron=payload.cron,
        run_at=payload.run_at,
        kb_ids=payload.kb_ids,
        model_pk=payload.model_pk,
        thinking=payload.thinking,
        thinking_effort=payload.thinking_effort,
        owner_id=caller.owner_id,
    )
    return _out(services, record)


@router.patch("/{scheduled_id}", response_model=ScheduledTaskOut, summary="改定时任务")
def update_scheduled_task(
    scheduled_id: str,
    payload: ScheduledTaskUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ScheduledTaskOut:
    record = services.schedules.update(
        scheduled_id,
        owner_id=caller.owner_id,
        name=payload.name,
        prompt=payload.prompt,
        kind=payload.kind,
        cron=payload.cron,
        run_at=payload.run_at,
        kb_ids=payload.kb_ids,
        enabled=payload.enabled,
        model_pk=payload.model_pk,
    )
    return _out(services, record)


@router.delete("/{scheduled_id}", status_code=204, summary="删定时任务")
def delete_scheduled_task(
    scheduled_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> None:
    """**已经跑出来的会话不删**（与"删工作区不删会话"同一条纪律）。"""
    services.schedules.delete(scheduled_id, owner_id=caller.owner_id)


@router.post("/{scheduled_id}/run", response_model=ScheduledTaskRunOut, summary="立即跑一次")
def run_scheduled_task_now(
    scheduled_id: str,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_write)],
) -> ScheduledTaskRunOut:
    """入队一次运行，**不动下次时间**（手动跑不改变周期）。

    返回的是队列任务 id：界面可以顺着它去任务列表里看这一轮跑到哪一步了。
    """
    task = services.schedules.run_now(scheduled_id, owner_id=caller.owner_id)
    return ScheduledTaskRunOut(
        task_id=task.id,
        detail="已经排上队，跑完的结果会落在这条任务的会话里（到点自动跑的那些也在那儿）",
    )
