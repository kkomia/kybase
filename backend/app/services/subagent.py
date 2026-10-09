"""子 Agent 派生（v0.16，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.3）。

QwenPaw 的 "Sub-agents at runtime"：一个 Agent 可以**派一个独立的子 Agent**去干
一件自成体系的事，拿回结论继续干自己的。它有用的地方很具体：

- **上下文隔离**：一件需要翻十几份材料才能下结论的事，把那些中间过程留在子 Agent 里，
  父 Agent 只拿到一段结论——否则父的上下文会被中间过程撑满；
- **任务边界清楚**：子 Agent 拿到的是一个**自足的任务描述**，而不是"接着聊"，
  这会逼调用方把任务说清楚（说不清的任务本来也不该派出去）。

**三件必须做的约束**（这是本模块存在的理由，不是可选优化）：

1. **深度只能是 1**。子 Agent 不能再派子 Agent：允许递归等于允许一个
   "模型自己决定要花多少钱"的循环，而没有任何一层能把它拦住。
   实现上不是"检查 depth 然后继续"，而是**子 Agent 的工具集里根本没有 spawn**。
2. **预算**：轮次与时间都有上限。超了就带着"已经查到什么"收尾，
   而不是继续跑——一个跑不完的子任务不该把父任务一起拖死。
3. **范围只能继承，不能扩**：子 Agent 拿到的是父的那份知识库范围与工作区，
   **不能自己挑库**。否则"派个子 Agent"就成了绕过权限检查的一条路。

**它不是"再开一个进程"**：子 Agent 跑在同一个进程里（同一份检索、同一份模型配置），
只是**独立的一套上下文与一个更小的工具面**。这样"派生"的代价是一次模型调用，
而不是一次进程启动。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.exceptions import InvalidRequestError

if TYPE_CHECKING:  # 只为类型标注：运行时不需要，避免与 chat.py 循环导入
    from app.services.chat import SourceRef

__all__ = [
    "MAX_DEPTH",
    "RESULT_BLOCK_HEADING",
    "RESULT_FIELDS",
    "SubAgentBudget",
    "SubAgentResult",
    "SubAgentTask",
    "build_task_prompt",
    "parse_result_block",
    "parse_tool_content",
]

logger = logging.getLogger(__name__)

#: **最大派生深度**。1 = 只有主 Agent 能派，子 Agent 不能。
#: 不是"我们暂时设成 1"，而是**设计上就该是 1**：见模块头第 1 条。
MAX_DEPTH = 1

#: 一个子任务最多几轮（一次工具调用算一轮）。
#:
#: **今天的实现够不到这个数**：`ChatService.run_subagent` 是"一次检索 + 一次作答"，
#: 不是循环（子 Agent 没有任何工具面），实际只走 1 轮。这道闸与时限一起留着，
#: 是给"将来子 Agent 自己带工具"准备的 —— 那时多轮才成立，而预算得先有。
#: （`max_searches` 与 `remaining_turns` 今天也只有用例在读，见
#: `tests/unit/services/test_subagent.py`。）
MAX_TURNS = 3

#: 一个子任务的总时限（秒）。与轮次上限是两道独立的闸：
#: 轮次挡住"来回很多次"，时限挡住"某一次调用卡很久"。
MAX_SECONDS = 90.0

#: 子任务的检索次数上限。搜索是这个 Agent 最贵的动作，单独限一道。
MAX_SEARCHES = 3

#: 派给子 Agent 的任务描述长度上限。**超长说明调用方没想清楚**：
#: 子任务该是一句自足的话，不是一篇背景材料。
MAX_TASK_CHARS = 2000

#: 交回结构里的六项（照抄 Kimi `parallel-agent` L65-79 那张清单）。
#:
#: 为什么要有这份清单：父 Agent 拿到的原先只有一段自由文本，于是"这段结论有多可靠、
#: 它替你做了哪些判断、下一步该往哪走"全要靠猜；多条并行结果之间也没法比较。
#: 每一项都是**显式**的——写不出来就空着（空列表 / ``null``），**不许拿正文硬塞**。
RESULT_FIELDS = (
    "key_findings",  # ① 关键发现
    "evidence",  # ② 证据或引用（机器可查的那份出处仍是 `sources`）
    "decisions",  # ③ 已做出的决策
    "files_changed",  # ④ 更改的文件（子 Agent 没有文件工具，恒为空）
    "risks",  # ⑤ 风险
    "confidence",  # ⑤ 置信度（0-1 的小数，给不出就是 None）
    "next_steps",  # ⑥ 建议的下一步
)

#: 那一块交回结果在上下文里的抬头（父 Agent 与用例都按这一行找它）。
RESULT_BLOCK_HEADING = "子 Agent 结构化交回（六项）"

#: 每一项最多留几条 / 每条多少字。这几项是**回给父 Agent 的摘要**，不是原文搬运：
#: 多了就把父的上下文重新撑满，那正是派子 Agent 要解决的问题。
#:
#: 上限的算法：五个列表项 × 6 条 × 200 字 ≈ 6KB，加上抬头与 JSON 结构约 6.6KB，
#: **远小于工具循环那条 12000 字的截断线**（`tool_loop.MAX_RESULT_CHARS`）——
#: 那一条会砍掉超长结果的**尾巴**，而尾巴正好是"建议的下一步"，
#: 恰好是父 Agent 最需要的一项。宁可每项短一点，也不要那一项被截掉。
RESULT_MAX_ITEMS = 6
RESULT_ITEM_CHARS = 200


@dataclass(frozen=True, slots=True)
class SubAgentBudget:
    """给一个子任务的预算。**父任务不能把自己的预算转给子任务**——
    这里只减不增，所以"派生"不会变成"绕过预算的一条路"。"""

    max_turns: int = MAX_TURNS
    max_seconds: float = MAX_SECONDS
    max_searches: int = MAX_SEARCHES
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def expired(self) -> bool:
        return self.elapsed >= self.max_seconds

    def remaining_turns(self, used: int) -> int:
        return max(0, self.max_turns - used)


@dataclass(frozen=True, slots=True)
class SubAgentTask:
    """要派出去的事。

    ``kb_ids`` 与 ``workspace_id`` **由调用方从父任务复制过来**，子 Agent 不允许
    自己挑——这条是"范围只继承、不扩"的实现方式（见模块头第 3 条）。
    """

    question: str
    kb_ids: list[str] = field(default_factory=list)
    workspace_id: str | None = None
    depth: int = 0


@dataclass(frozen=True, slots=True)
class SubAgentResult:
    """子 Agent 的产出：**一段结论 + 六项结构化交回**。

    ``sources`` 要带回来：父 Agent 引用这段结论时，出处得是真实的。

    六项的结构照抄 Kimi 的并行 Agent 契约（``parallel-agent`` L65-79）：
    **① 关键发现 ② 证据或引用 ③ 已做的决策 ④ 更改的文件 ⑤ 风险 / 置信度 ⑥ 建议的下一步**。
    写不出来就**空着**（空列表 / ``None``）：父 Agent 能分清"这一项没有"与"这一项被省掉了"，
    而拿正文硬塞会让它以为那些内容是真有出处的。
    """

    answer: str
    sources: list[SourceRef] = field(default_factory=list)
    turns: int = 0
    searches: int = 0
    elapsed_seconds: float = 0.0
    stopped_reason: str = ""
    """为什么停下来：``answered`` / ``budget`` / ``timeout`` / ``error``。

    **今天实际只会给三种**：``answered`` / ``timeout`` / ``error`` ——
    实现是一次检索 + 一次作答，走不到"轮次或检索次数用尽"那一档，
    ``budget`` 是给将来真做成多轮循环时留的。写在这里免得读的人以为子 Agent
    在循环、把 `MAX_TURNS` 当成今天就会撞到的闸。

    带上它是为了**不把"没查完"说成"查完了"**——父 Agent 需要知道这段结论
    有多可靠。
    """

    # ① 关键发现：这一段结论里最要紧的几条（`answer` 是它们的连贯叙述版）
    key_findings: list[str] = field(default_factory=list)
    # ② 证据或引用：哪条发现是靠哪份材料得出来的（人读的这一份）。
    #    机器可查的那份出处是 `sources`——两者分工，别互相替代：
    #    `sources` 由我们解析（真实、可点），`evidence` 是子 Agent 自己说的对应关系。
    evidence: list[str] = field(default_factory=list)
    # ③ 已做出的决策：它在查的过程中替你定了什么（例如"只取近三年"）。
    #    **空起来是常态**：它没有工具面，能自己做的决定本来就不多。
    decisions: list[str] = field(default_factory=list)
    # ④ 更改的文件：**恒为空**，这是机械事实而不是谦虚——
    #    子 Agent 的工具面里没有任何写文件的工具（见模块头第 2 条），
    #    所以模型在这一项上说的任何东西都不采信（`from_reply` 里直接丢）。
    files_changed: list[str] = field(default_factory=list)
    # ⑤ 风险：
    risks: list[str] = field(default_factory=list)
    # ⑤ 置信度：0-1 的小数；**给不出就是 `None`**（越界或非数字一律当"没说"，
    #    宁可没有，也别留一个读不出来的数给父 Agent 当把握程度用）。
    confidence: float | None = None
    # ⑥ 建议的下一步：父 Agent 接下来最该做的一件事（可多条）。
    next_steps: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.stopped_reason == "answered"

    @classmethod
    def from_reply(cls, reply: str) -> SubAgentResult:
        """把模型的回复**解析成结论 + 六项**。

        两件事刻意分开：能解析出那个 JSON 对象就按六项填；解析不出来就
        **只留一段 `answer`**（六项全空）——那是"这次没交回结构化结果"，
        不是"结论不重要"。**不拿正文去猜哪一句是风险、哪一句是下一步** ✗：
        猜出来的字段看起来和真的一样，而父 Agent 正是据此判断可靠性的。
        """
        text = (reply or "").strip()
        data = parse_result_block(text)
        if data is None:
            return cls(answer=text)
        findings = _text_list(data, "key_findings")
        # 结论那一栏：模型有时会另给一个 `answer`（不在六项里，但很常见），有就用它
        summary = _text(data.get("answer"))
        return cls(
            answer=summary or "\n".join(findings) or text,
            key_findings=findings,
            evidence=_text_list(data, "evidence"),
            decisions=_text_list(data, "decisions"),
            # ④ 不信模型：它没有文件工具，改不了文件
            files_changed=[],
            risks=_text_list(data, "risks"),
            confidence=_confidence(data.get("confidence")),
            next_steps=_text_list(data, "next_steps"),
        )

    def as_context_block(self) -> str:
        """把结论与六项渲染成**父 Agent 读的那段文字**。

        六项**一个不少**，缺的那几项是空列表 / ``null``。为什么不省掉空的：
        省掉之后"这一项没有"和"这个契约不包含这一项"读起来一模一样，
        而父 Agent 需要的恰恰是能分清这两件事——它据此判断"要不要让子 Agent 再跑一次"。
        """
        payload = {
            "key_findings": self.key_findings,
            "evidence": self.evidence,
            "decisions": self.decisions,
            "files_changed": self.files_changed,
            "risks": self.risks,
            "confidence": self.confidence,
            "next_steps": self.next_steps,
        }
        legend = (
            f"{RESULT_BLOCK_HEADING}：① 关键发现 ② 证据或引用 ③ 已做的决策 "
            "④ 更改的文件（子 Agent 没有文件工具，这一项恒为空）"
            "⑤ 风险 / 置信度（0-1） ⑥ 建议的下一步。"
            "空列表 = 这一项没有；confidence 为 null = 它没给把握程度；"
            "**缺少哪一项都不代表那一项不存在，只代表这次没交回来**。"
        )
        return f"{self.answer}\n\n{legend}\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def _text(value: object) -> str:
    """压平空白的一段文本（非字符串 / 空串都给空串）。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:RESULT_ITEM_CHARS]


def _text_list(data: Mapping[str, Any], key: str) -> list[str]:
    """取一项字符串列表。模型只写一句（不是列表）也认，去重且限量。"""
    raw = data.get(key)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out[:RESULT_MAX_ITEMS]


def _confidence(value: object) -> float | None:
    """置信度：只认 0-1 的数字（``"0.7"`` / ``"70%"`` 这两种走形也认）。

    越界、布尔、别的一律回 ``None``：一个 8.5 分的"置信度"比没有更糟——
    父 Agent 会拿它跟别的子结果比大小。
    """
    if isinstance(value, bool) or value is None:
        return None
    text = value.strip() if isinstance(value, str) else value
    percent = False
    if isinstance(text, str):
        if text.endswith("%"):
            percent = True
            text = text[:-1]
        try:
            text = float(text)
        except ValueError:
            return None
    if not isinstance(text, (int, float)):
        return None
    number = float(text) / 100 if percent else float(text)
    if 0.0 <= number <= 1.0:
        return round(number, 2)
    return None


def parse_result_block(text: str) -> dict[str, Any] | None:
    """从一段文本里取出那**一个 JSON 对象**（六项那一块）。

    口径与 ``skill_blurb.parse_blurbs`` 相同（那里也面对"模型多写一句话"这件事）：
    **往后找第一个 ``{``、往前找最后一个 ``}``**，容忍代码围栏与前后解释；
    取不到就 ``None``——调用方据此**如实空着**，而不是硬塞。
    """
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data: Any = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_tool_content(content: str) -> dict[str, Any] | None:
    """从**我们渲染的那段文字**里把六项取回来（父 Agent 那一侧读它）。

    与 `parse_result_block` 的差别只有一个：先在 ``RESULT_BLOCK_HEADING``
    那一行后面切一刀——那一段正文（结论）里完全可能自己带一个花括号，
    从第一个 ``{`` 找会取错。
    """
    raw = content or ""
    head = raw.find(RESULT_BLOCK_HEADING)
    if head < 0:
        return None
    return parse_result_block(raw[head:])


#: 子 Agent 的系统提示词。**与主 Agent 那套是不同的**，因为它要干的事不同：
#: 主 Agent 在跟人对话，子 Agent 是在交作业——只要结论与出处，不要寒暄与铺垫。
#:
#: 第二段（六项）是 **Kimi 并行 Agent 的契约**：交回的东西要结构化，
#: 父 Agent 才能横向比较多个子结果、也才知道"这段结论有多可靠、下一步往哪走"。
#: 要求写成 JSON 之后**由 `SubAgentResult.from_reply` 解析**：解析不出来就六项空着
#: （见那个方法的说明），**不靠猜正文补齐**。
#: 子 Agent 的交回格式（六项）写在提示词里而不是 schema 校验里：它没有工具面，
#: 输出走的是普通对话补全，能约束的只有提示词与解析这一层。
SUBAGENT_SYSTEM_PROMPT = (
    "你是一个子 Agent，被派去完成一件**自成体系的调研任务**。你的产出会被父 Agent "
    "直接当作结论使用，所以：\n"
    "1. 只给结论与依据，不要客套、不要复述任务、不要说「我查了…」的过程；\n"
    "2. 每个结论都要能对应到检索到的资料；资料不足就明确说「资料不足」——"
    "**不要拿常识补**，那会让父 Agent 以为这是有出处的；\n"
    "3. 篇幅控制在几段以内。你查过的中间过程留在你这里，不要搬给父 Agent。\n"
    "你**不能再派生**别的 Agent，也**不能**改变资料范围。\n"
    "\n"
    "交回格式：先用一段话给出结论，然后**再给一个 JSON 对象**（放在 ```json 代码块里），"
    "只含下面这七个小写键，一个都不要省：\n"
    '{"key_findings": ["关键发现，逐条"], "evidence": ["每条发现对应哪份资料"], '
    '"decisions": ["你替父 Agent 定下的口径，没有就 []"], "files_changed": [], '
    '"risks": ["不确定的地方或可能出错的地方"], "confidence": 0.0, '
    '"next_steps": ["父 Agent 接下来最该做的一件事"]}\n'
    "取值规则：几项都是字符串数组，**没有就写空数组 `[]`**；`confidence` 是 0 到 1 之间的小数，"
    "**没把握就给 null**；`files_changed` 你没有文件工具，**永远是 `[]`**。"
)


def build_task_prompt(task: SubAgentTask) -> str:
    """把任务描述拼成子 Agent 的用户消息。

    **要求调用方给的是自足的任务**：子 Agent 看不到父任务的对话历史
    （那正是上下文隔离的意义）。所以这里做一次检查——太短或太长都拒，
    因为它们分别对应"没说清"与"把历史搬过来了"两种失败。
    """
    text = " ".join((task.question or "").split())
    if not text:
        raise InvalidRequestError("缺少参数：question（要子 Agent 做什么）")
    if len(text) > MAX_TASK_CHARS:
        raise InvalidRequestError(
            f"任务描述太长（{len(text)} 字，上限 {MAX_TASK_CHARS}）。"
            "子 Agent 看不到父任务的对话历史，所以任务要自足；但把背景整段搬过来"
            "就失去了派它的意义——请把任务压成一句话"
        )
    return f"任务：{text}"


def check_depth(depth: int) -> None:
    """深度检查。**在入口处拦**，而不是靠"子 Agent 工具集里没有 spawn"这一条规矩：
    规矩会被下一个改动打破，而这里是代码。"""
    if depth >= MAX_DEPTH:
        raise InvalidRequestError(
            f"不能再派子 Agent 了（当前深度 {depth}，上限 {MAX_DEPTH}）："
            "允许递归等于允许模型自己决定要花多少钱，而没有哪一层能拦住它"
        )
