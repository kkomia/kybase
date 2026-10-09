"""隐式捕获那一步（§4.1 第②路、§4.4 的回执）。

镜像同构：``app/api/v1/chat.py`` 的 ``_capture_implicit`` / ``_implicit_capture_step``
→ 本文件。

四件事，每一件错了都不报错、只是慢慢不对：

1. **关着就不问**：``memory.capture`` 默认关，服务层在第一道闸就返回 ``None``；
2. **没写进去就不报**：过程面上那一步只在**真的写进档案**之后才出现——
   报一件没发生的事比不报更糟（用户会以为这一轮的内容留下来了）；
3. **两份一起进**：消息里的快照与事件日志各一份、且是同一个 payload，
   否则 P0-2 那条"日志的投影 == 快照"的验收会当场不成立；
4. **自动化通道不写**：API Key 那条路是 MCP 客户端、脚本与自动化测试在说话，
   把它们记进"这个人说过什么"里，等于让机器替人往记忆里写话。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.api.v1.chat import _capture_implicit, _implicit_capture_step
from app.services.api_key import Caller
from app.services.archive import WriteResult
from app.services.memory import CaptureOutcome
from app.services.session_events import KIND_STEP, steps_from_events

_ADDED = WriteResult(
    action="added",
    receipt="记下了：用户要求以后的回答都先给结论。",
    text="用户要求以后的回答都先给结论。",
    section="长期偏好与风格",
)


def _human() -> Caller:
    """管理员网页会话：人在说话，该写。"""
    return Caller(is_admin=True)


def _automated() -> Caller:
    """API Key 通道（MCP / 脚本 / 自动化测试）：机器在说话，不该写。

    ``api_key`` 只需要"非空"这个事实，所以给一个占位对象即可——判据就是它非空。
    """
    return Caller(api_key=SimpleNamespace(id="key_1"))  # type: ignore[arg-type]


class _Sink:
    """只实现这一步用到的两个格子（形状与真的 ``_TurnSink`` 一致）。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []
        self.events: list[Any] = []


def _services(outcome: Any) -> Any:
    """只实现这一步用到的那个入口：``memory.capture_implicit``。"""
    return SimpleNamespace(memory=SimpleNamespace(capture_implicit=outcome))


def _outcome(*results: WriteResult) -> CaptureOutcome:
    return CaptureOutcome(signal="以后", results=tuple(results))


def test_a_turn_without_a_capture_adds_no_step() -> None:
    """``capture_implicit`` 返回 None（关着 / 没信号）时**什么都不加**。"""
    sink = _Sink()
    steps: list[dict[str, object]] = []
    calls: list[str] = []

    def _none(text: str, *, user_id: str | None = None) -> None:
        calls.append(text)
        return None

    _implicit_capture_step(
        _services(_none), "帮我把这段改短", caller=_human(), steps=steps, sink=sink
    )

    assert calls == ["帮我把这段改短"], "仍然问了服务层一次：判定在那儿"
    assert steps == [] and sink.steps == [] and sink.events == []


def test_a_judgement_that_finds_nothing_adds_no_step() -> None:
    """判据说"这轮没有值得写的"（结果为空）时也不加——那是正常路径，不是事件。"""
    sink = _Sink()
    steps: list[dict[str, object]] = []

    _implicit_capture_step(
        _services(lambda text, *, user_id=None: _outcome()),
        "记住：今天先把这段改短",
        caller=_human(),
        steps=steps,
        sink=sink,
    )

    assert steps == [] and sink.steps == [] and sink.events == []


def test_the_step_carries_the_receipt_from_the_same_source() -> None:
    """那一步的 detail **就是** ``WriteResult.receipt``（§4.4：三条路共用一份文案）。

    界面自己另编一句的话，模型从工具听到的、人在变更流里看到的、
    过程面板上冒出来的，就会是三种说法。
    """
    outcome = _outcome(_ADDED)
    seen: list[str] = []

    def _capture(text: str, *, user_id: str | None = None) -> CaptureOutcome:
        seen.append(text)
        return outcome

    sink = _Sink()
    steps: list[dict[str, object]] = []
    _implicit_capture_step(
        _services(_capture), "我以后都要先看结论", caller=_human(), steps=steps, sink=sink
    )

    assert steps and steps[0]["phase"] == "memory"
    assert steps[0]["status"] == "done"
    assert steps[0]["detail"] == _ADDED.receipt


def test_the_step_goes_into_both_the_snapshot_and_the_log() -> None:
    """**两份一起进、且是同一个 payload**：P0-2 的"投影 == 快照"靠这个成立。

    只进其中一处的话那条验收会红，而它只在跑真实对话的端到端用例里才看得见。
    """
    sink = _Sink()
    steps: list[dict[str, object]] = []

    _implicit_capture_step(
        _services(lambda text, *, user_id=None: _outcome(_ADDED)),
        "我以后都要先看结论",
        caller=_human(),
        steps=steps,
        sink=sink,
    )

    assert steps == sink.steps, "两份内容必须一样"
    assert steps_from_events(sink.events) == steps, "日志的投影必须等于快照"
    assert sink.events[0].kind == KIND_STEP


def test_the_two_lists_stay_separate_when_they_are_different_objects() -> None:
    """非 Agent 那条链路上 ``steps`` 与 ``sink.steps`` 不是同一个列表。

    两边都要有：``turn/end`` 的步数读 sink，落库读 ``steps``——只写一边的话，
    要么日志里多一条快照里没有的（验收红），要么反过来（用户看不到）。
    """
    sink = _Sink()
    steps: list[dict[str, object]] = []

    _implicit_capture_step(
        _services(lambda text, *, user_id=None: _outcome(_ADDED)),
        "我以后都要先看结论",
        caller=_human(),
        steps=steps,
        sink=sink,
    )

    assert len(sink.steps) == 1 and len(steps) == 1


def test_an_automated_turn_never_calls_the_judgement() -> None:
    """自动化通道**连判定都不发起**（"自动化请求不入记忆"）。

    这条是"机器不替你往记忆里写话"的落点：API Key 那条路是脚本与 MCP 客户端。
    """
    calls: list[str] = []

    def _capture(text: str, *, user_id: str | None = None) -> CaptureOutcome:
        calls.append(text)
        return _outcome(_ADDED)

    assert _capture_implicit(_services(_capture), "记住这个", caller=_automated()) is None
    assert calls == []


def test_a_human_turn_still_runs_the_judgement() -> None:
    """对照组：人在说话时照旧走一遍——否则这条排除会把整件事一起关掉。"""
    calls: list[str] = []

    def _capture(text: str, *, user_id: str | None = None) -> CaptureOutcome:
        calls.append(text)
        return _outcome(_ADDED)

    outcome = _capture_implicit(_services(_capture), "记住这个", caller=_human())

    assert calls == ["记住这个"]
    assert outcome is not None and outcome.results == (_ADDED,)


def test_a_failing_judgement_is_swallowed_into_a_log() -> None:
    """判定要出网一次：它失败**绝不能影响一次已经成功的问答**（与 webhook 同口径），
    但也绝不静默——日志里有。"""

    def _boom(text: str, *, user_id: str | None = None) -> CaptureOutcome:
        raise RuntimeError("上游 500")

    assert _capture_implicit(_services(_boom), "记住这个", caller=_human()) is None
