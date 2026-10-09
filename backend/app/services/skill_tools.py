"""技能正文里的**工具名对齐**（第三方技能是照 Claude Code 写的）。

## 为什么需要这一层

导进来的那批技能（258 份真实 ``SKILL.md``）**全按 Claude Code 的工具名写**：
``Skill`` 被提及 1226 次、``Read`` 763、``Bash`` 691 …… 而我们的工具叫
``read_skill`` / ``read_file`` / ``run_command``。照原样注入，模型会去调一个
**不存在**的 ``Read`` / ``Bash`` / ``Task`` —— 技能看起来装上了，实际调不动。

## 两件事（一处定义）

1. **目录里注入一张对照表**（`catalog_note`）：模型看到技能列表时同时看到它，
   于是它去调用时用的是我们的名字；
2. **读正文时把旧名字换掉**（`translate_tool_names`）：改的是**给模型看的那一份**，
   磁盘上的原文一个字不动 —— 来源可核对（`skill_market._digests` 锁的也还是上游那一份）。

## `Write` / `Edit` 为什么不是"换一个同类工具"

本环境**刻意没有**写文件工具（写路径只有导出那一条，见 `tools.py` 的导出族）。
所以这两个名字换成的是**替代做法**而不是一个工具名：``Write`` → ``export_*``
（产出走导出）、``Edit`` → ``run_command``（改动用命令行）。零新写路径。
"""

from __future__ import annotations

import re

__all__ = [
    "TOOL_ALIASES",
    "TOOL_SUBSTITUTES",
    "catalog_note",
    "translate_tool_names",
]

#: Claude Code 的工具名 → 我们这边对应的名字（**一处定义**：注入与改写都读它）。
TOOL_ALIASES: dict[str, str] = {
    "Skill": "read_skill / list_skills",
    "Read": "read_file",
    "Bash": "run_command",
    "Grep": "search_files",
    "Task": "spawn_subagent",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
}

#: **我们没有对应物**的那几个：给的是替代做法，不是一个工具名。
#: `Write` / `Edit` 是 Lead 裁定过的立场（保持"没有写文件工具"这条约束）：
#: 产出用导出、改动用命令行，**不新增工具**。
TOOL_SUBSTITUTES: dict[str, str] = {
    "Write": "export_*（本环境没有写文件工具：产出请导出成文件）",
    "Edit": "run_command（本环境没有改文件工具：改动走命令行或重新导出）",
    "Glob": "list_files + search_files",
    "AskUserQuestion": "直接在回答里问用户",
    "Computer": "暂不支持（没有桌面控制工具）",
}

#: 全部要认的旧名字（改写与覆盖统计用）。
LEGACY_NAMES = tuple(TOOL_ALIASES) + tuple(TOOL_SUBSTITUTES)

#: 写作 ``Read`` / ``Bash(…)`` / ``Read tool`` 这三种形状时才算"在说工具"。
#: 不做"裸词替换"：正文里 ``Read the file`` 这种普通英文会被改坏（这批技能是英文写的）。
_BACKTICK = re.compile(r"`(?P<name>" + "|".join(LEGACY_NAMES) + r")`")
_CALL = re.compile(r"\b(?P<name>" + "|".join(LEGACY_NAMES) + r")(?=\s*\()")
_TOOL_WORD = re.compile(
    r"\b(?P<name>" + "|".join(LEGACY_NAMES) + r")\s+(?:tool|command)\b", re.IGNORECASE
)


def _replacement(name: str) -> str:
    if name in TOOL_ALIASES:
        return TOOL_ALIASES[name]
    return TOOL_SUBSTITUTES[name].split("（")[0]


def translate_tool_names(text: str) -> str:
    """把技能正文里的旧工具名换成我们的（只换"明显在说工具"的那三种形状）。

    改的是**给模型看的那一份**；磁盘上的原文不动。
    """
    if not text:
        return text
    out = _BACKTICK.sub(lambda match: f"`{_replacement(match.group('name'))}`", text)
    out = _CALL.sub(lambda match: _replacement(match.group("name")), out)
    out = _TOOL_WORD.sub(lambda match: f"{_replacement(match.group('name'))} 工具", out)
    return out


def catalog_note() -> str:
    """随技能目录一起注入的那段对照说明（模型据此把技能里的旧名字翻译成我们的）。"""
    pairs = "、".join(f"`{old}`→`{new}`" for old, new in TOOL_ALIASES.items())
    missing = "；".join(
        f"`{old}`→{new}" for old, new in TOOL_SUBSTITUTES.items()
    )
    return (
        "【工具名对照】上面这些技能多数是照 Claude Code 写的，正文里的工具名要按这张表换成我们的："
        f"{pairs}。**本环境没有 `Write` / `Edit`**：{missing}。"
        "读到旧名字时一律按这张表理解，**不要去调不存在的工具**——"
        "技能正文本体在被 `read_skill` 读出来时已经换过一遍。"
    )
