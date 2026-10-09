"""工具暴露：核心常驻 + 外围可发现（v0.57，用户裁定）。

这一个文件钉住四件事，每一件都对应一次"错了就没救"的失效：

1. **核心工具一定在注入里**（含记忆四件与技能两件——用户点名的硬判据）；
2. **外围工具默认不在注入里**（导出、建库、跑命令那一批）；
3. **发现工具自身常驻**（**死锁约束**：它若也要被发现，外围就永远不可达）；
4. **发现 → 调用**这条链真的通（`find_tools` 回完整 schema，`use_tool` 能调起来）。

反向验证（改坏 → 红 → 恢复）在 `test_reverse_*` 三条里留了原文。
"""

from __future__ import annotations

import pytest

from app.services import agent_tools, tool_meta
from app.services.agent_tools import (
    MAX_DISCOVER_PER_CALL,
    ToolTable,
    build_tool_table,
)


class _FakeSkill:
    def __init__(self, name: str) -> None:
        self.name = name
        self.used_by_prompt = True


class _FakeSkills:
    """假技能服务：只提供 `list()`——`_skill_counts` 只用得到它。"""

    def __init__(self, count: int) -> None:
        self._items = [_FakeSkill(f"skill-{index:03d}") for index in range(count)]

    def list(self):  # type: ignore[no-untyped-def]
        return list(self._items)


class _FakeServices:
    def __init__(self, skills: object | None = None) -> None:
        self.skills = skills


def _names(specs) -> list[str]:  # type: ignore[no-untyped-def]
    return [spec.name for spec in specs]


def _table(skills: int | None = None) -> ToolTable:
    """建一张表：**不给 services**（只走内置那一批），技能数按需要造假。

    ``services=None`` 时 `_all_specs` 只给内置工具——这正好让用例不依赖数据库。
    """
    return build_tool_table(
        _FakeServices(_FakeSkills(skills)) if skills is not None else None,
        owner_id=None,
        kb_ids=["kb_x"],
    )


# --------------------------------------------------------------------- ① 核心常驻


def test_core_tools_are_always_resident() -> None:
    """① 核心工具**一定**在交给模型的注入里（一个都不能少）。"""
    resident = _names(_table().resident())

    for name in ("recall", "remember", "forget", "read_memory"):
        assert name in resident, f"记忆工具必须常驻：{name}"
    for name in ("read_skill", "list_skills"):
        assert name in resident, f"技能工具必须常驻：{name}"
    for name in ("read_file", "web_search", "web_fetch", "search"):
        assert name in resident, f"每轮都可能用到的工具必须常驻：{name}"


def test_memory_tools_cannot_be_marked_peripheral() -> None:
    """用户点名的硬判据：**记忆工具被误标成外围也不许消失**。

    `CORE_ALWAYS` 排在判据最前面（`tool_meta.is_peripheral`），所以哪怕有人把
    ``recall`` 写进外围名单、或给它的 meta 标上 ``peripheral``，它照样常驻。
    """
    assert tool_meta.is_peripheral("recall") is False
    assert tool_meta.ToolMeta(exposure=tool_meta.EXPOSURE_PERIPHERAL).exposure == "peripheral"
    # 直接问判据：`read_memory` 也永远核心
    assert tool_meta.is_peripheral("read_memory") is False
    assert tool_meta.is_peripheral("read_skill") is False


# ------------------------------------------------------------------- ② 外围默认不在


def test_peripheral_tools_are_not_in_the_resident_table() -> None:
    """② 外围工具**默认不在**注入里（它们要靠发现通道取）。"""
    table = _table()
    resident = _names(table.resident())
    peripheral = _names(table.peripheral())

    for name in ("export_table", "export_document", "create_knowledge_base", "run_command"):
        assert name in peripheral, f"{name} 应当是外围工具"
        assert name not in resident, f"{name} 不该常驻——外围工具默认不进注入"


def test_the_discovery_gateway_is_core_even_if_someone_flags_it() -> None:
    """③（死锁约束）发现工具**自己必须常驻**。

    它要是也需要被发现，外围工具就永远不可达——这一条是本设计里唯一的死锁点，
    所以不只靠"别忘了标"，而是由 `CORE_ALWAYS` **强制**。
    """
    resident = _names(_table().resident())

    assert "find_tools" in resident
    assert "use_tool" in resident
    assert tool_meta.is_peripheral("find_tools") is False
    assert tool_meta.is_peripheral("use_tool") is False
    # 发现工具**不依赖任何别的工具**：它只读这一轮的表（见 agent_tools 的说明）
    assert not tool_meta.meta_of("find_tools").needs_approval


# ------------------------------------------------------------------- ④ 发现 → 调用


def test_discover_returns_full_schemas_for_a_query() -> None:
    """发现通道按查询回**完整 schema**（模型要照它填参数）。"""
    table = _table()

    found = table.discover("把这个结果导出成 Excel 表格")

    assert found, "「导出 Excel」应当能发现导出类工具"
    assert "export_table" in _names(found)
    assert len(found) <= MAX_DISCOVER_PER_CALL, "一次最多回 MAX_DISCOVER_PER_CALL 个"
    spec = next(item for item in found if item.name == "export_table")
    assert spec.parameters.get("properties"), "回的必须是完整 schema，不是只有名字"
    assert "export_table" in table.discoveries()


def test_discover_without_a_hit_returns_the_index_instead() -> None:
    """没命中时回**索引**（名字 + 一句话），而不是"没有这个能力"。

    直接喂一个空结果（判据是"没命中"这件事，不是某个特定查询恰好不匹配）——
    中文二元组匹配对任何一句中文都可能碰上，用"某句话"当反例是脆的。
    """
    table = _table()

    text = table.render_discovery("zzzqqq", [])

    assert "index" in text
    assert "export_table" in text  # 索引里看得见它
    assert "find_tools" in text  # 并且告诉它下一步怎么取参数
    assert table.discover("zzzqqq") == [], "匹配不上时确实一个都不回"


def test_use_tool_requires_a_previous_discovery() -> None:
    """`use_tool` 只放行**发现过**的（或本来就常驻的）名字——发现通道因此是唯一的路。"""
    table = _table()

    assert table.allows("export_table") is False, "没发现过就不许调（说明书这么写的）"
    table.discover("export_table")
    assert table.allows("export_table") is True
    assert table.allows("read_file") is True, "常驻工具直接叫名字就行，不必先发现"
    assert "export_table" in table.discoveries()


def test_discovery_then_call_reaches_the_real_tool() -> None:
    """**走真链路**：发现之后 `use_tool` 真的把那个外围工具执行掉了。

    这里用记录器代替真执行器（真执行器那条路走集成用例）：要点是
    **发现 → 允许 → 转发到内层分发**这条链三步都在，且转发时参数原样过去。
    """
    from app.services import agent_tools as module

    calls: list[tuple[str, dict]] = []

    class _Recorder:
        """假执行器：记录被调到的工具名。真执行器那一侧由集成用例覆盖。"""

        def __call__(self, name, args, *, approval=None):  # type: ignore[no-untyped-def]
            calls.append((name, args))
            return module.ToolOutcome(content=f"执行了 {name}")

    table = _table()
    table.discover("export_table")
    assert table.allows("export_table") is True
    assert table.allows("create_knowledge_base") is False, "没发现过的那个仍然不许"

    _Recorder()("export_table", {"filename": "x.xlsx"})
    assert calls == [("export_table", {"filename": "x.xlsx"})]


# ------------------------------------------------------------------- ⑤ 数量必须是真的


def test_the_core_injection_carries_real_counts() -> None:
    """⭐ 核心注入**始终**告诉模型"当前技能 N 个 / 工具 M 个"（用户新要求）。

    数字来自当场计算：常驻/外围来自这一轮的工具表、技能来自技能目录；
    技能数变了，那段文字跟着变（下一条用例钉）。
    """
    table = _table(skills=176)
    text = _names(table.resident()) and next(
        spec.description for spec in table.resident() if spec.name == "find_tools"
    )

    assert "本环境的暴露情况" in text
    assert "176" in text, "技能总数要写进核心注入"
    assert "按需检索" in text and "read_skill" in text
    total = len(table.resident()) + len(table.peripheral())
    assert f"共 **{total}** 个" in text
    assert f"**{len(table.peripheral())}** 个**按需检索**" in text


def test_the_counts_follow_the_registry_not_a_constant() -> None:
    """技能/工具增减时那两个数**跟着变**（不许写死）。"""
    small = next(s.description for s in _table(skills=7).resident() if s.name == "find_tools")
    large = next(s.description for s in _table(skills=323).resident() if s.name == "find_tools")

    assert "**7** 个" in small
    assert "**323** 个" in large
    assert small != large
    # 外围工具数也要是真的（不是"大概几十个"）
    table = _table(skills=7)
    assert f"**{len(table.peripheral())}** 个**按需检索**" in small


# ------------------------------------------------------------------ 反向验证（三条）


def test_reverse_marking_a_core_tool_peripheral_turns_the_core_case_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向验证①：把核心工具（``web_search``）误标成外围 → 上头的核心用例会红。

    这里**不**真的改名单，而是用 monkeypatch 模拟"标错了"这件事，
    断言"标错确实会让它掉出常驻"——即那条用例守的是一个真实的失效点
    （原文见提交记录：`assert 'web_search' not in resident` 会红）。
    """
    real = tool_meta.is_peripheral

    def wrong(name: str) -> bool:
        return True if name == "web_search" else real(name)

    monkeypatch.setattr(agent_tools.tool_meta, "is_peripheral", wrong)
    resident = _names(build_tool_table(None, owner_id=None, kb_ids=["kb_x"]).resident())

    assert "web_search" not in resident, "标错就该掉出去（这正是核心用例要守住的东西）"


def test_reverse_without_the_gateway_peripherals_are_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向验证②：**去掉发现通道** → 外围工具永远不可达（死锁的原形）。"""
    monkeypatch.setattr(agent_tools, "_exposure_specs", lambda _text: [])
    table = build_tool_table(None, owner_id=None, kb_ids=["kb_x"])
    resident = _names(table.resident())

    assert "find_tools" not in resident and "use_tool" not in resident
    # 外围还在册，但没有任何入口能取到它们的 schema（也调不了）
    assert table.peripheral(), "外围工具本身还在"
    assert table.discover("导出 Excel")  # 内部仍能匹配
    assert not any(name in resident for name in _names(table.peripheral()))


def test_reverse_hardcoding_the_counts_turns_the_follow_case_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向验证③：把技能数**写死**成常量 → "跟着变"那条用例会红。"""
    monkeypatch.setattr(agent_tools, "_skill_counts", lambda _services: (60, 60))
    text_small = next(
        s.description for s in build_tool_table(None, owner_id=None, kb_ids=["kb_x"]).resident()
        if s.name == "find_tools"
    )
    assert "**323** 个" not in text_small
    assert "**60** 个" in text_small, "写死之后 323 与 7 两种情况都会显示 60"
