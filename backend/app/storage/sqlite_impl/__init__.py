r"""本机 SQLite 存储实现（M2「会话落本机」阶段 1）。

与 ``postgres_impl/`` 的分工：那份是**服务器档**的唯一主实现（知识库 + 向量 + 全文
的家当都在那边），这份是**本机档**的元数据实现——会话、消息、事件、产物、笔记、
设置、工作区、定时任务、MCP、模型注册、用量，全部落在 ``<data_dir>/kylab.db``。
方案见 ``docs/规范/会话落本机-实施方案-v0.1.md`` §1。

- ``connection.py``   连接管理 + PRAGMA 口径 + ``read()/session()``
- ``schema.sql``      基线 v1 DDL（本机 17 张表，逐表出处见文件头；**冻结不动**）
- ``schema.py``       ``ensure_schema`` / 增量迁移 / 迁移前自动备份
                      （v2 = 知识库元数据缓存，见 ``MIGRATION_V2_KB_META_CACHE``；
                       v3 = 备份待传队列，见 ``MIGRATION_V3_BACKUP_SNAPSHOTS``）
- ``meta_store.py``   ``SqliteMetaStore``（本机域 + 导入台账 + 知识库快照 + 快照打包
                      + 备份待传队列）
- ``validate.py``     运维自检入口：``python -m app.storage.sqlite_impl.validate``
                      （表/索引计数之外还报出快照表的行数与字节数）

**本机域的清单都在这一个模块里**（都不许手抄，见下面五个常量）：

- ``LOCAL_METHODS`` —— ``MetaStore`` 的哪些方法归本机（阶段 2 的路由表按它算补集）；
- ``LOCAL_LEDGER_METHODS`` —— ``imports`` / ``import_items`` 那族方法。它们**不在**
  ``MetaStore`` 上（是本机独有的两张表），所以既不属于本机域也不属于 KB 域，
  单独登记并把理由写在那儿；
- ``LOCAL_CACHE_METHODS`` —— 知识库元数据快照那六个方法（M4）。同样**不在**
  ``MetaStore`` 上，同样单独登记；它多出来的一句是"为什么它既不属于本机域也不属于
  KB 域"；
- ``LOCAL_SNAPSHOT_METHODS`` —— 快照打包与读回那三个方法（M5 阶段 2 两个、阶段 5 加了
  一个"逐条读全量会话"）。第三块
  "本机独有"（服务器档的库就是它自己，没有"把自己打成一份包"这条动作），
  理由同样写在常量上；
- ``LOCAL_BACKUP_METHODS`` —— 备份待传队列那五个方法（M5 阶段 3）。第四块"本机独有"
  （服务器档自己就是备份的目的地，没有"把一份快照排队传出去"这条动作）；
- ``LOCAL_ERASER_METHODS`` —— 安全擦除那一个方法（M5 阶段 6 收尾）。第五块"本机独有"
  （它擦的是**本机那个库文件**，服务器档的数据在 PG 里，那份处置是 NAS 自己的访问控制）。

**只实现本机域，不实现 ``MetaStore`` 全量 ABC**：本机档里知识库那半没有数据源
（NAS 才是），所以 ``SqliteMetaStore`` 不继承 ``MetaStore``，也不该被当成一个完整的
``MetaStore`` 用。分档路由（``RouterMetaStore``：本机域走它、KB 域转给 KB 侧实现）
是阶段 2 的事，本模块只负责"本机这些表怎么读写"。

**五种存储只住这一个目录**：``scripts/check_layering.py`` 的 L2 规则禁止
``services/`` import ``sqlite3``（与 ``psycopg`` 同级），SQLite 代码只许住在
``app/storage/sqlite_impl/``，业务层只许见 ``app.storage.base`` 的接口。
"""

from __future__ import annotations

from app.storage.repositories import (
    ConversationRepo,
    MCPServerRepo,
    ModelRegistryRepo,
    NoteRepo,
    ScheduleRepo,
    SettingsRepo,
    UsageRepo,
    WorkspaceRepo,
)

__all__ = [
    "LOCAL_BACKUP_METHODS",
    "LOCAL_CACHE_METHODS",
    "LOCAL_ERASER_METHODS",
    "LOCAL_EXTRA",
    "LOCAL_LEDGER_METHODS",
    "LOCAL_METHODS",
    "LOCAL_PROTOCOLS",
    "LOCAL_SNAPSHOT_METHODS",
    "local_methods",
]


#: 归本机的**协议**（实施方案 §2.1）：这 8 个域的数据都在本机库里，
#: 它们的每个方法都由 ``SqliteMetaStore`` 实现。
LOCAL_PROTOCOLS: tuple[type, ...] = (
    ConversationRepo,
    NoteRepo,
    SettingsRepo,
    WorkspaceRepo,
    ScheduleRepo,
    MCPServerRepo,
    ModelRegistryRepo,
    UsageRepo,
)

#: ``MaintenanceRepo`` 不属于任何一个业务域，所以要逐方法点名：
#: 存储空间概览与 VACUUM 是本机库自己的事（本机库就是那个"文件"），
#: 而 ``purge_stage_events`` 清的是**文档阶段事件**——那是知识库流水线的表，归 KB 域。
LOCAL_EXTRA: frozenset[str] = frozenset({"storage_stats", "vacuum"})

#: **本机独有的导入台账**（阶段 5）：``imports`` / ``import_items`` 两张表的方法。
#:
#: 它**不在** ``LOCAL_METHODS`` 里，两个理由都不是"忘了"：
#:
#: 1. ``LOCAL_METHODS`` 是"``MetaStore`` 的两百来个方法里哪些归本机"的划分，
#:    而这两个方法族**不在 ``MetaStore`` 上**——服务器档没有这两张表，也没有
#:    "从别的部署导会话进来"这条动作（``RouterMetaStore`` 的路由表因此装不下它们）；
#: 2. 硬塞进 ``LOCAL_METHODS`` 会让三条既有用例当场红（``LOCAL_METHODS`` 必须是
#:    ``MetaStore`` 抽象方法的子集、与 ``REMOTE_METHODS`` 恰好划开全部方法、
#:    且 ``SqliteMetaStore`` 的方法集合恰好是它）——那三条纪律正是"不许悄悄超域"
#:    的守卫，所以**超域的登记方式就是这张表**，而不是把守卫改松。
#:
#: 谁用它们：``services/legacy_import.py``（唯一的调用方），经 ``StoreBundle.ledger``
#: 拿到（服务器档那个字段是 ``None``，契约见 ``app/storage/base.py`` 的 ``ImportLedger``）。
LOCAL_LEDGER_METHODS: frozenset[str] = frozenset(
    {
        # imports：批次与进度（CLI 与端点把进度写在同一张表上）
        "start_import_batch",
        "set_import_state",
        "get_import_batch",
        "list_import_batches",
        # import_items：幂等键与回滚依据
        "get_import_item",
        "record_import_item",
        "list_import_items",
        # 一条会话整体落库（一个事务）：幂等重跑与回滚恢复共用同一个入口
        "write_imported_conversation",
    }
)

#: **知识库元数据快照**（M4 阶段 1）：``kb_meta_cache`` 一张表上的六个方法。
#:
#: **为什么它既不属于本机域、也不属于 KB 域**（这一条就是这个常量存在的理由）：
#:
#: - **不进 ``LOCAL_METHODS``**：那是"``MetaStore`` 里哪些方法归本机"的划分，而这六个
#:   方法**不在 ``MetaStore`` 上**——服务器档没有这张表，也不该有（它的 KB 元数据就在
#:   自己的 PG 里，缓存一个"自己就是真相源"的东西只会多一层会过期的副本）；
#: - **也不是 KB 域**：它服务的是 **KB 域的读路径**（页面先画快照、reader 面打 NAS 之前
#:   先看本机），可它的**数据主人是本机**——写者只有本机后端一个（M4 §2.3），KB 域的
#:   路由表（``RouterMetaStore`` → ``RemoteMetaStore``）一个字都不该碰它。
#:
#: 所以它是第三块：与 ``LOCAL_LEDGER_METHODS`` 同一套登记手法（单独一块清单 + 单独一个
#: ``StoreBundle`` 字段 ``kb_cache`` + 用**同一个 ``SqliteMetaStore`` 实例**），
#: 而不是把守卫改松。接口契约见 ``app/storage/base.py`` 的 ``KbMetaCache``。
#:
#: 谁用它们：阶段 2 起的 ``services/kb_cache.py``（唯一的写者）与阶段 4 的
#: ``/local/kb-cache/*`` 只读端点；两者都经 ``StoreBundle.kb_cache`` 拿到（服务器档那个
#: 字段恒为 ``None``）。
LOCAL_CACHE_METHODS: frozenset[str] = frozenset(
    {
        # 一读（超龄当没有）/ 一整写（含两级上限的收口）
        "get_kb_meta_cache",
        "put_kb_meta_cache",
        # 只推确认（版本没变时走它，payload 与 fetched_at 一个字不动）
        "touch_kb_meta_cache",
        # 失效：单行删 / 按档清 / 报数
        "drop_kb_meta_cache",
        "purge_kb_meta_cache",
        "kb_meta_cache_stats",
    }
)

#: **快照打包与读回**（M5 阶段 2）：本机库 → 擦洗过的副本 → 便携的包，以及反向读回。
#:
#: **为什么它是第三块"本机独有"**（前两块是导入台账、知识库快照）：
#:
#: - **不进 ``LOCAL_METHODS``**：那是"``MetaStore`` 的哪些方法归本机"的划分，而这两个
#:   方法**不在 ``MetaStore`` 上**——服务器档的库就是它自己，没有"把自己打成一份便携的包"
#:   这条动作（NAS 侧那一半是**收包**：``api/v1/backup.py`` 的七条端点，与本模块无关）；
#: - **也不是 KB 域**：它读写的全是本机库那几张表（会话 / 产物 / 设置），与知识库没有关系。
#:
#: 所以它与前两块同一套登记手法：单独一块清单 + 单独一个 ``StoreBundle`` 字段 ``snapshot``
#: + 用**同一个** ``SqliteMetaStore`` 实例。接口契约见 ``app/storage/base.py`` 的
#: ``LocalSnapshotArchiver``（写面）与 ``SnapshotSource``（读面）。
#:
#: 谁用它们：``services/backup_snapshot.py``（打包，唯一的写面调用方）与阶段 5 的按点恢复
#: （读面）；两者都经 ``StoreBundle.snapshot`` 拿到（服务器档那个字段恒为 ``None``）。
LOCAL_SNAPSHOT_METHODS: frozenset[str] = frozenset(
    {
        # 写面：在线备份 + 擦洗 + VACUUM + 读数（源库一个字节不动）
        "dump_scrubbed_db",
        # 读面：从一份快照库里读会话 / 产物 Key / 设置键 / 计数（窄投影）
        "read_snapshot_db",
        # 读面：逐条读全量会话（阶段 5 的按点恢复要的那种 ConversationTransfer）
        "iter_snapshot_transfers",
        # 本机认得的 schema 版本（两份兼容判据要比它，常量住在这一层）
        "local_schema_version",
    }
)

#: **备份待传队列**（M5 阶段 3）：``backup_snapshots`` 一张表上的五个方法。
#:
#: **为什么它是第四块"本机独有"**（前三块是导入台账、知识库快照、快照打包）：
#:
#: - **不进 ``LOCAL_METHODS``**：那是"``MetaStore`` 的哪些方法归本机"的划分，而这五个
#:   方法**不在 ``MetaStore`` 上**——服务器档自己就是备份的目的地，它没有"把一份快照排队
#:   传出去"这条动作（NAS 侧那一半是**收包**：``api/v1/backup.py`` 的七条端点）。
#:   硬塞进去还会让三条既有守卫当场红（那是"不许悄悄超域"的守卫，不该改松）；
#: - **也不是 KB 域**：这张表与知识库没有关系。
#:
#: 所以它与前三块同一套登记手法：单独一块清单 + 单独一个 ``StoreBundle`` 字段
#: ``backup_queue`` + 用**同一个** ``SqliteMetaStore`` 实例。接口契约见
#: ``app/storage/base.py`` 的 ``BackupSnapshots``（含"为什么是库不是目录"的四条）。
#:
#: 谁用它们：``services/backup_queue.py``（补传队列，唯一的调用方）；它经
#: ``StoreBundle.backup_queue`` 拿到（服务器档那个字段恒为 ``None``）。
LOCAL_BACKUP_METHODS: frozenset[str] = frozenset(
    {
        # 整行写入（同 id 覆盖）/ 取一行
        "put_backup_snapshot",
        "get_backup_snapshot",
        # 两种读形状：队列（最旧在前 + 到期过滤）/ 最近几份（新的在前）
        "list_backup_snapshots",
        # 推进一步状态（自增在 SQL 里，终态清掉"下一次"）
        "mark_backup_snapshot",
        # 崩溃恢复：uploading → pending
        "reset_uploading_snapshots",
    }
)

#: **安全擦除**（阶段 6 收尾）：把本机库擦干净的那一个方法。
#:
#: **为什么它是第五块"本机独有"**（前四块是导入台账、知识库快照、快照打包、待传队列）：
#:
#: - **不进 ``LOCAL_METHODS``**：那是"``MetaStore`` 的哪些方法归本机"的划分，而这个方法
#:   **不在 ``MetaStore`` 上**——它操作的是**本机那个库文件**（主库 + ``-wal`` + ``-shm``），
#:   服务器档根本没有这份文件（它的数据在 PG 里，那里的处置是 NAS 自己的访问控制，R14）；
#: - **也不是 KB 域**：它连表都不看，纯粹是文件级的收尾（抹零 + 重建 + 收 WAL）。
#:
#: 它与前四块同一套登记手法：单独一块清单 + 单独一个 ``StoreBundle`` 字段 ``eraser``
#: + 用**同一个** ``SqliteMetaStore`` 实例。接口契约见 ``app/storage/base.py`` 的
#: ``LocalEraser``；真身在 ``sqlite_impl/connection.py`` 的 ``Database.secure_erase``。
#:
#: 谁用它：``services/credentials.py``（凭据迁移器清完明文之后的收尾那一下，唯一的调用方）；
#: 它经 ``StoreBundle.eraser`` 拿到（服务器档那个字段恒为 ``None``）。
LOCAL_ERASER_METHODS: frozenset[str] = frozenset(
    {
        # 抹零 → VACUUM → wal_checkpoint(TRUNCATE)，三件事一次做完（边车不用重启）
        "secure_erase",
    }
)


def _protocol_methods(protocol: type) -> frozenset[str]:
    """协议类体里的公开方法名。

    **不许手抄**（实施方案 §2.1）：两百来个方法手抄必漏，而漏掉的那个方法
    会在运行期以 ``AttributeError`` 的形式出现在某条边角路径上。所以从
    ``repositories.py`` 已经切好的协议里机械取名字——那边有
    ``tests/unit/storage/test_repositories.py`` 守着"每个方法恰好属于一个协议、
    一个不漏、签名与 ABC 逐字一致"，于是这份导出天然跟着接口走。
    """
    return frozenset(
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    )


def local_methods() -> frozenset[str]:
    """本机域方法集：8 个协议的公开方法 ∪ ``LOCAL_EXTRA``。

    做成函数而不是模块级常量，是为了让调用方每次拿到的是**当前**协议的样子
    （阶段 2 的 ``RouterMetaStore`` 要用它算"剩下的归 KB 域"）。
    """
    return frozenset().union(*(_protocol_methods(item) for item in LOCAL_PROTOCOLS)) | LOCAL_EXTRA


#: 本机域方法集（``SqliteMetaStore`` 实现的方法必须**恰好**是它）。
#:
#: 放在这里而不是阶段 2 的 ``split_impl/router.py``：它是"哪些域本机"这个决定的
#: 唯一落点，阶段 2 的路由器与本模块的用例都从这里取，避免出现第二份会漂的清单。
LOCAL_METHODS: frozenset[str] = local_methods()
