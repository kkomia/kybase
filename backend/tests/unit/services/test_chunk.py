"""切块人工干预（G3）。

镜像同构：``app/services/chunk.py`` → ``tests/unit/services/test_chunk.py``。

三个动作各有自己的"最容易漏"的地方，用例围着它们转：

- **删除**：三处（元数据、全文索引、向量）都要清，少一处就留下幽灵——
  检索还能召回它、却查不到正文；
- **编辑**：必须重新向量化，否则"命中的是旧向量、返回的是新文本"；
- **禁用**：不能删向量（恢复要零成本），要在**检索侧**被过滤掉。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.chunk import MAX_CHUNK_CHARS, ChunkService
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.retrieval import RetrievalQuery, RetrievalService
from app.storage.base import ChunkRecord

DIM = 16


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=DIM)


@pytest.fixture
def chunks(bundle, embedder) -> ChunkService:  # type: ignore[no-untyped-def]
    return ChunkService(bundle, embedder=embedder)


def _chunk(chunk_id: str, text: str, ordinal: int) -> ChunkRecord:
    """造一个块记录（**不落库**，由调用方一次性批量写入）。

    单独一个"写一个块"的助手会踩坑：``replace_chunks`` 的语义是
    "整体替换该文档的块"（先删后插），逐个调用会把先前写的块删掉。
    """
    from app.services.chunking import content_hash_of

    return ChunkRecord(
        chunk_id=chunk_id,
        document_id="doc_1",
        knowledge_base_id="kb_1",
        part_id=None,
        ordinal=ordinal,
        text=text,
        content_hash=content_hash_of(text),
    )


@pytest.fixture
def seeded(bundle, embedder, document):  # type: ignore[no-untyped-def]
    """一个带三个块的知识库，三处（元数据 / 全文索引 / 向量）都齐全。

    依赖 ``document`` fixture：chunks 有指向 documents 的外键，
    没有文档行时写入会 `FOREIGN KEY constraint failed`。
    """
    bundle.vectors.ensure_partition("kb_1", dim=DIM)
    records = [
        _chunk("c1", "眼轴长度的测量方法。", 0),
        _chunk("c2", "近视防控的政策建议。", 1),
        _chunk("c3", "屈光发育的参考值。", 2),
    ]
    bundle.meta.replace_chunks("doc_1", records)
    bundle.fulltext.index_chunks(records)
    bundle.vectors.upsert_vectors(
        "kb_1", items=[(r.chunk_id, embedder.embed([r.text])[0]) for r in records]
    )
    return bundle


def _indexed_ids(bundle, kb_id="kb_1") -> set[str]:  # type: ignore[no-untyped-def]
    """向量分区里现存的所有 chunk_id。"""
    hits = bundle.vectors.search(kb_id, query_vector=[0.0] * DIM, top_k=50)
    return {hit.chunk_id for hit in hits}


def _retrieve(bundle, embedder, query: str):  # type: ignore[no-untyped-def]
    # 不启用 rerank：这条用例只关心"禁用是否被过滤掉"，多一环重排只会引入噪声
    reranker = type("R", (), {"enabled": False})()
    service = RetrievalService(bundle, embedder=embedder, reranker=reranker)
    return service.search(RetrievalQuery(query=query, kb_ids=["kb_1"], top_k=10))


# --------------------------------------------------------------------- 读


def test_get_returns_the_chunk(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    assert chunks.get("c1").text == "眼轴长度的测量方法。"


def test_get_missing_chunk_raises(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        chunks.get("c_不存在")


# --------------------------------------------------------------------- 编辑


def test_update_text_keeps_the_same_chunk_id(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    """**chunk_id 不能变**：它是向量与全文索引的主键，改了要三处联动重建，
    而"用户改了一段文字"并不改变这块在文档里的身份。"""
    updated = chunks.update_text("c1", "改写后的正文。")

    assert updated.chunk_id == "c1"
    assert chunks.get("c1").text == "改写后的正文。"


def test_update_text_refreshes_the_content_hash(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    """hash 不更新的话，增量更新会认为这块没变过，于是把旧内容又写回来。"""
    from app.services.chunking import content_hash_of

    updated = chunks.update_text("c1", "新正文。")

    assert updated.content_hash == content_hash_of("新正文。")


def test_update_text_reembeds_the_vector(chunks: ChunkService, seeded, embedder) -> None:  # type: ignore[no-untyped-def]
    """**这条最关键**：不重新向量化的话，检索命中的是旧向量、返回的是新文本——
    用户看到"为什么这条会被搜出来"完全对不上，且极难排查。"""
    chunks.update_text("c1", "完全不同的内容：屈光度与轴率比的关系。")

    hits = _retrieve(seeded, embedder, "屈光度与轴率比的关系")
    assert hits.hits, "改完的块应当能被检索到"
    assert hits.hits[0].chunk_id == "c1"
    assert "轴率比" in hits.hits[0].text


def test_update_text_rejects_empty(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidRequestError):
        chunks.update_text("c1", "   ")


def test_update_text_rejects_overlong(
    chunks: ChunkService, seeded
) -> None:  # type: ignore[no-untyped-def]
    """允许把块改得无限长，等于让"块"这个概念失去意义（检索粒度会崩）。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        chunks.update_text("c1", "字" * (MAX_CHUNK_CHARS + 1))
    assert "上限" in str(excinfo.value)


def test_update_text_does_not_leave_half_done_state_on_embed_failure(
    bundle, seeded
) -> None:  # type: ignore[no-untyped-def]
    """向量算不出来时必须**整体失败**，不能留下"文本改了但向量还是旧的"。"""

    class BrokenEmbedder:
        dim = DIM
        model_id = "broken"
        max_batch = 8
        is_development = False

        def embed(self, texts):  # type: ignore[no-untyped-def]
            raise RuntimeError("模型不可用")

    service = ChunkService(bundle, embedder=BrokenEmbedder())
    before = service.get("c1").text

    with pytest.raises(RuntimeError):
        service.update_text("c1", "改了的正文")

    assert service.get("c1").text == before, "正文不该在向量失败后仍被改掉"


# --------------------------------------------------------------------- 禁用


def test_disable_filters_the_chunk_out_of_retrieval(
    chunks: ChunkService, seeded, embedder
) -> None:  # type: ignore[no-untyped-def]
    """禁用要**在检索侧**生效——这才是禁用的意义。"""
    before = _retrieve(seeded, embedder, "眼轴长度的测量方法")
    assert any(hit.chunk_id == "c1" for hit in before.hits)

    chunks.set_disabled("c1", disabled=True)

    assert not any(
        hit.chunk_id == "c1" for hit in _retrieve(seeded, embedder, "眼轴长度的测量方法").hits
    )


def test_disable_keeps_metadata_and_vector(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    """**禁用不能删向量**：恢复要零成本，也不该因为"关了又开"而丢掉原有向量。"""
    chunks.set_disabled("c1", disabled=True)

    assert chunks.get("c1").disabled is True
    assert "c1" in _indexed_ids(seeded), "禁用把向量删了——恢复就得重新 embedding"


def test_restore_brings_the_chunk_back(chunks: ChunkService, seeded, embedder) -> None:  # type: ignore[no-untyped-def]
    chunks.set_disabled("c1", disabled=True)
    chunks.set_disabled("c1", disabled=False)

    assert chunks.get("c1").disabled is False
    after = _retrieve(seeded, embedder, "眼轴长度的测量方法")
    assert any(hit.chunk_id == "c1" for hit in after.hits)


def test_disable_is_idempotent(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    chunks.set_disabled("c1", disabled=True)
    chunks.set_disabled("c1", disabled=True)
    assert chunks.get("c1").disabled is True


def test_filtered_out_counts_disabled_chunks(chunks: ChunkService, seeded, embedder) -> None:  # type: ignore[no-untyped-def]
    """界面要能解释"结果为什么变少了"，所以禁用要计入 filtered_out。"""
    chunks.set_disabled("c1", disabled=True)
    assert _retrieve(seeded, embedder, "眼轴长度的测量方法").filtered_out >= 1


# --------------------------------------------------------------------- 删除


def test_delete_removes_from_all_three_stores(chunks: ChunkService, seeded, embedder) -> None:  # type: ignore[no-untyped-def]
    """**三处都要清，少一处就留下幽灵**：检索还能召回它、却查不到正文。"""
    chunks.delete("c1")

    with pytest.raises(NotFoundError):
        chunks.get("c1")
    assert "c1" not in _indexed_ids(seeded), "向量没删干净"
    # 全文索引：用它的词去搜，不该再命中
    assert not any(
        hit.chunk_id == "c1" for hit in _retrieve(seeded, embedder, "眼轴长度的测量方法").hits
    )


def test_delete_renumbers_the_remaining_chunks(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    """ordinal 是界面上的"第 N 块"。留着空洞会让用户看到 1、2、4 而以为丢了数据。"""
    chunks.delete("c2")

    ordinals = [record.ordinal for record in seeded.meta.iter_chunks("doc_1")]
    assert ordinals == [0, 1]
    assert [record.chunk_id for record in seeded.meta.iter_chunks("doc_1")] == ["c1", "c3"]


def test_delete_keeps_other_chunks(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    chunks.delete("c1")
    assert chunks.get("c2").text == "近视防控的政策建议。"
    assert chunks.get("c3").text == "屈光发育的参考值。"


def test_delete_missing_chunk_raises(chunks: ChunkService, seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        chunks.delete("c_不存在")


# ------------------------------------------------------- 改正文后重出题（v23）


class _StubQuestions:
    """假的出题服务：给这一段返回固定的一条问题。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate_for_chunks(self, chunks, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return {chunks[0].chunk_id: ["这一段的题？"]}


class _RecordingEmbedder(DeterministicEmbedder):
    """记下被向量化的文本，用来断言用的是 index_text。"""

    def __init__(self, dim: int = DIM) -> None:
        super().__init__(dim=dim)
        self.seen: list[str] = []

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.seen.extend(texts)
        return super().embed(texts)


def _store_questions(bundle, chunk_id: str, questions: tuple[str, ...]) -> None:  # type: ignore[no-untyped-def]
    record = bundle.meta.get_chunks([chunk_id])[0]
    record.questions = questions
    bundle.meta.update_chunk(record)


def test_update_text_regenerates_the_questions(bundle, seeded) -> None:  # type: ignore[no-untyped-def]
    """正文改了必须**重出题**：旧问题是照着旧正文出的，留着它等于给检索埋一条
    "命中一段已经不存在的文字"的假线索。重出的问题也要一起进索引文本。"""
    stub = _StubQuestions()
    recorder = _RecordingEmbedder()
    service = ChunkService(bundle, embedder=recorder, questions=stub)
    _store_questions(bundle, "c1", ("旧问题？",))
    bundle.meta.set_knowledge_base_suggested(
        "kb_1", enabled=True, count=2, model_pk=None, prompt=""
    )

    updated = service.update_text("c1", "改写后的正文。")

    assert updated.questions == ("这一段的题？",)
    assert stub.calls == 1
    # 落库了，且**向量化用的是含问题的索引文本**
    assert bundle.meta.get_chunks(["c1"])[0].questions == ("这一段的题？",)
    assert any("这一段的题？" in text for text in recorder.seen)


def test_update_text_clears_stale_questions_when_disabled(bundle, seeded) -> None:  # type: ignore[no-untyped-def]
    """库上关着这个功能时**清空**而不是保留旧问题——宁可这一段没有题，
    也不能留一批对不上新正文的问题在索引里。"""
    stub = _StubQuestions()
    service = ChunkService(bundle, embedder=DeterministicEmbedder(dim=DIM), questions=stub)
    _store_questions(bundle, "c1", ("旧问题？",))

    updated = service.update_text("c1", "改写后的正文。")

    assert updated.questions == ()
    assert stub.calls == 0
    assert bundle.meta.get_chunks(["c1"])[0].questions == ()
