"""MCP 服务装配（T4.8）。

镜像同构：``app/mcp/server.py`` → 本文件。

**这里只测装配，不测传输**：stdio 与 HTTP 是 SDK 的事。
但有一类**只有装配期才会暴露**的问题必须钉住——定义顺序。

**工具清单收窄**（v0.60）也在这里：它落在 ``MCPServer.list_tools`` 这个装配面上，
而那条链（凭据 → 主体 → 过滤）只有走真实的 ``CallerMiddleware`` 才验得完整。
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
import uuid
from pathlib import Path

import pytest

from app.mcp_server.auth import MCP_KEY_ENV
from app.mcp_server.server import MCP_TOOL_NAMES, build_server, main

SERVER_PATH = Path(inspect.getfile(build_server))

#: 一份短正文：协议层要真灌一份进去，检索才有命中可回。
MARKDOWN = "# 眼轴\n\n眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一，不受调节能力影响。\n"


#: 对外面就应该是这些（v0.1.0 瘦身后的**名字清单**）。
#:
#: 写死一份是刻意的：上面那条说的是"注册的 == `MCP_TOOL_NAMES`"，
#: 而 `MCP_TOOL_NAMES` 现在直接取 `TOOL_NAMES`——只靠它的话，往 `tools.py`
#: 里加一个 Agent 侧工具会**一路绿着**通过。这一份就是那个刹车：
#: 变工具面必须是有意识的动作，而不是顺手加一个函数。
KB_TOOL_NAMES = (
    "list_knowledge_bases",
    "create_knowledge_base",
    "upload_document",
    "add_data_source",
    "search",
    "list_documents",
    "get_document_status",
    "get_document_content",
    "get_document_pages",
    "reparse_document",
    "delete_document",
)


def test_server_registers_exactly_the_kb_face() -> None:
    """挂上去的**正好是纯知识库那一组工具**（v0.1.0 瘦身收口）。

    两个方向都查：注册的那些必须是 ``MCP_TOOL_NAMES``，而 ``MCP_TOOL_NAMES``
    （= ``TOOL_NAMES``）必须**正好等于**下面那份名字清单——后半句是刹车，
    见 ``KB_TOOL_NAMES`` 的说明。
    """
    server = build_server()

    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == set(MCP_TOOL_NAMES)
    assert set(MCP_TOOL_NAMES) == set(KB_TOOL_NAMES)


def test_an_agent_tool_is_nowhere_in_the_face() -> None:
    """**Agent 侧的工具一个都不在**（架构设计 §6 阶段 1 第 5 步）：
    ``create_note`` 会真写笔记、``remember`` / ``forget`` 会真动用户档案、
    ``export_*`` 会把文件落进别人的会话产物区——它们的实现都已拆除，
    名字也不该出现在这个服务里。
    """
    server = build_server()

    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert not (names & {"create_note", "attach_note_to_kb", "ingest_artifact", "export_file"})
    assert not (names & {"recall", "remember", "forget", "web_search", "web_fetch"})


def test_list_tools_prints_the_registered_face(capsys) -> None:  # type: ignore[no-untyped-def]
    """``--list-tools`` 打的就是**要挂上去的那批**（一屏能核对的排障入口）。

    它原先打的是 ``TOOL_NAMES``（合体期 21 个）——那种"打出来的比挂上去的多"
    会让排障的人以为服务漏挂了工具。

    **最后一行是契约指纹**（v0.62）：那才是"工具面有没有变过"的那个数，
    排障时与名字清单一起看——名字数对得上、指纹对不上 = 这次部署改了描述或 schema。
    """
    from app.services.tools import surface_fingerprint

    assert main(["--list-tools"]) == 0

    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    *names, fingerprint = printed
    assert names == list(MCP_TOOL_NAMES) == list(KB_TOOL_NAMES)
    assert fingerprint == f"surface_fingerprint: {surface_fingerprint()}"


def test_every_tool_has_a_description() -> None:
    """描述是给模型读的——没有它，模型只能靠名字猜该不该用这个工具。"""
    server = build_server()

    for tool in server._tool_manager.list_tools():
        assert tool.description and tool.description.strip(), tool.name


def test_tool_parameters_are_derived_from_the_schema() -> None:
    """**参数必须真的出现在签名里**，而广告出去的是工具表里的那一份 schema。

    早先的实现只写 ``**kwargs``，而 SDK 从函数签名推导参数 schema——
    于是它推导出一个名为 ``kwargs`` 的必填字段，调用时报
    ``1 validation error for list_knowledge_basesArguments / kwargs Field required``，
    工具根本调不动。当时靠"签名是按 schema 造的"来防，现在签名刻意是"什么都能收"
    （校验归我们自己，见 ``_permissive_signature``），所以防的东西换成两条等式：

    1. **签名里的参数名 == 广告出去的参数名**——少一个的话 SDK 的参数模型里没有它，
       调用方传了会被安静丢掉（"这个参数没起作用"就是这么来的）；
    2. **广告出去的 schema == ``tool_definitions()`` 的那一份**——签名推出来的那份
       没有 ``required``、没有类型、没有枚举，绝不能拿给客户端看。
    """
    from app.services.tools import tool_definitions

    server = build_server()
    specs = {item["name"]: item for item in tool_definitions()}

    for tool in server._tool_manager.list_tools():
        advertised = tool.parameters["properties"]
        signature = inspect.signature(tool.fn).parameters
        assert [
            name for name, param in signature.items() if param.kind is not param.VAR_KEYWORD
        ] == list(advertised), tool.name
        assert tool.parameters == specs[tool.name]["inputSchema"], tool.name
        assert tool.output_schema == specs[tool.name].get("outputSchema"), tool.name


def test_document_content_tool_has_its_parameter_whitelist() -> None:
    """新工具 ``get_document_content``（v0.61）：挂上了、参数也进了白名单。

    **``format`` 必须在 ``_PARAMS`` 里**：那个白名单决定"哪些参数会被原样转给服务层"
    （见 ``make_handler``），漏一个的后果不是报错，而是**参数被安静丢掉**——
    工具会一直按默认格式（Markdown）回内容，看起来能用，只是永远给不出原文。
    """
    from app.mcp_server.server import _PARAMS

    server = build_server()

    tool = next(
        tool for tool in server._tool_manager.list_tools() if tool.name == "get_document_content"
    )
    assert set(tool.parameters.get("properties", {})) == {"document_id", "format"}
    assert tool.parameters.get("required") == ["document_id"]
    assert set(_PARAMS["get_document_content"]) == {"document_id", "format"}


def test_no_tool_exposes_a_kwargs_field() -> None:
    """``kwargs`` 出现在参数里就是上面那个 bug 复发。"""
    server = build_server()

    for tool in server._tool_manager.list_tools():
        assert "kwargs" not in tool.parameters.get("properties", {}), tool.name


def test_required_parameters_have_no_default() -> None:
    """必填参数在**广告出去的 schema** 里必须无默认值。

    这是文档里那句"客户端据此知道哪些必须传"的落点：它要求必填参数在
    ``required`` 里。签名那一侧的默认值（见 ``_permissive_signature``）**不参与广告**
    ——挂载之后由 ``pin_contract`` 换回工具表里的那一份，所以这里读的是工具表。
    """
    server = build_server()

    upload = next(
        tool
        for tool in server._tool_manager.list_tools()
        if tool.name == "upload_document"
    )
    assert set(upload.parameters.get("required", [])) == {
        "knowledge_base_id",
        "filename",
        "content_base64",
    }


def test_module_helpers_are_defined_before_the_main_guard() -> None:
    """**助手函数必须在 ``if __name__ == "__main__":`` 之前。**

    这条听起来像洁癖，但它是实测踩出来的：助手原先放在文件末尾、
    而 ``__main__`` 守卫在它们前面。用 ``import`` 时一切正常
    （模块会完整执行），可一用 ``python -m`` 就炸——
    模块以 ``__main__`` 身份从头执行，走到守卫就调 ``main()``，
    那时助手**还没被定义**，报 ``NameError``。

    这个不对称很难查：测试用 import 全过，只有真起服务才失败。
    所以用 AST 静态验一次顺序，别再让它复发。
    """
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))

    top_level_names: list[str] = []
    guard_index: int | None = None
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_level_names.append(node.name)
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "__name__" in test and "__main__" in test:
                guard_index = index

    assert guard_index is not None, "没找到 __main__ 守卫"

    before_guard = set(top_level_names)
    # 守卫之后不应再有顶层函数定义
    after = [
        node.name
        for node in tree.body[guard_index + 1 :]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert not after, f"这些函数定义在 __main__ 守卫之后，python -m 时会 NameError：{after}"
    assert {"build_server", "main", "_permissive_signature"} <= before_guard


def test_build_server_is_idempotent() -> None:
    """连造两次不能因为"工具重名"报错——测试与真实启动都可能各造一次。"""
    first = build_server()
    second = build_server()

    assert {t.name for t in first._tool_manager.list_tools()} == {
        t.name for t in second._tool_manager.list_tools()
    }


def test_instructions_mention_the_core_capability() -> None:
    """instructions 是给客户端模型的总纲：要说清"这服务是干什么的"。"""
    server = build_server()

    assert "检索" in server.instructions


# --------------------------------------------------------------------- 工具清单收窄（v0.60）


class _Ctx:
    """最小 ctx 替身：``CallerMiddleware`` 只从 ``ctx.headers``（或 ``ctx.request``）取凭据。"""

    def __init__(self, headers: dict[str, str] | None) -> None:
        self.headers = headers
        self.request = None


async def _list_tools_as(services, token: str):  # type: ignore[no-untyped-def]
    """走**真实的** ``CallerMiddleware`` 拿一次 ``tools/list`` 的结果。

    **不直接往 ContextVar 里塞 Caller**：正是"凭据 → 主体 → 过滤"这一段要验，
    塞进去就把它跳过去了（那等于只测了一半）。
    """
    from app.mcp_server.auth import CallerMiddleware

    server = build_server()
    middleware = CallerMiddleware(services)

    async def call_next(_ctx: object) -> object:
        return await server.list_tools()

    return await middleware(_Ctx({"Authorization": f"Bearer {token}"}), call_next)


@pytest.mark.asyncio
async def test_list_tools_hides_the_tools_outside_the_keys_scope() -> None:
    """限了工具范围的钥匙，``tools/list`` 只看得到它调得动的那些。

    **列出来再拒**的坏处不是安全，是可用性：模型会盯着一个永远调不成的工具反复试，
    而它没有任何线索知道为什么——回的是"未授权调用工具"还是"工具不存在"，
    对它来说都是同一句读不懂的话。
    """
    from app.core.services import get_services
    from app.models.enums import ApiKeyPermission

    services = get_services()
    issued = services.api_keys.create(
        name="只给检索的钥匙",
        permission=ApiKeyPermission.READONLY,
        allowed_tool_names=["search", "list_documents"],
    )

    names = [tool.name for tool in await _list_tools_as(services, issued.token)]

    # 顺序跟着注册顺序（这里不写死工具个数：工具表会长，见模块头）
    assert names == [name for name in MCP_TOOL_NAMES if name in {"search", "list_documents"}]


@pytest.mark.asyncio
async def test_list_tools_is_not_narrowed_for_an_unrestricted_key() -> None:
    """空工具范围 = 不受限：清单与从前一字不差（存量钥匙不受这一层影响）。"""
    from app.core.services import get_services
    from app.models.enums import ApiKeyPermission

    services = get_services()
    issued = services.api_keys.create(
        name="不限制的钥匙", permission=ApiKeyPermission.READONLY
    )

    names = [tool.name for tool in await _list_tools_as(services, issued.token)]

    assert set(names) == set(MCP_TOOL_NAMES)


@pytest.mark.asyncio
async def test_list_tools_without_credentials_still_lists_everything() -> None:
    """**没有凭据时列全**：``tools/list`` 是握手面，客户端还没配 Key 时也要连得上、
    看得见有哪些工具——排障全靠它（真正的闸门在 ``call_tool`` 里）。
    """
    server = build_server()

    names = [tool.name for tool in await server.list_tools()]

    assert set(names) == set(MCP_TOOL_NAMES)


# ------------------------------------------ 协议层：失败要看得出来、参数报错要是中文（v0.62）


@pytest.fixture
def services():  # type: ignore[no-untyped-def]
    """组合根（与 ``tests/unit/services/test_tools.py`` 同名同义）。

    协议层这些用例要**真发一把 Key**、真建一个库——手搓对象测不出"凭据 → 主体 → 工具"
    这条链，而那些毛病恰好都长在这条链上。
    """
    from app.core.services import get_services

    return get_services()


@pytest.fixture
def as_key(services, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """发一把**真 Key** 并把它塞进环境变量（``MCP_KEY_ENV``），返回"换一把钥匙"的开关。

    stdio 与 in-memory 这两条传输没有请求头，凭据只能从环境变量来（见 ``auth.py``）。
    走真实的发放 + 真实的中间件：协议层这些用例要验的正是"凭据 → 主体 → 工具"整条链，
    手搓一个 Caller 塞进 ContextVar 就把它跳过去了（那样只测了半条）。
    """

    def use(**kwargs: object) -> str:
        issued = services.api_keys.create(
            name=f"协议用例 {uuid.uuid4().hex[:6]}", **kwargs
        )
        monkeypatch.setenv(MCP_KEY_ENV, issued.token)
        return issued.token

    return use


async def _call(tool: str, arguments: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    """用一个**真的 MCP 客户端**连上真的 server 调一次工具（in-memory 传输）。

    不走 ``call_tool`` 直调：``isError``、``structuredContent``、参数的校验顺序
    全是协议层的事，直调服务层验不到——而被测出来的那几个毛病恰好都在这条缝里。
    """
    from mcp import Client

    async with Client(build_server()) as client:
        return await client.call_tool(tool, arguments)


async def test_a_domain_failure_comes_back_as_is_error_with_our_text(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """**业务失败必须是 ``isError: true``**，而文本仍是我们那句话。

    这是那个测试员报的第一件事：只读 Key 调 ``delete_document`` 被拒，
    回来的却是 ``isError: false`` ——"拒绝"在协议层被报成了"成功"，
    客户端与模型都只能自己去解析那段中文才能发现不对劲。
    """
    from app.models.enums import ApiKeyPermission

    as_key(permission=ApiKeyPermission.READWRITE)
    uploaded = await _call(
        "upload_document",
        {
            "knowledge_base_id": _new_kb(services),
            "filename": "只读删不掉.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
    )
    assert uploaded.is_error is False
    document_id = json.loads(uploaded.content[0].text)["document_id"]

    as_key(permission=ApiKeyPermission.READONLY)
    refused = await _call("delete_document", {"document_id": document_id})

    assert refused.is_error is True
    body = json.loads(refused.content[0].text)
    assert "只读" in body["error"], '文本形状没变（还是 {"error": …}），变的只是那个布尔量'


async def test_a_missing_parameter_comes_back_in_chinese(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """缺参数要看到 ``缺少参数：document_id``，而不是 SDK 的 pydantic 英文 dump。

    从前 SDK 先校验签名，我们那句中文**根本没机会说出口**：客户端拿到的是一段
    ``1 validation error for get_document_contentArguments / document_id Field required``
    加一个 pydantic 文档链接。
    """
    from app.models.enums import ApiKeyPermission

    as_key(permission=ApiKeyPermission.READWRITE)

    result = await _call("get_document_content", {})

    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert body["error"] == "缺少参数：document_id"
    assert "validation error" not in result.content[0].text
    assert "pydantic" not in result.content[0].text


async def test_a_bad_enum_comes_back_in_chinese_with_the_accepted_values(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """枚举写错要说清可选值（模型据此自己改对，而不是放弃这个工具）。"""
    from app.models.enums import ApiKeyPermission

    as_key(permission=ApiKeyPermission.READWRITE)

    result = await _call(
        "add_data_source",
        {"knowledge_base_id": _new_kb(services), "kind": "webdav", "url": "https://example.com"},
    )

    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert "kind 只能是 rss 或 html" in body["error"]
    assert "webdav" in body["error"]


async def test_a_tool_outside_the_keys_scope_is_an_error_result(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """工具范围之外的工具也是 ``isError: true`` 加我们那句话（闸门在 ``call_tool`` 里）。

    工具名**不存在**是另一回事：那种调用到不了我们这层，SDK 自己先拦下并回
    ``Unknown tool: …``（同样带 ``isError``）——``tools/list`` 的过滤只是"别把永远
    调不成的工具摆给模型看"，真正的闸门是这里这一道。
    """
    from app.models.enums import ApiKeyPermission

    as_key(permission=ApiKeyPermission.READWRITE, allowed_tool_names=["search"])

    result = await _call("list_knowledge_bases", {})

    assert result.is_error is True
    assert "list_knowledge_bases" in result.content[0].text


async def test_server_info_carries_the_surface_fingerprint() -> None:
    """``serverInfo.version`` = ``<发版号>+mcp.<指纹>``：一次握手就把契约带过去了。

    客户端因此**能发现契约变过**——只靠发版号不行（实测连着几次部署都是 0.1.0，
    工具面却变过）。
    """
    from mcp import Client

    from app.core.config import get_settings
    from app.services.tools import surface_fingerprint

    fingerprint = surface_fingerprint()
    async with Client(build_server()) as client:
        version = client.server_info.version

    assert version == f"{get_settings().app_version}+mcp.{fingerprint}"
    assert fingerprint in version


async def test_search_returns_structured_content_that_matches_the_text(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """``search`` 的判定要同时给两种读法：文本那份**原样不动**，结构化那份搬同一批数据。

    两份是**同一批数据的两种读法**（同一批判定 + 同一批命中原文），不是"摘要 + 全文"：
    结构化那份要是少了 ``hits``，按 ``outputSchema`` 读的调用方就什么都拿不到。
    客户端会照 ``outputSchema`` 校验 ``structuredContent``（缺字段或不合规直接报错），
    所以这条用例本身就是一次"结构对不对"的实测——而且挂在**协议层**，
    与 Codex 那种客户端看到的是同一条路。
    """
    from app.models.enums import ApiKeyPermission
    from app.services.tools import surface_fingerprint

    as_key(permission=ApiKeyPermission.READWRITE)
    kb_id = _new_kb(services)
    uploaded = await _call(
        "upload_document",
        {
            "knowledge_base_id": kb_id,
            "filename": "判定.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
    )
    services.ingest.ingest(json.loads(uploaded.content[0].text)["document_id"])

    result = await _call("search", {"query": "眼轴长度", "knowledge_base_ids": [kb_id]})

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None, "声明了 outputSchema 就必须给 structuredContent"
    assert set(structured) == {
        "summary",
        "fit",
        "distribution",
        "decision",
        "returned",
        "filtered_out",
        "offset",
        "next_offset",
        "query",
        "hits",
        "surface_fingerprint",
    }
    assert structured["surface_fingerprint"] == surface_fingerprint()

    # 文本那份**形状与键序一个字不改**（读它的是模型，键序就是阅读顺序）：
    # 翻页那两项紧跟在计数之后，别处的键一个都不动
    payload = json.loads(result.content[0].text)
    assert list(payload)[:7] == [
        "summary",
        "distribution",
        "decision",
        "returned",
        "filtered_out",
        "offset",
        "next_offset",
    ]
    assert payload["summary"] == structured["summary"]
    assert payload["decision"] == structured["decision"]
    assert structured["fit"] == payload["distribution"]["fit"]
    assert payload["offset"] == structured["offset"] == 0
    assert payload["next_offset"] == structured["next_offset"]
    # 机器那份**不能只剩判定**：这次的成果（问题与原文片段）必须逐条搬过去，
    # 否则按 outputSchema 读的客户端看到 returned: 3 却一条都拿不到
    assert structured["query"] == payload["query"] == "眼轴长度"
    assert structured["hits"] == payload["hits"]
    assert structured["hits"], "库里刚灌进一份命中率极高的文档"


async def test_search_carries_the_verdict_for_a_caller_with_no_library(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """一个库都没有时那条提前返回也要过 ``outputSchema`` 这一关（可空字段给 ``null``）。

    那条路的文本载荷只有 ``query`` / ``hits`` / ``note`` 加翻页那两项——不兜底的话，
    调用方照 ``outputSchema`` 校验会当场报"少字段"，而那是最不该失败的一种情况。
    """
    from app.models.enums import ApiKeyPermission

    # 范围落在**不存在的库**上 → 他看得见的库是空的 → 走"没有任何知识库"那条提前返回
    as_key(permission=ApiKeyPermission.READONLY, knowledge_base_ids=["kb_不存在的"])

    result = await _call("search", {"query": "随便问问"})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["decision"] is None
    assert result.structured_content["fit"] is None
    assert result.structured_content["returned"] == 0
    assert result.structured_content["offset"] == 0
    assert result.structured_content["next_offset"] is None, "空页不说「还有下一页」"
    # 空库不是"少两个键"：问题照回显，原文那一条本来就是空的（不能是 null——schema 说它是 array）
    assert result.structured_content["query"] == "随便问问"
    assert result.structured_content["hits"] == []
    assert json.loads(result.content[0].text)["note"] == "没有任何知识库"


def test_the_search_tool_advertises_and_accepts_offset() -> None:
    """``offset`` 必须**既在广告里、也在白名单里**——漏一半的表现都是"这个参数没起作用"。

    广告里漏了：模型压根不知道能翻页；白名单里漏了：它传了会被安静丢掉，于是**永远拿回
    第一页**（而响应里的 ``next_offset`` 明明说着"还有"）。后半句是 ``_PARAMS`` 那条
    老毛病，由用例逐工具钉住，这里对 ``offset`` 再点一次名。
    """
    from app.mcp_server.server import _PARAMS

    server = build_server()
    tool = next(tool for tool in server._tool_manager.list_tools() if tool.name == "search")

    assert "offset" in tool.parameters["properties"], "模型得看得见这个参数"
    assert tool.parameters["properties"]["offset"]["minimum"] == 0, "起点从 0 起，负数不收"
    assert "offset" in _PARAMS["search"], "看得见却传不进来，比看不见更糟"
    assert "next_offset" in (tool.output_schema or {})["properties"], "下一步要机器读得到"


async def test_a_paging_offset_survives_the_protocol_layer(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """``offset`` 走一次**真实 MCP 调用**：传 9999 回来必须是空页，而不是第一页。

    直接 ``call_tool`` 是绕开 ``_PARAMS`` 白名单的，而"参数被丢掉"的表现正是"翻页没生效"
    （回的还是第一页）——所以这一条必须走客户端。库里只有一段（``MARKDOWN``），
    起点 9999 必然越过末尾，于是 ``offset`` 的回显就是"它真的到了服务层"的证据。
    """
    from app.models.enums import ApiKeyPermission

    as_key(permission=ApiKeyPermission.READWRITE)
    kb_id = _new_kb(services)
    uploaded = await _call(
        "upload_document",
        {
            "knowledge_base_id": kb_id,
            "filename": "翻页.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
    )
    services.ingest.ingest(json.loads(uploaded.content[0].text)["document_id"])

    beyond = await _call(
        "search", {"query": "眼轴长度", "knowledge_base_ids": [kb_id], "offset": 9999}
    )
    payload = json.loads(beyond.content[0].text)

    assert beyond.is_error is False, "翻过头不是错"
    assert payload["offset"] == 9999, "起点被丢掉的话这里会回 0"
    assert payload["hits"] == []
    assert payload["next_offset"] is None
    assert payload["summary"], "判定照旧在——它说的是那批候选，与翻到哪儿无关"


def test_the_search_tool_advertises_and_accepts_filters() -> None:
    """``filters`` 必须**既在广告里、也在白名单里**（同 ``offset`` 那条的理由）。

    广告里漏了：模型压根不知道能"只查某本书某章"，只能把整库的命中捞出来再自己翻
    ——那正是用户抱怨的那件事；白名单里漏了：它传了会被安静丢掉，筛了跟没筛一样。
    """
    from app.mcp_server.server import _PARAMS

    server = build_server()
    tool = next(tool for tool in server._tool_manager.list_tools() if tool.name == "search")
    schema = tool.parameters["properties"]["filters"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False, "认不出的字段要报错，不能被安静丢掉"
    assert set(schema["properties"]) == {
        "document_ids",
        "source_kinds",
        "created_after",
        "created_before",
        "page_from",
        "page_to",
        "heading_contains",
    }
    assert schema["properties"]["page_from"]["minimum"] == 1, "页码从 1 起"
    assert "filters" in _PARAMS["search"], "看得见却传不进来，比看不见更糟"


async def test_search_filters_survive_the_protocol_layer(
    services, as_key  # type: ignore[no-untyped-def]
) -> None:
    """``filters`` 走一次**真实 MCP 调用**：筛到一个不存在的文档，命中必须是空的。

    直接 ``call_tool`` 是绕开 ``_PARAMS`` 的，而"参数被丢掉"的表现正是"筛了没生效"
    （照样回那一段命中）——所以这一条必须走客户端。同一个查询：不带 ``filters`` 有命中、
    带了就一条不剩，这一对差就是"它真的到了服务层"的证据。
    """
    from app.models.enums import ApiKeyPermission

    as_key(permission=ApiKeyPermission.READWRITE)
    kb_id = _new_kb(services)
    uploaded = await _call(
        "upload_document",
        {
            "knowledge_base_id": kb_id,
            "filename": "过滤.md",
            "content_base64": base64.b64encode(MARKDOWN.encode()).decode(),
        },
    )
    services.ingest.ingest(json.loads(uploaded.content[0].text)["document_id"])

    plain = await _call("search", {"query": "眼轴长度", "knowledge_base_ids": [kb_id]})
    filtered = await _call(
        "search",
        {
            "query": "眼轴长度",
            "knowledge_base_ids": [kb_id],
            "filters": {"document_ids": ["doc_不存在的"]},
        },
    )
    payload = json.loads(filtered.content[0].text)

    assert json.loads(plain.content[0].text)["hits"], "不带过滤时这份文档本来就该有命中"
    assert filtered.is_error is False, "筛到空不是错"
    assert payload["hits"] == [], "过滤条件被白名单丢掉的话，这里会回那段命中"
    assert payload["summary"], "判定照旧在——它说的是这一范围里的候选"


def _new_kb(services) -> str:  # type: ignore[no-untyped-def]
    return services.knowledge_bases.create(
        kb_id=f"kb_{uuid.uuid4().hex[:12]}", name="协议用例库"
    ).id
