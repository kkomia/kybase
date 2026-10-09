"""服务装配（组合根的服务侧）。

与 `app/core/storage.py` 同构：这里是**唯一**把"接口"和"具体服务实现"接起来的地方，
API 层通过依赖注入拿到服务，自己不 new 任何东西。

解析器注册顺序即优先级，且**必须由本模块决定**：`parsers/` 内部禁止互相 import，
"谁先试"这种跨实现的决策只能放在 services/（工程规范 §3.3 L3）。

当前顺序：纯文本直通 → MinerU 云端 → PaddleOCR 云端。
前两个都靠 ``supports()`` 自己排除纯文本类文件，所以顺序不会误伤本地直读。
云端解析器在未配置 token 时 ``supports()`` 恒为 False，因此**没配凭据也能跑通整条链路**。

## 两个根（2026-10-08 剥离阶段 1 第 3 步）

`Services` 原来把 KB 与 Agent 的服务混装在一张图上（方案 §4 第 1 条，57 个字段）。
现在按域拆成两个数据类：

- `KbServices`：KB 域服务（含账号体系实现——已拍板随 KB）+ 它需要的共享底座件；
- `Services`：Agent 域服务 + 它需要的共享底座件。

**两个根共用同一批对象**：共享底座（`runtime` / `models` / `secrets` / `usage` …）
在两个根上各是一个字段、指向**同一个实例**，所以不存在"两份运行期配置"这种分叉。

### 过渡期的四件粘连（都在这里说清，免得下一个人以为是漏了）

1. **装配还是一条链**：`_build_graph()` 仍是原来那一串线性装配——**顺序与副作用点
   一字未动**（`platform_store()` / `build_stores()` / `backup_queue.start()` /
   `ensure_url_signing_secret()` / 消费者线程都在原位置），只在末尾把结果分装进两个根。
   所以 `build_kb_services()` 现在也会把 Agent 那半边一起建出来，**它还不是一个独立
   的构建函数**——§6 阶段 1 第 4/5 步（`chat.py` / `tools.py` 对切）之后才会独立。
2. **混合体 `chat`**：`services/chat.py` 一半是 KB 检索、一半是 Agent 工具循环
   （§4 第 2 条），两个根都持有同一个实例——对切前它归不了任何一侧。
3. **接缝 `provider`**：`KnowledgeProviderClient` 是 Agent 侧访问 KB 的唯一通道
   （§2 事实 2），两个根都持有它。
4. **Agent 根上的 `kb`**：过渡期的组合访问器，见那个字段的说明。

### 依赖注入

- KB 侧的模块（`api/v1/` 下那 19 个 + `api/auth.py` + `mcp_server/`）注入 **`KbServices`**，
  依赖 `get_kb_services()`；
- Agent 侧与共享侧的模块注入 **`Services`**，依赖 `get_services()`；
- **进程里只有一份图**：`get_kb_services()` 返回的就是 `get_services().kb`
  （不二次装配，否则会出现两个 `StoreBundle`、两个补传线程、两份运行期配置）。
"""


from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.storage import build_stores
from app.services.api_key import ApiKeyService
from app.services.approvals import ApprovalRegistry
from app.services.artifacts import ArtifactService
from app.services.auth import AuthService, ensure_url_signing_secret
from app.services.avatars import AvatarService
from app.services.backup_provider import BackupProviderClient
from app.services.backup_queue import BackupQueueService
from app.services.backup_restore import BackupRestorer
from app.services.backup_snapshot import BackupSnapshotService
from app.services.batch import DocumentBatchService
from app.services.chat import ChatService
from app.services.chunk import ChunkService
from app.services.commands import CommandService
from app.services.conversation import ConversationService
from app.services.conversation_export import ConversationExportService
from app.services.credentials import CredentialsService
from app.services.documents import DocumentService
from app.services.embedding import build_embedder
from app.services.embedding.base import EmbeddingProvider
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.embedding.resolver import EmbeddingResolver
from app.services.folder import FolderService
from app.services.idempotency import IdempotencyService
from app.services.ingest import IngestService
from app.services.kb_cache import CachedKnowledgeMetaReader, KbMetaCacheService
from app.services.kb_prompt import KBPromptService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.knowledge_provider import (
    EnqueueGateway,
    IngestGateway,
    KnowledgeProviderClient,
)
from app.services.legacy_import import LegacyImporter
from app.services.lifecycle import LifecycleService
from app.services.llm import LLMUsage
from app.services.maintenance import MaintenanceService
from app.services.mcp_client import MCPClientService
from app.services.memory import MemoryService
from app.services.model_registry import ModelRegistryService
from app.services.note_ai import NoteAiService
from app.services.notes import NotesService
from app.services.observability import ObservabilityService
from app.services.parser_router import ParserRouter
from app.services.plugins import PluginService
from app.services.retrieval import RetrievalService, build_reranker
from app.services.retrieval.rerank import RerankProvider
from app.services.runtime_config import RuntimeConfigService
from app.services.schedule_runner import run_scheduled_task
from app.services.schedules import ScheduleService
from app.services.secrets import NullSecretStore, SecretStore, platform_store
from app.services.share import ShareService
from app.services.skill_blurb import SkillBlurbService
from app.services.skill_market import SkillMarketService
from app.services.skill_sources import SkillSourceService
from app.services.skills import SkillService
from app.services.sources import SourceService
from app.services.stats import StatsService
from app.services.suggested_questions import SuggestedQuestionsService
from app.services.summary import DocumentSummaryService
from app.services.system_load import SystemLoadService
from app.services.tabular import TabularService
from app.services.usage import UsageService
from app.services.users import UserService
from app.services.webhook import WebhookService
from app.services.wiki import WikiService
from app.services.workspace import WorkspaceService
from app.storage.base import StoreBundle
from app.workers.queue_worker import TaskWorker

__all__ = [
    "KbServices",
    "Services",
    "build_kb_services",
    "build_services",
    "get_kb_services",
    "get_services",
    "reset_services",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KbServices:
    """KB 域服务（含账号体系实现）+ 它需要的共享底座件。"""

    knowledge_bases: KnowledgeBaseService

    documents: DocumentService | EnqueueGateway
    """文档服务：服务器档是 `DocumentService`（列表 / 进度 / 取消……），**本机档只有
    入队那一面**——而且是个空操作（远端 ``start=true`` 已经排上了，本机没有队列）。"""

    folders: FolderService
    """知识库内目录：建/列/改名/删，以及把文档放进目录（v13）。"""

    ingest: IngestService | IngestGateway
    """摄入：服务器档是整个 ``IngestService``，**本机档只有"提交一份字节"那一面**
    （提供者网关，见 `services/knowledge_provider.py` 的两个 Protocol）。"""

    retrieval: RetrievalService

    stats: StatsService
    """驾驶舱统计：把散在几张表里的数字聚合成仪表盘要的形状。"""

    api_keys: ApiKeyService
    """API Key 的发放、校验与作用域判定（架构 §3.2）。"""

    chunks: ChunkService
    """切块人工干预：改正文并重新向量化、禁用、删除（调研报告 G3）。"""

    users: UserService
    """使用者名册：记录"是谁传的"，不参与鉴权（调研报告 G6）。"""

    auth: AuthService
    """账号引导、登录与会话校验（v10：名册升级为账号体系）。"""

    avatars: AvatarService
    """用户头像（v0.29）：图片在对象存储、库里只留 key，链接走签名。"""

    shares: ShareService
    """知识库分享：owner 把库授给其他成员，读/写两档（v10）。"""

    lifecycle: LifecycleService
    """数据生命周期：影响清单、级联删除、回收站（M6 / T6.3、T6.4）。"""

    batch: DocumentBatchService
    """文档批量动作：多选后的删除 / 重新摄入，逐条返回成败。"""

    maintenance: MaintenanceService
    """存储维护：空间概览与"整理"（丢无主向量分区 + VACUUM，v17）。"""

    sources: SourceService
    """数据源：HTML / RSS 的登记与拉取（M6 / T6.1–T6.3）。"""

    observability: ObservabilityService
    """运行态判据：任务是否卡住、是否长时间没被领取（M7 / T7.4）。"""

    tabular: TabularService
    """表格结构化副本：读写 CSV/Excel 的行列（M2 / T2.11）。"""

    summaries: DocumentSummaryService
    """文档摘要（v25）：入库时生成，问答上下文与界面都用它。"""

    suggested_questions: SuggestedQuestionsService
    """示例问题：依据所选知识库的语料让对话模型生成开场问题（对话页空状态）。"""

    kb_prompt: KBPromptService
    """库级提示词生成（v0.19）：按库里的文档摘要让模型写一版提示词草稿。"""

    wiki: WikiService
    """知识库 Wiki：把已入库内容整理成带出处的百科式页面（v24）。"""

    webhooks: WebhookService
    """Webhook 订阅与事件推送（M4 / T4.6）。"""

    embedder: EmbeddingProvider

    reranker: RerankProvider

    worker: TaskWorker
    """消费者之一。**单消费者场景用它**（测试、只跑一条任务）。
    实际并发数看 ``workers``。"""

    runtime: RuntimeConfigService
    """运行期配置（凭据与模型）：设置页读写它，各 provider 每次调用现取快照。"""

    models: ModelRegistryService
    """模型注册器：供应商 → 模型目录 → 按用途绑定（调研报告 G1）。"""

    usage: UsageService
    """用量统计：按次记 token 与调用量（调研报告 G7）。"""

    idempotency: IdempotencyService
    """幂等键：上传类接口防重试造成重复入库（架构 §3.2）。"""

    load: SystemLoadService
    """负载面板数据源：CPU / 内存 / 队列深度 / 并发槽位 / 云端解析额度（§12.115）。"""

    chat: ChatService
    """快速检索对话：检索 + 提示词 + LLM，定位是让用户快速验证知识库。"""

    provider: KnowledgeProviderClient | None = None
    """**知识库提供者客户端**（M3 阶段 2 建、阶段 5 收成进程级那一个实例）。

    本机档：这一份就是**全进程唯一**的那一个（`ChatService` 的检索、笔记与产物的
    入库网关、`Services.ingest`/`documents`、`stores.meta.kb` 的 reader、边车
    `Clients` 的工具表门控、`/local/provider` 端点**全从它取**）——它身上那份 30s
    握手缓存因此也只有一个，改完地址之后各处看到的必然是同一个结论。

    服务器档：``None``。它的知识库就是它自己（进程内那套一位不变），没有"第二个
    东西可以问"（R11）。

    为什么带默认值：服务器档与手工构造 ``Services`` 的地方（脚本、测试）都不该被迫
    传一个不适用的对象；"没有它"本身就是一个合法状态，而不是配置漏项。
    """

    workers: list[TaskWorker] = field(default_factory=list)
    """进程里全部消费者（``KYLAB_WORKER_CONCURRENCY`` 个），应用启动时各起一个协程。

    默认空列表是为了让"手工构造 Services 的测试"不必挨个补参数——
    但它**必须包含 ``worker``**（见 ``build_services``）。"""

    secrets: SecretStore | None = None
    """**系统钥匙串**（M5 阶段 6）：凭据的家（本机档就是 Windows 凭据管理器）。

    两个服务从它取：``runtime``（收编过的那几个设置键）与 ``models``（供应商的 API Key）。
    这一位是**进程级那一个**对象——两处各建一个就会出现"设置页存进去了、聊天那边读不到"
    这类最难查的分叉（两边都"成功"了）。

    服务器档：``NullSecretStore``（R14）。那一档**没有**系统钥匙串，它库里那份凭据照旧
    是凭据的家；`secrets.use_keychain()` 为假，两个服务走原来的库路径。
    手工构造的 ``Services``（用例、脚本）留 ``None``——同样是"没有钥匙串"那一档。

    为什么带默认值：同 ``provider``——"没有它"是合法状态。
    """

    credentials: CredentialsService | None = None
    """**旧明文收编**（M5 阶段 6）：把库里的明文凭据搬进钥匙串的那个服务。

    它只有三个动作（``status`` / ``migrate`` / NAS 钥匙那三个方法），而且是**显式的**：
    没有任何调用点会顺手调 ``migrate``（不静默迁移，方案 §4.2）。

    两种档都建：钥匙串不可用时 ``status()`` 如实回 ``store: unavailable`` 且
    ``pending_migration: 0``——那种机器上"等着迁"这件事不存在（库就是凭据的家）。
    """


@dataclass(frozen=True, slots=True)
class Services:
    """Agent 域服务 + 它需要的共享底座件。"""

    conversations: ConversationService
    """对话留存：会话与消息的读写（§11.2）。"""

    artifacts: ArtifactService
    """会话产物（v0.26）：Agent 做出来的文件落在哪、什么时候进知识库。
    与"文档"分开：产物先是文件，进知识库是它的一个可选去向。"""

    conversation_export: ConversationExportService
    """会话导出（M2 阶段 5）：`GET /conversations/export` 背后那一段（六型 NDJSON 流）。
    **两个档位都有**：服务器档导给本机导入器用，本机档导自己那批（同一份契约）。"""

    legacy_import: LegacyImporter | None
    """旧会话导入（M2 阶段 5）：拉 NAS 的导出流、写本机库、记账、回滚。

    **只有本机档不是 None**（服务器档的会话就是权威，没有"从别的部署导进来"
    这条动作——而台账那两张表也只在服务器不存在的本机库里）。"""

    notes: NotesService
    """笔记：Markdown 事实源 + 加入知识库（v20）。"""

    note_ai: NoteAiService
    """笔记的 AI 排版 / 润色（v20.2）。单独依赖 LLM，保住 NotesService 的"无模型也能用"。"""

    memory: MemoryService

    workspaces: WorkspaceService

    skills: SkillService

    skill_market: SkillMarketService

    skill_sources: SkillSourceService
    """技能源（v0.27）：内置的 GitHub 仓库清单 + 自定义源，浏览/取文件。"""

    plugins: PluginService
    """插件包（v0.43）：插件 = 一个目录 + 一份 plugin.json，**目录即本地市场**。

    与 `mcp` 是两件事：MCP 是"连出去的外部服务"，插件是"磁盘上的能力包"
    （技能/命令/钩子/工具四类能力面，见 `docs/设计/插件与技能-v0.1.md`）。"""

    mcp: MCPClientService

    schedules: ScheduleService
    """定时任务（v0.33）：到点替用户跑一轮问答。

    执行体见 ``services/schedule_runner.py``。"""
    """长期记忆的门面（设计见 `docs/设计/记忆档案-设计-v0.1.md`）。

    一份四区档案（`PROFILE.md`）+ 变更流；写入三条路，其中隐式捕获默认关。"""

    runtime: RuntimeConfigService
    """运行期配置（凭据与模型）：设置页读写它，各 provider 每次调用现取快照。"""

    models: ModelRegistryService
    """模型注册器：供应商 → 模型目录 → 按用途绑定（调研报告 G1）。"""

    usage: UsageService
    """用量统计：按次记 token 与调用量（调研报告 G7）。"""

    idempotency: IdempotencyService
    """幂等键：上传类接口防重试造成重复入库（架构 §3.2）。"""

    load: SystemLoadService
    """负载面板数据源：CPU / 内存 / 队列深度 / 并发槽位 / 云端解析额度（§12.115）。"""

    chat: ChatService
    """快速检索对话：检索 + 提示词 + LLM，定位是让用户快速验证知识库。"""

    kb: KbServices
    """**过渡期的组合访问器**：Agent 侧今天还有若干处经组合根取 KB 服务
    （`services.kb.documents` / `kb.api_keys` / `kb.folders` / `kb.embedder` …，
    见 `docs/计划与记录/域间引用基线-v0.1.md` 的「组合根」一节）。

    **它是过渡形态而不是终态**：剥离线画完之后，那些访问要么改走 `provider` 的窄面、
    要么随调用点一起搬到 KB 侧，这一格随之删。为什么不给那批模块各加一个
    `Depends(get_kb_services)`：其中几个住在被工具循环反复调的函数里
    （`services/tools.py`、`services/agent_tools.py`），改签名要连带改它们的所有调用方，
    风险远大于收益。

    **方向是单向的**：`KbServices` 上**没有**这一格（KB 不依赖 Agent）。
    """

    commands: CommandService = field(default_factory=lambda: CommandService(Path("data")))
    """斜杠命令（v0.44，P1-2）：内置表 + ``commands/*.md`` 自定义命令。

    **默认值给的是"只认内置命令"的那一份**（空数据目录 + 仓库自带目录）：手工构造
    ``Services`` 的测试不必为它造一个目录，而内置那六条正是大多数用例要用到的。"""

    approvals: ApprovalRegistry = field(default_factory=ApprovalRegistry)
    """对话里的待确认登记表（v0.41，见 ``services/approvals.py``）。

    ``ask`` 档的工具调用挂在这里等用户点头：**执行器登记、工具循环等、端点交决定**，
    三方在不同线程上，所以它必须与那一轮对话共用同一份实例。
    ``get_services()`` 是进程级单例，于是"同一个进程里的那两个请求"天然看到同一张表。

    用 ``default_factory`` 而不是在组合根里 new 一遍：手工构造 Services 的地方
    （测试、脚本）不必为它加参数，而"每个 Services 自带一张空表"比"忘了传就崩"稳。
    """

    provider: KnowledgeProviderClient | None = None
    """**知识库提供者客户端**（M3 阶段 2 建、阶段 5 收成进程级那一个实例）。

    本机档：这一份就是**全进程唯一**的那一个（`ChatService` 的检索、笔记与产物的
    入库网关、`Services.ingest`/`documents`、`stores.meta.kb` 的 reader、边车
    `Clients` 的工具表门控、`/local/provider` 端点**全从它取**）——它身上那份 30s
    握手缓存因此也只有一个，改完地址之后各处看到的必然是同一个结论。

    服务器档：``None``。它的知识库就是它自己（进程内那套一位不变），没有"第二个
    东西可以问"（R11）。

    为什么带默认值：服务器档与手工构造 ``Services`` 的地方（脚本、测试）都不该被迫
    传一个不适用的对象；"没有它"本身就是一个合法状态，而不是配置漏项。
    """

    backup_snapshot: BackupSnapshotService | None = None
    """**本机快照打包服务**（M5 阶段 2 建，阶段 4 起接上端点与队列）。

    本机档：打一份擦洗过的、可搬到别的机器的包（库 + 记忆 + 选择性产物 → tar.gz）。
    它每次调用现取运行期配置（含不含工作区产物），所以设置页改完**不必重启**。

    服务器档：``None``。那一档的库就是它自己，没有"把自己打成一份便携的包"这条动作
    （``/local/backup`` 那一族端点也只在 ``local_router`` 上，见 ``api/v1/router.py``）。

    为什么带默认值：与 ``provider`` / ``kb_cache`` 同一条理由——"没有它"是一个合法状态，
    而不是配置漏项。
    """

    backup_provider: BackupProviderClient | None = None
    """**备份提供者客户端**（M5 阶段 4 建）：本机打 NAS ``/backup/*`` 的唯一出口。

    两个身份、同一个对象：① ``/local/backup`` 与 ``/local/backup/points`` 问它的状态与
    恢复点清单；② 它是**补传队列的上传者**（``SnapshotUploader`` 那份协议），
    ``upload`` 里的两次 PUT（先 blob 后 manifest）就是它发的。全进程只有这一份，
    所以那份 30s 握手缓存与恢复点列表缓存也只有一份——改完地址各处看到的必然是同一个结论。

    服务器档：``None``。那一档**自己就是**备份的目的地：它提供 ``/backup/*``
    （``api/v1/backup.py``），没有"往另一台 NAS 传快照"这条动作。

    为什么带默认值：同 ``provider``——"没有它"是合法状态。
    """

    backup_queue: BackupQueueService | None = None
    """**备份待传队列服务**（M5 阶段 3 建，阶段 4 起在组合根里 ``start()``）。

    它持有那一个 ``backup-upload`` 守护线程（断网入队、联网补传、退避、本地队列上限）与
    "该不该自动打一份"的判据；``/local/backup`` 的 ``backlog`` 段读它的 :meth:`backlog`，
    ``POST /local/backup/snapshots`` 走它的 ``enqueue``。

    **它的线程在 ``build_services`` 里启动一次**（``start()`` = 崩溃复位
    ``uploading → pending`` + 起线程），所以"这台进程负责补传"这件事只有一个起点——
    与 worker 线程由组合根拉起的口径一致。

    服务器档：``None``（那一档没有"排队往别处传"这条动作）。

    为什么带默认值：同 ``provider``——"没有它"是合法状态。
    """

    backup_restore: BackupRestorer | None = None
    """**按点恢复服务**（M5 阶段 5 建）：把 NAS 上一份恢复点落到本机的那条路。

    两个动作、一个对象：``plan()``（dry-run：会新建哪些会话 / 哪些跳过、为什么 /
    哪些产物不在包里 / 要重配几项凭据）与 ``restore()``（真恢复：打一份本地兜底 →
    走 M2 导入器写会话 → 记忆 / 设置 / 产物落位 → 报告）。

    **会话那条链一个字都不重写**：它拿一台走 ``SnapshotFileSource`` 的 ``LegacyImporter``
    干活，所以进度与回滚复用既有两个端点（``GET /local/import/{batch}`` 与
    ``POST /local/import/{batch}/rollback``），恢复特有的那三段报告并进同一行的
    ``counts_json``（``counts["restore"]``）。

    服务器档：``None``。那一档的会话就是权威，而"整库替换"是另一件事（方案 §3.4 末尾
    那条干净路径：停边车 → 挪 ``kylab.db*`` → 再恢复）。

    为什么带默认值：同 ``provider``——"没有它"是合法状态。
    """

    kb_cache: KbMetaCacheService | None = None
    """**知识库元数据快照服务**（M4 阶段 3 建；阶段 4 起 ``/local/kb-cache/*`` 用它）。

    本机档：这一份是**全进程唯一**的那一个——reader 面（``ChatService.kb_prompt`` 经
    ``stores.meta.kb``）与页面面（``/local/kb-cache/*`` 那族端点）读的是同一份快照、
    同一套排程（单飞 / 15s 最短间隔 / 60s 失败退避）。两个入口各建一个就会各排各的，
    而 R3 的"再验证风暴"正是那么来的。

    它身上没有第二份事实：真话永远在 NAS 上，删了只丢速度（快照严格可弃，v0.3 §5.3）。

    服务器档：``None``。那一档没有"抄一份 NAS 快照"这条动作——它的知识库就是它自己，
    页面读到的已经是权威数据，再留一份只会多出一份会说谎的副本。

    为什么带默认值：与 ``provider`` 同一条理由——"没有它"是一个合法状态，
    而不是配置漏项。
    """

    secrets: SecretStore | None = None
    """**系统钥匙串**（M5 阶段 6）：凭据的家（本机档就是 Windows 凭据管理器）。

    两个服务从它取：``runtime``（收编过的那几个设置键）与 ``models``（供应商的 API Key）。
    这一位是**进程级那一个**对象——两处各建一个就会出现"设置页存进去了、聊天那边读不到"
    这类最难查的分叉（两边都"成功"了）。

    服务器档：``NullSecretStore``（R14）。那一档**没有**系统钥匙串，它库里那份凭据照旧
    是凭据的家；`secrets.use_keychain()` 为假，两个服务走原来的库路径。
    手工构造的 ``Services``（用例、脚本）留 ``None``——同样是"没有钥匙串"那一档。

    为什么带默认值：同 ``provider``——"没有它"是合法状态。
    """

    credentials: CredentialsService | None = None
    """**旧明文收编**（M5 阶段 6）：把库里的明文凭据搬进钥匙串的那个服务。

    它只有三个动作（``status`` / ``migrate`` / NAS 钥匙那三个方法），而且是**显式的**：
    没有任何调用点会顺手调 ``migrate``（不静默迁移，方案 §4.2）。

    两种档都建：钥匙串不可用时 ``status()`` 如实回 ``store: unavailable`` 且
    ``pending_migration: 0``——那种机器上"等着迁"这件事不存在（库就是凭据的家）。
    """


class _RuntimeEmbedder(EmbeddingProvider):
    """把 `RuntimeConfigService` 包成 embedding provider。

    为什么要有这一层：用户在设置页改完模型应当**立刻生效**，而不是重启进程。
    所以这里不缓存实例，每次 ``embed``/``embed_query`` 都按当前配置现建一个——
    建对象本身是廉价的（真正昂贵的是 HTTP 调用）。

    **用量记在这一层是刻意的**（G7）：向量化的调用点很多（摄入的每个分片、
    每次检索的 query），逐个去记必然漏。包在 provider 外面就有唯一入口。
    向量化接口通常**不返回 usage**，所以这里的 token 是按字符数估的——
    因此 ``reported=False``，界面上会明确标注"这是估算"。

    ``model_id`` / ``dim`` 走**配置快照**而不是 ``current``：接口层要读它们做展示，
    没配模型时那里不该抛异常，而应如实回"未配置"。
    """

    def __init__(
        self, runtime: RuntimeConfigService, usage_recorder=None, *, dev_embedding: bool = False
    ) -> None:  # type: ignore[no-untyped-def]
        self._runtime = runtime
        self._usage_recorder = usage_recorder
        self._dev_embedding = dev_embedding

    def _record(self, texts: Sequence[str], started: float) -> None:
        if self._usage_recorder is None:
            return
        snapshot = self._runtime.embedding()
        # 中文里 1 token ≈ 1.5 个字符（各家分词不同，这是保守估计）。
        # 明确标成"未上报"，界面据此说明数字是估算的
        estimated = sum(max(1, len(text)) for text in texts) // 2
        self._usage_recorder(
            kind="embedding",
            provider=snapshot.base_url,
            model_id=self.model_id,
            # estimated=True：这是我们按字符数估的，不是接口报的。
            # 不标记的话统计页会把估算当实测展示（假精度比没数字更糟）
            usage=LLMUsage(prompt_tokens=estimated, estimated=True),
            items=len(texts),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @property
    def current(self) -> EmbeddingProvider:
        return build_embedder(self._runtime, dev_embedding=self._dev_embedding)

    @property
    def configured(self) -> bool:
        """是否真的配了嵌入模型。界面据此区分"没配"与"配了但坏了"。"""
        return self._runtime.embedding().is_configured

    @property
    def model_id(self) -> str:
        snapshot = self._runtime.embedding()
        if snapshot.is_configured:
            return snapshot.model_id
        return DeterministicEmbedder.model_id if self._dev_embedding else ""

    @property
    def dim(self) -> int:
        snapshot = self._runtime.embedding()
        if snapshot.dim:
            return snapshot.dim
        return 256 if self._dev_embedding else 0

    @property
    def is_development(self) -> bool:
        """只有在**没配模型但显式开了开发兜底**时才为真。"""
        return self._dev_embedding and not self._runtime.embedding().is_configured

    @property
    def supports_media(self) -> bool:
        """当前配置的实现能不能嵌图片 / 视频。

        **问的是"这一档配置"而不是自己**：这一层只是个转发壳，真正的能力在
        `build_embedder` 按协议建出来的那个实现身上（`EmbeddingProvider.supports_media`）。
        摄入与解析路由据此决定"媒体文件走不走媒体接口"。
        """
        return bool(self.current.supports_media)

    def embed(self, texts):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        vectors = self.current.embed(texts)
        self._record(texts, started)
        return vectors

    def embed_media(self, data: bytes) -> list[float]:
        """媒体向量：**转发给当前配置的实现**（不支持时它自己会报错说明原因）。

        用量**不在这里记**：`_record` 记的是文本条数与按字符数估的 token，
        对一份几 MB 的视频没有意义（会得出一个荒唐的"token 数"）。媒体那条路的
        耗时与成败在日志里可见——宁可少一行仪表，也不要一行假数字。
        """
        return self.current.embed_media(data)

    def embed_query(self, text: str) -> list[float]:
        """查询向量化。

        **单列一个方法而不是复用 embed**：向量化接口对"文档"与"查询"常常用不同的
        前缀/指令（bge、e5 这类模型很常见），走错一边会**静默降低召回**。
        用量上按一条算。
        """
        started = time.monotonic()
        vector = self.current.embed_query(text)
        self._record([text], started)
        return vector


class _RuntimeReranker(RerankProvider):
    """同上：rerank 也按当前配置现取。"""

    name = "runtime"

    def __init__(self, runtime: RuntimeConfigService) -> None:
        self._runtime = runtime

    @property
    def enabled(self) -> bool:
        return build_reranker(self._runtime).enabled

    def rerank(self, query: str, documents):  # type: ignore[no-untyped-def]
        return build_reranker(self._runtime).rerank(query, documents)


def _build_graph(
    settings: Settings | None = None, stores: StoreBundle | None = None
) -> tuple[KbServices, Services]:
    """一条链装配出两个根（装配顺序与副作用点见模块头「过渡期的四件粘连」）。"""

    resolved = settings or get_settings()
    bundle = stores or build_stores(resolved)

    # 系统钥匙串（M5 阶段 6，方案 §4.1 / §4.5）：**先建它**，因为下面两个服务都要从它取。
    #
    # 本机档用这台机器上真正的钥匙串（Windows 凭据管理器；别的平台是 Null），
    # **服务器档恒 NullSecretStore**（R14）：NAS 容器里没有系统钥匙串，它库里那份凭据
    # 照旧是凭据的家（`use_keychain` 为假 → 两个服务走原来的库路径）。
    # 这不是"漏了收编"，而是如实登记边界——容器里那份凭据的边界是 NAS 的访问控制。
    secrets: SecretStore = platform_store() if resolved.deployment == "local" else NullSecretStore()
    # 注册器先建、再交给 runtime：runtime 的快照要**优先取注册表里绑定的模型**，
    # 未绑定时才回退到设置页那套字段（叠加层，见 services/model_registry.py）
    registry = ModelRegistryService(bundle, secrets=secrets)
    runtime = RuntimeConfigService(bundle, resolved, registry=registry, secrets=secrets)
    # 旧明文收编（M5 阶段 6）：**显式动作**的服务，没有任何调用点会顺手调它
    # （不静默迁移，方案 §4.2 末段）。两种档都建：钥匙串不可用时它如实报
    # `store: unavailable` 且 `pending_migration: 0`（那种档里"等着迁"这件事不存在）。
    credentials = CredentialsService(bundle, secrets)
    # 记忆服务**先建**：ChatService 要拿它把长期记忆注入 system prompt。
    # 工作区放在数据目录下（见 services/memory.py 与设计文档 §2.2）：
    # 与其它数据一起备份/迁移，一个部署只有一处要备份
    # 工作区也要 data_dir：它要拦住「把数据目录当工作区」这种配置
    # （指向那里等于绕过账号隔离，见 services/workspace.py 的第三道校验）
    workspace_service = WorkspaceService(bundle, resolved.data_dir)
    # 技能：扫描仓库自带 skills/ 与数据目录 data/skills/（见 services/skills.py）
    # 技能的门控要读运行期配置（`requires.config`，见 services/skills.py）：
    # 把"读一个配置键"的能力注进去，而不是把整个 runtime 塞给技能服务——
    # 技能层只需要这一个动作，多了就说不清它到底依赖什么。
    # D23 之后多了一个"写"：单条技能的启停（`chat.disabled_skills`）存在运行期配置里。
    skill_service = SkillService(
        resolved.data_dir, config_value=runtime.get, config_set=runtime.set
    )
    # 技能市场（v0.16）：安装/卸载。**只写 data/skills/**——仓库自带的那份动不了
    skill_market_service = SkillMarketService(resolved.data_dir, skill_service)
    # 技能源（v0.27）：从 GitHub 仓库浏览技能。**出站只在这一层**——
    # 前端永远不直接打 GitHub（匿名配额 60 次/小时，一分钟就能打爆，见该模块说明）
    skill_source_service = SkillSourceService(resolved.data_dir, token=resolved.github_token or "")
    # 插件包（v0.43）：扫描数据目录 plugins/ 与仓库自带 plugins/（目录即本地市场）。
    # **状态写在 app_settings**（启停/屏蔽），插件目录只读——见 services/plugins.py
    plugins_service = PluginService(resolved.data_dir, bundle)
    # MCP 客户端（v0.15）：连外部 MCP 服务，是「插件能力」的落点
    mcp_service = MCPClientService(bundle)
    # 定时任务（v0.33）：只做"到点入队"，跑问答的那一步在 schedule_runner 里
    # （它要一整套 Services，而这里还没有那个对象——见下面那个"槽"）
    schedule_service = ScheduleService(bundle)
    # 用量服务要**先建**：下面的 embedder 回调闭包引用了它
    usage = UsageService(bundle)

    def _record_embed_usage(**kwargs: object) -> None:
        usage.record(**kwargs)  # type: ignore[arg-type]

    embedder = _RuntimeEmbedder(runtime, _record_embed_usage, dev_embedding=resolved.dev_embedding)
    reranker = _RuntimeReranker(runtime)
    # 嵌入模型是知识库属性（v11）：按库解析。显式选了注册模型就用它，
    # 否则回退到上面的全局 embedder——老库与"不挑模型"的库行为不变。
    # 批大小取运行期配置（取值函数而不是常量）：否则设置页那个项对这条路径是 no-op
    embedding_resolver = EmbeddingResolver(
        registry,
        fallback=embedder,
        batch_size=lambda: runtime.embedding().batch_size,
        # 协议也跟着运行期配置走：设置页切成 WeMM 之后，按库解析出来的那一条
        # 也立刻换成 WeMM 客户端（否则"按库选模型"这条主路径会绕过协议）
        protocol=lambda: runtime.embedding().protocol,
    )
    retrieval = RetrievalService(
        bundle,
        embedder=embedder,
        reranker=reranker,
        embedders=embedding_resolver,
        # 检索次数原先只在日志里：接了回调仪表盘才有"检索量 / 平均命中"可看
        usage_recorder=lambda **kwargs: usage.record(**kwargs),  # type: ignore[arg-type]
    )
    # 记忆服务：**内容全在本地**（`data/memory/` 下的 Markdown）。出网只有一个动作
    # ——隐式捕获在信号出现的那一轮问一次对话模型（`memory.capture`，**默认关**），
    # 而它用的是运行期绑定的那条模型通道（见 `MemoryService._ask_model`）。
    # 所以这个服务不需要嵌入能力，也不需要存储：构造它是纯本地的。
    memory_service = MemoryService(runtime, resolved.data_dir)
    # webhook 先建：下面的摄入与生命周期都通过回调向它发事件（T4.6）。
    # **用回调而不是直接依赖**：通知是旁路，它挂了不能让摄入卡住
    webhooks = WebhookService(bundle)

    # 对话的 token 用量通过回调记（G7）：ChatService 不该依赖统计服务，
    # 那会让"记不记账"变成它的必需前提
    def _record_chat_usage(**kwargs: object) -> None:
        usage.record(**kwargs)  # type: ignore[arg-type]

    # **对话与出题服务必须在摄入之前建**（v23）：摄入现在要依赖"分段出题"
    # （SuggestedQuestionsService → ChatService）。反过来建的话 IngestService
    # 只能拿到 None，"上传即出题"就永远不生效。
    conversations_service = ConversationService(bundle)

    # 斜杠命令（v0.44，P1-2）：扫数据目录与仓库自带的 `commands/`（放进来一个 md 文件
    # 就是一条命令）。`conversations` 只用来读"这条会话上一轮的档"——模式观测的基线
    # （见 services/commands.ModeWatch），进程内不重复读。
    #
    # `skills` 是"技能即命令"那一半（v0.45，调研 §3 第 4-5 条）：每个技能注册一条
    # `/<技能名> [任务]`，正文仍由 ChatService.skill_prompt 注入（只有一处实现）。
    # `skill_summaries` 给菜单那一行用中文简介，与能力页读的是同一份数据
    # （市场装的在安装清单里、仓库自带的在 SKILL.md 的 frontmatter 里）。
    def _skill_summaries() -> dict[str, str]:
        """技能的中文简介（``技能名 → 简介``）；读不出来就当没有。

        两个来源，与能力页（api/v1/skills.py 的 ``_out``）**同一份数据、同一套优先序**：
        市场装的技能在安装清单里（``data/installed.json``），仓库自带的写在
        ``SKILL.md`` 的 frontmatter 里（``SkillRecord.summary``）。**两边都有时以清单那份
        为准**（市场那份是为中文界面存的、更短更贴；frontmatter 那份常是英文），
        所以下面先收 frontmatter 那批、再用清单覆盖——顺序必须与 ``_out`` 的
        ``summary or record.summary`` 一致，反了就会出现"能力页一句、菜单里另一句"。

        坏掉只是菜单里少几行中文，不该让命令表跟着 500。
        """
        out: dict[str, str] = {}
        try:
            for record in skill_service.list():
                if record.summary:
                    out[record.name] = record.summary
        except Exception:  # 扫技能失败不影响命令表的其余部分
            logger.warning("读技能自带的中文简介失败", exc_info=True)
        try:
            records = skill_market_service.installed_records()
        except Exception:  # 清单坏了不影响命令表（只是少几行中文简介）
            return out
        for name, item in records.items():
            summary = str(item.get("summary") or "")
            if summary:
                out[name] = summary
        return out

    commands_service = CommandService(
        resolved.data_dir,
        conversations=conversations_service,
        skills=skill_service,
        skill_summaries=_skill_summaries,
    )
    # 知识库提供者（M3 阶段 3）：**本机档**打 NAS 知识库的唯一出口（握手 / 检索 / 入库 /
    # 元数据，见 `services/knowledge_provider.py`）。**服务器档恒为 None** —— 它的知识库
    # 就是它自己，进程内那套一位不变（R11）。
    #
    # **恒建，不看地址有没有值**（与旧的那一行 `RemoteKnowledgeClient` 不同）：地址与钥匙
    # 是**每次调用现取**的（`provider.target()` 现读运行期设置），所以"现在没配、设置页填上
    # 之后立刻生效"这条要成立，对象就得先在这儿。没配时它如实回 `unconfigured`，
    # 两个网关的 `submit` 抛 `KnowledgeBaseUnavailable`（既有 503 映射，R10）——
    # 而不是回一个假的空结果，也不是 500。
    #
    # **它就是进程级那一个实例**（M3 阶段 5 收编，见 `Services.provider` 字段说明）：
    # 阶段 2/3 时边车 `Clients` 另建了一份（各有一份 30s 握手缓存），现在全进程只有
    # 这一份——`/local/provider` 的判定源、边车工具表门控被它一起回答，于是不可能出现
    # "端点说 ready、工具表说不 ready"这种两处各答一半的局面。
    provider = (
        KnowledgeProviderClient(settings=resolved, get_setting=runtime.get)
        if resolved.deployment == "local"
        else None
    )
    # **知识库元数据快照（M4 阶段 3）**：页面面与 reader 面共用的那一份服务。
    #
    # 建在**这里**、不建在别处——三件事只有这一层同时具备：① ``bundle.kb_cache`` 是本机
    # 那张缓存表（服务器档恒 ``None``，见 ``StoreBundle.kb_cache``）；② 键空间的第一列是
    # **提供者地址**，而地址是每次现取的能力（``provider.target()`` 现读运行期设置），
    # 那份能力只有服务层有；③ 它得是**全进程唯一的一个**（reader 面与 ``/local/kb-cache/*``
    # 共用同一套排程，见 ``Services.kb_cache`` 字段说明），所以与 ``provider`` 一样挂在
    # ``Services`` 上由端点去取。服务器档恒 ``None``：那一档没有这条动作。
    kb_cache: KbMetaCacheService | None = None
    if provider is not None:
        # 地址**每次现取**：`provider_key` 是个闭包而不是装配那一刻算出来的字符串——
        # 设置页改完地址，下一次读/写/清立刻落在新的那片键空间上（§5）。
        # 起个非空的名字只给下面那个闭包用（这一支里它一定有值）。
        local_provider = provider
        kb_cache = KbMetaCacheService(
            bundle.kb_cache, provider_key=lambda: local_provider.target().base_url
        )
        # **KB 侧那一条读线的装配就是这一处**（M3 阶段 4 后挂、M4 阶段 3 换成缓存包装）。
        # 把 reader 挂到本机档 `stores.meta.kb` 上（`_Router.kb` 是公开属性，
        # `split_impl/router.py`）。
        #
        # 为什么是后挂、不是构造参数：① **层序**——`core/storage.py` 先于本文件跑
        # （上面那行 `bundle = stores or build_stores(resolved)`），reader 那时还没出生；
        # ② **运行期配置**——reader 要的是"能随设置改地址"（`provider.target()` 每次
        # 现取），那份能力只有服务层有。这一处注入**只在装配期发生一次**，也不动
        # `split_impl` 的"路由表构造时定下"那条纪律：注入的是 KB 侧对象内部的引用，
        # 路由一个字没变。守卫见 `tests/unit/services/test_client_seams.py`。
        #
        # **为什么缓存包在 reader 外面**（M4 阶段 3）：``stores.meta`` 与 ``services/``
        # 的调用点因此一个字不用改（M3 §2.4 的承诺），缓存只在对象图上多一个节点；
        # 而"页面面"读的是**同一个服务**的另一面，两个读面共一份快照、一套排程。
        bundle.meta.kb.bind_reader(
            CachedKnowledgeMetaReader(inner=provider.knowledge_meta(), cache=kb_cache)
        )  # type: ignore[attr-defined]

    # **本机档的下载签名密钥**（这次修的 401）：本机档**没有任何初始化流程**
    # （它不挂 `/auth/*`，``AuthService.setup`` 那条路走不到），而它的 ``kylab.db``
    # 是全新的——不在装配时补这一下，``auth.url_signing_secret`` 就永远是空的，
    # 于是"下载签名"这条线上的每个端点都回 401，前端再把它当"登录失效"把人踢到
    # 登录页（桌面壳里实测到的那次：点产物卡片的「预览」）。
    #
    # 放在这里的三个理由：① 它只要 ``bundle.meta``，而这一层手上有完整的库；
    # ② 与"本机独有的那几件"同一处，读代码的人一眼看得到本机档补了哪些东西；
    # ③ 它是**幂等**的（见 ``services/auth.ensure_url_signing_secret``）——
    # 已有就不动，绝不每次启动换一把（换了的话已经发出去的链接会一起失效）。
    # **服务器档一个字都不动**：那一档的密钥来自 ``POST /auth/setup``。
    if resolved.deployment == "local":
        ensure_url_signing_secret(bundle.meta)

    # **备份那三件**（M5 阶段 4）：本机档才建，服务器档三个都是 None。
    #
    # 三件的分工是一条直线（与 M4 那条"提供者 + 缓存"同构）：
    #   BackupSnapshotService（打包：库 + 记忆 + 选择性产物 → 擦洗过的 tar.gz）
    #     → BackupQueueService（排队与补传：退避、上限、那一个 backup-upload 线程）
    #       → BackupProviderClient（上传者：先 blob 后 manifest 两次 PUT）
    # 而 `backup_provider` **同时**是 `/local/backup` 的状态源与恢复点清单源（两个读面、
    # 一个对象、一份 30s 缓存）。三件都在这一层建的理由与 `provider` / `kb_cache` 一样：
    # 它们要的是"能随设置改地址/改开关"的运行期能力，而那份能力只有服务层有。
    backup_snapshot: BackupSnapshotService | None = None
    backup_provider: BackupProviderClient | None = None
    backup_queue: BackupQueueService | None = None
    backup_restore: BackupRestorer | None = None
    if resolved.deployment == "local":
        # 设备身份从引导级来（壳的 `--device-id` → `KYLAB_DEVICE_ID` → Settings）：
        # **没有就是没有**（R12：`create()` 会如实拒绝打快照，绝不编一个 id）。
        backup_snapshot = BackupSnapshotService(
            stores=bundle,
            data_dir=resolved.data_dir,
            device_id=resolved.device_id,
            runtime_config=runtime,
        )
        backup_provider = BackupProviderClient(settings=resolved, get_setting=runtime.get)
        backup_queue = BackupQueueService(
            stores=bundle,
            data_dir=resolved.data_dir,
            uploader=backup_provider,
            snapshotter=backup_snapshot,
            runtime_config=runtime,
        )
        # **线程与崩溃复位的唯一起点**（阶段 3 定的纪律：`start()` = 复位
        # `uploading → pending` + 起那一个 `backup-upload` 线程）。放在组合根而不是
        # 放进某个端点：被谁先问到不该决定"这台机器有没有在补传"。
        backup_queue.start()
        # 按点恢复（阶段 5）：它读的是**同一个** `bundle`（快照读面 + 导入台账），
        # 打本地兜底用的是上面那一个打包器——三件共用一份对象，不各建各的。
        # 服务器档是 None：那一档没有台账，`BackupRestorer.__init__` 也会如实拒。
        backup_restore = BackupRestorer(
            stores=bundle,
            data_dir=resolved.data_dir,
            provider=backup_provider,
            snapshotter=backup_snapshot,
            runtime_config=runtime,
        )

    chat_service = ChatService(
        retrieval,
        runtime,
        # stores 用于"小块检索、大块阅读"（把命中块补成整段小节，v17）
        stores=bundle,
        usage_recorder=_record_chat_usage,
        # 会话读写（v20.1）：上下文压缩要读历史、写摘要
        conversations=conversations_service,
        # 长期记忆（v0.14）：非空时把 MEMORY.md / SOUL.md 注入 system prompt
        memory=memory_service,
        # 技能（v0.15）：把技能目录（名字 + 何时用）注入 system prompt，
        # 正文由 `use_skill` 按需展开——见 services/skills.py 的模块头
        skills=skill_service,
        # KB 检索（M2 §2.2 / M3 阶段 3）：非空时整段委托。本机档这一头**就是提供者客户端**
        # （每次调用现取地址与钥匙）；服务器档是 None，走进程内检索。
        knowledge=provider,
    )
    # 技能源的中文化（v0.28）：浏览器里那一屏是给中文用户看的，而技能描述基本都是英文。
    # 在这里接上而不是在源服务里 new：源服务只认识一个"翻译函数"，
    # 不该认识 ChatService（测试里注入一个 lambda 就够）。
    skill_source_service.use_translator(SkillBlurbService(chat_service))
    questions_service = SuggestedQuestionsService(
        bundle, chat_service, batch_concurrency=resolved.questions_concurrency
    )
    summary_service = DocumentSummaryService(
        bundle,
        chat_service,
        # 开关走运行期配置（设置页可改）。默认开：它是一次性成本换每轮问答的
        # token 节省，关掉只会让问答更贵（见 services/summary.py 的说明）
        enabled=lambda: runtime.get("ingest.summary_enabled").lower() not in ("0", "false", "no"),
    )
    # Wiki 生成（v24）：规划主题 + 逐页写作，资料直接复用上面的混合检索
    wiki_service = WikiService(bundle, chat=chat_service, retrieval=retrieval)
    kb_prompt_service = KBPromptService(bundle, chat_service)

    ingest = IngestService(
        bundle,
        # 路由按运行期配置现建解析器：设置页填完 token，下一个文件就走云端
        router=ParserRouter(runtime),
        embedder=embedder,
        embedders=embedding_resolver,
        notifier=webhooks.emit,
        # 分段出题（v23）：库上开着才用，失败不影响摄入
        questions=questions_service,
        # 文档摘要（v25）：入库时每篇一次调用，供问答上下文省 token
        summaries=summary_service,
    )

    # 可观测性服务**先建**：文档列表的进度条要判"停滞"，而那个判据（租约还在不在续）
    # 与任务中心那一列必须是同一个结论，所以两处共用这一个实例
    observability = ObservabilityService(bundle, worker_lease_seconds=resolved.worker_lease_seconds)
    documents_service = DocumentService(bundle, observability=observability)

    # **入库那两个接缝的换线，就是这一处**（M3 阶段 3，方案 §5.1「组合根一处」）：
    #
    # 本机档的"知识库"在 NAS 上，所以"提交一份字节"与"入队"都归提供者客户端；服务器档
    # 原样是这两个真服务。**三个构造点共用同一对对象**：产物服务、笔记服务（更下面）、
    # `Services.ingest` / `Services.documents`（`ingest_file`、文件入库那两个端点走它）。
    #
    # 为什么必须在这里换而不是 `dataclasses.replace(Services)`：笔记与产物**各自持有**
    # 一份真 `IngestService`（发现②），`replace` 只换得到 `Services` 上那两个槽位，
    # 换不到它们对象内部的引用——那正是 `attach_note_to_kb` 一直 503 的真机制。
    # 换完的守卫用例在 `tests/unit/services/test_client_seams.py`（"没有任何接缝还指向
    # 真 `IngestService`"，R6）。
    ingest_for_kb: IngestService | IngestGateway = ingest
    enqueue_documents: DocumentService | EnqueueGateway = documents_service
    if provider is not None:
        ingest_for_kb = provider.ingest_gateway()
        # 空操作：上传口带 `start=true`，NAS 那边自己入队了（本机没有队列可管）。
        # 理由与实现都在 `knowledge_provider._EnqueueGateway` 上，不在这里再抄一份。
        enqueue_documents = provider.enqueue_gateway()

    # 产物服务（v0.26）在建在这里：它要用摄入链路（复制一份进知识库）与文档服务
    # （入库后排队解析），而这两样都在上面就绪了。
    artifacts_service = ArtifactService(bundle, ingest=ingest_for_kb, documents=enqueue_documents)
    # 数据源要往摄入队列里塞任务，所以依赖 DocumentService（入队）与
    # IngestService（登记）两者——它们分工不同，见 services/sources.py
    # **这两个仍是真服务**：数据源与消费队列都是服务器那侧的家当（本机档里
    # `data_sources` 那族端点没挂，见 `api/v1/router.py` 的 local_router 白名单）。
    sources_service = SourceService(bundle, ingest, documents_service)

    # 会话导出（M2 阶段 5）：`GET /conversations/export` 背后那一段。**两个档位都建**：
    # 导出是服务器那侧的能力（NAS 上的会话导给本机），而本机档导自己那批也走同一份契约。
    export_service = ConversationExportService(
        bundle, conversations=conversations_service, artifacts=artifacts_service
    )
    # 旧会话导入（M2 阶段 5）：**只有本机档有台账**（`bundle.ledger` 是服务器档的 None，
    # 见 base.StoreBundle.ledger）。来源与令牌从这一档的引导配置来（`KYLAB_SERVER_URL` /
    # `KYLAB_TOKEN`，壳起边车时传的就是它们）——**令牌不经过 HTTP 请求体**，
    # 也就没有第二条让它进日志/落库的路。
    legacy_importer = (
        None
        if bundle.ledger is None
        else LegacyImporter(
            bundle,
            data_dir=resolved.data_dir,
            source=(resolved.server_url or "").rstrip("/"),
            token=resolved.token or "",
        )
    )

    idempotency = IdempotencyService(bundle)

    def _maintain() -> None:
        """空闲维护：把"会悄悄长大的表"收一收。

        两件事都是**本来就没有调用者**的承诺——``purge_expired_idempotency_keys`` 与
        ``purge_expired_trash`` 写在存储层很久了，但从没人调，于是"保留 7 天"
        和"键不会无限增长"实际上都没发生。挂在 worker 的空闲分支上让它真的跑起来。

        顺序无所谓，但都吞异常：清理失败不该影响消费（worker 那边还会再兜一层）。
        """
        removed_keys = idempotency.purge_expired()
        if removed_keys:
            logger.info("清理过期幂等键 %d 条", removed_keys)
        usage.purge_expired()
        # 任务与阶段事件这两张表只增不减，而它们都在热路径上（列表、队列概览、
        # 每行的进度条都会读）。保留期见 services/maintenance.py（§12.116）。
        MaintenanceService(bundle).prune_history()
        # 给还没有摘要的文档补摘要（v25）。挂在空闲分支上：它是"用空闲时间换
        # 后续每轮问答的 token"，一小批一小批地补，不需要用户点任何东西。
        try:
            summary_service.summarize_missing()
        except Exception:
            logger.warning("补生成文档摘要失败，跳过本轮", exc_info=True)
        # 走 lifecycle 而不是直接调存储层：它会**连磁盘上的原文一起删**。
        # 只删数据库行会把对象存储变成只增不减的垃圾场，
        # 而用户以为"7 天后就清掉了"
        lifecycle = LifecycleService(bundle)
        lifecycle.purge_expired_trash()

    lifecycle_service = LifecycleService(bundle, notifier=webhooks.emit)
    folders_service = FolderService(bundle)

    # 定时任务的执行体需要一个**装配好的 Services**（工具表、执行器、会话……都从它上面取），
    # 而 Services 要到这一行之下才存在。用一格可变的"槽"接住它：回调在应用起来之后
    # 才会被调用，那时槽里一定有值（不是懒加载的托词——这条链路上没有第二个时机）。
    runner_slot: list[Services] = []

    def _run_scheduled(scheduled_id: str) -> str:
        return run_scheduled_task(runner_slot[0], scheduled_id)

    workers = _build_workers(
        resolved.worker_concurrency,
        bundle=bundle,
        ingest=ingest,
        lease_seconds=resolved.worker_lease_seconds,
        maintain=_maintain,
        sources=sources_service,
        wiki=wiki_service,
        summaries=summary_service,
        schedules=schedule_service,
        run_scheduled=_run_scheduled,
    )

    # 两个根在这里分装：共享件、混合体 `chat` 与接缝 `provider` 在两边各是一个字段，
    # 指向的**是同一个实例**（见模块头「两个根」那一段）。
    kb = KbServices(
        knowledge_bases=KnowledgeBaseService(bundle, embedder=embedder, models=registry),
        documents=enqueue_documents,
        folders=folders_service,
        ingest=ingest_for_kb,
        retrieval=retrieval,
        chat=chat_service,
        stats=StatsService(bundle),
        runtime=runtime,
        api_keys=ApiKeyService(bundle),
        idempotency=idempotency,
        chunks=ChunkService(
            bundle,
            embedder=embedder,
            embedders=embedding_resolver,
            # 手工改块后要重出这一段的题（v23）
            questions=questions_service,
        ),
        models=registry,
        usage=usage,
        users=UserService(bundle),
        auth=AuthService(bundle),
        avatars=AvatarService(bundle),
        shares=ShareService(bundle),
        lifecycle=lifecycle_service,
        batch=DocumentBatchService(bundle, documents_service, lifecycle_service, folders_service),
        maintenance=MaintenanceService(bundle),
        tabular=TabularService(bundle),
        sources=sources_service,
        observability=observability,
        suggested_questions=questions_service,
        kb_prompt=kb_prompt_service,
        summaries=summary_service,
        wiki=wiki_service,
        webhooks=webhooks,
        embedder=embedder,
        reranker=reranker,
        worker=workers[0],
        workers=workers,
        load=SystemLoadService(
            bundle,
            concurrency=resolved.worker_concurrency,
            mineru_quota_pages=resolved.mineru_daily_page_quota,
            # "配没配 MinerU"问运行期配置（设置页可改），不能问启动期 Settings——
            # 用户填完令牌不重启就该生效
            mineru_configured=lambda: runtime.mineru().is_configured,
            observability=observability,
        ),
        provider=provider,
        # **进程级那一个快照服务**（M4 阶段 3，与它上面那一个同生共死）：`/local/kb-cache/*`
        # 那族端点从 `Services` 上取它；服务器档与 provider 一起是 None。
        secrets=secrets,
        credentials=credentials,
    )
    services = Services(
        chat=chat_service,
        runtime=runtime,
        idempotency=idempotency,
        models=registry,
        usage=usage,
        conversations=conversations_service,
        artifacts=artifacts_service,
        conversation_export=export_service,
        legacy_import=legacy_importer,
        notes=NotesService(bundle, ingest=ingest_for_kb, documents=enqueue_documents),
        note_ai=NoteAiService(chat_service),
        load=SystemLoadService(
            bundle,
            concurrency=resolved.worker_concurrency,
            mineru_quota_pages=resolved.mineru_daily_page_quota,
            # "配没配 MinerU"问运行期配置（设置页可改），不能问启动期 Settings——
            # 用户填完令牌不重启就该生效
            mineru_configured=lambda: runtime.mineru().is_configured,
            observability=observability,
        ),
        memory=memory_service,
        workspaces=workspace_service,
        skills=skill_service,
        skill_market=skill_market_service,
        skill_sources=skill_source_service,
        plugins=plugins_service,
        commands=commands_service,
        mcp=mcp_service,
        schedules=schedule_service,
        # **进程级那一个提供者实例**（M3 阶段 5）：上面建的 `provider` 直接挂在这里
        # （服务器档是 None）。挂它的理由与用途见字段说明——一句话是"让全进程只有
        # 一份握手缓存"，而 `/local/provider`（判定源）与边车的工具表门控都从它取。
        provider=provider,
        # **进程级那一个快照服务**（M4 阶段 3，与它上面那一个同生共死）：`/local/kb-cache/*`
        # 那族端点从 `Services` 上取它；服务器档与 provider 一起是 None。
        kb_cache=kb_cache,
        # **备份那三件**（M5 阶段 4）：本机档建（上面那一段），服务器档一起是 None。
        # `/local/backup` 那一族从它们取数；补传线程已经在上面起好了。
        backup_snapshot=backup_snapshot,
        backup_provider=backup_provider,
        backup_queue=backup_queue,
        backup_restore=backup_restore,
        # **钥匙串与收编**（M5 阶段 6）：上面建的那两份。`/local/secrets` 那两个端点
        # 从它们取数；两个服务（runtime / models）已经拿着同一个 `secrets` 对象了。
        secrets=secrets,
        credentials=credentials,
        kb=kb,
    )
    # 槽里放进刚装好的这一份：定时任务的执行体从这一刻起可用
    # （`_run_scheduled` 只在 worker 领到 SCHEDULED 任务时被调用，那时这里早已填上）
    runner_slot.append(services)
    if backup_queue is not None:
        # 登记它那一个**守护线程**（见 `_BACKUP_QUEUES` 的说明）：`reset_services`
        # 要把"这份服务图被扔掉了"这件事对线程也说到，否则它会继续碰旧的数据目录。
        _BACKUP_QUEUES.append(backup_queue)
    return kb, services


def build_kb_services(
    settings: Settings | None = None, stores: StoreBundle | None = None
) -> KbServices:
    """装配 **KB 侧的根**。

    **过渡期注意**：它现在也会把 Agent 那半边一起建出来（装配还是一条链，见模块头
    「过渡期的四件粘连」第 1 条）——混合体 `chat` 与队列消费者今天都在这条链上，
    砍掉它们就不是"行为不变"了。§6 阶段 1 第 4/5 步对切之后，这个函数才会真的只建
    KB 那半边。
    """
    return _build_graph(settings, stores)[0]


def build_services(
    settings: Settings | None = None, stores: StoreBundle | None = None
) -> Services:
    """装配 **Agent 侧的根**（含它要用的共享底座件与 KB 组合访问器）。

    与 `build_kb_services()` 共用同一条装配链，所以单独调它也会把 KB 那半边建出来
    （与今天的行为一致）。
    """
    return _build_graph(settings, stores)[1]


@lru_cache
def get_services() -> Services:
    """进程级单例，供 FastAPI 依赖注入使用（Agent 侧与共享侧的模块注入它）。"""
    return build_services()


def get_kb_services() -> KbServices:
    """KB 侧的根（供 KB 侧的模块注入）。

    **过渡期它就是 Agent 根上那一格**：进程里只有一份图，不二次装配——否则会出现
    两个 `StoreBundle`、两个补传线程、两份运行期配置。与 `get_services()` 一样是
    进程级单例（`reset_services()` 会一起清掉）。
    """
    return get_services().kb


def _build_workers(
    count: int,
    *,
    bundle: StoreBundle,
    ingest: IngestService,
    lease_seconds: int,
    maintain: Callable[[], None],
    sources: SourceService,
    wiki: WikiService,
    summaries: DocumentSummaryService,
    schedules: ScheduleService,
    run_scheduled: Callable[[str], object],
) -> list[TaskWorker]:
    """按 ``KYLAB_WORKER_CONCURRENCY`` 造 N 个消费者。

    **为什么是 N 个实例而不是给一个 worker 加并发**：任务表本身就是队列，
    ``claim_task`` 是原子单语句（``FOR UPDATE SKIP LOCKED``），多实例各自领各自的活
    就是天然的并发——给单实例加并发反而要自己实现"同时跑几个任务"的调度与心跳，
    等于把队列已经解决的事再做一遍。

    ``owner`` 必须**各不相同**：租约是按 owner 校验的，同名会让两个消费者互相
    认领对方的租约（``heartbeat_task`` 返回真、终态写入互相覆盖）。
    """
    if count <= 1:
        count = 1
    pid = os.getpid()
    return [
        TaskWorker(
            bundle,
            ingest,
            owner=f"worker-{pid}-{index}",
            lease_seconds=lease_seconds,
            maintain=maintain,
            # 数据源拉取没有 document_id，走 worker 里的独立分支（见 _handle_source）
            sync_source=sources.sync_now,
            # Wiki 重建同样是知识库级任务（见 _handle）
            compile_wiki=wiki.generate,
            # 补文档摘要（v25）：空闲时一小批一小批地补，不需要用户点任何东西
            summarize_gap=summaries.summarize_missing,
            # 定时任务（v0.33）：到点入队 + 到点执行。**两个回调分工明确**——
            # 调度侧只扫描与入队（快、独立循环），执行侧才跑问答（慢、在消费者线程里）
            due_schedules=schedules.enqueue_due,
            run_scheduled=run_scheduled,
        )
        for index in range(count)
    ]


_BACKUP_QUEUES: list[BackupQueueService] = []
"""本进程建过的待传队列（**只为让 `reset_services` 能停掉它们那一个线程**）。

为什么需要这一份登记：``backup-upload`` 是**守护线程**，它按节拍去碰本机库与
``backup/pending/``——而 ``reset_services()`` 的语义是"把这份进程级服务图扔掉"。
不登记的话，被扔掉的那一份会让线程接着跑：测试里表现为"tmp 目录都被清了它还在写"
（Windows 上就是一句 `PermissionError`），生产里则是"重装服务图之后还有旧线程在传"。
登记进来，`reset_services()` 就有东西可停（`stop()` 幂等，停一次就够）。
"""


def reset_services() -> None:
    for queue in _BACKUP_QUEUES:
        queue.stop()
    _BACKUP_QUEUES.clear()
    get_services.cache_clear()
