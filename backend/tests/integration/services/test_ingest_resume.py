"""补覆盖：ingest 的续跑分支、防御性错误路径与知识库服务（M2 收尾）。"""

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.models.enums import DataSourceKind, DocumentStage
from app.parsers.base import ParseError, ParseResult
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.documents import DocumentService
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestError, IngestService, _before
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.storage.base import DocumentPartRecord, DocumentRecord, StoreBundle

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


# --------------------------------------------------------------- 强制重新解析


class _CountingParser(PlainTextParser):
    """记着"被调用了几次"的直通解析器，并把次数写进产物。

    计数比"有没有换过 markdown"更直接：只要解析器没被再调一次，次数就不动。
    次数写进 markdown 是为了顺带证明**产物真的被重写了**（覆写同一份
    ``markdown/{id}.md``），而不只是"阶段来回走了一趟"。
    """

    name = "CountingParser"

    def __init__(self) -> None:
        self.calls = 0

    def parse(self, *, content, filename, mime_type=None, probe=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        inner = super().parse(content=content, filename=filename, mime_type=mime_type, probe=probe)
        return ParseResult(
            markdown=f"{inner.markdown}\n第 {self.calls} 次解析。\n",
            parser_name=self.name,
            page_count=inner.page_count,
            probe=probe,
        )


@pytest.fixture
def counting_parser() -> _CountingParser:
    return _CountingParser()


@pytest.fixture
def counting_service(
    bundle: StoreBundle, embedder: DeterministicEmbedder, counting_parser: _CountingParser
) -> IngestService:
    return IngestService(
        bundle,
        router=ParserRouter([counting_parser]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )


def _parse_markdown(bundle: StoreBundle, document_id: str) -> str:
    parsed = bundle.meta.get_parse_result(document_id)
    assert parsed is not None
    return bundle.objects.read(parsed.markdown_path).decode("utf-8")


def test_reparse_really_calls_the_parser_again(
    bundle: StoreBundle, counting_service: IngestService, counting_parser: _CountingParser, kb
) -> None:
    """**重新解析必须真的重调解析器**——这正是它与「重新摄入」的分界。

    修之前那条路（force）只把阶段推回 CHUNKING，`_reuse_parse_result` 看到
    `parse_results` 还在就直接复用——换了引擎再点一次，解析结果一个字节都不会变。
    """
    outcome = counting_service.submit(
        knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode()
    )
    document_id = outcome.document.id
    counting_service.ingest(document_id)
    assert counting_parser.calls == 1
    assert "第 1 次解析" in _parse_markdown(bundle, document_id)

    documents = DocumentService(bundle, ingest=counting_service)
    documents.enqueue_ingest(document_id, restart=True)
    # 复位落在入队之前：worker 只看阶段，阶段没退回去就只是一次普通续跑
    assert bundle.meta.get_document(document_id).stage is DocumentStage.UPLOADED

    counting_service.ingest(document_id)

    assert counting_parser.calls == 2, "重新解析没有重调解析器"
    assert "第 2 次解析" in _parse_markdown(bundle, document_id)


def test_reparse_clears_every_old_product(
    bundle: StoreBundle, counting_service: IngestService, kb
) -> None:
    """旧产物必须**清干净**，否则重新索引完成之前检索会同时命中新旧两版。

    清五处：向量（检索）、全文索引（检索）、chunks（抽屉与扩读读它）、
    解析产物（留着它会跳过解析）、子文件与 is_split（段数可能变少）。
    **原文不能删**：key 是内容 hash 寻址、跨库共享，删它等于弄坏别人的文档。
    """
    outcome = counting_service.submit(
        knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode()
    )
    document_id = outcome.document.id
    counting_service.ingest(document_id)

    # 纯文本不会产生子文件树，这两样手工造出来（它们同样是"旧产物"）
    bundle.meta.create_document_parts(
        [
            DocumentPartRecord(
                id=f"{document_id}#part000",
                document_id=document_id,
                part_index=0,
                page_start=1,
                page_end=10,
                stage=DocumentStage.PARSING,
            )
        ]
    )
    bundle.meta.mark_document_split(document_id)
    bundle.meta.update_document_summary(document_id, "旧正文的概括")
    original_path = bundle.meta.get_setting(f"document.{document_id}.original_path")
    query_vector = DeterministicEmbedder(dim=DIM).embed(["标题"])[0]

    # 前置：这些产物现在真的都在（否则下面的断言等于在测空气）
    assert bundle.meta.count_chunks(document_id) > 0
    assert bundle.fulltext.search(query="正文", top_k=5, kb_id=kb.id)
    assert bundle.vectors.search(kb.id, query_vector=query_vector, top_k=5)
    assert bundle.meta.get_parse_result(document_id) is not None
    assert bundle.meta.list_document_parts(document_id)
    assert bundle.meta.get_document(document_id).is_split is True

    counting_service.reset_for_reparse(document_id)

    assert bundle.meta.count_chunks(document_id) == 0
    assert list(bundle.meta.iter_chunks(document_id)) == []
    assert bundle.fulltext.search(query="正文", top_k=5, kb_id=kb.id) == []
    assert bundle.vectors.search(kb.id, query_vector=query_vector, top_k=5) == []
    assert bundle.meta.get_parse_result(document_id) is None
    assert bundle.meta.list_document_parts(document_id) == []
    reset = bundle.meta.get_document(document_id)
    assert reset.is_split is False
    assert reset.summary == ""
    assert reset.stage is DocumentStage.UPLOADED
    assert reset.error is None
    # 原文原封不动：接下来的解析读的就是它
    assert bundle.meta.get_setting(f"document.{document_id}.original_path") == original_path
    assert bundle.objects.read(original_path) == CONTENT.encode()


def test_reparse_then_ingest_reaches_indexed_and_is_searchable_again(
    bundle: StoreBundle, counting_service: IngestService, kb
) -> None:
    """重解析之后整条链路要能再走一遍，并且重新可检索——阶段回到 uploaded 只是开始。"""
    outcome = counting_service.submit(
        knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode()
    )
    document_id = outcome.document.id
    counting_service.ingest(document_id)

    DocumentService(bundle, ingest=counting_service).enqueue_ingest(document_id, restart=True)
    result = counting_service.ingest(document_id)

    assert result.document.stage is DocumentStage.INDEXED
    assert result.chunk_count > 0
    assert bundle.meta.get_parse_result(document_id) is not None
    assert bundle.vectors.search(
        kb.id, query_vector=DeterministicEmbedder(dim=DIM).embed(["标题"])[0], top_k=5
    )
    assert bundle.fulltext.search(query="正文", top_k=5, kb_id=kb.id)


def test_reparse_refuses_while_a_task_is_still_queued(
    bundle: StoreBundle, counting_service: IngestService, kb
) -> None:
    """有任务在跑时必须拒绝，**不能返回既有任务**：复位会清产物，静默返回就是撒谎。"""
    outcome = counting_service.submit(
        knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode()
    )
    document_id = outcome.document.id
    documents = DocumentService(bundle, ingest=counting_service)
    documents.enqueue_ingest(document_id)  # 排队中的摄入任务（测试里没有 worker 领它）

    with pytest.raises(ConflictError, match="处理中"):
        documents.enqueue_ingest(document_id, restart=True)


def test_reparse_is_refused_from_a_stage_that_is_still_in_flight(
    bundle: StoreBundle, counting_service: IngestService, kb
) -> None:
    """跑到一半的阶段不适用「重新解析」（那由「重新处理」负责从切分续跑）。"""
    outcome = counting_service.submit(
        knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode()
    )
    document_id = outcome.document.id
    counting_service._probe_and_parse(bundle.meta.get_document(document_id))
    assert bundle.meta.get_document(document_id).stage is DocumentStage.PARSED

    with pytest.raises(ConflictError, match="只有处理已结束"):
        counting_service.reset_for_reparse(document_id)
