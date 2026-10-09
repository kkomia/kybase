r"""**备份提供者客户端**（M5 阶段 4）：本机服务层打 NAS 备份端点的**唯一落点**。

## 它是哪一半的客户端

NAS 那半边（服务端）在 ``api/v1/backup.py`` 已经落地，七条端点；这一侧是**客户端**——
本机档自己就是那份备份的来源（快照在本机打、擦洗在本机做），它的角色是"把打好的那份
交给 NAS 保管、把 NAS 上有什么取回来"。方法的落点逐条对齐方案 §1.3 / §3.4：

| 能力 | 端点 | 这个方法 |
| --- | --- | --- |
| 连通性 + 能力集 + 设备与额度 | ``GET /backup/handshake`` | :meth:`BackupProviderClient.status` |
| 上传一份快照（先体后单） | ``PUT …/{device}/{id}/blob`` ＋ ``/manifest`` |
  :meth:`BackupProviderClient.upload` |
| 恢复点清单（透传给本机端点） | ``GET /backup/snapshots`` |
  :meth:`BackupProviderClient.list_snapshots` |
| 删一个恢复点 | ``DELETE …/{device}/{id}`` |
  :meth:`BackupProviderClient.delete_snapshot` |
| 取一份快照体（阶段 5 恢复用） | ``GET …/{device}/{id}/blob`` |
  :meth:`BackupProviderClient.download_snapshot` |

**本轮的上传者就是它**：``services/backup_queue.py`` 的 ``SnapshotUploader`` 那份协议
（``upload(pending) -> UploadReceipt | None``）由本类满足，组合根把这一份注入队列
（``core/services.py``）。所以"先 blob 后 manifest"这条顺序住在这里——队列那一层只
说"把这一份传上去"，两次 PUT 与它们的顺序是这一层的事（方案 §3.3 的那一格）。

## 两条口径（与知识库那条同源，别"顺手统一"）

1. **地址与凭据一处权威 + 一处覆盖**（方案 §3.2）：地址 = 运行期键
   ``provider.backup.base_url`` → 引导级 ``Settings.server_url``（**壳里那台 NAS**——
   备份提供者默认就是它）；token **只有引导级一个来源**（``Settings.token``，
   与壳、前端、知识库提供者共用同一把钥匙），**不落库、不进日志**（R3/R14）。
   解析是**纯函数** :func:`resolve_backup_target`：``enabled`` 显式关 → 不配。
   **每次调用现取目标**：设置页改了地址，下一次上传/列表立刻生效，不用重启边车。
2. **探针绝不抛**（:meth:`status`）：连不上 / 凭据错 / 版本不认识都是 ``unavailable``
   + 一句原因，**不是 500**。这一条的调用方是 ``/local/backup`` 与 ``/local/backup/points``，
   而"NAS 断着"正是用户要看那一页的时刻——状态页本身打不开是最糟的形态（R5 同源）。

## 三态与"桶没建出来"是两件事（R5）

``state=ready`` 说的是"这道端点族通了、凭据有效"，**不是**"桶能用"：NAS 侧桶缺失时
握手仍然 200，能力位 ``capabilities.snapshot.available=false`` + 一句可执行的下一步。
所以 :class:`BackupProviderStatus` 把这两件事分开给：``available``（三态）与
:attr:`BackupProviderStatus.snapshot_available`（能不能真收快照）。
**不把后者折进前者**：折进去会让"NAS 活着但还没建桶"显示成"连不上 NAS"，
而下一步的动作完全不同（一边去 NAS 上建桶，一边去查网络）。

## 上传：流式、带 Content-Length、有校验

``PUT …/blob`` 发的是盘上那份 ``.tar.gz``（最大 2 GiB）：**用文件对象交给 httpx**，
它按 64 KiB 分块读（``IteratorByteStream`` 的 ``read`` 那一支）、并用 ``fileno()`` 算出
``Content-Length``——于是"整包不进内存"与"服务端能早退 413"两条同时成立（R6/R10）。
``?sha256=&bytes=`` 是**声明**，服务端边收边算来核对；不符它会 400 且一个字节都不落桶。

响应三态照抄服务端的契约（``api/v1/backup.py``）：**201 新建 / 200 同内容 no-op**
（都算成功——重放安全）、**409 抛**（同路径不同内容 / 超保留份数 / 超配额：append-only，
绝不覆盖、也绝不替用户删）、**403 抛**（只读 key 不能写）。**200 不当失败**：
它正是"上次传了一半、这次重来"那条重试路径的答案（方案 §1.2 规矩 1）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.services import remote_clients
from app.services.backup_queue import PendingUpload, UploadReceipt
from app.services.remote_clients import (
    RemoteClientError,
    RemoteRejectedError,
    RemoteUnavailableError,
    _auth_headers,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HANDSHAKE_TIMEOUT_SECONDS",
    "HANDSHAKE_TTL_SECONDS",
    "PROTOCOL_VERSION",
    "SETTING_BASE_URL",
    "SETTING_ENABLED",
    "STATE_READY",
    "STATE_UNAVAILABLE",
    "STATE_UNCONFIGURED",
    "BackupProviderClient",
    "BackupProviderStatus",
    "BackupTarget",
    "backup_enabled",
    "resolve_backup_target",
]

PROTOCOL_VERSION = 1
"""本机**认识**的备份提供者协议版本（与 ``api/v1/backup.py`` 的发布值同源，**只增**）。

客户端规则与知识库那条逐字一样（方案 §1.3）：对方报的版本**大于本机所知即判不可用**，
原因句子里带版本号，**绝不硬试**——把新协议的语义错误当成网络故障是最难查的一类错。
"""

HANDSHAKE_TTL_SECONDS = 30.0
"""握手结论的缓存时长（进程内存，**不落库**，与知识库那条同一个数）。

恢复点列表用**同一个 TTL**（方案 §3.4："进程内缓存 30s，与提供者状态同一 TTL 口径"）——
两个读面看到的东西因此不会一个新一个旧地打架。
"""

HANDSHAKE_TIMEOUT_SECONDS = 3.0
"""探针超时。比上传那个短两个量级：握手回答的是"能不能用"，它跑在请求线程里（§3.2）。"""

UPLOAD_TIMEOUT_SECONDS = 300.0
"""上传/下载的**单次操作**超时（不是总时长上限）。

一份 2 GiB 的快照在局域网里要传几十秒到几分钟，而 httpx 的超时是"每次读写操作"的
上限——所以给 5 分钟：慢链路不会被误杀，真挂死的连接也不会让补传线程永远吊在那一份上。
"""

_CHUNK = 1024 * 1024
"""下载时一次读多少字节（1 MiB：既不进内存，也够少几次系统调用）。"""

_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
"""路径段的形状（与 ``api/v1/backup.py`` 的 ``_SEGMENT`` **同一份规则**）。

两处各写一份是有意的（客户端不该 import 服务端模块），而它是**同一串正则**：
``device_id`` 是 UUID v4、``snapshot_ts`` 是 ``YYYY-MM-DDTHH-MM-SSZ``（冒号已换成短横）。
本机这一侧拿它核对自己拼出来的两段——拼错了要在**发请求之前**发现。
"""

STATE_UNCONFIGURED = "unconfigured"
STATE_UNAVAILABLE = "unavailable"
STATE_READY = "ready"
"""三态（与知识库那条同一词汇）：``unconfigured`` = 没配 / 显式关；``unavailable`` = 配了
但连不上 / 凭据错 / 版本不认识；``ready`` = 这道端点族通了。**没有第四态**。"""

SETTING_ENABLED = "provider.backup.enabled"
SETTING_BASE_URL = "provider.backup.base_url"
"""两个运行期键（落本机库 ``app_settings``，与知识库那两个同一处置）。

**凭据不在这里**（R3/R14）：token 只有引导级一个来源，本机库里存不下它，
这一对白名单键（``PATCH /local/backup``）也就无从写入。
"""

DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
"""``provider.backup.enabled`` 的"关"档（认这几种写法，大小写不敏感）。

**默认（没有这个键时）是开**：地址在就接上去（与知识库那条同一条口径）。
"""

UNCONFIGURED_REASON = (
    "这台机器还没接备份提供者：壳里的 server 与 provider.backup.base_url 都是空的。"
    "在「备份」里填一个地址（或先在界面里登录那台 NAS、把地址交给桌面壳）。"
)
"""``unconfigured`` 的那句人话 + 下一步（两种"不在"各一句）。"""

DISABLED_REASON = (
    "备份提供者被关掉了（provider.backup.enabled=0）。要接回来，在「备份」里打开它。"
    "**关掉不影响本机打快照**：快照照旧落在 backup/pending/ 里等着传。"
)
"""``unconfigured`` 的另一种成因，单独一句（"关掉"与"没填地址"是两件事）。

后半句不是装饰：备份是**本地动作**，断网/关掉提供者时"立刻备份"仍然该能打
（方案 §7 A 第二行的判据），用户得知道关掉这一档只影响"传不传得出去"。
"""


# ---------------------------------------------------------------- 地址与凭据解析


@dataclass(frozen=True, slots=True)
class BackupTarget:
    """备份提供者目标：**一个地址 + 一把钥匙**（解析的结果，不是配置本身）。"""

    base_url: str
    token: str
    configured: bool
    reason: str = ""

    @property
    def credential(self) -> str:
        """凭据状态（``configured`` / ``missing``）——**只看有没有，不回显**。"""
        return "configured" if self.token else "missing"


def backup_enabled(get_setting: Callable[[str], str]) -> bool:
    """``provider.backup.enabled`` 那一栏现在是不是开着——**唯一判据**。

    两处要用它，所以抽成一个函数：:func:`resolve_backup_target`（显式关 → 不配地址）
    与 ``GET /local/backup`` 的 ``provider.enabled``（界面要**读得到**这一栏）。
    抄第二份的典型后果是"界面说开着、解析说关着"——那种对不上正是这一处要防的。

    口径（与知识库那条一致）：**没有这个键 = 开**（地址在就接上去）；命中
    :data:`DISABLED_VALUES`（``0`` / ``false`` / ``no`` / ``off``，大小写不敏感、
    两端空白不算）才算关。
    """
    return (get_setting(SETTING_ENABLED) or "").strip().lower() not in DISABLED_VALUES


def resolve_backup_target(settings: Settings, get_setting: Callable[[str], str]) -> BackupTarget:
    """地址与凭据的**唯一解析处**（纯函数：只读入参，不打网络、不读写库）。

    四路解析（与知识库那条同形，只是继承源少一层——备份没有"另一台 NAS"这一档）：

    - ``provider.backup.enabled`` 显式关 → **不配**（连地址都不再解析）；
    - 地址：运行期覆盖 ``provider.backup.base_url`` → ``Settings.server_url``
      （**壳里那台 NAS**，也是默认档：备份提供者默认就是登录的那一台）；
    - 凭据：``Settings.token``（**只有引导级这一个来源**，与壳/前端/知识库共用同一把）；
    - 一个地址都没有 → ``configured=False`` + 那句人话。

    纯函数的意义：这四条分支能被用例逐个钉住，而不用先跑起一台 NAS。
    """
    if not backup_enabled(get_setting):
        return BackupTarget("", (settings.token or "").strip(), False, DISABLED_REASON)
    base = (get_setting(SETTING_BASE_URL) or "").strip() or (settings.server_url or "").strip()
    token = (settings.token or "").strip()
    if not base:
        return BackupTarget("", token, False, UNCONFIGURED_REASON)
    return BackupTarget(base.rstrip("/"), token, True)


# ------------------------------------------------------------------ 状态（握手）


@dataclass(frozen=True, slots=True)
class BackupProviderStatus:
    """一次握手的结论（``GET /backup/handshake`` 的**本机侧形状**）。

    ``ready`` 时才有能力集 / 设备清单那两段：不 ready 时它们不是"空"，而是**根本没有**
    （回了空对象，界面就得猜"是没探到还是真没有"）——与 ``ProviderStatus`` 同一条纪律。

    两条判据**刻意分开**（见模块头 R5 那一段）：

    - :attr:`available`：三态里的 ``ready``（端点族通了、凭据有效）；
    - :attr:`snapshot_available`：能力位 ``capabilities.snapshot.available``
      （桶建好没有、对象存储连得上没有）。NAS 侧桶缺失时前者为真、后者为假，
      而 :attr:`snapshot_reason` 里是那句"下一步敲什么"。
    """

    state: str
    reason: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    base_url: str = ""
    credential: str = "missing"
    protocol_version: int | None = None
    app_version: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    devices: tuple[dict[str, Any], ...] = ()

    @property
    def available(self) -> bool:
        """``state == ready``（端点族通了）。**这不是"桶能用"**，见类说明。"""
        return self.state == STATE_READY

    @property
    def snapshot_caps(self) -> dict[str, Any]:
        """``capabilities.snapshot`` 那一段（不 ready 时是空字典）。"""
        section = self.capabilities.get("snapshot")
        return section if isinstance(section, dict) else {}

    @property
    def snapshot_available(self) -> bool:
        """这台提供者现在**能不能真收快照**（R5 的能力位）。"""
        return self.available and bool(self.snapshot_caps.get("available"))

    @property
    def snapshot_reason(self) -> str:
        """不能收快照时的**那一句可执行的下一步**（服务端给的，原样透传）。"""
        return str(self.snapshot_caps.get("unavailable_reason") or "")

    def to_payload(self) -> dict[str, Any]:
        """→ 一个可以原样回给前端的 dict（时间转 ISO 串，**不含凭据**）。

        与 ``ProviderStatus.to_payload()`` 同一条手法：**形状只在这里拼一份**，
        端点那边用响应模型 ``model_validate`` 过一道（不逐字段接——接一遍就是另写一份
        形状，两处迟早分叉）。不 ready 时那四段**键都不出现**，由端点的
        ``response_model_exclude_unset`` 保持这个语义。
        """
        payload: dict[str, Any] = {
            "state": self.state,
            "available": self.available,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
            "base_url": self.base_url,
            "credential": self.credential,
            # 这两位**三态都给**：界面画"能备份吗"那一条靠它，而不是自己去挖 capabilities
            "snapshot_available": self.snapshot_available,
            "snapshot_reason": self.snapshot_reason,
        }
        if self.available:
            payload.update(
                {
                    "protocol_version": self.protocol_version,
                    "app_version": self.app_version,
                    "capabilities": self.capabilities,
                    "devices": list(self.devices),
                }
            )
        return payload


class BackupProviderClient:
    """备份提供者的客户端（本机档那一侧的**唯一**出口，同时是队列的上传者）。

    ``get_setting`` / ``settings`` / ``transport`` 与 ``KnowledgeProviderClient``
    逐字同形（生产给 ``RuntimeConfigService.get`` 与引导级 ``Settings``；用例注入
    ``httpx.MockTransport``）。``clock`` 是单调钟（TTL），``now`` 是墙上钟（时间戳）——
    两把钟都要，理由与知识库那条一样。

    线程安全：``status()`` / ``list_snapshots()`` 会写缓存，最坏情况是两个线程各探一次
    （都不改状态）。这一层刻意**不加锁**——探测是只读的，多探一次的代价远小于一把锁。
    """

    def __init__(
        self,
        *,
        get_setting: Callable[[str], str] | None = None,
        settings: Settings | None = None,
        transport: Any = None,
        ttl: float = HANDSHAKE_TTL_SECONDS,
        handshake_timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
        transfer_timeout: float = UPLOAD_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._get_setting = get_setting or (lambda key: "")
        self._transport = transport
        self._ttl = ttl
        self._handshake_timeout = handshake_timeout
        self._transfer_timeout = transfer_timeout
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._status: BackupProviderStatus | None = None
        self._checked: float = 0.0
        self._points: tuple[tuple[str, int, int], float, dict[str, Any]] | None = None
        """恢复点列表的缓存：``(查询键, 取到的时刻, 那一份响应)``。

        **只留最近一次那个查询形状**（键 = ``(device_id, limit, offset)``）：这台机器的
        列表读只有一个调用方（``/local/backup/points``），它每次问的形状是稳定的；
        为三种形状各留一份只会让"这一份是什么时候取的"更难说清。
        """

    # -------------------------------------------------------------- 地址与状态

    def target(self) -> BackupTarget:
        """这次调用该打哪儿、用哪把钥匙（**每次现取**：设置改了立刻生效）。"""
        return resolve_backup_target(self._settings, self._get_setting)

    def status(self, refresh: bool = False) -> BackupProviderStatus:
        """握手的结论（缓存 **30s**，进程内存；``refresh=True`` 强制重探）。

        **不在启动时挡路**：首次被问到才探（与知识库那条同一条口径）。探针绝不抛
        （见模块头第 2 条），所以这一条永远给得出一个三态结论。
        """
        now = self._clock()
        if not refresh and self._status is not None and now - self._checked < self._ttl:
            return self._status
        status = self._probe()
        self._status = status
        self._checked = now
        return status

    # ------------------------------------------------------------------ 上传

    def upload(self, pending: PendingUpload) -> UploadReceipt | None:
        """把一份快照传上去：**先 blob 后 manifest**（方案 §3.3）。

        - blob 是盘上那份 ``.tar.gz``（``pending.blob_path``），声明
          ``?sha256=pending.sha256&bytes=pending.blob_bytes``——服务端边收边算来核对；
        - manifest 是包里第一成员的那串字节（``pending.manifest_bytes``），
          **它是完成标记**：没有它，NAS 上那个恢复点不算存在（枚举只认有清单的）；
        - 两次 PUT 都幂等（201 新建 / 200 同内容 no-op，都算成功），所以重试安全；
          409 / 403 / 400 / 413 / 5xx 一律**抛**——队列把消息原样记进 ``last_error``。

        返回**服务端确认下来的那一对坐标**（``device_id`` + 路径里那段 ``snapshot_id``）；
        服务端没回可解析的坐标就回 ``None``（"上传者没报坐标"是合法状态，
        队列那边两个远端列因此留空——本机不编一个，R12 同源）。
        """
        target = self._target_or_raise("上传快照")
        device, snapshot = _coordinates(pending)
        blob = Path(pending.blob_path)
        if not blob.is_file():
            raise RemoteClientError(
                f"要上传的那份快照不在本机了：{blob}（本机队列与盘上的包不一致）"
            )

        # ① 快照体（大件）：文件对象交给 httpx —— 64 KiB 分块读 + 由 fileno 算出 Content-Length
        with blob.open("rb") as handle:
            response = self._send(
                "上传快照体",
                target=target,
                method="PUT",
                path=f"/backup/snapshots/{device}/{snapshot}/blob",
                params={"sha256": pending.sha256, "bytes": str(pending.blob_bytes)},
                content=handle,
                headers={"Content-Type": "application/octet-stream"},
                timeout=self._transfer_timeout,
            )
        self._expect_written("上传快照体", response, target, device=device, snapshot=snapshot)

        # ② 清单（小件，完成标记）：**必须在上一步之后**，服务端也会拒"清单先到"
        response = self._send(
            "上传快照清单",
            target=target,
            method="PUT",
            path=f"/backup/snapshots/{device}/{snapshot}/manifest",
            content=pending.manifest_bytes,
            headers={"Content-Type": "application/json"},
            timeout=self._transfer_timeout,
        )
        self._expect_written("上传快照清单", response, target, device=device, snapshot=snapshot)

        payload = _json_or_none(response)
        if payload is None or not str(payload.get("snapshot_id") or ""):
            return None
        return UploadReceipt(device_id=device, snapshot_id=str(payload["snapshot_id"]))

    # ------------------------------------------------------------------ 列表

    def list_snapshots(
        self,
        *,
        device_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any] | None:
        """列恢复点（``GET /backup/snapshots``，缓存 30s，与状态同一 TTL 口径）。

        **没取到就回 ``None``**（未配 / 连不上 / 被拒 / 回的不是 JSON）：调用方
        （``/local/backup/points``）拿 ``status()`` 的结论配一句话如实回，**不是 500**。
        这一条不抛，是因为它的调用方是页面——"这一页打不开"比"这一页说连不上"糟得多。

        先看状态：``state != ready`` 时**一个请求都不发**（连不上的 NAS 不该被每开一次
        页面就打一次）。列表失败**不写负缓存**：那会让"刚恢复的网络"多等 30 秒。
        """
        status = self.status()
        if not status.available:
            return None
        key = (device_id or "", int(limit), int(offset))
        now = self._clock()
        if not refresh and self._points is not None:
            cached_key, cached_at, payload = self._points
            if cached_key == key and now - cached_at < self._ttl:
                return payload
        target = self.target()
        params = {"limit": str(limit), "offset": str(offset)}
        if device_id:
            params["device_id"] = device_id
        try:
            response = self._send(
                "列恢复点",
                target=target,
                method="GET",
                path="/backup/snapshots",
                params=params,
                timeout=self._handshake_timeout,
            )
        except RemoteClientError as exc:
            logger.warning("恢复点列表没取到：%s", exc)
            return None
        if response.status_code >= 400:
            logger.warning("恢复点列表没取到（HTTP %s）：%s", response.status_code, _body(response))
            return None
        payload = _json_or_none(response)
        if payload is None:
            return None
        self._points = (key, now, payload)
        return payload

    # ------------------------------------------------------------------ 下载 / 删除

    def manifest(self, device_id: str, snapshot_id: str) -> dict[str, Any] | None:
        """取一份恢复点的**清单**（``GET /backup/snapshots/{device}/{id}``，原样回）。

        M5 阶段 5 的 dry-run 用它：清单（≤1 MiB）里就有计数 / 被跳过项 / schema 版本 /
        ``redacted``，足够回答"会新建哪些会话、哪些产物不在包里、要重配几项凭据"——
        **不必把可能上 GB 的快照体先下下来**（那是"看清单再决定"的全部意义，也是
        NAS 那条清单 PUT 要求"先传快照体后传清单"的反面用法）。

        取不到（未配 / 连不上 / 被拒 / 没有这一份 / 回的不是 JSON 对象）→ ``None``：
        调用方（恢复的 dry-run）拿 ``status()`` 的结论配一句话如实回，**不是 500**。
        与 :meth:`list_snapshots` 同一条口径：这一层的读**不抛**。
        """
        status = self.status()
        if not status.available:
            return None
        target = self.target()
        try:
            response = self._send(
                "取恢复点清单",
                target=target,
                method="GET",
                path=f"/backup/snapshots/{device_id}/{snapshot_id}",
                timeout=self._handshake_timeout,
            )
        except RemoteClientError as exc:
            logger.warning("恢复点清单没取到：%s", exc)
            return None
        if response.status_code >= 400:
            logger.warning(
                "恢复点清单没取到（HTTP %s，%s/%s）：%s",
                response.status_code,
                device_id,
                snapshot_id,
                _body(response),
            )
            return None
        return _json_or_none(response)

    def download_snapshot(
        self, device_id: str, snapshot_id: str, dest: str | Path
    ) -> dict[str, Any]:
        """把一份快照体**流式**下到 ``dest``，并用 ``X-Kylab-Sha256`` 校验。

        阶段 5 的恢复用它（"下载 blob（流式）→ 校验 sha256（不符即中止，**绝不落位**）"，
        方案 §3.4 第 4 步）。校验不通过时**把刚写的那份删掉**再抛：留下一个内容不符的
        文件，等于把一次网络抖动变成一个"看起来像快照"的东西。

        服务端没给 ``X-Kylab-Sha256``（老版本 / 别的东西占了这条路）时**只报数不校验**，
        并在返回值里如实说 ``verified: false`` —— 不假装校验过。
        """
        target = self._target_or_raise("下载快照")
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        httpx = remote_clients._httpx()
        expected = ""
        digest = hashlib.sha256()
        total = 0
        try:
            with (
                httpx.Client(
                    base_url=target.base_url,
                    timeout=self._transfer_timeout,
                    transport=self._transport,
                    headers=_auth_headers(target.token),
                ) as client,
                client.stream(
                    "GET", f"/backup/snapshots/{device_id}/{snapshot_id}/blob"
                ) as response,
            ):
                if response.status_code >= 400:
                    raise _rejected(
                        "下载快照", response, target, device=device_id, snapshot=snapshot_id
                    )
                expected = (response.headers.get("X-Kylab-Sha256") or "").strip().lower()
                with path.open("wb") as sink:
                    for chunk in response.iter_bytes(_CHUNK):
                        if not chunk:
                            continue
                        sink.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
        except RemoteClientError:
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:  # 连不上 / 超时 / 盘写不下去：都不留半份
            path.unlink(missing_ok=True)
            raise RemoteUnavailableError(
                f"下载快照：没取完（{type(exc).__name__}）：{exc}"
            ) from exc

        got = digest.hexdigest()
        if expected and got != expected:
            path.unlink(missing_ok=True)
            raise RemoteUnavailableError(
                f"下载快照：落盘的字节与服务端声明的 sha256 不符（声明 {expected}，实收 {got}）"
                "——这一份已经删掉，绝不落位；请重试一次。"
            )
        return {"path": str(path), "bytes": total, "sha256": got, "verified": bool(expected)}

    def delete_snapshot(self, device_id: str, snapshot_id: str) -> int:
        """删一个恢复点（整份：快照体 + 清单一起走），返回实际删掉的**对象数**。

        ``404`` **不抛**：那是"这条路径上本来就没有"，如实回 ``0`` 比伪造一次成功
        或者炸一个 500 都好（调用方据此回"本来就没有"）。
        """
        target = self._target_or_raise("删除恢复点")
        response = self._send(
            "删除恢复点",
            target=target,
            method="DELETE",
            path=f"/backup/snapshots/{device_id}/{snapshot_id}",
            timeout=self._transfer_timeout,
        )
        if response.status_code == 404:
            return 0
        if response.status_code >= 400:
            raise _rejected("删除恢复点", response, target, device=device_id, snapshot=snapshot_id)
        payload = _json_or_none(response) or {}
        return int(payload.get("removed") or 0)

    # ------------------------------------------------------------------ 内部：网络

    def _target_or_raise(self, what: str) -> BackupTarget:
        """拿目标；没配就抛（调用方是队列与端点，它们都要"为什么"这句话）。"""
        target = self.target()
        if target.configured:
            return target
        raise RemoteUnavailableError(f"{what}不了：{target.reason}")

    def _probe(self) -> BackupProviderStatus:
        """真探一次（``status()`` 的缓存之外那一半）。**探针自己绝不抛**。

        三档与知识库那条同形：``unconfigured``（没配 / 关掉）、``unavailable``（连不上 /
        凭据错 / 版本不认识 / 回的不是握手体）、``ready``。
        """
        target = self.target()
        checked = self._now()
        if not target.configured:
            return BackupProviderStatus(
                state=STATE_UNCONFIGURED,
                reason=target.reason,
                checked_at=checked,
                base_url=target.base_url,
                credential=target.credential,
            )

        def unavailable(reason: str) -> BackupProviderStatus:
            return BackupProviderStatus(
                state=STATE_UNAVAILABLE,
                reason=reason,
                checked_at=checked,
                base_url=target.base_url,
                credential=target.credential,
            )

        try:
            payload = self._handshake(target)
        except RemoteClientError as exc:
            # **不在这里再拼一句"下一步"**（原来是 `f"{exc}。下一步：核对「备份」里的地址
            # 与网络。"`）：连不上那一条的提示语由 `_send` 拼——它知道"哪一步没发出去、
            # 发到哪儿"，是**唯一**该拼那句的地方。两处都拼就会出现"同一句话连出两遍、
            # 中间还多一个空句号"（阶段 8 的截图 08/09 就是那个现场：
            # `…无法连接。。下一步：… 。下一步：…`）。
            #
            # 拒绝类（401 / 404 / 5xx）与"回的不是握手体"那几档**本来就不该有那句**：
            # 它们说的是"到了、但对面说不"，下一步与"核对地址与网络"不是同一件事
            # ——各自的文案里已经写了该看什么（凭据 / 端点 / 升级）。
            return unavailable(str(exc))
        except Exception as exc:  # 探针绝不抛，理由见方法说明
            logger.warning("备份握手探测没能跑完（当不可用处理）", exc_info=True)
            return unavailable(f"握手没跑成（{type(exc).__name__}）：{exc}")

        version = payload.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return unavailable(
                f"握手响应里没有协议版本（{target.base_url}）："
                "对面回的不是备份提供者的握手体，地址可能指到了别的服务。"
            )
        if version > PROTOCOL_VERSION:
            # **绝不硬试**：新协议的语义可能完全不同，猜错了会被当成网络故障
            return unavailable(
                f"备份提供者的协议版本是 {version}，本机只认识到 {PROTOCOL_VERSION}"
                "（M5 的值）——请升级桌面端 / 本机后端，不要用旧客户端去猜新协议。"
            )
        return BackupProviderStatus(
            state=STATE_READY,
            checked_at=checked,
            base_url=target.base_url,
            credential=target.credential,
            protocol_version=version,
            app_version=str(payload.get("app_version") or ""),
            capabilities=_as_dict(payload.get("capabilities")),
            devices=tuple(item for item in payload.get("devices") or [] if isinstance(item, dict)),
        )

    def _handshake(self, target: BackupTarget) -> dict[str, Any]:
        """这一次握手（状态码分档在这里，因为"凭据"与"地址"是两句话）。"""
        response = self._send(
            "握手",
            target=target,
            method="GET",
            path="/backup/handshake",
            timeout=self._handshake_timeout,
        )
        code = response.status_code
        if code in (401, 403):
            raise RemoteRejectedError(f"备份提供者拒绝了这把凭据（HTTP {code}）：{_body(response)}")
        if code == 404:
            raise RemoteRejectedError(
                f"这个地址上没有备份握手端点（HTTP 404）：{target.base_url}/backup/handshake"
            )
        if code >= 500:
            raise RemoteUnavailableError(f"备份提供者出错了（HTTP {code}）：{_body(response)}")
        if code >= 400:
            raise RemoteRejectedError(f"备份提供者拒绝了握手（HTTP {code}）：{_body(response)}")
        payload = _json_or_none(response)
        return payload or {}

    def _send(
        self,
        what: str,
        *,
        target: BackupTarget,
        method: str,
        path: str,
        timeout: float,
        params: dict[str, str] | None = None,
        content: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发一次请求。**连不上 / 超时在这里就抛**——那是"不可用"，与状态码是两件事。

        ``content`` 可以是 ``bytes``（清单那种小件）或**文件对象**（快照体）：后者
        httpx 按 64 KiB 分块读、并用 ``fileno()`` 给出 ``Content-Length``——"不整包进内存"
        与"服务端能早退 413"两条因此同时成立（R6/R10）。文件对象的**关闭由调用方负责**
        （httpx 不关传进来的那个）。
        """
        httpx = remote_clients._httpx()
        try:
            with httpx.Client(
                base_url=target.base_url,
                timeout=timeout,
                transport=self._transport,
                headers=_auth_headers(target.token),
            ) as client:
                return client.request(method, path, params=params, content=content, headers=headers)
        except httpx.HTTPError as exc:
            # 这一条是**唯一**拼那句提示语的地方（"下一步"由知道"发到哪儿失败了"的这一层
            # 给出；状态那一层只把它原样透出去）。见 `_probe` 里那段说明。
            #
            # `_ended` 而不是 f"{exc}。"：Windows 自己的错误文本**已经带句号**
            # （`[WinError 10061] …无法连接。`），再拼一个就成了 `。。`
            # ——截图里那个空句号就是这么来的。
            raise RemoteUnavailableError(
                f"{what}：连不上备份提供者（{target.base_url}）——{_ended(str(exc))}"
                "下一步：核对「备份」里的地址与网络。"
            ) from exc

    def _expect_written(
        self,
        what: str,
        response: Any,
        target: BackupTarget,
        *,
        device: str,
        snapshot: str,
    ) -> None:
        """PUT 的响应分档：**201 新建 / 200 同内容 no-op 都算成功**，其余抛。"""
        code = response.status_code
        if code in (200, 201):
            logger.debug("%s：HTTP %s（%s/%s）", what, code, device, snapshot)
            return
        raise _rejected(what, response, target, device=device, snapshot=snapshot)


# ---------------------------------------------------------------- 内部小件


def _rejected(
    what: str, response: Any, target: BackupTarget, *, device: str, snapshot: str
) -> RemoteClientError:
    """4xx/5xx → 该抛哪一个（**409 与 403 各有一句自己的话**）。"""
    code = response.status_code
    body = _body(response)
    where = f"{device}/{snapshot}"
    if code == 409:
        return RemoteRejectedError(
            f"{what}：提供者拒收这一份（HTTP 409，{where}）：{body}"
            "——append-only：同路径不同内容永不覆盖，保留份数 / 配额超限也不会替你删一份。"
        )
    if code in (401, 403):
        return RemoteRejectedError(f"{what}：这把凭据没有这个权限（HTTP {code}）：{body}")
    if code == 413:
        return RemoteRejectedError(f"{what}：这一份超过提供者的单份上限（HTTP 413）：{body}")
    if code == 404:
        return RemoteRejectedError(f"{what}：提供者上没有这一份（HTTP 404，{where}）：{body}")
    if code >= 500:
        return RemoteUnavailableError(
            f"{what}：备份提供者出错了（HTTP {code}）：{body}（地址 {target.base_url}）"
        )
    return RemoteRejectedError(f"{what}：提供者拒绝了请求（HTTP {code}，{where}）：{body}")


def _ended(text: str) -> str:
    """给一句人话收尾：**已经有句号就不再加一个**。

    为什么需要它（截图 08/09 那个空句号的根因）：Windows 自己的错误文本**带句号**
    （``[WinError 10061] 由于目标计算机积极拒绝，无法连接。``），而我们在它后面还要接着
    写"下一步：…"——两处各加一个就成了 ``。。``。同一个动作在别处也会遇到
    （服务端回的那句话、``_body(response)`` 的片段都可能自带句号），所以收在这里。

    只认**句末**标点：句号 / 叹号 / 问号（含全角）。
    """
    cleaned = (text or "").strip()
    if not cleaned or cleaned.endswith(("。", "！", "？", ".", "!", "?")):
        return cleaned
    return f"{cleaned}。"


def _coordinates(pending: PendingUpload) -> tuple[str, str]:
    """从这份待传的东西里读出差旅要用的那一对坐标：``(device_id, snapshot_id)``。

    两段都**从清单里读**（那是打包器写下来的事实，也是包里第一成员），不猜：

    - ``device_id``：清单的 ``device_id``（R12：没有它就如实拒——绝不编一个）；
    - 路径里那段 ``snapshot_id``：清单的 ``snapshot_id`` 去掉 ``<device>-`` 前缀
      （方案 §1.2：路径段是 ``<ts>-<hash8>``）。**按前缀切而不是按短横切**——
      ``device_id`` 自己就是 UUID，短横切出来的一定是错的。

    两段都过一遍 ``_SEGMENT``（与服务端同一条规则）：拼错要在**发请求之前**发现，
    而不是让服务端回一个 400 之后才发现。
    """
    try:
        manifest = json.loads(pending.manifest_json)
    except ValueError as exc:
        raise RemoteRejectedError(f"这份快照的清单不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict):
        raise RemoteRejectedError("这份快照的清单不是 JSON 对象（应是一份 manifest）")
    device = str(manifest.get("device_id") or "").strip()
    if not device:
        raise RemoteRejectedError(
            "这份快照的清单里没有 device_id：这台机器还没有设备身份"
            "（先在桌面壳里登录一次），快照不该在没有身份的情况下打出来"
        )
    full = str(manifest.get("snapshot_id") or pending.snapshot_id).strip()
    prefix = f"{device}-"
    if not full.startswith(prefix):
        raise RemoteRejectedError(
            f"这份快照的 snapshot_id（{full}）与它的 device_id（{device}）对不上："
            "清单描述的不是这台设备的快照，传上去会把两边的账搅在一起。"
        )
    snapshot = full[len(prefix) :]
    if not _SEGMENT.match(device) or not _SEGMENT.match(snapshot):
        raise RemoteRejectedError(
            f"快照的坐标不合提供者的命名规则（device={device!r}，snapshot={snapshot!r}）："
            "命名规则见《备份提供者实施方案》§1.2（只允许字母数字点下划线短横）"
        )
    return device, snapshot


def _json_or_none(response: Any) -> dict[str, Any] | None:
    """把响应读成 dict；不是 JSON / 不是对象就 ``None``（**不抛**，调用方自己定档）。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _body(response: Any) -> str:
    """响应体里那句话（服务端给的文案比状态码有用得多）。取不到就给空串。"""
    try:
        text = response.text
    except Exception:  # 读流已经关了之类：排障信息不值得再抛一次
        return ""
    return " ".join((text or "").split())[:500]


def _as_dict(value: Any) -> dict[str, Any]:
    """只要字典（别的形状当空——能力集的形状由服务端定，客户端不猜）。"""
    return value if isinstance(value, dict) else {}
