"""一个库的嵌入服务坏掉，不该把整次跨库检索带走（v0.65）。

镜像同构：``app/services/retrieval/service.py`` 的 ``_vector_channel`` → 本文件。

**为什么用桩仓储**：这几条要验的是**降级的形状**——哪个库被跳过、谁照旧召回、
``decision`` 里怎么如实说明，不是 SQL。真库那一侧（真跨库、真嵌入实现）由
``tests/integration/services/test_retrieval.py`` 管；这里把"哪个库的 embedder 抛什么"
摆成唯一自变量，才判得出"降级只发生在那一个库身上"。

**现场**（实测）：7 个库里只有 1 个用 WeMM，它的嵌入端点连不上时，整次跨库检索全灭
——别的 6 个库打得开的全文通道、算得出的查询向量，调用方一条都拿不到。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from app.core.exceptions import EmbeddingError, EmbeddingNotConfiguredError
from app.models.enums import DataSourceKind, DocumentStage
from app.services.retrieval import RetrievalMode, RetrievalQuery, RetrievalService
from app.services.retrieval.rerank import NoopReranker
from app.storage.base import ChunkRecord, DocumentRecord, SearchHit, VectorMatch

#: 用例不碰任何仓储（全程进程内替身），所以按 ``local`` 那一档跑（见 conftest 说明）。
pytestmark = pytest.mark.local

BROKEN_KB = "kb_wemm"
HEALTHY_KB = "kb_siliconflow"

BROKEN_REASON = "WeMM 嵌入服务不可用（3 次尝试都失败）：ConnectTimeout"
QUERY = "眼轴长度"

OK_VECTOR_CHUNK = "c_ok_vector"
BROKEN_FULLTEXT_CHUNK = "c_broken_fulltext"
OK_FULLTEXT_CHUNK = "c_ok_fulltext"

#: 两个库用的都是**没标定过**的模型 id：判定整套关掉、也不做收敛（``FIT_UNKNOWN``）。
#: 这条用例要看的不是"哪些候选被判定层留下"，而是"降级发生在谁身上"——
#: 让分布那一套在旁边不起作用，形状才干净。
BROKEN_MODEL = "text-embedding-3-small"
HEALTHY_MODEL = "dev/deterministic-hash"


class _Embedder:
    """桩嵌入实现：给一个定值向量，或者抛给定的异常。"""

    is_development = False
    supports_media = False

    def __init__(self, model_id: str, *, error: Exception | None = None) -> None:
        self.model_id = model_id
        self.dim = 4
        self._error = error

    def embed(self, texts):  # type: ignore[no-untyped-def]
        if self._error is not None:
            raise self._error
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _Resolver:
    """``EmbeddingResolver`` 的最小替身：按库发不同的嵌入实现（模型是库属性）。"""

    def __init__(self, by_kb: dict[str, _Embedder]) -> None:
        self._by_kb = by_kb

    def for_kb(self, kb: object) -> _Embedder:
        return self._by_kb[kb.id]  # type: ignore[attr-defined]


class _Vectors:
    """向量通道的替身：记下**为哪个库真的查了**（"跳过"与"查了空"由此分得开）。"""

    def __init__(self, by_kb: dict[str, list[VectorMatch]]) -> None:
        self._by_kb = by_kb
        self.calls: list[str] = []

    def search(self, kb_id, *, query_vector, top_k, filter=None):  # type: ignore[no-untyped-def]
        self.calls.append(kb_id)
        return self._by_kb.get(kb_id, [])


class _FullText:
    """全文通道的替身：每个库各给几条词面命中。"""

    def __init__(self, by_kb: dict[str, list[SearchHit]]) -> None:
        self._by_kb = by_kb

    def search(self, *, query, top_k, kb_id=None, filter=None):  # type: ignore[no-untyped-def]
        return self._by_kb.get(kb_id, [])


class _Meta:
    """元数据替身：只给检索真正读的那几个方法。"""

    def __init__(self, chunks: list[ChunkRecord], documents: list[DocumentRecord]) -> None:
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self._documents = {document.id: document for document in documents}

    def any_disabled_documents(self, kb_ids) -> bool:  # type: ignore[no-untyped-def]
        return False

    def get_knowledge_base(self, kb_id: str) -> object:
        # 只被 ``_embedder_for`` 用来问"这个库用哪个模型"，桩 resolver 按 id 取
        return SimpleNamespace(id=kb_id, embedding_model_pk=None)

    def get_chunks(self, chunk_ids) -> list[ChunkRecord]:  # type: ignore[no-untyped-def]
        return [self._chunks[chunk_id] for chunk_id in chunk_ids if chunk_id in self._chunks]

    def get_documents_by_ids(self, document_ids: Sequence[str]) -> dict[str, DocumentRecord]:
        return {
            document_id: self._documents[document_id]
            for document_id in document_ids
            if document_id in self._documents
        }


def _document(document_id: str, kb_id: str, name: str) -> DocumentRecord:
    return DocumentRecord(
        id=document_id,
        knowledge_base_id=kb_id,
        name=name,
        source_kind=DataSourceKind.UPLOAD,
        content_hash=f"h-{document_id}",
        stage=DocumentStage.INDEXED,
    )


def _chunk(chunk_id: str, document_id: str, kb_id: str, text: str) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        knowledge_base_id=kb_id,
        part_id=None,
        ordinal=0,
        text=text,
        content_hash=f"h-{chunk_id}",
    )


def _hit(chunk: ChunkRecord, score: float) -> SearchHit:
    return SearchHit(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        knowledge_base_id=chunk.knowledge_base_id,
        text=chunk.text,
        score=score,
        source="fulltext",
    )


def _service(error: Exception | None) -> tuple[RetrievalService, _Vectors]:
    """两个库、两条通道：``BROKEN_KB`` 的 embedder 按 ``error`` 抛（``None`` = 都正常）。

    两条通道都摆上命中，是为了让"降级之后还剩什么"看得出来：
    ``HEALTHY_KB`` 有向量命中 + 全文命中，``BROKEN_KB`` 有全文命中。
    """
    chunks = [
        _chunk(OK_VECTOR_CHUNK, "doc_ok", HEALTHY_KB, "眼轴长度是衡量眼球发育的主要参数"),
        _chunk(OK_FULLTEXT_CHUNK, "doc_ok", HEALTHY_KB, "眼轴长度不受调节能力影响"),
        _chunk(BROKEN_FULLTEXT_CHUNK, "doc_broken", BROKEN_KB, "眼轴长度的正常范围是多少"),
    ]
    documents = [
        _document("doc_ok", HEALTHY_KB, "眼轴.md"),
        _document("doc_broken", BROKEN_KB, "医学笔记.md"),
    ]
    vectors = _Vectors({HEALTHY_KB: [VectorMatch(OK_VECTOR_CHUNK, 0.1)]})
    fulltext = _FullText(
        {
            HEALTHY_KB: [_hit(chunks[1], 9.9)],
            BROKEN_KB: [_hit(chunks[2], 8.8)],
        }
    )
    healthy = _Embedder(HEALTHY_MODEL)
    broken = _Embedder(BROKEN_MODEL, error=error)
    service = RetrievalService(  # type: ignore[arg-type]
        SimpleNamespace(meta=_Meta(chunks, documents), vectors=vectors, fulltext=fulltext),
        embedder=healthy,
        reranker=NoopReranker(),
        embedders=_Resolver({HEALTHY_KB: healthy, BROKEN_KB: broken}),  # type: ignore[arg-type]
    )
    return service, vectors


def _search(service, kb_ids: list[str], **overrides: object):  # type: ignore[no-untyped-def]
    return service.search(  # type: ignore[attr-defined]
        RetrievalQuery(query=QUERY, kb_ids=kb_ids, top_k=10, **overrides)  # type: ignore[arg-type]
    )


def _warnings(caplog) -> list[str]:  # type: ignore[no-untyped-def]
    """只看检索服务自己打的 WARNING（底座那些 logger 不算数）。"""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.services.retrieval.service" and record.levelno == logging.WARNING
    ]


def test_a_broken_embedder_degrades_only_that_knowledge_base(caplog) -> None:  # type: ignore[no-untyped-def]
    """一个库的嵌入服务坏了：**检索照常成功**，另一个库的命中与两边的全文都在。

    改动之前这里的表现是整次检索抛 ``EmbeddingError``（调用方连"别的库查得到"
    都拿不到）。降级之后要求三件事同时成立：

    1. 返回值里那个库被点名（``decision.embedding_unavailable_kbs``），原因带上；
    2. **别的库一条不少**：它的向量命中在，坏库自己的**全文**命中也在（向量那一路过不了，
       词面这一路本来就不依赖嵌入服务）；
    3. 坏库的向量通道是**跳过**（压根没查），不是"查了空手而归"——两者在下游看不出区别，
       但"跳过"才省得掉那 30 秒 × 3 的等待。
    """
    service, vectors = _service(EmbeddingError(BROKEN_REASON))

    with caplog.at_level(logging.WARNING, logger="app.services.retrieval.service"):
        response = _search(service, [BROKEN_KB, HEALTHY_KB])

    assert response.decision is not None, "检索成功返回（不是抛异常）"
    assert response.decision["embedding_unavailable_kbs"] == {BROKEN_KB: BROKEN_REASON}
    assert {hit.chunk_id for hit in response.hits} == {
        OK_VECTOR_CHUNK,
        OK_FULLTEXT_CHUNK,
        BROKEN_FULLTEXT_CHUNK,
    }
    assert [hit.knowledge_base_id for hit in response.hits].count(BROKEN_KB) == 1
    assert vectors.calls == [HEALTHY_KB], "坏库的向量通道压根没查"

    warnings = _warnings(caplog)
    assert any(BROKEN_KB in text and BROKEN_REASON in text for text in warnings), (
        "告警要带得上「哪个库、为什么」，否则不知道去修哪儿"
    )
    assert all(QUERY not in text for text in warnings), "告警里不许出现用户的查询文本"


def test_a_healthy_search_carries_no_degradation_list() -> None:
    """全部库都正常时**不出现** ``embedding_unavailable_kbs``。

    一个恒在的空映射会让每个调用方多写一次"它是不是空的"；而且"都没坏"与
    "这次根本没判"会变成同一种形状。
    """
    service, _ = _service(None)

    response = _search(service, [BROKEN_KB, HEALTHY_KB])

    assert response.hits, "前提：这次确实有命中（否则「没有那个键」只是因为什么都没跑）"
    assert response.decision is not None
    assert "embedding_unavailable_kbs" not in response.decision


def test_a_library_without_an_embedding_model_is_still_a_silent_skip(caplog) -> None:  # type: ignore[no-untyped-def]
    """**没配**嵌入模型仍走原来那条：静默跳过、不进降级名单、告警只打一次。

    它与"这个库这次坏了"是两件不同的事：前者要去设置里选个模型，后者要去修那个端点。
    混进同一份名单（或每次都刷一行同样的日志）会让这两件事长得一模一样。
    """
    service, vectors = _service(EmbeddingNotConfiguredError("未配置嵌入模型"))

    with caplog.at_level(logging.WARNING, logger="app.services.retrieval.service"):
        first = _search(service, [BROKEN_KB, HEALTHY_KB])
        second = _search(service, [BROKEN_KB, HEALTHY_KB])

    assert first.decision is not None and "embedding_unavailable_kbs" not in first.decision
    assert second.decision is not None and "embedding_unavailable_kbs" not in second.decision
    assert {hit.chunk_id for hit in first.hits} == {
        OK_VECTOR_CHUNK,
        OK_FULLTEXT_CHUNK,
        BROKEN_FULLTEXT_CHUNK,
    }
    assert vectors.calls == [HEALTHY_KB, HEALTHY_KB], "只有没配模型的那个库被跳过"

    unconfigured = [text for text in _warnings(caplog) if "未配置嵌入模型" in text]
    assert len(unconfigured) == 1, "同一条日志跨两次检索只打一次（_unconfigured_warned）"


def test_vector_only_mode_reports_the_reason_instead_of_a_silent_empty_page() -> None:
    """``mode="vector"`` 不开特例：那一档只剩向量通道，跳过它自然就是空结果——
    ``decision`` 必须说清为什么空，否则调用方只能以为"这个库里什么都没有"。
    """
    service, vectors = _service(EmbeddingError(BROKEN_REASON))

    response = _search(service, [BROKEN_KB], mode=RetrievalMode.VECTOR)

    assert response.hits == []
    assert response.decision is not None
    assert response.decision["embedding_unavailable_kbs"] == {BROKEN_KB: BROKEN_REASON}
    assert vectors.calls == []


def test_the_degradation_list_does_not_leak_into_the_next_search() -> None:
    """降级是**这一次**检索的属性：同一个服务实例换一个正常的库再查，那个键必须消失。

    ``RetrievalService`` 是进程级单例（组合根只建一次），所以"这次哪几个库坏了"
    一旦记在 ``self`` 上，就会变成"上一次的告警出现在这一次的载荷里"——那是最难查的
    一类错（载荷里说的库这次压根没查）。
    """
    service, _ = _service(EmbeddingError(BROKEN_REASON))

    dirty = _search(service, [BROKEN_KB])
    clean = _search(service, [HEALTHY_KB])

    assert dirty.decision is not None
    assert set(dirty.decision["embedding_unavailable_kbs"]) == {BROKEN_KB}  # type: ignore[arg-type]
    assert clean.decision is not None
    assert "embedding_unavailable_kbs" not in clean.decision
