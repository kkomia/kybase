r"""备份补传队列（M5 阶段 3，方案 §3.1 / §3.2 / §3.3）。

**它解决的是"断网那一刻也不能丢"**：手动/自动打好的快照先落在本机队列里（一张库表），
由**一个**守护线程按退避节奏一张一张往 NAS 补传；传成了就删掉本地那份包（append-only 的
保存处在远端，本机不留第二份明文副本）。

**队列为什么是库里一张表、不是目录**（方案 §3.1 的四条）：① 队列项要有状态机 / 尝试次数 /
下次可试时间 / 失败原因，目录名表达不了；② "断网入队、联网补传"要能**跨重启继续**
（只有库有这份持久性）；③ 幂等判据是内容哈希，得与那一行一起落库；④ 与 M2 的
``imports`` / ``import_items`` 同一套形态、同一个库、同一把写锁。表与五类存储方法见
``app/storage/base.py`` 的 ``BackupSnapshots`` 与 ``sqlite_impl.LOCAL_BACKUP_METHODS``。

**这一层不碰两样东西，两样都是注入的**：

- **不碰网络**：传输那件事是 :class:`SnapshotUploader`（阶段 4 才接 HTTP 提供者）。
  本模块**一个网络库都不 import**——用例拿假的 uploader 就能把退避、上限、顺序全测完，
  而真实现（阶段 4）换上去之后这一层一行不用改；
- **不碰 SQLite**（L2）：队列的读写全在 ``StoreBundle.backup_queue`` 那五个方法后面。

**打快照也是注入的**（:class:`SnapshotMaker`，阶段 2 的 ``BackupSnapshotService``
结构上就满足它），理由与上一条同源：这一层只管"什么时候该打、打完怎么排进队列"。
放在构造参数里而不是 import 进来，还多一条好处——**没有它就没有自动快照**（用例与服务器档
都不必造一个打包器，而"关掉自动快照"这件事不需要另加开关）。

**节拍与退避**（§3.3 逐格）：

- 一个 daemon 线程（名字 ``backup-upload``）、懒建、一个进程一个，照 ``services/kb_cache.py``
  那一处的手法：**绝不因一次异常而终结**（线程死了的现象是"备份悄悄不再补传"，
  比一次失败难查得多）；
- 5 分钟一次；**没有到点的项时零网络**（这一句是用例钉住的：uploader 一次都不该被调到）；
- 入队之后**立刻试一次**（不等 5 分钟）——失败只记 ``last_error``，行照旧排队
  （"断网入队"要的就是这个：报 202、队列多一行、原因可见）；
- 退避 60s → 2m → 5m → 15m → 1h（上限 1h，**永不放弃**）；
- 崩溃恢复：``start()`` 时把 ``uploading`` 复位成 ``pending``（进程被杀留下的半截）；
- **不做并发上传**：一次节拍里一张一张传（``_pass`` 那把锁保证"一次只有一个节拍在跑"）。

**pending 上限**（§3.2）：默认 3 份 / 1 GiB，超限按**最旧**标 ``discarded`` + 删包 +
``last_error='local_pending_cap'``——**如实报，不静默**（:meth:`BackupQueueService.backlog`
就是"有几份没备上去"那一读）。
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.exceptions import InvalidRequestError
from app.services.backup_snapshot import SnapshotResult
from app.services.runtime_config import RuntimeConfigService
from app.storage.base import (
    BACKUP_SNAPSHOT_KINDS,
    BACKUP_UNFINISHED_STATES,
    BackupSnapshotRecord,
    BackupSnapshots,
    StoreBundle,
)

__all__ = [
    "BACKOFF_SECONDS",
    "EVERY_HOURS_KEY",
    "PENDING_CAP_ERROR",
    "PENDING_LIMIT_BYTES",
    "PENDING_LIMIT_COUNT",
    "THREAD_NAME",
    "TICK_SECONDS",
    "BackupQueueService",
    "PendingUpload",
    "SnapshotMaker",
    "SnapshotUploader",
    "UploadBacklog",
    "UploadReceipt",
]

logger = logging.getLogger(__name__)

THREAD_NAME = "backup-upload"
"""补传线程的名字（一个进程一个）。名字要能在 ``ps`` / 线程栈里一眼认出来——
"这是那个补传线程"与"这是哪个请求在传"是两个不同的问题。"""

TICK_SECONDS = 5 * 60
"""节拍：5 分钟一次（§3.3）。**与退避是两回事**：节拍是"多久看一眼队列"，
退避是"这一份隔多久再试"——节拍比最短的退避（60s）长，所以看了一眼发现没到点就走，
是正常状态而不是漏试。"""

BACKOFF_SECONDS: tuple[int, ...] = (60, 120, 300, 900, 3600)
"""失败之后的退避序列（§3.3 那张表）：60s → 2m → 5m → 15m → 1h。

第 5 次之后**封顶 1 小时、永不放弃**：断网可能是几小时，也可能是几天；"放弃了"这件事
在产品上等于"你的备份没了，而且没人告诉你"。"""

PENDING_DIR = ("backup", "pending")
"""本地包的落点（``<data_dir>/backup/pending/<id>.tar.gz``，§3.2）。"""

PENDING_LIMIT_COUNT = 3
"""待传队列最多留几份（§3.2 的 pending 上限）。超限按**最旧**丢，理由见 :meth:`…_enforce_cap`。"""

PENDING_LIMIT_BYTES = 1024**3
"""待传队列最多占多少字节（1 GiB）。与条数上限是**两条独立的上限**，谁先到算谁。"""

PENDING_CAP_ERROR = "local_pending_cap"
"""上限丢弃时写进 ``last_error`` 的那个固定串（§3.2）。

**写成一个常量而不是一句人话**：它是"这一行是被上限丢掉的"这个事实的判据，
界面与用例按它分流；人话由界面去组织。"""

EVERY_HOURS_KEY = "provider.backup.every_hours"
"""自动快照的间隔设置键（小时）。``0`` = 关（方案 §3.2 / 决策点 D3）。

**缺省 24 小时（开）**：这一格是"断网也不丢"的价值所在，关掉就只剩手动。
（`DEFAULTS` 那一处是设置层的事——阶段 4 把它加进 ``runtime_config``；本模块的读法
对"键还不存在"这一档按 24 算，见 :meth:`BackupQueueService.every_hours`。）"""

DEFAULT_EVERY_HOURS = 24
"""``provider.backup.every_hours`` 没配时的生效值（决策点 D3：默认开、24 小时）。"""


# ---------------------------------------------------------------- 注入的两件事


@dataclass(frozen=True, slots=True)
class PendingUpload:
    """一次补传要用的**全部东西**（方案 §3.3 的"先 blob 后 manifest"就是它的两半）。

    为什么不是直接给阶段 2 的 ``SnapshotResult``：那是个**内存里的对象**，而队列的意义
    正是"跨重启继续"——进程重启之后那份 ``SnapshotResult`` 早没了，能交给上传者的只有
    "库里那一行 + 盘上那份包"。所以这一份是从 :class:`BackupSnapshotRecord` 拼出来的，
    字段与它一一对应（``manifest_json`` 就是包里的第一个成员，原样发出去）。
    """

    snapshot_id: str
    blob_path: Path
    blob_bytes: int
    sha256: str
    manifest_json: str

    @property
    def manifest_bytes(self) -> bytes:
        """清单原文（那份"完成标记"要发的字节）。

        从库里那串 TEXT 重新编码是**无损**的：它是打包器 ``manifest_bytes.decode("utf-8")``
        写进去的，而清单里只有 UTF-8（``ensure_ascii=False`` 的中文名字也是合法 UTF-8）。
        所以"另传一份 manifest.json"与包里第一成员仍然逐字节相同（一个真相）。
        """
        return self.manifest_json.encode("utf-8")


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    """远端确认下来的那一对坐标（方案 §1.2 的 ``(device_id, snapshot_id)``）。

    上传者**报了就给**、没报就算了（``upload`` 返回 ``None``）：那一对是**服务端**的事实，
    本机从 id 里拆一个出来就多了一个会说谎的来源（id 的拼法是个约定，服务端回的名字才是答案）。
    """

    device_id: str
    snapshot_id: str


@runtime_checkable
class SnapshotUploader(Protocol):
    """把一份快照传上去（阶段 4 的 ``services/backup_provider.py`` 满足它）。

    **先 blob 后 manifest 的纪律在实现里**，不在这里：manifest 是"这一份传完了"的完成标记，
    先传它只会让枚举看见一个取不到东西的恢复点（NAS 侧那条"清单 PUT 要求快照体已在桶里"
    的规则会直接拒掉它）。所以这一层的接口是"把这一份传上去"这一个动作，
    而不是"先传这个、再传那个"两个动作——两个动作的话，顺序就成了一份口头约定。

    失败**抛异常**（消息要能直接进 ``last_error`` 给用户看）：连不上、401、409、413
    都走这一条路，由队列那一层记原因 + 排下一次。返回 :class:`UploadReceipt` 或 ``None``。
    """

    def upload(self, pending: PendingUpload) -> UploadReceipt | None: ...


@runtime_checkable
class SnapshotMaker(Protocol):
    """打一份快照（阶段 2 的 ``BackupSnapshotService`` 结构上就满足它）。

    只要一个 ``create``：``into`` 是本地包的落点（自动快照就落在 ``backup/pending/``，
    于是入队那一步不必再搬一次文件），``kind`` 是 ``auto`` / ``manual`` 那一档，
    ``created_at`` 由调用方给——**这一层的钟**因此也是那份快照的钟（时间戳只有一个来源；
    用例拨一次时间，id 里那段时刻跟着变）。
    """

    def create(
        self, *, into: Path, kind: str, created_at: datetime | None = None
    ) -> SnapshotResult: ...


# ---------------------------------------------------------------- 读数


@dataclass(frozen=True, slots=True)
class UploadBacklog:
    """待传队列的概览（"有几份没备上去"那一读，§3.2）。

    ``queued`` 是**还没传上去**的份数（``pending`` / ``uploading`` / ``failed`` 三档，
    见 ``base.BACKUP_UNFINISHED_STATES``）：界面那句"还有 N 份没备上去"就是它。
    ``discarded`` 是历史上被本地上限丢掉的那些（**如实报**的另一半：
    那几份确实没备上去，而且原因不是网络）。
    """

    queued: int = 0
    bytes: int = 0
    failed: int = 0
    discarded: int = 0
    oldest_created_at: datetime | None = None
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class SweepReport:
    """一次节拍做了什么（日志与用例都读它）。

    ``skipped`` = 已经有另一个节拍在跑，这一轮什么都没做（**不做并发上传**）。
    ``auto_snapshot`` 是这一轮打出来的自动快照 id（没打就是空串）；
    ``auto_error`` 是"该打但打不出来"的原因（如实报，下一轮还会再试）。
    """

    uploaded: int = 0
    failed: int = 0
    discarded: int = 0
    auto_snapshot: str = ""
    auto_error: str = ""
    skipped: bool = False


# ---------------------------------------------------------------- 服务


class BackupQueueService:
    """备份的入队与补传（M5 阶段 3）：**队列、节拍、退避**三件事。

    构造只有六样东西，其中两样是注入的（见模块头）：队列（从 ``stores`` 取）、
    ``uploader``（必须给）、``snapshotter``（不给就没有自动快照）、``runtime_config``
    （不给就没有自动快照）、``data_dir``（本地包落点）、``now``（墙上钟，用例拨时间用）。

    ``now`` 是**墙上钟而不是单调钟**：退避要能跨重启继续（"下次可试时刻"是库里的一个
    时间戳），而单调钟的重启就归零——用它算出来的时刻在重启后毫无意义。
    """

    def __init__(
        self,
        *,
        stores: StoreBundle,
        data_dir: str | Path,
        uploader: SnapshotUploader,
        snapshotter: SnapshotMaker | None = None,
        runtime_config: RuntimeConfigService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        queue = stores.backup_queue
        if not isinstance(queue, BackupSnapshots):
            raise RuntimeError(
                "这个部署没有备份队列（StoreBundle.backup_queue 为 None）："
                "待传队列是本机档独有的（服务器档自己就是备份的目的地）"
            )
        self._queue = queue
        self._data_dir = Path(data_dir)
        self._uploader = uploader
        self._snapshotter = snapshotter
        self._runtime = runtime_config
        self._now = now or (lambda: datetime.now(UTC))
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._guard = threading.Lock()
        """守护线程与 ``_worker`` 那一格（懒建、只建一个）。"""
        self._pass = threading.Lock()
        """一轮节拍的独占锁：**不做并发上传**（§3.3）——拿不到就跳过这一轮。"""

    # -------------------------------------------------------------- 对外：生命周期

    def start(self) -> int:
        """启动：崩溃恢复 + 起那一个补传线程；返回复位了几行。

        **只有这一处做复位**（不要在别处顺手调 ``reset_uploading_snapshots``）：
        ``uploading`` 的含义是"有个进程正在传"，而"谁是那个进程"这个问题的答案只在
        进程启动那一刻是确定的——别处调用就会把另一个活着的进程正在传的那一行抢回来。
        """
        recovered = self.recover()
        self._ensure_worker()
        self._wake.set()
        return recovered

    def recover(self) -> int:
        """把 ``uploading`` 复位成 ``pending``（进程被杀留下的半截），返回复位行数。

        单独一个方法是为了用例与阶段 4 的端点能**不放线程**地做这一件事
        （``start()`` = 它 + 起线程）。
        """
        return self._queue.reset_uploading_snapshots()

    def stop(self) -> None:
        """叫停那个线程（用例与关停用）。

        边车退出时**不强制**调它（线程是 daemon，进程退出不会被它吊住）；它存在的意义
        是"让测试能确定性地收尾"与"给将来一个优雅关停的落点"。
        """
        self._stopped.set()
        self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)

    @property
    def worker(self) -> threading.Thread | None:
        """那一个补传线程（没起就是 ``None``）。

        "它到底在不在跑"只能从对象上问——线程死活这件事在日志里看不出来（线程死了以后
        就没有日志了，那正是最难查的那一类现象）。用例也靠它断言。
        """
        return self._worker

    # -------------------------------------------------------------- 对外：入队

    def enqueue(
        self, result: SnapshotResult, *, kind: str = "manual", attempt: bool = True
    ) -> BackupSnapshotRecord:
        """把一份打好的快照登记成待传，并**立刻试一次**（失败只记账，不抛）。

        包已经在 ``<data_dir>/backup/pending/`` 里时**不复制、不搬动**（打包器本来就把
        自动快照打在那儿）；打在别处（手动备份打到别处也行）就**搬**进那个目录——
        "本地包在 pending 目录里"这条不变量值得由队列来保证：它是"哪几份还没传上去"
        这个问题在磁盘上的答案。

        ``pre_restore`` **拒绝入队**（方案 §3.2：恢复前那一份只落本机、不往外传）。
        这条规则写在服务里而不是只写在文档里：它是一条"传出去就多一份明文副本"的边界，
        不该靠调用方记得。

        ``attempt=False`` 是给"我确定现在不想碰网络"的调用方留的（阶段 4 的端点若把
        入队放在请求线程里，可以用它把第一次尝试让给后台线程）。
        """
        if kind not in BACKUP_SNAPSHOT_KINDS:
            allowed = "、".join(sorted(BACKUP_SNAPSHOT_KINDS))
            raise InvalidRequestError(f"未知的快照类型 {kind!r}（只能是 {allowed}）")
        if kind == "pre_restore":
            raise InvalidRequestError(
                "恢复前的那一份快照只落本机、不入待传队列（它是误覆盖的第一道兜底，不发出去）"
            )
        blob = self._pending_dir() / f"{result.snapshot_id}.tar.gz"
        self._pending_dir().mkdir(parents=True, exist_ok=True)
        if not blob.is_file():
            # 三档：包已经在 pending 目录里（打包器打的就是那儿 → 什么都不做）；
            # 打在别处（搬进来）；哪都没有（如实报，别登记一行指向不存在的文件）。
            # 名字里带着快照体摘要的前 8 位，所以"同名"就是"同一份内容"（内容寻址）——
            # 反复入同一份时第二、三次走的就是第一条。
            if not result.blob_path.is_file():
                raise FileNotFoundError(f"要入队的快照包不在盘上：{result.blob_path}")
            shutil.move(str(result.blob_path), str(blob))
        record = self._register(result, kind=kind, blob_path=blob)
        self._ensure_worker()
        if attempt:
            self.sweep()
        return self._queue.get_backup_snapshot(record.id) or record

    # -------------------------------------------------------------- 对外：读数

    def backlog(self) -> UploadBacklog:
        """ "有几份没备上去、为什么"（界面与阶段 4 的端点读它）。

        队列那一半**永远很小**（上限就是 3 份），被丢掉的会越积越多——所以后者只用来报
        "一共丢过几份"，不是"现在还能读到的几份"（那些包已经删了，见 ``base`` 里
        ``BackupSnapshotRecord`` 那段关于 ``blob_path`` 的说明）。
        """
        queued = self._queue.list_backup_snapshots(states=BACKUP_UNFINISHED_STATES, limit=None)
        discarded = self._queue.list_backup_snapshots(states=("discarded",), limit=None)
        newest = max(queued, key=lambda row: (row.created_at, row.id), default=None)
        return UploadBacklog(
            queued=len(queued),
            bytes=sum(row.blob_bytes for row in queued),
            failed=sum(1 for row in queued if row.attempts > 0),
            discarded=len(discarded),
            oldest_created_at=min((row.created_at for row in queued), default=None),
            last_error=newest.last_error if newest is not None else "",
        )

    def recent(self, *, limit: int = 20) -> tuple[BackupSnapshotRecord, ...]:
        """最近几份（新的在前）——**给界面列"哪几份没传上去、为什么"用的那一读**。

        与 :meth:`backlog` 是一对：那个给计数（"还有 N 份"），这个给行（"是哪几份、什么
        状态、上次失败的原因"）。两个都从同一张表读、同一把尺子排序，所以界面上的数与
        列表不会是两件事。

        **为什么要这一个方法（M5 阶段 4 加）**：``/local/backup`` 要把它当窄投影回给前端，
        而 ``api/`` 那一层**不许 import 存储**（L1 规则：协议层只能转发 services/）——
        队列这张表只有这一个服务持有，所以读它只能经这个方法。它不移动、不改任何东西，
        纯粹是把 ``list_backup_snapshots(newest_first=True)`` 的读面收成一句。
        """
        return tuple(self._queue.list_backup_snapshots(newest_first=True, limit=int(limit)))

    def every_hours(self) -> int:
        """自动快照的间隔（小时，``0`` = 关）。

        三种取值来源，逐档写清楚（"读不到配置"与"配置成 0"是两件事，不能混）：

        - 没有 ``runtime_config`` → ``0``（**关**）：拿不到配置时按更保守的那一档算
          （与阶段 2 那个"工作区开关拿不到就按关"同一条口径）；
        - 有配置但没有这个键 → ``DEFAULT_EVERY_HOURS``（24，决策点 D3）；
        - 有键且能解析 → 那个数（负数按 0 算：**「关」比「每 -3 小时备份一次」有含义**）。
        """
        if self._runtime is None:
            return 0
        raw = (self._runtime.get(EVERY_HOURS_KEY) or "").strip()
        if not raw:
            return DEFAULT_EVERY_HOURS
        try:
            return max(int(raw), 0)
        except ValueError:
            logger.warning(
                "%s 不是整数（读到 %r），按默认的 %d 小时算",
                EVERY_HOURS_KEY,
                raw,
                DEFAULT_EVERY_HOURS,
            )
            return DEFAULT_EVERY_HOURS

    def due_for_auto_snapshot(self, *, now: datetime | None = None) -> bool:
        """该不该打一份自动快照（方案 §3.2 的判据）。

        判据是"**距最近一条快照记录超过 N 小时**"——**不论那一份成败**：``uploaded`` 固然算，
        ``failed`` / ``discarded`` 也算。理由是这个判据问的只有一件事："**这台机器最近打过一份
        快照吗**"（打快照是**本地**动作，它成没成是另一件事）。而"有没有备上去"那个问题由
        :meth:`backlog` 如实回答，一个字不改——**两只钟、两个问题，别拿一只替另一只**。

        为什么 ``failed`` 必须持有这只钟（这是一个实测过的坑）：立即尝试失败之后那一行就是
        ``failed``，若把它排除在外，下一轮（5 分钟后）判据看不见它 → 又算"到期" →
        每个节拍重打一份整库快照、上限反复丢弃。断网一天 = 288 份快照的 CPU 与磁盘空转，
        界面还会一路重复"有 N 份没备上去"。

        ``discarded`` 同理算"打过"：它是被**本地上限**丢掉的，而那台机器确实在那一刻打过一份。

        一条记录都没有时（新装、表被清过）**算到期**——"从来没有打过"显然比 N 小时更久。

        ``every_hours() == 0``（关）或没有 ``snapshotter`` 时永远是 ``False``。
        """
        hours = self.every_hours()
        if hours <= 0 or self._snapshotter is None:
            return False
        moment = now or self._now()
        recent = self._queue.list_backup_snapshots(newest_first=True, limit=1)
        if not recent:
            return True
        return moment - recent[0].created_at >= timedelta(hours=hours)

    # -------------------------------------------------------------- 对外：一次节拍

    def sweep(self) -> SweepReport:
        """跑一轮节拍：**自动快照 → 逐张补传 → 收上限**。

        顺序是刻意的：自动快照先落地（它一入队就是"到点可传"的一行），于是补传那一轮
        顺手就把它传出去（不然它得等到下一个 5 分钟）。上限放最后：刚传成的那一份已经
        不占本地空间了，不该被当成"还压着盘"的那一批。

        **拿不到独占锁就跳过**（``skipped=True``）：一次只有一个节拍在跑，
        这就是"不做并发上传"（§3.3 最后一行）的落点。
        """
        if not self._pass.acquire(blocking=False):
            logger.debug("备份补传：上一轮还没跑完，跳过这一轮")
            return SweepReport(skipped=True)
        try:
            return self._pass_once()
        finally:
            self._pass.release()

    # -------------------------------------------------------------- 内部：一轮

    def _pass_once(self) -> SweepReport:
        auto_id = ""
        auto_error = ""
        if self.due_for_auto_snapshot():
            try:
                auto_id = self._take_auto_snapshot()
            except Exception as exc:  # 打不出来不该让这一轮白跑（上传那一半照做）
                auto_error = str(exc).strip() or type(exc).__name__
                logger.exception("自动快照没打出来：%s", auto_error)

        uploaded = failed = 0
        now = self._now()
        due = self._queue.list_backup_snapshots(
            states=BACKUP_UNFINISHED_STATES, due_before=now, limit=None
        )
        for record in due:  # 最旧的在前：队列按到达顺序排空
            if self._attempt(record):
                uploaded += 1
            else:
                failed += 1

        return SweepReport(
            uploaded=uploaded,
            failed=failed,
            discarded=self._enforce_cap(),
            auto_snapshot=auto_id,
            auto_error=auto_error,
        )

    def _attempt(self, record: BackupSnapshotRecord) -> bool:
        """传一份：**先置 uploading、再试、按结果落终态或退避**。

        ``uploading`` 那一档不是装饰：它让"上一次试到一半进程被杀了"这件事在库里留痕，
        启动时那一次复位（:meth:`recover`）才有东西可复位。
        """
        pending = PendingUpload(
            snapshot_id=record.id,
            blob_path=Path(record.blob_path),
            blob_bytes=record.blob_bytes,
            sha256=record.sha256,
            manifest_json=record.manifest_json,
        )
        self._queue.mark_backup_snapshot(record.id, "uploading")
        try:
            receipt = self._uploader.upload(pending)
        except Exception as exc:
            reason = str(exc).strip() or type(exc).__name__
            delay = _backoff_seconds(record.attempts + 1)
            self._queue.mark_backup_snapshot(
                record.id,
                "failed",
                last_error=reason,
                bump_attempts=True,
                next_attempt_at=self._now() + timedelta(seconds=delay),
            )
            logger.warning(
                "备份补传失败（第 %d 次，%d 秒后再试）：%s —— %s",
                record.attempts + 1,
                delay,
                record.id,
                reason,
            )
            return False

        now = self._now()
        self._queue.mark_backup_snapshot(
            record.id,
            "uploaded",
            last_error="",
            clear_next_attempt=True,
            uploaded_at=now,
            remote_device_id=receipt.device_id if receipt else None,
            remote_snapshot_id=receipt.snapshot_id if receipt else None,
        )
        self._delete_blob(record)
        logger.info("备份已补传：%s", record.id)
        return True

    def _take_auto_snapshot(self) -> str:
        """打一份 ``auto`` 快照并登记（**落在 pending 目录**，所以入队不再搬文件）。"""
        maker = self._snapshotter
        if maker is None:  # due_for_auto_snapshot 已经挡过一次，这里是类型上的收口
            return ""
        result = maker.create(into=self._pending_dir(), kind="auto", created_at=self._now())
        self._register(result, kind="auto", blob_path=result.blob_path)
        logger.info("自动快照已入队：%s", result.snapshot_id)
        return result.snapshot_id

    def _register(
        self, result: SnapshotResult, *, kind: str, blob_path: Path
    ) -> BackupSnapshotRecord:
        """写一行 ``pending``（``next_attempt_at=None`` = 立即到期），**顺手守一次上限**。

        上限在这里也守一道（不只是节拍里那一处）：入队那一步可能紧接着就失败
        （``attempt=True`` 会立刻试一次），而"盘上不许压超过 N 份"这条纪律不该等下一个
        5 分钟——用户连点几次「立即备份」时，压盘的是那几次入队。
        """
        record = BackupSnapshotRecord(
            id=result.snapshot_id,
            created_at=_created_at_of(result),
            kind=kind,
            state="pending",
            sha256=result.blob_sha256,
            blob_path=str(blob_path),
            blob_bytes=result.blob_bytes,
            manifest_json=result.manifest_bytes.decode("utf-8"),
        )
        self._queue.put_backup_snapshot(record)
        self._enforce_cap()
        return record

    def _enforce_cap(self) -> int:
        """pending 上限（3 份 / 1 GiB）：超限的按**最旧**标 ``discarded`` 并删包。

        "按最旧"是这样落地的：从**最新**往旧数，装不下的那几份就是最旧的（于是用户
        按了「立即备份」得到的那一份永远留得住，被丢掉的是压在盘上最久的那些）。
        被丢的每一份都写 ``last_error='local_pending_cap'`` 并留在表里——**如实报，不静默**：
        "有 N 份没备上去"这句话的另一半就在这里。
        """
        rows = self._queue.list_backup_snapshots(
            states=BACKUP_UNFINISHED_STATES, newest_first=True, limit=None
        )
        kept_count = 0
        kept_bytes = 0
        dropped = 0
        for record in rows:
            fits = (
                kept_count + 1 <= PENDING_LIMIT_COUNT
                and kept_bytes + record.blob_bytes <= PENDING_LIMIT_BYTES
            )
            if fits:
                kept_count += 1
                kept_bytes += record.blob_bytes
                continue
            self._queue.mark_backup_snapshot(
                record.id,
                "discarded",
                last_error=PENDING_CAP_ERROR,
                clear_next_attempt=True,
            )
            self._delete_blob(record)
            dropped += 1
            logger.warning(
                "待传队列超过本地上限（%d 份 / %d 字节），丢掉最旧的一份：%s",
                PENDING_LIMIT_COUNT,
                PENDING_LIMIT_BYTES,
                record.id,
            )
        return dropped

    def _delete_blob(self, record: BackupSnapshotRecord) -> None:
        """删掉本地那份包（上传成功与丢弃都走这里，§3.2）。

        ``missing_ok``：包被人手工删过不该让状态推进失败——那一行要表达的是
        "这一份的去向"，不是"这个文件现在在不在"。
        """
        if not record.blob_path:
            return
        Path(record.blob_path).unlink(missing_ok=True)

    def _pending_dir(self) -> Path:
        return self._data_dir.joinpath(*PENDING_DIR)

    # -------------------------------------------------------------- 内部：那一个线程

    def _ensure_worker(self) -> None:
        """懒建补传线程（一个进程一个，照 ``services/kb_cache.py`` 那一处的手法）。"""
        with self._guard:
            if self._stopped.is_set():
                return
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
            self._worker = worker
            worker.start()

    def _loop(self) -> None:
        """节拍循环：等 5 分钟或被叫醒一次，然后跑一轮。

        **绝不因一次异常而终结**（照 ``kb_cache._work`` 那段）：线程死了的现象是
        "备份悄悄不再补传"——没有报错、没有日志、界面也照旧，比一次失败难查得多。
        异常只留在那一轮里（``logger.exception`` 留证据），下一轮照跑。
        """
        while not self._stopped.is_set():
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()
            if self._stopped.is_set():
                break
            try:
                self.sweep()
            except Exception:  # 线程绝不因一轮失败而退出
                logger.exception("备份补传这一轮异常退出（线程继续）")


def _backoff_seconds(attempts: int) -> int:
    """第 ``attempts`` 次失败之后等多久（``attempts`` 是**含这一次**的失败次数）。

    ``min(…, len(BACKOFF_SECONDS))`` 就是"封顶 1 小时"：第 5 次之后一直是最后一个值，
    而不是接着往上翻（指数增长到一天之后，用户联网那一刻也不会去试了）。
    """
    index = min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1
    return BACKOFF_SECONDS[index]


def _created_at_of(result: SnapshotResult) -> datetime:
    """这一份是什么时候打的：**manifest 里那个 ``created_at``**（包里写着的那一个）。

    不从 ``snapshot_id`` 里反解：那是**约定**（设备 id 自己就带短横，"切开取中间那段"
    必然错），而 manifest 里那串是打包器写下来的事实（UTC ``YYYY-MM-DDTHH:MM:SSZ``）。
    解析不了就退到"现在"——队列是补做的东西，一个时刻的精度不值得让入队失败。
    """
    try:
        moment = datetime.fromisoformat(result.manifest.created_at)
    except ValueError:
        logger.warning(
            "manifest 里的 created_at 读不出来（%r），这一行按现在算", result.manifest.created_at
        )
        return datetime.now(UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
