"""WeMM-Embedding-2B 客户端的协议用例（**本地 stub HTTP 服务**，不发真实请求）。

镜像同构：``app/services/embedding/wemm.py`` → ``tests/unit/services/embedding/test_wemm.py``。

为什么用**真的 HTTP 服务**（``http.server``）而不是拦 HTTP 的假件：这一家的坑全在
**线协议**上——三步媒体协议、marker 每请求现取、502 退避、请求体里绝不能出现旧式
``image_data``。断言"我们到底发了什么"最硬的办法，就是让一个本地服务真收一次，
再看它收到的东西（拦 HTTP 的假件容易被写成"我以为我发了什么"）。

用例**不依赖任何 pytest 夹具**（本机没有 ``KYLAB_TEST_DATABASE_URL`` 时，conftest 的
autouse 夹具会把整个套件 skip 掉；这几个文件因此能在无库环境下直接跑）。
"""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.services.embedding.base import EmbeddingError
from app.services.embedding.wemm import (
    MEDIA_TIMEOUT_SECONDS,
    NATIVE_DIM,
    PROPS_TIMEOUT_SECONDS,
    TEXT_TIMEOUT_SECONDS,
    WeMMEmbedder,
)

#: 默认的原生向量：前两位 0.6 / 0.8（范数正好 1），后面补零。
#: 前两位刻意不是"单位基向量"：截断到 1 维时才能看出到底有没有**重新归一化**
#: （只截断不给 1.0，重归一化给 1.0）。
_DEFAULT_VECTOR = [0.6, 0.8]


@dataclass
class _Reply:
    """一条预置响应。``json`` 与 ``text`` 二选一；``delay`` 用来模拟慢响应。"""

    status: int = 200
    json: object | None = None
    text: str = ""
    delay: float = 0.0


@dataclass
class _Stub:
    """本地假 WeMM 服务：``/props``、``/v1/embeddings``、``/embedding`` 三个口。"""

    marker: str = "<__media_first__>"
    native_dim: int = NATIVE_DIM
    #: 服务返回的原生向量（长度应等于 native_dim）
    vector: list[float] = field(default_factory=lambda: list(_DEFAULT_VECTOR))
    #: 按 index 逐个给的向量（非空时优先）：用来验"顺序与 index 对应"
    vectors: list[list[float]] = field(default_factory=list)
    #: 文本响应里把 data 倒序返回，验"按 index 排序"
    reverse_text_data: bool = False
    #: 预置响应队列（按路径），用完则走默认行为
    replies: dict[str, list[_Reply]] = field(default_factory=dict)
    #: 收到过的请求：``{"method", "path", "body", "authorization"}``
    requests: list[dict] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------------------------------------------------------------- 数据

    def native_vector(self) -> list[float]:
        vector = list(self.vector)
        return vector + [0.0] * max(0, self.native_dim - len(vector))

    def vector_for(self, index: int) -> list[float]:
        if not self.vectors:
            return self.native_vector()
        vector = list(self.vectors[index % len(self.vectors)])
        return vector + [0.0] * max(0, self.native_dim - len(vector))

    def media_payload(self) -> list[dict]:
        return [{"embedding": [self.native_vector()]}]

    def next_reply(self, path: str) -> _Reply | None:
        queue = self.replies.get(path) or []
        return queue.pop(0) if queue else None

    def paths(self) -> list[str]:
        return [item["path"] for item in self.requests]

    def bodies(self, path: str) -> list[object]:
        return [item["body"] for item in self.requests if item["path"] == path]

    def body_text(self, path: str) -> str:
        return json.dumps(self.bodies(path), ensure_ascii=False)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # pragma: no cover - 别把访问日志打进用例输出
        return

    def handle_one_request(self) -> None:
        """客户端因超时先断开时**别打栈**。

        冷启动那条用例会故意让客户端超时：它断开之后，服务这边还停在等下一行请求上，
        ``socketserver`` 会把那次连接中断当异常打出来。那是**预期内**的情形，
        而用例输出里出现一段与断言无关的栈，会让人以为出了别的问题。
        """
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # ---------------------------------------------------------------- 收

    def _record(self, *, with_body: bool) -> object:
        stub: _Stub = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if with_body and length else b""
        body: object = None
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode("utf-8", errors="replace")
        with stub.lock:
            stub.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            stub.in_flight += 1
            stub.max_in_flight = max(stub.max_in_flight, stub.in_flight)
        return body

    def _finish(self, reply: _Reply) -> None:
        stub: _Stub = self.server.stub  # type: ignore[attr-defined]
        try:
            if reply.delay:
                time.sleep(reply.delay)
            if reply.json is not None:
                payload = json.dumps(reply.json).encode()
            else:
                payload = (reply.text or "").encode()
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # **预期内的一种情形**：冷启动那条用例里客户端超时先走了，
            # 我们这边才把（慢吞吞的）响应写出去。这不是服务出错，别往用例输出里打栈。
            return
        finally:
            with stub.lock:
                stub.in_flight -= 1

    # ---------------------------------------------------------------- 路由

    def do_GET(self) -> None:
        """``BaseHTTPRequestHandler`` 的约定命名（N802 未启用，不必加 noqa）。"""
        stub: _Stub = self.server.stub  # type: ignore[attr-defined]
        self._record(with_body=False)
        reply = stub.next_reply(self.path)
        if reply is None:
            reply = _Reply(json={"media_marker": stub.marker})
        self._finish(reply)

    def do_POST(self) -> None:
        """同上：``BaseHTTPRequestHandler`` 的约定命名。"""
        stub: _Stub = self.server.stub  # type: ignore[attr-defined]
        body = self._record(with_body=True)
        reply = stub.next_reply(self.path)
        if reply is None:
            reply = self._default_reply(self.path, body)
        self._finish(reply)

    def _default_reply(self, path: str, body: object) -> _Reply:
        stub: _Stub = self.server.stub  # type: ignore[attr-defined]
        if path == "/v1/embeddings":
            inputs = body.get("input") if isinstance(body, dict) else None
            count = len(inputs) if isinstance(inputs, list) else 1
            data = [
                {"object": "embedding", "index": index, "embedding": stub.vector_for(index)}
                for index in range(count)
            ]
            if stub.reverse_text_data:
                data.reverse()
            return _Reply(json={"model": "WeMM-Embedding-2B-Q4_K_M.gguf", "data": data})
        if path == "/embedding":
            # **marker 不对是服务端的行为**：接的是 prompt_string 里那个值，
            # 与它当前的不一致就报错（这正是"服务重启后旧 marker 失效"的样子）
            content = body.get("content") if isinstance(body, dict) else None
            marker = content.get("prompt_string") if isinstance(content, dict) else None
            if marker != stub.marker:
                return _Reply(status=400, json={"error": f"invalid media_marker: {marker}"})
            return _Reply(json=stub.media_payload())
        return _Reply(status=404, json={"error": f"no such endpoint: {path}"})


@contextmanager
def _serve(stub: _Stub) -> Iterator[tuple[str, _Stub]]:
    """起一个本地 stub 服务，交出它的 ``base_url``。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.stub = stub  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _client() -> httpx.Client:
    """专用客户端：**绕开环境里的代理**（127.0.0.1 不该走代理）。"""
    return httpx.Client(trust_env=False)


def _embedder(base_url: str, **kwargs: object) -> WeMMEmbedder:
    params: dict[str, object] = {
        "base_url": base_url,
        "model_id": "WeMM-Embedding-2B-Q4_K_M.gguf",
        "dim": NATIVE_DIM,
        "client": kwargs.pop("client", None) or _client(),
        "sleep": kwargs.pop("sleep", lambda _seconds: None),
    }
    params.update(kwargs)
    return WeMMEmbedder(**params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- 文本


def test_text_batch_is_one_request_with_array_input() -> None:
    """批量**一个请求、input 是数组**：文档 §7 的吞吐就是这样来的（并发反而不变快）。"""
    with _serve(_Stub()) as (base, stub):
        vectors = _embedder(base, max_batch=8).embed(["甲", "乙", "丙"])

    assert stub.paths() == ["/v1/embeddings"]
    body = stub.bodies("/v1/embeddings")[0]
    assert body["input"] == ["甲", "乙", "丙"]
    assert body["model"] == "WeMM-Embedding-2B-Q4_K_M.gguf"
    assert len(vectors) == 3
    assert all(len(vector) == NATIVE_DIM for vector in vectors)
    assert sum(value * value for value in vectors[0]) == pytest.approx(1.0)


def test_text_batching_splits_by_max_batch_sequentially() -> None:
    """超过批大小就分几个请求，但**串行发**（不建线程、不并发）。"""
    stub = _Stub(vector=[1.0, 0.0])
    with _serve(stub) as (base, _):
        vectors = _embedder(base, dim=4, max_batch=1).embed(["甲", "乙", "丙"])

    assert stub.paths() == ["/v1/embeddings"] * 3
    assert stub.max_in_flight == 1
    assert len(vectors) == 3


def test_text_results_follow_input_order_not_response_order() -> None:
    """响应里的 data 不保证顺序：取错顺序会让向量与文本静默错位。"""
    stub = _Stub(
        vectors=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]],
    )
    stub.reverse_text_data = True
    with _serve(stub) as (base, _):
        vectors = _embedder(base, dim=4).embed(["第一条", "第二条", "第三条"])

    assert vectors[0][0] == pytest.approx(1.0)
    assert vectors[1][1] == pytest.approx(1.0)
    assert vectors[2][2] == pytest.approx(1.0)


def test_empty_input_makes_no_request() -> None:
    with _serve(_Stub()) as (base, stub):
        assert _embedder(base).embed([]) == []
    assert stub.paths() == []


def test_no_key_means_no_authorization_header() -> None:
    """局域网那台没有鉴权：不该硬塞一个空的 Bearer（文档 §5 的客户端就不发）。"""
    with _serve(_Stub()) as (base, stub):
        _embedder(base).embed(["甲"])
    assert stub.requests[0]["authorization"] is None


def test_key_is_forwarded_when_configured() -> None:
    with _serve(_Stub()) as (base, stub):
        _embedder(base, api_key="local-key").embed(["甲"])
    assert stub.requests[0]["authorization"] == "Bearer local-key"


# ---------------------------------------------------------------------- 图片 / 视频三步


def test_media_uses_three_steps_and_never_image_data() -> None:
    """媒体三步 + **绝不能出现旧式 ``image_data`` / ``[img-N]``**。

    这是文档里唯一一个"用了也不报错"的坑：llama.cpp 会把旧式字段当纯文本静默忽略，
    图片等于没嵌，而调用方以为成功了。所以这里把**整个请求体**翻出来查。
    """
    image = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
    with _serve(_Stub(marker="<__media_abc__>")) as (base, stub):
        vector = _embedder(base).embed_media(image)

    assert stub.paths() == ["/props", "/embedding"]
    body = stub.bodies("/embedding")[0]
    assert body == {
        "content": {
            "prompt_string": "<__media_abc__>",
            "multimodal_data": [base64.b64encode(image).decode()],
        }
    }
    text = stub.body_text("/embedding")
    assert "image_data" not in text
    assert "[img-" not in text
    assert len(vector) == NATIVE_DIM
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_media_accepts_openai_style_response() -> None:
    """OpenAI 兼容端点回的是 ``{"data": [...]}``：两条路文档都写着可用，都认。"""
    stub = _Stub()
    stub.replies["/embedding"] = [
        _Reply(json={"data": [{"index": 0, "embedding": stub.native_vector()}]})
    ]
    with _serve(stub) as (base, _):
        vector = _embedder(base).embed_media(b"bytes")

    assert len(vector) == NATIVE_DIM


def test_media_marker_is_fetched_for_every_request() -> None:
    """marker **每请求现取**：服务重启（含冷启动）之后它就变了，缓存下来的会一直失败。"""
    stub = _Stub(marker="<__media_v1__>")
    with _serve(stub) as (base, _):
        embedder = _embedder(base)
        embedder.embed_media(b"first")
        stub.marker = "<__media_v2__>"  # 服务重启了
        embedder.embed_media(b"second")

    assert stub.paths() == ["/props", "/embedding", "/props", "/embedding"]
    markers = [
        item["body"]["content"]["prompt_string"]
        for item in stub.requests
        if item["method"] == "POST"
    ]
    assert markers == ["<__media_v1__>", "<__media_v2__>"]


def test_stale_marker_is_refetched_and_retried_once() -> None:
    """marker 报错 → 重取一次 marker 再试（文档 §6 最后一行）。

    现场：服务在两次请求之间重启过，旧 marker 失效。这种情况**不该让一份文档白失败**。
    """
    stub = _Stub(marker="<__media_old__>")
    sleeps: list[float] = []
    with _serve(stub) as (base, _):
        embedder = _embedder(base, sleep=sleeps.append)
        # 第一次媒体请求一定失败（服务那边还是旧 marker），随后服务换了 marker
        stub.replies["/embedding"] = [_Reply(status=400, json={"error": "media_marker not found"})]
        original = embedder.media_marker

        def swap() -> str:
            stub.marker = "<__media_new__>"
            return original()

        embedder.media_marker = swap  # type: ignore[method-assign]
        vector = embedder.embed_media(b"bytes")

    assert len(vector) == NATIVE_DIM
    assert stub.paths() == ["/props", "/embedding", "/props", "/embedding"]
    # 第二次带的是**新** marker
    assert stub.bodies("/embedding")[1]["content"]["prompt_string"] == "<__media_new__>"
    # marker 失效不进退避重试：它是预期内的一种失败，不该白等
    assert sleeps == []


# ---------------------------------------------------------------------- 502 退避 / 冷启动


def test_502_is_retried_with_exponential_backoff() -> None:
    stub = _Stub()
    sleeps: list[float] = []
    stub.replies["/v1/embeddings"] = [
        _Reply(status=502, json={"error": "unavailable"}),
        _Reply(status=502, json={"error": "unavailable"}),
    ]
    with _serve(stub) as (base, _):
        vectors = _embedder(base, dim=4, sleep=sleeps.append).embed(["甲"])

    assert len(vectors) == 1
    assert stub.paths() == ["/v1/embeddings"] * 3
    assert sleeps == [1.0, 2.0]


def test_persistent_502_reports_the_service_is_unavailable() -> None:
    stub = _Stub()
    sleeps: list[float] = []
    stub.replies["/v1/embeddings"] = [_Reply(status=502, json={"error": "down"})] * 3
    with _serve(stub) as (base, _), pytest.raises(EmbeddingError, match="不可用") as excinfo:
        _embedder(base, dim=4, sleep=sleeps.append).embed(["甲"])

    assert "502" in str(excinfo.value)
    assert sleeps == [1.0, 2.0]
    # 报错里要给出下一步（冷启动 / 代理没起来），而不是一句"失败了"
    assert "wemm-proxy" in str(excinfo.value)


def test_cold_start_slow_first_request_is_retried_not_failed() -> None:
    """冷启动：第一个请求要等模型加载（约 10s）。**一次慢不等于失败**。

    现场用"第一次响应比客户端超时还慢"复现：第一次必然超时（传输层错误），
    退避之后第二次成功。用例里不等真的 10s——超时值调小、退避睡眠注入。
    """
    stub = _Stub()
    sleeps: list[float] = []
    stub.replies["/v1/embeddings"] = [_Reply(json={"data": []}, delay=0.4)]
    with _serve(stub) as (base, _):
        embedder = _embedder(base, dim=4, text_timeout=0.15, sleep=sleeps.append)
        vectors = embedder.embed(["甲"])

    assert len(vectors) == 1
    assert sleeps == [1.0]
    assert stub.paths() == ["/v1/embeddings"] * 2


def test_timeouts_are_at_least_the_documented_floors() -> None:
    """超时下限是文档写死的：文本 ≥30s、媒体 ≥120s（冷启动约 10s，视频要更久）。"""
    assert TEXT_TIMEOUT_SECONDS >= 30.0
    assert MEDIA_TIMEOUT_SECONDS >= 120.0
    assert PROPS_TIMEOUT_SECONDS >= 30.0


# ---------------------------------------------------------------------- 维度 / 归一化 / 非法响应


def test_native_dimension_is_validated() -> None:
    """不是 2048 维就报错：服务被换过 / 启动参数带了截断，都必须当场暴露。"""
    stub = _Stub(native_dim=8, vector=[1.0, 0.0])
    with _serve(stub) as (base, _), pytest.raises(EmbeddingError, match="2048"):
        _embedder(base, dim=8).embed(["甲"])


def test_unnormalized_vector_is_rejected() -> None:
    """"服务保证已归一化"不等于"不必校验"：范数不对时余弦与点积就不再等价。"""
    stub = _Stub(vector=[1.0, 1.0])  # 补零到 2048 维，范数 √2（≈1.414）
    with _serve(stub) as (base, _), pytest.raises(EmbeddingError, match="范数"):
        _embedder(base, dim=4).embed(["甲"])


def test_matryoshka_truncation_renormalizes() -> None:
    """截断到更小维度时必须**重新归一化**（只截断的话点积就不再是余弦）。

    现场用的原生向量前两位是 0.6 / 0.8（其余补零，范数正好 1），截到 1 维之后：
    只截断会得到 0.6，重新归一化才是 1.0——这就是"截断了但相似度悄悄不对"的那种坏法。
    """
    stub = _Stub(vector=[0.6, 0.8])
    with _serve(stub) as (base, _):
        vectors = _embedder(base, dim=1).embed(["甲"])

    assert vectors[0] == pytest.approx([1.0])

    # 目标维度 2：前两位原样保留（它们本来就是归一化的）
    with _serve(_Stub(vector=[0.6, 0.8])) as (base, _):
        full = _embedder(base, dim=2).embed(["甲"])[0]
    assert full == pytest.approx([0.6, 0.8])
    assert sum(value * value for value in full) == pytest.approx(1.0)


def test_target_dimension_longer_than_native_is_rejected() -> None:
    """目标维度大于原生：补零会造出一个与任何文本都不相似的假向量，必须报错。"""
    stub = _Stub()
    with _serve(stub) as (base, _), pytest.raises(EmbeddingError, match="少于配置"):
        _embedder(base, dim=NATIVE_DIM * 2).embed(["甲"])


def test_frozen_kb_dimension_1024_truncates_and_keeps_cosine_equal_to_dot() -> None:
    """库记录里冻结的是 1024 维：按 Matryoshka 截到 1024 + 重归一化，**不是拒绝**。

    这是"老库（1024）也能换用 WeMM"的那条路：维度由库决定，客户端负责把 2048 的原生
    向量对齐到它。判据落在**余弦仍然等于点积**上——这正是"必须重新归一化"的意义
    （只截断不归一化时两者会差一个模长因子，检索排序跟着歪）。
    """
    stub = _Stub(vectors=[[0.6, 0.8], [0.8, 0.6]])
    with _serve(stub) as (base, _):
        a, b = _embedder(base, dim=1024).embed(["甲", "乙"])

    assert len(a) == len(b) == 1024
    assert math.sqrt(sum(value * value for value in a)) == pytest.approx(1.0)
    assert math.sqrt(sum(value * value for value in b)) == pytest.approx(1.0)

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    cosine = dot / (
        math.sqrt(sum(value * value for value in a))
        * math.sqrt(sum(value * value for value in b))
    )
    assert dot == pytest.approx(0.96)  # 0.6*0.8 + 0.8*0.6
    assert cosine == pytest.approx(dot)  # 归一化之后余弦与点积是一回事


def test_malformed_responses_are_rejected() -> None:
    """响应形状不对一律报错，**绝不静默写入**。"""
    cases: list[tuple[str, object]] = [
        ("没有 data", {"oops": 1}),
        ("data 不是数组", {"data": "nope"}),
        ("条数不符", {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}),
        ("embedding 不是数组", {"data": [{"index": 0, "embedding": "nope"}]}),
        ("embedding 含非数字", {"data": [{"index": 0, "embedding": [1.0, "x"]}]}),
        ("embedding 为空", {"data": [{"index": 0, "embedding": []}]}),
    ]
    for name, payload in cases:
        stub = _Stub(vector=[1.0, 0.0])
        stub.replies["/v1/embeddings"] = [_Reply(json=payload)]
        with _serve(stub) as (base, _), pytest.raises(EmbeddingError):
            _embedder(base, dim=4).embed(["甲", "乙"])
        assert name  # 只用于失败时的可读性（用例名里看不出是哪一条）


def test_non_json_response_is_rejected() -> None:
    stub = _Stub(vector=[1.0, 0.0])
    stub.replies["/v1/embeddings"] = [_Reply(text="<html>502</html>")]
    with _serve(stub) as (base, _), pytest.raises(EmbeddingError, match="不是 JSON"):
        _embedder(base, dim=4).embed(["甲"])


def test_media_empty_bytes_is_rejected_before_any_request() -> None:
    with _serve(_Stub()) as (base, stub), pytest.raises(EmbeddingError, match="媒体内容为空"):
        _embedder(base).embed_media(b"")
    assert stub.paths() == []


def test_props_without_marker_is_rejected() -> None:
    stub = _Stub()
    stub.replies["/props"] = [_Reply(json={"model_alias": "x"})]
    with _serve(stub) as (base, _), pytest.raises(EmbeddingError, match="media_marker"):
        _embedder(base).embed_media(b"bytes")


def test_only_embedding_endpoints_are_called() -> None:
    """**只碰 embedding 接口**：那台服务上的模型是 embedding 专用的（文档 §2 第 4 条），
    ``/completion`` 一次都不该发。"""
    stub = _Stub()
    with _serve(stub) as (base, _):
        embedder = _embedder(base)
        embedder.embed(["甲"])
        embedder.embed_media(b"bytes")

    assert set(stub.paths()) == {"/props", "/v1/embeddings", "/embedding"}


def test_zero_dimension_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        WeMMEmbedder(base_url="http://127.0.0.1:1", model_id="m", dim=0)


def test_supports_media_is_advertised() -> None:
    """这一位是"摄入要不要走媒体接口"的判据，不能靠 isinstance 猜。"""
    assert WeMMEmbedder.supports_media is True
