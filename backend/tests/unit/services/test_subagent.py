"""子 Agent 派生（v0.16）。

镜像同构：``app/services/subagent.py`` → 本文件。

**三件必须做的约束**（模块头那三条）逐一钉住，因为它们的失效方式都是静默的：

1. **深度只能是 1**：允许递归等于允许模型自己决定要花多少钱，而没有哪一层能拦住。
   这里断言的不只是"depth=1 被拒"，还有**子 Agent 的调用面里没有派生能力**；
2. **预算只减不增**：父任务不能把自己的预算转给子任务（否则"派生"就是绕过预算的路）；
3. **范围只继承不扩**：``kb_ids`` 与 ``workspace_id`` 由调用方复制，子 Agent 挑不了。
"""

from __future__ import annotations

import time

import pytest

from app.core.exceptions import InvalidRequestError
from app.services import subagent as sa

# --------------------------------------------------------------------- 深度


def test_depth_zero_can_spawn() -> None:
    """主 Agent（depth=0）可以派。"""
    sa.check_depth(0)


def test_depth_one_cannot_spawn_again() -> None:
    """**子 Agent 不能再派子 Agent**。这条不是"暂时这么定"——
    允许递归就是允许一个"模型自己决定花多少钱"的循环，而没有一层能拦住它。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        sa.check_depth(1)

    assert "不能再派" in str(excinfo.value)


def test_depth_limit_is_explicitly_one() -> None:
    """常量本身就是契约：写成别的值时这条会失败，提醒改的人去读模块头。"""
    assert sa.MAX_DEPTH == 1


# --------------------------------------------------------------------- 预算


def test_budget_counts_down_turns() -> None:
    budget = sa.SubAgentBudget(max_turns=3)

    assert budget.remaining_turns(0) == 3
    assert budget.remaining_turns(3) == 0
    # 用超了也不返回负数（调用方据此判"没预算了"，负数会让那个判断写错）
    assert budget.remaining_turns(5) == 0


def test_budget_expires_by_time() -> None:
    """时限与轮次是**两道独立的闸**：轮次挡"来回很多次"，时限挡"某一次卡很久"。"""
    budget = sa.SubAgentBudget(max_seconds=0.01)
    time.sleep(0.02)

    assert budget.expired is True


def test_budget_has_all_three_knobs() -> None:
    budget = sa.SubAgentBudget()

    assert budget.max_turns > 0
    assert budget.max_seconds > 0
    assert budget.max_searches > 0


# --------------------------------------------------------------------- 任务


def test_task_prompt_is_self_contained() -> None:
    prompt = sa.build_task_prompt(sa.SubAgentTask(question="  查清 A 与 B 谁更快  "))

    assert prompt == "任务：查清 A 与 B 谁更快"


def test_empty_question_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        sa.build_task_prompt(sa.SubAgentTask(question="   "))


def test_too_long_question_is_rejected_with_a_useful_message() -> None:
    """太长的任务描述要**说清为什么不行**：子 Agent 看不到父的对话历史，
    任务要自足；但把整段背景搬过来就失去了派它的意义——
    把这句话写进报错里，调用方才知道该改什么。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        sa.build_task_prompt(sa.SubAgentTask(question="很长" * (sa.MAX_TASK_CHARS // 2 + 10)))

    message = str(excinfo.value)
    assert "自足" in message
    assert "压成一句话" in message


def test_task_defaults_are_conservative() -> None:
    """默认值的取舍：**没有知识库范围 = 查不到东西**，所以调用方必须显式给；
    ``workspace_id`` 为空表示"不碰任何工作区"（更保守的默认）。"""
    task = sa.SubAgentTask(question="x")

    assert task.kb_ids == []
    assert task.workspace_id is None
    assert task.depth == 0


# --------------------------------------------------------------------- 提示词


def _plain_prompt() -> str:
    """提示词去掉 Markdown 加粗再断言。

    **不要在断言里手写 ``**``**：提示词的强调写法会变（加粗、换措辞），
    而"这句话在不在"才是要测的东西——写死了标记，改一次排版就红一片，
    那种红不说明任何问题。
    """
    return sa.SUBAGENT_SYSTEM_PROMPT.replace("**", "")


def test_system_prompt_forbids_filling_gaps_from_common_sense() -> None:
    """**"不要拿常识补"是这段提示词里最要紧的一句**：子 Agent 的产出会被父 Agent
    当成有出处的结论用，而拿常识补出来的那部分会让父 Agent 误以为它有依据。"""
    prompt = _plain_prompt()

    assert "资料不足" in prompt
    assert "常识" in prompt
    # 而且要告诉它别再派人（工具面上也做不到，但明说一句省一次无效尝试）
    assert "不能再派生" in prompt


def test_system_prompt_asks_for_conclusions_not_process() -> None:
    """子 Agent 是在**交作业**，不是在跟人对话——所以它不要客套、不要复述过程。
    中间过程留在它那边，这正是"上下文隔离"的收益所在。"""
    prompt = _plain_prompt()

    assert "只给结论与依据" in prompt
    assert "过程" in prompt


# --------------------------------------------------------------------- 结果


def test_result_reports_why_it_stopped() -> None:
    """**不把"没查完"说成"查完了"**：父 Agent 需要知道这段结论有多可靠。"""
    answered = sa.SubAgentResult(answer="结论", stopped_reason="answered")
    budgeted = sa.SubAgentResult(answer="半截结论", stopped_reason="budget")

    assert answered.complete is True
    assert budgeted.complete is False
    assert budgeted.stopped_reason == "budget"


def test_result_carries_sources_so_citations_stay_real() -> None:
    """子 Agent 的出处要带回来：父 Agent 引用这段结论时，出处得是真的。"""
    result = sa.SubAgentResult(answer="结论", sources=["a", "b"], stopped_reason="answered")  # type: ignore[arg-type]

    assert len(result.sources) == 2


# --------------------------------------------------------- 六项结构化交回（D15）


#: 模型照提示词交回的那一份（真跑时就是这一段文本，见 `SUBAGENT_SYSTEM_PROMPT` 第二段）。
STRUCTURED_REPLY = """结论：近三年的结论一致，都指向同一套口径。

```json
{
  "key_findings": ["近三年结论一致", "口径以年为单位"],
  "evidence": ["《指南》第 3 页", "《年报》第 2 节"],
  "decisions": ["只取近三年"],
  "files_changed": [],
  "risks": ["样本量偏小"],
  "confidence": 0.7,
  "next_steps": ["补 2024 年的数据"]
}
```
"""


def test_from_reply_fills_all_six_items() -> None:
    """**六项都要落到字段上**（D15，照抄 Kimi `parallel-agent` L65-79）。

    这一条是这次改动的核心契约：父 Agent 不再只拿到一段自由文本。
    """
    result = sa.SubAgentResult.from_reply(STRUCTURED_REPLY)

    assert result.key_findings == ["近三年结论一致", "口径以年为单位"]
    assert result.evidence == ["《指南》第 3 页", "《年报》第 2 节"]
    assert result.decisions == ["只取近三年"]
    assert result.risks == ["样本量偏小"]
    assert result.confidence == 0.7
    assert result.next_steps == ["补 2024 年的数据"]
    # 结论那一栏：这一份里没有单独的 `answer`，就用关键发现拼出来
    assert "近三年结论一致" in result.answer


def test_from_reply_refuses_to_claim_file_changes() -> None:
    """④ 更改的文件**恒为空**：子 Agent 的工具面里没有任何写文件的工具（模块头第 2 条）。

    模型在这一项上说自己改了哪些文件时，**一个都不采信**——那不是谦虚，是它做不到。
    """
    reply = '{"key_findings": ["x"], "files_changed": ["a.py", "b.py"]}'

    assert sa.SubAgentResult.from_reply(reply).files_changed == []


def test_missing_items_stay_explicitly_empty() -> None:
    """**缺项如实空着**：只交了关键发现时，其余五项是空列表 / None —— 不是"省掉"。

    省掉与"这一项没有"读起来一模一样，而父 Agent 要靠这个区别决定要不要重跑一次。
    """
    result = sa.SubAgentResult.from_reply('{"key_findings": ["只有这一条"]}')

    assert result.key_findings == ["只有这一条"]
    assert result.evidence == []
    assert result.decisions == []
    assert result.files_changed == []
    assert result.risks == []
    assert result.confidence is None
    assert result.next_steps == []


def test_plain_text_reply_leaves_the_six_items_empty() -> None:
    """模型没按格式交回时：**只留一段结论**，其余空着。

    **不拿正文猜**哪一句是风险、哪一句是下一步：猜出来的字段看起来跟真的一样，
    而父 Agent 正是据此判断可靠性的。
    """
    result = sa.SubAgentResult.from_reply("  那就是一致的，没有例外。  ")

    assert result.answer == "那就是一致的，没有例外。"
    assert (result.key_findings, result.risks, result.next_steps) == ([], [], [])
    assert result.confidence is None


def test_result_block_tolerates_the_usual_warping() -> None:
    """两种实测都会出现的走形：前后多一句解释、没有代码围栏。"""
    wrapped = '好的，我按格式交回：{"key_findings": ["一条"], "confidence": "70%"} 以上。'

    result = sa.SubAgentResult.from_reply(wrapped)

    assert result.key_findings == ["一条"]
    # `"70%"` 这种写法也认
    assert result.confidence == 0.7


def test_confidence_out_of_range_is_dropped() -> None:
    """越界的"置信度"比没有更糟：父 Agent 会拿它跟别的子结果比大小。"""
    assert sa.SubAgentResult.from_reply('{"confidence": 8.5}').confidence is None
    assert sa.SubAgentResult.from_reply('{"confidence": -1}').confidence is None
    assert sa.SubAgentResult.from_reply('{"confidence": true}').confidence is None
    assert sa.SubAgentResult.from_reply('{"confidence": "很有把握"}').confidence is None
    # 0 与 1 是合法取值（"没把握"也是一种如实的态度）
    assert sa.SubAgentResult.from_reply('{"confidence": 0}').confidence == 0.0
    assert sa.SubAgentResult.from_reply('{"confidence": 1}').confidence == 1.0


def test_context_block_carries_all_six_items() -> None:
    """父 Agent 读到的那段文字里**六项一个不少**（缺的是空的，不是没有那几行）。"""
    block = sa.SubAgentResult.from_reply(STRUCTURED_REPLY).as_context_block()

    assert sa.RESULT_BLOCK_HEADING in block
    for field in sa.RESULT_FIELDS:
        assert f'"{field}"' in block
    assert "补 2024 年的数据" in block
    assert "样本量偏小" in block
    assert "0.7" in block
    # ④ 那一项要写明白它为什么恒为空（不然读的人以为"它没改文件"是它偷懒）
    assert "没有文件工具" in block


def test_context_block_round_trips_through_the_parent_side_parser() -> None:
    """我们渲染的那段文字，父 Agent 那一侧（spawn 分支）能原样读回来。"""
    result = sa.SubAgentResult.from_reply(STRUCTURED_REPLY)

    parsed = sa.parse_tool_content(result.as_context_block())

    assert parsed is not None
    assert parsed["confidence"] == 0.7
    assert parsed["next_steps"] == ["补 2024 年的数据"]


def test_parent_side_parser_needs_the_heading() -> None:
    """没有那行抬头就不是我们的交回块：正文里自己带一个花括号也不该被当成结果。"""
    assert sa.parse_tool_content('结论里有个 {花括号}，别的什么都没有') is None


def test_plain_text_content_has_no_structured_block() -> None:
    """老调用方（直接回一段结论）**照样能用**：拿不到结构就给 None，调用方照旧。"""
    assert sa.parse_tool_content("结论：是的") is None


# ------------------------------------------------- 父 Agent 那一侧（消费这份契约）


class _StubServices:
    """`build_runner` 要的那一点点（它只为 spawn 分支用到 `skills`）。"""

    class skills:
        @staticmethod
        def list() -> list[object]:
            return []


def test_spawn_branch_reads_the_six_items() -> None:
    """spawn 那一侧：**把结构里最该先看到的两项提到过程面板那一行上**。

    那一行是给用户看的："这次派出去的结果可不可信、接下来该干什么"，
    原先只有"回报了结论"四个字加任务名——用户还得点开才知道。
    """
    from app.services.agent_tools import build_runner

    structured = sa.SubAgentResult.from_reply(STRUCTURED_REPLY).as_context_block()
    runner = build_runner(  # type: ignore[arg-type]
        _StubServices(),
        None,
        kb_ids=["kb_1"],
        subagent=lambda task: (structured, []),
    )

    outcome = runner("spawn_subagent", {"task": "这批文献的结论一致吗"})

    assert outcome.summary == (
        "子 Agent 回报了结论（置信度 0.7，风险 1 条，建议下一步 1 条）：这批文献的结论一致吗"
    )
    # 六项原件一起进父 Agent 的上下文（这才是"合并"要读的东西）
    assert sa.RESULT_BLOCK_HEADING in outcome.content
    assert "补 2024 年的数据" in outcome.content
    parsed = sa.parse_tool_content(outcome.content)
    assert parsed is not None
    assert parsed["risks"] == ["样本量偏小"]


def test_spawn_branch_keeps_the_plain_summary_for_legacy_text() -> None:
    """拿不到结构时那一行**照旧**（不加括号、不编数字）：老调用方与旧用例都还在。"""
    from app.services.agent_tools import build_runner

    runner = build_runner(  # type: ignore[arg-type]
        _StubServices(), None, kb_ids=["kb_1"], subagent=lambda task: ("结论：是的", [])
    )

    assert runner("spawn_subagent", {"task": "随便问问"}).summary == (
        "子 Agent 回报了结论：随便问问"
    )


class _StubLLM:
    """替身模型：只回一段固定文本（这里要钉的是装配，不是模型）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, messages: object) -> str:
        return self.reply


class _StubChat:
    """只给 `run_subagent` / `run_subagent_text` 真正用到的那几样。

    **不用无绑定调用 + 替身**：这两条要钉的是"那条检索调用的形状"与"装配对不对"，
    而 `ChatService` 的其余部分（模型档位、用量记账、上下文压缩…）与它们无关。
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.queries: list[str] = []

    def retrieve_sources(
        self,
        *,
        query: str,
        kb_ids: list[str],
        top_k: int | None = None,
        candidate_k: int = 40,
        reader: object | None = None,
    ) -> list[object]:
        self.queries.append(query)
        return [_source()]

    def _chat_factory(self, config: object) -> _StubLLM:
        return _StubLLM(self.reply)

    def _record_usage(self, chat: object, started: float, items: int, config: object) -> None:
        return None

    def _resolve_llm(
        self, model_pk: str | None, thinking: bool | None, thinking_effort: str | None
    ) -> object:
        return object()


def _source():  # type: ignore[no-untyped-def]
    from app.services.chat import SourceRef

    return SourceRef(
        index=1,
        chunk_id="c1",
        document_id="d1",
        document_name="指南.pdf",
        heading_path="第 3 章",
        page=7,
        score=0.5,
        preview="原文",
    )


def _subagent_method(stub: _StubChat):  # type: ignore[no-untyped-def]
    """把真的 `run_subagent` 挂到替身上（无绑定调用）。"""
    from app.services.chat import ChatService

    def bound(task: sa.SubAgentTask, *, config: object, top_k: int | None = None):  # type: ignore[no-untyped-def]
        return ChatService.run_subagent(stub, task, config=config, top_k=top_k)

    stub.run_subagent = bound  # type: ignore[attr-defined]
    return stub


def test_run_subagent_retrieves_with_keyword_arguments() -> None:
    """子 Agent 那条检索调用**必须走关键字参数**（`retrieve_sources` 的 `*` 在 `query` 前面）。

    **这是一条回归**：原先写成 `retrieve_sources(task.question, kb_ids=…)`，于是
    "只要带上知识库范围"就抛 TypeError，被 `run_subagent` 的 except 收成 `error`——
    看起来像"模型没答上来"，实际上一份资料都没查。真跑一次子 Agent 时才暴露
    （D15，见交卷里那份真跑输出：修之前六项全空、`stopped_reason=error`）。
    """
    from app.services.chat import ChatService

    stub = _StubChat(reply="结论：一致。")
    result = ChatService.run_subagent(
        stub,  # type: ignore[arg-type]
        sa.SubAgentTask(question="这批文献的结论一致吗", kb_ids=["kb_1"]),
        config=object(),
    )

    assert stub.queries == ["这批文献的结论一致吗"]
    assert result.stopped_reason == "answered"
    assert result.searches == 1
    assert len(result.sources) == 1


def test_parent_context_carries_risks_and_next_steps() -> None:
    """**父 Agent 的上下文里要有"风险 / 置信度"与"建议下一步"**（验收那一条）。

    走的是生产那条装配（`run_subagent_text`）：子 Agent 交回结构化结果 →
    父 Agent 拿到的那段文字里六项齐全，且**能被父侧解析回来**。
    """
    from app.services.chat import ChatService

    stub = _subagent_method(_StubChat(reply=STRUCTURED_REPLY))
    content, sources = ChatService.run_subagent_text(
        stub,  # type: ignore[arg-type]
        question="这批文献的结论一致吗",
        kb_ids=["kb_1"],
        model_pk=None,
        thinking=None,
        thinking_effort=None,
    )

    assert sa.RESULT_BLOCK_HEADING in content
    assert "样本量偏小" in content  # 风险
    assert "0.7" in content  # 置信度
    assert "补 2024 年的数据" in content  # 建议下一步
    parsed = sa.parse_tool_content(content)
    assert parsed is not None
    assert parsed["confidence"] == 0.7
    assert parsed["next_steps"] == ["补 2024 年的数据"]
    # 停止原因照旧如实写在末尾（不把"没查完"说成"查完了"）
    assert "停止原因：answered" in content
    assert sources and sources[0].document_name == "指南.pdf"


def test_parent_context_falls_back_to_real_sources_for_evidence() -> None:
    """② 证据或引用：子 Agent 没写证据点时，用**真检索到的那几份材料**补上。

    这不是"拿正文硬塞"——`sources` 本来就是那次检索的产物（真实、可点），
    而父 Agent 靠这一项才知道这段结论是从哪几份材料里来的。
    """
    from app.services.chat import ChatService

    stub = _subagent_method(_StubChat(reply='{"key_findings": ["一致"]}'))
    content, _ = ChatService.run_subagent_text(
        stub,  # type: ignore[arg-type]
        question="这批文献的结论一致吗",
        kb_ids=["kb_1"],
        model_pk=None,
        thinking=None,
        thinking_effort=None,
    )

    parsed = sa.parse_tool_content(content)
    assert parsed is not None
    assert parsed["key_findings"] == ["一致"]
    assert parsed["evidence"] == ["指南.pdf · 第 3 章"]
