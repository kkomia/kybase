"""**模型代理**端点（Phase B · P2，2026-09-29）：服务器拿**自己的 key** 调模型，
把结果 / SSE 原样转给客户端 ✓。

## 为什么单开一个 router

`api/v1/chat.py` 是**会话那条链**（检索 → 循环 → 落库 ✓），它刚被别的 lane 提交过 ✗。
代理这条链与它无关 ✓：**只做"消息进、增量出"** ✓，所以放新文件、注册一行 ✓ ——
既减少争用，也让"代理"这件事有自己的落点。

## 边界（照裁定）

- **鉴权**：用户会话令牌 ✓（`require_read` ✓，与检索端点同一档 ✓）；
- **key 不下发** ✗：上游配置从 `services.runtime.llm_for(...)` 读 ✓（服务端自己的 key ✓）；
- **客户端传的是 OpenAI 兼容的消息形状** ✓（`_message_wire` 那个咽喉的产物 ✓）——
  代理只做一次反序列化 ✓，不重新拼提示词 ✗；
- **SSE 透传** ✓：每个增量一条 `data: {json}` ✓，收尾 `data: [DONE]` ✓ ——
  壳里聊天要逐字冒 ✓（这正是我们裁定"前端直连远端"的原因 ✓）；
- **配额/计费**：**占位注释** ✓（P4 做 ✗，现在不实现 ✗）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.api.auth import require_read
from app.core.caller import Caller
from app.core.services import Services, get_services
from app.services.llm import ChatMessage, LLMDelta, OpenAICompatChat, ToolCall, ToolSpec

router = APIRouter(tags=["model-proxy"])

#: SSE 的媒体类型（与前端/壳那侧的解析口径一致 ✓）。
SSE_MEDIA_TYPE = "text/event-stream"

#: 代理这条链**默认不思考**（`ProxyRequest.thinking` 留空时用这个值）。
#:
#: 为什么（2026-09-29 现场，真模型实测）：推理模型（SiliconFlow 上的 Qwen3.5）默认会先吐
#: 一大段 ``reasoning_content``，**把 ``max_tokens`` 吃光之后 ``content`` 是空的** ——
#: `services/llm.py` 的模块说明第 9 行早就写了这条，而代理这条链当时没压。
#: 边车的工具循环要的是**正文与工具调用** ✗ 思考 ✗：实测一次真请求是
#: **81 段思考 / 0 段正文 / 0 次工具调用** → 界面上的表现就是"**空回答**"。
#: 进程内那条链（`llm.py`）在测试场景一直显式传 ``enable_thinking: false`` ✓，代理照抄同一套语义 ✓。
PROXY_DEFAULT_THINKING = False


class _WireToolCall(BaseModel):
    """助手消息里那一轮的调用（形状与 `llm._message_wire` 的产物一致：嵌套 `function` ✓）。"""

    id: str = ""
    type: str = "function"
    function: dict = Field(default_factory=dict)


class _WireMessage(BaseModel):
    """客户端传来的消息（OpenAI 兼容形状 ✓，与 `llm._message_wire` 的产物一致 ✓）。

    ⚠️ **`tool_call_id` 与 `tool_calls` 必须在这儿收下**（2026-09-29 实测 422 的根因）：
    工具循环的第二轮要发回两条特殊消息 —— ``assistant``（带 `tool_calls` ✓）与
    ``tool``（带 `tool_call_id` ✓，见 `llm.py:664-665`）。Pydantic 默认**丢掉未声明的字段** ✗，
    所以这两个字段一旦不在模型里，代理转发给上游的消息就**缺 `tool_call_id`** ✗ →
    上游直接 422：``messages[3]: missing field `tool_call_id` `` ✓✓。

    症状比错误码更难认：422 是在**流已经吐了几块之后**才发生的 ✗ → Starlette 只能抛
    ``RuntimeError: Caught handled exception, but response already started`` ✓，
    客户端看到的是"代理连不上（incomplete chunked read）" ✗ —— 而**真正的错在消息不带 id** ✓。
    """

    role: str
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: list[_WireToolCall] | None = None


class _WireTool(BaseModel):
    name: str
    description: str = ""
    parameters: dict = Field(default_factory=dict)


class ProxyRequest(BaseModel):
    messages: list[_WireMessage] = Field(default_factory=list)
    tools: list[_WireTool] = Field(default_factory=list)
    model_pk: str | None = Field(default=None, description="留空用全局默认模型")
    thinking: bool | None = Field(
        default=None,
        description=(
            "要不要让上游**思考**。留空 = **不思考**（见 `PROXY_DEFAULT_THINKING` 的理由）；"
            "显式 true 才打开。语义与进程内那条完全一致（`LLMConfig.enable_thinking` → "
            "`thinking.build_thinking_payload` 按方言翻译），**不是新造的字段**。"
        ),
    )

    @model_validator(mode="after")
    def _require_messages(self) -> ProxyRequest:
        """**空 body 必须 422，不能变成 502** ✗（P2 复盘：真请求打过一次空 body → 502 ✓）。

        502 的意思是"上游不可用" ✗，而空 body 是**参数问题** ✓。把参数问题报成上游故障，
        正是"失败分档"要防的误导（离线明示、排障、配额统计都会跟着错 ✗）。
        所以先校验：一条消息都没有就拒（422 ✓），根本不去碰模型 ✓。
        """
        if not self.messages:
            raise ValueError("messages 不能为空：至少给一条消息（role + content）")
        return self


class ProxyTextOut(BaseModel):
    text: str


def _messages(payload: ProxyRequest) -> list[ChatMessage]:
    """`ProxyRequest` → `ChatMessage`（**工具位逐字带上** ✓，见 `_WireMessage` 的说明）。

    只做反序列化 ✓：不重拼提示词 ✗、不动顺序 ✗ —— 与 `llm._message_wire` 的产物一一对应。
    """
    out: list[ChatMessage] = []
    for item in payload.messages:
        calls = tuple(
            ToolCall(
                id=call.id,
                name=str(call.function.get("name") or ""),
                arguments=str(call.function.get("arguments") or ""),
            )
            for call in (item.tool_calls or [])
        )
        out.append(
            ChatMessage(
                role=item.role,
                content=item.content,
                tool_calls=calls,
                tool_call_id=item.tool_call_id,
            )
        )
    return out


def _tools(payload: ProxyRequest) -> list[ToolSpec] | None:
    if not payload.tools:
        return None
    return [
        ToolSpec(name=item.name, description=item.description, parameters=item.parameters)
        for item in payload.tools
    ]


def _chat(services: Services, payload: ProxyRequest) -> OpenAICompatChat:
    """用**服务端**的模型档位建客户端 ✓（key 在这一侧 ✓，不下发 ✗）。

    **思考预算在这一层压住**（见 `PROXY_DEFAULT_THINKING` 的现场记录）：档位来自
    `services.runtime.llm_for(...)`（模型注册里那份），这里按请求覆写 **同一个**
    `LLMConfig.enable_thinking` —— 之后 `llm.py` 会把它交给
    `thinking.build_thinking_payload(...)` 按供应商方言翻译成正确字段 ✓（我们不自造字段 ✗）。

    TODO(P4)：配额与计费在这里挂钩子 —— 现在不做 ✗。
    """
    config = services.runtime.llm_for(payload.model_pk)
    thinking = PROXY_DEFAULT_THINKING if payload.thinking is None else payload.thinking
    if thinking != config.enable_thinking:
        config = replace(config, enable_thinking=thinking)
    return OpenAICompatChat(config)


def _sse(delta: LLMDelta) -> str:
    body: dict[str, object] = {"text": delta.text, "reasoning": delta.reasoning}
    if delta.tool_calls:
        body["tool_calls"] = [
            {
                "index": call.index,
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in delta.tool_calls
        ]
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


@router.post("/model-proxy/complete", response_model=ProxyTextOut, summary="模型代理：一次性补全")
def complete(
    payload: ProxyRequest,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> ProxyTextOut:
    """一次性补全（子 Agent、摘要这类小任务 ✓）。``caller`` 只用于鉴权 ✓。"""
    del caller
    return ProxyTextOut(text=_chat(services, payload).complete(_messages(payload)))


def _stream(payload: ProxyRequest, services: Services, *, with_tools: bool) -> Iterator[str]:
    client = _chat(services, payload)
    events = client.stream_events(_messages(payload), _tools(payload) if with_tools else None)
    for delta in events:
        yield _sse(delta)
    yield "data: [DONE]\n\n"


@router.post("/model-proxy/stream", summary="模型代理：流式（只要正文）")
def stream(
    payload: ProxyRequest,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> StreamingResponse:
    """只转正文增量 ✓（这条给普通对话用 ✓）。``caller`` 只用于鉴权 ✓。"""
    del caller
    return StreamingResponse(
        _stream(payload, services, with_tools=False), media_type=SSE_MEDIA_TYPE
    )


@router.post("/model-proxy/events", summary="模型代理：流式 + 工具调用（SSE 透传）")
def events(
    payload: ProxyRequest,
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
) -> StreamingResponse:
    """带工具位的那条（边车的工具循环用 ✓）。``caller`` 只用于鉴权 ✓。"""
    del caller
    return StreamingResponse(
        _stream(payload, services, with_tools=True), media_type=SSE_MEDIA_TYPE
    )
