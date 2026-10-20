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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # **只在类型检查时导入**：窄协议模块反过来要在运行时导入本模块的记录类型，
    # 真导入就成环。注解有 `from __future__ import annotations` 兜底，运行时不需要它。
    from app.storage.repositories import (
        ApiKeyRepo,
        ChunkRepo,
        DataSourceRepo,
        DocumentRepo,
        FolderRepo,
        IdempotencyRepo,
        IdentityRepo,
        ImageRepo,
        KnowledgeBaseRepo,
        MaintenanceRepo,
        ModelRegistryRepo,
        ParseResultRepo,
        SettingsRepo,
        ShareRepo,
        TaskQueueRepo,
        TrashRepo,
        UsageRepo,
        WebhookRepo,
        WikiRepo,
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
    "ApiKeyRecord",
    "ChunkRecord",
    "DataSourceRecord",
    "DocumentPartRecord",
    "DocumentRecord",
    "FullTextStore",
    "ImageRecord",
    "KnowledgeBaseRecord",
    "MetaStore",
    "ObjectStore",
    "ParseResultRecord",
    "SearchFilter",
    "SearchHit",
    "SessionRecord",
    "ShareRecord",
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

    # ---- 按域切开的窄视图（v0.2，见 storage/repositories.py）----
    #
    # 它们**返回的是同一个 ``meta`` 实例**，只是按域收窄了类型：新代码依赖窄接口，
    # "这个模块需要什么"在签名里读得出来；老代码走 `meta.*` 零改动。
    # 这是拆 MetaStore 的第一步，不是行为变更——19 个域与 151 个方法的归属
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


KB_UNAVAILABLE_MESSAGE = "知识库当前不可用：这个部署连不上它的数据源"
"""知识库那一侧连不上时的那句话（照实说，不伪装成"库里没有"）。

**为什么要说"不可用"而不是回空**：回空列表等于告诉调用方"查过了，没有"，
而它其实**没查过**——那是把一次故障伪装成一次结论。抛出它的仓储与
``app/core/exceptions.py`` 的处理器一起，把这件事折成 503 + 这句话。
"""


class KnowledgeBaseUnavailable(StorageError):
    """知识库的存储现在连不上：不是"没有这条数据"，而是"这一问没问到"。

    **如实抛，不伪装**：回空列表等于告诉调用方"查过了，没有"，而它其实**没查过**。
    HTTP 面由 ``app/core/exceptions.py`` 映射成 503 + ``str(exc)`` 那句话（不伪装成
    500"内部错误"：这是"这个部署现在没有这个能力"，不是"我们出错了"）。

    ``StorageError`` 子类而不是 ``KylabError`` 子类：它是存储层说出的事实（"这个仓储
    没有这个能力"），而"折成哪个状态码"属于协议层——存储层不认识 HTTP。

    **为什么住在接口层**：``services/`` 只允许 import ``app.storage.base``
    （工程规范 §3.3 L2，``scripts/check_layering.py`` 会拦），所以面向服务层说的这两句
    存储层事实（``StorageError`` 与它）必须同处一地——那正是"services 不必 import
    具体实现"这条承诺的另一半。
    """

    message = KB_UNAVAILABLE_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)


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
    allowed_tool_names: Sequence[str] = field(default_factory=tuple)
    """这把钥匙能调的工具（MCP 工具名）；**空元组 = 不受限**。

    与 ``knowledge_base_ids`` 同一口径：空表示"不限"，不是"一个都不许"——
    所以账号体系之前发的老钥匙（这一列不存在）行为与从前完全一致。
    只作用于**API Key 通道**：管理员与登录成员会话不看它（见 ``services/api_key.py``）。
    """
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
class SearchEventRecord:
    """一次检索里**某个知识库**贡献的命中（v0.61）。

    **一行 = 一次检索 × 一个库**，不是"一次检索"：`usage_events(kind='search')`
    只有一个整数（返回了几条）且没有库这一维，于是控制台画不出"这个库的检索命中趋势"
    （交接文档 §9 第 6 条记着这个缺口）。这张表补的正是那一维。

    ``candidates`` 与 ``kept`` 刻意**分开记**：

    - ``candidates`` 是这个库在**分布收敛之前**贡献的条数（``summarize`` 看到的那一批）；
    - ``kept`` 是收敛之后**剩下**的条数。**它不是"某一页返回的条数"**（v0.63）：一次检索
      可以翻页（``offset``），而同一次检索在趋势上只该记一份——照页数记的话，翻三页就是
      三倍，"这个库的命中在涨还是在跌"这个问题当场失效。

    两者之差就是"这次检索里这个库被砍掉了多少"——只记一个数的话，
    一个库从"给 12 条、留 2 条"退化成"给 2 条、留 2 条"，在曲线上看不出任何区别，
    而前者说明它的召回还行、只是被收敛剪掉了，后者说明它压根没捞到东西。

    ``fit`` 是这次检索整体的判定（``distribution.fit``：``strong`` / ``weak`` /
    ``none`` / ``unknown``）。它**是整批的性质，不是这个库的**（分布按整次检索算），
    所以同一批记录里这一列相同；存下来是为了曲线之外还能回答"这几天有多少次
    是「资料里没有」"这种问题。
    """

    id: str
    kb_id: str
    candidates: int = 0
    kept: int = 0
    fit: str = "unknown"
    created_at: datetime | None = None


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
    def delete_parse_result(self, document_id: str) -> int:
        """删掉这个文档的**全部**解析产物，返回删掉的行数。

        "强制重新解析"要先把它清掉：`_reuse_parse_result` 的判据就是"这行在不在"，
        留着这行，重解析入队之后会被当成"解析已完成"直接跳到切分——
        也就是用户点了"重新解析"却什么都没重新解析。

        **是删除而不是标记**：那行只是解析器的输出缓存（正文在对象存储里，
        对象存储那份同 key 覆写），没有需要保留的历史。
        """

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
    def claim_legacy_ownership(self, owner_id: str) -> int:
        """把 owner 为 NULL 的知识库认领给指定账号（setup 向导用），返回认领条数。

        认领了几条要让用户知道，静默改掉一堆数据的归属而不吭声是不行的。
        （原先还会认领 `conversations`——那张表是 Agent 侧的，随分家一起删了。）
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
    def record_search_events(self, records: Sequence[SearchEventRecord]) -> None:
        """批量写一次检索的**按库命中记录**（一库一行，v0.61）。

        一次检索会同时落好几个库的行，所以整批一起写：逐行写等于一次检索多几次往返，
        而它们本来就是同一件事的几行。**空列表直接返回**——"没有可以归因的库"那一档
        （不指定库的全库检索、且一条都没命中）由调用方决定不写，存储层照办。
        """
        ...

    @abstractmethod
    def list_search_events(self, *, since: datetime | None = None) -> list[SearchEventRecord]:
        """取某时间点之后的检索命中记录（控制台的趋势曲线用）。

        **不做 SQL 聚合**，与 ``list_usage`` 同一条纪律：曲线按**本地日历日**分桶，
        而"日"是业务口径（见 ``services/search_stats.py``），压在 SQL 里就得把时区
        偏移传下去。本地部署的数据量（一次检索几行、一天几百行）全读回来聚合完全够用。
        """
        ...

    @abstractmethod
    def purge_search_events_before(self, before: datetime) -> int:
        """清掉某时间点之前的检索命中记录。返回删了几行。

        **保留期不跟用量表共用**：控制台能看的窗口上限是 365 天（``stats.MAX_WINDOW_DAYS``），
        而用量只留 180 天——照用量那套清会让最后半年的曲线整段变空，
        且是"慢慢变空"，看起来像没人检索。见 ``services/search_stats.py`` 的保留期常量。
        """
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


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """**下推到存储层**的检索过滤条件：块自身的元数据 + 它所属文档的元数据。

    **为什么要在存储层另立一份，而不是收 ``services`` 那侧的 ``MetadataFilter``**：
    分层纪律（工程规范 §3.3）规定 ``storage/`` 不得依赖 ``services/``；而过滤又偏偏
    只能在这里做才有意义——"只查某本书某章"必须在**候选生成之前**裁剪，等两条通道
    各自按 ``top_k`` 取完再筛，那本书的块根本没机会进候选（见
    :meth:`VectorStore.search` 的说明）。所以条件在存储这一侧重新声明一遍，
    由 ``services/retrieval/service.py`` 翻译过来，字段与 ``MetadataFilter`` 一一对应。

    各项的取值口径与 ``MetadataFilter`` 完全一致；``None`` / 空序列 = 这一项不设限。

    **没有页码或标题路径的块会被对应的条件整体排除**（``chunk.page`` /
    ``chunk.heading_path`` 为 ``NULL`` 时 SQL 比较结果是 ``NULL``、不是真，
    于是既不满足"页码在 40–60 之间"也不满足"标题含某某"）。这是有意的：给了
    ``page_from`` 就是"我只要那一段页码"，把"页码未知"的块混进来只会让调用方
    在原文里读到别处的内容。反过来，``.md`` / Office 这类本来就没有页码的来源，
    一旦让用户按页码筛，就是**一整批都查不到**——这一点必须在对外说明里写清。
    """

    document_ids: Sequence[str] | None = None
    source_kinds: Sequence[DataSourceKind] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    page_from: int | None = None
    page_to: int | None = None
    heading_contains: str | None = None

    def is_empty(self) -> bool:
        """一件要筛的事都没有——调用方据此完全跳过过滤（连 JOIN 都不加）。

        页码那两项判的是 ``is not None`` 而不是真假：``page_from = 0`` 是一个
        有意义的条件（"只要有页码的块"），按真假判会把它当成"没给"。
        """
        return not (
            self.document_ids
            or self.source_kinds
            or self.created_after is not None
            or self.created_before is not None
            or self.page_from is not None
            or self.page_to is not None
            or self.heading_contains
        )

    @property
    def needs_documents(self) -> bool:
        """这批条件里有没有落在 ``documents`` 表上的（决定要不要 JOIN 它）。

        单独一位是因为两个仓储的 JOIN 成本不同：全文那侧本来就已经 JOIN 了
        ``documents``（为裁掉停用的文档），而向量分区表里只有 ``chunk_id`` 与
        ``embedding``——不必要的那一次 JOIN 能省就省。
        """
        return bool(
            self.source_kinds or self.created_after is not None or self.created_before is not None
        )


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
        self,
        kb_id: str,
        *,
        query_vector: Sequence[float],
        top_k: int,
        filter: SearchFilter | None = None,
    ) -> list[VectorMatch]:
        """最近的 ``top_k`` 个向量。

        ``filter`` **在 SQL 里就裁掉不合条件的候选**，而不是取完 ``top_k`` 再由调用方
        一条条丢——两者的差别就是"只查某本书"能不能用：事后筛时 ``top_k`` 个名额被
        全库的块先占满，那本书往往一条都不剩。这是本参数存在的全部理由。

        带 ``filter`` 的实现必须**同时**满足两件事：结果属于过滤后的集合，
        以及条数仍按过滤后的集合取满 ``top_k``（而不是"取满全库的 top_k 再从里面挑"）。

        ``filter`` 为 ``None`` 或 ``is_empty()`` 时行为与从前**逐字一致**。
        """

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
    def search(
        self,
        *,
        query: str,
        top_k: int,
        kb_id: str | None = None,
        filter: SearchFilter | None = None,
    ) -> list[SearchHit]:
        """``filter`` 与 :meth:`VectorStore.search` 的那一位**同名同义**：在 SQL 里裁候选。

        两条通道必须筛得一模一样，否则会出现"全文搜不到、向量搜得到"这类静默不一致
        （与"文档级停用要在 SQL 里裁"是同一条纪律）。
        """


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
