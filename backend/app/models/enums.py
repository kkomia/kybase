"""领域枚举。

集中放置跨层共享的枚举，避免各层各写一套字符串常量。
pip 线状态序列的权威定义见《架构设计 v0.2》§4，本模块与之逐字对齐，并由
``tests/unit/models/test_enums.py`` 守住。
"""

from enum import StrEnum

__all__ = [
    "PIPELINE_STAGE_ORDER",
    "TERMINAL_STAGES",
    "ApiKeyPermission",
    "DataSourceKind",
    "DocumentStage",
    "TaskKind",
    "TaskState",
    "TrashKind",
]


class DocumentStage(StrEnum):
    """文档摄入流水线状态（《架构设计 v0.2》§4）。

    主链路：``uploaded → probing → parsing → parsed → chunking → chunked → embedding → indexed``。
    ``enriching/enriched`` 是可选增强分支（图谱/Wiki，默认关闭，失败不影响主链路）。
    """

    UPLOADED = "uploaded"
    PROBING = "probing"
    PARSING = "parsing"
    PARSED = "parsed"
    CHUNKING = "chunking"
    CHUNKED = "chunked"
    EMBEDDING = "embedding"
    INDEXED = "indexed"

    # 可选增强分支
    ENRICHING = "enriching"
    ENRICHED = "enriched"

    # 异常态
    FAILED = "failed"
    CANCELED = "canceled"
    """用户主动叫停（不是出错）。与 ``failed`` 分开：界面上"失败"与"我取消的"
    是两件不同的事，混在一起会让人以为自己把文档弄坏了。"""


PIPELINE_STAGE_ORDER: tuple[DocumentStage, ...] = (
    DocumentStage.UPLOADED,
    DocumentStage.PROBING,
    DocumentStage.PARSING,
    DocumentStage.PARSED,
    DocumentStage.CHUNKING,
    DocumentStage.CHUNKED,
    DocumentStage.EMBEDDING,
    DocumentStage.INDEXED,
)
"""主链路顺序，供 M2 的状态机校验"只允许向前一步"与"断点续跑定位"。"""

TERMINAL_STAGES: frozenset[DocumentStage] = frozenset(
    {DocumentStage.INDEXED, DocumentStage.FAILED, DocumentStage.CANCELED}
)
"""终态：到达后不再自动推进。取消也算终态——它不该被轮询继续当作"在跑"。"""


class TaskKind(StrEnum):
    """任务类型。每个摄入阶段与数据源动作都是独立任务，便于按阶段重试。"""

    PROBE = "probe"
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    ENRICH = "enrich"
    DELETE = "delete"
    FETCH_SOURCE = "fetch_source"
    QUESTIONS = "questions"
    """为一篇**已索引**文档的分段补生成问题（提升召回）。

    它不走摄入阶段机：解析/切块都已完成，只读现有的块出题，再把该段的
    `index_text`（原文 + 问题）重新向量化并重建全文索引，文档阶段保持 indexed。
    之所以单列一个任务类型而不是复用 CHUNK，是因为"重新切块"会连带重新解析、
    把块号与人工干预全部推翻——补出题不该有那些副作用。
    """

    WIKI = "wiki"
    """重建一个知识库的 Wiki 页面（v24）。

    **这是知识库级任务，没有 document_id**（payload 里是 ``kb_id``），
    所以与 FETCH_SOURCE 一样走 worker 里的独立分支。它是"读已经入库的内容、
    写出一层新产物"，不改任何文档的阶段与内容——失败只影响 Wiki 自己。
    """

    MEMORY = "memory"
    """**已退场**（v0.56，档案制 §4.1）：把一段对话交给记忆服务沉淀成长期记忆（v0.14）。

    它原先走队列、每 N 个用户回合入队一次；定时捕获退场之后**既没有生产者也没有
    消费者**（隐式捕获只在信号出现的那一轮同步跑一次判定，不排队）。

    枚举值本身留着：``TaskKind`` 是按字符串从库里读回来的，旧部署的 ``tasks`` 表里
    可能还留着这个 kind 的行——删掉枚举成员会让读那些行直接抛错，而"读不动自己的
    历史"是比"多一个没人用的取值"糟得多的事。它们真的被领到时会在 worker 里
    明确失败（未接线的那一类），不会静默丢掉。
    """

    SCHEDULED = "scheduled"
    """到点替用户做一件事（v0.33）：payload 里是 ``scheduled_id``。

    **它是"定时任务"这个产品面的执行体**：调度侧到点把这条任务放进队列，
    真正干活的是 worker——于是"跑得慢、失败要重试、进程崩了要回收"全部复用
    任务队列已有的那一套（租约、心跳、指数退避），而不是在调度那一侧再实现一遍。
    payload 里的 ``manual=True`` 表示这是用户点了「立即跑一次」。
    """


class TaskState(StrEnum):
    """任务状态。

    ``RUNNING`` 依赖租约（lease）+ 心跳回收：进程崩溃后超时任务回到 ``PENDING`` 实现断点续跑。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class DataSourceKind(StrEnum):
    """数据源类型（《架构设计 v0.2》§10）。

    ``WEBDAV`` 为框架预留，MVP 不实现（架构 §14「缓做」）。
    """

    UPLOAD = "upload"
    HTML = "html"
    RSS = "rss"
    WEBDAV = "webdav"


class ApiKeyPermission(StrEnum):
    """API Key 权限（《架构设计 v0.2》§3.2：只读 / 读写 两种）。"""

    READONLY = "readonly"
    READWRITE = "readwrite"


class UserRole(StrEnum):
    """账号角色（migration v10 起名册升级为账号）。

    只有两档，刻意不加第三档：设置页里是 embedding / LLM 的密钥，
    "能改配置的"与"只能用自己数据的"之间不需要中间态。
    """

    ADMIN = "admin"
    MEMBER = "member"


class SharePermission(StrEnum):
    """知识库分享档位：读 = 可检索可对话；写 = 还能上传与删除。"""

    READ = "read"
    WRITE = "write"


class TrashKind(StrEnum):
    """回收站条目类型（《架构设计 v0.2》§6.2：原文保留 7 天冷备，向量立即删除）。"""

    ORIGINAL = "original"
    IMAGE = "image"
