"""按域切开的仓储**窄协议**（strangler 的第二步：183 个方法全部切开）。

`MetaStore` 有 183 个方法、实现三千多行。问题不在行数，而在**接口本身**：
ABC 与实现一对一，于是任何消费者都只能依赖"什么都有的那个接口"——
"这个模块到底需要什么"在签名里读不出来，拆分也被接口锁死。

`StoreBundle` 把**同一个实例**按域再暴露一次：

    stores.documents.list_document_stats(...)   # 只依赖文档域
    stores.meta.get_setting(...)                # 老路径照旧，零改动

于是新代码可以依赖窄接口，老代码不动；等各域的调用点都迁过去，再谈拆实现。

**协议是结构化的**（``typing.Protocol``）：``PostgresMetaStore`` 天然满足它们，
不需要显式继承——这正是"渐进替换"能成立的前提。

**方法签名逐字取自** ``base.py`` 的 ``MetaStore``（先前手抄过一版，核对时抓出 16 处
形参与默认值不一致）。所以这里只写"为什么这么切"，参数语义与默认值以 ``base.py`` 为准。

**域怎么划的**：以 ABC 里的分节注释为底，修了三处——对话方法被误归在 MCP 节下
（拆到 `ConversationRepo`）、存储维护混在知识库节里（拆出 `MaintenanceRepo`）、
阶段事件的读与清理分归两个域（读在文档、清理在维护）。

``tests/unit/storage/test_repositories.py`` 机械核对三件事：**每个方法恰好属于一个协议**、
**183 个方法一个不漏**、**签名与 ABC 逐字一致**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.models.enums import DocumentStage, TaskState
from app.storage.base import (
    ApiKeyRecord,
    ChatMessageRecord,
    ChunkRecord,
    ConversationArtifactRecord,
    ConversationRecord,
    DataSourceRecord,
    DocumentPartRecord,
    DocumentRecord,
    DocumentStageEventRecord,
    DocumentStatRow,
    FolderRecord,
    IdempotencyRecord,
    ImageRecord,
    KnowledgeBaseRecord,
    MCPServerRecord,
    ModelProviderRecord,
    NoteFolderRecord,
    NoteRecord,
    ParseResultRecord,
    RegisteredModelRecord,
    ScheduledTaskRecord,
    SessionEventRecord,
    SessionRecord,
    ShareRecord,
    TaskCounts,
    TaskRecord,
    TaskStatRow,
    TrashRecord,
    UsageEventRecord,
    UserRecord,
    WebhookRecord,
    WikiPageRecord,
    WikiSourceRecord,
    WorkspaceRecord,
)

__all__ = [
    "ApiKeyRepo",
    "ChunkRepo",
    "ConversationRepo",
    "DataSourceRepo",
    "DocumentRepo",
    "FolderRepo",
    "IdempotencyRepo",
    "IdentityRepo",
    "ImageRepo",
    "KnowledgeBaseRepo",
    "MCPServerRepo",
    "MaintenanceRepo",
    "ModelRegistryRepo",
    "NoteRepo",
    "ParseResultRepo",
    "ScheduleRepo",
    "SettingsRepo",
    "ShareRepo",
    "TaskQueueRepo",
    "TrashRepo",
    "UsageRepo",
    "WebhookRepo",
    "WikiRepo",
    "WorkspaceRepo",
]


@runtime_checkable
class KnowledgeBaseRepo(Protocol):
    """知识库：库本体的增删改查与库级设置（切分参数、提示词、Wiki 开关、嵌入模型）。

    **库级设置也算这个域**：它们是「这个库怎么工作」的属性，与库记录同生共死。
    """

    def create_knowledge_base(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord: ...

    def get_knowledge_base(self, kb_id: str) -> KnowledgeBaseRecord | None: ...

    def list_knowledge_bases(self) -> list[KnowledgeBaseRecord]: ...

    def rename_knowledge_base(self, kb_id: str, name: str) -> None: ...

    def set_knowledge_base_chunking(self, kb_id: str, size: int, overlap: int) -> None: ...

    def set_knowledge_base_description(self, kb_id: str, description: str) -> None: ...

    def set_knowledge_base_suggested(
        self,
        kb_id: str,
        *,
        enabled: bool,
        count: int,
        model_pk: str | None,
        prompt: str,
    ) -> None: ...

    def set_knowledge_base_wiki(self, kb_id: str, *, enabled: bool) -> None: ...

    def set_knowledge_base_prompt(self, kb_id: str, *, prompt: str) -> None: ...

    def update_knowledge_base_embedding(
        self, kb_id: str, *, model_id: str, dim: int, base_url: str | None
    ) -> None: ...

    def delete_knowledge_base(self, kb_id: str) -> None: ...


@runtime_checkable
class DocumentRepo(Protocol):
    """文档域：文档本体、子文件（大文件切分）、阶段事件、按库的文档统计。

    子文件与阶段事件放在这里而不是各开一个协议：它们的生命周期**完全依附于文档**
    （`delete_document` 会级联删掉它们），拆开只会让"删一篇文档"要注入三次。
    `document_stats_by_kbs` 也在这里：它聚合的是**文档**（按库分组只是形状）。
    """

    def create_document(self, record: DocumentRecord) -> DocumentRecord: ...

    def get_document(self, document_id: str) -> DocumentRecord | None: ...

    def get_documents_by_ids(self, document_ids: Sequence[str]) -> dict[str, DocumentRecord]: ...

    def get_document_by_hash(self, kb_id: str, content_hash: str) -> DocumentRecord | None: ...

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
    ) -> list[DocumentRecord]: ...

    def count_documents(
        self,
        kb_id: str,
        *,
        folder_id: str | None = None,
        root_only: bool = False,
        q: str | None = None,
        stage: str | None = None,
        source_kind: str | None = None,
    ) -> int: ...

    def update_document_stage(
        self, document_id: str, stage: DocumentStage, *, error: str | None = None
    ) -> None: ...

    def replace_document_content(
        self,
        document_id: str,
        *,
        content_hash: str,
        name: str,
        size_bytes: int,
        mime_type: str | None,
    ) -> None: ...

    def update_document_page_count(self, document_id: str, page_count: int | None) -> None: ...

    def update_document_summary(self, document_id: str, summary: str) -> None: ...

    def list_documents_without_summary(self, *, limit: int) -> list[DocumentRecord]: ...

    def mark_document_split(self, document_id: str, is_split: bool = True) -> None: ...

    def delete_document(self, document_id: str) -> None: ...

    def rename_document(self, document_id: str, name: str) -> None: ...

    def set_document_disabled(self, document_id: str, disabled: bool) -> None: ...

    def any_disabled_documents(self, kb_ids: Sequence[str]) -> bool: ...

    def count_documents_by_folders(self, kb_id: str) -> dict[str, int]: ...

    def set_document_folder(self, document_id: str, folder_id: str | None) -> None: ...

    def document_stats_by_kbs(self) -> dict[str, tuple[int, datetime | None]]: ...

    def list_document_stats(self, kb_ids: Sequence[str] | None = None) -> list[DocumentStatRow]: ...

    def create_document_parts(self, records: Sequence[DocumentPartRecord]) -> None: ...

    def list_document_parts(self, document_id: str) -> list[DocumentPartRecord]: ...

    def delete_document_parts(self, document_id: str) -> None: ...

    def update_part_stage(
        self, part_id: str, stage: DocumentStage, *, error: str | None = None
    ) -> None: ...

    def list_document_stage_events(self, document_id: str) -> list[DocumentStageEventRecord]: ...

    def list_document_stage_events_for_documents(
        self, document_ids: Sequence[str]
    ) -> dict[str, list[DocumentStageEventRecord]]: ...

    def count_documents_by_user(self, user_id: str) -> int: ...


@runtime_checkable
class FolderRepo(Protocol):
    """目录域（v13）：单层目录的增删改查。

    **单层**是刻意的（理由见 `services/folder.py`）：父子嵌套会让"移动"/"删除"
    都变成子树操作，而知识库的目录只用来分组，不需要树。
    """

    def create_folder(self, record: FolderRecord) -> FolderRecord: ...

    def get_folder(self, folder_id: str) -> FolderRecord | None: ...

    def list_folders(self, kb_id: str) -> list[FolderRecord]: ...

    def rename_folder(self, folder_id: str, name: str) -> None: ...

    def delete_folder(self, folder_id: str) -> None: ...


@runtime_checkable
class NoteRepo(Protocol):
    r"""笔记域（v20 / 文件夹 v14）：笔记本体、计数、标签、文件夹，以及与文档的挂接。

    文件夹（`note_folders`）与"加入知识库"留下的那条边（`attach_note_document`）
    都在这里：它们读写的是同一批笔记行与它们的归属，拆到两个域只会让
    "移动笔记 / 改笔记"分成两处看。
    """

    def create_note(self, record: NoteRecord) -> NoteRecord: ...

    def get_note(self, note_id: str) -> NoteRecord | None: ...

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
    ) -> list[NoteRecord]: ...

    def count_notes(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        tag: str | None = None,
        folder_id: str | None = None,
        unfiled: bool = False,
    ) -> int: ...

    def update_note(
        self,
        note_id: str,
        *,
        title: str,
        content_md: str,
        pinned: bool,
        updated_at: datetime,
        tags: Sequence[str] | None = None,
    ) -> None: ...

    def delete_note(self, note_id: str) -> None: ...

    def attach_note_document(self, note_id: str, *, kb_id: str, doc_id: str) -> None: ...

    def list_note_tags(self, *, user_id: str | None) -> list[tuple[str, int]]: ...

    def create_note_folder(self, record: NoteFolderRecord) -> NoteFolderRecord: ...

    def get_note_folder(self, folder_id: str) -> NoteFolderRecord | None: ...

    def list_note_folders(self, *, user_id: str | None) -> list[NoteFolderRecord]: ...

    def rename_note_folder(self, folder_id: str, name: str) -> None: ...

    def set_note_folder_parent(self, folder_id: str, parent_id: str | None) -> None: ...

    def delete_note_folder(self, folder_id: str) -> None: ...

    def count_notes_by_folder(self, *, user_id: str | None) -> dict[str | None, int]: ...

    def set_note_folder(self, note_id: str, folder_id: str | None) -> None: ...


@runtime_checkable
class ChunkRepo(Protocol):
    r"""切块域：切块本体、按文档/小节/知识库的检索与计数、分段问题、人工干预。

    "按哪些维度查"是这个域的内聚点——文档列表要"每篇多少块"、补小节要"这一节的块"、
    检索要"这些 id 的块"，它们共享同一张表与同一套编号口径。
    人工干预（改/停用/删）也在这里：它改的就是同一张表，另开协议只会让
    \「检索读的块\」与\「用户改的块\」分成两处看。
    """

    def replace_chunks(self, document_id: str, chunks: Sequence[ChunkRecord]) -> None: ...

    def iter_chunks(
        self, document_id: str, *, limit: int | None = None
    ) -> Iterable[ChunkRecord]: ...

    def list_chunks_by_heading(self, document_id: str, heading_path: str) -> list[ChunkRecord]: ...

    def get_chunks(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]: ...

    def count_chunks(self, document_id: str) -> int: ...

    def count_chunks_by_documents(self, document_ids: Sequence[str]) -> dict[str, int]: ...

    def count_kb_chunks(self, kb_id: str) -> int: ...

    def sample_chunks(
        self, kb_ids: Sequence[str], *, limit: int, with_questions_only: bool = False
    ) -> list[ChunkRecord]: ...

    def update_chunk(self, record: ChunkRecord) -> None: ...

    def set_chunk_disabled(self, chunk_id: str, *, disabled: bool) -> None: ...

    def delete_chunk(self, chunk_id: str) -> None: ...

    def question_stats_by_documents(
        self, document_ids: Sequence[str]
    ) -> dict[str, tuple[int, int]]: ...

    def active_question_documents(self, document_ids: Sequence[str]) -> set[str]: ...


@runtime_checkable
class ImageRepo(Protocol):
    r"""图片域：解析产物里的图片与切块的关联。

    只有两个方法，但它的存在说明\「图片是独立对象\」——同一张图可能被多个切块引用。
    """

    def add_images(self, records: Sequence[ImageRecord]) -> None: ...

    def list_images(self, document_id: str) -> list[ImageRecord]: ...


@runtime_checkable
class ParseResultRepo(Protocol):
    r"""解析产物域：每个（文档, 解析器）的原始产物与页数用量。

    `parser_page_usage` 是\「这个月的云端解析额度花在哪\」的数据源，与产物同表。
    """

    def save_parse_result(self, record: ParseResultRecord) -> None: ...

    def get_parse_result(self, document_id: str) -> ParseResultRecord | None: ...

    def parser_page_usage(self, parser_name: str, *, since: datetime) -> tuple[int, int]: ...


@runtime_checkable
class TaskQueueRepo(Protocol):
    r"""任务队列域：入队、原子领取、续租、收尾、回收、计数。

    **它是一个域而不是"任务表的 CRUD"**：这十几个方法共同维持一条不变式——
    一条任务在同一时刻只被一个消费者持有（`claim_task` 的 `SKIP LOCKED` +
    租约续期 + 过期回收）。分开注入，就没有地方能保证\「领了必须续租\」被一起看见。
    """

    def enqueue_task(self, record: TaskRecord) -> TaskRecord: ...

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def list_tasks(self, state: TaskState | None = None) -> list[TaskRecord]: ...

    def list_task_stats(self, document_ids: Sequence[str] | None = None) -> list[TaskStatRow]: ...

    def claim_task(self, *, owner: str, lease_seconds: int) -> TaskRecord | None: ...

    def heartbeat_task(self, task_id: str, *, owner: str, lease_seconds: int) -> bool: ...

    def finish_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        owner: str,
        error: str | None = None,
    ) -> bool: ...

    def reschedule_task(
        self, task_id: str, *, owner: str, next_run_at: datetime, error: str | None
    ) -> bool: ...

    def cancel_tasks(self, task_ids: Sequence[str]) -> int: ...

    def cancel_tasks_for_document(self, document_id: str) -> int: ...

    def reclaim_expired_tasks(self, *, now: datetime | None = None) -> int: ...

    def purge_finished_tasks(self, *, before: datetime) -> int: ...

    def task_counts(self, *, now: datetime, overdue_before: datetime) -> TaskCounts: ...

    def active_tasks_by_documents(self, document_ids: Sequence[str]) -> dict[str, TaskRecord]: ...


@runtime_checkable
class DataSourceRepo(Protocol):
    """数据源域：订阅读取的登记、状态与拉取印记。

    `mark_data_source_pulled` 会同时写 ETag 与时间——条件 GET 的两个依据必须一起更新，
    所以它是一条方法而不是两条。
    """

    def create_data_source(self, record: DataSourceRecord) -> DataSourceRecord: ...

    def list_data_sources(self, kb_id: str) -> list[DataSourceRecord]: ...

    def get_data_source(self, source_id: str) -> DataSourceRecord | None: ...

    def list_all_data_sources(self) -> list[DataSourceRecord]: ...

    def update_data_source(self, record: DataSourceRecord) -> None: ...

    def delete_data_source(self, source_id: str) -> None: ...

    def mark_data_source_pulled(self, source_id: str, *, etag: str | None) -> None: ...


@runtime_checkable
class ApiKeyRepo(Protocol):
    """API Key 域：签发、按哈希查找、列举、吊销、触碰。

    **只按哈希查**（`get_api_key_by_hash`）：明文只在签发那一刻返回一次，库里没有它。
    """

    def create_api_key(self, record: ApiKeyRecord) -> ApiKeyRecord: ...

    def get_api_key_by_hash(self, key_hash: str) -> ApiKeyRecord | None: ...

    def list_api_keys(self) -> list[ApiKeyRecord]: ...

    def delete_api_key(self, key_id: str) -> None: ...

    def touch_api_key(self, key_id: str, *, used_at: datetime | None = None) -> None: ...


@runtime_checkable
class WebhookRepo(Protocol):
    """Webhook 域：订阅登记、开关与删除。

    与 API Key 分开：它们同属"对外投递"，但一个是入站鉴权、一个是出站通知，
    生命周期与权限面都不同。
    """

    def create_webhook(self, record: WebhookRecord) -> WebhookRecord: ...

    def list_webhooks(self) -> list[WebhookRecord]: ...

    def get_webhook(self, webhook_id: str) -> WebhookRecord | None: ...

    def set_webhook_enabled(self, webhook_id: str, enabled: bool) -> WebhookRecord | None: ...

    def delete_webhook(self, webhook_id: str) -> None: ...


@runtime_checkable
class IdempotencyRepo(Protocol):
    """幂等键域（架构 §3.2）：上传类接口的重试护栏。

    四个方法共用一条不变式：同一个键**先占位、后落响应**，重试时读回同一份结果。
    （`purge_finished_tasks` / `purge_stage_events` 不在这里：它们是留存清理，
    归各自的读侧与 `MaintenanceRepo`。）
    """

    def create_idempotency_key(self, record: IdempotencyRecord) -> IdempotencyRecord: ...

    def get_idempotency_key(self, key: str) -> IdempotencyRecord | None: ...

    def save_idempotent_response(self, key: str, response: dict[str, object]) -> None: ...

    def release_idempotency_key(self, key: str) -> None: ...

    def purge_expired_idempotency_keys(self, *, before: datetime) -> int: ...


@runtime_checkable
class ConversationRepo(Protocol):
    """对话留存域：会话、消息、置顶/归档/工作区归属、以及摘要与预览。

    **把一个会话的所有属性放在一起**：会话行上有七八个可改属性
    （模型、思考、置顶、归档、工作区、标题），分散注入会让"改会话"要凑三次依赖。
    `count_messages` / `count_workspace_conversations` 是它的两个聚合口径。
    """

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord: ...

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...

    def list_conversations(
        self, *, limit: int | None = None, q: str | None = None
    ) -> list[ConversationRecord]: ...

    def rename_conversation(self, conversation_id: str, title: str) -> None: ...

    def set_conversation_archived(self, conversation_id: str, archived: bool) -> None: ...

    def set_conversation_workspace(
        self, conversation_id: str, workspace_id: str | None
    ) -> None: ...

    def set_conversation_pinned(self, conversation_id: str, pinned: bool) -> None: ...

    def set_conversation_model(self, conversation_id: str, model_pk: str | None) -> None: ...

    def set_conversation_thinking(
        self, conversation_id: str, thinking: bool | None, effort: str | None
    ) -> None: ...

    def touch_conversation(self, conversation_id: str) -> None: ...

    def delete_conversation(self, conversation_id: str) -> None: ...

    def append_message(self, record: ChatMessageRecord) -> ChatMessageRecord: ...

    # 会话事件日志（P0-2）也在对话域里：它**只按会话**被读写，而且
    # `append_turn` 的整个意义就是"消息与事件同一个事务"——拆到另一个协议，
    # 那条原子性就得跨两个仓储去保证，而跨仓储没有事务。
    def append_turn(
        self,
        *,
        messages: Sequence[ChatMessageRecord],
        events: Sequence[SessionEventRecord],
    ) -> None: ...

    def append_session_events(
        self, records: Sequence[SessionEventRecord]
    ) -> list[SessionEventRecord]: ...

    def list_session_events(
        self, conversation_id: str, *, kinds: Sequence[str] | None = None
    ) -> list[SessionEventRecord]: ...

    def list_messages(self, conversation_id: str) -> list[ChatMessageRecord]: ...

    def last_assistant_previews(self, conversation_ids: Sequence[str]) -> dict[str, str]: ...

    def get_conversation_summary(self, conversation_id: str) -> tuple[str, str | None]: ...

    def set_conversation_summary(
        self, conversation_id: str, summary: str, upto_message_id: str | None
    ) -> None: ...

    def count_workspace_conversations(self, workspace_id: str) -> int: ...

    def count_messages(self, conversation_id: str) -> int: ...

    # 产物留在会话域里，而不是另开一个 `ArtifactRepo`：它**只按会话**被读取
    # （"这条会话产出了什么"），而它回答的另外半个问题——"进没进知识库"——
    # 只在生成它的那一刻被写一次。拆出去只会让"删会话"要凑两个仓储。

    def create_artifact(self, record: ConversationArtifactRecord) -> ConversationArtifactRecord: ...

    def get_artifact(self, artifact_id: str) -> ConversationArtifactRecord | None: ...

    def list_artifacts(self, conversation_id: str) -> list[ConversationArtifactRecord]: ...

    def mark_artifact_ingested(
        self, artifact_id: str, *, knowledge_base_id: str, document_id: str
    ) -> None: ...

    def delete_chat_messages(self, message_ids: Sequence[str]) -> int: ...


@runtime_checkable
class WorkspaceRepo(Protocol):
    """工作区域（v0.15）：账号下的项目容器。

    会话挂在工作区下，但**工作区本身只是一行记录**——归属关系由
    `ConversationRepo.set_conversation_workspace` 写，不在这里。
    """

    def create_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord: ...

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None: ...

    def list_workspaces(
        self, *, device_id: str | None = None, any_device: bool = False
    ) -> list[WorkspaceRecord]: ...

    def update_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord: ...

    def set_workspace_archived(self, workspace_id: str, archived: bool) -> None: ...

    def delete_workspace(self, workspace_id: str) -> None: ...


@runtime_checkable
class ScheduleRepo(Protocol):
    """定时任务域（v0.33）：到点自动跑一轮问答的那些事。

    调度状态（``next_run_at`` / ``enabled`` / ``last_*``）与任务本体放在同一个域：
    它们由同一个动作改写（"认领一次运行"），拆成两个域会让那次 CAS 变成跨域的两步。
    """

    def create_scheduled_task(self, record: ScheduledTaskRecord) -> ScheduledTaskRecord: ...

    def get_scheduled_task(self, scheduled_id: str) -> ScheduledTaskRecord | None: ...

    def list_scheduled_tasks(self) -> list[ScheduledTaskRecord]: ...

    def update_scheduled_task(self, record: ScheduledTaskRecord) -> ScheduledTaskRecord: ...

    def delete_scheduled_task(self, scheduled_id: str) -> None: ...

    def due_scheduled_tasks(
        self, *, now: datetime, limit: int = 10
    ) -> list[ScheduledTaskRecord]: ...

    def arm_scheduled_task(
        self,
        scheduled_id: str,
        *,
        expected_next_run_at: datetime | None,
        next_run_at: datetime | None,
        enabled: bool,
    ) -> bool: ...

    def finish_scheduled_run(
        self,
        scheduled_id: str,
        *,
        status: str,
        error: str | None,
        last_run_at: datetime,
        conversation_id: str | None = None,
    ) -> None: ...


@runtime_checkable
class IdentityRepo(Protocol):
    r"""身份域：使用者名册与登录会话。

    名册与会话放在一个域里：会话是"某人此刻登录着"，两者的写入方是同一套鉴权流程
    （建账号、改密、停用都要连带处理会话），拆开就会出现\「停用了账号但会话还活着\」。
    """

    def create_user(self, record: UserRecord) -> UserRecord: ...

    def get_user(self, user_id: str) -> UserRecord | None: ...

    def find_user_by_name(self, name: str) -> UserRecord | None: ...

    def find_user_by_username(self, username: str) -> UserRecord | None: ...

    def list_users(self) -> list[UserRecord]: ...

    def delete_user(self, user_id: str) -> None: ...

    def update_user_password(self, user_id: str, password_hash: str) -> None: ...

    def set_user_disabled(self, user_id: str, disabled: bool) -> None: ...

    def set_user_avatar(self, user_id: str, avatar_key: str) -> None: ...

    def claim_legacy_ownership(self, owner_id: str) -> dict[str, int]: ...

    def create_session(self, record: SessionRecord) -> SessionRecord: ...

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def touch_session(
        self, session_id: str, *, last_seen_at: datetime, expires_at: datetime
    ) -> None: ...

    def delete_session(self, session_id: str) -> None: ...

    def delete_sessions_for_user(
        self, user_id: str, *, except_session_id: str | None = None
    ) -> int: ...


@runtime_checkable
class ShareRepo(Protocol):
    r"""知识库分享域（v10）：私有是默认，owner 按登录名授出读 / 写两档。

    三个查询口径（按库、按人、删）对应界面的三处：库设置里的分享名单、
    \「别人分享给我的\」列表、以及撤销。
    """

    def put_share(self, record: ShareRecord) -> ShareRecord: ...

    def list_shares_for_kb(self, kb_id: str) -> list[ShareRecord]: ...

    def list_shares_for_user(self, user_id: str) -> list[ShareRecord]: ...

    def delete_share(self, kb_id: str, user_id: str) -> None: ...


@runtime_checkable
class UsageRepo(Protocol):
    """用量域（G7）：token 与调用量的落库、聚合读取与留存清理。

    **刻意不算钱**：价目表必然过期，这个域只存原始计数，聚合口径在服务层。
    """

    def record_usage(self, record: UsageEventRecord) -> UsageEventRecord: ...

    def list_usage(self, *, since: datetime | None = None) -> list[UsageEventRecord]: ...

    def purge_usage_before(self, before: datetime) -> int: ...


@runtime_checkable
class ModelRegistryRepo(Protocol):
    r"""模型注册器域（G1）：供应商、模型目录、以及\「哪个用途用哪个模型\」的绑定解算。

    `resolve_model_binding` 是热路径方法（每建一次 LLM 客户端解一遍），
    它一条 JOIN 拿到两张表——所以它与这两张表的其他方法必须同域。
    """

    def create_model_provider(self, record: ModelProviderRecord) -> ModelProviderRecord: ...

    def get_model_provider(self, provider_id: str) -> ModelProviderRecord | None: ...

    def list_model_providers(self) -> list[ModelProviderRecord]: ...

    def update_model_provider(self, record: ModelProviderRecord) -> None: ...

    def delete_model_provider(self, provider_id: str) -> None: ...

    def create_registered_model(self, record: RegisteredModelRecord) -> RegisteredModelRecord: ...

    def get_registered_model(self, model_pk: str) -> RegisteredModelRecord | None: ...

    def list_registered_models(
        self, provider_id: str | None = None
    ) -> list[RegisteredModelRecord]: ...

    def update_registered_model(self, record: RegisteredModelRecord) -> None: ...

    def delete_registered_model(self, model_pk: str) -> None: ...

    def resolve_model_binding(
        self, key: str
    ) -> tuple[ModelProviderRecord, RegisteredModelRecord] | None: ...


@runtime_checkable
class MCPServerRepo(Protocol):
    """外部 MCP 服务域（v0.15）：登记、开关、删除。

    与 `ApiKeyRepo` 相邻但不同：它管的是**对外连接**的配置，凭据在 `api_key` 字段里，
    但准入策略由服务层的工具闸执行。
    """

    def create_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord: ...

    def get_mcp_server(self, server_id: str) -> MCPServerRecord | None: ...

    def list_mcp_servers(self) -> list[MCPServerRecord]: ...

    def update_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord: ...

    def delete_mcp_server(self, server_id: str) -> None: ...


@runtime_checkable
class TrashRepo(Protocol):
    """回收站：删除的文档进这里保留 7 天。

    `set_trash_expiry` 存在是因为**恢复后再删**要重新计时——过期时间不是常量。
    """

    def add_to_trash(self, record: TrashRecord) -> None: ...

    def list_trash(self) -> list[TrashRecord]: ...

    def purge_expired_trash(self, *, now: datetime | None = None) -> list[TrashRecord]: ...

    def set_trash_expiry(self, trash_id: str, expires_at: datetime) -> None: ...

    def delete_trash(self, trash_id: str) -> None: ...


@runtime_checkable
class SettingsRepo(Protocol):
    """设置：键值读写（数据库 > `.env` 引导 > 代码默认的**存储侧**）。

    只有四个方法、看着像通用 KV——但它是三层优先级的落点，
    优先级判定在 `services/runtime_config.py`，这里只负责存取。
    """

    def get_setting(self, key: str) -> str | None: ...

    def get_settings(self, keys: Sequence[str]) -> dict[str, str]: ...

    def set_setting(self, key: str, value: str) -> None: ...

    def delete_setting(self, key: str) -> None: ...


@runtime_checkable
class WikiRepo(Protocol):
    r"""知识库 Wiki（v24）：页面树、页面本体与出处。

    `replace_wiki_pages` 是**整批替换**：一次生成产出整棵树，逐页 diff 只会引入
    \「半新半旧\」的中间态。
    """

    def replace_wiki_pages(
        self,
        kb_id: str,
        pages: Sequence[WikiPageRecord],
        sources: Sequence[WikiSourceRecord],
    ) -> None: ...

    def list_wiki_pages(self, kb_id: str) -> list[WikiPageRecord]: ...

    def get_wiki_page(self, page_id: str) -> WikiPageRecord | None: ...

    def list_wiki_sources(self, page_id: str) -> list[WikiSourceRecord]: ...

    def wiki_stats(self, kb_id: str) -> tuple[int, datetime | None]: ...


@runtime_checkable
class MaintenanceRepo(Protocol):
    """存储维护：空间概览、VACUUM、以及阶段事件的留存清理。

    **与业务域分开**：这三个都是"管理员显式触发"的动作，不属于任何业务对象。
    阶段事件的**读**在 `DocumentRepo`（它依附文档），清理归这里——
    它们共用一张表，但一个在业务路径上、一个只在维护路径上。
    """

    def storage_stats(self) -> dict: ...

    def vacuum(self) -> None: ...

    def purge_stage_events(self, *, before: datetime) -> int: ...
