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
from app.services.tools import MAX_TOP_K

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
    """批量动作：``delete``（进回收站）、``reprocess``（重新摄入）、``reparse``
    （强制重新解析：连解析器一起重跑）、``move``（移目录）、
    ``enable`` / ``disable``（停用或恢复检索）或 ``questions``（为已索引文档补生成分段问题）。

    ``document_ids`` 设上限而不是"随便多少"：一次勾几千篇会把请求体、逐条查询
    与响应都拉大，而界面上的多选本来也到不了那个量级。

    ``all=True``（v17）表示**对这个库的全部文档**执行，忽略 ``document_ids``。
    它服务的场景是"切分参数改了、要整库重跑"：由服务端自己解析全集，
    界面不必先翻页取 id 再回传（那个列表接口一次回全量，本身就是瓶颈）。
    """

    action: Literal["delete", "reprocess", "reparse", "move", "enable", "disable", "questions"]
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


class DocumentPageOut(BaseModel):
    """一段页范围里的一页。

    ``image_*`` 三个字段是一组：要么都有，要么都是 ``None``。三种情况会给 ``None``：
    请求里 ``images=false``、这份原件不能按页出图（Office / 纯文本 / SVG），
    或者这一页已经超出文档总页数。分开判它们没有意义——``image_url`` 有而
    ``image_kind`` 为 None 只会让界面不知道该拿它怎么办。

    **这几个字段都不要默认值**（全部必填、可空）：生成的 TS 类型里它们就是
    ``string | null`` 而不是 ``string | undefined``，界面不用再分"没给"与"给了个空"。
    """

    # 字段下面那几段说明要进 OpenAPI（进而进前端生成的类型）：客户端认的就是这份契约
    model_config = ConfigDict(use_attribute_docstrings=True)

    page: int
    """页码（从 1 开始）。与请求的范围一一对应，**没有正文的页也在列表里**。"""
    text: str
    """这一页的解析文字（已去掉内部页标记）。空串表示这一页没有正文。"""
    image_url: str | None
    """这一页原图的签名链接（相对路径，可直接塞进 ``<img src>``）。"""
    image_expires_at: int | None
    """上面那条链接的到期时间戳（Unix 秒）。"""
    image_kind: str | None
    """这张图的来源：``pdf``（原件是 PDF，服务端栅格化）或 ``image``（原件本身就是图）。"""


class DocumentPagesOut(BaseModel):
    """按页范围取原文的结果。

    ``pages`` 的长度**恒等于**请求的页数（``to_page - from_page + 1``）：
    界面按请求的页数铺格子，缺哪一页会让"第几页对不上"。
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    document_id: str
    from_page: int
    to_page: int
    total_pages: int | None
    """文档总页数（探测阶段写入）。``None`` 表示探测没给出页数——不代表只有一页。"""
    page_text_available: bool
    """这份文档的解析产物里有没有页码信息。``false`` 时每页 ``text`` 都是空串，
    界面要如实说"这份文档没有页码信息"，而不是显示成文档是空的。"""
    pages_reliable: bool | None
    """产物里的页码**可不可信**（三值）。

    - ``null``：产物里没有页标记（``page_text_available=false`` 同一件事）——没有页码
      可判，**不是"页码错了"**，界面的说法是"没有页码信息"而不是"页码不可信"；
    - ``true``：有页标记，且与 ``total_pages`` 自洽（最大页号不超过总页数、定位到的页数
      不少于总页数的一半）；``total_pages`` 拿不到时也回 ``true``（没有依据就不喊狼来了）；
    - ``false``：有页标记但与 ``total_pages`` 对不上（定位到的页数远少于总页数，或者
      出现了超过总页数的页号）——**按页读到的这一段不能当成"第 N 页"引用**，它可能
      横跨很多页；要修就用重新解析让产物重算页码。"""
    pages_located: int | None
    """解析产物里**定位到的不同页号个数**（不是块数）。``null`` = 产物里没有页码信息，
    此时 ``pages_reliable`` 也是 ``null``。"""
    truncated: bool
    """请求的页范围超出了文档总页数（``to_page > total_pages``）。多出来的那几页
    注定是空的（也不会签图片链接），界面据此收住而不是一直往后翻。"""
    pages: list[DocumentPageOut]


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
    """检索范围的条件。**条件在候选生成之前生效**（下推到 SQL），不是事后筛。

    所以"只查某本书的一章"能真正用：``document_ids`` + ``page_from``/``page_to``
    或 ``heading_contains`` 一起给，抽样的名额只在这本书的这一章里分。
    """

    # `use_attribute_docstrings`：这一层没有记录对象可映射，字段说明只能靠 docstring ——
    # 而"没有页码的文档会被页码过滤整体排除"这件事**必须进契约**（OpenAPI 与生成的
    # `schema.d.ts`），否则调用方拿到空结果只会以为"这本书里没有这段内容"。
    model_config = _RECORD_CONFIG

    document_ids: list[str] | None = None
    """只在这些文档里查（"只查某本书"）。"""

    source_kinds: list[DataSourceKind] | None = None
    created_after: datetime | None = None
    """文档的创建时间下界（含边界）。"""
    created_before: datetime | None = None
    """文档的创建时间上界（含边界）。"""
    page_from: int | None = Field(default=None, ge=1)
    """只要 ``page >= page_from`` 的块（"某章"，按页码）。

    **没有页码信息的内容会被整体排除**：``.md`` / Office 来源的块 ``page`` 为空，
    扫描版 PDF 的页码也可能不可信——按页码筛时它们一条都取不到。要按章节查，
    优先用 ``heading_contains``。
    """
    page_to: int | None = Field(default=None, ge=1)
    """只要 ``page <= page_to`` 的块。"""
    heading_contains: str | None = None
    """标题路径包含这个子串（大小写不敏感），如 ``"第3章"``。

    切块时按原文标题层级拼出来的路径（``第3章 > 3.2节``），比页码更可靠。
    **标题路径为空的块会被整体排除**。
    """

    @model_validator(mode="after")
    def _check_page_range(self) -> MetadataFilterIn:
        """区间反了当场说清——静默返回空结果会被读成"这本书里没有这段内容"。"""
        if self.page_from is None or self.page_to is None:
            return self
        if self.page_to < self.page_from:
            raise ValueError("page_to 不能小于 page_from")
        return self


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    kb_ids: list[str] = Field(min_length=1)
    top_k: int = Field(default=8, gt=0, le=MAX_TOP_K)
    """**这次能翻到的总量**（v0.63 起它才单独有意义）。

    从前它与 ``keep`` 是同一个数（都管"返回几条"）。现在 ``keep`` 是**一页几条**，
    收敛之后留下的那一批由它封顶——翻页翻的就是这个池子。上限与 MCP ``search``
    工具广告的是**同一个常量**（``services.tools.MAX_TOP_K``）：一边能翻 50 条、
    一边只能翻 20 条，就成了两种能力（同 ``keep`` 的理由）。
    """
    mode: str = "hybrid"
    candidate_k: int = Field(default=40, gt=0, le=500)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """相对融合分的下限（该条分 / 最高分）。只剪尾巴，剪不掉"整批都不相关"。"""
    min_vector_score: float | None = Field(default=None, ge=0.0, le=1.0)
    """向量余弦的**绝对**下限。不传 = 按库的嵌入模型用标定默认（见下），0 = 关闭。

    标定默认落在 ``services/retrieval`` 的 ``MIN_VECTOR_SCORE_BY_MODEL``：余弦的绝对值
    是模型属性，本项目只标定过 bge-m3（**0.531**，噪声头 0.460–0.520 / 真查询头 0.60–0.755）
    与 WeMM 多模态（**0.33**，噪声头 0.305）；**没标定过的嵌入模型默认不设限**——
    宁可少拦，也不能拿一个别的模型的尺度把真命中拦掉。
    """
    min_term_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    """词面覆盖的绝对下限：查询实词里至少多少比例出现在候选（或其文档名）里。

    不传 = 跟着向量下限走（向量下限开着时为 0.67，取值与噪声实测见
    ``services/retrieval`` 的 ``DEFAULT_MIN_TERM_COVERAGE``），0 = 关闭。
    两条证据是"或"的关系，见 ``services/retrieval/coverage.py``。
    """
    keep: int | None = Field(default=None, ge=1, le=MAX_TOP_K)
    """**按分布收敛**：这一轮最多要几段（不传 = 用分布给出的建议）。

    **v0.63 起它是"一页几条"**：一共能翻到多少由 ``top_k`` 封顶，``next_offset`` 指下一页。
    ``offset=0`` 时与从前完全一致（返回的就是前 ``keep`` 条）。

    上限与 MCP ``search`` 工具广告的是同一组数（``keep`` 1–``MAX_TOP_K``、``per_doc`` 1–10），
    两者是同一个旋钮的两个入口——一边能传 50、一边只能传 20，用起来就成了两种能力。
    """
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    """**按分布收敛**：相似度下限（绝对余弦，不传 = 用建议）。"""
    per_doc: int | None = Field(default=None, ge=1, le=10)
    """**按分布收敛**：每篇文档最多留几段（不传 = 用建议）。同一件事被好几本书
    各答一遍时，这个旋钮才是治"上下文太长"的那个。"""
    offset: int = Field(default=0)
    """**翻页的起点**（v0.63）：跳过前 N 条**幸存者**（收敛之后留下的那一批）。

    返回的是 ``幸存者[offset : offset + keep]``；``next_offset`` 说下一页从哪开始
    （``null`` = 翻到底了）。**判定与它无关**：``summary`` / ``distribution`` /
    ``decision`` 在同一批候选的每一页上都逐字相同——调用方正是靠它决定要不要接着翻，
    一个随翻页变动的判定是没法用的。

    **刻意不加 ``ge=0``**：负数退回 0 而不是 422——它是"顺手给的调节量"，
    与 MCP 那条路同一条容错取舍（见 ``services.retrieval.service.resolve_offset``
    与 ``services.tools._int_or_none``）。越过末尾同样不是错：回空 ``hits``
    加 ``next_offset: null``，判定照旧。
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
    basis: str
    """这条命中**是哪条通道捞上来的**：``"vector"`` / ``"fulltext"``（就是 ``raw_scores`` 的键名）。

    **为什么要单独给这一位**：`min_score` 只切向量通道的命中——词面通道没有余弦可比，
    它的命中不受阈值约束（条数记在 ``decision.unscored_kept`` 里）。所以
    ``similarity`` 为 ``null`` 的那些不是"相关性不明"，而是"这条是词面捞上来的"；
    要更严就得自己按 ``lexical_score`` 筛。MCP 的 `search` 给的是同一批字段。"""
    lexical_score: float | None = None
    """词面（全文）通道的原始分，**与 `similarity` 不同量纲、不可互相比较**（越大越相关）。

    ``None`` = 这条不是词面捞上来的。相关度下限只认余弦，词面那一路"够不够像"由实词
    覆盖率判（见 `services/retrieval/coverage.py`）——这一位交出来是让调用方自己筛。"""
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
    summary: str = ""
    """**判定在前，原文在后**（v0.55）：一句话说清候选多少、收敛后留了几条、契合程度如何。

    与 MCP `search` 工具的 `summary` 是同一句（``ScoreDistribution.headline``）——
    同一个服务在两个入口对同一批命中给出两种说法，是最难查的一类不一致。
    """
    distribution: dict[str, Any] | None = None
    """兜底阈值之上的命中分布：形状与 MCP `search` 工具的 `distribution` **完全一致**
    （同一个 ``ScoreDistribution.as_payload``）。v0.55 起无条件有值，只有"空查询 /
    没有库"那种提前返回是 `null`。"""
    decision: dict[str, Any] | None = None
    """这次**实际**按什么收敛的：``{"keep", "min_score", "per_doc", "decided_by",
    "fit", "dropped", "unscored_kept"}``——用户问"为什么只回了 3 条"时，答案必须是一个数
    加一个来源。

    ``decided_by`` 为 ``"model"`` = 这三个旋钮里**任意一个**是调用方给的（``per_doc``
    也算：一边照它收敛、一边说是系统建议的，是同一份载荷里的自相矛盾）。
    ``unscored_kept`` = 没有余弦可比、因而绕过 ``min_score`` 那一刀的词面命中条数
    （``min_score=0.99`` 却还有命中时，看它；没有这一刀或个个都有余弦时为 0）。"""
    hits: list[SearchHitOut]
    mode: str
    reranked: bool
    filtered_out: int = 0
    offset: int = 0
    """这一页是从第几条幸存者开始的（**回显实际生效的那个数**：请求负数时折成 0）。

    回显而不是回传请求值：调用方据此知道自己这一页到底从哪儿开始——"我传了 -3"与
    "系统按 0 走的"必须是同一份载荷里的同一句话。"""
    next_offset: int | None = None
    """下一页的起点；``null`` = 没有更多了（空页也是 ``null``）。

    **照它翻页就行，不必自己算**：把 ``next_offset`` 填回 ``offset`` 继续。空页为 ``null``
    是刻意的——``keep`` 收敛成 0 时照公式回 ``offset + 0`` 会让调用方原地打转。"""
    stats: list[ChannelStatOut] = Field(default_factory=list)
    embedding_configured: bool = True
    """是否配了嵌入模型。为假时本次只做了全文通道，界面要如实说明。"""
    embedding_is_development: bool = False
    """是否为开发用确定性嵌入（仅显式开着开发开关时）：界面提示"检索质量不代表真实效果"。"""


# --------------------------------------------------------------------- 统计


class ActivityPointOut(BaseModel):
    """一天的活跃度。"""

    model_config = _RECORD_CONFIG

    day: date
    documents: int
    chunks: int
    tasks: int
    hits: int = 0
    """这天的检索命中**候选数**（v0.61，各库之和）。概览页趋势图上的「检索命中」一档。"""


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


class SearchHitPointOut(BaseModel):
    """某一天、某个知识库的命中量（检索命中趋势上的一个点）。"""

    model_config = _RECORD_CONFIG

    day: date
    candidates: int
    """分布收敛**之前**这个库贡献的条数。"""
    kept: int
    """收敛之后**剩下**的条数（v0.63 起不是"某一页返回的条数"）。

    翻页不该把这条曲线乘上页数：同一次检索翻三页，趋势上仍然只记一份收敛结果。"""


class KbSearchHitsOut(BaseModel):
    """单个知识库在窗口内的命中情况 + 它的稀疏曲线。"""

    model_config = _RECORD_CONFIG

    kb_id: str
    name: str
    candidates: int
    kept: int
    searches: int
    """这个库在窗口内被检索了几次（= 它的行数，一次跨两库的检索算两行）。"""
    series: list[SearchHitPointOut] = Field(default_factory=list)
    """**稀疏**：只有真有记录的那些天，前端自己补零成连续曲线。"""


class SearchHitsOut(BaseModel):
    """检索命中趋势（``GET /stats/search-hits``，v0.61）。

    ``by_kb`` **只含窗口内真有记录的库**：一次都没被检索过的库没有曲线可画，
    出现在这里只会让图例多出一堆平线。
    """

    model_config = _RECORD_CONFIG

    days: int
    candidates: int
    kept: int
    searches: int
    by_kb: list[KbSearchHitsOut] = Field(default_factory=list)


# --------------------------------------------------------------------- API Key


class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    permission: ApiKeyPermission = ApiKeyPermission.READONLY
    """默认只读：**默认值要取最保守的那个**。给外部集成发一把能删库的钥匙，
    不应该是因为"没填那个字段"。"""
    knowledge_base_ids: list[str] = Field(default_factory=list)
    """空列表表示不限制范围（可访问全部知识库），见 services/api_key.py 的说明。"""
    allowed_tool_names: list[str] = Field(default_factory=list)
    """这把钥匙能调的 MCP 工具（``services.tools.TOOL_NAMES`` 里的名字）。

    空列表表示**不限制工具**（与 ``knowledge_base_ids`` 同一口径）——所以只限了库范围、
    没填这个字段的钥匙行为与从前一致。名字是否真实存在由端点校验（协议层），
    schema 只负责形状：把工具名表抄一份到这儿，两处迟早会漂。
    """


class ApiKeyOut(BaseModel):
    """列表展示用。**绝不回显 key_hash 或明文**——只有创建响应里有明文。"""

    model_config = _RECORD_CONFIG

    id: str
    name: str
    permission: ApiKeyPermission
    knowledge_base_ids: list[str] = Field(default_factory=list)
    allowed_tool_names: list[str] = Field(default_factory=list)
    """空 = 不限制工具（见 ``ApiKeyCreateIn`` 的说明）。列表里回显它是必要的：
    否则用户看不出"这把钥匙为什么调不动某个工具"。"""
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    prefix: str = ""
    """展示用前缀（``kylab_sk_ab12…``），让用户能分辨"哪把是哪把"。"""


class ApiKeyIssuedOut(ApiKeyOut):
    """创建响应：``token`` 是明文**唯一一次**出现的地方。"""

    token: str


class ApiKeyListOut(BaseModel):
    items: list[ApiKeyOut]


# ------------------------------------------------------------------ 存储概览


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

    带这一段是因为**同一个服务上有两套身份是事实**（页面用登录会话、外部客户端用
    长期 API Key，方案 §1.4 R7）：客户端把它显示出来，两边对不上时用户能自己看出来，
    而不是遇到一个说不清的 403。
    """

    model_config = _PROVIDER_CONFIG

    kind: Literal["session", "api_key"]
    """凭据种类：登录会话（页面那半）还是长期 API Key（外部客户端那半）。"""
    permission: ApiKeyPermission
    """权限档：``readwrite`` / ``readonly``。

    不受库范围限制的那两档（管理员、登录成员）报 ``readwrite``——
    它们都能写，报一个空值只会让人以为只是只读。**某个库能不能写看那一项的
    ``can_write``**，那才是判定。
    """
    is_admin: bool = False
    """**如实报**。长期 API Key **不是管理员**（方案 §1.4）：
    管理员专属的那些端点（``/settings``、``/api-keys``、``/users``、``/trash``、
    ``/maintenance``）对它是 403，界面据此**隐藏**这类入口，
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

    M3 的值是 ``1``。客户端规则：**不认识（大于自己所知）即判不可用**，
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


#
# 与上面那族同一份写法、同一个 `_PROVIDER_CONFIG`：两个提供者的握手结构是平行的
# （provider / protocol_version / capabilities / caller / server_time），差别只在
# 能力集的内容与"带回来的那一批东西"（库清单 ↔ 设备与额度）。
# **协议版本各是一个整数**：备份那边不认识知识库的 1，反过来也一样。


# --------------------------------------------------------------------- 工具目录


class ToolParamOut(BaseModel):
    """工具的一个参数。

    ``summary`` 取参数在 JSON Schema 里的 ``description``；没写就是空串——
    界面把它当"这一格填什么"，没有就只显示名字与必填标记。
    """

    name: str
    required: bool
    summary: str = ""


class ToolOut(BaseModel):
    """对外工具面里的一个工具（``GET /tools``）。

    参数由 ``tool_definitions()`` 的 ``inputSchema`` 推出来，**不在这里另写一份**：
    两份参数清单必然相漂，而漂出来的形态是"界面提示的参数与实际要的对不上"。
    """

    name: str
    label: str
    summary: str
    requires_write: bool = False
    params: list[ToolParamOut] = Field(default_factory=list)


class SkillOut(BaseModel):
    """随服务一起发出去的那份技能（挂在同一个工具面上）。

    ``transports`` 是它能走哪两条路：MCP（主路径）与 REST（脚本路径）。
    它就是一个目录里的文件，所以这里只报身份与路径，不报内容。
    """

    name: str
    summary: str
    path: str
    transports: list[str] = Field(default_factory=list)


class ToolCatalogOut(BaseModel):
    """工具目录。管理界面据此渲染那一列工具、以及钩选 API Key 的范围。"""

    items: list[ToolOut] = Field(default_factory=list)
    surface_fingerprint: str
    """对外工具面的指纹（``services.tools.surface_fingerprint()``）：**这张表变了它就变**。

    界面上把它显示出来，用户对着两次部署的值一比就知道契约有没有变过。版本号做不到
    这件事——那是发版号，同一次发版里改工具面它一动不动（详见那个函数的说明）。"""
    mcp_endpoint: str | None = None
    """MCP 的接入地址（整条，来自 ``KYLAB_MCP_PUBLIC_URL``）。**没配时为 ``None``**——
    拼一个相对地址、或者拿 REST 的基址接个 ``/mcp``，摆出来的都是**连不上**的地址。"""
    skill: SkillOut


