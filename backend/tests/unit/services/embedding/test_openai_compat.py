"""OpenAI 兼容 embedding 客户端的单元测试（用 respx 拦 HTTP，不发真实请求）。

镜像同构：``app/services/embedding/openai_compat.py``
→ ``tests/unit/services/embedding/test_openai_compat.py``。
"""

import httpx
import pytest
import respx

from app.services.embedding.openai_compat import OpenAICompatEmbedder

BASE_URL = "https://api.example.com/v1"
EMBEDDINGS_URL = f"{BASE_URL}/embeddings"
DIM = 4


def _embedder(**kwargs) -> OpenAICompatEmbedder:
    params = {
        "base_url": BASE_URL,
        "api_key": "test-key",
        "model_id": "BAAI/bge-m3",
        "dim": DIM,
    }
    params.update(kwargs)
    return OpenAICompatEmbedder(**params)


def _response(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": items, "model": "BAAI/bge-m3"})


def test_happy_path_returns_normalized_vectors() -> None:
    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(
            return_value=_response(
                [
                    {"index": 0, "embedding": [3.0, 4.0, 0.0, 0.0]},
                ]
            )
        )
        vectors = _embedder().embed(["一段文本"])

    assert len(vectors) == 1
    assert sum(value * value for value in vectors[0]) == pytest.approx(1.0)
    assert vectors[0][0] == pytest.approx(0.6)


def test_results_are_ordered_by_index_not_arrival() -> None:
    """响应里的 data 不保证顺序；取错顺序会让向量与文本静默错位。"""
    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(
            return_value=_response(
                [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                ]
            )
        )
        vectors = _embedder().embed(["第一条", "第二条"])

    assert vectors[0][0] == pytest.approx(1.0)
    assert vectors[1][1] == pytest.approx(1.0)


def test_request_body_follows_openai_spec() -> None:
    with respx.mock:
        route = respx.post(EMBEDDINGS_URL).mock(
            return_value=_response([{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}])
        )
        _embedder().embed(["甲"])

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-key"
    body = request.content.decode()
    assert '"model"' in body and "BAAI/bge-m3" in body
    assert '"input"' in body


def test_batching_splits_requests() -> None:
    with respx.mock:
        route = respx.post(EMBEDDINGS_URL).mock(
            side_effect=[
                _response([{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]),
                _response([{"index": 0, "embedding": [0.0, 1.0, 0.0, 0.0]}]),
            ]
        )
        vectors = _embedder(max_batch=1).embed(["甲", "乙"])

    assert route.call_count == 2
    assert len(vectors) == 2


def test_empty_input_makes_no_request() -> None:
    with respx.mock:
        route = respx.post(EMBEDDINGS_URL).mock(return_value=_response([]))
        assert _embedder().embed([]) == []
    assert route.call_count == 0


def test_401_mentions_token_or_quota() -> None:
    """MinerU/embedding 的 401 基本都是 token 失效或额度用尽，错误信息要直接点出来。"""
    from app.services.embedding.base import EmbeddingError

    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
        with pytest.raises(EmbeddingError, match="401"):
            _embedder().embed(["甲"])


def test_server_error_is_wrapped() -> None:
    from app.services.embedding.base import EmbeddingError

    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(EmbeddingError, match="500"):
            _embedder().embed(["甲"])


def test_malformed_response_is_wrapped() -> None:
    from app.services.embedding.base import EmbeddingError

    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(200, json={"oops": 1}))
        with pytest.raises(EmbeddingError, match="不符合 OpenAI 规范"):
            _embedder().embed(["甲"])


def test_dimension_mismatch_is_rejected_with_actionable_message() -> None:
    """维度不符会污染整个向量空间，必须硬失败并把配置项名字点出来。"""
    from app.services.embedding.base import EmbeddingError

    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(
            return_value=_response([{"index": 0, "embedding": [1.0, 0.0]}])  # 2 维，声明 4 维
        )
        with pytest.raises(EmbeddingError, match="KYLAB_EMBEDDING_DIM"):
            _embedder().embed(["甲"])


def test_count_mismatch_is_rejected() -> None:
    from app.services.embedding.base import EmbeddingError

    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(
            return_value=_response([{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}])
        )
        with pytest.raises(EmbeddingError, match="不一致"):
            _embedder().embed(["甲", "乙"])


def test_zero_dimension_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        _embedder(dim=0)


def test_base_url_trailing_slash_is_normalized() -> None:
    with respx.mock:
        route = respx.post(EMBEDDINGS_URL).mock(
            return_value=_response([{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}])
        )
        _embedder(base_url=f"{BASE_URL}/").embed(["甲"])
    assert route.call_count == 1


def test_injected_client_is_reused() -> None:
    """允许注入 client，便于复用连接池（也便于测试）。"""
    with respx.mock:
        respx.post(EMBEDDINGS_URL).mock(
            return_value=_response([{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}])
        )
        with httpx.Client() as client:
            vectors = _embedder(client=client).embed(["甲"])
    assert len(vectors) == 1


def test_no_client_is_built_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """不注入 client 时走**进程级共享**的那个（见 ``app/core/http.py``）。

    摄入一篇文档按 batch 会调好几次 embedding，以前每次调用都新建客户端——
    那就是每次重新握手。判据是硬的：把 ``httpx.Client`` 换成"一构造就炸"的替身。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _response([{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}])

    fake = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.embedding.openai_compat.shared_client", lambda: fake)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("不该每次调用都新建 httpx.Client（见 app/core/http.py）")

    monkeypatch.setattr(httpx, "Client", explode)

    assert len(_embedder().embed(["甲"])) == 1
