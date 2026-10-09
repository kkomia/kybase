"""对话客户端（``app/services/llm.py``）：请求载荷里思考字段的方言。

HTTP 用 ``httpx.MockTransport`` 拦掉——要验的是"发出去的那个 JSON 长什么样"，
真打网络既慢又不稳定。方言翻译本身在 ``test_thinking.py`` 里测，这里只确认
``_payload`` 确实把它接了进来、并且没有别的字段把开关覆盖回去。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.llm import (
    RETRYABLE_REASONS,
    STREAM_IDLE_TIMEOUT_SECONDS,
    ChatError,
    ChatMessage,
    LLMConfig,
    OpenAICompatChat,
    TextMarkerFilter,
    ToolCall,
    ToolCallDelta,
    ToolSpec,
    assemble_tool_calls,
    split_text_tool_calls,
)

_MESSAGES = [ChatMessage(role="user", content="你好")]


def _capture(config: LLMConfig) -> dict:
    """跑一次 complete，返回实际发出去的请求体。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "好"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    OpenAICompatChat(config, client=client).complete(_MESSAGES)
    return captured


def _config(**overrides) -> LLMConfig:  # type: ignore[no-untyped-def]
    base = {
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-test",
        "model_id": "deepseek-flash",
    }
    base.update(overrides)
    return LLMConfig(**base)


def test_deepseek_payload_carries_thinking_and_effort() -> None:
    body = _capture(_config(enable_thinking=True, thinking_effort="high"))
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    # 采样参数照旧
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is False


def test_deepseek_disable_sends_disabled() -> None:
    body = _capture(_config(enable_thinking=False))
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_dialect_override_is_honoured() -> None:
    """注册模型写了 thinking_dialect 时，按它走而不是按地址猜。"""
    body = _capture(
        _config(
            base_url="https://api.deepseek.com",
            enable_thinking=True,
            thinking_effort="low",
            thinking_dialect="qwen",
        )
    )
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 1024
    assert "thinking" not in body


def test_content_error_mentions_budget_and_strength() -> None:
    """只回思考不回正文时，错误文案要给出**可处置**的两条路。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "", "reasoning_content": "想…"}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        OpenAICompatChat(_config(), client=client).complete(_MESSAGES)
    except Exception as exc:
        text = str(exc)
        # 可处置的两条路：调低/关掉思考，或换个非推理模型。
        # **不再提「最大回复长度」**——那个设置项已经去掉了（不再替模型决定长度）
        assert "思考" in text
        assert "非推理模型" in text
    else:  # pragma: no cover - 走到这里说明行为变了
        raise AssertionError("空正文应当抛错")


# ------------------------------------------------- 长度上限与"一个字都没出来"


def _stream_client(chunks: list[dict], status: int = 200) -> httpx.Client:
    """把若干 SSE 行按顺序吐出来，模拟端点的流式响应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        # 用 chr(10) 拼换行：SSE 的空行是"事件结束"的标记，这里要的是真的换行符
        nl = chr(10)
        body = "".join(f"data: {json.dumps(c)}{nl}{nl}" for c in chunks)
        body += f"data: [DONE]{nl}{nl}"
        return httpx.Response(status, content=body.encode("utf-8"))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _delta(text: str, finish: str | None = None) -> dict:
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


# ------------------------------------------------- 出站客户端（共享，见 core/http.py）


def test_no_client_is_built_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """不注入 client 时走**进程级共享**的那个。

    以前这里是 `httpx.Client(timeout=...)`，用完即关——一轮默认的 Agent 对话要发出
    十几次模型调用，那就是十几次 TCP + TLS 握手（内网 5–20ms、公网 100–300ms，
    全是白花的固定开销）。

    判据是硬的：把 `httpx.Client` 换成"一构造就炸"的替身，
    被测代码只要自己新建，这条用例立刻红。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "好"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    fake = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.llm.shared_client", lambda: fake)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("不该每次调用都新建 httpx.Client（见 app/core/http.py）")

    monkeypatch.setattr(httpx, "Client", explode)

    # 不注入 client：走共享那条路
    assert OpenAICompatChat(_config()).complete(_MESSAGES) == "好"


def test_payload_omits_max_tokens_unless_explicitly_set() -> None:
    """**默认不发长度上限**：上限是模型自己的事。

    我们拍过的那个 2048 会把回复预算掐死在思考阶段，正文一个字都出不来
    （实测，而且时好时坏）。不传时端点会生成到模型自然收尾。
    """
    captured = _capture(_config())
    assert "max_tokens" not in captured
    # 但显式给了就得发——有些端点"不传就退化成很小的默认值"（走模型注册的 options）
    assert _capture(_config(max_tokens=4096))["max_tokens"] == 4096


def test_stream_passes_through_content_deltas() -> None:
    client = _stream_client([_delta("你"), _delta("好"), _delta("", "stop")])
    chunks = list(OpenAICompatChat(_config(), client=client).stream(_MESSAGES))
    assert chunks == ["你", "好"]


def test_stream_events_carry_tool_call_fragments() -> None:
    """流式里的工具调用**原样转出**，而且不算"空回答"（v0.34）。

    收尾那一步现在也带着工具表：模型除了作答，还可能在这一步要求继续调工具。
    那种响应的正文是空的——按老口径（只看正文）会被判成"模型没有返回任何正文"
    直接报错，而它其实满载信息。
    """
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "c1", "function": {"name": "search", "arguments": "{}"}}
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    client = _stream_client([chunk])
    deltas = list(OpenAICompatChat(_config(), client=client).stream_events(_MESSAGES))

    assert len(deltas) == 1
    assert deltas[0].tool_calls[0].name == "search"
    assert deltas[0].text == ""


def test_a_broken_fragment_does_not_take_the_text_with_it() -> None:
    """同一块里碎片坏掉时，**正文不能跟着丢**——它们常常同在一块里。"""
    chunk = {
        "choices": [
            {
                "delta": {"content": "我先看看。", "tool_calls": ["坏掉的形状"]},
                "finish_reason": None,
            }
        ]
    }
    client = _stream_client([chunk])
    deltas = list(OpenAICompatChat(_config(), client=client).stream_events(_MESSAGES))

    assert [item.text for item in deltas] == ["我先看看。"]
    assert deltas[0].tool_calls == ()


def test_stream_asks_for_the_tool_table_only_when_given() -> None:
    """流式请求**给了工具表才发 tools**：没给时请求体与以前逐字节一样。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        nl = chr(10)
        return httpx.Response(
            200, content=f"data: {json.dumps(_delta('好'))}{nl}{nl}data: [DONE]{nl}{nl}".encode()
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    chat = OpenAICompatChat(_config(), client=client)
    chat_spec = ToolSpec(name="search", description="查", parameters={"type": "object"})

    list(chat.stream_events(_MESSAGES))
    list(chat.stream_events(_MESSAGES, [chat_spec]))

    assert "tools" not in seen[0]
    assert seen[1]["tools"][0]["function"]["name"] == "search"
    assert seen[1]["tool_choice"] == "auto"


def test_fragments_assemble_across_chunks() -> None:
    """碎片按 ``index`` 拼：``arguments`` 是**逐字符切开**的 JSON，``id`` 只在第一块。

    容错与非流式那条路一致：没名字的丢掉（没法执行，模型下一轮通常还会再调一次），
    缺 id 的自己编一个稳定的（好让工具结果配对）。
    """
    fragments = [
        ToolCallDelta(index=0, id="c1", name="search", arguments='{"que'),
        ToolCallDelta(index=0, arguments='ry":"眼轴"}'),
        ToolCallDelta(index=1, name="web_fetch", arguments="{}"),  # 没 id
        ToolCallDelta(index=2, id="c3", arguments="{}"),  # 没名字：丢掉
    ]

    calls = assemble_tool_calls(fragments)

    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("c1", "search", '{"query":"眼轴"}'),
        ("call_1", "web_fetch", "{}"),
    ]


def test_stream_raises_when_not_a_single_character_came_back() -> None:
    """**一个字正文都没出来就报错**，不能静默收尾。

    静默的后果是上层存下一条空回答，用户看到"只有问题、没有回答"，却没有任何
    可处置的线索——非流式那条路一直有这道判断，两条路必须一个口径。
    推理模型把预算全花在思考上时正是这个现象（只吐 reasoning_content）。
    """
    reasoning_only = {
        "choices": [{"delta": {"reasoning_content": "想了很久……"}, "finish_reason": "length"}]
    }
    client = _stream_client([reasoning_only])
    chat = OpenAICompatChat(_config(), client=client)

    with pytest.raises(ChatError) as caught:
        list(chat.stream(_MESSAGES))

    message = str(caught.value)
    assert "长度上限" in message
    # 给的是"下一步做什么"，不是"参数非法"
    assert "深度思考" in message


def test_stream_raises_without_length_reason_too() -> None:
    """结束原因是 stop 但同样一个字都没有：也要报错，只是措辞不同。"""
    client = _stream_client([_delta("", "stop")])
    with pytest.raises(ChatError) as caught:
        list(OpenAICompatChat(_config(), client=client).stream(_MESSAGES))
    assert "没有返回任何正文" in str(caught.value)


# ------------------------------------------------- 思考的回传（v0.27 实测的 400）


_TOOL_SPEC = ToolSpec(name="search", description="查", parameters={"type": "object"})

_TOOL_MESSAGES = [
    ChatMessage(role="user", content="查一下"),
    ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="c1", name="search", arguments="{}"),),
        reasoning="先想清楚要查什么。",
    ),
    ChatMessage(role="tool", content="结果", tool_call_id="c1"),
]


def _capture_tools(config: LLMConfig, messages=None):  # type: ignore[no-untyped-def]
    """跑一次带工具表的**流式**调用，返回（实际发出去的请求体, 收到的增量）。

    工具循环每一步都是流式（v0.40 起），所以检查"请求里带了什么"也从这条路走。
    """
    captured: dict = {}
    # 端点的返回形状：先思考，再一串工具调用碎片（``id`` 只在第一块里）
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "端点的思考"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "search", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
                 "finish_reason": "tool_calls"}
            ]
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        nl = chr(10)
        body = "".join(f"data: {json.dumps(c)}{nl}{nl}" for c in chunks)
        body += f"data: [DONE]{nl}{nl}"
        return httpx.Response(200, content=body.encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    deltas = list(
        OpenAICompatChat(config, client=client).stream_events(
            messages or _TOOL_MESSAGES, [_TOOL_SPEC]
        )
    )
    return captured, deltas


def test_deepseek_thinking_echoes_the_reasoning_back() -> None:
    """思考模式下，带工具调用的助手消息**必须把推理传回去**。

    实测（2026-09-19，api.deepseek.com / deepseek-flash）：缺 ``reasoning_content``
    这个字段整条请求 400（"must be passed back to the API"），补空串就 200。
    这条链路在**工具循环**上：一次工具调用就多一轮请求，而每一轮都要把上一轮的
    助手消息发出去——不补这个字段的话，用户看到的是"工具都调完了、然后对话失败"。
    """
    captured, _ = _capture_tools(_config(enable_thinking=True))

    assistant = captured["messages"][1]
    assert assistant["reasoning_content"] == "先想清楚要查什么。"
    # 字段只加在带工具调用的助手消息上：系统 / 用户 / 工具结果都没有推理
    assert "reasoning_content" not in captured["messages"][0]
    assert "reasoning_content" not in captured["messages"][2]


def test_an_empty_reasoning_is_still_sent() -> None:
    """没有推理时**也要发这个字段**（空串合法）。

    端点要的是"字段在"：**每一个**带工具调用的助手消息都得有，
    只补前几条、漏掉最后一条同样 400（实测）。而"带着工具调用却没有推理"的消息
    是会出现的——模型某一轮就是没给 ``reasoning_content``，或者这条消息是从
    别处拼进来的；补一个空串的成本是零，漏掉的成本是整轮对话失败。
    """
    messages = [
        _TOOL_MESSAGES[0],
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="c1", name="search", arguments="{}"),),
        ),
        _TOOL_MESSAGES[2],
    ]

    captured, _ = _capture_tools(_config(enable_thinking=True), messages)

    assert captured["messages"][1]["reasoning_content"] == ""


def test_no_reasoning_field_when_thinking_is_off_or_the_dialect_differs() -> None:
    """**只对认这个字段的方言发**：思考关着时不需要，别的供应商也没有证据要它。

    给不认识的端点多发一个字段是有代价的（可能被严格网关打成 400），
    而这条要求只有 DeepSeek 实测过（见 thinking.ECHO_DIALECTS）。
    """
    off, _ = _capture_tools(_config(enable_thinking=False))
    assert "reasoning_content" not in off["messages"][1]

    other, _ = _capture_tools(
        _config(base_url="https://api.siliconflow.cn/v1", model_id="Qwen/Qwen3.5-4B")
    )
    assert "reasoning_content" not in other["messages"][1]


def test_the_reasoning_and_the_calls_come_out_of_the_stream() -> None:
    """解析侧：流式要**同时**把 ``reasoning_content`` 与工具调用碎片交出来。

    思考丢了不会当场报错——错在下一轮请求上（端点要它回传），而那时离起因已经很远；
    工具调用碎片丢了的后果更直接：那一步的工具根本不会执行，
    模型看到的是"我说了要查，但没人去查"。
    """
    _, deltas = _capture_tools(_config(enable_thinking=True))

    assert "".join(d.reasoning for d in deltas) == "端点的思考"
    fragments = [fragment for delta in deltas for fragment in delta.tool_calls]
    assert len(fragments) == 2, "碎片原样转出（拼装是工具循环的事）"
    calls = assemble_tool_calls(fragments)
    assert [(call.id, call.name, call.arguments) for call in calls] == [("c1", "search", "{}")]


# ------------------------------------------------- 流空闲超时与失败分类（P2-2）


class _Clock:
    """可推进的假时钟：用例不必真的等 30 秒。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _gapped_client(clock: _Clock, *, gaps: list[float], chunks: list[dict]) -> httpx.Client:
    """一个"每块之间停一会儿"的假端点（用假时钟记，不真 sleep）。

    ``gaps[i]`` 是**读到第 i 块之前**经过的时长。被测的判据正是"两块之间隔了多久"，
    所以这里把时间推进放在"吐那一块"的那一刻——与真实端点上一块晚到的形状一致。
    """

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nl = chr(10)
            for gap, chunk in zip(gaps, chunks, strict=True):
                clock.now += gap
                yield f"data: {json.dumps(chunk)}{nl}{nl}".encode()
            yield f"data: [DONE]{nl}{nl}".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Stream())

    return httpx.Client(transport=httpx.MockTransport(handler))


def _idle_chat(clock: _Clock, client: httpx.Client) -> OpenAICompatChat:
    return OpenAICompatChat(_config(), client=client, clock=clock)


def test_a_stream_that_goes_quiet_for_the_idle_timeout_is_cut_off() -> None:
    """**两块之间**超过空闲上限就当场断（P2-2，照 ZCode 的 ``MODEL_STREAM_IDLE_TIMEOUT``）。

    没有这道判定时的实测形状：连接还在、token 也偶尔来一点，于是要等到整次调用的
    120 秒才失败——用户盯着一个不动的光标等了两分钟，而那一刻他手里没有任何线索。

    这里给的是"第一块到了、第二块隔了 31 秒"：31 >= 30 当场断。
    """
    clock = _Clock()
    client = _gapped_client(
        clock, gaps=[0.0, STREAM_IDLE_TIMEOUT_SECONDS + 1.0], chunks=[_delta("你"), _delta("好")]
    )

    seen: list[str] = []
    with pytest.raises(ChatError) as caught:
        for delta in _idle_chat(clock, client).stream_events(_MESSAGES):
            seen.append(delta.text)

    assert caught.value.reason == "stream_idle_timeout"
    assert caught.value.retryable is True
    # 已经交出去的那一块仍然算数（上层据此判断"还能不能重试"，见 tool_loop._answer）
    assert seen == ["你"]
    # 文案要给出"下一步做什么"，而且说清等了多久
    assert "31 秒" in str(caught.value)
    assert "重试" in str(caught.value)


def test_a_stream_that_keeps_producing_is_not_cut_off() -> None:
    """一直在吐 token 的流**不受这道闸影响**（哪怕它整体很长）。

    上限管的是"两块之间"，不是"整次调用"：一轮长回答就是几十块拼起来的，
    每块都很快——把它误判成断流的话，长回答会稳定地在中间被砍掉。
    """
    clock = _Clock()
    client = _gapped_client(
        clock,
        # 每块之间都停 29 秒（刚好在上限之内），一共三块
        gaps=[0.0, STREAM_IDLE_TIMEOUT_SECONDS - 1.0, STREAM_IDLE_TIMEOUT_SECONDS - 1.0],
        chunks=[_delta("甲"), _delta("乙"), _delta("丙")],
    )

    deltas = list(_idle_chat(clock, client).stream_events(_MESSAGES))

    assert "".join(item.text for item in deltas) == "甲乙丙"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (429, True),   # 限流：等一会儿就是好了
        (500, True),   # 端点自己出错
        (503, True),   # 同上（网关那一层）
        (401, False),  # 密钥不对：重试一百次也一样，只会白花一次调用
        (404, False),  # 地址或模型名写错
        (400, False),  # 请求格式不对
    ],
)
def test_http_failures_are_classified_by_whether_a_retry_can_help(
    status: int, retryable: bool
) -> None:
    """429/5xx 判可重试、401/404/参数错误不可重试（P2-2 的白名单）。

    分类在这一层、**重试不在这层**：它只回答"值不值得再试"，具体重试几次、
    能不能重试（已经吐了一半正文的时候不行）是上层的策略（见 ``tool_loop._answer``）。
    """
    client = _stream_client([], status=status)

    with pytest.raises(ChatError) as caught:
        list(OpenAICompatChat(_config(), client=client).stream_events(_MESSAGES))

    assert caught.value.retryable is retryable
    assert (caught.value.reason in RETRYABLE_REASONS) is retryable
    # 非流式那条路同一套判定（两条路必须一个口径）
    with pytest.raises(ChatError) as caught_once:
        OpenAICompatChat(_config(), client=client).complete(_MESSAGES)
    assert caught_once.value.retryable is retryable


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError("连不上"), "network_error"),
        (httpx.ReadTimeout("读超时"), "timeout"),
        (httpx.RemoteProtocolError("流到一半断了"), "network_error"),
    ],
)
def test_transport_failures_come_out_classified_and_retryable(
    error: httpx.TransportError, reason: str
) -> None:
    """连接层的失败（超时 / 连不上 / 中途断开）都归到可重试那一类。

    不包的话它们以 httpx 的原始异常穿到协议层，被当成 500"服务内部错误"——
    而它其实是一次外部抖动，处置方式（再试一次 vs 去设置页改配置）完全不同。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ChatError) as caught:
        OpenAICompatChat(_config(), client=client).complete(_MESSAGES)

    assert caught.value.reason == reason
    assert caught.value.retryable is True


def test_an_unclassified_failure_is_not_retryable() -> None:
    """没归类的失败**默认不可重试**（白名单语义）。

    反过来的话，一个我们没想过的失败会被反复重试——而"新出现一种失败"正是最不该
    自动重试的时候（不知道它重试会发生什么）。
    """
    assert ChatError("说不清是什么").retryable is False
    assert ChatError("限流").retryable is False  # 只有 reason 才决定，不看文案
    assert ChatError("限流", reason="rate_limited").retryable is True


# ------------------------------------------- 正文里的工具调用标记（§12.227）


def test_a_tool_call_block_is_stripped_and_named() -> None:
    """闭合的 `<tool_call>{…}</tool_call>`：正文留下人话，标记去掉，工具名报出来。

    这就是 §12.219 里那 5 条消息的形状：正文**整条**是标记（模型想继续查，
    而收尾那一步没带工具表）。工具名只用于措辞（"它想调用 web_fetch"），
    解析它不是为了执行——那几条路上已经没有可执行的额度了。
    """
    text = (
        "NVIDIA 那边我还想再核一眼。\n"
        '<tool_call>\n{"name": "web_fetch", "arguments": {"url": "https://example.com"}}\n</tool_call>'
    )

    result = split_text_tool_calls(text)

    assert result.found is True
    assert result.text == "NVIDIA 那边我还想再核一眼。"
    assert result.names == ("web_fetch",)


def test_the_qwen_style_name_plus_json_is_recognized() -> None:
    """Qwen 那种"名字裸写一行 + 一段 JSON"：工具名也要认得出来。"""
    result = split_text_tool_calls('<tool_call>web_search\n{"query": "眼轴"}\n</tool_call>')

    assert result.names == ("web_search",)
    assert result.text == ""


def test_deepseek_special_tokens_and_dsml_are_both_stripped() -> None:
    """另外两族：DeepSeek 的特殊 token（`<｜tool▁calls▁begin｜>…`）与 DSML。

    特殊 token 那一族**认不出名字**（里面没有 JSON、也没有 ``name=``）——
    但认不出名字**照样是标记**：`found` 是单独一位，就是为了这种情况。
    """
    tokens = (
        "我查一下。\n<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>web_search\n"
        "<｜tool▁call▁end｜><｜tool▁calls▁end｜>"
    )
    dsml = '…\n<|DSML| tool_calls>\n<|DSML| invoke name="web_fetch">\n</|DSML|>'

    special = split_text_tool_calls(tokens)
    marked = split_text_tool_calls(dsml)

    assert special.found is True and special.text == "我查一下。"
    assert special.names == (), "形状里没有名字：认不出名字也算命中"
    assert marked.found is True and marked.text == "…"
    assert marked.names == ("web_fetch",)


def test_a_truncated_block_still_counts_but_a_quotation_does_not() -> None:
    """截断的（没有收尾标签）算标记；**引用**这个标签的正常回答不算。

    这条界线是刻意的：模型只写了一半时后面跟的是调用载荷（花括号或工具名），
    而用户问"``<tool_call>`` 是什么意思"时，那后面跟的是标点、中文或行内代码——
    按载荷那条判据分得开，于是不会把一段正常回答从中间剪掉。
    """
    truncated = '先看一眼。\n<tool_call>{"name": "search", "arguments": {"query": '
    quoted = "那个 `<tool_call>` 标签是模型想调工具时写出来的。"

    cut = split_text_tool_calls(truncated)
    kept = split_text_tool_calls(quoted)

    assert cut.found is True and cut.text == "先看一眼。"
    assert kept.found is False, "引用不是调用"
    assert kept.text == quoted, "没命中就要**逐字节原样**返回：这层不该顺手改标点"


def test_a_plain_answer_is_untouched() -> None:
    """没标记时原样返回——这条函数不能成为"每次回答都要过一遍"的加工。"""
    text = "眼轴长度是 24mm 上下，个体差异比年龄影响更大。"

    result = split_text_tool_calls(text)

    assert (result.text, result.names, result.found) == (text, (), False)



# -------------------------------- 流式过滤器：正文增量里也不该出现标记（§12.228）


def _streamed(chunks: list[str]) -> tuple[str, TextMarkerFilter]:
    """按给定的分块喂一遍，返回（发出去的那段正文, 过滤器）。"""
    marker = TextMarkerFilter()
    shown = "".join(marker.feed(chunk) for chunk in chunks)
    return shown + marker.flush(), marker


def test_a_marker_split_across_chunks_never_leaves_the_filter() -> None:
    """**分块切开的标记**也要扣住：真实流里一个标签会跨好几块（v0.34 实测）。

    这条钉的是 §12.219 §5 那条敞口的"屏幕那一半"：正文增量是边到边发的，
    只在收尾处剥等于"库里干净、屏幕上脏过"——实测那 5 条消息的正文整条都是标记，
    于是"闪一下"几乎等于整段回答。
    """
    shown, marker = _streamed(
        [
            "我还想再核一眼。",
            "\n<tool_",
            'call>\n{"name": "web_search",',
            ' "args": {}}\n</tool_',
            "call>",
        ]
    )

    assert shown == "我还想再核一眼。", "标记一个字都不该发出去"
    assert marker.dropped is True
    assert marker.names == ["web_search"], "认出的工具名要留着（说明里要用）"


def test_a_marker_in_the_middle_leaves_the_prose_around_it() -> None:
    """夹在人话里的标记：**两边的正文照旧发**，只有标记那块消失。"""
    shown, marker = _streamed(
        ["先查一下。\n<tool_call>web_search\n", '{"query": "x"}\n</tool_call>\n', "然后再回答。"]
    )

    assert shown == "先查一下。\n然后再回答。"
    assert marker.dropped is True


def test_a_marker_that_never_closes_is_dropped_at_flush() -> None:
    """被 max_tokens 截断（收尾标签永远不来）：流结束时按完整口径吃掉。"""
    shown, marker = _streamed(["先看一眼。\n<tool_call>", '{"name": "search", "arguments": {'])

    assert shown == "先看一眼。"
    assert marker.dropped is True
    assert marker.names == ["search"]


def test_prose_is_not_held_back_or_altered() -> None:
    """**扣住正常回答是这个文件最不该犯的错**：三种"看着像其实不是"的正文都要照原样过。

    - 孤零零的 `<`（数学比较、C++ 都在这里）；
    - 引用这个标签的正常句子（后面跟的是中文，不是载荷）；
    - 含尖括号的普通正文。
    """
    for text in (
        "a < b 且 c > d，都是比较。",
        "那个 `<tool_call>` 标签是模型想调工具时写的。",
        "把 <div> 换个名字。",
        "",
    ):
        shown, marker = _streamed(list(text))

        assert shown == text, text
        assert marker.dropped is False
