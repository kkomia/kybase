r"""本机侧快照打包（M5 阶段 2，方案 §2.2 / §2.3 / §2.4）。

把本机档的一切打成**一份便携的包**：``snapshot.tar.gz``（tar.gz，成员顺序固定）::

    manifest.json                       ← 第一成员（同时另传一份独立对象，作完成标记）
    db/kylab.db                         ← 擦洗后的在线备份副本（单文件、无 -wal）
    memory/<账号>/…                     ← 记忆与人设
    files/<对象 Key>                    ← 会话档产物，按原 Key 落（恢复时按同一 Key 还原）
    files/workspace/<产物 id>/<文件名>  ← 工作区产物（默认不备，开关打开才有）

**打包是流式的**（方案 R10）：逐成员 ``TarFile.addfile``、``sha256`` 边写边算，
一个成员一份读缓冲——**不整包进内存**，也不额外落一份中间 tar 文件。唯一多出来的一遍读
是"算快照体摘要"（见 :class:`SnapshotBlob` 那段）：manifest 是归档的第一个成员，
它里面那两个数必须在写第一个字节之前就知道。

**三块内容物**（方案 §2.1）：

- **必备**：本机库（擦洗后）与记忆；设置就在库里（擦洗只删凭据键）；
- **选择性**（方案 §2.2 的三条判据，按优先级）：

  1. **位置判据**（自动）：落在对象存储里的产物（``storage='object'``）一律备；
  2. **工作区判据**（默认关）：落在工作区真实目录里的产物（``storage='workspace'``）
     默认不备——工作区文件是用户的项目文件（v0.3 §4"永不上传"）；打开
     ``provider.backup.include_workspace`` 之后才备，并按第 3 条额度办；
  3. **额度判据**（永远生效）：单文件 ≤ ``BACKUP_ARTIFACT_MAX_BYTES``、一次快照产物
     总量 ≤ ``BACKUP_ARTIFACTS_TOTAL_BYTES``。超限项**逐条**进 manifest 的 ``skipped``
     （名字 / 字节 / 原因），界面与恢复报告都看得见"哪些没备"。

- **永不**：工作区里其他的文件（没有产物记录的那些）、``originals/`` / ``markdown/`` /
  ``images/`` 三个目录（本机档基本为空）、**秘密**（方案 §2.4 的擦洗在存储层做完）。

**manifest 的兼容判据**（方案 §2.3，一条都不猜）：``format_version`` 只增；
``schema_version`` 高于本机的 ``SCHEMA_VERSION`` → 拒绝恢复（照 ``schema.py`` 那条
"不降级"纪律）；读的一方不认识某个版本**当场拒绝**（:func:`parse_manifest` /
:func:`read_archive_manifest` 抛 ``SnapshotFormatError``）。不认识的**新字段原样忽略**
——那是"对方多写了一点"，与本机认不认识这个格式是两件事。

**不做加密**（方案 §2.5 / 决策点 D2）：包里**没有秘密**，所以不需要钥匙；``encryption``
自描述字段留着（今天是 ``"none"``），将来若加客户端加密，读的一方按它自识别。
会话正文里用户自己粘的密码洗不掉——那是数据本身，如实写在方案 §2.5-3 里。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.core.config import get_settings
from app.core.exceptions import InvalidRequestError
from app.services.runtime_config import RuntimeConfigService
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ARTIFACT_IN_WORKSPACE,
    LocalSnapshotArchiver,
    SnapshotArtifactRef,
    SnapshotDbView,
    SnapshotFormatError,
    SnapshotRedaction,
    SnapshotSource,
    StorageError,
    StoreBundle,
)

__all__ = [
    "BACKUP_ARTIFACTS_TOTAL_BYTES",
    "BACKUP_ARTIFACT_MAX_BYTES",
    "COUNT_KEYS",
    "ENCRYPTION",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "INCLUDE_WORKSPACE_KEY",
    "MANIFEST_NAME",
    "MEMBER_DB",
    "MEMBER_FILES",
    "MEMBER_MEMORY",
    "MEMBER_WORKSPACE_FILES",
    "SKIP_REASONS",
    "SNAPSHOT_KINDS",
    "BackupSnapshotService",
    "SnapshotBlob",
    "SnapshotIncluded",
    "SnapshotManifest",
    "SnapshotResult",
    "SnapshotSkipped",
    "parse_manifest",
    "read_archive_manifest",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 格式常量

FORMAT_NAME = "kylab-backup"
"""manifest 的 ``format``：这一份是本应用的备份包。读的一看不对就拒绝。"""

FORMAT_VERSION = 1
"""格式版本（**只增**）：结构变了就 +1，读的一方按它判"认不认识"。"""

ENCRYPTION = "none"
"""``encryption`` 自描述（方案 §2.5 / 决策点 D2）：今天是明文，包里没有秘密。"""

BLOB_NAME = "snapshot.tar.gz"
"""归档文件的名字（桶里的对象名也是它，见 ``services/backup_store.py``）。"""

MANIFEST_NAME = "manifest.json"
"""清单的成员名，也是桶里那份独立对象的名字。**必须是第一个成员**（方案 §2.3）。"""

MEMBER_DB = "db/kylab.db"
"""擦洗后那份库在包里的位置。``db/`` 前缀是刻意的：包解开后它是"一份库"，
而不是又一堆散文件——阶段 5 打开的就是 ``<staging>/db/kylab.db``。"""

DUMP_NAME = Path(MEMBER_DB).name
"""擦洗副本的文件名（``kylab.db``）：与包里的成员同名，两处只写一份。"""

MEMBER_MEMORY = "memory"
"""记忆与人设的成员前缀（``memory/<账号>/MEMORY.md`` …）。"""

MEMBER_FILES = "files"
"""会话档产物的成员前缀（``files/<对象 Key>``，恢复时按同一 Key 还原）。"""

MEMBER_WORKSPACE_FILES = "files/workspace"
"""工作区产物的成员前缀（``files/workspace/<产物 id>/<文件名>``）。

工作区那份产物的真实落点是一台机器上的**绝对路径**（用户自己的目录），进不了包也不该
进包——所以包里的名字由**产物 id**（库里唯一）与**文件名**拼出来，与那台机器无关。
恢复时按同一 id 找回去，而不是把别人机器的目录结构抄进这个包。
"""

SNAPSHOT_KINDS = ("manual", "auto", "pre_restore")
"""``kind`` 的词表（方案 §2.3，与 ``backup_snapshots`` 表的 CHECK 同一个集合）。"""

SKIP_REASONS = (
    "too_large",
    "total_budget",
    "workspace_not_included",
    "missing",
    "unsafe_location",
    "unknown_storage",
    "symlink",
)
"""``skipped[].reason`` 的词表。

前三条是方案 §2.2 写死的三条额度 / 开关判据；后四条是**如实报**那一类：

- ``missing``：库里记着这条产物、盘上那份字节不在了（被手工删过）；
- ``unsafe_location``：对象 Key 不是安全的相对路径（含 ``..`` / 绝对路径 / 反斜杠），
  拒绝把它写进包（写进去就是一个越界的成员名）；
- ``unknown_storage``：``storage`` 不是 ``object`` / ``workspace`` 那两个已知值——
  "不知道它在哪"比"猜一个"诚实；
- ``symlink``：记忆目录里的符号链接**不跟**（它可能指向工作区里的文件，而"工作区文件
  永不上传"这条纪律不该被一个链接绕过去）。
"""


# ---------------------------------------------------------------- 三条"选择性"判据的额度

BACKUP_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024
"""单份产物的上限（64 MiB，方案 §2.2-3）。超了一条进 ``skipped``，不截断、不静默丢。"""

BACKUP_ARTIFACTS_TOTAL_BYTES = 512 * 1024 * 1024
"""一次快照里产物字节总量的上限（512 MiB）。超了的**逐条**进 ``skipped``（``total_budget``）：
后面那些装不下的照报，前面装下的照备——用户看得到"哪几份没进去"。"""

INCLUDE_WORKSPACE_KEY = "provider.backup.include_workspace"
"""工作区产物那个开关的设置键（方案 §2.2-2，默认关）。

它是**行为参数**而不是凭据，但它落在 ``provider.`` 那一族里，于是与凭据一起被擦洗排除
（方案 §2.4 的三前缀判据）：恢复时设置只补本机没有的键，这类开关跟着本机走。
"""

MEMORY_SETTING_KEY = "memory.workspace"
"""记忆根目录的设置键。取值口径与 ``services/memory.py`` **逐字一致**（同一句回落 ``memory``）：
两处各写一遍"记忆在哪"，快照里备的记忆与实际用的那份就可能不是同一个目录。"""

_HASH_CHUNK = 1024 * 1024
"""算摘要 / 写归档时的读块大小。1 MiB：够大（少几次系统调用）又够小（不进内存）。"""


# ---------------------------------------------------------------- 数据记录


@dataclass(frozen=True, slots=True)
class SnapshotBlob:
    """manifest 的 ``blob`` 段：**快照体**的自述（名字 / 字节 / 摘要 / 压缩方式）。

    这里的两个数是"快照体"而不是"归档文件自己"——**这不是笔误**：manifest 是归档的
    **第一个成员**（方案 §2.3），而一个文件的字节数与 sha256 在写第一个成员的时候还不存在，
    把归档自己的摘要放进去是**自指**（摘要改变摘要，永远算不出来）。所以这一段记的是
    快照体：成员按归档顺序串起来的原始字节（``db/kylab.db`` → ``memory/…`` → ``files/…``，
    **不含 manifest 那一段**），它同时就是 ``snapshot_id`` 里那 8 位的内容寻址依据
    （方案 §1.2"hash8 = 快照体 sha256 前 8 位"）。

    归档文件自身的字节数与 sha256 **边写边算**，由 :class:`SnapshotResult` 交给上传那一层
    （上传时声明给服务端的、以及服务端边收边算要核对的，都是"文件自己"那一对）。
    """

    name: str = BLOB_NAME
    bytes: int = 0
    sha256: str = ""
    compression: str = "gzip"

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "compression": self.compression,
        }


@dataclass(frozen=True, slots=True)
class SnapshotSkipped:
    """``skipped`` 段的一条：**哪一份没备、多大、为什么**（方案 §2.2-3）。

    ``name`` 对对象存储的产物是它的 Key（一个相对名字，本来就出机器、也本来就是它的身份）；
    对工作区产物是**显示名**——绝对路径不进 manifest：manifest 是要发到 NAS 上去的，
    而"这台机器上用户自己的目录结构"不是备份要带走的东西（这条与方案 §2.3 例子里那个
    ``项目/大报告.pptx`` 是一个意思：只给相对身份）。
    """

    name: str
    size_bytes: int = 0
    reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {"name": self.name, "size_bytes": self.size_bytes, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SnapshotIncluded:
    """``included`` 段：这一份里**真的有什么**（与 ``counts`` 的"库里有几条"分开口径）。

    - ``db`` / ``settings``：恒 ``True``——库就是快照的主体，而设置就在库里
      （凭据类键已被擦洗删掉，见 ``redacted``）；
    - ``memory``：记忆那一块**扫到了文件**才为 ``True``（一个空记忆目录不该说"在包里"）；
    - ``artifacts_object`` / ``artifacts_workspace``：真正进包的产物条数（两档分开报，
      "工作区那份默认没备"这件事要看得见，见方案 §2.2-2）。
    """

    db: bool = True
    memory: bool = False
    settings: bool = True
    artifacts_object: int = 0
    artifacts_workspace: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "db": self.db,
            "memory": self.memory,
            "settings": self.settings,
            "artifacts_object": self.artifacts_object,
            "artifacts_workspace": self.artifacts_workspace,
        }


COUNT_KEYS = (
    "conversations",
    "messages",
    "session_events",
    "notes",
    "workspaces",
    "scheduled_tasks",
    "artifacts",
    "memory_files",
)
"""``counts`` 段**恰好**这八个键（方案 §2.3）。

``memory_files`` 是文件数（打包时扫目录数出来的），其余七个是库里的行数——所以 ``counts``
说的是"库侧事实"，与 ``included`` 的"真的进包几条"分工不同。写成常量是为了用例能机械核对
"多一个少一个都算变契约"，而不是靠肉眼对字段。
"""


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """``manifest.json`` 的全部字段（方案 §2.3 逐字段），一份自描述的自述文件。

    **它是客户端写、客户端读的那份文件**：NAS 侧原样收、原样回、不解释业务字段
    （服务端多知道一件事，就多一个要跟着改的地方）。所以这里的字段说明就是契约本身。

    ``created_at`` 是 UTC 的 ``YYYY-MM-DDTHH:MM:SSZ``；``snapshot_id`` 是
    ``<device_id>-<archive_ts>-<hash8>``，其中 ``archive_ts`` 把 ISO 时刻里的冒号换成短横
    ——Windows 文件名与 URL 段都不接受冒号，而快照 id 这两处都要走。
    """

    snapshot_id: str
    device_id: str
    created_at: str
    kind: str
    schema_version: int
    blob: SnapshotBlob
    counts: dict[str, int] = field(default_factory=dict)
    included: SnapshotIncluded = field(default_factory=SnapshotIncluded)
    device_name: str = ""
    app_version: str = ""
    platform: str = ""
    format: str = FORMAT_NAME
    format_version: int = FORMAT_VERSION
    skipped: tuple[SnapshotSkipped, ...] = ()
    redacted: tuple[SnapshotRedaction, ...] = ()
    encryption: str = ENCRYPTION

    def as_payload(self) -> dict[str, Any]:
        """manifest 的 JSON 形状（**唯一一处拼装点**：写包与另传一份都走它）。"""
        return {
            "format": self.format,
            "format_version": self.format_version,
            "snapshot_id": self.snapshot_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "created_at": self.created_at,
            "kind": self.kind,
            "app_version": self.app_version,
            "schema_version": self.schema_version,
            "platform": self.platform,
            "blob": self.blob.as_payload(),
            "counts": {key: int(self.counts.get(key, 0)) for key in COUNT_KEYS},
            "included": self.included.as_payload(),
            "skipped": [item.as_payload() for item in self.skipped],
            "redacted": [item.as_payload() for item in self.redacted],
            "encryption": self.encryption,
        }

    def to_bytes(self) -> bytes:
        """写进包与另传一份用的**同一串字节**（``ensure_ascii=False``：中文名字可读）。"""
        return json.dumps(self.as_payload(), ensure_ascii=False, indent=2).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """一次打包的结果：包在哪、多大、摘要是什么、清单原文。

    ``blob_bytes`` / ``blob_sha256`` 是**归档文件自己**的那一对（边写边算）：上传那一步
    用它们声明（``PUT …/blob?sha256=&bytes=``，服务端边收边算来核对）；``manifest_bytes``
    是另传一份 ``manifest.json`` 时该原样发的那串字节（与包里的第一个成员逐字节相同——
    两份不同就等于有了两个真相）。
    """

    snapshot_id: str
    blob_path: Path
    blob_bytes: int
    blob_sha256: str
    manifest: SnapshotManifest
    manifest_bytes: bytes


# ---------------------------------------------------------------- manifest 的读


def _require(payload: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
    """取一个必备字段并核对类型（不对就抛 ``SnapshotFormatError``，**不猜着读**）。"""
    if key not in payload:
        raise SnapshotFormatError(f"{where}里没有必备字段 {key!r}：这份清单不完整")
    value = payload[key]
    if kind is int:
        # 布尔是 int 的子类，而 `"format_version": true` 不该被当成 1 收下
        if not isinstance(value, int) or isinstance(value, bool):
            raise SnapshotFormatError(f"{where}的 {key!r} 不是整数（读到 {value!r}）")
    elif not isinstance(value, kind):
        raise SnapshotFormatError(f"{where}的 {key!r} 类型不对（读到 {value!r}）")
    return value


def _counts_of(value: Any) -> dict[str, int]:
    """``counts`` 段：只收整数（类型不对就拒绝——它不是可以"顺手转一下"的展示字段）。"""
    if not isinstance(value, Mapping) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in value.values()
    ):
        raise SnapshotFormatError(f"清单的 counts 必须是「名字 → 整数」的对象（读到 {value!r}）")
    return {str(name): int(item) for name, item in value.items()}


def parse_manifest(
    raw: bytes | str | Mapping[str, Any], *, local_schema_version: int
) -> SnapshotManifest:
    """把一份 manifest（原文或已解析的对象）读成 :class:`SnapshotManifest`。

    **三条兼容判据**（方案 §2.3）：

    1. ``format`` 不是 ``kylab-backup`` → 拒绝（这不是我们的包）；
    2. ``format_version`` 高于本机认识的 ``FORMAT_VERSION`` → 拒绝（不认识就**当场说**，
       绝不硬按旧结构读——那会把"格式变了"显示成"数据坏了"）；
    3. ``schema_version`` 高于本机的 ``SCHEMA_VERSION`` → 拒绝（快照里的库结构比本机新，
       读它就是在猜）。

    必备字段是"格式 / 版本 / 身份 / blob"这几样；``counts`` 那些展示段缺席按空算——
    它们不影响能不能读，只影响报告里显示什么。
    """
    payload: Any = raw
    if isinstance(raw, (bytes, str)):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SnapshotFormatError(f"清单不是合法 JSON：{exc}") from exc
    if not isinstance(payload, Mapping):
        raise SnapshotFormatError("清单必须是一个 JSON 对象（顶层不能是数组或标量）")

    where = "清单"
    found_format = _require(payload, "format", str, where)
    if found_format != FORMAT_NAME:
        raise SnapshotFormatError(f"这不是本应用的备份包（format={found_format!r}）")
    found_version = _require(payload, "format_version", int, where)
    if found_version > FORMAT_VERSION:
        raise SnapshotFormatError(
            f"这份快照的格式版本是 {found_version}，高于本机认识的 {FORMAT_VERSION}；"
            "请升级应用，而不是按旧结构猜着读"
        )
    found_schema = _require(payload, "schema_version", int, where)
    if found_schema > local_schema_version:
        raise SnapshotFormatError(
            f"这份快照的 schema 版本是 {found_schema}，高于本机的 {local_schema_version}；"
            "快照是被更新版应用打的，请升级应用，而不是按旧结构猜着读"
        )

    blob_where = "清单的 blob"
    blob_raw = _require(payload, "blob", Mapping, where)
    blob = SnapshotBlob(
        name=_require(blob_raw, "name", str, blob_where),
        bytes=_require(blob_raw, "bytes", int, blob_where),
        sha256=_require(blob_raw, "sha256", str, blob_where),
        compression=_require(blob_raw, "compression", str, blob_where),
    )
    included_raw = payload.get("included")
    included = SnapshotIncluded()
    if isinstance(included_raw, Mapping):
        included = SnapshotIncluded(
            db=bool(included_raw.get("db")),
            memory=bool(included_raw.get("memory")),
            settings=bool(included_raw.get("settings")),
            artifacts_object=int(included_raw.get("artifacts_object") or 0),
            artifacts_workspace=int(included_raw.get("artifacts_workspace") or 0),
        )

    return SnapshotManifest(
        snapshot_id=_require(payload, "snapshot_id", str, where),
        device_id=_require(payload, "device_id", str, where),
        created_at=_require(payload, "created_at", str, where),
        kind=_require(payload, "kind", str, where),
        schema_version=found_schema,
        blob=blob,
        counts=_counts_of(payload.get("counts", {})),
        included=included,
        device_name=str(payload.get("device_name") or ""),
        app_version=str(payload.get("app_version") or ""),
        platform=str(payload.get("platform") or ""),
        format=found_format,
        format_version=found_version,
        skipped=tuple(
            SnapshotSkipped(
                name=str(item.get("name") or ""),
                size_bytes=int(item.get("size_bytes") or 0),
                reason=str(item.get("reason") or ""),
            )
            for item in payload.get("skipped") or ()
            if isinstance(item, Mapping)
        ),
        redacted=tuple(
            SnapshotRedaction(
                table=str(item.get("table") or ""),
                column=str(item.get("column") or ""),
                key=str(item.get("key") or ""),
                rows=int(item.get("rows") or 0),
            )
            for item in payload.get("redacted") or ()
            if isinstance(item, Mapping)
        ),
        encryption=str(payload.get("encryption") or ENCRYPTION),
    )


def read_archive_manifest(blob_path: str | Path, *, local_schema_version: int) -> SnapshotManifest:
    """只读包里的**第一个成员**（``manifest.json``）并解析它。

    "manifest 必须第一个"（方案 §2.3）在写侧是纪律，在读侧就是这一条：第一个成员不是
    ``manifest.json`` 的包**读不了**。好消息是只读第一个成员就够了，不必解整包——
    这正是"将来流式读"要的形状（快照页能先拿到清单再决定要不要下完整个包）。
    """
    with tarfile.open(Path(blob_path), "r:gz") as archive:
        member = archive.next()
        name = None if member is None else member.name
        if member is None or name != MANIFEST_NAME:
            raise SnapshotFormatError(
                f"这个包里第一个成员不是 {MANIFEST_NAME}（读到 {name!r}）："
                "快照的清单必须是第一个成员"
            )
        handle = archive.extractfile(member)
        if handle is None:
            raise SnapshotFormatError(f"读不出 {MANIFEST_NAME} 的内容（它不是普通成员）")
        raw = handle.read()
    return parse_manifest(raw, local_schema_version=local_schema_version)


# ---------------------------------------------------------------- 打包


class _BytesReader:
    """把一段 ``bytes`` 当文件对象给 ``tarfile``（``addfile`` 只要求 ``read``）。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _CountingReader:
    """读一个成员时**顺手把它的字节喂进摘要**：打包这一遍读到的东西就是包里真有的东西。

    它换来的是"**自己的清单对得上自己**"这条：预读那遍算出来的快照体摘要（manifest 里
    那两个数、也是 ``snapshot_id`` 里那 8 位）在写包时被**逐字节复核**一遍——两遍之间
    产物被改小（或换掉）时，``addfile`` 只会照着 tar 头里声明的字节数少写一截并把整包
    写歪，而这里会当场把这份包判掉（见 ``_write_archive``）。
    """

    def __init__(self, handle: BinaryIO, digest: Any) -> None:
        self._handle = handle
        self._digest = digest

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        if data:
            self._digest.update(data)
        return data


class _HashingWriter:
    """一个"边写边算"的写口：把字节转给真文件，同时累加 sha256 与字节数。

    ``tarfile`` 在写模式下要 ``write`` 与 ``tell``（``TarFile.__init__`` 会读一次当前位置），
    gzip 那层还会 ``flush``——三个都在这儿。**不缓存数据**：进来的字节立刻转给文件，
    所以"整包不进内存"是这一层的形状保证的，不靠调用方自觉。
    """

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._digest = hashlib.sha256()
        self.bytes = 0

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def write(self, data: bytes) -> int:
        written = self._raw.write(data)
        self._digest.update(data)
        self.bytes += len(data)
        return written

    def tell(self) -> int:
        return self._raw.tell()

    def flush(self) -> None:
        self._raw.flush()


#: 成员的权限位一律 600：这份包里是**用户自己的会话与记忆**，解开之后不该是一个
#: "谁都能读"的文件（tar 默认的 644 把这件事交给了 umask 之外的运气）。
_MEMBER_MODE = 0o600


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes, *, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = _MEMBER_MODE
    archive.addfile(info, _BytesReader(data))


def _add_file(archive: tarfile.TarFile, name: str, path: Path, digest: Any) -> None:
    """加一个文件成员，并把它读到的字节喂进 ``digest``（复核见 ``_write_archive``）。"""
    status = path.stat()
    info = tarfile.TarInfo(name)
    info.size = status.st_size
    info.mtime = int(status.st_mtime)
    info.mode = _MEMBER_MODE
    with path.open("rb") as handle:
        archive.addfile(info, _CountingReader(handle, digest))


def _digest_members(members: Sequence[tuple[str, Path]]) -> tuple[str, int]:
    """快照体的 sha256 与字节数（成员按归档顺序串起来的原始字节，不含成员名与 tar 头）。

    这是**唯一**多出来的一遍读（见模块头）：manifest 是第一个成员，所以它里面那两个数
    必须在写归档之前算出来。读块 1 MiB，内存里只留一块。它的结论不是"说完就算"——
    写包那一遍会把同样的字节再喂进一个摘要并**逐字节复核**（见 ``_write_archive``）。
    """
    digest = hashlib.sha256()
    total = 0
    for _name, path in members:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
                total += len(chunk)
    return digest.hexdigest(), total


@dataclass(slots=True)
class _Selection:
    """三条判据跑完的结果（打包那一层拿它拼 manifest）。"""

    members: list[tuple[str, Path]] = field(default_factory=list)
    skipped: list[SnapshotSkipped] = field(default_factory=list)
    memory_files: int = 0
    artifacts_object: int = 0
    artifacts_workspace: int = 0


class BackupSnapshotService:
    """本机侧的打包服务：**一份擦洗过的库 + 记忆 + 选择性产物 → 一个 tar.gz**。

    **它不碰 SQLite**（L2）：库那一半（在线备份 / 擦洗 / ``VACUUM`` / 读快照库）全在
    ``StoreBundle.snapshot`` 那两份协议后面，本模块只做"挑、读、打包、写清单"。它也不碰
    网络、不碰队列：入队与补传是阶段 3 的事，这里只交出"一份打好的包 + 它的清单"。
    """

    def __init__(
        self,
        *,
        stores: StoreBundle,
        data_dir: str | Path,
        device_id: str,
        device_name: str = "",
        runtime_config: RuntimeConfigService | None = None,
        app_version: str = "",
        platform_name: str = "",
    ) -> None:
        snapshot = stores.snapshot
        if not (
            isinstance(snapshot, LocalSnapshotArchiver) and isinstance(snapshot, SnapshotSource)
        ):
            raise RuntimeError(
                "这个部署没有快照能力（StoreBundle.snapshot 为 None）：快照是本机档独有的"
                "（服务器档的库就是它自己，没有「打包带走」这条动作）"
            )
        self._snapshot = snapshot
        self._data_dir = Path(data_dir)
        self._device_id = device_id
        self._device_name = device_name
        self._runtime = runtime_config
        self._app_version = app_version or get_settings().app_version
        self._platform = platform_name or sys.platform

    # -------------------------------------------------------------- 对外

    def create(
        self,
        *,
        into: str | Path,
        kind: str = "manual",
        created_at: datetime | None = None,
    ) -> SnapshotResult:
        """打一份快照，落在 ``into/<snapshot_id>.tar.gz``，返回结果（含清单原文）。

        落在**调用方给的目录**里而不是某个写死的位置：本机档把它放
        ``<data_dir>/backup/pending/``（阶段 3 的队列），而"恢复前那一份"另有落点
        （方案 §3.2 的 ``pre_restore``）——路径是调用方的事，这里只负责"打出一份好包"。

        ``created_at`` 可注入（用例钉住 id 与时间戳）；不给就是此刻（UTC）。
        """
        if not self._device_id:
            # R12：**绝不编一个 id**。设备身份来自壳（``--device-id``），没有它就没法对齐
            # "同一台机器的恢复点"——说清下一步比造一个假 id 有用得多。
            raise InvalidRequestError(
                "这台机器还没有设备身份：先在桌面壳里登录一次（备份要按设备对齐恢复点）"
            )
        if kind not in SNAPSHOT_KINDS:
            raise InvalidRequestError(
                f"未知的快照类型 {kind!r}（只能是 {'、'.join(SNAPSHOT_KINDS)}）"
            )

        moment = created_at or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC)
        created_text = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        archive_stamp = moment.strftime("%Y-%m-%dT%H-%M-%SZ")

        target_dir = Path(into)
        target_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="kylab-snapshot-", dir=target_dir) as workdir:
            work = Path(workdir)
            dump = self._snapshot.dump_scrubbed_db(work / DUMP_NAME)
            self._assert_lone_db(work)
            view = self._snapshot.read_snapshot_db(dump.path)
            selection = self._select(view)
            members = [(MEMBER_DB, dump.path), *selection.members]

            content_sha256, content_bytes = _digest_members(members)
            snapshot_id = f"{self._device_id}-{archive_stamp}-{content_sha256[:8]}"
            counts = {key: dump.counts.get(key, 0) for key in COUNT_KEYS}
            counts["memory_files"] = selection.memory_files
            manifest = SnapshotManifest(
                snapshot_id=snapshot_id,
                device_id=self._device_id,
                created_at=created_text,
                kind=kind,
                schema_version=dump.schema_version,
                blob=SnapshotBlob(bytes=content_bytes, sha256=content_sha256),
                counts=counts,
                included=SnapshotIncluded(
                    memory=selection.memory_files > 0,
                    artifacts_object=selection.artifacts_object,
                    artifacts_workspace=selection.artifacts_workspace,
                ),
                device_name=self._device_name,
                app_version=self._app_version,
                platform=self._platform,
                skipped=tuple(selection.skipped),
                redacted=dump.redacted,
            )
            manifest_bytes = manifest.to_bytes()
            blob_path = target_dir / f"{snapshot_id}.tar.gz"
            blob_bytes, blob_sha256 = self._write_archive(
                blob_path,
                manifest_bytes,
                members,
                mtime=int(moment.timestamp()),
                expected=manifest.blob,
            )

        logger.info(
            "快照已打好：%s（%d 字节，内容摘要 %s…，洗掉 %d 项，跳过 %d 项）",
            blob_path,
            blob_bytes,
            content_sha256[:8],
            len(manifest.redacted),
            len(manifest.skipped),
        )
        return SnapshotResult(
            snapshot_id=snapshot_id,
            blob_path=blob_path,
            blob_bytes=blob_bytes,
            blob_sha256=blob_sha256,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
        )

    # -------------------------------------------------------------- 内部：断言与开关

    @staticmethod
    def _assert_lone_db(workdir: Path) -> None:
        """打包前断言：那个目录里**只有 ``kylab.db``**（方案 §2.4-4）。

        ``-wal`` / ``-shm`` 一旦出现，就说明这份副本还处在"库不是一个文件"的状态——
        那正是"照文件拷会少一截"那类事故的形状。宁可在这里当场停，也不要打出一份
        自己都说不清完整性的包。
        """
        found = sorted(item.name for item in workdir.iterdir())
        if found != [DUMP_NAME]:
            raise StorageError(
                f"擦洗副本的目录里不止 {DUMP_NAME}（读到 {found}）：副本不该带 -wal / -shm，"
                "请检查本机库的 journal 模式"
            )

    def _include_workspace(self) -> bool:
        """工作区产物那个开关（方案 §2.2-2，默认关）。

        拿不到运行期配置时按"没打开"算——**默认不备**是这条判据的原话，而"读不到配置"
        不该变成"那就备上"（那会把用户的文件发出去）。
        """
        if self._runtime is None:
            return False
        return self._runtime.get_bool(INCLUDE_WORKSPACE_KEY, default=False)

    def _memory_root(self) -> Path:
        """记忆根目录：取值口径与 ``services/memory.py`` 逐字一致（同一句回落 ``memory``）。"""
        raw = (self._runtime.get(MEMORY_SETTING_KEY) if self._runtime else "") or "memory"
        return self._data_dir / raw.strip()

    # -------------------------------------------------------------- 内部：三条判据

    def _select(self, view: SnapshotDbView) -> _Selection:
        """三条判据跑一遍，产出"进包的成员"与"没进包的理由"（方案 §2.2）。

        **判据的顺序**是优先级：**位置 → 工作区开关 → 额度**。额度判据永远生效，超限的
        逐条进 ``skipped`` 并**继续看后面的**（后面的更小就能装下——用户要的是"能备的都
        备上"，不是"碰到一份大的就全放弃"）。

        **成员顺序**是另一件事，照方案 §2.3 的归档布局：``db`` → ``memory`` → ``files``。
        两个顺序不是一回事（记忆没有额度判据、产物才有），所以这里分开攒，最后拼成
        ``[库, 记忆…, 产物…]``。
        """
        selection = _Selection()
        memory_members, memory_skipped = self._memory_members()
        selection.memory_files = len(memory_members)
        artifacts: list[tuple[str, Path]] = []
        include_workspace = self._include_workspace()
        total = 0

        for artifact in view.artifacts:
            if artifact.storage == ARTIFACT_IN_WORKSPACE and not include_workspace:
                selection.skipped.append(self._skipped(artifact, reason="workspace_not_included"))
                continue
            located = self._locate(artifact)
            if isinstance(located, SnapshotSkipped):
                selection.skipped.append(located)
                continue
            member_name, source = located
            size = source.stat().st_size
            if size > BACKUP_ARTIFACT_MAX_BYTES:
                selection.skipped.append(
                    self._skipped(artifact, reason="too_large", size_bytes=size)
                )
                continue
            if total + size > BACKUP_ARTIFACTS_TOTAL_BYTES:
                selection.skipped.append(
                    self._skipped(artifact, reason="total_budget", size_bytes=size)
                )
                continue
            total += size
            artifacts.append((member_name, source))
            if artifact.storage == ARTIFACT_IN_WORKSPACE:
                selection.artifacts_workspace += 1
            else:
                selection.artifacts_object += 1

        selection.members = [*memory_members, *artifacts]
        selection.skipped.extend(memory_skipped)
        return selection

    def _skipped(
        self, artifact: SnapshotArtifactRef, *, reason: str, size_bytes: int | None = None
    ) -> SnapshotSkipped:
        """没进包的一条：对象档给 Key（它本来就是相对名字），工作区档给显示名。

        工作区那份的**绝对路径不进 manifest**：manifest 是要发到 NAS 上去的，而那台机器上
        用户自己的目录结构不是备份的内容（见 :class:`SnapshotSkipped`）。``size_bytes``
        缺省用库里记的那个数（盘上找不到时它是唯一的线索）；能 stat 到时用真实大小——
        "这一份多大"报的是它真实的体量，不是库里那条可能过期的记录。
        """
        object_side = artifact.storage == ARTIFACT_IN_OBJECTS
        name = (artifact.location or artifact.name) if object_side else artifact.name
        return SnapshotSkipped(
            name=name or artifact.id,
            size_bytes=artifact.size_bytes if size_bytes is None else size_bytes,
            reason=reason,
        )

    def _locate(self, artifact: SnapshotArtifactRef) -> tuple[str, Path] | SnapshotSkipped:
        """位置判据：这条记录的字节在哪、进包叫什么名字（找不到就如实报一条）。

        两档的成员名与来源：

        - 对象档：``files/<Key>``，来源是 ``<data_dir>/<Key>``——**本机档的对象存储就是
          data_dir**（``core/storage.py`` 的装配），按 Key 直接取文件是为了**流式**：
          ``ObjectStore.read()`` 是整份进内存的，大产物一旦进内存，"打包占用可控"就落空了；
        - 工作区档：``files/workspace/<产物 id>/<文件名>``，来源是记录里那个绝对路径
          （用户在**自己这台机器**上打开了那个开关，读的是他自己的文件）。
        """
        if artifact.storage == ARTIFACT_IN_OBJECTS:
            key = (artifact.location or "").strip()
            if not key or key.startswith(("/", "\\")) or "\\" in key or ".." in Path(key).parts:
                logger.warning("产物 %s 的 Key 不是安全的相对路径，跳过：%r", artifact.id, key)
                return SnapshotSkipped(
                    name=artifact.name or artifact.id,
                    size_bytes=artifact.size_bytes,
                    reason="unsafe_location",
                )
                # 不写那个 Key：它**本身就是**那条不安全的路径（可能是这台机器上的绝对路径），
                # 而 manifest 要发到 NAS 上去——只报显示名，够用户认出是哪一份。
            path = self._data_dir / key
            if not path.is_file():
                return self._skipped(artifact, reason="missing")
            return f"{MEMBER_FILES}/{key}", path

        if artifact.storage == ARTIFACT_IN_WORKSPACE:
            source = Path(artifact.location)
            if not artifact.location or not source.is_file():
                return self._skipped(artifact, reason="missing")
            return f"{MEMBER_WORKSPACE_FILES}/{artifact.id}/{source.name or artifact.id}", source

        return self._skipped(artifact, reason="unknown_storage")

    # -------------------------------------------------------------- 内部：记忆

    def _memory_members(self) -> tuple[list[tuple[str, Path]], list[SnapshotSkipped]]:
        """记忆与人设的成员（``memory/<账号>/…``，方案 §2.1 的"原样"）与没进包的那些。

        两处刻意的取舍：

        - **符号链接不跟**（进 ``skipped``，理由 ``symlink``）：记忆目录里一个指向工作区的
          链接会让"工作区文件永不上传"这条纪律从后门失效；
        - **派生的向量索引照备**（``<账号>/mem_metadata/``）：方案 §2.1 说的是 ``memory/**``
          原样，而"原样"的意思是恢复之后那份记忆工作区与现在这个是同一份——少一份派生文件
          不丢数据，但会多一次重建。
        """
        root = self._memory_root()
        if not root.is_dir():
            return [], []
        members: list[tuple[str, Path]] = []
        skipped: list[SnapshotSkipped] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            member = f"{MEMBER_MEMORY}/{relative}"
            if path.is_symlink():
                skipped.append(SnapshotSkipped(name=member, reason="symlink"))
            elif path.is_file():
                members.append((member, path))
        return members, skipped

    # -------------------------------------------------------------- 内部：写归档

    def _write_archive(
        self,
        dest: Path,
        manifest_bytes: bytes,
        members: Sequence[tuple[str, Path]],
        *,
        mtime: int,
        expected: SnapshotBlob,
    ) -> tuple[int, str]:
        """流式写出 ``dest``：manifest 第一、逐成员 ``addfile``、sha256 边写边算。

        没有中间 tar 文件、没有整包进内存：进来的字节立刻落盘并进摘要。

        写完**复核一遍**：这一遍读到的快照体（``expected`` 是预读那遍算出来的那份摘要）
        必须逐字节对得上。对不上只有一种原因——两遍之间那份内容被改过（或短读了一截），
        而那种包最坏的形态是"tar 头声明的字节数大于实际写进去的"，后面每一个成员都会
        错位：读它的人拿到的是垃圾，却看不出是垃圾。所以这里**把那份包删掉并当场报错**，
        而不是留下一份自己的清单都对不上的包（备份工具最不该做的事就是把坏包交出去）。
        """
        body = hashlib.sha256()
        with dest.open("wb") as raw:
            sink = _HashingWriter(raw)
            with tarfile.open(fileobj=sink, mode="w|gz") as archive:
                _add_bytes(archive, MANIFEST_NAME, manifest_bytes, mtime=mtime)
                for name, path in members:
                    _add_file(archive, name, path, body)
        if body.hexdigest() != expected.sha256:
            dest.unlink(missing_ok=True)
            raise StorageError(
                "打包期间快照体的内容变了（写进去的字节与预读时算出的摘要不符）："
                "这份包已删掉，请重打一份（产物正在被改写时不适合打快照）"
            )
        return sink.bytes, sink.sha256
