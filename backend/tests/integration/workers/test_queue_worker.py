"""任务消费者的集成测试（M2 调度侧）。

守住架构 §4 的三条承诺：失败可重试（指数退避）、超过上限判终态、进程崩溃后超时回收续跑。
"""

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.models.enums import DocumentStage, TaskKind, TaskState
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.documents import DocumentService
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.storage.base import StoreBundle, TaskRecord
from app.workers.queue_worker import TaskWorker, is_retryable

DIM = 16
CONTENT = "# 标题\n\n用于验证任务消费者的正文内容。\n"


@pytest.fixture
def embedder() -> DeterministicEmbedder:
    return DeterministicEmbedder(dim=DIM)


@pytest.fixture
def ingest(bundle: StoreBundle, embedder: DeterministicEmbedder) -> IngestService:
    return IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
    )


@pytest.fixture
def worker(bundle: StoreBundle, ingest: IngestService) -> TaskWorker:
    return TaskWorker(bundle, ingest, owner="worker-test", poll_interval=0.01)


@pytest.fixture
def kb(bundle: StoreBundle, embedder: DeterministicEmbedder):
    return KnowledgeBaseService(bundle, embedder=embedder).create(kb_id="kb_1", name="库")


def _enqueue(bundle: StoreBundle, document_id: str, **kwargs) -> TaskRecord:
    params = {"kind": TaskKind.PARSE, "state": TaskState.PENDING, "document_id": document_id}
    params.update(kwargs)
    return bundle.meta.enqueue_task(TaskRecord(id=params.pop("id", "task_1"), **params))


# --------------------------------------------------------------------- 正常路径


@pytest.mark.asyncio
async def test_run_once_returns_false_when_queue_empty(worker: TaskWorker) -> None:
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_worker_indexes_document(bundle: StoreBundle, ingest: IngestService,
                                       worker: TaskWorker, kb) -> None:
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id)

    assert await worker.run_once() is True

    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED
    tasks = bundle.meta.list_tasks(TaskState.SUCCEEDED)
    assert [task.id for task in tasks] == ["task_1"]
    assert tasks[0].lease_owner is None  # 完成后释放租约


@pytest.mark.asyncio
async def test_second_run_once_finds_nothing(worker: TaskWorker, bundle: StoreBundle,
                                             ingest: IngestService, kb) -> None:
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id)

    assert await worker.run_once() is True
    assert await worker.run_once() is False


# --------------------------------------------------------------------- 失败与重试


@pytest.mark.asyncio
async def test_failure_reschedules_with_backoff(bundle: StoreBundle, ingest: IngestService,
                                                worker: TaskWorker, kb) -> None:
    """架构 §4：失败要按指数退避重试，而不是立刻判死。"""
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="scan.pdf",
                            content=b"\x89PNG\r\n\x1a\n\x00")
    _enqueue(bundle, outcome.document.id)

    await worker.run_once()

    pending = bundle.meta.list_tasks(TaskState.PENDING)
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert pending[0].next_run_at is not None
    assert pending[0].next_run_at > datetime.now(UTC)
    assert "暂不支持" in (pending[0].error or "")


@pytest.mark.asyncio
async def test_rescheduled_task_is_not_claimable_before_its_time(
    bundle: StoreBundle, ingest: IngestService, worker: TaskWorker, kb
) -> None:
    """退避期间任务不该被领走，否则"退避"就是摆设。"""
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="scan.pdf",
                            content=b"\x89PNG\r\n\x1a\n\x00")
    _enqueue(bundle, outcome.document.id)
    await worker.run_once()

    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_task_fails_after_exhausting_attempts(bundle: StoreBundle, ingest: IngestService,
                                                    worker: TaskWorker, kb) -> None:
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="scan.pdf",
                            content=b"\x89PNG\r\n\x1a\n\x00")
    _enqueue(bundle, outcome.document.id, attempts=5, max_attempts=5)

    await worker.run_once()

    failed = bundle.meta.list_tasks(TaskState.FAILED)
    assert [task.id for task in failed] == ["task_1"]


@pytest.mark.asyncio
async def test_unwired_task_kind_fails_without_retrying(bundle: StoreBundle,
                                                        worker: TaskWorker, kb) -> None:
    """类型没接线的任务重试一千次也不会成功，直接判失败。"""
    # 不挂文档：kind 检查先于 document_id 检查，这样能干净地命中"未接线"分支
    _enqueue(bundle, None, kind=TaskKind.FETCH_SOURCE)

    await worker.run_once()

    failed = bundle.meta.list_tasks(TaskState.FAILED)
    assert failed and "尚未接线" in (failed[0].error or "")
    assert bundle.meta.list_tasks(TaskState.PENDING) == []


@pytest.mark.asyncio
async def test_task_without_document_fails_immediately(bundle: StoreBundle,
                                                       worker: TaskWorker) -> None:
    bundle.meta.enqueue_task(TaskRecord(id="task_bad", kind=TaskKind.PARSE,
                                        state=TaskState.PENDING, document_id=None))
    await worker.run_once()

    failed = bundle.meta.list_tasks(TaskState.FAILED)
    assert failed and "document_id" in (failed[0].error or "")


def test_backoff_grows_exponentially_and_is_capped(worker: TaskWorker) -> None:
    assert worker.backoff_for(1) == 2.0
    assert worker.backoff_for(2) == 4.0
    assert worker.backoff_for(3) == 8.0
    assert worker.backoff_for(99) == 60.0  # 上限


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("参数非法"), False),
        (NotImplementedError("未接线"), False),
        (RuntimeError("网络抖动"), True),
    ],
)
def test_retryability_classification(exc: Exception, expected: bool) -> None:
    assert is_retryable(exc) is expected


# --------------------------------------------------------------------- 崩溃恢复


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_reprocessed(bundle: StoreBundle,
                                                          ingest: IngestService,
                                                          worker: TaskWorker, kb) -> None:
    """进程崩溃后：租约过期 → 任务回到队列 → 重新执行完成（架构 §4 断点续跑）。"""
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id)

    crashed = TaskWorker(bundle, ingest, owner="worker-crashed", lease_seconds=1)
    claimed = bundle.meta.claim_task(owner=crashed.owner, lease_seconds=1)
    assert claimed is not None

    reclaimed = bundle.meta.reclaim_expired_tasks(
        now=datetime.now(UTC) + timedelta(minutes=5)
    )
    assert reclaimed == 1

    assert await worker.run_once() is True
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED


@pytest.mark.asyncio
async def test_run_forever_stops_on_event(worker: TaskWorker) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert task.done() and task.exception() is None


@pytest.mark.asyncio
async def test_run_forever_keeps_going_after_a_failing_task(bundle: StoreBundle,
                                                            ingest: IngestService,
                                                            worker: TaskWorker, kb) -> None:
    """单次任务失败不能带走整个循环。"""
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="scan.pdf",
                            content=b"\x89PNG\r\n\x1a\n\x00")
    _enqueue(bundle, outcome.document.id)

    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop=stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert bundle.meta.list_tasks(TaskState.PENDING)  # 已按退避排回队列


def test_worker_rejects_bad_lease(bundle: StoreBundle, ingest: IngestService) -> None:
    with pytest.raises(ValueError):
        TaskWorker(bundle, ingest, owner="w", lease_seconds=0)


# --------------------------------------------------------------------- 文档服务


def test_document_service_enqueue_is_idempotent(bundle: StoreBundle, ingest: IngestService,
                                                embedder: DeterministicEmbedder, kb) -> None:
    """用户连点几次上传，队列里不该堆出重复任务。"""
    service = DocumentService(bundle)
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())

    first = service.enqueue_ingest(outcome.document.id)
    second = service.enqueue_ingest(outcome.document.id)

    assert first.id == second.id
    assert len(bundle.meta.list_tasks()) == 1


def test_document_service_refuses_reindex_without_force(bundle: StoreBundle, kb) -> None:
    from app.core.exceptions import ConflictError
    from app.models.enums import DataSourceKind
    from app.storage.base import DocumentRecord

    service = DocumentService(bundle)
    bundle.meta.create_document(
        DocumentRecord(id="doc_done", knowledge_base_id="kb_1", name="a.md",
                       source_kind=DataSourceKind.UPLOAD, content_hash="h",
                       stage=DocumentStage.INDEXED)
    )

    with pytest.raises(ConflictError):
        service.enqueue_ingest("doc_done")
    assert service.enqueue_ingest("doc_done", force=True) is not None


def test_document_service_reports_missing(bundle: StoreBundle) -> None:
    from app.core.exceptions import NotFoundError

    service = DocumentService(bundle)
    with pytest.raises(NotFoundError):
        service.get("doc_missing")
    with pytest.raises(NotFoundError):
        service.list_documents("kb_missing")
    with pytest.raises(NotFoundError):
        service.get_task("task_missing")


def test_document_service_lists_tasks_and_parts(bundle: StoreBundle, ingest: IngestService,
                                                kb) -> None:
    from app.models.enums import DataSourceKind
    from app.storage.base import DocumentPartRecord, DocumentRecord

    service = DocumentService(bundle)
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    service.enqueue_ingest(outcome.document.id)

    bundle.meta.create_document(
        DocumentRecord(id="doc_big", knowledge_base_id="kb_1", name="big.pdf",
                       source_kind=DataSourceKind.UPLOAD, content_hash="h2",
                       stage=DocumentStage.UPLOADED, is_split=True)
    )
    bundle.meta.create_document_parts([
        DocumentPartRecord(id="part_1", document_id="doc_big", part_index=1,
                           page_start=1, page_end=200, stage=DocumentStage.UPLOADED)
    ])

    assert len(service.list_tasks()) == 1
    assert service.get_task(service.list_tasks()[0].id).kind is TaskKind.PARSE
    assert len(service.list_parts("doc_big")) == 1
    assert service.list_documents("kb_1")


@pytest.mark.asyncio
async def test_question_task_runs_through_the_backfill_path(
    bundle: StoreBundle, embedder: DeterministicEmbedder, kb
) -> None:
    """QUESTIONS 任务走"补出题"而不是摄入——落进 `ingest()` 会按断点续跑空转。

    文档已经 indexed，摄入那条路 `_resume_stage` 返回 indexed、三个 `_before` 全 false，
    整次调用什么都不做。所以这个用例盯的是"任务真的产出了问题"。
    """

    class _StubQuestions:
        def __init__(self) -> None:
            self.calls = 0

        def generate_for_chunks(self, chunks, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return {chunk.chunk_id: ["这个任务派生的提问？"] for chunk in chunks}

    questions = _StubQuestions()
    service = IngestService(
        bundle,
        router=ParserRouter([PlainTextParser()]),
        embedder=embedder,
        chunk_config=ChunkingConfig(size=64, overlap=8),
        questions=questions,
    )
    outcome = service.submit(knowledge_base_id=kb.id, filename="a.md", content=CONTENT.encode())
    service.ingest(outcome.document.id)
    assert all(not chunk.questions for chunk in bundle.meta.iter_chunks(outcome.document.id))

    task = DocumentService(bundle).enqueue_questions(outcome.document.id)
    worker = TaskWorker(bundle, service, owner="worker-q", poll_interval=0.01)

    assert await worker.run_once() is True

    assert questions.calls == 1
    assert all(chunk.questions for chunk in bundle.meta.iter_chunks(outcome.document.id))
    done = bundle.meta.get_task(task.id)
    assert done is not None and done.state is TaskState.SUCCEEDED
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED


@pytest.mark.asyncio
async def test_wiki_task_runs_the_kb_level_callback(
    bundle: StoreBundle, ingest: IngestService, kb
) -> None:
    """WIKI 是知识库级任务：没有 document_id，必须走独立分支而不是摄入。

    落进 `ingest(document_id)` 会因为缺 document_id 直接报错，而且摄入那条路
    对"读已有内容写 Wiki"本来就不适用。
    """
    calls: list[str] = []
    worker = TaskWorker(
        bundle, ingest, owner="worker-wiki", poll_interval=0.01, compile_wiki=calls.append
    )
    task = bundle.meta.enqueue_task(
        TaskRecord(
            id="task_wiki",
            kind=TaskKind.WIKI,
            state=TaskState.PENDING,
            payload={"kb_id": "kb_1"},
        )
    )

    assert await worker.run_once() is True

    assert calls == ["kb_1"]
    done = bundle.meta.get_task(task.id)
    assert done is not None and done.state is TaskState.SUCCEEDED


@pytest.mark.asyncio
async def test_wiki_task_without_wiring_fails_clearly(
    bundle: StoreBundle, ingest: IngestService
) -> None:
    """没接上生成能力时明确失败，而不是被静默丢掉（"点了没反应"最难查）。"""
    worker = TaskWorker(bundle, ingest, owner="worker-nomap", poll_interval=0.01)
    task = bundle.meta.enqueue_task(
        TaskRecord(
            id="task_wiki2",
            kind=TaskKind.WIKI,
            state=TaskState.PENDING,
            payload={"kb_id": "kb_1"},
        )
    )

    assert await worker.run_once() is True

    failed = bundle.meta.get_task(task.id)
    assert failed is not None and "尚未接线" in (failed.error or "")


@pytest.mark.asyncio
async def test_stuck_lease_is_reclaimed_by_the_running_worker(
    bundle: StoreBundle, ingest: IngestService, kb
) -> None:
    """**worker 自己会把卡住的任务捞回来**（v24 补的调用点）。

    在这之前 `reclaim_expired_tasks` 只有实现和用例、生产代码里没有调用者，
    于是被崩溃/重启打断的任务永远停在 running：不重试、不失败、文档永远钉在
    中间阶段（实测挂过 8 小时）。这里钉住"回收真的会跑"。
    """
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id)
    # 模拟"上一个进程领走了任务然后死了"：租约已经过期，状态还写着 running
    bundle.meta.claim_task(owner="worker-dead", lease_seconds=1)
    from app.core.config import get_settings

    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute(
            "UPDATE tasks SET lease_expires_at = now() - interval '1 hour' WHERE state = 'running'"
        )

    worker = TaskWorker(bundle, ingest, owner="worker-alive", poll_interval=0.01)
    worker._reclaim_expired(force=True)  # type: ignore[attr-defined]

    assert bundle.meta.list_tasks(TaskState.PENDING)
    assert await worker.run_once() is True
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED


@pytest.mark.asyncio
async def test_reclaim_cadence_does_not_depend_on_how_long_a_task_takes(
    bundle: StoreBundle, ingest: IngestService
) -> None:
    """**回收的节奏只由租约时长决定**，与"手上这份活要跑多久"无关。

    这条用例钉的是一个踩了两次的坑（实机各验证过一次）：

    1. 回收原先只在"没活可干"的分支里跑 —— 队列一直有活就永远不跑；
    2. 改成每轮开头跑之后仍然不对：**一轮就是一份文档**（解析加出题几分钟到
       几十分钟），于是"回收间隔"实际等于"一份文档的耗时"。实机里那个僵尸任务
       带着刚过期 90 秒的租约一直挂着，界面上写着"疑似卡住"，而回收还在等当前
       这篇跑完。

    所以这里让"一轮"故意跑得比回收间隔长得多：回收仍然必须按自己的节拍发生。
    """
    calls = 0

    def counting_reclaim() -> int:
        nonlocal calls
        calls += 1
        return 0

    worker = TaskWorker(
        bundle, ingest, owner="worker-busy", lease_seconds=0.1, poll_interval=0.01
    )
    bundle.meta.reclaim_expired_tasks = counting_reclaim  # type: ignore[method-assign]

    async def one_long_task() -> bool:
        await asyncio.sleep(1.5)  # 一份"长任务"：比回收间隔（0.1s）长十几倍
        return True

    worker.run_once = one_long_task  # type: ignore[method-assign]

    stop = asyncio.Event()
    running = asyncio.create_task(worker.run_forever(stop=stop))
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(running, timeout=5)

    # 0.5 秒内按 0.1s 的节拍：至少 4 次（启动那次 + 之后每 0.1s 一次）。
    # 挂在消费循环上的话这里只会是 1 次——那份"长任务"还没跑完。
    assert calls >= 4, f"回收被长任务拖住了（0.5 秒内只调用了 {calls} 次）"
