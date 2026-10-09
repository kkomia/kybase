"""MCP 服务：stdio 与 Streamable HTTP 两种传输（T4.8）。

架构 §5 要求 MCP 以两种方式暴露同一批工具：

- **stdio**：本地客户端（Claude Desktop、Cursor 这类）把本进程当子进程拉起；
- **Streamable HTTP**：局域网内其他机器通过 HTTP 连过来。

两者的差别只在"消息怎么走"，工具集与实现**完全共用** ``tools.call_tool``。
这是把工具实现抽出去的唯一理由：写在某个入口里就得复制两份，而两份迟早会漂。

**对外面就是知识库那 8 个工具**（v0.1.0 瘦身收口）：这个服务是**知识库服务**，
而 ``services/tools.py`` 那张表是 kylab 合体期的产物——里面有 ``create_note`` 这类
会真写笔记、``remember`` / ``forget`` 这类会真动用户档案的工具。把它们留在对外 MCP 面上，
等于让外部客户端从这个服务里拿到 agent 的权限面。**瘦身之后 Agent 侧的实现已整体拆除**
（剥离方案 §6 阶段 1 第 5 步），``services.tools.TOOL_NAMES`` 剩下的正好就是纯知识库
那 8 个工具，所以 :data:`MCP_TOOL_NAMES` 直接取它——**名单只有一处定义**，
不在这里另抄一遍。

**SDK 版本注意**：本模块按 **mcp 2.x** 的 ``MCPServer`` 写。
1.x 里这个类叫 ``FastMCP``；官方迁移指南给的选项是
"改用 ``MCPServer``，或把依赖钉在 ``mcp<2``"。这里选前者——用新 API 比锁老版本活得久。
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.services.tools import TOOL_NAMES, call_tool

__all__ = ["MCP_TOOL_NAMES", "build_server", "main", "serve_http", "serve_stdio"]

logger = logging.getLogger(__name__)

#: **对外 MCP 面**：就是 ``TOOL_NAMES`` 那张纯知识库工具表（顺序也一致）。
#:
#: 为什么不在这里另写一份名单：抄一份到这里，两处迟早相漂，而漂出来的形态是
#: "挂上去的工具在实现表里没有"（``make_handler`` 取 spec 当场 KeyError）或反过来
#: （安静地少挂一个工具）。既然 ``services/tools.py`` 现在只剩知识库工具，
#: 这张表本身就是那份名单。
#:
#: 那条不变量由用例兜住（``tests/unit/mcp_server/test_server.py`` 断言它正好等于
#: ``TOOL_NAMES`` 与那 8 个名字）——app 代码里不写 assert（``ruff`` 的 S101）。
MCP_TOOL_NAMES: tuple[str, ...] = tuple(TOOL_NAMES)

#: 每个工具接受的参数名。**用来丢掉 schema 之外的参数**：
#: 服务层对未知关键字会直接 TypeError，而模型偶尔会多带一个字段——
#: 为此让整个工具失败不值得。
#:
#: **只列 :data:`MCP_TOOL_NAMES` 那 8 个**：这里多列一个不会有事
#: （它挂不上），少一个会直接 KeyError（服务起不来），而两者的门禁是同一条用例
#: （见 ``tests/unit/services/test_tools.py::test_every_tool_has_a_parameter_whitelist``
#: 与 ``tests/unit/mcp_server/test_server.py``）。
_PARAMS: dict[str, tuple[str, ...]] = {
    "list_knowledge_bases": (),
    "create_knowledge_base": ("name", "description"),
    "upload_document": ("knowledge_base_id", "filename", "content_base64"),
    "add_data_source": ("knowledge_base_id", "kind", "url", "name"),
    "search": ("query", "knowledge_base_ids", "top_k"),
    "list_documents": ("knowledge_base_id", "query", "limit"),
    "get_document_status": ("document_id",),
    "delete_document": ("document_id",),
}


def build_server():  # type: ignore[no-untyped-def]
    """组装 MCP server，把 :data:`MCP_TOOL_NAMES`（对外面那批纯知识库工具）挂上去。

    延迟 import：``mcp`` 在可选 extra 里。顶层 import 会让没装 extra 的用户
    连主服务都起不来——而 MCP 是**可选能力**，不该成为主链路的硬依赖。

    **别在这里写工具个数**：它随版本变（写"十一个"时已经涨到十六个），
    而一句过期的数字会让读的人以为少挂了几个。个数以 ``MCP_TOOL_NAMES`` 为准。

    """
    import json

    from mcp.server.mcpserver import MCPServer

    from app.core.config import get_settings
    from app.core.exceptions import KylabError
    from app.core.services import get_services
    from app.mcp_server.auth import CallerMiddleware, current_caller
    from app.services.tools import tool_definitions

    server = MCPServer(
        name="kylab",
        # 取自 Settings，不再手写：手写的那个只会在发版时漏改（v0.1.0→0.2.0 前，
        # 这里与 pyproject / package.json / Settings 是四处各自为政的版本号）
        version=get_settings().app_version,
        instructions=(
            "kylab 知识库。核心能力是检索原文：search 返回带出处的原文片段，"
            "不是生成的回答。先 list_knowledge_bases 确认有哪些库，再按库检索"
            "——库里没有的东西检索不出来。"
        ),
        # 身份解析：HTTP 读 Authorization 头，stdio 读 KYLAB_MCP_KEY（见 auth.py）。
        # 挂在这里而不是每个工具里：判定只写一处，漏判才不会成为可能。
        # **中间件读的那张脸**：`services.knowledge_bases`（库清单）与 `services.api_keys`。
        middleware=[CallerMiddleware(get_services())],
    )

    # 描述从 tool_definitions 读，不在这里另写一份：那份清单已经是唯一真相，
    # 两处各写一份迟早会漂（一个加了参数另一个没加），
    # 而漂了之后**客户端看到的与实现不一致**，比没有描述更糟。
    specs = {item["name"]: item for item in tool_definitions()}

    def make_handler(tool_name: str):  # type: ignore[no-untyped-def]
        schema = specs[tool_name]["inputSchema"]
        properties: dict[str, dict] = schema.get("properties", {})
        required = set(schema.get("required", ()))
        accepted = set(_PARAMS[tool_name])

        async def handler(**kwargs: object) -> str:
            payload = {key: value for key, value in kwargs.items() if key in accepted}
            try:
                # **身份在事件循环这一侧取**：ContextVar 是中间件在本任务里 set 的，
                # 而 to_thread 会把它复制到另一个线程——先取出来再带进去，语义更清楚
                caller = current_caller()
                # 同步的服务层调用扔到线程池：MCP 的请求处理跑在事件循环里，
                # 而检索与入库都是阻塞的，直接在循环里调会把整个服务卡住。
                #
                # **喂它与中间件同一个根**（v0.1.0 瘦身后只有一个组合根）：`tools.py`
                # 只依赖服务层，取的是 `services.knowledge_bases` / `services.retrieval` 那几张
                # 脸——不再有"混合体经组合根取对侧服务"那种过渡形态。
                result = await asyncio.to_thread(
                    call_tool, get_services(), tool_name, payload, caller=caller
                )
            except KylabError as exc:
                # **领域错误交给模型看，不要抛出去**：SDK 会把异常统一压成
                # 一句没有细节的 "Error executing tool xxx"（实测确认），
                # 模型看不到"缺少参数：filename"，只能盲猜着重试。
                # 返回成 JSON 之后，它能自己把参数改对，或者把原因转述给用户。
                return json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2)
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)

        handler.__name__ = tool_name
        # **签名必须是真的**：SDK 从函数签名推导工具的参数 schema，
        # 只写 ``**kwargs`` 会让它推导出一个名为 kwargs 的必填字段——
        # 实测报 "1 validation error for ...Arguments / kwargs Field required"，
        # 工具根本调不动。所以按 schema 现造一个签名装上去。
        handler.__signature__ = _signature_from(properties, required)  # type: ignore[attr-defined]
        handler.__annotations__ = {
            key: _annotation_for(spec) for key, spec in properties.items()
        }
        handler.__annotations__["return"] = str
        return handler

    for name in MCP_TOOL_NAMES:
        server.add_tool(
            make_handler(name),
            name=name,
            description=specs[name]["description"],
            # 关掉结构化输出：本项目的工具返回的是给模型读的 JSON 文本，
            # 让它再包一层会把可读性弄差（而且模型并不需要那层 schema）
            structured_output=False,
        )
        logger.debug("已注册 MCP 工具 %s", name)

    return server


async def serve_stdio() -> None:
    """stdio 传输：被客户端当子进程拉起时用这条。

    **凭据放在客户端配置的 ``env`` 里**（键名 ``KYLAB_MCP_KEY``，值是 API Key）：
    这条传输没有请求头可读，所以环境变量是唯一的来源。没设也能连上、能列出工具，
    但每个工具调用都会被拒并说明原因——见 ``auth.py`` 的解释。
    """
    await build_server().run_stdio_async()


async def serve_http(host: str, port: int) -> None:
    """Streamable HTTP 传输：局域网内其他机器连过来时用这条。

    **默认只监听 127.0.0.1**：MCP 能读写知识库（含删除）。
    要让别的机器连，得显式传 ``--host 0.0.0.0``。

    **鉴权现在是强制的**（v0.12 收口）：每个请求都要带
    ``Authorization: Bearer <API Key>``，Key 的作用域决定它能看到、能写哪些库。
    在此之前这里**没有任何身份**——实测不带凭据即可列出全部知识库，
    而同一进程还挂着 ``delete_document``。收口方式见 ``auth.py``。
    """
    await build_server().run_streamable_http_async(host=host, port=port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kylab-mcp",
        description="kylab 的 MCP 服务（架构 §5：stdio 与 Streamable HTTP 两种传输）",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio 给本地客户端；http 给局域网内其他机器",
    )
    parser.add_argument("--host", default="127.0.0.1", help="http 模式监听地址")
    parser.add_argument("--port", type=int, default=8765, help="http 模式监听端口")
    parser.add_argument(
        "--list-tools", action="store_true", help="只打印工具清单，不启动服务"
    )
    args = parser.parse_args(argv)

    if args.list_tools:
        # 便于排障的开关：用户能确认"装上了、工具认得出来"，不必先去配客户端
        for name in TOOL_NAMES:
            print(name)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

    if args.transport == "stdio":
        asyncio.run(serve_stdio())
    else:
        logger.info("MCP HTTP 服务监听 %s:%d", args.host, args.port)
        asyncio.run(serve_http(args.host, args.port))
    return 0


def _annotation_for(spec: dict) -> type:
    """JSON Schema 类型 → Python 注解。

    只覆盖本项目实际用到的几种。**认不出的一律给 ``str``**：
    注解错了只是类型提示不准，而缺注解会让 SDK 推不出参数——
    后者会让工具直接不可用。
    """
    kind = spec.get("type")
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "array":
        return list
    if kind == "object":
        return dict
    return str


def _signature_from(properties: dict, required: set[str]):  # type: ignore[no-untyped-def]
    """按工具 schema 现造一个函数签名。

    ``required`` 里的参数无默认值；其余给 ``None`` 默认值，
    这样 MCP 客户端不传也能过——本项目所有工具都自己做了
    "缺参数报明确错误"的处理，比让 SDK 抛校验错更可读
    （``缺少参数：filename`` 比 ``Field required`` 有用得多）。
    """
    import inspect

    parameters = []
    for key, spec in properties.items():
        annotation = _annotation_for(spec)
        if key in required:
            parameters.append(
                inspect.Parameter(
                    key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation
                )
            )
        else:
            parameters.append(
                inspect.Parameter(
                    key,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=annotation | None,  # type: ignore[operator]
                )
            )
    return inspect.Signature(parameters, return_annotation=str)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
