"""MCP 服务装配（T4.8）。

镜像同构：``app/mcp/server.py`` → 本文件。

**这里只测装配，不测传输**：stdio 与 HTTP 是 SDK 的事。
但有一类**只有装配期才会暴露**的问题必须钉住——定义顺序。

**工具清单收窄**（v0.60）也在这里：它落在 ``MCPServer.list_tools`` 这个装配面上，
而那条链（凭据 → 主体 → 过滤）只有走真实的 ``CallerMiddleware`` 才验得完整。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.mcp_server.server import MCP_TOOL_NAMES, build_server, main

SERVER_PATH = Path(inspect.getfile(build_server))


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
    """**Agent 侧的工具一个都不在**（剥离方案 §6 阶段 1 第 5 步）：
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
    """
    assert main(["--list-tools"]) == 0

    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert printed == list(MCP_TOOL_NAMES) == list(KB_TOOL_NAMES)


def test_every_tool_has_a_description() -> None:
    """描述是给模型读的——没有它，模型只能靠名字猜该不该用这个工具。"""
    server = build_server()

    for tool in server._tool_manager.list_tools():
        assert tool.description and tool.description.strip(), tool.name


def test_tool_parameters_are_derived_from_the_schema() -> None:
    """**参数必须真的出现在签名里。**

    早先的实现只写 ``**kwargs``，而 SDK 从函数签名推导参数 schema——
    于是它推导出一个名为 ``kwargs`` 的必填字段，调用时报
    ``1 validation error for list_knowledge_basesArguments / kwargs Field required``，
    工具根本调不动。所以这里验"该有的参数都在"。
    """
    server = build_server()

    search = next(
        tool for tool in server._tool_manager.list_tools() if tool.name == "search"
    )
    properties = search.parameters.get("properties", {})
    assert {"query", "knowledge_base_ids", "top_k"} <= set(properties)


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
    """必填参数不能给默认值，否则 SDK 会把它们也标成可选，
    客户端就会在不传的情况下调用，然后在服务层才报"缺少参数"。"""
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
    assert {"build_server", "main", "_signature_from", "_annotation_for"} <= before_guard


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
