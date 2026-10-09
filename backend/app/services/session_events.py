"""会话事件日志的**词表、草稿与投影**（P0-2，开发计划 §12.225）。

**照抄的是 ZCode 的第一条承重设计**（调研报告《Agent-与对话架构对标调研 v0.1》
§2.1「会话 = 只追加事件日志」，该条证据等级为字符串证据）：
``Session{Created,Resumed,Forked,Compacted,ModeChanged}`` /
``Turn{Started,InputReceived,Complete,Error}`` /
``Model{Request,Streaming,Complete,Error}`` /
``ToolCall{Scheduled,Started,Progress,Result,Error}`` 是同一族事件的不同 kind，
会话正文（我们这边是 ``chat_messages.steps``）只是这份日志的一个**投影**。

**为什么值得照抄**：它把「过程面板」「断流续跑」「插话」「压缩」「审计」
从五套各自的补丁变成对同一份日志的读操作。我们此前的 ``steps`` 是**流式当时
拍下的快照**，于是每一处要用它的地方都得自己想办法（续跑要把两轮拼起来、
压缩要另存一份 upto 标记）。日志一旦存在，这些都能现算。

**词的来源与裁剪**：ZCode 的枚举里与我们有关的逐个对应如下
（``CompactBoundary`` / ``CheckpointCreated`` 留给对应的后续项：P1-3 压缩）：

===================  ==========================================  ==============================
kind                 什么时候写                                  抄的是 ZCode 的哪一处
===================  ==========================================  ==============================
``turn/start``       一轮问答开始（问题、模型档、当前模式、       ``Turn{Started,InputReceived}``
                     是不是续跑）
``turn/end``         这一轮收尾（成功 / 降级 / 出错）           ``Turn{Complete,Error}``
``step``             非工具的一步（组织回答那一步）             步骤序列
``tool_call``        一次工具调用；**同一调用两条**：            ``ToolCall{Started}`` +
                     ``running`` 那条是「开始」，``done`` 那条    ``ToolCall{Result,Error}``
                     带 args / result 摘要与产出物
``thinking``         模型的一段连续思考（增量拼成一条）         ``Model{Streaming}``
``error``            这一轮失败的那句话                          ``Model{Error}`` / ``Turn{Error}``
``interrupted``      用户在流式期间停止 / 断开                  QwenPaw 的中断补齐（见下）
``mode/changed``     Agent 模式换了一档（带 previousMode）      ``SessionModeChanged``
``command``          一条斜杠命令被执行（不进模型历史）         DSH「命令执行写 session log」
===================  ==========================================  ==============================

**后两条是 P1-1 与 P1-2 的落点**：``mode/changed`` 抄 ZCode 的 ``SessionModeChanged``
（payload 带 ``previousMode`` 与 ``source``，见 ``mode_changed_draft``）；
``command`` 抄 DSH §2.7 的「命令执行**写 session log** 但不进模型历史」——
所以命令那一笔记在这里，而不是在 ``chat_messages`` 里多一条消息。

**QwenPaw 那条教训**（调研报告 §2.1「中断时给每个未完成 tool_use 伪造结果」）：
中断不补收尾的话，下一轮的消息不成对（OpenAI 兼容端点直接 400）。我们的
``chat_messages`` 只存 user / assistant 两条，不存在 tool 消息配对问题，但那份
**「哪些调用没有结果」**的信息仍然必须留下——它就是 ``interrupted`` 事件的
``unpaired`` 字段，下一轮（或续跑）据此知道哪些动作是半截的。

**只追加**：本模块不提供任何修改 / 删除事件的口子（``MetaStore`` 那边同样只有
append 与 list）。日志要是能被改，「回看当时发生了什么」就不再成立。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.services.agent import StepEvent, StepThinking, step_snapshot

__all__ = [
    "EVENT_KINDS",
    "KIND_COMMAND",
    "KIND_ERROR",
    "KIND_INTERRUPTED",
    "KIND_MODE_CHANGED",
    "KIND_STEP",
    "KIND_THINKING",
    "KIND_TOOL_CALL",
    "KIND_TURN_END",
    "KIND_TURN_START",
    "STEP_KINDS",
    "TURN_DEGRADED",
    "TURN_EMPTY",
    "TURN_ERROR",
    "TURN_OK",
    "TURN_STATUSES",
    "EventDraft",
    "SessionEvent",
    "command_draft",
    "fill_missing_thinking",
    "interrupted_payload",
    "mode_changed_draft",
    "step_event_draft",
    "steps_from_events",
    "steps_per_turn",
    "thinking_draft",
    "turn_end_draft",
    "turn_start_draft",
]

# ---- 词表 -----------------------------------------------------------------
#
# **一处定义**（这条是刻意的，见开发计划 §12.225 的纪律「能抄就抄，不发明」）：
# 端点参数校验、写侧、投影、测试都从这几个常量取，散着写字符串就会出现
# "某处写成 turn_start、某处写成 turn/start"这种只有对不上时才发现的错。

KIND_TURN_START = "turn/start"
KIND_TURN_END = "turn/end"
KIND_STEP = "step"
KIND_TOOL_CALL = "tool_call"
KIND_THINKING = "thinking"
KIND_ERROR = "error"
KIND_INTERRUPTED = "interrupted"
KIND_MODE_CHANGED = "mode/changed"
"""Agent 模式换了一档（P1-1 遗留 #6，抄 ZCode 的 ``SessionModeChanged``）。"""
KIND_COMMAND = "command"
"""一条斜杠命令被执行（P1-2，抄 DSH 的"命令写 session log"）。"""

#: 全部合法的 kind。**顺序即词表顺序**，端点报错时按它列给调用方。
EVENT_KINDS: tuple[str, ...] = (
    KIND_TURN_START,
    KIND_TURN_END,
    KIND_STEP,
    KIND_TOOL_CALL,
    KIND_THINKING,
    KIND_ERROR,
    KIND_INTERRUPTED,
    KIND_MODE_CHANGED,
    KIND_COMMAND,
)

#: 投影成 ``steps`` 时要读的 kind。工具调用也是"一步"——它在快照里与普通步骤
#: 同构（都有 phase/label/status，多一个 tool），所以投影必须把它一起算上。
STEP_KINDS = frozenset({KIND_STEP, KIND_TOOL_CALL})

# ---- turn 的终止原因 ----------------------------------------------------------
#
# 调研报告 §2.1 第 2 条：**终止原因是枚举返回值**，不要用"有没有抛异常"区分
# ——异常分不清"没预算了"与"崩了"，而这两种情况该给用户的下一步完全不同。
# 这四个取值写进 ``turn/end`` 的 ``status``。

TURN_OK = "ok"
"""正常收尾：模型给出了回答，而且这一轮没降级。"""

TURN_DEGRADED = "degraded"
"""**降级收尾**：工具步数或整轮墙钟用尽，按现有信息作答（v0.32 起界面据此给
「继续」入口，判定见 ``services/resume.degraded_reason``）。"""

TURN_ERROR = "error"
"""这一轮在流里失败了（模型调用、检索都算）。"""

TURN_EMPTY = "empty"
"""流跑完了但一个字都没吐（实测：推理模型的思考吃光预算时就是这样）。

它与 ``error`` 分开：没有人报错，模型只是没来得及说。
"""

TURN_STATUSES = (TURN_OK, TURN_DEGRADED, TURN_ERROR, TURN_EMPTY)
"""``turn/end.payload.status`` 的全部取值。"""

#: 「组织回答」这一步的 phase。它在 ``running`` 状态下**也要进快照**
#: （理由见 ``agent.step_snapshot``），投影要跟着同一条取舍。
_ANSWER_PHASE = "answer"

#: 快照里允许出现的键。**与 ``agent.step_snapshot`` 的产出逐一对齐**：
#: 投影出来的 dict 要和那份快照**一模一样**（P0-2 的核心验收），
#: 所以这里不是"白名单"而是同一份契约的另一半。
_SNAPSHOT_KEYS = (
    "phase",
    "label",
    "detail",
    "status",
    "degraded",
    "tool",
    # 语义种类（P2-1）：与 ``tool`` 一样是"界面渲染这一步要用到的东西"。
    # 少写它，投影出来的快照就与 ``agent.step_snapshot`` 差一个键——
    # 那条验收（投影 == 快照）会当场红，这正是这份并列清单存在的意义
    "kind",
    "added",
    "args",
    "result",
    "artifacts",
    # 产生这一步的那一段推理（增量）。它与其它键不同：不是写侧自己算出来的、
    # 而是**调用方算好快照后原样传进 step_event_draft** 的那一份（见那个函数），
    # 所以这里只能原样带走——投影**自己不去判**"这段属于哪一步"，
    # 判两遍（写侧按流切、读侧按事件重放）迟早会在"空回答的那一轮"或
    # "不是工具循环产出的那两步"上分叉，而那种分叉不报错、只是回看时少一段
    "thinking",
)


@dataclass(slots=True)
class EventDraft:
    """一条**尚未落库**的事件：kind + payload。

    为什么要有草稿这一层：``stream`` 那条链路在内存里攒事件（与它一直攒快照
    的做法一致），收尾时才算 ``seq``、才写库。草稿不带 id / seq / conversation_id
    ——那三样是存储层的事（见 ``storage/base.SessionEventRecord``）。

    ``payload`` 特意是可变的：一段连续思考在日志里只占**一条**，后续增量
    拼进同一个 dict。每来一个增量写一行会让长会话的日志膨胀几十倍，
    而那些行在投影里没有任何区别（思考不进 ``steps``）。
    """

    kind: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """读出来的一条事件（服务层形状）。

    **刻意摊开字段而不是把存储层的记录交给调用方**：协议层只该看到服务层给的
    东西（与 ``services/conversation.LastTurn`` 同一条理由——工程规范 §3.3 的 L1
    按 import 的模块名判，``api/`` 里出现 ``app.storage`` 直接红）。
    """

    id: int
    seq: int
    kind: str
    payload: dict[str, object]
    created_at: datetime | None = None


# ---- 写侧的构造 -----------------------------------------------------------------


def turn_start_draft(
    *,
    query: str,
    model_pk: str | None,
    resume_reason: str | None = None,
    mode: str | None = None,
) -> EventDraft:
    """``turn/start``：这一轮要做什么。

    带上 ``query`` 是照 ZCode 的 ``Turn/InputReceived``——日志要能独立回答
    "这一轮是什么问题"，而不是逼读的人去 ``chat_messages`` 里按时间找配对。
    ``resume_reason`` 非空表示这是**接着上一轮做**（续跑那条路）。

    ``mode``（P1-1，v0.44 补）：这一轮是以**哪一档**开跑的。它是 ``ModeWatch``
    判定"档换过了没有"的基线——上一档就是日志里最后一条 ``turn/start`` 的这一个字段
    （见 ``services/commands.ModeWatch``）：会话日志因此自己就能回答
    "这条会话什么时候换的档"，不必另立一张表。
    """
    payload: dict[str, object] = {"query": query}
    if model_pk:
        payload["model_pk"] = model_pk
    if mode:
        payload["mode"] = mode
    if resume_reason:
        payload["resume"] = True
        payload["resume_reason"] = resume_reason
    return EventDraft(kind=KIND_TURN_START, payload=payload)


def mode_changed_draft(*, previous_mode: str, mode: str, source: str) -> EventDraft:
    """``mode/changed``：Agent 模式换了一档（P1-1 遗留 #6）。

    抄的是 ZCode 的 ``SessionModeChanged``：**事件里必须带得动"从哪一档换到哪一档"**，
    否则它只能说明"现在是 yolo"，而回看的人要问的恰恰是"它是**什么时候**开始不问我的"
    （撤销与审计都靠这一条，见调研报告 §2.6 的抄点第 4 条）。

    ``source`` 是**切在哪儿**（``commands.MODE_SOURCE_COMMAND`` / ``..._SETTINGS``）：
    它在会话里当场切的、还是在设置页改完下一轮才被观测到，这两件事对排错完全不同。
    """
    return EventDraft(
        kind=KIND_MODE_CHANGED,
        payload={"previousMode": previous_mode, "mode": mode, "source": source},
    )


def command_draft(*, name: str, args: str = "", result: str = "", ok: bool = True) -> EventDraft:
    """``command``：一条斜杠命令被执行（P1-2）。

    抄 DSH 的"命令执行**写 session log** 但不进模型历史"（调研报告 §2.7）：
    ``chat_messages`` 里**不留**这一轮的痕迹（命令不是一轮问答），但"这个人当时
    敲了 ``/mode yolo``"必须能查得到——它解释了后面那几轮为什么一路不问。

    ``result`` 只存命令回显的那段话（人读的），**不进模型上下文**。
    """
    payload: dict[str, object] = {"name": name, "ok": ok}
    if args:
        payload["args"] = args
    if result:
        payload["result"] = result
    return EventDraft(kind=KIND_COMMAND, payload=payload)


def turn_end_draft(*, status: str, answer_chars: int, steps: int) -> EventDraft:
    """``turn/end``：这一轮怎么结束的。

    ``status`` 取 ``TURN_STATUSES`` 里的一个（``ok`` / ``degraded`` / ``error`` /
    ``empty``），不用"有没有异常"区分（调研报告 §2.1 第 2 条：终止原因要是枚举返回值）。
    """
    return EventDraft(
        kind=KIND_TURN_END,
        payload={"status": status, "answer_chars": answer_chars, "steps": steps},
    )


def step_event_draft(event: StepEvent, snapshot: dict[str, object] | None = None) -> EventDraft:
    """把一个步骤事件翻成事件草稿（``step`` / ``tool_call`` 二选一）。

    **payload 就是那份快照本身**（走 ``agent.step_snapshot``，绝不另写一版映射）——
    于是"从事件重建快照"退化成"筛掉不该进快照的那几条、其余原样拼起来"，
    两处口径不可能分叉。

    ``snapshot``：调用方**已经算过**那份快照时原样传进来（``_TurnSink.feed`` 就是）。
    同一条事件在一条链路上只该算一次——第二遍算会**第二次取走**"自上一处快照以来的
    推理"（见 ``agent.StepThinking``），于是消息里那份带上思考、日志里那份没有，
    而"投影 == 快照"那条硬验收只在端到端用例里才看得见它红。不给就自己算
    （测试与其它调用点照旧）。

    ``running`` 的那条（``step_snapshot`` 返回 ``None``）在这里**要留下**：
    ``step_snapshot`` 的取舍是"快照里不存没结论的步骤"，而日志要记的是
    "这件事开始过"——中断时"哪些调用没有结果"正是从这些 running 里数出来的。
    """
    if snapshot is None:
        snapshot = step_snapshot(event)
    if snapshot is not None:
        return EventDraft(kind=_kind_of(event), payload=snapshot)
    payload: dict[str, object] = {
        "phase": event.phase,
        "label": event.label,
        "status": event.status,
    }
    if event.tool:
        payload["tool"] = event.tool
    return EventDraft(kind=_kind_of(event), payload=payload)


def _kind_of(event: StepEvent) -> str:
    """带 ``tool`` 的一步是工具调用（``tool_call``），其余是普通步骤（``step``）。

    ZCode 把 ``ToolCall{...}`` 与步骤序列分成两族事件，我们**不另写两条**：
    这里的一步就是一次工具调用（``tool_loop`` 的 running / done 一对，正是
    DSH 的 ``tool/call`` + ``tool/result`` 事件对，靠 ``seq`` 关联），
    再写一条重复的只会让日志翻倍、投影还要去重。
    """
    return KIND_TOOL_CALL if event.tool else KIND_STEP


def thinking_draft() -> EventDraft:
    """开一段 ``thinking``：返回的草稿由调用方把后续增量拼进它的 payload。"""
    return EventDraft(kind=KIND_THINKING, payload={"text": ""})


def interrupted_payload(
    *,
    reason: str,
    unpaired: Sequence[dict[str, object]] = (),
    answer: str = "",
) -> dict[str, object]:
    """中断那一轮要补的信息（QwenPaw 那条教训的落点）。

    ``unpaired``：**已开始、没有结果**的调用（``running`` 有、``done`` 没等到）。
    它回答的是"这一轮停在了半截的哪一步"，也是下一轮成对的前提。
    与 DSH 的 ``interruptedBlocks()`` 同一个用途（保留用户已看到的那部分）。

    ``answer``：用户**已经看到**的那段正文。取消不等于"什么都没发生"——
    回看日志时要能看出当时屏幕上写到了哪。
    """
    payload: dict[str, object] = {"reason": reason, "unpaired": [dict(item) for item in unpaired]}
    if answer:
        payload["answer"] = answer
    return payload


# ---- 读侧的投影 -----------------------------------------------------------------


class _EventLike(Protocol):
    """投影只要求两样东西（结构化类型，所以 ``EventDraft`` 与 ``SessionEvent`` 都满足）。"""

    kind: str
    payload: dict[str, object]


def steps_from_events(events: Iterable[_EventLike]) -> list[dict[str, object]]:
    """把一段事件**投影**成 ``chat_messages.steps`` 那份快照（P0-2 的核心）。

    抄的是 ZCode 的"读的时候现算"：日志是事实，快照是视图。规则只有两条，
    都能在 ``agent.step_snapshot`` 里找到出处：

    1. 只读 ``step`` / ``tool_call`` 两种 kind（其余 kind 不进过程面板）；
    2. ``running`` 且不是「组织回答」的那条**不进投影**——这正是快照当年的取舍
       （回看时多一行没有结论的步骤没有意义），日志留着它是因为中断要数它。

    传进来的可以是 ``SessionEvent``（读端点）也可以是 ``EventDraft``（写侧自检），
    只要带 ``kind`` / ``payload`` 两个属性即可——**同一段代码同时回答"库里的是不是
    对"和"刚要写的是不是对"**。

    ## 每一步的推理（v0.54）：payload 里有就用，没有才现算

    这一版起快照多了一个 ``thinking`` 键（产生这一步的那一轮推理，见
    ``agent.StepThinking``）。**新**日志里它随 step payload 一起落库——那份是写侧
    当时的判定，原样带走（"投影 == 快照"那条验收靠的就是这个）。

    而**老**日志（这一版之前写的）里根本没有这个键：那时整轮只存一串，用户在回看
    那些会话时看到的仍然是"所有思考揉成一团"。所以这里按**同一份规则**现算一遍
    （``StepThinking``）——喂给它的是事件流而不是实况流，判定仍然只有一处。
    两条边界与写侧逐条对齐：``running`` 那条**不取段**（写侧只在真的产出快照时才取），
    以及没有推理时**不写这个键**（空串会让界面多出一块空白）。
    """
    projected: list[dict[str, object]] = []
    pending = StepThinking()
    for event in events:
        if event.kind == KIND_THINKING:
            text = event.payload.get("text")
            if isinstance(text, str):
                pending.note(text)
            continue
        if event.kind not in STEP_KINDS:
            continue
        payload = event.payload
        if payload.get("status") == "running" and payload.get("phase") != _ANSWER_PHASE:
            continue
        step = {key: payload[key] for key in _SNAPSHOT_KEYS if key in payload}
        segment = pending.take()
        if "thinking" not in step and segment.strip():
            step["thinking"] = segment
        projected.append(step)
    return projected


def steps_per_turn(events: Iterable[_EventLike]) -> list[list[dict[str, object]]]:
    """按 ``turn/start … turn/end`` 切开，每轮一份步骤快照（``steps_from_events`` 的上一层）。

    读会话详情时要用它把"事件流"与"消息列表"对上：一轮 = 一条 assistant 消息，
    对不上（条数不同）时调用方**什么都不补**（见 ``fill_missing_thinking``）——
    猜着补会把推理挂到错的那一轮上，而那比不补更难发现。
    """
    turns: list[list[_EventLike]] = []
    current: list[_EventLike] = []
    for event in events:
        if event.kind == KIND_TURN_START:
            current = []
            continue
        if event.kind == KIND_TURN_END:
            if current:
                turns.append(current)
            current = []
            continue
        current.append(event)
    if current:
        # 没有 ``turn/end``（被中断且没收尾）的那一轮也照收：它的步骤是真实发生过的
        turns.append(current)
    return [steps_from_events(turn) for turn in turns]


def fill_missing_thinking(
    steps: Sequence[dict[str, object]], projected: Sequence[dict[str, object]]
) -> list[dict[str, object]]:
    """把**老快照**缺的 ``thinking`` 从投影里按下标补上；对不上就原样返回。

    只补这一个键（其余字段一概以库里那份为准）：这一步要解决的是"老会话回看时思考
    还是揉成一团"，而不是重算历史。条数不同**什么都不做**——那种情况说明这条会话的
    事件与消息对不上（例如某个回答被顶替过），补出来的东西会挂错轮次。
    """
    if len(steps) != len(projected):
        return [dict(step) for step in steps]
    filled: list[dict[str, object]] = []
    # `strict=True`：上面刚判过条数相等，这里再让 zip 自己盯着——将来若有人挪走那条
    # 守卫，这条会当场抛，而不是静默少补几轮
    for step, source in zip(steps, projected, strict=True):
        merged = dict(step)
        if "thinking" not in merged and source.get("thinking"):
            merged["thinking"] = source["thinking"]
        filled.append(merged)
    return filled
