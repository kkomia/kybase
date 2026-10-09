"""``ObjectStore`` 的本地文件系统实现（M1 T1.6）。

目录规约（架构 §2 存储层）：``data/{originals,markdown,images}``，回收站走 ``data/.trash/<id>/``。

两种 Key 形态：

- **内容 hash 寻址**（推荐，用 :func:`content_key` 生成）：``originals/ab/abcdef...pdf``。
  相同内容天然同路径，重复上传不会产生第二份；两级散列目录避免单目录堆几万文件。
- **按业务 ID 命名**：如 ``markdown/<document_id>.md``，便于按文档定位与重跑覆盖。

安全：所有路径都经 :meth:`_resolve` 做**目录穿越校验**。Key 会来自数据源 URL、用户上传名等
不可信输入，``../../`` 一旦拼进去就能读写仓库外的文件。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.storage.base import (
    IMAGES,
    MARKDOWN,
    ORIGINALS,
    SAFE_KEY_CHARS,
    TRASH,
    ObjectStore,
    content_key,
)

__all__ = [
    "IMAGES",
    "MARKDOWN",
    "ORIGINALS",
    "TRASH",
    "LocalObjectStore",
    "content_key",
]


class LocalObjectStore(ObjectStore):
    """本地文件系统对象存储。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ 接口

    def write(self, key: str, data: bytes) -> str:
        """写入并返回**相对路径**（入库用相对路径，换部署目录不用改数据）。"""
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return self._relative(target)

    def read(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._resolve(path).is_file()

    def move_to_trash(self, path: str, *, trash_id: str) -> str:
        """把原文挪进回收站目录（架构 §6.2：原文保留 7 天冷备）。

        **目标路径必须 ``resolve()`` 之后再交给 _relative**：``self._root`` 可能是
        相对路径（实测 dev 的配置就是 ``data``），于是 ``_root / TRASH / ...``
        也是相对的，而 ``_relative`` 拿它去比 ``_root.resolve()``（绝对）——
        一个是 ``data\\.trash\\...``、一个是 ``E:\\...\\data``，
        ``relative_to`` 直接抛 ValueError。

        这条路径从 M1 写好起就没人调用过（接口层一直没有删除端点），
        所以这个 bug 一直没被发现——直到回收站第一次真的被用上。
        """
        if not trash_id or any(char not in SAFE_KEY_CHARS for char in trash_id):
            raise ValueError(f"非法回收站 ID：{trash_id!r}")
        source = self._resolve(path)
        if not source.is_file():
            raise FileNotFoundError(path)

        target = (self._root / TRASH / trash_id / source.name).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return self._relative(target)

    def delete(self, path: str) -> None:
        target = self._resolve(path)
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)

    def purge_trash(self, *, trash_id: str | None = None) -> int:
        """物理清除回收站内容；不传 trash_id 则清空整个回收站。返回删除的条目数。"""
        base = self._root / TRASH if trash_id is None else self._root / TRASH / trash_id
        base = base.resolve()
        if not base.is_relative_to((self._root / TRASH).resolve()) or not base.is_dir():
            return 0
        removed = sum(1 for item in base.rglob("*") if item.is_file())
        shutil.rmtree(base)
        return removed

    # ------------------------------------------------------------------ 内部

    def _resolve(self, key: str) -> Path:
        """把 Key 解析为根目录下的绝对路径，并拒绝任何越界路径。"""
        if not key or key.startswith(("/", "\\")) or ":" in key:
            raise ValueError(f"非法路径：{key!r}")
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root.resolve()):
            raise ValueError(f"路径越出存储根目录：{key!r}")
        return candidate

    def _relative(self, target: Path) -> str:
        return target.relative_to(self._root.resolve()).as_posix()
