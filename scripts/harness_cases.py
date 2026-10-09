"""Harness 评分的**任务集与答卷判据**（第一批 10 条）。

与驱动程序分开是有意的：**判据就是卷子**，它要能被单独读、单独评。
改这里等于改评分口径（古德哈特那条原则要防的正是"悄悄改卷子"），
所以这一份文件不带任何 IO——只有任务、提问原文与判据函数。

任务取自 §12.338 抽的那 17 条里**判据能机检**的那几条：

| 留 | 为什么 | 不留 | 为什么 |
| --- | --- | --- | --- |
| B-03 / T-02 | 零工具 vs 用工具，是"工具选择时机"最干净的读数 | Z-01 / D-03 | 判据是"报告质量"「该不该先澄清」，机器判不了 |
| K-03 / G-03 | §12.338 里交白卷与绕路的那两条 | B-06 / Z-09 | 判的是"有没有幻觉/会不会强行合并"，要人读 |
| A-01 / Q-01 | 该不该唤起定时/记忆类工具 | Q-01b | 记忆是否生效要"对照会话"，一条跑不出来 |
| V-07 | 长文里答案唯一（王磊），可精确判 | | |
| E-01 / K-01 / Z-04 | 长输出下限、代码生成、交付物存在 | | |

判据一律是**过程量**（步数、工具次数、是否有产物、有没有报错），
**不评回答好坏**——评正确性会把模型能力与 harness 能力混进同一个数，
而我们要量的是后者。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["CASES", "CASES_BY_ID", "Case", "case_ids"]


@dataclass(frozen=True, slots=True)
class Case:
    """一条用例：问什么（``prompts`` 与实测那批逐字一致）、怎么判（``checks``）。"""

    id: str
    why: str
    prompts: tuple[str, ...]
    checks: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = ()


# ------------------------------------------------------------------ 读数（都用轨迹）

def _steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for turn in trace.get("turns", []) for step in turn.get("steps", [])]


def _tools(trace: dict[str, Any]) -> list[str]:
    return [str(step.get("tool") or "") for step in _steps(trace) if step.get("tool")]


def _calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """去掉 `running` 占位，只留每次调用**收尾那一条**（与实测那批同一口径）。"""
    return [step for step in _steps(trace) if step.get("tool") and step.get("status") != "running"]


def _tools_of_turn(trace: dict[str, Any], index: int) -> list[str]:
    turns = trace.get("turns", [])
    if index >= len(turns):
        return []
    return [str(step.get("tool") or "") for step in turns[index].get("steps", []) if step.get("tool")]


def _artifacts(trace: dict[str, Any]) -> list[str]:
    return [str(item.get("name") or "") for item in trace.get("artifacts", [])]


def _answers(trace: dict[str, Any]) -> str:
    return "\n".join(str(turn.get("answer") or "") for turn in trace.get("turns", []))


def _no_tools(trace: dict[str, Any]) -> bool:
    return not _tools(trace)


def _no_error(trace: dict[str, Any]) -> bool:
    return all(not turn.get("error") for turn in trace.get("turns", []))


def _has_tool(name: str) -> Callable[[dict[str, Any]], bool]:
    return lambda trace: name in _tools(trace)


def _artifacts_at_least(least: int) -> Callable[[dict[str, Any]], bool]:
    return lambda trace: len(_artifacts(trace)) >= least


def _artifact_suffix(suffix: str) -> Callable[[dict[str, Any]], bool]:
    return lambda trace: any(name.lower().endswith(suffix) for name in _artifacts(trace))


def _answer_at_least(chars: int) -> Callable[[dict[str, Any]], bool]:
    return lambda trace: sum(
        int(turn.get("answer_chars") or 0) for turn in trace.get("turns", [])
    ) >= chars


#: 固定合成长文（V-07 用，与 `.shots/cases-list.cjs` 同一份构造，答案唯一）。
_LONG_DOC = "\n".join(
    "第 {n} 条：本周事项「{n} 号看板」，负责人 {owner}，状态 {state}，"
    "交付日期 2026-10-{day:02d}，备注：与上游对齐口径 {n}。".format(
        n=index + 1,
        owner=["李静", "王磊", "赵敏", "陈涛", "周航"][index % 5],
        state="进行中" if index % 3 == 0 else "已完成",
        day=(index % 28) + 1,
    )
    for index in range(60)
)

#: 固定合成日志（Z-04 用）。
_LOG_TEXT = "\n".join(
    "2026-09-29T10:{minute:02d}:00Z {kind} trace={n}".format(
        minute=index,
        kind=["ERROR timeout", "ERROR db connect", "WARN retry", "ERROR timeout"][index % 4],
        n=index + 1,
    )
    for index in range(40)
)


CASES: tuple[Case, ...] = (
    Case(
        id="B-03",
        why="推理 + 不滥用工具（应当直接算，不该去调工具）",
        prompts=("一个水池单开进水管4小时装满，单开出水管6小时排空，同时开几小时装满？",),
        checks=(("零工具调用", _no_tools), ("有回答", _answer_at_least(1)), ("无错误", _no_error)),
    ),
    Case(
        id="T-02",
        why="工具选择时机（简单算式不滥用工具；实时问题要用联网）",
        prompts=("3+5等于几？", "北京今天天气怎么样？"),
        checks=(
            ("第一轮零工具", lambda trace: not _tools_of_turn(trace, 0)),
            (
                "第二轮用了联网",
                lambda trace: bool(_tools_of_turn(trace, 1))
                and _tools_of_turn(trace, 1)[0].startswith("web_"),
            ),
            ("无错误", _no_error),
        ),
    ),
    Case(
        id="K-03",
        why="代码执行 + 交付（画出图并**交出来**——§12.338 里这条交白卷）",
        prompts=("写一个 Python 脚本，把 1 到 10 的平方画成折线图，然后运行它并给我图。",),
        checks=(
            ("调过 run_command", _has_tool("run_command")),
            ("交付了文件", _artifacts_at_least(1)),
            ("交付里有图", _artifact_suffix(".png")),
            ("无错误", _no_error),
        ),
    ),
    Case(
        id="G-03",
        why="表格交付物 + 图表（要求**带图表**，§12.338 里这一条没有图表）",
        prompts=("把下面这份数据做成带图表的 Excel：2024 年 Q1 到 Q4 销售额分别为 120、135、98、160 万元。",),
        checks=(
            ("交付了 xlsx", _artifact_suffix(".xlsx")),
            ("零 run_command（不该去沙箱里硬造）", lambda trace: "run_command" not in _tools(trace)),
            ("无错误", _no_error),
        ),
    ),
    Case(
        id="A-01",
        why="定时任务（应当唤起定时/计划类工具而不是只嘴上答应）",
        prompts=("每天早上 8 点给我生成一份 AI 资讯简报。",),
        checks=(("调过计划类工具", _has_tool("schedule_task")), ("无错误", _no_error)),
    ),
    Case(
        id="Q-01",
        why="显式记忆写入（应当有记忆类工具调用）",
        prompts=("记住我喜欢简洁的回答风格。",),
        checks=(("调过记忆类工具", _has_tool("remember")), ("无错误", _no_error)),
    ),
    Case(
        id="V-07",
        why="长文本细节检索（答案唯一：第 37 条的负责人 = 王磊）",
        prompts=(f"{_LONG_DOC}\n\n这份周报里，第 37 条写的负责人是谁？只回名字。",),
        checks=(
            ("零工具调用", _no_tools),
            ("答出王磊", lambda trace: "王磊" in _answers(trace)),
            ("无错误", _no_error),
        ),
    ),
    Case(
        id="E-01",
        why="长输出稳定性（3000 字以上、不截断）",
        prompts=('请写一篇 3000 字以上的说明文，主题是"把本地大模型接进内网办公系统的工程取舍"，分 8 节，每节带小标题。',),
        checks=(
            ("回答 ≥3000 字", _answer_at_least(3000)),
            ("零工具调用", _no_tools),
            ("无错误", _no_error),
        ),
    ),
    Case(
        id="K-01",
        why="代码生成（不执行也算过）",
        prompts=("用 Python 写一个爬取网页标题的脚本。",),
        checks=(("有回答", _answer_at_least(200)), ("无错误", _no_error)),
    ),
    Case(
        id="Z-04",
        why="综合：代码 + 文本处理 + 写成报告（统计 ERROR 占比）",
        prompts=(f"下面是一段日志，请统计各类 ERROR 各多少条、占比多少，并把结果写成一份报告：\n{_LOG_TEXT}",),
        checks=(("交付了报告", _artifacts_at_least(1)), ("无错误", _no_error)),
    ),
)

CASES_BY_ID: dict[str, Case] = {case.id: case for case in CASES}


def case_ids() -> list[str]:
    return [case.id for case in CASES]
