"""S3 兼容对象存储实现（MinIO / AWS S3 等）。

与 ``sqlite_impl/object_store.py``（本地文件系统）的对应关系：

- 接口是同一份 ``ObjectStore``（5 个方法），服务层感知不到差别；
- ``write`` 返回的仍然是**不含桶内前缀的相对 Key**，与本地版返回相对路径同理——
  前缀是部署细节，写进数据库会让"换个前缀"变成一次数据订正；
- 回收站布局沿用同一个 ``base.TRASH``（``.trash/<trash_id>/<文件名>``），
  因为 ``move_to_trash`` 的返回值会落库，两套实现必须对同一批历史路径都能解析。

**两处与本地实现有本质差异，值得知道**：

1. **``move_to_trash`` 不是原子的**。本地是 ``rename``（要么成要么没成）；
   S3 没有 rename，只能"copy 到回收站 → 删原件"。中途失败会留下两处之一：
   回收站有副本但原件还在（可重试，无害），或副本已建、原件未删（同上）。
   **不会丢数据**，但不会像本地那样瞬时完成，大文件还要付一次跨对象流量。
2. **没有目录**。``delete`` 只删单个对象；本地版对目录会 ``rmtree`` 的能力在
   S3 上不存在（当前调用方只删文件，所以不构成缺口）。

路径校验比本地版**更严**：本地允许 ``a/../b`` 这种"绕一下但仍在根内"的 Key
（resolve 后落在根里），这里直接拒绝 ``..`` 段。S3 的 Key 是平铺字符串，
没有"根"可以兜底，宁可严一点。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 只在注解里用到（``_code_of(exc: ClientError)`` ✓）：`from __future__ import annotations`
    # 之下注解不求值 ✓，所以这一句**不进运行时**、也就不进客户端的导入闭包 ✓。
    from botocore.exceptions import ClientError

from app.storage.base import SAFE_KEY_CHARS, TRASH, ObjectStore

__all__ = ["S3ObjectStore", "build_client"]

#: 惰性拿到的 (boto3, Config, ClientError)（**故意不是模块级导入** ✗）。
_BOTO: tuple[Any, Any, Any] | None = None


def _boto() -> tuple[Any, Any, Any]:
    """第一次用对象存储时才导入 boto3 / botocore（模块级导入会多背十几 MB ✗）。

    为什么惰性：这个模块挂在 ``app.storage`` 的导入链上 ✓，而"对象存储"只有**服务器**
    那条链会碰 ✓ —— 边车（客户端运行时）跑循环 + 工具 + 沙箱 ✓，产物落在本机目录 ✓，
    从来不往 S3 / MinIO 发东西。

    **行为一个字没变** ✗：第一次调用时才导入 ✓，boto3 真的不在时仍在**调用那一刻**
    抛 ``ModuleNotFoundError`` ✓（只是从导入期挪到了调用期 ✓）。

    返回 ``(boto3, botocore.config.Config, botocore.exceptions.ClientError)`` ✓。
    """
    global _BOTO
    if _BOTO is None:
        import boto3 as _boto3
        from botocore.config import Config as _Config
        from botocore.exceptions import ClientError as _ClientError

        _BOTO = (_boto3, _Config, _ClientError)
    return _BOTO


def _client_error() -> Any:
    """``botocore.exceptions.ClientError`` —— ``except`` 那一行要用它 ✓。"""
    return _boto()[2]

#: 对象不存在的几种表达：get_object 给 NoSuchKey，head_object 给 404。
_MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})

_CONNECT_TIMEOUT_S = 5
_READ_TIMEOUT_S = 30


def build_client(
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    region: str = "us-east-1",
    secure: bool = True,
) -> Any:
    """构造 S3 客户端。

    ``signature_version="s3v4"`` 是 MinIO 与 AWS 都认的签名版本；MinIO 不校验
    region，但 SDK 要求一个非空值。超时显式给死：默认值接近"无限等"，
    对象存储挂掉时会把请求线程连同连接池一起拖住。
    """
    boto3, BotoConfig, _ = _boto()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        use_ssl=secure,
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=_CONNECT_TIMEOUT_S,
            read_timeout=_READ_TIMEOUT_S,
        ),
    )


class S3ObjectStore(ObjectStore):
    """S3 兼容对象存储。``prefix`` 是桶内的公共前缀（如 ``dev/``），可为空。"""

    def __init__(self, client: Any, *, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise ValueError("bucket 不能为空")
        self._client = client
        self._bucket = bucket
        self._prefix = self._normalize(prefix) + "/" if prefix else ""
        # 启动即校验桶可达：配错了要在启动时炸，而不是等第一次上传
        self._client.head_bucket(Bucket=self._bucket)

    @property
    def bucket(self) -> str:
        return self._bucket

    # ------------------------------------------------------------------ 接口

    def write(self, key: str, data: bytes) -> str:
        """写入并返回**不含桶内前缀**的相对 Key（入库用它）。"""
        relative = self._normalize(key)
        self._client.put_object(Bucket=self._bucket, Key=self._key(relative), Body=data)
        return relative

    def read(self, path: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key(path))
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                # 与本地实现对齐：读不到就抛 FileNotFoundError，而不是驱动异常
                raise FileNotFoundError(path) from exc
            raise
        return response["Body"].read()

    def exists(self, path: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(path))
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                return False
            raise
        return True

    def move_to_trash(self, path: str, *, trash_id: str) -> str:
        """挪进回收站；**非原子**（copy + delete，见模块说明）。返回新的相对 Key。"""
        if not trash_id or any(char not in SAFE_KEY_CHARS for char in trash_id):
            raise ValueError(f"非法回收站 ID：{trash_id!r}")

        relative = self._normalize(path)
        if not self.exists(relative):
            raise FileNotFoundError(path)

        name = relative.rsplit("/", 1)[-1]
        target = f"{TRASH}/{trash_id}/{name}"
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                CopySource={"Bucket": self._bucket, "Key": self._key(relative)},
                Key=self._key(target),
            )
        except _client_error() as exc:
            if self._code_of(exc) in _MISSING_CODES:
                raise FileNotFoundError(path) from exc
            raise
        self._client.delete_object(Bucket=self._bucket, Key=self._key(relative))
        return target

    def delete(self, path: str) -> None:
        """删除对象。S3 的 DELETE 是幂等的：对象不存在也返回成功。"""
        self._client.delete_object(Bucket=self._bucket, Key=self._key(path))

    # ------------------------------------------------------------------ 内部

    def _key(self, relative: str) -> str:
        return f"{self._prefix}{relative}"

    def _normalize(self, key: str) -> str:
        """把 Key 规范成桶内相对 Key，并拒绝一切越界形态。

        比本地版更严：``..`` 段直接拒绝（见模块说明）。
        """
        if not key:
            raise ValueError(f"非法路径：{key!r}")
        candidate = key.replace("\\", "/").strip("/")
        if not candidate or key.startswith(("/", "\\")) or ":" in key:
            raise ValueError(f"非法路径：{key!r}")
        segments = candidate.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise ValueError(f"非法路径：{key!r}")
        return candidate

    @staticmethod
    def _code_of(exc: ClientError) -> str:
        error = exc.response.get("Error") or {}
        code = error.get("Code")
        if code:
            return str(code)
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", "")
        return str(status)
