r"""**按点恢复**（M5 阶段 5，方案 §3.4 的 11 步）。

把 NAS 上一份恢复点取回来、解包、**走 M2 那台导入机**写进本机库，再把记忆 / 设置 /
产物字节落位，最后给一份"哪几条没按预想办、为什么"的报告。

## 它与 M2 的关系：**复用，不重做**

会话那一条链**一个字都不重写**：`ConversationTransfer` → `LegacyImporter`（幂等键
``(source, conversation_id, source_updated_at_ms)``、三条覆盖规则、一次会话一个事务、
台账、回滚）。这一层只做两件事：

1. **换来源**：`LegacyImporter` 的来源抽成了 ``legacy_import.TransferSource``，
   这里给的是 :class:`SnapshotFileSource`（读解包出来的快照库，生成器一条一条给）；
2. **补上快照特有的三步**：包里的记忆 / 设置 / 产物字节（会话之外的东西 M2 没有）。

于是"恢复进度"与"回滚"**复用既有两个端点**（``GET /local/import/{batch}`` 与
``POST /local/import/{batch}/rollback``），台账那一行也**只有一条**
（``imports.source = "backup://<device>/<snapshot>"``）。

> **回滚的边界要说清**：那一条端点撤的是**会话**（新建的删掉、被替换的用快照恢复、
> 本机改过的保留——M2 那三条规则），而记忆 / 设置 / 产物这三块它管不着（M2 里没有它们，
> 台账里也没有"这三块原来长什么样"的记录）。恢复那一侧对应的是**默认不覆盖**：
> 记忆只补缺的、设置只补本机没有的键、产物字节同 Key 不同内容一律不覆盖——于是"撤一次
> 恢复"通常只需要撤会话。真要整体退回某一天，走下面那条"整机回滚"的路。

> 报告里那些"快照独有"的段落（记忆 / 设置 / 产物 / 要重配的凭据）**并进同一条台账的
> counts**（``counts["restore"]``）：轮询那个端点原样回 `counts_json`，所以界面上
> 看得到全部——不为一份报告新开端点，也不给台账加列。

## 11 步落在这里（方案 §3.4）

| 步 | 落点 |
| --- | --- |
| 1 选点 | :meth:`BackupRestorer.plan` / :meth:`BackupRestorer.restore` 的两个参数 |
| 2 dry-run | :meth:`BackupRestorer.plan`（**不碰本机库 / 记忆 / 产物**，只落 staging） |
| 3 恢复前的本地兜底 | :meth:`BackupRestorer._pre_restore`（落 ``restore-backup/<ts>/``，不入队） |
| 4 下载 + 校验 | ``download_snapshot``（流式 + ``X-Kylab-Sha256``，不符即中止、绝不落位） |
| 5 解包 | :meth:`BackupRestorer._unpack`（逐成员校验路径不越界） |
| 6 读快照库 | ``stores.snapshot.iter_snapshot_transfers``（只读打开，方言在存储层） |
| 7 走 M2 导入器 | :meth:`BackupRestorer._importer` + ``LegacyImporter.run``（``backup://…``） |
| 8 记忆 | :meth:`BackupRestorer._restore_memory`（**默认只补不覆盖**） |
| 9 设置 | :meth:`BackupRestorer._restore_settings`（只补本机没有的键；凭据不写） |
| 10 产物字节 | :meth:`BackupRestorer._restore_artifacts`（同 Key 同内容 skip / 不同内容报冲突） |
| 11 报告 | :class:`RestoreReport`（新建 / 替换 / 跳过（含原因）/ 要重配的凭据） |

## 五条安全栏（每一条都有用例钉着）

1. **sha256 校验**：由 ``download_snapshot`` 做（不符 → 删掉刚写的那份 + 抛）；
2. **解包路径不越界**：:meth:`BackupRestorer._unpack`（绝对路径 / ``..`` / 反斜杠 /
   软硬链接一律拒，写完再核一次"落点仍在 staging 里"）；
3. **冲突不覆盖**：本机已有的产物字节与包里那份**不同** → 如实报冲突并跳过（绝不静默覆盖，
   与 NAS 侧那条"同路径不同内容 409"同源）；
4. **凭据一个都不写**：设置只写"本机没有的键"，且 ``SECRET_KEYS`` 与
   ``provider.`` / ``model.`` / ``backup.`` 三个前缀一律跳过（包里本来就被擦洗干净，
   这一道是防漂）；
5. **``pre_restore`` 不入队**：它只落 ``restore-backup/<ts>/``，**不调** ``queue.enqueue``
   ——队列那边也会拒它（那里有一条显式的拒绝），两处同一条边界。

## "整机回滚到昨天"那条干净的路（§3.4 末尾）

**不做整库替换**（进程持有连接、WAL 在飞，在线换库文件是危险动作）。真要整机换回：

1. 停掉边车；
2. 把 ``<data_dir>/kylab.db*`` 挪到 ``restore-backup/<ts>/``；
3. 再跑一次恢复（此时库是空的 → 全量重建）。

CLI 的 ``--help`` 里写着这三步（见 :func:`_parser`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tarfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.exceptions import InvalidRequestError
from app.services.backup_provider import BackupProviderClient
from app.services.backup_snapshot import (
    BLOB_NAME,
    MEMBER_DB,
    MEMBER_FILES,
    MEMBER_MEMORY,
    MEMBER_WORKSPACE_FILES,
    BackupSnapshotService,
    SnapshotFormatError,
    parse_manifest,
)
from app.services.legacy_import import (
    LegacyImporter,
    LegacyImportError,
    TransferSource,
)
from app.services.runtime_config import SECRET_KEYS, RuntimeConfigService
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ARTIFACT_IN_WORKSPACE,
    SNAPSHOT_EXCLUDED_SETTING_PREFIXES,
    ConversationTransfer,
    ImportLedger,
    SnapshotDbView,
    SnapshotSource,
    StoreBundle,
)

__all__ = [
    "RESTORE_BACKUP_DIRNAME",
    "RESTORE_STAGING_DIRNAME",
    "BackupRestoreError",
    "BackupRestorer",
    "RestorePlan",
    "RestoreReport",
    "SnapshotFileSource",
    "main",
]

logger = logging.getLogger(__name__)

RESTORE_BACKUP_DIRNAME = "restore-backup"
"""恢复前那份本地兜底的落点：``<data_dir>/restore-backup/<ts>/``（方案 §3.2 / §3.4 第 3 步）。

它也是"整机回滚"那条文档路径让人把 ``kylab.db*`` 挪进去的地方——同一个目录，
两种用途（自动那一份是"恢复前的状态"，手动那一份是"你想回到的那台机器"）。
"""

RESTORE_STAGING_DIRNAME = "restore-staging"
"""解包暂存区：``<data_dir>/restore-staging/<snapshot_id>/``（方案 §3.4 第 5 步）。

**暂存区的语义是"一次一份"**：开始新的一次恢复/预演时会清掉别的快照目录
（它们只可能是一次中断留下的），别让它们把盘吃满。用户自己的数据**一个字节都不在这里**。
"""

MEMBER_SNAPSHOT = BLOB_NAME
"""下载下来的包体在 staging 里的名字。**取的是打包那一层的常量**（``backup_snapshot``
里的 ``BLOB_NAME``），不在这儿另写一个字符串：两处各写一遍，桶里那个对象名与这里
找的名字就可能分叉。"""

_UNPACK_CHUNK = 1024 * 1024
_MAX_UNPACK_BYTES = 8 * 1024 * 1024 * 1024
"""解包总字节的上限（8 GiB）。

不是什么精确的额度，而是一道**防坏包**的闸：一个被改坏/被塞了东西的包不该把用户的盘
写满。我们自己的打包器离这个量级差得远（产物配额 512 MiB + 库 + 记忆）。
"""


class BackupRestoreError(RuntimeError):
    """恢复这条链上的可预期失败（取不到清单 / 取不到包 / 包读不出来 / 落位失败）。

    与 ``LegacyImportError`` 分开是刻意的：那一条说的是"从源端导会话"（M2 的领域），
    这一条说的是"快照这件事"（清单、包体、staging、落位）。调用方（端点 / CLI）
    只把消息原样给用户，所以两类都只是"一句能读懂的话"。
    """


# ---------------------------------------------------------------- 来源：一份快照库


class SnapshotFileSource:
    """把**一份解包出来的快照库**当来源（导入器要的那种 ``TransferSource``）。

    与 ``HttpExportSource`` 一样是"逐条吐 ``ConversationTransfer``"的生成器——
    导入器因此一个字都不用知道"这一批是从 NAS 拉的还是从包里读的"。

    ``since`` 与导出端点同一条口径（只交更新时刻**严格晚于**它的会话）：按点恢复这条
    路永远不传它（恢复要的是"当时全量"），留着是为了协议完整——将来若要做"只补增量"，
    接缝已经在了。
    """

    def __init__(self, *, db_path: Path, snapshot: SnapshotSource, source: str) -> None:
        self._db_path = db_path
        self._snapshot = snapshot
        self._source = source

    @property
    def source(self) -> str:
        """台账里的来源标识：``backup://<device>/<snapshot>``（幂等键的第一段）。"""
        return self._source

    def transfers(self, *, since: datetime | None = None) -> Iterator[ConversationTransfer]:
        cutoff = None
        if since is not None:
            moment = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
            cutoff = int(moment.timestamp() * 1000)
        for transfer in self._snapshot.iter_snapshot_transfers(self._db_path):
            if cutoff is not None:
                updated = transfer.conversation.updated_at
                stamp = int(updated.timestamp() * 1000) if updated is not None else 0
                if stamp <= cutoff:
                    continue
            yield transfer


# ---------------------------------------------------------------- 报告与计划


@dataclass(slots=True)
class RestorePlan:
    """``plan()`` 的结果：**一个字节都不写本机**（只落 staging 暂存区 + 只读）。

    它回答方案 §3.4 第 2 步那四件事：会新建哪些会话 / 哪些跳过（含原因）/ 哪些产物不在
    包里 / 需要重配的凭据有几项。**逐会话的判定要读包里的库**（本机台账要与源端逐条比），
    所以"先下清单还是先下包"这个选择在这里是**两个都下**：清单（几 KB）用来做格式与
    版本判据 + 报"包里有什么"，包体用来报"这一条会怎么处理"——而那正是用户点下"恢复"
    之前最想知道的一件事。
    """

    device_id: str
    snapshot_id: str
    source: str
    manifest: dict[str, Any] = field(default_factory=dict)
    staging: str = ""
    database: str = ""
    package: dict[str, Any] = field(default_factory=dict)
    """包里有什么：``counts`` / ``included`` / ``schema_version`` / ``redacted``。"""
    created: list[dict[str, Any]] = field(default_factory=list)
    replaced: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    credentials_to_configure: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """给端点/CLI 的形状（键名就是界面上那几段）。"""
        return {
            "device_id": self.device_id,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "package": self.package,
            "created": self.created,
            "replaced": self.replaced,
            "skipped": self.skipped,
            "artifacts": self.artifacts,
            "memory": self.memory,
            "settings": self.settings,
            "credentials_to_configure": self.credentials_to_configure,
            "counts": {
                "created": len(self.created),
                "replaced": len(self.replaced),
                "skipped": len(self.skipped),
            },
        }


@dataclass(slots=True)
class RestoreReport:
    """``restore()`` 的结果（``counts`` 与台账里 ``counts_json`` 同源）。

    ``state`` 与 ``LegacyImporter`` 那三个一致（``done`` / ``failed`` / 以及回滚那条路
    由 M2 自己写），``counts`` 里除了导入器那份（``created`` / ``replaced`` /
    ``skipped_items`` …）还多一段 ``restore``：记忆 / 设置 / 产物 / 要重配的凭据。
    """

    device_id: str
    snapshot_id: str
    source: str
    batch_id: str
    state: str
    counts: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "done"

    @property
    def credentials_to_configure(self) -> list[str]:
        section = self.counts.get("restore")
        if isinstance(section, dict):
            value = section.get("credentials_to_configure")
            if isinstance(value, list):
                return [str(item) for item in value]
        return []

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "batch_id": self.batch_id,
            "state": self.state,
            "error": self.error,
            "counts": self.counts,
        }


# ---------------------------------------------------------------- 恢复本体


class BackupRestorer:
    """按点恢复（方案 §3.4）：**预演**与**真恢复**走同一套读，区别只在写不写。

    构造收四样：``stores``（本机库 + 存储面）、``data_dir``（staging / 兜底 / 产物落点）、
    ``provider``（取清单与包体）、``snapshotter``（打恢复前那份本地兜底）。
    ``runtime_config`` 给了就用它读 ``memory.workspace``（记忆落点与打包那一层同一个口径）。
    """

    def __init__(
        self,
        *,
        stores: StoreBundle,
        data_dir: str | Path,
        provider: BackupProviderClient,
        snapshotter: BackupSnapshotService,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        snapshot = stores.snapshot
        if not isinstance(snapshot, SnapshotSource):
            raise BackupRestoreError(
                "这个部署没有快照读面（StoreBundle.snapshot 为 None）：按点恢复是本机档独有的事"
            )
        if stores.ledger is None:
            raise BackupRestoreError(
                "这台机器没有导入台账（服务器档的会话就是权威）：恢复只在本机档可用"
            )
        self._stores = stores
        self._ledger: ImportLedger = stores.ledger
        self._snapshot: SnapshotSource = snapshot
        self._data_dir = Path(data_dir)
        self._provider = provider
        self._snapshotter = snapshotter
        self._runtime = runtime_config

    @property
    def provider(self) -> BackupProviderClient:
        """取清单 / 列恢复点的那一位。CLI 的 ``--list`` 要用它，端点那条路不用。"""
        return self._provider

    # -------------------------------------------------------------- 预演

    def plan(self, device_id: str, snapshot_id: str) -> RestorePlan:
        """dry-run（方案 §3.4 第 2 步）：**本机库 / 记忆 / 产物一个字节都不动**。

        四件事逐条：

        1. 取清单（``GET /backup/snapshots/{device}/{id}``）——它给了"包里有什么"、
           "哪些产物不在包里"（``skipped``）、"要重配哪些凭据"（``redacted``），
           以及格式 / 版本的兼容判据；清单只有几 KB，先看它比先下包划算；
        2. 下载 + 校验 + 解包到 staging（方案第 4/5 步的那两下，**只落暂存区**）；
        3. 走一遍导入器的 ``plan()``（M2 的 dry-run）拿"这一条会新建 / 替换 / 跳过（含原因）"
           ——那要读包里的库，所以第 2 步不能省；
        4. 把包里的记忆 / 设置 / 产物三块也预演一遍（哪几个文件会落、哪几个键会补、
           哪几份产物字节没跟过来）。

        **它不做**第 3 步（恢复前的本地兜底）：那是"要动本机"时才该付的代价。
        """
        manifest = self._fetch_manifest(device_id, snapshot_id)
        staging, db_path = self._stage(device_id, snapshot_id)
        source = self.source_of(device_id, snapshot_id)
        importer = self._importer(source=source, db_path=db_path)
        planned = importer.plan()
        view = self._snapshot.read_snapshot_db(db_path)
        artifacts = self._artifact_members(staging)
        return RestorePlan(
            device_id=device_id,
            snapshot_id=snapshot_id,
            source=source,
            manifest=manifest,
            staging=str(staging),
            database=str(db_path),
            package={
                "schema_version": view.schema_version,
                "counts": dict(view.counts),
                "included": dict(manifest.get("included") or {}),
                "redacted": list(manifest.get("redacted") or []),
                "skipped": list(manifest.get("skipped") or []),
            },
            created=[item.as_dict() for item in planned.created],
            replaced=[item.as_dict() for item in planned.replaced],
            skipped=[item.as_dict() for item in planned.skipped],
            artifacts={
                "in_package": len(artifacts[ARTIFACT_IN_OBJECTS])
                + len(artifacts[ARTIFACT_IN_WORKSPACE]),
                "object": len(artifacts[ARTIFACT_IN_OBJECTS]),
                "workspace": len(artifacts[ARTIFACT_IN_WORKSPACE]),
                "missing_from_package": list(manifest.get("skipped") or []),
            },
            memory=self._memory_plan(staging),
            settings=self._settings_plan(view),
            credentials_to_configure=self._credentials_to_configure(manifest, view),
        )

    # -------------------------------------------------------------- 真恢复

    def restore(
        self,
        device_id: str,
        snapshot_id: str,
        *,
        batch_id: str | None = None,
        overwrite_memory: bool = False,
        source_db: Path | None = None,
        staging: Path | None = None,
    ) -> RestoreReport:
        """按 §3.4 的第 3～11 步恢复一份快照；**失败也返回报告**（状态里写着）。

        ``source_db`` / ``staging`` 是给"刚才已经预演过"的那条路复用的：预演已经把包
        下载 + 校验 + 解包好了（方案第 4/5 步），真恢复不必再下一次——传进来就跳过
        下载那一步（**校验在预演那一次已经做过**，见 :meth:`plan`）。不传就自己下。

        ``overwrite_memory=False``（默认）时，本机已有的记忆文件**不动**，只在报告里说
        "跳过了哪几个"（方案第 8 步："默认只补不覆盖"）。
        """
        batch_id = batch_id or self._new_batch_id()
        started = datetime.now(UTC)
        source = self.source_of(device_id, snapshot_id)
        if self._ledger.get_import_batch(batch_id) is None:
            # 端点那条路已经用 ``begin()`` **同步**建过行了（客户端拿到 id 立刻要查得到）；
            # 这条兜底是给另外两个调用点的：CLI，以及"直接调服务"的用例/脚本。
            # 用"存在就不重建"而不是无条件插入：``start_import_batch`` 是纯 INSERT，
            # 重复插会抛 IntegrityError（那会把一次无害的分工变成失败）。
            #
            # 它也是"失败也有一份记录"的**前提**：收尾那一步是 UPDATE，行不存在就写不进去
            # ——失败发生得越早（取不到清单 / 包读不开），越会撞上这一点。
            self._ledger.start_import_batch(batch_id, source=source)
        report = RestoreReport(
            device_id=device_id,
            snapshot_id=snapshot_id,
            source=source,
            batch_id=batch_id,
            state="running",
        )
        try:
            manifest = self._fetch_manifest(device_id, snapshot_id)
            if staging is None or source_db is None:
                staging, source_db = self._stage(device_id, snapshot_id)
            # ③ 恢复前那份**本地兜底**（只落本机、不入队）——放在所有写动作之前
            pre_restore = self._pre_restore()
            report.counts["pre_restore_snapshot"] = pre_restore
            # ⑦ 会话：走 M2 的导入器（台账 / 幂等 / 覆盖三规则 / 后台线程都在那边）
            #
            # 这一行**不在这里建**：`run()` 自己会看"库里有没有这个批次"，没有才建
            # （`start_import_batch` 是纯 INSERT，重复插会抛 IntegrityError）。端点那条路
            # 已经用 `begin()` 同步建过行了，所以这里让它自己去判定。
            importer = self._importer(source=source, db_path=source_db)
            imported = importer.run(batch_id=batch_id)
            counts: dict[str, Any] = dict(imported.counts)
            if imported.state != "done":
                # 会话那一步失败：如实收尾（后面的记忆 / 设置 / 产物不做了——
                # 一份只导了一半的包不该再往盘上摊别的东西）
                counts["restore"] = {"skipped": "会话那一步失败了，记忆/设置/产物都没有动"}
                self._finish(batch_id, imported.state, counts, imported.error)
                report.state = imported.state
                report.counts = counts
                report.error = imported.error
                return report
            # ⑧⑨⑩ 快照特有的三块（M2 没有的东西）
            #
            # 先**把"导入完了"改回"还在跑"**：`run()` 收尾那一下写的是 ``done`` + 它自己
            # 那一段 counts，而恢复到这儿还没结束（记忆 / 设置 / 产物还没落位）。轮询的人
            # 若在这个窗口里读到 ``done``，会以为整件事完了——而且报告里没有 ``restore``
            # 那一段（界面拿不到"哪几条没按预想办"）。真正的终态是下面那句
            # ``_finish(..., "done", counts)``：那一下才是"全都落位了"。
            self._ledger.set_import_state(batch_id, "running", counts=counts)
            view = self._snapshot.read_snapshot_db(source_db)
            memory = self._restore_memory(staging, overwrite=overwrite_memory)
            settings = self._restore_settings(view)
            artifacts = self._restore_artifacts(staging)
            counts["restore"] = {
                "memory": memory,
                "settings": settings,
                "artifacts": artifacts,
                "credentials_to_configure": self._credentials_to_configure(manifest, view),
                "pre_restore_snapshot": pre_restore,
                "staging": str(staging),
                "seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
            }
            self._finish(batch_id, "done", counts)
            report.state = "done"
            report.counts = counts
            return report
        except Exception as exc:  # 端点那条路要"失败也是一份记录"，所以不往外抛
            message = str(exc) or exc.__class__.__name__
            report.state = "failed"
            report.error = message
            report.counts["restore"] = {"error": message}
            self._finish(batch_id, "failed", report.counts, message)
            logger.warning(
                "按点恢复失败（批次 %s，%s/%s）：%s",
                batch_id,
                device_id,
                snapshot_id,
                message,
                exc_info=True,
            )
            return report

    # -------------------------------------------------------------- 内部：清单与包

    def source_of(self, device_id: str, snapshot_id: str) -> str:
        """台账里的来源标识（幂等键的第一段）。**重跑必须给出同一个串**。"""
        return f"backup://{device_id}/{snapshot_id}"

    def _fetch_manifest(self, device_id: str, snapshot_id: str) -> dict[str, Any]:
        """取清单并过一遍格式 / 版本判据（**不猜着读**，方案 §2.3）。

        判据用 ``parse_manifest``（阶段 2 那份唯一实现），本机版本由存储层给
        （``SnapshotSource.local_schema_version``）——服务层不认识 ``SCHEMA_VERSION``
        那个常量（L2），也不该自己写一个。
        """
        raw = self._provider.manifest(device_id, snapshot_id)
        if raw is None:
            status = self._provider.status()
            raise BackupRestoreError(
                f"取不到这份恢复点的清单（{device_id}/{snapshot_id}）："
                f"{status.reason or '提供者现在不可用'}"
            )
        try:
            parsed = parse_manifest(raw, local_schema_version=self._snapshot.local_schema_version())
        except SnapshotFormatError as exc:
            raise BackupRestoreError(f"这份快照读不了：{exc}") from exc
        payload = parsed.as_payload()
        return payload

    def _stage(self, device_id: str, snapshot_id: str) -> tuple[Path, Path]:
        """准备 staging 并把包体取回来解好，返回 ``(staging, staging 里的库路径)``。"""
        staging = self._staging_dir(snapshot_id)
        self._prepare_staging(staging)
        package = staging / MEMBER_SNAPSHOT
        self._provider.download_snapshot(device_id, snapshot_id, package)
        db_path = self._unpack(staging, package)
        return staging, db_path

    def _staging_dir(self, snapshot_id: str) -> Path:
        return self._data_dir / RESTORE_STAGING_DIRNAME / Path(snapshot_id).name

    def _prepare_staging(self, staging: Path) -> None:
        """建一份干净的 staging，并**清掉别的快照留下的那些**（暂存区只留当前这一份）。

        staging 是暂存区，不是保存处：上一次中断留下的目录谁也不认领，留着只会把盘吃满。
        """
        root = self._data_dir / RESTORE_STAGING_DIRNAME
        root.mkdir(parents=True, exist_ok=True)
        for item in root.iterdir():
            if item == staging:
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:  # pragma: no cover - staging 里理论上只有目录
                item.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

    def _unpack(self, staging: Path, package: Path) -> Path:
        """解包到 staging（方案 §3.4 第 5 步）；**包本身的毛病都变成一句人话**。

        这一层的失败消息会原样给用户看（CLI 的 stderr / 端点报告里的 ``error``），
        所以 tar 那几个内部异常在这里收口成 :class:`BackupRestoreError`：
        "读不开 / 读到一半断了"要说清是**包**的问题，不是数据库坏了。

        另一类（成员想跑到外面去）由逐成员那一段自己抛，见 :meth:`_unpack_members`。
        """
        db_path = staging / MEMBER_DB
        try:
            return self._unpack_members(staging, package, db_path)
        except (tarfile.TarError, EOFError) as exc:
            raise BackupRestoreError(
                f"这个包读不开（不是一份 tar.gz，或者下载时被截断了）：{exc}"
            ) from exc

    def _unpack_members(self, staging: Path, package: Path, db_path: Path) -> Path:
        """逐成员写下包里的东西，**一道边界都不交给 tar**。

        ``extractall`` 一次都不调：它把"成员名怎么写"整件事交给 tar，而成员名来自
        别人的包。这里逐条自己写，并把四类可疑成员直接拒掉：

        - 绝对路径 / 盘符 / 反斜杠（Windows 上 ``C:x`` 与 ``\\`` 都是路径游戏）；
        - 含 ``..`` 的路径（经典穿越）；
        - 软链接 / 硬链接 / 设备文件（我们不产出它们，所以它们一定不是我们打的包）；
        - 写完再核一次"落点仍在 staging 里"（名字合法但拼接后跑出去的情况）。

        另外给一道总字节闸（``_MAX_UNPACK_BYTES``）：防一个坏包把盘写满。
        """
        total = 0
        with tarfile.open(package, "r:gz") as archive:
            for member in archive.getmembers():
                name = member.name
                if not member.isfile():
                    if member.isdir():
                        continue
                    raise BackupRestoreError(
                        f"包里有一个不是普通文件的成员（{name}）：快照里不该有链接或设备文件"
                    )
                if name.startswith(("/", "\\")) or "\\" in name or ":" in name:
                    raise BackupRestoreError(f"包里的成员名不是安全的相对路径：{name!r}")
                parts = Path(name).parts
                if ".." in parts or not parts:
                    raise BackupRestoreError(f"包里的成员名想跑到外面去：{name!r}")
                target = staging.joinpath(*parts)
                resolved = target.resolve()
                if not resolved.is_relative_to(staging.resolve()):
                    raise BackupRestoreError(f"包里的成员名解出来不在 staging 里：{name!r}")
                total += int(member.size)
                if total > _MAX_UNPACK_BYTES:
                    raise BackupRestoreError(
                        f"这个包解开之后超过 {_MAX_UNPACK_BYTES} 字节：不像是我们的快照，已中止"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:  # pragma: no cover - isfile 已经挡过
                    raise BackupRestoreError(f"包里的成员读不出来：{name}")
                with target.open("wb") as sink:
                    while True:
                        chunk = source.read(_UNPACK_CHUNK)
                        if not chunk:
                            break
                        sink.write(chunk)
        if not db_path.is_file():
            raise BackupRestoreError(
                f"包里没有库文件（{MEMBER_DB}）：这不是一份本应用的快照（清单说它是，包里不是）"
            )
        return db_path

    # -------------------------------------------------------------- 内部：导入器

    def _importer(self, *, source: str, db_path: Path) -> LegacyImporter:
        """配一台**走快照来源**的 M2 导入器（台账 / 幂等 / 覆盖规则全在它身上）。"""
        reader: TransferSource = SnapshotFileSource(
            db_path=db_path, snapshot=self._snapshot, source=source
        )
        return LegacyImporter(self._stores, data_dir=self._data_dir, source=reader)

    def _finish(self, batch_id: str, state: str, counts: dict[str, Any], error: str = "") -> None:
        """把报告并进**同一条台账**（轮询端点原样回 ``counts_json``，所以界面上看得到）。

        不新开端点、不给台账加列：``counts`` 本来就是自由形状的进度/报告字段
        （``imports.counts_json``），而恢复这件事实质上就是"用快照当来源的一次导入"。
        """
        self._ledger.set_import_state(batch_id, state, counts=counts, error=error)

    @staticmethod
    def _new_batch_id() -> str:
        return f"rst_{uuid.uuid4().hex[:12]}"

    def begin(self, device_id: str, snapshot_id: str) -> str:
        """**同步**把批次行写进库，返回批次 id（端点先给客户端 id 再去后台跑）。

        与 ``LegacyImporter.begin`` 同一条理由：客户端拿到 id 之后立刻来查必须查得到
        ——查不到只会得到 404，而那句话的意思是"这个 id 不存在"，不是"还没开始"。

        ``source`` 写的就是 :meth:`source_of` 那个串（``backup://<device>/<snapshot>``）：
        与真恢复那一步（``run``）写的是同一个值，**一行台账一个身份**。
        """
        batch_id = self._new_batch_id()
        self._ledger.start_import_batch(batch_id, source=self.source_of(device_id, snapshot_id))
        return batch_id

    # -------------------------------------------------------------- 内部：本地兜底

    def _pre_restore(self) -> str:
        """恢复前打一份 ``kind='pre_restore'`` 的本地兜底（方案 §3.4 第 3 步）。

        两个刻意的地方：落在 ``restore-backup/<ts>/``（**不是**队列的 ``backup/pending/``），
        而且**不调** ``enqueue``——它是"误覆盖"的第一道兜底，不是要传出去的备份
        （队列那边也有一条显式的拒绝，两处同一条边界）。
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self._data_dir / RESTORE_BACKUP_DIRNAME / stamp
        target.mkdir(parents=True, exist_ok=True)
        result = self._snapshotter.create(into=target, kind="pre_restore")
        logger.info("恢复前的本地兜底已打好：%s", result.blob_path)
        return result.snapshot_id

    # -------------------------------------------------------------- 内部：记忆

    def _memory_root(self) -> Path:
        """记忆根目录。**取值口径与 ``BackupSnapshotService._memory_root`` 逐字一致**
        （同一句回落 ``memory``）——两处各写一遍，快照里的记忆和恢复的落点就可能对不上。
        """
        raw = (self._runtime.get("memory.workspace") if self._runtime else "") or "memory"
        return self._data_dir / raw.strip()

    def _memory_members(self, staging: Path) -> list[tuple[str, Path]]:
        """staging 里的记忆成员：``(相对路径, 文件)``（``memory/<账号>/…`` 剥掉前缀）。"""
        root = staging / MEMBER_MEMORY
        if not root.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                found.append((path.relative_to(root).as_posix(), path))
        return found

    def _memory_plan(self, staging: Path) -> dict[str, Any]:
        """预演记忆那一块：包里几份、本机已有几份（那几份默认不覆盖）。"""
        members = self._memory_members(staging)
        target_root = self._memory_root()
        existing = [name for name, _ in members if (target_root / name).exists()]
        return {
            "in_package": len(members),
            "already_here": len(existing),
            "will_copy": len(members) - len(existing),
            "already_here_files": existing,
        }

    def _restore_memory(self, staging: Path, *, overwrite: bool) -> dict[str, Any]:
        """按 ``<账号>/<文件>`` 落位记忆（方案 §3.4 第 8 步）。

        **默认只补不覆盖**：本机已有同名文件就跳过并如实列出（用户在本机改过的记忆比
        包里那份新，覆盖它等于把"这台机器上学到的东西"抹掉）。``overwrite=True`` 时才覆盖
        ——那是用户明确勾选的动作。
        """
        target_root = self._memory_root()
        copied: list[str] = []
        skipped: list[str] = []
        for name, source in self._memory_members(staging):
            target = target_root.joinpath(*Path(name).parts)
            if not target.resolve().is_relative_to(target_root.resolve()):
                raise BackupRestoreError(f"记忆的文件名解出来不在记忆目录里：{name!r}")
            if target.exists() and not overwrite:
                skipped.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(name)
        return {
            "copied": len(copied),
            "skipped_existing": len(skipped),
            "copied_files": copied,
            "skipped_files": skipped,
            "overwrite": overwrite,
        }

    # -------------------------------------------------------------- 内部：设置

    def _settings_plan(self, view: SnapshotDbView) -> dict[str, Any]:
        """预演设置那一块：哪几个键会补上、哪几个已被本机改过（本机是权威）。"""
        would_fill: list[str] = []
        already: list[str] = []
        excluded: list[str] = []
        for key in view.settings_keys:
            if self._settings_excluded(key):
                excluded.append(key)
                continue
            if self._stores.meta.get_setting(key) is None:
                would_fill.append(key)
            else:
                already.append(key)
        return {"will_fill": would_fill, "already_here": already, "excluded": excluded}

    @staticmethod
    def _settings_excluded(key: str) -> bool:
        """凭据类设置判据（方案 §3.4 第 9 步）：``SECRET_KEYS`` ∪ 三个前缀。

        **判据引用常量本身**（与打包那一层的擦洗同一条纪律）：新增一个 SECRET_KEY 时，
        这里与那边一起自动跟上。包里本来就不该有这些键（擦洗删过），这一道是防漂——
        用户手工塞了一份库进快照、或者将来某条路上漏了擦洗，都会在这里被挡住。
        """
        return key in SECRET_KEYS or key.startswith(SNAPSHOT_EXCLUDED_SETTING_PREFIXES)

    def _restore_settings(self, view: SnapshotDbView) -> dict[str, Any]:
        """只补本机没有的键（方案 §3.4 第 9 步）；凭据类键**一律不写**。"""
        filled: list[str] = []
        skipped: list[str] = []
        excluded: list[str] = []
        for key, value in view.settings.items():
            if self._settings_excluded(key):
                excluded.append(key)
                continue
            if self._stores.meta.get_setting(key) is not None:
                skipped.append(key)
                continue
            self._stores.meta.set_setting(key, value)
            filled.append(key)
        return {"filled": filled, "kept_local": skipped, "excluded": excluded}

    # -------------------------------------------------------------- 内部：产物

    def _artifact_members(self, staging: Path) -> dict[str, list[str]]:
        """staging 里的产物成员：按对象档 / 工作区档分开（两份名字，各一条判据）。

        - 对象档的成员名是 ``files/<Key>``（恢复时按同一个 Key 落到 ``data_dir`` 下）；
        - 工作区档是 ``files/workspace/<产物 id>/<名字>`` —— **不落位**（见
          :meth:`_restore_artifacts`）。
        """
        root = staging / MEMBER_FILES
        found: dict[str, list[str]] = {ARTIFACT_IN_OBJECTS: [], ARTIFACT_IN_WORKSPACE: []}
        if not root.is_dir():
            return found
        workspace_prefix = f"{MEMBER_WORKSPACE_FILES}/"
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            name = f"{MEMBER_FILES}/{path.relative_to(root).as_posix()}"
            if name.startswith(workspace_prefix):
                found[ARTIFACT_IN_WORKSPACE].append(name)
            else:
                found[ARTIFACT_IN_OBJECTS].append(name)
        return found

    def _restore_artifacts(self, staging: Path) -> dict[str, Any]:
        """把对象档产物的字节按 Key 落进 ``data_dir``（方案 §3.4 第 10 步）。

        三档判定，一个都不静默：

        - 本机没有这个 Key → 写（报告 ``restored``）；
        - 有、且**字节相同** → 跳过（``identical``：重跑恢复的常态）；
        - 有、但**字节不同** → **如实报冲突并跳过**（``conflicts``：绝不静默覆盖，
          与 NAS 侧"同路径不同内容 409"同源。本机那份可能是用户改过的，也有可能是
          另一份快照留下的——两种都不该被悄悄盖掉）。

        工作区档那几份**不落位**：它们在那台机器上是用户项目目录里的文件，落进
        ``data_dir`` 只会造出一份"没人知道它属于哪儿"的副本。条数如实进报告
        （方案 §3.4 第 11 步那句"未随包过来的工作区产物数"）。
        """
        found = self._artifact_members(staging)
        restored: list[str] = []
        identical: list[str] = []
        conflicts: list[str] = []
        for name in found[ARTIFACT_IN_OBJECTS]:
            key = name[len(MEMBER_FILES) + 1 :]
            source = staging.joinpath(*Path(name).parts)
            # Key 进过打包那一层的检查（必须是安全相对路径），这里再核一次落点
            target = self._data_dir.joinpath(*Path(key).parts)
            if not target.resolve().is_relative_to(self._data_dir.resolve()):
                conflicts.append(key)
                continue
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                restored.append(key)
                continue
            if (
                hashlib.sha256(target.read_bytes()).digest()
                == hashlib.sha256(source.read_bytes()).digest()
            ):
                identical.append(key)
            else:
                conflicts.append(key)
        return {
            "restored": len(restored),
            "identical": len(identical),
            "conflicts": len(conflicts),
            "restored_keys": restored,
            "conflict_keys": conflicts,
            "workspace_not_placed": len(found[ARTIFACT_IN_WORKSPACE]),
            "workspace_files": found[ARTIFACT_IN_WORKSPACE],
        }

    # -------------------------------------------------------------- 内部：凭据清单

    def _credentials_to_configure(
        self, manifest: dict[str, Any], view: SnapshotDbView
    ) -> list[str]:
        """R13：恢复之后**要重配**的凭据（不这么做，用户会以为恢复失败了）。

        三样，逐条说人话 + **带具体名字**：

        - 模型凭据：清单里那几个供应商（打包时 ``api_key`` 被清空，见 §2.4）——
          名字取自快照库（``model_provider_names``），不是编的；
        - 设置里的凭据：manifest 的 ``redacted`` 里那几条 ``app_settings`` 的键
          （如 ``web.search_api_key``）；
        - NAS 的钥匙：它住在桌面壳的钥匙串里，**从来不随快照走**（§4.2 的边界）。
        """
        lines: list[str] = []
        names = list(view.model_provider_names)
        if names:
            lines.append(
                f"模型凭据 {len(names)} 个（{'、'.join(names)}）：恢复后要重新填一次"
                "（快照里这一列是空的）"
            )
        keys = [
            str(item.get("key"))
            for item in manifest.get("redacted") or []
            if isinstance(item, dict) and item.get("table") == "app_settings" and item.get("key")
        ]
        if keys:
            lines.append(f"设置里的凭据 {len(keys)} 项（{'、'.join(keys)}）：恢复后要重新填一次")
        lines.append("NAS 的钥匙：这台机器要重新登录一次（钥匙串不随快照走）")
        return lines


# ---------------------------------------------------------------- CLI


def _local_stores(data_dir: Path) -> StoreBundle:
    """CLI 这条路的装配：**按本机档**建库（与 ``legacy_import._local_stores`` 同一手法）。

    环境变量四个与 ``sidecar.pin_local_deployment`` 一模一样（``KYLAB_DATABASE_URL``
    **设空串**而不是删掉——本机 `.env` 里真的配了它，删掉会让它"复活"）。**不 import
    边车模块**：这条命令没必要把整个边车进程拖进导入图里。
    """
    import os

    from app.core.config import get_settings
    from app.core.storage import build_stores

    os.environ["KYLAB_DEPLOYMENT"] = "local"
    os.environ["KYLAB_DATABASE_URL"] = ""
    os.environ["KYLAB_DATA_DIR"] = str(data_dir)
    get_settings.cache_clear()
    return build_stores(get_settings())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.backup_restore",
        # 那三步要**按行**读（默认的 formatter 会把换行折成一段话），所以用 Raw：
        # "停边车 → 挪库 → 再恢复"的顺序本身就是这条路径的要点。
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "把 NAS 上一份备份快照恢复到本机（也可以只列出来、只预演）。\n"
            "\n"
            "整机回滚到某一天（比如「回到昨天」）走这条干净的路：\n"
            "  1) 停掉边车（本机后端进程）；\n"
            "  2) 把 <data-dir>/kylab.db* 挪到 <data-dir>/restore-backup/<ts>/；\n"
            "  3) 再跑一次本命令（此时库是空的 → 全量重建）。\n"
            "\n"
            "不要在本机后端跑着的时候换库文件：进程持有连接、WAL 还在飞——"
            "恢复这件事的语义是「往一个库上补 / 合并」，不是「替换那个文件」。"
        ),
    )
    parser.add_argument("--data-dir", required=True, help="本机数据目录（库与记忆都在它下面）")
    parser.add_argument(
        "--server", default="", help="NAS 的 API 基址（含 /api/v1）；默认用设置里那个"
    )
    parser.add_argument("--token", default="", help="NAS 的令牌（只在内存与请求头上）")
    parser.add_argument(
        "--device-id",
        default="",
        help="这台机器的设备身份（打恢复前那份本地兜底要用；默认读 KYLAB_DEVICE_ID）",
    )
    parser.add_argument("--list", action="store_true", help="只列出 NAS 上的恢复点")
    parser.add_argument(
        "--snapshot",
        default="",
        help="要恢复哪一份：<device_id>/<snapshot_id>（用 --list 看）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只预演：不碰本机库 / 记忆 / 产物")
    parser.add_argument(
        "--overwrite-memory",
        action="store_true",
        help="记忆也覆盖（默认只补本机没有的那几份）",
    )
    return parser


def _build(data_dir: Path, server: str, token: str, device_id: str) -> BackupRestorer:
    """装配 CLI 需要的那四件（库 / 运行期配置 / 提供者 / 打包器 → 恢复器）。"""
    from app.core.config import get_settings

    if server:
        import os

        os.environ["KYLAB_SERVER_URL"] = server
    if token:
        import os

        os.environ["KYLAB_TOKEN"] = token
    if device_id:
        import os

        os.environ["KYLAB_DEVICE_ID"] = device_id
    get_settings.cache_clear()
    settings = get_settings()
    stores = _local_stores(data_dir)
    runtime = RuntimeConfigService(stores, settings)
    provider = BackupProviderClient(settings=settings, get_setting=runtime.get)
    packer = BackupSnapshotService(
        stores=stores,
        data_dir=data_dir,
        device_id=settings.device_id,
        runtime_config=runtime,
    )
    return BackupRestorer(
        stores=stores,
        data_dir=data_dir,
        provider=provider,
        snapshotter=packer,
        runtime_config=runtime,
    )


def _split_snapshot(raw: str) -> tuple[str, str]:
    """``<device_id>/<snapshot_id>`` → 两段。**错误信息要说人话**（怎么拿到这两个值）。"""
    text = (raw or "").strip().strip("/")
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        raise BackupRestoreError(
            f"--snapshot 要写成 <device_id>/<snapshot_id>（读到 {raw!r}）；"
            "先跑 --list 看有哪些恢复点"
        )
    return parts[0], parts[1]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：``--list`` / ``--dry-run`` / 真恢复（三档，都先过一遍同一套判据）。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    try:
        restorer = _build(data_dir, args.server, args.token, args.device_id)
        if args.list:
            return _list_command(restorer)
        device_id, snapshot_id = _split_snapshot(args.snapshot)
        if args.dry_run:
            plan = restorer.plan(device_id, snapshot_id)
            print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
            print(
                f"只看不写：会新建 {len(plan.created)} / 替换 {len(plan.replaced)} / "
                f"跳过 {len(plan.skipped)}；"
                f"包里没有的产物 {len(plan.artifacts.get('missing_from_package') or [])} 份"
            )
            for line in plan.credentials_to_configure:
                print(f"恢复后要重配：{line}")
            return 0
        report = restorer.restore(device_id, snapshot_id, overwrite_memory=args.overwrite_memory)
        print(json.dumps(report.counts, ensure_ascii=False, indent=2))
        for line in report.credentials_to_configure:
            print(f"恢复后要重配：{line}")
        if report.ok:
            print(f"批次 {report.batch_id}：{report.state}")
            return 0
        print(f"批次 {report.batch_id} 失败：{report.error}", file=sys.stderr)
        return 1
    except (BackupRestoreError, LegacyImportError, InvalidRequestError) as exc:
        print(f"恢复没能进行：{exc}", file=sys.stderr)
        return 1


def _list_command(restorer: BackupRestorer) -> int:
    """``--list``：把 NAS 上的恢复点原样列出来（最近在前）。"""
    provider = restorer.provider
    status = provider.status()
    if not status.available:
        print(f"看不到恢复点：{status.reason}", file=sys.stderr)
        return 1
    payload = provider.list_snapshots(refresh=True)
    if payload is None:
        print("恢复点清单没取到：稍后再试一次", file=sys.stderr)
        return 1
    items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
    print(json.dumps(items, ensure_ascii=False, indent=2))
    print(f"共 {payload.get('total') or 0} 份（键：--snapshot <device_id>/<snapshot_id>）")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
