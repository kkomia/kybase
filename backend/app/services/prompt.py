"""提示词贡献者：把"每轮怎么拼 system prompt"从一段 append 变成一张表（P1）。

为什么值得单独一层：提示词来源会不断变多（基础提示词、人设文件、库级提示词、记忆、
技能目录、摘要，以后还有工具说明与工作区信息）。手写一串 `parts.append` 的代价不是
代码长，而是**顺序与"缺了会怎样"变成隐式知识**——加一个来源就得回头读整段、
并重新推演一遍它的位置。

三条约定照 QwenPaw 的 PromptManager（它的实测理由：贡献者是可插拔的，
插件也能往里加一段）：

1. **优先级升序拼接** —— 顺序是数据，不是注释里的一句话；
2. **空块跳过** —— 不产生空行，也不留下"这里本来有东西"的痕迹；
3. **单个贡献者抛异常只跳过它自己** —— 提示词组装不该被任何一个来源拖垮。
   这条不是理论：某个来源读文件失败（磁盘、权限、编码）在真实部署里都会发生，
   而"人设文件读不出来导致整轮对话失败"是完全不可接受的因果关系。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "WRAP_UP_NOTE",
    "PromptContext",
    "PromptContributor",
    "build_system_prompt",
    "converge_note",
    "default_contributors",
    "setting_blocks",
]

logger = logging.getLogger(__name__)

#: 各来源的优先级。**数字之间留空档**是为了以后能插进来而不必重排全部。
#: 语义上：越靠前越像"我是谁"，越靠后越像"这一轮的上下文"。
PRIORITY_BASE = 10
PRIORITY_PERSONA = 20
PRIORITY_ARCHIVE = 21
"""**用户档案**那一档（§5.1，2026-10-03 档案制）。

"挂在人设那一档"这条口径有两个半的含义，三条都刻意：

- **位置紧挨人设**（21，就在人设之后）：它与 `SOUL.md`/`AGENTS.md` 是**同一类东西**
  ——每轮都该在场的设定，而不是这一轮才取的资料；
- **但是独立的一个贡献者**：它有自己的配置项（`memory.enabled`）与自己的开关语义，
  不与人设共用一份配置。挂进 `memory.persona_files` 的后果是：用户从那一行里
  删掉一个文件名，档案就**静默停止注入**（§7.2）；
- **跟着人设那一档之后**（而不是之前）：先"我是谁、这类活怎么干"，再"对方是谁、
  他在意什么"——后者是数据，越靠后越像数据，这一条与这份文件里其它优先级的
  语义一致。
"""
PRIORITY_KB_PROMPT = 30
PRIORITY_MEMORY = 40
PRIORITY_SKILLS = 50
PRIORITY_SKILL_USE = 51
"""「技能怎么查」那两句（§12.341 ③④）。

紧跟技能目录那一档：目录是**资料**，这两句是**用法**，挨着读才不会"看完目录不知道用哪个口"。
"""
PRIORITY_SUMMARY = 60
PRIORITY_CLARIFY = 65
"""「要不要先问一句」那一档（§12.338 后续：D-03 的裁定）。

排在摘要之后、检索收敛与首次引导之前：它是**这一轮的做事规矩**（越靠后越近），
而"先澄清还是直接开跑"是这一轮最先要决定的事。
"""
PRIORITY_SEARCH = 66
"""「检索什么时候算够」那一档（§12.338 后续：B-06 / Z-01 的裁定）。

紧跟在澄清那一段后面：先决定要不要问，再决定查多少。
"""
PRIORITY_BOOTSTRAP = 70
"""「还没认识对方」那一段**单独排在最末**（v0.52）。

原先它拼在 ``AGENTS.md`` 那一份的末尾（靠前），后面还跟着知识库提示词、技能目录、
摘要——而技能目录本身就是一份**能力清单**。实测（用户在界面上只发了一句"你好"）：
模型照那份清单回了一串"联网、笔记、记忆这些都正常"，**没有做首次引导**，
而引导块当时确实在提示词里（机制没问题，是话说在了没人听的地方）。

两条理由把它挪到最后：① 它是**关于这一轮该做什么**的指令，不是待读的资料——
按本文件的语义就该排"这一轮的上下文"那一档；② 越靠后越近，模型读到的最后一段
是它，而不是一份可以被复述的能力列表。
"""


@dataclass(frozen=True, slots=True)
class PromptContext:
    """这一轮拼提示词要用到的全部素材（都是**待拼的原文**，不是拼好的块）。"""

    base: str = ""
    """基础提示词。工具循环与检索链路各有一份默认，由调用方给。"""

    persona: tuple[tuple[str, str], ...] = ()
    """``[(文件名, 正文)]``，来自 `MemoryService.persona_texts`（按人设顺序）。

    v0.56 起只有 ``SOUL.md`` 与 ``AGENTS.md`` 走这条路——``PROFILE.md``（档案）
    是下面那个独立的 ``archive`` 贡献者，``MEMORY.md`` 已退场。"""

    archive: str = ""
    """**用户档案**那一段（已含边界说明），来自 `MemoryService.archive_block`。

    **未启用记忆、或档案还是空的时是空串**（由服务层判，这里不重复判）。
    它是独立的贡献者而不是拼进 ``persona``（§5.1）：那份配置只管人设的取舍与顺序，
    档案挂上去就会被一次编辑静默关掉。"""

    memory_guidance: str = ""
    """「用户档案怎么用」那一段，来自 `MemoryService.guidance`。

    **未启用记忆时是空串**（由服务层判，这里不重复判）：关着时 ``recall``
    会明确报错，再告诉模型"什么时候该去查"只会换来每轮一次无效调用。"""

    bootstrap: str = ""
    """「还没认识对方：这一轮该做一次开场」那一段，来自 `MemoryService.bootstrap_block`。

    **只在档案还是空模板时非空**（由服务层判）：Agent 一写进去它自己就没了，
    所以这一段不需要"用过就删"的簿记。

    **它是独立一块、而且排在整份提示词的最后**（``PRIORITY_BOOTSTRAP``，v0.52）：
    它说的是"这一轮该做什么"，而不是"我是谁"或"要读什么资料"——原先拼在 ``AGENTS.md``
    末尾时，后面还跟着一整份技能目录，实测模型会照着那份目录复述能力而不做引导。"""

    kb_prompt: str = ""
    memory: str = ""
    skills: str = ""
    summary: str = ""


#: ``(优先级, 名字, 产出)``。名字用于日志（"哪个来源没拼上"要知道是谁）。
PromptContributor = tuple[int, str, Callable[[PromptContext], str]]


def default_contributors() -> list[PromptContributor]:
    """内置贡献者表。顺序即模型读到的顺序（升序）。"""
    return [
        (PRIORITY_BASE, "base", lambda ctx: ctx.base),
        (PRIORITY_PERSONA, "persona", _persona_block),
        (PRIORITY_ARCHIVE, "archive", lambda ctx: ctx.archive),
        (PRIORITY_KB_PROMPT, "kb_prompt", lambda ctx: ctx.kb_prompt),
        (PRIORITY_MEMORY, "memory", lambda ctx: ctx.memory),
        (PRIORITY_SKILLS, "skills", lambda ctx: ctx.skills),
        (PRIORITY_SKILL_USE, "skill_use", _skill_use_block),
        (PRIORITY_SUMMARY, "summary", _summary_block),
        (PRIORITY_CLARIFY, "clarify", _clarify_block),
        (PRIORITY_SEARCH, "search", _search_block),
        (PRIORITY_BOOTSTRAP, "bootstrap", lambda ctx: ctx.bootstrap),
    ]


def build_system_prompt(
    context: PromptContext,
    contributors: Sequence[PromptContributor] | None = None,
) -> str:
    """按优先级把非空的块拼成一条 system 消息的内容。"""
    parts: list[str] = []
    table = sorted(contributors or default_contributors(), key=lambda item: item[0])
    for _priority, name, build in table:
        try:
            fragment = build(context)
        except Exception:
            # 见模块头第 3 条：一个来源坏了只少它自己
            logger.warning("提示词贡献者 %s 失败，跳过它", name, exc_info=True)
            continue
        text = (fragment or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


#: **人设文件**的总起句（``SOUL.md`` + ``AGENTS.md``，v0.56 起只有这两份）。
#:
#: 为什么必须有它：这几份原先只有**来源标签**（"这是你的人格"），而"所以要照着做"
#: 从来没有任何一句说过——要求散在通用 base 提示词里，模型完全可以读完当资料。
#: 实测的后果就是用户说的"全程没有生效"：文件确实每轮都进去了（占 system 提示词
#: 三成多），但提示词里没有一句"这是你的设定"。标签回答"它是什么"，
#: 这一段回答"它跟我有什么关系"。
#:
#: 三条约束都是刻意的：
#: - **不复述原文**：否则模型会把注入当成"需要转述的内容"，回答里出现文件腔；
#: - **冲突以对方当下为准**：文件是过去写的快照；不写的话，模型会拿几天前的偏好
#:   去反驳对方今天刚说的；
#: - **它可能过时**：所以读的时候要有分寸，而不是把它当成事实基准。
#:
#: 占位词那条规则（D26，2026-09-28 走查：17 条新会话里 14 条在问"怎么称呼你"）
#: **v0.56 起搬进了档案块**（`memory._ARCHIVE_LEAD`）：病灶那几行字原先在
#: `PROFILE.md` 里，现在它是档案，规则跟着它走——写在这里会让"人设"这一段
#: 去讲一份它已经不管的文件。
_PERSONA_LEAD = (
    "【你的身份与长期设定：请始终照此说话做事】\n"
    "下面几份文件是你的身份与你做事要守的规程——"
    "它们是设定，不是待读的资料：说话风格、判断口径、做事顺序都按它们来。\n"
    "不要向对方复述文件原文，直接照它行事；需要引用时用自己的话说。\n"
    "它们是过去某个时刻写下的快照，未必每句都还成立；"
    "与对方此刻所说的冲突时，以他此刻说的为准，并把这当成本轮的新事实。"
)


def _persona_block(context: PromptContext) -> str:
    """人设文件块：**总起句 + 每份前面标出它是什么**。

    标名字不是为了好看：模型得知道哪句是"该怎么说话"（人格）、哪句是"这类活怎么干"
    （规程）。少了这层区分，它会把两份文件读成一整段没有分工的资料。

    总起句只在**真有文件进来**时才给（一份都没有时这一块整体为空）：
    空挂着一段"请遵守以下设定"而没有下文，比不写更糟。

    「用户档案怎么用」（``memory_guidance``）挂在**操作规程那一份的末尾**，
    照 QwenPaw 把记忆指导拼进 ``AGENTS.md`` 那一段的做法：这是"这类活怎么干"的
    一部分，单列成一块会让它读起来像另一份待读的资料。没有 ``AGENTS.md`` 时
    退化成独立一块——总比把整段指导丢掉好。

    **档案不在这一块里**：它是独立的贡献者（``PRIORITY_ARCHIVE``），理由见那里的说明。
    """
    blocks: list[str] = []
    agents_at: int | None = None
    for name, text in context.persona:
        body = text.strip()
        if not body:
            continue
        label = _PERSONA_LABELS.get(name, name)
        if name == "AGENTS.md":
            agents_at = len(blocks)
        blocks.append(f"【{label}（{name}）】\n{body}")
    guidance = context.memory_guidance.strip()
    if guidance:
        # 「长期记忆怎么用」挂在**操作规程那一份的末尾**（照 QwenPaw 把记忆指导拼进
        # AGENTS.md 那一段的做法）：它属于"这类活怎么干"，单列成一块会读起来像
        # 另一份待读的资料。没有 ``AGENTS.md`` 时退化成独立一块——总比丢掉好。
        if agents_at is None:
            blocks.append(guidance)
        else:
            blocks[agents_at] = f"{blocks[agents_at]}\n\n{guidance}"
    if not blocks:
        return ""
    return "\n\n".join([_PERSONA_LEAD, *blocks])


_PERSONA_LABELS = {
    "SOUL.md": "你的人格",
    "AGENTS.md": "操作规程",
    # ``PROFILE.md`` 从 v0.56 起是**档案**、走独立贡献者，正常不会再出现在这里；
    # 留着这个标签只是兜底（真出现时至少标对它是"对方的资料"，而不是裸文件名）。
    "PROFILE.md": "身份与对方",
}


#: 检索链路那几块的（优先级, 名字, 产出）表：**人设 + 档案 + 首次引导**。
#:
#: 检索链路（`chat.agent_enabled=false`）不走总表——它只有一条 system 消息，
#: 资料块要并进同一条（见 ``chat.build_messages``），顺序由那条链路自己定。
#: 但这三块的**样子与相对顺序**必须与工具循环那条**同一口径**：同一次对话换个链路，
#: 模型对"我是谁、对方是谁"的认知不该跟着变。所以这里复用同一批贡献者与同一个
#: 拼接器（`build_system_prompt`），只是把表缩到这三块。
#:
#: **它必须定义在 `_persona_block` 之后**（那份贡献者是它的一部分）。
_SETTING_CONTRIBUTORS: tuple[PromptContributor, ...] = (
    (PRIORITY_PERSONA, "persona", _persona_block),
    (PRIORITY_ARCHIVE, "archive", lambda ctx: ctx.archive),
    (PRIORITY_BOOTSTRAP, "bootstrap", lambda ctx: ctx.bootstrap),
)


def setting_blocks(context: PromptContext) -> str:
    """**人设 + 用户档案 + 首次引导**这三块（检索链路用它拼 "memory" 那一块）。"""
    return build_system_prompt(context, _SETTING_CONTRIBUTORS)


def _summary_block(context: PromptContext) -> str:
    if not context.summary:
        return ""
    head = "【此前对话的摘要】（用于保持上下文）"
    return f"{head}{chr(10)}{context.summary.strip()}"


# ------------------------------------------------------- 这一轮怎么做事（§12.338 后续）
#
# 两段**常驻**规矩（与下面"预算提示"那两段不同：那两段只在预算用尽时出现）：
#
# 1. **要不要先问一句**：D-03（"帮我整理一份研究报告"那种）预期"先确认范围/深度/格式"，
#    实测**一句没问直接开跑** 39 步 / 75s；而 Z-06 那次主动问了 4 个关键问题——
#    说明机制在、只是不稳定。裁定：**只在"歧义大 + 代价高"时先问 1~2 个关键问题**，
#    其余直接开跑、但**必须在回答里写明假设**（两边都要说清，否则模型只学会其中一半）。
# 2. **检索什么时候算够**：B-06 为查一本不存在的书搜了 9 次、Z-01 搜了 35 次。
#    官方 Kimi 的公开口径是"chat 模式下有选择地只用一部分工具、并减少工具调用轮数"
#    （K2 Thinking 博客）；它的官方轨迹（`samples/official_trace.json`）里 **43 次调用
#    全是 search、且只有最近 3 次的返回还留着正文**（更早的都是"历史tool_call已隐藏"）。
#    我们这边的第一级压缩（`chat.prune_tool_results`，保留最近 5 条工具结果、其余换成
#    占位符）**已经覆盖**"只留最近几轮结果"这条上下文政策，所以这里只补**判据**：
#    重复同参不搜、同一事实 ≥2 个独立来源就停、零命中换路子最多两轮。


#: 「先问还是先做」的判据（§12.338 后续的政策裁定）。
#:
#: 关键是**两条同时成立才问**：只有歧义（"随便聊聊"）或只有代价（"帮我算 1+1 很多遍"）
#: 都不该问。缺一半就问，会把日常小事变成问卷——用户报过的"怎么老反问我"就是它。
_CLARIFY_BLOCK = (
    "【什么时候先问一句：歧义大 + 代价高，两条同时成立才问】\n"
    "先问之前先自己判断两件事，两条都成立才问，而且**一次把 1~2 个关键问题问完**，"
    "不要来回问：\n"
    "1. **歧义大**：范围、深度、交付形式、口径里有一样缺失，而不同的理解会得出不同的答案"
    "（例如「整理一份报告」没说给谁看、要多长、要什么格式；"
    "「调研某行业」没说只看国内还是要对比国外）；\n"
    "2. **代价高**：这一轮要跑很多步、要上网很久，或者做错了要返工（要出文件、要跑代码、"
    "要交一份成型的东西）。\n"
    "其余情况**直接开跑**，但必须在回答里写明你的**假设**（「我按 X 前提答的」）——"
    "把前提摆出来比反问一句更省事，用户看完可以直接纠正。\n"
    "题目本身已经含着对象与范围（「调研 2026 中国储能行业格局」）→ **不许再问**。\n"
    "这是干活，不是寒暄：不要用一句反问把活推回去。"
)


#: 「检索到什么时候算够」的判据（**照搬 Kimi deep-research 的停止口径**）。
#:
#: 出处：`docs/归档/调研/Kimi-Resources-能力与实现-照搬清单.md` 第 2 条（机制级）——
#: https://www.kimi.com/features/deep-research 行 23 / 73：
#: 「执行**数十次**精准检索」「不断迭代直到**积累足够内容撰写全面报告**」。
#:
#: **这里原本有我们自造的三个数字，已经删掉**（同一份清单的"替换表"第 2~4 行）：
#: 「先做 2~3 次」「同一事实 ≥2 个独立来源就停」「零命中最多再试两轮」——
#: 它们与"每任务数十次检索"**方向相反**，会把研究型任务提前掐断。
#: 现在只留两条**可执行**的判据：
#:
#: - **产出判据**：够不够写出一份覆盖问题各面的完整报告（他们的原话口径）；
#: - **预算判据**：预算闸自己会提醒（`tool_loop.CONVERGE_RATIO` + `prompt.converge_note`，
#:   如实报还剩几步 / 几秒）——所以**不要自己另定一个搜索次数上限**。
#:
#: 另留两条**机器能核对**的纪律（官方轨迹也支持）：同参不重复发、返回里已有正文就不抓页。
_SEARCH_BLOCK = (
    "【检索到什么时候算够：够写一份完整报告 + 预算闸】\n"
    "（这一轮手上有检索 / 联网工具时才适用。）\n"
    "- **产出判据**：停下与否看手里攒到的东西**够不够写出一份覆盖问题各面的完整报告**。"
    "研究 / 调研这类活**执行数十次精准检索是正常的**，不要因为「已经搜了几次」就提前收尾；\n"
    "- **预算判据**：步数或时间快到上限时，系统会给你一条【预算提醒】（如实报还剩几步 / 几秒），"
    "**那才是该收尾的信号**；不要自己另定一个搜索次数上限；\n"
    "- 两条一直适用的纪律（可机器核对）：\n"
    "  1. **同一个查询不要重复发**——参数一模一样的那一次不会带来新信息；\n"
    "  2. **返回里已经有正文的，不必再抓页**；只有需要原文细节（具体数字、引文、表格）时，"
    "才去抓那一两个最有希望的候选页；\n"
    "- 一次检索**零命中**就换关键词或换路子（换措辞、换语言、换更宽或更窄的说法）——"
    "没有次数上限，停不停由上面两条判据决定；确实找不到时，**如实说「没有找到」**"
    "并给出你能确定的那部分。"
)


def _clarify_block(_context: PromptContext) -> str:
    """「先问还是先做」那段（常驻）。**不看上下文**：它是规矩，不是素材。"""
    return _CLARIFY_BLOCK


def _search_block(_context: PromptContext) -> str:
    """「检索什么时候算够」那段（常驻）。同上，不看上下文。"""
    return _SEARCH_BLOCK


#: 「技能怎么查」那两句（§12.341 ③④，2026-09-29 用户点名的会话 `conv_a5f4628f405f`）。
#:
#: 现场：为了"下载知网论文"，它**连 6 步**用 `..\..\` 去扫**服务端**磁盘找技能
#: （`dir /a ..\..\..`、`findstr /s … ..\..\skills\*\SKILL.md`），而不是用技能工具；
#: 而在那之前还先调了两次 `find_tools` 才找到技能——技能目录**每一轮都注入在提示词里**。
#:
#: 两句各堵一条路，都写成"不许做什么"（比"建议用什么"更能拦住一次已经跑偏的选择）：
#:
#: 1. **用技能工具，不许扫盘**：`find`/`dir`/`ls`/`findstr` 找 `SKILL.md` 既慢、
#:    又会扫到服务端别的目录，而 `list_skills`/`read_skill` 本来就是给这件事的通道
#:    （还知道"哪些被丢弃 / 被同名遮蔽"，扫盘看不到这层信息）；
#: 2. **找技能不必先 `find_tools`**：技能不在那张工具表里（它是"读自己身上的说明书"，
#:    见 `agent_tools` 模块头），先查一遍工具表纯属白跑两步。
#:
#: 只在**真有技能目录**时给（`context.skills` 非空）：一份技能都没有时，
#: 上面那句"目录每轮已注入"是假话，说了只会让模型去找不存在的东西。
_SKILL_USE_BLOCK = (
    "【技能怎么查】\n"
    "1. 技能目录已经在上面了，要用哪个技能直接读：`list_skills` 看状态、`read_skill` 读正文；"
    "**不许用 `find` / `dir` / `ls` / `findstr` 去磁盘上扫技能**"
    "（那样会扫到服务端的目录，慢且容易找错）。\n"
    "2. **找技能不必先 `find_tools`**：它不在那张工具表里，技能目录每轮已经注入过了。"
)


def _skill_use_block(context: PromptContext) -> str:
    """「技能怎么查」：只有真有技能目录时才给（理由见 `_SKILL_USE_BLOCK`）。"""
    return _SKILL_USE_BLOCK if context.skills.strip() else ""


# --------------------------------------------------------------------- 预算提示（§12.338 方案 3）
#
# 这两段**不是** system prompt 的一部分（它们不参与 `build_system_prompt` 的拼接）：
# 它们是"预算快用完"那一刻才插进对话的一条指令，由 `tool_loop` 在那一刻送进去。
# 放这一层是因为它们是**写给模型的话**——措辞要与其它提示词一起演进、也便于单测。
#
# 来历（2026-09-29 能力清单实测，`.shots/cases-api/G-03.json` / `K-03.json`）：
#   - G-03（要"带图表的 Excel"）：80 步 / 205s / 首 token 203s，数字全对但没图表。
#     模型自陈"中间走偏了（去翻后端代码找产物落盘位置），把工具步数用完了"；
#   - K-03（运行代码画图）：60 步 / 361s，交付物 0，全耗在"怎么装 matplotlib"上，
#     收尾还是悬着的（"检查一下装到哪了，再重跑一次补完"）——而这一轮已经结束了。
#
# 两条都不是能力不够，而是**没有人叫停、也没有人要求它交个底**：模型不知道预算还剩多少，
# 于是把预算打满；打满之后随口许一个"下次补完"，用户拿到的是一句无法执行的承诺。


def converge_note(*, steps_left: int | None = None, seconds_left: float | None = None) -> str:
    """「预算快到八成，该收尾了」那条提示（§12.338 方案 3 的第一半）。

    三条分寸：

    1. **如实报数**（"工具调用还剩 6 步"）：含糊说"快没时间了"模型不会换算成"该收口了"，
       一个具体数字才会——这也是"把还剩几步告诉它"的原话；
    2. **只说预算与动作，不替它判断任务**：这一轮做到哪一步算够，仍是它自己按任务判断；
    3. **只在接近耗尽时出现一次**（比由 `tool_loop.CONVERGE_RATIO` 定）：预算充足时
       一个字都不出现——正常的长任务不该被这条提示带着提前收口。
    """
    limits: list[str] = []
    if steps_left is not None:
        limits.append(f"工具调用还剩 {max(0, steps_left)} 步")
    if seconds_left is not None:
        limits.append(f"本轮时间还剩约 {max(0, int(seconds_left))} 秒")
    budget = "、".join(limits) if limits else "这一轮的预算快用完了"
    return (
        f"【预算提醒】{budget}。\n"
        "请开始收尾：把已经拿到的结果整理成回答，别再开新的探索方向"
        "（换个装法、翻源码找线索、把同一个查询再发一遍都算）。\n"
        "如果还差关键的一步，就把它放在接下来的第一步做，并确保它直接产出交付物。"
    )


#: 「收尾必须交代」那条格式要求（§12.338 方案 3 的第二半）。
#:
#: 为什么必须**强制**：G-03 / K-03 的收尾都是悬着的（"再重跑一次补完"），而那一轮已经结束。
#: 一个把预算用完的回合，回答里必须能读出四件事——做成了什么 / 没做成什么 / 为什么 /
#: 下一步怎么办。前两件是事实，第三件是"缺哪个能力或环境"，第四件是**现在**能做的动作。
#:
#: 为什么专门禁掉"下次再补"：那句话对用户没有任何可执行信息，还让他以为"东西马上就好"。
#: 要交代下一步，就写他此刻能做的（换个要法、先装什么、改成哪种格式）。
WRAP_UP_NOTE = (
    "【收尾要求：这一轮的预算已经用尽，现在给出最终回答】\n"
    "直接在正文里把话交代清楚，四件事一件都不能少：\n"
    "1. 已经做成了什么（拿到了什么结果、产出了哪些文件）；\n"
    "2. 哪部分没做成（若都做成了，就说清哪几处只是够用、并不完整）；\n"
    "3. 没做成的原因（缺哪个能力、哪个环境依赖、哪一步被拦下）；\n"
    "4. 下一步怎么办——写对方此刻就能做的动作（换个要法、先装什么、改用哪种格式）。\n"
    "不许用「我再跑一次补完」「稍后再试」这类悬着的收尾：这一轮已经结束，"
    "那样的句子等于什么都没交代。\n"
    "先给结论与已经拿到的东西，再交代这四件事；不要整篇都是道歉。"
)
