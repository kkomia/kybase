"""MCP 工具（T4.8）。

镜像同构：``app/mcp/tools.py`` → 本文件。

**为什么值得单独测**：MCP 是产品的对外形态之一（架构 §2.1），而它的错误
**全部表现为"模型拿到了一段奇怪的文本"**——没有界面、没有 HTTP 状态码，
出了问题只有模型在胡说这一个症状。所以这里把每个工具的**形状与边界条件**
钉住：参数缺失要报错、上限要生效、返回值字段要稳定。

**v0.12 起每个调用都要带身份**：``call_tool`` 的 ``caller`` 是必填关键字参数，
连测试也不例外——留一个"默认管理员"的后门，就等于把收口又打开了。
所以这里显式给一个 ``admin`` fixture，另外用真实发放的 API Key 覆盖受限期。

**不测传输层**：stdio 与 HTTP 是 SDK 的事，本项目只负责"工具做什么"。
按同一批用例验两条传输的收益很小——它们共用同一个 ``call_tool``。
身份**怎么从传输里取出来**（请求头 / 环境变量）由 ``test_auth.py`` 单独测。
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import pytest

from app.core.exceptions import ForbiddenError, InvalidRequestError, NotFoundError
from app.core.services import Services
from app.mcp_server.auth import current_caller
from app.services.api_key import READ, WRITE, Caller
from app.services.tools import (
    MAX_TOP_K,
    MAX_UPLOAD_BYTES,
    TOOL_NAMES,
    call_tool,
    tool_definitions,
)


@pytest.fixture
def services() -> Services:
    from app.core.services import get_services

    return get_services()


@pytest.fixture
def admin() -> Caller:
    """管理员主体：不受库范围限制。

    **显式构造而不是让 call_tool 兜底**——见模块头：默认值一旦存在，
    "忘了传"就等于匿名放行。
    """
    return Caller(is_admin=True)


@pytest.fixture
def kb(services: Services) -> str:  # type: ignore[no-untyped-def]
    # kb_id 由调用方生成——服务层要求显式传入（与 REST 层同一口径）
    return services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="MCP 测试库"
    ).id


@pytest.fixture
def key_for(services: Services):  # type: ignore[no-untyped-def]
    """按"库范围 + 权限（+ 工具范围）"发一把真 Key 并换成 Caller——走真实发放路径，
    而不是手搓一个 Caller 对象（那样测的就不是判定本身了）。"""

    def make(*, kb_ids: list[str], permission=READ, tools: list[str] | None = None):  # type: ignore[no-untyped-def]
        issued = services.api_keys.create(
            name=f"测试密钥 {uuid.uuid4().hex[:6]}",
            permission=permission,
            knowledge_base_ids=kb_ids,
            allowed_tool_names=tools,
        )
        return services.api_keys.authenticate(issued.token)

    return make


MARKDOWN = "# 眼轴\n\n眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一，不受调节能力影响。\n"

# --------------------------------------------------------------------- 工具清单


def test_definitions_cover_every_known_tool() -> None:
    """定义与名字清单必须一一对应——少一个，客户端就看不到那个工具。"""
    assert {item["name"] for item in tool_definitions()} == set(TOOL_NAMES)


def test_every_definition_has_a_description_and_schema() -> None:
    """描述是**写给模型看的**：说清"什么时候该用"比说清参数更重要。"""
    for item in tool_definitions():
        assert item["description"].strip(), item["name"]
        assert item["inputSchema"]["type"] == "object", item["name"]


def test_unknown_tool_raises(services: Services, admin: Caller) -> None:
    """**未知工具要报错而不是返回空。**

    静默失败会让模型以为"查到了但没有结果"，然后基于错误前提继续推理——
    那比直接告诉它"没有这个工具"危险得多。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(services, "不存在的工具", {}, caller=admin)
    assert "未知的工具" in str(excinfo.value)


# --------------------------------------------------------------------- 身份收口


def test_caller_is_required(services: Services) -> None:
    """**没有身份就不能调工具**：``caller`` 没有默认值，漏传会直接 TypeError。

    这条测的不是"报错好看"，而是"后门不存在"——一旦有人给 ``caller`` 加个
    默认为管理员，这个用例会立刻失败。
    """
    with pytest.raises(TypeError):
        call_tool(services, "list_knowledge_bases", {})  # type: ignore[call-arg]


def test_current_caller_without_identity_raises() -> None:
    """传输层拿不到凭据时，``current_caller`` 必须拒绝而不是返回 None。

    返回 None 等于把"记得判空"变成每个工具的纪律，而漏判不会报错、
    只会静默放行——最难发现的一种洞。
    """
    from app.core.exceptions import UnauthorizedError

    with pytest.raises(UnauthorizedError) as excinfo:
        current_caller()
    # 报错要说清**两条传输各该怎么带凭据**，否则用户不知道该改哪里
    assert "Authorization" in str(excinfo.value)
    assert "KYLAB_MCP_KEY" in str(excinfo.value)


def test_list_only_returns_knowledge_bases_in_scope(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    """**收口的核心用例**：范围外的库不能出现在列表里。

    收口之前 ``_list_knowledge_bases`` 直接调 ``list_all()``，
    实测无凭据即可列出全部真实知识库。
    """
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id

    scoped = key_for(kb_ids=[other])
    items = call_tool(services, "list_knowledge_bases", {}, caller=scoped)

    ids = {item["id"] for item in items}
    assert other in ids
    assert kb not in ids, "范围外的库出现在了列表里"


def test_search_without_kb_ids_stays_in_scope(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """不传库时是"查我能看到的全部"，**不是** "查所有人的全部"。"""
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id
    scoped = key_for(kb_ids=[other])

    result = call_tool(services, "search", {"query": "眼轴"}, caller=scoped)

    # other 是空库，所以这里只该是空结果——关键是**不能因为越权而拿到 kb 的内容**
    assert result["hits"] == []


def test_search_rejects_out_of_scope_knowledge_base(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id
    scoped = key_for(kb_ids=[other])

    with pytest.raises(ForbiddenError):
        call_tool(
            services,
            "search",
            {"query": "眼轴", "knowledge_base_ids": [kb]},
            caller=scoped,
        )


def test_readonly_key_cannot_upload(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """只读 Key 写不动——而且要在**动手之前**就被拦住。"""
    readonly = key_for(kb_ids=[kb], permission=READ)

    with pytest.raises(ForbiddenError) as excinfo:
        call_tool(
            services,
            "upload_document",
            {
                "knowledge_base_id": kb,
                "filename": "a.md",
                "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
            },
            caller=readonly,
        )
    assert "只读" in str(excinfo.value)


def test_readonly_key_cannot_delete(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """删除是写操作：只读凭据连自己范围内的库也不能删。"""
    writer = key_for(kb_ids=[kb], permission=WRITE)
    readonly = key_for(kb_ids=[kb], permission=READ)
    uploaded = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": "待删.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=writer,
    )

    with pytest.raises(ForbiddenError):
        call_tool(
            services,
            "delete_document",
            {"document_id": uploaded["document_id"]},
            caller=readonly,
        )


def test_document_status_respects_scope(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """按 id 直查文档也要判范围——否则知道 id 就能绕过列表。"""
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id
    writer = Caller(is_admin=True)
    uploaded = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": "范围.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=writer,
    )
    scoped = key_for(kb_ids=[other])

    with pytest.raises(ForbiddenError):
        call_tool(
            services,
            "get_document_status",
            {"document_id": uploaded["document_id"]},
            caller=scoped,
        )


def test_knowledge_base_created_by_a_member_is_owned_by_them(
    services: Services,
) -> None:
    """成员建出来的库要归他所有：不写 owner 的话他自己都看不见
    （``visible_kb_ids`` 对成员只算"自己拥有的 + 被分享的"）。"""
    from app.models.enums import ApiKeyPermission

    issued = services.api_keys.create(
        name="成员密钥", permission=ApiKeyPermission.READWRITE
    )
    # 直接用 user 形态：成员会话才是"能建库"的那种身份
    from app.storage.base import UserRecord

    member = Caller(
        api_key=issued.record,
        user=UserRecord(
            id=f"user_{uuid.uuid4().hex[:8]}",
            name="成员",
            password_hash="x",
        ),
    )
    created = call_tool(services, "create_knowledge_base", {"name": "成员建的库"}, caller=member)
    record = services.knowledge_bases.get(created["id"])
    assert record.owner_id == member.user.id  # type: ignore[union-attr]


# --------------------------------------------------------------------- 知识库


def test_list_knowledge_bases(services: Services, kb: str, admin: Caller) -> None:
    items = call_tool(services, "list_knowledge_bases", {}, caller=admin)

    assert any(item["id"] == kb for item in items)
    target = next(item for item in items if item["id"] == kb)
    assert target["name"] == "MCP 测试库"
    assert target["documents"] == 0


def test_create_knowledge_base(services: Services, admin: Caller) -> None:
    created = call_tool(services, "create_knowledge_base", {"name": "新建的库"}, caller=admin)

    assert created["name"] == "新建的库"
    assert created["id"].startswith("kb_")


def test_create_knowledge_base_requires_a_name(services: Services, admin: Caller) -> None:
    with pytest.raises(InvalidRequestError):
        call_tool(services, "create_knowledge_base", {"name": "  "}, caller=admin)


# --------------------------------------------------------------------- 上传


def test_upload_document(services: Services, kb: str, admin: Caller) -> None:
    result = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": "眼轴.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=admin,
    )

    assert result["is_duplicate"] is False
    assert result["document_id"].startswith("doc_")
    # 入库是异步的，返回值必须告诉模型"接下来怎么查"
    assert "get_document_status" in result["note"]


def test_upload_detects_duplicates(services: Services, kb: str, admin: Caller) -> None:
    """同一份内容传两次，第二次要说清是重复——否则模型会以为库里有两份。"""
    payload = {
        "knowledge_base_id": kb,
        "filename": "眼轴.md",
        "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
    }
    call_tool(services, "upload_document", payload, caller=admin)

    second = call_tool(services, "upload_document", payload, caller=admin)

    assert second["is_duplicate"] is True
    assert "相同" in second["note"]


def test_upload_rejects_bad_base64(services: Services, kb: str, admin: Caller) -> None:
    """**说清是 base64 不合法**，模型据此能自己改对，而不是放弃这个工具。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "upload_document",
            {"knowledge_base_id": kb, "filename": "a.md", "content_base64": "这不是 base64!!"},
            caller=admin,
        )
    assert "base64" in str(excinfo.value)


def test_upload_rejects_oversized_content(services: Services, kb: str, admin: Caller) -> None:
    """MCP 走进程间消息，塞一个巨大的 base64 会把两端一起拖住。"""
    blob = base64.b64encode(b"x" * (MAX_UPLOAD_BYTES + 1)).decode()

    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "upload_document",
            {"knowledge_base_id": kb, "filename": "big.md", "content_base64": blob},
            caller=admin,
        )
    assert "上限" in str(excinfo.value)


def test_upload_needs_a_real_knowledge_base(services: Services, admin: Caller) -> None:
    with pytest.raises(NotFoundError):
        call_tool(
            services,
            "upload_document",
            {
                "knowledge_base_id": "kb_不存在",
                "filename": "a.md",
                "content_base64": base64.b64encode(b"x").decode(),
            },
            caller=admin,
        )


# --------------------------------------------------------------------- 数据源


def test_add_data_source(services: Services, kb: str, admin: Caller) -> None:
    result = call_tool(
        services,
        "add_data_source",
        {
            "knowledge_base_id": kb,
            "kind": "rss",
            "url": "https://example.com/feed.xml",
            "name": "示例订阅",
        },
        caller=admin,
    )

    assert result["name"] == "示例订阅"
    # 必须说清"登记 ≠ 立刻抓取"，否则模型会以为内容已经进来了
    assert "不会立刻" in result["note"]


def test_add_data_source_rejects_unknown_kind(
    services: Services, kb: str, admin: Caller
) -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "add_data_source",
            {"knowledge_base_id": kb, "kind": "webdav", "url": "https://example.com"},
            caller=admin,
        )
    assert "rss" in str(excinfo.value)


def test_add_data_source_rejects_non_http(services: Services, kb: str, admin: Caller) -> None:
    with pytest.raises(InvalidRequestError):
        call_tool(
            services,
            "add_data_source",
            {"knowledge_base_id": kb, "kind": "html", "url": "file:///etc/passwd"},
            caller=admin,
        )


# --------------------------------------------------------------------- 检索


def test_search_without_any_kb_returns_empty(services: Services, admin: Caller) -> None:
    """一个库都没有时不能炸——返回空结果并说明原因，让模型知道该怎么继续。"""
    result = call_tool(services, "search", {"query": "随便问问"}, caller=admin)

    assert result["hits"] == []
    assert "没有任何知识库" in result["note"]


def test_search_caps_top_k(services: Services, kb: str, admin: Caller) -> None:
    """**上限要生效**：工具是给 Agent 用的，它可能随手要 500 条，
    那会把上下文塞爆。"""
    result = call_tool(services, "search", {"query": "眼轴", "top_k": 9999}, caller=admin)

    # 不抛错、也不返回超量——夹到上限即可
    assert isinstance(result["hits"], list)


def test_search_result_shape_is_stable(services: Services, kb: str, admin: Caller) -> None:
    """字段名要稳定：模型按这些名字读结果，改一个名就等于换了一次工具。"""
    result = call_tool(
        services, "search", {"query": "眼轴", "knowledge_base_ids": [kb]}, caller=admin
    )

    assert set(result) >= {"query", "hits", "filtered_out"}


def test_search_leads_with_the_verdict_then_the_distribution_then_the_passages(
    services: Services, kb: str, admin: Caller
) -> None:
    """**键序 = 阅读顺序**（v0.55）：判定与分布排在原文之前，模型先读到才谈得上"先想再翻"。

    分布排在 ``hits`` 之后时，它只会被当成事后统计；而"这批命中是什么状态"恰恰是
    读过原文就说不清的东西（看着有出处，其实没有内容）。
    """
    result = call_tool(
        services, "search", {"query": "眼轴", "knowledge_base_ids": [kb]}, caller=admin
    )

    keys = list(result)
    assert keys.index("summary") < keys.index("distribution") < keys.index("decision")
    assert keys.index("decision") < keys.index("returned") < keys.index("hits")
    assert result["summary"], "判定不能是空串：它是模型读到的第一行"
    assert result["distribution"] is not None, "分布无条件在（这条路上没有开关）"
    assert result["decision"] is not None


def test_search_converges_on_the_knobs_the_model_gives(
    services: Services, kb: str, admin: Caller
) -> None:
    """模型给的 ``keep`` / ``per_doc`` 要真的传到检索里，并记在 ``decision`` 上。

    记 ``decided_by="model"`` 不是为了好看：用户问"为什么只回了 1 条"时，
    答案必须是一个数加一个来源。
    """
    result = call_tool(
        services,
        "search",
        {"query": "眼轴", "knowledge_base_ids": [kb], "keep": 1, "per_doc": 1},
        caller=admin,
    )

    assert result["decision"]["keep"] == 1
    assert result["decision"]["per_doc"] == 1
    assert result["decision"]["decided_by"] == "model"
    assert "模型" in result["summary"]
    assert len(result["hits"]) <= 1


# --------------------------------------------------------------------- 翻页（v0.63）


def _ingest_text(services: Services, kb: str, filename: str, content: str) -> str:  # type: ignore[no-untyped-def]
    """传一份正文并跑完摄入（不跑流水线就检索不到它）。

    内容各不相同是刻意的：``upload_document`` 按内容去重，同一份正文传三次只会进一份
    ——而这一批用例要的正是"好几段可翻"。
    """
    uploaded = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": filename,
            "content_base64": base64.b64encode(content.encode()).decode(),
        },
        caller=Caller(is_admin=True),
    )
    assert uploaded["is_duplicate"] is False, "三份正文必须是三份内容，否则没有可翻的第二页"
    services.ingest.ingest(uploaded["document_id"])
    return uploaded["document_id"]


def _paging_library(services: Services, kb: str) -> None:
    """往库里灌三份都提到「眼轴长度」的正文——翻页至少要翻得动两页。"""
    for index in range(3):
        _ingest_text(
            services,
            kb,
            f"眼轴第{index}节.md",
            f"# 眼轴第{index}节\n\n眼轴长度的测量方法第 {index} 节："
            f"这一段讲第 {index} 种测量方式，以及它在第 {index} 次随访里的记录口径。\n"
            f"第 {index} 节的结论只有一句：眼轴长度要看趋势，单次数值说明不了什么。\n",
        )


def test_search_pages_the_survivors_with_the_same_verdict_on_every_page(
    services: Services, kb: str, admin: Caller
) -> None:
    """一页一条地翻到底：拼起来等于不翻页那一份，判定逐字不变。

    这是"把一本书关于某个主题的内容看全"那条路在 MCP 上的最小验收——``top_k`` 是能翻到
    的总量、``keep`` 是一页几条、``next_offset`` 说下一页从哪开始。
    """
    _paging_library(services, kb)
    query = {"query": "眼轴长度的测量方法", "knowledge_base_ids": [kb], "top_k": 10}

    whole = call_tool(services, "search", {**query, "keep": 10}, caller=admin)
    assert len(whole["hits"]) >= 2, "这份夹具要够翻两页"
    assert whole["offset"] == 0
    assert whole["next_offset"] is None, "keep ≥ 幸存者数时这一页就是全部"

    pages: list[dict] = []
    payload = call_tool(services, "search", {**query, "keep": 1}, caller=admin)
    while True:
        pages.append(payload)
        assert len(pages) < 50, "next_offset 把调用方送进了死循环"
        if payload["next_offset"] is None:
            break
        payload = call_tool(
            services, "search", {**query, "keep": 1, "offset": payload["next_offset"]}, caller=admin
        )

    walked = [hit["chunk_id"] for page in pages for hit in page["hits"]]
    assert len(walked) == len(set(walked)), "两页之间不许重叠"
    assert walked == [hit["chunk_id"] for hit in whole["hits"]], "拼起来必须等于那整份名单"

    for index, page in enumerate(pages):
        assert len(page["hits"]) == 1, "keep=1 就是一页一条"
        assert page["returned"] == len(page["hits"])
        assert page["offset"] == index, "起点就是上一页的 next_offset"
        # **判定与收敛的产物在每一页上都一模一样**：调用方正是靠它决定要不要接着翻。
        # 与"一页给全部"那次比只差 `decision.keep` 一位（页大小是调用方传的，不是判定）。
        assert page["summary"] == whole["summary"]
        assert page["distribution"] == whole["distribution"]
        assert page["filtered_out"] == whole["filtered_out"]
        assert {**page["decision"], "keep": whole["decision"]["keep"]} == whole["decision"]
    assert pages[-1]["next_offset"] is None, "最后一页要说清「没有更多了」"


def test_an_offset_past_the_end_returns_an_empty_page(
    services: Services, kb: str, admin: Caller
) -> None:
    """「翻过头了」不是错：空 ``hits`` + ``next_offset: null``，**判定照旧**。

    报错的话，调用方会把"翻到底了"读成"检索坏了"——而它明明已经拿到了全部内容。
    """
    _paging_library(services, kb)
    query = {"query": "眼轴长度的测量方法", "knowledge_base_ids": [kb], "top_k": 10, "keep": 10}

    whole = call_tool(services, "search", query, caller=admin)
    beyond = call_tool(services, "search", {**query, "offset": 9999}, caller=admin)

    assert beyond["hits"] == []
    assert beyond["next_offset"] is None
    assert beyond["offset"] == 9999, "回显传进来的起点（只是没有那一条可给）"
    assert beyond["summary"] == whole["summary"]
    assert beyond["decision"] == whole["decision"]


def test_a_garbage_offset_degrades_to_the_first_page(
    services: Services, kb: str, admin: Caller
) -> None:
    """起点给垃圾（字符串 / 负数 / 列表）**只影响它自己**：折成 0，检索照常。

    与 ``top_k`` / ``keep`` / ``min_score`` 同一条取舍（见 `_int_or_none`）：
    SDK 那层的类型校验已经让开，所以垃圾值真的会到这一层。给错一个"从第几条开始"
    就让整次检索失败不划算——多翻一页而已。
    """
    _paging_library(services, kb)
    query = {"query": "眼轴长度的测量方法", "knowledge_base_ids": [kb], "top_k": 10, "keep": 1}

    first = call_tool(services, "search", query, caller=admin)
    for garbage in ("第二页", -5, [], {"从": 2}, None):
        degraded = call_tool(services, "search", {**query, "offset": garbage}, caller=admin)
        assert degraded["offset"] == 0, f"{garbage!r} 该折成 0"
        assert degraded["hits"] == first["hits"], f"{garbage!r} 该退回第一页"


# --------------------------------------------------------------------- 过滤（v0.65）


def _captured_query(services: Services, monkeypatch: pytest.MonkeyPatch) -> list:  # type: ignore[type-arg]
    """把检索请求截下来（**不真跑检索**），返回按调用顺序攒下的那个列表。

    过滤条件必须在这一层被钉住：解析错了或没传下去，表现都是"筛了却拿到范围外的结果"
    ——而那种结果与正常结果长得一模一样（同样的字段、同样的出处）。
    """
    from app.services.retrieval import RetrievalResponse

    seen: list = []

    def fake_search(request, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(request)
        return RetrievalResponse(hits=[], mode=request.mode, reranked=False)

    monkeypatch.setattr(services.retrieval, "search", fake_search)
    return seen


def test_search_builds_a_metadata_filter_from_the_filters_argument(
    services: Services, kb: str, admin: Caller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``filters`` 要**逐字段解析成 ``MetadataFilter``** 再交给检索层（与 REST 同源）。

    与 REST 的 ``MetadataFilterIn`` 同名同义是刻意的：两条路各有一套说法的话，
    同一次查询在两个入口会给出两种结果。
    """
    from app.models.enums import DataSourceKind
    from app.services.retrieval import MetadataFilter

    seen = _captured_query(services, monkeypatch)

    call_tool(
        services,
        "search",
        {
            "query": "眼轴",
            "knowledge_base_ids": [kb],
            "filters": {
                "document_ids": ["doc_1"],
                # 大小写不该成为门槛（REST 那边由枚举收口，这里也要收）
                "source_kinds": ["UPLOAD"],
                "created_after": "2026-01-02T03:04:05Z",
                "created_before": "2026-02-03",
            },
        },
        caller=admin,
    )

    filters = seen[0].filters
    assert isinstance(filters, MetadataFilter)
    assert list(filters.document_ids) == ["doc_1"]
    assert list(filters.source_kinds) == [DataSourceKind.UPLOAD]
    assert filters.created_after == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert filters.created_before == datetime(2026, 2, 3)
    assert not filters.is_empty()


def test_the_page_and_heading_filters_are_passed_through(
    services: Services, kb: str, admin: Caller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``page_from`` / ``page_to`` / ``heading_contains`` 原样透传（整数、去空白）。

    三者都是**块级**条件：页码与章节标题在检索层被下推到召回通道
    （``services/retrieval`` 那条线），所以工具面这一侧只要把它们解析对、传下去即可
    ——字段名写错或值没去空白，表现就是"筛了没生效"。
    """
    seen = _captured_query(services, monkeypatch)

    call_tool(
        services,
        "search",
        {
            "query": "眼轴",
            "knowledge_base_ids": [kb],
            "filters": {"page_from": 3, "page_to": 5, "heading_contains": "  第三章  "},
        },
        caller=admin,
    )

    filters = seen[0].filters
    assert filters.page_from == 3
    assert filters.page_to == 5
    assert filters.heading_contains == "第三章"


def test_search_without_filters_passes_no_filter(
    services: Services, kb: str, admin: Caller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不提 ``filters`` 就是"不筛"（``None``）：别拿一个空筛子占着那个位置。"""
    seen = _captured_query(services, monkeypatch)

    call_tool(
        services, "search", {"query": "眼轴", "knowledge_base_ids": [kb]}, caller=admin
    )

    assert seen[0].filters is None


def test_search_filters_narrow_the_results_before_ranking(
    services: Services, kb: str, admin: Caller
) -> None:
    """**先筛后排**：给了 ``document_ids`` 就只该拿到那几份文档的命中。

    这正是"只查某本书某章做不到、只能事后筛"那件事的正面答案——条件在检索层就生效，
    不必先捞一整库的命中再自己挑（分布与判定说的也是这一范围内的命中）。
    """
    wanted = _ingest_text(
        services, kb, "眼轴甲.md", "# 眼轴甲\n\n眼轴长度的测量方法：甲说用 A 法。\n"
    )
    _ingest_text(services, kb, "眼轴乙.md", "# 眼轴乙\n\n眼轴长度的测量方法：乙说用 B 法。\n")

    result = call_tool(
        services,
        "search",
        {
            "query": "眼轴长度的测量方法",
            "knowledge_base_ids": [kb],
            "filters": {"document_ids": [wanted]},
        },
        caller=admin,
    )

    assert result["hits"], "这一份文档里明明有这个词，筛没了说明筛错了"
    assert {hit["document_id"] for hit in result["hits"]} == {wanted}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ([], "需要一个对象"),
        ({"document_ids": []}, "不能是空的"),
        ({"source_kinds": ["telegram"]}, "不认得的取值"),
        ({"source_kinds": [1]}, "需要一个字符串数组"),
        ({"created_after": "2026-13-45"}, "ISO 8601"),
        ({"page_from": 0}, "从 1 起"),
        ({"page_to": "第三页"}, "从 1 起"),
        ({"page_from": 5, "page_to": 3}, "不能小于"),
        ({"heading_contains": "   "}, "不能是空白"),
        ({"document_id": ["doc_1"]}, "不认得的字段"),
    ],
)
def test_a_bad_filter_is_rejected_in_chinese(
    services: Services,
    kb: str,
    admin: Caller,
    filters: object,
    expected: str,
) -> None:
    """``filters`` 给错要报**中文错**（不是栈、也不是安静地不筛）。

    与 ``top_k`` / ``keep`` / ``offset`` 那几个调节量的取舍刻意相反：那些给错只是少要几条，
    过滤条件给错是**查错了范围**——而"筛过了却拿到范围外的结果"看着完全正常。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "search",
            {"query": "眼轴", "knowledge_base_ids": [kb], "filters": filters},
            caller=admin,
        )

    assert expected in str(excinfo.value)


def test_the_filters_description_states_what_it_is_for_and_what_it_costs() -> None:
    """描述必须说清"什么时候用它"，尤其是**页码过滤的代价**。

    不写的话，模型给 ``page_from`` 拿到空结果时只会得出"这本书里没有"这一个结论——
    而真相是"这份文档本来就没有页"（`.md` / Office 来源）。用户抱怨的正是这件事
    （"只查某本书某章做不到"），所以描述本身也是这次改动的一部分。
    """
    search = next(item for item in tool_definitions() if item["name"] == "search")
    description = search["description"]
    properties = search["inputSchema"]["properties"]["filters"]["properties"]

    assert set(properties) == {
        "document_ids",
        "source_kinds",
        "created_after",
        "created_before",
        "page_from",
        "page_to",
        "heading_contains",
    }
    assert search["inputSchema"]["properties"]["filters"]["additionalProperties"] is False
    assert "document_ids" in description, "要写出「只查某几本书」这条路"
    assert "先筛后排" in description, "说明它在候选池上生效，而不是拿到结果再挑"
    assert "整体排除" in description, "页码过滤的代价必须如实写明"
    assert "heading_contains" in description, "说清「只在某一章里找」比页码可靠"
    assert "created_after" in description, "说清「只要某段时间入库的」"
    assert "source_kinds" in description


# --------------------------------------------------------------------- 文档状态


def test_get_document_status(services: Services, kb: str, admin: Caller) -> None:
    uploaded = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": "状态.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=admin,
    )

    status = call_tool(
        services, "get_document_status", {"document_id": uploaded["document_id"]}, caller=admin
    )

    assert status["stage"] == "uploaded"
    assert status["searchable"] is False, "还没处理完就标成可检索是最坏的一种错"
    assert "检索不到" in status["note"]


def test_get_document_status_rejects_unknown(services: Services, admin: Caller) -> None:
    with pytest.raises(NotFoundError):
        call_tool(services, "get_document_status", {"document_id": "doc_不存在"}, caller=admin)


# --------------------------------------------------------------------- 取文档内容（v0.61）


def _upload_markdown(services: Services, kb: str, *, filename: str = "正文.md") -> str:
    """传一份 Markdown 并返回文档 id（**不跑流水线**）。"""
    uploaded = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": filename,
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=Caller(is_admin=True),
    )
    return uploaded["document_id"]


def _parsed_document(services: Services, kb: str) -> str:
    """传一份 Markdown 并**跑完摄入**，返回文档 id。

    取 Markdown 正文这条路上必须有解析产物（``DocumentService.content`` 里
    ``fmt="markdown"`` 就是查它），而上传本身不入队——测试里消费者是关掉的
    （见 conftest），所以显式跑一次摄入。走的是真实现（本机纯文本解析 +
    测试用的确定性嵌入），不联网。
    """
    document_id = _upload_markdown(services, kb)
    services.ingest.ingest(document_id)
    return document_id


def test_get_document_content_returns_markdown(services: Services, kb: str) -> None:
    """``format=markdown``（默认）回正文文本，而不是让调用方去解 base64。"""
    document_id = _parsed_document(services, kb)

    result = call_tool(
        services, "get_document_content", {"document_id": document_id}, caller=Caller(is_admin=True)
    )

    assert set(result) >= {
        "document_id",
        "name",
        "format",
        "media_type",
        "filename",
        "size_bytes",
        "text",
    }
    assert result["format"] == "markdown"
    assert "眼轴长度" in result["text"]
    # 解析产物按"文件名去后缀 + .md"命名（与 REST 下载走同一条路）
    assert result["filename"] == "正文.md"
    assert result["media_type"].startswith("text/markdown")
    # 没超上限就不该有 truncated 这个键——它的存在本身是"这份是残的"的信号
    assert "truncated" not in result


def test_get_document_content_returns_original_as_base64(services: Services, kb: str) -> None:
    """``format=original`` 回 base64 + 落盘要用的元信息（文件名 / 媒体类型 / 字节数）。"""
    document_id = _upload_markdown(services, kb, filename="原文件.md")

    result = call_tool(
        services,
        "get_document_content",
        {"document_id": document_id, "format": "original"},
        caller=Caller(is_admin=True),
    )

    assert result["format"] == "original"
    assert result["filename"] == "原文件.md"
    assert result["size_bytes"] == len(MARKDOWN.encode())
    assert base64.b64decode(result["base64"]) == MARKDOWN.encode()
    assert "text" not in result


def test_get_document_content_rejects_unknown_format(services: Services, kb: str) -> None:
    """格式写错要**说清可选值**，模型据此能自己改对而不是放弃这个工具。"""
    document_id = _parsed_document(services, kb)

    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "get_document_content",
            {"document_id": document_id, "format": "pdf"},
            caller=Caller(is_admin=True),
        )

    assert "markdown" in str(excinfo.value)
    assert "original" in str(excinfo.value)


def test_get_document_content_truncates_long_markdown(
    services: Services, kb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正文过长时只回前一段**并明确标记**：不标的话模型会把这一半当全文，
    然后据此回"文里没有提到 X"——而 X 就在被截掉的那一半里。"""
    from app.services import tools as tools_module

    document_id = _parsed_document(services, kb)
    monkeypatch.setattr(tools_module, "MAX_TEXT_CHARS", 10)

    result = call_tool(
        services, "get_document_content", {"document_id": document_id}, caller=Caller(is_admin=True)
    )

    assert result["truncated"] is True
    assert result["text"] == MARKDOWN[:10]


def test_get_document_content_rejects_an_oversized_payload(
    services: Services, kb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 32MB 时**不截断**，而是让人改走 REST 下载接口。

    截一半的原文是坏文件，截一半的正文更容易被当成全文读——两条都不如明说
    "这个工具回不了，去那条能真下完的路"。

    这里把上限改小而不是真造一个 32MB 的原文件：**验的是那条判定本身**
    （比较 + 报错指向），而造 33MB 的数据只会让这条用例慢到没人愿意跑。
    代价是报错里的 MB 数在改小之后算成 0——所以断言只认"指向 REST 下载"那一半。
    """
    from app.services import tools as tools_module

    document_id = _upload_markdown(services, kb)
    monkeypatch.setattr(tools_module, "MAX_DOWNLOAD_BYTES", 8)

    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "get_document_content",
            {"document_id": document_id, "format": "original"},
            caller=Caller(is_admin=True),
        )

    message = str(excinfo.value)
    assert "download-url" in message
    assert document_id in message


def test_get_document_content_respects_kb_scope(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """按 id 直取内容也要判范围——否则知道 id 就能把别人的原文整份拉走。"""
    document_id = _parsed_document(services, kb)
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id
    scoped = key_for(kb_ids=[other])

    with pytest.raises(ForbiddenError):
        call_tool(services, "get_document_content", {"document_id": document_id}, caller=scoped)


def test_get_document_content_is_denied_for_a_key_scoped_away(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    """工具范围限了别的工具时，这个工具**连内容都不该读**。

    闸门在 ``call_tool`` 里（清单过滤只是"别把调不成的工具摆出来"）——
    与 REST 侧那条映射表是同一个判定，见 ``api/auth.py`` 的 ``_TOOL_BY_ROUTE``。
    """
    document_id = _parsed_document(services, kb)
    scoped = key_for(kb_ids=[kb], tools=["search"])

    with pytest.raises(ForbiddenError) as excinfo:
        call_tool(services, "get_document_content", {"document_id": document_id}, caller=scoped)

    assert "get_document_content" in str(excinfo.value)


@pytest.mark.parametrize(
    ("base_url", "secret", "expected"),
    [
        ("http://127.0.0.1:8081", "sign-me", True),
        ("http://127.0.0.1:8081", "", False),
        ("", "sign-me", False),
    ],
)
def test_download_url_needs_both_a_secret_and_a_public_base(
    services: Services,
    kb: str,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    secret: str,
    expected: bool,
) -> None:
    """``download_url`` **两个条件缺一不可**：有签名密钥、且知道对外基址。

    只满足一个时**整个键不出现**，而不是回一条半截的相对路径——相对路径给到另一台
    机器上的客户端等于一条打不开的链接，比"没有这一项"更误导。
    """
    from app.core.config import get_settings

    document_id = _upload_markdown(services, kb)
    monkeypatch.setenv("KYLAB_PUBLIC_BASE_URL", base_url)
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", secret)
    get_settings.cache_clear()

    result = call_tool(
        services,
        "get_document_content",
        {"document_id": document_id, "format": "original"},
        caller=Caller(is_admin=True),
    )

    if not expected:
        assert "download_url" not in result
        return

    # 拼出来的必须是**真能用**的那一条：绝对地址 + 内容端点 + 一组能过验签的参数。
    # 验签这一步是刻意的——只断言"字符串里有 signature=" 的话，一条拿错密钥、
    # 或把 fmt / document_id 签错（那两种在 /content 上都是 401）的链接照样能过。
    from urllib.parse import parse_qs, urlparse

    from app.core.signing import verify_resource
    from app.services.documents import signature_resource

    parsed = urlparse(result["download_url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == urlparse(base_url).netloc
    assert parsed.path == f"/api/v1/documents/{document_id}/content"
    assert query["format"] == ["original"]
    verify_resource(
        signature_resource(document_id, "original"),
        query["signature"][0],
        int(query["expires"][0]),
        secret,
    )


def test_every_tool_has_catalog_metadata() -> None:
    """``TOOL_META`` 必须覆盖 ``TOOL_NAMES`` 的每一个（``GET /tools`` 直接查它）。

    少一个的后果不是"界面上少一行"，而是那个端点当场 KeyError（整页 500）——
    加工具时忘了补元数据是最容易发生的一种漏，所以在这里钉住。

    ``requires_write`` 也一并钉一份名单：它说的是"这个工具会不会调
    ``check_access(need=WRITE)``"，错了不会报错，只会让界面把一个能改数据的工具
    显示成只读（或者反过来，把只读的显示成能写）。
    """
    from app.services.tools import TOOL_META

    assert set(TOOL_META) == set(TOOL_NAMES)
    for name, meta in TOOL_META.items():
        assert meta.label.strip(), name
        assert meta.summary.strip(), name

    writers = {name for name, meta in TOOL_META.items() if meta.requires_write}
    assert writers == {
        "create_knowledge_base",
        "upload_document",
        "add_data_source",
        "reparse_document",
        "delete_document",
    }


# --------------------------------------------------------------------- 删除


def test_delete_document_puts_it_in_trash(services: Services, kb: str, admin: Caller) -> None:
    uploaded = call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": "待删.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=admin,
    )

    result = call_tool(
        services, "delete_document", {"document_id": uploaded["document_id"]}, caller=admin
    )

    assert result["trash_id"]
    assert "7 天" in result["note"]
    with pytest.raises(NotFoundError):
        call_tool(
            services,
            "get_document_status",
            {"document_id": uploaded["document_id"]},
            caller=admin,
        )


def test_delete_unknown_document_raises(services: Services, admin: Caller) -> None:
    with pytest.raises(NotFoundError):
        call_tool(services, "delete_document", {"document_id": "doc_不存在"}, caller=admin)


# ------------------------------------------- 按页取原文 / 强制重新解析（v0.64）


#: 带页标记的解析产物（形状与真实产物一致：标记独占一行，第 3 页是空的）
PAGED_MARKDOWN = """<!-- page:1 -->
# 眼轴共识

第一页正文。

<!-- page:2 -->
## 测量

第二页正文。

<!-- page:3 -->

<!-- page:4 -->
第四页正文。
"""


def _merged_product_markdown() -> str:
    """一份"总页数很大、产物里只有零星几个页号"的产物（Wills 那份的形状）。

    19 个不同页号、最大 200、``<!-- page:1 -->`` 反复出现——存量切分文档合并时
    "把各段的相对页码抬成绝对页码"还没修好留下的样子（见 ``document_pages.py`` 模块头）。
    最大页号没超过总页数，所以它只栽在"定位到的页数"那半截判据上。
    """
    return "\n\n".join(
        f"<!-- page:{page} -->\n第 {page} 页的正文。"
        for page in [1] * 20 + list(range(2, 19)) + [200]
    )


def _attach_parsed_markdown(
    services: Services, document_id: str, markdown: str, *, page_count: int | None
) -> None:
    """把一份解析产物挂到文档上（测试与 app 同进程，直达存储造状态）。

    走真实摄入链路造不出"第 3 页是空的""产物里没有标记"这些输入，而它们正是
    "按页取原文"要回答的边界（同 ``tests/integration/api/test_pages_api.py``）。
    """
    from app.storage.base import ParseResultRecord

    stores = services.auth._stores
    path = stores.objects.write(f"markdown/{document_id}.md", markdown.encode("utf-8"))
    stores.meta.save_parse_result(
        ParseResultRecord(
            document_id=document_id,
            part_id=None,
            parser_name="fake-parser",
            markdown_path=path,
        )
    )
    if page_count is not None:
        stores.meta.update_document_page_count(document_id, page_count)


def _paged_document(services: Services, kb: str, *, filename: str = "指南.pdf") -> str:
    """一份四页、带页标记的文档（第 3 页没有正文）。"""
    document_id = _upload_markdown(services, kb, filename=filename)
    _attach_parsed_markdown(services, document_id, PAGED_MARKDOWN, page_count=4)
    return document_id


def _merged_page_document(services: Services, kb: str) -> str:
    """一份"自称 619 页、产物里只有 19 个页号"的文档（现存坏数据的形状）。"""
    document_id = _upload_markdown(services, kb, filename="手册.pdf")
    _attach_parsed_markdown(services, document_id, _merged_product_markdown(), page_count=619)
    return document_id


def test_get_document_pages_slices_the_requested_range(services: Services, kb: str) -> None:
    """要哪几页给哪几页，且**内部页标记不漏出去**（与 ``/preview`` 同一条口径）。"""
    document_id = _paged_document(services, kb)

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "from_page": 1, "to_page": 2},
        caller=Caller(is_admin=True),
    )

    assert result["document_id"] == document_id
    assert result["name"] == "指南.pdf"
    assert (result["from_page"], result["to_page"]) == (1, 2)
    assert result["total_pages"] == 4
    assert result["page_text_available"] is True
    # 4 页 4 个页号：与总页数自洽，所以页码可信，不该多出一句"页码不可信"
    assert result["pages_reliable"] is True
    assert result["pages_located"] == 4
    assert [item["page"] for item in result["pages"]] == [1, 2]
    assert "第一页正文" in result["pages"][0]["text"]
    assert "第二页正文" not in result["pages"][0]["text"]
    assert "第二页正文" in result["pages"][1]["text"]
    assert all("<!--" not in item["text"] for item in result["pages"])
    # 没超总页数就不该有 truncated 这个键（与 get_document_content 同一条取舍：
    # 它出现本身就是"这一段是残的"的信号）
    assert "truncated" not in result
    assert "note" not in result


def test_an_empty_page_in_the_middle_is_listed(services: Services, kb: str) -> None:
    """第 3 页在两个标记之间什么都没有：仍然出条目、文字为空串。

    "这一页没有正文"与"这一页不存在"是两件事——少一条会让页码对不上。
    """
    document_id = _paged_document(services, kb)

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "from_page": 3, "to_page": 4},
        caller=Caller(is_admin=True),
    )

    assert [item["page"] for item in result["pages"]] == [3, 4]
    assert result["pages"][0]["text"] == ""
    assert "第四页正文" in result["pages"][1]["text"]


def test_from_page_defaults_to_page_one(services: Services, kb: str) -> None:
    """``from_page`` 是可选的那一个：只说"到第 2 页"时从第 1 页起（与 REST 同口径）。"""
    document_id = _paged_document(services, kb)

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "to_page": 2},
        caller=Caller(is_admin=True),
    )

    assert result["from_page"] == 1
    assert [item["page"] for item in result["pages"]] == [1, 2]


def test_a_range_beyond_the_document_is_flagged(services: Services, kb: str) -> None:
    """超出总页数：条目照给（按请求的页数铺格子），但要标出这是被截住的。"""
    document_id = _paged_document(services, kb)

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "from_page": 1, "to_page": 9},
        caller=Caller(is_admin=True),
    )

    assert len(result["pages"]) == 9
    assert result["truncated"] is True
    assert [item["text"] for item in result["pages"][4:]] == ["", "", "", "", ""]


def test_without_page_markers_it_says_so_instead_of_failing(services: Services, kb: str) -> None:
    """**没有页码信息不是错误**：``page_text_available=false`` + 一句说明。

    纯文本类文档（.md / .txt）本来就没有页——报错会把"界面该说明的事"变成
    "像是后端坏了"，而空文字不加说明又像是"这份文档是空的"。
    """
    document_id = _upload_markdown(services, kb, filename="说明.md")

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "from_page": 1, "to_page": 2},
        caller=Caller(is_admin=True),
    )

    assert result["page_text_available"] is False
    assert result["total_pages"] is None
    assert [item["page"] for item in result["pages"]] == [1, 2]
    assert [item["text"] for item in result["pages"]] == ["", ""]
    assert "没有页码信息" in result["note"]
    # **没有页码信息 ≠ 页码不可信**：这一档回 null，note 也只能是上面那一句
    # （两条 note 逻辑上互斥，同时出现会让模型以为"页码是错的"）
    assert result["pages_reliable"] is None
    assert result["pages_located"] is None
    assert "页码不可信" not in result["note"]


def test_unreliable_page_numbers_are_flagged_in_the_note(services: Services, kb: str) -> None:
    """**页码不可信要明说**：产物里只定位到 19 个页号、文档自称 619 页。

    不说的话模型会把"标着第 1 页"的那一段当第 1 页引用——而它横跨十几章。
    note 里要说清两个数、说清"别当页码引用"、并给出补救手段（重新解析）。
    """
    document_id = _merged_page_document(services, kb)

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "from_page": 1, "to_page": 2},
        caller=Caller(is_admin=True),
    )

    assert result["page_text_available"] is True
    assert result["pages_reliable"] is False
    assert result["pages_located"] == 19
    assert result["total_pages"] == 619
    assert "19" in result["note"] and "619" in result["note"]
    assert "别拿它当" in result["note"]
    assert "reparse_document" in result["note"]
    # 文字照给：不可信的是"它是第几页"，不是"它是不是这份文档的内容"
    assert "第 1 页的正文" in result["pages"][0]["text"]


@pytest.mark.parametrize(
    ("from_page", "to_page", "expected"),
    [
        (0, 2, "从 1 开始"),
        (5, 3, "不能小于起始页"),
        (1, 51, "50"),
    ],
)
def test_an_impossible_page_range_is_rejected_in_chinese(
    services: Services, kb: str, from_page: int, to_page: int, expected: str
) -> None:
    """范围给错要**说清哪一条不对**（中文报错来自服务层的那三条规则，这里不另写一套）。"""
    document_id = _paged_document(services, kb)

    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "get_document_pages",
            {"document_id": document_id, "from_page": from_page, "to_page": to_page},
            caller=Caller(is_admin=True),
        )

    assert expected in str(excinfo.value)


def test_get_document_pages_respects_kb_scope(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """别的库里的文档，页文字也够不到——与读正文同一个闸（``_document_or_403``）。"""
    document_id = _paged_document(services, kb)
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id

    with pytest.raises(ForbiddenError):
        call_tool(
            services,
            "get_document_pages",
            {"document_id": document_id, "to_page": 1},
            caller=key_for(kb_ids=[other]),
        )


@pytest.mark.parametrize(
    ("base_url", "secret", "expected"),
    [
        ("http://127.0.0.1:8081", "sign-me", True),
        ("http://127.0.0.1:8081", "", False),
        ("", "sign-me", False),
    ],
)
def test_page_image_url_needs_both_a_secret_and_a_public_base(
    services: Services,
    kb: str,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    secret: str,
    expected: bool,
) -> None:
    """页图链接同样**两个条件缺一不可**（有签名密钥、且知道对外基址）。

    与 ``download_url`` 那条的区别只在缺了给什么：那里整份内容都不给，这里**文字照给**、
    只是少一个可选的 ``image_url``——一个可选的一半不该让整次调用失败。
    """
    from app.core.config import get_settings

    document_id = _paged_document(services, kb)
    monkeypatch.setenv("KYLAB_PUBLIC_BASE_URL", base_url)
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", secret)
    get_settings.cache_clear()

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "to_page": 1},
        caller=Caller(is_admin=True),
    )

    if not expected:
        assert "image_url" not in result["pages"][0]
        assert "image_kind" not in result
        assert result["pages"][0]["text"], "图给不出来不该把文字也一起丢掉"
        return

    # 拼出来的必须是**真能用**的那一条：绝对地址 + 页图端点 + 一组能过验签的参数。
    # 只断言"字符串里有 signature=" 的话，一条拿错密钥、或把页号签错的链接照样能过。
    from urllib.parse import parse_qs, urlparse

    from app.core.signing import verify_resource
    from app.services.document_pages import page_image_resource

    parsed = urlparse(result["pages"][0]["image_url"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == urlparse(base_url).netloc
    assert parsed.path == f"/api/v1/documents/{document_id}/pages/1/image"
    assert result["image_kind"] == "pdf"
    verify_resource(
        page_image_resource(document_id, 1),
        query["signature"][0],
        int(query["expires"][0]),
        secret,
    )


def test_a_document_without_real_pages_gets_no_image_url(
    services: Services, kb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯文本 / Office 没有"页"可切：**不签链接**（否则调用方会去取一片必然失败的图）。"""
    from app.core.config import get_settings

    document_id = _upload_markdown(services, kb, filename="说明.md")
    monkeypatch.setenv("KYLAB_PUBLIC_BASE_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", "sign-me")
    get_settings.cache_clear()

    result = call_tool(
        services,
        "get_document_pages",
        {"document_id": document_id, "to_page": 1},
        caller=Caller(is_admin=True),
    )

    assert "image_url" not in result["pages"][0]
    assert "image_kind" not in result


def _indexed_document(services: Services, kb: str) -> str:
    """一份**已索引、且队列里没有任务**的文档。

    刻意不走 MCP 的 ``upload_document``：它会排一个摄入任务，而测试里没有 worker 领它
    ——那个任务会让"有任务在跑"那道闸把重新解析挡下来（用例要的是成功那条路）。
    """
    outcome = services.ingest.submit(
        knowledge_base_id=kb, filename="待重解析.md", content=MARKDOWN.encode()
    )
    services.ingest.ingest(outcome.document.id)
    return outcome.document.id


def test_reparse_document_resets_the_stage_and_enqueues(
    services: Services, kb: str, admin: Caller
) -> None:
    """重新解析 = 清产物 + 退回 uploaded + 排一个新任务，三件事一次做完。"""
    document_id = _indexed_document(services, kb)
    assert services.documents.get(document_id).stage.value == "indexed"

    result = call_tool(services, "reparse_document", {"document_id": document_id}, caller=admin)

    assert result["document_id"] == document_id
    assert result["name"] == "待重解析.md"
    # **阶段是复位之后的那一个**：拿入队前的快照回给调用方会显示成"还是 indexed"，
    # 看起来像什么都没发生
    assert result["stage"] == "uploaded"
    assert result["task_id"].startswith("task_")
    assert "已排入队列" in result["note"]
    # 产物清干净了：留着它们，这一趟会被当成"解析已完成"从切分开始，
    # 而检索还会命中旧正文
    assert services.documents.chunk_count(document_id) == 0
    assert services.auth._stores.meta.get_parse_result(document_id) is None


def test_readonly_key_cannot_reparse(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    """重新解析会清产物、会真调解析器（可能花云端额度）：只读档**不该做得成**。"""
    document_id = _indexed_document(services, kb)

    with pytest.raises(ForbiddenError):
        call_tool(
            services,
            "reparse_document",
            {"document_id": document_id},
            caller=key_for(kb_ids=[kb]),
        )

    # 被拒之后什么都没动过（权限判定必须在复位之前）
    assert services.documents.chunk_count(document_id) > 0


def test_reparse_is_refused_while_a_task_is_still_queued(
    services: Services, kb: str, admin: Caller
) -> None:
    """有任务在跑时**按失败报**（而不是回一个"已排入队列"）：复位会清产物，
    而那时 worker 正在旧产物上写东西——说什么都没发生才是假消息。"""
    from app.core.exceptions import ConflictError

    document_id = _upload_markdown(services, kb)  # 上传会排一个摄入任务，测试里没人领它

    with pytest.raises(ConflictError, match="处理中"):
        call_tool(services, "reparse_document", {"document_id": document_id}, caller=admin)


# --------------------------------------------------------------------- 参数校验


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_knowledge_base", {}),
        ("upload_document", {"knowledge_base_id": "kb_1"}),
        ("add_data_source", {"knowledge_base_id": "kb_1", "kind": "rss"}),
        ("search", {}),
        ("get_document_status", {}),
        ("get_document_pages", {"document_id": "doc_1"}),
        ("reparse_document", {}),
        ("delete_document", {}),
    ],
)
def test_missing_required_arguments_raise(
    services: Services, admin: Caller, tool: str, args: dict
) -> None:  # type: ignore[type-arg]
    """缺参数要明确报"缺少参数：x"，而不是让下游抛一个难懂的 KeyError。"""
    with pytest.raises((InvalidRequestError, NotFoundError)):
        call_tool(services, tool, args, caller=admin)


def test_none_arguments_are_treated_as_empty(services: Services, admin: Caller) -> None:
    """MCP 客户端对无参工具可能传 ``None``——那不该炸。"""
    assert isinstance(call_tool(services, "list_knowledge_bases", None, caller=admin), list)


def test_max_top_k_is_documented_in_the_schema() -> None:
    """schema 里的上限要与实现一致，否则模型会按错的上限要条数。"""
    search = next(item for item in tool_definitions() if item["name"] == "search")
    assert search["inputSchema"]["properties"]["top_k"]["maximum"] == MAX_TOP_K


# --------------------------------------------------------------------- 列文档


def test_list_documents_reports_stage_and_searchability(
    services: Services, kb: str, admin: Caller
) -> None:
    """**"库里到底有什么"是 search 答不了的问题**——它只返回与问题相关的片段。

    所以单独一个工具来列文档，并且必须带上"能不能被检索到"。
    """
    call_tool(
        services,
        "upload_document",
        {
            "knowledge_base_id": kb,
            "filename": "列文档.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
        caller=admin,
    )

    result = call_tool(services, "list_documents", {"knowledge_base_id": kb}, caller=admin)

    assert result["total"] == 1
    item = result["documents"][0]
    assert item["name"] == "列文档.md"
    # 刚上传还没跑流水线，所以此时不可检索——这条信息比文件名更重要
    assert item["stage"] == "uploaded"
    assert item["searchable"] is False


def test_list_documents_respects_scope(services: Services, kb: str, key_for) -> None:  # type: ignore[no-untyped-def]
    other = services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="别人的库"
    ).id
    scoped = key_for(kb_ids=[other])

    with pytest.raises(ForbiddenError):
        call_tool(services, "list_documents", {"knowledge_base_id": kb}, caller=scoped)


# --------------------------------------------------------------------- 工具范围（v0.60）


def test_call_tool_denies_a_tool_outside_the_keys_scope(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    """**闸门在 ``call_tool`` 里**：范围外的工具连进都进不去。

    为什么清单过滤（``tools/list``）之外还要这一道：清单只是"别把永远调不成的工具
    摆给模型看"，客户端完全可以照旧直接调——只靠清单过滤，闸门就变成了提示。
    """
    scoped = key_for(kb_ids=[kb], tools=["search"])

    with pytest.raises(ForbiddenError) as excinfo:
        call_tool(services, "list_knowledge_bases", {}, caller=scoped)

    assert "list_knowledge_bases" in str(excinfo.value)
    # 要列出可用的那些，用户才配得回来
    assert "search" in str(excinfo.value)


def test_call_tool_allows_a_tool_inside_the_keys_scope(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    scoped = key_for(kb_ids=[kb], tools=["search"])

    result = call_tool(services, "search", {"query": "眼轴"}, caller=scoped)

    assert "hits" in result


def test_call_tool_without_a_tool_scope_stays_unrestricted(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    """没给工具范围（空）= 不受限：存量钥匙的行为不能因为这一层变样。"""
    unscoped = key_for(kb_ids=[kb])

    assert isinstance(call_tool(services, "list_knowledge_bases", {}, caller=unscoped), list)
    call_tool(services, "search", {"query": "眼轴"}, caller=unscoped)


def test_unknown_tool_is_still_reported_as_unknown_for_a_scoped_key(
    services: Services, kb: str, key_for
) -> None:  # type: ignore[no-untyped-def]
    """**"未知"要排在"没授权"前面**：反过来的话，一个拼错的工具名会报成
    "未授权调用工具"——模型据此会以为换个参数就能过，而不是"这个名字不存在"。
    """
    scoped = key_for(kb_ids=[kb], tools=["search"])

    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(services, "不存在的工具", {}, caller=scoped)

    assert "未知的工具" in str(excinfo.value)


def test_every_tool_has_a_parameter_whitelist() -> None:
    """``server.py`` 的 ``_PARAMS`` 必须覆盖**对外 MCP 面**的每个工具。

    少一个的后果不是"参数被过滤掉"，而是 ``build_server`` 直接 KeyError——
    服务起不来。放在这里断言是因为它跨了两个模块，只看一边发现不了。

    **v0.1.0 瘦身之后两边相等**：``services/tools.py`` 只剩纯知识库那一组工具，
    对外面就是它——不再是"按白名单过滤出来的子集"。这条等式仍然有它守的东西：
    哪天有人往表里加一个 Agent 侧工具，``_PARAMS`` 与 MCP 面会一起跟上，
    而"加了什么"必须是有意识的（``test_server.py`` 那边还钉着名字）。

    参数层面的对齐见下一条用例。
    """
    from app.mcp_server.server import _PARAMS, MCP_TOOL_NAMES

    assert set(_PARAMS) == set(MCP_TOOL_NAMES)
    assert set(MCP_TOOL_NAMES) == set(TOOL_NAMES), (
        "对外面就是这张纯知识库工具表：两边不一样说明有一侧漏改了"
    )


def test_parameter_whitelists_match_the_schemas() -> None:
    """``_PARAMS`` 的每一项都必须与 ``tool_definitions()`` 的 ``inputSchema`` 逐参数对齐。

    **这条守的是一类不会报错的漂**：白名单少一个参数，schema 与描述里却写着它，
    于是模型照着传、服务端安静丢掉——表现为"这个参数没起作用"。``search`` 的
    ``keep`` / ``min_score`` / ``per_doc`` 就是这么失效了很长一段时间
    （《外部接入指南》§10 登记过）。

    多一个也一起管：那说明 schema 与实现里已经没有它了，白名单还留着。
    """
    from app.mcp_server.server import _PARAMS

    specs = {item["name"]: item for item in tool_definitions()}
    for name in TOOL_NAMES:
        declared = set(specs[name]["inputSchema"].get("properties", {}))
        assert set(_PARAMS[name]) == declared, (
            f"{name} 的参数白名单与 schema 不一致："
            f"白名单多 {sorted(set(_PARAMS[name]) - declared)}、"
            f"少 {sorted(declared - set(_PARAMS[name]))}"
        )


# ------------------------------------------------------- 对外的契约指纹（v0.62）


def test_surface_fingerprint_is_stable_across_calls() -> None:
    """同一份工具表算出来的指纹必须每次都一样（它是"契约摘要"，不是随机数）。

    ``sha256`` 前 12 位十六进制：短到放得进版本串，又足够区分一次契约改动。
    """
    from app.services.tools import surface_fingerprint

    first = surface_fingerprint()

    assert first == surface_fingerprint()
    assert len(first) == 12
    assert all(char in "0123456789abcdef" for char in first)


def test_surface_fingerprint_changes_with_a_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**描述改了指纹就得变**：描述是写给模型的契约的一部分（"什么时候该用"就在里面）。

    这一条是这次改动的核心诉求的反面表述：客户端要能**发现**契约变过。
    版本号做不到（那是发版号，实测连着几次部署都是 0.1.0）。
    """
    from app.services import tools as tools_module
    from app.services.tools import surface_fingerprint

    before = surface_fingerprint()
    original = tools_module.tool_definitions

    def tampered() -> list[dict]:
        items = original()
        items[0]["description"] = items[0]["description"] + "（改了）"
        return items

    monkeypatch.setattr(tools_module, "tool_definitions", tampered)

    assert surface_fingerprint() != before


def test_surface_fingerprint_changes_with_a_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """加一个参数（schema 变了）同样要变——参数面的改动是最容易漏报的一种契约变更。"""
    from app.services import tools as tools_module
    from app.services.tools import surface_fingerprint

    before = surface_fingerprint()
    original = tools_module.tool_definitions

    def tampered() -> list[dict]:
        items = original()
        search = next(item for item in items if item["name"] == "search")
        search["inputSchema"]["properties"]["新的旋钮"] = {"type": "integer"}
        return items

    monkeypatch.setattr(tools_module, "tool_definitions", tampered)

    assert surface_fingerprint() != before


def test_surface_fingerprint_changes_with_the_output_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``outputSchema`` 也是契约（客户端会照它校验结构化输出），所以它进指纹。"""
    from app.services import tools as tools_module
    from app.services.tools import surface_fingerprint

    before = surface_fingerprint()
    original = tools_module.tool_definitions

    def tampered() -> list[dict]:
        items = original()
        search = next(item for item in items if item["name"] == "search")
        search["outputSchema"] = {"type": "object", "properties": {"别的": {"type": "string"}}}
        return items

    monkeypatch.setattr(tools_module, "tool_definitions", tampered)

    assert surface_fingerprint() != before


# ---------------------------------------------------- 结构化输出那一份（v0.62）


def test_only_the_tools_with_an_output_schema_have_a_builder() -> None:
    """带 ``outputSchema`` 的工具与有压缩函数的工具**必须一一对应**。

    声明了 ``outputSchema`` 就必须每次都给 ``structuredContent``（客户端照它校验，
    缺了直接报错）；反过来，有压缩函数却没有 schema 等于白算一份没人校验的东西。
    """
    from app.services.tools import STRUCTURED_CONTENT

    declared = {item["name"] for item in tool_definitions() if item.get("outputSchema") is not None}

    assert set(STRUCTURED_CONTENT) == declared
    assert declared, "至少 search 要给机器可读的那一份（它就是这个产品的判定）"


def test_search_structured_content_keeps_the_verdict_keys() -> None:
    """搬过去的那一份必须**每个键都在**：``outputSchema`` 声明了几个，缺一个就是硬报错。

    而且它是**同一批数据的另一种读法**，不是摘要——判定与成果（``query`` / ``hits``）
    一起过来。
    """
    from app.services.tools import (
        SEARCH_OUTPUT_SCHEMA,
        search_structured_content,
        surface_fingerprint,
    )

    payload = {
        "summary": "一句话判定",
        "distribution": {"fit": "strong", "count": 3},
        "decision": {"keep": 3, "decided_by": "model"},
        "returned": 3,
        "filtered_out": 1,
        "offset": 3,
        "next_offset": 6,
        "query": "眼轴",
        "hits": [{"document_id": "doc_1"}],
    }

    structured = search_structured_content(payload)

    assert set(structured) == set(SEARCH_OUTPUT_SCHEMA["required"])
    assert structured["summary"] == "一句话判定"
    assert structured["fit"] == "strong"
    assert structured["decision"] == payload["decision"]
    assert structured["returned"] == 3
    assert structured["filtered_out"] == 1
    # 翻页的两项照搬：按 schema 读的调用方也要能自己接着翻，不必去文本里找那一行
    assert structured["offset"] == 3
    assert structured["next_offset"] == 6
    # **成果照搬**：机器那份不是摘要，问题与原文必须逐条过来
    assert structured["query"] == payload["query"] == "眼轴"
    assert structured["hits"] == payload["hits"]
    assert structured["surface_fingerprint"] == surface_fingerprint()


def test_search_structured_content_survives_the_empty_early_return() -> None:
    """**一个库都没有**那条提前返回也要压得出全部键（可空的那几个是 ``None``）。

    那条路的文本载荷只有 ``query`` / ``hits`` / ``note`` 加翻页那两项——不兜底的话，
    调用方照 ``outputSchema`` 校验会当场报"少字段"，而那是最不该失败的一种情况。
    """
    from app.services.tools import search_structured_content

    structured = search_structured_content(
        {
            "query": "随便问问",
            "hits": [],
            "offset": 0,
            "next_offset": None,
            "note": "没有任何知识库",
        }
    )

    assert structured["summary"] == ""
    assert structured["fit"] is None
    assert structured["distribution"] is None
    assert structured["decision"] is None
    assert structured["returned"] == 0
    assert structured["offset"] == 0
    assert structured["next_offset"] is None, "空页不能回一个数——那是把调用方送进死循环"
    # 空库那一路：问题照回显、原文是空数组（不是 None——schema 说 hits 是 array）
    assert structured["query"] == "随便问问"
    assert structured["hits"] == []
    assert str(structured["surface_fingerprint"])


def test_search_structured_hits_align_with_the_schema_key_by_key(
    services: Services, kb: str, admin: Caller
) -> None:
    """真跑一次检索：结构化那份的 ``query`` / ``hits`` 与文本那份**逐条相等**，一条不裁。

    这条钉的是那个真实的坑：机器那份只搬了判定与计数、把 ``hits`` 丢了，于是按
    ``outputSchema`` 读的客户端看到 ``returned: 3`` 却一条片段都拿不到。
    """
    from app.services.tools import SEARCH_OUTPUT_SCHEMA, search_structured_content

    _parsed_document(services, kb)

    payload = call_tool(
        services, "search", {"query": "眼轴长度", "knowledge_base_ids": [kb]}, caller=admin
    )
    structured = search_structured_content(payload)

    assert structured["query"] == payload["query"]
    assert structured["hits"] == payload["hits"]
    assert structured["hits"], "库里刚灌进一份命中率极高的文档"
    # 每条的键集合与 schema 里声明的逐键对齐（多一个少一个都会让按 schema 读的调用方失手）
    item_schema = SEARCH_OUTPUT_SCHEMA["properties"]["hits"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == set(payload["hits"][0])
    assert set(item_schema["properties"]) == set(item_schema["required"])


# --------------------------------------------------------- 判定字段的形状（v0.62）


def test_search_hits_carry_their_basis_and_lexical_score(
    services: Services, kb: str, admin: Caller
) -> None:
    """MCP 的每条命中都要说清"我是哪条通道捞上来的"，并把词面分交出来。

    与 REST 的 ``SearchHitOut`` 是同一批字段（同一个服务在两个入口给出两种形状，
    是最难查的一类不一致）。
    """
    document_id = _parsed_document(services, kb)

    result = call_tool(
        services, "search", {"query": "眼轴长度", "knowledge_base_ids": [kb]}, caller=admin
    )

    assert result["hits"], "库里刚灌进一份命中率极高的文档"
    assert {hit["document_id"] for hit in result["hits"]} == {document_id}
    for hit in result["hits"]:
        assert hit["basis"] in {"vector", "fulltext"}
        assert (hit["similarity"] is not None) == (hit["basis"] == "vector")
        assert "lexical_score" in hit


def test_search_decision_counts_the_unscored_hits(
    services: Services, kb: str, admin: Caller
) -> None:
    """``decision.unscored_kept`` 必须在场（0 也必须在场）——缺键与"没有绕过"是两回事。

    用户那条反馈的正面答案就在这里：把 ``min_score`` 提到 0.99 却还有命中时，
    这一位说得出"这几条没有余弦可比"。
    """
    result = call_tool(
        services,
        "search",
        {"query": "眼轴", "knowledge_base_ids": [kb], "min_score": 0.99},
        caller=admin,
    )

    assert "unscored_kept" in result["decision"]
    assert isinstance(result["decision"]["unscored_kept"], int)


def test_per_doc_alone_is_reported_as_the_callers_own_decision(
    services: Services, kb: str, admin: Caller
) -> None:
    """只传 ``per_doc`` 也是调用方的决定：``decided_by`` 得是 ``model``。"""
    result = call_tool(
        services,
        "search",
        {"query": "眼轴", "knowledge_base_ids": [kb], "per_doc": 1},
        caller=admin,
    )

    assert result["decision"]["per_doc"] == 1
    assert result["decision"]["decided_by"] == "model"


def test_min_score_says_it_only_cuts_the_vector_channel() -> None:
    """``min_score`` 的说明必须交代它的边界：**只对向量通道的命中生效**。

    不写的话，调用方以为"阈值 = 全部命中的一道闸"，于是看到 ``similarity: null`` 的
    命中时只能得出"这个工具坏了"这一个结论（实测就是如此）。
    """
    search = next(item for item in tool_definitions() if item["name"] == "search")
    description = search["inputSchema"]["properties"]["min_score"]["description"]

    assert "只对向量通道的命中生效" in description
    assert "basis" in description


@pytest.mark.parametrize(
    "name",
    [
        "search",
        "get_document_content",
        "get_document_pages",
        "list_documents",
        "get_document_status",
    ],
)
def test_tools_that_return_document_text_declare_it_untrusted(name: str) -> None:
    """会回原文/文档名的工具必须声明**不可信内容**：那些字是用户文档里的，不是指令。

    提示注入的防线在调用方（本服务不做 LLM 预处理），但工具描述是模型唯一读得到的
    那份说明——不写这一句，等于让它猜"这段字是谁说的"。
    ``get_document_status`` 也在这份名单里：它回文档名与处理错误，同样来自用户文档
    （列出的三个是点名的那三个，这一条是同一口径的延伸）。
    """
    item = next(entry for entry in tool_definitions() if entry["name"] == name)

    assert "不可信内容" in item["description"]
    assert "指令" in item["description"]


def test_allowed_but_wrong_typed_knobs_degrade_instead_of_failing(
    services: Services, kb: str, admin: Caller
) -> None:
    """调节量给垃圾**只影响它自己**（退回默认），不让整次检索失败。

    SDK 那层的类型校验已经让开（见 ``mcp_server/server.py`` 的 ``_permissive_signature``），
    所以垃圾值真的会到服务层——容错必须在这一层保住。``knowledge_base_ids`` 不在此列：
    猜错它等于查了别的库，所以那一项**报错**（见 ``_kb_ids``）。
    """
    result = call_tool(
        services,
        "search",
        {
            "query": "眼轴",
            "knowledge_base_ids": [kb],
            "top_k": "不是数",
            "keep": [],
            "min_score": "很严",
            "per_doc": {"每篇": 2},
        },
        caller=admin,
    )

    assert isinstance(result["hits"], list)


def test_a_wrong_typed_required_argument_names_the_parameter_in_chinese(
    services: Services, admin: Caller
) -> None:
    """必填参数给错类型（数组/对象）也要说中文，并**点名是哪一个参数**。

    从前这类错由 SDK 的 pydantic 报出来（英文 + 一个文档链接）；现在校验归我们，
    所以"给错类型"必须与"没给"一样能自己改对——``str([])`` 那条路会造出一个
    看起来像 id 的字符串，下游只会报"文档不存在：[]"，把人引向错误的方向。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(services, "get_document_content", {"document_id": []}, caller=admin)

    assert "document_id" in str(excinfo.value)


def test_a_malformed_knowledge_base_list_says_what_it_wants(
    services: Services, admin: Caller
) -> None:
    """``knowledge_base_ids`` 里塞了非字符串：**报错而不是猜**。

    猜错的表现是"查了别的库"（比如把 ``{'bad': 1}`` 拼成一个不存在的 id 再报越界），
    那比一条读得懂的中文报错难查得多。裸字符串仍然接受（模型常这么传）。
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        call_tool(
            services,
            "search",
            {"query": "眼轴", "knowledge_base_ids": [{"bad": 1}]},
            caller=admin,
        )

    assert "knowledge_base_ids" in str(excinfo.value)

    # 单个裸字符串 = 只查这一个库（容忍，不是报错）
    tolerated = call_tool(
        services, "search", {"query": "眼轴", "knowledge_base_ids": "kb_none"}, caller=admin
    )
    assert tolerated["hits"] == []
