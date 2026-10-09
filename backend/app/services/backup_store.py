"""备份桶的薄封装（M5 阶段 1，路 B 的服务端那一半）。

**为什么另起一份，不扩 ``ObjectStore``**（决策点 D6，方案 §1.5）：``ObjectStore`` 是知识库
那 5 个方法的面（``write`` / ``read`` / ``exists`` / ``move_to_trash`` / ``delete``），
而它的构造器还会 ``head_bucket``——**缺桶时会把 NAS 启动搞崩**（`s3_impl/object_store.py:116`），
备份桶却不允许这样：它允许"先没建、后来再建"（方案 R5：**启动不校验桶**，懒 ``ensure_bucket``）。
备份要的东西也完全不同：分片上传（大文件不进内存）、按前缀列举、按"一个恢复点"整份删除、
以及"这一份落桶之后的真实 sha256 / 字节数是多少"。把这几样塞进知识库那 5 个方法里，
等于让两个不同的问题共用一份接口。

**桶布局与命名**（方案 §1.2，一个字不改）::

    桶：KYLAB_BACKUP_BUCKET（默认 kylab-backup，**与知识库那个桶分开**）
    键：<KYLAB_BACKUP_PREFIX>backup/<device_id>/<snapshot_ts>-<hash8>/
            snapshot.tar.gz     ← 先传（大）
            manifest.json       ← 后传（小，**完成标记**）

    provider = "backup"（写死常量 ``BACKUP_DIR``；将来别的提供者不混进来）
    device_id = 壳的 config.json.device_id（UUID v4）
    snapshot_ts = UTC "YYYY-MM-DDTHH-MM-SSZ"
    hash8 = 快照体 sha256 前 8 位（解决同秒冲突 + 天然幂等判据）

路径里那一段 ``<snapshot_ts>-<hash8>`` 就是 API 上说的 ``snapshot_id``：**恢复点由
``(device_id, snapshot_id)`` 这对坐标唯一确定**（URL 也是这两段），本机队列那条
``<device_id>-<ts>-<hash8>`` 的整串 id 只是这一对拼起来的样子（方案 §3.1）。

**append-only 的四条硬规矩**（方案 §1.2；契约写在 ``docs/规范/API-接口规范-v0.1.md`` §1.13，
用例钉在 ``tests/unit/api/test_backup_snapshots_api.py``）：

1. **不覆盖**：同路径同 sha256 → 201→200 no-op；同路径不同 sha256 → **409（永不覆盖）**——
   sha256 在上传时写进**对象元数据**，重试时靠 ``head`` 读回来比对（没有元数据的对象
   一律当"内容不同"，宁可 409 也不覆盖）；
2. **只能整份删**：删除的粒度是"一个恢复点"（``delete_prefix`` 把两个对象一起删），
   没有"删某个对象"的入口；
3. **枚举只认有 manifest 的**：孤儿 blob（上传中断）**留着不删**，但枚举忽略——
   这一条同时满足"append-only"与"完整才可见"；
4. **服务端绝不替用户删**：保留份数 / 配额超限时**报错**，不静默淘汰。

**boto3 是惰性 import 的**（``_boto()``，照 ``storage/s3_impl/object_store.py:42-61`` 的手法）：
这个模块挂在 ``app.api.v1.router`` 的导入链上，而那条链在**客户端运行时**（桌面壳的边车）
里也会被导入——客户端一个字节都不往 S3 发，所以 boto3 不许进它的闭包。
判据不是"看代码觉得不该有"，而是真跑一遍：``python scripts/sidecar-closure.py``
仍是 **17 个发行包**（M4 基线）。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, BinaryIO, TypeVar

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    # 只在注解与类型里用到：`from __future__ import annotations` 之下注解不求值，
    # 所以这一句**不进运行时**、也就不进客户端的导入闭包。
    from collections.abc import Iterator

__all__ = [
    "BACKUP_DIR",
    "BLOB_NAME",
    "MANIFEST_NAME",
    "BackupBucketStatus",
    "BackupError",
    "BackupObjectInfo",
    "BackupStore",
    "build_backup_store",
    "get_backup_store",
    "reset_backup_store",
]

logger = logging.getLogger(__name__)

BACKUP_DIR = "backup"
"""桶内第一段目录：提供者的名字。**写死**——将来别的提供者不混进这个桶的键空间。"""

BLOB_NAME = "snapshot.tar.gz"
"""快照体在恢复点目录里的文件名（先传的那一份，大）。"""

MANIFEST_NAME = "manifest.json"
"""清单在恢复点目录里的文件名（后传的那一份，小；**它的存在就是"这一份传完了"**）。"""

#: "对象/桶不存在"的几种表达：head_object 给 404，get_object 给 NoSuchKey，head_bucket 给 404。
_MISSING_CODES = frozenset({"NoSuchBucket", "NoSuchKey", "NotFound", "404"})

#: 一次 S3 批量删除最多这么多键（S3 的硬上限就是 1000）。
_DELETE_BATCH = 1000

T = TypeVar("T")

#: 惰性拿到的 (boto3, Config, ClientError, BotoCoreError)：**故意不是模块级导入**。
_BOTO: tuple[Any, Any, Any, Any] | None = None


def _boto() -> tuple[Any, Any, Any, Any]:
    """第一次真的要碰对象存储时才导入 boto3 / botocore（模块级导入会多背十几 MB）。

    返回 ``(boto3, botocore.config.Config, ClientError, BotoCoreError)``：
    后两个是 ``except`` 与异常翻译要用到的类（``ClientError`` 是服务端回了错，
    ``BotoCoreError`` 是连接层就没成——超时 / 连不上 / 证书，两类的处置不一样）。
    """
    global _BOTO
    if _BOTO is None:
        import boto3 as _boto3
        from botocore.config import Config as _Config
        from botocore.exceptions import BotoCoreError as _BotoCoreError
        from botocore.exceptions import ClientError as _ClientError

        _BOTO = (_boto3, _Config, _ClientError, _BotoCoreError)
    return _BOTO


class _NeverRaised(Exception):
    """一个**永远不会被抛出**的类，用给 ``except`` 那一行兜底（见 ``_client_error``）。"""


def _client_error() -> Any:
    """``botocore.exceptions.ClientError`` —— ``except`` 那一行要用它。

    **驱动还没加载起来时回那个永不匹配的类，绝不在这里抛**：``except`` 的表达式是在
    *处理另一个异常的过程里*求值的，这里一抛就会把真正的错顶掉——实测的形状是
    "服务器没装 boto3"最终报成 ``ModuleNotFoundError: import of boto3 halted``，
    一半的排查时间会花在"到底是谁在 import boto3"上。
    反过来倒是安全：真能抛 ``ClientError`` 的代码一定来自驱动，那时 ``_BOTO`` 必然已经缓存。
    """
    cached = _BOTO
    return _NeverRaised if cached is None else cached[2]


class BackupError(Exception):
    """备份桶这把封装自己的失败（配不出来 / 桶建不出来 / 对象读写失败）。

    **消息是给人看的一句话**，可能带下一步动作：它一路走到响应体里（``upstream_error``），
    所以不许出现凭据。与 :class:`BackupBucketStatus` 的分工是"失败"与"探测结论"：
    探测（握手）**如实报、不抛**，真正的读写失败了才抛这个。
    """


@dataclass(frozen=True)
class BackupBucketStatus:
    """一次探测的结论：**现在能不能用**。"""

    available: bool
    detail: str = ""
    """``available`` 为假时那句**可执行的下一步**（为真时是空串）。"""


@dataclass(frozen=True)
class BackupObjectInfo:
    """桶里一个对象的事实（大小与时间来自对象存储本身，不是谁自报的）。"""

    key: str
    size: int
    modified: datetime | None = None
    sha256: str = ""
    """上传时写进对象元数据的那份（外部写进来的对象可能是空串 = 内容未知）。"""


def _normalize_prefix(prefix: str) -> str:
    """桶内公共前缀规范成 ``xxx/`` 或空串（与 ``S3ObjectStore`` 同一个口径）。"""
    candidate = (prefix or "").replace("\\", "/").strip("/")
    return f"{candidate}/" if candidate else ""


def _translate(method: Callable[..., T]) -> Callable[..., T]:
    """把 botocore 的异常翻成 :class:`BackupError`（**一处，不是每个方法各写一遍**）。

    ``FileNotFoundError`` 与 :class:`BackupError` 原样放行：前者是"对象不在"的既有口径
    （与 ``S3ObjectStore`` 一致），后者是自己人已经翻过了。
    """

    @functools.wraps(method)
    def wrapper(self: BackupStore, *args: Any, **kwargs: Any) -> T:
        try:
            return method(self, *args, **kwargs)
        except (BackupError, FileNotFoundError):
            raise
        except Exception as exc:
            cached = _BOTO
            if cached is None or not isinstance(exc, (cached[2], cached[3])):
                # 驱动都没加载起来时没有什么可翻译的；不是驱动抛的原样上抛
                raise
            raise BackupError(
                f"对象存储这次调用没成（{type(exc).__name__}）：检查 KYLAB_S3_ENDPOINT 与凭据"
            ) from exc

    return wrapper


class BackupStore:
    """备份桶。**构造不碰网络、不 import boto3**——桶在不在、连不连得上都要能"如实报"。

    "启动不校验桶"（方案 R5 / D6）在代码上的样子就是这一条：构造函数只记配置，
    客户端是第一次真要发请求时才建的（``_client``）。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bucket = settings.backup_bucket
        self._prefix = _normalize_prefix(settings.backup_prefix)
        self._client_cache: Any | None = None

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        """桶内公共前缀（含末尾斜杠，可为空串）。"""
        return self._prefix

    # ------------------------------------------------------------------ 键布局

    def all_prefix(self) -> str:
        """全部恢复点的公共前缀（``<前缀>backup/``）。"""
        return f"{self._prefix}{BACKUP_DIR}/"

    def device_prefix(self, device_id: str) -> str:
        """一台设备的全部恢复点（``<前缀>backup/<device>/``）。"""
        return f"{self.all_prefix()}{device_id}/"

    def snapshot_prefix(self, device_id: str, snapshot_id: str) -> str:
        """一个恢复点（``<前缀>backup/<device>/<snapshot>/``）——**删除的粒度就是它**。"""
        return f"{self.device_prefix(device_id)}{snapshot_id}/"

    def blob_key(self, device_id: str, snapshot_id: str) -> str:
        return f"{self.snapshot_prefix(device_id, snapshot_id)}{BLOB_NAME}"

    def manifest_key(self, device_id: str, snapshot_id: str) -> str:
        return f"{self.snapshot_prefix(device_id, snapshot_id)}{MANIFEST_NAME}"

    # ------------------------------------------------------------------ 桶

    def status(self) -> BackupBucketStatus:
        """现在能不能用 + 不能用时**下一步敲什么**（握手要的就是这两样）。

        **这里不抛**：握手是"如实报"的出口（方案 R5 的判据是"桶缺失时握手仍 200，
        ``capabilities.snapshot.available=false`` + 一句可执行的下一步"），
        所以连配不出来（没配 S3）、连不上、桶不存在都从这里回一个结论。
        """
        try:
            self._client().head_bucket(Bucket=self._bucket)
        except BackupError as exc:
            return BackupBucketStatus(False, str(exc))
        except _client_error() as exc:
            code = self._code_of(exc)
            if code in _MISSING_CODES:
                return BackupBucketStatus(False, self._missing_bucket_hint())
            return BackupBucketStatus(
                False,
                f"对象存储拒绝了这次探测（{code}）："
                "检查 KYLAB_S3_ACCESS_KEY / KYLAB_S3_SECRET_KEY 有没有 S3 权限",
            )
        except Exception as exc:
            logger.warning("备份桶探测失败", exc_info=True)
            return BackupBucketStatus(
                False,
                f"连不上对象存储（{type(exc).__name__}）：检查 KYLAB_S3_ENDPOINT 与网络",
            )
        return BackupBucketStatus(True)

    @_translate
    def ensure_bucket(self) -> None:
        """缺桶就建（**懒 ensure**：不在启动时做，第一次真要写的时候才做）。

        桶本来就在时**一个写操作都不发**（先 ``head_bucket``）。建不出来（凭据没有
        ``s3:CreateBucket``、MinIO 只读挂载、Endpoints 不可达）就抛 :class:`BackupError`
        ——如实报"这块没配好"，而不是让后面每一步都失败一次。
        """
        try:
            self._client().head_bucket(Bucket=self._bucket)
            return
        except _client_error() as exc:
            if self._code_of(exc) not in _MISSING_CODES:
                raise
        self._client().create_bucket(Bucket=self._bucket)
        logger.info("备份桶不存在，已建：%s", self._bucket)

    def _missing_bucket_hint(self) -> str:
        """桶不存在时那句可执行的下一步（**命令与部署文档口径一致**）。"""
        return (
            f"对象存储里还没有备份桶 {self._bucket}：建它——"
            f"`mc mb --ignore-existing <别名>/{self._bucket}`"
            "（NAS 上 `mc` 从哪跑见 deploy/nas/README.md 的「备份桶」一节；"
            "一键起的那份 compose 由 minio-init 自动建）"
        )

    # ------------------------------------------------------------------ 对象

    @_translate
    def put_blob(self, key: str, fileobj: BinaryIO, *, sha256: str) -> BackupObjectInfo:
        """分片上传一份快照体（``upload_fileobj``：≥8 MiB 自动 multipart，>5 GiB 也成）。

        ``sha256`` **必须由调用方先算好**，两个理由：

        - 对象元数据在开始上传**之前**就得写进 ``CreateMultipartUpload`` / ``PutObject``，
          拿到流才知道的哈希塞不进去（想事后补就得再 copy 一遍整份数据）；
        - "服务端边收边算、不符不落桶"那条契约本来就要求调用方**先收完、验完**——
          所以哈希在它手上，这里接着用。

        配对流要 ``seek(0)``（这里替调用方做了）。返回的大小取自 ``head``：
        **落桶之后的真实字节数**，不是谁自报的那个。
        """
        fileobj.seek(0)
        self._client().upload_fileobj(
            fileobj,
            self._bucket,
            key,
            ExtraArgs={
                "ContentType": "application/octet-stream",
                "Metadata": {"sha256": sha256},
            },
        )
        stored = self.head(key)
        if stored is None:
            raise BackupError(f"对象刚上传就找不到了：{key}")
        return stored

    @_translate
    def put_manifest(self, key: str, data: bytes, *, sha256: str) -> BackupObjectInfo:
        """写清单。它是**小对象**（契约里 ≤ 1 MiB，由 API 侧把关）：一次 ``put_object`` 就够。"""
        self._client().put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/json",
            Metadata={"sha256": sha256},
        )
        stored = self.head(key)
        if stored is None:
            raise BackupError(f"对象刚上传就找不到了：{key}")
        return stored

    @_translate
    def get_blob(self, key: str) -> bytes:
        """读整份对象。**清单走这条**；快照体走 :meth:`open_stream`，不许整份进内存。"""
        try:
            response = self._client().get_object(Bucket=self._bucket, Key=key)
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                raise FileNotFoundError(key) from exc
            raise
        return response["Body"].read()

    @_translate
    def head(self, key: str) -> BackupObjectInfo | None:
        """对象在不在 + 多大 + 元数据里的 sha256；不在就回 ``None``（不抛）。"""
        try:
            response = self._client().head_object(Bucket=self._bucket, Key=key)
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                return None
            raise
        metadata = response.get("Metadata") or {}
        return BackupObjectInfo(
            key=key,
            size=int(response.get("ContentLength") or 0),
            modified=response.get("LastModified"),
            sha256=str(metadata.get("sha256") or ""),
        )

    @_translate
    def open_stream(self, key: str) -> tuple[Any, BackupObjectInfo]:
        """流式读（下载走这条）。返回 ``(body, 事实)``——**body 由调用方负责 close**。

        「事实」一起回是为了让下载那条路能顺手把 ``Content-Length`` 与
        **``X-Kylab-Sha256``** 两个头发出去（恢复流程要求"下载完先校验再落位"），
        而不必为了两个数字再发一次 HEAD。
        """
        try:
            response = self._client().get_object(Bucket=self._bucket, Key=key)
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                raise FileNotFoundError(key) from exc
            raise
        metadata = response.get("Metadata") or {}
        return response["Body"], BackupObjectInfo(
            key=key,
            size=int(response.get("ContentLength") or 0),
            modified=response.get("LastModified"),
            sha256=str(metadata.get("sha256") or ""),
        )

    @_translate
    def list_prefix(self, prefix: str) -> list[BackupObjectInfo]:
        """列这个前缀下的全部对象（**分页拉完**，不是只拉第一页）。

        桶不存在时回空列表（**不是错误**）：桶都没有，里面当然没有对象。真正"没配好"
        这件事由 :meth:`status` 在握手里如实说，读路径不必为它再造一种失败。
        """
        try:
            paginator = self._client().get_paginator("list_objects_v2")
            pages: Iterator[Any] = paginator.paginate(Bucket=self._bucket, Prefix=prefix)
            found: list[BackupObjectInfo] = []
            for page in pages:
                for item in page.get("Contents") or ():
                    found.append(
                        BackupObjectInfo(
                            key=str(item["Key"]),
                            size=int(item.get("Size") or 0),
                            modified=item.get("LastModified"),
                        )
                    )
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                return []
            raise
        return found

    @_translate
    def delete_prefix(self, prefix: str) -> int:
        """删掉这个前缀下的**全部**对象，返回删掉的个数（删除的粒度=一个恢复点）。

        返回 0 = 这条路径上一个对象都没有（调用方据此回 404）。对象存储的 DELETE 本身
        是幂等的，所以这里不怕重放。
        """
        keys = [item.key for item in self.list_prefix(prefix)]
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            self._client().delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        return len(keys)

    # ------------------------------------------------------------------ 内部

    def _client(self) -> Any:
        """第一次真要发请求时才建 S3 客户端（**构造不碰网络**，见类说明）。"""
        if self._client_cache is not None:
            return self._client_cache
        settings = self._settings
        if not settings.s3_endpoint:
            raise BackupError(
                "这台机器没有配对象存储（KYLAB_S3_ENDPOINT），备份提供者需要它："
                "备份桶就是快照的落点，没有「本地回退」这一档"
            )
        try:
            boto3, BotoConfig, _, _ = _boto()
        except ModuleNotFoundError as exc:
            raise BackupError("服务器运行时里没有 boto3：这是服务器档的依赖，装它再起") from exc
        self._client_cache = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key or "",
            aws_secret_access_key=settings.s3_secret_key or "",
            region_name=settings.s3_region,
            use_ssl=settings.s3_secure,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )
        return self._client_cache

    @staticmethod
    def _code_of(exc: Any) -> str:
        error = exc.response.get("Error") or {}
        code = error.get("Code")
        if code:
            return str(code)
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", "")
        return str(status)


def build_backup_store(settings: Settings | None = None) -> BackupStore:
    """按配置建一份（**不联网、不校验桶**）。"""
    return BackupStore(settings or get_settings())


#: 进程级单例。**懒建**：第一次用到时才建，启动路径上不碰对象存储。
_STORE: BackupStore | None = None


def get_backup_store() -> BackupStore:
    """进程级那一份（与 ``get_settings`` / ``get_stores`` 同一条口径）。"""
    global _STORE
    if _STORE is None:
        _STORE = build_backup_store()
    return _STORE


def reset_backup_store() -> None:
    """丢掉那份单例（用例与配置热更用；与 ``reset_stores`` / ``reset_services`` 同一手法）。"""
    global _STORE
    _STORE = None
