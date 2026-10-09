"""**两根轴**的判定矩阵：权限（能不能碰 × 要不要问）× 任务模式（怎么干活）。

这一组用例守的是用户定下的那几条口径，而不是"函数能跑"：

1. **权限四档 × 读 / 写 / 执行三类工具**：只有「仅查看」会**硬拦**写类，而且只拦
   "会改动东西"的；其余三档一律放行（差别在"要不要问"，第 5 条）；
2. **模式两档只决定"要不要先给计划"**：``goal`` 一律放行，``plan`` 在没给计划前拦写类；
3. **两道闸都要过，先权限后计划**（``decide``）：回给模型的理由必须**说清是哪一道拦的**
   ——输入框那一排并排摆着「权限」与「模式」两颗，指错了用户会去改另一颗；
4. **档不改工具清单**：判定函数只看元数据与档位，不接触工具表（工具表的同一性由
   ``test_tool_loop`` 那条端到端用例钉住）；
5. **``auto_approves`` / ``tool_needs_ask`` 归权限轴**（2026-09-29 四档 + 抄 Codex 的
   Auto 口径）：``view`` 压根不放行、``manual`` 每条都问、``smart``（默认）工作区内不问
   而出界/联网/说不清才问、``full`` 一律免问。

工具名从 ``tool_meta.TOOL_META`` 里**取真实的条目**（不新造假元数据当夹具）：
元数据被改动时（比如哪天把 ``create_note`` 改成只读），这里的矩阵就该跟着红——
这正是它存在的意义。少数几条用 ``ToolMeta(...)`` 现造，因为要测的是**判定规则**本身。
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import Settings
from app.services import modes
from app.services.runtime_config import SETTING_GROUPS, RuntimeConfigService
from app.services.tool_meta import TOOL_META, ToolMeta, meta_of

#: 三类工具的代表。**取真实的工具名**：判定读的是它们真实的元数据。
READ_TOOLS = ("search", "list_documents", "read_file", "web_fetch", "list_skills", "query_table")
WRITE_TOOLS = ("create_note", "upload_document", "export_document", "remember", "spawn_subagent")
EXEC_TOOLS = ("run_command",)


def _verdict(
    name: str,
    *,
    mode: str = modes.MODE_GOAL,
    permission: str = modes.PERMISSION_SMART,
    plan_given: bool = False,
) -> bool:
    return modes.decide(
        meta_of(name), mode=mode, permission=permission, plan_given=plan_given, tool=name
    )[0]


# ------------------------------------------------------------------ 词表本身


def test_the_two_axes_are_exactly_what_the_user_defined() -> None:
    """两根轴的取值与默认档。

    - 权限：``view`` / ``manual`` / ``smart`` / ``full``（**默认「默认（智能）」**——
      用户 2026-09-29 的硬要求：默认要能干活，而"出工作区 / 联网 / 说不清"仍然问一句，
      风险留在看得见的地方）；
    - 模式：``goal`` / ``plan``（**默认目标**——"要不要先给计划"是活法偏好，
      默认该是能干活的那个）。

    顺序 = 界面的排列顺序：权限由严到松，模式按用户列的那两个。
    """
    assert modes.PERMISSIONS == ("view", "manual", "smart", "full")
    assert modes.DEFAULT_PERMISSION == "smart"
    assert set(modes.PERMISSION_DEFS) == set(modes.PERMISSIONS)

    assert modes.MODES == ("goal", "plan")
    assert modes.DEFAULT_MODE == "goal"
    assert set(modes.MODE_DEFS) == set(modes.MODES)


def test_each_level_carries_its_one_line_copy() -> None:
    """两轴的每一档都要有 hint 与 detail：界面第二行、设置页下拉项都从它取，
    空串会让界面出现空白项。"""
    for item in [*modes.describe_modes(), *modes.describe_permissions()]:
        assert item["hint"], item
        assert item["detail"], item
    assert {item["name"]: item["label"] for item in modes.describe_permissions()} == {
        "view": "仅查看",
        "manual": "手动批准",
        "smart": "默认",
        "full": "全自动",
    }


def test_an_unknown_mode_falls_back_to_the_default_and_says_so(caplog) -> None:
    """设置页是自由文本：写错了**回默认档并留一条日志**，不让整轮对话失败。

    静默回默认是"改了不生效"那类问题里最难查的一种，所以日志是这条的一部分。
    """
    with caplog.at_level(logging.WARNING):
        assert modes.coerce("nonsense") == modes.DEFAULT_MODE
        assert modes.coerce("") == modes.DEFAULT_MODE
        assert modes.coerce(None) == modes.DEFAULT_MODE
        assert modes.coerce_permission("nonsense") == modes.DEFAULT_PERMISSION
    assert "nonsense" in caplog.text

    # 大小写与空白照收（.env 里手写的那一份常常带空格）
    assert modes.coerce("  PLAN ") == "plan"
    assert modes.coerce_permission("  FULL ") == "full"


def test_the_old_four_modes_and_the_old_exec_policy_are_mapped(caplog) -> None:
    """旧值照收：``build``/``edit``/``yolo`` → ``goal``；旧的命令执行策略四值 → 新四档。

    旧部署、``.env``、网页上写过的值都可能是旧的四档——判不出来的话会静默回默认档，
    而"我以前设的是全放行，怎么变回去了"是那种最难查的问题。映射时**留一条日志**。
    """
    with caplog.at_level(logging.INFO):
        assert modes.coerce("build") == "goal"
        assert modes.coerce("edit") == "goal"
        assert modes.coerce("yolo") == "goal"
        assert modes.coerce("plan") == "plan"

        # 旧的「命令执行策略」：允许 → 全自动；需确认 → 默认（智能）；拒绝 → 仅查看
        assert modes.coerce_permission("allow") == "full"
        assert modes.coerce_permission("ask") == "smart"
        assert modes.coerce_permission("deny") == "view"
        # 更早那一档（"照跑但不过审批"）：按它能做什么归到全自动
        assert modes.coerce_permission("sandbox") == "full"
        # 旧的中间档名字（旧的「工作区内编辑」）：最接近的是"默认（智能）"
        assert modes.coerce_permission("workspace") == "smart"
    assert "build" in caplog.text or "edit" in caplog.text


# ------------------------------------------------------------------ 判定矩阵：权限轴


@pytest.mark.parametrize("permission", modes.PERMISSIONS)
@pytest.mark.parametrize("mode", modes.MODES)
@pytest.mark.parametrize("name", READ_TOOLS)
def test_read_only_tools_run_in_every_combination(permission: str, mode: str, name: str) -> None:
    """只读工具**六种组合全放行**（两轴 × 三档），``plan`` 档也不例外。

    计划档要挡的是"动手"，不是"查资料"：不给它读，它连计划都写不出来。
    """
    assert _verdict(name, mode=mode, permission=permission) is True


@pytest.mark.parametrize("mode", modes.MODES)
@pytest.mark.parametrize("name", WRITE_TOOLS + EXEC_TOOLS)
def test_view_blocks_every_write_on_both_modes(mode: str, name: str) -> None:
    """「仅查看」的核心：**写类一律拦下**（模式是什么都一样）。

    这条是权限轴存在的理由——它管的是"能碰什么"，与"怎么干活"无关。
    """
    assert _verdict(name, mode=mode, permission=modes.PERMISSION_VIEW) is False
    assert _verdict(name, mode=mode, permission=modes.PERMISSION_VIEW, plan_given=True) is False


@pytest.mark.parametrize(
    "permission",
    (modes.PERMISSION_MANUAL, modes.PERMISSION_SMART, modes.PERMISSION_FULL),
)
@pytest.mark.parametrize("name", WRITE_TOOLS + EXEC_TOOLS)
def test_the_three_looser_levels_allow_writes(permission: str, name: str) -> None:
    """``manual`` / ``smart`` / ``full`` **一律放行**写类：它们的差别在"要不要问一句"
    （见 ``test_auto_approve_matrix``），不在"能不能做"。

    这是照抄 ZCode 的那条原则（"档不影响工具是否存在，只喂权限引擎"）：
    如果它们也各自拦一些，档就变成了第二张权限表，与工具元数据迟早说不到一起。
    """
    assert _verdict(name, permission=permission) is True
    assert modes.decide(
        meta_of(name), mode=modes.MODE_GOAL, permission=permission, tool=name, tool_label="写笔记"
    ) == (True, "")


# ------------------------------------------------------------------ 判定矩阵：模式轴


@pytest.mark.parametrize("name", WRITE_TOOLS + EXEC_TOOLS)
def test_plan_blocks_writes_until_the_plan_is_given(name: str) -> None:
    """``plan`` 档的核心：**没出计划之前写类一律被拦**（QwenPaw 的门闸）。

    计划给出来之后同一档就放行了——这一档要的不是"永远不许写"，
    而是"先给计划、等对方确认"。
    """
    assert _verdict(name, mode="plan", plan_given=False) is False
    assert _verdict(name, mode="plan", plan_given=True) is True


@pytest.mark.parametrize("name", WRITE_TOOLS + EXEC_TOOLS)
def test_goal_allows_everything_that_permission_allows(name: str) -> None:
    """``goal`` 档**一律放行**：它的差别不在"能不能做"，那件事归权限轴。"""
    assert _verdict(name, mode="goal", plan_given=False) is True


def test_both_gates_must_pass_and_permission_is_checked_first() -> None:
    """两道闸都要过，而且**先权限后计划**——顺序决定回给模型的是哪一句理由。

    ``view`` + ``plan``（都没计划）时拦下它的必须是**权限**：两句话的下一步完全不同，
    模型按"先给计划"去做也是白搭（这一档压根不许动东西）。
    """
    meta = meta_of("create_note")
    allowed, reason = modes.decide(
        meta,
        mode=modes.MODE_PLAN,
        permission=modes.PERMISSION_VIEW,
        plan_given=False,
        tool="create_note",
    )
    assert allowed is False
    assert "仅查看" in reason and "计划" not in reason

    # 权限那一档过了，才轮到计划档说话
    allowed, reason = modes.decide(
        meta,
        mode=modes.MODE_PLAN,
        permission=modes.PERMISSION_SMART,
        plan_given=False,
        tool="create_note",
    )
    assert allowed is False
    assert "「计划」档" in reason


def test_writes_are_decided_by_metadata_not_by_a_hardcoded_name_list() -> None:
    """判定读的是元数据（``read_only`` / ``side_effect_scope``），不是工具名清单。

    两处都算"写类"：工具自己没声明只读（fail-closed），**或者**影响面落在
    会话/工作区/机器上。所以新加一个写类工具时不必来改这个模块——声明对了就自动被拦；
    反过来，漏声明只读的只读工具会被多拦一次（保守方向，可接受）。
    """
    assert modes.is_write(meta_of("create_note")) is True
    assert modes.is_write(meta_of("run_command")) is True
    assert modes.is_write(meta_of("search")) is False
    assert modes.is_write(meta_of("web_fetch")) is False
    # 未知工具（外部 MCP）：元数据取最保守的那一档，所以算写类
    assert modes.is_write(meta_of("mcp__someone__do_something")) is True
    assert _verdict("mcp__someone__do_something", permission="view") is False
    assert _verdict("mcp__someone__do_something", mode="plan", plan_given=False) is False


def test_a_destructive_write_stays_blocked_in_plan_even_with_a_plan() -> None:
    """计划给出来了也不放行**需要单独点头的**那一些？——不，放行，理由在这条用例里。

    ``plan`` 档拦的是"没计划就动手"，与"要不要审批"是两件事（后者归 ``auto_approves``
    与 ``agent_exec`` 的三道闸）。所以 ``run_command`` 在计划给出之后照跑，
    只是仍然会停下来问用户（它 ``needs_approval``）。这条用例把那个分界钉住：
    **别人以后想"顺手在 plan 档里也拦掉 destructive"时，先来这里看一眼为什么没拦**。
    """
    meta = meta_of("run_command")
    assert meta.destructive and meta.needs_approval
    assert (
        modes.decide(
            meta,
            mode="plan",
            permission=modes.PERMISSION_SMART,
            plan_given=True,
            tool="run_command",
        )[0]
        is True
    )


def test_every_declared_tool_gets_a_verdict() -> None:
    """工具表里的每一个工具都要能判定（不抛、不返回 None）——表烂掉时立刻可见。"""
    for name, meta in TOOL_META.items():
        for permission in modes.PERMISSIONS:
            for mode in modes.MODES:
                allowed, reason = modes.decide(
                    meta, mode=mode, permission=permission, plan_given=False, tool=name
                )
                assert isinstance(allowed, bool)
                assert bool(reason) is (not allowed)


# ------------------------------------------------------------------ 拦下时说的话


def test_the_plan_refusal_says_why_and_what_to_do() -> None:
    """被**模式**拦时回给模型的话：这一档的规矩 + 这个工具为什么算写类 + 怎么办。"""
    allowed, reason = modes.decide(
        meta_of("create_note"),
        mode="plan",
        permission=modes.PERMISSION_SMART,
        plan_given=False,
        tool="create_note",
        tool_label="写笔记",
    )

    assert allowed is False
    assert "「计划」档" in reason  # 现在是哪一档
    assert "先给出计划、等对方确认" in reason  # 这一档的规矩
    assert "写笔记" in reason and "create_note" in reason  # 拦的是哪一个调用
    assert "workspace" in reason  # 为什么它算"会改动东西"（用元数据说事实）
    assert "不要重试这个调用" in reason  # 防它换个名字重试


def test_the_permission_refusal_says_why_and_what_to_do() -> None:
    """被**权限**拦时回给模型的话：现在哪一档 + 为什么它算写类 + **怎么办**。

    「仅查看」的"怎么办"是**如实说明并停手**（去改权限档、把要做的事说清楚），
    与计划档那句"先给计划"完全不同——这也是 `decide` 要按顺序判、并把理由分开给的原因。
    """
    allowed, reason = modes.decide(
        meta_of("run_command"),
        mode=modes.MODE_GOAL,
        permission=modes.PERMISSION_VIEW,
        tool="run_command",
        tool_label="执行命令",
    )

    assert allowed is False
    assert "「仅查看」权限" in reason
    assert "执行命令" in reason and "run_command" in reason
    assert "system" in reason  # 影响面（元数据里的事实）
    assert "不可逆" in reason  # destructive 的要额外说清
    assert "不要重试这个调用" in reason
    assert "只读的事" in reason  # 明确"哪些照做"，免得它把整轮都停掉
    assert "先给出计划" not in reason  # 不提计划（那是另一道闸的事）


def test_the_refusal_never_names_the_tool_it_did_not_get() -> None:
    """没有中文标签时退回原始工具名，而不是编一个人话或者留空。"""
    _, reason = modes.decide(
        meta_of("create_note"),
        mode="plan",
        permission=modes.PERMISSION_SMART,
        plan_given=False,
        tool="create_note",
    )
    assert "create_note" in reason


# ------------------------------------------------------------------ 要不要问一句（权限轴）


def test_auto_approve_matrix() -> None:
    """``auto_approves``：这一档权限下"要不要停下来问"（2026-09-29 四档）。

    - ``view``：压根不放行（``permission_allows`` 拦下），这里只是安全侧的兜底；
    - ``manual``：**一律要问**（每条都问，含工作区里的写）；
    - ``smart``（默认）：联网、影响面在机器上、工作区外、说不清 → 问；
      只读与工作区内的非破坏性写 → 不问；
    - ``full``：一律免问。**但"免问"不等于"越过拒绝"**：显式的拒绝规则在
      ``agent_exec`` 里排在审批之前（那三道闸不归权限轴管）。
    """
    assert modes.auto_approves(meta_of("run_command"), "view") is False
    # 执行命令的影响面在整台机器上：smart 要问、manual 更不用说要问
    assert modes.auto_approves(meta_of("run_command"), "smart") is False
    assert modes.auto_approves(meta_of("run_command"), "manual") is False
    assert modes.auto_approves(meta_of("run_command"), "full") is True

    workspace_write = ToolMeta(
        side_effect_scope="workspace", risk_level="medium", needs_approval=True
    )
    assert modes.auto_approves(workspace_write, "smart") is True
    assert modes.auto_approves(workspace_write, "manual") is False
    assert modes.auto_approves(workspace_write, "view") is False
    assert modes.auto_approves(workspace_write, "full") is True

    # destructive 的即使落在工作区里也不免问：删东西不可逆，而且这台机器上常常没有
    # 内核级沙箱兜底（Windows 上就是直接执行），所以宁可多问一句。
    assert (
        modes.auto_approves(
            ToolMeta(side_effect_scope="workspace", destructive=True, needs_approval=True),
            "smart",
        )
        is False
    )
    assert modes.auto_approves(meta_of("delete_document"), "smart") is False

    # 联网类：smart 也要问（Codex 那边默认是禁网的）
    assert modes.auto_approves(meta_of("web_fetch"), "smart") is False
    assert modes.auto_approves(meta_of("web_fetch"), "full") is True


# ---------------------------------------------------------- 智能档（默认）的风险判定


def test_smart_lets_commands_inside_the_workspace_run_without_asking() -> None:
    """① SMART：**工作区内的命令不问**（照 Codex 的 Auto：范围判定，不是命令白名单）。"""
    for command in ("git status", "ls -la", "cat a.txt", "echo hi > out.txt", "npm run build"):
        assert modes.command_needs_ask(command, "smart", "C:/ws") is False, command


def test_smart_asks_when_the_command_must_leave_the_workspace() -> None:
    """② SMART：**要出工作区就问**（绝对路径到别处、`..`、`~`）。"""
    for command in ("rm -rf /", "ls > /tmp/x", "cat ../secret", "cat ~/.ssh/id_rsa"):
        assert modes.command_needs_ask(command, "smart", "C:/ws") is True, command


def test_smart_asks_for_anything_that_goes_online() -> None:
    """③ SMART：**联网类就问**（curl/wget/ssh/npm install/git push…）。"""
    for command in (
        "curl http://example.com",
        "wget http://example.com/x",
        "ssh nas",
        "npm install",
        "pip install requests",
        "git push origin main",
    ):
        assert modes.command_needs_ask(command, "smart", "C:/ws") is True, command
    # 只是本机跑一跑的不算联网
    assert modes.command_needs_ask("npm run build", "smart", "C:/ws") is False


def test_smart_strips_wrappers_before_deciding() -> None:
    """④ 核心：**判定前先把包装器剥掉**（Claude Code 那份"内置、不可配置"的列表）。

    `timeout 5 X` 看的是 `X`，`bash -c "X"` 看的是 `X`——不剥的话
    `timeout 5 rm -rf /` 这类就能从范围判定的缝里过去。
    """
    assert modes.unwrap_command("timeout 5 rm -rf /") == "rm -rf /"
    assert modes.unwrap_command('bash -c "rm -rf /"') == "rm -rf /"
    assert modes.unwrap_command('sh -c "ls -la"') == "ls -la"
    assert modes.unwrap_command("env A=1 B=2 git status") == "git status"
    assert modes.unwrap_command("nohup npm install") == "npm install"
    # 剥之后照旧按"范围 / 联网"判：这两条都要问
    assert modes.command_needs_ask("timeout 5 rm -rf /", "smart", "C:/ws") is True
    assert modes.command_needs_ask('bash -c "curl http://x"', "smart", "C:/ws") is True
    # 剥不干净（引号没配平）→ **问**（安全侧兜底）
    assert modes.unwrap_command('bash -c "rm -rf /') is None
    assert modes.command_needs_ask('bash -c "rm -rf /', "smart", "C:/ws") is True


def test_smart_asks_when_it_cannot_tell() -> None:
    """⑤ 判定不了就问：空命令、没有工作区根时的绝对路径、参数里取不出命令。"""
    assert modes.command_needs_ask("", "smart", "C:/ws") is True
    # 不知道工作区在哪：绝对路径一律按"在外面"算（要问），相对路径算里面
    assert modes.command_needs_ask("cat /etc/hosts", "smart", None) is True
    assert modes.command_needs_ask("cat a.txt", "smart", None) is False
    # 工具侧：run_command 但参数里取不出命令 → 问
    assert (
        modes.tool_needs_ask(
            meta_of("run_command"),
            "smart",
            tool="run_command",
            arguments="{}",
            workspace="C:/ws",
        )
        is True
    )


def test_manual_asks_even_for_writes_inside_the_workspace() -> None:
    """⑥ MANUAL：连工作区里的写也要问（与 SMART 的分界就在这一条）。"""
    inside = ToolMeta(side_effect_scope="workspace", needs_approval=True)
    arguments = '{"path": "notes/a.md"}'
    assert modes.tool_needs_ask(inside, "manual", tool="create_note", arguments=arguments) is True
    assert modes.tool_needs_ask(inside, "smart", tool="create_note", arguments=arguments) is False
    # 工作区外的写：smart 也要问（MANUAL 当然也要问）
    outside = '{"path": "D:/elsewhere/a.md"}'
    assert modes.tool_needs_ask(inside, "smart", tool="create_note", arguments=outside) is True
    assert modes.tool_needs_ask(inside, "manual", tool="create_note", arguments=outside) is True


def test_the_default_permission_is_the_smart_one(runtime) -> None:  # type: ignore[no-untyped-def]
    """⭐ 硬要求：**没设置过 `chat.permission` 时就是「默认（智能）」**。"""
    assert modes.DEFAULT_PERMISSION == modes.PERMISSION_SMART
    assert runtime.get("chat.permission") == "smart"


# ------------------------------------------------------------------ 存得下来（验收 ④）


def test_the_mode_is_a_runtime_setting_with_an_env_bootstrap(bundle) -> None:  # type: ignore[no-untyped-def]
    """``chat.mode`` 是**运行期配置**：网页上改的落库，``.env`` 只是引导值。

    "刷新之后还在"靠的就是这一层（前端读的是同一个 ``/settings``）。
    引导值那一半与记忆那三项同一口径（见 ``runtime_config._bootstrap_value``）：
    容器部署能在 compose 里一次写清"这个部署默认用哪一档"，
    而它的优先级排在数据库**下面**——部署时预设一次，之后以网页上的为准。
    """
    settings = Settings(chat_mode="plan")  # type: ignore[call-arg]
    runtime = RuntimeConfigService(bundle, settings)

    assert runtime.get("chat.mode") == "plan"  # .env 引导

    runtime.set({"chat.mode": "goal"})
    assert runtime.get("chat.mode") == "goal"  # 网页上写的盖住引导值


def test_the_defaults_without_any_configuration(runtime) -> None:  # type: ignore[no-untyped-def]
    """什么都没配时：模式 ``goal``、权限 ``smart``（默认（智能））。"""
    assert runtime.get("chat.mode") == modes.DEFAULT_MODE
    assert runtime.get("chat.permission") == modes.DEFAULT_PERMISSION
    assert modes.coerce(runtime.get("chat.mode")) == modes.DEFAULT_MODE
    assert modes.coerce_permission(runtime.get("chat.permission")) == modes.DEFAULT_PERMISSION


def test_the_settings_page_offers_exactly_these_levels() -> None:
    """设置页那两项的下拉项与 ``modes.describe_*`` **同源**（不多不少）。

    两处各写一份的话，界面上的档名与引擎的档名迟早会对不上——
    而"选了一个引擎不认识的档"这件事只会表现为"好像没生效"。
    """
    mode_field = next(
        item for item in SETTING_GROUPS["chat"]["fields"] if item["key"] == "chat.mode"
    )
    permission_field = next(
        item for item in SETTING_GROUPS["chat"]["fields"] if item["key"] == "chat.permission"
    )

    assert mode_field["type"] == "select"
    assert [option["value"] for option in mode_field["options"]] == list(modes.MODES)
    assert permission_field["type"] == "select"
    assert [option["value"] for option in permission_field["options"]] == list(modes.PERMISSIONS)


def test_the_old_exec_policy_setting_is_gone() -> None:
    """旧的「命令执行策略」那一项**已经从设置里去掉**（折进权限轴）。

    留着它就会出现"两处都在说命令能不能跑"：一处写着允许、另一处被权限档拒掉，
    而用户只会看到"我明明开了它还是不让跑"。
    """
    keys = {item["key"] for item in SETTING_GROUPS["sandbox"]["fields"]}
    assert "sandbox.exec_policy" not in keys
