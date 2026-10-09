"""存储抽象层：接口与数据记录。

纪律（工程规范 §3.3）：

- ``services/`` 只允许 import 本模块的接口，**禁止 import** 任何 ``storage/*_impl/``；
- 本模块内不得出现任何 SQLite 方言（SQL、连接对象、``rowid`` 语义），
  这是 v0.12 从 SQLite 迁到 PostgreSQL 时接口一行未改的原因（《架构设计 v0.2》§8.3 迁移后门）。

关于 embedding 维度（M1 决策 D4）：维度**不是全局常量**，而是每个知识库的属性
（``KnowledgeBaseRecord.embedding_model_id`` / ``embedding_dim``），
向量表按知识库分区、建表时使用该库的维度。这样换模型/换 endpoint 的规则
（《架构设计 v0.2》§6.4）可以在运行时校验，而不必锁死 schema。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # **只在类型检查时导入**：窄协议模块反过来要在运行时导入本模块的记录类型，
    # 真导入就成环。注解有 `from __future__ import annotations` 兜底，运行时不需要它。
    from app.storage.repositories import (
        ApiKeyRepo,
        ChunkRepo,
        ConversationRepo,
        DataSourceRepo,
        DocumentRepo,
        FolderRepo,
        IdempotencyRepo,
        IdentityRepo,
        ImageRepo,
        KnowledgeBaseRepo,
        MaintenanceRepo,
        MCPServerRepo,
        ModelRegistryRepo,
        NoteRepo,
        ParseResultRepo,
        ScheduleRepo,
        SettingsRepo,
        ShareRepo,
        TaskQueueRepo,
        TrashRepo,
        UsageRepo,
        WebhookRepo,
        WikiRepo,
        WorkspaceRepo,
    )

from app.models.enums import (
    ApiKeyPermission,
    DataSourceKind,
    DocumentStage,
    SharePermission,
    TaskKind,
    TaskState,
    TrashKind,
    UserRole,
)

__all__ = [
    "ARTIFACT_IN_OBJECTS",
    "ARTIFACT_IN_WORKSPACE",
    "BACKUP_SNAPSHOT_KINDS",
    "BACKUP_SNAPSHOT_STATES",
    "BACKUP_UNFINISHED_STATES",
    "IMPORT_OUTCOMES",
    "IMPORT_STATES",
    "IMPORT_UNFINISHED_STATES",
    "SNAPSHOT_EXCLUDED_SETTING_PREFIXES",
    "ApiKeyRecord",
    "BackupSnapshotRecord",
    "BackupSnapshots",
    "ChunkRecord",
    "ConversationArtifactRecord",
    "ConversationTransfer",
    "DataSourceRecord",
    "DocumentPartRecord",
    "DocumentRecord",
    "FullTextStore",
    "ImageRecord",
    "ImportBatchRecord",
    "ImportItemRecord",
    "ImportLedger",
    "KbMetaCache",
    "KbMetaCacheRecord",
    "KbMetaCacheStats",
    "KnowledgeBaseRecord",
    "LocalSnapshotArchiver",
    "MetaStore",
    "NoteFolderRecord",
    "ObjectStore",
    "ParseResultRecord",
    "ScheduledTaskRecord",
    "SearchHit",
    "SessionEventRecord",
    "SessionRecord",
    "ShareRecord",
    "SnapshotArtifactRef",
    "SnapshotConversationRef",
    "SnapshotDbView",
    "SnapshotDumpReport",
    "SnapshotFormatError",
    "SnapshotRedaction",
    "SnapshotSource",
    "StorageError",
    "StoreBundle",
    "TaskRecord",
    "TrashRecord",
    "VectorDimensionMismatch",
    "VectorMatch",
    "VectorStore",
    "WebhookRecord",
]


@dataclass(frozen=True, slots=True)
class StoreBundle:
    """五个仓储的聚合视图（由组合根填充）。

    ``services/`` 依赖这个类型就能拿到全部存储能力，而**不必 import 任何具体实现**——
    字段类型全是接口，组合根 `app/core/storage.py` 负责把实现塞进来。
    """

    meta: MetaStore
    vectors: VectorStore
    fulltext: FullTextStore
    objects: ObjectStore
    tabular: TabularStore
    """表格结构化副本（DuckDB）。只有 CSV/Excel 会用，其余文档不碰它。"""

    ledger: ImportLedger | None = None
    """旧会话导入的**台账**（M2 阶段 5）：本机档才有，服务器档恒为 ``None``。

    **为什么它不是 ``MetaStore`` 的窄视图**（本文件里两个这样的字段之一，另一个是
    下面的 ``kb_cache``）：``imports`` / ``import_items`` 是**本机独有的两张表**，
    服务器档没有它们，也没有"从别的部署导会话进来"这条动作——所以它既不进
    ``repositories.py`` 的 24 个域（那里的每个方法都必须在 ``MetaStore`` 上存在，
    有两条用例逐名核对），也不进 ``LOCAL_METHODS``（那是"本机域 / KB 域"的划分，
    它两边都不属于）。它在 ``sqlite_impl.LOCAL_LEDGER_METHODS`` 单独登记，装配点见
    ``core/storage.py::_build_local_stores``（与 ``meta`` 用的是**同一个**实例）。

    默认 ``None`` 是刻意的：五个仓储之外的一切调用点、以及服务器档的两处装配
    都不需要改一个字。要用它的人必须显式处理"这台机器没有导入能力"那一支
    （本机档配上它、服务器档是 ``None``）——不留一个"静默的空实现"。
    """

    kb_cache: KbMetaCache | None = None
    """知识库元数据**快照**（M4 §3.3）：本机档才有，服务器档恒为 ``None``。

    **与 ``ledger`` 同一条纪律、同一套理由**（照那段改写一遍，因为形状一模一样）：
    ``kb_meta_cache`` 是**本机独有的一张表**——服务器档的 KB 元数据本来就在自己的
    PG 里，它没有"从 NAS 抄一份快照"这条动作，也不该有（缓存一个自己就是真相源的
    东西只会多一层会过期的副本）。所以它既不进 ``repositories.py`` 的 24 个域
    （那里的每个方法都必须在 ``MetaStore`` 上存在），也不进 ``LOCAL_METHODS``
    （那是"本机域 / KB 域"的划分：它服务的是 **KB 域的读路径**，人却不属于 KB 域）。
    它在 ``sqlite_impl.LOCAL_CACHE_METHODS`` 单独登记，装配点见
    ``core/storage.py::_build_local_stores``（与 ``meta`` / ``ledger`` 是**同一个**
    实例：写锁是进程内一把，那条纪律是对着 ``Database`` 说的）。

    页面上"先画快照"那一层（``/local/kb-cache/*``）与 reader 面的缓存包装器都从
    它取数。**它严格可弃**（v0.3 §5.3）：删了只丢速度，不丢数据——真话永远在 NAS 上。
    """

    snapshot: LocalSnapshotArchiver | SnapshotSource | None = None
    """本机档独有的**快照打包与读回**（M5 阶段 2）：服务器档恒为 ``None``。

    **与 ``ledger`` / ``kb_cache`` 同一条纪律、同一套理由**（照那两段改写一遍，因为形状
    一模一样）：快照这件事是**本机独有**的——服务器档的库就是它自己，没有"把自己打成
    一份便携的包"这条动作，也不该有（NAS 侧那一半是**收包**：``api/v1/backup.py`` 的
    七条端点，与这里的两份面是两回事）。所以它既不进 ``repositories.py`` 的 24 个域
    （那里的每个方法都必须在 ``MetaStore`` 上存在），也不进 ``LOCAL_METHODS``（那是
    "本机域 / KB 域"的划分：快照既不是本机域的读写，也不是 KB 域的东西）。它在
    ``sqlite_impl.LOCAL_SNAPSHOT_METHODS`` 单独登记，装配点见
    ``core/storage.py::_build_local_stores``（与 ``meta`` / ``ledger`` / ``kb_cache`` 是
    **同一个**实例：写锁是进程内一把，那条纪律是对着 ``Database`` 说的）。

    **两份面，同一个对象**：``LocalSnapshotArchiver`` 是写面（把在线备份 + 擦洗 + VACUUM
    出来的副本交给打包器），``SnapshotSource`` 是读面（从一份快照库里读会话 / 产物 Key /
    设置）。分开的理由不是"两个对象"，而是**两个调用方要的东西不同**：打包那一层只该
    看见"给我一份擦洗干净的副本"，而阶段 5 的按点恢复只该看见"这份快照里有什么"。
    两份面都由 ``SqliteMetaStore`` 满足（SQLite 方言只许住在 ``sqlite_impl/``）。
    """

    backup_queue: BackupSnapshots | None = None
    """备份的**待传队列**（M5 阶段 3）：本机档才有，服务器档恒为 ``None``。

    **与前两块（``ledger`` / ``kb_cache``）和上一块（``snapshot``）同一条纪律、同一套
    理由**：``backup_snapshots`` 是**本机独有的一张表**——服务器档自己就是备份的目的地，
    它没有"把一份快照排队传出去"这条动作（NAS 侧那一半是**收包**：``api/v1/backup.py``
    的七条端点，与这张表是两回事）。所以它既不进 ``repositories.py`` 的 24 个域
    （那里的每个方法都必须在 ``MetaStore`` 上存在），也不进 ``LOCAL_METHODS``（那是
    "本机域 / KB 域"的划分：队列既不是本机域的读写，也不是 KB 域的东西）。它在
    ``sqlite_impl.LOCAL_BACKUP_METHODS`` 单独登记，装配点见
    ``core/storage.py::_build_local_stores``（与 ``meta`` / ``ledger`` / ``kb_cache`` /
    ``snapshot`` 是**同一个**实例：写锁是进程内一把，那条纪律是对着 ``Database`` 说的）。

    **它与 ``snapshot`` 是两个字段、两件事**（名字上刻意分开）：``snapshot`` 对着
    "打一份快照"，``backup_queue`` 对着"把那一份传出去"。服务层那两层的分工也因此是
    一条直线：``BackupSnapshotService``（打包）把结果交给 ``BackupQueueService``（排队）,
    后者只在拿到 ``BackupSnapshots`` 时才存在——本机档配上它、服务器档是 ``None``。
    """

    eraser: LocalEraser | None = None
    """本机档独有的**安全擦除**（M5 阶段 6 收尾）：服务器档恒为 ``None``。

    **与前四块（``ledger`` / ``kb_cache`` / ``snapshot`` / ``backup_queue``）同一条纪律、
    同一套理由**（照那几段改写一遍，因为形状一模一样）：它擦的是**本机那个库文件**
    （主库 + ``-wal`` + ``-shm``）——服务器档的数据在 PG 里，也没有系统钥匙串那一整套
    动作（R14：那一档库里那份凭据不动，处置是 NAS 自己的访问控制）。所以它既不进
    ``repositories.py`` 的 24 个域（那里的每个方法都必须在 ``MetaStore`` 上存在），也不进
    ``LOCAL_METHODS``（那是"本机域 / KB 域"的划分：擦除连表都不看）。它在
    ``sqlite_impl.LOCAL_ERASER_METHODS`` 单独登记，装配点见
    ``core/storage.py::_build_local_stores``（与 ``meta`` / ``ledger`` / ``kb_cache`` /
    ``snapshot`` / ``backup_queue`` 是**同一个**实例）。

    **为什么值得单独一个字段**（而不是并进 ``meta``）：调用方是钥匙串收编的迁移器，
    它要表达的动作是"这几处明文已经从库里删掉了，现在把那几页真的还回去"——那是**文件级**
    的收尾（抹零 + 重建 + 收 WAL），不是任何一张表的读写。把它放在这一层，L2 那条纪律
    （``services/`` 不许 import ``sqlite3``）才不用被破一次：三个 PRAGMA / VACUUM / 检查点
    全在 ``sqlite_impl/`` 里，服务层只看见一个动词。

    谁用它：``services/credentials.py``（唯一的调用方）；它经 ``StoreBundle.eraser`` 拿到。
    """

    # ---- 按域切开的窄视图（v0.2，见 storage/repositories.py）----
    #
    # 它们**返回的是同一个 ``meta`` 实例**，只是按域收窄了类型：新代码依赖窄接口，
    # "这个模块需要什么"在签名里读得出来；老代码走 `meta.*` 零改动。
    # 这是拆 MetaStore 的第一步，不是行为变更——23 个域与 183 个方法的归属
    # 见 repositories.py，`tests/unit/storage/test_repositories.py` 机械核对。

    @property
    def knowledge_bases(self) -> KnowledgeBaseRepo:
        """知识库域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def documents(self) -> DocumentRepo:
        """文档域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def folders(self) -> FolderRepo:
        """目录域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def notes(self) -> NoteRepo:
        """笔记域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def chunks(self) -> ChunkRepo:
        """切块域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def images(self) -> ImageRepo:
        """图片域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def parse_results(self) -> ParseResultRepo:
        """解析产物域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def tasks(self) -> TaskQueueRepo:
        """任务队列域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def data_sources(self) -> DataSourceRepo:
        """数据源域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def api_keys(self) -> ApiKeyRepo:
        """API Key 域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def webhooks(self) -> WebhookRepo:
        """Webhook 域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def idempotency(self) -> IdempotencyRepo:
        """幂等键域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def conversations(self) -> ConversationRepo:
        """对话留存域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def workspaces(self) -> WorkspaceRepo:
        """工作区域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def schedules(self) -> ScheduleRepo:
        """定时任务域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def identity(self) -> IdentityRepo:
        """身份域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def shares(self) -> ShareRepo:
        """分享域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def usage(self) -> UsageRepo:
        """用量域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def models(self) -> ModelRegistryRepo:
        """模型注册域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def mcp_servers(self) -> MCPServerRepo:
        """外部 MCP 服务域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def trash(self) -> TrashRepo:
        """回收站视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def app_settings(self) -> SettingsRepo:
        """设置（键值）域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def wiki(self) -> WikiRepo:
        """Wiki 域视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]

    @property
    def maintenance(self) -> MaintenanceRepo:
        """存储维护视图（`meta` 的窄类型）。"""
        return self.meta  # type: ignore[return-value]


class StorageError(Exception):
    """存储层错误基类：让 services 不必 import 具体实现就能捕获。"""


class VectorDimensionMismatch(StorageError):
    """向量维度与既有分区不一致。

    架构 §6.4：维度相同不等于向量空间兼容，**维度不同更是绝对不能混写**——
    静默写入只会让检索结果悄悄错掉，所以这里必须硬失败。
    """


KB_UNAVAILABLE_MESSAGE = (
    "知识库在 NAS 服务器上：本机档没有它的数据源"
    "（本机走的是「知识库提供者」；连接状态在设置里的「知识库连接」）"
)
"""本机档访问知识库域时的那句话（照实说，不伪装成"库里没有"）。

**M3 起后半句改了**：原先写的是"…，M3 接知识库提供者"（那时它是个承诺）。
提供者已经落地，所以这句话要说的是"**这一条路**本机没接、以及去哪儿看状态"——
抛出它的仍然是 `Unavailable*` 那几个仓储（它们是进程内那条链的占位），
而**真的那条链**在 `services/knowledge_provider.py`（方案 §2.1）。"""


class KnowledgeBaseUnavailable(StorageError):
    """本机档访问知识库域的东西：数据源在 NAS 上，这里没有。

    **如实抛，不伪装**：回空列表等于告诉调用方"查过了，没有"，而它其实**没查过**。
    HTTP 面由 ``app/core/exceptions.py`` 映射成 503 + ``str(exc)`` 那句话（不伪装成
    500"内部错误"：这是"这个部署没有这个能力"，不是"我们出错了"）。

    ``StorageError`` 子类而不是 ``KylabError`` 子类：它是存储层说出的事实（"这个仓储
    没有这个能力"），而"折成哪个状态码"属于协议层——存储层不认识 HTTP。

    **为什么住在接口层**（M3 阶段 2 从 ``storage/split_impl/router.py`` 搬上来）：
    抛出它的不止存储实现——M3 的知识库提供者客户端（``services/knowledge_provider.py``）
    也要抛它（"未配/不可用"就是"本机档没有知识库"的另一面，方案 §5.1 / R10）。而
    ``services/`` 只允许 import ``app.storage.base``（工程规范 §3.3 L2，
    ``scripts/check_layering.py`` 会拦），所以它必须与 ``StorageError`` 同处一地——
    那正是"services 不必 import 具体实现"这条承诺的另一半。
    """

    message = KB_UNAVAILABLE_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)


class SnapshotFormatError(StorageError):
    """快照**读不了**：格式不认识、版本比本机新、结构不完整。

    **与 ``schema.py`` 那条"不降级"同一句话**（M5 §2.3 的兼容判据）：读到一份自己不认识的
    快照，唯一诚实的动作是**当场拒绝**，不是"按旧结构猜着读"——猜错了的结果是往用户的
    库里写进半截数据，而"读不了"只是恢复不了这一份。三种情况都走这里：

    - ``format`` 不是 ``kylab-backup``（这不是我们的包）；
    - ``format_version`` / ``schema_version`` 高于本机认识的上限（更省事的做法是
      "只读我认识的那几列"，那正是猜）；
    - 必备字段缺席或类型不对（一份半截的 manifest）。

    **住在接口层**的理由与 ``KnowledgeBaseUnavailable`` 一模一样：抛出它的不止
    ``sqlite_impl``（``read_snapshot_db`` 读快照库的 schema 版本时抛），还有
    ``services/backup_snapshot.py`` 的 manifest 解析器——而 ``services/`` 只允许
    import ``app.storage.base``（工程规范 §3.3 L2）。两处抛同一个类型，调用方才有
    一句话可捕获。
    """


# --------------------------------------------------------------------- 对象存储的
# 逻辑布局与寻址规则放在接口层：services 需要按同样的规约生成 Key，
# 但**不能**去 import 具体实现（那会踩到工程规范 §3.3 的 L2 规则）。

ORIGINALS = "originals"
MARKDOWN = "markdown"
IMAGES = "images"

TRASH = ".trash"
"""回收站目录名。

**放在接口层而不是某个实现里**：``move_to_trash`` 的返回值会写进 ``trash`` 表，
换实现（本地文件系统 ↔ S3）时那些历史路径必须仍然解析得到——两套实现各写一份
常量，迟早会漂成 ``.trash`` 与 ``trash``。
"""

SAFE_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
"""存储 Key 与回收站 ID 允许的字符：它们会拼进文件路径，必须白名单化。"""


def content_key(kind: str, content_hash: str, suffix: str = "") -> str:
    """按内容 hash 生成存储 Key，例如 ``content_key(ORIGINALS, sha, ".pdf")``。

    两级散列目录（``originals/ab/abcdef…pdf``）避免单目录堆几万文件；
    相同内容天然同路径，重复上传不会产生第二份。
    """
    if not content_hash:
        raise ValueError("content_hash 不能为空")
    digest = "".join(char for char in content_hash if char in SAFE_KEY_CHARS)
    if not digest:
        raise ValueError(f"content_hash 不含可用字符：{content_hash!r}")
    return f"{kind}/{digest[:2]}/{digest}{suffix}"


# --------------------------------------------------------------------------- 记录（值对象）


@dataclass(slots=True)
class KnowledgeBaseRecord:
    """知识库。

    ``embedding_model_id`` 一旦入库即冻结：库内已有向量时不允许更换模型，
    换 endpoint 时模型 ID 必须一致（《架构设计 v0.2》§6.4）。
    """

    id: str
    name: str
    embedding_model_id: str
    embedding_dim: int
    embedding_base_url: str | None = None
    chunk_strategy: str = "fixed"
    description: str = ""
    """库简介（v15）。列表卡片上的一句概述；空串 = 未填写。"""
    chunk_size: int = 512
    chunk_overlap: int = 64
    suggested_enabled: bool = False
    """入库时是否为每个分段生成推荐问题（v19 起，v23 起是这个含义）。

    **默认关**：生成发生在上传之后、要花模型调用（每 8 段一次请求），
    于是它必须由用户显式打开，而不是升级后默默开始烧 token。
    （列上的 DEFAULT 仍是 1——SQLite 改不了列默认值；但所有建库都经服务层，
    它显式传值，迁移 023 也把老库统一置 0。）"""
    suggested_count: int = 3
    """**每个分段生成几条**（v23 起；v22 时曾是"空状态显示几条"）。
    上限由服务层夹住（`suggested_questions.MAX_QUESTIONS`）。默认值在这里写一份、
    服务层写一份——storage 不许依赖 services（工程规范 §3.3 L3），
    两处由用例钉住一致。"""
    suggested_model_pk: str | None = None
    """出题用哪个对话模型（注册表主键）。``None`` = 跟随对话页当前选的模型。"""
    suggested_prompt: str = ""
    """自定义出题提示词。空串 = 用内置提示词（替换内置的**指令**那句，
    资料片段仍然由服务层附加）。"""
    system_prompt: str = ""
    """**库级提示词**（v0.19）：回答这个库的问题时，助手该怎么答。

    从对话页搬过来的。那份"系统提示词"原先挂在全局设置 `chat.system_prompt` 上，
    可它实质是**库的属性**——"这份资料该怎么被使用"随资料走，不随界面走。
    "换个库看还留着上一个库的规矩"是那个设计解释不了的。

    空串 = 用内置提示词（`services/chat.DEFAULT_SYSTEM_PROMPT`）；一轮里选了多个库时，
    有提示词的按库名拼成一段（见 `ChatService._kb_prompt`）。"""
    owner_id: str | None = None
    """归属账号（v10）。``None`` = 账号体系启用前的老数据，
    由 setup 向导认领给首个管理员（`services/auth.py`）。"""
    embedding_model_pk: str | None = None
    """所选的**注册模型**主键（v11）。嵌入模型是知识库属性而非全局设置：
    建库时从注册表里挑一个（文献量小的库可用高精度模型，量大的用小模型提速）。
    ``None`` = 没显式选，运行时回退到注册表的默认槽位 / 设置页配置。
    凭据不落这里——只在注册表存一份，运行时按 pk 解析。"""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    wiki_enabled: bool = False
    """库形态（v24）：是否把库里已录入的内容整理成一套 Wiki 页面。

    ``False``（默认）= 仅向量检索：问答按片段检索原文作答，最省 token。
    ``True`` = 向量检索 + Wiki：额外生成带原文出处的百科式页面。
    它**只是一个开关**——不改变检索链路，Wiki 只是多出来的一层产物。
    """


@dataclass(slots=True)
class WikiPageRecord:
    """生成出来的一页 Wiki（v24）。

    ``level`` + ``parent_id`` 就是摘要树的落库形态：level 0 是总览页（根），
    1 是主题页。``content_md`` 里带 ``[n]`` 标记，n 对应
    :class:`WikiSourceRecord.index` —— **每个要点都指得回原文**，这是自动 Wiki
    敢被人相信的前提（调研报告 §3.5）。
    """

    id: str
    kb_id: str
    title: str
    parent_id: str | None = None
    level: int = 0
    ord: int = 0
    slug: str = ""
    brief: str = ""
    content_md: str = ""
    status: str = "ready"
    """``ready`` / ``generating`` / ``failed``。本版按"整库一次性重建"写，
    所以生成期间行不存在；这个字段主要给失败重试与将来的增量留位。"""
    model: str | None = None
    generated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class WikiSourceRecord:
    """一页 Wiki 的一条出处（v24）。"""

    page_id: str
    chunk_id: str
    document_id: str
    rank: int = 0
    index: int = 0
    """正文里的 ``[n]`` 编号。**持久化时就是它**（列名 ``rank``）——
    引用编号必须与生成时喂给模型的那份清单一致，重排会让正文里的 ``[n]`` 指错段落。"""
    heading_path: str | None = None
    """命中那段所属的章节路径（生成时从检索命中一起抄下来）。

    **存快照而不是回查 chunks**：文档被删/重切之后，这一页引用的原文可能已经不在，
    但"当时引的是哪一段"必须留得住——否则页面上的出处会集体变成悬空编号。
    """
    page: int | None = None
    """命中那段的页码（同上，存快照）。"""


@dataclass(slots=True)
class DocumentStageEventRecord:
    """一次"进入某阶段"的事件（v24）。进度时间线的数据源。

    阶段会**重复进入**（失败重试、重新摄入、取消后重跑），所以事件是追加的、
    不是覆盖的——"每个环节各花多久"正是相邻两次进入的时间差。
    """

    document_id: str
    stage: str
    entered_at: datetime
    id: int = 0
    """自增序号。比时间戳更适合定序：同一微秒进入两次也分得清先后。"""
    error: str | None = None
    """进入该阶段时带上的错误（失败/取消时有值）。"""


@dataclass(slots=True)
class DocumentRecord:
    """文档。大文件切分后，本记录代表用户看到的那一个文件。"""

    id: str
    knowledge_base_id: str
    name: str
    source_kind: DataSourceKind
    content_hash: str
    stage: DocumentStage
    size_bytes: int = 0
    mime_type: str | None = None
    page_count: int | None = None
    is_split: bool = False
    error: str | None = None
    uploaded_by: str | None = None
    """上传者的使用者 id（G6）。``None`` = 系统摄入或名册启用前的老数据，
    界面据此显示"未记录"，而不是编一个名字出来。"""
    folder_id: str | None = None
    """所在目录（v13）。``None`` = 未归档（根目录）。"""
    disabled: bool = False
    """停用（v14）。停用后**不参与检索**，但原文/切块/向量都保留——与
    chunks.disabled 同一套语义：禁用与删除是两件事，恢复零成本。"""
    summary: str = ""
    """入库时生成的紧凑摘要（v25，见 services/summary.py）。空串 = 还没生成。

    **它的用途是省 token**：问答上下文里按文档带一行摘要，就不必把每段命中都补成
    "整个小节"（那是上万字）。顺带在界面上也是一句有用的说明文字。
    """
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class FolderRecord:
    """知识库内的目录（v13）。

    **单层、不嵌套**：个人知识库的规模下，一层分类就够把不同用途的文件分开，
    而嵌套会立刻带来拖拽跨层、路径拼接、删除策略一串复杂度。
    """

    id: str
    kb_id: str
    name: str
    created_at: datetime | None = None


@dataclass(slots=True)
class NoteFolderRecord:
    """笔记文件夹（v14）。

    **与 `FolderRecord` 刻意不同的一点是 `parent_id`**：知识库的目录单层（理由见上一条），
    笔记的文件夹允许嵌套——它承载的是用户自己的知识组织方式，"工作 / 会议记录"
    这样的两层在真实笔记里是常态，而不是要等规模变大才出现的需求。

    级联语义（与 ``schema.py`` 的迁移 v14 逐字对应，两处必须一起读）：
    删父文件夹连带删子文件夹；**子文件夹里的笔记一律回到未归档**（``ON DELETE SET NULL``），
    不跟着消失——删容器不该销毁内容。
    """

    id: str
    user_id: str | None = None
    name: str = ""
    #: 父文件夹 id；``None`` = 根级
    parent_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class NoteRecord:
    """一条笔记（v20）。

    ``content_md`` 是**唯一事实源**：编辑器（Tiptap）的 JSON 不落库，
    导出的 Markdown 既直接可读，也能原样喂给摄入流水线。
    """

    id: str
    user_id: str | None = None
    title: str = ""
    content_md: str = ""
    #: manual（手记）/ chat（问答存为）/ clip（剪藏）
    source_kind: str = "manual"
    #: chat 存会话或消息 id，clip 存 URL
    source_ref: str | None = None
    #: 「加入知识库」后指向生成的文档（未入库为空）
    kb_id: str | None = None
    doc_id: str | None = None
    #: 所属文件夹（v14）；``None`` = 未归档
    folder_id: str | None = None
    pinned: bool = False
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class DocumentPartRecord:
    """子文件（大文件强制切分后的页范围片段，UI 显示为可展开的子文件树）。

    页码偏移用于合并时把子文件的页号重映射回原文（《架构设计 v0.2》§4.2）。
    """

    id: str
    document_id: str
    part_index: int
    page_start: int
    page_end: int
    stage: DocumentStage
    error: str | None = None


@dataclass(slots=True)
class ChunkRecord:
    """切块。``chunk_id`` 稳定、``content_hash`` 用于增量更新时对齐新旧序列。"""

    chunk_id: str
    document_id: str
    knowledge_base_id: str
    part_id: str | None
    ordinal: int
    text: str
    content_hash: str
    heading_path: str | None = None
    page: int | None = None
    image_ids: Sequence[str] = field(default_factory=tuple)
    disabled: bool = False
    """人工禁用（§G3）。被禁用的块**不再参与检索**，但仍留在库里——
    表格切碎、公式拆开这类"切得不好"的块，用户往往想留着待改，而不是直接删掉。"""
    questions: Sequence[str] = field(default_factory=tuple)
    """入库时由模型为这一段生成的问题（v23）。

    **它们是"用问题换召回"的全部实现**：见 :attr:`index_text`。空元组 = 没生成
    （这个库的功能关着，或这一篇是开启之前入库的、还没重新摄入）。"""

    @property
    def index_text(self) -> str:
        """**真正拿去向量化与建全文索引的文本**：原文 + 生成的问题。

        原文 ``text`` 保持不动——引用预览、喂给模型的资料、重排都读它，
        用户不该在回答里看到"问题"混进原文。索引侧多出这一层之后，
        用户换一种问法（"怎么测眼轴" vs 正文里的"眼轴长度测量"）也能命中同一段：
        向量是原文与问题一起算的，全文索引里也有问题的词。

        所以检索链路（向量 / 全文 / RRF / 重排）**一行都不用改**——
        它们只认 ``chunk_id``，不关心这条记录的文本是怎么拼出来的。
        """
        if not self.questions:
            return self.text
        return f"{self.text}\n" + "\n".join(self.questions)


@dataclass(slots=True)
class ImageRecord:
    """图片（位置锚点方案，图片不入向量库，只记位置）。"""

    image_id: str
    document_id: str
    storage_path: str
    page: int | None = None
    bbox: str | None = None
    caption: str | None = None


@dataclass(slots=True)
class ParseResultRecord:
    """解析中间产物（分层持久化，升级解析器时只重跑下游）。"""

    document_id: str
    part_id: str | None
    parser_name: str
    markdown_path: str
    probe_meta: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class TaskRecord:
    """任务。租约字段支撑"进程崩溃后超时回收 → 断点续跑"。"""

    id: str
    kind: TaskKind
    state: TaskState
    payload: dict[str, Any] = field(default_factory=dict)
    document_id: str | None = None
    part_id: str | None = None
    attempts: int = 0
    max_attempts: int = 5
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    next_run_at: datetime | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskCounts:
    """任务队列的聚合概览（一次查询拿到，见 ``MetaStore.task_counts``）。"""

    running: int = 0
    pending: int = 0
    stalled: int = 0
    """在跑但租约已过期：没有 worker 在续约（与 ``reclaim_expired_tasks`` 同一判据）。"""
    overdue: int = 0
    """排队但已过了 ``overdue_before`` 还没被领走。"""
    oldest_pending_at: datetime | None = None
    """最老的排队任务是什么时候入的队（``None`` = 队列空）。"""
    pending_by_kind: dict[str, int] = field(default_factory=dict)
    """排队的任务按类型分布——"积压全是出题"和"积压全是解析"该做的事不同。"""


@dataclass(slots=True)
class DataSourceRecord:
    """数据源（本地上传 / HTML / RSS / WebDAV 预留）。"""

    id: str
    knowledge_base_id: str
    kind: DataSourceKind
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None
    last_pulled_at: datetime | None = None
    enabled: bool = True


@dataclass(slots=True)
class ApiKeyRecord:
    """API Key：绑定知识库范围 + 只读/读写（《架构设计 v0.2》§3.2）。"""

    id: str
    name: str
    key_hash: str
    permission: ApiKeyPermission
    knowledge_base_ids: Sequence[str] = field(default_factory=tuple)
    key_prefix: str = ""
    """明文的前若干位，**仅用于界面分辨"哪把是哪把"**。

    明文本身永不落库；这一段来自高熵随机串，不足以定位任何密钥（见 migrations 002）。
    """
    created_by: str | None = None
    """创建这把钥匙的账号 id（v10）。``None`` = 账号体系启用前发放的老钥匙。"""
    created_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass(slots=True)
class WebhookRecord:
    """Webhook 订阅（异步事件推送通道）。"""

    id: str
    url: str
    events: Sequence[str] = field(default_factory=tuple)
    secret: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class TrashRecord:
    """回收站条目：原文保留 7 天，向量已立即删除（《架构设计 v0.2》§6.2）。"""

    id: str
    document_id: str
    kind: TrashKind
    storage_path: str
    expires_at: datetime
    created_at: datetime | None = None


@dataclass(slots=True)
class IdempotencyRecord:
    """一次带幂等键的请求。

    ``response`` 为空表示"已经占住这个键，但业务还没跑完"——重放时据此回 409
    而不是让人以为没收到。见 ``services/idempotency.py``。
    """

    key: str
    request_hash: str
    response: dict[str, object] | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class ConversationRecord:
    """一次对话（会话）。

    ``title`` 由首轮提问生成——让用户自己起名字的对话工具，最后满屏都是"新对话"。
    """

    id: str
    title: str = ""
    kb_ids: Sequence[str] = field(default_factory=tuple)
    pinned: bool = False
    """置顶（v17）。置顶的会话排在列表最前，**且聊天不会改变它的名次**——
    用户置顶正是为了"别被新对话挤下去"。"""
    owner_id: str | None = None
    """归属账号（v10）。``None`` = 老数据，setup 时认领给首个管理员。"""
    model_pk: str | None = None
    """该会话选用的注册模型（v12）。``None`` = 走全局默认（注册表 chat 槽位）。"""
    thinking: bool | None = None
    """该会话是否开启思考（v16）。``None`` = 跟随全局默认。"""
    thinking_effort: str | None = None
    """该会话的思考强度（v16，low/medium/high）。``None`` = 跟随全局默认。"""
    archived_at: datetime | None = None
    """归档时间（v0.17）。``None`` = 未归档——**归档不是删除**：
    会话从列表里收起来，但内容与引用都还在，随时可以取消归档。
    用时间戳而不是布尔："什么时候收起来的"本身有用（归档视图按它排序）。"""
    workspace_id: str | None = None
    """所属工作区（v0.15）。``None`` = **未归档**，侧栏把它单独排一列。

    为什么不给未归档的会话自动建一个默认工作区：未归档是一个**真实存在的状态**
    （"我就是随手问一句"）。替它安一个默认工作区，用户就再也分不清"这条是我
    特意放进项目里的"还是"随手问的"——而那个区分正是工作区这个概念的用处。
    """
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class MCPServerRecord:
    """一个外部 MCP 服务（v0.15）：插件能力的落点。

    见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.2。``env`` / ``headers`` 里
    可能带凭据——**接口绝不回显它们的值**，只回"配过没有"。
    """

    id: str
    name: str
    transport: str
    """``stdio``（起子进程）或 ``http``（连远端服务）。"""
    target: str
    """stdio = 要执行的命令；http = 服务的 URL。"""
    args: Sequence[str] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    policy: str = "ask"
    """``allow`` / ``ask`` / ``deny``。默认 ``ask``：外部工具会以用户的名义执行动作。"""
    enabled: bool = True
    owner_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ScheduledTaskRecord:
    """定时任务（v0.33）：到点替用户做一件事。

    见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.6。它回答的是
    "有没有哪件事是**到点就该做**、而我不想每次自己去问一遍"——
    每天早晨把昨天的日志汇总、每周一把上周的周报底稿准备好。

    三处刻意的形状：

    - **两种时间**（``kind``）：``cron`` = 反复发生（5 字段表达式，按**服务器本地时间**
      解释），``once`` = 就跑一次（``run_at``）。不做"每 N 分钟"这种第三种形态——
      那用 ``*/N * * * *`` 表达得出来，多一种形态只会多一处要维护的语义；
    - **结果落进一条会话**（``conversation_id``）：每次运行都是那个会话里的一轮问答，
      所以"上周它都跑了些什么、结论是什么"就是翻会话记录——不另造一套"运行历史"
      的存储与界面。首次运行时才建这条会话（没跑过的任务不该先占一个会话）；
    - ``next_run_at`` 是**调度侧唯一的游标**：它同时承担"下次什么时候跑"与
      "这一次有没有人认领"（见 ``MetaStore.arm_scheduled_task`` 的 CAS）。
    """

    id: str
    """``sched_<hex>``。"""

    name: str
    """给人看的名字，同时会成为那条会话的标题。"""

    prompt: str
    """到点要问的那句话（它就是每次运行的用户消息）。"""

    kind: str
    """``cron`` 或 ``once``。"""

    cron: str = ""
    """5 字段 cron 表达式（``kind='cron'`` 时有效）：分 时 日 月 周。"""

    run_at: datetime | None = None
    """一次性任务的执行时刻（``kind='once'``）。"""

    next_run_at: datetime | None = None
    """下次该跑的时刻（`timestamptz`）。``None`` = 不会再跑（已停用或一次性已跑完）。"""

    enabled: bool = True
    kb_ids: Sequence[str] = field(default_factory=tuple)
    """运行时的检索范围。**独立于用户当时的会话**：这一步决定"它去哪儿找资料"，
    不勾库就是一次不查资料的运行。"""

    model_pk: str | None = None
    thinking: bool | None = None
    thinking_effort: str | None = None
    conversation_id: str | None = None
    owner_id: str | None = None
    last_run_at: datetime | None = None
    last_status: str = ""
    """``ok`` / ``failed``，或空串（还没跑过）。"""

    last_error: str = ""
    run_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class WorkspaceRecord:
    """工作区（v0.15）：Agent 的"在哪儿干活"。

    见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §3。与**沙箱**是两个概念：
    工作区是长期、用户拥有、`root_path` 是他自己的目录；沙箱是一次性的试错空间。
    """

    id: str
    name: str
    root_path: str
    """用户指定的真实目录（绝对路径）。创建时校验存在且是目录，
    并拒绝指向数据目录或文件系统根——否则"把工作区设成 /"就等于把整台机器交出去。"""
    owner_id: str | None = None
    """归属账号。与知识库 / 会话同一套口径：``None`` = 管理员或 API Key 通道，
    能看到全部；普通成员只看自己的。"""
    description: str = ""
    kb_ids: Sequence[str] = field(default_factory=tuple)
    """这个工作区**带着哪些知识库**。这是"知识库与 Agent 天生融合"的落点：
    进入工作区，资料范围就定了；新会话默认继承它们（见设计文档 §5）。"""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    """归档时间（v0.55）。``None`` = 未归档。与会话归档同一口径：用时间戳而不是布尔，
    "什么时候收起来的"本身有用；归档**不是删除**，里面的会话与内容都还在。"""
    device_id: str | None = None
    """归属设备（v0.59）。桌面壳每次请求带 ``X-Kylab-Device: <uuid>``，工作区因此
    **按设备隔离**：带头的只看得到 ``device_id`` 等于自己那一支的。

    **``None`` = 服务器端**：网页版/直连 API 不带设备头，看到的就是这一批——
    语义不是"没有归属"，而是"``root_path`` 在服务器的盘上"（与桌面端那种
    "路径在用户那台机器的盘上"相对）。所以这一列可空，存量记录全落在这一档。"""
    device_name: str = ""
    """设备名（``X-Kylab-Device-Name`` 头，可空）。**只给人看**：界面上说清
    "这个项目在哪台机器上"，判定一律按 ``device_id``。服务器端是空串。"""


@dataclass(slots=True)
class ChatMessageRecord:
    """会话里的一条消息。

    ``sources`` 是**引用快照**（当轮命中的原文出处），不是每轮重新检索的结果：
    历史回答当时依据的是哪几段，事后回看必须还是那几段，否则引用编号就对不上了。
    """

    id: str
    conversation_id: str
    role: str
    content: str
    sources: Sequence[dict[str, object]] = field(default_factory=tuple)
    steps: Sequence[dict[str, object]] = field(default_factory=tuple)
    """当轮的过程步骤（工具调用、组织回答…，v0.25）。

    与 ``sources`` 一样是**快照**：回看一条旧回答时，当时调了哪些工具、
    每一步拿到什么，都该是当时的样子。此前这两样只活在流式那几秒里，
    离开页面就没了——而"这句答案是怎么来的"正是回来要找的东西。
    """

    thinking: str = ""
    """当轮的思考过程全文（推理模型的 ``reasoning_content``，v0.25）。空串 = 没有思考。"""

    attachments: Sequence[dict[str, object]] = field(default_factory=tuple)
    """这条消息**随发的附件快照**（v0.55，只有用户消息会有）。

    每项形如 ``{"key": "art_…", "name": "指南.pdf", "kind": "pdf", "size_bytes": 448444}``。
    存快照而不是外键，与 ``sources`` 同一口径：回看时"当时带着哪几份文件"必须是当时的样子。
    """

    created_at: datetime | None = None


@dataclass(slots=True)
class SessionEventRecord:
    """会话事件（P0-2，抄 ZCode 的**只追加事件日志**）。

    ZCode 把整个会话表达成一串不可变的事件
    （``Turn{Started,Complete}`` / ``ToolCall{Started,Result}`` / ``Model{Error}``…），
    会话正文只是它的投影（调研报告《Agent-与对话架构对标调研 v0.1》§2.1）。
    这张表就是那条设计在 KYLAB 的落点：``chat_messages.steps`` 那份**流式当时
    拍下的快照**从此可以用这份日志现算（``services/session_events.steps_from_events``），
    续跑 / 压缩 / 回放也就不必各自打补丁。

    **只追加**：存储层只有 append 与 list，没有 update / delete。
    能被改的日志回答不了"当时发生了什么"——那是这份表存在的全部理由。
    """

    conversation_id: str
    kind: str
    """事件种类。取值是**词表**，定义在 ``services/session_events.EVENT_KINDS``
    一处；存储层不校验（它不认识业务词表），校验在服务层。"""
    payload: dict[str, object] = field(default_factory=dict)
    seq: int = 0
    """**会话内**单调递增的序号，由存储层在写那个事务里赋值（``max(seq)+1``）。

    为什么不复用 ``created_at`` 排序：同一毫秒内的多条事件（一批并发的工具调用）
    它分不出先后，而"哪条先发生"正是回放要读的东西。表上有
    ``UNIQUE (conversation_id, seq)``，重复的 seq 会被数据库拒掉。
    """
    id: int | None = None
    """``bigserial`` 主键，入库时由数据库给（与 ``document_stage_events``
    同一种形状——另一张"只追加的事件表"，写法上不发明第二套）。"""
    created_at: datetime | None = None


ARTIFACT_IN_WORKSPACE = "workspace"
"""产物落在一份**真实目录**里（会话挂在某个工作区上）。用户打开自己的项目就看得见。"""

ARTIFACT_IN_OBJECTS = "object"
"""产物落在对象存储里、按会话分前缀（没挂工作区的会话）。

**这是"临时"那一档**：它只许诺"这条会话里有效"，会话删了就连带清掉
（见 ``ArtifactService.discard_for_conversation``）。
"""


@dataclass(slots=True)
class ConversationArtifactRecord:
    """会话产物（v0.26）：Agent 做出来的一份**文件**。

    在这张表出现之前，导出类工具是"直接当一次入库提交"的——于是文件只有一个身份
    （某个知识库里的一份文档），而"这条会话产出了什么"要靠翻文档列表猜。
    实际后果用户撞上过：一个没挂工作区的会话要导出 docx，模型只好**挑一个语义最顺手的
    知识库塞进去**（它塞进了「笔记」），因为那是当时唯一能写的地方。

    两件事由此分开，这张表是分开的证据：

    - ``storage``/``location`` 回答**它现在在哪**（工作区目录 / 对象存储的会话前缀）；
    - ``document_id`` 回答**它有没有进知识库**，进的是哪个库。``None`` = 没进，
      这是默认值——入库是一个**显式动作**（用户点了「存进知识库」，或他明确要求）。
    """

    id: str
    conversation_id: str
    name: str
    """显示名（带扩展名）。用户看到的那个名字。"""
    format: str
    """扩展名小写（``docx`` / ``pdf`` / ``xlsx`` / ``pptx``），界面据此选图标。"""
    size_bytes: int = 0
    storage: str = ARTIFACT_IN_OBJECTS
    location: str = ""
    """真实落点：工作区那份是绝对路径，对象存储那份是 Key。**由服务层解释**——
    存储层不知道工作区是什么，它只存字符串。"""
    workspace_id: str | None = None
    """挂在哪个工作区上（``None`` = 没挂，落在对象存储）。"""
    owner_id: str | None = None
    """归属账号，与知识库/会话同一套口径：``None`` = 管理员或 API Key 通道。"""
    knowledge_base_id: str | None = None
    """进了哪个知识库（``None`` = 还没入）。"""
    document_id: str | None = None
    """入库之后那份文档的 id（``None`` = 还没入）。界面据此给"去看这份文档"的入口。"""
    created_at: datetime | None = None


@dataclass(slots=True)
class UserRecord:
    """使用者。**v10 起是名册与账号的合体**：

    - 只有 ``name`` 的是名册条目（纯归属标注，历史数据）；
    - 有 ``username`` + ``password_hash`` 的才是可登录账号。

    两件事共用一张表而不是分开：账号本来就要回答"这是谁"，
    另起一张表会让"归属标注"与"登录主体"成为两套需要互查的身份。
    """

    id: str
    name: str
    note: str = ""
    username: str | None = None
    """登录名。``None`` = 纯名册条目，不能登录。"""
    password_hash: str | None = None
    """argon2 哈希。慢哈希是口令的底线（`core/security.py` 的 SHA-256 不得用于口令）。"""
    role: UserRole = UserRole.MEMBER
    disabled: bool = False
    """被管理员禁用的账号：登录拒绝，既有 session 在下次校验时失效。"""
    created_at: datetime | None = None
    avatar_key: str = ""
    """头像在对象存储里的 key（v0.29）。空 = 没有头像，界面用名字生成默认头像。

    **只存 key**：头像是一张图，塞进这张表会让每次读账号都拖着一份二进制，
    而账号是每个页面都要读一次的东西（见 ``services/avatars.py``）。
    """


@dataclass(slots=True)
class SessionRecord:
    """一条登录会话。``id`` 是明文 token 的 SHA-256——**明文不落库**（与 API Key 同纪律）。"""

    id: str
    user_id: str
    expires_at: datetime
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(slots=True)
class ShareRecord:
    """知识库分享：owner 把库授给另一个成员（读/写两档）。"""

    kb_id: str
    user_id: str
    permission: SharePermission
    created_at: datetime | None = None


@dataclass(slots=True)
class UsageEventRecord:
    """一次模型调用的用量（调研报告 G7）。

    ``reported`` 记录"供应商到底报没报用量"：OpenAI 兼容协议里 ``usage`` 是可选的，
    很多自建网关不回。**必须与"真的用了 0 token"区分开**——
    否则统计页会把"没报"画成"没用"，那是在撒谎。
    """

    id: str
    kind: str
    """``chat`` / ``embedding`` / ``rerank``。"""
    provider: str = ""
    model_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    items: int = 0
    """这一批处理了几条（向量化按条数，对话为 1）。供应商不报 token 时，
    条数是唯一还能反映"干了多少活"的量。"""
    duration_ms: int = 0
    source: str = "none"
    """这个数字哪来的：``reported``（供应商实测）/ ``estimated``（我们估算）/
    ``none``（供应商没报，也没得估）。

    **三态而不是布尔**：把"自己按字符数估的"和"供应商真报的"混成一类，
    会让统计页显示一个假精度——用户拿它做成本判断就偏了。
    """
    created_at: datetime | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def reported(self) -> bool:
        """是否实测（供应商真的报了）。"""
        return self.source == "reported"


@dataclass(slots=True)
class ModelProviderRecord:
    """模型供应商：一个 base_url + 一把凭据（调研报告 G1）。

    **为什么不复用 ``app_settings`` 里的 ``embedding.base_url`` 那套**：
    那套的口径是"全局各一套凭据"，换模型就得覆盖旧凭据；而成熟产品（6/6）
    都是"供应商可注册多条、模型可注册多个"——同一个 base_url 下往往同时要用
    好几个模型（便宜的做向量化、贵的做对话）。两者不是同一件事。
    """

    id: str
    kind: str
    """``llm`` / ``embedding`` / ``rerank`` / ``parser``：这家供应商提供哪类服务。"""
    name: str
    base_url: str = ""
    api_key: str = ""
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class RegisteredModelRecord:
    """模型目录里的一条（G1）。

    ``capabilities`` 用集合存（落库为 JSON 数组）：一个模型能做什么随供应商与版本而变，
    用固定布尔列会僵化——每加一种能力就要改表。
    """

    id: str
    provider_id: str
    model_id: str
    label: str = ""
    dim: int | None = None
    capabilities: Sequence[str] = field(default_factory=tuple)
    options: dict[str, object] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class SearchHit:
    """检索命中。``score`` 的含义随来源不同（向量距离 / BM25 / RRF 融合分）。"""

    chunk_id: str
    document_id: str
    knowledge_base_id: str
    text: str
    score: float
    source: str
    page: int | None = None
    heading_path: str | None = None
    image_ids: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class VectorMatch:
    """向量召回结果（未融合前的原始命中）。"""

    chunk_id: str
    distance: float


@dataclass(slots=True)
class DocumentStatRow:
    """统计用的一行文档**投影**（外加它的切块数）。

    字段刻意是**裸字符串**而不是枚举：这一层的存在意义就是便宜——
    上万行时把每个枚举值再转一遍是白花的 CPU，而聚合方（驾驶舱）只需要比较字符串。

    `chunks` 由同一条 `LEFT JOIN ... GROUP BY` 带出来：
    原先"先按库取全量文档、再拿全部 id 换一次巨型 IN"要两次往返。
    """

    id: str
    knowledge_base_id: str
    name: str
    stage: str
    source_kind: str
    size_bytes: int
    created_at: datetime | None
    updated_at: datetime | None
    chunks: int = 0


@dataclass(slots=True)
class TaskStatRow:
    """统计用的一行任务**投影**。

    任务表带 `payload`（jsonb）与 `error`（文本），`SELECT *` 读全表在任务多的时候
    又慢又占内存——而统计只要状态、归属文档与两个时间戳。
    """

    state: str
    document_id: str | None
    created_at: datetime | None
    updated_at: datetime | None


# --------------------------------------------------------------------------- 接口


class MetaStore(ABC):
    """元数据仓储：知识库、文档、子文件、chunk、图片、任务、数据源、凭据、设置。

    任务表放进本接口是有意为之——任务状态属于元数据，且与文档状态同库同事务，
    保证"状态推进"与"任务入队"不会出现半写。
    """

    # ---- 知识库 ----
    @abstractmethod
    def create_knowledge_base(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord: ...

    @abstractmethod
    def get_knowledge_base(self, kb_id: str) -> KnowledgeBaseRecord | None: ...

    @abstractmethod
    def list_knowledge_bases(self) -> list[KnowledgeBaseRecord]: ...

    @abstractmethod
    def document_stats_by_kbs(self) -> dict[str, tuple[int, datetime | None]]:
        """每个知识库的 ``(文档数, 最近更新时间)``，一次 ``GROUP BY`` 拿到。

        **为什么必须是一个批量方法**：知识库列表与侧栏都要显示"每个库多少篇"，
        逐个库调 ``list_documents`` 就是 N 次查询（而且是取全量文档再在 Python 里数）。
        这里下推到 SQL，只回一行一个库的聚合结果。
        """

    @abstractmethod
    def list_document_stats(self, kb_ids: Sequence[str] | None = None) -> list[DocumentStatRow]:
        """统计投影：每个文档一行（**带它的切块数**），一条查询拿全。

        **为什么单独开一个方法**：驾驶舱原先按库逐个调 ``list_documents()``
        （K 次查询，且 `SELECT *` 会把每篇的摘要一起搬回来），再拿全量 id 去
        ``count_chunks_by_documents()`` 换一次巨型 `IN`。这里一条
        ``LEFT JOIN … GROUP BY`` 同时给出聚合要的那几列与切块数，
        大字段（`summary`、`content_hash`）一个都不取。

        ``kb_ids`` 为 ``None`` 表示全部库；空序列表示"没有可见的库"（回空表，
        而不是回全部——调用方是成员视角时那两者差别就是越权）。
        顺序与 ``list_documents`` 一致（``created_at DESC, id DESC``），
        这样按插入序遍历聚合出来的结果与改动前逐字一致。
        """

    @abstractmethod
    def list_task_stats(self, document_ids: Sequence[str] | None = None) -> list[TaskStatRow]:
        """统计投影：每个任务一行（状态 + 归属文档 + 两个时间戳）。

        ``document_ids`` 为 ``None`` 表示不筛；空序列表示"没有可见的文档"→ 回空表
        （成员视角的驾驶舱不能统计别人的任务，见 ``services/stats.py`` 的说明）。
        """

    @abstractmethod
    def rename_knowledge_base(self, kb_id: str, name: str) -> None:
        """改显示名。嵌入模型与切分参数都不受影响——名字只是标签。"""

    @abstractmethod
    def set_knowledge_base_chunking(self, kb_id: str, size: int, overlap: int) -> None:
        """改切分参数（v17）。**只影响之后摄入的文档**，已切好的块不动。"""

    @abstractmethod
    def set_knowledge_base_description(self, kb_id: str, description: str) -> None:
        """改库简介（v15）。与改名同性质：只是标签，不影响检索。"""

    @abstractmethod
    @abstractmethod
    def set_knowledge_base_suggested(
        self,
        kb_id: str,
        *,
        enabled: bool,
        count: int,
        model_pk: str | None,
        prompt: str,
    ) -> None:
        """改这个库的推荐问题设置（v19）。

        四个值一起写而不是逐个可空：它们是**同一组设置**，界面也是一屏提交，
        逐个判空只会多出"传了 null 是清除还是不改"的歧义。
        """

    @abstractmethod
    def set_knowledge_base_wiki(self, kb_id: str, *, enabled: bool) -> None:
        """改这个库的形态：要不要生成 Wiki 页面（v24）。

        只动开关，**不碰已有页面**——关掉只是"不再生成/不再展示"，
        页面留着（用户可能只是暂时不想看；真要清空有单独的删除接口）。
        """

    @abstractmethod
    def set_knowledge_base_prompt(self, kb_id: str, *, prompt: str) -> None:
        """改这个库的**库级提示词**（v0.19）。只碰 `system_prompt` 一列。"""

    @abstractmethod
    def update_knowledge_base_embedding(
        self, kb_id: str, *, model_id: str, dim: int, base_url: str | None
    ) -> None: ...

    @abstractmethod
    def delete_knowledge_base(self, kb_id: str) -> None: ...

    @abstractmethod
    def storage_stats(self) -> dict:
        """数据库物理占用与空闲页（维护页展示）。"""

    @abstractmethod
    def vacuum(self) -> None:
        """回收空闲页。**必须在事务之外执行**，只能由显式的用户动作触发。"""

    @abstractmethod
    def count_kb_chunks(self, kb_id: str) -> int: ...

    # ---- Wiki（v24）----
    @abstractmethod
    def replace_wiki_pages(
        self,
        kb_id: str,
        pages: Sequence[WikiPageRecord],
        sources: Sequence[WikiSourceRecord],
    ) -> None:
        """整体替换一个库的 Wiki 页面与出处（同一事务内先删后插）。

        **整库重建而不是逐页 upsert**：本版生成是"一次把全库重写一遍"，
        逐页 upsert 会留下上一版多出来的、已经不存在的页面（改名、合并之后
        它们就是无主页面）。先删后插最简单，也保证页树与正文同一次生成的结果一致。
        """

    @abstractmethod
    def list_wiki_pages(self, kb_id: str) -> list[WikiPageRecord]:
        """按 ``level, ord`` 列出页面（不含 ``content_md`` 之外的东西——它本来就带着）。

        列表接口只回目录信息时由服务层裁字段，存储层不做两种投影：
        Wiki 页面数量小（十几页），多读一列正文不值得多一个方法。
        """

    @abstractmethod
    def get_wiki_page(self, page_id: str) -> WikiPageRecord | None: ...

    @abstractmethod
    def list_wiki_sources(self, page_id: str) -> list[WikiSourceRecord]:
        """一页的出处，按 ``rank`` 升序（rank 就是正文里的 ``[n]``）。"""

    @abstractmethod
    def wiki_stats(self, kb_id: str) -> tuple[int, datetime | None]:
        """``(页面数, 最近生成时间)``。列表卡片与 Wiki 页头部都要显示。"""

    # ---- 文档 ----
    @abstractmethod
    def create_document(self, record: DocumentRecord) -> DocumentRecord: ...

    @abstractmethod
    def get_document(self, document_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    def get_documents_by_ids(self, document_ids: Sequence[str]) -> dict[str, DocumentRecord]:
        """按 id 批量取文档（``{id: record}``，不存在的 id 不会出现在结果里）。

        给"手上已经有一批 id、只差记录"的场景用（如按知识库过滤任务列表）——
        逐个 ``get_document`` 就是 N+1。
        """

    @abstractmethod
    def get_document_by_hash(self, kb_id: str, content_hash: str) -> DocumentRecord | None: ...

    @abstractmethod
    def list_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        """列某个库的文档。

        - ``root_only=True``：只看未归档的（``folder_id IS NULL``）；
        - ``folder_id`` 给了：只看这个目录里的；
        - ``q``：文件名含该子串（大小写不敏感，``%``/``_`` 按字面匹配）；
        - ``stage`` / ``source_kind``：精确值过滤；
        - ``limit`` / ``offset``：分页。``limit=None``（默认）不分页——
          服务内部的调用点（统计、批处理、生命周期）本来就要全量，不能被分页截断；
        - 都不给：整个库（默认，保持既有调用点行为不变）。

        过滤**在 SQL 里做而不是取回内存再筛**：一个库上万篇时，
        "把全部读出来再过滤"会把列表接口的耗时和内存随库大小一起放大。
        """

    @abstractmethod
    def count_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
    ) -> int:
        """与 ``list_documents`` 同一套过滤条件下有多少篇（``COUNT(*)`` 下推）。

        分页界面要显示"共 N 篇 / 第 X 页"，而 ``len(list_documents(..., limit=...))``
        只能数到当前页，越翻越错。
        """

    @abstractmethod
    def update_document_stage(
        self, document_id: str, stage: DocumentStage, *, error: str | None = None
    ) -> None: ...

    @abstractmethod
    def replace_document_content(
        self,
        document_id: str,
        *,
        content_hash: str,
        name: str,
        size_bytes: int,
        mime_type: str | None,
    ) -> None:
        """用新内容**原地替换**一份文档的元数据（v0.12）。

        与 ``create_document`` 的分工：那个是"新文档"，这个是"同一份文档的内容变了"。

        **为什么要有它**：判重是按内容哈希的，所以"编辑后重新入库"会**新建一份文档**
        而不是更新既有那份——旧内容还留在库里，同一份东西出现两版。
        要真正"更新"，必须有原地替换。

        调用方（``IngestService.replace``）负责：写新的原文对象、清掉旧切块与向量、
        把阶段推回 ``uploaded``（内容变了就是重新走一遍流水线）。
        这里只改元数据——存储层不编排流水线。
        """

    @abstractmethod
    def list_document_stage_events(self, document_id: str) -> list[DocumentStageEventRecord]:
        """某文档的阶段进入事件，按发生顺序。进度时间线读它。"""

    @abstractmethod
    def list_document_stage_events_for_documents(
        self, document_ids: Sequence[str]
    ) -> dict[str, list[DocumentStageEventRecord]]:
        """一批文档的阶段事件（一条 SQL 取回，按文档分组）。

        列表页每行都要画进度条，逐篇调 ``list_document_stage_events`` 就是 N+1 次
        查询——一页 20 篇就是 20 次往返。缺失的文档不出现在结果里（与
        ``get_documents_by_ids`` 同一约定：调用方 `.get(id, [])`）。
        """

    @abstractmethod
    def active_tasks_by_documents(self, document_ids: Sequence[str]) -> dict[str, TaskRecord]:
        """这些文档各自**还没结束**的任务（pending / running），一次取回。

        "停滞"判据要用它：租约过期 = 没有 worker 在续约（见
        ``ObservabilityService.assess``）。不给这个批量入口的话，列表页要么
        逐篇查任务，要么把整张任务表读出来再筛——前者是 N+1，后者随任务总数放大。

        一个文档同时只会有一条未结束的任务（入队是幂等的），所以
        ``{document_id: 任务}`` 这个形状是安全的。
        """

    @abstractmethod
    def update_document_page_count(self, document_id: str, page_count: int | None) -> None:
        """页数是**解析产物**而不是阶段推进，所以有独立入口。

        ``None`` 或 ``<= 0`` 一律忽略、保持 NULL：写 0 会让界面显示"0 页"，
        而 NULL 渲染成"—"，后者才是诚实的（"没测出来"不等于"有 0 页"）。
        """

    @abstractmethod
    def list_documents_without_summary(self, *, limit: int) -> list[DocumentRecord]:
        """**已索引、但还没有摘要**的文档，按入库时间从早到晚（最多 ``limit`` 篇）。

        给"补漏"用：摘要是 v25 才有的机制，已有文档不会重新入库，
        不补它们就永远享受不到"省 token"这件事（而这正是这个机制的理由）。
        只取已索引的：没跑完的文档块还没定稿，摘要写出来就得重写。
        """

    @abstractmethod
    def update_document_summary(self, document_id: str, summary: str) -> None:
        """写入文档摘要（空串 = 清除）。

        **不改阶段、不动 updated_at 之外的任何东西**：摘要是内容层的补充，
        与流水线阶段无关（它不参与阶段机，失败也不该让文档 failed）。
        """

    @abstractmethod
    def mark_document_split(self, document_id: str, is_split: bool = True) -> None:
        """标记"这个文档被切成子文件了"。

        界面据此把它渲染成**可展开的父行**（架构 §4.2："UI 显示为单个文件，
        点击展开子文件树"）。没有这个标记，子文件树就永远不会出现——
        用户只能看到一个文档，却不知道它内部被切成了 5 段、其中一段失败了。
        """

    @abstractmethod
    def delete_document(self, document_id: str) -> None: ...

    @abstractmethod
    def rename_document(self, document_id: str, name: str) -> None:
        """改文件名。**只是显示名**：不改 ``content_hash``、不重跑解析，
        下载时用的也是这个名字（``Content-Disposition`` 取的就是它）。
        """

    @abstractmethod
    def set_document_disabled(self, document_id: str, disabled: bool) -> None:
        """停用/恢复一个文档。**只动标记**：不删切块与向量，检索侧按标记过滤，
        恢复零成本（与 chunks.disabled 同一套做法）。"""

    @abstractmethod
    def any_disabled_documents(self, kb_ids: Sequence[str]) -> bool:
        """这些库里是否存在停用的文档。

        供检索的向量通道决定要不要超采：没有停用文档时按原深度召回（不浪费），
        有才加倍——KNN 没法按文档过滤，超采是唯一不伤召回的补偿。"""

    # ---- 目录（v13）----
    @abstractmethod
    def create_folder(self, record: FolderRecord) -> FolderRecord: ...

    @abstractmethod
    def get_folder(self, folder_id: str) -> FolderRecord | None: ...

    @abstractmethod
    def list_folders(self, kb_id: str) -> list[FolderRecord]:
        """按名字排序——目录是人自己起的名字，按名字找比按创建时间找自然。"""

    @abstractmethod
    def rename_folder(self, folder_id: str, name: str) -> None: ...

    @abstractmethod
    def delete_folder(self, folder_id: str) -> None: ...

    @abstractmethod
    def count_documents_by_folders(self, kb_id: str) -> dict[str, int]:
        """批量取每个目录的文档数（``GROUP BY``）：逐个目录查一次就是 N+1。"""

    @abstractmethod
    def set_document_folder(self, document_id: str, folder_id: str | None) -> None:
        """把文档移进目录；``None`` = 移回根。"""

    # ---- 笔记（v20）----
    @abstractmethod
    def create_note(self, record: NoteRecord) -> NoteRecord:
        """建笔记并写入标签。"""

    @abstractmethod
    def get_note(self, note_id: str) -> NoteRecord | None:
        """按 id 取一条（含标签）。"""

    @abstractmethod
    def list_notes(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        tag: str | None = None,
        folder_id: str | None = None,
        unfiled: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[NoteRecord]:
        """按置顶 + 更新时间倒序列出；``query`` 做标题/正文的子串匹配。

        ``user_id=None`` 表示"无归属"（管理员/API Key 建的笔记），
        用 ``IS`` 而不是 ``=`` 比较，才能同时匹配 NULL 与具体值。

        ``folder_id`` / ``unfiled`` 是**同一个轴上的两种取法**（与文档列表的
        ``folder_id`` / ``root_only`` 同款）：给具体 id 就是"这个文件夹里的"，
        ``unfiled=True`` 就是"未归档的"。不传两者 = 不按文件夹过滤（全部）。
        写成两个参数而不是一个 `'unfiled'` 哨兵，是因为存储层不该认识界面上的字符串。
        """

    @abstractmethod
    def count_notes(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        tag: str | None = None,
        folder_id: str | None = None,
        unfiled: bool = False,
    ) -> int:
        """与 ``list_notes`` 同一套过滤条件的总数（分页用）。"""

    @abstractmethod
    def update_note(
        self,
        note_id: str,
        *,
        title: str,
        content_md: str,
        pinned: bool,
        updated_at: datetime,
        tags: Sequence[str] | None = None,
    ) -> None:
        """整条覆盖更新；``tags=None`` 表示"不动标签"（只改正文时不必先读标签）。"""

    @abstractmethod
    def delete_note(self, note_id: str) -> None:
        """删笔记并清掉它的标签。"""

    @abstractmethod
    def attach_note_document(self, note_id: str, *, kb_id: str, doc_id: str) -> None:
        """记下"这条笔记已入库到哪个文档"，供检索命中时跳回笔记。"""

    @abstractmethod
    def list_note_tags(self, *, user_id: str | None) -> list[tuple[str, int]]:
        """该用户用过的标签与条数（按条数、名字排序）。"""

    # ---- 笔记文件夹（v14）----

    @abstractmethod
    def create_note_folder(self, record: NoteFolderRecord) -> NoteFolderRecord:
        """建一个文件夹（``parent_id`` 为空即根级）。"""

    @abstractmethod
    def get_note_folder(self, folder_id: str) -> NoteFolderRecord | None: ...

    @abstractmethod
    def list_note_folders(self, *, user_id: str | None) -> list[NoteFolderRecord]:
        """按名字（不区分大小写）整份列出——层级由调用方按 ``parent_id`` 自己接。

        **刻意不在存储层拼树**：树是界面的事，"哪些节点展开着"更是；
        存储只回答"有哪些文件夹、各自的父是谁"。
        """

    @abstractmethod
    def rename_note_folder(self, folder_id: str, name: str) -> None: ...

    @abstractmethod
    def set_note_folder_parent(self, folder_id: str, parent_id: str | None) -> None:
        """移动文件夹；``None`` = 移到根级。成环由业务层先挡住（这里不做图遍历）。"""

    @abstractmethod
    def delete_note_folder(self, folder_id: str) -> None:
        """删文件夹。**子文件夹由外键级联删掉、里面的笔记回到未归档**——
        两条语义都写在表定义上（见 schema.py 迁移 v14 的注释），这里只发一条 DELETE。
        """

    @abstractmethod
    def count_notes_by_folder(self, *, user_id: str | None) -> dict[str | None, int]:
        """``{文件夹 id（``None`` = 未归档）: 笔记数}``。

        **一次 GROUP BY 取全**：界面上每个文件夹都要显示条数，逐个查就是 N+1；
        三个数字（每个文件夹、未归档、总数）都由这一次聚合得出，总数 = 各项之和。
        """

    @abstractmethod
    def set_note_folder(self, note_id: str, folder_id: str | None) -> None:
        """把笔记移进文件夹；``None`` = 移回未归档。**只动归属，不碰正文与标签**。"""

    # ---- 子文件 ----
    @abstractmethod
    def create_document_parts(self, records: Sequence[DocumentPartRecord]) -> None:
        """建立子文件记录。**必须可重入**：摄入失败重跑时会用同一批 id 再来一次。"""

    @abstractmethod
    def list_document_parts(self, document_id: str) -> list[DocumentPartRecord]: ...

    @abstractmethod
    def delete_document_parts(self, document_id: str) -> None:
        """重跑切分前清一遍：段数可能变少，``INSERT OR REPLACE`` 清不掉多出来的高序号段。"""

    @abstractmethod
    def update_part_stage(
        self, part_id: str, stage: DocumentStage, *, error: str | None = None
    ) -> None: ...

    # ---- chunk ----
    @abstractmethod
    def replace_chunks(self, document_id: str, chunks: Sequence[ChunkRecord]) -> None: ...

    @abstractmethod
    def iter_chunks(self, document_id: str, *, limit: int | None = None) -> Iterable[ChunkRecord]:
        """按 ``ordinal`` 升序取文档的切块。

        ``limit`` 用于"只看前几块"的预览场景（文档详情页）：不设上限时，
        一个上万块的文档会把整份正文读进内存，而这只是为了显示开头几段。
        """

    @abstractmethod
    def list_chunks_by_heading(self, document_id: str, heading_path: str) -> list[ChunkRecord]:
        """某文档里**属于同一小节**（``heading_path`` 相同）的切块，按 ``ordinal`` 升序。

        **为什么需要它**：回答里的"小块检索、大块阅读"要把命中的那一块补成整段小节。
        原先的做法是把**整篇文档**的切块读进内存，再在 Python 里按小节名筛——
        一篇上千块时，为了一段小节补全读了一千行。
        小节名本身就是现成的过滤条件，下推到 SQL 之后只回这一节。

        ``heading_path`` 为空（整篇没有标题的纯文本）时语义上没有"小节"，
        调用方应当先判断（见 ``services/chat.py::_SectionReader``）。
        """

    @abstractmethod
    def get_chunks(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]:
        """按 ID 批量取回 chunk。

        检索时向量只给得出 chunk_id，正文/页码/图片锚点都得回表取——
        逐个查会变成 N 次查询，所以接口层就要求批量。
        """

    @abstractmethod
    def count_chunks(self, document_id: str) -> int: ...

    @abstractmethod
    def sample_chunks(
        self, kb_ids: Sequence[str], *, limit: int, with_questions_only: bool = False
    ) -> list[ChunkRecord]:
        """从若干知识库里**随机抽**若干切块（跳过人工禁用的）。

        用途是"给示例问题生成提供一点语料"，不是检索：不需要相关性排序，
        只要覆盖面够广——所以按库随机，而不是取每个文档的前几块（那样每个库
        都只看得到第一份文档的开头）。空 ``kb_ids`` 返回空列表。

        ``with_questions_only=True`` 只抽**已经出过题**的块。读端（对话页空状态）
        必须用它：库里绝大多数块没有题，在全库随机抽会一次次抽到空块，
        于是"有上百条问题却一条都显示不出来"。
        """

    @abstractmethod
    def count_chunks_by_documents(self, document_ids: Sequence[str]) -> dict[str, int]:
        """批量查切块数：文档列表页要显示每个文档有多少块。

        逐个 ``count_chunks`` 会变成 N+1（1000 个文档 = 1000 次查询），
        所以接口层直接要求批量。缺席的文档 ID 在返回里补 0。
        """

    @abstractmethod
    def question_stats_by_documents(
        self, document_ids: Sequence[str]
    ) -> dict[str, tuple[int, int]]:
        """批量查每个文档的出题情况，返回 ``{document_id: (有题块数, 问题总数)}``。

        文档列表要显示"这份有没有出题、出了多少"（v24）。同样是 N+1 问题，
        一条 ``GROUP BY`` 拿全；缺席的文档补 ``(0, 0)``。
        """

    @abstractmethod
    def active_question_documents(self, document_ids: Sequence[str]) -> set[str]:
        """这些文档里，哪几个还有排队/在跑的出题任务。

        列表用它显示"生成中…"，也用它决定还要不要继续轮询——出题不改变文档阶段，
        光看 ``stage`` 是看不出它在跑的（前端 `needsPolling` 就靠这个字段）。
        """

    # ---- 切块人工干预（§G3）----
    @abstractmethod
    def update_chunk(self, record: ChunkRecord) -> None:
        """就地更新一个块（正文、标题路径、页码）。**不改 chunk_id**。"""
        ...

    @abstractmethod
    def set_chunk_disabled(self, chunk_id: str, *, disabled: bool) -> None: ...

    @abstractmethod
    def delete_chunk(self, chunk_id: str) -> None:
        """删除单个块（含图片关联）。全文索引与向量由调用方一并清理。"""
        ...

    # ---- 图片 ----
    @abstractmethod
    def add_images(self, records: Sequence[ImageRecord]) -> None: ...

    @abstractmethod
    def list_images(self, document_id: str) -> list[ImageRecord]: ...

    # ---- 解析产物 ----
    @abstractmethod
    def save_parse_result(self, record: ParseResultRecord) -> None: ...

    @abstractmethod
    def get_parse_result(self, document_id: str) -> ParseResultRecord | None: ...

    @abstractmethod
    def parser_page_usage(self, parser_name: str, *, since: datetime) -> tuple[int, int]:
        """某个云端解析器自 ``since`` 起消耗的 ``(页数, 调用次数)``。

        云端渠道有**每日页数额度**（MinerU 1000 页/天），而额度用尽不是报错、
        是**降级排队**——用户只看到"卡住不动"。负载面板把已用量显示出来，
        这种"看起来卡住"才有可核查的解释（见 §12.115）。

        页数**按子文件页范围累加**（切分后每段单独送云端，一次调用只算它那几页），
        只有没切分的文档才用 ``documents.page_count``：否则一篇 1500 页切 8 段的
        文档会被记成 8 × 1500 页，额度数字立刻失真到没法用。
        """

    # ---- 任务 ----
    @abstractmethod
    def enqueue_task(self, record: TaskRecord) -> TaskRecord: ...

    @abstractmethod
    def claim_task(self, *, owner: str, lease_seconds: int) -> TaskRecord | None: ...

    @abstractmethod
    def heartbeat_task(self, task_id: str, *, owner: str, lease_seconds: int) -> bool: ...

    @abstractmethod
    def finish_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        owner: str,
        error: str | None = None,
    ) -> bool:
        """落终态，返回是否写成功。

        必须带 ``owner`` 做条件更新：租约被回收后，原消费者仍然可能跑完并回来写终态，
        无条件覆盖会把**新消费者正在跑的任务**改成成功，或者凭空清掉它的租约。
        返回 False 表示"这份任务已经不是你的了"，调用方应记日志而不是当成功。
        """

    @abstractmethod
    def cancel_tasks_for_document(self, document_id: str) -> int:
        """把这个文档还没结束（pending/running）的任务标成 canceled，返回条数。

        与 ``finish_task`` 不同，它**不需要 owner**：这是管理动作，由看到"用户点了取消"
        的 API 进程执行，而不是任务的持有者。它会一并清掉租约——租约还在，worker
        的心跳就还会续，任务也就还"活着"。
        """

    @abstractmethod
    def cancel_tasks(self, task_ids: Sequence[str]) -> int:
        """按 id 把还没结束的任务标成 canceled，返回实际改动的条数。

        给"任务中心里取消排队中的任务"用：用户看到几十条 pending 堵在队列里，
        得有一个地方把它们撤下来，而不是只能逐篇去取消文档。
        同样清租约、同样不需要 owner。已结束（succeeded/failed/canceled）的任务
        不在改动范围内——它们的 rowcount 自然是 0，调用方据此报"任务已结束"。
        """

    @abstractmethod
    def reschedule_task(
        self, task_id: str, *, owner: str, next_run_at: datetime, error: str | None
    ) -> bool:
        """把任务退回待执行并设定下次可领时间，返回是否写成功（同 ``finish_task``）。

        指数退避靠它实现：``finish_task`` 只能落终态，而重试要求任务**回到队列**，
        同时释放租约、记录本次失败原因。
        """

    @abstractmethod
    def reclaim_expired_tasks(self, *, now: datetime | None = None) -> int: ...

    @abstractmethod
    def list_tasks(self, state: TaskState | None = None) -> list[TaskRecord]: ...

    @abstractmethod
    def task_counts(self, *, now: datetime, overdue_before: datetime) -> TaskCounts:
        """队列概览：**全部计数下推到 SQL**，一行结果，与任务总量无关。

        负载面板每 2 秒问一次（§12.115），而任务表只增不减。原先那条路是
        "把整张表读出来、构造每条记录、再在 Python 里数"——代价随任务总量线性涨，
        而这里要的只是几个数。

        ``now`` 与 ``overdue_before`` 都由调用方给：**时钟归服务层**（它才好注入、
        好测），阈值也只该有一个出处（``services/observability.OVERDUE_AFTER``）——
        在这里再写一个 10 分钟就是两套判据。
        """

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRecord | None:
        """按主键取单个任务：不要为了找一条而拉全表。"""

    # ---- 数据源 / 凭据 / webhook ----
    @abstractmethod
    def create_data_source(self, record: DataSourceRecord) -> DataSourceRecord: ...

    @abstractmethod
    def list_data_sources(self, kb_id: str) -> list[DataSourceRecord]: ...

    @abstractmethod
    def get_data_source(self, source_id: str) -> DataSourceRecord | None: ...

    @abstractmethod
    def list_all_data_sources(self) -> list[DataSourceRecord]:
        """全部数据源（不分库）。

        定时拉取要遍历所有启用的源，按库找就得先把库读出来再逐个查——
        那是不必要的 N+1。
        """
        ...

    @abstractmethod
    def update_data_source(self, record: DataSourceRecord) -> None: ...

    @abstractmethod
    def delete_data_source(self, source_id: str) -> None: ...

    @abstractmethod
    def mark_data_source_pulled(self, source_id: str, *, etag: str | None) -> None:
        """记下这次拉取的时间与 ETag。

        **ETag 是增量拉取的关键**：下次带上 ``If-None-Match``，没变就返回 304，
        连正文都不用下载。省的不只是流量——解析与向量化才是大头。
        """
        ...

    @abstractmethod
    def create_api_key(self, record: ApiKeyRecord) -> ApiKeyRecord: ...

    @abstractmethod
    def get_api_key_by_hash(self, key_hash: str) -> ApiKeyRecord | None: ...

    @abstractmethod
    def list_api_keys(self) -> list[ApiKeyRecord]: ...

    @abstractmethod
    def delete_api_key(self, key_id: str) -> None: ...

    @abstractmethod
    def touch_api_key(self, key_id: str, *, used_at: datetime | None = None) -> None:
        """记录一次使用时间（只更新 ``last_used_at``，不动权限与范围）。"""
        ...

    @abstractmethod
    def create_webhook(self, record: WebhookRecord) -> WebhookRecord: ...

    @abstractmethod
    def list_webhooks(self) -> list[WebhookRecord]: ...

    @abstractmethod
    def get_webhook(self, webhook_id: str) -> WebhookRecord | None: ...

    @abstractmethod
    def set_webhook_enabled(self, webhook_id: str, enabled: bool) -> WebhookRecord | None:
        """只切开关，**不提供改地址**：换投递目标应当是一次有意识的新建。"""

    @abstractmethod
    def delete_webhook(self, webhook_id: str) -> None: ...

    # ---- 幂等键（架构 §3.2：上传类接口带幂等键，防重试造成重复入库）----
    @abstractmethod
    def create_idempotency_key(self, record: IdempotencyRecord) -> IdempotencyRecord:
        """占住一个幂等键。**键已存在时必须抛 ConflictError**，让调用方据此走重放。"""
        ...

    @abstractmethod
    def get_idempotency_key(self, key: str) -> IdempotencyRecord | None: ...

    @abstractmethod
    def save_idempotent_response(self, key: str, response: dict[str, object]) -> None:
        """把首次执行的结果挂到键上，后续重放直接回它。"""
        ...

    @abstractmethod
    def purge_expired_idempotency_keys(self, *, before: datetime) -> int:
        """清掉 ``created_at < before`` 的键，返回删除条数。

        没有这一步，幂等键表会随每次上传无限增长。客户端重试窗口是分钟级，
        所以保留一天就远远够用。
        """
        ...

    @abstractmethod
    def purge_finished_tasks(self, *, before: datetime) -> int:
        """清掉 ``updated_at < before`` 且**已经结束**的任务，返回删除条数。

        任务表只增不减：每次上传都会留下 probe/parse/chunk/embed 几条，
        出题与 Wiki 再各加一条。不清的话列表接口、队列概览、逐条健康判定
        全都会随历史缓慢变重——而"三个月前那次成功"没有任何人还会去看。
        **在跑/排队的一律不动**（未结束的任务是状态，不是历史）。
        """

    @abstractmethod
    def purge_stage_events(self, *, before: datetime) -> int:
        """清掉 ``entered_at < before`` 的阶段事件，返回删除条数。

        时间线的数据源只追加（重试、重新摄入都再加一条）。清理的代价是老文档
        在抽屉里只剩"当前这一步"的耗时——这是可接受的：**正在跑的东西才需要
        时间线**，几个月前跑完的文档不需要逐环节回放。
        """

    @abstractmethod
    def release_idempotency_key(self, key: str) -> None:
        """放掉一个还没产生结果的键。

        业务执行中途失败时用：键留着但 ``response`` 为空，客户端重试只会拿到
        "正在处理中"——而实际上什么都没在处理了。
        """
        ...

    # ---- 对话留存（架构 §3 的对话层；M6 后续）----
    @abstractmethod
    def create_conversation(self, record: ConversationRecord) -> ConversationRecord: ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...

    @abstractmethod
    def list_conversations(
        self, *, limit: int | None = None, q: str | None = None
    ) -> list[ConversationRecord]:
        """**置顶优先，其次按最近更新倒序**；``q`` 按标题做包含匹配。"""
        ...

    @abstractmethod
    def set_conversation_archived(self, conversation_id: str, archived: bool) -> None:
        """归档 / 取消归档。**不推 ``updated_at``**：与置顶、改名同理——
        归档是一次整理动作，不该把会话顶到"最近活动"的最前面。"""
        ...

    @abstractmethod
    def last_assistant_previews(self, conversation_ids: Sequence[str]) -> dict[str, str]:
        """``会话 id → 最后一条回答的原文``（历史会话面板的两行预览用它）。

        **一次查完，不逐个查**：列表最多几十条，逐个查就是几十次往返。
        实现上是一条 ``DISTINCT ON``，取每个会话最新的那条 assistant 消息。
        """
        ...

    @abstractmethod
    def set_conversation_workspace(self, conversation_id: str, workspace_id: str | None) -> None:
        """把会话挂到某个工作区，或（``None``）退回未归档。

        **不推 ``updated_at``**：归类是一次整理动作，和改名/置顶同理——推了的话
        "把五条会话整理进项目"会让它们按整理时间重排，而用户想按对话发生的时间找。
        """
        ...

    # ---- 工作区（v0.15；见 docs/设计/Agent-工作区与能力层设计-v0.1.md §3）----
    @abstractmethod
    def create_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord:
        """落一行工作区。``record.device_id`` / ``device_name`` 原样存下
        （``None`` = 服务器端，见 :class:`WorkspaceRecord`）。"""
        ...

    @abstractmethod
    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None: ...

    @abstractmethod
    def list_workspaces(
        self, *, device_id: str | None = None, any_device: bool = False
    ) -> list[WorkspaceRecord]:
        """按最近更新倒序。归属过滤在服务层做（存储层不认识调用者身份）。

        **设备过滤是三态**（v0.59）：

        - ``any_device=True`` —— 不过滤，跨设备全都要（管理员的跨机清理视图）；
        - 否则只列 ``device_id`` 与参数**相等**的那些，其中 ``None`` 这一档是
          **服务器端**（网页版/直连 API 看到的那些），不是"没有归属"。
        """
        ...

    @abstractmethod
    def update_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord: ...

    @abstractmethod
    def set_workspace_archived(self, workspace_id: str, archived: bool) -> None:
        """归档 / 取消归档一个工作区（v0.55）。**不推 ``updated_at``**：与会话那条同理
        （归档是一次整理动作，不该把它顶到"最近更新"的最前面）。"""
        ...

    @abstractmethod
    def delete_workspace(self, workspace_id: str) -> None:
        """删工作区。**里面的会话退回未归档**（外键是 ON DELETE SET NULL），
        不是跟着一起删——会话里有用户问过的内容，误删不可恢复。"""
        ...

    # ---- 定时任务（v0.33；见 docs/设计/Agent-工作区与能力层设计-v0.1.md §6.6）----
    @abstractmethod
    def create_scheduled_task(self, record: ScheduledTaskRecord) -> ScheduledTaskRecord: ...

    @abstractmethod
    def get_scheduled_task(self, scheduled_id: str) -> ScheduledTaskRecord | None: ...

    @abstractmethod
    def list_scheduled_tasks(self) -> list[ScheduledTaskRecord]:
        """按"下次该跑的时间"排序（``None`` 排最后），其次按创建时间倒序。

        归属过滤在服务层做（存储层不认识调用者身份），与工作区 / MCP 服务同一口径。
        """
        ...

    @abstractmethod
    def update_scheduled_task(self, record: ScheduledTaskRecord) -> ScheduledTaskRecord: ...

    @abstractmethod
    def delete_scheduled_task(self, scheduled_id: str) -> None: ...

    @abstractmethod
    def due_scheduled_tasks(self, *, now: datetime, limit: int = 10) -> list[ScheduledTaskRecord]:
        """到点该跑的那些（``enabled`` 且 ``next_run_at <= now``），按时间正序。

        **只查不算**：真正"认领"要过 :meth:`arm_scheduled_task`——
        查与认领分成两步是有意的，认领那一步是带条件的 UPDATE（见它的说明）。
        """
        ...

    @abstractmethod
    def arm_scheduled_task(
        self,
        scheduled_id: str,
        *,
        expected_next_run_at: datetime | None,
        next_run_at: datetime | None,
        enabled: bool,
    ) -> bool:
        """认领一次运行：**把下次时间推到下一回**，条件是目前还停在 ``expected_next_run_at``。

        返回 ``False`` = 有人先认领了（另一个 worker 或另一次扫描），这次别再跑。

        为什么要 CAS 而不是"先查后写"：多个 worker 会同时扫到同一条到点的任务，
        而"跑两次"的代价不是重复一次查询——它会重复**一次完整的问答与工具调用**
        （真花钱），并在会话里留下两条一模一样的记录。判据只能落在一条
        ``UPDATE ... WHERE next_run_at = 期望值`` 上（与任务队列的
        ``FOR UPDATE SKIP LOCKED`` 同一个思路：让数据库来裁决谁先）。
        """
        ...

    @abstractmethod
    def finish_scheduled_run(
        self,
        scheduled_id: str,
        *,
        status: str,
        error: str | None,
        last_run_at: datetime,
        conversation_id: str | None = None,
    ) -> None:
        """记一次运行的结果（状态 / 错误 / 时间，首次运行时把会话 id 落下来）。"""
        ...

    # ---- MCP 服务（v0.15）----
    @abstractmethod
    def create_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord: ...

    @abstractmethod
    def get_mcp_server(self, server_id: str) -> MCPServerRecord | None: ...

    @abstractmethod
    def list_mcp_servers(self) -> list[MCPServerRecord]:
        """按最近更新倒序。归属过滤在服务层做（存储层不认识调用者身份）。"""
        ...

    @abstractmethod
    def update_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord: ...

    @abstractmethod
    def delete_mcp_server(self, server_id: str) -> None: ...

    @abstractmethod
    def count_workspace_conversations(self, workspace_id: str) -> int:
        """这个工作区下有多少会话。侧栏每个工作区后面那个计数用它。"""
        ...

    @abstractmethod
    def rename_conversation(self, conversation_id: str, title: str) -> None: ...

    @abstractmethod
    def set_conversation_pinned(self, conversation_id: str, pinned: bool) -> None:
        """置顶/取消置顶。**不推 ``updated_at``**：置顶是一次整理动作，
        和改名一样不该把会话顶到"最近活动"的最前面（置顶本来就排最前了）。"""
        ...

    @abstractmethod
    def delete_chat_messages(self, message_ids: Sequence[str]) -> int:
        """按 id 删除消息，返回删除条数（「重新生成」回退一轮用）。"""
        ...

    @abstractmethod
    def set_conversation_model(self, conversation_id: str, model_pk: str | None) -> None:
        """记录该会话选用的对话模型（``None`` = 回到全局默认）。

        **不推 ``updated_at``**，与改名同理：切一次模型不该把会话顶到"最近活动"的最前面。
        """
        ...

    @abstractmethod
    def set_conversation_thinking(
        self, conversation_id: str, thinking: bool | None, effort: str | None
    ) -> None:
        """记录该会话的思考偏好（``None`` = 回到全局默认）。同样不推 ``updated_at``。"""
        ...

    @abstractmethod
    def touch_conversation(self, conversation_id: str) -> None:
        """把 ``updated_at`` 推到现在（追加消息后调用）。"""
        ...

    @abstractmethod
    def get_conversation_summary(self, conversation_id: str) -> tuple[str, str | None]:
        """取（上下文摘要，摘要已覆盖到的最后一条消息 id）。

        没摘要过时是 ``("", None)``。**不推 ``updated_at``**：压缩是内部优化，
        不该让会话在列表里被顶到最前——用户并没有说话。
        """

    @abstractmethod
    def set_conversation_summary(
        self, conversation_id: str, summary: str, upto_message_id: str | None
    ) -> None:
        """保存上下文摘要与它覆盖到的消息位置。"""

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> None:
        """删除会话**及其全部消息**（外键级联），以及它的产物记录。

        **只删记录，不删文件**：落在工作区里的产物是用户项目里的真实文件。
        对象存储里那些临时产物由服务层在调用本方法之前清掉。
        """
        ...

    @abstractmethod
    def append_message(self, record: ChatMessageRecord) -> ChatMessageRecord: ...

    @abstractmethod
    def list_messages(self, conversation_id: str) -> list[ChatMessageRecord]:
        """按写入顺序返回——顺序就是对话顺序，所以按 created_at 排序。"""
        ...

    # ---- 会话事件日志（P0-2）----
    @abstractmethod
    def append_turn(
        self,
        *,
        messages: Sequence[ChatMessageRecord],
        events: Sequence[SessionEventRecord],
    ) -> None:
        """把一轮的消息与它的事件**写在同一个事务里**（P0-2）。

        **为什么非要一起写**：事件日志是"当时发生了什么"的唯一事实源，而消息是
        它的投影。两者分开写，就必然存在"消息在、事件不在"的窗口——回看时会看到
        一条没有过程记录的回答，而那正是这个不可变日志要消灭的东西（v0.25 之前
        步骤只活在流里，用户刷新一次就永久丢了）。
        """
        ...

    @abstractmethod
    def append_session_events(
        self, records: Sequence[SessionEventRecord]
    ) -> list[SessionEventRecord]:
        """**只追加**一批事件，返回补好 ``seq`` / ``id`` 的那些记录。

        写在同一事务里：先锁住会话行（``FOR UPDATE``），再按该会话的
        ``max(seq)`` 逐个递增。锁是为了并发——同一会话同时有两轮在写时，
        不锁就会算出同一个 seq，而表上的唯一约束会把这变成一次失败。
        """
        ...

    @abstractmethod
    def list_session_events(
        self, conversation_id: str, *, kinds: Sequence[str] | None = None
    ) -> list[SessionEventRecord]:
        """按 ``seq`` 正序返回事件（``kinds`` 非空时只取那几种）。

        **没有分页**：一轮对话的事件量级是几十条，而读它的场景是"把这一轮的
        过程摊开"；真到几千条量级再谈（那时更该做的是按 turn 切片，不是 offset）。
        """
        ...

    # ---- 会话产物（v0.26）----
    @abstractmethod
    def create_artifact(self, record: ConversationArtifactRecord) -> ConversationArtifactRecord: ...

    @abstractmethod
    def get_artifact(self, artifact_id: str) -> ConversationArtifactRecord | None: ...

    @abstractmethod
    def list_artifacts(self, conversation_id: str) -> list[ConversationArtifactRecord]:
        """按产出顺序返回。"""
        ...

    @abstractmethod
    def mark_artifact_ingested(
        self, artifact_id: str, *, knowledge_base_id: str, document_id: str
    ) -> None:
        """记下这份产物进了哪个库、成了哪份文档。"""
        ...

    # ---- 使用者名册（调研报告 G6）----
    @abstractmethod
    def create_user(self, record: UserRecord) -> UserRecord: ...

    @abstractmethod
    def get_user(self, user_id: str) -> UserRecord | None: ...

    @abstractmethod
    def find_user_by_name(self, name: str) -> UserRecord | None:
        """按名字找——请求头里带的是名字（人记不住 id，也不该去记）。"""
        ...

    @abstractmethod
    def list_users(self) -> list[UserRecord]: ...

    @abstractmethod
    def delete_user(self, user_id: str) -> None: ...

    @abstractmethod
    def count_documents_by_user(self, user_id: str) -> int:
        """某人传过多少文档。删使用者前要能告诉他"会影响什么"。"""
        ...

    # ---- 账号与会话（v10：名册升级为账号）----
    @abstractmethod
    def find_user_by_username(self, username: str) -> UserRecord | None:
        """登录按 username 找。与 ``find_user_by_name`` 并存：name 给人看，username 给登录。

        **大小写归一化是调用方的责任**（服务层统一转小写后存储与查询）。
        存储层保持字节精确：在这里悄悄做 NOCASE 会让"库里到底存了什么"变得难追。
        """
        ...

    @abstractmethod
    def update_user_password(self, user_id: str, password_hash: str) -> None: ...

    @abstractmethod
    def set_user_disabled(self, user_id: str, disabled: bool) -> None: ...

    @abstractmethod
    def set_user_avatar(self, user_id: str, avatar_key: str) -> None:
        """换 / 清空头像（v0.29）。传空串 = 清空。

        只动这一列：头像的换与清不该走"整条账号更新"——那条路要重算用户名唯一性、
        要处理口令字段，而这里只是换一张图。
        """
        ...

    @abstractmethod
    def claim_legacy_ownership(self, owner_id: str) -> dict[str, int]:
        """把 owner 为 NULL 的知识库与会话认领给指定账号（setup 向导用）。

        返回 ``{"knowledge_bases": n, "conversations": n}``——认领了几条要让用户知道，
        静默改掉一堆数据的归属而不吭声是不行的。
        """
        ...

    @abstractmethod
    def create_session(self, record: SessionRecord) -> SessionRecord: ...

    @abstractmethod
    def get_session(self, session_id: str) -> SessionRecord | None:
        """按 id（明文 token 的 SHA-256）查。"""
        ...

    @abstractmethod
    def touch_session(
        self, session_id: str, *, last_seen_at: datetime, expires_at: datetime
    ) -> None:
        """滑动续期：每次用到都把 last_seen 与过期时间推后。"""
        ...

    @abstractmethod
    def delete_session(self, session_id: str) -> None: ...

    @abstractmethod
    def delete_sessions_for_user(
        self, user_id: str, *, except_session_id: str | None = None
    ) -> int:
        """吊销某人的会话（改密/禁用账号时）。``except_session_id`` 保住当前这条。

        返回吊销了几条——改密后界面要告诉用户"其他 N 处登录已退出"。
        """
        ...

    # ---- 知识库分享（v10）----
    @abstractmethod
    def put_share(self, record: ShareRecord) -> ShareRecord:
        """授出/调整分享档位。重复分享同一库同一人 = 改档位（INSERT OR REPLACE）。"""
        ...

    @abstractmethod
    def list_shares_for_kb(self, kb_id: str) -> list[ShareRecord]: ...

    @abstractmethod
    def list_shares_for_user(self, user_id: str) -> list[ShareRecord]:
        """某人被分享了哪些库——可见性过滤（owned + shared）里的 shared 半边。"""
        ...

    @abstractmethod
    def delete_share(self, kb_id: str, user_id: str) -> None: ...

    # ---- 用量（调研报告 G7）----
    @abstractmethod
    def record_usage(self, record: UsageEventRecord) -> UsageEventRecord: ...

    @abstractmethod
    def list_usage(self, *, since: datetime | None = None) -> list[UsageEventRecord]:
        """取某时间点之后的全部用量事件（驾驶舱窗口聚合用）。

        **不做 SQL 聚合**：驾驶舱的口径（按本地日历日分桶、按用途分类）
        属于业务判断，放在服务层更清楚，也便于单测。本地部署的数据量
        （一天几百行）全读回来聚合完全够用。
        """
        ...

    @abstractmethod
    def purge_usage_before(self, before: datetime) -> int:
        """清掉某时间点之前的用量。返回删了几行。

        用量数据只用于看趋势，留太久没有价值；与幂等键同一套保留期思路。
        """
        ...

    @abstractmethod
    def count_messages(self, conversation_id: str) -> int:
        """会话里的消息条数（列表页显示"几轮"，不必把消息全读出来数）。"""
        ...

    # ---- 模型注册器（调研报告 G1）----
    @abstractmethod
    def create_model_provider(self, record: ModelProviderRecord) -> ModelProviderRecord: ...

    @abstractmethod
    def get_model_provider(self, provider_id: str) -> ModelProviderRecord | None: ...

    @abstractmethod
    def list_model_providers(self) -> list[ModelProviderRecord]: ...

    @abstractmethod
    def update_model_provider(self, record: ModelProviderRecord) -> None: ...

    @abstractmethod
    def delete_model_provider(self, provider_id: str) -> None:
        """删供应商要**连同它下面的模型**一起删。

        外键级联在本项目不生效（连接没开 ``PRAGMA foreign_keys``），
        所以存储层显式清理——只删供应商会留下指向不存在供应商的孤儿模型。
        """
        ...

    @abstractmethod
    def create_registered_model(self, record: RegisteredModelRecord) -> RegisteredModelRecord: ...

    @abstractmethod
    def get_registered_model(self, model_pk: str) -> RegisteredModelRecord | None: ...

    @abstractmethod
    def list_registered_models(self, provider_id: str | None = None) -> list[RegisteredModelRecord]:
        """按供应商过滤（留空表示全部）。"""
        ...

    @abstractmethod
    def update_registered_model(self, record: RegisteredModelRecord) -> None: ...

    @abstractmethod
    def delete_registered_model(self, model_pk: str) -> None: ...

    @abstractmethod
    def resolve_model_binding(
        self, key: str
    ) -> tuple[ModelProviderRecord, RegisteredModelRecord] | None:
        """按"绑定键"一次取到（供应商, 模型）；键不存在或指向已删除的行时返回 ``None``。

        **为什么值得单开一个方法**：这是热路径——每建一次 LLM 客户端都要解一遍绑定，
        而一轮对话里每个工具步都要建一次。分三次查（绑定 → 模型 → 供应商）实测约 20ms，
        合成一条 JOIN 之后是它的三分之一，而且**不引入缓存**（没有"改完读到旧值"的窗口）。

        ``key`` 由调用方给（形如 ``model.binding.chat``）：仓储只认"这个键的值指向哪一行"，
        不解释键的语义。
        """
        ...

    # ---- 回收站 ----
    @abstractmethod
    def add_to_trash(self, record: TrashRecord) -> None: ...

    @abstractmethod
    def list_trash(self) -> list[TrashRecord]: ...

    @abstractmethod
    def purge_expired_trash(self, *, now: datetime | None = None) -> list[TrashRecord]: ...

    @abstractmethod
    def set_trash_expiry(self, trash_id: str, expires_at: datetime) -> None:
        """改一条回收站记录的到期时间。

        正常路径用不到它；测试要靠它模拟"7 天过去了"，
        比在用例里写裸 SQL 干净得多（也免得表结构一变就崩）。
        """
        ...

    @abstractmethod
    def delete_trash(self, trash_id: str) -> None:
        """删掉一条回收站记录（对象已由调用方处理）。

        与 ``purge_expired_trash`` 分开：那个按到期时间批量清，
        这个是"用户主动说这条不要了"，只删一条、不看时间。
        """
        ...

    # ---- 设置 ----
    @abstractmethod
    def get_setting(self, key: str) -> str | None: ...

    @abstractmethod
    def get_settings(self, keys: Sequence[str]) -> dict[str, str]:
        """一次取多个设置项（一条 SQL），只返回**库里真有值的**那些。

        单键版在每次请求的路径上被连读好几次（一次 `mineru()` 读三个键 = 三次往返，
        实测 18ms，而 PG 每次只要 2ms——成本全在往返上）。批量版把 N 次降到 1 次。
        没值的键**不出现在结果里**（不是空串）：调用方要按"库 > 引导值 > 默认值"
        的同一套优先级补齐。
        """

    @abstractmethod
    def set_setting(self, key: str, value: str) -> None: ...

    @abstractmethod
    def delete_setting(self, key: str) -> None:
        """删掉一个设置项。

        **与"设为空串"不是一回事**：空串仍是一个显式值，会参与
        "是否已配置"的判断；而删除意味着"回到没有这个设置的状态"。
        槽位解绑（G1）依赖这个区别——解绑后应当回退到 ``.env``/设置页那套，
        而不是被一个空值挡住。
        """
        ...


# ------------------------------------------------------------------ 旧会话导入（本机档独有）
#
# 一块**只属于本机档**的存储契约（M2 阶段 5，方案 §3.1 / §5 R1）。它住在这个文件里
# 的理由与其它记录一样：``services/`` 只许见 ``app.storage.base``（工程规范 §3.3 的 L2），
# 而导入器要读写的这两张表**只有本机档有**，所以契约只能落在这里。
#
# 它**不在 ``MetaStore`` 上**（服务器档没有这两张表，也没有"从别的部署导会话进来"
# 这条动作），也不在 ``repositories.py`` 的 24 个域里（那边的每个方法都必须在
# ``MetaStore`` 上存在，有两条用例逐名核对）。登记点见
# ``sqlite_impl.LOCAL_LEDGER_METHODS`` 与 ``StoreBundle.ledger``。
#
# 三件事共用这一个契约：**幂等**（键 ``(source, conversation_id, source_updated_at_ms)``
# 命中即跳过、一行不写）、**可回滚**（``outcome`` 决定删还是用快照恢复）、
# **一次会话一个事务**（``write_imported_conversation``）。

IMPORT_STATES: frozenset[str] = frozenset({"planned", "running", "done", "failed", "rolled_back"})
"""``imports.state`` 的词表（与 ``schema.sql`` 的 CHECK 同一个集合）。

放这里是为了让服务层在写之前就能拦下拼错的取值——那条 CHECK 会在事务里抛
``IntegrityError``，而那时错误信息只剩"约束失败"四个字。
"""

IMPORT_UNFINISHED_STATES: frozenset[str] = frozenset({"planned", "running"})
"""**没跑完**的那两个状态（"上一次导入断在半路"的判据，R1）。

启动自检与 ``/local/status`` 都按它判断"还有几笔账没结"——两处各写一份
``("planned", "running")`` 的话，将来加了状态（比如 ``paused``）只会有一处跟上。
"""

IMPORT_OUTCOMES: frozenset[str] = frozenset({"created", "replaced"})
"""``import_items.outcome`` 的词表：这一条当时是**新建**还是**替换**。

回滚的两条规则全靠它分岔（``created`` → 删；``replaced`` → 用快照恢复）。
"""


@dataclass(slots=True)
class ImportBatchRecord:
    """一次导入批次（``imports`` 表）。

    进度也在这张表上（``state`` + ``counts``）：CLI 开的批次与端点开的批次
    **写在同一个库里**，于是"现在到哪一步了"对两个触发入口是同一个答案。
    """

    id: str
    source: str
    """来源部署（NAS 的 API 基址，含 ``/api/v1``）。幂等键的第一段。"""
    since_ms: int | None = None
    """只导这个时刻之后更新过的会话（UTC 毫秒；``None`` = 全量）。"""
    state: str = "planned"
    """``planned`` / ``running`` / ``done`` / ``failed`` / ``rolled_back``。

    取值词表见 ``IMPORT_STATES``（与 ``schema.sql`` 的 CHECK 同一个集合）。
    """
    counts: dict[str, Any] = field(default_factory=dict)
    """计数器与如实列出的明细（列是 ``counts_json``）。

    形状按 ``state`` 分两种：跑到 ``done``/``failed`` 时是"扫了几条、新建几条、
    替换几条、跳过哪几条（**连原因**）"；``rolled_back`` 时是"删了几条、恢复几条、
    保留哪几条"。
    """
    error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ImportItemRecord:
    """台账里的一条会话（``import_items`` 表）：幂等键与回滚依据都在它身上。

    ``local_updated_at_ms`` 是**导入完成那一刻**本机这条会话的 ``updated_at``
    （毫秒）。它一个人撑着两条规则：

    - 覆盖策略 ``replace_if_local_untouched``：本机现在的值比它大 = 有人在导入之后
      动过这条会话 → **跳过**，绝不静默覆盖（方案 §3.1）；
    - 回滚：``outcome=created`` 且本机值仍等于它 → 删；``outcome=replaced`` 且仍等于
      它 → 用快照恢复；本机值变了 → 一律保留并如实报数（方案 §3.1 的两条）。
    """

    conversation_id: str
    source: str
    source_updated_at_ms: int
    outcome: str
    """``created`` / ``replaced``（见 ``IMPORT_OUTCOMES``）。"""
    batch_id: str = ""
    local_updated_at_ms: int | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class ConversationTransfer:
    """一条会话的**全量搬运形状**（会话 + 摘要 + 消息 + 事件 + 产物）。

    一个形状三处用（方案 §1.1 说的"一物两用"）：

    - 导出 = 把它编成 NDJSON（``services/conversation_export.py`` 的六型信封）；
    - 导入 = 把它整体写进本机库（``ImportLedger.write_imported_conversation``）；
    - 回滚 = 把导入前的它按同一套 NDJSON 存进 ``import-rollback/<batch>/``，恢复时读回来。

    ``summary`` / ``summary_upto`` 就是 ``conversations`` 表上那两个**不进 API** 的列
    （§1.3）：丢了它们，导入进来的长期会话就失去了"已经被压缩到哪里"，续聊会从头重算。
    """

    conversation: ConversationRecord
    summary: str = ""
    summary_upto: str | None = None
    messages: Sequence[ChatMessageRecord] = ()
    events: Sequence[SessionEventRecord] = ()
    artifacts: Sequence[ConversationArtifactRecord] = ()

    @property
    def file_references(self) -> int:
        """这条会话里**指向文件本体**的引用数（产物 + 消息附件）。

        它们不随导入过来（方案 §3.1 的取舍：体积不可控，且与"工作区文件永不上传"
        对称）：产物记录照落、``location`` 保原 key，但那几个字节还在 NAS 上。
        报告与 ``/local/status`` 要如实报这个数——否则用户会以为文件也导过来了。
        """
        return len(self.artifacts) + sum(len(message.attachments) for message in self.messages)


@runtime_checkable
class ImportLedger(Protocol):
    """本机档独有的导入台账（``imports`` / ``import_items``）+ 会话的整条写入。

    ``runtime_checkable`` 是为了让用例能一句话核对"装上去的那个实现真的满足它"
    ——结构化类型下不继承也必须满足，否则这份协议只是文档。

    **``write_imported_conversation`` 为什么也在这里**：它和台账同属"只有导入才需要"
    的本机独有能力——**一个会话一个事务**地整体落库（会话行 + 消息 + 事件 + 产物 + 摘要），
    幂等重跑与回滚恢复共用同一个入口。普通写路径没有这个形状（它是一轮一轮追加的），
    而把它加进 ``MetaStore`` 只会让服务器档也去实现一个它永远用不到的方法。
    """

    def start_import_batch(
        self, batch_id: str, *, source: str, since_ms: int | None = None
    ) -> None:
        """开一个批次（``state=planned``，``counts`` 为空）。"""
        ...

    def set_import_state(
        self,
        batch_id: str,
        state: str,
        *,
        counts: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        """推进状态；``counts`` / ``error`` 给了才覆盖（``None`` = 保持原样）。"""
        ...

    def get_import_batch(self, batch_id: str) -> ImportBatchRecord | None:
        """取一个批次（``/local/import/{batch}` 的轮询视图）。"""
        ...

    def list_import_batches(self, *, limit: int = 20) -> list[ImportBatchRecord]:
        """最近的批次，新的在前（``/local/status`` 用它与"没跑完的那一批"对账）。"""
        ...

    def get_import_item(self, *, source: str, conversation_id: str) -> ImportItemRecord | None:
        """这条会话在这个来源上**最近记下的那一条**台账（没有就 ``None``）。

        按 ``(source, conversation_id)`` 而不是整个三元组取，是为了让调用方能分辨
        两种"本机已经有这条会话"：键完全相同 = 这一版已经导过（跳过）；键更老 =
        导过的是上一版（源端更新过，本机又没动过 → 可以替换）。
        """
        ...

    def record_import_item(self, item: ImportItemRecord) -> None:
        """记一条台账（同时占下幂等键）。

        键重复时**覆盖**而不是抛：同一版重跑时不该让台账写入变成一条失败
        （``(source, conversation_id, source_updated_at_ms)`` 是主键，写的是同一件事）。
        """
        ...

    def list_import_items(self, batch_id: str) -> list[ImportItemRecord]:
        """这个批次写下的全部台账（回滚逐条按它办）。"""
        ...

    def write_imported_conversation(self, transfer: ConversationTransfer) -> None:
        """把一条会话**整体**写进本机库；**一个事务**（会话行 + 消息 + 事件 + 产物 + 摘要）。

        语义是"写到与这份 transfer 一模一样"：同 id 的已有会话（连同消息 / 事件 /
        产物）先删再写，所以它对新会话、替换、回滚恢复三种调用都是同一件事，
        重跑也安全。事件按 transfer 里给的顺序**原样落 ``seq``**——导入要保的是
        "源端当时是什么样"，不是"本机重算一遍"。
        """
        ...


# ---------------------------------------------------- 知识库元数据快照（本机档独有）
#
# 另一块**只属于本机档**的存储契约（M4 阶段 1，方案 §3.1 / §3.3）。它住在这里的理由
# 与导入台账一模一样：``services/`` 只许见 ``app.storage.base``（工程规范 §3.3 的 L2），
# 而这张 ``kb_meta_cache`` 表**只有本机档有**，所以契约只能落在这里。
#
# 它**不在 ``MetaStore`` 上**（服务器档的 KB 元数据就在自己的 PG 里，没有"抄一份 NAS
# 的快照"这条动作），也不在 ``repositories.py`` 的 24 个域里（那边的每个方法都必须在
# ``MetaStore`` 上存在）。登记点见 ``sqlite_impl.LOCAL_CACHE_METHODS`` 与
# ``StoreBundle.kb_cache``。
#
# **它是一份严格可弃的副本**（v0.3 §5.3）：真话永远在 NAS 上，删了只丢速度不丢数据。
# 所以这张表上的每一个判据都朝着"宁可说没有，也不要说错"：
#
# - 缓存里的条目**剥掉权限位**（``can_write`` / ``can_manage`` 按调用者身份算，见 M4 §1.1）
#   ——那是**调用方**在写之前做的，存储层只存它拿到的那份文本；
# - 时间到了、内容坏了、行太多、总量太大 → 一律**当没有**（下面三个上限 + 超龄）；
# - 两个时间戳**不许混**：``fetched_at``（这份内容什么时候看到的，界面说"上次更新于 X"）
#   与 ``checked_at``（最近一次确认，含"确认过没变"）。

MAX_ROWS_PER_PROVIDER = 500
"""每个提供者地址最多留多少行快照（M4 §3.4-2 的第一级上限）。

**超了按 ``fetched_at`` 淘汰最旧的**——判据刻意是"这份内容什么时候看到的"而不是
"这行什么时候写进来的"：重验证确认"没变"时不重写 payload，用写入时间会让一份天天
确认、内容稳定的快照莫名掉队。
"""

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
"""单条快照 payload 的上限（2 MiB，UTF-8 字节）。**超了不缓存**并记一条日志（§3.4-2）。

不截断、也不把这件事报给用户："这一份太大"是我们的实现细节，用户既不该看到它、
也不该因此看到半份内容。
"""

MAX_TOTAL_BYTES = 64 * 1024 * 1024
"""全部快照 payload 的总量上限（64 MiB）。超了同样按 ``fetched_at`` 淘汰最旧的。"""

SNAPSHOT_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
"""多久没被**确认**就当没有（§3.4-3 的 30 天）。

判据是 ``checked_at``（最近一次确认）而**不是** ``fetched_at``（内容上次变化）：
重验证确认"还是那份"时只推 ``checked_at``，若按 ``fetched_at`` 判，一份天天确认、
内容稳定的快照会在第 30 天被当成"旧内容"丢掉——而它恰恰是**被确认过的那一份**。
界面那句"上次更新于 X"读的仍然是 ``fetched_at``（两个时间戳不混）。
"""


@dataclass(frozen=True, slots=True)
class KbMetaCacheStats:
    """快照表的用量概览（``kb_meta_cache_stats`` 的返回值）。

    ``frozen`` 与这个类型存在的理由：它是**一次观察**的读数，不是一个可以被谁改的记录。
    形状给成 dataclass 而不是照 ``storage_stats()`` 那样回裸 dict——那个 dict 的键被
    PG 侧同名方法钉着（``MaintenanceService.overview`` 直接按键取值），这里没有那层约束，
    而"这四个数分别是什么"写在字段上比写在注释里更经得起读。
    """

    rows: int = 0
    """行数。"""
    payload_bytes: int = 0
    """这些行的 payload 合计多少字节（UTF-8；与 ``MAX_TOTAL_BYTES`` 同一把尺子）。"""
    oldest_fetched_at: datetime | None = None
    """最旧那份内容是什么时候看到的（没行就是 ``None``）。"""
    newest_fetched_at: datetime | None = None
    """最新那份内容是什么时候看到的（界面上的"最近更新"就是它）。"""


@dataclass(frozen=True, slots=True)
class KbMetaCacheRecord:
    """一行知识库元数据快照（``kb_meta_cache`` 表，M4 §3.1）。

    ``frozen`` 是刻意的——**34 个 ``*Record`` 里只有这一个不可变**：它是从 NAS 抄下来的
    一份观察，改一个字段等于伪造一份没发生过的观察。要"改动"就写一行新的
    （``put_kb_meta_cache``）或只推确认时间（``touch_kb_meta_cache``）。

    两个时间戳的分工见 ``SNAPSHOT_MAX_AGE_SECONDS`` 那段；其余字段逐条：

    - ``provider``：提供者地址（归一化：去尾斜杠）。**键空间隔离靠它**——家里的 NAS 与
      单位的 NAS 各自一份，谁也不会覆盖谁；
    - ``resource`` / ``scope_key``：资源名与资源内的键（``kb_list`` 的 ``scope_key`` 是
      空串，别的资源是 kb_id / document_id / 视图指纹）。**存储层不校验取值**：词表是
      服务层的事（与 ``session_events.kind`` 同一口径），这里只当字符串存；
    - ``payload``：NAS 回的那份 JSON 的**文本原样**。刻意不是解析好的对象：存储层不解释
      JSON（它只保证"写进去的是合法 JSON"，由列上的 ``CHECK (json_valid(...))`` 兜），
      而原样保存意味着"读出来再写回去"不会因为两次序列化而变形；
    - ``version``：内容哈希（``sha256:…``）。etag 缺席时的"变没变"判据（§4.1）；
    - ``etag`` / ``last_modified``：服务端给的时候才有——今天**恒 NULL**（NAS 的 KB 读
      端点还没有条件请求，§4.1 实测），列先建好，将来 NAS 侧加上就自动生效；
    - ``source``：这行是怎么来的（``handshake`` / ``reader`` / ``revalidate``）；
    - ``identity``：这条记录当时是**谁**取回来的（``api_key`` / ``session``）。
      **只作排障，不许当判据**——快照的"可信度"不该取决于它是哪把钥匙看到的；
    - ``stale``：上一次再验证失败了（内容照旧可读，但要如实标出来）；
    - ``last_error``：那次失败的原因（成功一次就清空）。
    """

    provider: str
    resource: str
    scope_key: str
    payload: str
    version: str
    source: str
    fetched_at: datetime
    checked_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    identity: str = ""
    stale: bool = False
    last_error: str = ""


@runtime_checkable
class KbMetaCache(Protocol):
    """本机档独有的知识库元数据快照存取（``kb_meta_cache`` 一张表）。

    ``runtime_checkable`` 与 ``ImportLedger`` 同一个理由：结构化类型下不继承也必须
    满足，否则这份协议只是文档——用例要能一句话核对"装上去的那个实现真的满足它"。

    **六个方法就是全部**（写者只有本机后端一个，见 M4 §2.3）：一读、一整写、一"只推
    确认"、一删行、一按档清、一报数。没有"更新 payload"这种方法：内容变了就是**另一次
    观察**，走 ``put`` 整行替换（连同 ``fetched_at`` 一起前进）。
    """

    def get_kb_meta_cache(
        self, provider: str, resource: str, scope_key: str
    ) -> KbMetaCacheRecord | None:
        """取一行；**超龄（``SNAPSHOT_MAX_AGE_SECONDS`` 没被确认）当没有**，并把那行删掉。

        "当没有"而不是"回一行旧的让人自己去判断"：调用方是页面与提示词增强，
        它们只会把回出来的东西当真话（"更久以前的内容连看一眼的价值都抵不过误导风险"，
        §3.4-3）。删行是顺手——它已经被判成没有，留着只会让下次读再判一遍。
        """
        ...

    def put_kb_meta_cache(self, record: KbMetaCacheRecord) -> bool:
        """整行写入/覆盖（主键是那三列）。返回是否真的落了库。

        ``False`` = **payload 超过 ``MAX_PAYLOAD_BYTES``，没有缓存**（并记一条日志）。
        这不是错误：一份过大的内容不该让调用方那条链失败，只是它不值得留副本。
        写完之后在同一个事务里收一次超限（行数 / 总量按 ``fetched_at`` 淘汰最旧的）。
        """
        ...

    def touch_kb_meta_cache(
        self,
        provider: str,
        resource: str,
        scope_key: str,
        *,
        checked_at: datetime,
        stale: bool = False,
        last_error: str = "",
    ) -> bool:
        """**只推确认**：改 ``checked_at`` / ``stale`` / ``last_error``，别的一列不动。

        它对应 SWR 里"变没变"的答案是**没变**：payload 与 ``version`` 原封不动
        （重写一遍内容相同的东西只会让 ``fetched_at`` 撒谎：界面会说"上次更新于 X"，
        而其实什么都没更新）。返回是否改到了行（``False`` = 这行已经不在了，
        调用方该走 ``put``）。
        """
        ...

    def drop_kb_meta_cache(self, provider: str, resource: str, scope_key: str) -> int:
        """删一行，返回删掉的条数。

        两个调用场景都是"这条内容不作数了"：远端回 404（这个库没了）、
        以及写类动作成功后的就地失效（§3.4-1——页面那条链自己知道写了什么）。
        """
        ...

    def purge_kb_meta_cache(
        self,
        *,
        provider: str | None = None,
        resource: str | None = None,
        scope_key: str | None = None,
        scope_prefix: str | None = None,
    ) -> int:
        """按档清空，返回删掉的条数（设置面板那颗「清除」与 ``DELETE /local/kb-cache``）。

        四个条件都是可选的，**给了就 AND 上去**；都不给就是全清。三档粒度这么表达：

        - 全清：不传；
        - 按地址清：``provider``；
        - 按库清：``resource`` + ``scope_key``（``kb_detail`` / ``folders`` 的键就是
          kb_id），文档列表那些视图用 ``scope_prefix``（它的 ``scope_key`` 以
          ``<kb_id>|`` 开头——带分隔符的**前缀由调用方拼**，存储层不认识视图指纹的格式）。
        """
        ...

    def kb_meta_cache_stats(self, *, provider: str | None = None) -> KbMetaCacheStats:
        """报数：多少行、多少字节、最旧/最新那份是什么时候看到的。

        不加"字节数从哪来"的判断：它就是把 ``payload`` 的 UTF-8 长度加起来——
        与 ``MAX_TOTAL_BYTES`` 同一把尺子，否则"报出来的数"与"淘汰时的数"会对不上。
        """
        ...


# ---------------------------------------------------- 快照打包与读回（本机档独有）
#
# 第三块**只属于本机档**的存储契约（M5 阶段 2，方案 §2.3 / §3.5）。它住在这里的理由与
# 导入台账、知识库快照一模一样：``services/`` 只许见 ``app.storage.base``（工程规范 §3.3
# 的 L2），而"把本机库打成一份擦洗干净、可搬到别的机器的包"这件事**只有本机档有**
# （服务器档的库就是它自己，没有"打包带走"这条动作；NAS 侧那一半是收包，见
# ``api/v1/backup.py``）。登记点见 ``sqlite_impl.LOCAL_SNAPSHOT_METHODS`` 与
# ``StoreBundle.snapshot``。
#
# **两份面分开**（形似 ``ImportLedger`` 与 ``KbMetaCache``，但那是两个对象、这里是同一个
# 对象的两面）：写面 ``LocalSnapshotArchiver`` 给打包那一层用，它关心的只有一件事——
# "给我一份擦洗过的副本"（在线备份 + 擦洗 + ``VACUUM`` 全在存储层，因为那些是 SQLite
# 方言）；读面 ``SnapshotSource`` 给按点恢复那一层用，它关心的是"这份快照里有什么"。
# 分成两份协议不是仪式：打包不该看得见"快照里有哪些会话"（它照单全收），
# 恢复不该看得见"怎么造一份副本"。


@dataclass(frozen=True, slots=True)
class SnapshotRedaction:
    """擦洗清单里的一条：**哪个表的哪一列 / 哪个键被清掉了，几行**（M5 §2.4）。

    ``frozen`` 与 ``KbMetaCacheRecord`` 同一个理由：它是一份**已经发生过的动作**的记录，
    改一个字段等于伪造一件没做过的事。形状就是 manifest 的 ``redacted`` 段那一条
    （方案 §2.3），``as_payload()`` 是那一处唯一的拼装点——两处各拼一遍，字段名迟早会漂。

    两种粒度用哪个字段表达，取决于擦洗动作本身：

    - **列级**（``model_providers.api_key`` → ``''``、``mcp_servers.env,headers`` → ``'{}'``）：
      行还在，值是空的，``column`` 写列名（两列一起清的那一条按方案写成 ``"env,headers"``）；
    - **行级**（``app_settings`` 里的凭据键 → ``DELETE``）：整行没了，``key`` 写那一个键。
    """

    table: str
    column: str = ""
    key: str = ""
    rows: int = 0

    def as_payload(self) -> dict[str, Any]:
        """manifest ``redacted`` 段里的一条（列级与行级两种形状，见类说明）。"""
        if self.column:
            return {"table": self.table, "column": self.column, "rows": self.rows}
        return {"table": self.table, "key": self.key, "rows": self.rows}


@dataclass(frozen=True, slots=True)
class SnapshotDumpReport:
    """一份**擦洗过的副本**的读数（``LocalSnapshotArchiver.dump_scrubbed_db`` 的返回）。

    它是一次观察，不是一个可以被改的记录（``frozen``）：副本落盘之后，这份读数就是
    "包里有什么、洗掉了什么"的唯一依据，manifest 那两个段直接由它来。

    ``counts`` 是**库侧事实**（会话 / 消息 / 事件 / 笔记 / 工作区 / 定时任务 / 产物 /
    设置的行数），键名与 manifest 的 ``counts`` 段对得上；``memory_files`` 不在里面——
    记忆是文件不是库，由打包那一层扫目录补上。
    """

    path: Path
    """副本落点（打包前那条"目录里只有 kylab.db"的断言查的就是它所在的目录）。"""
    bytes: int
    schema_version: int
    """副本里的 ``schema_metadata.version``：manifest 的 ``schema_version`` 就是它。"""
    counts: dict[str, int] = field(default_factory=dict)
    redacted: tuple[SnapshotRedaction, ...] = ()
    """洗掉了什么（**按表名排序，稳定**）：manifest 的 ``redacted`` 段原样用它。"""


@dataclass(frozen=True, slots=True)
class SnapshotConversationRef:
    """快照里的一条会话（读面的**窄投影**，不是 ``ConversationRecord``）。

    为什么另给一个类型而不是直接回 ``ConversationRecord``：这份东西来自**一份快照文件**，
    不是本机库——它的每个字段都只保证"当时是这样"。真要把会话整条搬进本机库，
    那是阶段 5 的 ``ConversationTransfer``（它走 ``ImportLedger`` 那条台账路径）。

    ``updated_at_ms`` 给成毫秒整数而不是 ``datetime``：它的唯一用途是与导入台账的
    ``source_updated_at_ms`` / ``local_updated_at_ms`` 比大小（M2 那三条覆盖规则），
    同一把尺子比"更早/更晚"才不会有第二次换算。
    """

    id: str
    title: str = ""
    messages: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotArtifactRef:
    """快照里的一条产物记录（``conversation_artifacts`` 的窄投影）。

    打包那一层要的三件事全在这里：``storage`` 决定走哪条"选择性"判据（位置判据 /
    工作区判据），``location`` 指出字节在哪（对象 Key 或工作区里的绝对路径），
    ``size_bytes`` 是库里记的大小（真实大小以磁盘为准，见打包那一层的额度判据）。

    ``location`` 是**由调用方解释**的字符串（与 ``ConversationArtifactRecord.location``
    同一口径）：存储层不知道工作区是什么，它只把这一列读出来。
    """

    id: str
    conversation_id: str
    name: str
    format: str = ""
    size_bytes: int = 0
    storage: str = ARTIFACT_IN_OBJECTS
    location: str = ""
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotDbView:
    """一份快照库读出来的全集（``SnapshotSource.read_snapshot_db`` 的返回）。

    六项就是读面能回答的全部问题（方案 §2.1 的内容物清单在库里的那一半）：

    - ``counts``：库侧事实的行数（与 ``SnapshotDumpReport.counts`` 同一个形状）；
    - ``conversations``：会话清单（dry-run 先据它报"会新建哪些"）；
    - ``artifacts``：产物记录（``location`` 就是打包/还原用的 Key 或路径）；
    - ``settings_keys`` / ``settings``：设置有哪些键、值是什么（**恢复第 9 步要写进本机**
      的那一份）。值单独给一份：dry-run 只报"补哪几个键"，恢复要真的写，两处共用这一读。
      键与值都是**擦洗之后**的（``SECRET_KEYS`` 与三个前缀的行已经不在包里了——
      读得出来就说明它们没进包）；
    - ``model_provider_names``：快照里那几个模型供应商的**名字**（恢复报告 R13 要用它说
      "这几个模型凭据要重配"）。只有名字：``api_key`` 那一列被擦洗清空了，包里没有凭据。

    **记忆不在这里**：它在 ``<data_dir>/memory/**`` 是文件，不是库里的行——
    读它的是打包/恢复那一层扫目录，不该让存储层假装它也在库里。
    """

    schema_version: int
    counts: dict[str, int] = field(default_factory=dict)
    conversations: tuple[SnapshotConversationRef, ...] = ()
    artifacts: tuple[SnapshotArtifactRef, ...] = ()
    settings_keys: tuple[str, ...] = ()
    settings: dict[str, str] = field(default_factory=dict)
    model_provider_names: tuple[str, ...] = ()


SNAPSHOT_EXCLUDED_SETTING_PREFIXES: tuple[str, ...] = (
    "provider.",
    "model.",
    "backup.",
    "auth.",
)
"""擦洗 ``app_settings`` 时**除了 ``SECRET_KEYS`` 还要整族排除**的键前缀（M5 §2.4）。

这四族是"连上谁 / 怎么连 / 这台机器自己的签名材料"那一类键（提供者地址与钥匙、模型侧
凭据、备份自己的开关、``auth.*``），与 ``runtime_config.SECRET_KEYS`` 一起构成
"凭据类设置"的判据。**为什么放在接口层**：它是**契约**而不是某一个实现的做法——阶段 6
（钥匙串收编）要拿同一份前缀去判"库里还有哪些明文凭据没迁完"，如果那边自己再写一遍
``"provider."``，两处就会各漂各的（那正是方案 §2.4 要求"引用常量本身、不手抄"的那类漂移）。

**``auth.`` 那一族是修 401 那次补的**：本机档在组合根生成一条
``auth.url_signing_secret``（见 ``services/auth.ensure_url_signing_secret``），
而在此之前它不在任何排除名单里——**它会随快照一起被传到 NAS 上去**
（这条是 M5 纪律的直接推论：快照里绝不放秘密）。为什么按**前缀族**写而不是给这一个键
开一条专门判据：这一族的语义是"本机自己的签名材料"，与前三族同类——它们都**不该跟着
快照走**（换一台机器重新生成更好：签出去的链接本来就该重签）；而且将来再加 ``auth.*``
的键（比如某个 token）会自动跟着排除，按单个键写判据就会漏。

判据的**另一半**（``SECRET_KEYS`` 本身）住在 ``services/runtime_config.py``：
"哪些键是密钥"是设置那一层的事实，存储层不许在模块级反向依赖它——所以那一半由擦洗的
调用点现取（见 ``sqlite_impl/backup_archive.py`` 的 ``_excluded_settings``）。
"""


@runtime_checkable
class LocalSnapshotArchiver(Protocol):
    """本机档独有的**写面**：造一份擦洗过的库副本（M5 §2.3 / §2.4）。

    ``runtime_checkable`` 与 ``ImportLedger`` / ``KbMetaCache`` 同一个理由：结构化类型下
    不继承也必须满足，否则这份协议只是文档——用例要能一句话核对"装上去的那个实现真的
    满足它"。

    **只有一个方法**：擦洗那三条（``secure_delete`` → 白名单外清列删行 → ``VACUUM``）
    与"在线备份"必须一起发生、且必须发生在**副本**上——把它们拆成几个可单独调用的方法，
    就等于允许调用方"先备份、忘了擦洗"。所以这里给的是一个整体的动作，不是零件。
    """

    def dump_scrubbed_db(self, dest: Path) -> SnapshotDumpReport:
        """把本机库**在线备份**到 ``dest``，在副本上擦洗掉秘密，``VACUUM`` 之后回收读数。

        四条口径（方案 §2.4，一条都不能省）：

        1. 用 ``sqlite3.Connection.backup()``（在线备份 API）：WAL 下安全，**复制期间
           不停边车**，拷出来的是一致的快照；
        2. 擦洗只动副本，**源库一个字节不动**；
        3. 先 ``PRAGMA secure_delete = ON`` 再删/清，最后 ``VACUUM``——SQLite 的
           UPDATE/DELETE 不覆盖旧页内容，只做前两步的话明文会留在空闲页里，
           而"包解开后 grep 哨兵串 0 命中"正是这一步的判据；
        4. 副本是**单文件、不带 ``-wal``**：它可能落在只读介质上（阶段 5 会只读打开它）。
        """
        ...


@runtime_checkable
class SnapshotSource(Protocol):
    """本机档独有的**读面**：从一份快照库里读出"包里有什么"（M5 §3.4 的恢复输入）。

    三个方法，两个读形状（阶段 5 加的第二个）：

    - :meth:`read_snapshot_db`：**窄投影**——"包里有什么"（计数、标题、Key / 设置键清单），
      预演那份报告主要靠它；
    - :meth:`iter_snapshot_transfers`：**逐会话全量**——导入器真正要吃的
      ``ConversationTransfer``（按点恢复走的就是这一条）；
    - :meth:`local_schema_version`：取"本机认得哪个版本"的那个数（版本判据要用）。

    读面的每一项都在同一份快照上，分几次读只会多几个可能对不上的瞬间（会话与产物是同一个
    库里的两张表）。三个方法都在同一个对象上（本机档是那一个 ``SqliteMetaStore``），
    所以"能读快照"这件事只需要一次 ``isinstance`` 判定。
    """

    def read_snapshot_db(self, db_path: Path) -> SnapshotDbView:
        """读一份快照库（``<解包目录>/db/kylab.db``）。

        **只读打开**（``mode=ro``）：快照是只读的观察对象，读它不该产生 ``-journal`` /
        ``-wal``，也不该给"顺手改一下"留下任何可能。

        版本不认识就抛 ``SnapshotFormatError``（``schema_version`` 高于本机识别的上限
        → 拒绝，照 ``schema.py`` 那条"不降级"的纪律）：更省事的做法是"只读我认识的那几列"，
        那正是"猜着读"。
        """
        ...

    def iter_snapshot_transfers(self, db_path: Path) -> Iterator[ConversationTransfer]:
        """逐条读一份快照库的**全量会话**（M5 阶段 5：按点恢复的输入）。

        与 :meth:`read_snapshot_db` 的分工：那一个是**窄投影**（计数、标题、Key 清单——
        "包里有什么"），这一个给的是导入器真正要的 :class:`ConversationTransfer`
        （会话 + 摘要 + 消息 + 事件 + 产物，与 NAS 那条 NDJSON 导出流产出**同一种东西**）。

        **生成器**：一次只在内存里留一条会话（与 ``HttpExportSource`` 的内存上界同一条
        纪律）——几千条会话的快照库整份读进内存，是这条链上最容易忽略的那一项。

        字段映射与 ``services/conversation_export`` 那一套**逐字段对齐**（同一批记录类型、
        同一套 JSON 列解码），列名与顺序照 ``schema.sql``：消息按 ``created_at_ms, rowid``、
        事件按 ``seq``、产物按 ``created_at_ms, id``——与存储层 ``list_messages`` /
        ``list_session_events`` / ``list_artifacts`` 三处**逐字一致**（顺序不同会让
        "恢复出来的会话"与"导出导入的会话"在回放上分岔）。

        版本不认识同样抛 ``SnapshotFormatError``。
        """
        ...

    def local_schema_version(self) -> int:
        """**本机库**的 schema 版本（``sqlite_impl/schema.SCHEMA_VERSION`` 那个数）。

        为什么要经存储层拿：判"这份快照我认不认识"要拿它跟快照自报的 ``schema_version``
        比，而那个常量住在 ``sqlite_impl/``——``services/`` 不许 import 具体实现（L2）。
        在服务层重写一个数字就是第二个真相源，所以这里给一个读。

        **只回数字，不做判断**：拒绝的纪律（不认识即拒、不降级）在 :meth:`read_snapshot_db`
        与 ``services/backup_snapshot.parse_manifest`` 两处执行——这个方法是给它们取数用的。
        """
        ...


# ---------------------------------------------------- 备份快照待传队列（本机档独有）
#
# 第四块**只属于本机档**的存储契约（M5 阶段 3，方案 §3.1 / §3.2 / §3.3）。它住在这里的
# 理由与前一块（快照打包、导入台账、知识库快照）一模一样：``services/`` 只许见
# ``app.storage.base``（工程规范 §3.3 的 L2），而"断网时先把要传的东西排队、联网再补传"
# 这件事**只有本机档有**（服务器档自己就是那份备份的目的地，没有"往别处传"这条动作）。
# 登记点见 ``sqlite_impl.LOCAL_BACKUP_METHODS`` 与 ``StoreBundle.backup_queue``。
#
# **它为什么是库里一张表、不是目录**（方案 §3.1 的四条，逐条都是这一块的形状理由）：
#
# 1. 队列项要有状态机 / 尝试次数 / 下次可试时间 / 失败原因——目录名表达不了这些；
# 2. "断网入队、联网补传"要能**跨重启继续**（进程被杀、机器重启都在"断网"这一档里），
#    而这份持久性只有库有；
# 3. 幂等判据是**内容哈希**（``sha256``），它得与那一行一起落库，重试才认得出"还是这一份"；
# 4. 与 M2 的 ``imports`` / ``import_items`` 同一套形态、同一个库、同一把写锁——
#    两台机器上的两条"后台补做"的链，不该有两种持久化形状。
#
# 它与 ``LocalSnapshotArchiver`` 是**两件事、两个字段**（阶段 2 落地后特意分开命名）：
# 归档器对着"打一份快照"，这张表对着"把那一份传出去"。名字撞在一起的话，
# 下一个读代码的人要在两处猜哪个是哪个。

BACKUP_SNAPSHOT_STATES: frozenset[str] = frozenset(
    {"pending", "uploading", "uploaded", "failed", "discarded"}
)
"""``backup_snapshots.state`` 的词表（与 DDL 的 ``CHECK`` 同一个集合）。

五档的含义就是状态机本身：``pending`` 等着传；``uploading`` 正在传（**进程被杀会留下
这一档**，启动时复位回 ``pending``，照 M2"未跑完的批次可续"同一句口径）；``uploaded``
传完了（本地那份包已经删掉）；``failed`` 传过但失败（退避之后再试，**永不放弃**）；
``discarded`` 本地队列上限到了被丢掉（"如实报，不静默"，见 ``BACKUP_UNFINISHED_STATES``）。
"""

BACKUP_UNFINISHED_STATES: tuple[str, ...] = ("pending", "uploading", "failed")
"""**还没备上去**的那三档（界面那句"有 N 份没备上去"就是数它）。

``uploaded`` 与 ``discarded`` 都不在里面：前者已经备上去了、后者已经如实报过它没备成。
按"三档"而不是"非终态"写出来，是因为将来加状态时**这一处必须重新想一遍**
（例如加 ``paused`` 时，它算不算"没备上去"是一个产品判断，不该由 ``not in (…终态…)``
悄悄替你答）。
"""

BACKUP_SNAPSHOT_KINDS: frozenset[str] = frozenset({"manual", "auto", "pre_restore"})
"""``backup_snapshots.kind`` 的词表（与 DDL 的 ``CHECK`` 同一个集合）。

与 ``services/backup_snapshot.py`` 的 ``SNAPSHOT_KINDS`` 是**同一份词表的两处写法**
（存储层的 CHECK 与打包器写的那个值），用例机械核对两边一致——两处各漂各的，
会在"一种快照类型悄悄进不了队"这种地方露出来。

``pre_restore`` 那一档**只落本机、不入这张表**（方案 §3.2：恢复前那一份是"误覆盖"的
第一道兜底，不是要传上去的备份）——它在词表里是因为 manifest 的 ``kind`` 有它。
"""


@dataclass(slots=True)
class BackupSnapshotRecord:
    """``backup_snapshots`` 的一行：**一份已经打好、正等着传出去的快照**。

    它是"打包器"与"补传队列"之间那份交接单，也是跨重启继续的唯一依据——所以它的字段
    必须让补传那一步**不依赖任何内存里的对象**：包在哪（``blob_path``）、多大
    （``blob_bytes``）、声明的摘要是什么（``sha256``）、清单原文是什么
    （``manifest_json``）全在行上。

    ``id`` 是内容寻址的（``<device_id>-<时刻>-<快照体摘要前 8 位>``，方案 §1.2），
    所以同一份内容重放不会造出第二行——``put_backup_snapshot`` 是**覆盖写**
    （点两次「立即备份」不该让第二次抛异常，与 NAS 侧"同内容 200 no-op"同一条口径）。

    ``blob_path`` 在 ``discarded`` 之后仍然指着那份（已经被删掉的）包：它是"这一份当时
    是什么、后来没备成"的记录，而不是"现在还能读到它"的承诺——判断能不能读，看 ``state``。
    """

    id: str
    created_at: datetime
    kind: str
    state: str
    sha256: str
    blob_path: str = ""
    blob_bytes: int = 0
    manifest_json: str = "{}"
    attempts: int = 0
    next_attempt_at: datetime | None = None
    """下一次可以试的时刻（UTC）。**``None`` = 立即到期**（刚入队、或从 ``uploading``
    复位回来的那一行）——而不是"永远不再试"：终态由 ``state`` 表达，不由这一列表达。
    """
    last_error: str = ""
    """最近一次失败的原因（成功一次就清空）。``discarded`` 那一档写的是上限那条理由。"""
    uploaded_at: datetime | None = None
    remote_device_id: str | None = None
    """远端确认下来的坐标（服务端那一对）。**上传者没报就是 ``None``**——本机不编一个。"""
    remote_snapshot_id: str | None = None


@runtime_checkable
class BackupSnapshots(Protocol):
    """本机档独有的**备份待传队列**（``backup_snapshots`` 一张表）。

    ``runtime_checkable`` 与 ``ImportLedger`` / ``KbMetaCache`` / 那两份快照协议同一个
    理由：结构化类型下不继承也必须满足，否则这份协议只是文档——用例要能一句话核对
    "装上去的那个实现真的满足它"。

    **五个方法就是全部**（名字定案）：写一行 / 读一行 / 列一批 / 推一步状态 / 复位半截。
    没有"删除行"方法：队列是**如实的历史**（传成的、丢弃的、失败的都留着），
    删它们没有任何产品动作要对它——要给用户看的正是"哪几份没备上去、为什么"。
    """

    def put_backup_snapshot(self, record: BackupSnapshotRecord) -> None:
        """整行写入；**同 id 覆盖**（不是"撞了报错"）。

        同 id = 同一份内容（内容寻址）。所以覆盖写要表达的是"这份又要传一次"，
        而不是"两件事撞车了"：手动点两次「立即备份」、或打包器与队列对同一份包各登记
        一次，都不该让用户看到一个 ``IntegrityError``。``created_at`` 以 id 里那个时刻
        为准（覆盖时不改它——id 与它必须是同一件事）。
        """
        ...

    def get_backup_snapshot(self, snapshot_id: str) -> BackupSnapshotRecord | None:
        """取一行（没有就 ``None``）。队列那一层用它回"我刚入的那一份现在什么状态"。"""
        ...

    def list_backup_snapshots(
        self,
        *,
        states: Sequence[str] | None = None,
        due_before: datetime | None = None,
        newest_first: bool = False,
        limit: int | None = 50,
    ) -> list[BackupSnapshotRecord]:
        """列一批。两种读形状（各有一条索引对着它）：

        - **队列**（补传那一层）：``states`` 给"还等着传的那几档" + ``due_before=now``，
          默认**最旧的在前**（先来先传：队列就该按到达顺序排空），只取 ``limit`` 条；
        - **最近几份**（界面上的队列视图）：``newest_first=True``，新的在前。

        ``due_before`` 的判据是 ``next_attempt_at IS NULL OR next_attempt_at <= due_before``
        ——``None`` 那一档按"立即到期"算（见 ``BackupSnapshotRecord.next_attempt_at``）。
        ``limit=None`` = 不设上限（报数那一读要用它，不给它就只能"取前 N 条再假装是全部"）。
        """
        ...

    def mark_backup_snapshot(
        self,
        snapshot_id: str,
        state: str,
        *,
        last_error: str | None = None,
        bump_attempts: bool = False,
        next_attempt_at: datetime | None = None,
        clear_next_attempt: bool = False,
        uploaded_at: datetime | None = None,
        remote_device_id: str | None = None,
        remote_snapshot_id: str | None = None,
    ) -> None:
        """推进一步状态（一次 UPDATE，**不做读-改-写**）。

        参数分三类，各自的缺省含义不一样（这是这一份签名唯一的难点，写在明处）：

        - ``last_error``：``None`` = 不碰；给字符串就写（成功时给 ``""`` = 清掉失败原因）；
        - ``bump_attempts``：``True`` 时在 SQL 里自增（``attempts = attempts + 1``）——
          自增必须在库里做：补传是"读 → 试 → 写"三步，而中间那一步是**网络**，
          在 Python 里算好再写回去会把并发踩成少记一次；
        - ``next_attempt_at`` / ``clear_next_attempt``：前者给值就写，后者为真就置 ``None``
          （终态"没有下一次了"），两个都给是调用方的错 → ``ValueError``；
        - ``uploaded_at`` / ``remote_*``：同 ``last_error``（``None`` = 不碰）。

        ``state`` 不在词表里 → ``ValueError``（照 ``set_import_state`` 那条）。
        """
        ...

    def reset_uploading_snapshots(self, *, next_attempt_at: datetime | None = None) -> int:
        """把 ``uploading`` 复位成 ``pending``，返回复位了几行（启动时那一次）。

        ``uploading`` 是"进程被杀留下的半截"（照 M2"未跑完的批次可续"同一句口径）：
        那一次到底传到哪儿了说不清，但**重试是安全的**（blob 与 manifest 都幂等，
        见方案 §3.3），所以复位就是正确答案，而不是一个需要人判断的岔路。

        ``attempts`` 与 ``last_error`` **一个都不动**：它们是历史（"试过几次、上次为什么
        失败"），复位改的是"能不能再试"，不是"试过没有"。``next_attempt_at`` 缺省为
        ``None`` = 立即到期——启动时没有理由再等一轮退避。
        """
        ...


@runtime_checkable
class LocalEraser(Protocol):
    """本机档独有的**安全擦除**（``secure_erase`` 一个方法）：把本机那个库文件擦干净。

    **与 ``ledger`` / ``kb_cache`` / ``snapshot`` / ``backup_queue`` 同一条纪律、同一套
    理由**（照那四段改写一遍，因为形状一模一样）：它擦的是**本机那个库文件**——服务器档
    的数据在 PG 里，那份凭据的处置是 NAS 自己的访问控制（方案 §5 的 R14 明写"不进本机
    钥匙串"、也明写那一档"库里那份凭据不动"）。所以它既不进 ``repositories.py`` 的 24 个域
    （那里的每个方法都必须在 ``MetaStore`` 上存在），也不进 ``LOCAL_METHODS``（那是
    "本机域 / KB 域"的划分：擦除连表都不看，纯粹是文件级的收尾）。它在
    ``sqlite_impl.LOCAL_ERASER_METHODS`` 单独登记，装配点见
    ``core/storage.py::_build_local_stores``（与 ``meta`` / ``ledger`` / ``kb_cache`` /
    ``snapshot`` / ``backup_queue`` 是**同一个**实例：写锁是进程内一把，那条纪律是对着
    ``Database`` 说的）。

    谁用它：``services/credentials.py``（钥匙串收编的迁移器——把库里那几处明文删掉 / 清掉
    之后，那几页得真的还回去）。调用方在**同一次运行里**就要看到干净：主库、``-wal``、
    ``-shm`` 三个文件一起（不是"等边车退出才算干净"）。
    """

    def secure_erase(self) -> None:
        """把本机库擦干净：**抹零删页 → 重建库文件 → 收 WAL 并截掉那个文件**。

        三件事的顺序不许换，每一步挡的是不同的一种残留：

        1. ``PRAGMA secure_delete = ON``：被删 / 被改的页腾出来时先抹零。它是**每连接**的
           PRAGMA，所以必须与下面那条语句在**同一条连接**上、且在它之前；
        2. ``VACUUM``：重建整份库文件，把抹零腾出来的那些页连同页里的残留一起丢掉
           （``VACUUM`` 不能在事务里跑）；
        3. ``wal_checkpoint(TRUNCATE)``：把 ``-wal`` 收进主库并**把那个文件截成 0 字节**。

        第 3 步是这条契约存在的理由：WAL 模式下明文的最近一份提交先落在 ``kylab.db-wal``
        里，前两步只保证**主库**干净——旧帧还躺在 WAL 里等着被下一个读的人捡到。
        所以判据是"同一次运行里，主库 + ``-wal``（+ ``-shm``）里 grep 明文都是 0 命中"，
        而不是"重启之后干净"。

        **边车跑着也能跑**（CLI 那条路正是这个场景）：三步在同一次调用里做完，不关任何
        长连接。收 WAL 那一步拿不到"没有别的读者"时会 ``busy``——实现里有少量重试，
        仍不行就**如实抛**（``StorageError``：主库已经擦干净了，剩下的是 WAL 里那几帧，
        那句话得让调用方看见，由它决定是再跑一次还是接受这个残留）。
        """
        ...


class VectorStore(ABC):
    """向量仓储：按知识库分区（架构 §8.3），维度在分区创建时确定。"""

    @abstractmethod
    def ensure_partition(self, kb_id: str, *, dim: int) -> str | None:
        """为知识库建向量分区；已存在且维度不一致时必须报错而不是静默写入。

        **返回一句"要告诉用户的话"**（没有该说的事就是 ``None``）。目前唯一的来源是
        "维度超过索引上限、这个库只能走**精确检索**"（见 ``PostgresVectorStore``）。
        做成返回值而不是只写日志：这件事**用户必须知道**（否则他会以为索引建好了、
        只是慢），而存储层够不着界面——把那句话交给调用方去落（摄入那一层会记日志，
        界面那面迟早按同一句话显示）。调用方可以忽略返回值，行为与从前一致。
        """

    @abstractmethod
    def upsert_vectors(self, kb_id: str, *, items: Sequence[tuple[str, Sequence[float]]]) -> None:
        """写入/覆盖向量，``chunk_id`` 为主键。"""

    @abstractmethod
    def delete_vectors(self, kb_id: str, *, chunk_ids: Sequence[str]) -> int: ...

    @abstractmethod
    def search(
        self, kb_id: str, *, query_vector: Sequence[float], top_k: int
    ) -> list[VectorMatch]: ...

    @abstractmethod
    def drop_partition(self, kb_id: str) -> None: ...

    @abstractmethod
    def list_partitions(self) -> list[str]:
        """列出已存在的向量分区（知识库 id）。维护页据此识别孤儿分区。"""


class FullTextStore(ABC):
    """全文仓储：FTS5 + 中文分词，与元数据同库。"""

    @abstractmethod
    def index_chunks(self, chunks: Sequence[ChunkRecord]) -> None: ...

    @abstractmethod
    def delete_chunks(self, chunk_ids: Sequence[str]) -> int: ...

    @abstractmethod
    def search(self, *, query: str, top_k: int, kb_id: str | None = None) -> list[SearchHit]: ...


class ObjectStore(ABC):
    """对象存储：原文、Markdown 产物、图片；内容 hash 寻址。"""

    @abstractmethod
    def write(self, key: str, data: bytes) -> str:
        """写入并返回可持久化的存储路径。"""

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def move_to_trash(self, path: str, *, trash_id: str) -> str: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...


class TabularStore(ABC):
    """表格结构化副本（M2 / T2.11；DuckDB 实现）。

    **为什么单独一个仓储、而不是又一张 SQLite 表**：它回答的是
    "某份 CSV 的第 3 行第 2 列是什么"这类**按行列定位**的问题。
    DuckDB 是列式分析库，按列读、按行扫都比 SQLite 合适；
    而且它与主库物理分离，"分析型查询拖慢主库"从结构上就不会发生。

    **表名约定为 ``document_id``**（一份文档一张表）：删文档时连带清理很直接，
    也不会出现两份文档的表结构冲突。
    """

    @abstractmethod
    def write_table(
        self, *, table: str, columns: Sequence[str], rows: Sequence[Sequence[str]]
    ) -> int:
        """写入（覆盖）一张表，返回写入行数。

        **覆盖而不是追加**：同一份文档重新摄入应当得到干净的副本。
        追加会让行数翻倍，而用户看到"这份表有两倍的行"时，
        很难联想到是自己点了一次重跑。
        """
        ...

    @abstractmethod
    def table_exists(self, table: str) -> bool: ...

    @abstractmethod
    def drop_table(self, table: str) -> None:
        """删表（删文档时调用）。表不存在不报错——那是幂等。"""
        ...

    @abstractmethod
    def columns(self, table: str) -> list[str]: ...

    @abstractmethod
    def row_count(self, table: str) -> int: ...

    @abstractmethod
    def read_rows(self, table: str, *, limit: int = 50, offset: int = 0) -> list[list[str]]:
        """按行列读一段，**按写入顺序返回**。

        DuckDB 不保证无 ``ORDER BY`` 时的行序，而"第 3 行"是用户能对照原文的说法，
        所以实现里另外记行号并据此排序。
        """
        ...

    @abstractmethod
    def list_tables(self) -> list[str]:
        """所有表格副本的表名（= ``document_id``），按名字排序。

        存在的理由：SQL 工具要先知道**有哪些表**才能把范围讲清楚
        （见 ``services/tabular_sql.check_tables``）。没有它，调用方只能逐个文档
        问一遍 ``table_exists``，而那是 N 次查询。
        """
        ...

    @abstractmethod
    def run_select(self, sql: str, *, max_rows: int) -> tuple[list[str], list[list[str]]]:
        """跑一条**已经校验过的**只读 SQL，返回 ``(列名, 行)``。

        **校验不在这里**（见 ``services/tabular_sql.validate_select``）：仓储是
        通用的存取层，把"允许什么样的 SQL"这条产品规则放进来，等于让它同时承担
        业务判断。这里只多做一件事——**把结果截到 ``max_rows``**：取回一百万行是
        资源问题，不该指望每个调用方都记得加 LIMIT。
        """
        ...
