"""备份提供者的窄 API（M5 阶段 1）：``/api/v1/backup/*`` —— 路 B 的服务端那一半。

**为什么是路 B**（方案 §1.1 的七条，一条不少）。对照的是"本机后端拿 S3 凭据直写 MinIO"
那条路（路 A）：

1. **凭据下发面**：路 A 要把 S3 的 access/secret 下发到每台桌面机并**永久落盘**——那等于
   **整个桶**（含知识库原件）的钥匙；路 B 的客户端手里仍然只有已经在用的那把 API Key
   （``kylab_sk_…``，``is_admin=false``），**一个字节的新凭据都不下发**；
2. **可达性**：NAS 上 MinIO 的端口默认只绑宿主机回环（``deploy/docker-compose.yml:80-84``），
   路 A 要为它**新开一个局域网端口**，还要处理 TLS 与反向代理；路 B 复用已有的 nginx + 后端；
3. **校验**：路 A 的哈希是客户端自算自报，服务端**无法证伪**；路 B 在 NAS 侧**边收边算**，
   不符即 400 且**一个字节都不落桶**；
4. **配额与保留**：路 A 只能事后知道；路 B 算得出来（份数、字节、删除），
   握手就能如实回"配了多少、用了多少"；
5. **幂等**：路 A 靠 S3 PUT 的覆盖语义，**append-only 没有保障**；路 B 路径即幂等键 +
   先 ``head_object`` 查——同内容 200 no-op、不同内容 409，**绝不覆盖**；
6. **实现量**：路 A 要让**客户端** import boto3（客户端运行时的红线：闭包 17 个发行包不涨，
   判据是 ``scripts/sidecar-closure.py`` 真跑一遍）；路 B 的 boto3 只在服务器档、
   而且是惰性 import（``services/backup_store.py``）；
7. **路 A 的变体**（NAS 签发**预签名** PUT/GET、客户端直传 MinIO）同样不交出 S3 凭据，
   但它需要 MinIO 在局域网可达 + 端口/TLS 决策——**登记为将来优化，M5 不做**（方案 §8-8）。

**三条设计裁量**（方案 §1.3；写在这里，省得下次有人重新论证一遍）：

1. **不复用知识库的对象布局**：备份桶独立（v0.3 §7"落 MinIO 快照桶，不落知识库的 PG"），
   后端另起一份薄封装（``services/backup_store.py``；这是决策点 D6：不扩 ``ObjectStore``
   ——它是知识库那 5 个方法的面，而且构造时 ``head_bucket`` 会让缺桶把启动搞崩）；
2. **握手独立、不并进 ``knowledge`` 那一段**：v0.3 §2 明写两个提供者可以指向不同 NAS ——
   并进去就会出现"不配知识库就没法握手备份"的死角。协议版本各自一个整数
   （备份这边是 ``PROTOCOL_VERSION = 1``，与知识库那个 1 是**两件事**）；
3. **PUT 的路径本身就是幂等键**（append-only + 内容寻址），**不需要 ``Idempotency-Key`` 头**。
   这条新约定手写在《API 接口规范》§1.13（与 §1.6"上传类接口的幂等键"并列），
   它是机器读不出来的那类约定，所以必须写进文档，不能只活在代码里。

**桶布局与 append-only 四条硬规矩**在 ``services/backup_store.py`` 的模块头
（键是那一层的事）；本模块负责的是**契约**：状态码三态（201 新建 / 200 同内容 no-op /
409 同路径不同内容或超保留）、413（超 ``max_blob_bytes``）、
400（sha256 / bytes 与实收不符，**不落桶**）、403（只读 key 上传或删除）。

**边界**（方案 §1.5 / R9，如实写进契约）：

- **可见范围 = 这把钥匙能看见的全部设备**。备份**不建 ACL**：v0.3 §3.1 那三条规则讲的是
  知识库的库范围，备份没有"设备范围"这个概念，所以谁拿着这把钥匙谁就看得见所有设备的
  恢复点。登记为"将来要按设备限权"的一条；
- **受限（只读）key 能下载、不能上传 / 删除**：写端点走 ``require_write``，
  只读档拿到的是 **403**（不是 404、也不是"静默不写"）；
- **本模块不进 ``local_router``**：本机档是**客户端**角色——它调这些端点，不提供它们
  （与 ``api/v1/provider.py`` 同一条口径，见 ``api/v1/router.py`` 的不挂清单）。

**七条端点**（方案 §1.3）：握手 / 列恢复点 / 传 blob / 传 manifest / 取 manifest /
流式下载 blob / 删一个恢复点。``snapshot_id`` 是**恢复点目录名**（``<ts>-<hash8>``），
与 ``device_id`` 合成一对坐标——本机队列那条 ``<device_id>-<ts>-<hash8>`` 的整串 id
只是这一对拼起来的样子（方案 §3.1）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.auth import ReadDep, WriteDep
from app.api.v1.provider import caller_brief
from app.api.v1.schemas import (
    BackupCapabilitiesOut,
    BackupDeviceBriefOut,
    BackupHandshakeOut,
    BackupQuotaOut,
    BackupRestoreCapsOut,
    BackupSnapshotCapsOut,
    BackupSnapshotListOut,
    BackupSnapshotOut,
    BackupUploadOut,
)
from app.core.caller import Caller
from app.core.config import API_VERSION, get_settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamError,
)
from app.services.backup_store import (
    BLOB_NAME,
    MANIFEST_NAME,
    BackupError,
    BackupObjectInfo,
    BackupStore,
    get_backup_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])

# ---------------------------------------------------------------- 能力集常量
#
# **这份常量是这个模块的唯一一份口径**（照 `api/v1/provider.py` 的写法）：响应拼装里
# 不再出现第二个字面量，用例逐个比对它（`tests/unit/api/test_backup_handshake.py`）。

PROVIDER_NAME = "backup"
"""提供者种类：备份。与知识库那个 ``knowledge`` 是两个提供者（各有各的协议版本）。"""

PROTOCOL_VERSION = 1
"""握手协议版本（**整数、只增**）。客户端规则同知识库那条：不认识即判不可用，绝不硬试。"""

SNAPSHOT_TRANSPORT = "octet-stream"
"""客户端**怎么编码这次上传**：裸字节流（``Content-Type: application/octet-stream``）。

契约草案 §1.3 那一位写的是 ``multipart``——它说的是**服务端分片上传**（``upload_fileobj``
在 8 MiB 以上自动走 multipart，>5 GiB 也成），不是"客户端要发 multipart 表单"。
这两件事在这个字段名上会撞，而读这个字段的是客户端，所以这里报**客户端要照着做的那一件事**
（``octet-stream``）；服务端怎么分片是它自己的实现细节，写进能力集只会让人照着一份
自己并不需要的编码去写上传。
"""

SNAPSHOT_FORMAT = "tar.gz"
"""快照体的打包格式（stdlib ``tarfile`` + ``gzip``，成员顺序见方案 §2.3）。"""

MANIFEST_FORMAT = "json"
"""清单格式：一个 JSON 对象（**服务端不解释它的业务字段**，原样存取）。"""

CHECKSUM = "sha256"
"""校验口径：整份快照体的 sha256（服务端边收边算，不符即 400）。"""

ENCRYPTION = "none"
"""自描述字段：这份快照**没有做应用层加密**（决策点 D2：秘密不进快照，所以不做加密）。

留着它是为了将来真要加客户端加密时，读的一方按它自识别——照"加密文件自描述"那条做法。
"""

MAX_BLOB_BYTES = 2 * 1024 * 1024 * 1024
"""单份快照体上限（2 GiB，方案 §7 R6）。超了回 **413**，且不落桶。

它是**服务端**的上限，与客户端自己的额度是两件事：客户端那份管"哪些产物不进包"，
这份管"一次别把 NAS 的内存与带宽打满"。
"""

MAX_MANIFEST_BYTES = 1024 * 1024
"""清单上限（1 MiB，方案 §1.3）。清单是"这一个恢复点有什么"的索引，不是数据本身。"""

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
"""列恢复点的分页（规范 §1.5）：默认 50，服务端上限 200，超限回 422 而不是静默截断。"""

RESTORE_POINT_IN_TIME = True
"""支持按时间点挑一份恢复点（就是"列恢复点 + 取 manifest"这两条能做的事）。"""

RESTORE_MANIFEST_LISTING = True
"""恢复点清单里有 manifest（计数 / 被跳过项 / schema 版本都在里面，恢复前能先看）。"""

RESTORE_DOWNLOAD = True
"""快照体可下载（流式，``Content-Length`` 与 sha256 都给）。"""

RESTORE_PARTIAL_RESTORE = True
"""一份快照**够得着"只恢复一部分"**：manifest 里逐条列了内容物与计数，tar.gz 的成员
也是逐个可取的——所以"只挑几条会话恢复"不需要另一种快照格式。"""

RETENTION_POLICY = "keep_n"
"""保留策略：留最近 N 份（服务端**不替用户删**，超限报 409——方案 §1.2 规矩 4）。"""

_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

_SPOOL_MAX_BYTES = 8 * 1024 * 1024
"""收请求体时的内存阈值：超过它就溢写到磁盘（而不是把 2 GiB 收进内存）。"""

_DOWNLOAD_CHUNK = 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def backup_store_dep() -> BackupStore:
    """FastAPI 依赖：进程级那一份备份桶封装（**懒建，启动不校验桶**）。

    做成依赖而不是模块级全局，是为了用例能 ``app.dependency_overrides`` 换成假 store
    ——这批用例**不打真 MinIO**（与 `test_provider_handshake.py` 同一手法）。
    """
    return get_backup_store()


StoreDep = Annotated[BackupStore, Depends(backup_store_dep)]


# ------------------------------------------------------------------ 路径校验


def _segment(value: str, field: str) -> str:
    """校验路径段（``device_id`` / ``snapshot_id``）。

    **比"能不能当键"更严一点是刻意的**：方案 §1.2 的命名规则里，``device_id`` 是 UUID v4、
    ``snapshot_ts`` 是 ``YYYY-MM-DDTHH-MM-SSZ``（冒号已经换成短横），所以键里本来就
    不含 ``/``、``:``、空格与任何非 ASCII。拒掉它们有两个好处：路径即幂等键这条规矩
    **不会被两种写法绕过**（同一个时间戳写成 ``12:00`` 与 ``12-00`` 会变成两份），
    以及桶里不会长出难看的键。
    """
    if not isinstance(value, str) or not _SEGMENT.match(value):
        raise BadRequestError(
            f"{field} 不合法：{value!r}。命名规则见《备份提供者实施方案》§1.2——"
            "只允许字母、数字、点、下划线、短横（不超过 128 字）"
        )
    return value


def _sha256(value: str) -> str:
    """把声明的 sha256 规范成小写并校验形状（**不是这里算的**，算的是实收那一份）。"""
    candidate = (value or "").strip().lower()
    if not _SHA256.match(candidate):
        raise BadRequestError(
            f"sha256 不合法：{value!r}。它得是整份快照体的十六进制 sha256（64 位）"
        )
    return candidate


# ------------------------------------------------------------------ 恢复点扫描


@dataclass(frozen=True)
class _Point:
    """桶里的**一个恢复点**：一对对象（blob + manifest）加它自报的那份清单。

    只有两样都在、且 manifest 读得出来，才算一个恢复点——这就是"枚举只认有 manifest 的"
    那条规矩的落点（孤儿 blob 留着不删、但**不列出来**）。
    """

    device_id: str
    snapshot_id: str
    size: int
    created_at: datetime
    sha256: str
    manifest: dict[str, Any]

    @property
    def device_name(self) -> str:
        return _text(self.manifest.get("device_name"))

    @property
    def kind(self) -> str:
        return _text(self.manifest.get("kind"))

    @property
    def schema_version(self) -> int:
        return _int(self.manifest.get("schema_version"))

    @property
    def app_version(self) -> str:
        return _text(self.manifest.get("app_version"))

    @property
    def encryption(self) -> str:
        return _text(self.manifest.get("encryption")) or ENCRYPTION


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _int_map(value: Any) -> dict[str, int]:
    """manifest 里的计数段：只收数字（不认识的类型忽略，不编一个值出来）。"""
    if not isinstance(value, dict):
        return {}
    return {str(key): int(item) for key, item in value.items() if isinstance(item, (int, float))}


def _skipped(value: Any) -> list[dict[str, Any]]:
    """被跳过的内容物：**原样透传**（服务端不重新解释它，更不裁剪它的字段）。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _parse_time(value: Any) -> datetime | None:
    """ISO 8601 → 带时区的 datetime（不认识的写法回 ``None``，由调用方退到对象时间）。"""
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _read_manifest(store: BackupStore, key: str) -> dict[str, Any] | None:
    """读一份清单；读不出来 / 不是 JSON 对象就回 ``None``（**不猜着读**）。

    ``None`` 的含义是"这一份不算一个可用的恢复点"：恢复要靠 manifest 才知道包里有什么，
    一份读不出来的清单等于一块搬不走的石头。它有对象在桶里（append-only 不删），
    但枚举与配额都不算它。
    """
    try:
        raw = store.get_blob(key)
    except FileNotFoundError:
        return None
    except (BackupError, UnicodeDecodeError):
        logger.warning("清单读不出来：%s", key, exc_info=True)
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("清单不是合法 JSON：%s", key)
        return None
    if not isinstance(parsed, dict):
        logger.warning("清单不是 JSON 对象：%s", key)
        return None
    return parsed


def _split_key(key: str, prefix: str) -> tuple[str, str, str] | None:
    """``<前缀>backup/<device>/<snapshot>/<名字>`` → 三段；不是这个形状就回 ``None``。

    **形状对不上的一律忽略**（而不是当成错误）：将来桶里若多出别的东西（别人手工放的、
    以后新布局的），它们不该让枚举整条挂掉。
    """
    if not key.startswith(prefix):
        return None
    parts = key[len(prefix) :].split("/")
    if len(parts) != 3 or parts[2] not in (BLOB_NAME, MANIFEST_NAME):
        return None
    return parts[0], parts[1], parts[2]


def _points(store: BackupStore, *, device_id: str | None = None) -> list[_Point]:
    """扫一遍恢复点（**最近在前**），可按设备筛。

    **一次扫描同时回答握手与列表两个问题**，因为两处的数必须一样：握住手说"这台设备 3 份、
    共 1.2 GB"，点进列表却是 4 份——那种自相矛盾没有用例能同时看见。
    代价是握手也要读每份清单（≤1 MiB，实际几 KB，一台设备最多 ``backup_keep`` 份），
    而这个代价换来的是一条代码路径与一组数字。

    **永远从 ``.../backup/`` 那一层列起，再在内存里按设备筛**（而不是改 list 的前缀）：
    键的解析于是只有一种形状。曾经按设备前缀列过一次、又用同一个解析函数去切，
    结果是"列全部"对、"列一台设备"永远空——那正是 keep 上限会悄悄失效的形状
    （`test_the_keep_limit_refuses_instead_of_evicting` 抓住的就是它）。
    """
    prefix = store.all_prefix()
    grouped: dict[tuple[str, str], dict[str, BackupObjectInfo]] = {}
    for item in store.list_prefix(prefix):
        parsed = _split_key(item.key, prefix)
        if parsed is None:
            continue
        device, snapshot, name = parsed
        grouped.setdefault((device, snapshot), {})[name] = item

    points: list[_Point] = []
    for (device, snapshot), found in grouped.items():
        if device_id is not None and device != device_id:
            continue
        manifest_item = found.get(MANIFEST_NAME)
        blob_item = found.get(BLOB_NAME)
        if manifest_item is None or blob_item is None:
            # 上传中断留下的孤儿 blob（或只有清单的怪状态）：**留着不删、枚举忽略**
            continue
        manifest = _read_manifest(store, manifest_item.key)
        if manifest is None:
            continue
        points.append(
            _Point(
                device_id=device,
                snapshot_id=snapshot,
                size=blob_item.size,
                created_at=_parse_time(manifest.get("created_at")) or blob_item.modified or _EPOCH,
                sha256=_text((manifest.get("blob") or {}).get("sha256"))
                if isinstance(manifest.get("blob"), dict)
                else "",
                manifest=manifest,
            )
        )
    points.sort(key=lambda point: point.created_at, reverse=True)
    return points


def _snapshot_out(point: _Point) -> BackupSnapshotOut:
    """恢复点 → 列表里的一行（字段就是契约 §1.3 那一张）。"""
    return BackupSnapshotOut(
        snapshot_id=point.snapshot_id,
        device_id=point.device_id,
        device_name=point.device_name,
        created_at=point.created_at,
        bytes=point.size,
        sha256=point.sha256,
        kind=point.kind,
        schema_version=point.schema_version,
        app_version=point.app_version,
        counts=_int_map(point.manifest.get("counts")),
        skipped=_skipped(point.manifest.get("skipped")),
        encryption=point.encryption,
    )


def _quota(points: list[_Point]) -> BackupQuotaOut:
    """额度那一段：**配了多少、用了多少**（方案 §1.4）。

    保留份数与配额是整把钥匙看得见的**全部设备**（与可见范围同一口径）——
    所以传了 ``device_id`` 过滤时 ``items`` 会变少，这一段不变。
    """
    settings = get_settings()
    return BackupQuotaOut(
        policy=RETENTION_POLICY,
        keep=settings.backup_keep,
        quota_bytes=settings.backup_quota_bytes,
        used_bytes=sum(point.size for point in points),
        snapshots=len(points),
    )


# ------------------------------------------------------------------ 响应拼装


def capabilities(points: list[_Point], available: bool, detail: str) -> BackupCapabilitiesOut:
    """能力集：**如实报这台提供者现在能做什么**。

    ``snapshot.available`` 是这里唯一会随环境变的一位：桶没建出来（或对象存储连不上）时
    它是 ``false``，旁边 ``unavailable_reason`` 里就是**下一步该敲的那一句**
    （方案 R5 的判据：**握手仍然 200**，只是能力位如实报不可用）。
    """
    settings = get_settings()
    return BackupCapabilitiesOut(
        snapshot=BackupSnapshotCapsOut(
            available=available,
            unavailable_reason=detail,
            transport=SNAPSHOT_TRANSPORT,
            format=SNAPSHOT_FORMAT,
            manifest=MANIFEST_FORMAT,
            checksum=CHECKSUM,
            max_blob_bytes=MAX_BLOB_BYTES,
            max_snapshots_per_device=settings.backup_keep,
            encryption=ENCRYPTION,
        ),
        restore=BackupRestoreCapsOut(
            point_in_time=RESTORE_POINT_IN_TIME,
            manifest_listing=RESTORE_MANIFEST_LISTING,
            download=RESTORE_DOWNLOAD,
            partial_restore=RESTORE_PARTIAL_RESTORE,
        ),
        retention=(
            _quota(points)
            if available
            else BackupQuotaOut(
                policy=RETENTION_POLICY,
                keep=settings.backup_keep,
                quota_bytes=settings.backup_quota_bytes,
                # **不是零，是数不出来**：桶不可用时这两位数没有意义，
                # `available=false` 才是判据（客户端据此别把 0 当"还没备过"显示）
                used_bytes=0,
                snapshots=0,
            )
        ),
    )


def _device_briefs(points: list[_Point]) -> list[BackupDeviceBriefOut]:
    """每台设备一段摘要（方案 §1.4：**握手一次给全，不另开端点**）。

    ``points`` 已经按时间倒序，所以每台设备取到的第一条就是它的最近一份。
    """
    grouped: dict[str, list[_Point]] = {}
    for point in points:
        grouped.setdefault(point.device_id, []).append(point)
    briefs = [
        BackupDeviceBriefOut(
            device_id=device,
            device_name=items[0].device_name,
            snapshots=len(items),
            bytes=sum(item.size for item in items),
            latest_snapshot_id=items[0].snapshot_id,
            latest_at=items[0].created_at,
        )
        for device, items in grouped.items()
    ]
    briefs.sort(key=lambda brief: brief.latest_at or _EPOCH, reverse=True)
    return briefs


def build_handshake(store: BackupStore, caller: Caller) -> BackupHandshakeOut:
    """握手响应（**纯拼装**：不写库、不发网络请求之外的事）。

    与 ``provider.build_handshake`` 同一手法：正因为它是纯拼装，用例能喂一份假 store
    把契约的形状与取值钉死，不必等到跑起一台带 MinIO 的实例。
    """
    settings = get_settings()
    status = store.status()
    points = _points(store) if status.available else []
    return BackupHandshakeOut(
        provider=PROVIDER_NAME,
        protocol_version=PROTOCOL_VERSION,
        app_version=settings.app_version,
        api_version=API_VERSION,
        capabilities=capabilities(points, status.available, status.detail),
        caller=caller_brief(caller),
        devices=_device_briefs(points),
        server_time=datetime.now(UTC),
    )


# ------------------------------------------------------------------ 收流 / 落桶


@asynccontextmanager
async def _receive(
    request: Request, *, limit: int, what: str
) -> AsyncIterator[tuple[Any, int, str]]:
    """边收边算：请求体收进一个**会溢写到磁盘**的临时文件，同时算 sha256 与字节数。

    收好之后把 ``(文件, 字节数, sha256)`` 交给调用方用——**用完即关**（contextmanager
    的收尾就是那个 ``with``）：临时文件里是用户数据，多留一秒都是多余的明文。

    三条理由，缺一不可（方案 R6 的判据正是"超限直接 413，且不落桶"）：

    - **不落桶**：``sha256`` / ``bytes`` 与实收不符时要 400，而那一刻**桶里一个字节都还没有**
      ——所以只能先收完、验完，再上传；
    - **不整份进内存**：``SpooledTemporaryFile`` 在 8 MiB 以内留在内存、超过就落磁盘。
      2 GiB 的上限不能靠 ``await request.body()`` 收（那是把整份塞进内存）；
    - **413 也要不落桶**：边收边数，超限立刻抛（不再读剩下的），临时文件就地关掉。
    """
    digest = hashlib.sha256()
    total = 0
    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as spool:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise PayloadTooLargeError(
                    f"{what}超过单份上限 {limit} 字节（max_blob_bytes）：切分或压缩再试"
                )
            digest.update(chunk)
            spool.write(chunk)
        spool.seek(0)
        yield spool, total, digest.hexdigest()


def _check_limits(points: list[_Point], *, size: int) -> None:
    """保留份数与配额**超限就报错**（方案 §1.2 规矩 4：服务端绝不替用户删）。

    两条都回 409：它们是"现在不行，先删一份再传"，不是"这个请求本身写错了"
    （规范 §1.3 那两档的分别）。数字如实报出来，用户自己能算出还差多少。
    """
    settings = get_settings()
    if len(points) >= settings.backup_keep:
        raise ConflictError(
            f"这台设备已经有 {len(points)} 份快照，到了保留份数上限"
            f"（keep={settings.backup_keep}）：服务端不替你删旧快照——"
            "先显式删掉一份恢复点再传"
        )
    used = sum(point.size for point in points)
    if used + size > settings.backup_quota_bytes:
        raise ConflictError(
            f"备份配额不够：已用 {used} 字节 + 这一份 {size} 字节 > 上限 "
            f"{settings.backup_quota_bytes} 字节：先删掉一些恢复点再传"
        )


def _store_blob(
    store: BackupStore, device_id: str, snapshot_id: str, *, sha256: str, size: int, spool: Any
) -> tuple[bool, BackupObjectInfo]:
    """把收好的一份快照体落桶；返回 ``(是不是新建的, 落桶之后的事实)``。

    **同步函数**：boto3 是阻塞的，由端点在**线程池**里跑（不是 async 端点里直调，
    那样会把事件循环按住整份上传的时间）。
    """
    key = store.blob_key(device_id, snapshot_id)
    existing = store.head(key)
    if existing is not None:
        if existing.sha256 == sha256:
            # 同路径同内容：**200 no-op**（不是 409、也不是再存一份）。重放安全，
            # 而且这条正好接住"上次 blob 传完了、manifest 没传成，这次重来"那种重试。
            return False, existing
        raise ConflictError(
            f"这个路径上已经有一份**内容不同**的快照（{device_id}/{snapshot_id}）："
            "append-only——路径即幂等键，**永不覆盖**。要留新的那一份就换个 snapshot_id"
        )
    _check_limits(_points(store, device_id=device_id), size=size)
    store.ensure_bucket()
    return True, store.put_blob(key, spool, sha256=sha256)


def _store_manifest(
    store: BackupStore, device_id: str, snapshot_id: str, *, data: bytes, sha256: str
) -> tuple[bool, BackupObjectInfo]:
    """把清单落桶（**它是完成标记**，所以先要求快照体已经在桶里）。"""
    if store.head(store.blob_key(device_id, snapshot_id)) is None:
        raise ConflictError(
            f"先传快照体再传清单（{device_id}/{snapshot_id}）："
            "manifest 是「这一份传完了」的标记，落空标记只会让枚举看见一个取不到东西的恢复点"
        )
    key = store.manifest_key(device_id, snapshot_id)
    existing = store.head(key)
    if existing is not None:
        if existing.sha256 == sha256:
            return False, existing
        raise ConflictError(
            f"这个恢复点的清单已经存在且**内容不同**（{device_id}/{snapshot_id}）："
            "append-only——清单描述的是已经落桶的那一份快照，不能改写"
        )
    store.ensure_bucket()
    return True, store.put_manifest(key, data, sha256=sha256)


def _upstream(exc: Exception) -> UpstreamError:
    """对象存储那边的失败 → 502（``upstream_error``）。

    **不翻成 500**：这一类失败几乎总是"地址 / 凭据 / 桶还没建"，用户看到文案就知道该去
    改哪一处；混进"服务端出错了"里就只剩一张要人猜的工单。
    """
    logger.warning("备份桶调用失败", exc_info=True)
    return UpstreamError(str(exc))


def _upload_payload(snapshot_id: str, info: BackupObjectInfo) -> dict[str, Any]:
    model = BackupUploadOut(snapshot_id=snapshot_id, bytes=info.size, sha256=info.sha256)
    return model.model_dump(mode="json")


# ------------------------------------------------------------------ 七条端点


@router.get(
    "/handshake",
    response_model=BackupHandshakeOut,
    summary="备份提供者握手（连通性 + 能力集 + 设备与额度）",
)
def handshake(store: StoreDep, caller: ReadDep) -> BackupHandshakeOut:
    """一次调用回答：**凭据有效吗、这台提供者能做什么、有哪些设备的恢复点、额度用了多少**。

    鉴权在它前面（``require_read``）：没有凭据是 401、凭据无效是 401、凭据有效但越权是 403
    ——**都不是这个响应体的一部分**（与知识库握手同一条口径：一次握手能回来，就说明
    连通与凭据都已经过了）。

    **桶不可用时它仍然是 200**（方案 R5）：``capabilities.snapshot.available=false``
    + 一句可执行的下一步，而不是让客户端把"NAS 还没建桶"当成"服务器坏了"。
    """
    return build_handshake(store, caller)


@router.get(
    "/snapshots",
    response_model=BackupSnapshotListOut,
    summary="列恢复点（最近在前，可按设备过滤）",
)
def list_snapshots(
    store: StoreDep,
    caller: ReadDep,
    device_id: str | None = Query(default=None, description="只看这台设备的（默认全部）"),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> BackupSnapshotListOut:
    """恢复点清单（规范 §1.5 的分页：``limit`` + ``offset`` + ``total``）。

    **只列"完整"的恢复点**：有清单、有快照体、清单读得出来（方案 §1.2 规矩 3）——
    上传中断留下的孤儿 blob 不在里面（它留着不删，append-only，但谁也恢复不了它）。
    """
    device = _segment(device_id, "device_id") if device_id else None
    points = _points(store)
    window = [point for point in points if device is None or point.device_id == device]
    page = window[offset : offset + limit]
    return BackupSnapshotListOut(
        items=[_snapshot_out(point) for point in page],
        total=len(window),
        quota=_quota(points),
    )


@router.put(
    "/snapshots/{device_id}/{snapshot_id}/blob",
    response_model=BackupUploadOut,
    summary="上传一份快照体（append-only：路径即幂等键）",
    responses={
        200: {"description": "同路径同内容：幂等 no-op，桶里那份不动"},
        201: {"description": "新建了一份恢复点的快照体"},
        400: {"description": "sha256 / bytes 与实收不符：拒收，且**不落桶**"},
        409: {"description": "同路径不同内容 / 超保留份数 / 超配额（服务端不覆盖、也不替你删）"},
        413: {"description": "超过 max_blob_bytes：不落桶"},
    },
)
async def put_snapshot_blob(
    request: Request,
    store: StoreDep,
    caller: WriteDep,
    device_id: str,
    snapshot_id: str,
    sha256: str = Query(..., description="整份快照体的 sha256（十六进制 64 位）"),
    bytes_: int = Query(..., alias="bytes", ge=0, description="整份快照体的字节数"),
) -> JSONResponse:
    """接收一整份快照体（``Content-Type: application/octet-stream``）。

    顺序是这条端点的要害：**先收完、验完，再落桶**。所以
    "sha256 / bytes 与实收不符 → 400"这条判据才成立（那一刻桶里还是干净的），
    "超 2 GiB → 413"也是同一条道理。

    只读 key 在 ``require_write`` 就被挡住了（403），根本走不到这里。
    """
    device = _segment(device_id, "device_id")
    snapshot = _segment(snapshot_id, "snapshot_id")
    want = _sha256(sha256)
    if bytes_ > MAX_BLOB_BYTES:
        raise PayloadTooLargeError(
            f"声明的字节数 {bytes_} 超过单份上限 {MAX_BLOB_BYTES}（max_blob_bytes）"
        )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BLOB_BYTES:
        # 早退：连读都不读（大文件在局域网里也要传半天，没必要先收完再拒）
        raise PayloadTooLargeError(
            f"请求体 {declared} 字节超过单份上限 {MAX_BLOB_BYTES}（max_blob_bytes）"
        )

    async with _receive(request, limit=MAX_BLOB_BYTES, what="快照体") as (spool, received, digest):
        if received != bytes_ or digest != want:
            raise BadRequestError(
                f"实收 {received} 字节 / sha256 {digest}，与请求里声明的 "
                f"{bytes_} 字节 / {want} 不一致：拒收，且**一个字节都没有落桶**"
                "（重传时请核对压缩产物是否被改动过）"
            )
        try:
            created, info = await run_in_threadpool(
                _store_blob, store, device, snapshot, sha256=want, size=received, spool=spool
            )
        except BackupError as exc:
            raise _upstream(exc) from exc

    payload = _upload_payload(snapshot, info)
    return JSONResponse(status_code=201 if created else 200, content=payload)


@router.put(
    "/snapshots/{device_id}/{snapshot_id}/manifest",
    response_model=BackupUploadOut,
    summary="上传清单（完成标记：这一份传完了）",
    responses={
        200: {"description": "同一份清单已经在了：幂等 no-op"},
        201: {"description": "清单已落桶——这一刻起这个恢复点可枚举、可恢复"},
        400: {"description": "不是合法 JSON 对象 / 清单里的 device_id、snapshot_id 与路径不符"},
        409: {"description": "快照体不在（先传 blob）/ 同路径清单内容不同"},
        413: {"description": "超过 1 MiB"},
    },
)
async def put_snapshot_manifest(
    request: Request,
    store: StoreDep,
    caller: WriteDep,
    device_id: str,
    snapshot_id: str,
) -> JSONResponse:
    """接收这一个恢复点的清单（``manifest.json``，≤1 MiB）。

    **它是完成标记**，所以有两条额外的硬规矩：快照体必须已经在桶里（否则 409），
    以及清单里的 ``device_id`` / ``snapshot_id`` 必须对得上这条路径（否则 400）——
    一份"描述别的设备"的清单落在这条路径上，恢复时会把两边的账搅在一起。
    """
    device = _segment(device_id, "device_id")
    snapshot = _segment(snapshot_id, "snapshot_id")
    async with _receive(request, limit=MAX_MANIFEST_BYTES, what="清单") as (spool, _, digest):
        # 清单已经全在内存里（≤1 MiB）：一次读出来交给 json，然后原样落桶
        raw = spool.read()
    _parse_manifest(raw, device_id=device, snapshot_id=snapshot)
    try:
        created, info = await run_in_threadpool(
            _store_manifest, store, device, snapshot, data=raw, sha256=digest
        )
    except BackupError as exc:
        raise _upstream(exc) from exc
    payload = _upload_payload(snapshot, info)
    return JSONResponse(status_code=201 if created else 200, content=payload)


def _parse_manifest(raw: bytes, *, device_id: str, snapshot_id: str) -> dict[str, Any]:
    """校一遍清单的形状与归属，返回解析后的对象（**原样留给 GET 那条路**）。"""
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadRequestError(f"清单不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise BadRequestError("清单必须是一个 JSON 对象（顶层不能是数组或标量）")
    found_device = _text(parsed.get("device_id"))
    if found_device and found_device != device_id:
        raise BadRequestError(f"清单里的 device_id（{found_device}）与这条路径（{device_id}）不符")
    found_snapshot = _text(parsed.get("snapshot_id"))
    if found_snapshot and found_snapshot not in (snapshot_id, f"{device_id}-{snapshot_id}"):
        raise BadRequestError(
            f"清单里的 snapshot_id（{found_snapshot}）与这条路径（{snapshot_id}）不符："
            "清单描述的就是它自己那一份快照"
        )
    return parsed


@router.get(
    "/snapshots/{device_id}/{snapshot_id}",
    summary="取一份恢复点的清单（manifest.json 原样）",
    responses={200: {"content": {"application/json": {}}, "description": "清单原文"}},
)
def get_snapshot_manifest(
    store: StoreDep,
    caller: ReadDep,
    device_id: str,
    snapshot_id: str,
) -> JSONResponse:
    """取清单。**原样回**（不做字段裁剪、不做类型收紧）。

    服务端**不解释**清单的业务字段：它是客户端写、客户端读的那份自描述文件
    （方案 §2.3）。用响应模型收一遍字段看着"更类型化"，代价是把不认识的新字段
    悄悄删掉——那是"不猜着读"的反面，也是数据丢失。
    """
    device = _segment(device_id, "device_id")
    snapshot = _segment(snapshot_id, "snapshot_id")
    try:
        raw = store.get_blob(store.manifest_key(device, snapshot))
    except FileNotFoundError as exc:
        raise NotFoundError(f"这个恢复点没有清单（{device}/{snapshot}）") from exc
    except BackupError as exc:
        raise _upstream(exc) from exc
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _upstream(BackupError(f"桶里的清单读不出来（{device}/{snapshot}）：{exc}")) from exc
    return JSONResponse(content=parsed)


@router.get(
    "/snapshots/{device_id}/{snapshot_id}/blob",
    summary="流式下载一份快照体",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "快照体（tar.gz）"}
    },
)
def get_snapshot_blob(
    store: StoreDep,
    caller: ReadDep,
    device_id: str,
    snapshot_id: str,
) -> StreamingResponse:
    """流式下载快照体（**不整份进内存**）。

    响应头里给两样东西：``Content-Length``（进度条用）与 ``X-Kylab-Sha256``（**下载方据此
    校验**——恢复流程要求"下载 → 校验 sha256（不符即中止，绝不落位）"，而不校验 sha256
    就落位，等于把一次网络抖动变成一份坏快照）。
    """
    device = _segment(device_id, "device_id")
    snapshot = _segment(snapshot_id, "snapshot_id")
    try:
        body, info = store.open_stream(store.blob_key(device, snapshot))
    except FileNotFoundError as exc:
        raise NotFoundError(f"这个恢复点没有快照体（{device}/{snapshot}）") from exc
    except BackupError as exc:
        raise _upstream(exc) from exc

    headers = {"Content-Length": str(info.size)}
    if info.sha256:
        headers["X-Kylab-Sha256"] = info.sha256
    return StreamingResponse(
        _stream(body),
        media_type="application/octet-stream",
        headers=headers,
    )


def _stream(body: Any) -> Iterator[bytes]:
    """把对象存储的流一截一截吐出去，**收尾一定把连接还回去**。"""
    try:
        while True:
            chunk = body.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


@router.delete(
    "/snapshots/{device_id}/{snapshot_id}",
    summary="删掉一个恢复点（整份：快照体 + 清单）",
    responses={404: {"description": "这条路径上什么都没有"}},
)
def delete_snapshot(
    store: StoreDep,
    caller: WriteDep,
    device_id: str,
    snapshot_id: str,
) -> dict[str, int]:
    """删掉一个恢复点 —— **删除的粒度就是它**（方案 §1.2 规矩 2：没有"删某个对象"的入口）。

    两个对象一起走，所以删除永远是一次显式动作、不会留下半个恢复点。返回 ``removed``
    是**实际删掉的对象数**（正常是 2；若上一次上传只留下了孤儿 blob，这里会是 1——
    那条路也是清掉半截上传的唯一入口）。什么都没删到就是 404：让它像"成了"对调用方
    没有好处（想删的那一份不在这儿，值得知道）。
    """
    device = _segment(device_id, "device_id")
    snapshot = _segment(snapshot_id, "snapshot_id")
    try:
        removed = store.delete_prefix(store.snapshot_prefix(device, snapshot))
    except BackupError as exc:
        raise _upstream(exc) from exc
    if removed == 0:
        raise NotFoundError(f"这条路径上没有恢复点可删：{device}/{snapshot}")
    return {"removed": removed}
