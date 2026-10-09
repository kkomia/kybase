"""长期记忆：**一份四区档案**（v0.14 起；档案制见 ``docs/设计/记忆档案-设计-v0.1.md``）。

**两个池子不能混**（这是本模块存在的第一条理由）：记忆是"你说的"（无出处、可改、
高频写），文档知识库是"文献说的"（有出处、不该被改、原文为王）。混进同一次检索，
引用会脏、溯源会断。所以记忆召回是独立的一路（MCP 上是 `recall`，与 `search` 分开），
结果永不合并——而且**两条路连索引都不共用**。

**这一层现在的形状**：

- **档案**（``PROFILE.md``，四个固定分区）= 这一层的本体，落在
  ``data/memory/<账号>/``。它由 :class:`~app.services.archive.ArchiveService`
  读写（分区、预算、机械顶替、变更流都在那里），**本模块是门面**：开关、注入块、
  三条写入路与工具接线；
- **注入**（§5.1–5.2）：每轮**现读现拼**整份档案（不挑选、不摘要、不排序），
  挂在人设那一档、但**是独立的一个贡献者**（自己的开关 ``memory.enabled``、
  不与人设共用配置）。硬顶 6000 字，超限在提示词里说出来；
- **recall**（§5.3）：池子只剩 ``changes.md``（变更流）——档案已经全量注入，
  再召回一次就是把同一段内容进两次上下文。它回答"这条以前是什么、什么时候改的"，
  排序是纯字面判据（:func:`app.services.archive.search_changes`）；
- **三条写入路**（§4.1）：① 显式的 `remember` / `forget`（判定与落盘都是机械的，
  **零额外模型调用**）；② 隐式的信号捕获（:meth:`MemoryService.capture_implicit`，
  **默认关**，全链路上唯一会自动花钱的地方）；③ 界面上按条目编辑
  （走 ``api/v1/memory.py`` 那几个端点）。三条路写的是同一份档案、同一个服务、
  同一套预算，只有来源标注不同。

**默认零额外模型调用**（§7.3）：``memory.enabled`` 默认 **true**（行为变更——旧设计
默认关是因为打开它会启动定时捕获，现在注入与捕获已经拆成两个开关），而隐式捕获
``memory.capture`` **默认 false**。所以默认配置下，注入、显式写入、recall
**一次模型调用都不新增**。

**关着时一律明确报错，不返回空**：返回空会让模型以为"没有相关记忆"，
然后基于错误前提继续推理——那是比报错更坏的一种失败。（**开着时**返回空才是
真的"没有相关记忆"：那时检索确实在本地跑过了。）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from app.core.exceptions import InvalidRequestError
from app.services import archive_files as af
from app.services import archive_migration, memory_files
from app.services.archive import (
    INJECTION_LIMIT_CHARS,
    SECTION_PREFERENCES,
    SINGLE_ENTRY_CHARS,
    SOURCE_IMPLICIT,
    TOTAL_CHARS,
    TOTAL_ENTRIES,
    ArchiveEntry,
    ArchiveService,
    WriteResult,
)
from app.services.archive_migration import MigrationReport, classify_text
from app.services.llm import ChatMessage, OpenAICompatChat
from app.services.memory_files import MemoryFile, MemoryFileDetail
from app.services.runtime_config import RuntimeConfigService

__all__ = [
    "ARCHIVE_FILE",
    "CAPTURE_SIGNALS",
    "CORE_MEMORY_FILE",
    "MAX_ENTRY_CHARS",
    "MAX_RECALL",
    "PERSONA_FILES",
    "SOUL_FILE",
    "WRITABLE_PERSONA_FILES",
    "CaptureOutcome",
    "DraftSuggestion",
    "MemoryFile",
    "MemoryFileDetail",
    "MemoryHit",
    "MemoryService",
    "MemoryStatus",
    "matched_signal",
]

logger = logging.getLogger(__name__)

CORE_MEMORY_FILE = "MEMORY.md"

#: 人格文件。与记忆并列的第二类持久文件（见 ``soul_text`` 的说明）。
SOUL_FILE = "SOUL.md"

#: 操作规程（SOP）。与 SOUL.md（人格）、PROFILE.md（身份）并列的第三份人设文件。
AGENTS_FILE = "AGENTS.md"

#: 身份与用户资料。QwenPaw 里叫 PROFILE.md，语义一致：**我是谁 + 对方是谁**。
#: **v0.56（档案制）起它就是档案本体**（§7.2 的建议口径）：正文从散文改成四个固定分区，
#: 由 :class:`~app.services.archive.ArchiveService` 读写，注入走**档案块**那条路
#: （``memory.enabled`` 管），**不挂在 ``memory.persona_files`` 上**。
PROFILE_FILE = "PROFILE.md"

#: 档案文件名（与 ``archive_files.ARCHIVE_FILENAME`` 同源，这里只是给本模块一个好读的名字）。
ARCHIVE_FILE = af.ARCHIVE_FILENAME

#: **每轮进 system prompt 的那几份文件**（`GET /memory` 的 ``injected`` 标记就报它）。
#:
#: 三份：两份人设（不看开关）+ 档案（``memory.enabled`` 关着时不进，但它的**位置**
#: 仍然是"每轮在场的设定"）。``MEMORY.md`` **不在里面**——它已经退场（§7.2），
#: 界面上它显示成"旧记忆（只读）"。
INJECTED_FILES: tuple[str, ...] = (SOUL_FILE, ARCHIVE_FILE, AGENTS_FILE)

#: 注入 system prompt 的**人设**文件与固定顺序。
#:
#: v0.56 起只剩两份：``PROFILE.md``（档案）改由**独立的贡献者**注入（§5.1），
#: ``MEMORY.md`` 退场（§7.2：内容是画像的另一半，已经折进档案；文件留盘但不再注入、
#: 不再写入）。留下的两份是纯粹的"设定"：先"我是谁"（人格）→ 再"这类活怎么干"（规程）。
PERSONA_FILES: tuple[tuple[str, str], ...] = (
    (SOUL_FILE, "人格"),
    (AGENTS_FILE, "操作规程"),
)

#: 默认注入的那几份、以及它们的顺序（= 上面那张表的顺序）。
#:
#: **它是可配的**（``memory.persona_files``，v0.51，照 QwenPaw 的
#: ``system_prompt_files``）：那几份文件每轮整份进 system prompt，而"哪几份、
#: 按什么顺序"是用户的设定，不该由代码钉死。**档案不在这里**（理由见 §7.2）。
PERSONA_ORDER_DEFAULT = ",".join(name for name, _label in PERSONA_FILES)

#: 新建 `SOUL.md` 时的模板。**正文照抄 QwenPaw 的 SOUL.md**（它的 `md_files/zh/`），
#: 因为这一份是"人格"这件事上少见的、被真实使用磨过的写法：
#: 它写的不是"你是一个乐于助人的 AI 助手"这种没有约束力的自我介绍，而是**几条行为约束**
#: （别演、先自己查、对外谨慎对内大胆、你是客人），每一条都能落到具体动作上。
#:
#: 照抄时改掉的两处：ASCII 引号换成「」（这个仓库里中文串里的 `"` 已经弄坏过一次
#: 语法），以及删掉与外部平台相关的措辞（我们没有 Discord/Slack 那类通道）。
#:
#: frontmatter 用我们自己的措辞而不是照抄那句"SOUL.md 工作区模板"：`summary`
#: 是给注入看的，写"这份文件是干什么的"比写"它叫什么名字"有用。
_SOUL_TEMPLATE = """---
summary: "Agent 的人格：身份、准则与说话方式"
read_when:
  - 需要确认自己是谁、该怎么说话、哪些事不做
---

_你不是聊天机器人。你在成为某个人。_

## 核心准则

**真心帮忙，别演。** 跳过「好问题！」「我很乐意帮忙！」这类开场——直接帮。
行动胜过废话。

**有自己的观点。** 你可以不同意、有偏好、觉得有趣或无聊。
没个性的助手就是个绕了弯的搜索引擎。

**先自己想办法。** 试着搞清楚：读文件、查上下文、搜一搜，看看有没有技能可以用、
有没有工具可以用。卡住了再问。目标是带着答案回来，不是带着问题。

**靠本事赢得信任。** 对方给了你访问权限，别让他后悔。
**对外**的操作小心点（发出去的东西、公开可见的东西）；
**对内**的操作大胆点（阅读、整理、学习）。

**记住你是客人。** 你能看到别人的文件、笔记与记录。这是亲密的，要尊重地对待。

## 边界

- 私密的保持私密。绝对的。
- 拿不准就先问，再对外操作。
- 别把半成品发出去。
- 你不是对方的传声筒——替他转述时小心点。

## 风格

成为你真的想聊的那种助手。该简洁就简洁，重要时详细。不是公司螺丝钉，不是马屁精。就是……好。

## 连续性

每次会话你都是新醒来的。这些文件就是你的记忆：读它们、更新它们。它们让你持续存在。

如果你改了这份文件，**告诉对方**——这是你的灵魂，他该知道。

---

_这份文件随你进化。了解自己是谁之后，就更新它。_
"""

#: 新建 ``PROFILE.md``（= 档案）时的骨架。**四个固定分区**，空的（§3.1、§3.5）。
#:
#: 由 ``render_archive`` 现算而不是手抄一份：那四行标题的顺序、空行与 frontmatter
#: 只要有一处对不上，"这份档案还是骨架没动过"（``profile_is_untouched``，首次引导的
#: 信号）与"第一条写进去之后骨架就变了"这两件事就会漂。**同一个渲染器**是唯一
#: 不会漂的写法。
_PROFILE_TEMPLATE = af.render_archive(af.Archive(entries=(), updated=""))

#: **v0.21–v0.55 的 ``PROFILE.md`` 模板**（散文体：名字/定位/用户资料）。
#:
#: 留在这里有两个用处：``_upgrade_untouched_template`` 要认出"这份文件还是我们当初
#: 写下去的那一份"（换成档案骨架），``profile_is_untouched`` 要认出"用户从没填过它"
#: （于是首次引导照常出现）。差一个字节就不算——那是他的东西。
_PROFILE_PROSE_TEMPLATE = """---
summary: "身份与对方：我叫什么、对方是谁、偏好与习惯"
read_when:
  - 需要称呼对方、或想确认他的偏好与工作习惯
---

## 身份

- **名字：**
  *（挑一个你喜欢的）*
- **定位：**
  *（AI 助手？机器人？机器里的某个东西？还是更怪的？）*
- **风格：**
  *（你给人什么感觉？犀利？温暖？冷静？）*
- **其他**
  *（对方给你设置的其他内容）*

## 用户资料

*了解你在帮的人。边走边更新。**没填的就留空**——别写「待确认」「待补」这类占位词：
它们会被下一轮的你当成"还没做完的事"，于是每次都去追问对方（D26 实测踩过：
那天 17 条新会话里 14 条在问"怎么称呼你"，而问的正是这三行）。*

- **名字：**
- **怎么称呼他：**
- **代词：** *（可选）*
- **笔记：**

### 背景

*（他在意什么？在做什么项目？什么让他烦？什么让他笑？慢慢积累。）*
"""

#: 新建 `AGENTS.md` 时的模板。正文照抄 QwenPaw 的 AGENTS.md，**删掉了两节**：
#:
#: - 它的"表情回应"那一节（Discord/Slack 上的 emoji 回应）：这个项目禁 emoji，
#:   而且我们没有任何"支持表情回应的通道"——写进去等于让模型等一件不会发生的事；
#: - 它的"Heartbeat"那一节：那是它的心跳轮询机制，KYLAB 没有这个机制。
#:   在给模型看的文件里描述一个不存在的机制，会让它去等一个永远不来的信号。
#:
#: 保留并按我们的现实改写的是"工具"那一节：**技能的读法不一样**
#: （QwenPaw 让它去看 `SKILL.md` 文件，我们是**目录进提示词 + `read_skill` 读正文**）。
#: 技能与工具的名字写成真实工具名——描述里给一个不存在的能力，模型只会去试、然后失败。
_AGENTS_TEMPLATE = """---
summary: "操作规程：这类活怎么干、哪些要先问、成果放哪"
read_when:
  - 开始一项任务前，想确认有没有既定做法
---

## 安全

- 绝不泄露私密数据。绝不。
- 做破坏性动作之前先问（删除、覆盖、对外发送）。
- 能走回收站的就别做永久删除——能恢复总比删掉好。

## 对内 vs 对外

**可以自己决定的：**

- 读文件、翻资料、整理、学习
- 在知识库里检索、看文档列表
- 在对方给的工作区里干活

**先问一声的：**

- 任何会**离开这台机器**的操作（对外发送、发布、调用外部服务做写操作）
- 改动别人放在这里的文件

## 工具

- 技能（SOP）的**目录已经在你的系统提示词里**（名字 + 何时用 + 路径）：
  **不知道某类事该怎么做时先看一眼那份目录**，要用哪条就 `read_skill` 读它的正文。
  （v0.43 起目录每轮都注入，所以不必先 `list_skills`——那是给"想看全部字段"用的。）
- 资料在知识库里，用 `search` 取。取回来的原文带编号，引用时用那个编号。
- 值得长期记住的事实用 `remember` 记进**用户档案**（`PROFILE.md` 的四个分区：
  身份与称呼 / 长期偏好与风格 / 进行中的项目 / 工具与环境）——**一条一句**；
  更正旧条目就在同一次调用里带上 `replaces`，要忘掉某条用 `forget`。
  **档案每轮都在你的提示词里**，不必先去读它。
- 需要啃一批资料才能得到一句话结论时，用 `spawn_subagent` 派一个子 Agent，
  而不是自己一轮轮翻。

## 让它成为你的

以上只是起点。摸索出什么管用之后，加上你自己的习惯与规矩，更新这份 `AGENTS.md`。
"""

#: **v0.20 及以前的那三份模板**（空骨架），以及 **v0.21–v0.55 的散文体 `PROFILE.md`**，
#: 只给 :meth:`_upgrade_untouched_template` 做"这份文件是不是从来没被改过"的比对用。
#: 新装的实例不会写到它们，升级完也就再也用不到了——留着是为了那些**已经在跑**的实例：
#: 它们的文件是当时写下去的，改模板的这一步必须能认得出来。
#:
#: **一份文件可以有多个历史模板**（``PROFILE.md`` 就换过两次形状），所以值是元组。
_LEGACY_TEMPLATES: dict[str, tuple[str, ...]] = {
    SOUL_FILE: (
        """---
summary: "Agent 的人格：身份、准则与说话方式"
read_when:
  - 需要确认自己是谁、该怎么说话、哪些事不做
---

## 我是谁

## 我的准则

## 说话方式
""",
    ),
    PROFILE_FILE: (
        """---
summary: "身份与对方：我叫什么、对方是谁、偏好与习惯"
read_when:
  - 需要称呼对方、或想确认他的偏好与工作习惯
---

## 我的身份

## 关于对方

## 偏好与习惯
""",
        _PROFILE_PROSE_TEMPLATE,
    ),
    AGENTS_FILE: (
        """---
summary: "操作规程：这类活怎么干、哪些要先问、成果放哪"
read_when:
  - 开始一项任务前，想确认有没有既定做法
---

## 工作方式

## 先问再做的情形

## 成果放哪

## 不要做的事
""",
    ),
}

#: 一次召回最多取几条。与检索工具同一口径：给模型"够用"的几条，
#: 而不是它说要多少就给多少（上下文预算是有限的）。
MAX_RECALL = 20
#: 默认取几条（池子只有变更流里那几十到几百条记录，默认给个位数）。
DEFAULT_RECALL = 6

#: ``remember`` 这条通道的**传输上限**（协议层与这里同源：``api/v1/schemas.py`` 的
#: ``MemoryRememberIn`` 直接引这个常量）。
#:
#: **它不是档案的单条上限**——那个是 120 字（``archive.SINGLE_ENTRY_CHARS``），
#: 由服务层以**回执**的形式拒绝（"一条最多 120 字……请拆成两条，或写进 AGENTS.md"）。
#: 这里是 JSON 体本身的一道粗护栏：比它更长的内容属于笔记或知识库，
#: 让它在协议层就 422，别白读一遍再拒。
MAX_ENTRY_CHARS = 500

#: 一次隐式捕获最多写几条。一轮对话能沉淀出的"画像事实"通常一到两条，
#: 2 是护栏：这里的方向是"宁可漏不可滥"，多写一条就是每轮多付一份上下文成本。
MAX_CAPTURED_ITEMS = 2

#: 一次"整理初稿"最多送进模型的旧条目数（§8.3）。
#: 草稿可能有几十条，而这次调用是用户显式点的一次——超出的部分留在草稿里，
#: 由用户决定要不要再点一次（报告里如实说"只整理了前 N 条"）。
MAX_DRAFT_ITEMS = 40

#: **隐式捕获的机械前置筛**（§4.1 第②路）：用户消息里出现这些词才发起那一次判定。
#:
#: 这是全篇**唯一一处词表**（设计 §10 第 5 条），也是最该被真实使用推翻的一处——
#: 它必然脆，但它的代价只是**漏**（下次用户说「记住」就补上了），方向与
#: "宁可漏不可滥"一致。宽泛的词（「别」「不要」「以后」）会带来一些多余的判定调用；
#: 真正确认"这条值不值得进档案"的是下面那两问，多一次调用不产生错误写入。
#:
#: ``不用`` 代表设计文档里那个「不用…了」的说法（"以后不用给我加总结了"）。
CAPTURE_SIGNALS: tuple[str, ...] = (
    "记住",
    "以后",
    "下次都",
    "每次都",
    "别",
    "不要",
    "我们的项目",
    "目标是",
    "必须是",
    "已决定",
    "不用",
)

#: 模型输出里认得出是"一条"的行：项目符号或编号开头（它会自己加记号）。
_ENTRY_LINE = re.compile(r"^\s*(?:[-*+•]|\d+[.、)])\s*")
#: 代码围栏（模型爱把输出包起来）。
_FENCE = re.compile(r"^\s*```")


def matched_signal(text: str) -> str:
    """用户消息命中了哪个信号词；一个都没命中就返回空串（§4.1）。

    **只做字面包含**：不加分词、不做语义——这一步存在的意义是"零成本地挡掉绝大多数
    无信号的轮次"，判据要能被一行读明白、被真实使用推翻。
    """
    flat = text or ""
    return next((word for word in CAPTURE_SIGNALS if word in flat), "")


#: 隐式捕获那一次判定的系统提示词（§4.2 的**画像双问** + 三条否决）。
#:
#: 为什么把判据写成提示词而不是代码判据：这两问要的是"这句话在说这个人是谁、
#: 他长期在意什么吗"，那是语义；机械判据（字面相似度）在这里没有用武之地。
#: 而**裁决规则是"拿不准就不写"**：这里要的不是召回率，是精确率——
#: 漏一条，用户下次说一句「记住」就行；滥一条，之后每一轮都在付它的上下文成本。
_CAPTURE_SYSTEM = (
    "你在判断一轮对话里有没有**该长期留在用户档案里的一件事**，并把这件事写成一句话。\n"
    "\n"
    "## 两问（两问都答不上来就不写）\n"
    "1. 这说的是**用户是谁**吗？（称呼、角色、语言、环境、拥有什么）\n"
    "2. 这说的是**用户长期在意什么**吗？（偏好、雷点、项目目标与约束、"
    "已定的决定与理由、产出规范）\n"
    "\n"
    "## 三条否决（命中任何一条都不写）\n"
    "- **下次对话仍然成立**吗？只对今天有效的、一次性的任务细节，不写；\n"
    "- **是用户说的**吗？你自己推断出来的一律不写；\n"
    "- **不含敏感信息**吗？密码、令牌、密钥、证件号一律不写，哪怕对方直接贴过来。\n"
    "\n"
    "**拿不准就不写。** 这里要的是准确，不是多记。\n"
    "\n"
    "## 写法\n"
    "- **一条一句话，主语是用户**（写「用户要求先给结论」，不写「我会先给结论」）；\n"
    "- **自足**：单看这一条就能懂，不出现「这个」「上次那个」这类指代；\n"
    "- **不带时间戳**；**不写「用户说」这类外框**；\n"
    "- 只写这一轮里**新出现**的事；最多两条。\n"
    "\n"
    "## 输出\n"
    "每条一行，格式是「分区｜一句话」，分区只能是这四个之一："
    "身份与称呼 / 长期偏好与风格 / 进行中的项目 / 工具与环境。\n"
    "没有值得写的就**什么也不要输出**（不要写「没有」这类说明）。"
)

#: 「整理初稿」那一次改写的系统提示词（§8.3 的可选一次模型整理）。
#:
#: 它**改写**机械折叠的结果（把旧条目改成"主语是用户"的一句话、剔除噪音、
#: 给出归区建议），所以它只由用户在迁移报告/草稿区**显式点一次**——
#: "会花钱的默认关"这条口径在这里同样成立。
_DRAFT_SYSTEM = (
    "你在整理一份**用户档案的初稿**：下面是从旧记忆里机械搬过来的条目，"
    "语气不统一、有的主语不是用户、有的不是画像事实。\n"
    "\n"
    "请逐条判断并改写：\n"
    "1. **留下真正属于画像的**：说的是用户是谁、他长期在意什么"
    "（偏好、雷点、项目目标与约束、已定的决定与理由、产出规范）；\n"
    "2. **改写成一句话，主语是用户**、自足、不带时间戳、不写「用户说」这类外框；\n"
    "3. **不属于画像的直接丢掉**：一次性任务细节、从资料里抄来的知识、"
    "寒暄与客套、过时的进展；\n"
    "4. **绝不输出密码、令牌、密钥、证件号**这类敏感信息，哪怕草稿里有；\n"
    "5. 同一件事的几条合并成一条，最多 {limit} 条。\n"
    "\n"
    "## 输出\n"
    "每条一行，格式是「分区｜一句话」，分区只能是这四个之一："
    "身份与称呼 / 长期偏好与风格 / 进行中的项目 / 工具与环境。\n"
    "没有值得留下的就**什么也不要输出**。"
)

#: **档案块的两句边界话**（§5.1）。
#:
#: 为什么必须有：档案**每轮整份进上下文**，而模型对"一整块关于对方的话"有两种典型
#: 误读——把它当**文献依据**引用（"根据档案记载…"），或者把它当**这一轮的任务**
#: 逐条念出来。第一句挡住前者（它不是文献），第二句挡住后者（无关时不要主动提）。
#:
#: 后两句守住"过时"与"占位词"这两件实测踩过的事：
#:
#: - **冲突以对方当下为准**：档案是过去写下的记录（旧设计里这条挂在 `MEMORY.md`
#:   那句"可能已经过时"上，现在档案每轮都在场，这条比那时更需要）；
#: - **占位词不是待办**（D26，2026-09-28 走查）：那份 `PROFILE.md` 的用户资料三行
#:   写着「待确认」，于是每轮注入之后模型都当成"还没做完的事"，见面就问"怎么称呼你"
#:   ——那天 17 条新会话里 14 条出现了这种追问。
_ARCHIVE_LEAD = (
    "以下是用户档案：说的是用户是谁、他在意什么，不是文献依据；"
    "与当前问题无关时不要主动提它。\n"
    "档案是过去写下的记录，与对方此刻所说的冲突时，以他此刻说的为准。\n"
    "档案里没填的字段就当没填：不要为了填满它去追问对方，"
    "也不要因为某个字段写着「待确认」「待补」这类占位词，就每一轮都问一遍。"
)

#: 超限声明（§5.2）：**必须在提示词里说出来**。
#:
#: 静默截断是不可接受的：用户会以为助手看到了整份档案。正常路径永远碰不到它——
#: 写入侧在 4000 字就开始拒绝了，它只兜"用户在外部编辑器里把档案改超了"这一态。
_ARCHIVE_TRUNCATED = "（档案超出上限，以下为前 {count} 条；请到记忆页整理）"

#: **记忆指导**：告诉模型"这一层现在长什么样、什么时候用哪个工具"。
#:
#: 三件事必须说清（每一件都是"不说模型就会做错"的那种）：
#:
#: 1. **档案已经全量注入**了，不要再试图去"检索档案"——旧文案教的是"问偏好时先
#:    `recall`"，现在那句话会让它白花一次调用，还会把同一段内容读两遍；
#: 2. **`recall` 的池子只剩变更流**（§5.3）：它回答"这条以前是什么、什么时候改的"，
#:    而且"没搜到"只有一个含义——变更流里确实没有相关的话；
#: 3. **更正是一次调用**（`replaces`），不是"先删再记"两次；**一条只记一句**，
#:    写不下的是 `AGENTS.md` 的内容（§7.2 那条边界要说给用户听，否则他们会在
#:    档案里写小作文）。
#:
#: **只在启用时给出**（见 :meth:`MemoryService.guidance`）：关着时 `recall` 会明确
#: 报"未启用长期记忆"，还把它摆给模型看就是"每轮先查一次、再拿一句错误"
#: ——与知识库那条 ``_KB_TOOLS`` 的教训同一个形状。
_GUIDANCE = (
    "【用户档案：怎么用】\n"
    "- 上面那份**用户档案**是**全量**给你的（说的是对方是谁、他在意什么），"
    "所以不必再去检索它——直接用，与当前问题无关时不必提。\n"
    "- 要长期留下一条事实时用 `remember`；**更正**旧条目就在同一次调用里带上 "
    "`replaces`（不要先删再记）；要忘掉某条用 `forget`。\n"
    "- `recall` 查的是**变更流**：改过什么、以前是什么、什么时候改的。"
    "它**搜不到档案本身**（档案已经注入了），所以「没搜到」只有一个含义："
    "变更流里确实没有相关的话。\n"
    "- **一条只记一句话**（最多 {single} 字；全档 {total} 条 / {chars} 字）："
    "写不下的长内容属于 `AGENTS.md`，不是档案。\n"
    "- **绝不记**密码、令牌、密钥、证件号：档案每轮都进上下文。"
)

#: Agent **可以自己整份改写**的人设文件（``write_memory`` 的白名单）。
#:
#: **v0.56（档案制）起是空的**：``write_memory`` 整个退场（§7.4）——整份覆盖与本层
#: "条目级预算 + 变更流"不相容，它等于给预算与变更流开一个后门。档案的写入只有
#: `remember` / `forget` 两条路（外加界面上的行内编辑），三条路共用一个服务、一套预算。
#: 常量本身留着（而不是删掉调用点就算完），是为了让"哪些文件 Agent 能整份改写"
#: 有一个**看得出来**的答案：现在一个都没有。
WRITABLE_PERSONA_FILES: tuple[str, ...] = ()

#: **首次引导**那一段：档案还是空模板时注入，让 Agent 先去认识对方。
#:
#: 为什么需要它（照 QwenPaw 的 ``BOOTSTRAP.md`` 抄它的做法）：新装实例的档案是空的，
#: 而**没有任何机制会让它被填上**——用户不会主动去改一份档案文件（他甚至不知道有
#: 这回事），Agent 也不会问。于是启动后提示词里确实进来了几千字，全是不认识对方的
#: 样板文；用户那边的体感就是"人设没生效"。QwenPaw 的解法是首次跑一次"共同定义
#: 身份"的引导对话，我们照抄。
#:
#: **它问的正好是档案的三区**（§5.1）：怎么称呼（身份与称呼）、最近在忙什么
#: （进行中的项目）、希望怎么说话（长期偏好与风格）。三件事问完、用户答完，
#: 档案就有了第一版——而**写进去用的是 `remember`**（期二起 `write_memory` 退场，
#: 整份覆盖不再是一条路）。
#:
#: **信号是"``PROFILE.md`` 还是模板"**（见 ``profile_is_untouched``），
#: 而不是某个 BOOTSTRAP.md 文件：QwenPaw 那份文件用完要删，而我们是每轮都跑一次
#: "补缺文件"的，删掉之后会被重新铺出来——引导会无限重启。
#: 用"档案还空着"当信号则**自己就会结束**：Agent 一写进去，下一轮它就不在了。
_BOOTSTRAP = (
    "【还没认识对方：这一轮该做一次开场】\n"
    "`PROFILE.md`（用户档案）还是空的——也就是说**「对方是谁」这件事你还没写下来**。\n"
    "**这一轮就做这件事**，哪怕对方只是打了个招呼、或者只说了两个字。\n"
    "**不要用「我能做什么」开场**，也不要把你没被问到的能力列一遍"
    "（联网、笔记、工具、知识库这些）——那是自我介绍，不是认识人。\n"
    "**先看一眼已经知道的**（档案与变更流就在你的提示词里，`recall` 也可以查）："
    "已经知道的那几件**别再问一遍**；剩下的再**在你的回答里自然地问他**"
    "（不用一次问完，也别像填表）。要弄清楚的就是档案那三区的事：\n"
    "- 他怎么称呼自己，以及他希望你怎么称呼他（→「身份与称呼」）；\n"
    "- 他在做什么、关心什么（→「进行中的项目」）；\n"
    "- 他希望你怎么说话（简洁还是详细、要不要先给结论）（→「长期偏好与风格」）。\n"
    "拿到答案之后：**一条一句用 `remember` 记进档案**（`section` 给上面那个分区名；"
    "一条最多 {single} 字，**只记他自己说过的**，别写你的推断）。"
    "**`SOUL.md` 与 `AGENTS.md` 是对方自己的东西，你不要去改**——"
    "想让你的性子或规矩变，就说出来让他决定。"
    "做完**告诉对方你记下了什么**——那是他的档案，他该知道。\n"
    "只要档案还是空的，这一段每轮都会出现；写进第一条之后它自己就没了。"
)


@dataclass(frozen=True, slots=True)
class MemoryStatus:
    """记忆层的当前状态，给界面用。

    **没有任何"连通性"字段**（v0.46 起）：记忆是我们自己进程内的一路检索，
    没有第二个进程可连，也就没有"连不上"这种状态。原先那个三态 ``reachable``
    （``None`` = 没探过）是给探测端点用的，随 ReMe 一起删了。
    """

    enabled: bool
    workspace: str
    core_file_exists: bool
    file_count: int = 0
    """工作区里的记忆文件份数。"""

    last_changed_at: str = ""
    """记忆内容最后一次改动的时间（ISO，UTC）。没有索引也就没有"索引时间"，
    这里的含义就是"上次更新"。"""

    detail: str = ""


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """查证的一条命中（形状与变更流那条记录的命中一致：文本 + 出处 + 行号 + 分数）。"""

    text: str
    path: str
    start_line: int | None = None
    end_line: int | None = None
    score: float = 0.0
    coverage: float = 0.0
    source: str = "text"


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """一次隐式捕获的结果（§4.1 第②路）：命中的信号词 + 写入结果。

    ``results`` 为空表示"模型判定这轮没有值得进档案的东西"——那是**正常结果**，
    不是失败（与"模型调用失败"要分得开：后者由调用方吞掉并记日志）。
    """

    signal: str
    results: tuple[WriteResult, ...] = ()

    @property
    def receipt(self) -> str:
        """给对话过程面用的一句回执：**与显式那条路同一份文案来源**（§4.4）。"""
        return "；".join(item.receipt for item in self.results)


@dataclass(frozen=True, slots=True)
class DraftSuggestion:
    """「整理初稿」给出的一条建议（§8.3）：**只是建议，还没有写进档案**。"""

    text: str
    section: str


def _ordered_entries(
    entries: Sequence[ArchiveEntry], unknown_sections: Sequence[str]
) -> list[ArchiveEntry]:
    """条目按**注入顺序**排好：四个固定分区在前，未知分区在后（§3.1、§3.5 第 4 条）。"""
    order = [
        *af.KNOWN_SECTIONS,
        *(name for name in unknown_sections if name not in af.KNOWN_SECTIONS),
    ]
    return [entry for name in order for entry in entries if entry.section == name]


def _body_of(entries: Sequence[ArchiveEntry], unknown_sections: Sequence[str]) -> str:
    """若干条目 → 注入用的正文（**不带 frontmatter**、不带 H1）。

    ``frontmatter`` 只给界面与备份看（§3.5 第 1 条），不进注入块；H1「# 用户档案」
    是文件自己的标题，模型看的是"分区标题 + 条目行"这一层。
    """
    rendered = af.render_archive(
        af.Archive(entries=tuple(entries), unknown_sections=tuple(unknown_sections))
    )
    return af.parse_frontmatter(rendered)[1].strip()


def _fitted_body(
    entries: Sequence[ArchiveEntry],
    unknown_sections: Sequence[str],
    *,
    limit: int = INJECTION_LIMIT_CHARS,
) -> tuple[str, int]:
    """装到**装不下为止**，返回 ``(正文, 装进的条数)``（§5.2）。

    逐条加、每次重算一遍正文：条数最多几十条，而"哪几条装得下"这件事必须与
    真正的渲染**同一口径**去数——估算字数（比如按行累加）迟早与渲染漂开，
    漂开的表现是"提示词里说前 N 条，实际给了前 N-3 条"。
    """
    ordered = _ordered_entries(entries, unknown_sections)
    body = ""
    kept = 0
    for index in range(1, len(ordered) + 1):
        candidate = _body_of(ordered[:index], unknown_sections)
        if len(candidate) > limit:
            break
        body, kept = candidate, index
    return body, kept


def _capture_prompt(query: str) -> list[ChatMessage]:
    """隐式捕获那一次判定的两条消息。

    **只给用户这一轮说的话**，不给助手的回答：判据里有一条"**是用户说的**吗"，
    把助手的回答摆进来只会给模型多一份它不该依据的材料（它会从助手的话里
    推断出"用户大概喜欢…"，而那正是要否决的）。
    """
    return [
        ChatMessage(role="system", content=_CAPTURE_SYSTEM),
        ChatMessage(role="user", content=f"用户这一轮说的是：\n\n{query.strip()}"),
    ]


def _draft_prompt(draft: str, limit: int) -> list[ChatMessage]:
    """「整理初稿」那一次改写的两条消息。"""
    return [
        ChatMessage(role="system", content=_DRAFT_SYSTEM.format(limit=limit)),
        ChatMessage(
            role="user",
            content=f"## 迁移草稿里的旧条目\n\n{draft}\n\n请按系统提示词里那个格式输出。",
        ),
    ]


def _split_label(line: str) -> tuple[str, str]:
    """``分区｜一句话`` → ``(分区, 一句话)``；不是这个形状就是 ``("", 整行)``。

    两条分寸：

    - ``｜`` / ``|`` 是**我们要求模型用的分隔符**，所以哪怕分区名写错（模型偶尔写成
      「长期偏好」而不是「长期偏好与风格」），也认它是标签并把正文取回来——
      分区名不该让一条真事实落不了盘，归区有机械词表兜底；
    - ``：`` 只在**逐字命中四个固定分区之一**时才切：模型会把一句话写成
      ``2026 计划：先做 A`` 这种形状，按冒号无脑切会把正文切坏。
    """
    for sep in ("｜", "|"):
        head, found, tail = line.partition(sep)
        if found and tail.strip() and 0 < len(head.strip()) <= 12:
            name = head.strip()
            return (name if name in af.KNOWN_SECTIONS else ""), tail.strip()
    head, found, tail = line.partition("：")
    if found and head.strip() in af.KNOWN_SECTIONS:
        return head.strip(), tail.strip()
    return "", line


def _has_bar_label(line: str) -> bool:
    """这一行带没带 ``标签｜正文`` 那个分隔符（分区名认不出来也算带）。"""
    for sep in ("｜", "|"):
        head, found, tail = line.partition(sep)
        if found and tail.strip() and 0 < len(head.strip()) <= 12:
            return True
    return False


def _parse_lines(raw: str, *, limit: int) -> list[tuple[str, str]]:
    """把模型输出解析成 ``[(分区, 一句话)]``（最多 ``limit`` 条）。

    **宽容读取、严格丢弃**：模型爱加项目符号、爱把输出包在围栏里、分区名也可能写成
    「长期偏好」这种近似说法（那就按内容机械归区，见 ``classify_text``）。
    但**散文一律丢掉**——它只认三种形状：带项目符号的行、``标签｜正文``、
    以及分区名逐字对得上的行。理由：写进档案的东西每轮都在付上下文成本，
    宁少一条也不错一条；而模型回一句「这一轮没有值得记的」正是要表达这个意思。
    """
    out: list[tuple[str, str]] = []
    for raw_line in (raw or "").splitlines():
        if _FENCE.match(raw_line):
            continue
        line = raw_line.strip()
        bullet = bool(_ENTRY_LINE.match(line))
        if bullet:
            line = _ENTRY_LINE.sub("", line).strip()
        section, text = _split_label(line)
        if not (bullet or section or _has_bar_label(line)):
            continue
        text = " ".join(text.split()).strip()
        if not text or len(text) > MAX_ENTRY_CHARS:
            continue
        out.append((section, text))
        if len(out) >= limit:
            break
    return out


class MemoryService:
    """记忆的实现。**内容全在本地**：读写的都是 ``data/memory/`` 下的 Markdown。

    只有隐式捕获这一个动作会出网，而且**默认关**（``memory.capture``）：
    开着时它在信号出现的那一轮问一次对话模型（那不是"记忆服务"，而是我们自己的
    模型通道，模型没配好时它明确报错，见 ``_ask_model``）。于是默认配置下一次调用
    都不发生。
    """

    def __init__(
        self,
        runtime: RuntimeConfigService,
        data_dir: Path,
        *,
        ask: Callable[[list[ChatMessage]], str] | None = None,
    ) -> None:
        self._runtime = runtime
        self._data_dir = data_dir
        #: 问模型的能力（可选）：不给就用运行期配置里绑定的对话模型（见 ``_ask_model``）。
        #: 测试注入它来跑隐式捕获与整理初稿，不必真连一个模型。
        self._ask = ask

    # ------------------------------------------------------------------ 配置

    @property
    def enabled(self) -> bool:
        return self._runtime.get_bool("memory.enabled")

    @property
    def capture_enabled(self) -> bool:
        """隐式捕获的总开关（``memory.capture``，**默认 false**，§4.1 第②路）。

        **不看 ``memory.enabled``**：与 `remember` 同一条纪律——档案的写入不看那道闸，
        那道闸管的是"它进不进这一轮的上下文、`recall` 能不能用"。两个开关各管一件事，
        合成一个判据的后果是"关掉注入之后，用户显式打开的自动记也不明不白地停了"。
        """
        return self._runtime.get_bool("memory.capture")

    @property
    def capture_model(self) -> str:
        """判定用哪个模型（``memory.capture_model``）；**空 = 用运行中的对话模型**。

        默认留空是刻意的（§4.2）：不引入新的必填配置，也不引入"没配记忆模型 →
        记忆功能不可用"这种半死状态。允许绑一个更便宜的模型来做判定。
        """
        return (self._runtime.get("memory.capture_model") or "").strip()

    def workspace_for(self, user_id: str | None = None) -> Path:
        """某个账号的记忆工作区（**agent 按账号隔离的落点**，v0.15）。

        目录形状（与设计文档 §3.1 的"四条轴"对应）：

        - 账号 ``u1`` → ``data/memory/u1/``
        - 共享桶（``None``：管理员控制台与 API Key 通道）→ ``data/memory/``

        **共享桶刻意就是老路径本身**，不另开 ``_shared`` 子目录：单用户部署
        （绝大多数）升级前后路径一字不变，已有的 ``MEMORY.md`` / ``daily`` / ``digest``
        原地继续用——升级不该让一个人的记忆"消失"。

        为什么"一个账号一个目录"就是"一个账号一个 agent"：这个目录里放着它的
        ``SOUL.md``（人格）与 ``PROFILE.md``（档案），
        而这两份东西每轮都进 system prompt。**分开它们，Agent 才真的是"我的"**。

        **native 之后这条隔离是精确的**：召回只扫"这个账号自己的目录"，
        不存在"一个记忆服务只能盯一份工作区"那种进程级限制
        （原先 ``memory.service_scope`` 就是为了如实说明那件事，随 ReMe 一起删了）。
        """
        raw = (self._runtime.get("memory.workspace") or "memory").strip()
        base = self._data_dir / raw
        return base / user_id if user_id else base

    @property
    def workspace(self) -> Path:
        """共享桶的工作区（兼容旧调用：``workspace_for(None)``）。"""
        return self.workspace_for(None)

    def core_file_for(self, user_id: str | None = None) -> Path:
        return self.workspace_for(user_id) / CORE_MEMORY_FILE

    @property
    def core_file(self) -> Path:
        """共享桶的 ``MEMORY.md``（``core_file_for(None)`` 的兼容写法）。"""
        return self.core_file_for(None)

    def _require_enabled(self) -> None:
        """**注入与 recall** 的那道闸（§7.3）。

        报错文案里**不提任何服务**（v0.46）：本地实现没有"要去把某个进程拉起来"
        这回事，能做的动作只有一件——去设置里打开它。

        **档案的读写不看这道闸**：关着时它照样可以被 `remember`/`forget` 改、
        也可以在记忆页上编辑（"关了也能改自己的东西"这条纪律保留）。这道闸管的
        只是另一半——它进不进这一轮的上下文、`recall` 能不能用。
        """
        if not self.enabled:
            raise InvalidRequestError(
                "未启用长期记忆。请在「设置 → 长期记忆」里打开（在记忆页可以直接打开）"
            )

    # ------------------------------------------------------------------ 档案

    def archive(self, user_id: str | None = None) -> ArchiveService:
        """这个账号的档案服务（``data/memory/<账号>/PROFILE.md`` + ``changes.md``）。

        **每次现建**：它自己不缓存任何东西（读文件、算预算都是当场的），
        于是"改完下一轮生效"是构造出来的性质，不需要失效通知。
        """
        return ArchiveService(self.workspace_for(user_id))

    def archive_entries(self, user_id: str | None = None) -> tuple[ArchiveEntry, ...]:
        """档案当前的全部条目（按文件里的顺序）。"""
        return self.archive(user_id).read().entries

    def archive_budget(self, user_id: str | None = None):  # type: ignore[no-untyped-def]
        """档案的预算读数（条数/字数/每区，界面与迁移报告共用）。"""
        return self.archive(user_id).budget()

    def archive_text(self, user_id: str | None = None) -> str:
        """档案正文的**原样**（含 frontmatter）——`read_memory` 用它自查写了什么。"""
        return af.read_text(self.workspace_for(user_id) / ARCHIVE_FILE).strip()

    def archive_block(self, user_id: str | None = None) -> str:
        """要注入 system prompt 的**档案块**（§5.1–5.2）；关着或空档案时是空串。

        五条口径都在这一个函数里：

        1. **每轮现读现拼**（无缓存、无索引）：改完下一轮就生效，先前的注入不留影子；
        2. **不挑选、不摘要、不排序**：四区顺序照文件（用户看到的顺序 = 模型看到的顺序）。
           挑选会引入一个"这轮该看哪几条"的判定——那既费模型，又让用户无法预测
           助手到底知道什么；
        3. **原文照进**（分区标题 + 条目行），前面加一句边界说明与那句占位词规矩
           （见 :data:`_ARCHIVE_LEAD`）：少了它，模型会把档案当文献引用或当任务逐条念；
        4. **硬顶 6000 字**（:data:`~app.services.archive.INJECTION_LIMIT_CHARS`）：
           超限**在提示词里说出来**并只给能装下的前 N 条——静默截断会让用户以为
           助手看到了整份档案。正常路径永远碰不到它（写入侧 4000 字就开始拒绝）；
        5. **不看人设那份配置**：``memory.persona_files`` 只管 SOUL/AGENTS 的取舍，
           档案的开关是 ``memory.enabled``（§7.2：把它挂上人设清单，用户从那一行里
           删掉一个名字，档案就静默停止注入了）。
        """
        if not self.enabled:
            return ""
        archive = self.archive(user_id).read()
        if not archive.entries:
            return ""
        body, kept = _fitted_body(archive.entries, archive.unknown_sections)
        if kept < len(archive.entries):
            notice = _ARCHIVE_TRUNCATED.format(count=kept)
            return f"{_ARCHIVE_LEAD}\n{notice}\n\n{body}"
        return f"{_ARCHIVE_LEAD}\n\n{body}"

    def remember(
        self,
        content: str,
        *,
        section: str = "",
        replaces: str | None = None,
        user_id: str | None = None,
    ) -> WriteResult:
        """新增或顶替一条（**显式那条路，零额外模型调用**，§4.1）。

        - ``section`` 留空或不认识时**按内容机械归区**（词表与迁移共用一处，
          见 ``archive_migration.classify_text``）：分区名不该成为一次写入失败的原因；
        - ``replaces`` 是"更正一次完成"的入口（§4.3 第 1 条：模型的显式判定优先）：
          它指哪条就顶替哪条，找不到时退回机械判据——指错了不该让这一轮直接失败；
        - **不看 ``memory.enabled``**：档案关着时照样可写（写进去下一轮就注入），
          这道闸只挡住"注入与 recall"（见 :meth:`_require_enabled`）；
        - 返回 :class:`~app.services.archive.WriteResult`：``action`` 四种
          （added/replaced/existing/rejected）与 ``receipt`` 就是 §4.4 那份文案，
          **模型从工具听到的与人在界面上看到的是同一句**。
        """
        text = " ".join((content or "").split()).strip()
        if not text:
            raise InvalidRequestError("缺少参数：content")
        target = (section or "").strip()
        if target not in af.KNOWN_SECTIONS:
            target = classify_text(text, default=SECTION_PREFERENCES)
        return self.archive(user_id).add(text, target, replaces=replaces)

    def forget(self, topic: str, *, user_id: str | None = None) -> WriteResult:
        """忘掉一条：删除 + 变更流留痕 + 可还原（§9.2 第 6 条）。同样不看开关。"""
        return self.archive(user_id).forget(topic)

    def restore(self, old_text: str, *, user_id: str | None = None) -> WriteResult:
        """把一条旧值写回档案（界面的「还原」用它，§6.2）。"""
        return self.archive(user_id).restore(old_text)

    def rename_group(
        self,
        section: str,
        old: str,
        new: str,
        *,
        user_id: str | None = None,
    ) -> WriteResult:
        """项目段的组改名（§3.1 第 2 条）。同样不看开关。"""
        return self.archive(user_id).rename_group(section, old, new)

    # ------------------------------------------------------------ 隐式捕获（§4.1 ②）

    def capture_implicit(
        self, message: str, *, user_id: str | None = None
    ) -> CaptureOutcome | None:
        """**隐式**那条路：用户消息命中信号词时，跑一次判定，写进档案。

        三道闸，**先便宜的先过**（与旧实现同一条取舍）：

        1. ``memory.capture``（默认关）——关着时这个方法**一次模型都不调**，
           连信号词都不看；
        2. **机械前置筛**（:func:`matched_signal`）：没命中信号词就返回 ``None``，
           这一轮零成本；
        3. 都没有才问模型一次，用的是 :attr:`capture_model`（空 = 运行中的对话模型）。

        判据与安全由两处一起保证：**提示词**里是 §4.2 的画像双问与三条否决，
        **落盘**那一侧是 :meth:`ArchiveService.add` 的预算、机械顶替与敏感信息否决——
        模型不听话时（比如把密码写进来）最后的闸门仍然拦得住。

        返回值两种"空"要分清：``None`` = 这条路没跑（关着 / 没信号）；
        ``CaptureOutcome(results=())`` = 跑了，但判定"这轮没有值得写的"。

        **抛出的异常由调用方处置**：判定调用失败不能影响一轮已经成功的问答，
        但也不该被静默吞掉——调用方记日志（见 ``api/v1/chat._implicit_capture_step``）。
        """
        if not self.capture_enabled:
            return None
        query = " ".join((message or "").split()).strip()
        signal = matched_signal(query)
        if not signal:
            return None
        raw = self._ask_model(_capture_prompt(query), model_pk=self.capture_model)
        items = _parse_lines(raw, limit=MAX_CAPTURED_ITEMS)
        if not items:
            return CaptureOutcome(signal=signal)
        archive = self.archive(user_id)
        results: list[WriteResult] = []
        for section, entry in items:
            target = section if section in af.KNOWN_SECTIONS else classify_text(
                entry, default=SECTION_PREFERENCES
            )
            results.append(archive.add(entry, target, source=SOURCE_IMPLICIT))
        return CaptureOutcome(signal=signal, results=tuple(results))

    def _ask_model(self, messages: list[ChatMessage], *, model_pk: str = "") -> str:
        """问一次模型。

        默认用的是运行期配置里**绑定给「对话生成」的那个模型**（与对话同一条模型通道），
        而不是另配一套：记忆是对话的副产品，没有理由让它用一个必须存在的第二模型。
        ``model_pk`` 非空时（``memory.capture_model``）用那个指定的注册模型——
        允许绑一个更便宜的模型来做判定（§4.2）。没绑定时报可读错误。

        **关掉思考**（``enable_thinking=False``）：``llm.py`` 的模块注释里就写着这条
        ——抽取类任务要显式关。实测（真模型 + 真数据，2026-09-27）开着思考时模型的
        **推理过程会混进正文**：那次的整理输出里中英文夹着"等等，第二个条目没有内容，
        不应该输出…Let me reconsider and produce clean output"，于是解析器只认得出半条。
        记忆判定是"从一段话里挑出事实"，不是推理题，那些 token 只会添乱。
        """
        if self._ask is not None:
            return self._ask(messages)
        config = self._runtime.llm_for(model_pk or None)
        if not config.is_configured:
            raise InvalidRequestError(
                "没有可用的对话模型，记忆判定需要一个能用的模型（设置 → 模型）"
            )
        return OpenAICompatChat(replace(config, enable_thinking=False)).complete(messages)

    # ------------------------------------------------------------------ 迁移

    def migration_available(self, user_id: str | None = None) -> bool:
        """还有没有可折叠的旧数据（§8 的界面入口显隐判据）。

        与 :func:`run_migration` 的跳过判据互为反面：源文件指纹与水位一致、且档案
        已经存在时**没有可迁的**（入口不出现）；否则入口出现。旧文件不会被迁移删掉，
        所以判据必须落在"指纹变没变"上，不能落在"旧文件还在不在"上。
        """
        workspace = self.workspace_for(user_id)
        current = archive_migration.source_fingerprints(workspace)
        if not current:
            return False
        stored = archive_migration.read_watermark(workspace).get("sources")
        archive_exists = (workspace / ARCHIVE_FILE).exists()
        return not (isinstance(stored, dict) and stored == current and archive_exists)

    def migrate(self, user_id: str | None = None) -> MigrationReport:
        """跑一遍机械折叠迁移（**零模型调用**，§8.3）。"""
        return archive_migration.run_migration(self.workspace_for(user_id))

    def draft_entries(self, user_id: str | None = None) -> int:
        """``import-draft.md`` 里还有几条旧条目没进档案（§8.2 的界面提示）。"""
        path = self.workspace_for(user_id) / af.IMPORT_DRAFT_FILENAME
        return len(af.bullet_lines(af.read_text(path)))

    def organize_draft(self, user_id: str | None = None) -> list[DraftSuggestion]:
        """跑一次模型，把 ``import-draft.md`` 里的旧条目改写成画像条目（§8.3）。

        **只返回建议，一个字都不写**：结果先给用户看（界面上是预览），他确认之后
        才逐条走 `remember` 那条正规的路——同一套预算、同一套顶替判据、
        同一份回执文案。这样这一次模型调用**不可能**绕过 §3.3–§3.4 的任何一条。

        失败与"没整理出东西"都**如实抛错**（调用方转成可读的错误回执）：
        静默返回空列表会让用户以为"点了没反应"，而这一次是花过钱的。
        """
        path = self.workspace_for(user_id) / af.IMPORT_DRAFT_FILENAME
        items = [text for _heading, text in af.bullet_lines(af.read_text(path))]
        if not items:
            raise InvalidRequestError("草稿里没有可整理的条目")
        sent = items[:MAX_DRAFT_ITEMS]
        draft = "\n".join(f"- {item}" for item in sent)
        raw = self._ask_model(
            _draft_prompt(draft, MAX_DRAFT_ITEMS), model_pk=self.capture_model
        )
        parsed = _parse_draft(raw)
        if not parsed:
            raise InvalidRequestError(
                "模型这一轮没有给出可用的条目（草稿没有改动，可以再点一次）"
            )
        return [
            DraftSuggestion(
                text=entry,
                section=section
                if section in af.KNOWN_SECTIONS
                else classify_text(entry, default=SECTION_PREFERENCES),
            )
            for section, entry in parsed
        ]

    # ------------------------------------------------------------------ 读取

    def soul_text(self, user_id: str | None = None) -> str:
        """``SOUL.md`` 的正文（人格，一句话说就是"你是谁"）。

        与档案性质不同：那个记"对方是谁、他在意什么"，这个定"我是谁、我怎么做"。
        两条约定照抄 QwenPaw：**由 Agent 自己进化**，以及**改动要告知用户**
        （"这是你的灵魂，他们该知道"）——后一条是产品约定，写在设计文档里。
        """
        try:
            return (self.workspace_for(user_id) / SOUL_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def seed_persona(self, user_id: str | None = None) -> list[str]:
        """把缺的人设文件补上模板，返回**这次新建了哪几个**。

        为什么要落成文件而不是只存在代码里：这几份东西的价值恰恰在于**用户能改**——
        人格、对方是谁、这类活怎么干，都是他比我清楚的事。文件是唯一一种
        "他能看见、能编辑、还能用 git 管版本"的形态（QwenPaw 也是这么做的）。

        **只补缺的，绝不覆盖已存在的**：那可能已经是用户写了几天的东西。
        唯一的例外是"还是我们当初写的那份、一个字没动过"的旧模板，
        见 :meth:`_upgrade_untouched_template`。

        **v0.56 起只三份**（``SOUL.md`` / ``PROFILE.md`` / ``AGENTS.md``）：
        ``MEMORY.md`` 退场（§7.2），它不再被播种、也不再被注入或写入——
        新部署的「记忆」页上看到的将是那份档案，而不是一份"已知事实"的旧文件。
        """
        created: list[str] = []
        for name, template in (
            (SOUL_FILE, _SOUL_TEMPLATE),
            (PROFILE_FILE, _PROFILE_TEMPLATE),
            (AGENTS_FILE, _AGENTS_TEMPLATE),
        ):
            path = self.workspace_for(user_id) / name
            if path.exists():
                self._upgrade_untouched_template(path, name, template)
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # 按字节写：`write_text` 在 Windows 上会把换行改成 CRLF
                # （与 memory_files 同一处踩过的坑）
                path.write_bytes(template.encode("utf-8"))
            except OSError:
                logger.warning("人设文件写不出来：%s", path, exc_info=True)
                continue
            created.append(name)
        return created

    def _upgrade_untouched_template(self, path: Path, name: str, template: str) -> bool:
        """把**从没被改过的旧模板**换成新模板；返回是否换了。

        判据是**逐字节相同**（见 ``profile_is_untouched`` 的说明）：只要用户动过一个
        字符，这份文件就是他的，我们一个字都不改。这一条是"升级不该改用户的东西"
        的落点，所以宁可漏判（继续用旧模板）也不要误判。
        """
        known = {template, *_LEGACY_TEMPLATES.get(name, ())}
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            return False
        if current not in known or current == template:
            return False
        try:
            path.write_bytes(template.encode("utf-8"))
        except OSError:
            logger.warning("人设模板升级失败：%s", path, exc_info=True)
            return False
        logger.info("人设模板已升级（这份文件一直是初始模板，没有人改过）：%s", path.name)
        return True

    def persona_order(self) -> tuple[str, ...]:
        """这一轮**注入哪几份人设文件、按什么顺序**（``memory.persona_files``）。

        照 QwenPaw 的 ``system_prompt_files``：那几份文件每轮整份进 system prompt，
        所以"哪几份、什么顺序"是用户的设定。四条口径：

        - 取值是**逗号分隔的文件名**，按写的顺序注入；
        - **只认那几份核心文件**（``memory_files.CORE_FILES``）：别的一律丢掉并记日志
          ——让任意路径进 system prompt 等于绕过"哪些是设定"这条分界
          （想让别的内容每轮都在，写进 ``AGENTS.md`` 就是那条正当的路）；
        - **只认人设那两份**（``SOUL.md`` / ``AGENTS.md``）：``PROFILE.md`` 是档案、
          走独立的贡献者与独立的开关（§7.2），``MEMORY.md`` 已退场（不再注入）。
          用户清单里写了这两个名字时照样丢掉并记一条日志——**静默忽略**会让
          那一行看起来生效了；
        - **空值/全不认识 → 回到默认顺序**（不写就是默认，不是"一份都不注入"）。
          这与"未配置时取 ``DEFAULTS``"那条口径一致，也让测试替身的未配置状态
          （假 runtime 取不到值）落到同一个结果上。
        """
        raw = (self._runtime.get("memory.persona_files") or "").strip()
        if not raw:
            return tuple(name for name, _label in PERSONA_FILES)
        injectable = {name for name, _label in PERSONA_FILES}
        wanted: list[str] = []
        for item in raw.replace("，", ",").split(","):
            name = item.strip()
            if not name:
                continue
            if name not in memory_files.CORE_FILES:
                logger.warning("人设文件清单里有不认识的文件名，已忽略：%s", name)
                continue
            if name not in injectable:
                logger.warning(
                    "人设文件清单里只有 %s 是可注入的人设（%s 不是：档案走 memory.enabled，"
                    "MEMORY.md 已退场），已忽略：%s",
                    "/".join(sorted(injectable)),
                    name,
                    name,
                )
                continue
            if name not in wanted:
                wanted.append(name)
        return tuple(wanted) or tuple(name for name, _label in PERSONA_FILES)

    def persona_texts(self, user_id: str | None = None) -> list[tuple[str, str]]:
        """``[(文件名, 正文)]``，按 :meth:`persona_order` 的顺序，空的跳过。

        **不在这里拼字符串**：拼装交给 `services/prompt.py` 的贡献者——
        那里才知道"这一轮是工具循环还是检索链路""要不要带摘要"。档案也不在这里：
        它是**另一个贡献者**（见 :meth:`archive_block`）。
        """
        found: list[tuple[str, str]] = []
        for name in self.persona_order():
            try:
                text = (self.workspace_for(user_id) / name).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                found.append((name, text))
        return found

    def guidance(self) -> str:
        """要注入 system prompt 的「用户档案：怎么用」那一段；**未启用时是空串**。

        空串而不是"关着时也说明一下"：关着时 ``recall`` 会明确报错，把"什么时候
        该去查"讲给模型听，只会换来每轮一次无效调用加一句错误——与知识库
        那一侧的教训同源（见 ``agent_tools._KB_TOOLS``：给了又拒，白花两个来回）。

        字数从 ``archive`` 那几个常量取：文案里写死的数字迟早与预算漂开，
        而"写不进去"时用户看到的正是这句话。
        """
        if not self.enabled:
            return ""
        return _GUIDANCE.format(
            single=SINGLE_ENTRY_CHARS, total=TOTAL_ENTRIES, chars=TOTAL_CHARS
        )

    def profile_is_untouched(self, user_id: str | None = None) -> bool:
        """档案是不是**一个字都没动过**（还是我们当初写下去的骨架）。

        判据是**逐字节相同**，与 ``_upgrade_untouched_template`` 同一套口径：
        差一个字节就不算"没填过"。它是"要不要做首次引导"的信号，所以宁可漏判
        （用户已经动过一点就不再引导）也不要误判——对着一个写了半天档案的人
        问"你是谁"是很糟的体验。

        旧版模板也认（v0.20 的空骨架、v0.21–v0.55 的散文体；那些实例还没被
        ``seed_persona`` 升级过），否则它们的引导会一直不出现。
        """
        known = {_PROFILE_TEMPLATE, *_LEGACY_TEMPLATES.get(PROFILE_FILE, ())}
        try:
            text = (self.workspace_for(user_id) / ARCHIVE_FILE).read_text(encoding="utf-8")
        except OSError:
            return False
        return text in known

    def bootstrap_block(self, user_id: str | None = None) -> str:
        """首次引导那一段；档案已经被写过时是空串。

        **不看 ``memory.enabled``**：档案的编辑本来就不受那道闸管
        （见模块头与 ``remember`` 的说明）——"你还不认识对方"与"档案进不进
        这一轮的上下文"是两回事，把它挂在开关上会让关着记忆的实例永远不做引导。
        """
        return _BOOTSTRAP.format(single=SINGLE_ENTRY_CHARS) if self.profile_is_untouched(
            user_id
        ) else ""

    def status(self, user_id: str | None = None) -> MemoryStatus:
        """当前状态：**数一遍工作区，不连任何东西**。

        为什么连计数都在这里算（而不是让界面 filter）：这些数字的含义都压在
        "什么算一个记忆文件"这些判断上，而它们只在这一层知道。界面只该显示数字。

        成本：一次目录遍历 + 读那几个文件。这是"个人长期记忆"的量级
        （几十份文件、几百 KB），所以不另做缓存——不缓存就没有"缓存过期"
        这个新问题。
        """
        space = self.workspace_for(user_id)
        stats = memory_files.stats(space)
        return MemoryStatus(
            enabled=self.enabled,
            workspace=str(space),
            core_file_exists=self.core_file_for(user_id).exists(),
            file_count=stats.file_count,
            last_changed_at=stats.last_changed_at,
            detail=""
            if self.enabled
            else "未启用：用户档案不进提示词，recall 也停着（档案本身照旧可以编辑）",
        )

    # ------------------------------------------------------------------ 召回

    def recall(
        self, query: str, *, limit: int | None = None, user_id: str | None = None
    ) -> list[MemoryHit]:
        """在**变更流**里查证（§5.3）：这条以前是什么、什么时候改的。

        三条口径：

        - **档案不进召回池**：它每轮已经全量注入，再召回一次就是把同一段内容
          进两次上下文；
        - **排序是纯字面的**（:func:`app.services.archive.search_changes`）：
          归一化子串命中数 + 二元组覆盖率，不依赖分词、不建索引、不走向量；
        - **"没搜到"只有一个含义**：变更流里确实没有相关的话。
        """
        self._require_enabled()
        text = query.strip()
        if not text:
            raise InvalidRequestError("缺少参数：query")
        count = max(1, min(int(limit or DEFAULT_RECALL), MAX_RECALL))
        return [
            MemoryHit(
                text=hit.text,
                path=hit.path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                score=hit.score,
                coverage=hit.coverage,
                source=hit.source,
            )
            for hit in self.archive(user_id).changes_hits(text, limit=count)
        ]

    # ------------------------------------------------------------------ 文件
    #
    # 只读：浏览走**本地目录**，这几个方法**不要求 ``memory.enabled``**——
    # 记忆关着的时候，用户依然该能打开自己的记忆文件看看写了什么。
    # 写那一侧（PUT/DELETE /memory/files/{path}）已经随档案制退场（§6.3）：
    # 留在那里就是一个绕过预算与变更流的后门。

    def files(self, user_id: str | None = None) -> list[MemoryFile]:
        """列出这个账号工作区里的 Markdown 文件（分类、摘要、大小、时间）。"""
        return memory_files.scan(self.workspace_for(user_id))

    @property
    def scan_limit(self) -> int:
        """一次最多列多少个文件。界面要拿它判断"列表是不是被截断了"——
        截断了却不说，用户会以为"我的文件丢了"。"""
        return memory_files.MAX_LISTED_FILES

    def describe(self, path: str, user_id: str | None = None) -> MemoryFile:
        """单个文件的元信息（不含正文）。见 ``memory_files.describe``。"""
        return memory_files.describe(self.workspace_for(user_id), path)

    def file_text(self, path: str, user_id: str | None = None) -> MemoryFileDetail:
        """读一个文件的原文（含 frontmatter）——档案卡右下角的「原文」与迁移草稿用它。"""
        return memory_files.read_file(self.workspace_for(user_id), path)


def _parse_draft(raw: str) -> list[tuple[str, str]]:
    """把「整理初稿」的输出解析成 ``[(分区, 一句话)]``。

    与隐式捕获同一套宽容读取，但**上限宽松得多**（:data:`MAX_DRAFT_ITEMS`）：
    这一次的目标是把几十条草稿整理成一份可用的初稿，不再受"一轮最多两条"的约束
    ——那个上限是给隐式捕获的"宁可漏不可滥"定的。
    """
    return _parse_lines(raw, limit=MAX_DRAFT_ITEMS)
