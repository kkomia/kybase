"""MCP 工具的装配与调用（M6 之后的独立里程碑 / T4.8）。

**这张表就是知识库服务对外的那一面**（v0.1.0 瘦身收口）：只有纯知识库这一组工具，
``services/tools.py`` 里不再有笔记 / 记忆 / 交付 / 网页那些 Agent 侧工具——它们的实现
已随 Agent 侧代码一起拆除（剥离方案 §6 阶段 1 第 5 步）。

| 工具 | 作用 |
|------|------|
| `list_knowledge_bases` | 有哪些库 |
| `create_knowledge_base` | 建库 |
| `upload_document` | 传文档（内容用 base64 传） |
| `add_data_source` | 挂 RSS / 网页订阅 |
| `search` | 检索（产品的主打能力） |
| `list_documents` | 库里有哪些文档、处理到什么状态 |
| `get_document_status` | 文档处理到哪一步了 |
| `get_document_content` | 取文档的正文（Markdown）或原文件 |
| `delete_document` | 删文档（进回收站） |

**工具个数以 :data:`TOOL_NAMES` 为准**，上面那张表与它同源——这里刻意不写数字：
一句过期的"共 N 个"只会让读的人以为少挂了一个。

**为什么工具实现在这里、而不在 stdio/HTTP 的入口里**：两种传输方式要暴露同一批
工具。写在入口里就得复制两份，而两份迟早会漂（一个加了字段另一个没加）。
这里只依赖服务层，入口只负责把它挂到各自的传输上。

**与协议层的分层关系**：`mcp_server/` 与 `api/` 平级——都是"把服务层暴露出去"的适配层，
所以同样不许出现 SQL 与业务规则（工程规范 §3.3 L1）。工具函数做的是
"参数校验 + 调服务 + 收成可序列化的形状"这三件事。

**关于返回值**：MCP 工具的结果要给 LLM 读，所以**不用 pydantic 模型**，
直接给 dict / list——模型不需要 schema，而多一层转换只多一处出错的地方。

**每个工具都必须带上调用者**：``call_tool`` 的 ``caller``
是**必填关键字参数、没有默认值**——默认值一旦存在，"忘了传"就等于匿名放行，
而这类洞不会报错。作用域判定一律走 ``ApiKeyService``（``check_access`` /
``visible_kb_ids`` / ``check_tool``），不在这里另写一套：两套判定必然相漂。
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.exceptions import InvalidRequestError
from app.core.services import Services
from app.models.enums import DataSourceKind
from app.services.api_key import WRITE, Caller
from app.services.signing import signing_secret

__all__ = ["TOOL_META", "TOOL_NAMES", "call_tool", "tool_definitions"]

logger = logging.getLogger(__name__)

#: 上传大小的上限。MCP 走的是进程间消息，塞一个 200MB 的 base64
#: 会把客户端与服务端一起拖住——所以这里比 HTTP 上传更保守。
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: 取内容的字节上限。**与上传同一个 32MB**（直接借用那个常量，不另写一个数字）：
#: 两个方向走的是同一条进程间消息，一边卡住的量级另一边同样卡得住。超限时
#: **不截断**——原文件截一半是坏文件，回一条 REST 下载链接才有意义。
MAX_DOWNLOAD_BYTES = MAX_UPLOAD_BYTES

#: 正文内联返回的字符上限（约 20 万字符）。超了只回前 N 个字符并标 ``truncated``：
#: 一整本书的 Markdown 塞进模型的上下文既装不下也读不完，而"前 20 万字符 + 一个
#: 明确标志"足以让它知道该改用 REST 下载。
MAX_TEXT_CHARS = 200_000

#: 检索条数上限：工具是给 Agent 用的，它通常只要"够回答"的几条。
MAX_TOP_K = 20

#: 列表类工具的每页条数上限。给模型一个上限而不是"它说要多少就给多少"：
#: 上下文预算是有限的，而 50 条已经够它判断"库里有没有我要的东西"。
MAX_DOC_PAGE = 100
DEFAULT_DOC_PAGE = 30

#: **对外工具面 = 这张表本身**（v0.1.0）。MCP 的 ``MCP_TOOL_NAMES`` 直接取它，
#: 不存在第二份白名单——两份手写清单迟早会漂，而漂出来的形态是
#: "挂上去的工具在实现表里没有"或反过来。
TOOL_NAMES = (
    "list_knowledge_bases",
    "create_knowledge_base",
    "upload_document",
    "add_data_source",
    "search",
    "list_documents",
    "get_document_status",
    "get_document_content",
    "delete_document",
)


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """一个工具**给人看**的那点说明（工具目录端点用，见 ``api/v1/tools.py``）。

    与 ``tool_definitions()`` 的分工是刻意的：那里的 ``description`` 是**写给模型看的**
    （说清"什么时候该用"），这里的 ``label`` / ``summary`` 是**给管理界面看的**——
    它会渲染到屏幕上，所以只允许短词短句（界面上不写解释性小字，前端规范 §5.1）。
    两者不是一份东西，也就不能互相顶替。

    ``requires_write`` 与实现同源：它标注的是"这个工具会不会调 ``check_access(need=WRITE)``"，
    界面据此把只读那批与要写的那批分开。**不手写第二份参数表**——参数由
    ``tool_definitions()`` 的 ``inputSchema`` 推出来，两处各写一份必然相漂。
    """

    label: str
    """界面上的名字，短（如「文档内容」）。"""

    summary: str
    """一句话说清它做什么，短。"""

    requires_write: bool
    """是否写操作（要 ``WRITE`` 档、受库范围的写权限约束）。"""


#: 每个工具的人读元数据（``TOOL_META``）。**键必须覆盖 ``TOOL_NAMES`` 的每一个**：
#: 少一个的后果是工具目录端点当场 KeyError（500），而那条不变量的门禁是一条用例
#: （``tests/unit/services/test_tools.py::test_every_tool_has_catalog_metadata``）。
TOOL_META: dict[str, ToolMeta] = {
    "list_knowledge_bases": ToolMeta(
        label="知识库列表", summary="列出所有知识库", requires_write=False
    ),
    "create_knowledge_base": ToolMeta(
        label="新建知识库", summary="新建一个知识库", requires_write=True
    ),
    "upload_document": ToolMeta(
        label="上传文档", summary="把一份文档加进知识库", requires_write=True
    ),
    "add_data_source": ToolMeta(
        label="添加数据源", summary="挂一个 RSS 或网页来源", requires_write=True
    ),
    "search": ToolMeta(label="检索", summary="在知识库里检索原文片段", requires_write=False),
    "list_documents": ToolMeta(
        label="文档列表", summary="列出库里的文档与状态", requires_write=False
    ),
    "get_document_status": ToolMeta(
        label="文档状态", summary="查文档处理进度", requires_write=False
    ),
    "get_document_content": ToolMeta(
        label="文档内容", summary="取文档的正文或原文件", requires_write=False
    ),
    "delete_document": ToolMeta(
        label="删除文档", summary="删除文档，进回收站", requires_write=True
    ),
}


def tool_definitions() -> list[dict[str, Any]]:
    """工具的 JSON Schema 定义（MCP 协议用它描述给客户端）。

    描述文案是**写给模型看的**，不是写给人看的：说清"什么时候该用"比
    说清"参数是什么"更重要——模型选错工具的代价比填错参数大得多。
    """
    return [
        {
            "name": "list_knowledge_bases",
            "description": (
                "列出所有知识库及其文档数。"
                "在检索之前先调它，确认要查哪个库——库里没有的东西检索不出来。"
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "create_knowledge_base",
            "description": "新建一个知识库。库之间相互隔离，检索时按库过滤。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "库名"},
                    "description": {"type": "string", "description": "可选说明"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "upload_document",
            "description": (
                "把一份文档加进知识库。内容用 base64 编码。"
                "入库是异步的：拿到 document_id 后可用 get_document_status 查进度。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "filename": {"type": "string", "description": "文件名，扩展名决定用哪个解析器"},
                    "content_base64": {"type": "string", "description": "文件内容的 base64"},
                },
                "required": ["knowledge_base_id", "filename", "content_base64"],
                "additionalProperties": False,
            },
        },
        {
            "name": "add_data_source",
            "description": (
                "给知识库挂一个会持续更新的来源（RSS 订阅或某个网页），"
                "之后内容会自动抓进来。**登记不会立刻抓取**。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["rss", "html"]},
                    "url": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["knowledge_base_id", "kind", "url"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search",
            "description": (
                "在知识库里检索原文片段。**这是本服务的主打能力**："
                "返回的是原文出处（含文档名与页码），不是生成的回答。"
                "需要一段连贯的话时用 REST 的 chat 接口，这个工具给的是依据。\n\n"
                "**结果的第一行就是判定**：`summary` 一句话说清候选多少、收敛后留了几条、"
                "契合程度（`distribution` 是它背后的完整分布，排在原文之前）——"
                "先读判定、再决定要不要翻 `hits`。\n"
                "- `fit`：`strong` 很对得上 / `weak` 只对上一部分 / `none` 这份资料答不了"
                "（`none` 时**如实说资料里没有**，不要拿这几条噪声编答案）/ "
                "`unknown` 这个库的嵌入模型没标定过、**判不了契合度**（这时按你要的条数返回，"
                "不自动收敛）；\n"
                "- `count` / `above_baseline`：兜底阈值之上有多少条、其中多少条真的过了基线；\n"
                "- `scores`：max / p90 / p75 / median / p25 / min 与 `band`（带宽小 = "
                "这一批都差不多，换阈值切出来的还是同一批内容）；\n"
                "- `documents` / `per_document`：落在几篇文档上、每篇几条——**冗余在这里**。\n\n"
                "`keep` / `min_score` / `per_doc` 就是**你按这份分布收敛**的三个旋钮"
                "（不传 = 用 `distribution.suggested` 的建议）：这一轮只要最相关的两三段就传 "
                "`keep=3`；要更严就传 `min_score`（绝对余弦）；同一个问题被好几本书各答一遍时"
                "用 `per_doc`（每篇最多留几条）收敛。`decision` 会告诉你这次是模型定的还是"
                "按建议走的。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，用自然语言即可"},
                    "knowledge_base_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "在哪些库里查；留空则查全部",
                    },
                    "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K},
                    "keep": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TOP_K,
                        "description": "这一轮最多要几段（留空 = 用分布给出的建议）",
                    },
                    "min_score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "相似度下限（绝对余弦，留空 = 用建议）",
                    },
                    "per_doc": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "每篇文档最多留几段（留空 = 用建议）",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_document_status",
            "description": (
                "查一份文档处理到哪一步了。"
                "阶段依次是 uploaded → parsing → parsed → chunked → embedding → indexed。"
                "**只有 indexed 才可被检索到**。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_document_content",
            "description": (
                "读一份文档的全文。search 只返回与问题相关的片段，"
                "**要通读全文或核对原文时用它**。\n\n"
                "- `format` 取 `markdown`（默认）：解析后的 Markdown 文本，检索依据的就是它；\n"
                "- `format` 取 `original`：原文件的 base64（要拿原始版式时用），"
                "带 filename 与 media_type 供你落盘。\n\n"
                "原文超过 32MB 时**不回内容**（截一半的原文是坏文件），"
                "改用返回的 download_url 或 REST 下载接口取。"
                "Markdown 很长时只回前一段并给出 `truncated=true`。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "original"],
                        "description": "markdown=解析产物（默认），original=原文件",
                    },
                },
                "required": ["document_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_document",
            "description": (
                "删除一份文档。原文会进回收站保留 7 天，切块与向量立即清除——"
                "所以删除后**立刻搜不到**了。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_documents",
            "description": (
                "列出某个知识库里的文档及其处理状态。"
                "**想确认「这个库里到底有什么」时用它**——search 只返回与问题相关的片段，"
                "看不出库的全貌。可按文件名片段过滤。"
                "只有 searchable 为 true 的文档能被检索到。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "query": {"type": "string", "description": "按文件名片段过滤，可留空"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_DOC_PAGE,
                        "description": f"最多返回几条，默认 {DEFAULT_DOC_PAGE}",
                    },
                },
                "required": ["knowledge_base_id"],
                "additionalProperties": False,
            },
        },
    ]


def call_tool(
    services: Services,
    name: str,
    arguments: dict[str, Any] | None,
    *,
    caller: Caller,
) -> Any:
    """执行一个工具。**未知工具报错而不是返回空**——静默失败会让模型
    以为"查到了但没有结果"，然后基于错误前提继续推理。

    ``caller`` 是必填的（见模块头）：调用方从 ``auth.current_caller()`` 取，
    拿不到就会在那里抛 401，而不是走到这里变成匿名调用。
    """
    args = arguments or {}
    handler = _HANDLERS.get(name)
    if handler is None:
        raise InvalidRequestError(f"未知的工具：{name}（可用：{'、'.join(TOOL_NAMES)}）")
    # 已知工具之后再判工具范围：**顺序不能反**。反过来的话，一个不存在的工具名
    # 会先撞上工具范围（报"未授权调用工具：xxx"），把"这个名字不存在"这件事
    # 说成"你没有权限"——模型据此会以为换个参数就能过。
    services.api_keys.check_tool(caller, name)
    return handler(services, args, caller=caller)


# --------------------------------------------------------------------- 各工具


def _require(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise InvalidRequestError(f"缺少参数：{key}")
    return value


def _int_or_none(value: Any) -> int | None:
    """可选的整数参数：缺失/空/解析不了都返回 ``None``（= 用默认）。

    解析不了**不报错**：这些参数是"模型顺手给的调节量"，给错一个数就让整次检索失败
    不划算——退回默认值，结果照样能用（与 `modes.coerce` 同一取舍）。
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    """可选的浮点参数（同 ``_int_or_none`` 的取舍）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _visible_items(services: Services, caller: Caller) -> list[Any]:
    """当前调用者能看到的库。``visible_kb_ids`` 返回 ``None`` 表示**不受限**
    （管理员会话，或范围为空 = 不限范围的 API Key），此时不过滤。"""
    visible = services.api_keys.visible_kb_ids(caller)
    items = services.knowledge_bases.list_all()
    if visible is None:
        return items
    allowed = set(visible)
    return [item for item in items if item.id in allowed]


def _list_knowledge_bases(
    services: Services, args: dict[str, Any], *, caller: Caller
) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "documents": services.documents.count_documents(item.id),
            "embedding_model": item.embedding_model_id,
        }
        for item in _visible_items(services, caller)
    ]


def _create_knowledge_base(
    services: Services, args: dict[str, Any], *, caller: Caller
) -> dict[str, Any]:
    name = _require(args, "name")
    # 不涉及既有库，所以只判权限档位（只读 Key 会被拒）。
    # **kb_ids 传 None**：这是"要新建"，不是"要访问某个既有库"
    services.api_keys.check_access(caller, need=WRITE)
    # kb_id 由调用方生成：服务层要求显式传入（与 REST 层同一口径），
    # 这样将来要支持"由客户端指定 id"时不用改服务层签名
    record = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}",
        name=name,
        # 归属要跟着身份走：不写 owner 的话，成员建出来的库**自己都看不见**
        # （visible_kb_ids 对成员只算"自己拥有的 + 被分享的"）
        owner_id=caller.user.id if caller.user is not None else None,
    )
    return {"id": record.id, "name": record.name}


def _upload_document(services: Services, args: dict[str, Any], *, caller: Caller) -> dict[str, Any]:
    kb_id = _require(args, "knowledge_base_id")
    filename = _require(args, "filename")
    raw = _require(args, "content_base64")

    # 写入必须先判作用域：越界时**指出是哪个库**，
    # 模型据此能告诉用户"这把 Key 没有那个库的写权限"，而不是笼统地失败
    services.api_keys.check_access(caller, need=WRITE, kb_ids=[kb_id])

    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        # 说清是"base64 不合法"而不是笼统的"参数错误"——
        # 模型据此能自己改对（重新编码）而不是放弃这个工具
        raise InvalidRequestError(f"content_base64 不是合法的 base64：{exc}") from exc

    if len(content) > MAX_UPLOAD_BYTES:
        raise InvalidRequestError(
            f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限。"
            "MCP 走进程间消息，更大的文件请用 REST 上传"
        )

    outcome = services.ingest.submit(
        knowledge_base_id=kb_id,
        filename=filename,
        content=content,
        # 记上"是谁传的"：多用户下这是文档列表里的上传者列，缺了就显示"未记录"
        uploaded_by=caller.user.id if caller.user is not None else None,
    )
    if not outcome.is_duplicate:
        services.documents.enqueue_ingest(outcome.document.id)

    return {
        "document_id": outcome.document.id,
        "name": outcome.document.name,
        "is_duplicate": outcome.is_duplicate,
        "note": (
            "内容与库中已有文档相同，未重复入库"
            if outcome.is_duplicate
            else "已入队处理，可用 get_document_status 查进度"
        ),
    }


def _add_data_source(services: Services, args: dict[str, Any], *, caller: Caller) -> dict[str, Any]:
    kb_id = _require(args, "knowledge_base_id")
    kind = _require(args, "kind").lower()
    url = _require(args, "url")
    if kind not in ("rss", "html"):
        raise InvalidRequestError(f"kind 只能是 rss 或 html，收到：{kind}")

    # 挂数据源会让内容源源不断进库，是**写**操作
    services.api_keys.check_access(caller, need=WRITE, kb_ids=[kb_id])

    record = services.sources.create(
        knowledge_base_id=kb_id,
        kind=DataSourceKind(kind),
        name=str(args.get("name") or ""),
        url=url,
    )
    return {
        "id": record.id,
        "name": record.name,
        "note": "已登记。内容不会立刻抓取——等定时任务，或在控制台点「立即拉取」",
    }


def _search(services: Services, args: dict[str, Any], *, caller: Caller) -> dict[str, Any]:
    query = _require(args, "query")
    kb_ids = [str(item) for item in (args.get("knowledge_base_ids") or [])]
    if not kb_ids:
        # 留空 = 查**这个调用者能看到的全部**：Agent 常常不知道有哪些库，
        # 逼它先列一遍是多余的一步。注意这里**不是** list_all()——
        # 那是"所有人的库"，在收口之前正是越权的来源
        kb_ids = [item.id for item in _visible_items(services, caller)]
    else:
        # 显式指定了库就把越界挡在检索之前：检索是很重的操作，
        # 让它先跑完再拒，白烧一次算力
        services.api_keys.check_access(caller, kb_ids=kb_ids)
    if not kb_ids:
        return {"query": query, "hits": [], "note": "没有任何知识库"}

    top_k = int(args.get("top_k") or 6)
    top_k = max(1, min(MAX_TOP_K, top_k))

    # 动态返回（v0.54；v0.55 起**无条件**）：兜底阈值与基线从设置读（用户可配，只调不关），
    # 模型给的 keep/min_score/per_doc 覆盖分布的建议。**读出什么就交什么**——
    # 这一位不再有"关掉"的取值（曾经 ``floor_score = "0"`` 就等于"不要分布"，
    # 见 `retrieval.service.resolve_stats_floor`）。
    stats_floor, baseline = services.runtime.retrieval_thresholds()
    response = services.retrieval.search(
        _query(
            query=query,
            kb_ids=kb_ids,
            top_k=top_k,
            stats_floor=stats_floor,
            baseline=baseline,
            keep=_int_or_none(args.get("keep")),
            min_score=_float_or_none(args.get("min_score")),
            per_doc=_int_or_none(args.get("per_doc")),
        )
    )
    distribution = response.distribution
    decision = response.decision
    decided_by = "" if decision is None else str(decision.get("decided_by") or "")
    payload: dict[str, Any] = {
        # **判定在前、原文在后**（v0.55 的产品口径）：模型先读到一句话的判定
        # （候选多少、收敛后留几条、契合程度如何），再决定要不要翻下面的原文。
        # 键的顺序就是阅读顺序——分布排在 `hits` 之后时，它只会被当成事后统计。
        "summary": (
            distribution.headline(kept=len(response.hits), decided_by=decided_by)
            if distribution is not None
            else ""
        ),
        "distribution": distribution.as_payload() if distribution is not None else None,
        "decision": decision,
        "returned": len(response.hits),
        "filtered_out": response.filtered_out,
        "query": query,
        "hits": [
            {
                "document_id": hit.document_id,
                "document_name": hit.document_name or hit.document_id,
                "text": hit.text,
                "score": round(hit.score, 4),
                "page": hit.page,
                "heading_path": hit.heading_path,
                # `chunk_id` 与所属库是**给"要用这些片段做引用"的调用方**用的：
                # 界面上的出处卡片要有 chunk_id 才连得回原文。对外部 MCP 客户端同样有用。
                "chunk_id": hit.chunk_id,
                "knowledge_base_id": hit.knowledge_base_id,
                # 相似度（余弦）与融合分**不是一回事**：界面与模型都按它判断"多相关"
                "similarity": (
                    round(hit.raw_scores["vector"], 4) if "vector" in hit.raw_scores else None
                ),
            }
            for hit in response.hits
        ],
    }
    if distribution is not None and distribution.fit == "none":
        # 拟合度是"这份资料答不了"时**必须说清**：不说的话模型会把这批 0.8x 的片段
        # 当依据编答案，而那正是最坏的一种回答（看着有出处，其实没有内容）。
        # 这一条**不折进 `summary`**：`summary` 是每批都有的一行判定，读多了会被跳过，
        # 而"资料里没有"必须被看见。收敛参数（keep / min_score / per_doc 与来路）
        # 已经写在 `summary` 与 `decision` 里，不再另起一句重复一遍。
        payload["note"] = distribution.note
    return payload


def _document_or_403(services: Services, document_id: str, *, caller: Caller) -> Any:
    """取文档并判它所属库的读权限。

    顺序是"先取再判"：文档记录里才有所属库 id，没有它无从判起。
    代价是"猜 id 探测存在性"——不存在的 id 报 404、存在但越界的报 403，
    两者可分。局域网自用工具的这个量级上可接受，真要收紧就得把
    doc_id 也变成不可枚举的。
    """
    record = services.documents.get(document_id)
    services.api_keys.check_access(caller, kb_ids=[record.knowledge_base_id])
    return record


def _get_document_status(
    services: Services, args: dict[str, Any], *, caller: Caller
) -> dict[str, Any]:
    document_id = _require(args, "document_id")
    record = _document_or_403(services, document_id, caller=caller)
    chunks = services.documents.chunk_count(record.id)
    # `searchable` 按**产物**算而不是按 stage（见 DocumentsService.is_searchable 的说明）：
    # 卡在 embedding 但已经切好块/落了向量的文档**查得到**，原先这里回 false，
    # 模型据此就不搜了。
    searchable = services.documents.is_searchable(record, chunks=chunks)
    return {
        "document_id": record.id,
        "name": record.name,
        "stage": record.stage.value,
        "chunks": chunks,
        "error": record.error,
        "searchable": searchable,
        "note": (
            "已可检索"
            if searchable
            else "尚未完成处理，此时检索不到它"
            if not record.disabled
            else "这份文档已停用，不参与检索"
        ),
    }


def _absolute_download_url(services: Services, document_id: str, *, fmt: str) -> str | None:
    """把签名后的**相对**下载地址拼成绝对地址；拼不出来时返回 ``None``。

    两个条件缺一不可：**有签名密钥**（没有密钥就签不出任何链接，见 ``services.signing``）
    且**知道对外基址**（``settings.public_base_url``）。缺任何一个都回 ``None``，而不是
    半截相对路径——相对路径给到另一台机器上的客户端等于一条打不开的链接，
    那比"没有这一项"更误导。
    """
    settings = get_settings()
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        return None
    secret = signing_secret(settings, services)
    if not secret:
        return None
    relative, _expires = services.documents.download_url(document_id, fmt=fmt, secret=secret)
    return f"{base}{relative}"


def _get_document_content(
    services: Services, args: dict[str, Any], *, caller: Caller
) -> dict[str, Any]:
    document_id = _require(args, "document_id")
    fmt = str(args.get("format") or "markdown").strip().lower()
    if fmt not in ("markdown", "original"):
        # 说清可选值：模型据此能自己改对，而不是放弃这个工具
        raise InvalidRequestError(f"format 只能是 markdown 或 original，收到：{fmt}")

    # **先判范围再取内容**：内容可能很大，越界的那一次不该先把它整份读进内存
    record = _document_or_403(services, document_id, caller=caller)
    content = services.documents.content(document_id, fmt=fmt)

    if len(content.data) > MAX_DOWNLOAD_BYTES:
        # **不截断**：截一半的原文是坏文件，截一半的正文更容易被当成全文读。
        # 让调用方改走 REST 那条能真下完的路
        raise InvalidRequestError(
            f"内容超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB，MCP 不回这么大的载荷。"
            f"请改用 REST 下载接口：GET /api/v1/documents/{document_id}/download-url"
        )

    payload: dict[str, Any] = {
        "document_id": record.id,
        "name": record.name,
        "format": fmt,
        "media_type": content.media_type,
        "filename": content.filename,
        "size_bytes": len(content.data),
    }

    if fmt == "markdown":
        text = content.data.decode("utf-8", errors="replace")
        payload["text"] = text[:MAX_TEXT_CHARS]
        if len(text) > MAX_TEXT_CHARS:
            # **截断了必须说**：不说的话模型会把这一半当全文，然后据此回"文里没有提到 X"
            # ——而 X 就在被截掉的那一半里
            payload["truncated"] = True
    else:
        payload["base64"] = base64.b64encode(content.data).decode("ascii")

    url = _absolute_download_url(services, document_id, fmt=fmt)
    if url is not None:
        payload["download_url"] = url
    return payload


def _delete_document(services: Services, args: dict[str, Any], *, caller: Caller) -> dict[str, Any]:
    document_id = _require(args, "document_id")
    record = services.documents.get(document_id)
    # 删除是写操作：只读分享拿到的库不能删
    services.api_keys.check_access(caller, need=WRITE, kb_ids=[record.knowledge_base_id])
    entry = services.lifecycle.delete_document(document_id)
    return {
        "document_id": document_id,
        "trash_id": entry.id,
        "restorable_until": entry.expires_at.isoformat(),
        "note": "原文保留 7 天可恢复；切块与向量已清除，现在搜不到了",
    }


def _list_documents(services: Services, args: dict[str, Any], *, caller: Caller) -> dict[str, Any]:
    kb_id = _require(args, "knowledge_base_id")
    services.api_keys.check_access(caller, kb_ids=[kb_id])
    limit = max(1, min(int(args.get("limit") or DEFAULT_DOC_PAGE), MAX_DOC_PAGE))
    records = services.documents.list_documents(
        kb_id, q=str(args.get("query") or "").strip() or None, limit=limit
    )
    # 批量取切块数（一次查询），`searchable` 由此按产物算——见 DocumentsService.is_searchable
    counts = services.documents.chunk_counts([item.id for item in records])
    return {
        "knowledge_base_id": kb_id,
        "total": services.documents.count_documents(kb_id),
        "documents": [
            {
                "document_id": item.id,
                "name": item.name,
                "stage": item.stage.value,
                "searchable": services.documents.is_searchable(
                    item, chunks=counts.get(item.id, 0)
                ),
                "disabled": item.disabled,
            }
            for item in records
        ],
        "note": "searchable 为 true 的文档现在就能被 search 检索到（按已落库的切块算，不按阶段）",
    }


_HANDLERS = {
    "list_knowledge_bases": _list_knowledge_bases,
    "create_knowledge_base": _create_knowledge_base,
    "upload_document": _upload_document,
    "add_data_source": _add_data_source,
    "search": _search,
    "list_documents": _list_documents,
    "get_document_status": _get_document_status,
    "get_document_content": _get_document_content,
    "delete_document": _delete_document,
}


def _query(  # type: ignore[no-untyped-def]
    *,
    query: str,
    kb_ids: list[str],
    top_k: int,
    stats_floor: float | None = None,
    baseline: float | None = None,
    keep: int | None = None,
    min_score: float | None = None,
    per_doc: int | None = None,
):
    """构造检索请求。

    放在函数里 import：``mcp_server`` 层与 ``services`` 层都往这儿引用，
    顶层 import 会让这条单向依赖变得不明显。

    ``stats_floor`` / ``baseline`` 是**这次检索的基础阈值**（设置页可调，见
    ``runtime_config.retrieval_thresholds``）。注意 ``stats_floor`` **不是开关**：
    给它 ``None`` 或非正数只是"用默认窗口"，分布与收敛照常跑（v0.55）。
    """
    from app.services.retrieval import RetrievalQuery

    return RetrievalQuery(
        query=query,
        kb_ids=kb_ids,
        top_k=top_k,
        stats_floor=stats_floor,
        baseline=baseline,
        keep=keep,
        min_score=min_score,
        per_doc=per_doc,
    )
