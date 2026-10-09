"""对话模型（LLM）客户端（M6 快速验证版）。

与 embedding 客户端分开，因为它们是**两种模型**：embedding 只把文本变成向量，
不会说话；对话要的是能读上下文并作答的生成模型。所以配置、错误处理、超时都各自一套。

几个来自实测的约束（都踩过）：

- **SiliconFlow 上的 Qwen3.5 是推理模型**：默认会先吐一大段 ``reasoning_content``，
  把 ``max_tokens`` 吃光后 ``content`` 是空的。测试场景要显式传 ``enable_thinking: false``，
  否则表现为"模型没有回答"。
- ``reasoning_content`` 是**独立字段**，不是 content 的一部分；读的时候不能只看 content。
- 流式与非流式共用同一套请求构造，只有 ``stream`` 一个开关不同。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from app.core.exceptions import UpstreamError
from app.core.http import shared_client
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.core.text_hygiene import sanitize_prompt_text
from app.services.thinking import DEFAULT_EFFORT, build_thinking_payload, echoes_reasoning

__all__ = [
    "RETRYABLE_REASONS",
    "STREAM_IDLE_TIMEOUT_SECONDS",
    "ChatError",
    "ChatMessage",
    "LLMConfig",
    "LLMDelta",
    "OpenAICompatChat",
    "TextMarkerFilter",
    "TextToolCalls",
    "ToolCallDelta",
    "assemble_tool_calls",
    "split_text_tool_calls",
    "tidy_text",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120.0
"""比 embedding 宽：生成一段答案比算一次向量慢得多。"""

STREAM_IDLE_TIMEOUT_SECONDS = 30.0
"""流式响应里**两块之间**最多等多久（秒），超了就当场断掉。

抄的是 ZCode 的 ``MODEL_STREAM_IDLE_TIMEOUT``（见《Agent 与对话架构对标调研 v0.1》
§2.1："差 30 秒没事件就断"）。**为什么需要它**：原先只有"整次调用 120 秒"那一道
上限，它管的是"这一趟总共别太久"，管不了"端点在中间卡死"——实测遇到的卡法就是
连接还在、token 也偶尔来一点，最后 120 秒到了才失败，用户盯着一个不动的光标
等了两分钟，而那一刻他手里没有任何可处置的线索。30 秒是"思考再久也不至于这么久"
的量级：推理模型的首字延迟实测在 10 秒内，一旦开始吐 token，块间隔都在毫秒级。

它**不算"整次调用"的长度**：一轮长回答分几十块来，每块都不超过这个间隔就一直是活的，
所以正常的长回答不会被它误伤。
"""

#: 可重试的失败原因（**白名单**）。照 ZCode 的可重试集合：``stream_idle_timeout`` /
#: ``rate_limited`` / ``server_error`` / ``network_error`` / ``timeout``
#: （调研报告 §2.1 第 3 条）。
#:
#: 为什么是白名单而不是黑名单：新出现一种失败时默认落到"不可重试"那一侧——
#: 把 401（密钥错）或参数错误重试一次，只是把同一个错再犯一遍，还白花一次调用；
#: 而漏掉一种本该重试的抖动，代价只是"这次没救回来、用户再点一次"。
RETRYABLE_REASONS = frozenset(
    {"stream_idle_timeout", "rate_limited", "server_error", "network_error", "timeout"}
)

_MAX_ERROR_BODY = 300


class ChatError(UpstreamError):
    """对话调用失败。

    继承 ``UpstreamError`` 而不是自己一套：协议层就不必逐个接口写 try/except，
    统一异常处理器会把它映射成 502 + 可读文案（换 key / 换模型 / 稍后重试）。

    **P2-2 起它多带一个"能不能重试"**：``reason`` 是失败原因（取值见
    ``RETRYABLE_REASONS``），``retryable`` 由它算出来。分类在这一层、重试在上层——
    这一层只知道"这次失败是什么"，不知道该重试几次、还能不能重试（已经吐了一半
    正文的时候重试会把回答写两份），那些是策略，见 ``tool_loop._answer``。
    """

    code = "chat_error"
    message = "对话模型调用失败"

    def __init__(
        self, message: str | None = None, *, reason: str = "", status: int | None = None
    ) -> None:
        super().__init__(message)
        #: 失败原因（分类用，见 ``RETRYABLE_REASONS``）。空串 = 没归类，按不可重试算。
        self.reason = reason
        #: 上游的 HTTP 状态码（连接层失败没有）。**面给用户的文案只认它和 ``reason``**，
        #: 不认 ``message``——那句里带着端点地址与上游返回体，是给日志看的
        #: （见 ``services/failures.py``）。资源在连接层失败时是 ``None``。
        self.status = status

    @property
    def retryable(self) -> bool:
        """这次失败**再试一次有没有意义**（白名单判定，见 ``RETRYABLE_REASONS``）。"""
        return self.reason in RETRYABLE_REASONS


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型要求调用的一次工具。

    ``arguments`` 保留**原始字符串**，不在这里解析成 dict：模型可能给出不完整或
    不合法的 JSON（尤其是流式拼起来时），而"解析失败"要作为一次可上报的工具错误
    回到循环里，而不是在构造消息时就抛。解析放在执行那一步（见 services/tool_loop.py）。
    """

    id: str
    name: str
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """流式响应里一次工具调用的**碎片**（v0.34）。

    同一个 ``index`` 的若干块按到达顺序拼起来才是完整调用：``id`` 与 ``name``
    通常只在第一块里，``arguments`` 是**逐字符切开**的 JSON（一个参数值可能横跨几块）。
    所以单块不能解析，完整调用由 :func:`assemble_tool_calls` 在流结束后拼。
    """

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个工具的定义（发给模型的 JSON Schema）。"""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True, slots=True)
class LLMReply:
    """一次模型回复：正文、它要求的工具调用、以及它的思考。

    三者可能同时非空（模型先说一句再做），所以不是一个"二选一"的联合类型。

    **产出者只有一处**：工具循环那一步（``tool_loop._answer``）把流式增量拼成这个形状
    ——正文与思考是攒出来的字符串，工具调用由 :func:`assemble_tool_calls` 按 ``index``
    拼回完整调用。v0.40 之前这里还有一条非流式产线（``complete_with_tools``），
    合并"选工具"与"作答"两次调用之后就没人用了，已删除。
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()

    reasoning: str = ""
    """模型这一轮的思考（``reasoning_content``），**要原样带回下一轮请求**。

    留它不是因为我们想显示它（显示那条走流式，见 ``LLMDelta.reasoning``），
    而是有些端点在思考模式下**强制要求**：带工具调用的助手消息若不把这个字段
    传回去，下一轮直接 400（实测记录见 ``thinking.ECHO_DIALECTS``）。
    """

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条对话消息。

    ``tool_calls`` / ``tool_call_id`` 是工具循环要的两个附加位：
    助手消息可以"带着一组工具调用"（此时 ``content`` 常为空），
    而 ``role="tool"`` 的结果消息必须用 ``tool_call_id`` 指回是哪一次调用的结果
    ——**少了它，OpenAI 兼容端点会直接 400**（工具结果必须与调用配对）。

    ``reasoning`` 是第三个附加位，**只有工具循环那条路用得到**：它是"这一轮的思考"，
    要随助手消息回到下一轮请求里（见 ``LLMReply.reasoning`` 与
    ``thinking.ECHO_DIALECTS``）。``None`` 表示这条消息不该带这个字段——
    普通消息（用户、系统、工具结果）本来就没有推理，多发一个空字段只是噪声。
    """

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """运行期配置快照（来自设置页）。"""

    base_url: str
    api_key: str
    model_id: str
    temperature: float = 0.3
    max_tokens: int | None = None
    """回复长度上限。**默认不传**（``None`` = 请求里不带这个字段）。

    为什么不设默认值：上限本来就是模型自己的事——不传时端点会一直生成到模型自然收尾
    或撞上它自己的上限（OpenAI 兼容协议就是这么定的，实测 DeepSeek 也是），
    我们再拍一个数字只会引入一类新故障：设小了思考就把预算吃光、正文一个字都出不来
    （2048 时实测过，而且**时好时坏**，因为思考长短随采样波动）。

    只有显式给了值才发出去：给「有些端点不传就退化成很小的默认值」留一条手动出路
    （走模型注册里的 ``options.max_tokens``），而不是把它当成默认。
    """
    enable_thinking: bool = True
    """思考开关，**默认开**（市场上主流模型默认都思考）。

    注意：不同的供应商用不同的字段表示它，由 ``services/thinking.py`` 按方言翻译；
    发错字段的后果是"开关看起来有效、实际没生效"（DeepSeek 就是这样）。
    """
    thinking_effort: str = DEFAULT_EFFORT
    """思考强度，归一化为 low / medium / high；同样交方言层翻译。"""
    thinking_dialect: str | None = None
    """思考方言的显式覆盖（模型 ``options.thinking_dialect``）；空则按地址与模型名自动识别。"""

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.model_id)


@dataclass(frozen=True, slots=True)
class LLMDelta:
    """流式的一块增量。

    **思考与正文分开**：推理模型把思考写在 ``reasoning_content`` 里，它与 ``content``
    是同一条流里的两个字段，可能交替出现。界面要把它们分成两个区域显示
    （思考是过程、正文是结果），所以从这一层就分开，而不是在上层再拆字符串。
    三者都为空（例如只带 ``finish_reason`` 的收尾块）时，这一块直接跳过。
    """

    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCallDelta, ...] = ()
    """这一块里的工具调用碎片（见 ``ToolCallDelta``）；空元组 = 这一块不含工具调用。"""


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """一次调用的 token 用量（G7 用量统计）。

    **供应商不一定会回这个字段**：OpenAI 兼容协议里 ``usage`` 是可选的，
    流式响应更是通常没有。所以各字段允许为 0，界面要能区分
    "真的用了 0 token" 与 "这家没报"——前者不可能，后者是常态。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False
    """这份数字是不是我们自己估的。

    **必须能区分"实测"与"估算"**：向量化接口（OpenAI 兼容的 ``/embeddings``）
    通常根本不返回 usage，我们只能按字符数估。把它当成实测数字展示，
    等于让用户拿一个假精度去做成本判断——那是比不给数字更糟的事。
    """

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_reported(self) -> bool:
        """供应商是否真的报了用量（全 0 视为没报；自己估的不算报）。"""
        return not self.estimated and self.total_tokens > 0


class OpenAICompatChat:
    """``POST {base_url}/chat/completions``，OpenAI 兼容。"""

    #: 上一次 ``complete`` 的 token 用量；供应商没回 ``usage`` 时为 ``None``。
    #:
    #: 用属性而不是改 ``complete`` 的返回值：它的调用方大多只想要文本，
    #: 为了顺手统计用量去改签名会波及每一处调用（G7 只需要在这里记一下）。
    last_usage: LLMUsage | None = None

    def __init__(
        self,
        config: LLMConfig,
        *,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        idle_timeout: float = STREAM_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._client = client
        self._timeout = timeout
        # 空闲上限**可以单独给**（见 ``STREAM_IDLE_TIMEOUT_SECONDS``）：它是流的判据，
        # 与"整次调用多久算太久"是两件事，混成一个数就必然有一头不合适。
        self._idle_timeout = idle_timeout
        # 时钟可注入：用例不必真的等 30 秒（见 tests/unit/services/test_llm.py）
        self._clock = clock

    # ------------------------------------------------------------------ 接口

    def complete(self, messages: Sequence[ChatMessage]) -> str:
        """一次性拿完整回答。"""
        with self._open() as client:
            try:
                response = client.post(
                    f"{self.config.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json={**self._payload(messages), "stream": False},
                    # 超时按调用点给：共享客户端自带的那个只是兜底（见 app/core/http.py）
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                # 连不上 / 中途断开 / 超时都归到**可重试**那一类（见 RETRYABLE_REASONS）。
                # 不包的话它们会以 httpx 的原始异常穿到协议层，被当成 500
                # "服务内部错误"——而它其实是一次外部服务的抖动，处置方式完全不同。
                raise _transport_error(exc) from exc
        body = self._decode(response)
        # 记下这一轮的 token 用量，供调用方取（见 ``last_usage``）。
        # 放在这里而不是让 complete 换返回值：那会改掉所有调用方的签名，
        # 而绝大多数调用方并不关心用量。
        self.last_usage = _usage_of(body)
        return _content_of(body)

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        """流式产出**正文**增量（SSE）。

        逐块 yield，调用方可以直接转给前端做打字机效果——快速验证场景里
        "看着它写"比"等十秒然后一次出现"重要得多。

        思考增量在这里被有意丢掉；要展示思考过程（界面的"过程面板"）用 ``stream_events``。
        """
        for delta in self.stream_events(messages):
            if delta.text:
                yield delta.text

    def stream_events(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None
    ) -> Iterator[LLMDelta]:
        """流式产出增量，**正文与思考分开**（推理模型的 ``reasoning_content`` 单独成块）。

        **可以带工具表**（v0.34）：收尾那次作答也把工具发出去——走到那里不等于
        "模型已经不想用工具了"，而不带工具表时它会**把调用写进正文**
        （实测 DeepSeek Flash：同一个请求带 tools 回结构化 ``tool_calls``，
        去掉 tools 就把 ``<｜｜DSML｜｜ invoke …>`` 写进 content，见
        ``tool_loop._answer``）。带工具时这一路只负责**原样转出碎片**
        （``LLMDelta.tool_calls``），拼装与执行在工具循环那边。

        **一个字正文都没吐出来就报错**，不能静默收尾：静默的后果是上层存下一条空回答，
        用户看到"只有问题、没有回答"，却拿不到任何可处置的线索（实测过：推理模型的
        思考把预算吃光时就是这个现象，而且时好时坏）。非流式那条路一直有这道判断——
        两条路必须一个口径。

        判断的口径：**正文与工具调用都算"产出了"**——只想调工具的那一轮正文是空的，
        那不是空回答；只有思考不算。

        **P2-2 起两道新的判定都在这里**（都不在这层重试，只分类，见 ``ChatError``）：

        - **空闲超时**：两块之间超过 ``STREAM_IDLE_TIMEOUT_SECONDS`` 就当场断掉，
          抛 ``reason="stream_idle_timeout"``。两道保险——HTTP 那一层的读超时
          （``timeout=`` 里的 ``read``）先叫醒真卡住的 socket，下面那个墙钟检查管
          假客户端与"块到了但间隔过长"；少了后者，那些卡法要等到整次调用的 120 秒。
        - **连接层失败**：超时/断开都翻成带分类的 ``ChatError``，而不是让 httpx 的
          原始异常穿上去（到了协议层就成了一句"服务内部错误"）。
        """
        produced = False
        finish_reason = ""
        with (
            self._open() as client,
            client.stream(
                "POST",
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(),
                json={**self._payload(messages, tools), "stream": True},
                # 读超时按**空闲**给：httpx 的 read 超时就是"两次读到数据之间"的上限，
                # 正好是我们要的那条判据（connect/write/pool 仍是 120 秒那只）
                timeout=httpx.Timeout(self._timeout, read=self._idle_timeout),
            ) as response,
        ):
            if response.status_code != 200:
                response.read()
                raise _status_error(response)
            last_seen = self._clock()
            try:
                for line in response.iter_lines():
                    now = self._clock()
                    if now - last_seen >= self._idle_timeout:
                        raise ChatError(
                            _idle_hint(now - last_seen), reason="stream_idle_timeout"
                        )
                    last_seen = now
                    chunk = _chunk_of(line)
                    if chunk is None:
                        continue
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason
                    if chunk.text or chunk.tool_calls:
                        produced = True
                    if chunk.text or chunk.reasoning or chunk.tool_calls:
                        yield LLMDelta(
                            text=chunk.text,
                            reasoning=chunk.reasoning,
                            tool_calls=chunk.tool_calls,
                        )
            except httpx.ReadTimeout as exc:
                # 真卡住时是它先把我们叫醒（见上面的 read 超时）
                raise ChatError(
                    _idle_hint(self._idle_timeout), reason="stream_idle_timeout"
                ) from exc
            except httpx.TransportError as exc:
                raise _transport_error(exc) from exc
        if not produced:
            raise ChatError(_empty_stream_hint(finish_reason), reason="empty_answer")

    # ------------------------------------------------------------------ 内部

    def _open(self) -> httpx.Client:
        """拿一个客户端：注入的优先（测试用），否则是**进程级共享**的那个。

        两者都不在这里关闭——注入的那个属于调用方，共享的那个属于整个进程。
        以前是"没注入就每次新建、`with` 退出时关掉"，那正是每次调用都要重新握手的来源
        （见 ``app/core/http.py``）。所以返回的仍是 ``_ReusedClient`` 这层壳：
        它的 ``__exit__`` 什么都不做。
        """
        return _ReusedClient(self._client or shared_client())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
    ) -> dict:
        # 思考模式下有些端点要求把推理回传（见 thinking.ECHO_DIALECTS）：
        # **只在这次请求开着思考、且方言认这个字段时**才发——思考关着时端点不认这个
        # 要求（实测 200），而给不认识的端点多发字段是有风险的（见 thinking 模块头）。
        echo = self.config.enable_thinking and echoes_reasoning(
            self.config.base_url, self.config.model_id, self.config.thinking_dialect
        )
        payload: dict = {
            "model": self.config.model_id,
            "messages": [_message_wire(m, echo_reasoning=echo) for m in messages],
            "temperature": self.config.temperature,
        }
        # `tools` 只在给了的时候发：不带工具的调用与以前**逐字节一样**
        # （多发一个空数组会被某些端点当成"要求工具调用"而拒答）
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.parameters,
                    },
                }
                for item in tools
            ]
            # auto：由模型自己决定要不要调。**不设 required**——大多数对话没有工具可调，
            # 强制调用会逼它为了"用一次工具"而瞎调一个
            payload["tool_choice"] = "auto"
        # 长度上限**没显式给就不发**：把它交给模型自己（见 ``LLMConfig.max_tokens``）。
        # 发一个我们拍的数字，只会在某些模型上把回复预算掐死在思考阶段
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
        # 思考参数按供应商方言翻译：各家字段名不同，见 services/thinking.py 的模块注释。
        # 只发这家认识的字段——多发一个未知字段会被严格的端点打成 400。
        payload.update(
            build_thinking_payload(
                base_url=self.config.base_url,
                model_id=self.config.model_id,
                enabled=self.config.enable_thinking,
                effort=self.config.thinking_effort,
                dialect=self.config.thinking_dialect,
                max_tokens=self.config.max_tokens,
            )
        )
        return payload

    def _decode(self, response: httpx.Response) -> dict:
        if response.status_code != 200:
            raise _status_error(response)
        try:
            return response.json()
        except ValueError as exc:
            raise ChatError(f"对话端点返回的不是 JSON：{exc}", reason="bad_response") from exc


class _ReusedClient:
    """把外部 client 包成上下文管理器，这样调用方不必区分两种来源。"""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, *_: object) -> None:
        return None


def _usage_of(body: dict) -> LLMUsage | None:
    """从响应体里取 token 用量；没有就返回 ``None``。

    **不因为缺字段而报错**：usage 在 OpenAI 兼容协议里是可选的，
    自建网关与部分国产服务都不回。为了统计把整次对话搞失败是本末倒置。
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        return LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
    except (TypeError, ValueError):
        return None


def _content_of(body: dict) -> str:
    """取回答正文。

    推理模型会把思考写在 ``reasoning_content``；关掉思考后正文在 ``content``。
    两者都空时给出**可处置**的提示，而不是返回一个空字符串让界面发呆。
    """
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatError(
            f"对话响应格式不符合 OpenAI 规范：{exc}", reason="bad_response"
        ) from exc

    content = (message.get("content") or "").strip()
    if content:
        return content

    if (message.get("reasoning_content") or "").strip():
        raise ChatError(
            "模型只返回了思考过程、没有正文：这是推理模型，思考把回复预算用完了。"
            "请在输入框把「深度思考」调低或关掉，或换一个非推理模型",
            reason="thinking_only",
        )
    # 两者都空：**不能**返回空串。返回空串的话上层只会得到一句"没有回答"，
    # 用户看不出是模型没配好、被限流还是提示词太长——所以在这里就给出可处置的原因。
    raise ChatError(
        "模型返回了空正文：常见原因是提示词过长被截断，或该模型不支持当前请求格式。"
        "请到设置 → 对话模型里检查后重试",
        reason="empty_answer",
    )


def _empty_stream_hint(finish_reason: str) -> str:
    """流式一个字正文都没出来时的可处置提示。

    与非流式的 ``_content_of`` 同一口径：给"下一步做什么"，不给"参数非法"。
    """
    if finish_reason == "length":
        return (
            "模型到了长度上限就停住了，正文一个字都没留下——推理模型把预算全花在思考上"
            "就是这个现象。请在输入框把「深度思考」调低或关掉，或换一个非推理模型"
        )
    return (
        "模型没有返回任何正文（可能只返回了思考内容）。请把「深度思考」调低或关掉，"
        "或换一个非推理模型再试"
    )


def _idle_hint(waited: float) -> str:
    """流空闲超时（见 ``STREAM_IDLE_TIMEOUT_SECONDS``）时的可处置提示。

    **把等待时长说出来**：用户那一刻看到的是"写了一半不动了"，他需要知道
    这是我们主动断的、断了多久，而不是"模型还在想"。
    """
    return (
        f"对话流中断：{int(waited)} 秒没有收到任何数据（上限 "
        f"{int(STREAM_IDLE_TIMEOUT_SECONDS)} 秒）。常见原因是网络抖动或端点排队，"
        "可以重试；一直这样请检查接口地址或换个模型"
    )


def _status_reason(status: int) -> str:
    """HTTP 状态码 → 失败原因（取值见 ``RETRYABLE_REASONS``；空串 = 不可重试）。

    分类照 ZCode 的口径：**429（限流）与 5xx（端点自己出错）可重试**，
    401/404/参数错误不可重试——后三者重试一千次结果一样，只会把同一个错再犯一遍。
    408 也算超时那一类（端点自己说"我没等到请求"）。
    """
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "timeout"
    if 500 <= status < 600:
        return "server_error"
    return ""


def _status_error(response: httpx.Response) -> ChatError:
    """非 200 的响应 → **带分类**的 ``ChatError``（文案仍是 ``_error_hint`` 那份）。

    ``status`` 一并带上：401/404 这些给不出 ``reason``（它们不可重试），但**给用户的
    那句话要按状态码挑**（见 ``services/failures.py`` 的 ``_BY_STATUS``）。
    """
    return ChatError(
        _error_hint(response),
        reason=_status_reason(response.status_code),
        status=response.status_code,
    )


def _transport_error(exc: httpx.TransportError) -> ChatError:
    """连接层失败 → 带分类的 ``ChatError``。

    超时（``TimeoutException`` 那一族）与"连不上 / 中途断开"分开写：它们对用户是
    两件事（一个是慢，一个是没通上），而两者都进可重试白名单。
    """
    # 连接层失败没有 HTTP 状态码（``status`` 留空）：给用户的文案只按 ``reason`` 挑
    if isinstance(exc, httpx.TimeoutException):
        return ChatError(f"对话端点超时：{exc}", reason="timeout")
    return ChatError(f"对话端点连不上或中途断开：{exc}", reason="network_error")


def _message_wire(message: ChatMessage, *, echo_reasoning: bool = False) -> dict:
    """把一条消息转成 OpenAI 兼容的线上形状。

    **只有带工具位时才多发字段**：普通消息序列化出来与以前完全一样，
    不会因为升级客户端而改变既有请求（`content` 为空的助手消息仍发空串，
    而不是省略——省略会被某些端点当成非法消息）。

    ``echo_reasoning`` 见 ``thinking.ECHO_DIALECTS``：它是**端点级别的开关**，
    不是消息自己的属性——所以由调用方按这一轮的配置算好，这里只管把字段补上。

    **这里是进提示词的唯一咽喉**（D16 P0）：`content` 与工具参数的 `arguments`
    都过一遍 `sanitize_prompt_text`（合并代理对 → 仍孤立的换 U+FFFD → 去控制字符）。
    放在这一层而不是各个上游：历史 / 记忆 / 技能 / 工具返回**全都得从它过**——
    上游无论谁漏一个不可编码的字符，httpx 都会 `UnicodeEncodeError: surrogates not allowed`，
    结果是**整句话都问不出去**（实测两条真会话全废）。清洗是幂等的。
    """
    body: dict = {"role": message.role, "content": sanitize_prompt_text(message.content)}
    if message.tool_calls:
        body["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": sanitize_prompt_text(call.arguments)},
            }
            for call in message.tool_calls
        ]
        if echo_reasoning:
            # 端点要的是"字段在"，不是"内容对"：没有推理时**也必须发空串**
            # （实测缺字段 400、空串 200；只补一部分消息同样 400）
            body["reasoning_content"] = message.reasoning or ""
    if message.tool_call_id:
        body["tool_call_id"] = message.tool_call_id
    return body


@dataclass(frozen=True, slots=True)
class _Chunk:
    """SSE 一行的解析结果（见 ``_chunk_of``）。"""

    text: str = ""
    reasoning: str = ""
    finish_reason: str = ""
    tool_calls: tuple[ToolCallDelta, ...] = ()


def _chunk_of(line: str) -> _Chunk | None:
    """从一行 SSE 里取出（正文增量，思考增量，结束原因，工具调用碎片）。

    非数据行与 ``[DONE]`` 返回 ``None``。**结束原因也要取**：它是判断
    "为什么一个字都没出来"的唯一依据（``length`` = 被长度上限截断）。
    **思考增量单独取**：推理模型把它写在 ``delta.reasoning_content``，
    界面要把它与正文分两个区域显示（见 ``LLMDelta``）。

    工具调用碎片（``delta.tool_calls``）**原样转出、不在这里拼**：每块只带一部分
    （见 ``ToolCallDelta``），拼装要有"整条流"的视野，放在 ``assemble_tool_calls``。
    """
    if not line or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        body = json.loads(payload)
        choice = body["choices"][0]
        delta = choice.get("delta", {}) or {}
        return _Chunk(
            text=str(delta.get("content") or ""),
            reasoning=str(delta.get("reasoning_content") or ""),
            finish_reason=str(choice.get("finish_reason") or ""),
            tool_calls=_call_deltas(delta.get("tool_calls")),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _call_deltas(raw_calls: object) -> tuple[ToolCallDelta, ...]:
    """取这一块里的工具调用碎片。

    **单块坏掉不能丢整行**：正文与碎片常常同在一块里，为一段拼不起来的碎片
    把正文也丢掉，等于让回答凭空少一句。所以这里逐块容错，坏的那块跳过。
    """
    if not isinstance(raw_calls, list):
        return ()
    fragments: list[ToolCallDelta] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        try:
            index = int(raw.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        fragments.append(
            ToolCallDelta(
                index=index,
                id=str(raw.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=str(function.get("arguments") or ""),
            )
        )
    return tuple(fragments)


def assemble_tool_calls(fragments: Sequence[ToolCallDelta]) -> tuple[ToolCall, ...]:
    """把流式碎片按 ``index`` 拼成完整调用（见 ``ToolCallDelta``）。

    **容错与非流式那条路同一套**（``_reply_of``）：没名字的丢掉——无法执行，
    而模型下一轮通常还会正常再调一次；缺 id 的自己编一个稳定的，好让工具结果配对。

    参数是碎片拼起来的，这里**不判断 JSON 合不合法**：那是执行那一步的事，
    拼坏了会作为"工具参数不是合法 JSON"回给模型让它自己改
    （见 ``tool_loop._parse_arguments``）。
    """
    order: list[int] = []
    grouped: dict[int, list[ToolCallDelta]] = {}
    for fragment in fragments:
        if fragment.index not in grouped:
            grouped[fragment.index] = []
            order.append(fragment.index)
        grouped[fragment.index].append(fragment)

    calls: list[ToolCall] = []
    for index in order:
        parts = grouped[index]
        call = _call_or_none(
            index=index,
            ident=next((item.id for item in parts if item.id), ""),
            name=next((item.name for item in parts if item.name.strip()), ""),
            arguments="".join(item.arguments for item in parts),
        )
        if call is not None:
            calls.append(call)
    return tuple(calls)


def _call_or_none(*, index: int, ident: str, name: str, arguments: str) -> ToolCall | None:
    """拼一次调用，**两条路（流式拼接 / 非流式直读）共用这一份容错**。

    两处各写一份必然相漂，而它们该给出的是同一个判断：没名字的调用无法执行，
    丢掉比报错好（模型偶尔会吐一个空壳，而它下一轮通常还会正常再调一次）；
    id 可能缺失（少数实现不给），自己编一个稳定的，好让工具结果能配对。
    """
    cleaned = name.strip()
    if not cleaned:
        return None
    return ToolCall(
        id=ident or f"call_{index}",
        name=cleaned,
        arguments=arguments,
    )


class TextToolCalls(NamedTuple):
    """从正文里剥出来的工具调用标记（见 :func:`split_text_tool_calls`）。

    ``found`` 是**单独一位**，不能只靠 ``names`` 推：认不出名字的标记一样是标记
    （它是模型写出来的调用，只是形状不在我们认识的几种里），照旧得从回答里去掉。
    """

    text: str
    """剥掉标记、收拾过空白之后的正文。"""
    names: tuple[str, ...]
    """它想调用哪些工具（认不出来就是空元组）。**只用于措辞**，不做执行。"""
    found: bool
    """正文里到底有没有标记。"""


#: 正文里那些"本该是工具调用"的标记：**每族一对**（整块 + 孤立的标签），
#: 到这儿为止都要求**收尾标签必须在**。
#:
#: 为什么需要这一层（§12.219 的 §5 那条敞口）：收尾那一步有两条路**不带工具表**
#: ——超时与步数用尽（时间/额度已经花光，再补一轮请求与闸门本身的意思相反），
#: 而"不带工具表 + 上下文还在催它去查"时，模型只剩"把调用写进正文"这一条路
#: （实测 DeepSeek Flash：同一个请求带 tools 回结构化 ``tool_calls``，去掉 tools
#: 就写进 content）。那段标记于是被当成回答**落库并显示**——用户看到的就是
#: "回答里冒出一段 `<tool_call>` / DSML 标记"，还挂着复制、存为笔记的按钮。
#:
#: "一对"是实测逼出来的：一族的**收尾标签可能比开头多**（DeepSeek 会同时写
#: `<｜tool▁call▁end｜>` 与 `<｜tool▁calls▁end｜>`、DSML 那族还有 ``invoke`` 这种
#: 中间标签），只按"第一个收尾"配对会留下一截孤立的标签在正文里。所以整块剥完之后，
#: **该族的孤立标签**再清一遍——**只在这一族真的出现过一整块时才做**：正常回答里
#: 引用这个标签（"``<tool_call>`` 是什么意思"）不该被当成标记剪掉（见
#: :data:`_TEXT_CALL_OPEN` 的说明与那条"截断 vs 引用"的用例）。
_CLOSED_MARKERS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        # 1) `<tool_call>…</tool_call>`（Qwen 系；块里可能是 ``{"name": …}``，
        #    也可能是"名字裸写一行 + 一段 JSON"）
        re.compile(r"<tool_calls?\s*>.*?</tool_calls?\s*>", re.DOTALL | re.IGNORECASE),
        re.compile(r"</?tool_calls?\s*>", re.IGNORECASE),
    ),
    (
        # 2) DeepSeek 的特殊 token 被当正文吐出来（begin / end 那一对成对出现）：
        #    中间是工具名与参数，竖线可能是全角 `｜` 也可能是半角，
        #    那个窄空格是 `▁`；都按"像就行"匹配
        re.compile(
            r"<[｜|]{1,2}\s*tool[▁_ ]?calls?[▁_ ]?begin[｜|]{1,2}>.*?"
            r"<[｜|]{1,2}\s*tool[▁_ ]?calls?[▁_ ]?end[｜|]{1,2}>",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"<[｜|]{1,2}\s*tool[▁_ ]?calls?[▁_ ]?(?:begin|end)?[｜|]{1,2}>", re.IGNORECASE),
    ),
    (
        # 3) DSML（DeepSeek 的另一套方言）：`<｜｜DSML｜｜ invoke name="…">` 那一族
        re.compile(
            r"<[｜|]{1,2}\s*/?\s*DSML[｜|]{1,2}[^>]*>.*?"
            r"</?\s*[｜|]{1,2}\s*/?\s*DSML[｜|]{1,2}[^>]*>",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"</?\s*[｜|]{1,2}\s*/?\s*DSML[｜|]{1,2}[^>]*>", re.IGNORECASE),
    ),
    (
        # 4) `<function=名字>{…}</function>`（早期 Qwen 与一些兼容端点的写法）
        re.compile(
            r"<function\s*(?:=[^>]*|name\s*=\s*[\"'][^\"']*[\"'][^>]*)>.*?</function\s*>",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"</?function\s*[^>]*>", re.IGNORECASE),
    ),
)

#: **收尾标签可能永远不来**的那些（被 max_tokens 截断，或模型只写了一半）。
#:
#: 与 :data:`_CLOSED_MARKERS` 分开是因为**时机不同**，这一点是踩出来的：
#: 流式那一层每收到一块就得判一次，而"到这段文字结束"在流里等于"到目前收到的为止"
#: ——按它当场吃掉，后面紧接着到的载荷碎片（``,"arguments": …}`` 那种）就会被当成
#: 正文漏出去。所以这一组**只在"这一趟文字已经结束"时才敢用**（一次性剥的那条路、
#: 以及流式那层的 ``flush``）。
#:
#: "标签后面**必须**紧跟调用载荷"在这里是一半判据、也是防误伤的那条：回答里引用
#: 这个标签时后面跟的是标点或中文，不满足"名字 / 花括号"，于是不会被剪掉。
_TEXT_CALL_OPEN: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(
            r"<tool_calls?\s*>\s*(?:\{.*|[A-Za-z_][\w.]*\s*\{.*)\Z",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"</?tool_calls?\s*>", re.IGNORECASE),
    ),
    (
        re.compile(
            r"<[｜|]{1,2}\s*tool[▁_ ]?calls?[▁_ ]?begin[｜|]{1,2}>.*\Z",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"<[｜|]{1,2}\s*tool[▁_ ]?calls?[▁_ ]?(?:begin|end)?[｜|]{1,2}>", re.IGNORECASE),
    ),
    (
        re.compile(
            r"<[｜|]{1,2}\s*/?\s*DSML[｜|]{1,2}[^>]*>.*\Z",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"</?\s*[｜|]{1,2}\s*/?\s*DSML[｜|]{1,2}[^>]*>", re.IGNORECASE),
    ),
    (
        re.compile(
            r"<function\s*(?:=[^>]*|name\s*=\s*[\"'][^\"']*[\"'][^>]*)>.*\Z",
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(r"</?function\s*[^>]*>", re.IGNORECASE),
    ),
)


def split_text_tool_calls(text: str) -> TextToolCalls:
    """把正文里混进来的工具调用标记**剥掉**（两组标记见 :data:`_CLOSED_MARKERS`）。

    只做两件事：认出标记、把工具名报出来给上面写措辞用。**不执行**——这一层的调用
    出现在"已经没有工具表"的那几条收尾路上（超时 / 步数用尽 / 这条链路本来就没有
    工具），补一轮执行与那两道闸的意思相反；要接着做是用户点「继续」的事。

    没命中时原样返回（``found=False``、``text`` 与入参逐字节相同），调用方据此
    什么都不做——这条函数不能成为"每次回答都要过一遍、顺手改点标点"的那种加工。
    """
    return _strip_families(text, (*_CLOSED_MARKERS, *_TEXT_CALL_OPEN))


def _strip_families(
    text: str,
    families: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...],
    *,
    tidy: bool = True,
) -> TextToolCalls:
    """按给定的一族族规则剥一遍（见 :func:`split_text_tool_calls`）。

    ``tidy=False`` 给流式那层用：它剥掉的只是**中间**的一块，剩下的那段还要接着
    往下发，收拾空白会把两块正文之间的那个换行也吃掉（一次性剥那条路不存在这个问题
    ——它看到的是整篇）。真正的收拾留到收尾（``_clean_answer`` 的 ``tidy_text``）。
    """
    names: list[str] = []

    def _drop(block: re.Match[str]) -> str:
        names.extend(_names_in(block.group(0)))
        return ""

    cleaned = text
    for block, leftover in families:
        if block.search(cleaned) is None:
            continue
        cleaned = block.sub(_drop, cleaned)
        # 配对的整块剥掉之后，同族**孤立的标签**再清一遍（一族的收尾标签可能比开头多，
        # 见 :data:`_CLOSED_MARKERS` 里那段说明）。**只在这一族真的出现过一块时才做**
        # ——回答里引用这个标签（"``<tool_call>`` 是什么意思"）不该被当成标记剪掉。
        cleaned = leftover.sub("", cleaned)
    if cleaned == text:
        return TextToolCalls(text=text, names=(), found=False)
    return TextToolCalls(
        text=tidy_text(cleaned) if tidy else cleaned,
        names=tuple(dict.fromkeys(names)),
        found=True,
    )


def _names_in(block: str) -> tuple[str, ...]:
    """从一段标记里认出它想调用哪些工具（认不出就空——那份信息只用于措辞）。

    三种写法都来自真实输出：``{"name": "web_search"}``（JSON）、
    ``name="web_search"``（DSML 的 ``invoke`` / ``parameter``）、
    以及 ``<tool_call>web_search`` 之后才跟 JSON 的"名字裸写在开头"。
    """
    found = re.findall(r'["\']name["\']\s*:\s*["\']([^"\']+)["\']', block)
    found += re.findall(r'\bname\s*=\s*["\']([^"\']+)["\']', block)
    head = re.match(r"\s*<tool_call>\s*([A-Za-z_][\w.]*)", block, re.IGNORECASE)
    if head is not None:
        found.insert(0, head.group(1))
    return tuple(item.strip() for item in found if item.strip())


#: 标记**开头**的那几种写法（小写）。流式过滤靠它判断"这段还可能是标记的开头"
#: （见 :class:`TextMarkerFilter`）。
_MARKER_OPENERS = ("<tool_call", "<tool_calls", "<｜", "<|", "<function")

#: ``<tool_call>`` 这类标签后面**必须**跟的东西：一段 JSON，或"一个工具名 + 一段 JSON"。
#: 回答里引用这个标签时（"``<tool_call>`` 是模型想调工具时写的"）后面跟的是标点或中文，
#: 于是不会被当成标记——这条与 :data:`_TEXT_CALL_MARKERS` 最后一族是同一个判据。
_PAYLOAD_HINT = re.compile(r"(?:\{|[A-Za-z_][\w.]*\s*\{)")

#: 一个**还没写完**的工具名（"web_sea" 这种，后面还等着 `{`）。
_NAME_TAIL = re.compile(r"^[A-Za-z_][\w.]*\s*$")

#: 收尾时要一起带走的空白（见 :class:`TextMarkerFilter` 的 `_gap`）。
_BLANKS = " \t\r\n"

#: 扣留的上限（字符）：超过它就不再扣着——我们认识的标记没有这个长度，
#: 再扣下去只会让一段正常回答迟迟不显示。
MARKER_HOLD_LIMIT = 4000


class TextMarkerFilter:
    """流式正文的**标记过滤器**（§12.228）：边收边把"本该是工具调用"的标记丢掉。

    为什么要在**流**这一层也做一遍（协议层已经会剥掉收尾那条全文了）：正文增量是
    **边到边发**的，而用户看的就是增量——只在收尾处剥，等于"库里干净、屏幕上脏过"，
    实测那 5 条消息的正文**整条**都是标记，于是"闪一下"几乎等于整段回答。
    已经发出去的字收不回来（见 ``tool_loop._answer`` 里"吐过了就不能重试"那段），
    所以只能在这层**先不发**。

    三档处置（``_pending`` 里逐块判）：

    - **丢掉**：认得出的整块标记（与 :func:`split_text_tool_calls` 同一份判据，
      含"人话里夹着标记"的那种——标记丢、人话放行）；
    - **扣住**：这段还可能是标记的开头（"<tool_ca" 这种半截，或 ``<tool_call>`` 之后
      载荷还没到）——等下一块再判，最多扣 :data:`MARKER_HOLD_LIMIT` 个字符；
    - **放行**：不是标记（``<b>`` 这种、或引用这个标签的正常句子），照常发。

    丢掉的东西**不静默**：``dropped`` / ``names`` 由调用方读走，用来补一条说明
    （见 ``tool_loop.text_marker_step``）——"我们压掉了模型的一段输出"必须让人知道。
    """

    def __init__(self) -> None:
        self._pending = ""
        #: 上一段末尾**还没定**的空白：跟着下一块走——是标记就一起丢，是人话就补回去
        #: （见 `_drain`；不这么做的话，标记前面那个换行会留在回答末尾）
        self._gap = ""
        self.dropped = False
        self.names: list[str] = []

    @property
    def removed(self) -> TextToolCalls:
        """它丢掉了什么（``text`` 恒为空——丢掉的都没有留下来的部分）。"""
        return TextToolCalls(text="", names=tuple(dict.fromkeys(self.names)), found=self.dropped)

    def feed(self, chunk: str) -> str:
        """喂一个增量，返回**这一段里该显示的部分**（可能为空串）。"""
        self._pending += chunk
        return self._drain()

    def flush(self) -> str:
        """流结束了：把还扣着的那段定下来（按**完整**口径——含"被截断的那族"标记）。"""
        rest, self._pending = self._pending, ""
        judged = _strip_families(rest, (*_CLOSED_MARKERS, *_TEXT_CALL_OPEN), tidy=False)
        if judged.found:
            self._note(judged)
            return judged.text
        # 不是标记：`_gap` 里记着的那点空白（见 `_drain`）跟着它一起放行
        return self._emit_gap() + rest

    def _drain(self) -> str:
        out: list[str] = []
        while self._pending:
            index = self._pending.find("<")
            if index < 0:
                # 这一段里没有标记的开头可言：整段放行。**末尾的空白留在 `_gap` 里**
                # ——紧跟其后的很可能就是一块标记，而标记前面那点空白该跟着它一起消失
                # （一次性剥的那条路是 `tidy_text` 收的；流里得提前留一手，
                # 否则回答末尾会多出一个换行）
                body = self._pending.rstrip(_BLANKS)
                out.append(self._emit_gap() + body)
                self._gap = self._pending[len(body) :]
                self._pending = ""
                break
            prefix = self._pending[:index]
            body = prefix.rstrip(_BLANKS)
            if body:
                # `<` 之前确实有人话：那部分与后面的标记无关，放行
                out.append(self._emit_gap() + body)
            # 前缀里剩下的空白（可能还带着更早扣下的那点）**先记着**——
            # 它正好在标记前面，那块要是标记，它就跟着一起走（见下面两个分支）
            self._gap += prefix[len(body) :]
            self._pending = self._pending[index:]
            # **只用闭合的那组**：流还没完，"到这段结束"不能当收尾（见 `_TEXT_CALL_OPEN`）
            judged = _strip_families(self._pending, _CLOSED_MARKERS, tidy=False)
            if judged.found:
                self._note(judged)
                self._gap = ""  # 标记前面那点空白跟着它一起走
                out.append(judged.text)
                self._pending = ""
                break
            if len(self._pending) <= MARKER_HOLD_LIMIT and _may_be_marker(self._pending):
                break  # 还可能是标记的一段：扣着，等下一块
            # 不是标记：`_gap` 还回去，这个 `<` 当普通字符放行，再看它后面还有没有
            out.append(self._emit_gap() + "<")
            self._pending = self._pending[1:]
        return "".join(out)

    def _emit_gap(self) -> str:
        """`_gap` 里那点空白**该放行了**（不是标记前面那段）。"""
        gap, self._gap = self._gap, ""
        return gap

    def _note(self, judged: TextToolCalls) -> None:
        self.dropped = True
        self.names.extend(judged.names)


def _may_be_marker(text: str) -> bool:
    """这段（以 ``<`` 开头）还可能是**一段标记的开头**吗（见 :class:`TextMarkerFilter`）。

    两档都算"可能"：还没写出完整的开头（"<tool_ca"）、或已经写出开头但后面还没出现
    "它不是标记"的证据（载荷、或内层标签）。一旦后面跟的是人话，就当场判定"这是引用"，
    于是那段回答一个字都不必被扣着——**扣住正常回答是这个文件最不该犯的错**。
    """
    lowered = text.lower()
    if any(opener.startswith(lowered) for opener in _MARKER_OPENERS):
        return True
    for opener in _MARKER_OPENERS:
        if not lowered.startswith(opener):
            continue
        close = text.find(">")
        if close < 0:
            return True  # 标签还没写完
        return _may_be_payload(text[close + 1 :].lstrip())
    return False


def _may_be_payload(head: str) -> bool:
    """标签后面这段还可能是"调用载荷"的开头吗（见 :func:`_may_be_marker`）。

    **要按"还没写完"来判**：流里 "web_search" 是先到 "w"、再到 "we" 的，
    拿"完整的载荷"去比会在第一个字符上就判成"不是标记"，那段标记就漏出去了。
    所以只有"已经不可能再变成载荷"（出现了中文、标点这类不是名字也不是花括号的字符）
    才说不像。
    """
    if not head or head.startswith("<"):
        return True  # 载荷还没到；或这一族的内层标签（DeepSeek 会写两层）
    if _PAYLOAD_HINT.match(head):
        return True  # 载荷已经在眼前了
    return bool(_NAME_TAIL.match(head))  # 名字写了一半，还在等那个 `{`


def tidy_text(text: str) -> str:
    """剥掉标记之后收拾空白：连续空行折成一个，首尾空白去掉。

    不收拾的话，被剥掉的那一块在回答中间留一大段空白——那不是模型写的停顿，
    是我们自己剥出来的，用户看到的是一条回答中间空了好几行。
    """
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _error_hint(response: httpx.Response) -> str:
    """把 HTTP 错误翻成处置建议。"""
    body = response.text[:_MAX_ERROR_BODY]
    if response.status_code == 401:
        return f"对话端点鉴权失败（401）：请检查 API Key。{body}"
    if response.status_code == 404:
        return f"对话端点不存在（404）：请检查接口地址与模型 ID。{body}"
    if response.status_code == 429:
        return f"对话端点限流（429）：免费模型额度可能已用尽，稍后重试。{body}"
    return f"对话端点返回 {response.status_code}：{body}"
