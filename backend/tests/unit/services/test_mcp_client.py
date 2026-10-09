"""MCP 客户端（v0.15）。

镜像同构：``app/services/mcp_client.py`` → 本文件。

**这里有一部分是"自己连自己"的真实验证**：把 KYLAB 的 MCP **服务端**
（``app/mcp_server/server.py``，本来就有）当外部服务起起来，用这一侧的**客户端**
去连、去发现工具。上下游都是真代码、真子进程、真协议，没有打桩——
用假服务器测的话，"我们发的初始化参数它收不收、它的返回我们解不解得开"
这两件最容易出问题的事恰好都测不到。

其余用假会话测的是**策略与形状**：命名前缀、策略三档、凭据不回显、
连不上时不返回空清单。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.exceptions import ConflictError, ForbiddenError, InvalidRequestError
from app.services.mcp_client import MCPClientService, MCPTool, tool_qualified_name
from app.storage.base import MCPServerRecord

BACKEND = Path(__file__).resolve().parents[3]


class _FakeMeta:
    """只够 ``MCPClientService`` 用的一小撮存储方法（它不是本文件的测试对象）。"""

    def __init__(self) -> None:
        self.records: dict[str, MCPServerRecord] = {}

    def create_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord:
        self.records[record.id] = record
        return record

    def get_mcp_server(self, server_id: str) -> MCPServerRecord | None:
        return self.records.get(server_id)

    def list_mcp_servers(self) -> list[MCPServerRecord]:
        return list(self.records.values())

    def update_mcp_server(self, record: MCPServerRecord) -> MCPServerRecord:
        self.records[record.id] = record
        return record

    def delete_mcp_server(self, server_id: str) -> None:
        self.records.pop(server_id, None)


class _FakeBundle:
    """``MCPClientService`` 只用得到 ``meta`` 一个仓储（它不碰向量/对象存储）。

    这里刻意**不**构造真的 ``StoreBundle``：那会要求把五个仓储都填上，
    而填假的那几个只会让"这个服务到底依赖什么"变得看不清。
    """

    def __init__(self, meta: _FakeMeta) -> None:
        self.meta = meta


def _service() -> tuple[MCPClientService, _FakeMeta]:
    meta = _FakeMeta()
    return MCPClientService(_FakeBundle(meta)), meta  # type: ignore[arg-type]


# ------------------------------------------------------------------ 命名


def test_qualified_name_has_the_convention_prefix() -> None:
    """``mcp__<服务>__<工具>``：外部工具名我们无法约束，必须加前缀，
    否则撞上内置工具（``search``）时"到底在调哪个"无解。"""
    assert tool_qualified_name("github", "create_issue") == "mcp__github__create_issue"


def test_qualified_name_folds_underscores_in_server_name() -> None:
    """服务名里的下划线会与分隔符混淆（``a_b__c`` 读不出边界），统一折成横线。"""
    assert tool_qualified_name("my_server", "tool") == "mcp__my-server__tool"


# ------------------------------------------------------------------ 策略


def test_deny_policy_refuses_before_connecting() -> None:
    """``deny`` 要在**连之前**就拒——连上去再拒等于已经起了子进程、发了网络请求。"""
    service, meta = _service()
    record = MCPServerRecord(
        id="mcp_1", name="x", transport="stdio", target="python", policy="deny"
    )
    meta.records["mcp_1"] = record

    with pytest.raises(ForbiddenError):
        service.call(record, "tool", {})


def test_ask_policy_needs_approval_and_says_why() -> None:
    """``ask`` 未确认时是 **409**（不是 403）：不是"你不能做"，是"要先确认"。"""
    service, meta = _service()
    record = MCPServerRecord(
        id="mcp_1", name="外部服务", transport="stdio", target="python", policy="ask"
    )
    meta.records["mcp_1"] = record

    with pytest.raises(ConflictError) as excinfo:
        service.call(record, "tool", {})

    message = str(excinfo.value)
    assert "确认" in message
    assert "外部服务" in message


def _stub_run(value: str):  # type: ignore[no-untyped-def]
    """替掉 ``_run``（真实现里是 ``asyncio.run``，单测不发网络）。

    **替身必须把协程关掉**：拿到就丢弃会留下
    ``RuntimeWarning: coroutine ... was never awaited``，而那正是"创建了协程却没跑"
    这类真 bug 的告警——被测试替身的噪音淹掉之后，真出现时就没人看得见了。
    """

    def run(coro):  # type: ignore[no-untyped-def]
        coro.close()
        return value

    return run


def test_ask_policy_proceeds_when_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认之后要真的发出去（否则确认框是个摆设）。"""
    service, meta = _service()
    record = MCPServerRecord(
        id="mcp_1", name="x", transport="stdio", target="python", policy="ask"
    )
    meta.records["mcp_1"] = record
    monkeypatch.setattr(service, "_run", _stub_run("工具返回的文本"))

    assert service.call(record, "tool", {}, approved=True) == "工具返回的文本"


def test_allow_policy_calls_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    service, meta = _service()
    record = MCPServerRecord(
        id="mcp_1", name="x", transport="stdio", target="python", policy="allow"
    )
    meta.records["mcp_1"] = record
    monkeypatch.setattr(service, "_run", _stub_run("ok"))

    assert service.call(record, "tool", {}) == "ok"


# ------------------------------------------------------------------ 登记


def test_create_validates_transport_and_policy() -> None:
    service, _ = _service()
    with pytest.raises(InvalidRequestError):
        service.create(name="x", transport="carrier-pigeon", target="y", user_id=None)
    with pytest.raises(InvalidRequestError):
        service.create(name="x", transport="stdio", target="y", policy="maybe", user_id=None)
    with pytest.raises(InvalidRequestError):
        service.create(name="x", transport="stdio", target="  ", user_id=None)


def test_members_only_see_their_own_servers() -> None:
    """归属口径与工作区/知识库一致：成员只看自己的。"""
    service, _ = _service()
    service.create(name="甲的", transport="stdio", target="python", user_id="u1")
    service.create(name="乙的", transport="stdio", target="python", user_id="u2")

    assert [item.name for item in service.list(user_id="u1")] == ["甲的"]
    assert len(service.list(user_id=None)) == 2


def test_cross_account_access_is_404() -> None:
    from app.core.exceptions import NotFoundError

    service, _ = _service()
    record = service.create(name="甲的", transport="stdio", target="python", user_id="u1")

    with pytest.raises(NotFoundError):
        service.get(record.id, user_id="u2")


def test_update_replaces_credentials_wholesale() -> None:
    """凭据是**整份替换**：不这样的话"删掉一个 header"没法表达。"""
    service, _ = _service()
    record = service.create(
        name="x",
        transport="http",
        target="https://example.test/mcp",
        headers={"Authorization": "Bearer a", "X-Trace": "b"},
        user_id=None,
    )

    updated = service.update(record.id, user_id=None, headers={"Authorization": "Bearer c"})

    assert updated.headers == {"Authorization": "Bearer c"}


# ------------------------------------------------------- 真连（自己连自己）


def _kylab_mcp_record() -> MCPServerRecord:
    """把 KYLAB 自己的 MCP 服务端当成"外部服务"。

    ``-m app.mcp_server.server`` 从 ``backend/`` 目录起，环境里带上仓库路径，
    这样它和当前进程用的是同一份代码——**上游改了、这个测试就会跟着动**，
    这正是我们要的（它不是"另一个产品"，是我们自己的另一半）。
    """
    return MCPServerRecord(
        id="mcp_self",
        name="kylab-self",
        transport="stdio",
        target=sys.executable,
        args=("-m", "app.mcp_server.server", "--transport", "stdio"),
        env={"PYTHONPATH": str(BACKEND)},
        policy="allow",
    )


def test_lists_tools_from_a_real_stdio_server() -> None:
    """真起子进程、真走 MCP 握手、真拿回工具清单。"""
    service, _ = _service()

    tools = service.list_tools(_kylab_mcp_record())

    names = {item.name for item in tools}
    # 这几个是 KYLAB MCP 服务端确实暴露的工具（见 app/services/tools.py）
    assert {"search", "list_knowledge_bases", "recall"} <= names
    # 限定名要带上服务名前缀
    assert all(item.qualified.startswith("mcp__kylab-self__") for item in tools)
    # 描述要非空：模型就是靠它决定用哪个工具的
    assert all(item.description for item in tools)


def test_probe_reports_tool_count() -> None:
    service, meta = _service()
    record = _kylab_mcp_record()
    meta.records[record.id] = record

    ok, detail, tools = service.probe(record.id, user_id=None)

    assert ok is True
    assert "连接正常" in detail
    assert len(tools) == len(service.list_tools(record))


def test_probe_reports_failure_without_raising() -> None:
    """连不上时 probe **回一句话**（它是「测试连接」，报错就是它的产出），
    而不是把异常抛给调用方——抛了的话前端只剩一句"请求失败"。"""
    service, meta = _service()
    record = MCPServerRecord(
        id="mcp_bad",
        name="连不上的",
        transport="stdio",
        target="definitely-not-a-real-command-kylab",
        policy="allow",
    )
    meta.records[record.id] = record

    ok, detail, tools = service.probe(record.id, user_id=None)

    assert ok is False
    assert tools == []
    assert detail


def test_list_tools_does_not_return_empty_on_failure() -> None:
    """连不上**报错**，不返回空清单：空清单意味着"这个服务没有工具"，
    而连不上是另一回事——混起来界面会显示"发现 0 个工具"，让人去查错方向。"""
    service, _ = _service()
    record = MCPServerRecord(
        id="mcp_bad", name="x", transport="stdio", target="nope-not-real", policy="allow"
    )

    with pytest.raises(Exception) as excinfo:
        service.list_tools(record)

    assert not isinstance(excinfo.value, AssertionError)


# ------------------------------------------------------------------ 返回解析


def test_flatten_result_keeps_text_only() -> None:
    """非文本内容不往上下文里塞：图片与资源引用该在"文件"那一层处理，
    塞成 base64 只会烧 token。"""
    from app.services.mcp_client import _flatten_result

    class Block:
        def __init__(self, text: str | None) -> None:
            self.text = text

    class Result:
        # 实例属性而不是类属性：类属性上挂可变默认值是 RUF012 明确要拦的写法
        def __init__(self) -> None:
            self.content = [Block("第一段"), Block(None), Block("第二段")]
            self.isError = False

    assert _flatten_result(Result()) == "第一段\n第二段"

    class Empty:
        def __init__(self) -> None:
            self.content: list[object] = []
            self.isError = True

    assert "错误" in _flatten_result(Empty())


def test_tool_dataclass_shape() -> None:
    """``MCPTool`` 是这一层对外的形状，字段少了界面上就显示不全。"""
    tool = MCPTool(name="t", qualified="mcp__s__t", description="d", server_id="1", server_name="s")

    assert (tool.name, tool.qualified, tool.server_name) == ("t", "mcp__s__t", "s")


# ------------------------------------------------------------------ 判定（两处共用）


def _rule_set(**lists: str):  # type: ignore[no-untyped-def]
    from app.services.command_policy import rules_from_runtime

    class _Runtime:
        def get(self, key: str) -> str:
            return lists.get(key, "")

    return rules_from_runtime(_Runtime())


def _service_with(policy: str) -> tuple[MCPClientService, MCPServerRecord]:
    service, meta = _service()
    record = MCPServerRecord(
        id="mcp_1", name="web", transport="stdio", target="python", policy=policy
    )
    meta.records["mcp_1"] = record
    return service, record


def test_decide_allows_when_policy_allows_and_no_rule_matches() -> None:
    """**最常见的部署**：服务设成允许、清单一个字没写 → 就是放行。

    这里踩过一个真坑：规则集没命中时的默认档也是 ``ask``，把它当成"一张命中的
    ask 规则"用，就会让"我已经把它设成允许了"变成"每次还是要确认"。
    判定的依据必须是 ``decision.rule is not None``（真的命中了），而不是档位值。
    """
    service, record = _service_with("allow")

    assert service.decide(record, "search", rules=_rule_set()).action == "allow"


def test_decide_lets_a_matching_rule_outrank_the_service_policy() -> None:
    """规则更具体，所以命中时它说了算——两个方向都要成立。

    能放宽：``ask`` 的服务下，给这一个工具写一条放行规则，它就直接通过
    （不必把整台服务放开，这是最小权限的用法）。
    能收紧：``allow`` 的服务下，一条 ask 规则能让它重新需要确认。
    """
    service, record = _service_with("ask")
    relaxed = _rule_set(**{"sandbox.rules_allow": "mcp__web__search"})
    assert service.decide(record, "search", rules=relaxed).action == "allow"

    service, record = _service_with("allow")
    tightened = _rule_set(**{"sandbox.rules_ask": "mcp__web__search"})
    assert service.decide(record, "search", rules=tightened).action == "ask"


def test_decide_keeps_the_deny_switch_absolute() -> None:
    """服务被设成拒绝时，**一条放行规则也不能把它掀开**。

    否则"我在能力页关掉了它"会被设置页里一条旧规则静默推翻，
    而用户以为自己已经禁掉了——这正是 deny 优先级那条规矩要防的事。
    """
    service, record = _service_with("deny")
    rules = _rule_set(**{"sandbox.rules_allow": "mcp__web__search"})

    assert service.decide(record, "search", rules=rules).action == "deny"


# ------------------------------------------------------------------ 工具清单缓存


def _discovered(monkeypatch: pytest.MonkeyPatch, calls: list[str]):  # type: ignore[no-untyped-def]
    """把"连上去问一次"换成一个计数器（真连接要起子进程，不该在单测里跑）。"""

    def fake(self, record):  # type: ignore[no-untyped-def]
        calls.append(record.name)
        return [MCPTool(name="t", qualified="mcp__web__t", server_name=record.name)]

    monkeypatch.setattr(MCPClientService, "list_tools", fake)


def test_tool_list_is_cached_between_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """**工具清单每轮都要用**，而取它 = 连一次服务。不缓存的话，
    问一句话的成本会变成"起子进程 + 握手"乘上服务数。"""
    calls: list[str] = []
    _discovered(monkeypatch, calls)
    service, record = _service_with("allow")

    assert len(service.cached_tools(record)[0]) == 1
    assert len(service.cached_tools(record)[0]) == 1
    assert calls == ["web"]  # 只连了一次


def test_failure_is_cached_too_but_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """连不上要缓存**失败**而不是空清单，且**不抛异常**给调用方。

    两件事都重要：不缓存失败 → 一个挂掉的服务让每轮对话都白等一个超时；
    抛出异常 → 一个外部服务挂掉会把整张工具表带走。
    """
    state = {"n": 0}

    def boom(self, record):  # type: ignore[no-untyped-def]
        state["n"] += 1
        raise FileNotFoundError("没有这个命令")

    monkeypatch.setattr(MCPClientService, "list_tools", boom)
    service, record = _service_with("allow")

    tools, reason = service.cached_tools(record)

    assert tools == [] and "没有这个命令" in reason
    assert service.cached_tools(record) == (tools, reason)
    assert state["n"] == 1  # 第二次没再连


def test_invalidating_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """改了配置就得重新发现：否则用户改完 target，对话里用的还是旧服务那张表。"""
    calls: list[str] = []
    _discovered(monkeypatch, calls)
    service, record = _service_with("allow")
    service.cached_tools(record)

    service.invalidate(record.id)
    service.cached_tools(record)

    assert calls == ["web", "web"]


def test_available_tools_skips_disabled_and_broken_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**一个人不能因为另一个服务挂了就用不了工具**：坏的服务跳过，停用的不出现。"""
    _discovered(monkeypatch, [])
    service, meta = _service()
    meta.records["mcp_1"] = MCPServerRecord(
        id="mcp_1", name="good", transport="stdio", target="python"
    )
    meta.records["mcp_2"] = MCPServerRecord(
        id="mcp_2", name="off", transport="stdio", target="python", enabled=False
    )
    meta.records["mcp_3"] = MCPServerRecord(
        id="mcp_3", name="dead", transport="stdio", target="python"
    )
    original = MCPClientService.list_tools

    def picky(self, record):  # type: ignore[no-untyped-def]
        if record.name == "dead":
            raise TimeoutError("握手超时")
        return original(self, record)

    monkeypatch.setattr(MCPClientService, "list_tools", picky)

    names = [tool.server_name for _r, tool in service.available_tools(user_id=None)]

    assert names == ["good"]


# ------------------------------------------------------- 并发（v0.27）


def test_two_calls_from_two_threads_each_get_their_own_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一批里的几次外部工具调用会**并发**发出，所以每次调用必须各有一条事件循环。

    这一层能并发的前提就在 `_run` 上：它每次 `asyncio.run`（新建循环、跑完就关），
    而不是大家共用一个。共用的后果不是"慢一点"，是第二次调用直接撞上
    "循环已在运行"——那种错误只有在真并发时才出现，平时看不出来。
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    service, record = _service_with("allow")
    seen: list[str] = []

    async def fake_call(record_, tool, arguments):  # type: ignore[no-untyped-def]
        seen.append(tool)
        await asyncio.sleep(0.15)  # 让两次调用真的重叠
        return f"{tool} 的结果"

    monkeypatch.setattr(service, "_a_call", fake_call)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda tool: service.call(record, tool, {}, approved=True), ["a", "b"])
        )

    assert sorted(results) == ["a 的结果", "b 的结果"]
    assert sorted(seen) == ["a", "b"]
