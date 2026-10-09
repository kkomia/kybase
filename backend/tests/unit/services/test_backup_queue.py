"""备份补传队列的验收用例（M5 阶段 3，方案 §3.1 / §3.2 / §3.3）。

镜像同构：``app/services/backup_queue.py`` → ``tests/unit/services/test_backup_queue.py``。

**这一份用例守四件事**（每一条都能对回方案的一格）：

1. **退避与永不放弃**（§3.3）：60s → 2m → 5m → 15m → 1h 封顶，到点自动再试；
2. **本地队列上限**（§3.2）：3 份 / 1 GiB 两档，超限按**最旧**标 ``discarded`` + 删包 +
   ``last_error='local_pending_cap'``，而且**如实报**（``backlog`` 数得出来）；
3. **崩溃恢复与节拍**（§3.3）：``uploading`` 复位成 ``pending``；一个 daemon 线程、
   5 分钟一次、**没有到点的项时零网络**、**绝不因一次异常而终结**；
4. **传输契约**：两半（blob + manifest）在同一次调用里给全、调到我那一刻包还在盘上、
   成功之后才删包，行落 ``uploaded`` + 远端坐标。

**假的是什么、真的是什么**：真的本机库与真的队列表（``build_stores`` 装出来的本机档）、
真的阶段 2 打包器（自动快照那几条走真打包）；假的是**上传者**（阶段 4 才有真的）
与**时钟**（退避要拨时间，不能真等一小时）。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import InvalidRequestError
from app.core.storage import build_stores, reset_stores
from app.services import backup_queue as module
from app.services.backup_queue import (
    BACKOFF_SECONDS,
    EVERY_HOURS_KEY,
    PENDING_CAP_ERROR,
    PENDING_LIMIT_BYTES,
    PENDING_LIMIT_COUNT,
    THREAD_NAME,
    BackupQueueService,
    PendingUpload,
    UploadReceipt,
)
from app.services.backup_snapshot import (
    SNAPSHOT_KINDS,
    BackupSnapshotService,
    SnapshotBlob,
    SnapshotManifest,
    SnapshotResult,
)
from app.services.runtime_config import RuntimeConfigService
from app.storage.base import (
    BACKUP_SNAPSHOT_KINDS,
    BACKUP_UNFINISHED_STATES,
    BackupSnapshotRecord,
    BackupSnapshots,
    StoreBundle,
)

pytestmark = pytest.mark.local

DEVICE_ID = "3f1c8b2e-0a4d-4a77-9d55-2c6a1b7e9f01"
T0 = datetime(2026, 10, 5, 8, 3, 0, tzinfo=UTC)


# ------------------------------------------------------------------ 替身与帮手


class FakeClock:
    """可拨的墙上钟（退避与"距上次备份多久"都问它，用例不必真等一小时）。"""

    def __init__(self, moment: datetime = T0) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment += timedelta(**kwargs)


class RecordingUploader:
    """假的上传者：照真实现的顺序走一遍，并把每一步记下来。

    **"先 blob 后 manifest"这个顺序住在阶段 4 的上传者里**（它两次 PUT），所以这一层能钉的
    是那个顺序的**两个前提**——这份替身把它们当场核对成断言，而不是只记下来事后看：

    ① 两半在**同一次**调用里给全（接口上就没有"先给一半"这条路，所以 manifest 不可能
       先于 blob 出去）；
    ② 调到我那一刻包**还在盘上**、字节数与摘要都对得上（删包只发生在成功之后）。

    ``fail`` 是可改的：一句"网络回来了"就是把它清空（"断网入队、联网补传"整张图要用它）。
    """

    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.requests: list[tuple[str, str]] = []
        self.seen: list[PendingUpload] = []

    def upload(self, pending: PendingUpload) -> UploadReceipt | None:
        if self.fail:
            raise RuntimeError(self.fail)
        blob = Path(pending.blob_path)
        assert blob.is_file(), "传的那一刻包必须还在盘上（删包只发生在成功之后）"
        raw = blob.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == pending.sha256
        assert len(raw) == pending.blob_bytes
        self.requests.append(("blob", pending.snapshot_id))
        assert pending.manifest_json.startswith("{"), "manifest 必须与 blob 一起给到"
        self.requests.append(("manifest", pending.snapshot_id))
        self.seen.append(pending)
        return UploadReceipt(device_id=DEVICE_ID, snapshot_id=pending.snapshot_id)


class ExplodingSweep(BackupQueueService):
    """把 ``sweep`` 换成"每次都炸"的版本：用来验**节拍不因异常而终结**。

    这一层兜的正是"我们自己的 bug"（写错了、库坏了都会在这儿炸），而线程死了的现象是
    "备份悄悄不再补传"——没有报错可看，所以值得一条用例把它钉住。
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.passes = 0

    def sweep(self) -> module.SweepReport:
        self.passes += 1
        raise RuntimeError("这一轮烂掉了（用例）")


class BrokenPacker:
    """打不出快照的打包器（盘满 / 库坏了那一档）。"""

    def create(
        self, *, into: Path, kind: str, created_at: datetime | None = None
    ) -> SnapshotResult:
        raise OSError("磁盘满了（用例）")


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[StoreBundle]:
    """本机档的真装配（连带证明 ``backup_queue`` 接上了）。"""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, deployment="local", data_dir=tmp_path / "data"
    )
    bundle = build_stores(settings)
    assert bundle.backup_queue is not None, "本机档的待传队列必须接上（阶段 3）"
    yield bundle
    reset_stores()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def uploader() -> RecordingUploader:
    return RecordingUploader()


@pytest.fixture
def failing() -> RecordingUploader:
    return RecordingUploader(fail="连不上 NAS（用例）")


def _service(
    stores: StoreBundle,
    data_dir: Path,
    uploader: RecordingUploader,
    clock: FakeClock,
    **extra: object,
) -> BackupQueueService:
    return BackupQueueService(
        stores=stores, data_dir=data_dir, uploader=uploader, now=clock, **extra
    )


@pytest.fixture
def queue(
    stores: StoreBundle,
    data_dir: Path,
    uploader: RecordingUploader,
    clock: FakeClock,
) -> Iterator[BackupQueueService]:
    service = _service(stores, data_dir, uploader, clock)
    yield service
    service.stop()


@pytest.fixture
def failing_queue(
    stores: StoreBundle,
    data_dir: Path,
    failing: RecordingUploader,
    clock: FakeClock,
) -> Iterator[BackupQueueService]:
    service = _service(stores, data_dir, failing, clock)
    yield service
    service.stop()


@pytest.fixture
def packer(stores: StoreBundle, data_dir: Path) -> BackupSnapshotService:
    """真的阶段 2 打包器（自动快照那几条走真打包，不是替身）。"""
    return BackupSnapshotService(
        stores=stores, data_dir=data_dir, device_id=DEVICE_ID, device_name="测试机"
    )


def make_result(
    data_dir: Path,
    *,
    when: datetime = T0,
    payload: bytes = b"snapshot-bytes",
    kind: str = "manual",
) -> SnapshotResult:
    """造一份"已经打好"的结果（队列那一层只认这个形状，所以不必真打包）。

    摘要与 id 照阶段 2 的规矩算（``<device>-<ts>-<快照体前 8 位>``），manifest 用真的
    ``SnapshotManifest`` 序列化——于是队列那一边读到的 ``created_at`` 就是清单里那一个。
    """
    digest = hashlib.sha256(payload).hexdigest()
    snapshot_id = f"{DEVICE_ID}-{when.strftime('%Y-%m-%dT%H-%M-%SZ')}-{digest[:8]}"
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        device_id=DEVICE_ID,
        created_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        kind=kind,
        schema_version=2,
        blob=SnapshotBlob(bytes=len(payload), sha256=digest),
    )
    blob_path = data_dir.parent / "packed" / f"{snapshot_id}.tar.gz"
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(payload)
    return SnapshotResult(
        snapshot_id=snapshot_id,
        blob_path=blob_path,
        blob_bytes=len(payload),
        blob_sha256=hashlib.sha256(payload).hexdigest(),
        manifest=manifest,
        manifest_bytes=manifest.to_bytes(),
    )


def row_of(stores: StoreBundle, snapshot_id: str):  # type: ignore[no-untyped-def]
    assert stores.backup_queue is not None
    return stores.backup_queue.get_backup_snapshot(snapshot_id)


def all_rows(stores: StoreBundle) -> dict[str, BackupSnapshotRecord]:
    assert stores.backup_queue is not None
    return {row.id: row for row in stores.backup_queue.list_backup_snapshots(limit=None)}


def pending_files(data_dir: Path) -> list[str]:
    folder = data_dir / "backup" / "pending"
    return sorted(item.name for item in folder.iterdir()) if folder.is_dir() else []


def as_moment(result: SnapshotResult) -> datetime:
    return datetime.fromisoformat(result.manifest.created_at)


def wait_until(condition, message: str, *, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """等一个条件成立（线程上的事只能等，不能断言"立刻"）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(message)


# ------------------------------------------------------------------ 常量与词表


def test_the_caps_and_the_ladder_are_the_documented_numbers() -> None:
    """§3.2 的两条上限与 §3.3 的退避序列，逐格写死（改它们是有意识的动作）。"""
    assert PENDING_LIMIT_COUNT == 3
    assert PENDING_LIMIT_BYTES == 1024**3
    assert PENDING_CAP_ERROR == "local_pending_cap"
    assert module.TICK_SECONDS == 5 * 60
    assert THREAD_NAME == "backup-upload"
    assert BACKOFF_SECONDS == (60, 120, 300, 900, 3600)
    assert [module._backoff_seconds(n) for n in range(1, 8)] == [
        60,
        120,
        300,
        900,
        3600,
        3600,
        3600,
    ]


def test_kinds_and_states_match_the_table_vocabulary() -> None:
    """服务层与存储层是**同一份词表**（``SNAPSHOT_KINDS`` ↔ ``BACKUP_SNAPSHOT_KINDS``）。"""
    assert set(SNAPSHOT_KINDS) == set(BACKUP_SNAPSHOT_KINDS)
    assert "pending" in BACKUP_UNFINISHED_STATES
    assert "uploaded" not in BACKUP_UNFINISHED_STATES
    assert "discarded" not in BACKUP_UNFINISHED_STATES


# ------------------------------------------------------------------ 入队


def test_enqueue_registers_pending_and_tries_once_immediately(
    queue: BackupQueueService,
    uploader: RecordingUploader,
    stores: StoreBundle,
    data_dir: Path,
) -> None:
    """入队**立刻试一次**（不等 5 分钟）：行落 ``uploaded``、本地包删掉、远端坐标记下。

    "立刻"是可观察的：``enqueue`` 返回时上传者已经被调过一次——没有跑任何节拍。
    """
    result = make_result(data_dir)

    row = queue.enqueue(result)

    assert uploader.requests == [("blob", result.snapshot_id), ("manifest", result.snapshot_id)]
    assert row.state == "uploaded"
    assert row.uploaded_at == T0
    assert row.remote_device_id == DEVICE_ID
    assert row.remote_snapshot_id == result.snapshot_id
    assert pending_files(data_dir) == [], "传成之后本地那份包必须删掉（本机不留第二份明文副本）"


def test_a_queued_snapshot_survives_a_failed_attempt(
    failing_queue: BackupQueueService, stores: StoreBundle, data_dir: Path
) -> None:
    """断网入队：行留在队列里、**原因可见**、包还在 ``backup/pending/``（不抛给调用方）。"""
    result = make_result(data_dir)

    row = failing_queue.enqueue(result)

    assert row.state == "failed"
    assert row.attempts == 1
    assert "连不上 NAS" in row.last_error
    assert row.next_attempt_at == T0 + timedelta(seconds=BACKOFF_SECONDS[0])
    assert pending_files(data_dir) == [f"{result.snapshot_id}.tar.gz"]


def test_enqueue_moves_the_package_into_the_pending_folder(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path
) -> None:
    """包打在别处 → **搬**进 ``backup/pending/``（"包在哪"这条不变量归队列保证）。"""
    result = make_result(data_dir)

    row = queue.enqueue(result, attempt=False)  # 不试：这一条只看文件搬到哪儿了

    assert not result.blob_path.exists(), "搬走之后老位置不该还留一份"
    assert row.blob_path == str(data_dir / "backup" / "pending" / f"{result.snapshot_id}.tar.gz")
    assert pending_files(data_dir) == [f"{result.snapshot_id}.tar.gz"]


def test_enqueue_does_not_touch_a_package_already_in_place(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path
) -> None:
    """包已经在 ``backup/pending/`` 里（打包器打的就是那儿）→ **原样登记**，不搬不复制。

    观察法：同一份内容入两次队。第二次那份包已经在位，于是它必须**原地不动**
    （mtime 不变、目录里仍然只有一份），而且两行是同一行（内容寻址 → 同一个 id）。
    """
    first = make_result(data_dir, payload=b"same-bytes")
    queue.enqueue(first, attempt=False)
    inside = data_dir / "backup" / "pending" / f"{first.snapshot_id}.tar.gz"
    before = (inside.stat().st_mtime_ns, inside.stat().st_size)

    again = make_result(data_dir, payload=b"same-bytes")
    assert again.snapshot_id == first.snapshot_id, "同一份内容必须算出同一个 id（内容寻址）"
    row = queue.enqueue(again, attempt=False)

    assert (inside.stat().st_mtime_ns, inside.stat().st_size) == before, "包被搬过或重写过"
    assert pending_files(data_dir) == [inside.name], "pending 目录里不该出现第二份"
    assert row.id == first.snapshot_id and row.state == "pending"
    assert len(all_rows(stores)) == 1, "同 id 是覆盖写，不是新增一行"


def test_enqueue_refuses_pre_restore_snapshots(queue: BackupQueueService, data_dir: Path) -> None:
    """恢复前那一份**只落本机、不入队上传**（方案 §3.2）——这条边界写在服务里。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        queue.enqueue(make_result(data_dir, kind="pre_restore"), kind="pre_restore")

    assert "只落本机" in str(excinfo.value)


def test_enqueue_refuses_an_unknown_kind(queue: BackupQueueService, data_dir: Path) -> None:
    with pytest.raises(InvalidRequestError):
        queue.enqueue(make_result(data_dir), kind="whenever")


def test_enqueue_refuses_a_package_that_is_not_on_disk(
    queue: BackupQueueService, data_dir: Path
) -> None:
    """盘上那份包不见了 → 如实报，**不登记一行指向空文件的记录**。"""
    result = make_result(data_dir)
    result.blob_path.unlink()

    with pytest.raises(FileNotFoundError):
        queue.enqueue(result)


# ------------------------------------------------------------------ 退避与永不放弃


def test_consecutive_failures_walk_the_whole_backoff_ladder(
    failing_queue: BackupQueueService,
    stores: StoreBundle,
    data_dir: Path,
    clock: FakeClock,
) -> None:
    """连续失败逐档退避、每次都记原因、**到点自动再试、永不放弃**（§3.3 那张表逐格）。

    每一轮的观察点：``attempts`` 前进一格、``next_attempt_at`` = **当时** + 那一档、
    ``last_error`` 有原因；而"到点才会再试"由"拨到最后时刻的前一秒什么都不发生"钉住。
    """
    result = make_result(data_dir)
    failing_queue.enqueue(result)

    seen: list[tuple[int, float]] = []
    for _ in range(len(BACKOFF_SECONDS) + 2):
        row = row_of(stores, result.snapshot_id)
        seen.append((row.attempts, (row.next_attempt_at - clock.moment).total_seconds()))
        clock.moment = row.next_attempt_at + timedelta(seconds=1)  # 到点了
        failing_queue.sweep()

    assert [attempts for attempts, _ in seen] == list(range(1, len(BACKOFF_SECONDS) + 3))
    assert [delay for _, delay in seen[: len(BACKOFF_SECONDS)]] == list(BACKOFF_SECONDS)
    assert {delay for _, delay in seen[len(BACKOFF_SECONDS) :]} == {BACKOFF_SECONDS[-1]}
    row = row_of(stores, result.snapshot_id)
    assert row.state == "failed" and "连不上 NAS" in row.last_error


def test_a_retry_before_its_time_does_nothing(
    failing_queue: BackupQueueService,
    stores: StoreBundle,
    data_dir: Path,
    clock: FakeClock,
) -> None:
    """没到点 → 这一轮**一根网络都不发**（"只动到点的项"那一条）。"""
    result = make_result(data_dir)
    failing_queue.enqueue(result)
    assert row_of(stores, result.snapshot_id).attempts == 1

    clock.advance(seconds=BACKOFF_SECONDS[0] - 1)
    report = failing_queue.sweep()

    assert report == module.SweepReport()
    assert row_of(stores, result.snapshot_id).attempts == 1


def test_nothing_due_means_zero_network(
    queue: BackupQueueService, uploader: RecordingUploader
) -> None:
    """**没有待传项时零网络**（§3.3）：空队列跑一轮节拍，上传者一次都不该被调到。"""
    report = queue.sweep()

    assert report.uploaded == 0 and report.failed == 0
    assert uploader.requests == []


def test_a_late_network_comes_back(
    failing_queue: BackupQueueService,
    failing: RecordingUploader,
    stores: StoreBundle,
    data_dir: Path,
    clock: FakeClock,
) -> None:
    """ "断网入队、联网后补传"：失败几次之后网络回来 → 下一轮到点就传上去。"""
    result = make_result(data_dir)
    failing_queue.enqueue(result)
    clock.advance(hours=1)
    assert row_of(stores, result.snapshot_id).state == "failed"

    failing.fail = ""  # 网络回来了
    report = failing_queue.sweep()

    assert report.uploaded == 1
    row = row_of(stores, result.snapshot_id)
    assert row.state == "uploaded" and row.last_error == "" and row.uploaded_at == clock.moment
    assert pending_files(data_dir) == []


# ------------------------------------------------------------------ 上限


def test_the_count_cap_discards_the_oldest_and_says_so(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path, clock: FakeClock
) -> None:
    """条数上限（3 份）：超限按**最旧**标 ``discarded`` + 删包 + 固定理由，且数得出来。"""
    made = []
    for index in range(PENDING_LIMIT_COUNT + 2):
        clock.advance(minutes=1)
        result = make_result(data_dir, when=clock.moment, payload=f"payload-{index}".encode())
        made.append(result)
        queue.enqueue(result, attempt=False)

    rows = all_rows(stores)
    assert len(rows) == PENDING_LIMIT_COUNT + 2, "被丢的那几份**留在表里**（如实报，不删行）"
    for result in made[:2]:  # 最旧的两份
        assert rows[result.snapshot_id].state == "discarded"
        assert rows[result.snapshot_id].last_error == PENDING_CAP_ERROR
        assert rows[result.snapshot_id].next_attempt_at is None
    for result in made[2:]:
        assert rows[result.snapshot_id].state == "pending"
    assert pending_files(data_dir) == [f"{result.snapshot_id}.tar.gz" for result in made[2:]]

    backlog = queue.backlog()
    assert backlog.queued == PENDING_LIMIT_COUNT
    assert backlog.discarded == 2
    assert backlog.oldest_created_at == as_moment(made[2])


def test_the_byte_cap_discards_the_oldest(
    queue: BackupQueueService,
    stores: StoreBundle,
    data_dir: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字节上限（1 GiB）：与条数上限是**两条独立的上限**，谁先到算谁。

    真造 1 GiB 只会让用例变慢，判据的位置一点没变——所以把上限调小
    （与阶段 2 的额度用例同一个手法）。
    """
    monkeypatch.setattr(module, "PENDING_LIMIT_BYTES", 14)
    made = []
    for _ in range(3):
        clock.advance(minutes=1)
        result = make_result(data_dir, when=clock.moment, payload=b"x" * 6)
        made.append(result)
        queue.enqueue(result, attempt=False)

    rows = all_rows(stores)
    assert [rows[item.snapshot_id].state for item in made] == ["discarded", "pending", "pending"]
    assert rows[made[0].snapshot_id].last_error == PENDING_CAP_ERROR
    assert queue.backlog().bytes == 12
    assert queue.backlog().queued == 2
    assert queue.backlog().discarded == 1


def test_a_discarded_package_does_not_come_back(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path, clock: FakeClock
) -> None:
    """被丢的那一份**不会**被节拍重新捡起来（``discarded`` 不在待传那三档里）。"""
    for index in range(PENDING_LIMIT_COUNT + 1):
        clock.advance(minutes=1)
        queue.enqueue(
            make_result(data_dir, when=clock.moment, payload=f"p{index}".encode()), attempt=False
        )

    report = queue.sweep()

    assert report.uploaded == PENDING_LIMIT_COUNT
    assert report.discarded == 0
    assert queue.backlog().queued == 0
    uploaded = [row for row in all_rows(stores).values() if row.state == "uploaded"]
    assert len(uploaded) == PENDING_LIMIT_COUNT


# ------------------------------------------------------------------ 崩溃恢复


def test_recover_resets_the_half_uploaded_rows(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path
) -> None:
    """崩溃恢复：``uploading`` 复位成 ``pending``，``attempts`` 与原因**一个都不动**。"""
    result = make_result(data_dir)
    queue.enqueue(result, attempt=False)
    assert stores.backup_queue is not None
    stores.backup_queue.mark_backup_snapshot(
        result.snapshot_id, "uploading", bump_attempts=True, last_error="上一轮传到一半没了"
    )

    recovered = queue.recover()

    assert recovered == 1
    row = row_of(stores, result.snapshot_id)
    assert row.state == "pending"
    assert row.attempts == 1, "复位改的是「能不能再试」，不是「试过没有」"
    assert row.last_error == "上一轮传到一半没了"
    assert row.next_attempt_at is None, "复位之后立即到期（启动时没理由再等一轮退避）"


def test_recover_touches_nothing_else(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path
) -> None:
    """只动 ``uploading`` 那一档：``pending`` 的行一个字不改（别把别人的两次尝试合并了）。"""
    fresh = make_result(data_dir, when=T0, payload=b"fresh")
    queue.enqueue(fresh, attempt=False)

    assert queue.recover() == 0
    row = row_of(stores, fresh.snapshot_id)
    assert (row.state, row.attempts, row.next_attempt_at) == ("pending", 0, None)


def test_start_recovers_then_uploads(
    queue: BackupQueueService, stores: StoreBundle, data_dir: Path, uploader: RecordingUploader
) -> None:
    """``start()`` = 复位 + 起线程 + 叫醒一次：已经排队的那一份很快就被传上去。"""
    result = make_result(data_dir)
    queue.enqueue(result, attempt=False)

    assert queue.start() == 0
    wait_until(lambda: uploader.requests != [], "start 之后那一份没被传出去")
    queue.stop()

    assert row_of(stores, result.snapshot_id).state == "uploaded"


# ------------------------------------------------------------------ 节拍那条线程


def test_the_tick_keeps_running_after_a_failing_pass(
    stores: StoreBundle,
    data_dir: Path,
    uploader: RecordingUploader,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**线程绝不因一轮异常而终结**（照 ``kb_cache._work`` 那条纪律）。

    把 ``sweep`` 换成"每次都炸"的版本、把节拍调成毫秒级：跑几轮之后线程还得活着，
    而且真的跑过不止一轮——线程死了的现象是"备份悄悄不再补传"，那种 bug 没有报错可看。
    """
    monkeypatch.setattr(module, "TICK_SECONDS", 0.01)
    service = ExplodingSweep(stores=stores, data_dir=data_dir, uploader=uploader, now=clock)
    try:
        service.start()
        wait_until(lambda: service.passes >= 2, "节拍没有继续跑（线程多半死了）")
        worker = service.worker
        assert worker is not None and worker.is_alive()
        assert worker.name == THREAD_NAME
    finally:
        service.stop()
    assert not service.worker.is_alive()  # type: ignore[union-attr]


def test_the_worker_is_lazy_and_single(queue: BackupQueueService) -> None:
    """线程**懒建、一个进程一个**：没人用之前不存在，叫两次也只有一个。"""
    assert queue.worker is None

    queue.start()
    first = queue.worker
    queue.start()

    assert first is not None and queue.worker is first
    queue.stop()


def test_the_worker_is_a_daemon(queue: BackupQueueService) -> None:
    """线程是 daemon：边车退出不该被一次补传吊住（照 kb_cache 那处的先例）。"""
    queue.start()

    worker = queue.worker
    assert worker is not None and worker.daemon
    queue.stop()


def test_stop_is_idempotent(queue: BackupQueueService) -> None:
    queue.start()
    queue.stop()
    queue.stop()

    assert not queue.worker.is_alive()  # type: ignore[union-attr]


# ------------------------------------------------------------------ 自动快照判据


def test_no_runtime_config_means_no_automatic_snapshot(
    stores: StoreBundle, data_dir: Path, uploader: RecordingUploader, clock: FakeClock
) -> None:
    """没有运行期配置 → **按关算**（拿不到配置时保守一档，与阶段 2 那个开关同一条口径）。"""
    service = _service(stores, data_dir, uploader, clock)

    assert service.every_hours() == 0
    assert service.due_for_auto_snapshot() is False


def test_no_snapshotter_means_no_automatic_snapshot(
    stores: StoreBundle, data_dir: Path, uploader: RecordingUploader, clock: FakeClock
) -> None:
    """有配置、没有打包器 → 也按关算（"关掉自动快照"不必另加开关）。"""
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "6"})
    service = _service(stores, data_dir, uploader, clock, runtime_config=runtime)

    assert service.every_hours() == 6
    assert service.due_for_auto_snapshot() is False


def test_the_default_interval_is_open(
    stores: StoreBundle,
    data_dir: Path,
    uploader: RecordingUploader,
    clock: FakeClock,
    packer: BackupSnapshotService,
) -> None:
    """配置给上了但没配这一格 → **默认 24 小时（开）**（决策点 D3）。"""
    runtime = RuntimeConfigService(stores)
    service = _service(
        stores, data_dir, uploader, clock, snapshotter=packer, runtime_config=runtime
    )

    assert service.every_hours() == module.DEFAULT_EVERY_HOURS == 24
    assert service.due_for_auto_snapshot() is True, "从来没有过备份 → 比 N 小时更久，算到期"


def test_zero_hours_turns_it_off(
    stores: StoreBundle, data_dir: Path, uploader: RecordingUploader, clock: FakeClock
) -> None:
    """``provider.backup.every_hours=0`` → 关（§3.2 写着的那一档）。"""
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "0"})
    service = _service(stores, data_dir, uploader, clock, runtime_config=runtime)

    assert service.every_hours() == 0
    assert service.due_for_auto_snapshot() is False


def test_a_broken_interval_falls_back_to_the_default(
    stores: StoreBundle, data_dir: Path, uploader: RecordingUploader, clock: FakeClock
) -> None:
    """配了个读不出来的值 → 回落默认值（不因为一个手滑的字符就静默关掉自动备份）。"""
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "每天"})
    service = _service(stores, data_dir, uploader, clock, runtime_config=runtime)

    assert service.every_hours() == module.DEFAULT_EVERY_HOURS


def test_the_interval_is_measured_from_the_last_snapshot(
    stores: StoreBundle,
    data_dir: Path,
    uploader: RecordingUploader,
    clock: FakeClock,
    packer: BackupSnapshotService,
) -> None:
    """判据是"距**最近一条快照记录**超过 N 小时"（走真打包器一遍）。

    这一条走的是"传成功"那一支（`uploaded`）。``failed`` / ``discarded`` 也持有这只钟
    （它们是"打过一份"的另外两种结局）——见下面两条用例。
    """
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "6"})
    service = _service(
        stores, data_dir, uploader, clock, snapshotter=packer, runtime_config=runtime
    )
    assert service.due_for_auto_snapshot() is True

    first = service.sweep()
    assert first.auto_snapshot != ""
    auto_row = row_of(stores, first.auto_snapshot)
    assert auto_row.kind == "auto" and auto_row.state == "uploaded"

    clock.advance(hours=1)
    assert service.due_for_auto_snapshot() is False, "刚打过一份，不该再打"
    clock.advance(hours=5, minutes=1)
    assert service.due_for_auto_snapshot() is True

    second = service.sweep()
    assert second.auto_snapshot != "" and second.auto_snapshot != first.auto_snapshot
    assert second.uploaded == 1, "自动快照落进队列之后，同一轮就把它传出去（不必等下一个 5 分钟）"


def test_a_failed_snapshot_still_holds_the_clock(
    stores: StoreBundle,
    data_dir: Path,
    failing: RecordingUploader,
    clock: FakeClock,
    packer: BackupSnapshotService,
) -> None:
    """**失败的那一份也持有那只钟**：判据问的是"最近打过一份吗"，不是"备成了吗"。

    这条踩的是一个实测过的坑：立即尝试失败之后那一行是 ``failed``，把它排除在判据之外，
    下一轮（5 分钟后）就会又算"到期"→ 每个节拍重打一份整库快照（断网一天 = 288 份）。

    "有没有备上去"那个问题一个字不改地由 ``backlog()`` 如实回答——两只钟、两个问题。
    """
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "6"})
    service = _service(stores, data_dir, failing, clock, snapshotter=packer, runtime_config=runtime)

    first = service.sweep()
    assert first.auto_snapshot != ""
    failed_row = row_of(stores, first.auto_snapshot)
    assert failed_row.state == "failed", "上传失败的那一份在库里就是 failed（如实）"

    # 5 分钟后那一轮：**不再打**（那只钟被上面那份 failed 的行拿着）
    clock.advance(minutes=5)
    second = service.sweep()
    assert second.auto_snapshot == ""
    assert [row.id for row in all_rows(stores).values() if row.kind == "auto"] == [
        first.auto_snapshot
    ]

    # 而 N 小时之后该打（间隔到了，与那几份成没成无关）
    clock.advance(hours=6)
    assert service.due_for_auto_snapshot() is True
    third = service.sweep()
    assert third.auto_snapshot not in ("", first.auto_snapshot)


def test_a_48_hour_outage_only_takes_one_snapshot_per_interval(
    stores: StoreBundle,
    data_dir: Path,
    failing: RecordingUploader,
    clock: FakeClock,
    packer: BackupSnapshotService,
) -> None:
    """48 小时断网、每 5 分钟一轮节拍 → **一共恰好 2 份**（0h 与 24h），不是 288 份。

    这是上面那条坑的量化版：``every_hours=24`` 而节拍是 5 分钟一次，两者相差 288 倍——
    判据只要"看不见"最近打的那一份，就会一路重打、把上限反复撑爆。
    """
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "24"})
    service = _service(stores, data_dir, failing, clock, snapshotter=packer, runtime_config=runtime)

    ticks = 48 * 12  # 48 小时 / 5 分钟
    for _ in range(ticks):
        clock.advance(minutes=5)
        service.sweep()

    made = sorted(
        (row for row in all_rows(stores).values() if row.kind == "auto"),
        key=lambda row: row.created_at,
    )
    assert len(made) == 2, (
        f"48 小时断网该只有两份（0h 与 24h），读到 {len(made)} 份："
        f"{sorted(row.created_at.isoformat() for row in made)[:3]}…"
    )
    assert [row.created_at for row in made] == [
        T0 + timedelta(minutes=5),
        T0 + timedelta(hours=24, minutes=5),
    ], "第二份必须正好落在第一份的 24 小时之后"

    # 队列那一半如实：两份都还压在本地（断网窗口里**没有丢弃的抖动**）
    backlog = service.backlog()
    assert backlog.queued == len(made) == 2
    assert backlog.queued <= PENDING_LIMIT_COUNT
    assert backlog.discarded == 0
    assert backlog.failed == 2, "两份都试过、都没成（原因在 backlog.last_error）"


def test_a_failing_packer_does_not_kill_the_pass(
    stores: StoreBundle, data_dir: Path, uploader: RecordingUploader, clock: FakeClock
) -> None:
    """自动快照打不出来（盘满 / 没设备身份）→ 一轮照跑：上传那一半不受影响，原因如实报。"""
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "6"})
    service = _service(
        stores, data_dir, uploader, clock, snapshotter=BrokenPacker(), runtime_config=runtime
    )
    result = make_result(data_dir)
    assert service.enqueue(result, attempt=False).state == "pending"
    clock.advance(hours=24)  # 到了该打自动快照的时候（判据看的是"距最近一条多久"）

    report = service.sweep()

    assert report.auto_snapshot == ""
    assert "磁盘满了" in report.auto_error
    assert report.uploaded == 1, "打包失败不该拦住补传"


def test_the_auto_snapshot_lands_in_the_pending_folder(
    stores: StoreBundle,
    data_dir: Path,
    clock: FakeClock,
    packer: BackupSnapshotService,
) -> None:
    """自动快照走**真打包器**（阶段 2 那条链）：落在 ``backup/pending/``、被传上去、kind=auto。

    这一条是阶段 2 与阶段 3 的接缝：``SnapshotMaker`` 那份协议由真的
    ``BackupSnapshotService`` 满足，``created_at`` 用这一层的钟（时间戳只有一个来源）。
    """
    runtime = RuntimeConfigService(stores)
    runtime.set({EVERY_HOURS_KEY: "6"})
    uploader = RecordingUploader()
    service = _service(
        stores, data_dir, uploader, clock, snapshotter=packer, runtime_config=runtime
    )

    report = service.sweep()

    assert report.auto_snapshot.startswith(f"{DEVICE_ID}-2026-10-05T08-03-00Z-")
    seen = uploader.seen[0]
    assert seen.snapshot_id == report.auto_snapshot
    manifest = json.loads(seen.manifest_json)
    assert manifest["format"] == "kylab-backup" and manifest["kind"] == "auto"
    assert row_of(stores, report.auto_snapshot).kind == "auto"
    assert pending_files(data_dir) == []


# ------------------------------------------------------------------ 读数与部署档


def test_backlog_counts_what_is_not_uploaded_yet(
    failing_queue: BackupQueueService, data_dir: Path, clock: FakeClock
) -> None:
    """ "有几份没备上去"那一读（界面那句提示就数它）。"""
    clock.advance(minutes=1)
    first = make_result(data_dir, when=clock.moment, payload=b"a" * 10)
    failing_queue.enqueue(first)
    clock.advance(minutes=1)
    second = make_result(data_dir, when=clock.moment, payload=b"b" * 20)
    failing_queue.enqueue(second)

    backlog = failing_queue.backlog()

    assert backlog.queued == 2
    assert backlog.bytes == 30
    assert backlog.failed == 2
    assert backlog.discarded == 0
    assert backlog.oldest_created_at == as_moment(first)
    assert "连不上 NAS" in backlog.last_error


def test_backlog_is_quiet_when_there_is_nothing_waiting(queue: BackupQueueService) -> None:
    """队列空的时候那一读也要能答（``0`` 份、没有最旧、没有原因）。"""
    backlog = queue.backlog()

    assert (backlog.queued, backlog.bytes, backlog.failed, backlog.discarded) == (0, 0, 0, 0)
    assert backlog.oldest_created_at is None and backlog.last_error == ""


def test_the_store_satisfies_the_queue_protocol(stores: StoreBundle) -> None:
    """结构化类型下，装上去的那个实现**真的满足**那份协议；且五块是**同一个**实例。"""
    assert isinstance(stores.backup_queue, BackupSnapshots)
    assert stores.backup_queue is stores.snapshot is stores.kb_cache is stores.ledger


def test_the_service_refuses_to_exist_without_a_queue(tmp_path: Path) -> None:
    """服务器档（``StoreBundle.backup_queue`` 恒为 ``None``）→ **构造时就拒**。"""
    from app.storage.local_impl.object_store import LocalObjectStore
    from app.storage.split_impl import (
        UnavailableFullTextStore,
        UnavailableTabularStore,
        UnavailableVectorStore,
    )
    from app.storage.sqlite_impl.connection import Database
    from app.storage.sqlite_impl.meta_store import SqliteMetaStore
    from app.storage.sqlite_impl.schema import prepare

    db = Database(tmp_path / "kylab.db")
    prepare(db)
    server_side = StoreBundle(
        meta=SqliteMetaStore(db),
        vectors=UnavailableVectorStore(),
        fulltext=UnavailableFullTextStore(),
        objects=LocalObjectStore(tmp_path),
        tabular=UnavailableTabularStore(),
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            BackupQueueService(stores=server_side, data_dir=tmp_path, uploader=RecordingUploader())
        assert "队列" in str(excinfo.value)
    finally:
        db.close()
