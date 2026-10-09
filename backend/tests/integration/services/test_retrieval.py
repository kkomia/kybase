"""混合检索的集成测试（M3）。

先用真实的摄入链路把文档灌进库，再验证召回、融合、过滤、rerank 与调试信息。
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import DataSourceKind
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.embedding.base import EmbeddingNotConfiguredError
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.services.retrieval import (
    MetadataFilter,
    NoopReranker,
    RetrievalMode,
    RetrievalQuery,
    RetrievalService,
)
from app.services.retrieval.rerank import RerankError, RerankProvider
from app.storage.base import StoreBundle

DIM = 64

DOC_A = (
    "# 向量检索\n\n"
    "本系统使用向量检索做语义召回，配合全文检索形成混合召回。\n\n"
    "## 存储\n\n"
    "向量存放在 sqlite-vec 的虚拟表中，按知识库分区。\n"
)
DOC_B = (
    "# 部署说明\n\n"
    "服务以 Docker Compose 部署，支持局域网访问，默认端口 8000。\n\n"
    "## 配置\n\n"
    "通过环境变量配置解析节点与 embedding 端点。\n"
)


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=DIM)


@pytest.fixture
def ingest_service(bundle: StoreBundle, embedder: DeterministicEmbedder) -> IngestService:
    return IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=80, overlap=10),
    )


@pytest.fixture
def retrieval(bundle: StoreBundle, embedder: DeterministicEmbedder) -> RetrievalService:
    return RetrievalService(bundle, embedder=embedder, reranker=NoopReranker())


@pytest.fixture
def seeded(bundle: StoreBundle, ingest_service: IngestService, embedder: DeterministicEmbedder):
    """两个库、三份文档，用来验证按库收敛与过滤。"""
    kb_service = KnowledgeBaseService(bundle, embedder=embedder)
    kb_service.create(kb_id="kb_1", name="技术库")
    kb_service.create(kb_id="kb_2", name="运维库")

    for kb_id, name, content in [
        ("kb_1", "vector.md", DOC_A),
        ("kb_1", "deploy.md", DOC_B),
        ("kb_2", "ops.md", DOC_B),  # 同内容不同库：验证库范围隔离
    ]:
        outcome = ingest_service.submit(
            knowledge_base_id=kb_id, filename=name, content=content.encode()
        )
        if not outcome.is_duplicate:
            ingest_service.ingest(outcome.document.id)
    return bundle


# --------------------------------------------------------------------- 基本召回


def test_hybrid_search_finds_relevant_document(seeded: StoreBundle,
                                               retrieval: RetrievalService) -> None:
    response = retrieval.search(RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"]))

    assert response.hits
    assert response.mode == RetrievalMode.HYBRID
    assert response.hits[0].document_name == "vector.md"
    assert "向量检索" in response.hits[0].text


def test_hits_carry_source_metadata_for_the_console(seeded: StoreBundle,
                                                    retrieval: RetrievalService) -> None:
    """调试台要显示来源文档、页码、相似度（架构 §3.3）。"""
    hit = retrieval.search(RetrievalQuery(query="sqlite-vec 分区", kb_ids=["kb_1"])).hits[0]

    assert hit.document_name
    assert hit.heading_path  # 标题路径注入的成果
    assert hit.channels  # 这条是怎么被捞上来的
    assert hit.raw_scores  # 原始分（向量侧为余弦相似度）


def test_channel_stats_are_reported(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    """慢在哪一路、每路召回多少，界面要能看出来。"""
    response = retrieval.search(RetrievalQuery(query="部署", kb_ids=["kb_1"]))
    channels = {stat.channel: stat for stat in response.stats}

    assert set(channels) == {"vector", "fulltext"}
    assert all(stat.count >= 0 and stat.elapsed_ms >= 0 for stat in channels.values())


def test_modes_restrict_channels(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    vector_only = retrieval.search(
        RetrievalQuery(query="向量检索", kb_ids=["kb_1"], mode=RetrievalMode.VECTOR)
    )
    fulltext_only = retrieval.search(
        RetrievalQuery(query="向量检索", kb_ids=["kb_1"], mode=RetrievalMode.FULLTEXT)
    )

    assert [stat.channel for stat in vector_only.stats] == ["vector"]
    assert [stat.channel for stat in fulltext_only.stats] == ["fulltext"]


def test_top_k_is_respected(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    response = retrieval.search(
        RetrievalQuery(query="检索 部署 配置", kb_ids=["kb_1"], top_k=1, candidate_k=10)
    )
    assert len(response.hits) == 1


def test_empty_query_returns_nothing(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    assert retrieval.search(RetrievalQuery(query="   ", kb_ids=["kb_1"])).hits == []


def test_empty_kb_list_returns_nothing(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    assert retrieval.search(RetrievalQuery(query="检索", kb_ids=[])).hits == []


def test_unknown_kb_returns_nothing(retrieval: RetrievalService) -> None:
    assert retrieval.search(RetrievalQuery(query="检索", kb_ids=["kb_none"])).hits == []


def test_injected_query_vector_is_used(seeded: StoreBundle,
                                       retrieval: RetrievalService,
                                       embedder: DeterministicEmbedder) -> None:
    """允许调用方直接给向量（省一次 embedding 调用，也便于测试）。"""
    vector = embedder.embed(["向量检索"])[0]
    response = retrieval.search(
        RetrievalQuery(query="向量检索", kb_ids=["kb_1"], mode=RetrievalMode.VECTOR),
        query_vector=vector,
    )
    assert response.hits


# --------------------------------------------------------------------- 库范围与过滤


def test_results_are_scoped_to_requested_kbs(seeded: StoreBundle,
                                            retrieval: RetrievalService) -> None:
    """架构 §8.3：检索先按 kb_id 收敛范围，不同库互不串味。"""
    kb_1 = retrieval.search(RetrievalQuery(query="Docker Compose 部署", kb_ids=["kb_1"]))
    kb_2 = retrieval.search(RetrievalQuery(query="Docker Compose 部署", kb_ids=["kb_2"]))

    assert {hit.knowledge_base_id for hit in kb_1.hits} == {"kb_1"}
    assert {hit.knowledge_base_id for hit in kb_2.hits} == {"kb_2"}


def test_multiple_kbs_can_be_searched_together(seeded: StoreBundle,
                                               retrieval: RetrievalService) -> None:
    response = retrieval.search(
        RetrievalQuery(query="Docker Compose 部署", kb_ids=["kb_1", "kb_2"], top_k=10)
    )
    assert {hit.knowledge_base_id for hit in response.hits} == {"kb_1", "kb_2"}


def test_filter_by_document_id(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    documents = {
        document.name: document
        for document in seeded.meta.list_documents("kb_1")
    }
    target = documents["vector.md"]

    response = retrieval.search(
        RetrievalQuery(
            query="检索 部署",
            kb_ids=["kb_1"],
            top_k=10,
            filters=MetadataFilter(document_ids=[target.id]),
        )
    )

    assert response.hits
    assert {hit.document_id for hit in response.hits} == {target.id}
    assert response.filtered_out > 0  # 被挡掉多少要说清楚，否则"结果为空"无从解释


def test_filter_by_source_kind(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    only_html = retrieval.search(
        RetrievalQuery(
            query="检索",
            kb_ids=["kb_1"],
            filters=MetadataFilter(source_kinds=[DataSourceKind.HTML]),
        )
    )
    assert only_html.hits == []
    assert only_html.filtered_out > 0


def test_filter_by_time_window(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    past = datetime.now(UTC) - timedelta(days=1)

    assert retrieval.search(
        RetrievalQuery(query="检索", kb_ids=["kb_1"],
                       filters=MetadataFilter(created_after=future))
    ).hits == []
    assert retrieval.search(
        RetrievalQuery(query="检索", kb_ids=["kb_1"],
                       filters=MetadataFilter(created_before=past))
    ).hits == []


def test_filter_empty_object_keeps_everything(seeded: StoreBundle,
                                              retrieval: RetrievalService) -> None:
    response = retrieval.search(
        RetrievalQuery(query="检索", kb_ids=["kb_1"], filters=MetadataFilter())
    )
    assert response.hits


def test_relative_score_threshold_filters_weak_hits(seeded: StoreBundle,
                                                    retrieval: RetrievalService) -> None:
    """阈值语义是"相对最高分的比例"，因此在任何模式下都成立。"""
    everything = retrieval.search(
        RetrievalQuery(query="检索 部署 配置 存储", kb_ids=["kb_1"], top_k=20, candidate_k=40)
    )
    strict = retrieval.search(
        RetrievalQuery(query="检索 部署 配置 存储", kb_ids=["kb_1"], top_k=20, candidate_k=40,
                       score_threshold=1.0)
    )

    assert len(strict.hits) <= len(everything.hits)
    # 1.0 = 只保留并列第一。**不能写死条数**：并列与否取决于两个通道的名次，
    # 而 bm25 与 ts_rank_cd 对同一批命中的排序可以不同（换后端时实测踩到：
    # 两通道名次互为镜像 → 融合分相等 → 出现两个并列第一）。
    # 要验的是语义本身：留下来的分数都等于最高分。
    assert strict.hits
    top = everything.hits[0].score
    assert all(hit.score == pytest.approx(top) for hit in strict.hits)


# --------------------------------------------------------------------- 文档级停用（v14）


def test_disabled_document_is_excluded_from_both_channels(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """停用一份文档后，全文与向量两条通道都不应再命中它。

    全文在 SQL 里裁（JOIN documents）；向量靠下游过滤 + 超采补偿。
    两边一起改才不会出现"全文搜不到、向量搜得到"的静默不一致——这条用例钉住它。
    """
    bundle = seeded
    document = bundle.meta.get_document_by_hash("kb_1", _hash_of(bundle, "kb_1", "deploy.md"))
    assert document is not None

    bundle.meta.set_document_disabled(document.id, True)
    assert bundle.meta.get_document(document.id).disabled is True

    # 全文通道单独查
    fulltext_hits = bundle.fulltext.search(query="部署说明", top_k=5, kb_id="kb_1")
    assert all(hit.document_id != document.id for hit in fulltext_hits)

    # 混合检索整体（向量通道也覆盖）
    response = retrieval.search(
        RetrievalQuery(query="Docker Compose 部署 端口", kb_ids=["kb_1"], top_k=5)
    )
    assert all(hit.document_id != document.id for hit in response.hits)
    # 另一份未停用的文档不受影响
    assert response.hits


def test_disabled_document_is_restorable_without_reingest(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """恢复=把标记翻回来：切块与向量原样保留，立刻重新可检索。"""
    bundle = seeded
    document = bundle.meta.get_document_by_hash("kb_1", _hash_of(bundle, "kb_1", "deploy.md"))
    bundle.meta.set_document_disabled(document.id, True)

    bundle.meta.set_document_disabled(document.id, False)

    response = retrieval.search(RetrievalQuery(query="部署说明", kb_ids=["kb_1"], top_k=5))
    assert any(hit.document_id == document.id for hit in response.hits)


def _hash_of(bundle: StoreBundle, kb_id: str, name: str) -> str:
    """找到已入库文档的 content_hash：测试里用名字反查（文档少，可接受）。"""
    for document in bundle.meta.list_documents(kb_id):
        if document.name == name:
            return document.content_hash
    raise AssertionError(f"fixture 里没有 {name}")


# --------------------------------------------------------------------- rerank


class _FakeReranker(RerankProvider):
    """把顺序整个倒过来，便于断言"确实重排了"。"""

    model_id = "fake"
    enabled = True

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0

    def rerank(self, *, query, documents, top_n):
        self.calls += 1
        if self._fail:
            raise RerankError("端点挂了")
        return [(index, float(index)) for index in range(min(top_n, len(documents)))][::-1]


def test_rerank_reorders_hits(seeded: StoreBundle, embedder: DeterministicEmbedder) -> None:
    baseline = RetrievalService(seeded, embedder=embedder, reranker=NoopReranker())
    request = RetrievalQuery(
        query="检索 部署 配置 存储 向量 服务 环境变量",
        kb_ids=["kb_1"],
        top_k=10,
        candidate_k=40,
        rerank=True,
    )
    before = baseline.search(request)
    assert len(before.hits) > 1, "语料太少会让本用例失去意义（只有一条命中时不会重排）"

    reranker = _FakeReranker()
    after = RetrievalService(seeded, embedder=embedder, reranker=reranker).search(request)

    assert reranker.calls == 1
    assert after.reranked is True
    assert after.hits[0].chunk_id == before.hits[-1].chunk_id
    assert after.hits[0].rerank_score is not None


def test_rerank_not_requested_is_skipped(seeded: StoreBundle,
                                         embedder: DeterministicEmbedder) -> None:
    reranker = _FakeReranker()
    service = RetrievalService(seeded, embedder=embedder, reranker=reranker)
    response = service.search(
        RetrievalQuery(query="检索", kb_ids=["kb_1"], rerank=False)
    )

    assert reranker.calls == 0
    assert response.reranked is False


def test_rerank_failure_degrades_to_rrf_order(seeded: StoreBundle,
                                              embedder: DeterministicEmbedder) -> None:
    """rerank 是可选增强：它挂了不能把整次检索也拖挂（架构 §5）。"""
    baseline = RetrievalService(seeded, embedder=embedder, reranker=NoopReranker())
    degraded = RetrievalService(seeded, embedder=embedder, reranker=_FakeReranker(fail=True))

    request = RetrievalQuery(query="检索 部署", kb_ids=["kb_1"], rerank=True)
    assert [hit.chunk_id for hit in degraded.search(request).hits] == [
        hit.chunk_id for hit in baseline.search(request).hits
    ]
    assert degraded.search(request).reranked is False


def test_single_hit_needs_no_rerank(seeded: StoreBundle, embedder: DeterministicEmbedder) -> None:
    reranker = _FakeReranker()
    service = RetrievalService(seeded, embedder=embedder, reranker=reranker)
    response = service.search(
        RetrievalQuery(query="sqlite-vec 分区", kb_ids=["kb_1"], top_k=1, candidate_k=1,
                       rerank=True)
    )
    assert reranker.calls == 0
    assert response.reranked is False


# --------------------------------------------------------------------- 脏数据与防御


def test_stale_index_entries_are_skipped(seeded: StoreBundle,
                                         retrieval: RetrievalService) -> None:
    """索引里可能还留着已经删掉的 chunk：跳过它，而不是让检索崩掉。"""
    documents = {d.name: d for d in seeded.meta.list_documents("kb_1")}
    target = documents["vector.md"]
    seeded.meta.replace_chunks(target.id, [])  # 只删 chunk，FTS 索引仍留有旧条目

    response = retrieval.search(
        RetrievalQuery(query="向量检索 sqlite-vec", kb_ids=["kb_1"], top_k=10, candidate_k=40)
    )

    assert all(hit.document_id != target.id for hit in response.hits)
    assert response.filtered_out > 0


def test_filter_without_document_is_rejected() -> None:
    """防御路径：命中却查不到文档记录时，带过滤条件应判为不通过。"""
    from app.services.retrieval.service import _passes_filters

    assert _passes_filters(None, MetadataFilter(document_ids=["doc_x"])) is False
    assert _passes_filters(None, None) is True


# --------------------------------------------------------------------- 参数校验


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "unknown"},
        {"top_k": 0},
        {"top_k": 10, "candidate_k": 3},
    ],
)
def test_invalid_query_parameters_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetrievalQuery(query="q", kb_ids=["kb_1"], **kwargs)


# ------------------------------------------------- 每个库用自己的嵌入模型（v11）


class _RecordingResolver:
    """记录"为哪个库解析了 embedder"，并把同一个确定性嵌入实现发下去。

    这里验的是**检索会按库解析模型**这条性质（混合模型的库必须各自算查询向量），
    而不是嵌入质量——质量由 embedding 自己的测试管。
    """

    def __init__(self, embedder) -> None:  # type: ignore[no-untyped-def]
        self._embedder = embedder
        self.calls: list[str | None] = []

    def for_kb(self, kb):  # type: ignore[no-untyped-def]
        self.calls.append(kb.embedding_model_pk)
        return self._embedder


def test_vector_channel_resolves_an_embedder_per_knowledge_base(seeded: StoreBundle,
                                                               embedder) -> None:  # type: ignore[no-untyped-def]
    """跨库检索时，每个库都要用**它自己的**嵌入模型算查询向量。

    共用一个查询向量是错的：不同模型的向量空间不同，相似度没有意义。
    """
    resolver = _RecordingResolver(embedder)
    service = RetrievalService(
        seeded, embedder=embedder, reranker=NoopReranker(), embedders=resolver
    )

    service.search(RetrievalQuery(query="向量检索", kb_ids=["kb_1", "kb_2"]))

    assert resolver.calls == [None, None]  # 两个库各解析一次（这里都没显式选模型）


def test_retrieval_falls_back_when_no_resolver_is_wired(seeded: StoreBundle,
                                                       retrieval: RetrievalService) -> None:
    """没接注册器时行为与改动前一致——升级不打断既有部署。"""
    assert retrieval.search(RetrievalQuery(query="部署", kb_ids=["kb_1"])).hits


def test_unconfigured_embedding_skips_the_vector_channel(seeded: StoreBundle) -> None:
    """没配嵌入模型时跳过向量通道，全文照常返回（v0.8 取消哈希兜底）。

    这里刻意不降级成"无语义的假向量"——那会让用户以为语义召回是有效的。
    正确行为是：这个通道用不了就不用，并且**不报错**，界面上另有字段说明。
    """

    class _Unconfigured:
        model_id = ""
        dim = 0

        def embed(self, texts):  # type: ignore[no-untyped-def]
            raise EmbeddingNotConfiguredError("未配置嵌入模型")

        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingNotConfiguredError("未配置嵌入模型")

    service = RetrievalService(seeded, embedder=_Unconfigured(), reranker=NoopReranker())

    response = service.search(RetrievalQuery(query="部署说明", kb_ids=["kb_1"]))

    # 全文通道仍能命中：库里已有的内容不会因为没配模型就查不到
    assert response.hits
    vector_stat = next(stat for stat in response.stats if stat.channel == "vector")
    assert vector_stat.count == 0


# --------------------------------------------------------------------- 相关度下限（v0.53）


def test_floors_are_off_for_the_development_embedder(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """开发用哈希嵌入没有标定过余弦尺度 → 默认整套不设限，既有行为一字不变。

    这不是"顺手放过"：余弦的绝对值随模型变，拿 bge-m3 的尺度拦别的模型会把
    它的真命中全砍掉（见 ``test_relevance_floors.py``）。
    """
    default = retrieval.search(RetrievalQuery(query="检索 部署 配置", kb_ids=["kb_1"], top_k=10))
    explicit_off = retrieval.search(
        RetrievalQuery(
            query="检索 部署 配置",
            kb_ids=["kb_1"],
            top_k=10,
            min_vector_score=0.0,
            min_term_coverage=0.0,
        )
    )

    assert [hit.chunk_id for hit in default.hits] == [hit.chunk_id for hit in explicit_off.hits]
    assert default.filtered_out == explicit_off.filtered_out


def test_vector_floor_filters_and_counts_what_it_dropped(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """余弦下限是**绝对**判定：1.0 谁也不可能达到（余弦等于 1 只对自己），
    这时又没有词面那一路兜底（显式关掉），于是命中为空、且 filtered_out 说得清原因。"""
    response = retrieval.search(
        RetrievalQuery(
            query="检索 部署 配置",
            kb_ids=["kb_1"],
            top_k=10,
            min_vector_score=1.0,
            min_term_coverage=0.0,
        )
    )

    assert response.hits == []
    assert response.filtered_out > 0


def test_word_evidence_rescues_hits_below_the_vector_floor(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """两条证据是"或"：语义那一路被卡死时，词面对得上的候选照样留下。

    这正是"误杀真命中比留下噪声更糟"的落点——人名、索引号、缩写这类查询的
    余弦本来就低（实测「郭正茂」那条只有 0.852），只能靠词面这一路救。
    """
    response = retrieval.search(
        RetrievalQuery(
            query="向量检索 混合召回",
            kb_ids=["kb_1"],
            top_k=10,
            min_vector_score=1.0,  # 语义那一路必然不通过
            min_term_coverage=0.5,
        )
    )

    assert response.hits
    # 头一条就是查询词都落在里面的那段（其余候选是"覆盖到一半"的，不写死它们）
    assert "向量检索" in response.hits[0].text


def test_coverage_floor_can_be_turned_off_alone(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """只关词面那一条：语义够近的候选照样回来（参数是分别可关的）。"""
    response = retrieval.search(
        RetrievalQuery(
            query="向量检索 混合召回",
            kb_ids=["kb_1"],
            top_k=10,
            min_vector_score=1.0,
            min_term_coverage=0.0,
        )
    )

    assert response.hits == []


def test_keyword_only_mode_is_left_alone(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """纯关键词那一档没有语义证据（向量通道空着）→ 判定层不下结论，产出与改动前一致。

    那一档是**调试台**用来分辨"召回不行"还是"融合不行"的：它要显示的是通道本身的
    产出，被判定层改写之后就说不清是谁的问题了。
    """
    off = retrieval.search(
        RetrievalQuery(
            query="检索 部署 配置 存储",
            kb_ids=["kb_1"],
            mode=RetrievalMode.FULLTEXT,
            top_k=20,
            min_vector_score=0.0,
            min_term_coverage=0.0,
        )
    )
    on = retrieval.search(
        RetrievalQuery(
            query="检索 部署 配置 存储",
            kb_ids=["kb_1"],
            mode=RetrievalMode.FULLTEXT,
            top_k=20,
            min_vector_score=0.99,
            min_term_coverage=0.99,
        )
    )

    assert [hit.chunk_id for hit in on.hits] == [hit.chunk_id for hit in off.hits]
    assert on.filtered_out == off.filtered_out


def test_vector_mode_without_candidates_does_not_judge(
    seeded: StoreBundle,
) -> None:
    """没配嵌入模型（向量通道空）→ 即便模型标定过、下限开着，也不判不相关。

    这时全文通道是唯一的来源，把它按"语义不相关"砍掉是拿错了尺子。
    """

    class _Unconfigured:
        model_id = "BAAI/bge-m3"  # 标定过，但一条候选都产不出来

        def embed(self, texts):  # type: ignore[no-untyped-def]
            raise EmbeddingNotConfiguredError("未配置嵌入模型")

        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingNotConfiguredError("未配置嵌入模型")

    service = RetrievalService(seeded, embedder=_Unconfigured(), reranker=NoopReranker())

    response = service.search(RetrievalQuery(query="部署说明", kb_ids=["kb_1"]))
    without = service.search(
        RetrievalQuery(
            query="部署说明",
            kb_ids=["kb_1"],
            min_vector_score=0.0,
            min_term_coverage=0.0,
        )
    )

    assert response.hits
    assert response.filtered_out == without.filtered_out


# ------------------------------------------------- 相关度地板：比的是余弦，不是名次分
#
# 现场（方案 14，真机实测）：一条明显不相关的查询照样回满 top_k=8，而向量第 1 名的
# `score` 恒为 0.0164（hybrid 档 0.0328）——那是**名次分**（``1/(k+rank)``）。
# 所以必须钉住两件事：
#   1. 地板比的是**真实余弦**（``similarity`` / ``raw_scores['vector']``）；
#   2. 命中上给出的 ``score`` 与 ``similarity`` 是两个东西，别混用。
# 做法：把候选的余弦**精确摆好**（直接覆盖向量），查询向量固定为 [1,0,0,…]，
# 于是"该拦谁、该留谁"没有随机性。

WEMM_MODEL_ID = "WeMM-Embedding-2B-Q4_K_M.gguf"
"""用它当库的嵌入模型 → 命中标定表，地板 0.78（见 ``MIN_VECTOR_SCORE_BY_MODEL``）。"""

UNCALIBRATED_MODEL_ID = "text-embedding-3-small"
"""没标定过的模型 → 地板 0.0（不设限）：这条也要钉住，别被"顺手加个默认值"改掉。"""


def _unit_vector(cosine: float) -> list[float]:
    """与 ``[1, 0, 0, …]`` 的余弦**正好**是 ``cosine`` 的单位向量。"""
    orthogonal = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return [cosine, orthogonal] + [0.0] * (DIM - 2)


class _FixedQueryEmbedder:
    """固定的查询向量 + 一个说得出口的模型身份（不外呼）。"""

    is_development = False
    supports_media = False

    def __init__(self, model_id: str, vector: list[float]) -> None:
        self.model_id = model_id
        self.dim = DIM
        self._vector = vector

    def embed(self, texts):  # type: ignore[no-untyped-def]
        return [list(self._vector) for _ in texts]


class _FixedResolver:
    """``EmbeddingResolver`` 的替身：这个库用哪个模型由我说了算。"""

    def __init__(self, embedder: _FixedQueryEmbedder) -> None:
        self._embedder = embedder

    def for_kb(self, kb: object) -> object:  # type: ignore[no-untyped-def]
        return self._embedder


def _seed_two_documents(
    bundle: StoreBundle, ingest_service: IngestService, embedder: DeterministicEmbedder
) -> tuple[str, str]:
    """一个库、两篇文档（各一段），返回它们的 chunk_id（先"近"后"远"）。"""
    KnowledgeBaseService(bundle, embedder=embedder).create(kb_id="kb_floor", name="地板库")
    chunk_ids: list[str] = []
    for name, text in (
        ("near.md", "# 近\n\n这段文字与查询讲的是同一件事。\n"),
        ("far.md", "# 远\n\n这段文字与查询毫无关系。\n"),
    ):
        outcome = ingest_service.submit(
            knowledge_base_id="kb_floor", filename=name, content=text.encode()
        )
        ingest_service.ingest(outcome.document.id)
        chunk_ids.append(next(iter(bundle.meta.iter_chunks(outcome.document.id))).chunk_id)
    return chunk_ids[0], chunk_ids[1]


def _floor_service(
    bundle: StoreBundle, model_id: str, query_vector: list[float]
) -> RetrievalService:
    embedder = _FixedQueryEmbedder(model_id, query_vector)
    return RetrievalService(
        bundle,
        embedder=embedder,
        reranker=NoopReranker(),
        embedders=_FixedResolver(embedder),  # type: ignore[arg-type]
    )


def _search_vector_only(
    service: RetrievalService, query_vector: list[float], **overrides: float
):  # type: ignore[no-untyped-def]
    return service.search(
        RetrievalQuery(
            query="问点什么",
            kb_ids=["kb_floor"],
            mode=RetrievalMode.VECTOR,
            **overrides,
        ),
        query_vector=query_vector,
    )


def test_the_floor_compares_the_real_cosine_not_the_rank_score(
    bundle: StoreBundle, ingest_service: IngestService, embedder: DeterministicEmbedder
) -> None:
    """摆好的 0.60 留、0.20 拦——地板比的是**真实余弦**（WeMM 那条地板是 0.35）。

    如果哪天有人把判据改成 ``score``（名次分，0.016 量级），这条会**一条都不回**：
    0.60 那条也会低于 0.0164…… 反过来，真命中被误杀。这正是"地板永远不触发"的镜像错误。
    """
    near_id, far_id = _seed_two_documents(bundle, ingest_service, embedder)
    query_vector = _unit_vector(1.0)
    bundle.vectors.upsert_vectors(
        "kb_floor",
        items=[(near_id, _unit_vector(0.60)), (far_id, _unit_vector(0.20))],
    )
    service = _floor_service(bundle, WEMM_MODEL_ID, query_vector)

    response = _search_vector_only(service, query_vector)

    assert [hit.chunk_id for hit in response.hits] == [near_id]
    assert response.filtered_out == 1
    hit = response.hits[0]
    assert hit.similarity == pytest.approx(0.60, abs=1e-6)  # 真实余弦
    assert hit.score < 0.05  # 融合/名次分：一个很小的常数，**不能**当相似度用
    assert hit.raw_scores["vector"] == pytest.approx(0.60, abs=1e-6)

    # 显式关掉地板 → 两条都回来。证明"少了一条"是地板干的，不是别的过滤
    off = _search_vector_only(service, query_vector, min_vector_score=0.0)
    assert {hit.chunk_id for hit in off.hits} == {near_id, far_id}


def test_the_floor_does_not_kill_real_hits(
    bundle: StoreBundle, ingest_service: IngestService, embedder: DeterministicEmbedder
) -> None:
    """真命中那一侧：0.717 / 0.510（实测的正面区间）都留，一条都不许被误杀。"""
    near_id, far_id = _seed_two_documents(bundle, ingest_service, embedder)
    query_vector = _unit_vector(1.0)
    bundle.vectors.upsert_vectors(
        "kb_floor",
        items=[(near_id, _unit_vector(0.717)), (far_id, _unit_vector(0.510))],
    )
    service = _floor_service(bundle, WEMM_MODEL_ID, query_vector)

    response = _search_vector_only(service, query_vector)

    assert {hit.chunk_id for hit in response.hits} == {near_id, far_id}
    assert response.filtered_out == 0


def test_uncalibrated_models_are_not_filtered(
    bundle: StoreBundle, ingest_service: IngestService, embedder: DeterministicEmbedder
) -> None:
    """没标定过的模型**不设限**（既有行为，刻意保留）。

    说不出"这个模型上多少算相关"时，拦下来只会误杀——这是"宁可漏也别误杀"的落点。
    这里的 0.30 换成 WeMM 就会被拦（低于 0.35），未标定时必须留下。
    """
    near_id, far_id = _seed_two_documents(bundle, ingest_service, embedder)
    query_vector = _unit_vector(1.0)
    bundle.vectors.upsert_vectors(
        "kb_floor",
        items=[(near_id, _unit_vector(0.95)), (far_id, _unit_vector(0.30))],
    )
    service = _floor_service(bundle, UNCALIBRATED_MODEL_ID, query_vector)

    response = _search_vector_only(service, query_vector)

    assert {hit.chunk_id for hit in response.hits} == {near_id, far_id}
    assert response.filtered_out == 0
