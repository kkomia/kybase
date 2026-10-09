"""批量动作里"整库"那一档（v17）。

`all=True` 是为了"改了切分参数要整库重跑"——由服务端解析全集，界面不必先取一遍
文档 id 再回传（那个列表接口一次回全量，本身就是瓶颈）。这里钉住两件事：
选谁、以及什么情况下该拒绝。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.models.enums import DocumentStage
from app.parsers.plain_text import PlainTextParser
from app.services.batch import BATCH_ALL_LIMIT, DocumentBatchService
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.storage.base import StoreBundle

DIM = 16


class _RecordingDocuments:
    """只关心"有没有被排进队列"。真跑摄入在 test_ingest 里覆盖。"""

    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.questions: list[str] = []

    def enqueue_ingest(self, document_id: str, *, force: bool = False) -> None:
        self.enqueued.append(document_id)

    def enqueue_questions(self, document_id: str) -> None:
        self.questions.append(document_id)


@pytest.fixture
def kb(bundle: StoreBundle) -> str:
    """先有库才能提交文档：摄入第一步就是查库拿嵌入模型。"""
    KnowledgeBaseService(bundle, embedder=DeterministicEmbedder(dim=DIM)).create(
        kb_id="kb_1", name="批处理测试库"
    )
    return "kb_1"


@pytest.fixture
def ingest_service(bundle: StoreBundle) -> IngestService:
    return IngestService(
        bundle, router=ParserRouter([PlainTextParser()]), embedder=DeterministicEmbedder(dim=DIM)
    )


@pytest.fixture
def seeded(bundle: StoreBundle, ingest_service: IngestService, kb: str) -> tuple[str, str, str]:
    """一个库 + 三篇文档：一篇已索引、一篇失败、一篇还在跑。"""

    def submit(name: str) -> str:
        outcome = ingest_service.submit(
            knowledge_base_id="kb_1", filename=name, content=f"# {name}\n\n正文。\n".encode()
        )
        return outcome.document.id

    indexed, failed, running = submit("已索引.md"), submit("失败.md"), submit("还在跑.md")
    bundle.meta.update_document_stage(indexed, DocumentStage.INDEXED)
    bundle.meta.update_document_stage(failed, DocumentStage.FAILED, error="模拟失败")
    return indexed, failed, running


def _service(bundle: StoreBundle, documents) -> DocumentBatchService:  # type: ignore[no-untyped-def]
    return DocumentBatchService(bundle, documents, lifecycle=None, folders=None)  # type: ignore[arg-type]


def test_all_skips_documents_still_in_the_pipeline(
    bundle: StoreBundle, ingest_service: IngestService, seeded
) -> None:
    """正在跑的文档不重复排队——同一篇被解析两遍是要花钱的。"""
    indexed, failed, running = seeded
    documents = _RecordingDocuments()

    items = _service(bundle, documents).run("kb_1", "reprocess", [], all_documents=True)

    # 顺序由 list_documents 决定（按名称/时间），这里只关心"选了哪些"
    assert sorted(item.document_id for item in items) == sorted([indexed, failed])
    assert running not in documents.enqueued
    assert sorted(documents.enqueued) == sorted([indexed, failed])


def test_all_on_empty_kb_says_there_are_no_documents(bundle: StoreBundle) -> None:
    with pytest.raises(InvalidRequestError, match="还没有文档"):
        _service(bundle, _RecordingDocuments()).run("kb_1", "reprocess", [], all_documents=True)


def test_all_when_everything_is_running_says_so(
    bundle: StoreBundle, ingest_service: IngestService, kb: str
) -> None:
    """有文档但全在跑，与"空库"是两件事——文案分开，否则用户以为文档丢了。"""
    ingest_service.submit(knowledge_base_id="kb_1", filename="a.md", content=b"# a\n\nx\n")
    with pytest.raises(InvalidRequestError, match="处理中"):
        _service(bundle, _RecordingDocuments()).run("kb_1", "reprocess", [], all_documents=True)


def test_all_refuses_above_the_limit(
    bundle: StoreBundle, ingest_service: IngestService, kb: str
) -> None:
    """超过上限就拒绝：一次点击排出上万个云端解析任务，比"稍等分批"更糟。"""
    for index in range(3):
        ingest_service.submit(
            knowledge_base_id="kb_1", filename=f"d{index}.md", content=f"# {index}\n\nx\n".encode()
        )
    for record in bundle.meta.list_documents("kb_1"):
        bundle.meta.update_document_stage(record.id, DocumentStage.INDEXED)

    service = _service(bundle, _RecordingDocuments())
    import app.services.batch as batch_module

    original = batch_module.BATCH_ALL_LIMIT
    try:
        batch_module.BATCH_ALL_LIMIT = 2
        with pytest.raises(InvalidRequestError, match="一次最多处理 2 篇"):
            service.run("kb_1", "reprocess", [], all_documents=True)
    finally:
        batch_module.BATCH_ALL_LIMIT = original
    assert BATCH_ALL_LIMIT == 2000


def test_ids_are_still_supported(
    bundle: StoreBundle, ingest_service: IngestService, seeded
) -> None:
    """显式给 id 的老路径不受影响。"""
    _indexed, _failed, running = seeded
    documents = _RecordingDocuments()

    items = _service(bundle, documents).run("kb_1", "reprocess", [running])

    assert [item.document_id for item in items] == [running]
    assert documents.enqueued == [running]


def test_foreign_document_is_reported_not_executed(
    bundle: StoreBundle, ingest_service: IngestService
) -> None:
    """不属于这个库的 id 记成失败，不进入动作分支（越权防护）。"""
    documents = _RecordingDocuments()

    items = _service(bundle, documents).run("kb_missing", "reprocess", ["doc_none"])

    assert items[0].ok is False
    assert items[0].error is not None
    assert documents.enqueued == []


def test_questions_action_queues_only_indexed_documents(
    bundle: StoreBundle, ingest_service: IngestService, seeded
) -> None:
    """「生成问题」只认已索引的文档：别的阶段要么没切块、要么正被重写。

    用真的 ``DocumentService`` 而不是记录器：阶段这条规则就在它身上，
    假掉的话这个用例就只剩"函数被调到了"，等于没测。
    """
    from app.models.enums import TaskKind, TaskState
    from app.services.documents import DocumentService

    indexed, failed, running = seeded
    items = _service(bundle, DocumentService(bundle)).run(
        "kb_1", "questions", [indexed, failed, running]
    )
    by_id = {item.document_id: item for item in items}

    assert by_id[indexed].ok is True
    assert by_id[failed].ok is False and "已索引" in (by_id[failed].error or "")
    assert by_id[running].ok is False
    queued = [task for task in bundle.meta.list_tasks(TaskState.PENDING)
              if task.kind is TaskKind.QUESTIONS]
    assert [task.document_id for task in queued] == [indexed]
    # 出题不重置阶段：文档仍是 indexed（重新摄入那条路才会推回 CHUNKING）
    assert bundle.meta.get_document(indexed).stage is DocumentStage.INDEXED
