"""MCP 服务装配（T4.8）。

镜像同构：``app/mcp/server.py`` → 本文件。

**这里只测装配，不测传输**：stdio 与 HTTP 是 SDK 的事。
但有一类**只有装配期才会暴露**的问题必须钉住——定义顺序。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.mcp_server.server import MCP_TOOL_NAMES, build_server
from app.services.agent_tools import _KB_TOOLS
from app.services.tools import TOOL_NAMES

SERVER_PATH = Path(inspect.getfile(build_server))


def test_server_registers_exactly_the_kb_face() -> None:
    """挂上去的**正好是知识库那一侧的工具**（v0.1.0 收窄）。

    两个方向都查：注册的那些必须是 ``MCP_TOOL_NAMES``，且 ``MCP_TOOL_NAMES`` 必须
    **正好等于** ``_KB_TOOLS``——后半句是那份"过滤"的护栏：``_KB_TOOLS`` 里哪天多出
    一个不在 ``TOOL_NAMES`` 里的名字，过滤会把它安静地丢掉，而这里当场红。
    """
    server = build_server()

    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == set(MCP_TOOL_NAMES)
    assert set(MCP_TOOL_NAMES) == set(_KB_TOOLS)


def test_server_never_registers_an_agent_tool() -> None:
    """**Agent 侧的工具一个都不许出现在对外面上**（剥离方案 §7.5）。

    这条是收窄那一次改动的验收本身：``create_note`` 会真写笔记、``remember`` /
    ``forget`` 会真动用户档案、``export_*`` 会把文件落进别人的会话产物区——
    它们都是 **agent 的权限面**，从这个知识库服务里拿不到才对。

    判据取 ``TOOL_NAMES`` 减去 ``_KB_TOOLS``：**不写死一份黑名单**，
    哪天 ``tools.py`` 长出新的 Agent 工具，它自动进这条断言。
    """
    server = build_server()

    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert not (names & (set(TOOL_NAMES) - _KB_TOOLS))


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
