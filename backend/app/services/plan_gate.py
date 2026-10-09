"""计划门闸：``plan`` 档下"本会话是否已给出计划"（P1-1，开发计划 §12.225）。

抄的是 **QwenPaw 的 ``plan.enabled`` + ``set_plan_gate``**（《Agent-与对话架构对标调研
v0.1》§2.6 第 3 条）：计划模式下**没出计划之前写类工具一律被拦**，并把"为什么被拦"
回灌给模型。QwenPaw 那边"计划"是 ``create_plan`` 这个工具写下去的；我们这一侧的
"计划"只有一条通道——**模型这一轮的正文**（工具循环里没有"提交计划"的工具，
也不打算为它加一个：ZCode 的 ``EnterPlanMode`` 是模式工具，属于 P1-2 的斜杠命令那一批）。

## 状态放在哪里，以及为什么

**进程内存，按会话 id 一个格子**（不落库、不进设置）。取舍：

- 落库（比如 `app_settings` 里按会话拼一个键）能让"计划已给出"熬过重启，
  但会话删除时那条键没人清、多 worker 部署时又各写一份，收益是"重启后少要一次计划"
  ——而这一档真正管的是**同一轮与相邻几轮**的护栏（对方的下一条消息就是确认），
  重启之后重新给一次计划并不算错，反而是更保守的方向。
- 进程内存的代价是**多 worker 时每个进程一份**：某个进程没记过这个会话的计划，
  就照样拦一次。方向是"多要一次计划"，不是"误放行"，所以可以接受。
- 另一个可选做法是把它折进 `ToolLoop` 的实例状态。没这么做：`ToolLoop` 每一轮新建
  （见 `services/chat.tool_loop`），状态活不过一轮，而门闸的意义恰恰是跨轮次的。

## 什么时候算"已给出计划"

**这一轮以正文收尾**（模型没再要工具、把话说完了）就算。理由：对话里模型能把计划
交到用户眼前的唯一通道就是正文；而"同一轮里先写一段计划、紧接着调写类工具"**不算**
——那时候对方还没机会看到、更没确认，正是这一档要挡的形状（ZCode 那边要用户点一次
"批准"，我们的"批准"就是对方的下一条消息）。

离开 ``plan`` 档（切到 build / edit / yolo）时状态**清空**：计划阶段结束了。
再切回 ``plan`` 也是**新的一段**——哪怕中间一轮都没跑过（用户把档切来切去又切回来，
多半就是想让它重新规划一次），所以 :meth:`PlanGate.sync_mode` 认得"刚回到 plan"这件事。

## 第二条线：研究型任务的「澄清 → 出计划 → 等确认 → 执行」（照搬 Kimi 深度研究）

出处：`docs/归档/调研/Kimi-Resources-能力与实现-照搬清单.md` 第 1 条（机制级）——
https://www.kimi.com/features/deep-research 行 19 / 45 / 73：深度研究固定三阶段
「澄清问题，自主执行，生成成果」，而且「**正式检索前**绘制出完整的研究计划」，
用户可以「确认、缩小或扩大研究范围，随后开始执行」。

**为什么要有它**：我们原来只有 `prompt.py` 的一段**形容词判据**（"歧义大 + 代价高，
两条同时成立才问"）——形容词判不出来、也没法测；而 `plan` 档那道门闸只拦**写类工具**，
研究型任务一个写操作都没有，那道闸**永远不会触发**。于是"先给计划再执行"只剩提示词，
没有机制、没有"待确认"状态。

**现在**：判据是**可计算的**（:func:`decide_research`，纯函数、不看模型脸色），
状态是**显式的**（下面五个阶段），计划是一次**可确认的产出**——复用
``approvals.py`` 那条通道与前端已有的审批卡片，不另造一套审批。

    澄清（问 1~2 句）→ 出计划 → awaiting_plan_confirm → 执行（executing）

三个阶段的进入条件（都在 :func:`decide_research` 里，逐条可测）：

- **澄清**：题目把「范围 / 交付形式 / 口径」**三样都缺**（「帮我研究一下」）——
  这时候出计划也只能是瞎猜，先问 1~2 句；
- **出计划**：缺 1~2 样（「帮我整理一份研究报告」缺范围与口径），
  或者**预计步数**超过预算的一半（:data:`PLAN_STEP_RATIO` ×
  ``tool_loop.DEFAULT_MAX_STEPS``，不新造数字）；
- **直接执行**：三样都齐、预计步数也不高（「调研 2026 中国储能格局，800 字三节列 3 个来源」）
  ——**明确的需求不许再出计划卡**，那是用户拍板的边界。

## 两条线的状态**互不覆盖**（一个踩过的坑）

档位那条（``given`` / ``note`` / ``mode``）只在 ``plan`` 档里有意义，切走就清；
研究这条**与档位无关**（默认档是 goal，研究流程照样要按状态机走），所以
:meth:`PlanGate.sync_mode` **只清档位那半边**，研究阶段由
:meth:`PlanGate.mark_clarified` / :meth:`PlanGate.await_confirm` /
:meth:`PlanGate.confirm_plan` 自己推进。把两者一起清掉的表现是
"每一轮都重新问一次要不要出计划"，而原因藏在另一个模块里。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from app.services import modes

__all__ = [
    "AFFIRMATIVE_WORDS",
    "NO_SESSION",
    "PLAN_CONFIRM_TIMEOUT_SECONDS",
    "PLAN_STEP_RATIO",
    "SLOT_ANGLE",
    "SLOT_FORMAT",
    "SLOT_SCOPE",
    "STAGE_AWAITING",
    "STAGE_CLARIFY",
    "STAGE_EXECUTE",
    "STAGE_EXECUTING",
    "STAGE_IDLE",
    "STAGE_PLAN",
    "PlanGate",
    "ResearchDecision",
    "confirmed_note",
    "confirmed_step",
    "decide_research",
    "gate_for",
    "is_affirmative",
    "opening_note",
    "opening_step",
    "plan_step_threshold",
    "plan_summary",
    "reset_all",
]

#: 没有会话上下文时共用的那一个格子（脚本、外部 MCP 客户端、定时任务的早期路径）。
#: **不是"不设门闸"**：那种链路上 `plan` 档仍然是拦的，只是它们共用一个状态。
NO_SESSION = ""


# ---------------------------------------------------------------- 研究流程的阶段（显式枚举）

STAGE_EXECUTE = "execute"
"""**不进研究流程**，这一轮直接干活（普通问答，或需求已经明确到不用先出计划）。"""

STAGE_IDLE = "idle"
"""没有研究流程在跑（普通问答、或流程已结束）。"""

STAGE_CLARIFY = "clarify"
"""已经把澄清问题发出去了，等用户补充（下一轮拿新题目重新判）。"""

STAGE_PLAN = "plan"
"""这一轮要（或已经）出计划，但还没进"等确认"（``await_confirm`` 之后才是）。"""

STAGE_AWAITING = "awaiting_plan_confirm"
"""计划卡已经发到界面上，等用户点确认 / 缩小 / 扩大。"""

STAGE_EXECUTING = "executing"
"""用户确认过了，按计划执行（这一阶段 :attr:`PlanGate.plan_given` 为真）。"""

#: 预计步数**超过预算这个比例**就该先出计划。用 ``tool_loop.DEFAULT_MAX_STEPS`` 的比例，
#: 不新造一个数字（照搬清单第 1 条明确要求）：默认 30 步 × 0.5 = 15 步。
#:
#: 取一半而不是 1/4：实测「调研 2026 中国储能格局，800 字三节列 3 个来源」这种
#: **明确需求**的估算就有 8 步左右，1/4（7.5）会把它误判成"要出计划"——
#: 而用户拍板的边界是"明确的需求不许再问"。
PLAN_STEP_RATIO = 0.5

#: 缺哪一样就说哪一样（计划里要补的正是这几样）。
SLOT_SCOPE = "范围"
SLOT_FORMAT = "交付形式"
SLOT_ANGLE = "口径"

#: 「这是要产出东西的活」的动词/名词。**刻意窄**：写代码、算数、聊天都不在里面
#: （否则"写个爬虫"也会弹计划卡）。
_DELIVERY_WORDS = (
    "调研",
    "研究",
    "整理",
    "梳理",
    "综述",
    "盘点",
    "对标",
    "白皮书",
    "报告",
    "简报",
    "分析",
    "方案",
    "可行性",
    "立项",
    "深度研究",
)

#: 交付形式的词：说了这些就是"要交一份成型的东西"。
_FORMAT_WORDS = (
    "报告",
    "文档",
    "文件",
    "白皮书",
    "综述",
    "简报",
    "方案",
    "摘要",
    "清单",
    "大纲",
    "提纲",
    "表格",
    "markdown",
    "字",
    "节",
    "页",
    "章",
)

#: 「要出文件」——导出类交付（计划里要写清格式与落点）。
_FILE_WORDS = (
    "pdf",
    "ppt",
    "pptx",
    "excel",
    "xlsx",
    "word",
    "docx",
    "导出",
    "附件",
    "下载",
    "文件",
)

#: 范围/对象：说了地域、时间、行业，或者拿"手上这份数据"当对象，都算范围有了。
_SCOPE_WORDS = (
    "中国",
    "国内",
    "全球",
    "海外",
    "国际",
    "全国",
    "行业",
    "市场",
    "产业",
    "领域",
    "赛道",
    "城市",
    "地区",
)
_DEMONSTRATIVE_WORDS = ("这份", "上面", "以下", "下面", "附件", "上传的", "刚才那", "这组")
_YEAR_RE = re.compile(r"(19|20)\d{2}")

#: 口径/角度：说了从哪个角度看、要什么产出要素。
_ANGLE_WORDS = (
    "角度",
    "口径",
    "重点",
    "侧重",
    "对比",
    "优劣势",
    "优势",
    "劣势",
    "痛点",
    "挑战",
    "机会",
    "风险",
    "趋势",
    "现状",
    "格局",
    "原因",
    "影响",
    "建议",
    "来源",
    "引用",
    "数据",
)

#: 判"这一轮要跑很久"的两个加成项（都是**可计算**的表面特征，不做语义理解）。
_BREADTH_WORDS = ("现状", "趋势", "格局", "对比", "优劣势", "综述", "盘点", "全面", "深入", "系统")
_MULTI_SOURCE_WORDS = ("来源", "引用", "文献", "数据", "案例")

#: 用户回的那句"同意"（研究计划的确认）。**刻意短**：长句里出现"可以"多半是在
#: 提条件（"可以，但要改成 3000 字"），那种要走"重新判"那一边。
AFFIRMATIVE_WORDS = (
    "确认",
    "同意",
    "可以",
    "开始",
    "执行",
    "就按这个",
    "按这个来",
    "没问题",
    "好的",
    "好",
    "行",
    "ok",
    "OK",
    "go",
)
#: 超过这个长度的一句"同意"不算同意（大概率带着条件，见 ``AFFIRMATIVE_WORDS``）。
_AFFIRMATIVE_MAX_CHARS = 12


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    """这一轮该走研究流程的哪一步——**纯计算的结果**，可以照着判、也可以照着测。

    字段里没有"歧义大不大"这种形容词：只有**缺了哪几样**、**预计几步**、
    以及预算阈值。日志与界面上要回答"它当时为什么停下来问"时，读的就是这一份。
    """

    stage: str
    missing: tuple[str, ...] = ()
    estimated_steps: int = 0
    threshold: float = 0.0
    reason: str = ""

    @property
    def needs_opening(self) -> bool:
        """要不要先走一次"没有工具"的开场调用（澄清或出计划）。"""
        return self.stage in (STAGE_CLARIFY, STAGE_PLAN)

    def slots_text(self) -> str:
        return _slots_text(self.missing)


def plan_step_threshold() -> float:
    """出计划的步数阈值（``tool_loop.DEFAULT_MAX_STEPS`` × :data:`PLAN_STEP_RATIO`）。

    **延迟导入**：``tool_loop`` 反过来 import 本模块（它要用门闸），模块级 import
    会成环。这里只在真正算的时候取一次现成的数字——不复制一个 30 到这边。
    """
    from app.services.tool_loop import DEFAULT_MAX_STEPS

    return DEFAULT_MAX_STEPS * PLAN_STEP_RATIO


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word.lower() in text for word in words)


def _missing_slots(query: str) -> tuple[str, ...]:
    """「范围 / 交付形式 / 口径」里哪几样没说到——**这是可计算触发的主判据**。

    刻意只做表面特征（词表 + 年份正则 + "手上这份数据"这类指代），不做语义理解：
    判错的代价是"多问一句/多给一张计划卡"，而语义判据判错的代价是"该问的时候不问"。
    两样都缺/都齐的边界由调用方按数量分档（见 :func:`decide_research`）。
    """
    text = query.lower()
    scope = _has_any(text, _SCOPE_WORDS) or bool(_YEAR_RE.search(text))
    if not scope and _has_any(text, _DEMONSTRATIVE_WORDS) and _has_any(text, _FORMAT_WORDS):
        # "把这份数据做成带图表的 Excel"：对象就在手上（指代 + 明确格式），
        # 不该因为没提地域/年份就把它判成"缺范围"
        scope = True
    missing: list[str] = []
    if not scope:
        missing.append(SLOT_SCOPE)
    if not _has_any(text, _FORMAT_WORDS):
        missing.append(SLOT_FORMAT)
    if not _has_any(text, _ANGLE_WORDS):
        missing.append(SLOT_ANGLE)
    return tuple(missing)


def _estimate_steps(query: str) -> int:
    """预计要跑几步——**不看模型**，只看题目里能数出来的东西。

    基准 3 步（取资料、看一眼、写出来），然后按可数的表面特征加：

    - 提了几件事（逗号/顿号/分号切出来的段数，最多算 4）；
    - 题目本身是宽的（现状 / 趋势 / 格局 / 对比 / 综述…）；
    - 要出文件（多一轮导出与核对）；
    - 要多来源（来源 / 引用 / 文献 / 数据 / 案例）。

    它是**估算**，不追求准：只用来回答"这活像不像要跑掉半个预算"。
    """
    text = (query or "").lower()
    parts = [part for part in re.split(r"[，,、;；/]|(?:\s+和\s+)", query or "") if part.strip()]
    aspects = len(parts)
    estimate = 3
    estimate += min(aspects, 4)
    if _has_any(text, _BREADTH_WORDS):
        estimate += 2
    if _has_any(text, _FILE_WORDS):
        estimate += 2
    if _has_any(text, _MULTI_SOURCE_WORDS):
        estimate += 2
    return estimate


def decide_research(query: str) -> ResearchDecision:
    """这一轮走哪一步（**唯一的判据入口**，纯函数）。

    分档就是三句话：

    - 不是产出型的活（写代码、算数、闲聊）→ :data:`STAGE_EXECUTE`；
    - 三样都缺 → :data:`STAGE_CLARIFY`（先问 1~2 句，出计划也是瞎猜）；
    - 缺 1~2 样，或预计步数超过阈值 → :data:`STAGE_PLAN`；
    - 其余 → :data:`STAGE_EXECUTE`（**明确的需求不许再出计划卡**）。
    """
    text = (query or "").strip()
    threshold = plan_step_threshold()
    estimate = _estimate_steps(text)
    if not text or not _has_any(text.lower(), _DELIVERY_WORDS):
        return ResearchDecision(
            stage=STAGE_EXECUTE,
            estimated_steps=estimate,
            threshold=threshold,
            reason="不是产出型的活（没有调研/整理/报告这类意图）",
        )
    missing = _missing_slots(text)
    if len(missing) >= 3:
        return ResearchDecision(
            stage=STAGE_CLARIFY,
            missing=missing,
            estimated_steps=estimate,
            threshold=threshold,
            reason=f"范围、交付形式、口径三样都没说（缺 {len(missing)} 样）",
        )
    if missing or estimate > threshold:
        why = (
            f"缺 {len(missing)} 样（{_slots_text(missing)}）"
            if missing
            else f"预计 {estimate} 步 > 阈值 {threshold:g} 步"
        )
        return ResearchDecision(
            stage=STAGE_PLAN,
            missing=missing,
            estimated_steps=estimate,
            threshold=threshold,
            reason=why,
        )
    return ResearchDecision(
        stage=STAGE_EXECUTE,
        estimated_steps=estimate,
        threshold=threshold,
        reason=f"范围/交付形式/口径都齐，预计 {estimate} 步 ≤ 阈值 {threshold:g} 步",
    )


def _slots_text(missing: tuple[str, ...]) -> str:
    return "、".join(missing) if missing else "（都齐了）"


def is_affirmative(text: str) -> bool:
    """用户这句话是不是"同意按计划来"（**短句 + 命中同意词**才算）。

    长句不算："可以，但把范围缩到 2026 年"是**改需求**，那条路要走"重新判"，
    照着计划直接开跑会把用户的修正丢掉。
    """
    line = " ".join((text or "").split())
    if not line or len(line) > _AFFIRMATIVE_MAX_CHARS:
        return False
    lowered = line.lower()
    return any(word.lower() in lowered for word in AFFIRMATIVE_WORDS)


def opening_note(decision: ResearchDecision) -> str:
    """开场那一轮给模型的**这一轮指令**（澄清或出计划）。

    为什么把这段放在这个模块而不是系统提示词里：它是**这一轮的状态**决定的
    （缺哪几样、预计几步），不是常驻规矩；写进提示词就变成每轮都在说的背景音，
    而"这一轮只准出计划、不准动手"这件事必须恰好在那几轮里说。
    """
    if decision.stage == STAGE_CLARIFY:
        return (
            "【这一轮先澄清，不要动手】\n"
            f"我判断这道题缺：{_slots_text(decision.missing)}，"
            "现在直接开干只会猜错方向。请**只做一件事**："
            "问 1~2 个最关键的问题（一句话一个，别写成问卷），"
            "问完就停——**不要调用任何工具**，也不要先给方案。\n"
            f"（判据是算出来的：缺 {len(decision.missing)} 样，"
            f"预计 {decision.estimated_steps} 步，阈值 {decision.threshold:g} 步。）"
        )
    return (
        "【这一轮只出研究计划，不要动手】\n"
        f"这道题我判断要先对计划：{decision.reason}。"
        "请**只做一件事**：给出一份可以直接开工的研究计划，"
        "写清这几样——①研究范围与边界（含时间/地域口径）②要交付什么形式"
        "（多长、几节、要不要出文件）③打算怎么查（检索方向、需要哪些数据与来源）"
        "④预计产出结构。**不要调用任何工具**，也不要开始检索："
        "计划会先交给用户确认，确认之后你再动手。\n"
        "计划用简短的小标题与条目，别写成一篇报告。"
    )


def opening_step(decision: ResearchDecision):
    """开场那一步的过程事件（**面板上要看得见"为什么停下来"**）。"""
    from app.services.tool_loop import StepEvent

    if decision.stage == STAGE_CLARIFY:
        return StepEvent(
            phase="plan",
            label="先澄清需求",
            detail=f"缺 {_slots_text(decision.missing)}，先问 1~2 句再动手",
            status="done",
        )
    return StepEvent(
        phase="plan",
        label="先出研究计划",
        detail=f"{decision.reason}，计划确认后再执行",
        status="done",
    )


def confirmed_step():
    """确认之后那一步（把"计划已确认、开始执行"写在面板上）。"""
    from app.services.tool_loop import StepEvent

    return StepEvent(
        phase="plan",
        label="计划已确认，开始执行",
        detail="按用户确认的计划检索与产出",
        status="done",
    )


def plan_summary(plan: str, limit: int = 400) -> str:
    """计划正文压成一行（放进审批卡片的 ``args``——那一行就是用户看到的东西）。"""
    text = " ".join((plan or "").split())
    return text[:limit]


#: 计划正文在状态里留多长（诊断与"再确认一次"都要看它）。
PLAN_CHARS = 4000

#: 计划卡等多久（秒）。**比审批那条的 120 秒长**：一份研究计划要读、要想、
#: 可能还要改成"缩小范围"再点——120 秒对这种内容偏短，而超时的代价是用户
#: 得在下一轮打字说"确认"（那条路也接得上，见 :meth:`PlanGate.confirm_by_text`）。
PLAN_CONFIRM_TIMEOUT_SECONDS = 300.0


def confirmed_note(plan: str) -> str:
    """计划确认之后，交给**执行那一轮**的指令（把计划原文带上）。

    为什么不把计划当成一条 assistant 消息塞回去：这一轮的计划正文已经作为
    正文流出去了（用户看得见），再插一条 assistant 消息会让历史里出现两遍，
    回看时"它到底说过几次计划"就说不清了。指令挂成 user 那一侧的一句话，
    历史里只有一条，而且模型一定看得见。
    """
    text = " ".join((plan or "").split())
    return (
        "【计划已确认，开始执行】\n"
        "用户确认了下面这份研究计划。请**按它执行**：先做检索，再按计划里的"
        "结构与交付形式产出结果；中途需要调整计划时，在回答里说明改了什么。\n"
        f"计划原文：\n{text}"
    )


@dataclass(slots=True)
class _State:
    """一条会话的门闸状态（**可变**，由 :class:`PlanGate` 在锁里改）。"""

    given: bool = False
    """这一轮计划阶段里，模型有没有把计划交出来。"""

    at: float = 0.0
    """记下这条计划的时间（``time.time()``）。只用于回看/排查，不参与判定。"""

    note: str = ""
    """计划正文的开头一段。**留在内存里做证据**：日志或者界面上要回答
    "它当时给的是什么计划"时不必去翻消息表。截断保存，见 ``NOTE_CHARS``。"""

    generations: int = 0
    """这一段计划阶段被重置过几次（切走再切回 ``plan`` 一次算一次）。"""

    mode: str = ""
    """上一轮看到的档。**只用来认"刚进/回到 plan 档"这一件事**：
    同一段计划阶段里每轮都会看一眼档，而"从别的档回到 plan"意味着新的一段开始
    （哪怕中间没有跑过任何一轮——用户可能只是把档位切来切去又切回来）。"""

    # ---------------------------------------------------------- 研究流程那半边
    # 刻意与上面那几个字段**分开放**：档位那条只在 plan 档里有意义（切走就清），
    # 而研究流程与档位无关（默认档是 goal，它照样要按状态机走）。见模块头那段。

    stage: str = STAGE_IDLE
    """研究流程走到哪一步（从 :data:`STAGE_IDLE` 起）。"""

    plan: str = ""
    """这一轮给出的计划正文（截断保存）。**"再确认一次"与排查都要看它**：
    计划卡超时之后，用户下一条消息说"确认"时按的就是这一份。"""

    approval_id: str = ""
    """计划卡对应的审批 id（复用 ``approvals.py``；确认/拒绝都要对得上号）。"""

    missing: tuple[str, ...] = ()
    """判据算出来的"缺哪几样"（留作证据，日志与界面用）。"""

    estimated_steps: int = 0
    """判据算出来的预计步数。"""

    reason: str = ""
    """判据给出的理由 / 用户拒绝时捎带的那句话。"""

    confirmed_at: float = 0.0
    """计划被确认的时间（``time.time()``）。"""


#: ``note`` 的保留长度。计划正文可能很长，而这里只是留个证据。
NOTE_CHARS = 400


class PlanGate:
    """一条会话的"计划给没给"。

    形状是**对象而不是裸函数**：`ToolLoop` 拿到的就是这个对象（见 `tool_loop`），
    所以判定时不需要再知道会话 id——而那正是最容易传错的一个参数
    （传成别的会话，表现是"这条会话忽然能写了"，且只在多会话并行时出现）。
    """

    def __init__(self, conversation_id: str | None = None, *, clock=time.time) -> None:
        self._key = str(conversation_id or NO_SESSION)
        self._clock = clock

    # ------------------------------------------------------------------ 身份

    @property
    def conversation_id(self) -> str:
        return self._key

    # ------------------------------------------------------------------ 读写

    @property
    def plan_given(self) -> bool:
        """计划是否已经"算数"了——**两条路之一**：

        - ``plan`` 档那条：这一轮以正文收尾（模型把计划说出来了）；
        - 研究流程那条：**用户已经确认过计划**（:data:`STAGE_EXECUTING`）。

        合成一个属性的理由：调用方（``tool_loop`` 的写类工具门闸）只关心
        "现在能不能动手"，不关心这个"能动手"是从哪条路来的。
        """
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            return state.given or state.stage == STAGE_EXECUTING

    def sync_mode(self, mode: object) -> None:
        """每一轮开始时把当前档告诉门闸：**不在 ``plan`` 档就清空**。

        为什么由循环每轮调一次而不是由"切档"那个动作调：切档发生在设置端点里
        （`chat.mode` 是一次 ``PATCH /settings``），那条路上既不知道会话、
        也不该耦合到门闸；而"每一轮开始时看一次当前档"是循环本来就要做的事
        （见 `tool_loop.run`），顺手就同步了。

        两件事，缺一件都不对：

        - **不在了就清空**：切到 build / edit / yolo 之后，上一段计划不该还算数；
        - **刚回来也算新的一段**：`plan → build → plan` 中间哪怕一轮都没跑过，
          回到 ``plan`` 时也要重新给一次计划——否则"我又切回计划档了"会白捡
          上一段的那份计划，而用户切这一下多半就是想让它重新规划。
        """
        name = modes.coerce(mode)
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            was = state.mode
            state.mode = name
            if name != modes.MODE_PLAN or was != modes.MODE_PLAN:
                _clear(state)

    def note_plan(self, text: str = "") -> None:
        """记下"计划已给出"（**这一轮以正文收尾**时由工具循环调用）。

        空正文不算：端点抽风吐了个空回答，不是计划。
        """
        plan = " ".join((text or "").split())
        if not plan:
            return
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            # 只在第一次（或重置之后）落时间：同一段计划阶段里再答一次正文，
            # 不该把它变回"刚刚才给"——那会让"给了很久了"这件事看不出来
            if not state.given:
                state.at = self._clock()
            state.given = True
            state.note = plan[:NOTE_CHARS]

    def reset(self) -> None:
        """清空这一条会话的门闸状态（**两半都清**：档位那条 + 研究流程那条）。"""
        with _LOCK:
            state = _STATES.get(self._key)
            if state is not None:
                _clear(state)
                _clear_research(state)

    # ------------------------------------------------------- 研究流程（状态机）

    @property
    def stage(self) -> str:
        """研究流程当前阶段（:data:`STAGE_IDLE` 表示没有流程在跑）。"""
        with _LOCK:
            return _STATES.setdefault(self._key, _State()).stage

    def open_research(self, decision: ResearchDecision) -> str:
        """进入"澄清"或"出计划"阶段（**进入条件由判据算出来**，见 :func:`decide_research`）。

        只在 ``needs_opening`` 时落状态：:data:`STAGE_EXECUTE` 那一档不动状态
        （明确需求不许留痕——留着的话下一轮的"确认"语义会被它污染）。
        """
        if not decision.needs_opening:
            return STAGE_EXECUTE
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            state.stage = decision.stage
            state.missing = tuple(decision.missing)
            state.estimated_steps = decision.estimated_steps
            state.reason = decision.reason
            state.plan = ""
            state.approval_id = ""
            state.confirmed_at = 0.0
            return state.stage

    def await_confirm(self, plan: str, approval_id: str) -> bool:
        """把"计划已交出、等用户确认"落成状态（**计划卡发出之后立刻调**）。

        只有 :data:`STAGE_PLAN` 能走这一步：从"澄清"直接跳"等确认"意味着澄清那一步
        被跳过了，那是调用方的错，这里**返回 False 而不是纠正它**——
        静默纠正会让"跳过澄清"这种 bug 一直看不出来。
        """
        text = (plan or "").strip()
        if not text or not approval_id:
            return False
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            if state.stage != STAGE_PLAN:
                return False
            state.plan = text[:PLAN_CHARS]
            state.approval_id = approval_id
            state.stage = STAGE_AWAITING
            return True

    def confirm_plan(self, approval_id: str = "") -> bool:
        """用户确认了（**审批通道那一侧**）：进入执行阶段。

        ``approval_id`` 对不上不算：计划卡与审批 id 是配对的，错配的表现是
        "上一张卡被确认、这一张计划却开跑了"。
        """
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            if state.stage != STAGE_AWAITING:
                return False
            if approval_id and state.approval_id and approval_id != state.approval_id:
                return False
            state.stage = STAGE_EXECUTING
            state.confirmed_at = self._clock()
            return True

    def confirm_by_text(self) -> bool:
        """用户**在下一轮打字**说"确认"（计划卡超时/没点时的第二条路）。

        与 :meth:`confirm_plan` 同一终点，区别只是"确认从哪来"：一个来自审批端点，
        一个来自用户的下一条消息（那种情况计划卡早就超时了，审批那边已经没有条目）。
        """
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            if state.stage != STAGE_AWAITING or not state.plan:
                return False
            state.stage = STAGE_EXECUTING
            state.confirmed_at = self._clock()
            return True

    def reject_plan(self, reason: str = "", approval_id: str = "") -> bool:
        """用户没确认（拒绝或超时）：退回 :data:`STAGE_PLAN`，计划留着。

        退回而不是清空：用户下一句多半是"改成 X"或"确认"，两种都要能接上——
        计划正文留着才有"再确认一次"这条可能。
        """
        with _LOCK:
            state = _STATES.setdefault(self._key, _State())
            if state.stage != STAGE_AWAITING:
                return False
            if approval_id and state.approval_id and approval_id != state.approval_id:
                return False
            state.stage = STAGE_PLAN
            state.reason = " ".join((reason or "").split())
            state.approval_id = ""
            return True

    def clear_research(self) -> None:
        """把研究流程收掉（用户换了话题、或流程走完开始新的一段）。"""
        with _LOCK:
            state = _STATES.get(self._key)
            if state is not None:
                _clear_research(state)

    def research_plan(self) -> str:
        """当前留着的计划正文（空 = 没有）。"""
        with _LOCK:
            return _STATES.setdefault(self._key, _State()).plan

    def research_snapshot(self) -> dict[str, object]:
        """研究流程那半边的只读快照（日志、界面与用例都用它，不去读私有字段）。"""
        with _LOCK:
            state = _STATES.get(self._key) or _State()
            return {
                "stage": state.stage,
                "missing": list(state.missing),
                "estimated_steps": state.estimated_steps,
                "plan": state.plan,
                "approval_id": state.approval_id,
                "reason": state.reason,
                "confirmed_at": state.confirmed_at,
            }

    def snapshot(self) -> dict[str, object]:
        """给排查/测试看的只读快照（界面暂时不用它）。"""
        with _LOCK:
            state = _STATES.get(self._key) or _State()
            return {
                "conversation_id": self._key,
                "plan_given": state.given,
                "at": state.at,
                "note": state.note,
                "generations": state.generations,
                # 上一轮看到的档：排查"为什么忽然又要求给计划"时，第一眼看的就是它
                "mode": state.mode,
            }


#: 进程级的状态表：会话 id → 状态。见模块头"状态放在哪里"。
_STATES: dict[str, _State] = {}
_LOCK = threading.Lock()


def _clear(state: _State) -> None:
    """把一段计划阶段收掉（**只能清档位那半边**，调用方持锁）。

    研究流程那半边**不在这里清**：它与档位无关（默认档是 goal），
    一起清掉的表现是"每一轮都重新问一次要不要出计划"。见模块头那段。
    """
    if state.given or state.note:
        state.generations += 1
    state.given = False
    state.at = 0.0
    state.note = ""


def _clear_research(state: _State) -> None:
    """把研究流程收掉（调用方持锁）。"""
    state.stage = STAGE_IDLE
    state.plan = ""
    state.approval_id = ""
    state.missing = ()
    state.estimated_steps = 0
    state.reason = ""
    state.confirmed_at = 0.0


def gate_for(conversation_id: str | None) -> PlanGate:
    """取一条会话的门闸。**同一个会话 id 拿到的是同一份状态**（进程内）。"""
    return PlanGate(conversation_id)


def reset_all() -> None:
    """清空所有会话的状态。**只给用例用**（进程级状态在用例之间会互相传染）。"""
    with _LOCK:
        _STATES.clear()
