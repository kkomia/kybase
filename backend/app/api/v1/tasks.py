"""任务端点（M4；可观测性收口 M7 / T7.4）。

界面需要看到"哪些任务在跑、卡在哪一步、重试了几次"（架构 §12 可观测性），
所以任务响应里带上 attempts / max_attempts / error / 租约到期时间。

**M7 补上的是"健康判据"**：原先只有状态标签，而 `running` 本身说明不了任何事——
一个跑了 5 秒的和一个卡了两小时的看起来完全一样。现在每个任务带一个由后端算出的
``health``，界面据此把"可能卡住"标出来。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.auth import check_kb_scope, require_read, require_write
from app.api.v1.schemas import (
    HealthOverviewOut,
    SystemLoadOut,
    TaskCancelIn,
    TaskCancelItemOut,
    TaskCancelOut,
    TaskList,
    TaskOut,
)
from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, KylabError
from app.core.services import Services, get_services
from app.models.enums import TaskState
from app.services.api_key import WRITE, Caller

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskList, summary="任务列表（每项带健康判据）")
def list_tasks(
    state: TaskState | None = Query(default=None, description="按状态过滤"),
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> TaskList:
    # 成员（v10）只看到自己库里的文档任务；任务内容（文件名、报错）是私有数据
    kb_ids = services.api_keys.visible_kb_ids(caller) if caller.user is not None else None
    tasks = services.documents.list_tasks(state, kb_ids=kb_ids)
    # 任务 → 所属库，一次批量取回：界面的"按知识库筛选"靠它，
    # 不必逐个库拉文档来反查归属（那是 N 次请求）
    documents = services.documents.get_documents_by_ids(
        [task.document_id for task in tasks if task.document_id]
    )
    items: list[TaskOut] = []
    for task in tasks:
        health = services.observability.assess(task)
        out = TaskOut.model_validate(task)
        out.health = health.status
        out.health_label = health.label
        out.health_detail = health.detail
        if task.document_id and task.document_id in documents:
            document = documents[task.document_id]
            out.knowledge_base_id = document.knowledge_base_id
            out.document_name = document.name
        items.append(out)
    return TaskList(items=items)


@router.get("/health", response_model=HealthOverviewOut, summary="运行态总览")
def tasks_health(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> HealthOverviewOut:
    """任务运行态总览。

    把"有没有卡住"变成一个可以直接看的数，而不是让用户自己去列表里比对时间。
    ``worker_enabled`` 尤其重要：**内嵌消费线程关掉时任务不会自己跑**——
    这是"任务一直排队"最常见的原因，界面必须能解释它。

    成员（v10）看不到这里：总览包含全局运维信息（有没有别的任务在跑、
    worker 状态），那是管理员的视角。成员的任务在列表里已经够用。
    """
    if caller.user is not None and not caller.is_admin:
        raise ForbiddenError("运行态总览需要管理员身份")
    overview = services.observability.overview()
    # worker 是**启动期**开关（KYLAB_RUN_WORKER，见 main.py），不是运行期设置——
    # 它决定进程里到底有没有那个消费协程，改它要重启，所以从 Settings 读
    return HealthOverviewOut.model_validate(
        {**overview, "worker_enabled": get_settings().run_worker}
    )


@router.get("/load", response_model=SystemLoadOut, summary="负载面板（CPU / 内存 / 队列 / 额度）")
def tasks_load(
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> SystemLoadOut:
    """这台机器现在有多忙。

    **为什么要一个单独的端点**：任务列表回答的是"每个任务怎么了"，而"后台为什么慢"
    常常与任何单个任务无关——是 CPU 满了、并发槽位只有 1 个、还是云端额度用尽。
    这些数都属于**服务端**资源（浏览器那边的 CPU 是用户自己电脑的，与此无关），
    所以只能由后端回。

    与 ``/tasks/health`` 同一档：**管理员专属**。它暴露的是机器资源与运维参数，
    对成员没有可操作的意义。
    """
    if caller.user is not None and not caller.is_admin:
        raise ForbiddenError("负载信息需要管理员身份")
    snapshot = services.load.snapshot()
    return SystemLoadOut.model_validate(snapshot, from_attributes=True)


@router.post("/cancel", response_model=TaskCancelOut, summary="取消还没结束的任务")
def cancel_tasks(
    payload: TaskCancelIn,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_write),
) -> TaskCancelOut:
    """撤销排队中（或正在跑）的任务，**逐条返回成败**。

    存在这一条的理由：队列里堆了几十条 pending 时，用户唯一能按的刹车是"逐篇取消文档"。
    文档级的取消仍然保留（它同时把文档置为 canceled），这里给的是任务视角的入口。

    权限沿用列表那一套：成员只能动**自己可见知识库**里的文档任务；
    没有挂文档的全局任务（数据源拉取、Wiki 生成）只有管理员能动——
    它们属于运维面，与列表里"对成员隐藏"是同一条口径。

    ``state='all'`` 会连正在跑的一起撤：那条路径靠"把文档置 canceled"让 worker 在
    下一个阶段边界停手，所以**不是立刻中断**，云端解析仍会跑完当前那次调用。
    """
    wanted = set(payload.task_ids)
    if payload.task_ids:
        tasks = [task for task in services.documents.list_tasks() if task.id in wanted]
        missing = wanted - {task.id for task in tasks}
    else:
        states = {
            "pending": {TaskState.PENDING},
            "running": {TaskState.RUNNING},
            "all": {TaskState.PENDING, TaskState.RUNNING},
        }[payload.state]
        tasks = [task for task in services.documents.list_tasks() if task.state in states]
        missing = set()

    documents = services.documents.get_documents_by_ids(
        [task.document_id for task in tasks if task.document_id]
    )
    allowed: list[str] = []
    items: list[TaskCancelItemOut] = [
        TaskCancelItemOut(task_id=task_id, ok=False, error="任务不存在")
        for task_id in sorted(missing)
    ]
    for task in tasks:
        document = documents.get(task.document_id or "")
        if document is None:
            # 没有挂文档的全局任务：只有管理员能动
            if not caller.is_admin:
                items.append(
                    TaskCancelItemOut(task_id=task.id, ok=False, error="需要管理员权限")
                )
                continue
        else:
            try:
                check_kb_scope(services, caller, [document.knowledge_base_id], need=WRITE)
            except KylabError as exc:
                items.append(TaskCancelItemOut(task_id=task.id, ok=False, error=str(exc)))
                continue
        allowed.append(task.id)

    for item in services.documents.cancel_tasks(allowed):
        items.append(TaskCancelItemOut(task_id=item.task_id, ok=item.ok, error=item.error))

    return TaskCancelOut(
        succeeded=sum(1 for item in items if item.ok),
        failed=sum(1 for item in items if not item.ok),
        items=items,
    )
