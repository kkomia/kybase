"""摄入流水线的集成测试（M2 主链路）。

覆盖架构 §4 的端到端承诺：上传 → 探测 → 解析 → 切分 → 向量化 → indexed，
以及去重、失败定位、断点续跑与模型锁定。
"""

import os
import pathlib
import struct
import zlib

import pytest

from app.core.exceptions import ConflictError, InvalidRequestError
from app.models.enums import DocumentStage
from app.parsers.base import ParseError
from app.parsers.media_direct import MediaDirectParser
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.documents import DocumentService
from app.services.embedding.base import EmbeddingError
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestCanceled, IngestError, IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.storage.base import StoreBundle

DIM = 32
MARKDOWN = (
    "# 知识库设计\n\n"
    "向量检索与全文检索混合召回。\n\n"
    "## 切分策略\n\n"
    "固定长度与语义切块两种模式。\n"
)


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=DIM)


@pytest.fixture
def kb_service(bundle: StoreBundle, embedder: DeterministicEmbedder) -> KnowledgeBaseService:
    return KnowledgeBaseService(bundle, embedder=embedder)


@pytest.fixture
def ingest_service(bundle: StoreBundle, embedder: DeterministicEmbedder) -> IngestService:
    return IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )


@pytest.fixture
def kb(bundle: StoreBundle, kb_service: KnowledgeBaseService):
    return kb_service.create(kb_id="kb_1", name="默认库")


# --------------------------------------------------------------------- 知识库


def test_create_knowledge_base_freezes_model_and_dim(kb) -> None:
    """架构 §6.4：模型与维度随库冻结，作为后续写入的校验依据。"""
    assert kb.embedding_model_id == "dev/deterministic-hash"
    assert kb.embedding_dim == DIM


def test_ingest_above_the_index_limit_works_and_says_so(
    bundle: StoreBundle, caplog
) -> None:  # type: ignore[no-untyped-def]
    """>2000 维的库：**照常入库**（精确检索），但要在日志里说清"没有向量索引"。

    实测踩到的原文：``column cannot have more than 2000 dimensions for hnsw index``
    （pgvector 的 HNSW 上限）。处置是"不建索引 + 如实说清"，**不是**让建库失败——
    2048 正是 WeMM 的原生维度，把它拦下来等于因为建不了索引就不许用原生精度。
    """
    from app.storage.postgres_impl.vector_store import HNSW_MAX_DIM, PostgresVectorStore

    big_dim = HNSW_MAX_DIM + 48
    embedder = DeterministicEmbedder(dim=big_dim)
    kb = KnowledgeBaseService(bundle, embedder=embedder).create(kb_id="kb_big", name="大维度库")
    service = IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )

    with caplog.at_level("WARNING", logger="app.services.ingest"):
        outcome = service.submit(
            knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode()
        )
        result = service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert result.chunk_count > 0
    assert bundle.vectors.declared_dim(kb.id) == big_dim
    # 没有索引这件事必须留在日志里（那句话同时由 `ensure_partition` 返回给调用方）
    assert any("没有向量索引" in record.getMessage() for record in caplog.records)
    vectors = bundle.vectors
    assert isinstance(vectors, PostgresVectorStore)
    assert vectors.has_hnsw_index(kb.id) is False
    # 而且检索照常（精确扫描）：刚写进去的那一条能被召回
    assert bundle.vectors.search(
        kb.id, query_vector=embedder.embed([MARKDOWN])[0], top_k=1
    ), "没有索引不等于查不到——精确扫描必须仍然返回结果"


def test_kb_service_raises_for_missing_kb(kb_service: KnowledgeBaseService) -> None:
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        kb_service.get("kb_none")


# --------------------------------------------------------------------- 主链路


def test_ingest_reaches_indexed(bundle: StoreBundle, ingest_service: IngestService, kb) -> None:
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    assert outcome.document.stage is DocumentStage.UPLOADED

    result = ingest_service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert result.document.error is None
    assert result.chunk_count > 0


# --------------------------------------------------------- 原地替换内容（v0.12）


OLD = "# 旧结论\n\n锂价下跌必然利好电池厂。\n"
NEW = "# 新结论\n\n锂价下跌通常缓解材料成本，但净影响取决于售价联动与库存。\n"


def test_replace_rewrites_content_and_resets_stage(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """替换必须做到三件事：换元数据、**清掉旧产物**、把阶段推回 uploaded。

    清产物这条最要紧：旧切块留着的话，重新索引完成之前检索会同时命中新旧两版，
    表现成"我明明改了，搜出来还是旧的"。
    """
    outcome = ingest_service.submit(
        knowledge_base_id="kb_1", filename="note.md", content=OLD.encode()
    )
    doc_id = outcome.document.id
    ingest_service.ingest(doc_id)
    before = bundle.meta.get_document(doc_id)
    assert before is not None
    assert bundle.meta.count_chunks(doc_id) > 0

    replaced = ingest_service.replace(doc_id, filename="改过的笔记.md", content=NEW.encode())

    assert replaced.id == doc_id, "文档 id 必须不变——它是引用的锚点"
    assert replaced.content_hash != before.content_hash
    assert replaced.name == "改过的笔记.md"
    assert replaced.size_bytes == len(NEW.encode())
    assert replaced.stage is DocumentStage.UPLOADED, "内容变了就要重走一遍流水线"
    assert bundle.meta.count_chunks(doc_id) == 0, "旧切块必须清掉"

    # 重跑之后检索到的应该是新内容
    result = ingest_service.ingest(doc_id)
    assert result.document.stage is DocumentStage.INDEXED
    texts = "".join(item.text for item in bundle.meta.iter_chunks(doc_id))
    assert "售价联动" in texts
    assert "必然利好" not in texts


def test_replace_with_the_same_content_is_a_noop(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """内容没变就不该动：白跑一遍切分与向量化是纯浪费。"""
    outcome = ingest_service.submit(
        knowledge_base_id="kb_1", filename="note.md", content=OLD.encode()
    )
    doc_id = outcome.document.id
    ingest_service.ingest(doc_id)
    before = bundle.meta.get_document(doc_id)
    assert before is not None

    again = ingest_service.replace(doc_id, filename="note.md", content=OLD.encode())

    assert again.content_hash == before.content_hash
    assert again.stage is DocumentStage.INDEXED, "没变就不该把阶段推回去重跑"


def test_replace_refuses_while_a_task_is_running(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """有任务在跑时拒绝替换：worker 正在旧内容上写产物，这时替换会让新旧交叉。

    宁可报错让人稍后再改，也不留下错乱的产物——那类问题查起来极其费劲。
    """
    import uuid as _uuid

    from app.models.enums import TaskKind, TaskState
    from app.storage.base import TaskRecord

    outcome = ingest_service.submit(
        knowledge_base_id="kb_1", filename="note.md", content=OLD.encode()
    )
    doc_id = outcome.document.id
    bundle.meta.enqueue_task(
        TaskRecord(
            id=f"task_{_uuid.uuid4().hex[:12]}",
            kind=TaskKind.PARSE,
            state=TaskState.PENDING,
            payload={"document_id": doc_id},
            document_id=doc_id,
        )
    )

    with pytest.raises(ConflictError):
        ingest_service.replace(doc_id, filename="note.md", content=NEW.encode())


def test_ingest_writes_markdown_artifact(bundle: StoreBundle, ingest_service: IngestService,
                                         kb) -> None:
    """架构 §4.3：所有格式统一产出 Markdown，原文另留一份。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    ingest_service.ingest(outcome.document.id)

    parsed = bundle.meta.get_parse_result(outcome.document.id)
    assert parsed is not None
    assert parsed.parser_name == "PlainTextParser"
    assert bundle.objects.read(parsed.markdown_path).decode() == MARKDOWN
    assert parsed.probe_meta["kind"] == "text"
    assert "route_reason" in parsed.probe_meta  # 路由理由要能被界面解释


def test_ingest_makes_document_searchable_by_fulltext(bundle: StoreBundle,
                                                      ingest_service: IngestService, kb) -> None:
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    ingest_service.ingest(outcome.document.id)

    hits = bundle.fulltext.search(query="混合召回", top_k=5, kb_id="kb_1")
    assert hits
    assert hits[0].document_id == outcome.document.id


def test_ingest_writes_vectors(bundle: StoreBundle, ingest_service: IngestService, kb) -> None:
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    result = ingest_service.ingest(outcome.document.id)

    assert bundle.vectors.declared_dim("kb_1") == DIM
    query = [0.0] * DIM
    query[0] = 1.0
    matches = bundle.vectors.search("kb_1", query_vector=query, top_k=result.chunk_count)
    assert len(matches) == result.chunk_count


def test_original_file_is_kept(bundle: StoreBundle, ingest_service: IngestService, kb) -> None:
    """架构 §4.3：原文在文件系统保留一份（下载可给原件）。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    path = bundle.meta.get_setting(f"document.{outcome.document.id}.original_path")

    assert path is not None and path.startswith("originals/")
    assert bundle.objects.read(path) == MARKDOWN.encode()


def test_probe_meta_records_coverage_and_suffix(bundle: StoreBundle,
                                                ingest_service: IngestService, kb) -> None:
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    ingest_service.ingest(outcome.document.id)

    meta = bundle.meta.get_parse_result(outcome.document.id).probe_meta
    assert meta["text_coverage"] == pytest.approx(1.0)
    assert meta["suffix"] == ".md"


# --------------------------------------------------------------------- 取消（v13 后）

class _CancelingParser(PlainTextParser):
    """解析过程中把文档置为 canceled，模拟"用户在这时候点了取消"。"""

    def __init__(self, bundle: StoreBundle, holder: dict) -> None:
        self._bundle = bundle
        self._holder = holder

    def parse(self, *, content, filename, mime_type, probe):  # type: ignore[no-untyped-def]
        result = super().parse(
            content=content, filename=filename, mime_type=mime_type, probe=probe
        )
        self._bundle.meta.update_document_stage(
            self._holder["id"], DocumentStage.CANCELED, error="已取消"
        )
        return result


def test_ingest_stops_at_the_next_stage_boundary_when_canceled(
    bundle: StoreBundle, embedder: DeterministicEmbedder, kb
) -> None:
    """取消是协作式的：阶段边界发现 canceled 就抛 IngestCanceled，不再往下走。

    这一条要证的是**不会出现"取消了却继续向量化"**：解析回来之后、切分之前
    就停手，文档保持在 canceled（不被后续推进覆盖）。
    """
    holder: dict = {}
    service = IngestService(
        bundle,
        router=ParserRouter([_CancelingParser(bundle, holder)]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )
    outcome = service.submit(knowledge_base_id="kb_1", filename="kb.md", content=MARKDOWN.encode())
    holder["id"] = outcome.document.id

    with pytest.raises(IngestCanceled):
        service.ingest(outcome.document.id)

    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.CANCELED
    assert bundle.meta.count_chunks(outcome.document.id) == 0


def test_canceled_document_can_be_reingested(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """"取消"不是死路：重新摄入应当从已有产物续跑并走到 indexed。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    bundle.meta.update_document_stage(outcome.document.id, DocumentStage.CANCELED, error="已取消")

    result = ingest_service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED


# --------------------------------------------------------------------- 去重


def test_duplicate_upload_is_detected(ingest_service: IngestService, kb) -> None:
    """架构 §6.3：上传命中相同 hash 时提醒"检测到相同文件"，只保留一份。"""
    first = ingest_service.submit(knowledge_base_id="kb_1", filename="a.md",
                                 content=MARKDOWN.encode())
    second = ingest_service.submit(knowledge_base_id="kb_1", filename="a-copy.md",
                                  content=MARKDOWN.encode())

    assert second.is_duplicate is True
    assert second.document.id == first.document.id


def test_same_content_in_different_kb_is_not_duplicate(
    bundle: StoreBundle, ingest_service: IngestService, kb_service: KnowledgeBaseService, kb
) -> None:
    kb_service.create(kb_id="kb_2", name="第二库")
    first = ingest_service.submit(knowledge_base_id="kb_1", filename="a.md",
                                 content=MARKDOWN.encode())
    second = ingest_service.submit(knowledge_base_id="kb_2", filename="a.md",
                                  content=MARKDOWN.encode())

    assert second.is_duplicate is False
    assert second.document.id != first.document.id


# --------------------------------------------------------------------- 失败与续跑


def test_empty_file_fails_with_stage_recorded(ingest_service: IngestService, kb) -> None:
    """失败要落到文档记录上：界面才能显示卡在哪一步、为什么（架构 §12）。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="empty.md", content=b"")

    with pytest.raises(IngestError) as excinfo:
        ingest_service.ingest(outcome.document.id)

    assert excinfo.value.stage == "parsing"
    document = ingest_service._stores.meta.get_document(outcome.document.id)
    assert document.stage is DocumentStage.FAILED
    assert "内容为空" in (document.error or "")


def test_unsupported_binary_is_rejected(ingest_service: IngestService, kb) -> None:
    outcome = ingest_service.submit(
        knowledge_base_id="kb_1",
        filename="scan.pdf",
        content=b"\x89PNG\r\n\x1a\n\x00\x00",
        mime_type="application/pdf",
    )

    with pytest.raises(IngestError, match="暂不支持"):
        ingest_service.ingest(outcome.document.id)


def test_resume_after_failure_skips_completed_steps(bundle: StoreBundle,
                                                    ingest_service: IngestService,
                                                    kb_service: KnowledgeBaseService, kb) -> None:
    """断点续跑：解析已完成就不该再调一次解析器（云解析是要花钱的）。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    document_id = outcome.document.id

    # 手工推进到 parsed，模拟"解析已完成、切分前失败"
    ingest_service._probe_and_parse(
        bundle.meta.get_document(document_id)
    )
    bundle.meta.update_document_stage(document_id, DocumentStage.FAILED, error="模拟切分失败")

    # 用一个"一旦被调用就失败"的路由器，证明它没被调用
    class _ExplodingParser(PlainTextParser):
        name = "ExplodingParser"

        def parse(self, **_kwargs):  # type: ignore[override]
            raise AssertionError("解析器不该在续跑时被再次调用")

    resumed = IngestService(
        bundle,
        router=ParserRouter([_ExplodingParser()]),
        embedder=DeterministicEmbedder(dim=DIM),
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )
    result = resumed.ingest(document_id)

    assert result.document.stage is DocumentStage.INDEXED


def test_resume_without_artifact_reports_parsing_stage(bundle: StoreBundle,
                                                       ingest_service: IngestService, kb) -> None:
    """状态说解析完了、产物却不在：不能假装能续跑，要报在 parsing 阶段。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())
    bundle.meta.update_document_stage(outcome.document.id, DocumentStage.PARSED)

    with pytest.raises(IngestError) as excinfo:
        ingest_service.ingest(outcome.document.id)

    assert excinfo.value.stage == "parsing"
    assert "找不到解析产物" in str(excinfo.value)


# --------------------------------------------------------------------- 模型锁定


def test_model_change_is_allowed_before_any_vector_exists(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """库内还没有向量时允许更正配置，不必逼用户重建库。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                    content=MARKDOWN.encode())

    switched = IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=DeterministicEmbedder(dim=64),
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )
    result = switched.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert bundle.meta.get_knowledge_base("kb_1").embedding_dim == 64  # type: ignore[union-attr]


def test_model_change_is_refused_once_vectors_exist(bundle: StoreBundle,
                                                    ingest_service: IngestService, kb) -> None:
    """架构 §6.4：库内已有向量化文件就不允许换模型——混用会让检索静默失效。"""
    first = ingest_service.submit(knowledge_base_id="kb_1", filename="kb.md",
                                  content=MARKDOWN.encode())
    ingest_service.ingest(first.document.id)

    second = ingest_service.submit(knowledge_base_id="kb_1", filename="another.md",
                                   content="另一个文件的内容。".encode())
    switched = IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=DeterministicEmbedder(dim=64),
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )

    # 配置错误直接抛 EmbeddingError：重试无意义，也不该把文档标成 failed
    with pytest.raises(EmbeddingError, match="不允许改用"):
        switched.ingest(second.document.id)

    assert bundle.meta.get_document(second.document.id).stage is DocumentStage.UPLOADED


def test_missing_kb_is_reported(bundle: StoreBundle, embedder: DeterministicEmbedder) -> None:
    from app.core.exceptions import NotFoundError

    service = IngestService(bundle, router=ParserRouter([PlainTextParser()]), embedder=embedder)
    with pytest.raises(NotFoundError):
        service.submit(knowledge_base_id="kb_none", filename="a.md", content=b"x")


def test_embedding_error_stage_is_reported(bundle: StoreBundle, kb) -> None:
    """embedding 端点挂了：失败要归到 embedding 阶段，而不是含混地算作解析失败。"""
    from app.services.embedding.base import EmbeddingProvider

    class _BrokenEmbedder(EmbeddingProvider):
        model_id = "dev/deterministic-hash"  # 与库内一致，绕过模型锁
        dim = DIM

        def embed(self, texts):
            raise EmbeddingError("端点不可达")

    service = IngestService(
        bundle, router=ParserRouter([PlainTextParser()]), embedder=_BrokenEmbedder()
    )
    outcome = service.submit(knowledge_base_id="kb_1", filename="kb.md", content=MARKDOWN.encode())

    with pytest.raises(IngestError) as excinfo:
        service.ingest(outcome.document.id)
    assert excinfo.value.stage == "embedding"


# --------------------------------------------------------------------- 切分参数按库生效（v17）


def _per_kb_service(bundle: StoreBundle, embedder: DeterministicEmbedder) -> IngestService:
    """**不注入 chunk_config** 的摄入服务：生产走的就是这条路径（按库读参数）。"""
    return IngestService(bundle, router=ParserRouter([PlainTextParser()]), embedder=embedder)


def test_chunking_follows_the_knowledge_base_config(
    bundle: StoreBundle, embedder: DeterministicEmbedder, kb_service: KnowledgeBaseService
) -> None:
    """切分参数按库生效——这是"存了没人用"那个缺陷的回归用例。

    之前 `KnowledgeBaseService.create(chunk_size=...)` 只是把值存进库，
    `IngestService` 永远用硬编码的 512/64。现在两个库给同一份文本、不同的块长，
    切出来的块数必须不同，且各自不超过自己的上限。
    """
    text = ("这是一段用于验证切分参数的文本。" * 60).encode()
    kb_service.create(kb_id="kb_tiny", name="小块库", chunk_size=128, chunk_overlap=16)
    kb_service.create(kb_id="kb_big", name="大块库", chunk_size=1024, chunk_overlap=0)
    service = _per_kb_service(bundle, embedder)

    tiny = service.submit(knowledge_base_id="kb_tiny", filename="a.md", content=text)
    service.ingest(tiny.document.id)
    big = service.submit(knowledge_base_id="kb_big", filename="b.md", content=text)
    service.ingest(big.document.id)

    tiny_sizes = [len(item.text) for item in bundle.meta.iter_chunks(tiny.document.id)]
    big_sizes = [len(item.text) for item in bundle.meta.iter_chunks(big.document.id)]
    assert tiny_sizes and big_sizes
    assert max(tiny_sizes) <= 128
    assert max(big_sizes) <= 1024
    # 同一个文本，块长差 8 倍，块数必须明显不同——否则说明参数根本没被读
    assert len(tiny_sizes) > len(big_sizes)


def test_injected_chunk_config_still_wins(
    bundle: StoreBundle, embedder: DeterministicEmbedder, kb_service: KnowledgeBaseService,
    ingest_service: IngestService,
) -> None:
    """显式注入的配置优先于库里的值（测试与批量导入的旁路，不能因为接线而失效）。"""
    kb_service.create(kb_id="kb_1", name="默认库", chunk_size=2048, chunk_overlap=0)
    text = ("用于验证注入优先的文本。" * 80).encode()
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="a.md", content=text)
    ingest_service.ingest(outcome.document.id)

    sizes = [len(item.text) for item in bundle.meta.iter_chunks(outcome.document.id)]
    assert max(sizes) <= 64  # 注入的是 size=64，不是库里的 2048


def test_legacy_out_of_range_chunking_is_clamped(
    bundle: StoreBundle, embedder: DeterministicEmbedder, kb_service: KnowledgeBaseService,
) -> None:
    """历史库里的越界参数（例如 overlap=4000 配 size=512）不许把摄入打挂。

    旧版建库接口允许这种组合入库；切分器收到 overlap >= size 会直接抛错，
    看起来像"文件坏了"。所以读出来时钳位。
    """
    record = kb_service.create(kb_id="kb_1", name="默认库")
    bundle.meta.set_knowledge_base_chunking("kb_1", 512, 4000)  # 模拟历史脏数据
    assert bundle.meta.get_knowledge_base("kb_1").chunk_overlap == 4000

    service = _per_kb_service(bundle, embedder)
    outcome = service.submit(
        knowledge_base_id="kb_1", filename="a.md", content=("文本内容。" * 200).encode()
    )
    result = service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert record.id == "kb_1"


def test_uploaded_html_is_stripped_before_indexing(
    bundle: StoreBundle, embedder, kb  # type: ignore[no-untyped-def]
) -> None:
    """上传的 .html 走正文提取（v17）。

    回归用例：原先 `.html` 落到纯文本直通（靠 MIME text/* 兜底），
    `<script>` 与导航原样入库——用户问什么都能匹配到菜单里的"首页 关于 联系方式"。
    """
    from app.parsers.html_upload import HtmlUploadParser

    service = IngestService(
        bundle,
        router=ParserRouter([HtmlUploadParser(), PlainTextParser()]),
        embedder=embedder,
    )
    html = (
        "<html><body>"
        "<nav>首页 关于我们 联系方式</nav>"
        "<p>眼轴长度是近视防控的核心指标。</p>"
        "<script>var tracking = 1;</script>"
        "</body></html>"
    )
    outcome = service.submit(
        knowledge_base_id="kb_1", filename="page.html", content=html.encode()
    )
    result = service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    parsed = bundle.meta.get_parse_result(outcome.document.id)
    assert parsed is not None
    assert parsed.parser_name == "HtmlUploadParser"
    markdown = bundle.objects.read(parsed.markdown_path).decode()
    assert "眼轴长度是近视防控的核心指标" in markdown
    assert "首页 关于我们" not in markdown
    assert "var tracking" not in markdown


# ------------------------------------------------- 解析引擎自动降级（v17）


class _FailingParser(PlainTextParser):
    """首选引擎：一定失败。"""

    name = "FailingParser"

    def parse(self, **_kwargs):  # type: ignore[override]
        raise ParseError("云端额度用尽", stage="parsing")


def test_parser_failure_degrades_to_the_next_candidate(bundle: StoreBundle, kb) -> None:  # type: ignore[no-untyped-def]
    """首选引擎失败时自动换下一个：扫描件有两条云端通道，文字型 PDF 还有本地直提。

    原先只挑第一个，第一个失败就整份文档失败——后面的通道白放着。
    """
    service = IngestService(
        bundle,
        router=ParserRouter([_FailingParser(), PlainTextParser()]),
        embedder=DeterministicEmbedder(dim=DIM),
    )
    outcome = service.submit(knowledge_base_id="kb_1", filename="a.md", content=MARKDOWN.encode())

    result = service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    parsed = bundle.meta.get_parse_result(outcome.document.id)
    assert parsed is not None
    assert parsed.parser_name == "PlainTextParser"
    # 降级原因要写进产物说明：界面上"这个文件是谁解析的"必须能解释为什么不是首选
    reason = parsed.probe_meta.get("route_reason", "")
    assert "FailingParser" in reason
    assert "云端额度用尽" in reason


def test_all_parsers_failing_reports_every_reason(bundle: StoreBundle, kb) -> None:  # type: ignore[no-untyped-def]
    """全挂时把**每个引擎为什么失败**都说出来。

    只回最后一条会误导：用户看到"PaddleOCR 超时"以为换个引擎就行，
    其实 MinerU 早就因为额度用尽失败了（那才是要处理的事）。
    """

    class _AlsoFailing(PlainTextParser):
        name = "AlsoFailingParser"

        def parse(self, **_kwargs):  # type: ignore[override]
            raise ParseError("接口抖动", stage="parsing")

    service = IngestService(
        bundle,
        router=ParserRouter([_FailingParser(), _AlsoFailing()]),
        embedder=DeterministicEmbedder(dim=DIM),
    )
    outcome = service.submit(knowledge_base_id="kb_1", filename="a.md", content=MARKDOWN.encode())

    with pytest.raises(IngestError) as excinfo:
        service.ingest(outcome.document.id)

    message = str(excinfo.value)
    assert "FailingParser" in message and "云端额度用尽" in message
    assert "AlsoFailingParser" in message and "接口抖动" in message


# ------------------------------------------------------- 分段出题（v23）


class _RecordingEmbedder(DeterministicEmbedder):
    """把"被拿去向量化的文本"记下来，用来断言用的是 index_text 而不是原文。"""

    def __init__(self, dim: int = DIM) -> None:
        super().__init__(dim=dim)
        self.seen: list[str] = []

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.seen.extend(texts)
        return super().embed(texts)


class _StubQuestions:
    """假的出题服务：按 chunk_id 给固定问题，并记录调用次数。"""

    def __init__(self, question: str = "这一段能回答什么？") -> None:
        self.question = question
        self.calls = 0
        self.last_count: int | None = None
        self.last_prompt: str | None = None
        self.last_model: str | None = None

    def generate_for_chunks(self, chunks, *, model_pk=None, count=3, prompt=""):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last_count = count
        self.last_prompt = prompt
        self.last_model = model_pk
        return {chunk.chunk_id: [f"{self.question}{index}"] for index, chunk in enumerate(chunks)}


def _service_with_questions(bundle, embedder, questions) -> IngestService:  # type: ignore[no-untyped-def]
    return IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
        questions=questions,
    )


def test_ingest_generates_questions_only_when_the_kb_turns_them_on(
    bundle: StoreBundle, kb, embedder: DeterministicEmbedder
) -> None:
    """开关关着时**一次模型调用都不发**，块也没有问题——这是默认状态。"""
    questions = _StubQuestions()
    service = _service_with_questions(bundle, embedder, questions)

    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())
    service.ingest(outcome.document.id)

    assert questions.calls == 0
    assert all(not chunk.questions for chunk in bundle.meta.iter_chunks(outcome.document.id))


def test_ingest_stores_questions_and_indexes_the_augmented_text(
    bundle: StoreBundle, kb, kb_service: KnowledgeBaseService, embedder: DeterministicEmbedder
) -> None:
    """开着时：问题落进 chunks，且**向量化用的是"原文 + 问题"**。

    后者是这个功能的全部意义——只存问题而不把它带进索引，召回一点都不会变好。
    """
    kb_service.set_suggested(kb.id, enabled=True, count=2, model_pk=None, prompt="")
    questions = _StubQuestions("这一段能回答什么？")
    recorder = _RecordingEmbedder()
    service = _service_with_questions(bundle, recorder, questions)

    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())
    service.ingest(outcome.document.id)

    chunks = list(bundle.meta.iter_chunks(outcome.document.id))
    assert questions.calls == 1
    assert questions.last_count == 2  # 用库上设置的"每段几条"
    assert all(chunk.questions for chunk in chunks)
    assert chunks[0].questions[0].startswith("这一段能回答什么？")
    # 向量化的文本 = index_text（含问题），而不是纯原文
    assert any("这一段能回答什么？" in text for text in recorder.seen)
    assert any(chunk.index_text in recorder.seen for chunk in chunks)
    # 原文那一列没被污染（引用预览读的是它）
    assert "这一段能回答什么？" not in chunks[0].text


def test_ingest_survives_a_failing_question_service(
    bundle: StoreBundle, kb, kb_service: KnowledgeBaseService, embedder: DeterministicEmbedder
) -> None:
    """出题整体挂掉也只能让文档"没有问题"，**绝不能把摄入打成 failed**。"""

    class _Boom:
        def generate_for_chunks(self, chunks, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("出题服务炸了")

    kb_service.set_suggested(kb.id, enabled=True, count=2, model_pk=None, prompt="")
    service = _service_with_questions(bundle, embedder, _Boom())

    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())
    result = service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert all(not chunk.questions for chunk in bundle.meta.iter_chunks(outcome.document.id))


def test_reprocess_actually_reruns_chunking_and_embedding(
    bundle: StoreBundle, kb, embedder: DeterministicEmbedder
) -> None:
    """**重新摄入对已 indexed 的文档必须真的重跑**（v23 修）。

    修之前：force 只允许入队、不重置阶段，`_resume_stage` 返回 indexed，
    三个 `_before(...)` 全 false → 整次摄入什么都不做。于是"打开分段出题后重新摄入"
    这个动作永远不会生效，用户会以为功能坏了。
    """
    service = _service_with_questions(bundle, embedder, _StubQuestions())
    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())
    service.ingest(outcome.document.id)
    before = list(bundle.meta.iter_chunks(outcome.document.id))
    assert before

    # 走用户那条路：文档服务入队 force → worker 消费
    documents = DocumentService(bundle)
    documents.enqueue_ingest(outcome.document.id, force=True)
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.CHUNKING

    service.ingest(outcome.document.id)

    after = list(bundle.meta.iter_chunks(outcome.document.id))
    assert after  # 块被重建了（不是空转）
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED


# --------------------------------------------------- 补出题（v24，用户显式触发）


def test_generate_questions_backfills_an_indexed_document(
    bundle: StoreBundle, kb, kb_service: KnowledgeBaseService, embedder: DeterministicEmbedder
) -> None:
    """对已索引的文档补出题：问题落库、两条索引都按新 index_text 重建，阶段不变。

    这是文档列表「生成问题」按钮干的事——老文档入库时功能还没开（或开关关着），
    需要一个"事后补上"的入口。它**不重新解析、不重新切块**，所以块号、正文、
    人工干预都原样保留。
    """
    kb_service.set_suggested(kb.id, enabled=False, count=2, model_pk="m1", prompt="按这个出")
    questions = _StubQuestions("青稞酒的酿造温度")
    recorder = _RecordingEmbedder()
    service = _service_with_questions(bundle, recorder, questions)

    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())
    service.ingest(outcome.document.id)
    # 开关关着 → 入库时一段题都没有，正是"老文档"的样子
    chunks = list(bundle.meta.iter_chunks(outcome.document.id))
    assert chunks and all(not chunk.questions for chunk in chunks)
    texts_before = [chunk.text for chunk in chunks]
    questions.calls = 0
    recorder.seen.clear()

    total = service.generate_questions(outcome.document.id)

    assert total == len(chunks)  # stub 每段给一条
    assert questions.calls == 1
    assert questions.last_count == 2  # 用库上配置的"每段几条"
    assert questions.last_model == "m1"
    assert questions.last_prompt == "按这个出"
    after = list(bundle.meta.iter_chunks(outcome.document.id))
    assert [chunk.text for chunk in after] == texts_before  # 正文没被动
    assert [chunk.chunk_id for chunk in after] == [c.chunk_id for c in chunks]  # 没重切
    assert all(chunk.questions for chunk in after)
    # 重新向量化用的是含问题的 index_text（否则"换个问法命中同一段"不会发生）
    assert any("青稞酒" in text for text in recorder.seen)
    assert all(chunk.index_text in recorder.seen for chunk in after)
    # 全文索引也重建了：问题里的词能搜到这一段
    hits = bundle.fulltext.search(query="青稞酒", top_k=5, kb_id=kb.id)
    assert hits and hits[0].chunk_id in {chunk.chunk_id for chunk in after}
    # 文档阶段不变——补出题不是摄入
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED
    # 列表用的统计跟着出结果
    stats = bundle.meta.question_stats_by_documents([outcome.document.id])
    assert stats[outcome.document.id] == (len(after), len(after))


def test_generate_questions_rejects_a_document_still_in_the_pipeline(
    bundle: StoreBundle, kb, embedder: DeterministicEmbedder
) -> None:
    """还没索引完就出题会被随后的重切覆盖，白花模型调用——直接拒绝。"""
    questions = _StubQuestions()
    service = _service_with_questions(bundle, embedder, questions)
    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())

    with pytest.raises(InvalidRequestError, match="已索引"):
        service.generate_questions(outcome.document.id)
    assert questions.calls == 0


def test_generate_questions_reports_when_nothing_was_generated(
    bundle: StoreBundle, kb, embedder: DeterministicEmbedder
) -> None:
    """一条题都没出出来时报错，而不是"成功但什么都没发生"（用户显式动作的诉求）。

    报错发生在写库之前，所以原有的问题不会被清掉。
    """

    class _Silent:
        def __init__(self) -> None:
            self.calls = 0

        def generate_for_chunks(self, chunks, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return {}

    silent = _Silent()
    service = _service_with_questions(bundle, embedder, silent)
    outcome = service.submit(knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode())
    service.ingest(outcome.document.id)
    before = list(bundle.meta.iter_chunks(outcome.document.id))

    with pytest.raises(InvalidRequestError, match="没有生成出任何问题"):
        service.generate_questions(outcome.document.id)
    after = list(bundle.meta.iter_chunks(outcome.document.id))
    assert [chunk.questions for chunk in after] == [chunk.questions for chunk in before]


def test_generate_questions_without_the_service_is_rejected(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """没接出题服务时给一句可读的拒绝，而不是 AttributeError。"""
    outcome = ingest_service.submit(
        knowledge_base_id=kb.id, filename="kb.md", content=MARKDOWN.encode()
    )
    ingest_service.ingest(outcome.document.id)

    with pytest.raises(InvalidRequestError, match="出题能力"):
        ingest_service.generate_questions(outcome.document.id)


# ------------------------------------------------- 媒体（图片 / 视频）真机入库（按需开）


#: 真机 WeMM 服务的地址。**不设就跳过**：这一条要一台局域网 GPU 服务，CI 上没有。
#: 例：``KYLAB_WEMM_E2E_URL=http://192.168.31.18:8234``（地址只在环境里，不进源码）。
WEMM_E2E_URL_ENV = "KYLAB_WEMM_E2E_URL"
#: 想换成一段真视频时给它的路径（不给就用下面生成的那张 8x8 PNG）。
#: 媒体这条路对图片与视频是**同一套三步协议**，所以两种都能验；视频那条真机数字
#: 见《开发计划》那条记录与接入文档。
WEMM_E2E_MEDIA_ENV = "KYLAB_WEMM_E2E_MEDIA"
#: 目标维度。默认 **1024**，不是原生 2048——原因是一条硬限制：
#: **pgvector 的 HNSW 索引最多 2000 维**（实测 2048 会在建分区时报
#: ``column cannot have more than 2000 dimensions for hnsw index``）。
#: 1024 正是接入文档列出的 Matryoshka 截断档位，于是"用 WeMM"这条路上
#: 维度 = 1024 是**能索引**的那个选择（2048 要么不用 HNSW、要么等存储层支持 halfvec，
#: 两件都需要更大的一次决定，见报告）。
WEMM_E2E_DIM_ENV = "KYLAB_WEMM_E2E_DIM"
WEMM_MODEL_ID = "WeMM-Embedding-2B-Q4_K_M.gguf"


def tiny_png() -> bytes:
    """生成一张 8x8 纯红 PNG（纯标准库）。

    为什么不往仓库放一份图片 fixture：几十 KB 的二进制进 git，只为一条**按需跑**的
    用例不值当；而这一侧要的只是"一段真能被解码器读出来的图片字节"。
    """
    width = height = 8
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.mark.skipif(
    not os.environ.get(WEMM_E2E_URL_ENV),
    reason=f"需要真机的 WeMM 多模态嵌入服务：设 {WEMM_E2E_URL_ENV} 后才会跑",
)
def test_media_document_is_indexed_with_the_media_vector(bundle: StoreBundle) -> None:
    """真机端到端：图片 / 视频 → 媒体接口的向量 → **落库** → 用同一份向量精确召回它。

    这条补的是"单元用例只到向量为止"的那一段（解析产物、分段、pgvector 分区、检索）。
    断言刻意只落在**机械事实上**，不押模型语义：

    1. 文档走到 ``indexed``、只有一段、段正文里如实写着"媒体向量索引"；
    2. 分区维度 = 目标维度（默认 1024）、库记录里冻结的也是它；
    3. 用**同一份媒体向量**去搜 → 距离 ≈ 0（证明库里存的就是它）；
    4. 用**这段文字**的文本向量去搜 → 距离明显大于 0（证明存的不是文本向量）。

    第 3、4 条合起来才是"向量真的来自媒体接口"的证据——只看"能搜到"是不够的，
    文本向量同样能搜到它自己。
    """
    from app.services.embedding.wemm import WeMMEmbedder

    base_url = os.environ[WEMM_E2E_URL_ENV]
    dim = int(os.environ.get(WEMM_E2E_DIM_ENV) or 1024)
    embedder = WeMMEmbedder(base_url=base_url, model_id=WEMM_MODEL_ID, dim=dim)
    kb = KnowledgeBaseService(bundle, embedder=embedder).create(kb_id="kb_wemm", name="WeMM 真机库")

    media_path = os.environ.get(WEMM_E2E_MEDIA_ENV, "")
    if media_path and pathlib.Path(media_path).is_file():
        content = pathlib.Path(media_path).read_bytes()
        filename = pathlib.Path(media_path).name
    else:
        content = tiny_png()
        filename = "一面红旗.png"

    service = IngestService(
        bundle,
        # 这一条验的是媒体那条路本身；"门控在什么条件下才挂它"由解析路由的单测钉
        router=ParserRouter([MediaDirectParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=512, overlap=0),
    )
    submitted = service.submit(knowledge_base_id=kb.id, filename=filename, content=content)
    result = service.ingest(submitted.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert result.document.error is None

    chunks = list(bundle.meta.iter_chunks(submitted.document.id))
    assert len(chunks) == 1
    assert "媒体向量索引" in chunks[0].text
    assert "媒体向量索引" in chunks[0].index_text

    assert bundle.vectors.declared_dim(kb.id) == dim
    assert bundle.meta.get_knowledge_base(kb.id).embedding_dim == dim

    media_vector = embedder.embed_media(content)
    hits = bundle.vectors.search(kb.id, query_vector=media_vector, top_k=1)
    assert hits[0].chunk_id == chunks[0].chunk_id
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6), "库里存的不是刚刚那份媒体向量"

    text_vector = embedder.embed([chunks[0].index_text])[0]
    other = bundle.vectors.search(kb.id, query_vector=text_vector, top_k=1)
    assert other[0].distance > 0.01, "存的像是文本向量——那说明媒体那条路根本没走到"


# --------------------------------------------------------------- 入库看门狗（P0）


def _document(bundle: StoreBundle, ingest_service: IngestService, *, name: str = "看门狗.md"):
    """造一份刚上传的文档（还没人跑它）。"""
    return ingest_service.submit(
        knowledge_base_id="kb_1", filename=name, content=(MARKDOWN + name).encode()
    ).document


def _watchdog_task(bundle: StoreBundle, document_id: str, *, round_: int, state=None):
    from app.models.enums import TaskKind, TaskState
    from app.storage.base import TaskRecord

    return bundle.meta.enqueue_task(
        TaskRecord(
            id=f"task_wd_{document_id}_{round_}",
            kind=TaskKind.PARSE,
            state=state or TaskState.SUCCEEDED,
            payload={"document_id": document_id, "watchdog_round": round_},
            document_id=document_id,
        )
    )


def test_watchdog_requeues_a_document_whose_task_is_gone(bundle: StoreBundle, kb,
                                                         ingest_service: IngestService) -> None:
    """卡在中间阶段、又**没有任何待领任务**的文档要被自动重排（P0，实测那 3/6 份）。

    复现方式：把一份文档推进到 `embedding` 之后**谁都不管它**（这正是实测现场：
    `stage=embedding` 停 15~30 分钟、`error` 为空、任务已经收场）。判据用的时间是
    `now`，所以这里把"现在"往后拨 5 分钟，不用真等。
    """
    from datetime import UTC, datetime, timedelta

    from app.models.enums import DocumentStage
    from app.services.ingest import sweep_stalled

    document = _document(bundle, ingest_service)
    bundle.meta.update_document_stage(document.id, DocumentStage.EMBEDDING)

    outcome = sweep_stalled(bundle, now=datetime.now(UTC) + timedelta(minutes=5))

    assert [item[0] for item in outcome.requeued] == [document.id]
    assert outcome.requeued[0][1] == "embedding"
    assert outcome.requeued[0][2] == 1, "第一次重排"
    assert not outcome.failed
    queued = bundle.meta.active_tasks_by_documents([document.id])
    assert queued[document.id].payload["watchdog_round"] == 1


def test_watchdog_keeps_its_hands_off_a_live_task(bundle: StoreBundle, kb,
                                                  ingest_service: IngestService) -> None:
    """**还在跑/还在排队**的文档一根汗毛都不许动（否则等于和正在干活的 worker 抢）。"""
    from datetime import UTC, datetime, timedelta

    from app.models.enums import DocumentStage, TaskKind, TaskState
    from app.services.ingest import sweep_stalled
    from app.storage.base import TaskRecord

    document = _document(bundle, ingest_service, name="在跑.md")
    bundle.meta.update_document_stage(document.id, DocumentStage.PARSING)
    bundle.meta.enqueue_task(
        TaskRecord(
            id="task_live_1",
            kind=TaskKind.PARSE,
            state=TaskState.RUNNING,
            payload={"document_id": document.id},
            document_id=document.id,
        )
    )

    outcome = sweep_stalled(bundle, now=datetime.now(UTC) + timedelta(hours=1))

    assert outcome.requeued == ()
    assert outcome.failed == ()


def test_watchdog_backs_off_before_the_next_round(bundle: StoreBundle, kb,
                                                  ingest_service: IngestService) -> None:
    """重排过一次之后，阈值翻倍：第 5 分钟不碰它，第 9 分钟才碰（退避，别把库刷爆）。"""
    from datetime import UTC, datetime, timedelta

    from app.models.enums import DocumentStage
    from app.services.ingest import sweep_stalled

    document = _document(bundle, ingest_service, name="退避.md")
    bundle.meta.update_document_stage(document.id, DocumentStage.EMBEDDING)
    _watchdog_task(bundle, document.id, round_=1)

    early = sweep_stalled(bundle, now=datetime.now(UTC) + timedelta(minutes=5))
    assert early.requeued == (), "阈值已翻倍到 8 分钟，5 分钟时不该再排"

    late = sweep_stalled(bundle, now=datetime.now(UTC) + timedelta(minutes=9))
    assert [item[2] for item in late.requeued] == [2], "第二轮"


def test_watchdog_gives_up_with_a_human_reason(bundle: StoreBundle, kb,
                                               ingest_service: IngestService) -> None:
    """重排到上限还不行 → **明确 failed 并写清原因**，不许继续无声无息（P0 的另一半）。"""
    from datetime import UTC, datetime, timedelta

    from app.models.enums import DocumentStage, TaskState
    from app.services.ingest import sweep_stalled

    document = _document(bundle, ingest_service, name="到头.md")
    bundle.meta.update_document_stage(document.id, DocumentStage.EMBEDDING)
    for round_ in (1, 2, 3):
        _watchdog_task(bundle, document.id, round_=round_, state=TaskState.FAILED)

    outcome = sweep_stalled(bundle, now=datetime.now(UTC) + timedelta(minutes=60))

    assert outcome.requeued == ()
    assert [item[0] for item in outcome.failed] == [document.id]
    refreshed = bundle.meta.get_document(document.id)
    assert refreshed.stage is DocumentStage.FAILED
    assert refreshed.error and "watchdog" in refreshed.error
    assert "自动重排 3 次" in refreshed.error
    assert "重新处理" in refreshed.error, "要给人一条出路，不能只说失败"


def test_watchdog_leaves_terminal_and_fresh_documents_alone(bundle: StoreBundle, kb,
                                                            ingest_service: IngestService) -> None:
    """终态（已索引/失败/用户取消）与**刚刚**进来的文档都不该被看门狗碰。

    另外钉住一个**有意的**行为：一份"刚上传、还没有任何任务"的文档，过了阈值之后
    **是要被重排的**——"上传成功了但任务根本没建出来"本身就是同一种泄漏
    （实测那 3 份素材就是这个形状：文档在，任务没了），不该因为"它看起来刚到"放过。
    """
    from datetime import UTC, datetime, timedelta

    from app.models.enums import DocumentStage
    from app.services.ingest import sweep_stalled

    indexed = _document(bundle, ingest_service, name="已完成.md")
    bundle.meta.update_document_stage(indexed.id, DocumentStage.INDEXED)
    fresh = _document(bundle, ingest_service, name="刚到.md")
    canceled = _document(bundle, ingest_service, name="取消了.md")
    bundle.meta.update_document_stage(canceled.id, DocumentStage.CANCELED)

    now = datetime.now(UTC)
    outcome_now = sweep_stalled(bundle, now=now)
    assert outcome_now.requeued == (), "刚进来的还不该被碰"
    assert outcome_now.failed == ()

    later = sweep_stalled(bundle, now=now + timedelta(hours=2))
    assert [item[0] for item in later.requeued] == [fresh.id], "只碰那个非终态、没人管的"
    assert later.failed == ()


def test_searchable_follows_the_artifacts_not_the_stage(bundle: StoreBundle, kb,
                                                        ingest_service: IngestService) -> None:
    """`searchable` 按**产物**算：卡在 `embedding` 但切块已落库的文档**查得到**（P1）。

    实测（2026-09-29 第 6 条）：xlsx 卡在 `embedding` 时已经能被检索到，而模型从
    `get_document_status` 读到 `searchable=false`（旧口径 = `stage == indexed`），
    于是它根本不去搜。这条把口径钉在"切块在不在"上，并覆盖停用与空文档两个反面。
    """
    from app.models.enums import DocumentStage
    from app.services.documents import DocumentService

    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="卡住.md",
                                    content=(MARKDOWN + "卡住").encode())
    ingest_service.ingest(outcome.document.id)  # 走完一遍：切块 + 全文 + 向量都落库
    bundle.meta.update_document_stage(outcome.document.id, DocumentStage.EMBEDDING)

    record = bundle.meta.get_document(outcome.document.id)
    chunks = bundle.meta.count_chunks(record.id)
    assert chunks > 0
    assert DocumentService.is_searchable(record, chunks=chunks) is True
    # 它真的查得到（不是"我们以为它查得到"）
    hits = bundle.fulltext.search(query="混合召回", top_k=5, kb_id="kb_1")
    assert hits and hits[0].document_id == record.id

    fresh = _document(bundle, ingest_service, name="空.md")
    assert DocumentService.is_searchable(fresh, chunks=bundle.meta.count_chunks(fresh.id)) is False

    bundle.meta.set_document_disabled(record.id, True)
    disabled = bundle.meta.get_document(record.id)
    assert DocumentService.is_searchable(disabled, chunks=chunks) is False


def test_a_video_that_every_channel_rejects_says_what_to_configure(bundle: StoreBundle, kb) -> None:  # type: ignore[no-untyped-def]
    """视频被云端拒收、又没配多模态时，错误里要带**去哪一栏配置**（方案 13）。

    真现场：一份自造 `.webm`（浏览器给的 mime 是 `application/octet-stream`，探测结论
    落到 `scanned`）被 MinerU 拒收（-60002），而那一刻"媒体直通"不在候选里
    （当时嵌入协议还不支持媒体）→ 候选只剩一个 → 用户看到的只有
    "MinerU 不支持该文件格式"，手上那份视频其实**配一下就能进库**。
    这里钉住那句下一步，同时确认**原始原因不被吞掉**。
    """
    class _MinerUish(PlainTextParser):
        """像 MinerU 那样：**先认下**再拒（探测结论 `scanned` 的它都收）。"""

        name = "MinerUFake"

        def supports(self, **_kwargs) -> bool:  # type: ignore[override]
            return True

        def parse(self, **_kwargs):  # type: ignore[override]
            raise ParseError(
                "MinerU 申请上传地址失败（-60002）：MinerU 不支持该文件格式", stage="parsing"
            )

    service = IngestService(
        bundle,
        router=ParserRouter([_MinerUish()]),
        embedder=DeterministicEmbedder(dim=DIM),
    )
    outcome = service.submit(
        knowledge_base_id="kb_1",
        filename="录屏.webm",
        content=b"\x1aE\xdf\xa3" + b"webm-ish bytes",
        mime_type="application/octet-stream",
    )

    with pytest.raises(IngestError) as excinfo:
        service.ingest(outcome.document.id)

    message = str(excinfo.value)
    assert "-60002" in message, "原始原因不能丢"
    assert "视频只能走多模态嵌入" in message, "要告诉用户下一步去哪一栏"
    assert "设置" in message
    failed = bundle.meta.get_document(outcome.document.id)
    assert failed.stage is DocumentStage.FAILED
    assert failed.error and "多模态" in failed.error
