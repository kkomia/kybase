"""后台跑一轮 + 环形缓冲 + 重连补发（P2-2）的单元测试。

镜像同构：``app/services/live_turns.py`` → 本文件。

端到端那几条（断开后继续跑完、重连接着流、收口）在
``tests/integration/api/test_chat_live.py``；这里测的是这一层的三条性质：
**编号接着日志**、**缓冲有界**、**补发按位置切**。
"""

from __future__ import annotations

import time
from queue import Queue

from app.services.live_turns import (
    LIVE_RING_MAX_EVENTS,
    LiveEmit,
    LiveTurnHub,
    is_end,
)


def _turn(hub: LiveTurnHub, conversation_id: str = "conv_1", base_seq: int = 0):  # type: ignore[no-untyped-def]
    return hub.begin(conversation_id, base_seq=base_seq)


def _drain(queue) -> list[object]:  # type: ignore[no-untyped-def]
    items: list[object] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_seq_of_a_logged_event_is_its_session_event_number() -> None:
    """缓冲里的 ``seq`` **就是**会话日志的编号（``base_seq + 第几条``）。

    这是"重连补发"与"事后读日志"是同一件事两半的全部依据：
    ``base_seq`` 由协议层从库里现读（见 ``chat._last_event_seq``），
    所以一条事件的缓冲 seq 与它落库后的 seq 相等——前端拿它当锚点，
    与 ``GET /conversations/{id}/events`` 看到的是同一套位置。
    """
    hub = LiveTurnHub()
    turn = _turn(hub, base_seq=41)
    turn.publish(LiveEmit({"type": "step"}, log_index=1))
    turn.publish(LiveEmit({"type": "sources"}))  # 不落库：没有 seq，但占流位置
    turn.publish(LiveEmit({"type": "step"}, log_index=2))

    events = turn.events
    assert [item.seq for item in events] == [42, None, 43]
    # 流位置是另一条线：每发一条 +1（去重与补发按它切）
    assert [item.index for item in events] == [1, 2, 3]


def test_the_ring_keeps_only_the_most_recent_events() -> None:
    """环形缓冲**有界**：超了就丢最旧的（``LIVE_RING_MAX_EVENTS`` 是那个上限）。

    没有它，一条长会话（每轮几十条 × 几百轮）会把进程内存吃掉——
    而缓冲的全部用途只是"重连时补最近这一段"，更早的那些在日志里。
    """
    hub = LiveTurnHub()
    turn = _turn(hub, base_seq=0)
    for index in range(1, LIVE_RING_MAX_EVENTS + 6):
        turn.publish(LiveEmit({"type": "step"}, log_index=index))

    events = turn.events
    assert len(events) == LIVE_RING_MAX_EVENTS
    assert events[0].seq == 6, "最旧的几条被挤掉了"
    assert events[-1].seq == LIVE_RING_MAX_EVENTS + 5


def test_replay_starts_right_after_the_last_seen_seq() -> None:
    """``after`` 之后补发：**从最后一条 seq ≤ after 的事件之后开始**。

    - 刚连上（``after=0``）：整圈都给它；
    - 看过到某一条：补它之后的——**包括中间那几条没有 seq 的**（出处、审批）：
      它们在客户端那侧可重复（出处是累计列表、审批按 id 认同一条），
      而漏掉它们才是真问题（断在一条待确认上的人回来会两头一起等）。
    """
    hub = LiveTurnHub()
    turn = _turn(hub, base_seq=10)
    turn.publish(LiveEmit({"type": "step"}, log_index=1))  # seq=11
    turn.publish(LiveEmit({"type": "sources"}))  # 无 seq
    turn.publish(LiveEmit({"type": "approval"}))  # 无 seq
    turn.publish(LiveEmit({"type": "step"}, log_index=2))  # seq=12

    fresh, cutoff = turn.replay(0)
    assert [item.index for item in fresh] == [1, 2, 3, 4]
    assert cutoff == 0

    # 看过 seq=11（第一条 step）：补它之后的三条，包括那两条没有 seq 的
    tail, cutoff = turn.replay(11)
    assert [item.index for item in tail] == [2, 3, 4]
    assert cutoff == 1

    # 全都看过了：补发为空
    nothing, cutoff = turn.replay(12)
    assert nothing == [] and cutoff == 4

    # after 比缓冲里的号还小（那一圈被后来的轮次挤掉了）：整圈给它（尽力而为）
    everything, _ = turn.replay(0)
    assert len(everything) == 4


def test_subscribers_get_what_is_published_and_a_sentinel_at_the_end() -> None:
    """订阅者拿到的顺序与发布顺序一致，收尾那条是哨兵（``is_end``）。"""
    hub = LiveTurnHub()
    turn = _turn(hub)
    queue = turn.subscribe()
    turn.publish(LiveEmit({"type": "delta", "text": "甲"}, keep=False))
    turn.publish(LiveEmit({"type": "done", "answer": "甲"}, terminal=True))
    turn.finish()

    items = _drain(queue)
    assert [getattr(item, "payload", None) for item in items[:2]] == [
        {"type": "delta", "text": "甲"},
        {"type": "done", "answer": "甲"},
    ]
    assert is_end(items[2])
    assert turn.finished and turn.terminal == {"type": "done", "answer": "甲"}
    # 退订之后不再收到新的
    turn.unsubscribe(queue)
    turn.publish(LiveEmit({"type": "delta", "text": "乙"}, keep=False))
    assert len(_drain(queue)) == 0


def test_a_slow_subscriber_loses_content_but_never_the_terminal() -> None:
    """慢订阅者：**丢内容、保收尾**（``SUBSCRIBER_QUEUE_MAX`` 那条取舍）。

    收尾那条（``done`` / ``error``）是前端收口的唯一依据：收不到它就永远在转圈。
    所以队列满时先挤掉一条内容，无论如何要把收尾送出去。
    """
    # 队列上限真给 4096：这里换一个只有两格的小队列，等价且不必先塞 4000 条
    turn = LiveTurnHub().begin("conv_slow", base_seq=0)
    queue = Queue(maxsize=2)
    # 直接挂进去：用例要看的就是"队列满了"那条处置（``SUBSCRIBER_QUEUE_MAX``）
    turn._subscribers.append(queue)

    turn.publish(LiveEmit({"type": "delta", "text": "1"}, keep=False))
    turn.publish(LiveEmit({"type": "delta", "text": "2"}, keep=False))
    turn.publish(LiveEmit({"type": "delta", "text": "3"}, keep=False))  # 满了：丢掉
    turn.publish(LiveEmit({"type": "done", "answer": "1"}, terminal=True))

    items = _drain(queue)
    assert [getattr(item, "payload", None) for item in items] == [
        {"type": "delta", "text": "2"},  # 队首那条被收尾挤掉了
        {"type": "done", "answer": "1"},
    ], "收尾那条必须挤进去（前端靠它收口）"


def test_run_runs_the_source_in_the_background_and_finishes() -> None:
    """``run`` 把生产侧搬到**另一个线程**：调用方不等它，``wait`` 才等。

    这一条就是 P2-2 的形状：一轮的生命周期不跟着请求走——请求（调用 ``run``
    之后马上返回）与那一轮（还在后面慢慢跑）是两件事。
    """
    collected: list[int] = []

    def source():  # type: ignore[no-untyped-def]
        for index in range(3):
            collected.append(index)
            time.sleep(0.01)
            yield LiveEmit({"type": "step"}, log_index=index + 1)

    hub = LiveTurnHub()
    turn = _turn(hub)
    started = time.monotonic()
    hub.run(turn, source())
    assert time.monotonic() - started < 0.5, "run 不该等那一轮跑完"

    assert turn.wait(5.0), "wait 要能等到它结束"
    assert collected == [0, 1, 2]
    assert [item.seq for item in turn.events] == [1, 2, 3]
    assert not hub.running("conv_1")


def test_a_failing_source_becomes_one_error_event() -> None:
    """生产侧抛出来的东西**在流内报**（一条 ``error``），而不是把订阅者挂死。"""
    hub = LiveTurnHub()

    def source():  # type: ignore[no-untyped-def]
        yield LiveEmit({"type": "step"}, log_index=1)
        raise RuntimeError("模型炸了")

    turn = _turn(hub)
    queue = turn.subscribe()
    hub.run(turn, source())
    assert turn.wait(5.0)

    items = _drain(queue)
    assert getattr(items[-2], "payload", {}).get("type") == "error"  # type: ignore[union-attr]
    assert "模型炸了" in getattr(items[-2], "payload", {}).get("message", "")  # type: ignore[union-attr]
    assert is_end(items[-1])


def test_the_hub_evicts_finished_turns_first() -> None:
    """超量时先淘汰**跑完的**（还在跑的那些被踢掉，重连就找不到它了）。"""
    hub = LiveTurnHub(max_conversations=2)
    first = _turn(hub, "conv_a")
    second = _turn(hub, "conv_b")
    first.finish()
    third = _turn(hub, "conv_c")

    assert hub.current("conv_a") is None, "跑完的最先被淘汰"
    assert hub.current("conv_b") is second
    assert hub.current("conv_c") is third


def test_finished_turns_expire_after_the_keep_window() -> None:
    """跑完的轮次过了保留窗口就不在了（``LIVE_FINISHED_KEEP_SECONDS``）。

    时钟是注入的：真等十分钟的用例没人会跑，而被测的判据只有"过期了没有"。
    """
    now = [1000.0]
    hub = LiveTurnHub(finished_keep=10.0, clock=lambda: now[0])
    turn = _turn(hub)
    turn.finish()
    assert hub.current("conv_1") is turn

    now[0] += 11.0
    assert hub.current("conv_1") is None
