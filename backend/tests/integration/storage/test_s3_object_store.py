"""``S3ObjectStore`` 的集成测试（需要真实 S3/MinIO，默认跳过）。

单元测试用假客户端覆盖了逻辑与分支；这里验的是**真家伙**：签名版本、
``ClientError`` 的真实返回形状、以及 S3 语义下的 copy+delete（S3 没有 rename）。

运行方式：

    KYLAB_TEST_S3_ENDPOINT=http://host:9000 \
    KYLAB_TEST_S3_ACCESS_KEY=... KYLAB_TEST_S3_SECRET_KEY=... \
    KYLAB_TEST_S3_BUCKET=kylab \
      pytest tests/integration/storage/test_s3_object_store.py -q

每次都写在一个随机前缀下，跑完清理，不碰桶里既有对象。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from app.storage.s3_impl.object_store import S3ObjectStore, build_client

ENDPOINT = os.environ.get("KYLAB_TEST_S3_ENDPOINT")
ACCESS_KEY = os.environ.get("KYLAB_TEST_S3_ACCESS_KEY")
SECRET_KEY = os.environ.get("KYLAB_TEST_S3_SECRET_KEY")
BUCKET = os.environ.get("KYLAB_TEST_S3_BUCKET", "kylab")

pytestmark = pytest.mark.skipif(
    not (ENDPOINT and ACCESS_KEY and SECRET_KEY),
    reason="未提供 KYLAB_TEST_S3_*，跳过需要真实对象存储的集成测试",
)


@pytest.fixture(scope="module")
def store() -> Iterator[S3ObjectStore]:
    assert ENDPOINT and ACCESS_KEY and SECRET_KEY
    client = build_client(
        endpoint=ENDPOINT,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        secure=ENDPOINT.startswith("https"),
    )
    prefix = f"_pytest/{uuid.uuid4().hex[:8]}"
    store = S3ObjectStore(client, bucket=BUCKET, prefix=prefix)
    yield store

    # 清理：S3 没有目录，逐个删本次写进去的对象
    listed = client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    for item in listed.get("Contents", []):
        client.delete_object(Bucket=BUCKET, Key=item["Key"])


def test_write_read_exists(store: S3ObjectStore) -> None:
    key = "originals/ab/abcdef.pdf"
    assert store.exists(key) is False

    path = store.write(key, b"%PDF-1.4 real")
    assert path == key, "返回值不应带桶内前缀"
    assert store.exists(key) is True
    assert store.read(key) == b"%PDF-1.4 real"


def test_read_missing_raises_file_not_found(store: S3ObjectStore) -> None:
    """真服务下也要抛 FileNotFoundError（botocore 的 ClientError 得被翻译掉）。"""
    with pytest.raises(FileNotFoundError):
        store.read("originals/ab/definitely-not-there.pdf")


def test_move_to_trash_keeps_content_and_removes_original(store: S3ObjectStore) -> None:
    key = "markdown/doc_it.md"
    store.write(key, "# 标题".encode())

    moved = store.move_to_trash(key, trash_id="tr_it")
    assert moved == ".trash/tr_it/doc_it.md", "回收站布局要与本地实现一致"
    assert store.exists(moved) is True
    assert store.read(moved) == "# 标题".encode()
    assert store.exists(key) is False


def test_move_to_trash_rejects_unsafe_id(store: S3ObjectStore) -> None:
    store.write("images/a.png", b"png")
    with pytest.raises(ValueError, match="非法回收站 ID"):
        store.move_to_trash("images/a.png", trash_id="tr/../x")


def test_delete(store: S3ObjectStore) -> None:
    store.write("images/b.png", b"png")
    store.delete("images/b.png")
    assert store.exists("images/b.png") is False
    store.delete("images/b.png")  # 幂等
