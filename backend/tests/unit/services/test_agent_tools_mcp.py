"""外部 MCP 服务接进对话工具表（v0.20）。

镜像同构：``app/services/agent_tools.py`` 的 ``tool_specs`` / ``_call_mcp`` → 本文件。

这一组盯的是**四件容易做错的事**，它们各自都能让"接了外部服务"变成一句空话
或者一个坑：

1. **工具表里真的出现它们**（不然模型永远不知道有这个能力）；
2. **别人登记的服务的工具不会出现在我的对话里**（归属收口）；
3. **准入策略在调用那一刻真的拦得住**——尤其是默认档 ``ask``：
   不能因为"对话里没法弹确认框"就悄悄放行；
4. **一个服务连不上不该让整张表消失**（接外部依赖最不该有的耦合）。

命名（前缀、下划线折叠、工具名里带 ``__``）在 ``test_mcp_client.py`` 里测，
这里只测"工具表与执行器怎么用它"。
"""

from __future__ import annotations

import pytest

from app.services.agent_tools import MAX_MCP_RESULT_CHARS, _call_mcp, build_runner, tool_specs
from app.services.api_key import Caller
from app.services.mcp_client import MCPClientService, MCPTool
from app.services.tool_loop import tool_label
from app.storage.base import MCPServerRecord, UserRecord


class _FakeMeta:
    """只够 ``MCPClientService`` 用的一小撮存储方法（它不是本文件的测试对象）。"""

    def __init__(self, records: list[MCPServerRecord] | None = None) -> None:
        self.records: dict[str, MCPServerRecord] = {item.id: item for item in records or []}

    def get_mcp_server(self, server_id: str) -> MCPServerRecord | None:
        return self.records.get(server_id)

    def list_mcp_servers(self) -> list[MCPServerRecord]:
        return list(self.records.values())


class _FakeBundle:
    def __init__(self, meta: _FakeMeta) -> None:
        self.meta = meta


class _FakeRuntime:
    """运行期配置的假形状。**方法名要与真的对齐**（``get`` / ``get_bool`` / ``get_int``）：
    只填一半的话，需要另一个方法的那条路径会在测试里抛 AttributeError，
    而那看起来像"被测代码坏了"。"""

    def __init__(self, **values: object) -> None:
        self._values = values

    def get(self, key: str) -> str:
        return str(self._values.get(key, ""))

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self._values.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        value = self._values.get(key, default)
        return int(value) if isinstance(value, (int, str)) else default


class _FakeServices:
    """执行器只用到 ``mcp`` / ``runtime`` / ``skills`` 三样。"""

    def __init__(
        self,
        records: list[MCPServerRecord],
        *,
        runtime_values: dict[str, object] | None = None,
    ) -> None:
        self.mcp = MCPClientService(_FakeBundle(_FakeMeta(records)))  # type: ignore[arg-type]
        self.runtime = _FakeRuntime(**(runtime_values or {}))


def _record(
    name: str = "web",
    *,
    policy: str = "ask",
    owner_id: str | None = None,
    enabled: bool = True,
    rid: str = "mcp_1",
) -> MCPServerRecord:
    return MCPServerRecord(
        id=rid,
        name=name,
        transport="stdio",
        target="python",
        policy=policy,
        enabled=enabled,
        owner_id=owner_id,
    )


def _tool(name: str, server: str = "web", **kwargs: object) -> MCPTool:
    from app.services.mcp_client import tool_qualified_name

    qualified = tool_qualified_name(server, name)
    extra = dict(kwargs)
    return MCPTool(
        name=name,
        qualified=qualified,
        description=str(extra.pop("description", "搜一下")),
        server_id=str(extra.pop("server_id", "mcp_1")),
        server_name=server,
        schema=extra.pop("schema", {"type": "object", "properties": {"q": {"type": "string"}}}),  # type: ignore[arg-type]
    )


def _discover(monkeypatch, mapping: dict[str, object]) -> None:
    """把"连上去问工具清单"这一步换掉（那是真网络/真子进程，不该在单测里跑）。"""

    def fake_list_tools(self, record):  # type: ignore[no-untyped-def]
        value = mapping.get(record.name, [])
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(MCPClientService, "list_tools", fake_list_tools)


def _member(user_id: str = "u1") -> Caller:
    return Caller(user=UserRecord(id=user_id, name=user_id, username=user_id, password_hash="x"))


# ------------------------------------------------------------------ 工具表


def test_plain_specs_are_builtin_and_skills_only() -> None:
    """不给 services 时行为与 P0 一致：内置 + 技能，**一个外部工具都没有**。

    这条是给单测与纯规格检查留的入口，也保证"没接外部服务"的部署拿到的表不变。
    **不传 `kb_ids` 就没有知识库那一侧的工具**（见下一条），这里用一个库来
    看完整的表。
    """
    names = [spec.name for spec in tool_specs(kb_ids=["kb_1"])]

    assert "search" in names
    assert "list_skills" in names
    assert not [name for name in names if name.startswith("mcp__")]


def test_knowledge_tools_disappear_when_the_switch_is_off() -> None:
    """关掉知识库开关（这一轮没有可查的库）→ **知识库那一侧的工具一个都不给**。

    改之前只是"检索会被拒绝"，工具照给：于是模型每轮都先 `list_knowledge_bases`
    看一眼、再 `search` 一次拿到"这一轮没有可查的知识库"——两个来回白花
    （用户报的"没开知识库，但每轮都去知识库检索"）。**给了又拒，不如不给。**

    边界只画在知识库上：记忆（recall / remember）与笔记（create_note / list_notes）
    不属于这一侧，用户点名说过"这里的知识库不包括 agent 记忆"。
    """
    names = {spec.name for spec in tool_specs(kb_ids=[])}

    for gone in (
        "search",
        "list_knowledge_bases",
        "list_documents",
        "get_document_status",
        "delete_document",
        "upload_document",
        "add_data_source",
        "create_knowledge_base",
        "attach_note_to_kb",
        "ingest_artifact",
    ):
        assert gone not in names, f"{gone} 不该在关掉知识库时出现"
    # 记忆与笔记照旧；联网与技能也不受影响
    assert {"recall", "remember", "create_note", "list_notes", "web_search", "list_skills"} <= names


def test_no_kb_ids_at_all_is_treated_as_no_knowledge_base() -> None:
    """**不传**（单测、脚本）按"没有知识库"处理——与 ``build_runner`` 同一口径：
    没有范围就是查不了，那么工具表里也不该有它。"""
    names = {spec.name for spec in tool_specs()}

    assert "search" not in names


def test_external_tools_join_the_table_with_their_schema(monkeypatch) -> None:
    """外部工具进表，并且**带上它的参数定义**。

    参数定义缺了的话，模型只知道"有这个工具"、不知道要传什么，每次调用都靠猜
    ——那是"接进来了但用不起来"最常见的样子。
    """
    _discover(monkeypatch, {"web": [_tool("search_web")]})
    services = _FakeServices([_record()])

    specs = {spec.name: spec for spec in tool_specs(services)}

    assert "mcp__web__search_web" in specs
    spec = specs["mcp__web__search_web"]
    assert spec.parameters == {"type": "object", "properties": {"q": {"type": "string"}}}
    # 描述要带服务名：平铺的表里，光看工具名说不清它是谁的能力
    assert "web" in spec.description


def test_a_broken_service_does_not_take_the_whole_table_down(monkeypatch) -> None:
    """一个服务连不上，**其余照常在**——这是"接外部依赖"最不该有的耦合。"""
    _discover(
        monkeypatch,
        {"web": [_tool("search_web")], "dead": FileNotFoundError("没有这个命令")},
    )
    services = _FakeServices([_record(rid="mcp_1"), _record(name="dead", rid="mcp_2")])

    names = [spec.name for spec in tool_specs(services)]

    assert "mcp__web__search_web" in names
    assert not [name for name in names if name.startswith("mcp__dead")]


def test_disabled_service_is_not_offered(monkeypatch) -> None:
    """停用的服务不该出现在工具表里——"停用"是用户表达"别用它"的方式。"""
    _discover(monkeypatch, {"web": [_tool("search_web")]})
    services = _FakeServices([_record(enabled=False)])

    assert not [s.name for s in tool_specs(services) if s.name.startswith("mcp__")]


def test_other_peoples_tools_stay_out(monkeypatch) -> None:
    """**归属收口**：甲登记的服务不出现在乙的工具表里。

    不拦的话，"别人接的外部服务在我说话时被调起来"就会发生，而界面上完全看不出来。
    """
    _discover(monkeypatch, {"web": [_tool("search_web")]})
    services = _FakeServices([_record(owner_id="u1")])

    assert [s.name for s in tool_specs(services, owner_id="u1")]  # 自己看得到
    assert not [s.name for s in tool_specs(services, owner_id="u2") if s.name.startswith("mcp__")]


# ------------------------------------------------------------------ 准入策略


def test_ask_policy_does_not_execute_and_says_how_to_open_it() -> None:
    """**默认档 ``ask`` 不执行**，并给出可操作的那一行。

    对话这条链路没有"弹确认框"的往返（原因见 ``_call_mcp`` 的说明），
    所以这一档的正确行为是"这轮用不了 + 说清怎么打开"，
    而不是"反正问不了就放行"——那等于外部服务默认可以拿用户的名义做事。
    """
    services = _FakeServices([_record(policy="ask")])
    calls: list[tuple] = []
    services.mcp.call = lambda *a, **k: calls.append((a, k)) or "不该发生"  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {"q": "x"}, owner_id=None)

    assert calls == []
    assert "没有执行" in outcome.content
    assert "mcp__web__search_web" in outcome.content  # 给出可以直接粘进清单的那一行
    assert outcome.summary == "需要先确认，本轮没执行"


def test_allow_policy_executes_and_marks_approved() -> None:
    """``allow`` 真的调起来，并以"已获准"的形态过服务层的闸（不是绕过它）。"""
    services = _FakeServices([_record(policy="allow")])
    seen: dict[str, object] = {}

    def fake_call(record, tool, args, *, approved=False):  # type: ignore[no-untyped-def]
        seen.update({"tool": tool, "args": args, "approved": approved})
        return "搜到三条结果"

    services.mcp.call = fake_call  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {"q": "x"}, owner_id=None)

    assert seen == {"tool": "search_web", "args": {"q": "x"}, "approved": True}
    assert outcome.content == "搜到三条结果"
    assert outcome.summary == "web 的 search_web 返回了结果"


def test_deny_policy_refuses_and_explains() -> None:
    """``deny`` 不执行，理由给模型（它据此换路，而不是反复重试同一个工具）。"""
    services = _FakeServices([_record(policy="deny")])
    services.mcp.call = lambda *a, **k: pytest.fail("拒绝档不该调用")  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert "被拒绝调用" in outcome.content
    assert outcome.summary == "被策略拒绝"


def test_rules_can_relax_an_ask_service() -> None:
    """规则清单比服务的策略档更具体，所以它能放宽（用户写出 ``mcp__web__*`` 就是那个意思）。"""
    services = _FakeServices(
        [_record(policy="ask")],
        runtime_values={"sandbox.rules_allow": "mcp__web__search_web"},
    )
    services.mcp.call = lambda *a, **k: "放行了"  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert outcome.content == "放行了"


def test_an_empty_rule_list_does_not_force_approval() -> None:
    """清单是空的时候，"允许"就是允许。

    规则集**没命中**时的默认档也是 ``ask``（那是它的总开关语义），
    但如果把它当"一张 ask 规则"用，最常见的部署（服务设成允许、清单一个字没写）
    会每次都要求确认——用户明明已经放行了。这一条是那个 bug 的回归测试。
    """
    services = _FakeServices([_record(policy="allow")], runtime_values={})
    services.mcp.call = lambda *a, **k: "直接调了"  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert outcome.content == "直接调了"


def test_a_matching_ask_rule_tightens_an_allow_service() -> None:
    """反过来：一条显式命中的 ask 规则能把"允许"的服务收得更紧（规则更具体，命中时它说了算）。"""
    services = _FakeServices(
        [_record(policy="allow")],
        runtime_values={"sandbox.rules_ask": "mcp__web__search_web"},
    )
    services.mcp.call = lambda *a, **k: pytest.fail("命中 ask 规则之后不该调用")  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert outcome.summary == "需要先确认，本轮没执行"


def test_a_deny_rule_beats_an_allow_service() -> None:
    """**deny 永远优先**：服务档位调到"允许"也不该把一条显式的拒绝规则盖掉。"""
    services = _FakeServices(
        [_record(policy="allow")],
        runtime_values={"sandbox.rules_deny": "mcp__web__search_web"},
    )
    services.mcp.call = lambda *a, **k: pytest.fail("被拒绝规则拦住之后不该调用")  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert outcome.summary == "被策略拒绝"


def test_call_failure_comes_back_as_text() -> None:
    """调用失败**如实回到循环**，不抛出去把整轮打掉。"""
    services = _FakeServices([_record(policy="allow")])

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("握手超时")

    services.mcp.call = boom  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert "握手超时" in outcome.content


def test_long_results_are_clipped_and_said_so() -> None:
    """超长结果截断并**明确说明**：悄悄截断会让模型以为"这就是全部"。"""
    services = _FakeServices([_record(policy="allow")])
    services.mcp.call = lambda *a, **k: "字" * (MAX_MCP_RESULT_CHARS + 100)  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id=None)

    assert "已截断" in outcome.content
    assert len(outcome.content) < MAX_MCP_RESULT_CHARS + 200


# ------------------------------------------------------------------ 找不到/认不出


def test_unknown_server_is_reported_not_guessed() -> None:
    """没登记（或不属于我）的服务：**明说用不了**，不去猜一个近似的服务来调。"""
    services = _FakeServices([_record(owner_id="u1")])
    services.mcp.call = lambda *a, **k: pytest.fail("找不到的服务不该被调用")  # type: ignore[method-assign]

    outcome = _call_mcp(services, "mcp__web__search_web", {}, owner_id="u2")

    assert "没有找到启用的 MCP 服务「web」" in outcome.content


def test_malformed_qualified_name_is_refused() -> None:
    """名字不合法就明说——**不要**把它当内置工具去查。"""
    services = _FakeServices([])

    assert "认不出" in _call_mcp(services, "mcp__web", {}, owner_id=None).content


# ------------------------------------------------------------------ 与循环/界面的接缝


def test_runner_routes_mcp_names_to_the_gate(monkeypatch) -> None:
    """执行器按前缀分流：``mcp__`` 走外部那条路，内置工具不受影响。

    顺带盯住归属：**成员只能调自己登记的服务**（记录挂在 u1 名下）。
    """
    _discover(monkeypatch, {"web": [_tool("search_web")]})
    services = _FakeServices([_record(policy="allow", owner_id="u1")])
    services.mcp.call = lambda *a, **k: "外部结果"  # type: ignore[method-assign]
    runner = build_runner(services, _member("u1"))

    assert runner("mcp__web__search_web", {"q": "x"}).content == "外部结果"


def test_step_label_is_readable_for_external_tools() -> None:
    """过程面板要给人看：限定名是协议层的东西，用户想知道的是"哪个服务的哪个工具"。"""
    assert tool_label("mcp__web__search_web") == "外部工具：web · search_web"
    # 内置工具的标签不受影响（它们是常用的一批，标签更短）
    assert tool_label("search") == "检索知识库"
    # 认不出来的名字**原样显示**，不隐藏它：界面上少一步比多一步更误导
    assert tool_label("weird") == "weird"


# ------------------------------------------------------------------ 步骤摘要


def test_step_summary_handles_the_bare_list_shape() -> None:
    """`list_knowledge_bases` 返回的是**裸 list**，摘要也要认得。

    实测报过来的现象：过程面板里那一行显示的是原始 JSON
    （`[{"id": "kb_...", "name": "城市建成环境研究现状", "documents": 23…`）。
    原因是摘要只处理了 dict 里的 `items`，而工具返回的是 list——
    于是回退路径把结构化结果原样倒给了用户。
    """
    from app.services.agent_tools import _summary

    payload = [
        {"id": "kb_1", "name": "城市建成环境研究现状", "documents": 23},
        {"id": "kb_2", "name": "另一个库", "documents": 2},
    ]

    assert _summary("list_knowledge_bases", payload) == "共 2 个知识库"
    # 其余裸列表也兜住（"共 N 条"总比一串 JSON 强）
    assert _summary("list_notes", [{}, {}]) == "共 2 条"
    # **具体的措辞不能被兜底那条抢掉**
    assert _summary("recall", {"items": [1, 2, 3]}) == "查到 3 条变更"
    assert _summary("list_documents", {"total": 23}) == "共 23 篇文档"


def test_step_summary_never_dumps_raw_json() -> None:
    """过程面板那一行**不能是 JSON**（v0.26 用户报的"工具调用的 UI 排版很有问题"）。

    实测那条会话里，`导出幻灯` 与 `记住` 两步的结论是两段原始 JSON
    （`{"artifact_id": "art_89cb…", "name": …}`），在面板里占了好几行。
    根因是 `_summary` 只给"结果里有条数"的那几个写了摘要，其余**回退到结果开头**——
    而那些工具回的正是 dict。
    """
    from app.services.agent_tools import _summary

    assert _summary(
        "export_deck", {"artifact_id": "art_1", "name": "攻略.pptx", "size_bytes": 46153}
    ) == ("已生成「攻略.pptx」（45 KB）")
    assert (
        _summary("export_document", {"name": "方案.docx", "size_bytes": 2048})
        == "已生成「方案.docx」（2 KB）"
    )
    assert _summary("remember", {"action": "added", "receipt": "记下了：……"}) == "已记进档案"
    assert _summary("remember", {"action": "replaced"}) == "已更正档案里的一条"
    assert _summary("remember", {"action": "existing"}) == "档案里已经有了"
    assert _summary("remember", {"action": "rejected"}) == "档案没收下这条"
    assert _summary("forget", {"action": "forgotten"}) == "已从档案里删掉"
    assert _summary("forget", {"action": "rejected"}) == "档案里没有这一条"
    assert (
        _summary("create_note", {"note_id": "n1", "title": "会议纪要"}) == "已存为笔记「会议纪要」"
    )
    assert _summary("upload_document", {"name": "报表.xlsx", "is_duplicate": True}) == (
        "已上传「报表.xlsx」（库里已有同样的内容）"
    )
    assert _summary("list_notes", {"total": 3, "notes": []}) == "共 3 条笔记"
    assert _summary("search", {"query": "眼轴", "hits": [{}, {}]}) == "命中 2 段原文"
    assert _summary("search", {"query": "眼轴", "hits": []}) == "没有命中任何片段"
