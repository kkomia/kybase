"""rerank 实现的单元测试（respx 拦 HTTP）。

镜像同构：``app/services/retrieval/rerank.py`` → ``tests/unit/services/retrieval/test_rerank.py``。
"""

import json

import httpx
import pytest
import respx

from app.services.retrieval.rerank import (
    NoopReranker,
    OpenAICompatReranker,
    RerankError,
    build_reranker,
)

BASE_URL = "https://api.example.com/v1"
RERANK_URL = f"{BASE_URL}/rerank"


def _reranker(**kwargs) -> OpenAICompatReranker:
    params = {"base_url": BASE_URL, "api_key": "k", "model_id": "BAAI/bge-reranker-v2-m3"}
    params.update(kwargs)
    return OpenAICompatReranker(**params)  # type: ignore[arg-type]


def test_noop_reranker_keeps_order_and_is_disabled() -> None:
    """未配置 rerank 时必须干净跳过：不报错、也不改变顺序。"""
    reranker = NoopReranker()
    assert reranker.enabled is False
    assert reranker.rerank(query="q", documents=["甲", "乙"], top_n=2) == [(0, 0.0), (1, 0.0)]


def test_noop_reranker_honours_top_n() -> None:
    assert len(NoopReranker().rerank(query="q", documents=["甲", "乙", "丙"], top_n=2)) == 2


def test_rerank_returns_indices_sorted_by_score() -> None:
    with respx.mock:
        respx.post(RERANK_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 2, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.5},
                        {"index": 1, "relevance_score": 0.1},
                    ]
                },
            )
        )
        ranked = _reranker().rerank(query="向量检索", documents=["甲", "乙", "丙"], top_n=3)

    assert ranked == [(2, 0.9), (0, 0.5), (1, 0.1)]


def test_request_body_follows_convention() -> None:
    with respx.mock:
        route = respx.post(RERANK_URL).mock(
            return_value=httpx.Response(200, json={"results": [{"index": 0,
                                                                "relevance_score": 1.0}]})
        )
        _reranker().rerank(query="问题", documents=["文档"], top_n=1)

    body = route.calls.last.request.content.decode()
    assert '"query"' in body and '"documents"' in body and '"top_n"' in body
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"


def test_empty_documents_makes_no_request() -> None:
    with respx.mock:
        route = respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={"results": []}))
        assert _reranker().rerank(query="q", documents=[], top_n=5) == []
    assert route.call_count == 0


def test_top_n_is_capped_by_document_count() -> None:
    with respx.mock:
        route = respx.post(RERANK_URL).mock(
            return_value=httpx.Response(200, json={"results": [{"index": 0,
                                                                "relevance_score": 1.0}]})
        )
        _reranker().rerank(query="q", documents=["甲"], top_n=10)

    body = json.loads(route.calls.last.request.content)
    assert body["top_n"] == 1


def test_http_error_status_is_wrapped() -> None:
    with respx.mock:
        respx.post(RERANK_URL).mock(return_value=httpx.Response(429, text="rate limited"))
        with pytest.raises(RerankError, match="429"):
            _reranker().rerank(query="q", documents=["甲"], top_n=1)


def test_network_error_is_wrapped() -> None:
    with respx.mock:
        respx.post(RERANK_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(RerankError, match="不可达"):
            _reranker().rerank(query="q", documents=["甲"], top_n=1)


def test_malformed_response_is_wrapped() -> None:
    with respx.mock:
        respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={"oops": 1}))
        with pytest.raises(RerankError, match="不符合预期"):
            _reranker().rerank(query="q", documents=["甲"], top_n=1)


def test_result_missing_fields_is_wrapped() -> None:
    with respx.mock:
        respx.post(RERANK_URL).mock(
            return_value=httpx.Response(200, json={"results": [{"index": 0}]})
        )
        with pytest.raises(RerankError, match="缺少"):
            _reranker().rerank(query="q", documents=["甲"], top_n=1)


def test_injected_client_is_used() -> None:
    """允许注入 client 复用连接池（也便于测试）。"""
    with respx.mock:
        respx.post(RERANK_URL).mock(
            return_value=httpx.Response(
                200, json={"results": [{"index": 0, "relevance_score": 0.5}]}
            )
        )
        with httpx.Client() as client:
            ranked = _reranker(client=client).rerank(query="q", documents=["甲"], top_n=1)

    assert ranked == [(0, 0.5)]


def test_no_client_is_built_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """不注入 client 时走**进程级共享**的那个（见 ``app/core/http.py``）。

    检索链路上每次 rerank 都新建客户端，等于每次都重新握手。
    判据是硬的：把 ``httpx.Client`` 换成"一构造就炸"的替身。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})

    fake = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.retrieval.rerank.shared_client", lambda: fake)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("不该每次调用都新建 httpx.Client（见 app/core/http.py）")

    monkeypatch.setattr(httpx, "Client", explode)

    assert _reranker().rerank(query="q", documents=["甲"], top_n=1) == [(0, 0.9)]


# --------------------------------------------------------------------- 工厂


def test_factory_returns_noop_without_credentials(runtime) -> None:
    assert isinstance(build_reranker(runtime), NoopReranker)


def test_factory_returns_real_reranker_when_configured(runtime, bind_slot) -> None:
    """重排模型只在注册表里选（v0.8）：绑定了才真的启用。"""
    bind_slot("rerank", model_id="bge-reranker", capabilities=["rerank"])
    reranker = build_reranker(runtime)
    assert isinstance(reranker, OpenAICompatReranker)
    assert reranker.enabled is True


def test_factory_needs_an_api_key(runtime, bind_slot) -> None:
    """绑了个没填 Key 的供应商不算配好——否则重排会在调用时才炸。"""
    bind_slot("rerank", model_id="bge-reranker", capabilities=["rerank"], api_key="")
    assert isinstance(build_reranker(runtime), NoopReranker)
