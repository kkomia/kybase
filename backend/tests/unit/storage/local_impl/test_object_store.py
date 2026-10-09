"""``LocalObjectStore`` 的单元测试。

镜像同构：``app/storage/local_impl/object_store.py``
→ ``tests/unit/storage/local_impl/test_object_store.py``。
"""

import pytest

from app.storage.local_impl.object_store import (
    MARKDOWN,
    ORIGINALS,
    TRASH,
    LocalObjectStore,
    content_key,
)


def test_write_returns_relative_path_and_creates_dirs(object_store: LocalObjectStore) -> None:
    path = object_store.write("originals/ab/x.pdf", b"%PDF-1.7")
    assert path == "originals/ab/x.pdf"
    assert object_store.read(path) == b"%PDF-1.7"
    assert object_store.exists(path) is True


def test_read_missing_file_raises(object_store: LocalObjectStore) -> None:
    assert object_store.exists("originals/none.pdf") is False
    with pytest.raises(FileNotFoundError):
        object_store.read("originals/none.pdf")


def test_delete_removes_file_and_is_idempotent(object_store: LocalObjectStore) -> None:
    path = object_store.write("markdown/d1.md", "# 标题".encode())
    object_store.delete(path)
    assert object_store.exists(path) is False
    object_store.delete(path)  # 再删一次不应抛错


# --------------------------------------------------------------------- 目录穿越防护


@pytest.mark.parametrize(
    "evil",
    [
        "../outside.txt",
        "originals/../../outside.txt",
        "/etc/passwd",
        "\\windows\\system32\\config",
        "C:/Windows/win.ini",
    ],
)
def test_path_traversal_is_rejected(object_store: LocalObjectStore, evil: str) -> None:
    """Key 可能来自上传文件名或数据源 URL，属于不可信输入。"""
    with pytest.raises(ValueError):
        object_store.write(evil, b"x")


def test_empty_key_is_rejected(object_store: LocalObjectStore) -> None:
    with pytest.raises(ValueError):
        object_store.write("", b"x")


def test_traversal_cannot_read_outside(object_store: LocalObjectStore, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    with pytest.raises(ValueError):
        object_store.read("../secret.txt")


# --------------------------------------------------------------------- 内容 hash 寻址


def test_content_key_is_two_level_and_deterministic() -> None:
    key = content_key(ORIGINALS, "abcdef123456", ".pdf")
    assert key == "originals/ab/abcdef123456.pdf"
    assert content_key(ORIGINALS, "abcdef123456", ".pdf") == key  # 同内容必然同路径


def test_content_key_rejects_empty_hash() -> None:
    with pytest.raises(ValueError):
        content_key(ORIGINALS, "")


def test_same_content_lands_on_same_path(object_store: LocalObjectStore) -> None:
    """重复上传同一文件不会产生第二份原文。"""
    key = content_key(ORIGINALS, "deadbeef", ".pdf")
    first = object_store.write(key, b"same")
    second = object_store.write(key, b"same")
    assert first == second


def test_markdown_key_helper_usable(object_store: LocalObjectStore) -> None:
    path = object_store.write(f"{MARKDOWN}/doc_1.md", "# 标题".encode())
    assert path.startswith("markdown/")


# --------------------------------------------------------------------- 回收站


def test_move_to_trash_relocates_file(object_store: LocalObjectStore) -> None:
    original = object_store.write("originals/ab/x.pdf", b"PDF")
    trashed = object_store.move_to_trash(original, trash_id="tr_1")

    assert trashed == f"{TRASH}/tr_1/x.pdf"
    assert object_store.exists(trashed) is True
    assert object_store.exists(original) is False


def test_move_to_trash_rejects_unsafe_trash_id(object_store: LocalObjectStore) -> None:
    original = object_store.write("originals/ab/x.pdf", b"PDF")
    with pytest.raises(ValueError):
        object_store.move_to_trash(original, trash_id="../escape")


def test_move_missing_file_raises(object_store: LocalObjectStore) -> None:
    with pytest.raises(FileNotFoundError):
        object_store.move_to_trash("originals/none.pdf", trash_id="tr_1")


def test_purge_trash_removes_everything(object_store: LocalObjectStore) -> None:
    first = object_store.move_to_trash(
        object_store.write("originals/ab/a.pdf", b"A"), trash_id="tr_1"
    )
    object_store.move_to_trash(object_store.write("originals/cd/b.pdf", b"B"), trash_id="tr_2")

    assert object_store.purge_trash() == 2
    assert object_store.exists(first) is False


def test_purge_single_trash_entry(object_store: LocalObjectStore) -> None:
    object_store.move_to_trash(object_store.write("originals/ab/a.pdf", b"A"), trash_id="tr_1")
    object_store.move_to_trash(object_store.write("originals/cd/b.pdf", b"B"), trash_id="tr_2")

    assert object_store.purge_trash(trash_id="tr_1") == 1
    assert object_store.exists(f"{TRASH}/tr_2/b.pdf") is True


def test_purge_unknown_trash_is_noop(object_store: LocalObjectStore) -> None:
    assert object_store.purge_trash(trash_id="tr_none") == 0
