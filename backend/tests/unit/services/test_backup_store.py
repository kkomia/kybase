r"""备份桶薄封装（``services/backup_store.py``）的行为——**用假的 S3 客户端，不联网**。

镜像同构：``app/services/backup_store.py`` → 本文件。

为什么这一层要单独有用例（方案阶段 1 只点名了 API 那两个文件）：这一层是**唯一碰真
S3 API 的地方**（``upload_fileobj`` / ``list_objects_v2`` 分页 / ``delete_objects`` 批量 /
对象元数据），而 API 那层用的是一份假 store——它证明不了"把这份封装接到真 MinIO 上会不会
在某次调用里写错一个参数名"。写错的下场不是报错就是**静默少删/少列**，那类问题在阶段 8
的真机验收里才现形，代价高得多。

假的客户端是**注入进 ``_BOTO``** 的（不是给生产代码开一个"测试用"的构造参数）：
``_boto()`` 本来就是个惰性缓存的全局，用例改它正好也顺带证明"构造函数一个网络调用都不发"。

**整份标 ``local``**：它一个存储都不连（假客户端 + 假配置）。
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.services import backup_store
from app.services.backup_store import BLOB_NAME, BackupError, BackupStore

pytestmark = pytest.mark.local

_KEYS = "abcdef0123"


class _ClientError(Exception):
    """假 ``botocore.exceptions.ClientError``：``response`` 就是驱动给的那个形状。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 404}}


class _CoreError(Exception):
    """假 ``botocore.exceptions.BotoCoreError``（连接层失败，如 EndpointConnectionError）。"""


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.names: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        prefix = kwargs.get("Prefix", "")
        return [
            {
                "Contents": [
                    item for item in page.get("Contents", []) if item["Key"].startswith(prefix)
                ]
            }
            for page in self._pages
        ]


class _StubClient:
    """够用的假 S3 客户端：只实现被封装用到的那几个方法。"""

    def __init__(
        self,
        *,
        head_bucket: str = "ok",
        objects: dict[str, bytes] | None = None,
        pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._head_bucket = head_bucket
        self._objects = dict(objects or {})
        self._pages = pages or []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.uploads: list[dict[str, Any]] = []
        self.seeks: list[int] = []
        self.paginator = _Paginator(self._pages)

    # -- 桶 --------------------------------------------------------------

    def head_bucket(self, Bucket: str) -> None:
        if self._head_bucket != "ok":
            raise _ClientError(self._head_bucket)

    def create_bucket(self, Bucket: str) -> None:
        self.created.append(Bucket)

    # -- 对象 ------------------------------------------------------------

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self._objects:
            raise _ClientError("404")
        return {
            "ContentLength": len(self._objects[Key]),
            "LastModified": datetime(2026, 10, 5, tzinfo=UTC),
            "Metadata": {"sha256": _KEYS * 6 + "abcd"},
        }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self._objects:
            raise _ClientError("NoSuchKey")
        return {"Body": _Body(self._objects[Key]), "ContentLength": len(self._objects[Key])}

    def put_object(self, **kwargs: Any) -> None:
        self._objects[kwargs["Key"]] = kwargs["Body"]

    def upload_fileobj(self, fileobj: Any, Bucket: str, Key: str, ExtraArgs: Any = None) -> None:
        self.seeks.append(fileobj.tell())
        self.uploads.append({"bucket": Bucket, "key": Key, "extra": ExtraArgs})
        self._objects[Key] = fileobj.read()

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2", name
        self.paginator.names.append(name)
        return self.paginator

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> None:
        self.deleted.extend(item["Key"] for item in Delete["Objects"])


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._data if size < 0 else self._data[:size]
        self._data = self._data[len(chunk) :]
        return chunk

    def close(self) -> None:
        self.closed = True


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "s3_endpoint": "http://minio:9000",
        "s3_access_key": "kylab",
        "s3_secret_key": "kylab-dev-secret",
        "backup_bucket": "kylab-backup",
    }
    base.update(overrides)
    return Settings(**base)


def _store(client: Any, *, settings: Settings | None = None) -> BackupStore:
    """建一份封装，并让它的惰性客户端落到假驱动上。

    （**与生产同一条路**：``_boto()`` → ``boto3.client()``。）

    **不预置 ``_client_cache``**：那样会跳过"从配置建客户端"那一步，于是
    "这台机器没配对象存储"这类分支永远走不到（用例会静默变成假绿）。
    """
    backup_store._BOTO = (_FakeBoto3(client), _FakeConfig, _ClientError, _CoreError)
    return BackupStore(settings or _settings())


class _FakeBoto3:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[dict[str, Any]] = []

    def client(self, service: str, **kwargs: Any) -> Any:
        self.calls.append({"service": service, **kwargs})
        return self._client


class _FakeConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _restore_boto(monkeypatch: pytest.MonkeyPatch) -> None:
    """用例改过的那份惰性缓存要还原（它是模块级全局）。"""
    monkeypatch.setattr(backup_store, "_BOTO", None)


# ------------------------------------------------------------------ 构造与键布局


def test_building_a_store_touches_nothing() -> None:
    """**构造不碰网络、不建客户端**（"启动不校验桶"在代码上的样子）。"""
    built: list[str] = []

    class _Explode:
        def client(self, *args: Any, **kwargs: Any) -> Any:
            built.append("client")
            raise AssertionError("构造期不许建客户端")

    backup_store._BOTO = (_Explode(), _FakeConfig, _ClientError, _CoreError)

    store = BackupStore(_settings())

    assert built == []
    assert store.bucket == "kylab-backup"


def test_the_key_layout_is_the_contract_layout() -> None:
    """键布局就是方案 §1.2 那一张（``backup/<device>/<snapshot>/<名字>``）。"""
    store = _store(_StubClient())

    assert store.all_prefix() == "backup/"
    assert store.device_prefix("dev-1") == "backup/dev-1/"
    assert store.snapshot_prefix("dev-1", "2026-10-05T12-00-00Z-ab12cd34") == (
        "backup/dev-1/2026-10-05T12-00-00Z-ab12cd34/"
    )
    assert store.blob_key("dev-1", "snap") == f"backup/dev-1/snap/{BLOB_NAME}"
    assert store.manifest_key("dev-1", "snap") == "backup/dev-1/snap/manifest.json"


def test_the_public_prefix_is_honoured() -> None:
    """桶内公共前缀（``KYLAB_BACKUP_PREFIX``）拼在最前面，写法与 ``s3_prefix`` 一致。"""
    store = _store(_StubClient(), settings=_settings(backup_prefix="/dev/"))

    assert store.prefix == "dev/"
    assert store.all_prefix() == "dev/backup/"
    assert store.blob_key("dev-1", "snap") == f"dev/backup/dev-1/snap/{BLOB_NAME}"


# ------------------------------------------------------------------ 探测与建桶


def test_status_is_ok_when_the_bucket_is_there() -> None:
    client = _StubClient()

    status = _store(client).status()

    assert status.available is True
    assert status.detail == ""


def test_a_missing_bucket_is_reported_with_the_next_step() -> None:
    """桶不存在 → ``available=false`` + 一句**可执行的下一步**（方案 R5 的判据）。"""
    client = _StubClient(head_bucket="NoSuchBucket")

    status = _store(client).status()

    assert status.available is False
    assert "mc mb" in status.detail
    assert "kylab-backup" in status.detail
    assert client.created == [], "探测不该顺手把桶建了（建桶是一次有意识的部署动作）"


def test_an_access_denied_is_reported_as_a_credential_problem() -> None:
    """凭据不对 → 如实报凭据（**不是"没建桶"**：两条路的下一步完全不同）。"""
    status = _store(_StubClient(head_bucket="AccessDenied")).status()

    assert status.available is False
    assert "KYLAB_S3_ACCESS_KEY" in status.detail


def test_an_unreachable_endpoint_is_reported_as_a_connection_problem() -> None:
    """连不上 → 报地址/网络。"""

    class _Dead(_StubClient):
        def head_bucket(self, Bucket: str) -> None:
            raise _CoreError("endpoint unreachable")

    status = _store(_Dead()).status()

    assert status.available is False
    assert "KYLAB_S3_ENDPOINT" in status.detail


def test_without_object_storage_configured_the_status_says_so() -> None:
    """这台机器根本没配对象存储 → 一句说清（备份没有"本地回退"这一档）。"""
    store = _store(_StubClient(), settings=_settings(s3_endpoint=None))

    status = store.status()

    assert status.available is False
    assert "KYLAB_S3_ENDPOINT" in status.detail


def test_ensure_bucket_creates_only_when_missing() -> None:
    """缺桶就建；桶在时**一个写操作都不发**。"""
    missing = _StubClient(head_bucket="404")
    _store(missing).ensure_bucket()
    assert missing.created == ["kylab-backup"]

    present = _StubClient()
    _store(present).ensure_bucket()
    assert present.created == []


def test_ensure_bucket_refuses_loudly_when_it_cannot_create() -> None:
    """建不出来（没有 CreateBucket 权限）→ 抛 ``BackupError``，不假装成功。"""

    class _ReadOnly(_StubClient):
        def create_bucket(self, Bucket: str) -> None:
            raise _ClientError("AccessDenied")

    with pytest.raises(BackupError):
        _store(_ReadOnly(head_bucket="404")).ensure_bucket()


# ------------------------------------------------------------------ 读写


def test_put_blob_uploads_from_the_start_with_the_digest_in_metadata() -> None:
    """``put_blob``：从头读（配对流刚写完，指针在末尾）+ **把 sha256 写进对象元数据**。

    元数据那一栏是幂等判定的唯一依据（方案 §1.2 规矩 1），写错名字就等于"同内容重放
    永远 409"——而那种错在假 store 那一层看不见。
    """
    client = _StubClient()
    store = _store(client)
    stream = io.BytesIO(b"hello")
    stream.seek(3)

    info = store.put_blob(store.blob_key("dev-1", "snap"), stream, sha256="a" * 64)

    assert client.seeks == [0], "上传前先 seek(0)（调用方那份流刚写完，指针在末尾）"
    assert client.uploads == [
        {
            "bucket": "kylab-backup",
            "key": "backup/dev-1/snap/snapshot.tar.gz",
            "extra": {
                "ContentType": "application/octet-stream",
                "Metadata": {"sha256": "a" * 64},
            },
        }
    ]
    assert info.size == 5, "返回的大小取自落桶之后的事实（head），不是调用方自报的"
    assert info.sha256 == _KEYS * 6 + "abcd"


def test_put_blob_takes_a_multipart_capable_path() -> None:
    """上传走的是 ``upload_fileobj``（**自动分片**，2 GiB 的上限靠它，不整份进内存）。"""
    client = _StubClient()
    store = _store(client)

    store.put_blob(store.blob_key("dev-1", "snap"), io.BytesIO(b"x" * 10), sha256="b" * 64)

    assert len(client.uploads) == 1, "一次上传 = 一次 upload_fileobj（分片是驱动内部的事）"


def test_head_is_none_for_a_missing_object() -> None:
    client = _StubClient()

    assert _store(client).head("backup/dev-1/snap/snapshot.tar.gz") is None


def test_reading_a_missing_object_raises_file_not_found() -> None:
    """读不到 → ``FileNotFoundError``（与对象存储那套既有实现同一个口径，端点据此回 404）。"""
    store = _store(_StubClient())

    with pytest.raises(FileNotFoundError):
        store.get_blob("backup/dev-1/snap/manifest.json")

    with pytest.raises(FileNotFoundError):
        store.open_stream("backup/dev-1/snap/snapshot.tar.gz")


def test_open_stream_returns_the_body_and_its_facts() -> None:
    """流式读把"事实"一起回（下载要据此发 ``Content-Length`` 与 ``X-Kylab-Sha256``）。"""
    client = _StubClient(objects={"backup/dev-1/snap/snapshot.tar.gz": b"hello"})

    body, info = _store(client).open_stream("backup/dev-1/snap/snapshot.tar.gz")

    assert body.read() == b"hello"
    assert info.size == 5
    body.close()
    assert body.closed is True


def test_list_prefix_walks_every_page() -> None:
    """列举**分页拉完**（只拉第一页会让第 1001 份以后的恢复点在列表里消失）。"""
    pages = [
        {"Contents": [{"Key": "backup/dev-1/sa/snapshot.tar.gz", "Size": 1}]},
        {"Contents": [{"Key": "backup/dev-1/sa/manifest.json", "Size": 2}]},
    ]
    client = _StubClient(pages=pages)

    found = _store(client).list_prefix("backup/dev-1/")

    assert [item.key for item in found] == [
        "backup/dev-1/sa/snapshot.tar.gz",
        "backup/dev-1/sa/manifest.json",
    ]
    assert client.paginator.names == ["list_objects_v2"]


def test_a_missing_bucket_lists_as_empty_rather_than_failing() -> None:
    """桶不存在时列举回空（**不是错误**）：桶都没有，里面当然没有对象。

    "没配好"这件事由 ``status`` 在握手里如实说；读路径为它再报一次错只会让列表端点
    在没建桶的机器上直接 500。
    """

    class _NoBucket(_StubClient):
        def get_paginator(self, name: str) -> Any:
            raise _ClientError("NoSuchBucket")

    assert _store(_NoBucket()).list_prefix("backup/") == []


def test_delete_prefix_removes_every_object_and_counts_them() -> None:
    """删一个前缀 → 全部对象都删、返回条数（删除的粒度 = 一个恢复点）。"""
    objects = {
        "backup/dev-1/snap/snapshot.tar.gz": b"hello",
        "backup/dev-1/snap/manifest.json": b"{}",
        "backup/dev-1/other/snapshot.tar.gz": b"other",
    }
    client = _StubClient(pages=[{"Contents": [{"Key": key} for key in objects]}])
    store = _store(client)

    removed = store.delete_prefix("backup/dev-1/snap/")

    assert removed == 2
    assert sorted(client.deleted) == [
        "backup/dev-1/snap/manifest.json",
        "backup/dev-1/snap/snapshot.tar.gz",
    ]


# ------------------------------------------------------------------ 异常翻译


def test_a_botocore_failure_becomes_a_backup_error() -> None:
    """驱动层的失败翻成 ``BackupError``（端点据此回 502 + 一句人话，而不是 500）。

    **不翻 ``FileNotFoundError``**：那是"对象不在"的既有口径，端点按 404 处理。
    """
    client = _StubClient(objects={"backup/dev-1/snap/manifest.json": b"{}"})

    class _Denied(_StubClient):
        def get_object(self, Bucket: str, Key: str) -> Any:
            raise _ClientError("AccessDenied")

    with pytest.raises(BackupError):
        _store(_Denied(objects=client._objects)).get_blob("backup/dev-1/snap/manifest.json")


def test_a_missing_boto3_is_reported_as_a_server_side_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """惰性 import 的代价如实报：服务器档真的没装 boto3 时说清是缺依赖。

    ``sys.modules["boto3"] = None`` 就是"包不在"在解释器里的形状（``import`` 会抛 ImportError）
    ——这台开发机的 venv 里装着 boto3，光清掉那份惰性缓存测不到这条分支。
    """
    store = _store(_StubClient())
    store._client_cache = None  # 回到"还没建客户端"的态，逼它去 import
    backup_store._BOTO = None
    monkeypatch.setitem(sys.modules, "boto3", None)

    with pytest.raises(BackupError) as caught:
        store.head("backup/dev-1/snap/snapshot.tar.gz")

    assert "boto3" in str(caught.value)
