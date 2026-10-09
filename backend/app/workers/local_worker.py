"""**本机档的消费者**（定时任务 + 本机库的空闲维护）。

服务器档那个消费者（``queue_worker.TaskWorker``）跑的是**摄取流水线**：认领任务 →
解析 / 切块 / 嵌入 / 向量索引。本机档没有那几张表（知识库在 NAS 上），所以那条循环在本机
只会每隔几秒撞一次不可用的库——于是 M2 时期的结论是"本机档不起消费者"
（见 `app/main.py` 的 lifespan）。那个结论对**摄取**是对的，但它顺手把两件
**本机真的需要、且数据真在本机**的事一起漏掉了：

1. **定时任务到点了没人跑**：调度记录落在本机库的 `scheduled_tasks` 表里
   （`ScheduleRepo` 属本机域），"到点"判定也在本机（`due_scheduled_tasks`）——
   但那条链的下一环是"入队"，而队列表（`TaskQueueRepo`）在 NAS 上。
   于是界面上建好的任务永远是"下次运行时间"往后跳、什么也不发生；
2. **本机库只增不减**：`usage_events` 有保留期（`usage.USAGE_RETENTION_DAYS`），
   而清它的那个动作挂在服务器档消费者的空闲分支上（`core/services.py::_maintain`）
   ——本机档没有那个分支，所以它一次都没跑过。

所以这一模块只做这两件事，**一行摄取都不做**：

| 这一档 | 服务器档 |
| --- | --- |
| 到点 → 交给本机运行队列（`ScheduleRunner`）直接跑 | 到点 → 入队，消费者认领后跑 |
| 空闲维护：**只碰本机表**（用量） | 空闲维护：清回收站 / 幂等键 / 任务与阶段事件 / 补摘要 |

**为什么不复用 `TaskWorker`**：它的一半循环（消费 / 续租 / 看门狗 / 补摘要）每一个
第一步都要读 KB 域的表（`claim_task` / `reclaim_expired_tasks` / `sweep_stalled` /
`list_documents_without_summary`），在本机档全部抛 `KnowledgeBaseUnavailable`
——那是"进程里那个协程每几秒撞一次墙"的形态，正是 M2 要避免的。而本机这半边**没有**
租约、没有重试、没有崩溃回收（本机档跑的是自己这台机器上的一次问答，进程没了就是没了，
下一次到点会再来一遍）。

**安全边界**（起点就在这条纪律上）：本机的空闲维护**只调用本机域那几个方法**
（现在只有 `usage.purge_expired()`，它的下界是 `USAGE_RETENTION_DAYS = 180` 天）。
不复制服务器那份 `_maintain`：里面的 `purge_expired_trash` 会**连磁盘上的原文一起删**，
`prune_history` 删的是文档阶段事件——那些数据都在 NAS 上，而"什么该删"的判定必须跟着
数据走（NAS 那份消费者自己会做，它管的是它自己的库）。

**默认起、可关**：它只做"用户自己建过的定时任务"与"过期的用量行"，没有一条是
"用户没要过的动作"，所以本机档默认就起（与服务器档的 `KYLAB_RUN_WORKER` 同一把开关：
设成假值两边都不起，见两个入口里的那几行）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from uuid import uuid4

from app.services.schedule_runner import run_scheduled_task
from app.workers.queue_worker import (
    DEFAULT_MAINTAIN_INTERVAL,
    DEFAULT_SCHEDULE_INTERVAL,
)

if TYPE_CHECKING:
    # **只在类型检查时导入**：`core.services` 的组合根要 import `workers.queue_worker`
    # （上面的常量与服务器档那个消费者），运行时再 import 它就多一条无用的边。
    from app.core.services import Services

__all__ = ["LocalScheduler", "bind_local_scheduler", "run_local_scheduler"]

logger = logging.getLogger(__name__)

#: 运行 id 的前缀（"这一次运行"的标识）。与队列任务 id（``task_``）刻意不同：
#: 本机档没有队列表，这个 id 不进任何表，只在"立即跑一次"的响应里当标识用
#: （见 `services/schedules.py::run_now` 的说明）。
RUN_ID_PREFIX = "localrun_"


class LocalScheduler:
    """本机档的消费者：定时任务到点就跑 + 本机库的空闲维护。

    它同时是 `services/schedules.ScheduleService` 要的那个 ``ScheduleRunner``
    （``submit`` 那一个方法）——**装配点只有一处**（``bind_local_scheduler``），
    因为"谁来跑这条到点的任务"这件事不该有两个答案。
    """

    def __init__(
        self,
        services: Services,
        *,
        interval: float = DEFAULT_SCHEDULE_INTERVAL,
        maintain_interval: float = DEFAULT_MAINTAIN_INTERVAL,
    ) -> None:
        self._services = services
        self._interval = interval
        self._maintain_interval = maintain_interval
        # 单线程：一轮问答（几分钟、要调模型）自己排好队，不并发——
        # 本机档的定时任务多半是"汇总昨天"这种，两条一起跑只会互相抢模型额度与磁盘写锁。
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-schedule")
        self._busy = False

    # ---------------------------------------------------------------- 接缝（ScheduleRunner）

    def submit(self, scheduled_id: str) -> str:
        """排一条进本机运行队列，**立刻返回**这次运行的标识（真跑在后台线程里）。

        为什么不像服务器档那样"入队 + 由消费者认领"：本机没有那张队列表
        （见模块头）。这一层就是那条链在本机的最小替身，而"到点判定 / CAS 认领 /
        下一次时刻"仍然共用 `ScheduleService` 里那一份（不抄第二遍 cron 逻辑）。
        """
        run_id = f"{RUN_ID_PREFIX}{uuid4().hex[:12]}"
        logger.info("本机档：定时任务 %s 开跑（%s）", scheduled_id, run_id)
        self._pool.submit(self._run_one, scheduled_id)
        return run_id

    # ---------------------------------------------------------------- 循环

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """跑到 ``stop`` 置位（与服务器档消费者同一条退出语义：**不取消、自己收工**）。

        `stop` 由调用方（两个入口的 lifespan）给：置位后两条循环立刻从
        ``wait_for(stopping.wait(), …)`` 醒来返回，不会多等一个间隔。
        """
        stopping = stop or asyncio.Event()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._schedule_loop(stopping))
                group.create_task(self._maintain_loop(stopping))
        finally:
            # 正在跑的那一轮**不打断**（它会自己把会话与 `last_status` 写完），
            # 但不在这里干等：卡住关停比丢一轮更糟。`ThreadPoolExecutor` 的线程
            # 会在解释器退出时被 join，所以那一轮仍然跑得完。
            if self._busy:
                logger.info("本机档：还有一轮定时任务在跑，关停不等它（它会自己收尾）")
            self._pool.shutdown(wait=False)

    async def _schedule_loop(self, stopping: asyncio.Event) -> None:
        """按 `DEFAULT_SCHEDULE_INTERVAL` 扫一遍"有没有定时任务到点"。

        与服务器档那条 ``_schedule_loop`` 同一条理由、同一个默认节奏（20 秒 ≈ cron 的
        最小粒度）：它做的事（"这条到点了没有"）与手上有没有活干无关，所以**独立成一条
        循环**，不挂在别的分支上。扫与认领都要写库（``arm_scheduled_task`` 是一次 CAS），
        所以丢进线程跑，不占事件循环。
        """
        while not stopping.is_set():
            try:
                count = await asyncio.to_thread(self._services.schedules.run_due)
            except Exception:
                logger.warning("本机档：扫描定时任务失败，跳过本轮", exc_info=True)
            else:
                if count:
                    logger.info("本机档：定时任务到点 %d 条（已交给本机运行队列）", count)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._interval)

    async def _maintain_loop(self, stopping: asyncio.Event) -> None:
        """按 `DEFAULT_MAINTAIN_INTERVAL` 收一收**本机**那几张只增不减的表。

        一小时一次（与服务器档同一节奏）：它防的是"表无限长大"，不必更勤。
        异常一律吞掉——清理失败不该带走消费者（下一轮还会再来一次）。
        """
        while not stopping.is_set():
            try:
                await asyncio.to_thread(self._maintain)
            except Exception:
                logger.warning("本机档：空闲维护失败，跳过本轮", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._maintain_interval)

    def _maintain(self) -> None:
        """本机库自己的收尾：**只碰本机域的表**（见模块头"安全边界"）。

        现在只有一件：过期的用量行（`usage_events` 是本机域唯一的"有保留期的表"）。
        做法与服务器档那份 `_maintain` 一致——由服务层去做（这个类不碰存储细节）。
        """
        removed = self._services.usage.purge_expired()
        if removed:
            logger.info("本机档：清理过期用量记录 %d 条", removed)

    # ---------------------------------------------------------------- 跑一条

    def _run_one(self, scheduled_id: str) -> None:
        """真跑一轮（在运行线程里）。**失败必须落到调度的状态上**，不能只有日志。

        `run_scheduled_task` 自己会写 ``last_status`` / ``last_error``（成功、降级、失败
        三种都写，见 `services/schedule_runner.py`），所以这里只兜住它抛出来的异常——
        否则线程池会把它吞成一条看不见的 traceback，而界面上那条任务会永远停在"还在跑"。
        """
        self._busy = True
        try:
            run_scheduled_task(self._services, scheduled_id)
        except Exception as exc:
            logger.warning("本机档：定时任务 %s 这一轮没跑成：%s", scheduled_id, exc)
        finally:
            self._busy = False


def bind_local_scheduler(services: Services, **kwargs: float) -> LocalScheduler:
    """装好本机消费者并**挂上接缝**（两个入口各调一次，实现只有这一份）。

    为什么两个入口都要调：桌面壳起的是**边车**（`python -m app.sidecar`，端口 8765），
    而"本机档后端"那个入口（`app.main`，`KYLAB_DEPLOYMENT=local`）是另一条路径——
    两边的本机库里是同一张 `scheduled_tasks` 表，所以"到点跑"这件事两边都该有人做。
    同时起两个也不会跑两遍：认领是一次 CAS（``arm_scheduled_task``），
    第二个进程要么认领失败、要么在下一轮看到 ``next_run_at`` 已经推到下一个周期。
    """
    scheduler = LocalScheduler(services, **kwargs)  # type: ignore[arg-type]
    services.schedules.bind_runner(scheduler)
    return scheduler


async def run_local_scheduler(scheduler: LocalScheduler, stop: asyncio.Event) -> None:
    """跑消费循环并兜住异常（与 `main.py::_run_worker` 同一条口径）。

    取消原样上抛：``CancelledError`` 被吞掉的话 asyncio 会以为它正常结束了，
    取消语义就丢了（那条教训写在 `main.py` 里）。
    """
    try:
        await scheduler.run_forever(stop=stop)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("本机档消费者异常退出")
