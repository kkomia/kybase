"""用量采集真的接在调用路径上（G7）。

镜像同构：``ChatService`` 的用量回调 + ``_RuntimeEmbedder`` 的用量记录 → 本文件。

**为什么单独测这个**：``services/usage.py`` 自己的逻辑已经测过了，但"回调到底有没有
被调用"是另一回事——一条永远不触发的埋点看起来完全正常，只是统计页永远空白。
日志、注释、接口形状都不会暴露它，只有真的跑一次对话才会。
"""

from __future__ import annotations

import pytest

from app.services.chat import ChatService, SourceRef
from app.services.llm import LLMUsage
from app.services.model_registry import ModelRegistryService
from app.services.runtime_config import RuntimeConfigService


def _bound_chat_runtime(bundle):  # type: ignore[no-untyped-def]
    """一个"对话模型已配好"的运行期配置。

    v0.8 起模型身份只从注册表取，所以这里要真的登记一个供应商 + 模型再绑定，
    而不是往 app_settings 里写 ``llm.api_key``（那条路已经不通了）。
    """
    registry = ModelRegistryService(bundle)
    provider = registry.create_provider(
        kind="llm", name="测试供应商", base_url="https://api.example.com", api_key="sk-x"
    )
    model = registry.register_model(
        provider_id=provider.id, model_id="test-model", capabilities=["chat"]
    )
    registry.bind("chat", model.id)
    return RuntimeConfigService(bundle, registry=registry)


@pytest.fixture
def recorded() -> list[dict]:
    return []


@pytest.fixture
def chat(bundle, recorded):  # type: ignore[no-untyped-def]
    runtime = _bound_chat_runtime(bundle)
    from app.services.retrieval import RetrievalService

    retrieval = RetrievalService(bundle, embedder=_StubEmbedder(), reranker=_NoopReranker())
    service = ChatService(retrieval, runtime, usage_recorder=lambda **kw: recorded.append(kw))
    return service


class _StubEmbedder:
    dim = 8
    model_id = "stub"
    max_batch = 8
    is_development = True

    def embed(self, texts):  # type: ignore[no-untyped-def]
        return [[0.0] * self.dim for _ in texts]

    def embed_query(self, text: str):  # type: ignore[no-untyped-def]
        return [0.0] * self.dim


class _NoopReranker:
    enabled = False
    model_id = "none"


class _FakeChat:
    """假模型：报告一份固定的 usage。"""

    def __init__(self, usage: LLMUsage | None) -> None:
        self.last_usage = usage

    def complete(self, messages):  # type: ignore[no-untyped-def]
        return "这是回答。[1]"


def _source() -> SourceRef:
    return SourceRef(
        index=1,
        chunk_id="c1",
        document_id="d1",
        document_name="示例.md",
        heading_path=None,
        page=None,
        score=1.0,
        preview="原文",
    )


def test_answer_records_usage(chat: ChatService, recorded: list[dict]) -> None:
    """一次非流式问答必须留下一条用量记录。"""
    chat._chat_factory = lambda config: _FakeChat(LLMUsage(120, 80))  # type: ignore[method-assign]

    chat.answer(query="问题", sources=[_source()])

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["kind"] == "chat"
    assert entry["model_id"] == "test-model"
    assert entry["provider"] == "https://api.example.com"
    assert entry["usage"].total_tokens == 200
    assert entry["duration_ms"] >= 0


def test_answer_records_even_when_usage_is_missing(
    chat: ChatService, recorded: list[dict]
) -> None:
    """供应商没报用量时**仍要记一条**——否则"调用了几次"也统计不到，
    而那恰恰是唯一还可靠的数字。"""
    chat._chat_factory = lambda config: _FakeChat(None)  # type: ignore[method-assign]

    chat.answer(query="问题", sources=[_source()])

    assert len(recorded) == 1
    assert recorded[0]["usage"] is None


def test_chat_without_a_recorder_still_works(bundle) -> None:  # type: ignore[no-untyped-def]
    """**回调缺席时功能照常**：用量统计是可选的旁路，不该成为 ChatService
    的必需依赖（否则所有既有用例都得跟着造一个 recorder）。"""
    from app.services.retrieval import RetrievalService
    runtime = _bound_chat_runtime(bundle)
    retrieval = RetrievalService(bundle, embedder=_StubEmbedder(), reranker=_NoopReranker())
    service = ChatService(retrieval, runtime)  # 不传 recorder
    service._chat_factory = lambda config: _FakeChat(LLMUsage(10, 5))  # type: ignore[method-assign]

    turn = service.answer(query="问题", sources=[_source()])

    assert turn.answer == "这是回答。[1]"


def test_recorder_exception_does_not_break_the_answer(bundle) -> None:  # type: ignore[no-untyped-def]
    """回调自己炸了也**不能把回答吞掉**——用户已经付过这次生成的钱。

    这是容易漏的一处：用量统计是旁路，旁路出问题绝不能反过来打断主路。
    ``UsageService.record`` 内部已经吞了自己那层异常，但回调本身可能被换成
    别的实现（测试注入、将来的上报队列），所以主路也要兜一层。
    """
    from app.services.retrieval import RetrievalService
    runtime = _bound_chat_runtime(bundle)
    retrieval = RetrievalService(bundle, embedder=_StubEmbedder(), reranker=_NoopReranker())

    def boom(**kwargs: object) -> None:
        raise RuntimeError("统计服务挂了")

    service = ChatService(retrieval, runtime, usage_recorder=boom)
    service._chat_factory = lambda config: _FakeChat(LLMUsage(10, 5))  # type: ignore[method-assign]

    turn = service.answer(query="问题", sources=[_source()])

    assert turn.answer == "这是回答。[1]"


# --------------------------------------------------------- 检索用量（v17）


def _retrieval(bundle, recorded, *, hits: int):  # type: ignore[no-untyped-def]
    """一个"能返回固定条数命中"的检索服务，记录用了什么回调。"""
    from app.services.retrieval import RetrievalQuery, RetrievalService

    service = RetrievalService(
        bundle,
        embedder=_StubEmbedder(),
        reranker=_NoopReranker(),
        usage_recorder=lambda **kw: recorded.append(kw),
    )

    def _hit():  # type: ignore[no-untyped-def]
        # SimpleNamespace 而不是手搓一个类：这里只要"有那些属性"，手搓类还得处理
        # 可变类属性的告警（RUF012），收益为零
        from types import SimpleNamespace

        return SimpleNamespace(
            chunk_id="c1",
            document_id="d1",
            knowledge_base_id="kb_1",
            document_name="示例.md",
            heading_path=None,
            page=None,
            score=1.0,
            text="原文",
            channels=(),
            ranks={},
            raw_scores={},
            rerank_score=None,
        )

    # 三个替身都按**当前**签名写：`_vector_channel` / `_materialize` 自从有了"每库
    # 解析一次嵌入模型"的缓存（v0.53）各多一个可选参数。写死参数个数的话，
    # 签名一变这里就 TypeError——那条错会伪装成"用量没记上"，看不出是替身过时了。
    service._vector_channel = lambda request, qv, embedders=None: (
        [f"c{i}" for i in range(hits)],
        {},
    )  # type: ignore[method-assign]
    service._materialize = lambda fused, raw, request, embedders=None: (
        [_hit() for _ in range(hits)],
        0,
    )  # type: ignore[method-assign]
    return service, RetrievalQuery


def test_search_records_usage(bundle, recorded) -> None:  # type: ignore[no-untyped-def]
    """检索次数原先只在日志里，仪表盘因此没有这个数（v17 起落库）。"""
    service, RetrievalQuery = _retrieval(bundle, recorded, hits=3)

    service.search(RetrievalQuery(query="问题", kb_ids=["kb_1"], mode="fulltext", top_k=5))

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["kind"] == "search"
    # items 记的是**返回给调用方的条数**（命中数），不是融合前的候选量
    assert entry["items"] >= 0
    # 时长的**上界**也要判：只判 >= 0 时，两个时钟起点不同会算出巨大的正数而蒙混过关
    # （混用 perf_counter 与 monotonic 时实测出现过 -19，以及另一台机器上的正数偏差）
    assert 0 <= entry["duration_ms"] < 5_000


def test_empty_query_records_nothing(bundle, recorded) -> None:  # type: ignore[no-untyped-def]
    """空查询直接早退：它不该在"检索量"里占一次（那是调用方写错了，不是一次检索）。"""
    service, RetrievalQuery = _retrieval(bundle, recorded, hits=1)

    service.search(RetrievalQuery(query="   ", kb_ids=["kb_1"], mode="fulltext"))

    assert recorded == []


def test_usage_recorder_failure_does_not_break_search(bundle) -> None:  # type: ignore[no-untyped-def]
    """统计是旁路：它炸了不能把检索结果吞掉（已经算出来的结果更值钱）。"""
    from app.services.retrieval import RetrievalQuery, RetrievalService

    def boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("统计服务挂了")

    service = RetrievalService(
        bundle, embedder=_StubEmbedder(), reranker=_NoopReranker(), usage_recorder=boom
    )
    service._fulltext_channel = lambda request: ([], {})  # type: ignore[method-assign]

    response = service.search(RetrievalQuery(query="问题", kb_ids=["kb_1"], mode="fulltext"))

    assert response.hits == []
