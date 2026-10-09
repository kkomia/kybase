"""模型代理的消息反序列化：**工具位不能在代理这一跳被丢掉**。

现场（2026-09-29，真烟测）：工具循环第二轮把「助手那条带 `tool_calls` 的消息 + 工具结果那条带
`tool_call_id` 的消息」发回代理 ✓，而 `model_proxy._WireMessage` 当时只有 `role`/`content` ✗
→ Pydantic 丢掉未声明字段 ✗ → 转发给上游的消息缺 `tool_call_id` ✗ → 上游 422：
``messages[3]: missing field `tool_call_id` `` ✓。

这一组用例专治这个洞（**只有真实帧才走得到**：假上游要能看到代理真正发出去的消息 ✓）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from app.api.v1 import model_proxy
from app.services.llm import LLMDelta


def _wire(role: str, content: str, **extra: object) -> dict[str, object]:
    return {"role": role, "content": content, **extra}


def test_tool_call_id_survives_the_proxy_hop() -> None:
    """工具结果那条消息的 `tool_call_id` 必须原样到达上游 ✓（这就是 422 的根因）。"""
    arguments = '{"path": "hello.txt"}'
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": arguments},
    }
    payload = model_proxy.ProxyRequest(
        messages=[
            _wire("system", "你是本机 Agent"),
            _wire("user", "读一下 hello.txt"),
            _wire("assistant", "", tool_calls=[call]),
            _wire("tool", "文件内容：你好", tool_call_id="call_1"),
        ],
    )

    messages = model_proxy._messages(payload)

    assert [item.role for item in messages] == ["system", "user", "assistant", "tool"]
    # ① 工具结果那条：id 在
    assert messages[3].tool_call_id == "call_1"
    # ② 助手那条：调用也在（否则上游会问"这个 tool_call_id 对应哪次调用"）
    assert messages[2].tool_calls
    assert messages[2].tool_calls[0].id == "call_1"
    assert messages[2].tool_calls[0].name == "read_file"
    assert messages[2].tool_calls[0].arguments == '{"path": "hello.txt"}'


def test_tool_call_id_reaches_the_upstream_request_body() -> None:
    """再往下一跳：`llm._message_wire` 产出的线上形状里必须有 `tool_call_id` ✓。

    两跳连起来才是"上游收得到" ✓：代理反序列化 ✓ → `ChatMessage` ✓ → 线上形状 ✓。
    """
    from app.services.llm import _message_wire

    payload = model_proxy.ProxyRequest(
        messages=[_wire("tool", "文件内容：你好", tool_call_id="call_1")]
    )
    body = _message_wire(model_proxy._messages(payload)[0])

    assert body["role"] == "tool"
    assert body["tool_call_id"] == "call_1"


def test_empty_messages_still_rejected() -> None:
    """空 body 仍然是 422（P2 复盘那条别被这次改动带坏 ✓）。"""
    with pytest.raises(ValueError):
        model_proxy.ProxyRequest(messages=[])


def test_sse_serialises_a_delta_with_tool_calls() -> None:
    """`_sse` 对**带 tool_calls 的 `LLMDelta`** 必须能序列化出一行合法 `data:` ✓。

    这一条是派单里点名的：代理流到一半炸掉时，先要排除"序列化那一步抛异常" ✗。
    真因是上游 422（消息缺 `tool_call_id` ✓，见上面两条用例 ✓），但这一条留着守门 ✓。
    """
    from app.services.llm import LLMDelta, ToolCallDelta

    delta = LLMDelta(
        text="",
        reasoning="",
        tool_calls=(
            ToolCallDelta(index=0, id="call_1", name="read_file", arguments='{"path": "a"}'),
        ),
    )
    line = model_proxy._sse(delta)

    assert line.startswith("data: ") and line.endswith("\n\n")
    chunk = json.loads(line[6:].strip())
    assert chunk["tool_calls"][0]["name"] == "read_file"
    assert chunk["tool_calls"][0]["arguments"] == '{"path": "a"}'
    # 形状必须是**扁平**的（客户端 `_proxy_call_deltas` 按这个收 ✓）
    assert "function" not in chunk["tool_calls"][0]


def test_stream_yields_sse_frames_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端（假上游）：`/model-proxy/events` 出来的 SSE 里工具调用不丢 ✓。"""
    from app.services.llm import LLMConfig, ToolCallDelta

    class _FakeChat:
        def stream_events(self, messages, tools=None) -> Iterator[LLMDelta]:  # type: ignore[no-untyped-def]
            assert tools, "带工具那条必须把工具传下去"
            yield LLMDelta(text="", reasoning="", tool_calls=(
                ToolCallDelta(index=0, id="c1", name="read_file", arguments="{}"),
            ))
            yield LLMDelta(text="读完了")

    class _FakeRuntime:
        def llm_for(self, model_pk):  # type: ignore[no-untyped-def]
            # 真 `LLMConfig`：`_chat()` 会读它的 `enable_thinking` 并可能 `replace` ✓
            return LLMConfig(api_key="k", base_url="http://localhost", model_id="m")

    class _FakeServices:
        runtime = _FakeRuntime()

    monkeypatch.setattr(model_proxy, "OpenAICompatChat", lambda config: _FakeChat())

    payload = model_proxy.ProxyRequest(
        messages=[_wire("user", "读 a.txt")],
        tools=[{"name": "read_file", "description": "读文件", "parameters": {}}],
    )
    frames = list(model_proxy._stream(payload, _FakeServices(), with_tools=True))

    assert frames[-1] == "data: [DONE]\n\n"
    kinds = [json.loads(frame[6:].strip()) for frame in frames[:-1]]
    calls = [chunk for chunk in kinds if chunk.get("tool_calls")]
    assert calls, "SSE 里工具调用丢了"
    assert calls[0]["tool_calls"][0]["name"] == "read_file"
