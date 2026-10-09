"""把一条定时任务跑成一轮问答（v0.33）。

与对话页那条链路的**唯一**区别是"谁在问、结果去哪儿"：这边没有 HTTP 请求、
没有 SSE 订阅者，问题来自调度记录，答案落进那条任务的会话里。除此之外
——工具表、执行器、预算、降级标记、过程快照——全部走同一套服务层实现，
不另开一条"后台简化版"链路（那样两条链路迟早答得不一样，而差异只在半夜出现）。

**没有 SSE，所以事件在这里直接收掉**：攒正文、攒思考、攒过程快照、攒出处，
收法与 ``api/v1/chat.py`` 的 ``_TurnSink`` 一致（快照那一份已提到服务层的
``agent.step_snapshot``，就是为了不让两处漂开）。

**不沉淀记忆**（对话页那边会）：定时任务的每一轮都是"机器自己问自己"，
把它当成用户说过的话记进长期记忆，会让"我们之前怎么说的"里混进一堆
系统巡检式的自问自答。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from app.core.caller import Caller
from app.core.exceptions import NotFoundError
from app.services.agent import (
    DeltaEvent,
    SourcesEvent,
    StepEvent,
    StepThinking,
    ThinkingEvent,
    step_snapshot,
)
from app.services.schedules import ScheduleService
from app.storage.base import ScheduledTaskRecord

if TYPE_CHECKING:
    # **只在类型检查时导入**：``app.core.services`` 的组合根要 import 本模块
    # （它把"跑一条定时任务"接成 worker 的回调），运行时导入就成环。
    from app.core.services import Services

__all__ = ["run_scheduled_task"]

logger = logging.getLogger(__name__)

#: 失败时写进 ``last_error`` 的字符上限。错误信息要能看，但不该把一整段堆栈
#: 塞进一行记录（界面上那一列是单行的）。
MAX_ERROR_CHARS = 500


def run_scheduled_task(services: Services, scheduled_id: str) -> str:
    """跑一次。返回回答的正文（进任务日志），失败时抛出去让队列按策略重试。

    收尾一定发生：**成功、降级、失败三种结局都会写回 ``last_status``**——
    界面上的"上次跑成没跑成"正是靠它，漏写一种就会让用户看到一条停在
    "还在跑"的任务（而它早就不跑了）。
    """
    schedules: ScheduleService = services.schedules
    record = schedules.get_for_worker(scheduled_id)
    caller = _caller_for(services, record.owner_id)
    try:
        conversation_id = _conversation_for(services, record, caller)
        turn = _ask(services, record, caller, conversation_id)
    except Exception as exc:
        schedules.finish(
            scheduled_id, status="failed", error=str(exc)[:MAX_ERROR_CHARS], conversation_id=None
        )
        raise

    answer = turn.answer
    if not answer.strip():
        # 与对话页同一口径：**没有正文就不落库**（半截记录会让回看的人以为它答了空的）
        schedules.finish(
            scheduled_id,
            status="failed",
            error="这一轮没有产出正文（模型可能报错或预算耗尽）",
            conversation_id=conversation_id,
        )
        raise RuntimeError("定时任务没有产出正文")

    try:
        _record_turn(services, conversation_id, record, turn)
    except Exception:
        # 落库失败不该让"已经跑完的一轮"变成失败：回答已经在手上，
        # 而重试会再花一次钱。记日志即可（与 `_record_turn` 的取舍一致）。
        logger.exception("定时任务的问答落库失败：%s", conversation_id)

    if turn.degraded_reason:
        schedules.finish(
            scheduled_id,
            status="degraded",
            error=turn.degraded_reason,
            conversation_id=conversation_id,
        )
    else:
        schedules.finish(scheduled_id, status="ok", conversation_id=conversation_id)
    return answer


@dataclasses.dataclass(slots=True)
class _Turn:
    """一轮问答收下来的东西（与 ``_TurnSink`` 的四样一一对应）。"""

    answer: str = ""
    sources: list = dataclasses.field(default_factory=list)
    steps: list[dict[str, object]] = dataclasses.field(default_factory=list)
    thinking: list[str] = dataclasses.field(default_factory=list)
    #: 推理的**切段**累加器（见 ``agent.StepThinking``）：与上面那份整轮全文分开，
    #: 它每挂到一处步骤快照上就取空——定时任务的会话里"思考对应到哪一步"
    #: 与对话页是同一条规则（同一份 ``step_snapshot``）
    step_thinking: StepThinking = dataclasses.field(default_factory=StepThinking)
    degraded_reason: str = ""


def _ask(
    services: Services, record: ScheduledTaskRecord, caller: Caller, conversation_id: str
) -> _Turn:
    """跑这一轮（Agent 工作流，或它被关掉时的"检索 + 回答"）。"""
    # 工具表与执行器在**函数里** import：``agent_tools`` → ``tools`` → ``core.services``，
    # 而 ``core.services`` 要 import 本模块（它把这里接成 worker 回调）——
    # 顶层导入就成环。放在这里只多一次模块查找（模块已缓存）。
    from app.services.agent_tools import build_runner, tool_specs

    chat = services.chat
    kb_ids = list(record.kb_ids)
    history, summary = _context(services, conversation_id, record)
    turn = _Turn()

    if not services.runtime.get_bool("chat.agent_enabled", default=True):
        # Agent 工作流被关掉时**尊重这个设置**：那条链路（检索 + 生成）也能回答，
        # 只是不会用工具。定时任务没有理由绕过用户的这个开关。
        sources = chat.retrieve_sources(query=record.prompt, kb_ids=kb_ids or None, top_k=None)
        turn.sources = list(sources)
        for delta in chat.answer_stream(
            query=record.prompt,
            sources=sources,
            history=history,
            summary=summary,
            model_pk=record.model_pk,
            thinking=record.thinking,
            thinking_effort=record.thinking_effort,
            owner_id=record.owner_id,
        ):
            turn.answer += delta
        return turn

    loop = chat.tool_loop(
        model_pk=record.model_pk,
        thinking=record.thinking,
        thinking_effort=record.thinking_effort,
        tools=tool_specs(services, owner_id=record.owner_id, kb_ids=kb_ids),
        runner=build_runner(
            services,
            caller,
            kb_ids=kb_ids,
            conversation_id=conversation_id,
            subagent=lambda task: chat.run_subagent_text(
                question=task,
                kb_ids=kb_ids,
                model_pk=record.model_pk,
                thinking=record.thinking,
                thinking_effort=record.thinking_effort,
            ),
        ),
    )
    for event in loop.run(
        messages=chat.agent_messages(
            query=record.prompt,
            history=history,
            summary=summary,
            kb_ids=kb_ids,
            skill_names=None,
            model_pk=record.model_pk,
            owner_id=record.owner_id,
        )
    ):
        if isinstance(event, DeltaEvent):
            turn.answer += event.text
        elif isinstance(event, ThinkingEvent):
            turn.thinking.append(event.text)
            # 同一段推理也喂给切段那个累加器（与对话页同一处置，见
            # ``api/v1/chat.py`` 的 ``_TurnSink.feed``）：攒到下一处快照被取走
            turn.step_thinking.note(event.text)
        elif isinstance(event, SourcesEvent):
            turn.sources = list(event.sources)
        elif isinstance(event, StepEvent):
            # 把"产生这一步的那一轮推理"挂到快照上：取段只有 ``step_snapshot``
            # 那一处判定，两处各写一份必然分叉
            snapshot = step_snapshot(event, thinking=turn.step_thinking)
            if snapshot is not None:
                turn.steps.append(snapshot)
            if event.degraded:
                # 降级原因就是那一步的 ``detail``（"本轮时间已用尽，已用 312 秒"），
                # 界面直接显示它——不在这里再编一句措辞
                turn.degraded_reason = event.detail or event.label
    return turn


def _context(
    services: Services, conversation_id: str, record: ScheduledTaskRecord
) -> tuple[list, str]:
    """这次要带给模型的历史与摘要。

    **带上历史**：同一条定时任务的每次运行都落在同一条会话里，于是第二轮能看到
    第一轮问了什么、答了什么（"上周那份也一起看一下"这种话才成立）。
    压缩走 ``prepare_context``（与对话页同一条），失败时退回最近若干条——
    上下文准备失败不该让一次到点的运行整个失败。
    """
    try:
        prepared = services.chat.prepare_context(
            conversation_id=conversation_id, query=record.prompt, model_pk=record.model_pk
        )
        return prepared.history, prepared.summary
    except Exception:
        logger.warning("定时任务的上下文准备失败，退回最近历史：%s", conversation_id, exc_info=True)
        return services.conversations.history(conversation_id), ""


def _conversation_for(services: Services, record: ScheduledTaskRecord, caller: Caller) -> str:
    """这条任务的结果落在哪条会话里。**首次运行时才建**。

    会话被用户删掉时**重新建一条**（``conversation_id`` 的那个外键是
    ``ON DELETE SET NULL``，所以这里会看到空值）：删一条会话的意思是"这段记录
    我不要了"，不是"这个定时任务取消"——后者要用停用或删除来表达。
    """
    if record.conversation_id:
        try:
            services.conversations.get(record.conversation_id)
            return record.conversation_id
        except NotFoundError:
            logger.info("定时任务 %s 的会话已被删除，重新建一条", record.id)
    created = services.conversations.create(
        title=record.name,
        kb_ids=list(record.kb_ids),
        owner_id=record.owner_id,
        # 模型档位跟着任务走：这样用户之后从这条会话点「继续」时用的是同一档
        model_pk=record.model_pk,
        thinking=record.thinking,
        thinking_effort=record.thinking_effort,
    )
    return created.id


def _record_turn(
    services: Services, conversation_id: str, record: ScheduledTaskRecord, turn: _Turn
) -> None:
    """把这一轮写进会话——**形状与对话页落库完全一致**（含过程快照）。

    标题不在这里动：它建会话时就已经是任务名了（``ensure_title`` 只在标题为空时
    才写，而这里不是空）——用问题当标题的话，第二次运行会把标题改成另一句话。
    """
    services.conversations.append(conversation_id, role="user", content=record.prompt)
    services.conversations.append(
        conversation_id,
        role="assistant",
        content=turn.answer,
        # 出处存快照：事后重查会得到不同的结果，引用编号就对不上了
        sources=[dataclasses.asdict(item) for item in turn.sources],
        steps=list(turn.steps),
        thinking="".join(turn.thinking),
    )


def _caller_for(services: Services, owner_id: str | None) -> Caller:
    """这条任务以**谁的身份**跑。

    任务有主（普通成员建的）就按那个账号跑：知识库范围、会话归属、
    执行权限都该和他自己提问时一致。无主（管理员建的）按共享桶跑——
    与"管理员会话的 owner_id 是 None"同一口径。
    账号被删了则退回共享桶并留一条日志：任务还在、人没了，
    让它继续跑比让它静默失败更合理。
    """
    if owner_id is None:
        return Caller(is_admin=True)
    try:
        return Caller(is_admin=False, user=services.kb.users.get(owner_id))
    except NotFoundError:
        logger.warning("定时任务的主账号已不存在（%s），按共享桶跑", owner_id)
        return Caller(is_admin=True)
