"""异步任务消费者（M2 T2.2 的调度侧）。

把 SQLite 任务表当成队列用（架构 §2.1：不引入 Redis，重启可恢复）：

- **领取是原子单语句**（见 ``MetaStore.claim_task``），多个 worker 不会双领；
- **执行期间持续续租**：解析可能跑几分钟，租约到期被别人回收就会重复干活；
- **长任务放线程池**：摄入是同步阻塞逻辑，直接在事件循环里跑会卡住所有 API 请求
  （这正是计划风险登记册 R5 提到的"云端解析轮询拖垮单进程 asyncio"）；
- **失败按指数退避重回队列**，超过 ``max_attempts`` 才判终态失败（架构 §4）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.exceptions import NotFoundError
from app.models.enums import TaskKind, TaskState
from app.services.ingest import IngestCanceled, IngestService, sweep_stalled
from app.storage.base import StoreBundle, TaskRecord

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 60
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_BASE_BACKOFF = 2.0
DEFAULT_MAX_BACKOFF = 60.0
DEFAULT_MAINTAIN_INTERVAL = 3600.0
"""空闲维护间隔（秒）。一小时一次：清理是"防表无限长大"，不必更勤。"""

DEFAULT_WATCHDOG_INTERVAL = 60.0
"""入库看门狗扫描间隔（秒）。

一分钟一次：看门狗的门槛是 `ingest.STALL_SECONDS`（4 分钟），再加上这个间隔，
一份卡住的文档最迟 **5 分钟**被发现——与方案 8 要的"≤5 分钟自动完成或失败"对齐。
比这更勤没有意义（判定门槛是分钟级），更懒则会把"最迟多久被发现"顶到 5 分钟以上。"""

DEFAULT_SCHEDULE_INTERVAL = 20.0
"""扫一遍"有没有定时任务到点"的间隔（秒）。

**为什么单独一条循环、而不是挂在空闲分支上**：定时任务要的是"到点就跑"，
而挂在空闲分支上意味着"队列一直忙 → 它一直不跑"（实测过同一个形状的坑：
租约回收挂在空闲分支上时，队列忙起来就永远不回收，见 ``_reclaim_loop``）。
20 秒是"分钟级精度"与"扫描开销"之间的折中：cron 的最小粒度就是分钟，
再密也快不了一秒，而每次扫描只是一条带索引的 SELECT。
"""

DEFAULT_SUMMARY_INTERVAL = 30.0
"""补文档摘要的间隔（秒）。

**为什么要与 ``DEFAULT_MAINTAIN_INTERVAL`` 分开**：维护是"防表长大"，一小时一次
没问题；补摘要是"把已有文档补上，好让后续每轮问答省 token"——一小时只补 2 篇的话，
一个 20 篇的库要 10 小时才补完，而它恰恰是这版新加的机制、老文档全都缺（实测踩到：
按小时节拍跑了 10 分钟只补了 2 篇）。30 秒一批、一批 2 篇，20 篇几分钟收敛。
离线时它一次都不跑（只在"没活可干"的分支里触发），不会与摄入抢配额。"""

HANDLED_KINDS = frozenset(
    {
        TaskKind.PROBE,
        TaskKind.PARSE,
        TaskKind.CHUNK,
        TaskKind.EMBED,
    }
)
"""摄入链路的任务类型：都靠 ``document_id`` 跑同一个 ``ingest``。"""

DOCUMENT_KINDS = HANDLED_KINDS
"""需要 ``document_id`` 的那几种。与数据源拉取区分开——后者没有文档。"""


class TaskWorker:
    """单消费者。多进程/多协程只需各自实例化（``owner`` 不同即可）。"""

    def __init__(
        self,
        stores: StoreBundle,
        ingest: IngestService,
        *,
        owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        maintain: Callable[[], None] | None = None,
        maintain_interval: float = DEFAULT_MAINTAIN_INTERVAL,
        sync_source: Callable[[str], object] | None = None,
        compile_wiki: Callable[[str], object] | None = None,
        summarize_gap: Callable[[], object] | None = None,
        run_scheduled: Callable[[str], object] | None = None,
        due_schedules: Callable[[], object] | None = None,
        schedule_interval: float = DEFAULT_SCHEDULE_INTERVAL,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("租约时长必须为正")
        self._stores = stores
        self._ingest = ingest
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        #: 空闲时的维护动作（清过期幂等键、清过期回收站）。
        #:
        #: 为什么挂在 worker 上而不是单起一个定时任务：worker 本来就常驻、本来就在
        #: 轮询，顺手做一次的成本接近零；而多一个后台任务就多一处生命周期要管
        #: （启动、关停、异常隔离），为一个"每天跑一次"的清理不值得。
        #: 传 None 表示不做维护——测试与只跑单任务的场景用得上。
        self._maintain = maintain
        self._maintain_interval = maintain_interval
        # 数据源拉取的执行体（M6 / T6.1）。可选：没有它时 FETCH_SOURCE 任务会
        # 明确失败，而不是被静默丢掉——静默丢掉会让"拉取一直没反应"极难排查
        self._sync_source = sync_source
        # Wiki 重建（v24）。同样是可选回调：没接上时 WIKI 任务明确失败，
        # 而不是被静默丢掉
        self._compile_wiki = compile_wiki
        # 补文档摘要（v25）。同样是可选回调：不接上就没有这个动作，
        # 而不是"静默什么都不做"——它由组合根显式传入。
        self._summarize_gap = summarize_gap
        #: 定时任务的**执行体**（v0.33）：到这个 id 去问一趟。
        self._run_scheduled = run_scheduled
        #: 定时任务的**调度体**：扫一遍到点的、认领并放进队列（见 ``_schedule_loop``）。
        self._due_schedules = due_schedules
        self._schedule_interval = schedule_interval
        self._last_schedule = 0.0
        self._summary_interval = DEFAULT_SUMMARY_INTERVAL
        self._last_summary = 0.0
        self._last_maintain = 0.0
        # 租约回收的节奏（v24 补）：**必须比维护间隔短得多**。维护是"清会长大的表"，
        # 一小时一次没问题；而租约回收是"把被崩溃/重启打断的任务放回队列"——
        # 一小时一次意味着队列要卡整整一小时（实测卡了 8 小时，因为压根没人调）。
        # 默认跟着租约时长走：租约过期多久，就该在多久内被发现。
        self._reclaim_interval = float(lease_seconds)
        self._last_reclaim = 0.0
        # 入库看门狗的节奏：与租约回收**分开**（见 `_watchdog_loop`），一秒都别共用节拍
        self._watchdog_interval = DEFAULT_WATCHDOG_INTERVAL
        self._last_watchdog = 0.0
        self._current_task_id: str | None = None
        self._thread: asyncio.Task[None] | None = None
        #: 租约已易主的标志。见 ``_mark_lease_lost`` 的说明：它必须活在 worker 级，
        #: 不能只在心跳循环里的局部变量上。
        self._lease_lost = asyncio.Event()

    @property
    def owner(self) -> str:
        return self._owner

    def _mark_lease_lost(self, task_id: str, stopping: asyncio.Event | None = None) -> None:
        """记下"租约已不属于自己"，让消费与心跳两个循环都收工。

        **为什么信号必须落在 worker 自己身上**：``run_forever(stop=...)`` 允许调用方
        传入自己的事件对象（应用生命周期就是这么用的），那种情况下"置位 stopping"
        只是在别人的事件上打标记；而"是否丢过租约"这件事是 worker 的性质，不该随
        某个函数的局部变量消失。两个循环各自读同一个标志，谁先发现都能叫停对方。

        ``stopping`` 有值时顺手一并置位：``TaskGroup`` 要等**所有**子任务返回才结束，
        只让消费循环退出的话，``run_forever`` 还得再等一个心跳周期。
        """
        if not self._lease_lost.is_set():
            logger.warning("任务 %s 的租约已被回收，本 worker 停止领取新任务", task_id)
        self._lease_lost.set()
        if stopping is not None:
            stopping.set()

    # ------------------------------------------------------------------ 主循环

    async def run_once(self) -> bool:
        """处理一个任务；没有可领任务返回 False（便于测试与优雅退出）。"""
        if self._lease_lost.is_set():
            # 已经丢过租约：不再领新活。手上那份的收尾由 run_forever 负责
            return False

        task = self._stores.meta.claim_task(owner=self._owner, lease_seconds=self._lease_seconds)
        if task is None:
            return False

        logger.info("领取任务 %s（%s，第 %d 次）", task.id, task.kind.value, task.attempts)
        try:
            await self._execute_with_heartbeat(task)
        except IngestCanceled:
            # 用户叫停不是故障：收成 canceled，别进重试链（重试只会再跑一遍用户不要的事）
            self._finish_canceled(task)
        except Exception as exc:
            self._retry_or_fail(task, exc)
        else:
            if self._stores.meta.finish_task(task.id, TaskState.SUCCEEDED, owner=self._owner):
                logger.info("任务 %s 完成", task.id)
            else:
                # 租约已经不在自己手上：这一份的成败不归我写，等新主人按自己的节奏收尾。
                #
                # 这里**必须**记成"租约丢失"而不是只打一条日志：任务跑得比一个心跳周期
                # 还快时（摄入是本地线程，几十毫秒就能完事），心跳循环还没轮到看一眼，
                # ``_current_task_id`` 就已经被清空，它永远发现不了租约易主。
                # 实测这条路径会让"租约被抢后停手"变成时序抽奖：慢机器上心跳先到就过，
                # 快机器上必然不停手。信号的兜底点就在这里。
                self._mark_lease_lost(task.id)
        return True


    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """持续消费，直到 ``stop`` 被置位、租约被抢走，或者自己被取消。

        消费循环与续租循环同属一个 ``TaskGroup``，但要注意 ``TaskGroup`` 的语义：
        **子任务正常返回不会取消它的兄弟**，它会一直等下去。所以租约被抢时
        续租循环必须自己把 ``stopping`` 置位，否则消费循环会毫无察觉地继续领新任务。

        退出时还要**把手上的活收完**：``await asyncio.to_thread(...)`` 被取消
        **不会**让线程里的摄入停下来，它会继续写库——"循环停了"不等于"文档写完了"。
        收尾在 ``_execute_with_heartbeat`` 里做，这里的 ``finally`` 只是兜底。
        """
        stopping = stop or asyncio.Event()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._consume_loop(stopping))
                group.create_task(self._beat_loop(stopping))
                group.create_task(self._schedule_loop(stopping))
                group.create_task(self._reclaim_loop(stopping))
                group.create_task(self._watchdog_loop(stopping))
        finally:
            await self._drain_thread()

    async def _consume_loop(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set() and not self._lease_lost.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                # TaskGroup 会把异常当"要收工"处理，所以偶发故障必须在这里兜住，
                # 否则一次存储抖动就把整个消费者带走了
                logger.exception("worker 循环出现未预期异常，继续运行")
                worked = False
            if not worked:
                # 维护仍然只在空闲时跑：它是"清会长大的表"，没必要和摄入抢 IO
                self._run_maintenance()
                # 补摘要也用空闲时间，但节奏快得多（见 DEFAULT_SUMMARY_INTERVAL）
                self._fill_summaries()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=self._poll_interval)

    async def _reclaim_loop(self, stopping: asyncio.Event) -> None:
        """按租约时长周期回收过期任务，**独立成一条循环**。

        为什么不能挂在消费循环里（v24 的两版修法，实机各踩了一次）：

        1. 最先它只在"没活可干"的分支里跑 —— 而队列里只要一直有活，那个分支就永远
           进不去，"消费者忙着长任务、另一个消费者崩了"这种最需要回收的情形恰恰永不回收；
        2. 改到每轮开头跑之后，仍然不对：**一轮就是一份文档**（解析加出题几分钟到
           几十分钟），于是"回收间隔"实际等于"一份文档的耗时"。实机验证时那个僵尸任务
           带着刚过期 90 秒的租约一直挂着——界面写着"疑似卡住"，而回收要等当前这篇跑完。

        第三条路就是它：回收的节奏只由租约时长决定，与手上有没有活无关。
        多一条协程的成本可以忽略，而它做的是一条带条件的 UPDATE（幂等）。

        启动时先回收一次：上一次进程崩溃/被 kill 时手上的任务还写着 ``running``，
        不回收就永远不会被重新领取（文档也永远停在中间阶段）。
        """
        while not stopping.is_set() and not self._lease_lost.is_set():
            self._reclaim_expired(force=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._reclaim_interval)

    async def _watchdog_loop(self, stopping: asyncio.Event) -> None:
        """入库看门狗：把"任务已经没了、文档却停在半路"的那些捞回来（P0）。

        **为什么要单起一条循环**：与 `_reclaim_loop` / `_schedule_loop` 同一条理由——
        它做的事与手上有没有活**无关**。挂在空闲分支上的话，队列一忙它就永远不跑，
        而"卡住"恰恰最可能发生在队列很忙的时候（那份文件就是在长任务间隙里被丢下的）。

        它与租约回收是**两件不同的事**，别指望后者兜住：

        - 租约回收管的是"任务还写着 running、租约过期了"（进程崩了、被 kill 了）；
        - 看门狗管的是"任务已经收场（成功/失败/重试次数用尽）但文档阶段停在中间"
          ——实测那三份素材就是后者：停在 `embedding` 15~30 分钟、`error` 为空、
          没有待领任务，界面只会说"还在处理"。

        异常一律吞掉：看门狗失败不该带走消费者，下一轮再扫一遍是免费的。
        """
        while not stopping.is_set() and not self._lease_lost.is_set():
            self._sweep_stalled()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._watchdog_interval)

    def _sweep_stalled(self) -> None:
        """扫一轮。判定与动作都在 `ingest.sweep_stalled`（纯函数，可单测）。"""
        try:
            outcome = sweep_stalled(self._stores)
        except Exception:
            logger.exception("入库看门狗这一轮失败，跳过（下一轮再试）")
            return
        if outcome.touched:
            logger.info(
                "入库看门狗这一轮：重排 %d 份、判失败 %d 份",
                len(outcome.requeued),
                len(outcome.failed),
            )

    async def _schedule_loop(self, stopping: asyncio.Event) -> None:
        """到点就把定时任务放进队列，**独立成一条循环**（v0.33）。

        与 ``_reclaim_loop`` 同一条理由：它做的事（"这条到点了没有"）与手上
        有没有活干**无关**。挂在空闲分支上的话，一个长时间摄入就能让
        "每天 9 点"变成"9 点之后的某个时刻，取决于当时队列忙不忙"。

        它只做**入队**（一条 SQL + 一次 CAS），不跑问答——真正的执行在消费者那侧，
        于是"跑得慢"不会把调度也堵住。异常一律吞掉：调度失败不该带走消费者
        （下一轮还会再扫一遍，那时重试是免费的）。
        """
        while not stopping.is_set() and not self._lease_lost.is_set():
            self._enqueue_due_schedules(force=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=self._schedule_interval)

    def _enqueue_due_schedules(self, *, force: bool = False) -> None:
        if self._due_schedules is None:
            return
        now = time.monotonic()
        if not force and now - self._last_schedule < self._schedule_interval:
            return
        self._last_schedule = now
        try:
            count = self._due_schedules()
        except Exception:
            logger.warning("扫描定时任务失败，跳过本轮", exc_info=True)
            return
        if count:
            logger.info("定时任务入队 %d 条", count)


    def _reclaim_expired(self, *, force: bool = False) -> None:
        """把租约过期、还写着 ``running`` 的任务回收掉（回到队列或判失败）。

        **这是断点续跑的兜底那一环**：架构 §4 承诺"进程崩溃后超时任务回到 PENDING"，
        而它过去只写在 ``MetaStore.reclaim_expired_tasks`` 与文档里，**生产代码里没有
        任何调用者**——于是任何被中断的任务都永远停在 running：既不会被重试，
        也不会失败，文档就永远钉在 parsing/chunking 上。实测有一篇这样挂了 8 小时。

        **节奏由 ``_reclaim_loop`` 决定，与忙闲无关**（那条理由——"别和摄入抢写锁"——
        来自 SQLite 单写者的年代，到了 PG 就是一条带条件的 UPDATE）。
        ``force`` 表示跳过这里的节流：调用方自己负责节奏（回收循环与启动那一次）。
        失败一律吞掉——回收失败不该带走消费者。
        """
        now = time.monotonic()
        if not force and now - self._last_reclaim < self._reclaim_interval:
            return
        self._last_reclaim = now
        try:
            reclaimed = self._stores.meta.reclaim_expired_tasks()
        except Exception:
            logger.warning("回收过期租约失败，跳过本轮", exc_info=True)
            return
        if reclaimed:
            logger.info("回收过期租约 %d 条（已放回队列或判失败）", reclaimed)

    def _fill_summaries(self) -> None:
        """空闲时给缺摘要的文档补摘要（一小批，可失败）。

        与 ``_run_maintenance`` 的三点一致：只在空闲分支跑、按自己的间隔节流、
        失败一律吞掉（补摘要失败不该带走消费者）。
        """
        if self._summarize_gap is None:
            return
        now = time.monotonic()
        if now - self._last_summary < self._summary_interval:
            return
        self._last_summary = now
        try:
            self._summarize_gap()
        except Exception:
            logger.warning("补文档摘要失败，跳过本轮", exc_info=True)

    def _run_maintenance(self) -> None:
        """空闲时按间隔跑一次维护。

        **只在拿到"没活可干"这一支里调用**：维护要写库，和摄入抢同一个 SQLite
        没有意义；而且失败必须被吞掉——一次清理失败不该带走消费者，
        这正是这个项目在续租路径上已经踩过的教训。
        """
        if self._maintain is None:
            return
        now = time.monotonic()
        if now - self._last_maintain < self._maintain_interval:
            return
        self._last_maintain = now
        try:
            self._maintain()
        except Exception:
            logger.warning("空闲维护失败，跳过本轮", exc_info=True)

    # ------------------------------------------------------------------ 单任务执行

    async def _execute_with_heartbeat(self, task: TaskRecord) -> None:
        """把同步摄入丢进线程池。

        取消到达时先等线程收尾再上抛：``to_thread`` 被取消不会让线程里的摄入停下来，
        不 Handle 的话取消一返回、事件循环一关，那份文档就停在写了一半的状态。
        """
        self._current_task_id = task.id
        self._thread = asyncio.create_task(asyncio.to_thread(self._handle, task))
        try:
            # shield：先不让取消打断线程，交给下面这个 except 里统一收尾
            await asyncio.shield(self._thread)
        except asyncio.CancelledError:
            await self._drain_thread()
            raise
        finally:
            self._current_task_id = None

    async def _drain_thread(self) -> None:
        """等线程池里那份摄入写完（幂等，写完再停比半途丢下安全）。"""
        thread = self._thread
        if thread is None or thread.done():
            return
        logger.info("等待当前任务收尾")
        with contextlib.suppress(Exception):
            await asyncio.shield(thread)

    async def _beat_loop(self, stopping: asyncio.Event) -> None:
        """按租约的三分之一周期续租；租约不再属于自己就置位 ``stopping`` 收工。

        这里**不抛异常**：租约被抢说明另一个消费者在跑同一份任务，而摄入是按已有产物
        续跑的（幂等），让它写完比写一半时硬拽回来更安全。终态写入另有 ``owner``
        条件更新兜底，不会被这次过期的执行结果覆盖。

        **续租失败也不许把消费者带走**：这个循环跑在 ``TaskGroup`` 里，
        异常会一路冒到 ``run_forever``，结果是"存储抖动一次 → 消费者整个死掉"，
        而手上那份摄入还在线程里继续跑。偶发失败只记日志；
        真的连不上库时租约自然过期，由 ``reclaim_expired_tasks`` 兜底回收。
        """
        interval = max(self._lease_seconds / 3, 0.1)
        while not stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=interval)
            if self._lease_lost.is_set():
                return  # 已经在别处判定丢失，别再给一个不属于自己的任务续租
            task_id = self._current_task_id
            if task_id is None:
                continue  # 空闲，没有需要保住的租约
            try:
                still_mine = self._stores.meta.heartbeat_task(
                    task_id, owner=self._owner, lease_seconds=self._lease_seconds
                )
            except Exception:
                logger.exception("续租失败，本轮跳过（租约过期后由回收机制兜底）")
                continue
            if not still_mine:
                self._mark_lease_lost(task_id, stopping)
                return

    def _handle(self, task: TaskRecord) -> None:
        if task.kind is TaskKind.FETCH_SOURCE:
            self._handle_source(task)
            return
        if task.kind is TaskKind.WIKI:
            # 知识库级任务：没有 document_id，也不能落进下面的摄入分支
            if self._compile_wiki is None:
                raise NotImplementedError("Wiki 生成尚未接线")
            kb_id = str(task.payload.get("kb_id") or "")
            if not kb_id:
                raise ValueError(f"任务 {task.id} 缺少 kb_id")
            self._compile_wiki(kb_id)
            return
        if task.kind is TaskKind.SCHEDULED:
            # 定时任务（v0.33）：payload 里是 scheduled_id，没有 document_id。
            # 与其它分支一样**没接线就明确报错**，不静默跳过——跳过会让"到点了
            # 什么都没发生"变成一个查不出原因的现象
            if self._run_scheduled is None:
                raise NotImplementedError("定时任务尚未接线")
            scheduled_id = str(task.payload.get("scheduled_id") or "")
            if not scheduled_id:
                raise ValueError(f"任务 {task.id} 缺少 scheduled_id")
            self._run_scheduled(scheduled_id)
            return
            return
        if task.kind is TaskKind.QUESTIONS:
            # 补出题不走摄入阶段机（文档已 indexed，只读现有块），所以**不能**
            # 落进下面那条 `self._ingest.ingest(...)`——那会按断点续跑规则什么也不做。
            if not task.document_id:
                raise ValueError(f"任务 {task.id} 缺少 document_id")
            self._ingest.generate_questions(task.document_id)
            return
        if task.kind not in DOCUMENT_KINDS:
            raise NotImplementedError(f"任务类型尚未接线：{task.kind.value}")
        if not task.document_id:
            raise ValueError(f"任务 {task.id} 缺少 document_id")
        self._ingest.ingest(task.document_id)

    def _handle_source(self, task: TaskRecord) -> None:
        """拉取一个数据源（M6 / T6.1）。

        **它没有 document_id**，所以不能走上面那条路——这也是当初把
        "数据源动作"单列成一个任务类型的原因（见 TaskKind 的注释）。
        """
        if self._sync_source is None:
            raise NotImplementedError("数据源拉取尚未接线")
        source_id = str(task.payload.get("source_id") or "")
        if not source_id:
            raise ValueError(f"任务 {task.id} 缺少 source_id")
        self._sync_source(source_id)

    # ------------------------------------------------------------------ 重试策略

    def _finish_canceled(self, task: TaskRecord) -> None:
        """把被叫停的任务收成 ``canceled``。

        取不到租约是**正常路径**：取消接口在把文档置为 canceled 的同时就把任务的
        租约清掉了，所以这里多半写不进去——那条日志只是留痕，不是异常。
        """
        if self._stores.meta.finish_task(task.id, TaskState.CANCELED, owner=self._owner):
            logger.info("任务 %s 已取消", task.id)
        else:
            logger.info("任务 %s 已在别处取消，本 worker 不再写入", task.id)

    def _retry_or_fail(self, task: TaskRecord, exc: Exception) -> None:
        message = str(exc)
        stage = getattr(exc, "stage", None)
        detail = f"[{stage}] {message}" if stage else message

        if is_retryable(exc) and task.attempts < task.max_attempts:
            delay = self.backoff_for(task.attempts)
            if not self._stores.meta.reschedule_task(
                task.id,
                owner=self._owner,
                next_run_at=datetime.now(UTC) + timedelta(seconds=delay),
                error=detail,
            ):
                logger.warning("任务 %s 的租约已易主，放弃安排重试", task.id)
                return
            logger.warning(
                "任务 %s 第 %d 次失败，%.1fs 后重试：%s", task.id, task.attempts, delay, detail
            )
            return

        if not self._stores.meta.finish_task(
            task.id, TaskState.FAILED, owner=self._owner, error=detail
        ):
            logger.warning("任务 %s 的租约已易主，放弃写入失败状态", task.id)
            return
        logger.error(
            "任务 %s 判定失败（已尝试 %d 次，可重试=%s）：%s",
            task.id,
            task.attempts,
            is_retryable(exc),
            detail,
        )

    def backoff_for(self, attempts: int) -> float:
        """指数退避：2s、4s、8s…上限 ``max_backoff``（架构 §4 的"指数退避"）。"""
        exponent = max(attempts - 1, 0)
        return float(min(self._max_backoff, self._base_backoff * (2**exponent)))


NON_RETRYABLE = (NotImplementedError, ValueError, NotFoundError)
"""重试没有意义的异常：类型没接线、参数非法、目标不存在——重试一千次结果一样，只会刷日志。

与文档级失败的区别：解析失败/端点抖动属于可重试（``IngestError``、``EmbeddingError``）。
"""


def is_retryable(exc: Exception) -> bool:
    return not isinstance(exc, NON_RETRYABLE)
