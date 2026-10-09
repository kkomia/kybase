"""每一步快照带上"产生它的那一轮推理"（``steps[].thinking``）。

镜像同构：``app/services/agent.py`` 的 ``StepThinking`` / ``step_snapshot``
+ ``app/api/v1/chat.py`` 的 ``_TurnSink.feed`` + ``app/services/schedule_runner.py``
的 ``_ask`` 收集循环 → 本文件。

要钉住的是**边界与归属**，不是"能带上"：

1. 值是**增量**（自上一处快照以来那一段），不是整串——否则二十一步各挂一份整串，
   那条消息的 JSON 大二十倍，界面把同一段话念二十遍（用户报的就是这个：
   "他是把所有思考的内容全部放在一起了"）；
2. 没有推理时**不写这个键**（不是写空串）：界面靠"有没有这个键"决定要不要渲染，
   空串会让每一步底下多出一块空白；
3. 一轮里并行的多个调用**只有第一个快照**带得上它：同一段推理挂在三个工具上，
   等于同一份内容被念三遍；
4. 两条链路（实况流与定时任务）**同一条规则**——本文件用同一段事件脚本喂两边，
   直接比对两边收出来的 ``steps``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1.chat import _TurnSink
from app.services.agent import StepEvent, StepThinking, ThinkingEvent, step_snapshot
from app.services.api_key import Caller
from app.services.session_events import step_event_draft, steps_from_events

#: 两轮各一段推理，各调一个工具（用例 1 的脚本）。
#: 每一段都是**好几个增量**：真流里 reasoning 是一片一片来的，
#: 只吐一块的话"攒起来再取走"这件事在用例里根本没发生。
_ROUND_ONE = "先查眼轴的共识。"
_ROUND_TWO = "共识不够，再读那份报告。"


def _two_rounds() -> list[object]:
    """两轮：推理 → 工具 → 推理 → 另一个工具 → 组织回答。"""
    return [
        *(ThinkingEvent(text=piece) for piece in ("先查眼轴", "的共识。")),
        StepEvent(phase="tool", label="检索知识库", tool="search", kind="search", status="running"),
        StepEvent(phase="tool", label="检索知识库", tool="search", kind="search", status="done"),
        *(ThinkingEvent(text=piece) for piece in ("共识不够，", "再读那份报告。")),
        StepEvent(phase="tool", label="读文件", tool="read_file", kind="read", status="running"),
        StepEvent(phase="tool", label="读文件", tool="read_file", kind="read", status="done"),
        StepEvent(phase="answer", label="组织回答", status="running"),
    ]


def _feed(sink: _TurnSink, events: list[object]) -> None:
    """把事件喂进 sink（``feed`` 是生成器，必须消费掉才会走到那一步的处理）。"""
    for event in events:
        list(sink.feed(event))


def test_each_step_carries_the_reasoning_of_its_own_round() -> None:
    """两个轮次各吐一段推理、各调一个工具：两个快照各自带**自己那一段**。"""
    sink = _TurnSink()
    _feed(sink, _two_rounds())

    tools = [step for step in sink.steps if step.get("tool")]
    assert [step.get("thinking") for step in tools] == [_ROUND_ONE, _ROUND_TWO], (
        "第一段推理该挂在第一次工具调用上、第二段该挂在第二次上（不能是同一串）"
    )
    # 增量而不是整串：所有快照上的推理加起来**正好等于**整串一次，
    # 多一个字就说明某一段被挂了第二遍（二十一步时那就是二十份整串）
    total = sum(len(str(step["thinking"])) for step in sink.steps if "thinking" in step)
    assert total == len(_ROUND_ONE + _ROUND_TWO)


def test_a_parallel_batch_hangs_its_reasoning_on_the_first_snapshot_only() -> None:
    """一轮里并行两个工具：只有第一个快照带那一段（其余的不带）。

    两个 ``running`` 先发、两个 ``done`` 后发（``tool_loop._perform`` 的发法），
    而 ``running`` 不产生快照——于是"这一轮的第一处快照"就是第一个 ``done``。
    """
    sink = _TurnSink()
    _feed(
        sink,
        [
            ThinkingEvent(text="这两页互不依赖，一起抓。"),
            StepEvent(phase="tool", label="联网搜索", tool="web_search", kind="search"),
            StepEvent(phase="tool", label="读文件", tool="read_file", kind="read"),
        ],
    )

    assert [step.get("thinking") for step in sink.steps] == ["这两页互不依赖，一起抓。", None]


def test_no_reasoning_means_no_key_at_all() -> None:
    """没开思考（或模型没吐 reasoning）：快照里**没有**这个键，不是空串。

    空串会让界面多渲染一块空白——而"缺省即空"在读取侧是一句话的事。
    """
    sink = _TurnSink()
    _feed(
        sink,
        [
            StepEvent(phase="tool", label="检索知识库", tool="search", kind="search"),
            StepEvent(phase="answer", label="组织回答", status="running"),
        ],
    )

    assert sink.steps, "这条用例得先有快照，否则它测了个空"
    assert all("thinking" not in step for step in sink.steps)


def test_a_blank_segment_is_not_a_segment() -> None:
    """只有空白的推理不落键：它渲染出来就是一块空白（与"没有推理"同一个结果）。

    但**照旧要被取走**——不然它会跟到下一步去，那一步就凭白多出一块空白。
    """
    sink = _TurnSink()
    _feed(
        sink,
        [
            ThinkingEvent(text="\n\n"),
            StepEvent(phase="tool", label="检索知识库", tool="search"),
            ThinkingEvent(text="真的要查。"),
            StepEvent(phase="tool", label="读文件", tool="read_file"),
        ],
    )

    assert [step.get("thinking") for step in sink.steps] == [None, "真的要查。"]


def test_the_whole_turn_thinking_is_still_the_whole_string() -> None:
    """顶层那份 ``thinking`` 仍是**整串**：既有行为不动（老会话与其它读它的地方照旧）。

    它是另一个累加器，跨轮一直拼；切段那份（``step_thinking``）每挂上一处快照就取空。
    两个合成一个的话，要么顶层只剩最后一段，要么每一步都变成整串。
    """
    sink = _TurnSink()
    _feed(sink, _two_rounds())

    assert "".join(sink.thinking) == _ROUND_ONE + _ROUND_TWO


def test_the_live_step_payload_carries_the_segment_once() -> None:
    """直播那条 ``step`` 帧**带上这一步的推理**，而且**一段只带一次**（v0.54）。

    为什么直播也要带（原先是"契约只说快照、直播不带"）：这一批解决的正是
    "正在跑的那一轮里思考全挤成一团"。只给快照的话，用户答完看到的过程面板里
    仍然没有每步推理，得刷新页面才出现——而那一刻正是他要看的那一眼。

    为什么不是"每个增量都发"：那样一次长思考会让这条流白扛几十 KB；
    这里发的是**已经攒好的一段**（挂在产生它的那一步上，各段加起来等于整串）。
    """
    sink = _TurnSink()
    payloads = [
        emit.payload
        for event in _two_rounds()
        for emit in sink.feed(event)
        if emit.payload.get("type") == "step"
    ]

    assert payloads, "这条用例得先有 step 帧"
    segments = [payload["thinking"] for payload in payloads if "thinking" in payload]
    assert segments == [_ROUND_ONE, _ROUND_TWO], (
        "每一段只挂在产生它的那一步上；多一条说明同一段被发了两次"
    )


def test_the_log_projection_still_equals_the_snapshot() -> None:
    """带推理的快照，日志投影仍与它**逐字相等**（P0-2 的核心验收）。

    投影**自己不去判**"这段属于哪一步"：落进日志的那份 payload 就是消息里那份
    dict（``step_event_draft`` 收到算好的快照就原样用）。让读取侧按事件重放一遍
    同一套切段规则的话，只要有一处对不上（空回答那一轮的边界、不是工具循环产出的
    那两步）就会悄悄分叉——而那条验收只在端到端用例里才看得见红。
    """
    sink = _TurnSink()
    _feed(sink, _two_rounds())

    assert steps_from_events(sink.events) == sink.steps


def test_the_draft_reuses_the_snapshot_it_was_given() -> None:
    """算好的快照传进 ``step_event_draft`` 时**原样使用**，不再算第二遍。

    第二遍算会**第二次取走**推理增量（见 ``StepThinking.take``）：消息里那份带上
    思考、日志里那份没有——两处就差一个键，而它在界面上表现为"回看时思考不见了"。
    """
    thinking = StepThinking()
    thinking.note("先想清楚要查什么。")
    event = StepEvent(phase="tool", label="检索知识库", tool="search")
    snapshot = step_snapshot(event, thinking=thinking)

    assert snapshot is not None
    draft = step_event_draft(event, snapshot)

    assert draft.payload is snapshot, "同一个 dict：日志那份不该是重算出来的复制品"
    assert thinking.take() == "", "已经挂出去的那段不该还能被第二次取走"


def test_the_scheduled_chain_segments_exactly_like_the_live_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定时任务那条链路与对话页**同一条规则**（同一段脚本喂两边，收出来的一样）。

    它没有 SSE，事件在一个循环里直接收掉（``schedule_runner._ask``）。两处各写一份
    "哪段推理挂哪一步"的话，分叉的表现是"定时任务的会话里思考错位"——
    而那种会话只在半夜跑，没人盯着看。
    """
    from app.services.schedule_runner import _ask

    # 工具表与执行器是这条链路真正要去搭的东西（库、沙箱、MCP 都在里面）：
    # 这里要钉的只是"事件怎么收"，把它们换成空实现
    monkeypatch.setattr("app.services.agent_tools.tool_specs", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.services.agent_tools.build_runner", lambda *args, **kwargs: None)

    events = _two_rounds()
    sink = _TurnSink()
    _feed(sink, events)

    turn = _ask(_services(events), _record(), Caller(is_admin=True), "conv_1")

    assert turn.steps == sink.steps
    assert "".join(turn.thinking) == _ROUND_ONE + _ROUND_TWO


class _Loop:
    """假的工具循环：按脚本吐事件（真循环的行为由 ``test_tool_loop`` 钉）。"""

    def __init__(self, events: list[object]) -> None:
        self._events = events

    def run(self, *, messages: list[Any]) -> Any:
        return iter(self._events)


class _Chat:
    """只实现 ``_ask`` 用到的那几件事。"""

    def __init__(self, events: list[object]) -> None:
        self._events = events

    def prepare_context(self, **kwargs: Any) -> Any:
        return SimpleNamespace(history=[], summary="")

    def agent_messages(self, **kwargs: Any) -> list[Any]:
        return []

    def tool_loop(self, **kwargs: Any) -> _Loop:
        return _Loop(self._events)


def _services(events: list[object]) -> Any:
    return SimpleNamespace(
        chat=_Chat(events),
        runtime=SimpleNamespace(get_bool=lambda *args, **kwargs: True),
    )


def _record() -> Any:
    """一条定时任务记录里 ``_ask`` 真正读到的几个字段。"""
    return SimpleNamespace(
        prompt="眼轴的共识是什么",
        kb_ids=[],
        model_pk=None,
        thinking=None,
        thinking_effort=None,
        owner_id=None,
    )
