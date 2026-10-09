"""两个**远端实现**的契约与失败分档（Phase B · P2）。

原则：**测试里不打真网络** ✗ —— 全部用 `httpx.MockTransport` 注入假传输 ✓。
覆盖：正常 ✓ / 超时 ✓ / 4xx ✓ / 5xx ✓ / SSE 分片重组 ✓ / "不可用"与"没命中"分开 ✓。

**注册怎么钉**（Lead 用真请求验过 ✓，判据记在这里以免下次误判）：
`POST /api/v1/model-proxy/complete` 空 body → **502**（说明路由**已注册**、handler 真跑了 ✓，
502 是因为没配模型/上游失败 ✗）；**404 才是没注册** ✗。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.knowledge_client import KnowledgeClient
from app.services.llm import ChatMessage, ToolSpec
from app.services.model_client import ModelClient
from app.services.remote_clients import (
    RemoteKnowledgeClient,
    RemoteModelClient,
    RemoteRejectedError,
    RemoteUnavailableError,
)

BASE = "http://kylab.test/api/v1"


def _knowledge(handler) -> RemoteKnowledgeClient:  # type: ignore[no-untyped-def]
    return RemoteKnowledgeClient(BASE, token="t", transport=httpx.MockTransport(handler))


def _model(handler) -> RemoteModelClient:  # type: ignore[no-untyped-def]
    return RemoteModelClient(BASE, token="t", transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ 契约


def test_both_remote_clients_satisfy_their_protocols() -> None:
    """远端实现**就是协议的一份实现** ✓（换装配点时循环不用改 ✗）。"""
    knowledge = _knowledge(lambda request: httpx.Response(200, json={"hits": []}))
    model = _model(lambda request: httpx.Response(200, json={"text": ""}))

    assert isinstance(knowledge, KnowledgeClient)
    assert isinstance(model, ModelClient)


# ------------------------------------------------------------------ KB


def test_remote_knowledge_maps_hits_to_source_refs() -> None:
    """正常返回 → 与进程内实现**同一形状**的 `SourceRef`（编号 1..N ✓）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search")
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_name": "指南.pdf",
                        "heading_path": "第 3 章",
                        "page": 7,
                        "score": 0.5,
                        "preview": "原文",
                    },
                    {"chunk_id": "c2", "document_id": "d2", "document_name": "年报.pdf"},
                ]
            },
        )

    sources = _knowledge(handler).retrieve_sources(query="储能", kb_ids=["kb_1"], top_k=3)

    assert [source.index for source in sources] == [1, 2]
    assert sources[0].document_name == "指南.pdf"
    assert sources[0].page == 7


def test_remote_knowledge_returns_empty_on_no_hits() -> None:
    """**200 且没有命中 = 空列表** ✓（这才是"没命中"，不是"不可用" ✗）。"""
    client = _knowledge(lambda request: httpx.Response(200, json={"hits": []}))

    assert client.retrieve_sources(query="没有这条", kb_ids=["kb_1"]) == []


def test_remote_knowledge_raises_unavailable_on_timeout() -> None:
    """超时 → **不可用**（抛 ✓）—— 离线要能明示，别退化成空结果 ✗。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(RemoteUnavailableError) as excinfo:
        _knowledge(handler).retrieve_sources(query="q", kb_ids=["kb"])

    assert "连不上" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_remote_knowledge_rejects_on_4xx(status: int) -> None:
    """4xx → **请求被拒**（与 5xx 分档 ✗）。"""
    client = _knowledge(lambda request: httpx.Response(status, text="nope"))

    with pytest.raises(RemoteRejectedError) as excinfo:
        client.retrieve_sources(query="q", kb_ids=["kb"])

    assert f"HTTP {status}" in str(excinfo.value)


@pytest.mark.parametrize("status", [500, 503])
def test_remote_knowledge_unavailable_on_5xx(status: int) -> None:
    client = _knowledge(lambda request: httpx.Response(status, text="boom"))

    with pytest.raises(RemoteUnavailableError):
        client.retrieve_sources(query="q", kb_ids=["kb"])


# ------------------------------------------------------------------ 模型


def test_remote_model_complete_posts_wire_messages() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"text": "回答"})

    text = _model(handler).complete([ChatMessage(role="user", content="问题")])

    assert text == "回答"
    assert str(seen["path"]).endswith("/model-proxy/complete")
    assert "问题" in str(seen["body"])


def test_remote_model_stream_reassembles_split_sse_chunks() -> None:
    """**SSE 分片重组** ✓：一个事件被 TCP 切成几段也要完整拼回来。"""
    body = (
        'data: {"text": "你"}\n\n'
        'data: {"text": "好", "reasoning": "想"}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    chunks = list(_model(handler).stream([ChatMessage(role="user", content="q")]))
    assert chunks == ["你", "好"]

    deltas = list(
        _model(handler).stream_events(
            [ChatMessage(role="user", content="q")],
            [ToolSpec(name="read_file", description="读文件", parameters={})],
        )
    )
    assert [delta.text for delta in deltas] == ["你", "好"]
    assert deltas[1].reasoning == "想"


def test_remote_model_stream_reads_the_proxy_flat_tool_call_shape() -> None:
    """代理发的是**扁平**形状（`{index,id,name,arguments}`），名字与参数不能丢 ✓。

    这是 P3 收口的最后一环，也是"**只有真实帧才走得到**"那条教训的第二次落实：
    - 代理侧 `model_proxy._sse` 发的是**扁平**形状 ✓（它自己拼的，不是上游原样透传）；
    - 而 `llm._call_deltas` 认的是 **OpenAI 嵌套**形状 ``{index,id,function:{name,arguments}}`` ✓；
    - 差一层 `function` → name/arguments 全取成空串 ✗。

    现场数字（`.shots/sidecar-path-probe.json`，真实一轮、8 件工具）：
    修之前 **13 个碎片、名字与参数全是空** ✗；修之后 **第一个碎片 `name=read_file`、
    `arguments={"path": "hello.txt"}`** ✓ —— 而"名字是空的"在循环那边的表现就是
    "**模型手上有 8 件工具却只宣布意图**" ✓。

    所以这条用例故意喂**扁平**形状：`_call_deltas` 单独用在这里会失败 ✗，
    必须经 `_proxy_call_deltas` 归一 ✓。
    """
    # 形状与 `model_proxy._sse` 逐字一致：**扁平**的 index/id/name/arguments
    arguments = json.dumps({"path": "hello.txt"}, ensure_ascii=False)
    body = (
        "data: "
        + json.dumps(
            {
                "text": "",
                "reasoning": "",
                "tool_calls": [
                    {"index": 0, "id": "call_9", "name": "read_file", "arguments": arguments}
                ],
            },
            ensure_ascii=False,
        )
        + "\n\ndata: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    deltas = list(
        _model(handler).stream_events(
            [ChatMessage(role="user", content="读 hello.txt")],
            [ToolSpec(name="read_file", description="读文件", parameters={})],
        )
    )

    calls = [call for delta in deltas for call in delta.tool_calls]
    assert calls, "扁平形状的工具调用碎片被丢了（这就是'模型只宣布意图'的根因）"
    assert calls[0].name == "read_file", f"工具名丢了：{calls[0]!r}"
    assert calls[0].arguments == '{"path": "hello.txt"}'
    assert calls[0].id == "call_9"
    assert calls[0].index == 0


def test_remote_model_stream_raises_on_5xx() -> None:
    client = _model(lambda request: httpx.Response(503, text="down"))

    with pytest.raises(RemoteUnavailableError):
        list(client.stream([ChatMessage(role="user", content="q")]))


def test_remote_model_stream_carries_tool_call_fragments() -> None:
    """**带 `tool_calls` 的帧必须转出去** ✓（2026-09-29 查实的根因）。

    为什么这条用例是必要的：代理那侧（`api/v1/model_proxy.py::_sse`）一直把
    `tool_calls` 发出去 ✓，而 `remote_clients._stream` 当时只拼了 `text` / `reasoning` ✗
    → 工具循环永远看不到调用 ✗ → 真模型上**任何工具都调不起来**，症状是"空回答" ✓。

    这一条**只有真实帧的形状才走得到**（假传输里塞一段带 `tool_calls` 的 SSE）：
    之前的用例只喂了 `text`/`reasoning` ✗，所以这个分支没被覆盖 ✗。
    """
    body = (
        'data: {"tool_calls": [{"index": 0, "id": "call_1", "function": '
        '{"name": "read_file", "arguments": "{\\"path\\": \\"a.txt\\"}"}}]}\n\n'
        'data: {"text": "读到了"}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    deltas = list(
        _model(handler).stream_events(
            [ChatMessage(role="user", content="读一下 a.txt")],
            [ToolSpec(name="read_file", description="读文件", parameters={})],
        )
    )

    assert deltas, "SSE 帧没解析出来"
    # 第一块：只有工具调用碎片，没有正文
    assert deltas[0].tool_calls, "工具调用碎片被丢了（这正是空回答的根因）"
    call = deltas[0].tool_calls[0]
    assert call.index == 0
    assert call.id == "call_1"
    assert call.name == "read_file"
    assert "a.txt" in call.arguments
    # 工具调用与正文可以同流共存：后面那块正文照旧要出来
    assert [delta.text for delta in deltas if delta.text] == ["读到了"]
    # 不带 tool_calls 的帧仍然是空元组（别为了这条把没调用的块也填上东西）
    assert deltas[1].tool_calls == ()
