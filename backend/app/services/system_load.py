"""负载面板的数据源：把"这台机器现在有多忙"变成几个可核查的数（§12.115）。

**要解决的问题**：用户看到的是"后台跑得慢"，而慢有四种完全不同的原因，界面上
当时一个都看不到：

| 现象 | 真实原因 | 面板上对应哪个数 |
|------|----------|------------------|
| 任务一直排队不开始 | 消费者不够（默认只 1 个），或压根没起 | 并发槽位、队列深度 |
| 解析/向量化变慢 | 机器 CPU 或内存吃满 | CPU%、内存占用 |
| 云端解析忽然不动 | MinerU 当日额度用尽 → **降级排队**（不是报错） | 今日已用页数 / 额度 |
| 进程本身在膨胀 | 进程常驻内存（切词、向量都在进程内） | 本进程 RSS |

**为什么由后端算**：CPU/内存是**服务端**的资源（浏览器看到的那个数字是用户自己
笔记本的，与后台快慢无关）；队列深度与额度只有后端有。前端只负责画。

**为什么把 psutil 关在一个 HostProbe 后面**：一是可测——真实 `psutil.cpu_times()`
在 CI 上每毫秒都在变，用例没法断言百分比，只能断言"算了、且在 0~100 之间"；
注入一个假探针就能断言精确的 25%。二是 CPU% 必须**按两次采样之差**算，
即"服务实例自己持有上一次采样"，这层状态放在这里最清楚。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.parsers.mineru_cloud import MinerUCloudParser
from app.services.observability import OVERDUE_AFTER, ObservabilityService
from app.storage.base import StoreBundle

__all__ = [
    "HardwareLoad",
    "HostProbe",
    "LoadSnapshot",
    "ParserQuota",
    "PsutilProbe",
    "QueueLoad",
    "SystemLoadService",
]

logger = logging.getLogger(__name__)

CONFIG_CACHE_SECONDS = 30.0
"""运行期配置的短缓存时长。见 ``_mineru_configured_cached``。"""

MIN_CPU_SAMPLE_GAP = 0.2
"""两次 CPU 采样至少隔这么久才算得动。

页面上每 2 秒轮询一次，间隔天然够。设这个下限是为了防"前端连点刷新"时
拿着分母为 0 的两次采样去除——那会算出 0% 或除零，两种都是假数据。
间隔不够时**返回 ``None``**（界面显示"—"），不编一个数出来。
"""


class HostProbe(Protocol):
    """读本机资源。唯一实现是 :class:`PsutilProbe`，测试注入假的。"""

    def cpu_times(self) -> Any:
        """累计 CPU 时间（含 ``idle``），跨平台由 psutil 归一。"""

    def cpu_count(self) -> int: ...

    def memory(self) -> tuple[int, int]:
        """``(已用字节, 总字节)``。"""

    def process_rss(self) -> int | None:
        """本进程常驻内存（字节）；读不到返回 ``None``。"""


class PsutilProbe:
    """``psutil`` 的薄封装。

    ``psutil`` 在 Windows / Linux / macOS 上都有实现，正是它让我们不必去读
    ``/proc/meminfo``——那套代码在开发机（Windows）上根本不成立。
    """

    def __init__(self) -> None:
        import psutil

        self._psutil = psutil
        self._process = psutil.Process(os.getpid())

    def cpu_times(self) -> Any:
        return self._psutil.cpu_times()

    def cpu_count(self) -> int:
        return int(self._psutil.cpu_count(logical=True) or 1)

    def memory(self) -> tuple[int, int]:
        mem = self._psutil.virtual_memory()
        return (int(mem.used), int(mem.total))

    def process_rss(self) -> int | None:
        try:
            return int(self._process.memory_info().rss)
        except Exception:  # pragma: no cover - 进程刚退出/权限受限时的兜底
            return None


@dataclass(slots=True)
class HardwareLoad:
    """机器与本进程的资源占用。"""

    cpu_percent: float | None
    """0–100。**首次采样为 ``None``**：没有上一次采样就不知道占了多少，
    这时显示"—"比显示"0%"诚实（0% 会被读成"机器很空闲"）。"""
    cpu_count: int
    memory_used_bytes: int
    memory_total_bytes: int
    memory_percent: float
    process_rss_bytes: int | None


@dataclass(slots=True)
class QueueLoad:
    """队列深度与并发槽位。"""

    running: int
    pending: int
    slots: int
    """并发上限（``KYLAB_WORKER_CONCURRENCY``）。"""
    pending_by_kind: dict[str, int] = field(default_factory=dict)
    """排队任务按类型分布。**这是最可操作的一个数**：实测一次积压里
    28/30 条都是"出题"，那就该关掉或调小出题，而不是加机器。"""
    oldest_pending_seconds: float | None = None
    """最老的排队任务等了多久（``None`` = 队列是空的）。"""
    stalled: int = 0
    overdue: int = 0


@dataclass(slots=True)
class ParserQuota:
    """云端解析器的当日额度。"""

    parser_name: str
    configured: bool
    """令牌配了没。没配时下面三个数一律为 0，界面显示"未配置"。"""
    pages_used: int = 0
    calls: int = 0
    daily_quota: int = 0

    @property
    def remaining(self) -> int:
        return max(self.daily_quota - self.pages_used, 0)

    @property
    def exhausted(self) -> bool:
        """额度用尽。**它不是错误状态**：云端只是不再优先处理，任务会继续
        但变慢——界面要把这句写出来，否则用户以为卡住了。"""
        return self.configured and self.daily_quota > 0 and self.pages_used >= self.daily_quota


@dataclass(slots=True)
class LoadSnapshot:
    """面板要的全部数据。"""

    hardware: HardwareLoad
    queue: QueueLoad
    quota: ParserQuota
    sampled_at: datetime


class SystemLoadService:
    """装配硬件探针 + 队列 + 云端额度。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        concurrency: int = 1,
        mineru_quota_pages: int = 1000,
        mineru_configured: Callable[[], bool] | None = None,
        observability: ObservabilityService | None = None,
        host: HostProbe | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._stores = stores
        self._slots = max(1, int(concurrency))
        self._mineru_quota = max(0, int(mineru_quota_pages))
        self._mineru_configured = mineru_configured or (lambda: True)
        # **复用可观测性那个唯一的判据**：停滞 = 租约过期、逾期 = 等太久，
        # 都是 ``ObservabilityService.assess`` 的判断。在这里另写一套阈值
        # 就等于同一件事有两种说法，而"哪一套才对"没人说得清。
        self._observability = observability or ObservabilityService(stores)
        # 探针**惰性构造**：装配服务时不该去碰 psutil 的进程句柄，
        # 也不该让"没装 psutil"变成启动失败——面板挂了不该拖垮整个 API。
        self._host = host
        self._now = now or (lambda: datetime.now(UTC))
        self._cpu_sample: tuple[float, float, float] | None = None
        self._lock = threading.Lock()
        # "配没配 MinerU"的短缓存（§12.116）。它要读运行期配置（一条 SQL ≈ 6ms，
        # 成本几乎全在往返上），而面板每 2 秒问一次；令牌不会秒级变化，
        # 缓存 30 秒既省掉这笔固定税，又不会让"刚填完令牌"看起来没生效。
        self._mineru_checked_at: float = 0.0
        self._mineru_is_configured: bool = False

    # ------------------------------------------------------------------ 对外

    def snapshot(self) -> LoadSnapshot:
        return LoadSnapshot(
            hardware=self._hardware(),
            queue=self._queue(),
            quota=self._quota(),
            sampled_at=self._now(),
        )

    # ------------------------------------------------------------------ 硬件

    def _hardware(self) -> HardwareLoad:
        """读硬件。**任何一步失败都退化成"不知道"，而不是 500**。

        这不是防御性编程的洁癖：面板上另外两块（队列、云端额度）是独立的信息，
        而"看负载"这个动作本身挂了会让整个任务中心打不开——为了一个附带的展示
        牺牲主功能，代价完全不成比例。
        """
        try:
            host = self._probe()
            cpu_percent = self._cpu_percent(host)
            count = host.cpu_count()
            used, total = host.memory()
            rss = host.process_rss()
        except Exception:
            logger.warning("读取本机负载失败", exc_info=True)
            return HardwareLoad(None, 1, 0, 0, 0.0, None)

        percent = (used / total * 100) if total > 0 else 0.0
        return HardwareLoad(
            cpu_percent=cpu_percent,
            cpu_count=count,
            memory_used_bytes=used,
            memory_total_bytes=total,
            memory_percent=round(percent, 1),
            process_rss_bytes=rss,
        )

    def _cpu_percent(self, host: HostProbe) -> float | None:
        """按**两次采样的差值**算占用率。

        不用 ``psutil.cpu_percent(interval=1)``：那会阻塞整整一秒——一个
        每 2 秒被轮询一次的端点不该每次都卡住事件循环一秒。
        也不用 ``interval=None``：它的"上次采样"按**线程**存，而请求可能落在
        不同的线程上（FastAPI 的线程池），那样算出来的分母是乱的。
        """
        times = host.cpu_times()
        # guest 已经计入 user（Linux），不扣掉就是重复计数；Windows 上没有这两个字段
        guest = float(getattr(times, "guest", 0.0)) + float(getattr(times, "guest_nice", 0.0))
        total = float(sum(times)) - guest
        busy = total - float(times.idle)

        moment = time.monotonic()
        with self._lock:
            previous = self._cpu_sample
            self._cpu_sample = (moment, total, busy)

        if previous is None:
            return None
        elapsed, last_total, last_busy = previous
        if moment - elapsed < MIN_CPU_SAMPLE_GAP:
            return None
        span = total - last_total
        if span <= 0:
            # 时钟回拨或平台没给累计时间：宁可说"不知道"
            return None
        return round(max(0.0, min((busy - last_busy) / span * 100, 100.0)), 1)

    def _probe(self) -> HostProbe:
        if self._host is None:
            self._host = PsutilProbe()
        return self._host

    # ------------------------------------------------------------------ 队列

    def _queue(self) -> QueueLoad:
        """队列深度：**一条聚合查询**，不把任务表搬进 Python（见 ``MetaStore.task_counts``）。

        负载面板每 2 秒被问一次（§12.115），而任务表只增不减。原先那条路会把每一行
        都构造出来再在 Python 里数，代价随任务总量线性涨，而这里要的只是几个数。

        逾期阈值**从可观测性服务拿**（``OVERDUE_AFTER``）：判定"等太久"的只能有一个
        数字，不能这里写一个、那里写一个。
        """
        load = QueueLoad(running=0, pending=0, slots=self._slots)
        try:
            moment = self._now()
            counts = self._stores.meta.task_counts(
                now=moment, overdue_before=moment - OVERDUE_AFTER
            )
        except Exception:
            logger.warning("读取任务队列失败", exc_info=True)
            return load

        load.running = counts.running
        load.pending = counts.pending
        load.stalled = counts.stalled
        load.overdue = counts.overdue
        load.pending_by_kind = dict(counts.pending_by_kind)
        if counts.oldest_pending_at is not None:
            age = (moment - _aware(counts.oldest_pending_at)).total_seconds()
            load.oldest_pending_seconds = round(max(0.0, age), 1)
        return load

    def _mineru_configured_cached(self) -> bool:
        """带 30 秒缓存地读"配没配令牌"（见构造函数里的说明）。"""
        moment = time.monotonic()
        if moment - self._mineru_checked_at < CONFIG_CACHE_SECONDS:
            return self._mineru_is_configured
        try:
            # 注意字段名不能与注入进来的回调重名（第一版就是这样炸的：
            # 缓存值把回调覆盖了，下一次调用 `bool` 直接 TypeError）
            self._mineru_is_configured = bool(self._mineru_configured())
        except Exception:  # pragma: no cover - 配置读取失败按"没配"处理
            logger.warning("读取 MinerU 配置失败", exc_info=True)
            self._mineru_is_configured = False
        self._mineru_checked_at = moment
        return self._mineru_is_configured

    # ------------------------------------------------------------------ 云端额度

    def _quota(self) -> ParserQuota:
        configured = self._mineru_configured_cached()

        quota = ParserQuota(
            parser_name=MinerUCloudParser.name,
            configured=configured,
            daily_quota=self._mineru_quota,
        )
        if not configured:
            return quota
        try:
            pages, calls = self._stores.meta.parser_page_usage(
                MinerUCloudParser.name, since=_day_start(self._now())
            )
        except Exception:
            logger.warning("统计云端解析用量失败", exc_info=True)
            return quota
        quota.pages_used = pages
        quota.calls = calls
        return quota


def _aware(moment: datetime | None) -> datetime | None:
    """补齐时区。

    列是 ``timestamptz``，读回来带时区；但 PG 会话时区、以及其他实现（本地文件
    存储的测试替身）可能给出 naive 时间——直接与 ``datetime.now(UTC)`` 相减会抛
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``。
    """
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def _day_start(now: datetime) -> datetime:
    """ "今天"的零点，按**本机时区**算，返回 UTC 时刻。

    MinerU 是东八区的服务，额度按它的自然日重置；本地机器在哪个时区就把零点算在
    哪个时区——比起硬编码 UTC+8，这样在别的部署里也说得通，且误差最多一天。
    """
    local = now.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
