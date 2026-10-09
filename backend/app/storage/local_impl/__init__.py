"""本地文件系统对象存储实现。

与 ``s3_impl/`` 平级：``ObjectStore`` 有两个实现，由组合根按是否配置了
S3 端点选择。**不随 SQLite 一起退役**——SQLite 是数据库选型，本地文件系统
是另一个维度的事，而且测试需要一个不联网、可隔离的对象存储。

（原在 ``sqlite_impl/`` 下，SQLite 退役后移到这里。）
"""

from app.storage.base import TRASH, content_key
from app.storage.local_impl.object_store import LocalObjectStore

__all__ = ["TRASH", "LocalObjectStore", "content_key"]
