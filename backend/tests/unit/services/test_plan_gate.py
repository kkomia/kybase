"""计划门闸的状态机（P1-1，开发计划 §12.225）。

``plan`` 档的拦与放**只看这一份状态**（"本会话给没给过计划"），所以它自己必须先是对的。
四条边界：

1. **初始是"没给"**：``plan`` 档下一进来就拦（fail-closed，与工具元数据的默认值同口径）；
2. **以正文收尾才算给了计划**：空正文不算（端点抽风不是计划）；
3. **离开 ``plan`` 档就清空**：计划阶段结束了；再切回来是**新的一次**，不该白捡上一份；
4. **按会话隔离**：会话 A 给过计划，不影响会话 B——串了的表现是"某条会话忽然能写了"。

状态放在**进程内存**（取舍写在 ``plan_gate`` 模块头）：所以用例必须自己清，
否则上一个用例留下的状态会传染给下一个（与 ``useLiveTurn`` 那条同一个坑）。
"""

from __future__ import annotations

from app.services import modes, plan_gate


def _gate(conversation_id: str | None = "c1") -> plan_gate.PlanGate:
    plan_gate.reset_all()
    return plan_gate.gate_for(conversation_id)


def test_a_new_session_has_not_given_a_plan_yet() -> None:
    """初始是"没给"：fail-closed 的那一边（``plan`` 档下宁可不写）。"""
    gate = _gate()

    assert gate.plan_given is False
    assert gate.snapshot() == {
        "conversation_id": "c1",
        "plan_given": False,
        "at": 0.0,
        "note": "",
        "generations": 0,
        "mode": "",
    }


def test_an_answer_ending_turn_marks_the_plan_as_given() -> None:
    """**以正文收尾**就算把计划给出来了，并把开头一段留作证据。"""
    gate = _gate()

    gate.note_plan("计划：先读三份文档，再写一份对比笔记，最后导出一份 md。")

    assert gate.plan_given is True
    assert "先读三份文档" in str(gate.snapshot()["note"])


def test_an_empty_answer_is_not_a_plan() -> None:
    """空正文不算：那是端点抽风（推理模型 max_tokens 不够时会这样），不是计划。"""
    gate = _gate()

    gate.note_plan("")
    gate.note_plan("   \n  ")

    assert gate.plan_given is False


def test_leaving_plan_mode_clears_the_state() -> None:
    """切走 ``plan`` 档就清空；再切回来是**新的一次**（不该白捡上一份计划）。"""
    gate = _gate()
    gate.note_plan("计划：做 A、B、C")

    gate.sync_mode(modes.MODE_GOAL)
    assert gate.plan_given is False
    assert gate.snapshot()["generations"] == 1

    # 回到 plan 档：仍然要重新给一次计划
    gate.sync_mode(modes.MODE_PLAN)
    assert gate.plan_given is False
    gate.note_plan("计划：还是那三件事")
    assert gate.plan_given is True


def test_syncing_while_still_in_plan_keeps_the_state() -> None:
    """每一轮开始时都会同步一次档位：**还在 ``plan`` 档就不许把状态冲掉**。

    冲掉的后果很直接——模型给完计划、下一轮准备动手时又被拦一次，
    用户看到的是"它说完了计划却什么都没做"。

    调用顺序就是真实顺序（``tool_loop.run`` 每轮**先同步、再跑**）：
    先 `sync_mode`，那一轮结束以正文收尾时才 `note_plan`。
    """
    gate = _gate()
    gate.sync_mode(modes.MODE_PLAN)
    gate.note_plan("计划：做 A")

    # 下一轮：还在同一档，状态必须留着
    gate.sync_mode(modes.MODE_PLAN)
    gate.sync_mode("plan")

    assert gate.plan_given is True


def test_coming_back_to_plan_mode_starts_a_new_plan_phase() -> None:
    """``plan → build → plan`` 中间**一轮都没跑过**，回到计划档也要重新给一次计划。

    用户把档切来切去又切回来，多半就是"我想让它重新规划一次"。白捡上一段那份计划，
    表现是"我刚切回计划档，它就直接动手了"——而那一档唯一的意义就是先给计划。
    """
    gate = _gate()
    gate.sync_mode(modes.MODE_PLAN)
    gate.note_plan("计划：做 A")

    gate.sync_mode(modes.MODE_GOAL)
    gate.sync_mode(modes.MODE_PLAN)

    assert gate.plan_given is False
    assert gate.snapshot()["mode"] == modes.MODE_PLAN


def test_the_state_is_per_conversation() -> None:
    """按会话隔离：一条会话给过计划**不等于**另一条也给过。"""
    plan_gate.reset_all()
    first = plan_gate.gate_for("c1")
    second = plan_gate.gate_for("c2")

    first.note_plan("计划：做 A")

    assert first.plan_given is True
    assert second.plan_given is False
    # 同一条会话再取一次门闸，拿到的是同一份状态
    assert plan_gate.gate_for("c1").plan_given is True


def test_no_conversation_shares_one_slot() -> None:
    """没有会话上下文的那条链路（脚本、外部 MCP）共用一个格子，**而不是不设门闸**。"""
    gate = _gate(None)

    assert gate.conversation_id == plan_gate.NO_SESSION
    assert gate.plan_given is False  # 照样拦


# ---------------------------------------------------------------------------
# 研究型任务的状态机（照搬清单第 1 条：澄清 → 出计划 → 等确认 → 执行）
#
# 为什么这些用例必须存在：这一段原来是 `prompt.py` 的**形容词判据**（"歧义大 + 代价高"），
# 既判不出来也没法测；现在判据是纯函数、状态是显式的，两样都要被钉住。
# ---------------------------------------------------------------------------


def test_a_vague_research_ask_goes_to_the_plan_stage() -> None:
    """「帮我整理一份研究报告」→ **出计划**（缺范围与口径，但交付形式说了）。

    这是验收里那条真会话的形状：它应当出现"可确认的研究计划"，而不是直接开跑。
    """
    decision = plan_gate.decide_research("帮我整理一份研究报告")

    assert decision.stage == plan_gate.STAGE_PLAN
    assert set(decision.missing) == {plan_gate.SLOT_SCOPE, plan_gate.SLOT_ANGLE}
    assert "缺 2 样" in decision.reason


def test_an_explicit_research_ask_goes_straight_to_execution() -> None:
    """**明确的需求不许出计划卡**（用户拍板的边界）。

    "调研 2026 中国储能格局，800 字三节列 3 个来源"：范围（2026/中国）、交付形式（800 字三节）、
    口径（来源）都在，预计步数也在阈值内 → 直接执行。
    """
    decision = plan_gate.decide_research("调研 2026 中国储能格局，800 字三节列 3 个来源")

    assert decision.stage == plan_gate.STAGE_EXECUTE
    assert decision.missing == ()
    assert decision.estimated_steps <= decision.threshold
    assert decision.needs_opening is False


def test_a_topicless_research_ask_goes_to_clarification() -> None:
    """三样都缺（「帮我研究一下」）→ **先澄清**：这时候出计划也只能是瞎猜。"""
    decision = plan_gate.decide_research("帮我研究一下")

    assert decision.stage == plan_gate.STAGE_CLARIFY
    assert len(decision.missing) == 3
    assert decision.needs_opening is True


def test_non_delivery_tasks_never_enter_the_research_flow() -> None:
    """不是产出型的活（算数、写代码、天气）**一个阶段都不进**。

    这条是"别把日常小事变成问卷"的机制版：判据里没有"调研/整理/报告"这类意图，
    直接就是执行——连"缺不缺范围"都不算。
    """
    for query in (
        "一个水池单开进水管 4 小时装满，同时开几小时装满",
        "用 Python 写一个爬取网页标题的脚本",
        "今天北京天气怎么样",
        "17 的平方是多少",
    ):
        decision = plan_gate.decide_research(query)
        assert decision.stage == plan_gate.STAGE_EXECUTE, query
        assert decision.missing == ()


def test_the_explicit_ask_is_not_misjudged_by_the_step_threshold() -> None:
    """阈值取 ``DEFAULT_MAX_STEPS`` 的**一半**：明确需求的估算不该被它误伤。

    实测那道明确题（"调研 2026 中国储能格局，800 字三节列 3 个来源"）估算 8 步；
    取 1/4（7.5）就会把它判成"要出计划"——所以这里把阈值本身也钉住。
    """
    from app.services.tool_loop import DEFAULT_MAX_STEPS

    threshold = plan_gate.plan_step_threshold()
    explicit = plan_gate.decide_research("调研 2026 中国储能格局，800 字三节列 3 个来源")
    broad = plan_gate.decide_research(
        "调研一下新能源汽车换电模式的现状，分析主要玩家优劣势，最后生成一份 PDF 报告"
    )

    assert threshold == DEFAULT_MAX_STEPS * plan_gate.PLAN_STEP_RATIO
    assert explicit.estimated_steps <= threshold
    assert broad.stage in (plan_gate.STAGE_PLAN, plan_gate.STAGE_CLARIFY)


def test_a_delivery_with_the_data_at_hand_is_not_missing_scope() -> None:
    """「把这份数据做成带图表的 Excel」：对象就在手上（指代 + 明确格式）→ 不算缺范围。"""
    decision = plan_gate.decide_research("把这份数据做成带图表的 Excel")

    assert plan_gate.SLOT_SCOPE not in decision.missing


def test_the_state_machine_walks_the_four_stages_in_order() -> None:
    """三段迁移连通：**澄清 / 出计划 → awaiting_plan_confirm → executing**。

    每一步都只认"上一步真的发生过"：先 ``open_research`` 才能 ``await_confirm``，
    先 ``await_confirm`` 才能 ``confirm_plan``——跳步会被拒绝（返回 False），
    而不是被静默纠正（静默纠正会让"跳过澄清"这种 bug 一直看不出来）。
    """
    gate = _gate()
    plan_decision = plan_gate.decide_research("帮我整理一份研究报告")

    # 初始：没有流程在跑
    assert gate.stage == plan_gate.STAGE_IDLE
    # 跳步不允许
    assert gate.await_confirm("计划", "ap-1") is False
    assert gate.confirm_plan("ap-1") is False

    # 出计划 → 等确认
    assert gate.open_research(plan_decision) == plan_gate.STAGE_PLAN
    assert gate.stage == plan_gate.STAGE_PLAN
    assert gate.await_confirm("一、范围……二、交付……", "ap-1") is True
    assert gate.stage == plan_gate.STAGE_AWAITING
    assert gate.research_plan().startswith("一、范围")
    # 等确认期间还没"能动手"
    assert gate.plan_given is False

    # id 对不上不算确认（错配 = 上一张卡被点了、这一份计划却开跑）
    assert gate.confirm_plan("ap-别的") is False
    assert gate.stage == plan_gate.STAGE_AWAITING

    # 确认 → 执行阶段，并且**写类工具的门闸就此放开**（plan_given 为真）
    assert gate.confirm_plan("ap-1") is True
    assert gate.stage == plan_gate.STAGE_EXECUTING
    assert gate.plan_given is True


def test_clarify_is_a_stage_of_its_own() -> None:
    """澄清也是一段状态（不是"什么都没发生"）：写下来才知道下一轮该接着干什么。"""
    gate = _gate()

    assert gate.open_research(plan_gate.decide_research("帮我研究一下")) == plan_gate.STAGE_CLARIFY
    assert gate.research_snapshot()["stage"] == plan_gate.STAGE_CLARIFY
    assert gate.research_snapshot()["missing"] == [
        plan_gate.SLOT_SCOPE,
        plan_gate.SLOT_FORMAT,
        plan_gate.SLOT_ANGLE,
    ]
    # 澄清阶段还没有计划可确认
    assert gate.await_confirm("计划", "ap-2") is False
    assert gate.plan_given is False


def test_a_rejected_plan_keeps_the_plan_but_not_the_go_ahead() -> None:
    """用户没确认（拒绝或超时）：退回"计划"阶段，**计划正文留着**、执行权不留。"""
    gate = _gate()
    gate.open_research(plan_gate.decide_research("帮我整理一份研究报告"))
    gate.await_confirm("计划正文", "ap-3")

    assert gate.reject_plan("范围太大，缩到 2026 年", "ap-3") is True
    assert gate.stage == plan_gate.STAGE_PLAN
    assert gate.research_plan() == "计划正文"
    assert gate.research_snapshot()["reason"] == "范围太大，缩到 2026 年"
    assert gate.plan_given is False


def test_confirming_by_text_works_after_the_card_timed_out() -> None:
    """计划卡超时之后，用户下一轮打字说"确认"也要能接上（第二条确认路）。"""
    gate = _gate()
    gate.open_research(plan_gate.decide_research("帮我整理一份研究报告"))
    gate.await_confirm("计划正文", "ap-4")

    assert gate.confirm_by_text() is True
    assert gate.stage == plan_gate.STAGE_EXECUTING
    assert gate.plan_given is True


def test_bare_agreement_is_short_and_modifications_are_not() -> None:
    """"确认"要短句才算；带条件的"可以，但…"是**改需求**，走重新判那一边。"""
    assert plan_gate.is_affirmative("确认")
    assert plan_gate.is_affirmative("好，开始")
    assert plan_gate.is_affirmative("OK")
    assert plan_gate.is_affirmative("可以，但要把范围缩到 2026 年") is False
    assert plan_gate.is_affirmative("我想想") is False


def test_leaving_the_plan_slot_does_not_wipe_the_research_stage() -> None:
    """**两条线的状态互不覆盖**：切档只清档位那半边，研究阶段留着。

    这一条是踩过的坑：默认档是 goal（不是 plan），要是每轮 ``sync_mode``
    把研究阶段一起清了，"等确认"永远等不到。
    """
    gate = _gate()
    gate.open_research(plan_gate.decide_research("帮我整理一份研究报告"))
    gate.await_confirm("计划正文", "ap-5")

    gate.sync_mode(modes.MODE_GOAL)

    assert gate.stage == plan_gate.STAGE_AWAITING
    assert gate.research_plan() == "计划正文"
    assert gate.plan_given is False

    # 显式 reset 才是"两半都清"
    gate.reset()
    assert gate.stage == plan_gate.STAGE_IDLE
    assert gate.research_plan() == ""


def test_the_opening_note_says_what_this_turn_is_allowed_to_do() -> None:
    """开场那一轮的指令要**说清"这一轮只准干什么"**，并把可计算的理由带上。

    它是"这一轮的状态"，不是常驻规矩——所以放在 plan_gate 而非常驻提示词里。
    """
    clarify = plan_gate.opening_note(plan_gate.decide_research("帮我研究一下"))
    plan = plan_gate.opening_note(plan_gate.decide_research("帮我整理一份研究报告"))

    assert "先澄清" in clarify and "不要调用任何工具" in clarify
    assert "只出研究计划" in plan and "不要调用任何工具" in plan
    # 判据算出来的事实要写进去（"缺哪几样"是模型写计划时的输入）
    assert "缺 3 样" in clarify
    assert plan_gate.SLOT_SCOPE in plan and plan_gate.SLOT_ANGLE in plan
