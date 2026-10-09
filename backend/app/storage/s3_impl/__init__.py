"""S3 兼容对象存储实现（MinIO / AWS S3）。

与 ``sqlite_impl/object_store.py``（本地文件系统）平级：``ObjectStore`` 有两个
实现，由 ``app/core/storage.py::build_stores()`` 按是否配置了 S3 端点来选择。

**为什么单开一个包而不是塞进 sqlite_impl**：它不是 SQLite 的附属，
SQLite 退役后本地文件系统实现与它都还要在——届时本地版应移出 sqlite_impl。
"""

from app.storage.s3_impl.object_store import S3ObjectStore, build_client

__all__ = ["S3ObjectStore", "build_client"]
