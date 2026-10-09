"""两个维度的控制：**权限**（能碰什么 / 要不要问）× **任务行为模式**（怎么干活）。

口径是用户 2026-09-27 定的原话："一个是仅查看 / 工作区内编辑 / 完全访问，这是用来控制
权限的；另一个是计划 / 目标这种模式，是用来控制 agent 行为的，不是用来控制权限，
而是控制任务行为。**所以这是两个维度的东西**。"

2026-09-29 用户把权限这一轴定成**四档「能不能碰 × 要不要问」**（原话整理）：

======================  ==========================================================
维度                    取值与含义
======================  ==========================================================
**权限**（`chat.permission`）  ``view`` 仅查看 / ``manual`` 手动批准 /
``smart`` 默认（智能）/ ``full`` 全自动
**模式**（`chat.mode`）      ``goal`` 目标 / ``plan`` 计划
======================  ==========================================================

**四档的语义**（由严到松，界面也按这个次序排）：

1. **仅查看**（``view``）：**硬拦一切写类** —— 执行器压根不会被叫到（旧档原样保留 ✓）；
2. **手动批准**（``manual``）：**都能碰、但每条都问**（含工作区里的写入）；
3. **默认（智能）**（``smart``，**默认档**）：只读与**工作区内**（含临时目录）的改动/命令
   **直接做、不问**；**要出工作区 / 要联网 / 包装器剥不干净 / 判定不了** → **问**。
   口径抄的是 Codex 的 Auto 档（`workspace-write` + `on-request`：**范围判定**，不是
   安全命令白名单 —— 白名单一漏就被绕过，见下面"智能判定的来源"那一段）；
4. **全自动**（``full``）：全部放行、不再问（= 原来的 ``full``）。

``view`` 与 ``manual`` 不是一回事：前者"压根不给做"，后者"能做、但先问一句"。
两条判定因此**分开**：

- **能不能碰** → ``permission_allows``（只有 ``view`` 会拦 ✓）；
- **要不要问** → ``tool_needs_ask`` / ``command_needs_ask``（``auto_approves`` 是它的反向门面）。

**任务行为模式两档**（"怎么干活"，判定在 ``plan_allows``）：``goal`` 直接干；
``plan`` 在**没给出计划、对方没确认**之前，会改动东西的工具一律不执行（只读的照跑）。

## 三条不能破的规矩（继承自四档时代，理由没变）

1. **档不改工具清单，只喂权限引擎**：交给模型的工具表**一个都不多、一个都不少**，
   变的只是"这一步允不允许执行"。否则每加一档都要去改所有工具的可用性，
   档与工具会互相锁死。——唯一的例外是 ``plan`` 的**计划门闸**，而它拦的是"执行"
   而不是"看见"：工具照样出现在表里，模型照样能调，只是调用会被拦下并拿到理由。
2. **判定只有一处**（``decide`` / ``auto_approves``）：循环、审批、界面提示都从这里取答案。
   散在几处写判定，等于给"同一档下两处口径不同"留门。
3. **权限先于计划**：两道闸都要过。顺序是**先权限后计划**——"这一档根本不许做"
   与"这一档许做，但得先给计划"回给模型的下一步完全不同（前者该放弃、后者该写计划）。

## 拦下时说什么

被拦下**不是错误**，是这一档的规矩，所以回给模型的话必须包含两样：
**为什么被拦**（这一档的规矩 + 这个工具为什么算"会改动东西"）与**怎么办**。
只说"不允许"的回话，模型只会反复重试同一个调用，而屏幕前的人不知道发生了什么。

## 老值怎么办

``chat.permission`` 里可能还留着旧取值（旧部署、``.env``、网页上写过的）：
``workspace``（旧的中间档）→ ``smart``；更早的"命令执行策略"四值
``allow`` → ``full``、``ask`` → ``smart``、``deny`` → ``view``、``sandbox`` → ``full``
（见 ``_LEGACY_PERMISSIONS``）。
``coerce`` 对模式那一轴同理：``plan`` → ``plan``，``build``/``edit``/``yolo`` → ``goal``。
不认识的取值回默认档——写错一个字母就让整轮对话失败是不划算的。
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass

from app.services.tool_meta import ToolMeta

__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_PERMISSION",
    "MODES",
    "MODE_DEFS",
    "MODE_GOAL",
    "MODE_PLAN",
    "PERMISSIONS",
    "PERMISSION_DEFS",
    "PERMISSION_FULL",
    "PERMISSION_MANUAL",
    "PERMISSION_SMART",
    "PERMISSION_VIEW",
    "LegacyMode",
    "ModeDef",
    "PermissionDef",
    "auto_approves",
    "blocked_reason",
    "coerce",
    "coerce_permission",
    "command_from_arguments",
    "command_needs_ask",
    "decide",
    "describe_modes",
    "describe_permissions",
    "inside_workspace",
    "is_network_command",
    "is_write",
    "label_of",
    "permission_label_of",
    "tool_needs_ask",
    "unwrap_command",
]

logger = logging.getLogger(__name__)

# ---- 权限（"能不能碰 × 要不要问"） --------------------------------------------

#: ① 仅查看：**硬拦一切写类**（执行器压根不会被叫到）。2026-09-29 用户明确要保留这一档。
PERMISSION_VIEW = "view"
#: ② 手动批准：都能碰，但**每条都问**（含工作区里的写入）。
PERMISSION_MANUAL = "manual"
#: ③ 默认（智能）：只读与工作区内直接做；越界 / 联网 / 删除 / 危险命令要问。
PERMISSION_SMART = "smart"
#: ④ 全自动：全部放行、不再问（= 原来的 full）。
PERMISSION_FULL = "full"

#: 全部合法取值。**顺序 = 由严到松**，界面按它排列（用户列的就是这个次序）。
PERMISSIONS: tuple[str, ...] = (
    PERMISSION_VIEW,
    PERMISSION_MANUAL,
    PERMISSION_SMART,
    PERMISSION_FULL,
)

#: 默认档 = **智能**（用户 2026-09-29 的硬要求）。
#: 取它的理由：默认要能干活（"仅查看"当默认，产品一上来就是废的；"手动批准"当默认，
#: 每一步都弹一次，用它的人第一分钟就会去关掉），而**越界 / 联网 / 删除 / 危险命令**
#: 仍然要问一句——风险留在看得见的地方，日常那部分不打断人。
DEFAULT_PERMISSION = PERMISSION_SMART


@dataclass(frozen=True, slots=True)
class PermissionDef:
    """一档权限的展示文案（界面与提示词都用它，不另写一份）。"""

    name: str
    label: str
    """短名字（界面上的胶囊）。"""
    hint: str
    """一句话说明（四到六个字）。"""
    detail: str
    """选这一档会发生什么（界面第二行 + 提示词里都会用）。"""


PERMISSION_DEFS: dict[str, PermissionDef] = {
    PERMISSION_VIEW: PermissionDef(
        name=PERMISSION_VIEW,
        label="仅查看",
        hint="只看不动",
        detail="不改文件、不跑命令：会改动东西的工具这一轮一律不执行，只读的照跑。",
    ),
    PERMISSION_MANUAL: PermissionDef(
        name=PERMISSION_MANUAL,
        label="手动批准",
        hint="每条都问你",
        detail="写与命令都能做，但每一条都要先问一次——**包括工作区里的改动**；只读的照跑。",
    ),
    PERMISSION_SMART: PermissionDef(
        name=PERMISSION_SMART,
        label="默认",
        hint="只读与工作区内直接做",
        detail=(
            "只读与工作区里的改动、命令直接做、不再逐条问；"
            "要出工作区、要联网、说不清的一律先问一句。"
        ),
    ),
    PERMISSION_FULL: PermissionDef(
        name=PERMISSION_FULL,
        label="全自动",
        hint="不再逐条问",
        detail="写与命令都放行、也不再问；显式的拒绝规则仍然拦得住。",
    ),
}

# ---- 模式（"怎么干活"） --------------------------------------------------------

MODE_GOAL = "goal"
MODE_PLAN = "plan"

MODES: tuple[str, ...] = (MODE_GOAL, MODE_PLAN)

DEFAULT_MODE = MODE_GOAL


@dataclass(frozen=True, slots=True)
class ModeDef:
    """一档模式的展示文案（界面与设置页都用它，不另写一份）。"""

    name: str
    label: str
    hint: str
    detail: str


MODE_DEFS: dict[str, ModeDef] = {
    MODE_GOAL: ModeDef(
        name=MODE_GOAL,
        label="目标",
        hint="给个目标就去做",
        detail="直接动手：能用工具就用，边做边说。要不要问、能碰多少由**权限**那一档决定。",
    ),
    MODE_PLAN: ModeDef(
        name=MODE_PLAN,
        label="计划",
        hint="先给计划再动手",
        detail="没给出计划、对方没确认之前，会改动东西的工具一律不执行（只读的照跑）。",
    ),
}

#: 旧的四档取值 → 新档（``build``/``edit``/``yolo`` 的差别现在归**权限**轴，见模块头）。
LegacyMode = dict[str, str]
_LEGACY_MODES: LegacyMode = {
    "build": MODE_GOAL,
    "edit": MODE_GOAL,
    "yolo": MODE_GOAL,
    "plan": MODE_PLAN,
    # 旧常量仍导出（别处还按名字引用它们，见 __all__）：它们现在都指向新档
    MODE_GOAL: MODE_GOAL,
    MODE_PLAN: MODE_PLAN,
}

_LEGACY_PERMISSIONS: dict[str, str] = {
    # 旧的"命令执行策略"三档（`sandbox.exec_policy`，已折进权限轴）：
    #   allow  → 全自动（命令直接跑）
    #   ask    → 默认（智能：只读命令不问、危险的要问）
    #   deny   → 仅查看（命令压根不跑）—— deny 的语义就是"不给做"，所以映到硬拦那一档
    "allow": PERMISSION_FULL,
    "ask": PERMISSION_SMART,
    "deny": PERMISSION_VIEW,
    # 更早的那一档（"照跑但不过审批"）：按它能做什么归到全自动
    "sandbox": PERMISSION_FULL,
    # 旧的中间档名字（2026-09-29 之前叫「工作区内编辑」）：现在最接近的就是"默认（智能）"
    # ——只读与工作区内直接做 ✓。注意它与 smart **不完全等价**：smart 多了一条命令白名单
    # （只读命令不问），而且旧 workspace 的"工作区外写入"没有今天这条判定。
    "workspace": PERMISSION_SMART,
}


def coerce(value: object) -> str:
    """把设置里的模式取值归一成两档之一；旧的四档与不认识的一律回默认档。

    不抛异常：这个值来自设置页（自由文本）与 ``.env``，写错一个字母就让整轮对话
    失败是不划算的——回默认档是**最不意外**的处置。但**要留一条日志**：
    静默回默认是那种"改了不生效"的问题里最难查的一类。
    """
    text = str(value or "").strip().lower()
    if text in MODE_DEFS:
        return text
    if text in _LEGACY_MODES:
        mapped = _LEGACY_MODES[text]
        logger.info("agent 模式 %r 是旧的四档取值，按新档 %s 处理", value, mapped)
        return mapped
    if text:
        logger.warning("不认识的 agent 模式 %r，按默认档 %s 处理", value, DEFAULT_MODE)
    return DEFAULT_MODE


def coerce_permission(value: object) -> str:
    """把权限取值归一成三档之一；旧的"命令执行策略"三档**当取值**也认。

    **认的是"值"、不是"旧的键"**：`sandbox.exec_policy` 那一项已经从设置里删掉、
    也没人再读它（见 `runtime_config` 那一项的注释）。这里认 `allow`/`ask`/`deny`/`sandbox`
    是为了"有人照旧写法把它写在 `chat.permission` 上"这种情况——那种写法应当照旧生效，
    而不是静默回默认档。
    """
    text = str(value or "").strip().lower()
    if text in PERMISSION_DEFS:
        return text
    if text in _LEGACY_PERMISSIONS:
        mapped = _LEGACY_PERMISSIONS[text]
        logger.info("权限取值 %r 来自旧的「命令执行策略」，按 %s 处理", value, mapped)
        return mapped
    if text:
        logger.warning("不认识的权限档 %r，按默认档 %s 处理", value, DEFAULT_PERMISSION)
    return DEFAULT_PERMISSION


def label_of(mode: object) -> str:
    """档位的短名字（界面与回话里都用它，例如「计划」）。"""
    return MODE_DEFS[coerce(mode)].label


def permission_label_of(permission: object) -> str:
    """权限档的短名字（界面与回话里都用它，例如「仅查看」）。"""
    return PERMISSION_DEFS[coerce_permission(permission)].label


def describe_modes() -> list[dict[str, str]]:
    """两档模式的展示定义（设置页的下拉项从这里取，不各写一份）。"""
    return [
        {"name": item.name, "label": item.label, "hint": item.hint, "detail": item.detail}
        for item in (MODE_DEFS[name] for name in MODES)
    ]


def describe_permissions() -> list[dict[str, str]]:
    """三档权限的展示定义（设置页与输入区那颗胶囊都从这里取）。"""
    return [
        {"name": item.name, "label": item.label, "hint": item.hint, "detail": item.detail}
        for item in (PERMISSION_DEFS[name] for name in PERMISSIONS)
    ]


# ---- 判定 ---------------------------------------------------------------------


def is_write(meta: ToolMeta) -> bool:
    """这个工具算不算"会改动东西"的那一类。

    两个条件取或，两个都有出处：

    - ``not read_only``：工具自己声明过"我只读"就不算写类；
    - ``side_effect_scope not in ("none", "network")``：影响面落在会话/工作区/机器上，
      必然动了东西。**``network`` 不算写**，与 ``tool_meta.ToolMeta.parallel``
      同一处有据的偏离：我们这一档的 ``network`` 明确是"只发请求、不改任何东西"
      （抓网页、搜索）。

    ``fail-closed``：没声明元数据的工具（外部 MCP、新加的）按写类算——
    元数据默认值本身就是最保守的那一档（``read_only=False`` / ``system``）。
    """
    return (not meta.read_only) or meta.side_effect_scope not in ("none", "network")


def _scope_is_local(meta: ToolMeta) -> bool:
    """影响面收在这一轮会话或用户的工作区里（默认档"工作区内不问"覆盖的范围）。"""
    return meta.side_effect_scope in ("session", "workspace")


def permission_allows(
    meta: ToolMeta,
    permission: object,
    *,
    tool: str = "",
    tool_label: str = "",
) -> tuple[bool, str]:
    """**权限那道闸**：这一档下允许执行这个工具吗；不允许就把理由一并给出。

    - ``view`` 仅查看：写类一律拦下（只读的照跑）—— **这一档的硬拦语义原样保留** ✓；
    - ``manual`` / ``smart`` / ``full``：**一律允许**，差别在"要不要问一句"
      （见 ``tool_needs_ask`` / ``auto_approves``）。"能做但先问"是手动批准那一档的
      全部意思，所以在这一层不能拦——拦在这里，那就变成"压根不给做"了（那是 ``view``）。

    返回 ``(是否允许, 理由)``。允许时理由为空串——调用方据此直接执行，
    不需要再看别的条件（判定散成两处，就会漂）。
    """
    name = coerce_permission(permission)
    if name != PERMISSION_VIEW:
        return True, ""
    if not is_write(meta):
        return True, ""
    return False, permission_blocked_reason(
        tool=tool, tool_label=tool_label, meta=meta, permission=name
    )


def plan_allows(
    meta: ToolMeta,
    mode: object,
    *,
    plan_given: bool = True,
    tool: str = "",
    tool_label: str = "",
) -> tuple[bool, str]:
    """**计划那道闸（任务行为）**：``plan`` 档下没给计划时，写类一律拦下。

    ``goal`` 档**一律允许**：它的差别不在"能不能做"，那件事归权限轴
    （见 ``permission_allows``）。
    """
    name = coerce(mode)
    if name != MODE_PLAN:
        return True, ""
    if plan_given or not is_write(meta):
        return True, ""
    return False, blocked_reason(tool=tool, tool_label=tool_label, meta=meta, mode=name)


def decide(
    meta: ToolMeta,
    *,
    mode: object,
    permission: object,
    plan_given: bool = True,
    tool: str = "",
    tool_label: str = "",
) -> tuple[bool, str]:
    """**唯一的合流判定**：两道闸都要过，**先权限后计划**（顺序的理由见模块头第 3 条）。

    返回 ``(是否允许, 理由)``；允许时理由为空串。
    """
    allowed, reason = permission_allows(meta, permission, tool=tool, tool_label=tool_label)
    if not allowed:
        return False, reason
    return plan_allows(meta, mode, plan_given=plan_given, tool=tool, tool_label=tool_label)


def permission_blocked_reason(
    *,
    tool: str,
    meta: ToolMeta,
    permission: str,
    tool_label: str = "",
) -> str:
    """被**权限档**拦下时回给模型的那段话：为什么被拦 + 怎么办。

    三段都要有（与 ``blocked_reason`` 同一套理由）：现在是什么档、为什么这个工具算
    "会改动东西"、怎么办。``view`` 档的"怎么办"是**如实说明并停手**——
    这一档不许做，换个名字重试或绕路都不行。
    """
    label = PERMISSION_DEFS[coerce_permission(permission)].label
    who = tool_label or tool or "这个工具"
    name = tool or who
    return (
        f"现在是「{label}」权限，这一步没有执行。\n"
        f"这一档的规矩是{PERMISSION_DEFS[PERMISSION_VIEW].hint}：不改动任何东西。\n"
        f"「{who}」（{name}）会改动东西——影响面是 {meta.side_effect_scope}"
        f"{'，而且不可逆' if meta.destructive else ''}，所以被拦下了。\n"
        "怎么办：如实告诉对方这一步需要更高的权限（设置 → 聊天 → 权限，或在输入区那一颗上改），"
        "把你要做的事说清楚让他决定；**不要重试这个调用，也不要换个名字绕过去**。"
        "只读的事（查资料、读文件、搜索）照做，那些不受这一档限制。"
    )


def blocked_reason(
    *,
    tool: str,
    meta: ToolMeta,
    mode: str,
    tool_label: str = "",
) -> str:
    """被**计划档**拦下时**回给模型**的那段话：为什么被拦 + 怎么办。

    写给模型看，所以三段都要有：**现在是什么档**（它可能压根不知道）、
    **为什么这个工具算"会改动东西"**（用元数据里的事实说，不用形容词）、
    **怎么办**（这一档要求的动作是什么）。最后一句是防它换个名字重试。
    """
    label = MODE_DEFS[coerce(mode)].label
    who = tool_label or tool or "这个工具"
    name = tool or who
    return (
        f"现在是「{label}」档，这一步没有执行。\n"
        f"这一档的规矩是{MODE_DEFS[MODE_PLAN].hint}：**先给出计划、等对方确认**，"
        f"在那之前不改动任何东西。\n"
        f"「{who}」（{name}）会改动东西——影响面是 {meta.side_effect_scope}"
        f"{'，而且不可逆' if meta.destructive else ''}，所以被拦下了。\n"
        "怎么办：用一段文字把计划说清楚——要做什么、动哪些东西、分几步、有哪些风险，"
        "然后停下来等对方确认（他的下一句话就是确认）。确认之后这一步再接着做。\n"
        "**不要重试这个调用，也不要换个名字绕过去**；如果你认为现在就必须做，"
        "先把理由与影响面说清，让对方自己决定。"
    )


def auto_approves(
    meta: ToolMeta,
    permission: object,
    *,
    tool: str = "",
    arguments: str = "",
    workspace: str | None = None,
) -> bool:
    """这一档权限下，**需要用户点头的调用**能不能免掉那一次询问。

    与 ``permission_allows`` 分开是因为它们答的是两个问题："能不能做"与"要不要问"。
    吞成一个返回值，调用方就得自己拆，而拆错的那一半不会有任何报错。

    这就是 ``not tool_needs_ask(...)`` 的门面（同一个判定，两个说法）：
    老调用点只传 ``meta`` 也能用（拿不到工具名与参数时按元数据判，见 ``tool_needs_ask``），
    想知道"为什么"的调用点直接调 ``tool_needs_ask`` 拿理由。

    - ``full``：一律免问。**但"免问"不等于"越过拒绝"**：显式的拒绝规则与
      拒绝执行（`agent_exec` 的三道闸）排在审批之前，仍然拦得住——权限档只决定
      "要不要问一句"，不决定"能不能"（那是 `permission_allows` 的事）。
    - ``smart``（默认）：按风险判 —— 只读、工作区内的写 → 免问；越界 / 联网 / 删除 /
      危险命令 / 判定不了 → 照问。
    - ``manual``：一律不免问（每条都问）。
    - ``view``：压根不放行（`permission_allows` 已经拦下了），这里返回 False 是
      "别免问"这个安全侧的兜底——正常情况下它到不了这里。
    """
    return not tool_needs_ask(
        meta, permission, tool=tool, arguments=arguments, workspace=workspace
    )


# ---- "智能"（默认档）的风险判定 -------------------------------------------------
#
# 这一段是默认档的**全部智能**：纯函数、不碰 IO、好测（用户 2026-09-29 点名的能力）。
#
# ## 口径是"抄成熟的"（Codex 的 Auto 档），**不是**"安全命令白名单"
#
# 用户原话："默认白名单直接抄成熟的"。查下来成熟产品的机制跟我们初版想的不一样：
# Codex 的 Auto（默认给受信任仓库的那一档）原文是
# `workspace-write` + `ask-for-approval on-request`：
#
#   *"Codex runs sandboxed commands that can write inside the workspace without prompting.
#     Escalates only when it must leave the sandbox."*
#   *"In `workspace-write`, network is disabled by default unless enabled in config."*
#
# ⇒ **没有"安全命令白名单"这个东西**：它的"智能"是**沙箱范围判定** ——
#   在工作区（含临时目录）内 → 直接跑；**要出工作区 / 要联网 / 兜不住 → 才问**。
#   白名单那条路我们也放弃了：它一漏就被绕过（`git status` 放行了，
#   可 `git status > 文件`、`timeout 5 rm -rf /` 就从缝里过去 ✗）。
#
# ## 值得抄的第二条清单：wrapper 剥离（Claude Code 的 permissions 文档，
# 那份 wrapper 列表写的是"内置、不可配置"）
#
# 判定前先把包装器剥掉，再看**真实命令**：`timeout 5 X` → 看 `X`；
# `bash -c "X"` / `sh -c` / `cmd /c` / `env VAR=1 X` → 拆开看 `X`。
# ⚠️ **剥不干净就按"要问"处理**（fail-closed）。这条是"正确解析"，不是"哪些命令安全"。

#: 包装器：剥掉它们才能看见真实命令（Claude Code 那份内置列表里最常用的几个）。
_WRAPPERS: frozenset[str] = frozenset(
    {"timeout", "time", "nice", "nohup", "stdbuf", "setsid", "env"}
)

#: shell 的"执行一段脚本"开关：`bash -c "…"` / `sh -c '…'` / `cmd /c …`。
_SHELLS: frozenset[str] = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "cmd.exe"})

#: PowerShell 那两个：`powershell -Command "…"` / `pwsh -c "…"`。
_POWERSHELLS: frozenset[str] = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})

#: **一条命令里出现它就说明会出网**（curl/wget 那一类，或 ssh 那一类）。
_NETWORK_COMMANDS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "http",
        "https",
        "ssh",
        "scp",
        "sftp",
        "telnet",
        "nc",
        "netcat",
        "ftp",
        "invoke-webrequest",
        "invoke-restmethod",
        "iwr",
        "irm",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "pacman",
        "brew",
        "choco",
        "winget",
        "scoop",
    }
)

#: **这些命令只有某些子命令会出网**（`npm run build` 不出网、`npm install` 出网）。
_NETWORK_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"install", "i", "add", "ci", "publish", "dlx", "create", "update", "audit"}),
    "pnpm": frozenset({"install", "i", "add", "ci", "publish", "dlx", "create", "update", "audit"}),
    "yarn": frozenset({"install", "add", "dlx", "create", "publish", "upgrade"}),
    "pip": frozenset({"install", "download", "wheel"}),
    "pip3": frozenset({"install", "download", "wheel"}),
    "uv": frozenset({"pip", "add", "sync", "tool"}),
    "poetry": frozenset({"add", "install", "update"}),
    "cargo": frozenset({"add", "install", "publish", "update", "fetch", "search", "login"}),
    "go": frozenset({"get", "install", "download"}),
    "git": frozenset(
        {"fetch", "pull", "push", "clone", "ls-remote", "submodule", "remote", "request-pull"}
    ),
    "docker": frozenset({"pull", "push", "login", "build", "run", "compose"}),
    "docker-compose": frozenset({"pull", "push", "build", "up", "run"}),
    "npx": frozenset({"*"}),  # npx 一律可能拉包（保守）
}

#: 写这些"设备"不碰任何真实文件（`git status 2>/dev/null` 不该因此被问一句）。
_NULL_DEVICES: frozenset[str] = frozenset({"/dev/null", "nul", "nul:", "/dev/zero"})


def _split_words(text: str) -> list[str] | None:
    """按 shell 的规矩切词；切不动（引号没配平之类）返回 ``None`` = 判定不了 → 问。"""
    import shlex

    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return None


def unwrap_command(command: str) -> str | None:
    """把包装器剥掉，返回**真实命令**；剥不干净返回 ``None``（调用方按"要问"处理）。

    剥的层数上限是 6：够对付 `timeout 5 env A=1 bash -c "X"` 这种套娃，
    又不至于在畸形输入上绕圈。**任何一层看不懂就放弃**（不是猜）。
    """
    words = _split_words((command or "").strip())
    if not words:
        return None
    for _ in range(6):
        head = words[0].lstrip("./").lower()
        if head in _WRAPPERS:
            rest = words[1:]
            if head == "env":
                # `env [-i] [VAR=VAL …] CMD`：跳过开关与赋值（两者都跳过，顺序不限）
                while rest and (
                    rest[0].startswith("-") or ("=" in rest[0] and not rest[0].startswith("="))
                ):
                    rest = rest[1:]
            elif head == "timeout":
                # `timeout [-k D] [-s SIG] DURATION CMD`：**只跳它自己的开关**与那个时长——
                # 别碰被包起来那条命令的开关（`rm -rf` 里的 `-rf` 不是 timeout 的）。
                while rest and rest[0].startswith("-"):
                    flag = rest.pop(0)
                    if flag in ("-k", "--kill-after", "-s", "--signal") and rest:
                        rest = rest[1:]
                if rest and _looks_like_duration(rest[0]):
                    rest = rest[1:]
            elif head in ("nice", "ionice"):
                while rest and rest[0].startswith("-"):
                    rest = rest[1:]
                if head == "nice" and rest and rest[0].lstrip("-").isdigit():
                    rest = rest[1:]
            else:  # time / nohup / stdbuf / setsid：跳过开关（stdbuf 还带一个参数）
                flags = []
                while rest and rest[0].startswith("-"):
                    flags.append(rest.pop(0))
                if head == "stdbuf" and rest:
                    rest = rest[1:]  # stdbuf 的参数是 `-o0` 这类，跟开关一起跳过了
            if not rest:
                return None
            words = rest
            continue
        if head in _SHELLS and len(words) >= 2 and words[1].lower() in ("-c", "/c"):
            inner = " ".join(words[2:])
            nested = _split_words(inner)
            if not nested:
                return None
            words = nested
            continue
        if head in _POWERSHELLS and len(words) >= 2 and words[1].lower() in (
            "-c",
            "-command",
            "/c",
        ):
            inner = " ".join(words[2:])
            nested = _split_words(inner)
            if not nested:
                return None
            words = nested
            continue
        break
    return " ".join(words)


def _looks_like_duration(token: str) -> bool:
    """`5` / `5s` / `0.5` / `1m` 这种时长（`timeout` 的第一个位置参数）。"""
    text = token.strip().lower()
    if not text:
        return False
    units = ("s", "m", "h", "d")
    if text[-1] in units:
        text = text[:-1]
    try:
        float(text)
    except ValueError:
        return False
    return True


def is_network_command(command: str) -> bool:
    """这条命令**会不会出网**（默认档据此问一句；Codex 那边默认是禁网的）。

    看的是解开包装器之后的**第一段动词**；`npm install` 这种再看子命令。
    **不猜**：看不懂就返回 False，让"判定不了 → 问"那条兜底（调用方保证）。
    """
    words = _split_words((command or "").strip())
    if not words:
        return False
    head = words[0].lstrip("./").lower()
    if head in _NETWORK_COMMANDS:
        return True
    subcommands = _NETWORK_SUBCOMMANDS.get(head)
    if subcommands and len(words) > 1:
        return "*" in subcommands or words[1].lower() in subcommands
    if head == "go" and len(words) > 2 and words[1].lower() == "mod":
        return words[2].lower() == "download"
    return False


def _is_absolute(path: str) -> bool:
    text = path.replace("\\", "/")
    return text.startswith("/") or (len(text) > 1 and text[1] == ":")


def inside_workspace(path: str, workspace: str | None) -> bool:
    """这个路径算不算"在工作区里"（含系统临时目录，照 Codex 的 workspace-write 口径）。

    - 相对路径 → **算在里面**（Codex 的 workspace-write 就是"在工作区里随便写"）；
    - 绝对路径 → 看它是否落在工作区根或临时目录下；
    - **不知道工作区在哪**（``workspace`` 为空）→ 绝对路径一律**算在外面**（要问），
      相对路径算在里面——这是保守侧：宁可多问一句。
    """
    text = (path or "").strip().strip('"').strip("'")
    if not text:
        return True
    if text in _NULL_DEVICES:
        return True
    if not _is_absolute(text):
        return True
    normalized = text.replace("\\", "/").rstrip("/") or "/"
    roots = [workspace] if workspace else []
    roots.append(tempfile.gettempdir())
    for root in roots:
        if not root:
            continue
        candidate = str(root).replace("\\", "/").rstrip("/")
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(candidate + "/"):
            return True
    return False


#: 从工具参数（JSON 字符串）里找命令时看的键（`agent_exec` 的 `argv`/`command` 都在这）。
_COMMAND_KEYS: tuple[str, ...] = ("command", "cmd", "script")


def command_from_arguments(arguments: str) -> str:
    """尽力从工具参数里取出"要跑的那条命令"（取不到返回空串 → 调用方按"要问"处理）。

    认 ``argv``（数组，拼起来）与 ``command``/``cmd``/``script``（字符串）——
    与 ``agent_exec._argv_of`` 认的是同一批键，两处口径要一致。
    """
    text = (arguments or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text  # 参数不是 JSON：那就把它当命令本身（保守侧：不认识就该问）
    if not isinstance(payload, dict):
        return ""
    argv = payload.get("argv")
    if isinstance(argv, list):
        return " ".join(str(item) for item in argv)
    for key in _COMMAND_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def command_needs_ask(command: str, permission: object, workspace: str | None = None) -> bool:
    """这一档下，**这条命令**要不要问一句（纯函数；默认档 = Codex 的 Auto 口径）。

    ``smart`` 三步，任何一步"说不清"都归到问：

    1. **wrapper 剥不干净** → 问（`unwrap_command` 返回 None）；
    2. **会出网** → 问（curl/wget/ssh/npm install…；Codex 那边默认禁网）；
    3. **有路径出了工作区**（绝对路径到别处、`..`、`~`）→ 问；
       都在工作区（含临时目录）里 → **不问**，直接跑 ✓。
    """
    name = coerce_permission(permission)
    if name == PERMISSION_FULL:
        return False
    if name == PERMISSION_MANUAL:
        return True
    if name == PERMISSION_VIEW:
        # 到不了这里（`permission_allows` 已经把写类硬拦了）；真到了就问 —— 安全侧兜底，
        # 与旧代码一致（旧用例 `auto_approves(run_command, "view") is False` 就是钉这个）。
        return True
    bare = unwrap_command(command)
    if bare is None:
        return True  # 剥不干净 → 问（fail-closed）
    if is_network_command(bare):
        return True  # 要出网 → 问
    words = _split_words(bare) or []
    for token in words[1:]:  # 第一个词是动词，不是路径
        if token.startswith("-"):
            continue
        if token in ("..", "~") or token.startswith("../") or token.startswith("~/"):
            return True
        if _is_absolute(token) and not inside_workspace(token, workspace):
            return True
    return False


#: 从工具参数里找路径时看的键。
_PATH_KEYS: tuple[str, ...] = ("path", "file", "filename", "target", "destination", "to", "from")


def _paths_in_arguments(arguments: str) -> list[str]:
    """尽力从参数里取出路径（取不到就空列表 = "说不出在哪"）。"""
    text = (arguments or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    found: list[str] = []
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
    return found


def tool_needs_ask(
    meta: ToolMeta,
    permission: object,
    *,
    tool: str = "",
    arguments: str = "",
    workspace: str | None = None,
) -> bool:
    """**默认档那颗脑子**：这一步要不要先问一句（纯函数，好测）。

    四档的行为在开头四行就分完了；下面是 ``smart`` 的判定（Codex 的 Auto 口径）：

    1. **联网类工具**（``side_effect_scope == "network"``：web_fetch / web_search…）→ 问；
    2. **只读工具** → 不问（``is_write`` 为假）；
    3. **执行命令**（工具名 ``run_command`` 或影响面是 ``system``）→ 交给
       ``command_needs_ask``（范围 / 联网 / 包装器三条）；
    4. **写类、影响面在会话或工作区里** → 参数里的路径**都在工作区内**就不问；
       有任何一个在外面 → 问；
    5. **其它**（影响面在机器上、没声明元数据的外部工具）→ **问**。

    拿不到工具名与参数时（老调用点只传 ``meta``）退化成"按元数据判"：
    只读与本地写入不问、联网与系统影响面都问——**工作区外写入**那一条要拿到参数才判得出来。
    """
    name = coerce_permission(permission)
    if name == PERMISSION_FULL:
        return False
    if name == PERMISSION_MANUAL:
        return True
    if name == PERMISSION_VIEW:
        # 到不了这里（`permission_allows` 已经把写类硬拦了）；真到了就问 —— 安全侧兜底。
        return True

    # ---- smart（默认档）----
    if meta.side_effect_scope == "network":
        return True  # 要出网 → 问（Codex 默认禁网）

    if not is_write(meta):
        return False  # 只读：不问

    is_command = tool == "run_command" or meta.side_effect_scope == "system"
    if is_command:
        command = command_from_arguments(arguments)
        if not command:
            return True  # 取不出命令 → 问（fail-closed）
        return command_needs_ask(command, name, workspace)

    # **destructive 的仍要问**（即使影响面在工作区里）：Codex 那边"工作区内随便做"的前提是
    # **真有一个沙箱把范围兜住**；我们这台机器上常常没有 bwrap/docker（Windows 就是直接执行），
    # 所以"删东西"这一类不可逆的，宁愿多问一句。
    if _scope_is_local(meta) and not meta.destructive:
        paths = _paths_in_arguments(arguments)
        if not paths:
            return False  # 工具自己声明影响面在工作区里，且参数里没有越界路径 → 不问
        return any(not inside_workspace(item, workspace) for item in paths)

    return True  # 影响面在机器上 / destructive / 元数据说不清 → 问
