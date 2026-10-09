"""后台跑一轮 + 按会话的环形缓冲 + 重连重放（P2-2 的后半，开发计划 §12.225）。

**照抄的是谁**（《Agent-与对话架构对标调研 v0.1》§2.1 与 §2.5）：

- ZCode 的长连接处置：空闲 30 秒断开 + 可重试错误白名单 + **重连锚点**
  （``stream_recovery_anchor_*``）——断流之后按锚点续，而不是把那一轮作废；
- QwenPaw 那条：SSE + **后台 run** + **环形缓冲 reconnect 重放**。

两家说的是同一件事：**一轮的生命周期不该绑在那条 TCP 连接上**。KYLAB 此前
（v0.41 之前）客户端一断开，Starlette 就 close 掉那个生成器，`GeneratorExit`
一路穿到 ``_events``，那一轮当场没了——用户看到的是"回答写到一半没了"，
而模型那边一切正常，报错也指不到真正的病因。v0.41 的 ``useLiveTurn`` 只解决了
"前端别主动断"这一半；这里补上后端那一半。

三个部件，缺任何一个这条链都不成立：

1. **后台线程跑那一轮**（``LiveTurnHub.run``）：请求生命周期与那一轮解耦。
   客户端断开只是"少了一个订阅者"，不是"取消那一轮"——取消只有一个入口：
   用户点 ``/stop``（见 ``commands.TurnControl``，那条路照旧）。
2. **按会话的环形缓冲**（``LiveTurn``）：最近 ``LIVE_RING_MAX_EVENTS`` 条事件
   留在内存里，重连时按 ``after`` 补发。
3. **补发 + 接着流**（``LiveTurn.replay`` 与 ``LiveTurn.subscribe``）：
   重连的客户端先拿到自己没看到的那几条，然后**挂到同一个后台任务上**继续收——
   若那一轮已经跑完，补发完给一条 ``done`` 收口（见 ``api/v1/chat._live_stream``）。

**seq 与 P0-2 的会话日志是同一套编号**：缓冲里每一条都带着它在
``session_events`` 里的 ``seq``（由 ``api/v1/chat`` 在开一轮时从库里读出
"这条会话现在最大的 seq" 作种子，见 ``LiveTurnHub.begin`` 的 ``base_seq``），
所以"重连补发"与"事后读日志"读的是同一套位置。**不落库的那几条**
（出处、审批、收尾）没有 seq：它们只活在流里，补发时按位置一并带上。

**为什么 seq 要种子**：落库的 seq 由数据库在写那个事务里算（``max(seq)+1``，
见 ``storage/postgres_impl/meta_store._insert_events``）——库外任何一方自己定号，
下次并发写就会撞上唯一约束。所以这里只能"**从库里的最大号接着往下数**"，
而种子必须在开一轮时现读（``/mode`` 与各种斜杠命令会在会话里插事件，
拿进程内上一次的水位当种子迟早会算错一个号）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from queue import Empty, Full, Queue

__all__ = [
    "LIVE_FINISHED_KEEP_SECONDS",
    "LIVE_MAX_CONVERSATIONS",
    "LIVE_RING_MAX_EVENTS",
    "SUBSCRIBER_QUEUE_MAX",
    "LiveEmit",
    "LiveEvent",
    "LiveTurn",
    "LiveTurnHub",
    "is_end",
    "reset_shared_hub",
    "shared_hub",
]

logger = logging.getLogger(__name__)

#: 环形缓冲保留的**条数**（按会话）。
#:
#: 240 的来由：一轮对话的事件在几十条量级（``tool_loop`` 每步两条 + 思考 + 首尾），
#: 而 P0-2 的端到端用例里一轮最长的那种（30 步跑满）也就 70 条上下。
#: 取 240 是"最长的一轮 ×3"：重连的客户端不管在这一轮里断在第几条，
#: 那一条都还在缓冲里（缓冲被后来的轮次挤掉的只有更早的历史）。
#: 内存量级：一条事件连同它的 payload（args / result 各自已被 ``tool_loop``
#: 截到 2000 字）大约 1~2KB，240 条 ≈ 0.5MB/会话。
LIVE_RING_MAX_EVENTS = 240

#: 缓冲最多留几条会话（LRU 淘汰）。0.5MB × 16 ≈ 8MB 上限——够"几个标签页 + 几个
#: 后台会话"，而且**跑完的轮次本身也会过期**（见 ``LIVE_FINISHED_KEEP_SECONDS``）。
LIVE_MAX_CONVERSATIONS = 16

#: 一轮跑完之后，缓冲**还留多久**（秒）。
#:
#: 为什么不是"跑完就丢"：前端收口那一下与"跑完"之间必然有间隔（人在切页、
#: 网络在抖、最后一个包的往返）。10 分钟是"泡杯茶回来还接得上"的量级；
#: 再久就该去看会话消息了（那条路是持久的，见 ``/conversations/{id}``）。
LIVE_FINISHED_KEEP_SECONDS = 600.0

#: 单个订阅者队列的上限（条）。慢客户端不至于把内存拖垮：满了就丢**内容事件**
#: （``delta``），而收尾那条（``done`` / ``error``）会顶掉最旧的一条挤进去——
#: 收不到 done 的客户端会一直等下去，那比丢几个字严重得多。
SUBSCRIBER_QUEUE_MAX = 4096

#: 订阅者队列里的收尾哨兵（后台那一轮结束了）。**别直接比它**：用 ``is_end``。
_END = object()


def is_end(item: object) -> bool:
    """队列里取出来的这一条是不是"这一轮结束了"那个哨兵。

    哨兵本身是私有的：消费侧只该问"结束了没有"，不该去认一个对象常量
    （认错了会表现成"流凭空断掉"，而那与"跑完了"长得一模一样）。
    """
    return item is _END


@dataclass(slots=True)
class LiveEmit:
    """**生产侧**要发的一条：SSE 载荷 + 它与会话日志的关系。

    这一层是 ``_TurnSink`` 与 hub 之间的契约，只有三个字段，但每一个都不能省：

    - ``payload``：摊给客户端的 SSE 载荷（``{"type": "step", ...}`` 那一形状）；
    - ``log_index``：它对应**这一轮的第几条会话事件**（1 起；``None`` = 不落库的
      纯流内事件，如正文增量、出处、审批）。hub 用它算 seq（``base_seq + log_index``）；
    - ``keep``：要不要进环形缓冲。正文增量与思考增量的后续部分**不进**——
      正文由 ``done`` 的全文兜底（它本来就是"便于前端兜底"那个设计），
      思考则由第一条的 payload 累加（见 ``LiveTurn.publish`` 的合并）。
      每条增量都留在缓冲里的话，一段 2000 字的回答就能把整圈挤掉。
    - ``terminal``：这条是**这一轮的最后一条**（``done`` / ``error``）。
      hub 记下它，重连时据此收口。
    """

    payload: dict[str, object]
    log_index: int | None = None
    keep: bool = True
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """**缓冲里/队列里**的一条。

    ``index`` 是**这一轮里**的流位置（1 起，每发一条 +1）：订阅与补发之间的
    那一小段窗口里同一条会被算两次，靠它去重（见 ``api/v1/chat._live_stream``）。
    ``seq`` 是它在会话日志里的编号（``None`` = 不落库的那几条）。
    """

    index: int
    seq: int | None
    payload: dict[str, object]

    @property
    def terminal(self) -> bool:
        """这一条是不是收尾那条（``done`` / ``error``）。

        只在"补发"这一侧用（判断要不要再补一条"已收尾"的 done）：
        生产侧的那份意图带在 ``LiveEmit.terminal`` 上，落进缓冲的是形状本身。
        """
        return self.payload.get("type") in ("done", "error")


class LiveTurn:
    """这一轮：环形缓冲 + 订阅者名单 + 收尾状态。

    **只活在内存里**（与 ``ApprovalRegistry``、``TurnControl`` 同一口径）：
    它是"连接这一侧"的状态，不是会话内容——内容那半边由 P0-2 的事件日志负责持久化。
    进程重启之后本来也没有"在跑的一轮"了。
    """

    def __init__(
        self,
        conversation_id: str,
        *,
        base_seq: int,
        ring: int = LIVE_RING_MAX_EVENTS,
        queue_max: int = SUBSCRIBER_QUEUE_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conversation_id = conversation_id
        #: seq 的种子 = 开这一轮时库里最大的 seq（见模块头"为什么 seq 要种子"）。
        self._base_seq = max(0, int(base_seq))
        self._events: deque[LiveEvent] = deque(maxlen=max(1, int(ring)))
        self._queue_max = max(1, int(queue_max))
        #: 时钟可注入：淘汰与"跑完还留多久"都按它算（用例不必真等十分钟）
        self._clock = clock
        self._index = 0
        self._lock = threading.Lock()
        self._subscribers: list[Queue] = []
        self._done = threading.Event()
        #: 收尾那条的载荷（``done`` / ``error``）；重连收口时把它带回去。
        self.terminal: dict[str, object] | None = None
        self.finished_at: float | None = None
        #: 后台那个线程（``LiveTurnHub.run`` 填）。``wait`` 靠它。
        self._worker: threading.Thread | None = None
        #: 最近一次有动静的时刻（LRU 淘汰用）。
        self.last_active = self._clock()

    # ------------------------------------------------------------------ 状态

    @property
    def base_seq(self) -> int:
        return self._base_seq

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    @property
    def events(self) -> list[LiveEvent]:
        """缓冲里的那些（副本；给用例与调试看，别拿它当同步点）。"""
        with self._lock:
            return list(self._events)

    def wait(self, timeout: float | None = None) -> bool:
        """等这一轮结束（收了收尾哨兵）。返回是否真的结束了。

        给用例用（"让后台那一轮跑完"），也给优雅停机留一个口子——
        没有它，测试只能靠 sleep 猜时间，那正是最脆的一种用例。
        """
        return self._done.wait(timeout)

    # ------------------------------------------------------------------ 生产

    def publish(self, emit: LiveEmit) -> LiveEvent:
        """收下一条，编号、进缓冲、发给所有订阅者；**返回发出去的那条**。

        返回值不是摆设：``_TurnSink`` 拿它累加"这一段思考"的正文
        （连续思考在缓冲里只占一条，见 ``LiveEmit`` 的说明）。
        """
        payload = emit.payload
        with self._lock:
            self._index += 1
            index = self._index
            seq = None if emit.log_index is None else self._base_seq + emit.log_index
            if emit.keep:
                # 缓冲里存的就是生产侧那个 dict：连续思考靠它往后累加正文
                # （见 ``LiveEmit`` 的说明），换成副本那条路就断了
                self._events.append(LiveEvent(index=index, seq=seq, payload=payload))
            if emit.terminal:
                self.terminal = dict(payload)
            self.last_active = self._clock()
            subscribers = list(self._subscribers)
            # 发给订阅者的那份是**当时**的样子：慢订阅者拿到的是快照，
            # 不会因为后面又拼了一段思考而变成"这一段发了两遍"
            fan = LiveEvent(index=index, seq=seq, payload=dict(payload))
        self._fan_out(subscribers, fan)
        return LiveEvent(index=index, seq=seq, payload=payload)

    def _fan_out(self, subscribers: list[Queue], event: LiveEvent) -> None:
        for queue in subscribers:
            self._offer(queue, event)

    def _offer(self, queue: Queue, item: object) -> None:
        """往一个订阅者队列里放一条；满了就**丢内容、保收尾**（见 ``SUBSCRIBER_QUEUE_MAX``）。"""
        try:
            queue.put_nowait(item)
            return
        except Full:
            pass
        terminal = isinstance(item, LiveEvent) and item.terminal
        if not terminal:
            logger.warning("直播订阅者跟不上了，丢一条事件：%s", self.conversation_id)
            return
        try:
            queue.get_nowait()  # 挤掉最旧的一条，收尾必须送出去
            queue.put_nowait(item)
        except (Empty, Full):  # 极端情形：丢一条总比把收尾吞掉好，只记日志
            logger.warning("直播订阅者队列已满，收尾事件没能送出去：%s", self.conversation_id)

    def finish(self) -> None:
        """这一轮结束了（成功、失败或被停）：叫醒所有还挂着的订阅者。"""
        with self._lock:
            if self.finished_at is None:
                self.finished_at = self._clock()
            self.last_active = self._clock()
            subscribers = list(self._subscribers)
        for queue in subscribers:
            self._offer(queue, _END)
        self._done.set()

    # ------------------------------------------------------------------ 消费

    def subscribe(self) -> Queue:
        """登记一个订阅者，返回它自己的队列。

        **先订阅、再补发**（``api/v1/chat._live_stream`` 就是这个顺序）：
        反过来的话，两步之间发出来的事件既不在补发里、也不在队列里，直接丢了。
        代价是那一段窗口里的事件会来两次——由 ``LiveEvent.index`` 去重。
        """
        queue: Queue = Queue(maxsize=self._queue_max)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        # 已经退过（或这一轮被淘汰时清过）就当退过了：这个在 finally 里调，
        # 抛出去只会盖过真正的问题
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    def replay(self, after: int) -> tuple[list[LiveEvent], int]:
        """``after`` 之后该补发的那些，以及**补发到的位置**（``index``）。

        规则是一条：**从"最后一条 seq ≤ after 的事件"之后开始补**。

        - 客户端刚连上（``after=0``）：一条 seq ≤ 0 的都没有，于是整圈都补给它；
        - 客户端看过到 ``seq=N``：补 N 之后的所有事件——**包括中间那几条没有 seq 的**
          （出处、审批）。它们在客户端那一侧是可重复的（出处是累计列表、
          审批按 id 认同一条），而漏掉它们才是真问题：断在一个待确认上的人
          回来时看不到那条确认，两头会一起等到超时；
        - ``after`` 比缓冲里的号还小（那一圈被后来的轮次挤掉了）：整圈给它。
          少了的部分补不出来，客户端该做的是重新读会话——所以补发完之后
          照样给一条收尾说明（见 ``_live_stream``）。
        """
        with self._lock:
            events = list(self._events)
        cut = -1
        for index, event in enumerate(events):
            if event.seq is not None and event.seq <= after:
                cut = index
        if cut < 0:
            return events, 0
        return events[cut + 1 :], events[cut].index


class LiveTurnHub:
    """按会话记着"当前这一轮"（``LiveTurn``），负责起后台线程与淘汰。

    **进程内一张表**（``shared_hub()`` 给的是全局那一张，与 ``ApprovalRegistry``
    同一条理由）：同一轮要能被**另一个请求**（重连那条）找得到，
    那就必须活过单个请求的作用域。
    """

    def __init__(
        self,
        *,
        ring: int = LIVE_RING_MAX_EVENTS,
        max_conversations: int = LIVE_MAX_CONVERSATIONS,
        finished_keep: float = LIVE_FINISHED_KEEP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ring = ring
        self._max_conversations = max(1, int(max_conversations))
        self._finished_keep = max(0.0, float(finished_keep))
        self._clock = clock
        self._turns: OrderedDict[str, LiveTurn] = OrderedDict()
        self._lock = threading.Lock()

    def begin(self, conversation_id: str, *, base_seq: int) -> LiveTurn:
        """开一轮：登记它、返回那个 ``LiveTurn``（调用方接着调 ``run``）。

        **同一条会话再开一轮会顶掉上一轮**（"当前这一轮"只有一条）：上一轮的对象
        还在它自己的线程里跑、它的订阅者也照旧收（订阅是订阅、登记是登记），
        只是重连那条路从此指向新的一轮——这与界面上的行为一致：
        用户在同一个会话里又发了一句，他关心的是新的那句。
        """
        self._sweep()
        with self._lock:
            turn = LiveTurn(
                conversation_id, base_seq=base_seq, ring=self._ring, clock=self._clock
            )
            self._turns[conversation_id] = turn
            self._turns.move_to_end(conversation_id)
            self._evict()
        return turn

    def run(self, turn: LiveTurn, source: Iterator[LiveEmit]) -> LiveTurn:
        """**在后台线程里**把 ``source`` 跑完，逐条发给这个 ``turn``。

        这是 P2-2 的核心那一句：从这一刻起，那一轮不归请求管了。客户端断开
        只是少一个订阅者；取消只有 ``/stop``（协作式，见 ``commands.TurnControl``，
        那一轮自己会在两次事件之间看到并收尾）。

        ``source`` 抛出来的任何东西都在这里翻成一条 ``error`` 事件发给订阅者，
        再 ``finish``：**流内报错**是这个端点的一贯口径（状态码早就发出去了），
        而"后台线程里悄悄炸掉"会让所有订阅者一起挂住。
        """
        def pump() -> None:
            try:
                for emit in source:
                    turn.publish(emit)
            except BaseException as exc:
                # 连 BaseException 一起接：这里的职责只是**原样转成一条事件**，
                # 判断留给上层；漏掉一类会让所有订阅者一起挂住
                logger.exception("后台对话轮次异常：%s", turn.conversation_id)
                if turn.terminal is None:
                    # 不带 ``log_index``：这一刻已经不指望知道日志写到哪了，
                    # 而收尾那条的 seq 本来只是"游标"（见 api/v1/chat 里 done 的说明）
                    turn.publish(
                        LiveEmit(
                            {"type": "error", "message": f"对话失败：{exc}"}, terminal=True
                        )
                    )
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # 收尾失败不该盖过已经发生的那件事（成功或失败）
                        logger.exception("关闭后台对话流失败：%s", turn.conversation_id)
                turn.finish()

        worker = threading.Thread(
            target=pump, name=f"chat-turn-{turn.conversation_id}", daemon=True
        )
        turn._worker = worker
        worker.start()
        return turn

    def current(self, conversation_id: str) -> LiveTurn | None:
        """这条会话当前的那一轮。

        跑完的**还留着**（``LIVE_FINISHED_KEEP_SECONDS`` 之内）：重连的人要先看到
        "它已经收尾了"，这条说明只有它答得出来。
        """
        self._sweep()
        with self._lock:
            turn = self._turns.get(conversation_id)
            if turn is not None:
                self._turns.move_to_end(conversation_id)
        return turn

    def running(self, conversation_id: str) -> bool:
        """这条会话上有没有一轮**还在跑**（界面/脚本查询用）。"""
        turn = self.current(conversation_id)
        return turn is not None and not turn.finished

    def forget(self, conversation_id: str) -> None:
        """忘掉一条会话（删会话时调用；best-effort，留着只是多占一点内存）。"""
        with self._lock:
            self._turns.pop(conversation_id, None)

    def reset(self) -> None:
        """清空（用例用：全局那一张是跨用例的）。"""
        with self._lock:
            self._turns.clear()

    # ------------------------------------------------------------------ 内部

    def _sweep(self) -> None:
        """扫掉过期与超量的条目（调用方**不要**持锁）。"""
        now = self._clock()
        with self._lock:
            stale = [
                key
                for key, turn in self._turns.items()
                if turn.finished
                and turn.finished_at is not None
                and now - turn.finished_at > self._finished_keep
            ]
            for key in stale:
                self._turns.pop(key, None)
            self._evict()

    def _evict(self) -> None:
        """超出上限就淘汰最久没动静的（调用方持锁）。

        **先淘汰跑完的**：还在跑的那些被踢掉，重连就找不到它了（虽然订阅者照旧收），
        而跑完的那些本来就只是"还能补发一次"。
        """
        if len(self._turns) <= self._max_conversations:
            return
        candidates = [key for key, turn in self._turns.items() if turn.finished]
        for key in candidates:
            if len(self._turns) <= self._max_conversations:
                break
            self._turns.pop(key, None)
        # 还是超量（一堆会话同时在跑）：按最久没动静的来
        while len(self._turns) > self._max_conversations:
            self._turns.popitem(last=False)


_hub: LiveTurnHub | None = None
_hub_lock = threading.Lock()


def shared_hub() -> LiveTurnHub:
    """进程内共享的那一张（``get_services()`` 同一条理由：同一轮要被另一个请求找到）。

    刻意不挂进 ``Services``：它记的是"**连接**这半边"的状态（谁能补发、
    谁在订阅），不是业务依赖——业务侧对它的全部要求就是"同一轮能被找回来"。
    """
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = LiveTurnHub()
    return _hub


def reset_shared_hub() -> None:
    """清空共享的那一张（用例的夹具体里调；见 ``tests/integration/api``）。"""
    shared_hub().reset()
