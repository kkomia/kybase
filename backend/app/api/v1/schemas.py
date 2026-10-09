"""API 请求/响应模型（协议层）。

命名与字段刻意贴近期望的界面：``stage`` 给状态列，``channels``/``raw_scores`` 给调试台，
``image_ids`` 给图片锚点。**不在此处做业务判断**，只做形状定义（工程规范 §3.3）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ApiKeyPermission, DataSourceKind, DocumentStage, TaskKind, TaskState
from app.services.chunking import (
    CHUNK_OVERLAP_MAX,
    CHUNK_SIZE_MAX,
    CHUNK_SIZE_MIN,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
)
from app.services.knowledge_base import (
    SYSTEM_PROMPT_MAX_CHARS as KB_SYSTEM_PROMPT_MAX_CHARS,
)
from app.services.memory import MAX_ENTRY_CHARS as MAX_MEMORY_ENTRY_CHARS
from app.services.memory import MAX_RECALL as MAX_MEMORY_RECALL
from app.services.suggested_questions import (
    DEFAULT_QUESTIONS_PER_CHUNK as DEFAULT_SUGGESTED_COUNT,
)
from app.services.suggested_questions import (
    MAX_QUESTIONS as SUGGESTED_COUNT_MAX,
)
from app.services.suggested_questions import (
    MIN_QUESTIONS as SUGGESTED_COUNT_MIN,
)
from app.services.suggested_questions import (
    PROMPT_MAX_CHARS as SUGGESTED_PROMPT_MAX_CHARS,
)

_RECORD_CONFIG = ConfigDict(from_attributes=True, use_attribute_docstrings=True)
"""记录类响应模型直接由服务/存储的记录对象构建。

调用方写 ``DocumentOut.model_validate(record)``，字段名对不上会在改字段时立刻报错，
比手写一遍 ``_to_out`` 映射少一处"加了字段忘了同步"的漏点。
这也是协议层不 import ``app.storage`` 还能拼出响应的原因（工程规范 §3.3 L1）。

``use_attribute_docstrings=True``（v0.2）：把**字段下面那段文档字符串**放进 OpenAPI 的
description。不加它，前端的派生类型只能拿到字段名——而 `schema.d.ts` 是生成的，
手写的字段说明会在迁移时丢掉。加上它，说明跟着契约走：后端写一处，
生成的类型、Swagger、前端的 IDE 提示都有。
"""

# --------------------------------------------------------------------- 知识库


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: 切分参数。**范围与 `services/chunking.py` 的常量同源**——在这里写死一份
    #: 迟早会与真正生效的那套漂移（这一层只声明"形状"，业务判断仍在服务层）。
    #: 单个字段的越界在这里就是 422；"重叠 < 块长"这种**跨字段**约束服务层才能
    #: 判断（PATCH 可能只传其中一个），所以那种情况由服务层回 400 + 可读文案。
    chunk_size: int = Field(default=DEFAULT_CHUNK_SIZE, ge=CHUNK_SIZE_MIN, le=CHUNK_SIZE_MAX)
    chunk_overlap: int = Field(default=DEFAULT_OVERLAP, ge=0, le=CHUNK_OVERLAP_MAX)
    embedding_model_pk: str | None = None
    """建库时选定的嵌入模型（注册表主键）。留空 = 用服务端默认。

    嵌入模型是**知识库属性**：文档量小的库可以选高精度模型，量大的选小模型提速。
    模型 ID 与维度在建库时冻结，换模型必须新建库（架构 §6.4 模型锁）。
    """

    #: 分段问题生成（v19 起，v23 起是"入库时为每个分段出题"）。**建库时就能定**，
    #: 之后在「知识库设置 → 切块策略」里改——它与分段同属"入库时怎么处理文本"。
    #: 四个字段与库设置同源，范围常量取自服务层。**默认关**：生成发生在上传之后，要花钱。
    suggested_enabled: bool = False
    suggested_count: int = Field(
        default=DEFAULT_SUGGESTED_COUNT, ge=SUGGESTED_COUNT_MIN, le=SUGGESTED_COUNT_MAX
    )
    suggested_model_pk: str | None = None
    """出题用哪个对话模型（注册表主键）。留空 = 跟随对话页当前选的模型。"""
    suggested_prompt: str = Field(default="", max_length=SUGGESTED_PROMPT_MAX_CHARS)
    """自定义出题提示词。留空 = 用内置提示词。"""

    wiki_enabled: bool = False
    """库形态（v24）：要不要把这个库的已录入内容整理成 Wiki 页面。

    **默认关**：生成要把库里的片段喂给模型（每页一次调用），属于要花钱的产物，
    不该在用户没选之前就开始跑。老库可以在设置里随时打开。"""


class KnowledgeBaseUpdate(BaseModel):
    """改知识库的可编辑属性：名称 / 简介 / 切分参数。**都可选**，只传要改的那个。

    名称与建库同一个上限（120），改名不该比建库更宽松；简介上限 200（卡片两行）。
    空简介（``""``）是合法值 = 清空，所以不加 min_length。

    切分参数（v17）改的是**之后摄入的文档**怎么切；已经切好的块不会自己变，
    界面据此提示"已有文档需要重新摄入"。范围常量与建库同源。
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=200)
    chunk_size: int | None = Field(default=None, ge=CHUNK_SIZE_MIN, le=CHUNK_SIZE_MAX)
    chunk_overlap: int | None = Field(default=None, ge=0, le=CHUNK_OVERLAP_MAX)

    #: 分段问题生成设置（v19/v23）。四个字段一起提交，但都可不传（不传 = 不改）；
    #: ``suggested_model_pk`` 传空串表示"清除"= 回到跟随对话模型（与简介空串同一套约定）。
    #:
    #: **只对之后摄入的文档生效**：问题是在切块那一步生成的，已入库的块不会自己
    #: 长出新问题——要生效得重新摄入（「重新摄入全部文档」，不重新解析）。
    suggested_enabled: bool | None = None
    suggested_count: int | None = Field(
        default=None, ge=SUGGESTED_COUNT_MIN, le=SUGGESTED_COUNT_MAX
    )
    suggested_model_pk: str | None = None
    suggested_prompt: str | None = Field(default=None, max_length=SUGGESTED_PROMPT_MAX_CHARS)
    wiki_enabled: bool | None = None
    """库形态（v24）：要不要生成 Wiki 页面。不传 = 不改。"""
    system_prompt: str | None = Field(default=None, max_length=KB_SYSTEM_PROMPT_MAX_CHARS)
    """**库级提示词**（v0.19）：回答这个库的问题时的额外要求。

    从对话页搬过来的（原先挂在全局设置 `chat.system_prompt` 上）。空串是合法值
    = 清除（回到只剩内置提示词），所以不加 min_length——与简介同一套约定。
    它不是"替换内置提示词"，而是**追加**在内置那两条底线之后，
    见 `services/chat.build_messages` 里的说明。
    """


class KBPromptGenerateIn(BaseModel):
    """生成库提示词的入参（v0.19）。"""

    model_pk: str | None = Field(
        default=None,
        description="用哪个对话模型来生成；留空用设置里的默认对话模型",
    )


class KBPromptSourceOut(BaseModel):
    """生成时用到的某一篇文档摘要，以及它**有没有被生成的文本引用**。"""

    model_config = _RECORD_CONFIG

    document_id: str
    name: str
    summary: str
    cited: bool = False
    """生成的提示词里有没有 `[来源: 这篇]`。

    ``False`` **不代表这篇没用上**——它可能只提供了背景，而没贡献具体事实。
    界面据此把"被引用的"排在前面。"""


class KBPromptDraftOut(BaseModel):
    """生成结果。**不落库**：用户在设置里看着改完再保存。"""

    model_config = _RECORD_CONFIG

    prompt: str
    sources: list[KBPromptSourceOut] = Field(default_factory=list)
    filename_style_citations: list[str] = Field(default_factory=list)
    """这段提示词里仍在要求把文件名写进正文的地方（旧口径，v0.41 起改成编号式引用）。

    非空 = 模型没听那句"别把文件名写进正文"，界面据此提示核对。
    """
    """模型标了来源、但文件名不在给定清单里的那些。

    **这是"编造"的直接证据**（它引了一篇不存在的文件），界面据此提示核对后再保存。"""


class KnowledgeBaseOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    name: str
    description: str = ""
    """库简介（v15）。空串 = 未填写，卡片上显示"暂无简介"。"""
    embedding_model_id: str
    embedding_dim: int
    chunk_strategy: str
    chunk_size: int
    chunk_overlap: int
    suggested_enabled: bool = False
    """是否为每个分段生成推荐问题（v23）。界面据此回显。"""
    suggested_count: int = DEFAULT_SUGGESTED_COUNT
    suggested_model_pk: str | None = None
    """出题模型。``None`` = 跟随对话页当前选的模型。"""
    suggested_prompt: str = ""
    """自定义出题提示词；空串 = 用内置提示词。"""
    system_prompt: str = ""
    """库级提示词；空串 = 只用内置提示词。界面在库设置里回显与编辑。"""
    wiki_enabled: bool = False
    """库形态（v24）：``False`` = 仅向量检索；``True`` = 向量检索 + Wiki 页面。

    界面据此决定要不要给「Wiki」入口、以及在设置里回显勾选态。"""
    created_at: datetime | None = None
    can_manage: bool = False
    """当前调用主体能否管理这个库的分享（owner / 管理员）。

    **由后端算而不是前端推**：判定规则在 `services/share.py`（"看得见"与"管得动"
    是两次判定），前端再实现一遍必然与它漂。界面据此决定要不要显示「分享」入口。
    """
    can_write: bool = False
    """能否写入这个库（上传/删除）。只读分享的成员看得见但写不动，界面据此收起写入口。"""
    document_count: int = 0
    """库内文档数。**由列表接口一并算出**（一条 GROUP BY），
    前端不必再"逐库拉一次文档列表只为了数数"——那会随库数量线性放大请求数。"""
    last_activity: datetime | None = None
    """库内文档的最近更新时间；没有文档时为 None（界面显示占位符，而不是一个含糊的 0）。"""


class KnowledgeBaseList(BaseModel):
    items: list[KnowledgeBaseOut]


# --------------------------------------------------------------------- 文档


class DocumentProgressOut(BaseModel):
    """列表行上那条分段进度所需要的信息（§12.115）。

    **不给百分比**（用户也这么要求）：6 个环节的耗时极不均（解析几分钟、切分几秒），
    百分比只会编出一个对不上的数字——"第 3/6 步 · 解析内容 · 已用 2 分 14 秒"
    每一项都能和实际对上。
    """

    status: str = "running"
    """``running`` / ``done`` / ``failed`` / ``canceled``。"""
    step_index: int = 1
    """当前第几步（1-based）。失败时是**停下那一步**，所以能读出"炸在哪"。"""
    step_total: int = 0
    step_label: str = ""
    elapsed_ms: int = 0
    """当前这一步已花的时间。跑着时每次轮询都在涨——这是"还在动"的证据。"""
    total_ms: int = 0
    """整条流水线累计（含重试与重新摄入）。"""
    retries: int = 0
    """这一步重试过几次。"卡住"与"反复重试"要看的处置完全不同。"""
    stalled: bool = False
    """执行租约已过期 = 没有 worker 在续约。**唯一能确定说"卡住"的情形**。"""


class DocumentOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    knowledge_base_id: str
    name: str
    source_kind: DataSourceKind
    stage: DocumentStage
    size_bytes: int
    mime_type: str | None = None
    page_count: int | None = None
    is_split: bool = False
    error: str | None = None
    chunk_count: int = 0
    uploaded_by: str | None = None
    """上传者的使用者 id（G6）。``None`` = 未记录，界面显示"未记录"而不是编一个名字。"""
    uploaded_by_name: str = ""
    """解析后的名字。**由后端解析**：前端拿 id 还得再查一次名册，
    列表里就会有 N 次多余请求。"""
    folder_id: str | None = None
    """所在目录（v13）。``None`` = 未归档（根目录）。"""
    disabled: bool = False
    """停用（v14）。停用后不参与检索（两条通道都过滤），其余一切保留。"""
    original_kind: str = "binary"
    """原件能不能在这页里渲染出来（``pdf`` / ``image`` / ``docx`` / ``pptx`` / ``excel``）。

    界面据此决定首页要不要给「原文版式 / 解析文本」这个切换、以及**先取哪一个**——
    判在后端是为了不让前端去猜文件后缀（同 ``content_kind`` 的理由）。
    """
    created_at: datetime | None = None
    updated_at: datetime | None = None
    question_count: int = 0
    """该文档各分段已生成问题的**总条数**（v24）。0 = 还没出过题。"""
    questioned_chunk_count: int = 0
    """有题的分段数。配合 ``chunk_count`` 显示"几段里有几段出了题"。"""
    questions_pending: bool = False
    """是否还有出题任务在队列里/在跑（v24）。

    列表据此显示"生成中…"，也据此决定继续轮询——出题**不改变文档阶段**，
    只看 ``stage`` 的话前端永远等不到它完成。"""
    summary: str = ""
    """入库时生成的文档摘要（v25）。**问答上下文靠它省 token**，
    界面也把它当一句话说明（抽屉里显示、列表行悬浮显示）。空串 = 还没生成。"""
    progress: DocumentProgressOut | None = None
    """分段进度的摘要（§12.115）。列表行的进度条吃它；完整那棵树在
    ``GET /documents/{id}/timeline``。``None`` = 这条路径没算（老调用点）。"""


class DocumentList(BaseModel):
    """一页文档。

    ``total`` 是**这套筛选条件下的总数**（不是 ``items`` 的长度）：界面要显示
    "共 N 篇 · 第 X / Y 页"，只回一页数据的话前端算不出总页数。
    ``limit`` / ``offset`` 原样回显，调用方不必自己记住请求时传了什么。
    """

    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class FolderOut(BaseModel):
    """知识库内的目录（v13）。``document_count`` 由后端算——列表要显示"几篇"。"""

    model_config = _RECORD_CONFIG

    id: str
    kb_id: str
    name: str
    document_count: int = 0
    created_at: datetime | None = None


class FolderListOut(BaseModel):
    items: list[FolderOut]


class FolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class FolderRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class DocumentFolderIn(BaseModel):
    """把文档移进目录；``folder_id`` 为 ``None`` 表示移回根目录。"""

    folder_id: str | None = None


class DocumentRenameIn(BaseModel):
    """改文件名。上限与服务层的常量一致（``DOCUMENT_NAME_MAX_CHARS``）。"""

    name: str = Field(min_length=1, max_length=200)


class DocumentDisabledIn(BaseModel):
    """停用/恢复检索。**只动标记**：不删切块与向量，恢复零成本。"""

    disabled: bool


class DocumentBatchIn(BaseModel):
    """批量动作：``delete``（进回收站）、``reprocess``（重新摄入）、``move``（移目录）、
    ``enable`` / ``disable``（停用或恢复检索）或 ``questions``（为已索引文档补生成分段问题）。

    ``document_ids`` 设上限而不是"随便多少"：一次勾几千篇会把请求体、逐条查询
    与响应都拉大，而界面上的多选本来也到不了那个量级。

    ``all=True``（v17）表示**对这个库的全部文档**执行，忽略 ``document_ids``。
    它服务的场景是"切分参数改了、要整库重跑"：由服务端自己解析全集，
    界面不必先翻页取 id 再回传（那个列表接口一次回全量，本身就是瓶颈）。
    """

    action: Literal["delete", "reprocess", "move", "enable", "disable", "questions"]
    document_ids: list[str] = Field(default_factory=list, max_length=500)
    folder_id: str | None = None
    """``move`` 的目标目录；``None`` 表示移回根目录。其它动作忽略此字段。"""
    all: bool = False

    @model_validator(mode="after")
    def _require_target(self) -> DocumentBatchIn:
        """两者都不给是写错了，当场说清——否则服务层会执行一个空批次、看起来像成功。"""
        if not self.all and not self.document_ids:
            raise ValueError("document_ids 与 all 至少要给一个")
        return self


class DocumentBatchItemOut(BaseModel):
    document_id: str
    ok: bool
    error: str | None = None


class DocumentBatchOut(BaseModel):
    """逐条结果。**部分失败是常态**，所以要给出每一篇的成败而不是一个总数。"""

    action: str
    succeeded: int
    failed: int
    items: list[DocumentBatchItemOut]


class DocumentPartOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    part_index: int
    page_start: int
    page_end: int
    stage: DocumentStage
    error: str | None = None


class DocumentPartList(BaseModel):
    items: list[DocumentPartOut]


class ChunkOut(BaseModel):
    """切块（文档详情页的正文预览）。

    与 `SearchHitOut` 是两件事：命中带分数与通道，切块只描述"文档被切成了什么"。
    """

    model_config = _RECORD_CONFIG

    chunk_id: str
    document_id: str
    ordinal: int
    text: str
    heading_path: str | None = None
    page: int | None = None
    image_ids: list[str] = Field(default_factory=list)
    disabled: bool = False
    """被禁用的块不再参与检索，但仍留在库里（§G3）。"""
    questions: list[str] = Field(default_factory=list)
    """入库时为这一段生成的问题（v23）。**只读展示**——它由模型产出，
    用户要判断"出题质量如何、值不值得开着"，就得看得见它。"""


class ChunkList(BaseModel):
    items: list[ChunkOut]
    total: int = Field(description="该文档的切块总数，与 items 长度无关（items 可能被 limit 截断）")


class UploadAccepted(BaseModel):
    """上传响应。``is_duplicate`` 对应架构 §6.3 的"检测到相同文件"提醒。"""

    document: DocumentOut
    is_duplicate: bool
    task_id: str | None = None


# --------------------------------------------------------------------- 任务


class TaskOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    kind: TaskKind
    state: TaskState
    document_id: str | None = None
    knowledge_base_id: str | None = None
    """任务所属文档的知识库（v14 后随列表带回）。

    界面的"按知识库筛选"靠它——没有它，任务中心就得逐个库拉文档来反查归属。
    没有挂文档的任务（数据源拉取）为 None。"""
    document_name: str = ""
    """关联文档名。**由后端批量解析**：否则任务中心要为每个库各拉一次文档列表
    只为把 id 换成名字（实测那是这一页最慢的一段）。"""
    attempts: int
    max_attempts: int
    error: str | None = None
    next_run_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    health: str = "idle"
    """``running`` / ``stalled`` / ``overdue`` / ``idle`` / ``done``（M7 / T7.4）。

    **由后端判定而不是前端猜**：它要比较租约到期时间与当前时刻，
    还要知道租约时长，那些只有后端有。前端只负责按这个值上色。
    """
    health_label: str = ""
    health_detail: str = ""


class TaskList(BaseModel):
    items: list[TaskOut]
    """任务列表。每项都带 ``health``，界面据此标出"可能卡住"。"""


class TaskHealthOut(BaseModel):
    task_id: str
    status: str
    label: str
    detail: str = ""


class HealthOverviewOut(BaseModel):
    """运行态总览（T7.4）。"""

    total: int = 0
    running: int = 0
    queued: int = 0
    stalled: int = 0
    overdue: int = 0
    problems: list[TaskHealthOut] = Field(default_factory=list)
    """需要用户注意的任务（卡住 / 长时间未执行），最多 20 条。"""
    worker_enabled: bool = True
    """内嵌消费线程是否开着。**关掉时任务不会自己跑**——
    这是"任务一直排队"最常见的原因，界面必须能解释它。"""


class HardwareLoadOut(BaseModel):
    """机器与本进程的资源占用（负载面板）。"""

    cpu_percent: float | None = None
    """0–100；**首次采样为 null**（没有上一次采样就没有差值可算），界面显示"—"。"""
    cpu_count: int = 1
    memory_used_bytes: int = 0
    memory_total_bytes: int = 0
    memory_percent: float = 0.0
    process_rss_bytes: int | None = None


class QueueLoadOut(BaseModel):
    """队列深度与并发槽位。"""

    running: int = 0
    pending: int = 0
    slots: int = 1
    """并发上限（``KYLAB_WORKER_CONCURRENCY``）。"""
    pending_by_kind: dict[str, int] = Field(default_factory=dict)
    """排队任务按类型分布——"积压全是出题"和"积压全是解析"该做的事完全不同。"""
    oldest_pending_seconds: float | None = None
    stalled: int = 0
    overdue: int = 0


class ParserQuotaOut(BaseModel):
    """云端解析器的当日额度。"""

    parser_name: str = ""
    configured: bool = False
    pages_used: int = 0
    calls: int = 0
    daily_quota: int = 0
    remaining: int = 0
    exhausted: bool = False
    """额度用尽。**不是错误**：云端只是不再优先处理，任务会继续但变慢。"""


class SystemLoadOut(BaseModel):
    """负载面板（§12.115）：CPU / 内存 / 队列 / 槽位 / 云端额度。"""

    hardware: HardwareLoadOut
    queue: QueueLoadOut
    quota: ParserQuotaOut
    sampled_at: datetime


# --------------------------------------------------------------------- 检索


class MetadataFilterIn(BaseModel):
    document_ids: list[str] | None = None
    source_kinds: list[DataSourceKind] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    kb_ids: list[str] = Field(min_length=1)
    top_k: int = Field(default=8, gt=0, le=100)
    mode: str = "hybrid"
    candidate_k: int = Field(default=40, gt=0, le=500)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """相对融合分的下限（该条分 / 最高分）。只剪尾巴，剪不掉"整批都不相关"。"""
    min_vector_score: float | None = Field(default=None, ge=0.0, le=1.0)
    """向量余弦的**绝对**下限。不传 = 按库的嵌入模型用标定默认（见下），0 = 关闭。

    标定默认落在 ``services/retrieval`` 的 ``MIN_VECTOR_SCORE_BY_MODEL``：余弦的绝对值
    是模型属性，本机只标定过 bge-m3（**0.531**，噪声头 0.460–0.520 / 真查询头 0.60–0.755）
    与 WeMM 多模态（**0.33**，噪声头 0.305）；**没标定过的嵌入模型默认不设限**——
    宁可少拦，也不能拿一个别的模型的尺度把真命中拦掉。
    """
    min_term_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    """词面覆盖的绝对下限：查询实词里至少多少比例出现在候选（或其文档名）里。

    不传 = 跟着向量下限走（向量下限开着时为 0.67，取值与噪声实测见
    ``services/retrieval`` 的 ``DEFAULT_MIN_TERM_COVERAGE``），0 = 关闭。
    两条证据是"或"的关系，见 ``services/retrieval/coverage.py``。
    """
    rerank: bool = False
    filters: MetadataFilterIn | None = None


class SearchHitOut(BaseModel):
    model_config = _RECORD_CONFIG

    chunk_id: str
    document_id: str
    document_name: str | None = None
    knowledge_base_id: str
    text: str
    score: float
    """**融合分**（RRF 家族：``1/(k+rank)`` 量级），只反映**名次**、不是相似度。

    实测：向量档第 1 名恒为 ``0.0164``、hybrid 档第 1 名恒为 ``0.0328``——
    它随名次走，不随"像不像"走。**界面不要把它显示成"相似度"**，
    也不要拿它跟"相关度下限"比（那是 ``similarity`` 的活）。
    """
    similarity: float | None = None
    """这条命中的**真实余弦相似度**（向量通道的原始分；``None`` = 只被全文捞到）。

    **这个才是"相似度"**：相关度地板比的是它（bge-m3 0.531 / WeMM 0.35，按模型标定，
    见 `services/retrieval/service.py` 的 `MIN_VECTOR_SCORE_BY_MODEL`）。
    界面要显示相似度、或要判断"到此为止"，读这一位。"""
    page: int | None = None
    heading_path: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    ranks: dict[str, int] = Field(default_factory=dict)
    raw_scores: dict[str, float] = Field(default_factory=dict)
    rerank_score: float | None = None


class ChannelStatOut(BaseModel):
    model_config = _RECORD_CONFIG

    channel: str
    count: int
    elapsed_ms: float


class SearchResponse(BaseModel):
    hits: list[SearchHitOut]
    mode: str
    reranked: bool
    filtered_out: int = 0
    stats: list[ChannelStatOut] = Field(default_factory=list)
    embedding_configured: bool = True
    """是否配了嵌入模型。为假时本次只做了全文通道，界面要如实说明。"""
    embedding_is_development: bool = False
    """是否为开发用确定性嵌入（仅显式开着开发开关时）：界面提示"检索质量不代表真实效果"。"""


# --------------------------------------------------------------------- 设置


class SettingFieldOptionOut(BaseModel):
    value: str
    label: str


class SettingFieldOut(BaseModel):
    """一个配置项。密钥只给掩码，``configured`` 说明是否已填。"""

    key: str
    label: str
    type: str
    value: str
    configured: bool
    options: list[SettingFieldOptionOut] = Field(default_factory=list)
    """``type == "select"`` 时的候选值；其余类型为空。"""


class SettingGroupOut(BaseModel):
    key: str
    label: str
    fields: list[SettingFieldOut]


class SettingsViewOut(BaseModel):
    """设置页要的全部信息：分组字段 + 当前实际生效的模型与模式。"""

    groups: list[SettingGroupOut]
    embedding_model_id: str
    embedding_dim: int
    embedding_configured: bool
    """是否已选定嵌入模型。为假时不能建库，界面要给出去哪儿配的指引。"""
    embedding_is_development: bool
    rerank_enabled: bool


class SettingValueIn(BaseModel):
    key: str
    value: str = ""


class SettingsPatchIn(BaseModel):
    values: list[SettingValueIn]


class SettingsPatchOut(BaseModel):
    updated: int
    rejected: list[str] = Field(default_factory=list)
    """被拒绝的键：拼错键名时报出来，而不是静默"保存成功"。"""


class TestConnectionOut(BaseModel):
    ok: bool
    detail: str


# --------------------------------------------------------------------- 统计


class ActivityPointOut(BaseModel):
    """一天的活跃度。"""

    model_config = _RECORD_CONFIG

    day: date
    documents: int
    chunks: int
    tasks: int


class KbStatOut(BaseModel):
    # 嵌套模型也要 from_attributes：外层配了不代表内层能从 dataclass 读
    model_config = _RECORD_CONFIG

    id: str
    name: str
    embedding_model_id: str
    embedding_dim: int
    documents: int
    chunks: int
    last_activity: datetime | None = None


class DashboardOut(BaseModel):
    """驾驶舱要的全部数字。"""

    model_config = _RECORD_CONFIG

    generated_at: datetime
    window_days: int
    total_knowledge_bases: int
    total_documents: int
    total_chunks: int
    indexed_documents: int
    failed_documents: int
    running_tasks: int
    failed_tasks: int
    storage_bytes: int
    recent_documents: int
    activity: list[ActivityPointOut] = Field(default_factory=list)
    by_stage: dict[str, int] = Field(default_factory=dict)
    by_suffix: dict[str, int] = Field(default_factory=dict)
    by_source_kind: dict[str, int] = Field(default_factory=dict)
    knowledge_bases: list[KbStatOut] = Field(default_factory=list)


# --------------------------------------------------------------------- 对话


class ChatHistoryIn(BaseModel):
    """历史消息：只带 role 与 content，不落库（会话持久化不在本轮范围）。"""

    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatAttachmentIn(BaseModel):
    """这一轮用户消息**随发的附件**（v0.55）。

    只带 ``key``（文件区里的产物 id）就够：名字 / 类型 / 字节数一律由服务端按
    **库里的记录**回填，不信客户端送来的那几个字段（它们只是显示用的）。
    服务端还会校验这个 key **属于这条会话**——不校验的话任何 key 都能被写进消息。
    """

    key: str = Field(min_length=1)


class ChatRequestIn(BaseModel):
    # 上限与前端 `Composer.tsx` 的 `MAX_QUERY_CHARS` **同一个数**（D06，2026-09-28 走查）：
    # 那边先拦是为了给用户一句能照做的话（"存成文件用附件传"），这里是防线——
    # 没有它，一个几十万字的 query 会照单全收，既吃满上下文窗口，也让这一轮的答案变差。
    query: str = Field(min_length=1, max_length=32_000)
    kb_ids: list[str] = Field(
        default_factory=list,
        description="这一轮依据哪些知识库；**空 = 不使用知识库**（界面上那个开关关掉时）",
    )
    skill_names: list[str] = Field(
        default_factory=list,
        description="本轮钉住（必定展开正文）的技能名，来自输入框「加号 → 技能」",
    )
    top_k: int | None = Field(default=None, gt=0, le=20, description="留空用设置里的条数")
    history: list[ChatHistoryIn] = Field(default_factory=list)
    conversation_id: str | None = Field(
        default=None,
        description="指定则把这一轮存进该会话，并以库里的历史为准（忽略上方的 history）",
    )
    model_pk: str | None = Field(
        default=None,
        description="这一轮用哪个注册对话模型；留空则用会话已存的，再留空用全局默认",
    )
    thinking: bool | None = Field(
        default=None,
        description="这一轮是否开启思考；留空则用会话已存的，再留空用全局默认（默认开）",
    )
    thinking_effort: Literal["low", "medium", "high"] | None = Field(
        default=None, description="这一轮的思考强度；同上，留空逐级回退"
    )
    attachments: list[ChatAttachmentIn] = Field(
        default_factory=list,
        description="这一轮用户消息随发的附件（文件区里的 key）；会作为快照写进那条消息",
    )


class ChatResumeIn(BaseModel):
    """续跑上一轮的请求体。**没有 query**——问题在会话里，不在这次请求里。

    这一轮的模型档位、思考档位、库范围都取**会话已存的**（续跑是"接着同一轮做"，
    不是新一轮提问，所以不给它换模型的机会）。唯一从界面来的是钉住的技能：
    它存在输入框的偏好里、不入库，而续跑同样需要那几个技能在场。
    """

    skill_names: list[str] = Field(
        default_factory=list,
        description="本轮钉住的技能名（与提问时同一份，来自输入框「加号 → 技能」）",
    )
    model_pk: str | None = Field(default=None, description="留空用会话已存的；一般不必给")
    thinking: bool | None = Field(default=None, description="留空用会话已存的")
    thinking_effort: Literal["low", "medium", "high"] | None = Field(default=None)


class ChatApprovalIn(BaseModel):
    """对一条待确认的工具调用做出决定（v0.41）。

    **只有三个取值**，都是用户在确认条上明确点出来的：允许一次 / 这类都允许 / 拒绝。
    "超时"与"这条链路没人可问"不是请求参数——它们是执行侧自己的结论，
    不该能从外面伪造（伪造了就等于给了一条绕过"等用户点头"的路）。
    """

    decision: Literal["allow_once", "allow_always", "deny"] = Field(
        description="允许一次 / 这类都允许（写进放行清单）/ 拒绝"
    )
    reason: str = Field(
        default="",
        max_length=500,
        description=(
            "拒绝时给模型的一句理由（P2-1，可空）。它会拼进回灌给模型的工具结果"
            "（「对方拒绝了这次执行，理由是：…」），让下一轮模型据此改路子，"
            "而不是把同一条命令原样再试一次；留空则与加这个字段之前完全一样。"
        ),
    )


class ChatApprovalOut(BaseModel):
    """决定有没有真的交到那一头。"""

    accepted: bool
    """``True`` = 正在等的那次执行已经收到它，会立刻接着往下跑。"""
    detail: str = ""
    """给人看的一句话（没送到时说清为什么）。"""


class ChatSourceOut(BaseModel):
    """回答引用的原文出处。带 preview，界面点开就能看到依据。"""

    model_config = _RECORD_CONFIG

    index: int
    chunk_id: str
    document_id: str
    document_name: str
    heading_path: str | None = None
    page: int | None = None
    score: float = 0.0
    preview: str = ""
    #: 出处所属知识库，界面用它把引用直连到库页抽屉。
    #: 默认空串：历史会话里存的快照没有这个字段，读出来要能兼容。
    knowledge_base_id: str = ""
    #: 这篇文档的摘要（v25）。**它进了提示词**（每条资料后面跟一行"文档背景"，
    #: 同一篇只带一次），在这里回给前端是为了**可核对**：用户能看见模型
    #: 到底拿到了什么背景，而不是只能猜"它为什么这么答"。
    document_summary: str = ""


class ChatTurnOut(BaseModel):
    answer: str
    sources: list[ChatSourceOut] = Field(default_factory=list)


class ChatResponseOut(ChatTurnOut):
    """一次性问答的响应（与流式共用同一套 source 结构）。"""


class SuggestedQuestionsOut(BaseModel):
    """对话页空状态的示例问题。

    ``generated`` 为假（``questions`` 为空）时前端回退到静态样例——
    生成不出来不是错误，只是没有语料依据的建议可给。
    """

    questions: list[str] = Field(default_factory=list)
    generated: bool = False


# --------------------------------------------------------------------- API Key


class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    permission: ApiKeyPermission = ApiKeyPermission.READONLY
    """默认只读：**默认值要取最保守的那个**。给外部集成发一把能删库的钥匙，
    不应该是因为"没填那个字段"。"""
    knowledge_base_ids: list[str] = Field(default_factory=list)
    """空列表表示不限制范围（可访问全部知识库），见 services/api_key.py 的说明。"""


class ApiKeyOut(BaseModel):
    """列表展示用。**绝不回显 key_hash 或明文**——只有创建响应里有明文。"""

    model_config = _RECORD_CONFIG

    id: str
    name: str
    permission: ApiKeyPermission
    knowledge_base_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    prefix: str = ""
    """展示用前缀（``kylab_sk_ab12…``），让用户能分辨"哪把是哪把"。"""


class ApiKeyIssuedOut(ApiKeyOut):
    """创建响应：``token`` 是明文**唯一一次**出现的地方。"""

    token: str


class ApiKeyListOut(BaseModel):
    items: list[ApiKeyOut]


# --------------------------------------------------------------------- 对话留存


class ConversationCreateIn(BaseModel):
    title: str = Field(default="", max_length=64)
    kb_ids: list[str] = Field(default_factory=list)
    workspace_id: str | None = None
    """挂到哪个工作区（v0.15）。``None`` = 未归档。

    传了工作区而 ``kb_ids`` 留空时，**知识库范围继承工作区的**（见设计文档 §5）：
    "进入项目，资料范围就定了"——用户不必每次重勾一遍。"""
    model_pk: str | None = None
    """本条会话选用的对话模型（v12）。``None`` = 跟随全局默认。"""
    thinking: bool | None = None
    """本条会话是否开启思考（v16）。``None`` = 跟随全局默认。"""
    thinking_effort: Literal["low", "medium", "high"] | None = None
    """本条会话的思考强度（v16）。``None`` = 跟随全局默认。"""


class ConversationUpdateIn(BaseModel):
    """改会话的可编辑属性：标题 / 置顶。**都可选**，只传要改的那个。

    从一个字段扩成两个而不是新加一个端点：两者都是"整理这条会话"的同一类动作，
    分两个端点只会让前端的"改完刷新"写两遍。
    """

    title: str | None = Field(default=None, min_length=1, max_length=64)
    pinned: bool | None = None
    archived: bool | None = None
    """归档 / 取消归档（v0.17）。与 ``workspace_id`` 的区别是不需要哨兵：
    它只有真/假两种意图，没有"不传"与"传空"的歧义。"""
    workspace_id: str | None = None
    """改归属：传工作区 id = 挂进去，传 ``null`` = 退回未归档。

    这个字段与上两个的区别是它**必须能区分"不传"与"传 null"**——
    所以用了一个哨兵（``UNSET``）在服务端判断，见 conversations.py 的说明。"""


class ConversationRewindIn(BaseModel):
    """回退最近 N 轮问答（「重新生成」用）。默认一轮。"""

    turns: int = Field(default=1, ge=1, le=20)


class ConversationRewindOut(BaseModel):
    """回退结果：``query`` 是被删掉的那句提问，调用方拿它重新发一次。

    没有可回退的内容时 ``query`` 为空串——调用方据此提示，而不是发一次空提问。
    """

    query: str = ""
    removed: int = 0


class ConversationBranchIn(BaseModel):
    """**从这里重开**（D11）：从第 ``turn`` 轮分叉出一条新会话。

    ``turn`` 是**第几个提问**（1 起数）。越界（0 / 超过总轮数）不夹到边界、
    也不"取最后一条"，而是报错——静默夹过去会让用户以为分叉点就是他点的那一处，
    而拿到的是另一段历史。``0`` 与小数由这一层的 ``ge=1`` 挡住（422）。
    """

    turn: int = Field(ge=1)


class ConversationOut(BaseModel):
    """会话摘要。列表用它，所以带上 ``message_count`` 让界面能写"6 条消息"。"""

    model_config = _RECORD_CONFIG

    id: str
    title: str
    kb_ids: list[str] = Field(default_factory=list)
    model_pk: str | None = None
    """本条会话选用的对话模型（v12）；``None`` = 全局默认。界面据此回填模型选择器。"""
    thinking: bool | None = None
    """本条会话是否开启思考（v16）；``None`` = 全局默认。界面据此回填思考开关。"""
    thinking_effort: str | None = None
    """本条会话的思考强度（v16）；``None`` = 全局默认。"""
    pinned: bool = False
    """置顶（v17）。置顶的会话排在列表最前，且聊天不改变它的名次。"""
    workspace_id: str | None = None
    """所属工作区（v0.15）；``None`` = 未归档。"""
    archived_at: datetime | None = None
    """归档时间（v0.17）。非空 = 已归档——**归档不是删除**：
    默认列表里看不到它，但内容还在，随时可以取消归档。"""
    preview: str = ""
    """最近一条回答的开头一段（历史会话面板的两行预览）。

    给回答而不是给提问：用户回看历史时想认出的是"这次聊出了什么"，
    而问题往往几条都长得很像（"帮我看看这个"）。
    """
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int = 0


class ConversationListOut(BaseModel):
    items: list[ConversationOut]


class ChatAttachmentOut(BaseModel):
    """用户消息随发的附件快照（v0.55，见 :class:`ChatAttachmentIn`）。

    ``key`` 是文件区里的 key：界面拿它去预览 / 下载（与文件抽屉同一套端点），
    所以就算那份文件后来被删了，这条消息仍然说得清"当时带的是它"。
    """

    key: str
    name: str
    kind: str = ""
    size_bytes: int = 0


class ChatMessageOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    role: str
    content: str
    sources: list[ChatSourceOut] = Field(default_factory=list)
    steps: list[dict[str, object]] = Field(default_factory=list)
    """当轮的过程步骤（工具调用、组织回答…，v0.25）。

    与 ``sources`` 同为快照：回看旧回答时，当时调了哪些工具、每步拿到什么，
    都该是当时的样子。老消息没有这一项，返回空列表。
    """

    thinking: str = ""
    """当轮的思考过程全文（v0.25）。空串 = 这一轮没有思考。"""

    attachments: list[ChatAttachmentOut] = Field(default_factory=list)
    """用户消息随发的附件（v0.55）。**只有用户消息会有**；老消息返回空列表。"""

    created_at: datetime | None = None


class ConversationDetailOut(ConversationOut):
    messages: list[ChatMessageOut] = Field(default_factory=list)


class SessionEventOut(BaseModel):
    """会话日志里的一条事件（P0-2，只追加）。

    抄的是 ZCode 的会话事件日志（开发计划 §12.225）：会话是一串不可变事件，
    消息里的 ``steps`` 是它的投影。字段刻意贴着存储的列（``seq`` / ``kind`` /
    ``payload``）——读的人要能照着日志判断"当时到底发生了什么"，
    在这里再包一层"好看的形状"只会让日志与它的读取方变成两件事。

    ``kind`` 是**词表**取值（``turn/start``、``turn/end``、``step``、
    ``tool_call``、``thinking``、``error``、``interrupted``、``mode/changed``、
    ``command``），定义在 ``services/session_events.py`` 一处。
    """

    model_config = _RECORD_CONFIG

    id: int
    seq: int
    """**会话内**单调递增的序号。同一条会话不会出现两个相同的 ``seq``
    （表上有唯一约束），所以它同时是"事件只追加"的判据。"""
    kind: str
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime | None = None


class SessionEventListOut(BaseModel):
    items: list[SessionEventOut] = Field(default_factory=list)


class ContextUsageItemOut(BaseModel):
    """上下文用量分解里的**一项来源**（P1-3，抄 ZCode 的 ``chat.contextUsage.breakdown``）。

    ``kind`` 是稳定取值（``messages`` / ``system_prompt`` / ``skills`` / ``tools`` /
    ``memory`` / ``other``），``label`` 是界面上显示的那几个字——**界面不要自己翻译
    ``kind``**：分解的口径是服务端定的（比如"记忆与人设"包含哪几份文件），
    两处各写一份迟早会对不上。
    """

    model_config = _RECORD_CONFIG

    kind: str
    """来源：消息 / 系统提示词 / 技能目录 / 工具定义 / 记忆与人设 / 其它。"""
    label: str
    """给人看的中文名。"""
    chars: int = 0
    """这一来源的字符数（估算所依据的那个数）。"""
    tokens: int = 0
    """按字符数估的 token（见 ``estimated``）。"""
    share: float = 0.0
    """占**已用**的比例（0~1）。界面画分解条用它，比每次自己除一遍稳。"""
    preview: str = ""
    """这一项**实际文本的开头一段**（D09，2026-09-28 走查）。

    这一排原先只有数字：能看出"系统提示词占多少 token"，但"本轮到底给它灌了什么"
    没有入口（同一页里工具结果与出处早就有"加载全部 / 看全文"）。
    截断长度是服务端定的（``services/chat.CONTEXT_PART_PREVIEW_CHARS``，600 字），
    前端只负责显示，别自己再截一遍。"""


class ContextUsageOut(BaseModel):
    """这一轮上下文的占用与分解（P1-3 的仪表）。

    **是估算**：按字符数算（中日韩 1 字 ≈ 1 token、其余 4 字符 ≈ 1，刻意偏高），
    真实用量只有端点返回的 ``usage`` 才知道。所以 ``estimated`` 恒为真、
    ``note`` 里写明这句话——仪表上不能把估算画成账单。
    """

    items: list[ContextUsageItemOut] = Field(default_factory=list)
    used: int = 0
    """各项之和（token，估算）。**与 ``items`` 的 ``tokens`` 求和相等**（有用例钉住）。"""
    total: int = 0
    """上下文窗口（token，设置项 ``chat.context_window``）。"""
    ratio: float = 0.0
    """``used / total``（0~1）。"""
    compress_at: int = 0
    """触发自动压缩的阈值（百分比，设置项 ``chat.compress_at``）。界面画那条线要用它。"""
    compress_budget: int = 0
    """**实际**触发压缩的 token 数：``compress_at`` 与绝对上限（``chat.compress_max_tokens``）
    取小的那个（D37）。界面说"到多少会自动压"要用**这个**——窗口调大之后
    ``total × compress_at`` 会是个永远到不了的数（实测 1M 窗口报 70 万，那条会话才 1.2 万）。"""
    estimated: bool = True
    """恒为真：这些数字是**按字符数估的**，不是分词器给的。"""
    note: str = ""


class ConversationArtifactOut(BaseModel):
    """会话产出的一份文件（v0.26）。

    与 ``ChatStep.artifacts`` 是**同一个形状**（由 ``ArtifactService.describe``
    生成），这不是巧合：步骤里那一份是流式当时的样子，这里这一份是**现在的样子**。
    两者分叉的话，"刷新之后卡片突然显示已入库"就成了必然。
    """

    artifact_id: str
    name: str
    size_bytes: int = 0
    format: str = ""
    """扩展名小写（``docx`` / ``pdf`` / …），界面据此选图标。"""
    storage: str = "object"
    """``workspace``（落在工作区目录）或 ``object``（落在会话的临时区）。"""
    where: str = ""
    """给人看的那句话：「工作区「我的项目」」/「本会话」。"""
    path: str | None = None
    """工作区那份的绝对路径；对象存储那份没有。"""
    knowledge_base_id: str | None = None
    """进了哪个知识库。``None`` = 没进（默认），界面据此决定要不要给「存进知识库」。"""
    document_id: str | None = None
    created_at: datetime | None = None


class ConversationArtifactListOut(BaseModel):
    items: list[ConversationArtifactOut]


class FileDownloadUrlOut(BaseModel):
    """一条文件链接（预览与下载共用）。**相对路径**：对外域名只有部署时才知道。"""

    url: str
    expires_at: int
    name: str


class FileEntryOut(BaseModel):
    """文件区里的一行（v0.26）。

    ``key`` 是**在这个文件区里唯一指代它**的东西，界面拿它当不透明字符串用：
    工作区模式是相对路径（``报告/初稿.docx``），临时区是产物 id。
    """

    key: str
    name: str
    is_dir: bool = False
    size_bytes: int = 0
    modified_at: datetime | None = None
    kind: str = ""
    """扩展名小写（``docx`` / ``md`` / ``png``…）或 ``dir``。**界面按它选渲染器**，
    服务端算好——两处各算一遍迟早会分叉。"""


class FileListingOut(BaseModel):
    """一层目录（v0.26；两档都能进子目录，见 ``mode``）。"""

    mode: str
    """``workspace``（项目目录）/ ``object``（会话文件区）。
    **它不是"平铺 / 分层"那一档**——从 D20 起会话文件区也按名字里的相对路径分层。"""
    label: str
    """给人看的那句话：「工作区「我的项目」」/「本会话」。"""
    path: str = ""
    parent: str | None = None
    entries: list[FileEntryOut] = Field(default_factory=list)
    truncated: bool = False
    """条目被截断过。界面要如实说"只显示了前 N 项"——
    否则"这个项目只有 300 个文件"与"我只给你看了 300 个"看起来一模一样。"""


class ConversationFileImportIn(BaseModel):
    """把**项目目录**里的一份文件取进这条会话的文件区（D20）。

    只给一个相对路径（项目档那一行给的 key）：落点、名字都归服务端算——
    界面不该也不能决定"复制到哪儿"。路径只走工作区那道闸（绝对路径 / ``..`` /
    符号链接出界都拒），与读文件、预览同一条。
    """

    path: str = Field(min_length=1, max_length=1024)


class IngestArtifactIn(BaseModel):
    """把一份产物存进知识库。**库必须由调用方点明**——服务端不替他挑。"""

    knowledge_base_id: str = Field(min_length=1, max_length=64)


class StorageOverviewOut(BaseModel):
    """存储空间概览（v17，管理员）。

    ``data_bytes + free_bytes`` 就是数据库文件大小——拆成两个数是因为
    "可回收"才是用户能动手改的那部分：删掉的行留下**死元组**，
    ``VACUUM (ANALYZE)`` 之后那部分空间才可被复用。
    **文件本身不会因此变小**（那是 ``VACUUM FULL`` 的事，它要独占重写整库，本项目不做）。
    """

    model_config = _RECORD_CONFIG

    file_bytes: int
    data_bytes: int
    free_bytes: int
    partitions: int
    """向量分区数。每个分区写入第一个向量就占一个 4MB 块，所以它值得单独看。"""
    orphans: list[str] = Field(default_factory=list)
    """无主的向量分区（知识库已删、表还留在库里）。「整理存储」会丢掉它们。"""


# --------------------------------------------------------------------- 切块干预（G3）


class ChunkUpdateIn(BaseModel):
    """改块的正文。

    只允许改文本：标题路径与页码来自解析器的版面分析，用户在这一页没有可对照的
    依据去"修正"它们，开放了只会制造不一致。要改那些应当重新解析。
    """

    text: str = Field(min_length=1)


class ChunkToggleIn(BaseModel):
    """禁用 / 恢复一个块。"""

    disabled: bool


# --------------------------------------------------------------------- 模型注册器（G1）


class ProviderCreateIn(BaseModel):
    """新建供应商。"""

    kind: str = Field(description="llm / embedding / rerank / parser")
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=512)
    enabled: bool = True


class ProviderUpdateIn(BaseModel):
    """改供应商。

    **字段全部可选，且 ``api_key`` 用 ``str | None``**：``None`` 表示"没改"，
    空串表示"清空"。设置页把密钥掩码显示成占位符，用户不动它时前端回传的是掩码；
    若把掩码当新值写库，密钥就被毁了——所以"没改"必须能与"清空"区分开。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class ProviderOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    kind: str
    name: str
    base_url: str
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    api_key_configured: bool = False
    """**只回"配没配"，绝不回密钥本身**（与设置页同一纪律）。"""
    api_key_hint: str = ""
    """掩码后的尾巴，够用户认出"是哪一把"，不足以还原。"""
    model_count: int = 0


class ProviderListOut(BaseModel):
    items: list[ProviderOut]


class ModelRegisterIn(BaseModel):
    """登记一个模型。"""

    provider_id: str
    model_id: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=64)
    dim: int | None = Field(default=None, gt=0)
    capabilities: list[str] = Field(default_factory=list)
    options: dict[str, object] = Field(default_factory=dict)


class ModelUpdateIn(BaseModel):
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=64)
    dim: int | None = Field(default=None, gt=0)
    capabilities: list[str] | None = None
    options: dict[str, object] | None = None


class ModelOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    provider_id: str
    provider_name: str = ""
    provider_kind: str = ""
    model_id: str
    label: str = ""
    dim: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    options: dict[str, object] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    bound_slots: list[str] = Field(default_factory=list)
    """这个模型被哪些用途绑定了——界面上要能一眼看出"它正被用着"。"""


class ModelListOut(BaseModel):
    items: list[ModelOut]


class AvailableModelOut(BaseModel):
    """上游 ``GET /models`` 列表里的一条。

    ``owned_by`` 是给用户辨认的旁注（上游不一定给），不参与任何逻辑。
    """

    model_id: str
    owned_by: str = ""


class AvailableModelsOut(BaseModel):
    """供应商可用模型探测结果。

    **不落库**：上游动辄列出几十上百个模型，全登记进来只是噪声；
    "选哪一个"才是用户的决定（界面据此喂下拉框，仍允许手写）。
    """

    models: list[AvailableModelOut]
    count: int


class SlotOut(BaseModel):
    """一个用途（任务槽位）的当前状态。"""

    slot: str
    label: str
    capability: str
    bound_model_pk: str | None = None
    bound_model_label: str = ""
    provider_name: str = ""
    configured: bool = False
    """最终是否可用于该用途（v0.8 起只看注册表里有没有绑定）。"""
    source: str = "none"
    """``registry`` / ``none``——保留字段以便将来出现第二种来源时不用改接口形状。"""


class SlotBindIn(BaseModel):
    """绑定用途到模型；``model_pk`` 为 ``None`` 表示解绑（回退到设置页配置）。"""

    model_pk: str | None = None


class DataSourceCreateIn(BaseModel):
    """登记一个数据源（M6 / T6.1）。"""

    kind: str = Field(description="html 或 rss")
    name: str = Field(default="", max_length=64)
    url: str = Field(min_length=1, max_length=1024)
    max_items: int | None = Field(default=None, gt=0, le=500)


class DataSourceOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    knowledge_base_id: str
    kind: str
    name: str
    url: str
    max_items: int | None = None
    enabled: bool = True
    etag: str | None = None
    last_pulled_at: datetime | None = None


class DataSourceListOut(BaseModel):
    items: list[DataSourceOut] = Field(default_factory=list)


class SyncResultOut(BaseModel):
    """一次拉取的结果。

    ``duplicates`` 与 ``created`` 分开报：用户看到"取回 20 条但新入库 0 条"
    时该立刻明白"这个源没更新"，而不是以为抓取失败了。
    """

    task_id: str | None = None
    """入队模式返回的任务 id；同步模式为 None。"""
    source_id: str = ""
    fetched: int = 0
    created: int = 0
    duplicates: int = 0
    not_modified: bool = False
    """服务端回了 304：源没有任何变化。"""
    errors: list[str] = Field(default_factory=list)


class TableRowsOut(BaseModel):
    """表格文档的结构化副本（M2 / T2.11）。

    ``rows`` 是**字符串矩阵**而不是对象数组：表格副本刻意不做类型推断
    （``007`` 变成 ``7`` 是数据损失），所以返回时也保持原样。
    """

    document_id: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    total: int = 0
    """该表总行数，与 ``rows`` 长度无关（``rows`` 会被 limit 截断）。"""


class ImpactOut(BaseModel):
    """删除会波及什么（M6 / T6.3）。

    **数字要具体**：说"这会删除该知识库及其内容"没人会有感觉；
    说"3 份文档、412 个切块"才会让人停一下。这是二次确认能有意义的前提。
    """

    kind: str
    id: str
    name: str
    documents: int = 0
    chunks: int = 0
    parts: int = 0
    size_bytes: int = 0
    running_tasks: int = 0
    document_names: list[str] = Field(default_factory=list)
    restorable: bool = True
    """能否从回收站恢复。知识库级删除不可恢复，界面据此显示不同的警示强度。"""


class TrashEntryOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    document_id: str
    kind: str
    expires_at: datetime
    created_at: datetime | None = None


class TrashListOut(BaseModel):
    items: list[TrashEntryOut] = Field(default_factory=list)


class UserCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=64)
    # 账号字段（v10）：带了 username 就是开通账号，不带就是纯名册条目
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str | None = Field(default=None)
    role: Literal["admin", "member"] = "member"


class UserPasswordIn(BaseModel):
    """管理员重置某人的密码。"""

    password: str = Field(min_length=1)


class UserDisabledIn(BaseModel):
    disabled: bool


class UserOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    name: str
    note: str = ""
    username: str | None = None
    role: str = "member"
    disabled: bool = False
    created_at: datetime | None = None
    document_count: int = 0
    """这个人传过多少文档——删他之前要能说清"会影响什么"。"""
    avatar_url: str = ""
    """头像链接（签名 URL，v0.29）。空 = 没有头像 → 界面用名字生成默认头像。"""


class UserListOut(BaseModel):
    items: list[UserOut]
    header: str = ""
    """前端应当把操作者放在哪个请求头里。由后端给出，免得两边各写一份会漂。"""


class UsageBucketOut(BaseModel):
    """一档用量。``day`` / ``kind`` / ``model`` 三选一出，其余留空。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    items: int = 0
    unreported: int = 0
    """这一档里有多少次调用**供应商没报用量**。"""
    estimated: int = 0
    """这一档里有多少次调用是**我们自己估算**的（向量化接口通常不返回用量）。"""


class UsageDayOut(UsageBucketOut):
    day: str


class UsageKindOut(UsageBucketOut):
    kind: str
    label: str


class UsageModelOut(UsageBucketOut):
    model: str


class UsageTotalsOut(UsageBucketOut):
    pass


class UsageOut(BaseModel):
    """模型用量汇总（G7）。

    **刻意不含费用**：单价随供应商与版本变化，内置价目表必然过期，
    而过期的价钱比不给更糟。只给客观的 token 与调用量，让用户自己换算。
    """

    days: int
    total: UsageTotalsOut
    by_day: list[UsageDayOut] = Field(default_factory=list)
    by_kind: list[UsageKindOut] = Field(default_factory=list)
    by_model: list[UsageModelOut] = Field(default_factory=list)
    reported_calls: int = 0
    """实测调用数（供应商真的回了 usage）。"""
    estimated_calls: int = 0
    """估算调用数（我们按字符数估的，主要是向量化）。"""
    unreported_calls: int = 0
    """既没实测也没得估的调用数。"""
    estimated_tokens: int = 0
    """估算出来的 token 总数（已包含在 total 里）。

    **界面必须把这块单独说清**——假精度比没数字更糟：用户会拿估算值
    去做成本判断，而它可能偏离好几倍。
    """


class PresetModelOut(BaseModel):
    """预设里的一条模型建议。"""

    model_id: str
    label: str = ""
    capabilities: list[str] = Field(default_factory=list)
    dim: int | None = None


class ProviderPresetOut(BaseModel):
    """常见供应商预设：一键填好名称 / 类别 / 接口地址与几条常见模型。"""

    id: str
    label: str
    kind: str
    base_url: str
    hint: str = ""
    models: list[PresetModelOut] = Field(default_factory=list)


class RegistryOut(BaseModel):
    """注册器总览：界面一次拿全，免得开设置页要打四个请求。"""

    providers: list[ProviderOut] = Field(default_factory=list)
    models: list[ModelOut] = Field(default_factory=list)
    slots: list[SlotOut] = Field(default_factory=list)
    provider_kinds: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(default_factory=dict)
    provider_presets: list[ProviderPresetOut] = Field(default_factory=list)
    """常见供应商预设。只给"添加供应商"填表单用，**不落库**——用户选了什么才存什么。"""


class WebhookCreateIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    events: list[str] = Field(default_factory=list)
    """留空 = 订阅全部事件。显式列出更安全，但默认全订更省事。"""
    secret: str | None = Field(default=None, max_length=256)
    enabled: bool = True


class WebhookUpdateIn(BaseModel):
    """PATCH 只改开关。

    **单独一个模型而不是复用 ``WebhookCreateIn``**：复用会强迫调用方
    在改开关时也传 ``url``，而那个字段会被静默忽略——"传了但没用"是最迷惑人的
    一类接口。真传了 url 就明确报错（多出来的字段被忽略，见下方注释），
    想换地址请删了重建，那一步是有意识的。
    """

    enabled: bool


class WebhookOut(BaseModel):
    id: str
    url: str
    events: list[str] = Field(default_factory=list)
    enabled: bool = True
    has_secret: bool = False
    secret_masked: str | None = None
    secret: str | None = None
    """**明文只在新建成的那一次返回**，之后永远是 ``null``。

    这不是小气：能反复读到签名密钥就等于签名没有意义——任何能列出订阅的人
    都能伪造一份"验签通过"的载荷。
    """


class WebhookListOut(BaseModel):
    items: list[WebhookOut] = Field(default_factory=list)


class WebhookEventListOut(BaseModel):
    events: list[str] = Field(default_factory=list)
    signature_header: str = ""
    max_attempts: int = 0
    delivery_semantics: str = ""
    """``at-least-once``。**必须让接收端知道这件事**——
    不知道的话它会把重复投递当成故障去查，而那是契约的一部分。"""


# --------------------------------------------------------------------- 笔记（v20）


class NoteCreateIn(BaseModel):
    title: str = Field(default="", max_length=80)
    content_md: str = ""
    source_kind: Literal["manual", "chat", "clip"] = "manual"
    source_ref: str | None = None
    tags: list[str] = Field(default_factory=list)
    folder_id: str | None = None
    """建在哪一层（左栏选中文件夹时点「+」）；留空 = 未归档。"""


class NoteUpdateIn(BaseModel):
    """全部字段可空：只传要改的字段。``None`` = 不动这一项。

    **刻意没有 ``folder_id``**：归属走 ``PATCH /notes/{id}/folder``。
    编辑器每 800ms 自动保存一次，如果归属也走这条路径，草稿里那份旧的 folder_id
    会把用户在左栏刚移好的位置刷回去（理由详见服务层 ``NotesService.move_note``）。
    """

    title: str | None = Field(default=None, max_length=80)
    content_md: str | None = None
    pinned: bool | None = None
    tags: list[str] | None = None


class NoteMoveIn(BaseModel):
    """把笔记移动到某个文件夹；``folder_id=None`` = 移回未归档。"""

    folder_id: str | None = None


class NoteFolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    parent_id: str | None = None
    """父文件夹；留空 = 建在根级。"""


class NoteFolderRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class NoteFolderParentIn(BaseModel):
    """把文件夹移动到某个文件夹下；``parent_id=None`` = 挪回根级。"""

    parent_id: str | None = None


class NoteFolderOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    name: str
    parent_id: str | None = None
    note_count: int = 0
    """这个文件夹里**直接**有多少篇笔记（不含子文件夹里的）。"""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NoteFolderListOut(BaseModel):
    """左栏那棵树的读数：文件夹 + 三个数字（未归档 / 总数）一次给全。

    **为什么不只给树**：树上每个节点都要显示条数，未归档与"全部"也各要一个，
    而它们每次移动笔记都会一起变；拆成"列表 + 额外两次计数请求"只会让
    三个数字有机会对不上（用户看到的是一棵树，数字就该是同一时刻的）。
    """

    items: list[NoteFolderOut] = Field(default_factory=list)
    unfiled_count: int = 0
    total_count: int = 0


class NoteAttachIn(BaseModel):
    kb_id: str = Field(min_length=1)


class NoteOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    title: str
    content_md: str
    source_kind: str
    source_ref: str | None = None
    kb_id: str | None = None
    doc_id: str | None = None
    folder_id: str | None = None
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NoteListItemOut(NoteOut):
    """列表项不带正文：列表页只要标题、标签与时间，带上正文会让响应体积翻很多倍。"""

    content_md: str = ""
    preview: str = ""


class NoteListOut(BaseModel):
    items: list[NoteListItemOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0


class NoteTagOut(BaseModel):
    tag: str
    count: int


class NoteTagListOut(BaseModel):
    items: list[NoteTagOut] = Field(default_factory=list)


class NoteAiIn(BaseModel):
    """笔记 AI 处理请求。

    ``action`` 三档：``format`` 只排版 / ``polish`` 只润色 / ``both`` 两者一起。
    """

    action: Literal["format", "polish", "both"]
    model_pk: str | None = None
    """用哪个对话模型；留空用全局默认。"""


class NoteAiOut(BaseModel):
    content_md: str
    """处理后的 Markdown。**不自动落库**：由调用方决定要不要写回，用户可先看再存。"""


class NoteImageOut(BaseModel):
    url: str
    """可直接放进 ``<img src>`` 的相对地址（带签名，无需自定义请求头）。"""

    name: str
    alt: str


# --------------------------------------------------------------------- Wiki（v24）


class WikiSourceOut(BaseModel):
    """Wiki 页面的一条出处。``index`` 就是正文里 ``[n]`` 的 n。"""

    index: int
    chunk_id: str
    document_id: str
    document_name: str = ""
    """后端解析好的文档名——前端拿 id 还得再查一次。"""
    heading_path: str | None = None
    page: int | None = None


class WikiPageOut(BaseModel):
    """目录里的一个页面（不含正文，导航树只需要这些）。"""

    id: str
    parent_id: str | None = None
    level: int = 0
    ord: int = 0
    title: str
    brief: str = ""
    status: str = "ready"
    generated_at: datetime | None = None


class WikiOverviewOut(BaseModel):
    """Wiki 页头需要的全部信息 + 页面目录。

    ``status`` 只有四档（``idle`` / ``generating`` / ``ready`` / ``failed``），
    **由后端推出来**（有没有在跑的任务 + 有没有页面），前端不自己组合状态。
    """

    kb_id: str
    enabled: bool
    status: str
    page_count: int = 0
    generated_at: datetime | None = None
    model: str | None = None
    last_error: str | None = None
    """上次失败的原文（任务里的 error）。只在 ``status='failed'`` 时有值。"""
    pages: list[WikiPageOut] = Field(default_factory=list)


class WikiPageDetailOut(WikiPageOut):
    kb_id: str
    content_md: str = ""
    model: str | None = None
    updated_at: datetime | None = None
    sources: list[WikiSourceOut] = Field(default_factory=list)


class WikiGenerateOut(BaseModel):
    task_id: str
    kb_id: str


class TaskCancelIn(BaseModel):
    """取消还没结束的任务（v24）。

    **两种用法**，都支持：

    - 点名取消：给 ``task_ids``；
    - 一键清空排队：不给 ids，只给 ``state``（默认 ``pending``），
      服务端按调用方**可见范围**解析出全部候选——这才是"几十条堵在队列里"时
      真正想点的那个按钮。
    """

    task_ids: list[str] = Field(default_factory=list, max_length=500)
    state: Literal["pending", "running", "all"] = "pending"


class TaskCancelItemOut(BaseModel):
    task_id: str
    ok: bool
    error: str | None = None


class TaskCancelOut(BaseModel):
    """逐条结果：批量里"30 条撤下 28 条"是正常结果，界面要能指出剩下两条为什么没成。"""

    succeeded: int
    failed: int
    items: list[TaskCancelItemOut] = Field(default_factory=list)


class TimelineStepOut(BaseModel):
    """时间线上的一个环节。"""

    key: str
    label: str
    status: Literal["done", "running", "pending", "failed", "canceled"]
    duration_ms: int
    visits: int
    """进入过几次；>1 = 重试或重新摄入过。"""
    error: str | None = None


class DocumentTimelineOut(BaseModel):
    """一篇文档的处理进度：共几步、现在第几步、共耗时多少、每步各花多久。

    **不给百分比**：摄入的环节耗时不均（解析可能几分钟、切分几秒），
    百分比只会编出一个骗人的数字；"第 3/6 步 + 每步实际耗时"才是能对得上的信息。
    """

    document_id: str
    status: Literal["running", "done", "failed", "canceled"]
    current_index: int
    step_total: int
    total_ms: int
    steps: list[TimelineStepOut] = Field(default_factory=list)
    stalled: bool = False
    """执行租约已过期 = 没有 worker 在续约（§12.115）。

    抽屉要显示它，而它是**唯一能确定说"卡住"**的判据——光看"某一步跑了一小时"
    说明不了问题（解析大文件本来就慢）。判据来自 ``ObservabilityService``，
    与任务中心那一列同源。"""


# ------------------------------------------------------------------ 沙箱（v0.16）


class SandboxCapabilityOut(BaseModel):
    """这台机器上的内核级隔离能力。

    ``direct`` 是**降级档**（v0.55）：没有真隔离且没开严格模式时，就是"直接在本机执行、
    未隔离"。它不是沙箱，`detail` 里会如实说明。
    """

    backend: Literal["bwrap", "sandbox-exec", "docker", "direct", "none"]
    available: bool
    detail: str = ""
    """人话说明（为什么可用/不可用）。界面直接显示它。"""
    max_output_chars: int = 0
    default_timeout_seconds: float = 0


class SandboxExecIn(BaseModel):
    """一次沙箱执行。``argv`` 是**命令数组**（不是 shell 字符串）——
    数组没有引号解析、没有管道、没有重定向，省掉一整类注入面。"""

    argv: list[str] = Field(min_length=1, max_length=64)
    workspace_id: str | None = None
    """在哪个工作区里跑。不给就只在沙箱里跑，不挂任何真实工作区。"""
    session_id: str = ""
    """沙箱按会话分（同一会话的多次尝试共享一份，见设计文档 §4）。"""
    allow_network: bool = False
    """**默认断网**：Agent 跑的命令绝大多数不需要网络，
    而"能联网"是数据外泄那条路上最省事的一环。"""
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    approved: bool = False
    """``ask`` 政策下第一次不带它（回 409），用户确认后再带上重调。"""
    remember: bool = False
    """确认之后**把这条规则写进放行清单**（「以后都允许」）。

    写入的是**建议的词前缀**（``Bash(git status:*)``）而不是完整命令——
    记住完整命令等于没记住（下次参数就不同了）。
    """


class SandboxPlanOut(BaseModel):
    """隔离后的命令行——**给用户核对的**。"""

    backend: str
    available: bool
    detail: str = ""
    argv: list[str] = Field(default_factory=list)
    workdir: str = ""


class SandboxExecOut(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    backend: str = ""


# ------------------------------------------------------------------ 工作区（v0.15）


class WorkspaceCreateIn(BaseModel):
    """新建工作区。``root_path`` 是**用户指定的真实目录**——
    这是"可以指定路径作为工作区"的落点。"""

    name: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1, max_length=1000)
    """根目录的绝对路径（或带 ``~``）。服务端校验：存在、是目录、
    不是文件系统根、不指向数据目录。"""
    description: str = Field(default="", max_length=500)
    kb_ids: list[str] = Field(default_factory=list)
    """这个工作区绑定的知识库（"知识库与 Agent 天生融合"的落点）。
    新会话默认继承它们，所以用户不必每开一次会话重勾一遍。"""


class WorkspaceUpdateIn(BaseModel):
    """改工作区。**都可选**，只改传了的那些（``None`` = 不动）。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    root_path: str | None = Field(default=None, min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=500)
    kb_ids: list[str] | None = None
    archived: bool | None = None
    """归档 / 取消归档（v0.55）。**不是删除**：里面的会话与内容都还在。
    与会话那条同一口径（`ConversationUpdateIn.archived`）。"""


class WorkspaceOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    name: str
    root_path: str
    description: str = ""
    kb_ids: list[str] = Field(default_factory=list)
    conversation_count: int = 0
    """这个工作区下的会话数。侧栏每个工作区后面那个数字。"""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None
    """归档时间（v0.55）。``None`` = 未归档。"""
    device_id: str | None = None
    """归属设备（v0.59）。``None`` = **服务器端**：这个项目的 ``root_path`` 在服务器的
    盘上（网页版/直连 API 建的）。非空 = 桌面壳那台机器（``X-Kylab-Device``）。"""
    device_name: str = ""
    """设备名（``X-Kylab-Device-Name``，可空）。只给人看，判定按 ``device_id``。"""


class WorkspaceListOut(BaseModel):
    items: list[WorkspaceOut] = Field(default_factory=list)


class DirectoryEntryOut(BaseModel):
    """目录浏览里的一行：一个子目录，或一个"起点"。

    **三对字段**（v0.41）：能不能选、能不能在它里面新建目录、能不能给它改名——
    各带各的原因。合成一个 ``reason`` 会让人分不清"不能选"还是"不能建"，
    而这两件事的下一步动作不一样（换一个目录 vs 换一个地方动手）。"""

    name: str
    path: str
    selectable: bool = True
    """能不能直接拿它当工作区。**与建工作区时同一份判定**（``root_path_problem``），
    所以界面上灰掉的那些，点"选择"也一定建不出来。"""
    reason: str = ""
    """不能选的原因（原样显示给用户）。"""
    creatable: bool = True
    """能不能在**它里面**新建目录（判定在 ``workspace.create_problem``）。

    **除了数据目录树都是真**（v0.58）：文件系统根、区域外的任意目录、用户自己挑的
    自定义路径都能建。这里标的是"这一条规则允不允许"，**不是"写性探测的结果"**——
    能不能真的写进去由那次真实的 ``mkdir`` 回答，界面照旧会拿到一句具体的失败原因。
    界面据此把"新建文件夹"灰掉并把 ``create_reason`` 摆出来。"""
    create_reason: str = ""
    """不能在它里面新建目录的原因（原样显示）。"""
    renamable: bool = True
    """能不能给它改名（判定在 ``workspace.rename_problem``）。"""
    rename_reason: str = ""
    """不能改名的原因（原样显示）。"""


class DirectoryCreateIn(BaseModel):
    """在服务器上新建一个目录（``POST /workspaces/dirs``，v0.36）。"""

    parent: str = Field(min_length=1, max_length=1000)
    """建在哪一层（绝对路径，来自浏览接口给的 ``path``）。"""
    name: str = Field(min_length=1, max_length=80)
    """新目录名。**只建一层**：名字里带分隔符会当场被拒，不替你递归造父目录。"""


class DirectoryRenameIn(BaseModel):
    """给服务器上的一个目录改名（``PATCH /workspaces/dirs``，v0.36）。**只改名，不搬位置**。"""

    path: str = Field(min_length=1, max_length=1000)
    """要改名的那个目录（绝对路径）。"""
    name: str = Field(min_length=1, max_length=80)
    """新名字。"""


class WorkspaceBrowseOut(BaseModel):
    """浏览服务器目录的结果（``GET /workspaces/browse``，v0.35）。

    **为什么这件事在服务端做**：工作区根目录是**服务器上**的路径（后端跑在 NAS 上），
    而浏览器既拿不到、也不该拿到服务器上的绝对路径——客户端的目录选择器指向的是
    另一台机器。所以"选择"只能是"服务端列给你看"。"""

    path: str
    current: DirectoryEntryOut
    """**当前这一层自己**（名字 + 能不能选 + 不能选的原因 + 能不能建 + 不能建的原因）。

    服务端给而不是让界面自己判：那几条判定只有一份，界面再猜一次就会出现
    "按钮亮着、点了却建不出来"。"""
    parent: str | None = None
    """上一级；已经在最上层时为 ``None``（界面把"上一级"置灰）。"""
    entries: list[DirectoryEntryOut] = Field(default_factory=list)
    roots: list[DirectoryEntryOut] = Field(default_factory=list)
    """起点（**专用区域** / 家目录 / 盘符 / 已有工作区的目录）：路径很深时不用从根一路点下来。"""
    note: str = ""
    """一句人话说明（"共有 N 个，只列了前 M 个"这类）。空串 = 没什么要说的。"""
    area: str = ""
    """专用可写区域（``<data_dir>/workspaces``）：选择器的默认落脚点，也是**改名**的
    唯一范围（v0.58 起新建不限区域，除了数据目录树）。

    界面用它认出"现在站的这一层就是区域"（给一句"在这里可以新建 / 改名"的提示）。
    由服务端给而不是前端拼：数据目录在哪只有服务端知道。"""


# ------------------------------------------------------------------ MCP（v0.15）


class MCPServerCreateIn(BaseModel):
    """登记一个外部 MCP 服务。见《Agent-工作区与能力层设计》§6.2。"""

    name: str = Field(min_length=1, max_length=120)
    transport: Literal["stdio", "http"]
    """``stdio`` 会**起一个本地子进程**；``http`` 连远端服务。"""
    target: str = Field(min_length=1, max_length=1000)
    """stdio = 要执行的命令（如 ``python``）；http = 服务的 URL。"""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    """子进程的环境变量。**可能含凭据，接口不回显它的值。**"""
    headers: dict[str, str] = Field(default_factory=dict)
    """http 服务的请求头（如 ``Authorization``）。同上，不回显。"""
    policy: Literal["allow", "ask", "deny"] = "ask"
    """**默认 ask**：外部工具会以用户的名义执行动作，接进来就默认静默执行
    是这一层最不该有的默认。"""


class MCPServerUpdateIn(BaseModel):
    """改配置。**凭据是整份替换**（合并语义下"删掉"与"没传"分不开）。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    transport: Literal["stdio", "http"] | None = None
    target: str | None = Field(default=None, min_length=1, max_length=1000)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    policy: Literal["allow", "ask", "deny"] | None = None
    enabled: bool | None = None


class MCPToolOut(BaseModel):
    name: str
    qualified: str
    """限定名 ``mcp__<服务>__<工具>``：外部工具名我们无法约束，
    撞上内置工具（``search``）会让"在调哪个"变得无解。"""
    description: str = ""
    server_id: str = ""
    server_name: str = ""


class MCPServerOut(BaseModel):
    id: str
    name: str
    transport: Literal["stdio", "http"]
    target: str
    args: list[str] = Field(default_factory=list)
    policy: Literal["allow", "ask", "deny"] = "ask"
    enabled: bool = True
    secret_keys: list[str] = Field(default_factory=list)
    """配过凭据的**键名**（不含值）。界面据此显示"已配置"。"""
    has_secrets: bool = False
    tool_prefix: str = ""
    """该服务工具的限定名前缀，界面上照它拼全名。"""
    reachable: bool | None = None
    """只有 ``probe`` 会给：``None`` = 没探过（列表里就是这样）。"""
    detail: str = ""
    tools: list[MCPToolOut] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MCPServerListOut(BaseModel):
    items: list[MCPServerOut] = Field(default_factory=list)


class MCPCallIn(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    approved: bool = False
    """``ask`` 策略下：第一次调用不带它（回 409），用户确认后再带上它重调。"""
    remember: bool = False
    """确认之后把这条工具的规则写进放行清单（「以后都允许」）。"""


class MCPCallOut(BaseModel):
    server_id: str
    tool: str
    text: str
    """工具返回的**文本**内容。非文本（图片、资源引用）不往上下文里塞。"""


# ------------------------------------------------------------------- 技能（v0.15）


class SkillOut(BaseModel):
    """一个技能（列表项，不含正文）。"""

    name: str
    description: str = ""
    summary: str = ""
    """**中文简介**（v0.28）。空 = 没有（随代码发布的、手放的技能都没有）。

    它只是界面上的那一行说明：技能的 ``description`` 一个字都不改——
    那是模型判断"何时该用"的触发文本（见 services/skill_blurb.py）。"""
    source: Literal["builtin", "user", "agents"] = "builtin"
    """``builtin`` = 随代码发布（仓库 ``skills/``）；``user`` = 数据目录 ``data/skills/`` 里
    用户放的；``agents`` = ``~/.agents/skills``（跨工具共享的用户级目录，P0-3）。"""
    path: str = ""
    """``SKILL.md`` 的绝对路径。排错时要能找到它。"""
    directory: str = ""
    """技能目录（``references/`` 相对它解析）。"""
    used_by_prompt: bool = True
    """会不会进 system prompt 的目录。被安全扫描拦下、依赖没满足、被丢弃的都是 false。"""
    enabled: bool = True
    """**用户**没把它关掉（D23）。

    与 ``used_by_prompt`` 分开：那个是"实际进没进"（安全扫描、依赖、丢弃都会让它 false），
    这个是"用户的开关在哪一边"。两者都可能为 false 而原因不同——能力页上那颗开关
    只能照着 ``enabled`` 画，理由那一栏照旧读 ``flagged``。
    """
    flagged: list[str] = Field(default_factory=list)
    """没进目录的原因（人话）。空 = 没问题。"""
    category: str = ""
    """**分类**（v0.61）：`skill_categories` 里登记过的 slug（`slides` / `code` / `other`…）。

    技能自己**没有**这个字段（SKILL.md 的格式里没有分类，第三方的 `tags:` 也不认），
    它是按一套可复现的加权信号算出来的（`services/skill_categories.py`），
    与页面读的那张离线映射同源。空串 = 没经过分类（只有手工构造的记录会这样）。

    它**不进提示词目录**：目录里那一行是给模型判断"何时该用"的，分类是给人分组看的。
    """
    featured: bool = False
    """是不是**本类精选**（v0.61：每类前 2 条，判据见 `services/skills.featured_by_category`）。

    **只在列表端点里有意义**：详情端点是"看这一条"，不参与"每类排前 2"的评选，
    那里恒为 `false`。精选**不进提示词目录**——它只回答"界面默认该摆哪几条"。
    """
    discarded: bool = False
    """**被丢弃**（P0-3）：frontmatter 缺 ``name``/``description`` 或描述超长，
    照 ZCode 的规则整个技能不加载。它仍然出现在列表里（带着 ``flagged`` 那条理由），
    但既不进提示词，也读不出正文——能力页要能看见"装了但没通过校验"的那些。"""


class SkillEnabledIn(BaseModel):
    """开/关一条技能（D23）。"""

    model_config = _RECORD_CONFIG

    enabled: bool
    """``False`` = 关掉：不进提示词，模型也不知道有它（文件不动）。"""


class SkillDetailOut(SkillOut):
    body: str = ""
    """正文（frontmatter 之后的部分）。**按需展开的那一段**。"""


class SkillMarketIn(BaseModel):
    """浏览一个源。``source`` 可以是目录 / ``catalog.json`` / 它们的 URL。"""

    source: str = Field(min_length=1, max_length=2000)


class SkillMarketEntryOut(BaseModel):
    name: str
    description: str = ""
    source: str = ""
    installed: bool = False
    """已经装过——界面上据此把"安装"按钮换成"已安装"。"""


class SkillMarketOut(BaseModel):
    source: str
    items: list[SkillMarketEntryOut] = Field(default_factory=list)


class SkillInstallIn(BaseModel):
    """安装一个技能。"""

    name: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=2000)
    """目录 / zip / URL。三种形态服务端都认（见 services/skill_market.py）。"""
    catalog: str = ""
    """可选的源目录：``source`` 只是源里的条目名时，由它定位。"""


class SkillInstalledOut(BaseModel):
    """已装清单：``技能名 → 来源``。"""

    items: dict[str, str] = Field(default_factory=dict)
    total: int = 0


# --------------------------------------------------------------------- 技能源（v0.27）
#
# 与上面那组"市场"的区别：那组面向**一个源地址**（目录 / zip / catalog.json），
# 这组面向**一个线上仓库**（浏览 → 看清单 → 按 SHA 装）。
# 调研见《技能仓库与技能市场调研-v0.1》§4.6。


class SkillSourceOut(BaseModel):
    """一个可浏览的技能源（就是"一个 GitHub 仓库"）。"""

    id: str
    name: str
    repo: str
    """``owner/repo``。**与显示名分开**：名字会改，仓库地址不会。"""
    ref: str = ""
    """分支 / 标签。空 = 用仓库的默认分支。"""
    subpath: str = ""
    """只在这个子目录里找技能（空 = 全仓递归扫）。"""
    builtin: bool = True
    enabled: bool = True
    why: str = ""
    """为什么内置它（一句话）。空 = 用户自己加的源。"""


class SkillSourceListOut(BaseModel):
    items: list[SkillSourceOut] = Field(default_factory=list)


class SkillSourceIn(BaseModel):
    """添加一个自定义源。``repo`` 认 ``owner/repo`` 与 GitHub 的仓库 / 子目录 URL。"""

    repo: str = Field(min_length=1, max_length=500)


class SkillSourcePatchIn(BaseModel):
    enabled: bool


class MarketSkillOut(BaseModel):
    """浏览结果里的一条（还没装）。"""

    name: str
    description: str = ""
    summary: str = ""
    """中文简介（v0.28）。空 = 没翻成，界面退回 ``description``。"""
    path: str
    """技能目录在仓库里的相对路径（安装时按它取文件）。"""
    source_id: str = ""
    repo: str = ""
    installed: bool = False


class SkillBrowseIn(BaseModel):
    source_id: str = Field(min_length=1, max_length=120)
    refresh: bool = False
    """忽略缓存重新抓一次。**默认用缓存**：GitHub 匿名配额 60 次/小时。"""


class SkillBrowseOut(BaseModel):
    source: SkillSourceOut
    items: list[MarketSkillOut] = Field(default_factory=list)
    cached: bool = True
    """这一份是不是缓存——界面上要说清楚"看到的是几小时前的清单"。"""


class SkillFileOut(BaseModel):
    """技能目录里的一个文件。``kind`` = doc / code / asset。"""

    path: str
    size: int = 0
    kind: str = "doc"


class SkillInspectIn(BaseModel):
    source_id: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)


class SkillBundleOut(BaseModel):
    """**装之前**摊给用户看的那一份：文件清单 + 版本 + 体积。"""

    source_id: str
    repo: str
    sha: str = ""
    """40 位 commit SHA。安装按它取——分支会在两步之间变。"""
    path: str
    name: str
    description: str = ""
    ref: str = ""
    license: str = ""
    files: list[SkillFileOut] = Field(default_factory=list)
    total_bytes: int = 0
    summary: str = ""
    """中文简介（v0.28）：装完会跟着记进安装清单，能力页上还能看到。"""
    code_count: int = 0
    """其中会被当作代码执行的文件数。装之前要显眼地提示。"""
    truncated: bool = False
    """仓库太大、GitHub 的文件树被截断：**清单可能不全**，界面要如实说。"""


class SkillSourceInstallIn(BaseModel):
    """从线上源安装：``source_id`` + 浏览结果里的 ``path``。"""

    source_id: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)


class SkillCategoryOut(BaseModel):
    """一个分类（v0.61）：页面按它分组，`featured` 是默认要摆出来的那几条。"""

    slug: str
    """稳定标识（`slides` / `documents` / `code`…），技能的 `category` 就是它的取值。"""
    label: str
    """给人看的中文名（`演示与幻灯片`…）。"""
    total: int = 0
    """**全库**落在这一类的条数（与 `usable` 同一口径：不是这一页的，也不只算可用的）。"""
    featured: list[str] = Field(default_factory=list)
    """这一类默认展示的技能名（每类最多 2 条，某类不够就有几条给几条）。

    只从**能用**的里挑（被安全扫描拦下 / 依赖没满足 / 被关掉 / 被丢弃的不参选），
    排序判据是机械可复现的（内置 → 装进来的 → 描述完整度 → 名字可读性）。"""


class SkillListOut(BaseModel):
    items: list[SkillOut] = Field(default_factory=list)
    usable: int = 0
    """其中真正会进模型目录的条数——界面上一眼看出"装了 N 个，能用 M 个"。"""
    categories: list[SkillCategoryOut] = Field(default_factory=list)
    """分类清单（v0.61）：**顺序就是界面分组的顺序**（`skill_categories.CATEGORIES`）。

    它回答的是"界面默认摆什么"：每类给一个中文名、一个全库条数、两条精选
    （见 `SkillCategoryOut`）。**新增字段，老客户端不读它照旧**——
    技能的 `category` / `featured` 两位也一样，都是只增不改。"""


# ------------------------------------------------------------------ 斜杠命令（P1-2）


class CommandOut(BaseModel):
    """一条斜杠命令（照 ZCode 的内置表 + ``commands/*.md`` 自定义命令）。

    前四个字段 ``{name, summary, usage, group}`` 就是前端那个 ``/`` 菜单吃的东西
    （``details`` 给 ``/help <命令名>`` 展开用；``shadowed_by`` / ``error`` 是排错用的）。
    技能注册来的那批也在里面（``/技能名 [任务]``）——它们取 ``group="skill"``：
    技能是**单独一档**（``CommandDef.group``），不混进"内置 / 你放的 / 随代码发布"
    里，那三档装不下二十多条命令、也辨认不出技能。
    """

    name: str
    summary: str = ""
    """一句话：这条命令是干什么的（菜单那一行）。

    **技能那批是短的**：有中文简介用它，没有就取技能描述的第一句并截到约 40 个字
    （``commands.SKILL_SUMMARY_CHARS``）——技能的 ``description`` 是给模型看的
    触发文本，整段塞进菜单会被前端再截一次，读起来只剩一句半。
    """
    usage: str = ""
    """怎么用（形如 ``/mode [goal|plan]``）。"""
    group: Literal["builtin", "user", "repo", "skill"] = "builtin"
    """菜单分组：**内置 / 你放的（数据目录 commands/）/ 随代码发布 / 技能**。"""
    details: list[str] = Field(default_factory=list)
    """展开说明（``/help <命令名>`` 用它，与菜单同一份数据）。"""
    argument_hint: str = ""
    """自定义命令 frontmatter 里的 ``argument-hint``（照 ZCode）。"""
    short_circuit: bool = True
    """**这条通常要不要模型**：为真的是 ``/help`` ``/mode`` 这一类。

    **界面不拿它分流**（见 ``_CommandResult.short_circuit`` 与前端 ``ChatView.runCommand``）：
    它是**表级**的保守口径，而 ``/plan`` 是"看有没有参数"的两面派——不带描述时只是切档，
    带上描述时那段描述就是这一轮的提示。真正的判据是**这一轮的结果**：出了内容
    （步骤 / 出处 / 正文）就按普通一轮渲染，没出才算"只回一句系统提示"。
    """
    shadowed_by: str = ""
    """**被谁遮蔽**（空 = 没被遮蔽）：同名时内置 > 用户 > 仓库 > 技能，first match wins。
    被遮蔽的**仍然在列表里**（照插件列表的做法）——静默藏掉会让人以为文件没生效。"""
    error: str = ""
    """加载失败的原因（人话）。空 = 没问题。失败的也留在列表里，带原因。"""
    path: str = ""
    """md／``SKILL.md`` 的绝对路径（内置命令为空）。排错时要能找到它。"""


class CommandListOut(BaseModel):
    """``GET /api/v1/chat/commands`` 的返回：菜单 + 两条发现源。"""

    items: list[CommandOut] = Field(default_factory=list)
    total: int = 0
    user_dir: str = ""
    builtin_dir: str = ""
    """两条发现源——**放进 ``user_dir`` 的 md 文件就是一个命令**（零注册、零重启）。
    空串 = 这个目录不存在，扫描时跳过。"""


class ChatCommandEventOut(BaseModel):
    """短路类命令那一轮的事件体（SSE 里的 ``type=command``，P1-2）。

    **刻意不进 OpenAPI**：这条流没有 ``response_model``（SSE 是一串裸载荷），
    所以这个类不是"接口文档"，而是**契约的唯一落点**——协议层按它拼载荷、
    前端的 ``ChatCommandResult`` 按它对齐，两边都改的时候有个共同的地方可看。

    两个可选的字段（``action`` / ``refill``）**没有就不出现在载荷里**：
    老客户端不认它们时行为一个字都不变（``wire()`` 那道判断就是这件事的实现）。
    """

    type: Literal["command"] = "command"
    name: str
    """命令名（``/help`` → ``help``）。"""
    text: str = ""
    """回给用户看的那段话（界面按普通文本渲染，**不进模型上下文**）。"""
    ok: bool = True
    """失败的命令也走这条事件：那句解释就是回话，不是 HTTP 500。"""
    action: dict[str, object] | None = None
    """界面要顺手做的事（开新会话 / 切到某档 / 停掉这一轮 / 换模型）。"""
    refill: str = ""
    """要**回填到输入框**的文字（``/rewind`` 交回被撤掉的那句提问）。

    与前端约定死的可选字段：有就填上（用户改一版就能重发，Claude/Gemini 里
    ``/rewind`` 的手感），没有就什么都不做。
    """

    def wire(self) -> dict[str, object]:
        """给 SSE 用的那一条：**可选字段没有就不出现**（向后兼容的唯一实现处）。

        不用 ``model_dump(exclude_none=True)``：那样 ``action`` 里的 ``None`` 会被
        剔掉，而 ``/model`` 那条动作里 ``kind`` 与 ``model_pk`` 是并列的，
        "哪些字段该永远在、哪些该消失"是这条流的语义，值得写出来而不是靠默认行为。
        """
        event: dict[str, object] = {
            "type": self.type,
            "name": self.name,
            "text": self.text,
            "ok": self.ok,
        }
        if self.action:
            event["action"] = dict(self.action)
        if self.refill:
            event["refill"] = self.refill
        return event


# ------------------------------------------------------------------ 插件包（v0.43）


class PluginComponentOut(BaseModel):
    """插件提供的一样东西（四类能力面之一）。

    照 QwenPaw 的 ``register(api)`` 四类收窄而来（provider / 生命周期 hook /
    控制命令 / 工具配置），见 `docs/设计/插件与技能-v0.1.md` §3。
    """

    kind: Literal["skill", "command", "hook", "tool"]
    name: str
    description: str = ""
    path: str = ""
    """相对插件根的路径（``tool`` 那一类就是 manifest 里写的 ``entry``）。"""
    status: str = ""
    """这一类**现在到底能不能用**。做不到的明说"未实现"——四类都还没接执行。"""


class PluginOut(BaseModel):
    """一个插件（本地市场里的一条）。

    **加载失败的也在列表里**（``loaded=false`` 且 ``error`` 非空），照 DSH
    "失败的 preset 也列出"：静默藏掉会让用户以为插件没装上。
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    source: Literal["builtin", "user"] = "user"
    """``user`` = 数据目录里用户放的（本地市场）；``builtin`` = 随代码发布。"""
    path: str = ""
    """插件目录的绝对路径。"""
    manifest_path: str = ""
    enabled: bool = True
    blocked: bool = False
    """内置且被用户屏蔽（照 ZCode 的屏蔽标记）：**不是删除**，只是不要再启用。"""
    loaded: bool = True
    """校验过了没有。``false`` 时 ``error`` 一定非空。"""
    error: str = ""
    """加载失败的原因（人话）。空 = 加载成功。"""
    components: list[PluginComponentOut] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    """提供了哪几类能力（``skill``/``command``/``hook``/``tool`` 的子集）。"""
    user_config: list[str] = Field(default_factory=list)
    """manifest 里 ``userConfig`` 声明的字段名。**本轮只登记名字**（录入未实现）。"""


class PluginListOut(BaseModel):
    items: list[PluginOut] = Field(default_factory=list)
    total: int = 0
    enabled: int = 0
    failed: int = 0
    """加载失败的条数。它们**也在 items 里**（带着原因），这个数只是给状态栏用。"""
    user_dir: str = ""
    builtin_dir: str = ""
    """两条发现源——**本地市场就是这两个目录**（放进来的目录就是"上架"）。
    空串 = 这个目录不存在，扫描时跳过。"""


# --------------------------------------------------------------------- 记忆（v0.14 三期）


class MemoryFileOut(BaseModel):
    """记忆工作区里的一个文件（列表项，不含正文）。"""

    path: str
    name: str
    title: str
    kind: Literal["core", "daily", "digest", "other"]
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    size_bytes: int = 0
    modified_at: str = ""

    injected: bool = False
    """每轮对话会不会被注入 system prompt（`SOUL.md` / 档案 / `AGENTS.md`）。

    ``MEMORY.md`` **不在里面**：它已经退场（§7.2），内容折进了档案；
    界面上它据此显示成"旧记忆（只读）"。"""


class MemoryFilesOut(BaseModel):
    files: list[MemoryFileOut] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    """文件数达到扫描上限被截断了——列表不完整这件事要让用户知道。"""


class MemoryFileDetailOut(MemoryFileOut):
    content: str = ""
    """原文，**含 frontmatter**：只读展示要逐字还原，不能因为我们"顺手格式化"
    而丢掉用户手写的东西。"""

    meta: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False


class MemoryStatusOut(BaseModel):
    """记忆层的状态。**全是本地数字**（v0.46）：

    没有"连没连上"这一项——记忆跑在我们自己的进程里，没有第二个进程可连。
    原先那对 ``base_url`` / ``reachable``（三态）随 ReMe 一起删了：
    它们的存在只为了让界面说清"服务在不在"，而现在这件事不存在。
    """

    enabled: bool
    workspace: str = ""
    core_file_exists: bool = False
    detail: str = ""
    file_count: int = 0
    """工作区里的记忆文件份数。"""

    last_changed_at: str = ""
    """记忆内容最后一次改动的时间（界面上写的"上次更新"）。"""


class MemoryOverviewOut(BaseModel):
    status: MemoryStatusOut
    files: list[MemoryFileOut] = Field(default_factory=list)
    truncated: bool = False


class MemoryRecallIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int | None = Field(default=None, ge=1, le=MAX_MEMORY_RECALL)


class MemoryHitOut(BaseModel):
    text: str
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    score: float | None = None
    """排序用的分。**只在本条查询内可比**：它跟工作区里有几块正文有关，
    不是归一化的量——所以界面上不要拿它当"相关度百分比"读。"""

    coverage: float | None = None
    """命中判据：查询里的字对有多少比例在这条记录里出现过（0–1）。

    与分数量纲不同，这个是**归一化**的：它决定"算不算命中"
    （见 ``archive.MIN_CHANGE_COVERAGE``），分数只决定排在第几条。
    """

    source: str = "text"
    """这条是怎么被找到的：**变更流里永远是 ``text``**。

    字段留着是因为 REST 与外部客户端一直按同一个形状读（v0.50 起它用来区分
    词面命中与语义命中，而语义那一路已随档案制退场）。"""


class MemoryRecallOut(BaseModel):
    query: str
    hits: list[MemoryHitOut] = Field(default_factory=list)
    note: str = ""
    """一句话提醒这是记忆而不是知识库原文（与 MCP 那份同口径）。"""


class MemoryRememberIn(BaseModel):
    """写一条进档案（§7.4 的 ``POST /memory/remember``）。

    ``section`` 留空或不认识时服务层按内容机械归区；``replaces`` 是"更正一次完成"
    的入口（要顶替的那条原文）。``content`` 的 ``max_length`` 只是协议层的一道
    粗护栏（500），**真正的单条上限是 120 字**——它由服务层以**回执**的形式拒绝
    （"请拆成两条，或写进 AGENTS.md"），不是 422：被拒时调用方要拿到那条出路。
    """

    content: str = Field(min_length=1, max_length=MAX_MEMORY_ENTRY_CHARS)
    section: str = Field(default="", max_length=40)
    replaces: str = Field(default="", max_length=MAX_MEMORY_ENTRY_CHARS)


class MemoryRememberOut(BaseModel):
    """一次写入的结果与回执（§4.4）。

    ``action`` 四种：``added`` / ``replaced`` / ``existing`` / ``rejected``
    （``forget`` 那条路还会给 ``forgotten``）。``receipt`` 是**给人看的那一句话**，
    界面与模型共用同一个来源——谁也不该自己另编一句。
    """

    action: str
    receipt: str = ""
    text: str = ""
    """留在档案里的那一条原文（``rejected`` 时是这次想写进去的那条）。"""

    section: str = ""
    replaced: str = ""
    """被顶替掉的旧值（``replaced`` 时有）。"""

    entries: int = 0
    """写完之后档案里一共几条（界面上的读数）。"""


class MemoryEntryOut(BaseModel):
    """档案里的一条（界面按区渲染，项目区还按 ``group`` 分组）。"""

    text: str
    group: str = ""
    source: str = ""
    """它是怎么进来的：``显式`` / ``隐式`` / ``界面`` / ``迁移``；空 = 没有对得上的记录
    （多半是用户拿外部编辑器直接改的）。"""

    change_at: str = ""
    change_index: int = -1
    """在变更流里的位置（文件顺序，最旧为 0）；``-1`` = 没找到。界面点来源小字时跳过去。"""


class MemoryGroupOut(BaseModel):
    name: str
    entries: int = 0


class MemorySectionOut(BaseModel):
    """一个分区的读数与条目。**未知分区照常报**（``known=false``），界面标"分区不认识"。"""

    name: str
    known: bool = True
    entries: int = 0
    chars: int = 0
    limit: int = 0
    """条数上限；0 = 这一区不按条数限（项目区按组限）。"""

    suggested_chars: int = 0
    group_limit: int = 0
    groups: list[MemoryGroupOut] = Field(default_factory=list)
    items: list[MemoryEntryOut] = Field(default_factory=list)


class MemoryBudgetOut(BaseModel):
    """顶部那条全局读数（§5.2、§6.1）。"""

    entries: int = 0
    chars: int = 0
    entry_limit: int = 0
    char_limit: int = 0


class MemoryDraftOut(BaseModel):
    """``import-draft.md``：迁移时没挤进档案的旧条目（§8.2 第 3、4 类）。"""

    exists: bool = False
    path: str = ""
    entries: int = 0


class MemoryDraftSuggestionOut(BaseModel):
    """「整理初稿」给出的一条建议（§8.3）：**只是建议，还没有写进档案**。"""

    text: str
    section: str = ""


class MemoryDraftOrganizeOut(BaseModel):
    """一次「整理初稿」的结果。

    **这一步不写任何东西**：它只跑一次模型、把草稿里的旧条目改写成画像条目并把
    归区建议带回来，用户在界面上确认之后才逐条走 ``POST /memory/remember``
    ——于是这一次模型调用**不可能绕过预算与变更流**（§3.3–§3.4）。
    """

    items: list[MemoryDraftSuggestionOut] = Field(default_factory=list)
    note: str = ""


class MemoryArchiveOut(BaseModel):
    """档案卡首屏要的一切（一页两块里的第一块）。"""

    path: str = ""
    updated: str = ""
    budget: MemoryBudgetOut
    sections: list[MemorySectionOut] = Field(default_factory=list)
    draft: MemoryDraftOut
    migration_available: bool = False
    """还有没有可折叠的旧数据——**没有时迁移入口不出现**（§8）。"""


class MemoryChangeOut(BaseModel):
    """变更流里的一条（§3.4）。``old`` / ``new`` 是逐字节的原文。"""

    index: int = 0
    at: str = ""
    action: str = ""
    section: str = ""
    source: str = ""
    old: str = ""
    new: str = ""
    restorable: bool = False
    """能不能还原：``顶替`` 与 ``忘掉`` 有旧值可写回，其余动作没有。"""


class MemoryChangesOut(BaseModel):
    changes: list[MemoryChangeOut] = Field(default_factory=list)


class MemoryForgetIn(BaseModel):
    """忘掉一条（界面上行尾的删除）。``topic`` 是那一条的原文。"""

    topic: str = Field(min_length=1, max_length=MAX_MEMORY_ENTRY_CHARS)


class MemoryRestoreIn(BaseModel):
    """还原：把一条旧值写回档案（界面上变更流的「还原」）。"""

    text: str = Field(min_length=1, max_length=MAX_MEMORY_ENTRY_CHARS)


class MemoryGroupRenameIn(BaseModel):
    """项目段的组改名（§3.1 第 2 条：改名 = 一次顶替，旧名进变更流）。"""

    section: str = Field(default="", max_length=40)
    old: str = Field(min_length=1, max_length=MAX_MEMORY_ENTRY_CHARS)
    new: str = Field(min_length=1, max_length=MAX_MEMORY_ENTRY_CHARS)


class MemoryMigrationOut(BaseModel):
    """一次机械折叠迁移的计数（§8.4 的迁移报告）。"""

    added: int = 0
    replaced: int = 0
    existing: int = 0
    dropped_sensitive: int = 0
    downgraded: int = 0
    trimmed: int = 0
    skipped: bool = False
    archive_changed: bool = False
    draft_entries: int = 0
    per_source: list[tuple[str, int]] = Field(default_factory=list)


# ------------------------------------------------------------------ 定时任务（v0.33）


class ScheduledTaskCreateIn(BaseModel):
    """新建一条定时任务。见《Agent-工作区与能力层设计》§6.6。"""

    name: str = Field(min_length=1, max_length=80)
    """叫什么（同时会成为它那条会话的标题）。"""
    prompt: str = Field(min_length=1, max_length=4000)
    """**到点要问它的那句话**——它就是每次运行的用户消息，写具体一点：
    "把昨天的构建日志汇总成三条结论"比"看看日志"得到的东西有用得多。"""
    kind: Literal["cron", "once"] = "cron"
    cron: str = Field(default="", max_length=120)
    """5 字段表达式（分 时 日 月 周），按**服务器时区**解释。例：``0 9 * * *``。"""
    run_at: datetime | None = None
    """一次性任务的时刻。**不带时区的按服务器本地时间解释**
    （界面上的 datetime-local 就是这个形状）。"""
    kb_ids: list[str] = Field(default_factory=list)
    """到点检索哪些库。留空 = 不查资料（只靠模型自己的知识与工具）。"""
    model_pk: str | None = None
    thinking: bool | None = None
    thinking_effort: str | None = None


class ScheduledTaskUpdateIn(BaseModel):
    """改一条。**只改传进来的字段**（``None`` = 不动）。"""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: Literal["cron", "once"] | None = None
    cron: str | None = Field(default=None, max_length=120)
    run_at: datetime | None = None
    kb_ids: list[str] | None = None
    enabled: bool | None = None
    model_pk: str | None = None


class ScheduledTaskOut(BaseModel):
    model_config = _RECORD_CONFIG

    id: str
    name: str
    prompt: str
    kind: Literal["cron", "once"]
    cron: str = ""
    run_at: datetime | None = None
    next_run_at: datetime | None = None
    enabled: bool = True
    kb_ids: list[str] = Field(default_factory=list)
    model_pk: str | None = None
    thinking: bool | None = None
    thinking_effort: str | None = None
    conversation_id: str | None = None
    """结果落在哪条会话里（首次运行后才会有）。界面据此给"看跑过的结果"一个落点。"""
    last_run_at: datetime | None = None
    last_status: str = ""
    """``ok`` / ``degraded`` / ``failed`` / 空串（还没跑过）。
    ``degraded`` = 跑完了但没跑完（撞上步数或时间闸，可以在那条会话里点「继续」）。"""
    last_error: str = ""
    run_count: int = 0
    schedule_text: str = ""
    """给人看的一句话（"每天 09:00" / "2026-09-21 09:00 跑一次"）。由服务端生成——
    界面自己把 cron 翻成人话，就得再维护一份解析。"""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScheduledTaskListOut(BaseModel):
    items: list[ScheduledTaskOut] = Field(default_factory=list)
    timezone: str = ""
    """当前服务器时区（如 ``CST UTC+08:00``）。cron 按它解释，
    所以界面必须显示出来——否则"每天 9 点"是哪个 9 点就成了猜。"""


class ScheduledTaskRunOut(BaseModel):
    """「立即跑一次」的结果：任务已经入队。"""

    task_id: str
    detail: str = ""


class ChatStepRetryIn(BaseModel):
    """重跑某一步的请求体（D24，2026-09-28 走查）。

    **只有一项，而且是"界面独有"的那一项**：这一步重跑之后要接着把这一轮答完，
    而"这一轮都调了哪些技能"存在输入框的偏好里、本来就不入库（与 ``ChatResumeIn``
    逐字同一条理由）。模型 / 思考档 / 库范围一律取**会话已存的**——重试一步是接着
    同一轮做，不给它换模型的机会。

    **要重跑哪一步不进这里**：它在路径上（``(message_id, step_index)``），
    放请求体里就等于同一件事有两个入口，而"哪一步"是这条请求的唯一主语。
    """

    skill_names: list[str] = Field(
        default_factory=list,
        description="本轮钉住的技能名（与提问时同一份，来自输入框「加号 → 技能」）",
    )


# ------------------------------------------------- 知识库提供者（M3 握手）


_PROVIDER_CONFIG = ConfigDict(
    from_attributes=True, use_attribute_docstrings=True, populate_by_name=True
)
"""握手这一族模型的公共配置。

- ``use_attribute_docstrings``：字段下面那段说明进 OpenAPI（与 ``_RECORD_CONFIG``
  同理）——客户端认的就是这份契约，说明得跟着它走；
- ``populate_by_name``：``ProviderIngestCapsOut`` 有一个字段的**对外名字是 Python
  关键字**（``async``），属性只能叫 ``async_``；允许按属性名构造，按别名序列化。
"""


class ProviderRetrievalCapsOut(BaseModel):
    """检索能力（``POST /search`` 那一条）。"""

    model_config = _PROVIDER_CONFIG

    modes: list[str] = Field(default_factory=list)
    """支持的检索模式：``hybrid`` / ``vector`` / ``fulltext``。"""
    default_mode: str = "hybrid"
    """不指定 ``mode`` 时用的那一个。"""
    rerank: bool = False
    """支不支持重排（``SearchRequest.rerank``）。"""
    filters: bool = False
    """支不支持元数据过滤（``SearchRequest.filters``）。"""
    top_k_max: int = 0
    """``top_k`` 的上限。**从请求模型的约束读出来**，不另写一份数字。"""
    candidate_k_max: int = 0
    """``candidate_k`` 的上限（同上）。"""


class ProviderIngestCapsOut(BaseModel):
    """入库能力（``POST /knowledge-bases/{kb_id}/documents`` 那一条）。"""

    model_config = _PROVIDER_CONFIG

    transport: str = "multipart"
    """上传的编码：``multipart/form-data``。"""
    async_: bool = Field(default=False, alias="async")
    """上传是不是异步的。

    ``true`` = 立刻回 202 + ``document_id``，解析 / 切分 / 向量化在服务端的队列里跑，
    进度另走 ``tracking`` 那两条。**对外名字是 ``async``**（Python 关键字，
    所以这个属性只能叫 ``async_``；序列化与 OpenAPI 都用别名）。
    """
    dedup: str = ""
    """去重口径：``content_hash`` = 同一份内容重复上传回 ``is_duplicate=true``，
    不会入两份（幂等键 ``Idempotency-Key`` 是另一件事，见规范 §1.6）。"""
    max_bytes: int = 0
    """单文件上限（字节）。**就是上传端点自己的那个常量**，
    界面据此做上传前的校验，不再自己硬编码一份。"""
    extensions: list[str] = Field(default_factory=list)
    """界面要提示的格式（不带点号）。

    **是提示，不是硬白名单**：服务端不按扩展名拦截，真正的接受面由解析路由按
    后缀 / MIME / 内容探测决定，比这份宽（见 ``api/v1/provider.py`` 的模块头）。
    """


class ProviderTrackingCapsOut(BaseModel):
    """入库进度跟踪的能力。"""

    model_config = _PROVIDER_CONFIG

    document: bool = False
    """能不能查单个文档的当前状态。"""
    timeline: bool = False
    """能不能查它的阶段时间线（每一步什么时候完成、失败在哪一步）。"""


class ProviderKbCapsOut(BaseModel):
    """库管理能力（页面直连的那一族）。"""

    model_config = _PROVIDER_CONFIG

    create: bool = False
    """建库。"""
    delete: bool = False
    """删除（含影响清单）。"""
    folders: bool = False
    """库内目录。"""
    shares: bool = False
    """把库分享给其他成员（读 / 写两档）。"""
    wiki: bool = False
    """库形态里的 Wiki 页面。"""


class ProviderEmbeddingCapsOut(BaseModel):
    """向量化能力（**如实报当前这一台的状态**，不是"代码里支持什么"）。"""

    model_config = _PROVIDER_CONFIG

    configured: bool = False
    """嵌入模型配好了没有。为假时仍然能上传，但检索拿不到向量通道。"""
    is_development: bool = False
    """是不是开发用的确定性嵌入：为真时检索结果**不代表真实效果**。"""
    model_id: str = ""
    """当前嵌入模型标识。"""
    dim: int = 0
    """向量维度。"""


class ProviderCapabilitiesOut(BaseModel):
    """提供者的能力集（方案 §1.2 契约）。

    每一项都回答"这台提供者**现在**能不能做这件事"，而不是"这份代码支持不支持"。
    客户端按它决定摆哪些入口；**不认识的字段忽略、不认识的能力位就不摆**——
    所以新增能力位是向后兼容的（``protocol_version`` 不用动）。
    """

    model_config = _PROVIDER_CONFIG

    retrieval: ProviderRetrievalCapsOut
    ingest: ProviderIngestCapsOut
    tracking: ProviderTrackingCapsOut
    knowledge_bases: ProviderKbCapsOut
    embedding: ProviderEmbeddingCapsOut


class ProviderCallerOut(BaseModel):
    """这次调用在提供者看来**是谁**。

    带这一段是因为**同一台 NAS 上有两套身份是事实**（页面用登录会话、本机后端用
    长期 API Key，方案 §1.4 R7）：客户端把它显示出来，两边对不上时用户能自己看出来，
    而不是遇到一个说不清的 403。
    """

    model_config = _PROVIDER_CONFIG

    kind: Literal["session", "api_key"]
    """凭据种类：登录会话（页面那半）还是长期 API Key（本机后端那半）。"""
    permission: ApiKeyPermission
    """权限档：``readwrite`` / ``readonly``。

    不受库范围限制的那两档（管理员、登录成员）报 ``readwrite``——
    它们都能写，报一个空值只会让人以为只是只读。**某个库能不能写看那一项的
    ``can_write``**，那才是判定。
    """
    is_admin: bool = False
    """**如实报**。本机后端那把 API Key 在 NAS 侧**不是管理员**（方案 §1.4）：
    管理员专属的那些端点（``/settings``、``/api-keys``、``/users``、``/trash``、
    ``/maintenance``、``/sandbox``）对它是 403，界面据此**隐藏**这类入口，
    而不是摆出来等着失败。
    """
    can_write: bool = False
    """这把凭据的权限档能不能写（**不看具体库**）。逐库的答案在每项的 ``can_write``。"""
    knowledge_base_ids: list[str] = Field(default_factory=list)
    """这把凭据被限定的库范围。**空 = 不限范围**（与发钥匙时的约定同一套）。"""


class ProviderKbBriefOut(BaseModel):
    """握手里的库摘要：**够界面与客户端判断"这个库是什么、我能不能写"**。

    不是完整的 ``KnowledgeBaseOut``：握手可能带回几十个库，而切分参数 / 提示词那些
    只有库设置页要，页面本来就直接打 ``GET /knowledge-bases``。
    """

    model_config = _PROVIDER_CONFIG

    id: str
    name: str
    document_count: int = 0
    """库内文档数（**一次聚合查询**算出来的，与列表端点同一个数）。"""
    last_activity: datetime | None = None
    """库内文档的最近更新时间；没有文档时为 ``None``。"""
    can_write: bool = False
    """这把凭据能不能往这个库里写（上传 / 删除）。

    **由后端算**，与 ``GET /knowledge-bases`` 走同一份口径
    （``api/v1/knowledge_bases.kb_access_flags``）：只读档分享的成员是 ``false``。
    """
    embedding_model_id: str
    """这个库冻结的嵌入模型标识（换模型要重建库，所以它随库走）。"""
    embedding_dim: int
    wiki_enabled: bool = False
    """库形态：``True`` = 向量检索 + Wiki 页面。"""


class ProviderHandshakeOut(BaseModel):
    """知识库提供者握手（方案 §1.2）。

    一次调用回答三件事：**连通且凭据有效**（否则根本到不了这里——凭据问题走 401/403，
    见 `api/v1/provider.py` 的模块头）、**这台提供者能做什么**、**我能用哪些库**。

    **响应体里没有"凭据错"这类字段**：那是 HTTP 状态码 + 统一错误信封的事
    （规范 §1.3）。客户端据此把"改钥匙"与"改地址"分成两档——一次连不上变成
    一句"凭据无效"是最让人绕路的一种错。
    """

    model_config = _PROVIDER_CONFIG

    provider: str
    """提供者种类，固定 ``knowledge``（将来还有别的提供者时用它分流）。"""
    protocol_version: int
    """**握手协议版本**：整数、只增。

    M3 的值是 ``1``。客户端规则：**不认识（大于本机所知）即判不可用**，
    原因句子里带上版本号，**绝不硬试**（方案 §1.2 裁量 3）——一个"试着发一条
    请求看看能不能用"的实现会把新协议的语义错误当成网络故障。
    """
    app_version: str
    """服务端应用版本（给人看、排查用）。"""
    api_version: str
    """HTTP 路径版本（固定 ``v1``）。与 ``protocol_version`` 是两件事：
    前者是"地址怎么拼"，后者是"两侧谈得下去吗"。"""
    capabilities: ProviderCapabilitiesOut
    caller: ProviderCallerOut
    """这次调用在提供者看来是谁。"""
    knowledge_bases: list[ProviderKbBriefOut] = Field(default_factory=list)
    """**这次调用看得见**的库（受限 key 只看到范围内的：方案 R9 要防的元信息泄露）。"""
    server_time: datetime
    """服务端当前时间（UTC，带时区）。界面据此显示"上次确认是什么时候"。"""


# ------------------------------------------------- 备份提供者（M5 阶段 1 握手）
#
# 与上面那族同一份写法、同一个 `_PROVIDER_CONFIG`：两个提供者的握手结构是平行的
# （provider / protocol_version / capabilities / caller / server_time），差别只在
# 能力集的内容与"带回来的那一批东西"（库清单 ↔ 设备与额度）。
# **协议版本各是一个整数**：备份那边不认识知识库的 1，反过来也一样。


class BackupSnapshotCapsOut(BaseModel):
    """快照这一族能不能用、怎么用（``/backup/snapshots/*`` 那五条）。"""

    model_config = _PROVIDER_CONFIG

    available: bool = True
    """**现在能不能写快照**：为假时通常是"备份桶还没建出来"或对象存储连不上。

    它是握手唯一会随环境变的能力位。为假时**握手仍然是 200**——客户端据此把页面切成
    "这里还没准备好 + 下一步做什么"，而不是显示成服务器坏了（方案 R5）。
    """
    unavailable_reason: str = ""
    """``available`` 为假时那句**可执行的下一步**（为真时是空串）。"""
    transport: str = "octet-stream"
    """快照体怎么发：``octet-stream`` = 裸字节流 ``PUT``（不是 multipart 表单）。"""
    format: str = "tar.gz"
    """打包格式。"""
    manifest: str = "json"
    """清单格式。"""
    checksum: str = "sha256"
    """校验算法：整份快照体的 sha256，服务端**边收边算**。"""
    max_blob_bytes: int = 0
    """单份快照体的上限（字节）。超了是 ``413``，且不落桶——客户端据此在打包时就分档。"""
    max_snapshots_per_device: int = 0
    """每台设备最多留几份（保留份数）。与 ``retention.keep`` **同一个数**。"""
    encryption: str = "none"
    """加密口径（自描述字段，当前恒为 ``none``）。"""


class BackupRestoreCapsOut(BaseModel):
    """恢复这一族：这份存储**够不够按点恢复**。"""

    model_config = _PROVIDER_CONFIG

    point_in_time: bool = False
    """能不能按时间点挑一份恢复点（就是"列恢复点 + 取清单"这两件事）。"""
    manifest_listing: bool = False
    """恢复点清单里有没有清单（计数 / 被跳过项 / schema 版本都在里面，恢复前能先看）。"""
    download: bool = False
    """快照体能不能下载。"""
    partial_restore: bool = False
    """一份快照够不够"只恢复一部分"（清单逐条列了内容物，成员也逐个可取）。"""


class BackupQuotaOut(BaseModel):
    """额度那一段：**配了多少、用了多少**（方案 §1.4：握手一次给全，不另开端点）。"""

    model_config = _PROVIDER_CONFIG

    policy: str = "keep_n"
    """保留策略：留最近 N 份。服务端**不替用户删**（超限报 409）——所以这里只有策略名。"""
    keep: int = 0
    """每台设备的保留份数上限。"""
    quota_bytes: int = 0
    """这一整批设备的字节上限（``KYLAB_BACKUP_QUOTA_BYTES``）。"""
    used_bytes: int = 0
    """已经用了多少字节。桶不可用时是 ``0``——**那不是"零"，是数不出来**，
    以 ``capabilities.snapshot.available`` 为准。"""
    snapshots: int = 0
    """已经有多少份恢复点（同上：桶不可用时数不出来）。"""


class BackupCapabilitiesOut(BaseModel):
    """备份提供者的能力集（方案 §1.3 契约）。

    每一项都回答"这台提供者**现在**能不能做这件事"——与知识库那族同一条口径：
    不认识的字段忽略、不认识的能力位就不摆，所以新增能力位是向后兼容的。
    """

    model_config = _PROVIDER_CONFIG

    snapshot: BackupSnapshotCapsOut
    restore: BackupRestoreCapsOut
    retention: BackupQuotaOut


class BackupDeviceBriefOut(BaseModel):
    """握手里的一台设备：**几份、多大、最近一份是什么时候**。"""

    model_config = _PROVIDER_CONFIG

    device_id: str
    """设备 id（壳的 ``config.json.device_id``，UUID v4）。"""
    device_name: str = ""
    """设备名（清单里自报的那个，只给人看，不参与判等——与工作区那个设备头同一条口径）。"""
    snapshots: int = 0
    """这台设备有几份**完整**的恢复点。"""
    bytes: int = 0
    """这台的恢复点一共占了多少字节（快照体之和）。"""
    latest_snapshot_id: str = ""
    """最近那一份的 ``snapshot_id``（恢复点目录名；配 ``device_id`` 用）。"""
    latest_at: datetime | None = None
    """最近那一份的时间（清单里的 ``created_at``，取不到时退到对象时间）。"""


class BackupSnapshotOut(BaseModel):
    """一个恢复点（``GET /backup/snapshots`` 的一行）。

    这几位的取值来自**清单**（客户端写的那份自描述文件）：服务端只做形状上的归一
    （不认识的类型给默认值），不做业务解释——所以新增清单字段不需要改这里。
    """

    model_config = _PROVIDER_CONFIG

    snapshot_id: str
    """恢复点目录名（``<snapshot_ts>-<hash8>``）。配 ``device_id`` 唯一确定一份。"""
    device_id: str
    """哪台设备的。"""
    device_name: str = ""
    """设备名（只给人看）。"""
    created_at: datetime
    """这份快照的时间（UTC，带时区）。"""
    bytes: int = 0
    """快照体在桶里的**真实字节数**（不是清单自报的那个）。"""
    sha256: str = ""
    """快照体的 sha256（清单里那份；清单没写就是空串）。"""
    kind: str = ""
    """怎么来的：``manual`` / ``auto`` / ``pre_restore``。"""
    schema_version: int = 0
    """快照里那份本机库的 schema 版本（恢复前据此判"能不能读"）。"""
    app_version: str = ""
    """打这份快照时的应用版本。"""
    counts: dict[str, int] = Field(default_factory=dict)
    """内容物计数（会话 / 消息 / 事件 / 笔记 / 产物…），清单里报什么就是什么。"""
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    """**没进包的内容物，逐条如实列**（名称 / 大小 / 原因）。

    服务端原样透传（不裁剪字段）：界面与恢复报告都要照着它说清"哪些没备"。
    """
    encryption: str = "none"
    """加密口径（自描述字段）。"""


class BackupSnapshotListOut(BaseModel):
    """``GET /backup/snapshots`` 的响应（规范 §1.5 的分页）。"""

    model_config = _PROVIDER_CONFIG

    items: list[BackupSnapshotOut] = Field(default_factory=list)
    """这一页的恢复点（**最近在前**）。"""
    total: int = 0
    """符合过滤条件的总数（不是这一页的条数）。"""
    quota: BackupQuotaOut
    """额度：**与设备过滤无关**，永远是这把钥匙看得见的全局用量。"""


class BackupUploadOut(BaseModel):
    """一次上传的结果（blob 与 manifest 同一个形状）。

    ``bytes`` / ``sha256`` 都是**落桶之后从对象存储读回来的事实**（不是请求里自报的那个）
    ——重试的客户端据此确认"桶里那一份和我手里这一份是不是同一份"。
    """

    model_config = _PROVIDER_CONFIG

    snapshot_id: str
    """哪一份恢复点（``device_id`` 在路径里，所以这里只回它）。"""
    bytes: int = 0
    """落桶之后的字节数。"""
    sha256: str = ""
    """落桶之后对象元数据里的 sha256。"""


class BackupHandshakeOut(BaseModel):
    """备份提供者握手（方案 §1.3）。

    与知识库握手同一个形状（``provider`` / ``protocol_version`` / ``capabilities`` /
    ``caller`` / ``server_time``），把"库清单"换成"设备摘要 + 额度"：

    - ``devices``：**这次调用看得见**的全部设备。备份**不建 ACL**——可见范围就是这把钥匙
      能看见的全部设备（方案 §1.5 的边界，登记为"将来要按设备限权"的一条）；
    - ``retention`` 在 ``capabilities`` 里（与知识库把"库清单"放顶层不同）：额度是
      "这台提供者怎么记账"的一部分，跟着能力集走更顺。

    响应体里同样**没有"凭据怎么了"**：401 / 403 走 HTTP 状态码与统一错误信封。
    """

    model_config = _PROVIDER_CONFIG

    provider: str
    """提供者种类，固定 ``backup``。"""
    protocol_version: int
    """**握手协议版本**：整数、只增，备份这边当前是 ``1``。

    与知识库那个 ``1`` 是两件事：两个提供者各自演进，客户端**分开判**。
    """
    app_version: str
    """服务端应用版本（给人看、排查用）。"""
    api_version: str
    """HTTP 路径版本（固定 ``v1``）。"""
    capabilities: BackupCapabilitiesOut
    caller: ProviderCallerOut
    """这次调用在提供者看来是谁（**与知识库握手同一份形状**，所以客户端可以共用一份解析）。
    """
    devices: list[BackupDeviceBriefOut] = Field(default_factory=list)
    """**这次调用看得见**的设备摘要（含各自的份数、字节与最近一份）。"""
    server_time: datetime
    """服务端当前时间（UTC，带时区）。"""
