"""对话里的「要用户点头才执行」：把一次调用挂起，等界面上的那个决定（v0.41）。

**为什么需要这一层**：``ask`` 这一档在端点那条路上是"回 409，界面确认后带
``approved`` 重调一次"——那条路天生有第二次请求可以承载"用户的决定"。
对话不是：模型的一批工具调用走到一半，没有第二个请求能把它接过去。
所以这里补上"停下来等"：执行器登记一条待确认，工具循环把它发到界面上（SSE），
然后**阻塞等人回答**；用户在界面上点的那一下走
``POST /api/v1/chat/approvals/{approval_id}``，从另一个线程把决定交回来。

三个细节不能省，每一个都对应一次会踩到的坑：

1. **先发事件、再阻塞**（见 ``tool_loop._perform``）：等待发生在生成器**被恢复之后**，
   而不是发出询问之前。反过来的话界面根本收不到那个询问，两边一起等死
   （那条死锁的形状记在 ``agent_tools._call_mcp`` 的说明里）。
2. **三方不同线程**：执行器跑在工具循环的线程池里（登记）、等待发生在生成器的线程上
   （阻塞）、决定来自 FastAPI 的另一个请求线程（唤醒）。所以登记表是
   ``threading.Event`` + 一把锁，不是 asyncio 那套。
3. **等不到就按"没批准"处理**，并且**如实告诉模型"对方没有回应"**：
   把超时当成"用户拒绝了"会让模型以为对方看过并否了，它下一轮的措辞就会说错话。

**为什么等待不设成无限**：一轮对话有墙钟闸（``tool_loop.DEFAULT_MAX_SECONDS``），
而"用户走开了"是常态。120 秒是"去泡杯茶回来还来得及、而这一轮不会被它挂死"的量级。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

__all__ = [
    "ALLOW_ALWAYS",
    "ALLOW_ONCE",
    "DECISIONS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DENY",
    "MAX_REASON_CHARS",
    "TIMEOUT",
    "UNAVAILABLE",
    "ApprovalDecision",
    "ApprovalRegistry",
    "ApprovalRequest",
]

logger = logging.getLogger(__name__)

#: 用户在界面上能选的三个决定（也是端点接受的取值）。
ALLOW_ONCE = "allow_once"
ALLOW_ALWAYS = "allow_always"
DENY = "deny"
DECISIONS = (ALLOW_ONCE, ALLOW_ALWAYS, DENY)

#: 拒绝理由的字数上限（P2-1）。**这一句是要进提示词的**，不设上限等于开了一个
#: 从确认条直达模型上下文的输入口。500 字够说清"为什么不行、换哪条路"，
#: 又不至于让用户把一整段需求贴进来（那件事该在主输入框里做）。
MAX_REASON_CHARS = 500

#: 内部取值：**不是用户选的**，是"等不到人"的两种情形。
#:
#: - ``TIMEOUT``：有人在等，但对方一直没回应（到点按拒绝处理）；
#: - ``UNAVAILABLE``：这条链路上根本没有界面（定时任务、脚本），问都没处问。
#:   与"用户拒绝了"分开说，模型才知道下一步该说什么话。
TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"

#: 等待上限（秒）。见模块头最后一段。
DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """一条待确认。字段就是界面上那一行要给用户看的东西。

    ``rule`` 是**「这类都允许」会写下的那行规则**（``Bash(git status:*)``）：
    不预先告诉用户他会往清单里添什么，那个按钮就是一次盲签。
    """

    approval_id: str
    tool: str
    """工具名（目前只有 ``run_command``）。"""
    label: str
    """界面上的标题（「执行命令」）。不在这里做翻译表：那是过程面板的事，见 ``tool_loop``。"""
    args: str
    """参数摘要——**就是用户看到的那一行命令**，也是规则匹配用的那一串。"""
    detail: str = ""
    """再补一句上下文（在什么隔离里跑、断没断网、工作区挂没挂）。"""
    rule: str = ""
    """「这类都允许」会写进放行清单的那行规则。"""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """一个决定的**完整形状**：取值 + 用户捎带的那句话（P2-1）。

    为什么把理由单独装一层而不是拼进取值字符串（``"deny:理由"``）：
    取值是**执行器要判等的枚举**（``agent_exec`` 里一串 ``==``），往它里面塞内容
    迟早会有人写出"没匹配上于是当成没批准"这类错；而理由只是给人/给模型的一句话。
    分开之后，取值这一列永远是三个之一（或 ``timeout`` / ``unavailable``）。
    """

    decision: str
    reason: str = ""
    """拒绝时用户填的那句话（可空）。**空的含义是"就像以前一样"**：
    回给模型的文本一个字不变，所以"不填"这条路上没有任何新增的差异。"""


@dataclass(slots=True)
class _Pending:
    """一条待确认的运行时状态。``event`` 是那句"等人的"——谁决定谁 set。"""

    request: ApprovalRequest
    expires_at: float
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = TIMEOUT
    #: 用户随决定捎带的那句话（P2-1）。见 ``ApprovalDecision.reason``。
    reason: str = ""


class ApprovalRegistry:
    """进程内的待确认登记表。

    **不做持久化**：它只活在"那一轮对话正在跑"的这段时间里，
    进程重启之后那一轮本来也没了（响应流已断），存下来只会得到一条永远等不到人的记录。
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        # 负数按 0 处理（= 不等待）：等待窗口是**调用方给的**，这里只挡住没意义的负数，
        # 不额外抬到一个"看起来更合理"的下限——那会让"我不想等"这种配置悄悄变成"等一秒"
        self._timeout = max(0.0, float(timeout))
        self._items: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def open(
        self,
        *,
        tool: str,
        label: str,
        args: str,
        detail: str = "",
        rule: str = "",
        timeout: float | None = None,
    ) -> ApprovalRequest:
        """登记一条待确认，返回它（执行器随后把它挂在结果上，见 ``agent_exec``）。"""
        seconds = self._timeout if timeout is None else max(0.0, float(timeout))
        request = ApprovalRequest(
            approval_id=uuid.uuid4().hex,
            tool=tool,
            label=label,
            args=args,
            detail=detail,
            rule=rule,
            timeout_seconds=seconds,
        )
        with self._lock:
            # 顺手清掉过期条目：没人来等的那些（见 UNAVAILABLE）不会自己消失，
            # 不清理就会随进程一直长。挂在 open 上而不是起一个后台线程：
            # 这一步本来就在持锁，成本是零。
            self._prune(time.monotonic())
            self._items[request.approval_id] = _Pending(
                request=request, expires_at=time.monotonic() + seconds
            )
        return request

    def wait(self, approval_id: str) -> str:
        """**阻塞**等这条的决定；到点返回 ``TIMEOUT``。

        调用方必须是"能把事件先送出去"的那个线程（生成器线程），
        见模块头第 1 条——从工具线程里调它会死锁。

        只要取值（老调用方），理由走 ``wait_decision``——两条路都只是同一个
        等待窗口的读法，不该各自等一次。
        """
        return self.wait_decision(approval_id).decision

    def wait_decision(self, approval_id: str) -> ApprovalDecision:
        """同上，但把**用户捎带的那句话**一起带回来（P2-1，工具循环用它）。"""
        with self._lock:
            item = self._items.get(approval_id)
        if item is None:
            # 没有这条（id 不对，或者已经被处理掉了）：按没批准处理，不放行
            return ApprovalDecision(TIMEOUT)
        remaining = item.expires_at - time.monotonic()
        if remaining > 0 and not item.event.wait(remaining):
            logger.info(
                "待确认超时（%s 秒）：%s",
                round(item.request.timeout_seconds),
                item.request.args,
            )
        with self._lock:
            self._items.pop(approval_id, None)
        return ApprovalDecision(item.decision, item.reason)

    def decide(self, approval_id: str, decision: str, reason: str = "") -> bool:
        """交一个决定回来；**返回是否真的送到了**。

        已经超时（或已经处理过）时返回 ``False``：端点据此告诉用户"这条已经失效"，
        而不是回一句"已记录"让他以为命令跑了——而那边其实早就按超时处理完了。

        ``reason``（P2-1）：拒绝时用户填的那句给模型的话。它被**压成一行并限长**
        （见 ``MAX_REASON_CHARS``）——它随后会进提示词，而多行文本会让"一句理由"
        在模型的眼里变成好几条指令。
        """
        if decision not in DECISIONS:
            return False
        with self._lock:
            item = self._items.get(approval_id)
            if item is None or time.monotonic() >= item.expires_at:
                return False
            item.decision = decision
            item.reason = _clean_reason(reason)
            item.event.set()
        return True

    def _prune(self, now: float) -> None:
        """清掉已经过期的条目（调用方持锁）。"""
        expired = [key for key, item in self._items.items() if now >= item.expires_at]
        for key in expired:
            self._items.pop(key, None)


def _clean_reason(reason: str) -> str:
    """把用户填的那句话收成一行、限长（P2-1）。

    两件事都不能省：**换行压成空格**（它随后会进提示词，多行会让一句话看起来
    像几条并列的指令）、**超长截断**（见 ``MAX_REASON_CHARS``）。
    空白（"什么都没填"）与没填是同一件事，都归空串。
    """
    text = " ".join(str(reason or "").split())
    return text[:MAX_REASON_CHARS]
