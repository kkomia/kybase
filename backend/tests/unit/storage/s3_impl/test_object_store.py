"""``S3ObjectStore`` 的单元测试（用假客户端，不联网）。

假客户端刻意模仿 boto3 的返回结构与报错方式（``ClientError`` + ``Code``）——
否则"读到不存在的对象要抛 FileNotFoundError"这类分支根本测不到，
而那正是服务层依赖的行为（与本地文件系统实现对齐）。
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from app.storage.s3_impl.object_store import S3ObjectStore

BUCKET = "kylab"


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeS3Client:
    """只实现 ObjectStore 用到的那几个调用。

    ``head_object`` 用数字码 404（真实 boto3 就是这样，不是 "NoSuchKey"），
    ``get_object`` 用 "NoSuchKey"——两条分支都要覆盖到。

    参数名 ``Bucket`` / ``Key`` / ``Body`` 大写是照抄 boto3 的调用签名，
    这样被测代码不用为"假客户端"做任何妥协。
    """

    def __init__(self, *, buckets: tuple[str, ...] = (BUCKET,)) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self._buckets = set(buckets)

    def head_bucket(self, *, Bucket: str) -> dict:
        if Bucket not in self._buckets:
            raise _client_error("NoSuchBucket", 404, "HeadBucket")
        return {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict:
        self.objects[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        return {"Body": _FakeBody(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise _client_error("404", 404, "HeadObject")
        return {}

    def copy_object(self, *, Bucket: str, CopySource: dict, Key: str) -> dict:
        source = (CopySource["Bucket"], CopySource["Key"])
        if source not in self.objects:
            raise _client_error("NoSuchKey", 404, "CopyObject")
        self.objects[(Bucket, Key)] = self.objects[source]
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture
def client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def store(client: FakeS3Client) -> S3ObjectStore:
    return S3ObjectStore(client, bucket=BUCKET)


# ------------------------------------------------------------------ 基本读写


def test_write_read_roundtrip(store: S3ObjectStore, client: FakeS3Client) -> None:
    path = store.write("originals/ab/abcdef.pdf", b"%PDF-1.4")
    assert path == "originals/ab/abcdef.pdf"
    assert store.read(path) == b"%PDF-1.4"
    assert client.objects[(BUCKET, "originals/ab/abcdef.pdf")] == b"%PDF-1.4"


def test_exists(store: S3ObjectStore) -> None:
    assert store.exists("originals/ab/abcdef.pdf") is False
    store.write("originals/ab/abcdef.pdf", b"x")
    assert store.exists("originals/ab/abcdef.pdf") is True


def test_read_missing_raises_file_not_found(store: S3ObjectStore) -> None:
    """与本地实现对齐：读不到就是 FileNotFoundError，不是驱动异常。

    服务层（lifecycle / documents）按 FileNotFoundError 处理"原件丢了"，
    这里若抛出 botocore 的 ClientError，那些分支就永远不生效。
    """
    with pytest.raises(FileNotFoundError):
        store.read("originals/ab/nope.pdf")


def test_delete_is_idempotent(store: S3ObjectStore, client: FakeS3Client) -> None:
    store.write("images/x.png", b"png")
    store.delete("images/x.png")
    assert client.objects == {}
    store.delete("images/x.png")  # 再删一次不该抛（S3 的 DELETE 本就幂等）


# ------------------------------------------------------------------ 回收站


def test_move_to_trash_copies_then_deletes(store: S3ObjectStore, client: FakeS3Client) -> None:
    store.write("originals/ab/abcdef.pdf", b"data")
    moved = store.move_to_trash("originals/ab/abcdef.pdf", trash_id="tr_1")

    assert moved == ".trash/tr_1/abcdef.pdf"
    assert store.exists(moved) is True
    assert store.exists("originals/ab/abcdef.pdf") is False
    assert store.read(moved) == b"data"


def test_move_to_trash_missing_source_raises(store: S3ObjectStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.move_to_trash("originals/ab/nope.pdf", trash_id="tr_1")


def test_move_to_trash_rejects_unsafe_id(store: S3ObjectStore) -> None:
    """回收站 ID 会拼进 Key，必须白名单校验（与本地实现同一份常量）。"""
    store.write("originals/ab/abcdef.pdf", b"data")
    with pytest.raises(ValueError, match="非法回收站 ID"):
        store.move_to_trash("originals/ab/abcdef.pdf", trash_id="../escape")


# ------------------------------------------------------------------ 路径与前缀


def test_trash_layout_matches_local_implementation(store: S3ObjectStore) -> None:
    """回收站前缀必须与本地版一致：返回值会落库，换实现后历史路径还得解析得到。"""
    store.write("markdown/doc_1.md", "# 标题".encode())
    moved = store.move_to_trash("markdown/doc_1.md", trash_id="tr_9")
    assert moved.startswith(".trash/tr_9/")


def test_prefix_is_applied_but_not_returned(store: S3ObjectStore, client: FakeS3Client) -> None:
    """桶内前缀是部署细节：实际 Key 带上它，但返回值**不带**。

    否则"给现有部署加一个前缀"就变成一次全量数据订正。
    """
    prefixed = S3ObjectStore(client, bucket=BUCKET, prefix="dev")
    path = prefixed.write("originals/ab/abcdef.pdf", b"x")

    assert path == "originals/ab/abcdef.pdf"
    assert (BUCKET, "dev/originals/ab/abcdef.pdf") in client.objects
    assert prefixed.read(path) == b"x"


@pytest.mark.parametrize(
    "bad",
    ["", "/abs/path", "\\abs", "a/../../etc/passwd", "a//b", "C:/windows", "a/./b"],
)
def test_rejects_unsafe_keys(store: S3ObjectStore, bad: str) -> None:
    with pytest.raises(ValueError, match="非法路径"):
        store.write(bad, b"x")


def test_constructor_fails_fast_on_missing_bucket(client: FakeS3Client) -> None:
    """桶配错要在构造时炸，而不是等第一次上传才报错。"""
    with pytest.raises(ClientError):
        S3ObjectStore(client, bucket="not-there")
