"""补覆盖：worker 的续租与循环容错，以及应用生命周期里的消费者（M2/M4 收尾）。

这几条不是"为了覆盖率"，而是真实故障场景：
租约到期被别人回收会让同一份文档被两个 worker 同时写；循环被单次异常带走则整个摄入停摆。
"""

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.models.enums import DocumentStage, TaskKind, TaskState
from app.parsers.plain_text import PlainTextParser
from app.services.chunking import ChunkingConfig
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.ingest import IngestError, IngestService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.parser_router import ParserRouter
from app.storage.base import StoreBundle, TaskRecord
from app.workers.queue_worker import TaskWorker

DIM = 16
CONTENT = "# 标题\n\n用于验证续租与容错的正文。\n"


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
def kb(bundle: StoreBundle, embedder: DeterministicEmbedder):
    return KnowledgeBaseService(bundle, embedder=embedder).create(kb_id="kb_1", name="库")


def _enqueue(bundle: StoreBundle, document_id: str, task_id: str = "task_x") -> TaskRecord:
    return bundle.meta.enqueue_task(
        TaskRecord(id=task_id, kind=TaskKind.PARSE, state=TaskState.PENDING,
                   document_id=document_id)
    )


# --------------------------------------------------------------------- 续租


@pytest.mark.asyncio
async def test_long_task_keeps_renewing_its_lease(bundle: StoreBundle, ingest: IngestService,
                                                  kb) -> None:
    """跑得比租约还久的任务必须持续续租，否则会被回收后重复执行。

    这里不直接调 ``run_once``：续租是 ``run_forever`` 里的独立循环，
    必须走真实入口才能验证它真的在续。
    """
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id, "task_slow")

    worker = TaskWorker(bundle, ingest, owner="w-slow", lease_seconds=0.3)
    claimed = threading.Event()
    extended = threading.Event()
    original = worker._handle
    observed: dict[str, datetime | None] = {}

    def slow_handle(task: TaskRecord) -> None:
        observed["lease"] = bundle.meta.list_tasks(TaskState.RUNNING)[0].lease_expires_at
        claimed.set()  # 已领到任务（此刻租约已被后台接管）
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = bundle.meta.list_tasks(TaskState.RUNNING)[0].lease_expires_at
            if (
                observed["lease"] is not None
                and current is not None
                and current > observed["lease"]
            ):
                extended.set()  # 租约被推到更晚 = 后台续租真的在跑
                break
            time.sleep(0.02)
        original(task)

    worker._handle = slow_handle  # type: ignore[method-assign]
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run_forever(stop=stop))

    assert await asyncio.to_thread(claimed.wait, 5), "worker 没有领到任务"
    assert await asyncio.to_thread(extended.wait, 5), "跑得比租约久的任务没有被续租"
    stop.set()
    await asyncio.wait_for(runner, timeout=5)
    assert bundle.meta.list_tasks(TaskState.SUCCEEDED), "任务应当正常完成"


@pytest.mark.asyncio
async def test_lease_loss_is_noticed_even_when_the_task_finishes_first(
    bundle: StoreBundle, ingest: IngestService, kb, monkeypatch,
    pg_database,
) -> None:
    """任务跑得比心跳还快时，也必须发现租约被抢。

    这是上面那条用例的"快机器"分支，也是它长期偶发失败的原因：摄入是本地线程，
    几十毫秒就能跑完。若它在下一个心跳 tick 之前结束，``_execute_with_heartbeat``
    的 ``finally`` 已经把 ``_current_task_id`` 清成 ``None``，心跳循环再也看不到
    租约易主——worker 于是继续领新任务，**停手完全变成时序抽奖**。

    （在本机 Python 3.12 上这条曾稳定失败十轮：机器足够快，永远走这条分支。）
    兜底点在 ``run_once`` 里 ``finish_task`` 返回 False 的那一刻：租约不在自己手上，
    它就在那里把丢失标志立起来，两个循环都能看见。
    """
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    task = _enqueue(bundle, outcome.document.id, "task_fast")

    # 心跳周期放大到"这条任务几乎不可能被 tick 撞上"的程度：
    # 只要停手依赖心跳，本用例必然超时——这正是在锁定上面那个竞态
    worker = TaskWorker(bundle, ingest, owner="w-fast", lease_seconds=30)

    async def instant_execute(record: TaskRecord) -> None:
        # 模拟"摄入瞬间完成"：不启动线程，直接给任务写终态
        worker._stores.meta.finish_task(record.id, TaskState.SUCCEEDED, owner=worker.owner)

    monkeypatch.setattr(worker, "_execute_with_heartbeat", instant_execute)

    # 先把租约判给别人，再让 worker 跑：finish_task 必然以 owner 不符而失败
    _steal_lease(pg_database, task.id, "someone-else")

    await asyncio.wait_for(worker.run_once(), timeout=5)

    assert worker._lease_lost.is_set(), "任务先跑完时，租约丢失也必须被记下来"


@pytest.mark.asyncio
async def test_lease_loss_stops_the_worker_without_stealing_the_outcome(
    bundle: StoreBundle, ingest: IngestService, kb, monkeypatch,
    pg_database,
) -> None:
    """租约被抢走后要停手，但**不能**去写这份任务的终态。

    这里让摄入卡住，同时把租约判给别人：worker 应当停止（续租发现租约易主即退出），
    而终态写入被 ``finish_task(owner=...)`` 拦下——否则它会用一个过期的执行结果
    覆盖新主人的状态，这是最容易出现"文档状态莫名奇妙"的一类 bug。
    """
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    task = _enqueue(bundle, outcome.document.id, "task_lost")

    worker = TaskWorker(bundle, ingest, owner="w-lost", lease_seconds=0.2)
    started = threading.Event()
    release = threading.Event()
    original = worker._handle

    def blocked_handle(record: TaskRecord) -> None:
        started.set()
        release.wait(timeout=5)  # 卡住摄入，制造"干活期间租约被抢"
        original(record)

    monkeypatch.setattr(worker, "_handle", blocked_handle)
    runner = asyncio.create_task(worker.run_forever())
    assert await asyncio.to_thread(started.wait, 5)

    _steal_lease(pg_database, task.id, "someone-else")
    release.set()

    await asyncio.wait_for(runner, timeout=5)

    assert bundle.meta.list_tasks(TaskState.RUNNING), "任务应留在 RUNNING，由新主人收尾"
    assert not bundle.meta.list_tasks(TaskState.SUCCEEDED), "过期执行结果不能判成功"
    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED


def _steal_lease(pg_database, task_id: str, owner: str) -> None:
    """直接把租约判给别人，模拟"任务被另一个消费者回收后重新领取"。

    不用 ``heartbeat_task``：那个接口本身要求租约还在自己名下（``lease_owner = owner``），
    所以它只能在"自己还持有租约"时续期，没法把租约判给第二个人。

    这条用例的价值就在于绕过仓储接口改库，所以直接写测试库的连接。
    """
    expires = datetime.now(UTC) + timedelta(seconds=60)
    with pg_database.session() as conn:
        changed = conn.execute(
            "update tasks set lease_owner = %s, lease_expires_at = %s where id = %s",
            (owner, expires, task_id),
        ).rowcount
    assert changed == 1, f"没找到任务 {task_id}"


# --------------------------------------------------------------------- 循环容错


@pytest.mark.asyncio
async def test_loop_survives_an_unexpected_exception(bundle: StoreBundle,
                                                     ingest: IngestService) -> None:
    """单次未预期异常不能带走整个消费循环。"""
    worker = TaskWorker(bundle, ingest, owner="w-flaky", poll_interval=0.01)
    stop = asyncio.Event()
    calls = {"n": 0}

    async def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟存储层抽风")
        stop.set()  # 撑过第一次异常之后主动收工
        return False

    worker.run_once = flaky  # type: ignore[method-assign]

    task = asyncio.create_task(worker.run_forever(stop=stop))
    await asyncio.wait_for(task, timeout=2)

    assert calls["n"] >= 2, "异常之后循环应当继续跑"
    assert task.exception() is None

@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_kill_the_worker(bundle: StoreBundle,
                                                          ingest: IngestService, kb,
                                                          monkeypatch) -> None:
    """续租时的一次存储抖动不能带走消费者。

    续租循环在 TaskGroup 里，异常会一路冒到 ``run_forever``——那就成了
    "数据库抖一下 → 消费者整个停摆，而手上那份摄入还在线程里默默跑"。
    这里让摄入慢过续租周期，保证抖动发生在任务执行期间：消费者必须活到任务完成。
    """
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id, "task_flaky_beat")

    worker = TaskWorker(bundle, ingest, owner="w-flaky-beat", lease_seconds=0.3)
    original_handle = worker._handle

    def slow_handle(task: TaskRecord) -> None:
        time.sleep(0.35)  # 慢过续租周期，让抖动落在执行期间
        original_handle(task)

    monkeypatch.setattr(worker, "_handle", slow_handle)

    real_heartbeat = bundle.meta.heartbeat_task
    calls = {"n": 0}

    def flaky_heartbeat(task_id: str, *, owner: str, lease_seconds: int) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟存储抖动")
        return real_heartbeat(task_id, owner=owner, lease_seconds=lease_seconds)

    monkeypatch.setattr(bundle.meta, "heartbeat_task", flaky_heartbeat)

    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run_forever(stop=stop))
    deadline = time.monotonic() + 5
    while not bundle.meta.list_tasks(TaskState.SUCCEEDED):
        assert time.monotonic() < deadline, "续租抖动之后消费者没能把任务跑完"
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert calls["n"] >= 2, "抖动之后续租应当继续"
    assert runner.exception() is None


# --------------------------------------------------------------------- 应用生命周期


def _fresh_app_env(monkeypatch, tmp_path, *, run_worker: str):
    from app.core.config import get_settings
    from app.core.services import reset_services
    from app.core.storage import reset_stores

    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_RUN_WORKER", run_worker)
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    return get_settings, reset_services, reset_stores


def test_lifespan_starts_and_stops_worker(monkeypatch, tmp_path) -> None:
    """配置打开时，应用启动要真的把消费者拉起来，退出时干净收尾。"""
    _, reset_services, reset_stores = _fresh_app_env(monkeypatch, tmp_path, run_worker="true")
    try:
        from app.main import create_app

        with TestClient(create_app()) as client:
            assert client.get("/api/v1/health").status_code == 200
    finally:
        reset_services()
        reset_stores()


def test_lifespan_warns_when_worker_disabled(monkeypatch, tmp_path, caplog) -> None:
    """关掉消费者时必须有告警：否则用户会以为"上传了没反应"是 bug。"""
    _, reset_services, reset_stores = _fresh_app_env(monkeypatch, tmp_path, run_worker="false")
    try:
        from app.main import create_app

        with (
            caplog.at_level(logging.WARNING, logger="app.main"),
            TestClient(create_app()) as client,
        ):
            assert client.get("/api/v1/health").status_code == 200
    finally:
        reset_services()
        reset_stores()

    assert any("未启动任务消费者" in record.getMessage() for record in caplog.records)


def test_worker_crash_is_logged_and_does_not_kill_the_app(monkeypatch, tmp_path, caplog) -> None:
    """消费者崩了要留下堆栈，但应用本身必须还活着：检索接口不依赖消费者。"""
    _, reset_services, reset_stores = _fresh_app_env(monkeypatch, tmp_path, run_worker="true")
    try:
        from app.main import create_app

        async def boom(self, *, stop: asyncio.Event | None = None) -> None:
            raise RuntimeError("模拟消费循环崩溃")

        monkeypatch.setattr(TaskWorker, "run_forever", boom)
        with (
            caplog.at_level(logging.ERROR, logger="app.main"),
            TestClient(create_app()) as client,
        ):
            assert client.get("/api/v1/health").status_code == 200
    finally:
        reset_services()
        reset_stores()

    assert any("任务消费者异常退出" in record.getMessage() for record in caplog.records)


def test_retry_is_dropped_when_lease_was_taken_over(bundle: StoreBundle,
                                                    ingest: IngestService, kb,
                                                    pg_database) -> None:
    """失败重试也要认租约：过期消费者不能把任务拽回队列，否则会和新主人一起被调度。"""
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    task = _enqueue(bundle, outcome.document.id, "task_retry")

    worker = TaskWorker(bundle, ingest, owner="w-old")
    claimed = bundle.meta.claim_task(owner="w-old", lease_seconds=60)
    assert claimed is not None
    _steal_lease(pg_database, task.id, "w-new")

    worker._retry_or_fail(
        claimed,
        IngestError("解析端点抖动", document_id=outcome.document.id, stage=DocumentStage.PARSING),
    )

    assert bundle.meta.list_tasks(TaskState.PENDING) == [], "不该被拽回队列"


def test_failure_is_dropped_when_lease_was_taken_over(bundle: StoreBundle,
                                                      ingest: IngestService, kb,
                                                      pg_database) -> None:
    """不可重试的失败同理：判失败也必须确认租约还在自己手上。"""
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    task = _enqueue(bundle, outcome.document.id, "task_fail")

    worker = TaskWorker(bundle, ingest, owner="w-old")
    claimed = bundle.meta.claim_task(owner="w-old", lease_seconds=60)
    assert claimed is not None
    _steal_lease(pg_database, task.id, "w-new")

    worker._retry_or_fail(claimed, ValueError("参数非法，重试没意义"))

    assert bundle.meta.list_tasks(TaskState.FAILED) == [], "过期执行结果不能判失败"


def test_worker_is_cancelled_on_shutdown(bundle: StoreBundle, ingest: IngestService,
                                        monkeypatch) -> None:
    """关停时消费者一定是被取消掉的，取消信号必须原样上抛。

    真实实现多半卡在 ``await asyncio.to_thread(...)`` 上，只能靠取消叫醒；
    若这里把 CancelledError 吞掉，asyncio 会以为任务正常结束，取消语义就丢了。
    """
    from app.main import _run_worker

    seen: list[asyncio.Event | None] = []

    async def cancellable(self, *, stop: asyncio.Event | None = None) -> None:
        seen.append(stop)
        assert stop is not None
        await stop.wait()

    monkeypatch.setattr(TaskWorker, "run_forever", cancellable)

    async def scenario() -> None:
        worker = TaskWorker(bundle, ingest, owner="w-stop")
        task = asyncio.create_task(_run_worker(worker, asyncio.Event()))
        await asyncio.sleep(0)  # 让消费者真正跑起来
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert seen and isinstance(seen[0], asyncio.Event)


def test_shutdown_waits_for_the_in_flight_task(bundle: StoreBundle, ingest: IngestService,
                                                kb, monkeypatch) -> None:
    """关停不能把手上这个摄入任务半途丢下：文档会停在写了一半的状态。

    用真的同步阻塞函数顶替"线程池里的摄入"，因为要验证的正是
    "取消协程拦不住已经在跑的线程"这个语义。
    """
    from app.main import _run_worker

    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id, "task_drain")

    async def scenario() -> None:
        worker = TaskWorker(bundle, ingest, owner="w-drain")
        started = threading.Event()
        original = worker._handle

        def slow_handle(task: TaskRecord) -> None:
            started.set()
            time.sleep(0.3)  # 相当于摄入还没跑完
            original(task)

        monkeypatch.setattr(worker, "_handle", slow_handle)
        task = asyncio.create_task(_run_worker(worker, asyncio.Event()))
        await asyncio.to_thread(started.wait)  # 等到摄入真的开始
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 摄入必须已经写完（而不是停在写了一半），但任务**不算成功**：
        # 租约状态不明，判终态会覆盖别人；留在 RUNNING 由租约机制重领更安全。
        assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED
        assert bundle.meta.list_tasks(TaskState.RUNNING), "任务应留在 RUNNING 等重领"
        assert not bundle.meta.list_tasks(TaskState.SUCCEEDED)

    asyncio.run(scenario())


def test_document_is_indexed_by_manual_worker_run(bundle: StoreBundle, ingest: IngestService,
                                                  kb) -> None:
    outcome = ingest.submit(knowledge_base_id="kb_1", filename="a.md", content=CONTENT.encode())
    _enqueue(bundle, outcome.document.id)

    asyncio.run(TaskWorker(bundle, ingest, owner="w").run_once())

    assert bundle.meta.get_document(outcome.document.id).stage is DocumentStage.INDEXED
