"""混合检索的集成测试（M3）。

先用真实的摄入链路把文档灌进库，再验证召回、融合、过滤、rerank 与调试信息。
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import DataSourceKind, DocumentStage
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.embedding.base import EmbeddingNotConfiguredError
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.services.retrieval import (
    DEFAULT_STATS_FLOOR,
    MetadataFilter,
    NoopReranker,
    RetrievalMode,
    RetrievalQuery,
    RetrievalService,
)
from app.services.retrieval.distribution import FIT_NONE
from app.services.retrieval.rerank import RerankError, RerankProvider
from app.storage.base import (
    ChunkRecord,
    DocumentRecord,
    KnowledgeBaseRecord,
    StoreBundle,
)

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
    """调试台要显示来源文档、页码、相似度（架构 3.3）。"""
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
    """架构 8.3：检索先按 kb_id 收敛范围，不同库互不串味。"""
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
    # **`filtered_out` 为 0 是 v0.64 起对的**：条件已经下推到 SQL，别的文档的块
    # 压根没进候选，也就没有"取回来之后又被丢掉"的条数可报（见
    # ``service._materialize`` 里那个计数）。它不再能用来解释"结果为什么为空"——
    # "池子里本来就没有"与"筛掉了"现在是两件事。
    assert response.filtered_out == 0


def test_filter_by_source_kind(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    only_html = retrieval.search(
        RetrievalQuery(
            query="检索",
            kb_ids=["kb_1"],
            filters=MetadataFilter(source_kinds=[DataSourceKind.HTML]),
        )
    )
    assert only_html.hits == []
    # 空结果来自"没有一篇是 html"这个事实本身，而不是"取回来后全被丢掉"：
    # 条件在 SQL 里就生效了（v0.64），所以这个计数是 0
    assert only_html.filtered_out == 0


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
    """rerank 是可选增强：它挂了不能把整次检索也拖挂（架构 5）。"""
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


def test_chunk_level_guard_matches_the_pushed_down_semantics() -> None:
    """兜底守卫（``_passes_chunk_filters``）与 SQL 那一侧**同口径**（v0.64）。

    条件现在由 SQL 生效，这条守卫正常一次都不该拦到东西；留着它是为了"下推不完整"
    的那一类（新维度忘了进 ``SearchFilter`` 的翻译）。**两条路给不同答案最难查**：
    同一次检索会因为下推有没有生效而给出不同结果。所以边界直接钉在这里。
    """
    from app.services.retrieval.service import _passes_chunk_filters

    def chunk(page: int | None, heading: str | None) -> ChunkRecord:
        return ChunkRecord(
            chunk_id="c1",
            document_id="d1",
            knowledge_base_id="kb_1",
            part_id=None,
            ordinal=0,
            text="正文",
            content_hash="h1",
            page=page,
            heading_path=heading,
        )

    # 边界含端点
    assert _passes_chunk_filters(chunk(40, "第3章"), MetadataFilter(page_from=40, page_to=60))
    assert _passes_chunk_filters(chunk(60, "第3章"), MetadataFilter(page_from=40, page_to=60))
    assert not _passes_chunk_filters(chunk(39, "第3章"), MetadataFilter(page_from=40, page_to=60))
    assert not _passes_chunk_filters(chunk(61, "第3章"), MetadataFilter(page_from=40, page_to=60))
    # 没有页码的块被排除（与 SQL 的 `NULL >= 40` 不为真同口径）
    assert not _passes_chunk_filters(chunk(None, "第3章"), MetadataFilter(page_from=40))
    # 标题：子串 + 大小写不敏感；标题为空的块被排除
    assert _passes_chunk_filters(chunk(50, "Chapter 3 > 3.2"), MetadataFilter(
        heading_contains="chapter 3"
    ))
    assert not _passes_chunk_filters(chunk(50, None), MetadataFilter(heading_contains="第3章"))
    # 没给条件就放行
    assert _passes_chunk_filters(chunk(None, None), MetadataFilter(document_ids=["d1"]))


# --------------------------------------------------------------------- 参数校验


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "unknown"},
        {"top_k": 0},
        {"top_k": -1},
    ],
)
def test_invalid_query_parameters_are_rejected(kwargs: dict) -> None:
    """模式不认识、条数不是正数——这些是真的没法执行，报错。

    **``candidate_k < top_k`` 从前也在这份名单里，v0.63 起不在了**：``top_k`` 变成
    "能翻到的总量"之后，一次要 50 条池子的调用可能压根没传 ``candidate_k``（默认 40），
    报错的后果是"上限抬到 50"只有显式传了 candidate_k 的人叫得动。要 50 条却没有
    50 个候选是自相矛盾的，补上即可——见下面那条用例。
    """
    with pytest.raises(ValueError):
        RetrievalQuery(query="q", kb_ids=["kb_1"], **kwargs)


def test_a_shallow_recall_depth_is_raised_to_the_pool_size() -> None:
    """召回深度比池子浅时**抬到池子**，而不是报错：要 50 条却只捞 40 条是自相矛盾的。

    它也是 v0.63 抬 ``MAX_TOP_K`` 的必要配套：``SearchRequest`` 的 ``top_k`` 上限跟着
    抬到 50，而它的 ``candidate_k`` 默认是 40——不抬的话，"要一页 50 条"这件事照默认
    参数根本走不通（服务层当场 ValueError，表现成一个 500）。
    """
    request = RetrievalQuery(query="q", kb_ids=["kb_1"], top_k=50, candidate_k=10)

    assert request.candidate_k == 50
    # 本来就比池子深的那些不动（多召回是给 RRF 留排序空间的，别顺手削掉）
    assert RetrievalQuery(query="q", kb_ids=["kb_1"], top_k=5, candidate_k=40).candidate_k == 40


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


def test_the_distribution_is_reported_without_anyone_asking_for_it(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """**无条件**：不传 ``stats_floor`` 也有分布与决定（v0.55 之前这两个都是 ``None``）。

    这条性质就是这次改动的全部要点：命中分布是这个服务的特色，它不该取决于调用方
    记不记得传一个开关键——那正是它当年只在 MCP 那条路上生效的原因
    （``retrieval.floor_score = "0"`` 曾经等于"关掉特色"）。
    """
    response = retrieval.search(RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"]))

    assert response.distribution is not None
    assert response.decision is not None
    assert response.decision["fit"] == response.distribution.fit
    # 条数要对得上账：窗口里收了多少 = 收敛后剩了多少 + 收敛时切掉了几条。
    # **剩下多少用 `kept`**（v0.63）：`hits` 只是这一页，拿它入账只在"keep ≥ 幸存者数"
    # 时碰巧成立；翻页那条路上它必然对不上（第二页只有两三条）。
    assert response.distribution.count == response.kept + response.decision["dropped"]
    assert len(response.hits) <= response.kept


def test_a_zero_window_no_longer_turns_the_distribution_off(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """``stats_floor=0``（也就是设置里的 ``floor_score = "0"``）只是"用默认窗口"。

    这是那个坑的正面用例：曾经 0 = 关闭、``None`` = 不统计，于是"要不要这个特色"
    变成了一个可以被一个数字悄悄关掉的开关。现在填 0 与不填**完全等价**：窗口是默认窗口，
    分布、判定、收敛一件都不少。
    """
    off = retrieval.search(RetrievalQuery(query="向量检索", kb_ids=["kb_1"], stats_floor=0.0))
    default = retrieval.search(RetrievalQuery(query="向量检索", kb_ids=["kb_1"]))

    assert off.distribution is not None
    assert off.distribution.floor == DEFAULT_STATS_FLOOR
    assert off.distribution.floor == default.distribution.floor
    assert [hit.chunk_id for hit in off.hits] == [hit.chunk_id for hit in default.hits]
    assert off.decision == default.decision


def test_an_empty_window_still_reports_a_distribution(retrieval: RetrievalService) -> None:
    """没有命中时分布照样在（``count=0`` → 建议 0 条），**不是 ``None``**。

    "这个库里没有答案"与"没有算过"是两件事：前者要给模型一句话，后者只会让它乱猜。
    """
    response = retrieval.search(RetrievalQuery(query="检索", kb_ids=["kb_none"]))

    assert response.distribution is not None
    assert response.distribution.count == 0
    assert response.hits == []
    assert "库里没有命中" in response.distribution.headline(kept=0)


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
# 现场（方式 14，真机实测）：一条明显不相关的查询照样回满 top_k=8，而向量第 1 名的
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

    # 关掉判定下限 → 0.20 那条不再被判"不相关"。但**分布那一刀还在**：它低于基线 0.33，
    # 于是被收敛切掉（min_score 用分布的建议值）——判定在前、按分布收敛，这就是新的常态。
    off = _search_vector_only(service, query_vector, min_vector_score=0.0)
    assert [hit.chunk_id for hit in off.hits] == [near_id]
    assert off.filtered_out == 1, "少了的那条仍要说得出为什么少"
    assert off.decision is not None
    assert off.decision["min_score"] == pytest.approx(0.33)

    # 连分布那一刀也让开（min_score=0）才真的两条都回来：证明"少了一条"是地板干的，
    # 不是别的过滤（检索路径本身没吃掉它）
    unconstrained = _search_vector_only(service, query_vector, min_vector_score=0.0, min_score=0.0)
    assert {hit.chunk_id for hit in unconstrained.hits} == {near_id, far_id}


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


def test_the_window_widens_below_the_calibrated_floor_so_the_fit_can_be_judged(
    bundle: StoreBundle, ingest_service: IngestService, embedder: DeterministicEmbedder
) -> None:
    """统计窗口比标定下限（bge-m3 **0.531**）低 → "可能相关"那一段先进分布，再由收敛切掉。

    这就是"放宽相关度下限"存在的理由：只看 0.531 之上时，噪声那一整段根本不进分布，
    ``max`` 与 ``baseline`` 没得比，``fit`` 永远是 strong/weak——"资料里没有"这句
    最要紧的话就再也说不出来。摆好的两条分别是：

    - **0.40**：窗口（0.37）之上、标定下限（0.531）之下 → 收进分布，判 ``none``；
    - **0.25**：窗口之下 → 连进来都不该进来。

    最后收敛回 0 条（``fit=none`` 的建议就是 0）：``filtered_out`` 因此是 2——
    一条在窗口外、一条被收敛切掉，计数要把两件事都算上。
    """
    near_id, far_id = _seed_two_documents(bundle, ingest_service, embedder)
    query_vector = _unit_vector(1.0)
    bundle.vectors.upsert_vectors(
        "kb_floor",
        items=[(near_id, _unit_vector(0.40)), (far_id, _unit_vector(0.25))],
    )
    service = _floor_service(bundle, "BAAI/bge-m3", query_vector)

    response = _search_vector_only(service, query_vector)

    assert response.distribution is not None
    assert response.distribution.floor == DEFAULT_STATS_FLOOR
    assert response.distribution.count == 1, "0.40 那一段必须先进分布，否则判不出契合程度"
    assert response.distribution.fit == FIT_NONE
    assert response.filtered_out == 2
    assert response.hits == []
    assert response.kept == 0, "收敛成 0 条（fit=none 的建议）时连名单都不留"
    assert response.next_offset is None, "一条都没有时不该说「还有下一页」（那会变成死循环）"
    assert response.decision is not None and response.decision["keep"] == 0


# ------------------------------------------------------------------ 翻页（v0.63）
#
# ``keep`` 从"这一轮一共几条"变成"一页几条"，``top_k`` 是"能翻到的总量"。
# 这几条钉住三件事：一页一页拼起来等于不翻页的那一份（不漏不重）、判定不随翻页变、
# 翻到底与翻过头都不报错。


def test_paging_walks_the_survivors_without_gaps_or_overlaps(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """一页一页走一遍，拼起来正好是**不翻页时那一整份幸存者名单**。

    这是"把这本书关于 X 的内容看全"那条路上的最小验收：``top_k`` 开大、``keep`` 当页大小，
    按 ``next_offset`` 一路翻。两条性质一起钉住——**不漏不重**（拼起来逐条相等），
    以及**每一页的账都对得上**（``kept`` 与 ``decision`` 不随翻页动）。
    """
    whole = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], top_k=10, keep=10)
    )
    assert whole.kept == len(whole.hits), "keep ≥ 幸存者数时这一页就是全部"
    assert whole.kept >= 2, "这份夹具里够翻两页（否则下面的循环只跑一轮，测不出拼接）"

    walked: list[str] = []
    offsets: list[int] = []
    offset: int | None = 0
    while offset is not None:
        page = retrieval.search(
            RetrievalQuery(
                query="向量检索 混合召回", kb_ids=["kb_1"], top_k=10, keep=2, offset=offset
            )
        )
        assert page.offset == offset, "回显的是**实际生效**的那个起点"
        assert len(page.hits) <= 2, "keep=2 就是一页两条"
        # 判定与收敛的产物在每一页上都必须一模一样
        assert page.kept == whole.kept
        assert page.distribution == whole.distribution
        assert page.filtered_out == whole.filtered_out
        assert page.decision is not None and whole.decision is not None
        # 与"一页给全部"那一次比**只差 `keep` 这一位**——页大小是调用方传的，不是判定；
        # 于是翻页本身（只动 offset）改不动 `decision` 的任何一位
        assert {**page.decision, "keep": whole.decision["keep"]} == whole.decision
        walked.extend(hit.chunk_id for hit in page.hits)
        offsets.append(offset)
        offset = page.next_offset

    assert offsets == list(range(0, len(offsets) * 2, 2)), "下一跳就是上一页的条数累计"
    assert len(walked) == len(set(walked)), "两页之间不许重叠"
    assert walked == [hit.chunk_id for hit in whole.hits], "拼起来必须等于那整份名单"


def test_the_last_page_says_there_is_nothing_after_it(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """最后一页的 ``next_offset`` 是 ``None``——调用方不必自己算"翻完没有"。"""
    whole = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], top_k=10, keep=10)
    )
    last = retrieval.search(
        RetrievalQuery(
            query="向量检索 混合召回",
            kb_ids=["kb_1"],
            top_k=10,
            keep=2,
            offset=whole.kept - 1,
        )
    )

    assert len(last.hits) == 1, "从倒数第二条起只剩一条"
    assert last.next_offset is None


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """「翻过头了」不是错：回一页空的，判定照旧。

    两件事都要在：**不抛异常**（把"翻到底了"报成一次失败，只会让调用方以为检索坏了），
    以及**判定一字不变**（它说的是那批候选，与调用方翻到哪儿无关）。
    """
    whole = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], top_k=10, keep=10)
    )

    beyond = retrieval.search(
        RetrievalQuery(
            query="向量检索 混合召回", kb_ids=["kb_1"], top_k=10, keep=10, offset=whole.kept + 99
        )
    )

    assert beyond.hits == []
    assert beyond.next_offset is None
    assert beyond.offset == whole.kept + 99, "回显传进来的起点（只是没有那一条可给）"
    assert beyond.kept == whole.kept
    assert beyond.distribution == whole.distribution
    assert beyond.decision == whole.decision, "翻过头也改不动判定（同一个 keep）"


@pytest.mark.parametrize("garbage", [-3, None, "不是数", [], {"从": 2}])
def test_a_garbage_offset_degrades_to_the_first_page(
    seeded: StoreBundle, retrieval: RetrievalService, garbage: object
) -> None:
    """负的、给不出数的、压根不是数的起点一律当 0——**不报错**。

    与三个收敛旋钮同一条取舍（``tools._int_or_none`` / ``service.resolve_offset``）：
    它是"顺手给的调节量"，给错一个数就让整次检索失败不划算。``[]`` 与 ``{"从": 2}``
    也在这份名单里：**服务层自己不信任类型**，容错不能只写在 MCP 那一层。
    """
    first = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], top_k=10, keep=10)
    )

    response = retrieval.search(
        RetrievalQuery(
            query="向量检索 混合召回",
            kb_ids=["kb_1"],
            top_k=10,
            keep=10,
            offset=garbage,  # type: ignore[arg-type]
        )
    )

    assert response.offset == 0, "折成 0 之后要如实回显"
    assert [hit.chunk_id for hit in response.hits] == [hit.chunk_id for hit in first.hits]


# ------------------------------------------------- 按库命中记录（v0.61）


def _recording_service(
    bundle: StoreBundle,
    embedder: DeterministicEmbedder,
    captured: list[dict],
) -> RetrievalService:
    """带一个"把回调参数接住"的 ``search_recorder`` 的检索服务。

    **接住而不是落库**：这一批用例要钉的是"检索层交出了什么"，
    落库与聚合由 ``tests/unit/services/test_search_stats.py`` 管。
    """
    return RetrievalService(
        bundle,
        embedder=embedder,
        reranker=NoopReranker(),
        search_recorder=lambda **kwargs: captured.append(kwargs),
    )


def test_search_records_each_knowledge_base_including_the_empty_one(
    seeded: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    """一次检索按库交出 ``(候选, 留存)``，**检索了但一条没命中的库也在里面**。

    ``kb_3`` 是个空库：向量分区都没有，一路回来什么都没有。它必须留下 ``(0, 0)``
    的一行——"这个库最近没人查"与"查了但答不上来"是两件事，
    而后者正是 KB 卡片上那条曲线要替用户看出来的。
    """
    captured: list[dict] = []
    service = _recording_service(seeded, embedder, captured)
    KnowledgeBaseService(seeded, embedder=embedder).create(kb_id="kb_3", name="空库")

    service.search(RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1", "kb_3"]))

    assert len(captured) == 1, "一次检索只交一批，不是一个库一次"
    call = captured[0]
    assert set(call["hits_by_kb"]) == {"kb_1", "kb_3"}
    assert call["hits_by_kb"]["kb_3"] == (0, 0), "检索过但没命中也要记账"
    candidates, kept = call["hits_by_kb"]["kb_1"]
    assert candidates > 0
    assert 0 < kept <= candidates
    assert call["fit"], "契合度是整批的判定，不能空着"
    assert call["duration_ms"] >= 0


def test_the_record_keeps_the_pre_cut_and_post_cut_counts_apart(
    seeded: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    """``candidates`` 是**收敛前**那批，``kept`` 是收敛后剩下的——被切掉的留在前者里。

    这里显式把**池子**收到 1 条（``top_k=1``，v0.63 起它才是"能翻到的总量"）：
    收敛之后只剩 1 条。如果记录只留"返回了几条"，这次的"捞上来一堆、只留一条"与
    另一次"只捞上来一条"就长得一模一样了。

    **``keep`` 已经不是那把刀**（它现在是一页几条）：从前这条用 ``keep=1`` 来切，
    而那时 ``keep`` 与 ``top_k`` 是同一个数——现在切名单的是 ``top_k``。
    """
    captured: list[dict] = []
    service = _recording_service(seeded, embedder, captured)

    response = service.search(RetrievalQuery(query="部署 配置 环境变量", kb_ids=["kb_1"], top_k=1))

    candidates, kept = captured[0]["hits_by_kb"]["kb_1"]
    assert len(response.hits) == 1
    assert kept == 1 == response.kept
    assert candidates > kept, "收敛切掉的那些必须留在 candidates 里"


def test_paging_does_not_inflate_the_recorded_kept(
    seeded: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    """**翻三页不等于三次命中**：``kept`` 记的是那一次收敛留下的总数，不是这一页几条。

    这是翻页最容易踩的那个坑：照"这次返回了几条"记的话，同一个主题翻三页就把那一天
    在曲线上记成三倍——而那条曲线要回答的恰恰是"这个库的命中在涨还是在跌"，
    被页数乘一遍之后就只剩"用户翻得多不多"。所以 ``candidates`` 与 ``kept`` 在每一页上
    都必须一模一样（它们是**这次检索**的属性）。
    """
    captured: list[dict] = []
    service = _recording_service(seeded, embedder, captured)
    query = "向量检索 混合召回"

    whole = service.search(RetrievalQuery(query=query, kb_ids=["kb_1"], top_k=10, keep=10))
    assert whole.kept >= 2, "这份夹具里够翻两页"

    offset: int | None = 0
    pages = 0
    while offset is not None:
        page = service.search(
            RetrievalQuery(query=query, kb_ids=["kb_1"], top_k=10, keep=1, offset=offset)
        )
        assert len(page.hits) == 1, "一页一条，翻的正是那条名单"
        offset = page.next_offset
        pages += 1

    assert pages == whole.kept, "翻的页数 = 幸存者条数（keep=1 时正好一个一条）"
    assert len(captured) == pages + 1, "每一次调用记一批（翻页也是调用，只是一次检索）"
    for call in captured:
        assert call["hits_by_kb"]["kb_1"] == (
            whole.distribution.count,
            whole.kept,
        ), "每一页记下来的都是同一次检索的候选数与收敛结果"


def test_a_search_that_finds_nothing_still_records_the_knowledge_base(
    seeded: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    """一个都没命中：``fit=none`` + 候选 0 的一行，而不是不写。

    "库里没有"是这条数据的一半价值（"这几天有多少次是答不上来"）。
    """
    captured: list[dict] = []
    service = _recording_service(seeded, embedder, captured)
    KnowledgeBaseService(seeded, embedder=embedder).create(kb_id="kb_3", name="空库")

    service.search(RetrievalQuery(query="随便问问", kb_ids=["kb_3"]))

    assert captured[0]["hits_by_kb"] == {"kb_3": (0, 0)}
    assert captured[0]["fit"] == FIT_NONE


def test_a_search_with_no_knowledge_base_records_nothing(
    seeded: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    """``kb_ids`` 为空的"全库检索"在入口就返回了（连分布都不算）。

    真走到记录那一步又一条都没命中时，**没有任何库可以被归因**——
    写一行 ``kb_id=''`` 只会让趋势图上多一条点不进去的曲线，所以宁可不写。
    """
    captured: list[dict] = []
    service = _recording_service(seeded, embedder, captured)

    response = service.search(RetrievalQuery(query="向量检索", kb_ids=[]))

    assert response.hits == []
    assert captured == []


def test_a_broken_recorder_never_breaks_the_search(
    seeded: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    """记录失败**绝不能**反过来影响检索：用户要的是结果，不是一行统计。"""

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("统计库挂了")

    service = RetrievalService(
        seeded, embedder=embedder, reranker=NoopReranker(), search_recorder=_boom
    )

    response = service.search(RetrievalQuery(query="向量检索", kb_ids=["kb_1"]))

    assert response.hits, "记录抛异常不该让这次检索变成空结果"


# ------------------------------------------- 每条命中来自哪条通道 / min_score 的两半（v0.62）


def test_hybrid_hits_say_they_came_from_the_vector_channel(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """``basis`` 说的是"这条是哪条通道捞上来的"，取值就是 ``raw_scores`` 的键名。

    为什么要单独一位：``min_score`` **只切向量通道的命中**（词面通道没有余弦可比），
    所以调用方要逐条看它才知道手里这条受不受阈值约束。取值不许另起一套说法
    （"semantic" 之类）——同一件事两个名字，必然有一处先漂。
    """
    response = retrieval.search(RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"]))

    assert response.hits
    for hit in response.hits:
        assert hit.basis in {"vector", "fulltext"}
        # 有余弦的那些必然是向量通道捞上来的（两者同源，见 `_materialize`）
        assert (hit.similarity is not None) == ("vector" in hit.raw_scores)
        if hit.basis == "vector":
            assert hit.lexical_score is not None or "fulltext" not in hit.raw_scores


def test_a_keyword_only_hit_has_a_lexical_score_and_no_similarity(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """词面通道捞上来的那条：``similarity`` 为 ``None``，但 ``lexical_score`` 有值。

    这一位是给调用方自己筛的：阈值管不着词面通道，它想更严只能读这个数。
    """
    response = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], mode=RetrievalMode.FULLTEXT)
    )

    assert response.hits
    for hit in response.hits:
        assert hit.basis == "fulltext"
        assert hit.similarity is None
        assert hit.lexical_score == hit.raw_scores["fulltext"]


def test_a_high_min_score_keeps_the_unscored_hits_but_counts_them(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """**纯关键词那一档 + ``min_score=0.99``**：命中照旧回来，但条数被记下来了。

    这是用户实测踩到的那个现象——把阈值提到 0.99，回来的每条 ``similarity`` 都是
    ``null``。留是对的（没有余弦可比，就不该拿一个不是它的数去比），但**必须说清**：
    ``decision.unscored_kept`` 就是那句"这几条绕过了这一刀"。
    """
    response = retrieval.search(
        RetrievalQuery(
            query="向量检索 混合召回",
            kb_ids=["kb_1"],
            mode=RetrievalMode.FULLTEXT,
            min_score=0.99,
            min_vector_score=0.0,
            min_term_coverage=0.0,
        )
    )

    assert response.hits, "没有余弦可比不等于该被这一刀切掉"
    assert all(hit.similarity is None for hit in response.hits)
    assert response.decision["min_score"] == 0.99
    # 这一批**每条**都没有余弦，所以窗口里收了多少条就有多少条绕过这一刀
    assert response.decision["unscored_kept"] == response.distribution.count


def test_a_vector_search_bypasses_nothing(seeded: StoreBundle, retrieval: RetrievalService) -> None:
    """反过来：个个都有余弦时这一位就是 0——它不是"某次检索的一个装饰数字"。"""
    response = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], min_score=0.0)
    )

    assert response.hits
    assert response.decision["min_score"] == 0.0
    assert response.decision["unscored_kept"] == 0


def test_per_doc_alone_is_a_decision_from_the_caller(
    seeded: StoreBundle, retrieval: RetrievalService
) -> None:
    """只传 ``per_doc`` 也算调用方自己的决定：``decided_by`` 必须是 ``model``。

    一边照他给的数收敛、一边在载荷里说"这是系统建议的"，是同一份载荷里的自相矛盾
    （实测被用户抓到的就是这一条）。三个旋钮都是"调用方按分布自己算出来的收敛方式"。
    """
    response = retrieval.search(
        RetrievalQuery(query="向量检索 混合召回", kb_ids=["kb_1"], per_doc=1)
    )

    assert response.decision["per_doc"] == 1
    assert response.decision["decided_by"] == "model"


# ------------------------------------------------- 过滤下推：先裁候选池，再谈条数（v0.64）
#
# 从前元数据过滤只在**取完候选之后**应用：两条通道各自按 ``candidate_k`` 取满，再一条条
# 按它所属文档的元数据丢掉。后果正是用户报的那件事——"只查某本书"筛成 0 条或很少条，
# 因为那本书的块压根没进过候选（名额被别的书占满了）。医学场景想"只查某本书某章"
# 于是只能事后筛。
#
# 现在条件被翻成存储层的 ``SearchFilter`` 下推进 SQL（向量那侧 JOIN ``chunks`` /
# ``documents``），名额在**过滤后的集合里**分。
#
# 下面这批用例摆的是**手工构造的块**（不走摄入链路）：要验的是"条件有没有进 SQL"，
# 页码与标题路径必须由用例指定——真走解析器的话 ``.md`` 来源的 ``page`` 恒为 ``None``
# （那正是"按页码筛会把整份 md 排除"的由来），摆不出边界。

POOL_KB = "kb_pool"


def _pool_chunk(
    chunk_id: str,
    document_id: str,
    ordinal: int,
    text: str,
    *,
    page: int | None = None,
    heading: str | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        knowledge_base_id=POOL_KB,
        part_id=None,
        ordinal=ordinal,
        text=text,
        content_hash=f"h-{chunk_id}",
        heading_path=heading,
        page=page,
    )


#: 每段的余弦（与查询向量的重合度）。摆法：**运维那本整体更近**——于是"全局最近的两条
#: 都在别的书里"，这正是"只查医学手册"在旧实现下会筛空的原因。
_POOL_COSINES = {
    "c_med_a": 0.90,
    "c_med_b": 0.89,
    "c_med_c": 0.88,
    "c_med_d": 0.87,
    "c_ops_a": 0.99,
    "c_ops_b": 0.98,
    "c_ops_c": 0.97,
}

_POOL_CHUNKS = (
    # 医学手册：三段的页码与标题都在，另有一段**两样都没有**（用来钉住"被整体排除"）
    _pool_chunk("c_med_a", "doc_med", 0, "检索 与 医学 手册 第一节 的 正文", page=5,
                heading="第3章 > 3.1节"),
    _pool_chunk("c_med_b", "doc_med", 1, "检索 与 医学 手册 第二节 的 正文", page=10,
                heading="第3章 > 3.2节"),
    _pool_chunk("c_med_c", "doc_med", 2, "检索 与 医学 手册 第四节 的 正文", page=20,
                heading="第4章 > 4.1节"),
    _pool_chunk("c_med_d", "doc_med", 3, "检索 与 医学 手册 的 无页码 段落"),
    # 运维手册：词面密度更高（排在全文通道前面），页码与标题自成一套
    _pool_chunk("c_ops_a", "doc_ops", 0, "检索 检索 检索 检索", heading="Chapter 7 > 7.1"),
    _pool_chunk("c_ops_b", "doc_ops", 1, "检索 检索 检索 检索", page=7,
                heading="Chapter 7 > 7.2"),
    _pool_chunk("c_ops_c", "doc_ops", 2, "检索 检索 检索 检索", page=8,
                heading="Chapter 7 > 7.3"),
)


def _seed_pool(bundle: StoreBundle) -> None:
    """一个库、两篇文档、七个块：页码 / 标题路径 / 向量全部由用例指定。"""
    bundle.meta.create_knowledge_base(
        KnowledgeBaseRecord(
            id=POOL_KB,
            name="池子库",
            embedding_model_id=DeterministicEmbedder.model_id,
            embedding_dim=DIM,
        )
    )
    for document_id, name in (("doc_med", "医学手册.pdf"), ("doc_ops", "运维手册.pdf")):
        bundle.meta.create_document(
            DocumentRecord(
                id=document_id,
                knowledge_base_id=POOL_KB,
                name=name,
                source_kind=DataSourceKind.UPLOAD,
                content_hash=f"h-{document_id}",
                stage=DocumentStage.UPLOADED,
            )
        )
    # 分文档写入：``replace_chunks`` 是按文档整份替换的
    bundle.meta.replace_chunks("doc_med", [c for c in _POOL_CHUNKS if c.document_id == "doc_med"])
    bundle.meta.replace_chunks("doc_ops", [c for c in _POOL_CHUNKS if c.document_id == "doc_ops"])
    bundle.fulltext.index_chunks(_POOL_CHUNKS)
    # 向量分区：正常由摄入链路建，这里手工建（用例只关心检索）
    bundle.vectors.ensure_partition(POOL_KB, dim=DIM)
    bundle.vectors.upsert_vectors(
        POOL_KB,
        items=[(chunk.chunk_id, _unit_vector(_POOL_COSINES[chunk.chunk_id]))
               for chunk in _POOL_CHUNKS],
    )


@pytest.fixture
def pool(bundle: StoreBundle) -> StoreBundle:
    """摆好那七段（见 ``_seed_pool``）。"""
    _seed_pool(bundle)
    return bundle


def _search_pool(
    retrieval: RetrievalService,
    filters: MetadataFilter | None,
    *,
    mode: str = RetrievalMode.FULLTEXT,
    **overrides: object,
):
    """在池子库上跑一次（默认全文通道：七段都含查询词，集合式的断言不看名次）。"""
    params: dict[str, object] = {
        "query": "检索",
        "kb_ids": [POOL_KB],
        "mode": mode,
        "top_k": 8,
        "candidate_k": 8,
        "filters": filters,
    }
    params.update(overrides)
    return retrieval.search(
        RetrievalQuery(**params),  # type: ignore[arg-type]
        query_vector=_unit_vector(1.0),
    )


def test_the_document_filter_shrinks_the_candidate_pool_vector(pool: StoreBundle,
                                                               retrieval: RetrievalService) -> None:
    """**这次改动的核心用例（向量通道）**：名额要在那本书里分，不是取完再丢。

    摆法：运维手册那三段的余弦整体更高（0.99/0.98/0.97），医学手册是 0.90 往下；
    ``top_k=2``、``candidate_k=2``，只看医学手册。于是：

    - **旧实现（事后筛）**：向量通道先取全局最近的两条——两条都在运维手册里——
      再按文档丢掉，**一条都不剩**（用户说的"只能事后筛"就是这个形状）；
    - **现在（下推）**：SQL 里就只要医学手册的块，两条名额在它自己的段里分。
    """
    response = _search_pool(
        retrieval,
        MetadataFilter(document_ids=["doc_med"]),
        mode=RetrievalMode.VECTOR,
        top_k=2,
        candidate_k=2,
    )

    assert [hit.document_id for hit in response.hits] == ["doc_med", "doc_med"]
    assert [hit.chunk_id for hit in response.hits] == ["c_med_a", "c_med_b"]


def test_the_document_filter_shrinks_the_candidate_pool_fulltext(
    pool: StoreBundle, retrieval: RetrievalService
) -> None:
    """同一个场景走**全文通道**：名额同样在过滤后的集合里分。

    运维手册那三段词面密度更高（``ts_rank_cd`` 排在前面），所以旧的"取满两条再丢"
    同样会全军覆没。两条通道各自都要受限——只改一路会留下"全文搜不到、向量搜得到"
    那种静默不一致。
    """
    response = _search_pool(
        retrieval,
        MetadataFilter(document_ids=["doc_med"]),
        top_k=2,
        candidate_k=2,
    )

    assert len(response.hits) == 2
    assert {hit.document_id for hit in response.hits} == {"doc_med"}


def test_page_range_bounds_are_inclusive_and_null_pages_are_excluded(
    pool: StoreBundle, retrieval: RetrievalService
) -> None:
    """``page_from`` / ``page_to`` **含边界**，且**没有页码的块被整体排除**。

    ``c_med_d``（页码为空）与 ``c_ops_a``（页码为空）在两种条件里都不满足：
    SQL 里 ``NULL >= 5`` 是 ``NULL``、不是真——"我只第 5–20 页"那句话里没有
    "页码未知的也要"的位置。代价写在 ``MetadataFilter`` 的说明里：
    没有页码的**来源**（.md / Office）一旦按页码筛就是一整批都查不到。
    """
    def pages(filters: MetadataFilter) -> set[int | None]:
        return {hit.page for hit in _search_pool(retrieval, filters).hits}

    assert pages(MetadataFilter(page_from=5)) == {5, 7, 8, 10, 20}
    assert pages(MetadataFilter(page_to=7)) == {5, 7}
    assert pages(MetadataFilter(page_from=5, page_to=10)) == {5, 7, 8, 10}
    # 区间之外：一条都没有，而不是"回退成不过滤"
    assert pages(MetadataFilter(page_from=100)) == set()
    assert pages(MetadataFilter(page_to=1)) == set()


def test_heading_contains_is_case_insensitive_and_skips_empty_headings(
    pool: StoreBundle, retrieval: RetrievalService
) -> None:
    """``heading_contains`` 是**子串**匹配且大小写不敏感；标题路径为空的块被排除。

    找"某章"它比页码准：标题路径是切块时按原文层级拼出来的，中文文档里也没有页码。
    大小写不敏感与 SQL 那一侧同一口径（``position(lower(x) in lower(heading_path))``），
    否则同一份条件在"下推生效"与"兜底守卫"两条路上会给出不同答案。
    """
    def chunk_ids(filters: MetadataFilter) -> set[str]:
        return {hit.chunk_id for hit in _search_pool(retrieval, filters).hits}

    assert chunk_ids(MetadataFilter(heading_contains="第3章")) == {"c_med_a", "c_med_b"}
    assert chunk_ids(MetadataFilter(heading_contains="3.2")) == {"c_med_b"}
    # 大小写不敏感：库里存的是 "Chapter 7 > 7.2"
    assert chunk_ids(MetadataFilter(heading_contains="chapter 7")) == {
        "c_ops_a",
        "c_ops_b",
        "c_ops_c",
    }
    assert chunk_ids(MetadataFilter(heading_contains="CHAPTER")) == {
        "c_ops_a",
        "c_ops_b",
        "c_ops_c",
    }
    assert chunk_ids(MetadataFilter(heading_contains="没这一章")) == set()
    # `c_med_d` 的 heading_path 为空 → 任何标题条件都排除它
    assert chunk_ids(MetadataFilter(heading_contains="第")) == {"c_med_a", "c_med_b", "c_med_c"}


def test_filters_combine_with_and(pool: StoreBundle, retrieval: RetrievalService) -> None:
    """多个条件一起给是**与**：文档、页码区间、标题子串都得满足。"""
    response = _search_pool(
        retrieval,
        MetadataFilter(
            document_ids=["doc_med"],
            page_from=5,
            page_to=10,
            heading_contains="3.2",
        ),
    )

    assert [hit.chunk_id for hit in response.hits] == ["c_med_b"]


def test_both_channels_respect_the_same_filter(pool: StoreBundle,
                                               retrieval: RetrievalService) -> None:
    """三条路（纯向量 / 纯全文 / 混合）在同一个条件下回**同一批**块。

    只把过滤加在其中一条通道上，就会出现"某些模式下过滤不生效"——那种不一致最难查，
    因为它取决于调用方选了哪个 mode。
    """
    for mode in (RetrievalMode.VECTOR, RetrievalMode.FULLTEXT, RetrievalMode.HYBRID):
        response = _search_pool(retrieval, MetadataFilter(heading_contains="第3章"), mode=mode)
        assert {hit.chunk_id for hit in response.hits} == {"c_med_a", "c_med_b"}, mode


def test_paging_still_walks_the_filtered_survivors(pool: StoreBundle,
                                                   retrieval: RetrievalService) -> None:
    """过滤之后翻页照旧：拼起来等于不翻页那一整份，且**每一页都满足条件**。

    这条钉的是"下推没有把翻页弄坏"：名额裁掉之后，``top_k`` / ``keep`` / ``offset``
    那套账要在**过滤后的集合**上算（旧实现下这里会算出"第二页是空的"）。
    """
    filters = MetadataFilter(page_from=5)
    whole = _search_pool(retrieval, filters, keep=8)
    assert whole.kept == 5, "第 5 页及以后的块共五段（两段没有页码）"

    walked: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = _search_pool(retrieval, filters, keep=2, offset=offset)
        assert page.kept == whole.kept, "判定与翻页无关"
        assert all(hit.page is not None and hit.page >= 5 for hit in page.hits)
        walked.extend(hit.chunk_id for hit in page.hits)
        offset = page.next_offset

    assert walked == [hit.chunk_id for hit in whole.hits]
