"""MCP 客户端（v0.15，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.2）。

在此之前 KYLAB **只做 MCP 服务端**（把自己的知识库能力暴露给外部 Agent）。
这个模块补上反向的那一半：**连别人的 MCP 服务、发现它的工具、在对话里调用**——
这就是"技能与插件的能力"里的插件那一半。

QwenPaw 把这层叫 **Drivers**，并强调两件事，这里都照做：

1. **凭据加密存放、绝不回显**。接口只回"这个 key 配过没有"（``has_secrets``），
   与设置页里模型注册表对 api_key 的做法一致——回显一次，日志与截图里就有了一份。
2. **每次调用过策略闸**。三档：``allow``（直接调）、``ask``（要先确认，接口回 409
   并把"要调什么"讲清楚）、``deny``（拒绝）。默认 ``ask``：
   接一个外部服务进来就默认让它静默执行，是这层最不该有的默认。

**工具命名**：一律 ``mcp__<服务名>__<工具名>``（Anthropic 的 MCP 约定）。
加前缀不只是好看——外部服务的工具名我们无法约束，撞上内置工具名（``search``、
``list_documents``）会让模型与我们都分不清在调哪个。

**进程与超时**：``stdio`` 传输会**起一个子进程**。这意味着两件事：它是
"用户明确配置了才发生"的动作（不是自动发现），以及必须设超时——
一个卡住的子进程会拖住整个请求。超时后我们主动断开并如实报错。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
)
from app.core.logging import sanitize_log_value
from app.services.command_policy import (
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    Decision,
    RuleSet,
)
from app.storage.base import MCPServerRecord, StoreBundle

__all__ = [
    "POLICIES",
    "MCPClientService",
    "MCPTool",
    "split_qualified",
    "tool_qualified_name",
]

logger = logging.getLogger(__name__)

#: 三档准入策略。默认 ``ask``：接进来就默认静默执行是最不该有的默认。
POLICIES = ("allow", "ask", "deny")

#: 一次连接/调用的超时。stdio 会起子进程，所以给得比普通 HTTP 宽一点；
#: 但必须有——卡住的子进程会拖住整个请求。
_TIMEOUT_SECONDS = 20.0

#: 工具名前缀。见模块头：外部工具名我们无法约束，撞名会让"到底在调哪个"变得无解。
TOOL_PREFIX = "mcp"

#: 工具清单的缓存时长。**对话每一轮都要这张清单**（它要进工具表），
#: 而拿清单 = 连一次那个服务（stdio 还要起子进程）。每轮现连是不可接受的：
#: 一句话的成本会变成"起三个子进程 + 三次握手"。
_CACHE_TTL_SECONDS = 300.0

#: 连不上时的缓存时长。**失败也要缓存，但要短**：
#: 不缓存的话，一个挂掉的服务会让每一轮对话都白等一个超时；
#: 缓存太久的话，用户刚把它修好却还要等五分钟。
_CACHE_FAIL_TTL_SECONDS = 60.0


def tool_qualified_name(server_name: str, tool_name: str) -> str:
    """``mcp__<服务>__<工具>``。服务名里的下划线会与分隔符混淆，统一折成横线。"""
    return f"{TOOL_PREFIX}__{normalized_server_name(server_name)}__{tool_name}"


def normalized_server_name(server_name: str) -> str:
    """服务名在限定名里的形态（见 :func:`tool_qualified_name`）。"""
    return "_".join(part for part in (server_name or "").replace("_", "-").split() if part)


def split_qualified(qualified: str) -> tuple[str, str] | None:
    """``mcp__<服务>__<工具>`` → ``(服务名, 工具名)``；不是外部工具就回 ``None``。

    **只切前两个分隔符**（``split(sep, 2)``）：工具名自己可能带 ``__``
    （``mcp__github__create__issue`` 是"服务 github、工具 create__issue"），
    按全部下划线切会把工具名切碎，然后我们会去找一个不存在的服务。
    """
    parts = (qualified or "").split("__", 2)
    if len(parts) != 3 or parts[0] != TOOL_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


@dataclass(frozen=True, slots=True)
class _CachedTools:
    """一份工具清单的缓存条目。``error`` 非空表示上次连失败了（``tools`` 必为空）。"""

    tools: tuple[MCPTool, ...]
    error: str
    at: float

    def fresh(self, now: float) -> bool:
        ttl = _CACHE_FAIL_TTL_SECONDS if self.error else _CACHE_TTL_SECONDS
        return (now - self.at) < ttl


@dataclass(frozen=True, slots=True)
class MCPTool:
    """外部服务上的一个工具（已带限定名）。"""

    name: str
    qualified: str
    description: str = ""
    server_id: str = ""
    server_name: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    """它的输入参数定义（MCP 的 ``inputSchema``）。

    要带上是因为**对话那侧要把它原样交给模型**：不给的话模型只知道有这个工具、
    不知道要传什么参数，于是每次调用都靠猜。能力页上不显示它，但它是
    "这个工具能不能真的被用起来"的关键。
    """


class MCPClientService:
    """MCP 服务的登记与调用。**无状态**：每次调用现连现断。

    不保持长连接的理由：这些服务是**用户配的外部依赖**，它们的可用性不该影响
    KYLAB 的进程健康（连不上只该让那一次调用失败）；而常驻连接要处理重连、
    子进程泄漏、配置变更时失效——那些复杂度换来的只是几百毫秒。
    """

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores
        #: 服务 id → 上次问到的工具清单。见 ``_CACHE_TTL_SECONDS`` 上的说明。
        #: 进程内（不落库）：它是**缓存**，重启后重新发现一次是正确行为，
        #: 而落库会引入"库里那份是旧的"这个新的不一致。
        self._cache: dict[str, _CachedTools] = {}

    # ------------------------------------------------------------------ 登记

    def list(self, *, user_id: str | None) -> list[MCPServerRecord]:
        records = self._stores.meta.list_mcp_servers()
        if user_id is None:
            return records
        return [item for item in records if item.owner_id == user_id]

    def get(self, server_id: str, *, user_id: str | None) -> MCPServerRecord:
        record = self._stores.meta.get_mcp_server(server_id)
        if record is None or not self._visible(record, user_id):
            raise NotFoundError(f"MCP 服务不存在：{server_id}")
        return record

    def create(
        self,
        *,
        name: str,
        transport: str,
        target: str,
        user_id: str | None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        policy: str = "ask",
    ) -> MCPServerRecord:
        clean = (name or "").strip()
        if not clean:
            raise InvalidRequestError("缺少参数：name")
        if transport not in ("stdio", "http"):
            raise InvalidRequestError("transport 只能是 stdio 或 http")
        if not (target or "").strip():
            raise InvalidRequestError("缺少参数：target（stdio 填命令，http 填地址）")
        if policy not in POLICIES:
            raise InvalidRequestError(f"policy 只能是 {'/'.join(POLICIES)}")
        return self._stores.meta.create_mcp_server(
            MCPServerRecord(
                id=f"mcp_{uuid.uuid4().hex[:12]}",
                name=clean,
                transport=transport,
                target=target.strip(),
                args=tuple(args or ()),
                env=dict(env or {}),
                headers=dict(headers or {}),
                policy=policy,
                owner_id=user_id,
            )
        )

    def update(self, server_id: str, *, user_id: str | None, **fields: Any) -> MCPServerRecord:
        """改配置。**凭据字段是"整份替换"而不是合并**：删掉一个 header 也得能表达，
        而合并语义下"删掉"与"没传"分不开。"""
        record = self.get(server_id, user_id=user_id)
        for key in ("name", "target", "policy"):
            value = fields.get(key)
            if value is not None:
                setattr(record, key, str(value).strip())
        if record.policy not in POLICIES:
            raise InvalidRequestError(f"policy 只能是 {'/'.join(POLICIES)}")
        if not record.name or not record.target:
            raise InvalidRequestError("name 与 target 都不能为空")
        if fields.get("transport") is not None:
            if fields["transport"] not in ("stdio", "http"):
                raise InvalidRequestError("transport 只能是 stdio 或 http")
            record.transport = fields["transport"]
        if fields.get("args") is not None:
            record.args = tuple(str(item) for item in fields["args"])
        if fields.get("env") is not None:
            record.env = {str(k): str(v) for k, v in fields["env"].items()}
        if fields.get("headers") is not None:
            record.headers = {str(k): str(v) for k, v in fields["headers"].items()}
        if fields.get("enabled") is not None:
            record.enabled = bool(fields["enabled"])
        saved = self._stores.meta.update_mcp_server(record)
        # 改了配置（命令、地址、开关）工具清单就可能变了，缓存必须作废：
        # 留着的话，用户改完 target 之后对话里用的还是旧服务的那张工具表
        self.invalidate(saved.id)
        return saved

    def delete(self, server_id: str, *, user_id: str | None) -> None:
        self.get(server_id, user_id=user_id)
        self._stores.meta.delete_mcp_server(server_id)
        self.invalidate(server_id)

    # -------------------------------------------------------------- 工具清单

    def invalidate(self, server_id: str | None = None) -> None:
        """作废缓存。``server_id`` 为空时全清（改全局策略、测试用）。"""
        if server_id is None:
            self._cache.clear()
            return
        self._cache.pop(server_id, None)

    def cached_tools(self, record: MCPServerRecord) -> tuple[list[MCPTool], str]:
        """这个服务的工具清单，**从不抛异常**：返回 ``(工具, 连不上的原因)``。

        与 :meth:`list_tools` 的分工：那个是"我现在要问它一次"（探活、测试连接用，
        失败就该报出来）；这个是"我要给模型一张工具表，拿不到就别挡道"。
        两条路径要的东西正好相反，所以不合并成一个方法。
        """
        now = time.monotonic()
        cached = self._cache.get(record.id)
        if cached is not None and cached.fresh(now):
            return list(cached.tools), cached.error
        try:
            tools = self.list_tools(record)
        except Exception as exc:
            # 兜住一切：上游异常类型不受我们控制（实测有 FileNotFoundError、
            # MCPError、TimeoutError，还有 anyio 包出来的 ExceptionGroup）。
            # 一个外部服务挂掉不该让整张工具表消失——那正是"接外部依赖"
            # 最不该有的耦合。
            reason = f"{type(exc).__name__}: {exc}"
            self._cache[record.id] = _CachedTools(tools=(), error=reason, at=now)
            logger.info(
                "MCP 服务 %s 的工具清单取不到：%s", record.name, sanitize_log_value(exc)
            )
            return [], reason
        self._cache[record.id] = _CachedTools(tools=tuple(tools), error="", at=now)
        return tools, ""

    def available_tools(self, *, user_id: str | None) -> list[tuple[MCPServerRecord, MCPTool]]:
        """**启用中**且能连上的服务所暴露的全部工具（带它们的服务）。

        服务列表按调用方收口（``user_id``），所以"我能用的外部工具"与"我能看到的
        MCP 服务"是同一批——不会出现"能力页里看不见、对话里却调得动"。
        """
        out: list[tuple[MCPServerRecord, MCPTool]] = []
        for record in self.list(user_id=user_id):
            if not record.enabled:
                continue
            tools, _reason = self.cached_tools(record)
            out.extend((record, tool) for tool in tools)
        return out

    # ------------------------------------------------------------------ 判定

    def decide(
        self, record: MCPServerRecord, tool_name: str, *, rules: RuleSet | None = None
    ) -> Decision:
        """这次调用该放行、该问、还是该拒。**两个调用点共用这一处判定。**

        判定顺序（与规则层同一条规矩：``deny > ask > allow``，越具体越说了算）：

        1. 服务策略是 ``deny`` → 拒。**这一档是硬停**，命中的放行规则也不能把它掀开
           ——否则"我在能力页把它关掉了"会被设置页里一条旧规则静默推翻；
        2. **命中的规则** → 按它的档位。规则比服务那一栏更具体（能细到单个工具），
           所以命中时它说了算：一条 ``mcp__web__search_web`` 的放行规则
           就该让"整台服务需要确认"下的这一个工具直接通过，而不必把整台服务放开
           ——这本就是最小权限的用法；
        3. 没命中规则 → 回服务那一档（它是这里的"总开关"）；
        4. 服务是 ``allow`` → 放行。

        **只看"命中的规则"**，不看规则集的默认档：规则集没命中时它的默认值也是
        ``ask``，把它当成一张 ask 规则用，会让"服务设成允许、清单一个字没写"
        这个最常见的部署变成每次都要求确认——用户明明已经放行了。

        为什么要合在一处而不是两个调用点各判一次：会话链路与 REST 端点各写一份的话，
        迟早出现"这边拦得住、那边拦不住"，而用户以为自己已经禁掉了。
        """
        rule_decision = (
            rules.decide(tool_qualified_name(record.name, tool_name), "") if rules else None
        )
        matched = rule_decision is not None and rule_decision.rule is not None
        if record.policy == "deny":
            return Decision(ACTION_DENY, reason=f"服务「{record.name}」的策略是拒绝调用")
        if matched:
            return rule_decision
        if record.policy == "ask":
            return Decision(ACTION_ASK, reason=f"服务「{record.name}」的策略是先确认")
        return Decision(ACTION_ALLOW, reason="服务策略是放行，且没有命中任何规则")

    @staticmethod
    def _visible(record: MCPServerRecord, user_id: str | None) -> bool:
        """可见性：无归属过滤的通道（管理员/API Key）看全部；成员只看自己的。

        与工作区/知识库/会话同一口径；**无主的老数据对成员不可见**。
        """
        if user_id is None:
            return True
        return record.owner_id == user_id

    # ------------------------------------------------------------------ 调用

    def probe(self, server_id: str, *, user_id: str | None) -> tuple[bool, str, list[MCPTool]]:
        """连通性 + 工具清单（「测试连接」用它）。失败**不抛错**，回一句人话。

        它同时是**刷新缓存的入口**：用户在能力页点一下，就顺带把对话那侧看到的
        工具表更新了。没有这一步，工具表只能等缓存自己过期（最多 5 分钟），
        而"我刚点了测试连接、它明明有这些工具"是最容易让人以为坏了的空档。
        """
        record = self.get(server_id, user_id=user_id)
        now = time.monotonic()
        try:
            tools = self.list_tools(record)
        except Exception as exc:  # 刻意兜住一切，理由见下
            # **probe 不向上抛任何东西**。它是「测试连接」，报错就是它的产出；
            # 抛出去的话前端只剩一句"请求失败"，用户看不到"是命令不存在、
            # 还是握手被拒、还是超时"——而那正是他要点这个按钮的原因。
            #
            # 实测过会走到这里的几类：命令不存在（`FileNotFoundError`）、
            # 子进程立刻退出（`MCPError: Connection closed`）、握手超时
            # （`TimeoutError`）、以及 anyio 把它们包成的 `ExceptionGroup`。
            # 它们的共同点是"都不在 `KylabError` 体系里"——上游的异常类型
            # 不受我们控制，所以这里只能按"兜住一切"来写。
            reason = f"{type(exc).__name__}: {exc}"
            self._cache[record.id] = _CachedTools(tools=(), error=reason, at=now)
            logger.info("MCP 服务 %s 探活失败：%s", record.name, exc, exc_info=True)
            return False, reason, []
        self._cache[record.id] = _CachedTools(tools=tuple(tools), error="", at=now)
        return True, f"连接正常，发现 {len(tools)} 个工具", tools

    def list_tools(self, record: MCPServerRecord) -> list[MCPTool]:
        """连上去问它有哪些工具。连不上就报错（**不返回空**）。

        不返回空是有意的：空列表意味着"这个服务没有任何工具"，而连不上是另一回事。
        混起来的话，界面会显示"发现 0 个工具"——用户会去检查服务配置，
        而真正的问题是网络。
        """
        raw = self._run(self._a_list_tools(record))
        tools: list[MCPTool] = []
        for item in raw:
            name = str(getattr(item, "name", "") or "")
            if not name:
                continue
            tools.append(
                MCPTool(
                    name=name,
                    qualified=tool_qualified_name(record.name, name),
                    description=str(getattr(item, "description", "") or ""),
                    server_id=record.id,
                    server_name=record.name,
                    schema=_schema_of(item),
                )
            )
        return tools

    def call(
        self,
        record: MCPServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approved: bool = False,
    ) -> str:
        """调用外部工具，返回**文本结果**。

        策略闸在这里（而不是在 API 层）：调用点将来不止一个（对话链路、
        MCP 服务端转发、定时任务），把闸放在能被绕过的地方等于没有闸。
        """
        if record.policy == "deny":
            raise ForbiddenError(
                f"这个 MCP 服务（{record.name}）被设为「拒绝调用」。"
                "要放行请在能力页把策略改成「允许」或「需要确认」"
            )
        if record.policy == "ask" and not approved:
            # 409 而不是 403：这不是"你不能做"，是"要先确认一下"。
            # 界面上据此弹确认框，确认后带着 approved=true 再调一次。
            raise ConflictError(
                f"调用 {record.name} 的 {tool_name} 需要你确认："
                "外部工具会以你的名义执行动作，确认后才会真正发出这次调用"
            )
        return self._run(self._a_call(record, tool_name, arguments))

    # -------------------------------------------------------------- 异步桥接

    def _run(self, coro: Any) -> Any:  # type: ignore[no-untyped-def]
        """在同步代码里跑一次异步调用。

        **每次新建一个事件循环**（``asyncio.run``）：协议层的端点都是同步 ``def``，
        FastAPI 把它们放在线程池里跑，那里没有正在运行的循环，所以这是安全的。
        反过来，如果哪天有人从 ``async def`` 端点里调到这里，会因为
        "循环已在运行"直接报错——那正是我们想要的：**别在事件循环里阻塞**。
        """
        return asyncio.run(asyncio.wait_for(coro, timeout=_TIMEOUT_SECONDS))

    async def _a_list_tools(self, record: MCPServerRecord) -> list[Any]:
        async with self._session(record) as session:
            result = await session.list_tools()
            return list(getattr(result, "tools", []) or [])

    async def _a_call(
        self, record: MCPServerRecord, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        async with self._session(record) as session:
            result = await session.call_tool(tool_name, arguments)
        return _flatten_result(result)

    def _session(self, record: MCPServerRecord):  # type: ignore[no-untyped-def]
        """建一条到外部服务的会话（``async with`` 用）。"""
        return _session(record)


def _session(record: MCPServerRecord):  # type: ignore[no-untyped-def]
    """按传输方式建会话。**两种传输分开建**，因为它们要的参数完全不同
    （stdio 是子进程 + 管道，http 是 URL + 请求头）。

    返回一个异步上下文管理器；调用方负责 ``wait_for`` 超时。
    """
    from mcp import ClientSession, StdioServerParameters

    if record.transport == "stdio":
        params = StdioServerParameters(
            command=record.target, args=list(record.args), env=dict(record.env) or None
        )
        return _stdio_session(ClientSession, params)

    from mcp.client.streamable_http import streamablehttp_client

    return _http_session(ClientSession, streamablehttp_client, record)


class _stdio_session:
    def __init__(self, client_session: Any, params: Any) -> None:
        self._client_session = client_session
        self._params = params

    async def __aenter__(self):
        from mcp.client.stdio import stdio_client

        self._stdio = stdio_client(self._params)
        read, write = await self._stdio.__aenter__()
        self._session = self._client_session(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def __aexit__(self, *exc: Any) -> None:
        # 先关会话再关管道：反过来的话，会话的关闭消息没有通道可发，
        # 子进程会留在那里等（Windows 上尤其明显）
        await self._session.__aexit__(*exc)
        await self._stdio.__aexit__(*exc)


class _http_session:
    def __init__(self, client_session: Any, transport: Any, record: MCPServerRecord) -> None:
        self._client_session = client_session
        self._transport = transport
        self._record = record

    async def __aenter__(self):
        self._streams = self._transport(
            self._record.target, headers=dict(self._record.headers) or None
        )
        streams = await self._streams.__aenter__()
        read, write = streams[0], streams[1]
        self._session = self._client_session(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def __aexit__(self, *exc: Any) -> None:
        await self._session.__aexit__(*exc)
        await self._streams.__aexit__(*exc)


def _schema_of(item: Any) -> dict[str, Any]:
    """外部工具的输入参数定义。

    **必须是对象**：MCP 里叫 ``inputSchema``，但不少服务给的是 ``null`` 或者
    干脆没有。原样传给模型侧会让整张工具表被拒（OpenAI 协议要求它是对象），
    所以这里回退成一个空对象 schema——"参数随便传"，比"这张表用不了"好。
    """
    schema = getattr(item, "inputSchema", None)
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


def _flatten_result(result: Any) -> str:
    """把工具返回摊成文本。

    MCP 的返回是一组 content block（文本、图片、资源引用）。**只取文本**：
    图片与资源引用要落到"文件在哪、怎么显示"那一层才有意义，而本轮不做那件事；
    把它们塞成 base64 进对话上下文只会烧 token。
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    if parts:
        return "\n".join(parts)
    if getattr(result, "isError", False):
        return "（外部工具返回了错误，但没有文本内容）"
    return "（外部工具返回了非文本内容）"
