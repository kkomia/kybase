"""Harness 评分脚本的**纯逻辑**（`scripts/harness_eval.py`）。

**为什么单测落在 backend/tests 里而不是 scripts/ 旁边**：scripts/ 不是包（没有
`__init__.py`，也不在 pytest 的 rootdir 里），而这个项目跑测试只有一条路
（`cd backend && pytest tests`）。所以用路径注入把那一个模块导进来，
测的东西仍然是**同一份源文件**（不是复制一份判据——那样两边迟早漂）。

这一组只盯三件在"可重复评分"里最要紧的事（都不需要真跑模型）：

1. **评分是纯函数**：同一份轨迹评两遍逐字节相同（时间/顺序/全局状态都进不去）；
2. **卷子真的在判**：把一条不该调工具的用例喂上"调了工具"的轨迹 → 必须不过
   （反向验证的静态版——判据恒真/恒假在这里当场现形）；
3. **harness 名随版本与配置变**：改一项配置，摘要必须换一个（否则两份记分卡会
   顶着同一个名字比，那正是"必须附 harness 名"要防的事）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

#: scripts/ 不是包（没有 `__init__.py`）。**按文件路径直接加载**，不动 sys.path：
#: pytest 的 `prepend` 导入模式自己会往 `sys.path[0]` 插东西，靠 sys.path 注入时灵时不灵
#: （实测第一次就"找不到模块"）。导的仍是**同一份源文件**——判据不复制第二份。
#:
#: 两步缺一不可：先登记进 `sys.modules` 再 `exec_module`——`@dataclass` 会回头去
#: `sys.modules[cls.__module__]` 取命名空间（`_is_type` 那一处），没登记就是
#: `AttributeError: 'NoneType' object has no attribute '__dict__'`（实测踩到）。
HARNESS_EVAL = Path(__file__).resolve().parents[4] / "scripts" / "harness_eval.py"
_spec = importlib.util.spec_from_file_location("harness_eval", HARNESS_EVAL)
assert _spec and _spec.loader  # 源文件不见了就是环境坏了，这里当场说清
harness_eval = importlib.util.module_from_spec(_spec)
sys.modules["harness_eval"] = harness_eval
_spec.loader.exec_module(harness_eval)


def _trace(case: str, *, tools: list[str] | None = None, artifacts: list[str] | None = None,
           answer: str = "好的", error: str | None = None, completed: bool = True) -> dict:
    """造一份**轨迹**（形状与 `Runner.run_case` 落盘的那份一致，字段只留用得上的）。"""
    tools = tools or []
    steps = [
        {"tool": tool, "status": "running", "detail": "", "phase": "tool"} for tool in tools
    ] + [
        {"tool": tool, "status": "done", "detail": "", "phase": "tool"} for tool in tools
    ]
    return {
        "case": case,
        "conversation_id": "conv_test",
        "turns": [
            {
                "prompt": "问题",
                "steps": steps,
                "answer": answer,
                "answer_chars": len(answer),
                "first_token_ms": 100,
                "total_ms": 1000,
                "error": error,
                "completed": completed,
            }
        ],
        "artifacts": [
            {"name": name, "kind": name.rsplit(".", 1)[-1], "size_bytes": 10}
            for name in artifacts or []
        ],
    }


def _failed(result: dict) -> list[str]:
    """某次运行里没过的判据名。**这一层是每次运行**；
    `failed_checks` 那个字段在**记分卡**上（把 N 次运行并起来之后）。"""
    return sorted(name for name, ok in result["checks"].items() if not ok)


def test_scoring_is_a_pure_function() -> None:
    """同一份轨迹评两遍**逐字节相同**——"可重复"这句话的机械形式。

    判据里只要混进时间、随机数、共享缓存或"上一次评过什么"，这条就会红。
    """
    trace = _trace("K-03", tools=["run_command"], artifacts=["squares.png"])

    first = harness_eval.score_trace(trace)
    second = harness_eval.score_trace(trace)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["passed"] is True


def test_the_rubric_actually_judges() -> None:
    """**卷子真的在判**：不该调工具的用例调了工具 → 必须不过。

    这是"把评分那一步改成恒返回通过就会红"的静态版：判据恒真/恒假在这里当场现形。
    """
    ok = harness_eval.score_trace(_trace("B-03", answer="12 小时"))
    bad = harness_eval.score_trace(_trace("B-03", tools=["web_search"], answer="12 小时"))

    assert ok["passed"] is True
    assert bad["passed"] is False
    assert bad["checks"]["零工具调用"] is False


def test_a_case_that_should_deliver_and_does_not_is_red() -> None:
    """K-03（要求"把图给我"）：没交付 → 不过；交付了 png → 过。

    这条钉的正是 §12.338 里 K-03 交白卷的那个缺口——**交付物数量是判据的一部分**。
    """
    no_delivery = harness_eval.score_trace(_trace("K-03", tools=["run_command"]))
    delivered = harness_eval.score_trace(
        _trace("K-03", tools=["run_command", "export_file"], artifacts=["chart.png"])
    )

    assert no_delivery["passed"] is False
    assert {"交付了文件", "交付里有图"} & set(_failed(no_delivery))
    assert delivered["passed"] is True


def test_g03_prefers_the_tool_over_the_sandbox() -> None:
    """G-03 的判据里有一条"**零 run_command**"：带图表的表该用 export_table 做，
    不该去沙箱里硬造（§12.338 里这一条绕了 80 步）。"""
    good = harness_eval.score_trace(_trace("G-03", tools=["export_table"], artifacts=["q.xlsx"]))
    detour = harness_eval.score_trace(
        _trace("G-03", tools=["run_command", "export_file"], artifacts=["q.xlsx"])
    )

    assert good["passed"] is True
    assert detour["passed"] is False
    assert _failed(detour) == ["零 run_command（不该去沙箱里硬造）"]


def test_pass_rate_is_computed_per_run_not_per_case() -> None:
    """`--runs N` 时通过率按**每次运行**算：两次里过一次就是 0.5。

    先平均再判分的话，"两次里一次过"会被抹成"看起来过了"——而抖动正是我们要量的东西。
    """
    traces = [
        _trace("B-03", answer="12 小时"),
        _trace("B-03", tools=["web_search"], answer="12 小时"),
    ]

    card = harness_eval.scorecard(traces, {"label": "t"})

    assert card["cases"][0]["pass_rate"] == 0.5
    assert card["cases"][0]["runs"] == 2


def test_harness_name_changes_with_config_and_with_the_budget_constants() -> None:
    """harness 名 = git 版本 + 配置摘要：**改一项配置必须换一个摘要**。

    摘要不变就意味着两份记分卡会顶着同一个名字比——而它们本来不是同一个 harness。
    预算常量（`DEFAULT_MAX_STEPS` / `CONVERGE_RATIO`…）从源码里读，也在摘要里：
    改一个数字就换了 harness，尽管配置项一个都没动。
    """
    baseline = harness_eval.config_digest({"model_pk": "m1", "thinking_effort": "medium"})
    changed = harness_eval.config_digest({"model_pk": "m1", "thinking_effort": "high"})

    assert baseline["digest"] != changed["digest"]
    # 摘要里必须能读到**改的是什么**，不能只留一串哈希
    assert baseline["thinking_effort"] == "medium"
    assert baseline["max_steps"] and baseline["converge_ratio"]


def test_unknown_case_does_not_pass_on_an_empty_rubric() -> None:
    """卷子里没有的用例**不许判过**：空判据等于恒真（古德哈特那条原则的边角）。"""
    result = harness_eval.score_trace(_trace("不存在的用例"))

    assert result["passed"] is False
    assert result["checks"] == {}


def test_scorecard_keeps_the_spread_and_the_conversation_ids() -> None:
    """记分卡要给**分布**（min/中位/max）与**会话 id**。

    只留一个中位数的话，"同一 harness 两次"的抖动就看不见了；而会话 id 是
    "这一轮跑在哪条会话上"的唯一凭据（防数据污染那条原则要求能追）。
    """
    traces = [
        _trace("T-02", tools=["web_search"], answer="晴"),
        _trace("T-02", tools=["web_search", "web_fetch"], answer="晴"),
    ]

    card = harness_eval.scorecard(traces, {"label": "x"})
    item = card["cases"][0]

    # 一次工具调用会发 running + done 两条 step，所以"工具次数"是 2 / 4
    # （与 §12.338 那批实测口径一致：那里数的是 step 事件）
    assert item["tool_calls"] == {"min": 2, "median": 3.0, "max": 4}
    # 去掉 running 占位之后是"真正几次调用"：1 次与 2 次
    assert item["steps"] == {"min": 1, "median": 1.5, "max": 2}
    assert len(item["conversation_ids"]) == 2


def test_compare_table_has_one_row_per_case(tmp_path) -> None:
    """对照表**每条用例一行**。

    原先写成 `[*left, *right]`，两边键相同时会把同一行印两遍（实测踩到）——
    这张表是给人读的，重复行会让人以为跑了两遍。
    """
    before = harness_eval.scorecard([_trace("B-03", answer="12 小时")], {"label": "before"})
    after = harness_eval.scorecard(
        [_trace("B-03", tools=["web_search"], answer="12 小时")], {"label": "after"}
    )
    for side, card in (("before", before), ("after", after)):
        (tmp_path / side).mkdir()
        (tmp_path / side / "scorecard.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8"
        )

    class Args:
        left = str(tmp_path / "before")
        right = str(tmp_path / "after")

    assert harness_eval.command_compare(Args()) == 0
    table = (tmp_path / "after" / "compare.md").read_text(encoding="utf-8")

    assert table.count("| B-03 |") == 1
    # 通过率变了必须写进"变化"那一列（0.0 → 1.0，因为改后那条调了工具）
    assert "通过率 1.0→0.0" in table or "通过率 0.0→1.0" in table
