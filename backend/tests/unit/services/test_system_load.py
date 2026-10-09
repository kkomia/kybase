"""负载面板数据源（§12.115）。

镜像同构：``app/services/system_load.py`` → 本文件。

**这一层的价值全在"数是不是真的"上**，所以用例分三组：

1. **CPU 百分比必须按两次采样之差算**——这是最容易写错、也最容易看起来没写错的一段
   （真实 ``psutil`` 每次都给个 0~100 的数，写错了照样"有值"）。注入假探针才断言得出精确值。
2. **队列口径**：停滞的任务仍算"在跑"、逾期的仍算"排队"，否则同一条任务会在
   "在跑 1"和"排队 1"两处各数一次。
3. **云端额度**：没配令牌时不该去查库（查询没意义），配了才查，且额度用尽要有明确标志
   ——它不是故障，是"变慢"的原因。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import pytest

from app.models.enums import DataSourceKind, DocumentStage, TaskKind, TaskState
from app.services.system_load import (
    MIN_CPU_SAMPLE_GAP,
    PsutilProbe,
    SystemLoadService,
)
from app.storage.base import (
    DocumentPartRecord,
    DocumentRecord,
    KnowledgeBaseRecord,
    ParseResultRecord,
    StoreBundle,
    TaskRecord,
)


class FakeCpuTimes(NamedTuple):
    """``psutil.cpu_times()`` 的最小子集（含 Linux 上的 guest 字段）。

    **必须是 namedtuple 而不是 dataclass**：真实返回的就是 namedtuple，
    服务端用 ``sum(times)`` 求总时间。拿 dataclass 当替身，测的就不是真东西了
    ——第一版正是这么挂的（``TypeError: object is not iterable``）。
    """

    user: float
    system: float
    idle: float
    iowait: float = 0.0
    guest: float = 0.0


class FakeHost:
    """可控的假探针：每次 ``cpu_times()`` 取下一组累计值。"""

    def __init__(self, samples: list[FakeCpuTimes] | None = None) -> None:
        self._samples = list(samples or [])
        self.memory_pair = (4 * 1024**3, 16 * 1024**3)
        self.rss: int | None = 512 * 1024**2

    def cpu_times(self) -> Any:
        if len(self._samples) > 1:
            return self._samples.pop(0)
        return self._samples[0]

    def cpu_count(self) -> int:
        return 8

    def memory(self) -> tuple[int, int]:
        return self.memory_pair

    def process_rss(self) -> int | None:
        return self.rss


def _service(bundle: StoreBundle, host: Any, **kwargs: Any) -> SystemLoadService:
    return SystemLoadService(
        bundle,
        concurrency=kwargs.pop("concurrency", 2),
        host=host,
        mineru_configured=kwargs.pop("mineru_configured", lambda: False),
        **kwargs,
    )


# --------------------------------------------------------------------- 硬件


def test_first_cpu_sample_is_unknown(bundle: StoreBundle) -> None:
    """**第一次采样必须是 None**，不能是 0。

    0% 会被读成"机器很空闲"，而那正好是最误导的答案——本该说"我还不知道"。
    """
    host = FakeHost([FakeCpuTimes(user=0, system=0, idle=100)])

    hardware = _service(bundle, host).snapshot().hardware

    assert hardware.cpu_percent is None
    assert hardware.cpu_count == 8


def test_cpu_percent_is_computed_from_the_delta(bundle: StoreBundle) -> None:
    """两次采样之间：总时间 +100（忙 +25、闲 +75）→ 占用 25%。"""
    host = FakeHost(
        [
            FakeCpuTimes(user=0, system=0, idle=0),
            FakeCpuTimes(user=25, system=0, idle=75),
        ]
    )
    service = _service(bundle, host)

    service.snapshot()  # 第一次只留下基准
    time.sleep(MIN_CPU_SAMPLE_GAP + 0.05)
    hardware = service.snapshot().hardware

    assert hardware.cpu_percent == pytest.approx(25.0, abs=0.5)


def test_guest_time_is_not_counted_twice(bundle: StoreBundle) -> None:
    """Linux 上 guest 已含在 user 里；不扣掉会把总时间算大、占用率算得不一样。

    造一个"guest 很大"的样本：扣掉是 50%，不扣是 60%——两条路径分得出来。
    """
    host = FakeHost(
        [
            FakeCpuTimes(user=0, system=0, idle=0, guest=0),
            FakeCpuTimes(user=100, system=100, idle=200, guest=100),
        ]
    )
    service = _service(bundle, host)

    service.snapshot()
    time.sleep(MIN_CPU_SAMPLE_GAP + 0.05)

    # sum = 500；扣掉 guest 100 → 总时间 400，忙 = 400 - 200 = 200 → 50%；
    # 不扣则是 500 总、300 忙 → 60%
    assert service.snapshot().hardware.cpu_percent == pytest.approx(50.0, abs=0.5)


def test_two_samples_too_close_together_give_up(bundle: StoreBundle) -> None:
    """间隔太近时返回 None：分母趋近 0 会算出 0% 或除零，两种都是假数据。"""
    host = FakeHost(
        [FakeCpuTimes(user=1, system=1, idle=1), FakeCpuTimes(user=9, system=9, idle=9)]
    )
    service = _service(bundle, host)

    service.snapshot()
    hardware = service.snapshot().hardware  # 紧接着再采一次

    assert hardware.cpu_percent is None


def test_memory_and_process_rss_are_reported(bundle: StoreBundle) -> None:
    host = FakeHost([FakeCpuTimes(user=0, system=0, idle=1)])

    hardware = _service(bundle, host).snapshot().hardware

    assert hardware.memory_used_bytes == 4 * 1024**3
    assert hardware.memory_total_bytes == 16 * 1024**3
    assert hardware.memory_percent == pytest.approx(25.0)
    assert hardware.process_rss_bytes == 512 * 1024**2


def test_broken_probe_does_not_take_the_whole_panel_down(bundle: StoreBundle) -> None:
    """探针炸了也要返回队列与额度：三件事互相独立，一个坏不该让面板整个变 500。"""

    class Broken(FakeHost):
        def cpu_times(self) -> Any:
            raise RuntimeError("psutil 挂了")

    snapshot = _service(bundle, Broken()).snapshot()

    assert snapshot.hardware.cpu_percent is None
    assert snapshot.hardware.memory_total_bytes == 0
    assert snapshot.queue.slots == 2  # 队列那一半照常


def test_real_psutil_probe_reads_something_sane() -> None:
    """真实探针只断言"读得到、在值域内"——机器负载是变的，断言具体值就是脆弱用例。"""
    probe = PsutilProbe()

    used, total = probe.memory()

    assert total > 0
    assert 0 < used <= total
    assert probe.cpu_count() >= 1
    assert probe.process_rss() is None or probe.process_rss() > 0


# --------------------------------------------------------------------- 队列


def _enqueue(store, task_id: str, kind: TaskKind, **kwargs: Any) -> TaskRecord:  # type: ignore[no-untyped-def]
    return store.enqueue_task(TaskRecord(id=task_id, kind=kind, state=TaskState.PENDING, **kwargs))


def test_queue_depth_counts_pending_by_kind(bundle: StoreBundle) -> None:
    """按类型分布是这一格最可操作的信息："积压全是出题"该关出题，不是加机器。"""
    _enqueue(bundle.meta, "t1", TaskKind.QUESTIONS)
    _enqueue(bundle.meta, "t2", TaskKind.QUESTIONS)
    _enqueue(bundle.meta, "t3", TaskKind.PARSE)

    queue = _service(bundle, FakeHost([FakeCpuTimes(0, 0, 1)])).snapshot().queue

    assert queue.pending == 3
    assert queue.pending_by_kind == {"questions": 2, "parse": 1}
    assert queue.running == 0
    assert queue.slots == 2


def test_stalled_task_counts_as_running_not_as_a_fourth_state(bundle: StoreBundle) -> None:
    """停滞的任务仍在"在跑"那一格里。

    它是"在跑但没人续约"，不是新的一类任务——单独数出来的话，
    "在跑 0 / 上限 2"与"停滞 1"会同时成立，读者没法判断槽位到底占没占。
    """
    _enqueue(bundle.meta, "t1", TaskKind.PARSE)
    claimed = bundle.meta.claim_task(owner="w1", lease_seconds=1)
    assert claimed is not None
    later = datetime.now(UTC) + timedelta(minutes=5)

    queue = _service(bundle, FakeHost([FakeCpuTimes(0, 0, 1)]), now=lambda: later).snapshot().queue

    assert queue.running == 1
    assert queue.stalled == 1
    assert queue.pending == 0


def test_completed_tasks_are_not_in_the_depth(bundle: StoreBundle) -> None:
    _enqueue(bundle.meta, "t1", TaskKind.PARSE)
    # 必须先领再收：``finish_task`` 是条件更新（``lease_owner = owner``），
    # 没领过的任务收不掉——这正是"租约易主后原消费者写不进来"的那道保险
    assert bundle.meta.claim_task(owner="w1", lease_seconds=60) is not None
    assert bundle.meta.finish_task("t1", TaskState.SUCCEEDED, owner="w1") is True

    queue = _service(bundle, FakeHost([FakeCpuTimes(0, 0, 1)])).snapshot().queue

    assert queue.pending == 0
    assert queue.running == 0
    assert queue.oldest_pending_seconds is None


def test_oldest_pending_seconds_uses_the_earliest_arrival(bundle: StoreBundle) -> None:
    """最老的排队任务等了多久——"队列里有没有东西卡了很久"只能靠这个数看出来。"""
    now = datetime.now(UTC)
    _enqueue(bundle.meta, "t1", TaskKind.PARSE, created_at=now - timedelta(minutes=30))
    _enqueue(bundle.meta, "t2", TaskKind.PARSE, created_at=now - timedelta(minutes=2))

    queue = _service(bundle, FakeHost([FakeCpuTimes(0, 0, 1)]), now=lambda: now).snapshot().queue

    assert queue.oldest_pending_seconds == pytest.approx(1800, abs=5)


def test_reading_a_broken_queue_does_not_raise(bundle: StoreBundle) -> None:
    """存储抖动时返回空队列而不是 500：面板要能显示"读不到"，而不是整页打不开。"""

    class BrokenMeta:
        def list_tasks(self) -> Any:
            raise RuntimeError("库连不上")

    broken = StoreBundle(
        meta=BrokenMeta(),  # type: ignore[arg-type]
        vectors=bundle.vectors,
        fulltext=bundle.fulltext,
        objects=bundle.objects,
        tabular=bundle.tabular,
    )

    queue = _service(broken, FakeHost([FakeCpuTimes(0, 0, 1)])).snapshot().queue

    assert queue.pending == 0
    assert queue.slots == 2


# --------------------------------------------------------------------- 云端额度


def test_unconfigured_parser_reports_zero_without_querying(bundle: StoreBundle) -> None:
    """没配令牌时是"未配置"，不是"用了 0 页"——两者含义完全不同。"""
    quota = (
        _service(bundle, FakeHost([FakeCpuTimes(0, 0, 1)]), mineru_configured=lambda: False)
        .snapshot()
        .quota
    )

    assert quota.configured is False
    assert quota.pages_used == 0
    assert quota.exhausted is False


def test_configured_parser_reports_pages_and_remaining(bundle: StoreBundle) -> None:
    """额度用尽要能标出来：它不是错误，是"解析忽然变慢"的原因。"""
    kb = _kb(bundle)
    _parse_result(bundle, kb, "doc_1", pages=1200, parser="MinerUCloudParser")

    quota = (
        _service(
            bundle,
            FakeHost([FakeCpuTimes(0, 0, 1)]),
            mineru_configured=lambda: True,
            mineru_quota_pages=1000,
        )
        .snapshot()
        .quota
    )

    assert quota.pages_used == 1200
    assert quota.calls == 1
    assert quota.remaining == 0
    assert quota.exhausted is True


def test_split_document_pages_are_summed_per_part_not_per_document(bundle: StoreBundle) -> None:
    """切分后每段单独送云端，额度只能按**各段页数**累加。

    按文档总页数算的话，一篇 1500 页切 8 段的文档会被记成 8 × 1500 = 12000 页，
    额度显示立刻失真到没法用（这正是把页数下推到 SQL 时要小心的那个陷阱）。
    """
    kb = _kb(bundle)
    document_id = _document(bundle, kb, "doc_split", page_count=400)
    for index, (start, end) in enumerate([(1, 200), (201, 400)], start=1):
        part_id = f"{document_id}_p{index}"
        bundle.meta.create_document_parts(
            [
                DocumentPartRecord(
                    id=part_id,
                    document_id=document_id,
                    part_index=index,
                    page_start=start,
                    page_end=end,
                    stage=DocumentStage.PARSING,
                )
            ]
        )
        _parse_result(
            bundle, kb, "doc_split", pages=400, parser="MinerUCloudParser", part_id=part_id
        )

    usage = bundle.meta.parser_page_usage(
        "MinerUCloudParser", since=datetime.now(UTC) - timedelta(hours=1)
    )

    assert usage == (400, 2)


def test_other_parsers_do_not_consume_the_mineru_quota(bundle: StoreBundle) -> None:
    """本地解析器不花云端额度——把它们的页数算进来会让额度永远显示"用尽"。"""
    kb = _kb(bundle)
    _parse_result(bundle, kb, "doc_local", pages=500, parser="LocalPdfTextParser")

    usage = bundle.meta.parser_page_usage(
        "MinerUCloudParser", since=datetime.now(UTC) - timedelta(hours=1)
    )

    assert usage == (0, 0)


def test_usage_before_the_window_is_ignored(bundle: StoreBundle) -> None:
    """只算今天：昨天用的页数不该算进今天的额度。"""
    kb = _kb(bundle)
    _parse_result(
        bundle,
        kb,
        "doc_old",
        pages=900,
        parser="MinerUCloudParser",
        created_at=datetime.now(UTC) - timedelta(days=1),
    )

    usage = bundle.meta.parser_page_usage(
        "MinerUCloudParser", since=datetime.now(UTC) - timedelta(hours=1)
    )

    assert usage == (0, 0)


# --------------------------------------------------------------------- 夹具助手


def _kb(bundle: StoreBundle) -> str:
    bundle.meta.create_knowledge_base(
        KnowledgeBaseRecord(id="kb_1", name="库里", embedding_model_id="m", embedding_dim=8)
    )
    return "kb_1"


def _document(bundle: StoreBundle, kb_id: str, document_id: str, *, page_count: int) -> str:
    bundle.meta.create_document(
        DocumentRecord(
            id=document_id,
            knowledge_base_id=kb_id,
            name=f"{document_id}.pdf",
            source_kind=DataSourceKind.UPLOAD,
            content_hash=f"hash-{document_id}",
            stage=DocumentStage.PARSED,
            page_count=page_count,
        )
    )
    return document_id


def _parse_result(
    bundle: StoreBundle,
    kb_id: str,
    document_id: str,
    *,
    pages: int,
    parser: str,
    part_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    if bundle.meta.get_document(document_id) is None:
        _document(bundle, kb_id, document_id, page_count=pages)
    bundle.meta.save_parse_result(
        ParseResultRecord(
            document_id=document_id,
            part_id=part_id,
            parser_name=parser,
            markdown_path=f"markdown/{document_id}.md",
            created_at=created_at,
        )
    )
