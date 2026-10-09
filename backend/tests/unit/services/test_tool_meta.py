"""工具元数据表（v0.42，抄 ZCode 的六字段 + DSH 的 fail-closed 分组）。

这一组的价值在**防漏**：表里少一个工具，那条路就是"按独占算"（慢，但不坏数据），
所以漏了不会报错——只会在某天有人问"为什么这批突然不并发了"。
于是用一条门禁把口子封死：**内置工具一个都不能漏**。
"""

from __future__ import annotations

import pytest

from app.services.agent_tools import _LOCAL_TOOLS, _MEMORY_TOOLS, _SKILL_TOOLS
from app.services.tool_meta import TOOL_META, meta_of, parallel_groups
from app.services.tools import TOOL_NAMES


def _builtin_names() -> set[str]:
    """内置工具名：MCP 那批（``TOOL_NAMES``）+ 技能那批（``_SKILL_TOOLS``）+
    这台机器上那批（``_LOCAL_TOOLS``）+ 记忆那批（``_MEMORY_TOOLS``）。

    **四段都要，漏一段门禁就有洞**：加 ``_MEMORY_TOOLS`` 时这处没跟上，
    ``read_memory`` 就掉进了 fail-closed 的"未知工具"档——策略上被当成
    "动整台机器"（独占、risk=high），显示上被画成中性图标。两处都不是报错，
    只是安静地不对，所以这条门禁必须把新分组一起收进来。
    """
    return (
        set(TOOL_NAMES)
        | {str(item["name"]) for item in _SKILL_TOOLS}
        | {str(item["name"]) for item in _LOCAL_TOOLS}
        | {str(item["name"]) for item in _MEMORY_TOOLS}
    )


def test_every_builtin_tool_declares_its_metadata() -> None:
    """**内置工具一个都不能漏**（新加工具时必须来这张表上声明一次）。

    漏掉的后果不是报错，而是它被当成"独占"——那是安全的默认，
    但"为什么这个工具不并发"会变成没人答得上来的问题。
    """
    missing = sorted(_builtin_names() - set(TOOL_META))
    assert missing == [], f"这些内置工具没有声明元数据：{missing}"


def test_the_table_has_no_ghosts() -> None:
    """反过来也要查：表里不该有已经不存在的工具名（改名后忘了删）。

    **两个例外：暴露网关自己发的那两个**（`find_tools` / `use_tool`）。
    它们**刻意不在** `tool_specs()` 里——网关把"发现通道"（`find_tools`）与
    "转发通道"（`use_tool`）从常规清单里摘出去、由网关那一支单独发出去
    （见 `tool_meta.py` 里那两条的注释与 `agent_tools` 的"暴露：核心常驻 + 外围可发现"一节）。
    所以判据是"**既不在内置清单、也不是网关发的那两个**"，而不是简单的差集：
    简单差集会把一个**设计决定**报成"改名忘删" ✗。

    为什么仍要保留这条用例：别的确有幽灵的风险（改名后忘删元数据、删工具留元数据）——
    只是这两个名字属于被网关接管的例外，删它们的元数据会**弄坏网关那一侧** ✗。
    """
    gateway_issued = {"find_tools", "use_tool"}
    ghosts = sorted(set(TOOL_META) - _builtin_names() - gateway_issued)
    assert ghosts == [], f"表里有已经不存在的工具：{ghosts}"


def test_the_gateway_exception_stays_a_deliberate_one() -> None:
    """把那个例外钉住：网关那两个字**不得**回到内置清单里（那会破坏暴露设计 ✗）。

    这条同时挡住"顺手把它们加回 `tool_specs()` 让上面那条变绿"这种修法 ✗。
    """
    from app.services.tool_meta import TOOL_META as meta_table

    assert "find_tools" in meta_table and "use_tool" in meta_table
    assert "find_tools" not in _builtin_names()
    assert "use_tool" not in _builtin_names()


def test_values_are_from_the_agreed_vocabulary() -> None:
    """取值只能来自约定的词表（照 ZCode 的枚举）——拼错了会被当成"另一个档"。"""
    from app.services.tool_meta import RISKS, SCOPES

    for name, meta in TOOL_META.items():
        scope = meta.side_effect_scope
        assert scope in SCOPES, f"{name} 的影响面取值非法：{scope}"
        assert meta.risk_level in RISKS, f"{name} 的风险分级非法：{meta.risk_level}"


def test_reading_tools_are_the_ones_that_parallelize() -> None:
    """**只读 + 无副作用**的才并发；写类与执行类一律独占。

    这是这张表存在的全部理由：v0.41 之前整批一律并发，
    而"导出文档 + 写笔记 + 上传"并发就是在赌它们之间没有共享状态。
    """
    parallel = {name for name, meta in TOOL_META.items() if meta.parallel}
    assert "search" in parallel
    assert "web_search" in parallel
    assert "read_file" in parallel
    for name in (
        "create_note",
        "delete_document",
        "upload_document",
        "run_command",
        "remember",
        "forget",
    ):
        assert name not in parallel, f"{name} 会改东西，不该并发"
    # 联网那两个**影响面是 network 而不是 none**，但它们是只读的：
    # 这里刻意允许（照 ZCode 的判定，一次网络读不会改任何东西）
    assert meta_of("web_fetch").read_only is True


def test_an_unknown_tool_falls_back_to_the_conservative_side() -> None:
    """未知工具（外部 MCP、以后新加的）**按独占算**：fail-closed。"""
    meta = meta_of("mcp__whatever__do_something")
    assert meta.parallel is False
    assert meta.concurrent_safe is False
    assert meta.side_effect_scope == "system"
    assert meta.risk_level == "high"


def test_run_command_always_needs_approval() -> None:
    """执行命令在元数据这一层就写着"总是要问"——策略之外的第二道（照 ZCode 的 Bash）。"""
    meta = meta_of("run_command")
    assert meta.needs_approval is True
    assert meta.destructive is True
    assert meta.side_effect_scope == "system"
    assert meta.parallel is False


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        # 三条只读连成一组
        (["read_file", "read_file", "read_file"], [[0, 1, 2]]),
        # 独占的在中间：前后各成一组，它自己是屏障
        (["read_file", "create_note", "read_file"], [[0], [1], [2]]),
        # 混排：两条读并发 → 一条写独占 → 两条读并发
        (
            ["web_fetch", "read_file", "run_command", "read_file", "web_fetch"],
            [[0, 1], [2], [3, 4]],
        ),
        # 未知工具也是独占，两个未知之间不并发
        (["mcp__a__x", "mcp__a__x"], [[0], [1]]),
    ],
)
def test_grouping_shape(names: list[str], expected: list[list[int]]) -> None:
    """分组的形状（组序 = 调用序，组内可并发，独占各成一组）。"""
    assert parallel_groups(names) == expected


# --------------------------------------------------------- 语义种类（P2-1，界面用）


def test_every_builtin_tool_has_a_kind_from_the_vocabulary() -> None:
    """种类是**界面渲染的唯一依据**（P2-1，照 ZCode 的（kind, status, input, output））。

    取值必须落在词表里：拼错一个（``reads``）在界面上只表现为"那一行退回中性图标"，
    不报错、不崩——正是那种没人会注意到的坏。
    """
    from app.services.tool_meta import KINDS, kind_of

    for name in _builtin_names():
        assert kind_of(name) in KINDS, f"{name} 的种类不在词表里：{kind_of(name)}"


def test_two_tools_of_the_same_kind_are_the_same_kind() -> None:
    """**同一类的两个工具，种类必须一样**——界面按种类画，于是它们长得一样。

    这是 P2-1 的验收①：卡片的样子不再取决于工具名。
    """
    from app.services.tool_meta import kind_of

    # 读文件 / 看笔记 / 查表格：都是"看一眼"，画同一张卡
    assert {kind_of(name) for name in ("read_file", "list_notes", "query_table")} == {"read"}
    # 写笔记 / 上传文档：都是"往里写"
    assert {kind_of(name) for name in ("create_note", "upload_document")} == {"write"}
    # 检索知识库 / 联网搜索 / 抓网页：都是"找东西"
    assert {kind_of(name) for name in ("search", "web_search", "web_fetch")} == {"search"}


def test_the_kind_is_derived_from_the_policy_fields() -> None:
    """推导规则对得上那三个字段（不然"从元数据推"就只是一句口号）。"""
    from app.services.tool_meta import kind_of, meta_of

    # 动整台机器的 = 执行；删除类最显眼；写读按影响面与 destructive 分
    assert kind_of("run_command") == "exec"
    assert meta_of("run_command").side_effect_scope == "system"
    assert kind_of("delete_document") == "delete"
    assert meta_of("delete_document").destructive is True
    assert kind_of("upload_document") == "write"
    assert meta_of("upload_document").read_only is False
    assert kind_of("read_file") == "read"
    assert meta_of("read_file").read_only is True
    # 动的是这一轮的会话（子 Agent），不是工作区
    assert kind_of("spawn_subagent") == "session"


def test_an_unknown_tool_is_drawn_neutrally_not_as_exec() -> None:
    """未知工具（外部 MCP）：策略上 fail-closed，**显示上不能画成"执行"**。

    它的元数据是"可能动整台机器"，照那条推会得到一个"在这台机器上跑命令"的图标——
    而用户看到的可能只是一个只读的 MCP 工具，那一行就成了假话。
    """
    from app.services.tool_meta import KINDS, kind_of

    assert kind_of("mcp__whatever__do_something") == "tool"
    assert "tool" in KINDS
    # 空名字同样落这一档（不抛）
    assert kind_of("") == "tool"
