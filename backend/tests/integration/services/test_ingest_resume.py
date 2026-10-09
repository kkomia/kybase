"""补覆盖：ingest 的续跑分支、防御性错误路径与知识库服务（M2 收尾）。"""

import pytest

from app.core.exceptions import NotFoundError
from app.models.enums import DataSourceKind, DocumentStage
from app.parsers.base import ParseError
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestError, IngestService, _before
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.storage.base import DocumentRecord, StoreBundle

DIM = 16
CONTENT = "# 标题\n\n一段足够长的正文，用于产生至少一个 chunk。\n"


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=DIM)


@pytest.fixture
def ingest_service(bundle: StoreBundle, embedder: DeterministicEmbedder) -> IngestService:
    return IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )


@pytest.fixture
def kb(bundle: StoreBundle, embedder: DeterministicEmbedder):
    return KnowledgeBaseService(bundle, embedder=embedder).create(kb_id="kb_1", name="库")


# --------------------------------------------------------------------- 续跑：只差向量化


def test_resume_from_chunked_reuses_existing_chunks(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """切分已完成、向量化前挂掉：续跑应直接复用已有 chunk，不重新解析也不重新切分。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="a.md",
                                    content=CONTENT.encode())
    document_id = outcome.document.id

    ingest_service._probe_and_parse(bundle.meta.get_document(document_id))
    ingest_service._chunk(bundle.meta.get_document(document_id), kb, None)
    bundle.meta.update_document_stage(document_id, DocumentStage.FAILED, error="模拟向量化失败")

    result = ingest_service.ingest(document_id)

    assert result.document.stage is DocumentStage.INDEXED
    assert result.chunk_count > 0
    assert bundle.vectors.declared_dim("kb_1") == DIM


def test_resume_from_scratch_when_no_artifacts(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """失败态但什么产物都没有：从头再跑一遍。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="a.md",
                                    content=CONTENT.encode())
    bundle.meta.update_document_stage(outcome.document.id, DocumentStage.FAILED, error="启动即崩")

    result = ingest_service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert bundle.meta.get_document(outcome.document.id).error is None


# --------------------------------------------------------------------- 空内容与防御路径


def test_markdown_without_chunks_still_reaches_indexed(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """只有空白的 Markdown 切不出 chunk，但流程要正常收尾而不是卡在 embedding。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="blank.md",
                                    content=b"   \n\n   \n")
    result = ingest_service.ingest(outcome.document.id)

    assert result.document.stage is DocumentStage.INDEXED
    assert result.chunk_count == 0
    assert bundle.vectors.declared_dim("kb_1") is None  # 没内容就不必建分区


def test_ingest_unknown_document_is_reported(ingest_service: IngestService) -> None:
    with pytest.raises(NotFoundError, match="文档不存在"):
        ingest_service.ingest("doc_missing")


def test_document_without_original_path_reports_probing_stage(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """绕过 submit 直接建记录（没有原文）时，错误要归到探测阶段。"""
    bundle.meta.create_document(
        DocumentRecord(id="doc_orphan", knowledge_base_id="kb_1", name="a.md",
                       source_kind=DataSourceKind.UPLOAD, content_hash="h-orphan",
                       stage=DocumentStage.UPLOADED)
    )

    with pytest.raises(Exception) as excinfo:
        ingest_service.ingest("doc_orphan")
    assert "找不到原文路径" in str(excinfo.value)


def test_read_markdown_without_artifact_raises_parse_error(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """防御路径：产物在两步之间消失时也要给出可读错误，而不是 AttributeError。"""
    bundle.meta.create_document(
        DocumentRecord(id="doc_x", knowledge_base_id="kb_1", name="a.md",
                       source_kind=DataSourceKind.UPLOAD, content_hash="h-x",
                       stage=DocumentStage.PARSED)
    )
    with pytest.raises(ParseError, match="找不到解析产物"):
        ingest_service._read_markdown(bundle.meta.get_document("doc_x"))


def test_failed_document_stays_failed_after_repeat_failure(
    bundle: StoreBundle, ingest_service: IngestService, kb
) -> None:
    """连续失败只记一次状态迁移，不应因重复更新而报错。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="bad.pdf",
                                    content=b"\x00\x01\x02")
    for _ in range(2):
        with pytest.raises(Exception, match=r"暂不支持|找不到"):
            ingest_service.ingest(outcome.document.id)

    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.FAILED


def test_before_helper_handles_stages_outside_main_chain() -> None:
    """增强分支与失败态不在主链路上，一律视为"没有更早"，避免误判要跳过哪些步骤。"""
    assert _before(DocumentStage.ENRICHING, DocumentStage.INDEXED) is False
    assert _before(DocumentStage.FAILED, DocumentStage.CHUNKED) is False


# --------------------------------------------------------------------- 知识库服务


def test_kb_service_lists_all(bundle: StoreBundle, embedder: DeterministicEmbedder) -> None:
    service = KnowledgeBaseService(bundle, embedder=embedder)
    service.create(kb_id="kb_a", name="甲库")
    service.create(kb_id="kb_b", name="乙库")

    assert {item.id for item in service.list_all()} == {"kb_a", "kb_b"}


def test_kb_service_returns_existing_record(
    bundle: StoreBundle, embedder: DeterministicEmbedder
) -> None:
    service = KnowledgeBaseService(bundle, embedder=embedder)
    service.create(kb_id="kb_a", name="甲库")

    assert service.get("kb_a").name == "甲库"


# --------------------------------------------------------------------- 嵌入器可读性


def test_embedder_repr_is_informative() -> None:
    """排障时要能从日志里看出用的是哪个模型、多少维。"""
    text = repr(DeterministicEmbedder(dim=8))
    assert "DeterministicEmbedder" in text
    assert "dev/deterministic-hash" in text
    assert "dim=8" in text


def test_ingest_error_carries_document_and_stage(ingest_service: IngestService, kb) -> None:
    """worker 要靠这两个字段决定重试策略。"""
    outcome = ingest_service.submit(knowledge_base_id="kb_1", filename="empty.md", content=b"")
    with pytest.raises(IngestError) as excinfo:
        ingest_service.ingest(outcome.document.id)

    error = excinfo.value
    assert getattr(error, "document_id", None) == outcome.document.id
    assert getattr(error, "stage", None) == "parsing"
