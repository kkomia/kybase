"""定时任务：服务层（v0.33，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.6）。

它回答的是**"到点替我做事"**：每天早晨把昨天的日志汇总、每周一把周报底稿准备好。
在此之前，"到点自动发生的事"只有两种——数据源拉取（RSS/网页）与入库维护，
它们都是**知识库的维护**；而"到点替我跑一次问答"没有对应物。

三处刻意的设计：

1. **结果落进一条会话**：每次运行就是那条会话里的一轮问答，于是"上周它都跑了什么、
   结论是什么"就是翻会话记录——不另造一套"运行历史"的存储与界面。
   会话首次运行时才建（没跑过的任务不该先占一条会话）；
2. **执行走任务队列**（``TaskKind.SCHEDULED``）：调度侧只做"到点把活放进队列"，
   剩下的租约、心跳、失败重试、进程崩溃后回收**全部复用已有那一套**。
   在调度侧自己实现一遍，等于把队列已经解决过的问题再做一次（而且更差）。
    ⚠️ **本机档没有那张队列表**（``TaskQueueRepo`` 属知识库域，见 ``ScheduleRunner``
    的说明）：那一档把"入队 + 消费者认领"换成**就地跑**（``bind_runner`` + ``run_due``），
    而"到点判定 / CAS 认领 / 下一次时刻"两档共用同一段（``_arm``）；
3. **时区按服务器本地时间**：用户说的"每天 9 点"是他钟表上的 9 点。
   容器默认是 UTC，所以部署里显式设了 ``TZ``（见 ``deploy/docker-compose.yml``），
   接口也会把当前时区回给界面显示——"9 点"到底是哪个 9 点，得让人看得见。

**认领是 CAS**（见 ``MetaStore.arm_scheduled_task``）：多个 worker 会同时扫到
同一条到点的任务，而"跑两次"的代价不是重复一次查询，是重复**一整轮问答与工具调用**。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.models.enums import TaskKind, TaskState
from app.services import cron as cron_service
from app.storage.base import ScheduledTaskRecord, StoreBundle, TaskRecord

__all__ = ["KIND_CRON", "KIND_ONCE", "ScheduleRunner", "ScheduleService"]

logger = logging.getLogger(__name__)

KIND_CRON = "cron"
KIND_ONCE = "once"

MAX_NAME_CHARS = 80
MAX_PROMPT_CHARS = 4000

#: 一次扫描最多认领几条。**不设成"一次全跑"**：一个写错的任务（比如每分钟一次）
#: 攒下几百条到点的记录时，一口气全入队会把队列和后端一起压住。
DUE_BATCH = 5

#: 定时任务失败的**重试次数**（任务队列的 ``max_attempts``）。
#:
#: 2 = "上游抖一次还能补上"。为什么不给默认的 5：重复执行会往会话里写第二遍，
#: 而每一遍都是真花钱的一轮问答；而周期性的任务本来就有下一个周期。
#: （失败时不会写会话——落库只发生在拿到回答之后，所以重试不会留下重复的问答对。）
SCHEDULED_MAX_ATTEMPTS = 2


class ScheduleRunner(Protocol):
    """**本机档**的"就地跑一条"接缝（服务器档没有它，走队列）。

    为什么需要它：服务器那半条链是"到点入队（``TaskKind.SCHEDULED``）→ 消费者认领 →
    ``schedule_runner.run_scheduled_task``"（见模块头第 2 条）。而**本机档没有任务表**
    ——``TaskQueueRepo`` 属知识库域，在本机档由 ``RemoteMetaStore`` 整体抛
    （`enqueue_task` / `claim_task` 都不在本机库里，见
    `app/storage/split_impl/remote_meta.py` 的模块头："本机档不启动消费者，没有队列可管"）。
    于是本机档"到点就跑"这条链缺的正是`入队 + 认领`那一环：把它换成一个**直接跑的接缝**，
    调度的"到点判定 / CAS 认领 / 下一次时刻"照旧走本类（一份 cron 逻辑，不抄第二遍）。

    一个方法：``submit`` **立刻返回**一个运行 id，真跑在后台线程里。两条调用路径共用它
    ——"到点自动"（``run_due``）与"立即跑一次"（``run_now``）：两者都不能被几分钟的一轮
    问答按在原处（前者会把调度循环堵住，后者会让界面转圈等到超时），所以接缝这一侧
    **只有"排队"，没有"等它跑完"**。
    """

    def submit(self, scheduled_id: str) -> str:
        """把一条排进本机运行队列，返回这次运行的标识（**不阻塞**）。"""
        ...


class ScheduleService:
    """定时任务的增删改查 + 到点入队。**不跑问答**（那是 ``schedule_runner`` 的事）。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores
        #: 本机档的"就地跑"接缝（组合根后挂一次，见 ``bind_runner``）。服务器档恒 ``None``。
        self._runner: ScheduleRunner | None = None

    def bind_runner(self, runner: ScheduleRunner | None) -> None:
        """挂上"就地跑"接缝（**本机档专属**，服务器档恒不调）。

        手法与 ``RemoteMetaStore.bind_reader`` 逐字相同：后挂一次、只在装配期发生。
        为什么是后挂而不是构造参数——接缝要的是一份**装配好的 ``Services``**
        （工具表、执行器、会话都从它上面取），而它要到组合根末尾才存在
        （组合根里那个 ``runner_slot`` 也记着同一件事）。
        """
        self._runner = runner

    # ------------------------------------------------------------------ 读

    def list(self, *, owner_id: str | None) -> list[ScheduledTaskRecord]:
        """可见的那些。``owner_id=None`` = 管理员/API Key 通道，看全部。"""
        records = self._stores.schedules.list_scheduled_tasks()
        if owner_id is None:
            return records
        return [item for item in records if item.owner_id == owner_id]

    def get(self, scheduled_id: str, *, owner_id: str | None) -> ScheduledTaskRecord:
        """取一条。**越权与不存在都回 404**：403 会暴露"这个 id 存在"。"""
        record = self._stores.schedules.get_scheduled_task(scheduled_id)
        if record is None or (owner_id is not None and record.owner_id != owner_id):
            raise NotFoundError(f"定时任务不存在：{scheduled_id}")
        return record

    def get_for_worker(self, scheduled_id: str) -> ScheduledTaskRecord:
        """worker 那条路取记录：**没有调用者身份可校验**（它由队列驱动）。
        取不到就报错——任务指向一条已被删掉的调度时，worker 该如实失败。"""
        record = self._stores.schedules.get_scheduled_task(scheduled_id)
        if record is None:
            raise NotFoundError(f"定时任务不存在：{scheduled_id}")
        return record

    # ------------------------------------------------------------------ 写

    def create(
        self,
        *,
        name: str,
        prompt: str,
        kind: str,
        cron: str = "",
        run_at: datetime | None = None,
        kb_ids: list[str] | None = None,
        model_pk: str | None = None,
        thinking: bool | None = None,
        thinking_effort: str | None = None,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> ScheduledTaskRecord:
        clean_name = (name or "").strip()
        clean_prompt = (prompt or "").strip()
        if not clean_name:
            raise InvalidRequestError("缺少参数：name（这个定时任务叫什么）")
        if len(clean_name) > MAX_NAME_CHARS:
            raise InvalidRequestError(f"名字最多 {MAX_NAME_CHARS} 字")
        if not clean_prompt:
            raise InvalidRequestError("缺少参数：prompt（到点要问它什么）")
        if len(clean_prompt) > MAX_PROMPT_CHARS:
            raise InvalidRequestError(f"要问的话最多 {MAX_PROMPT_CHARS} 字")
        clean_kind, clean_cron, clean_run_at = self._when(kind, cron=cron, run_at=run_at)

        moment = now or datetime.now(UTC)
        record = ScheduledTaskRecord(
            id=f"sched_{uuid.uuid4().hex[:12]}",
            name=clean_name,
            prompt=clean_prompt,
            kind=clean_kind,
            cron=clean_cron,
            run_at=clean_run_at,
            enabled=True,
            kb_ids=tuple(dict.fromkeys(str(item) for item in (kb_ids or ()))),
            model_pk=model_pk,
            thinking=thinking,
            thinking_effort=thinking_effort,
            owner_id=owner_id,
        )
        record.next_run_at = self._first_run(record, now=moment)
        if record.next_run_at is None:
            # 一次性任务的时间已经过去了：**当场报错**而不是建一条永远不会跑的记录。
            # 后者在界面上表现为"建好了，但一直没动"，而用户会以为是自己没等到点。
            raise InvalidRequestError(
                "这个时间已经过去了。要一次性的任务请给一个将来的时间；"
                "要反复发生的用 cron（例：`0 9 * * *` = 每天 9:00）"
            )
        return self._stores.schedules.create_scheduled_task(record)

    def update(
        self,
        scheduled_id: str,
        *,
        owner_id: str | None,
        name: str | None = None,
        prompt: str | None = None,
        kind: str | None = None,
        cron: str | None = None,
        run_at: datetime | None = None,
        kb_ids: list[str] | None = None,
        enabled: bool | None = None,
        model_pk: str | None = None,
        now: datetime | None = None,
    ) -> ScheduledTaskRecord:
        """改一条。**只改传进来的字段**（``None`` = 不动）。

        改了时间（cron / run_at / kind）就**重算下次运行**：不重算的话，
        "把每天 9 点改成每天 18 点"会等到下一个 9 点才生效，而用户以为已经改好了。
        """
        record = self.get(scheduled_id, owner_id=owner_id)
        moment = now or datetime.now(UTC)
        timing_changed = False
        if name is not None:
            clean = name.strip()
            if not clean or len(clean) > MAX_NAME_CHARS:
                raise InvalidRequestError(f"名字要在 1–{MAX_NAME_CHARS} 字之间")
            record.name = clean
        if prompt is not None:
            clean_prompt = prompt.strip()
            if not clean_prompt or len(clean_prompt) > MAX_PROMPT_CHARS:
                raise InvalidRequestError(f"要问的话要在 1–{MAX_PROMPT_CHARS} 字之间")
            record.prompt = clean_prompt
        if kind is not None or cron is not None or run_at is not None:
            clean_kind, clean_cron, clean_run_at = self._when(
                kind or record.kind,
                cron=cron if cron is not None else record.cron,
                run_at=run_at if run_at is not None else record.run_at,
            )
            record.kind, record.cron, record.run_at = clean_kind, clean_cron, clean_run_at
            timing_changed = True
        if kb_ids is not None:
            record.kb_ids = tuple(dict.fromkeys(str(item) for item in kb_ids))
        if model_pk is not None:
            record.model_pk = model_pk or None
        if enabled is not None:
            record.enabled = enabled
        if timing_changed or enabled is not None:
            record.next_run_at = self._first_run(record, now=moment) if record.enabled else None
        return self._stores.schedules.update_scheduled_task(record)

    def delete(self, scheduled_id: str, *, owner_id: str | None) -> None:
        """删一条。**已经跑出来的会话不删**（与"删工作区不删会话"同一条纪律）。"""
        self.get(scheduled_id, owner_id=owner_id)
        self._stores.schedules.delete_scheduled_task(scheduled_id)

    def set_enabled(
        self, scheduled_id: str, *, owner_id: str | None, enabled: bool, now: datetime | None = None
    ) -> ScheduledTaskRecord:
        """停用 / 启用。停用把 ``next_run_at`` 清空——"停用的任务还挂着一个下次时间"
        会让人以为它还会跑（而它不会）。"""
        record = self.get(scheduled_id, owner_id=owner_id)
        moment = now or datetime.now(UTC)
        record.enabled = enabled
        record.next_run_at = self._first_run(record, now=moment) if enabled else None
        return self._stores.schedules.update_scheduled_task(record)

    # ------------------------------------------------------------------ 调度

    def enqueue_due(self, *, now: datetime | None = None, limit: int = DUE_BATCH) -> int:
        """把到点的任务放进队列，返回放进去了几条。

        每一步的失败都**只影响那一条**：一条调度的认领失败（CAS 输给了别人、
        或者入队时存储抖动）不该让同批里其余几条也不跑。
        """
        moment = now or datetime.now(UTC)
        count = 0
        for record in self._stores.schedules.due_scheduled_tasks(now=moment, limit=limit):
            try:
                if self._arm(record, now=moment):
                    self._enqueue(record, manual=False)
                    count += 1
            except Exception:
                logger.warning("定时任务 %s 入队失败，跳过", record.id, exc_info=True)
        return count

    def run_due(self, *, now: datetime | None = None, limit: int = DUE_BATCH) -> int:
        """**本机档**那条"到点就跑"：扫一遍、认领、交给接缝直接跑，返回跑了几条。

        与 ``enqueue_due`` 是同一件事的两半（到点的判定、CAS 认领、下一次时刻**共用**
        ``_arm`` 那一段），差别只在认领之后干什么：服务器档把活放进队列（消费者去领，
        于是"跑得慢"不会把调度也堵住），本机档没有队列，就当场交给 ``ScheduleRunner``
        （它把活丢进后台线程，所以这里**不会**被一轮问答按在原处）。

        没挂接缝时**一条都不跑**（如实回 0）：那是"这台机器没有执行口"的形态，
        静默地把活丢掉比不跑更糟——所以它由调用方（本机消费者）在启动时挂上，
        而不是在这里编一个默认实现。
        """
        runner = self._runner
        if runner is None:
            return 0
        moment = now or datetime.now(UTC)
        count = 0
        for record in self._stores.schedules.due_scheduled_tasks(now=moment, limit=limit):
            try:
                if self._arm(record, now=moment):
                    runner.submit(record.id)
                    count += 1
            except Exception:
                logger.warning("定时任务 %s 排进本机运行队列失败，跳过", record.id, exc_info=True)
        return count

    def run_now(self, scheduled_id: str, *, owner_id: str | None) -> TaskRecord:
        """「立即跑一次」：入队，**不动下次时间**（它是一次手动的，不改变周期）。

        本机档（挂了 ``runner`` 时）改走"就地跑"：交给接缝、立刻返回一个**这次运行的标识**。
        那个 ``TaskRecord`` 不是队列表里的一行（本机没有那张表）——它承载的只是"这一次运行
        叫什么"，而端点回给界面的正是它（``ScheduledTaskRunOut.task_id``）。
        **两种档都不动 ``next_run_at``**：手动跑不改变周期。
        """
        record = self.get(scheduled_id, owner_id=owner_id)
        if self._runner is not None:
            run_id = self._runner.submit(record.id)
            return TaskRecord(
                id=run_id,
                kind=TaskKind.SCHEDULED,
                state=TaskState.RUNNING,
                payload={"scheduled_id": record.id, "manual": True},
                max_attempts=SCHEDULED_MAX_ATTEMPTS,
            )
        return self._enqueue(record, manual=True)

    def _arm(self, record: ScheduledTaskRecord, *, now: datetime) -> bool:
        """认领这条到点的记录：推进 ``next_run_at``（CAS），返回"认领到了没有"。

        **先认领再干活**：反过来的话，认领失败就留下一次白跑（服务器档表现为多一条
        队列任务，本机档表现为多花一轮问答的钱）。两档共用这一段，所以"下一次时刻怎么算"
        只有一处实现。
        """
        if record.kind == KIND_ONCE:
            # 一次性任务：跑过就不再跑（``next_run_at`` 留着不动，界面要显示它是什么时候跑的）
            following, enabled = record.next_run_at, False
        else:
            following, enabled = self._next_cron(record, now=now), True
        if not self._stores.schedules.arm_scheduled_task(
            record.id,
            expected_next_run_at=record.next_run_at,
            next_run_at=following,
            enabled=enabled,
        ):
            logger.debug("定时任务 %s 已被别人认领，本次跳过", record.id)
            return False
        return True

    def _enqueue(self, record: ScheduledTaskRecord, *, manual: bool) -> TaskRecord:
        return self._stores.meta.enqueue_task(
            TaskRecord(
                id=f"task_{uuid.uuid4().hex[:12]}",
                kind=TaskKind.SCHEDULED,
                state=TaskState.PENDING,
                payload={"scheduled_id": record.id, "manual": manual},
                max_attempts=SCHEDULED_MAX_ATTEMPTS,
            )
        )

    def finish(
        self,
        scheduled_id: str,
        *,
        status: str,
        error: str = "",
        conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """记一次运行的结果（``ok`` / ``degraded`` / ``failed``）。"""
        self._stores.schedules.finish_scheduled_run(
            scheduled_id,
            status=status,
            error=error or None,
            last_run_at=now or datetime.now(UTC),
            conversation_id=conversation_id,
        )

    # ------------------------------------------------------------------ 时间

    def next_run_text(self, record: ScheduledTaskRecord) -> str:
        """给界面/工具结果用的一句话（"每天 09:00" 或 "2026-09-21 09:00 一次"）。"""
        if record.kind == KIND_ONCE:
            moment = _local_text(record.run_at)
            return f"{moment} 跑一次" if moment else "（时间未知）"
        return cron_service.describe_cron(record.cron) if record.cron else "（表达式未知）"

    def _first_run(self, record: ScheduledTaskRecord, *, now: datetime) -> datetime | None:
        """这条记录的**下一次**运行时刻（现在之后）。"""
        if record.kind == KIND_ONCE:
            if record.run_at is None or record.run_at <= now:
                return None
            return record.run_at
        return self._next_cron(record, now=now)

    @staticmethod
    def _next_cron(record: ScheduledTaskRecord, *, now: datetime) -> datetime:
        """按**服务器本地时间**算下一个 cron 时刻，返回带时区的时刻。

        ``datetime.now()`` 是本地时间（naive），``cron.next_after`` 按它算；
        算出来的还是本地墙上时间，用 ``astimezone()`` 挂上本地时区——
        库里存的是 ``timestamptz``，两边必须是同一根时间轴上的东西。
        """
        local_now = datetime.now() if now.tzinfo is None else now.astimezone()
        following = cron_service.next_after(record.cron, local_now)
        # **统一折成 UTC 再落库**：库里是 timestamptz，而"按本地时间算出来"的那个时刻
        # 带着本地偏移（+08:00）。两种表示在库里是同一时刻，但接口回出去的字符串形状
        # 会随路径不同（建的时候回 +08:00、列表回 Z），前端"两次读到的不是同一个值"
        # 这类现象就是这么来的（用例抓到的）
        return (following if following.tzinfo else following.astimezone()).astimezone(UTC)

    @staticmethod
    def _when(kind: str, *, cron: str, run_at: datetime | None) -> tuple[str, str, datetime | None]:
        """把 ``kind`` 与两个时间字段校验收成一组（三处写入口共用一份判定）。"""
        clean = (kind or "").strip().lower()
        if clean == KIND_CRON:
            expression = cron_service.validate_cron(cron)
            return KIND_CRON, expression, None
        if clean == KIND_ONCE:
            if run_at is None:
                raise InvalidRequestError("一次性任务要给时间（run_at）")
            # 不带时区的时间按**服务器本地时间**解释：界面上的 datetime-local
            # 就是这种形状（用户填的是他看到的钟点）
            moment = run_at if run_at.tzinfo is not None else run_at.astimezone()
            return KIND_ONCE, "", moment.astimezone(UTC)
        raise InvalidRequestError(f"kind 只能是 {KIND_CRON} 或 {KIND_ONCE}，收到：{kind!r}")


def timezone_name() -> str:
    """当前的服务器时区名（界面显示"按 XX 时间"用）。

    ``tzname()`` 在容器里可能是 ``UTC``，在配了 TZ 的机器上是 ``CST``——
    两者都不够具体，所以连带 ``utcoffset`` 一起给出去（``UTC+08:00`` 这种读法
    比一个可能引起歧义的缩写强）。
    """
    moment = datetime.now().astimezone()
    offset = moment.utcoffset()
    if offset is None:
        return moment.tzname() or "本地时间"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{moment.tzname() or '本地'} UTC{sign}{hours:02d}:{minutes:02d}"


def _local_text(moment: datetime | None) -> str:
    if moment is None:
        return ""
    local = moment.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")
