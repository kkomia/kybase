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
        ("http://192.168.31.18:8081", "sign-me", True),
        ("http://192.168.31.18:8081", "", False),
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


# --------------------------------------------------------------------- 参数校验


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_knowledge_base", {}),
        ("upload_document", {"knowledge_base_id": "kb_1"}),
        ("add_data_source", {"knowledge_base_id": "kb_1", "kind": "rss"}),
        ("search", {}),
        ("get_document_status", {}),
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

