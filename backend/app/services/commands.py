"""斜杠命令：内置表 + ``commands/*.md`` 自定义命令（P1-2，开发计划 §12.225）。

**照抄的是三家各自最容易漏掉的那一半**（调研报告《Agent-与对话架构对标调研 v0.1》
§2.7「agent 命令」，ZCode 那一半是文档级证据、DSH 与 QwenPaw 那一半是源码级）：

===================  ============================================  ===========================
抄谁                  它的原话                                     抄到我们这里的落点
===================  ============================================  ===========================
QwenPaw              「三类命令进 LLM **之前**短路」                 ``parse`` 在协议层被调用，
                                                                  短路类命令不建工具循环
DSH                  「``/`` 行**永不静默降级**为普通 prompt」     认不出的 ``/`` 也当命令答一句，
                     +「命令执行写 session log 但**不进模型       并且不落 user/assistant 消息
                     历史**」
ZCode                内置表 ``{name, summary, usage, details[]}``   ``BUILTIN_COMMANDS``
ZCode                自定义命令 = 一个 md 文件，**文件名即命令名**， ``_load`` / ``COMMAND_NAME_RE``
                     扁平 frontmatter，``$ARGUMENTS`` / ``$1..$N``，
                     「有参数但正文无占位符时自动追加 User arguments:」
===================  ============================================  ===========================

**优先级：内置 > 用户自定义 > 仓库自带，重名 first match wins**（ZCode 的
「用户级 > 工作区级 > 插件级」那一条按我们的三层改写）。被遮蔽的那条**仍然列出来**、
带 ``shadowed_by``——与插件列表同一套做法，静默藏掉会让人以为文件没生效。

**命令分两类**，这个区分是这一模块对协议层最重要的一句话：

- **短路类**（``short_circuit=True``）：``/help`` ``/new`` ``/stop`` ``/mode``
  ``/model`` ``/compact`` ``/rewind`` ``/context`` ``/status`` ``/skills``。它们
  **不进模型**、**不产生 assistant 消息**——由协议层直接执行并把结果回给界面
  （QwenPaw 那套优先级 0/10/20/30 要的就是这个）。
- **改写类**（``short_circuit=False``）：``/skill``、``/plan <描述>``、全部自定义
  md 命令与**全部技能命令**。它们把渲染后的正文**当作这一轮的提示**（ZCode：
  ``/skill`` 会重写下一条 prompt），所以还是要过一次模型、还是会留下回答；
  进历史的也只是渲染后的正文。``/plan`` 是**看有没有参数的两面派**（不带描述时
  就是 ``/mode plan``），真正决定走哪条路的见 ``CommandDef.short_circuit`` 那一段说明。

**参数语义五条**（前两条照 ZCode，后三条照 Claude 的
``.claude/commands`` 那套 —— 见调研报告《对话命令-调研 v0.1》§3 第 5、6 条）：

1. ``$ARGUMENTS``（全部参数原样）；
2. ``$1``…``$N``（按空白切分后的第 N 个，**1 起**，ZCode 的老口径）；
3. ``$0``（**第一个**）与 ``$ARGUMENTS[N]``（**0 起**，Claude 的写法）——
   与第 2 条并存，不是替代：把 ``$1`` 改成"第二个"会让所有老命令当场坏掉；
4. ``$name``：frontmatter 的 ``arguments:`` 声明了名字，正文里按**位置**映射
   （``arguments: [topic, tone]`` → ``$topic`` 是第一个、``$tone`` 是第二个）；
5. **正文里一个占位符都没命中、但用户给了参数**时自动把参数追加到末尾——
   那是最常见的"命令写了但好像没收到"的情形，照抄 ZCode 的兜底
   （ZCode 是 ``User arguments:``，这里用中文是因为这一句是给**模型**读的，
   而它接下来要用中文作答）。

**命名空间**：``commands/git/commit.md`` → ``/git:commit``（Claude 与 ZCode 都这么分，
分隔符就是 ZCode 那条正则里已经允许的 ``:``）。目录名一样参与"``.``/``_`` 开头跳过"。

**技能也是命令**（Claude 的"命令＝技能" + QwenPaw 的"技能目录名即命令"）：
``data/skills`` 与仓库 ``skills/`` 里的每个技能都注册一条 ``/<技能名> [任务]``，
**排在最后**——与内置/自定义重名时让位（first match wins），被顶掉的那条
带着 ``shadowed_by`` 留在列表里。执行时**不走这里**：它们是改写类，正文由
``ChatService.skill_prompt`` 注入（另写一份技能注入实现就会出现两种正文口径）。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.services.memory_files import parse_frontmatter
from app.services.modes import MODE_DEFS, MODES
from app.services.session_events import KIND_TURN_START

__all__ = [
    "ARGUMENT_NAME_RE",
    "BUILTIN_COMMANDS",
    "COMMANDS_DIR",
    "COMMAND_FILE_SUFFIX",
    "COMMAND_MAX_DEPTH",
    "COMMAND_NAME_RE",
    "MODE_SOURCE_COMMAND",
    "MODE_SOURCE_SETTINGS",
    "MODE_VALUES",
    "NAME_COMPACT",
    "NAME_CONTEXT",
    "NAME_HELP",
    "NAME_MODE",
    "NAME_MODEL",
    "NAME_NEW",
    "NAME_PLAN",
    "NAME_REWIND",
    "NAME_SKILL",
    "NAME_SKILLS",
    "NAME_STATUS",
    "NAME_STOP",
    "PLACEHOLDER_RE",
    "SKILL_SUMMARY_CHARS",
    "USER_ARGUMENTS_LABEL",
    "CommandDef",
    "CommandService",
    "ModeWatch",
    "ParsedCommand",
    "TurnControl",
    "builtin_names",
    "is_builtin",
    "modes_text",
    "parse",
    "render",
    "split_args",
]

logger = logging.getLogger(__name__)

#: 用户级自定义命令目录（``<data_dir>/commands/``，与 ``data/skills`` 同一层）。
COMMANDS_DIR = "commands"

#: 自定义命令的文件后缀。ZCode 用 ``.md``。同一个目录里还可能有 README、
#: 示例之类的东西，靠后缀区分比靠"能不能解析"稳。
COMMAND_FILE_SUFFIX = ".md"

#: 命令名的合法形状，**逐字照抄 ZCode**（``^[a-z0-9][a-z0-9_:-]{0,63}$``）：
#: 小写字母或数字开头，之后允许 ``_``、``:``、``-``（ZCode 用冒号表达嵌套目录，
#: 例如 ``review/code.md`` → ``/review:code``）。**违规的文件直接丢弃并记原因**：
#: 名字不合法的命令在界面上就是"不见了"，而原因只有这里知道。
COMMAND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,63}$")

#: 参数没处可放时的兜底前缀（ZCode 的 ``User arguments:``）。
USER_ARGUMENTS_LABEL = "用户参数："

#: 命令目录的**递归深度上限**。命令目录是人手放的目录树，一层子目录已经够表达
#: 命名空间了（``git/commit.md``）；封顶是为了挡住"符号链接指回上级"这类布局
#: 把一次请求变成无限遍历——它不是功能，是护栏。
COMMAND_MAX_DEPTH = 3

#: 技能命令那条摘要的**字符上限**（约 40 个字：菜单那一行只放得下一句）。
#:
#: 动手截在**后端**是因为菜单还有一道 ``truncate``（``Menus.tsx``）：两层截断
#: 叠起来是"前端按像素切、后端按句子切"，读起来就是一句半。技能的 ``description``
#: 是给**模型**看的触发文本（可以是一整段），而菜单与 ``/help`` 要的是给人看的
#: 一句话——两件事在这一处分开。
SKILL_SUMMARY_CHARS = 40

#: 正文里的占位符。**一次扫描、四种写法**，顺序不能换：
#: ``$ARGUMENTS[0]`` 必须排在 ``$ARGUMENTS`` 前面，否则会被后者当成"整体参数"
#: 替掉、后面的 ``[0]`` 留在正文里。
#:
#: ``$ARGUMENTS`` 后面那个 lookahead 是给"``$ARGUMENTS[`` 但括号里不是数字"用的
#: （``$ARGUMENTS[名字]`` 不是占位符，原样留着比替成半截好）；``$name`` 那一段
#: **只在 frontmatter 的 ``arguments:`` 声明过时才替换**（见 ``render``）——
#: 否则正文里正常的 ``$`` 词（美元符号、变量名）会被吃掉。
PLACEHOLDER_RE = re.compile(
    r"\$ARGUMENTS\[(?P<whole>\d+)\]"
    r"|\$(?P<all>ARGUMENTS)(?![A-Za-z0-9_\[])"
    r"|\$(?P<index>\d+)"
    r"|\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
"""占位符的**词法与优先级**（同一条正则，四个分支）。"""

#: 命名参数的名字形状：只认 ASCII 标识符（``$名字`` 这种不替换——它没法与正文里
#: 的中文区分开）。``arguments:`` 里不合形状的项**丢弃**，不因此报错：
#: frontmatter 是手写的，为一个小笔误让整条命令打不开不值。
ARGUMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``mode/changed`` 事件的两个来源（P1-1 遗留第 6 条，payload 里的 ``source``）。
MODE_SOURCE_COMMAND = "command"
"""用户在会话里当场敲了 ``/mode``——切换就发生在这一条会话上。"""

MODE_SOURCE_SETTINGS = "settings"
"""这一轮开始时发现档与上一轮不同：上次是**在会话之外**改的（设置页、输入框那一排
的控件、``.env``）。我们的模式是应用级配置而不是会话字段（见 ``services/modes``），
所以"会话里换过档"只能靠这一处观测出来——不观测，那条审计信息在日志里永远是缺的。"""


# --------------------------------------------------------------------- 数据结构


@dataclass(frozen=True, slots=True)
class CommandDef:
    """一条命令的完整定义。

    ``name`` / ``summary`` / ``usage`` / ``details`` 四个字段**照 ZCode 的内置表**
    （调研报告 §2.7 第 1 条逐字列了它的实现）——``/help <命令>`` 与前端那个 ``/``
    菜单共用这一份数据，于是"菜单里有一个、``/help`` 里没有"这类不一致不可能发生。
    """

    name: str
    summary: str
    """一句话：这条命令是干什么的（菜单那一行 + ``/help`` 的列表）。"""
    usage: str = ""
    """怎么用（形如 ``/mode [goal|plan]``），空 = 没有参数。"""
    details: tuple[str, ...] = ()
    """展开说明（``/help <命令>`` 用它）。空 = 没有更多可说的。"""

    source: str = "builtin"
    """``builtin`` / ``user`` / ``repo``——**发现源**（内置 > 用户自定义 > 仓库自带
    的优先级、``is_builtin`` 与排序都读它）。技能命令也取它自己那条技能的来源
    （仓库 ``skills/`` → ``builtin``，``data/skills`` 与 ``~/.agents/skills`` →
    ``user``）——但**菜单不按它分组**：技能是单独一档，见 ``group``。"""
    argument_hint: str = ""
    """自定义命令 frontmatter 里的 ``argument-hint``（照 ZCode）。"""
    arguments: tuple[str, ...] = ()
    """frontmatter 的 ``arguments:``：**命名参数**（照 Claude）。

    按**位置**映射：``arguments: [topic, tone]`` 时正文里的 ``$topic`` 是第一个
    参数、``$tone`` 是第二个。它不改变别的占位符（``$1`` 仍是第一个，见 ``render``）。
    """
    skill: str = ""
    """**非空 = 这条命令是技能注册来的**，值就是技能名（``kylab-web`` 这种）。

    两条用法：``/skills`` 靠它把技能那批挑出来；``short_circuit`` 靠它把技能
    排除在"内置一律短路"之外——技能是**改写类**（正文要注入这一轮）。"""
    allowed_tools: tuple[str, ...] = ()
    """``allowed-tools``（照 ZCode）。**本轮只登记、不消费**：这一轮的工具表由模式与
    库范围决定（见 ``services/modes`` 模块头第 1 条"模式不改工具清单"），再加一条
    "按命令收窄工具"的路会与它打架。列在列表端点里，让人看得见它被读到了。"""
    model: str = ""
    """``model``（照 ZCode）。同上：本轮只登记。"""
    body: str = ""
    """正文（仅自定义命令）；内置命令是空串——它们的行为在协议层。"""
    path: str = ""
    """md 文件的绝对路径（内置命令为空）。排错时要能找到它。"""

    shadowed_by: str = ""
    """**被谁遮蔽**：同名的更高优先级命令（空 = 没被遮蔽）。被遮蔽的仍然出现在
    列表里（照插件列表的做法），但调不到。"""
    error: str = ""
    """加载失败的原因（人话）。空 = 没问题。失败的一条**也在列表里**——
    静默跳过等于把"为什么我放的文件不生效"变成一个查不出的问题。"""

    @property
    def is_builtin(self) -> bool:
        return self.source == "builtin"

    @property
    def group(self) -> str:
        """**菜单分组**：技能单独一档 ``skill``，其余取 ``source``。

        ``/chat/commands`` 的 ``group`` 给的就是它（``CommandOut.group`` 的四个取值
        ``builtin`` / ``user`` / ``repo`` / ``skill`` 与前端 ``Menus.tsx`` 的
        ``COMMAND_GROUPS`` 一一对应）。技能与内置/自定义分开摆的理由是**辨认**：
        ``/chat/commands`` 现在二十多条，技能混在内置里时 ``/rewind`` ``/status``
        ``/skills`` 这些会话动作被挤到首屏之外，而"技能是另一类东西"从命令名上
        看不出来（照 Claude 的菜单分区）。

        **与 ``source`` 分开是刻意的**：``source`` 是发现源、决定优先级与
        ``is_builtin``；把技能那批的 ``source`` 改成 "skill" 会让"仓库自带的技能
        算不算内置"这类判断全部走样。前端那边从"按 key 硬过滤、认不出的静默丢"
        改成了"认不出的收进末尾「其它」"（``Menus.tsx`` 的 ``OTHER_GROUP``），
        所以现在多一个取值**不会**让任何一条命令消失。
        """
        return "skill" if self.skill else self.source

    @property
    def short_circuit(self) -> bool:
        """是否**不进模型**（见模块头"命令分两类"）。

        内置的十二条里只有 ``/skill`` 不短路（它的正文就是这一轮的提示）；
        自定义命令与**技能命令**一律不短路（``skill`` 非空的那批是技能注册来的，
        它们与 ``/skill`` 同一条路）。

        ``/plan`` 是**看有没有参数的两面派**：不带描述时就是 ``/mode plan``（不碰模型），
        带上描述时那段描述是这一轮的提示（与 ``/skill`` 同一条路）。所以这个**表级**
        标记给的是保守口径（"不带参数时不产生回答"，菜单据此不建气泡），而**这一次**
        到底走哪条路由 ``api/v1/chat.py`` 的 ``_CommandResult.short_circuit`` 决定
        ——它看的是这次的结果带没带 ``prompt``。两处都留着是刻意的：菜单只拿得到表，
        真正的分流必须在拿到结果之后。
        """
        return self.is_builtin and not self.skill and self.name != NAME_SKILL

    @property
    def usable(self) -> bool:
        return not self.shadowed_by and not self.error


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """一次输入解析出来的结果：命令名 + 原样的参数字符串。"""

    name: str
    args: str = ""
    """``/mode plan`` → ``"plan"``（**原样**，不做切分：``$ARGUMENTS`` 要的就是它）。"""


# --------------------------------------------------------------------- 内置表

BUILTIN_COMMANDS: tuple[CommandDef, ...] = (
    CommandDef(
        name="help",
        summary="列出所有命令；给个命令名就看它的详细用法",
        usage="/help [命令名]",
        details=(
            "不带参数时列出全部可用命令（内置 + 自定义，按名字排序）。",
            "带一个命令名时展开它的用法与说明——与前端那个 `/` 菜单读的是同一份数据。",
        ),
    ),
    CommandDef(
        name="compact",
        summary="把较早的对话压成摘要，腾出上下文",
        usage="/compact",
        details=(
            "走的是平时那条压缩链路（services/chat.ChatService.compact）：把当前还没进",
            "摘要的对话交给模型压成一段摘要，并把标记推到最新一条消息。",
            "与自动压缩的区别只有一个——它不等占用越过阈值，你说压就压。",
            "**这一条自己要调用一次摘要模型**（压缩本身就是一次模型调用），",
            "但这一轮不产生回答、也不进模型历史。",
        ),
    ),
    CommandDef(
        name="new",
        summary="开一条新会话（当前这条留在历史里）",
        usage="/new",
        details=(
            "新建一条空会话并把界面切过去。库范围、模型、思考档取当前这条会话的，",
            "所以「新会话」不会把这一轮的选择也一起重置掉。",
            "当前这条会话不必担心：它**不会被删**，只是不再是当前会话。",
        ),
    ),
    CommandDef(
        name="stop",
        summary="停止正在跑的那一轮",
        usage="/stop",
        details=(
            "把这一轮叫停：已经流出来的正文留着（它仍然是有用的），会话日志里补一条",
            "`interrupted`——含「哪些调用没有结果」，下一轮据此知道哪些动作是半截的。",
            "没有在跑的一轮时如实回一句，不会假装做了什么。",
        ),
    ),
    CommandDef(
        name="mode",
        summary="切 Agent 模式（plan / build / edit / yolo）",
        usage="/mode [goal|plan]",
        details=(
            "不带参数时只报当前档，四档的语义见 services/modes.py：",
            "plan = 先给计划再动手 / build = 变更前确认（默认档）/",
            "edit = 自动编辑 / yolo = 少确认全放行。",
            "切换**下一轮生效**，并在会话日志里记一条 `mode/changed`",
            "（带 previousMode 与 source，照 ZCode 的 SessionModeChanged）。",
        ),
    ),
    CommandDef(
        name="model",
        summary="看这条会话用的对话模型，或者换一个",
        usage="/model [模型名]",
        details=(
            "不带参数时列出**可选的对话模型**，并标出这条会话现在用的是哪一个"
            "（筛选口径与输入框右侧那个 ModelPicker 一致：供应商启用、能力为空或含 chat）。",
            "带参数时把**这条会话**换成它：模型 ID（形如 gpt-4o）或清单里那个 pk 都认，",
            "认不出来时把可选清单回给你。",
            "写的是会话记录里那一栏（`ConversationService.set_model`）——与在界面上换模型",
            "是同一条链路，不是另存一份。",
        ),
    ),
    CommandDef(
        name="plan",
        summary="切到计划档；带上描述就直接开始规划",
        usage="/plan [描述]",
        details=(
            "不带参数就是 `/mode plan` 的语义化入口：切到 plan 档，并在会话日志里记一条",
            "`mode/changed`（source=command）。这一档下没给出计划、对方没确认之前，",
            "会改动东西的工具一律不执行（只读的照跑，见 services/plan_gate.py）。",
            "带上描述时那一段描述**当作这一轮的提示**（与 /skill 同一条改写法）：",
            "模型先给计划、等你确认，所以这一轮照常过模型、也照常有回答。",
        ),
    ),
    CommandDef(
        name="skill",
        summary="把某个技能的正文注入这一轮（并可选地带上任务）",
        usage="/skill <技能名> [任务]",
        details=(
            "照 ZCode 的 `/skill [<name> [task]]`：它**强制先加载技能**，把正文当作",
            "这一轮的提示，所以还是要过一次模型、还是会留下回答。",
            "不给任务时让它按技能的流程开始（需要什么会先问你）。",
            "技能已经有**自己那条命令**了：`/kylab-web [任务]` 与 `/skill kylab-web [任务]`",
            "是同一条路（见 `/skills`）。这一条是通用入口——技能名打错时它给的是",
            "一句人话，而 `/技能名` 会被当成「没有这个命令」。",
        ),
    ),
    CommandDef(
        name="rewind",
        summary="撤回最近 N 轮问答，并把那句提问填回输入框",
        usage="/rewind [轮数]",
        details=(
            "不带参数撤回 1 轮，`/rewind 2` 撤回最近 2 轮（一次最多 20 轮）。",
            "撤回 = 把这 N 轮的**提问与回答一起删掉**：它们不再出现在历史里，",
            "模型下一轮也看不到它们了（这一条是给“这个答案我不满意、重来一次”用的，",
            "而不是「再生成一个版本」——两条回答并存的代价是回看时还得选版本）。",
            "被撤掉的那句提问会**填回输入框**（结果里那个 `refill` 字段）：",
            "改一版再发，就是 Claude/Gemini 里 `/rewind` 的手感。",
            "撤回数超过这条会话的轮数时**如实说**，不会假装撤了什么。",
        ),
    ),
    CommandDef(
        name="context",
        summary="看这一轮的上下文被什么占着（按来源分解）",
        usage="/context",
        details=(
            "逐条列出：消息、系统提示词、技能目录、工具定义、记忆与人设、其它，",
            "外加合计与自动压缩的阈值——与输入框旁边那个仪表是**同一份数据**",
            "（`GET /chat/context-usage`，工具表也一样由协议层现拼）。",
            "数字是**估算**（按字符数算，刻意偏高）：真实的用量只有模型端点返回的",
            "`usage` 才知道，所以那一行必须跟着说。",
            "这一条**只读**：调它不会触发压缩。",
        ),
    ),
    CommandDef(
        name="status",
        summary="这条会话的一览（模型 / 模式 / 项目 / 轮数 / 上下文占用）",
        usage="/status",
        details=(
            "照 Claude 的 `/status`：把「现在这条会话是什么状态」一次说清，",
            "省掉「这个回答是用哪个模型、哪一档跑出来的」这种要翻三处才能拼出来的问题。",
            "只报**已有的**东西：模型与模式取当前生效值（并区分「会话自己选的」还是",
            "「跟随全局默认」），项目看它会话挂在哪个工作区，上下文占用与 `/context` 同源。",
        ),
    ),
    CommandDef(
        name="skills",
        summary="列出可用技能（每个技能都能直接当命令用）",
        usage="/skills",
        details=(
            "每个技能都是一条命令：`/技能名 [任务]` 等价于 `/skill 技能名 [任务]`",
            "（正文注入这一轮，照 Claude 的「命令＝技能」与 QwenPaw 的 `/<技能目录名>`）。",
            "名字不能当命令的、被同名命令遮蔽的也在列表里，并写明该怎么办",
            "——静默少一条只会让人以为技能没装上。",
        ),
    ),
)
"""内置命令表。

``/model`` 与 ``/plan`` 是**补**进来的那两条（开发计划 §12.225 P1-2 点名、第一轮漏掉）：
``/model`` 照 QwenPaw 的 ``/model`` 与 ZCode 的 ``CommandUiSpec.popupSelect``
（"命令弹一个选择列表"→ 我们没有那层 UI 协议，等价物就是"不带参数列清单、带参数切"），
``/plan`` 照 QwenPaw 的 ``/plan``（``/mode plan`` 的语义化入口，顺带把描述当这一轮的提示）。
上一轮只落六条的理由是"`/mode plan` 已经覆盖了切计划档、选模型在 ModelPicker 里更好用"；
现在的结论是**它们不是重复**：少一次"先敲 `/mode plan` 再重新说一遍任务"的往返，
而且脚本/MCP 那条没有界面的路上，`/model` 是唯一能换模型的地方。

后四条（`/rewind` `/context` `/status` `/skills`）来自《对话命令-调研 v0.1》§3 的
第 1-4 条差距：三家的共识是「**会话自身的动作**与**上下文/状态**都要是一等命令」
（我们此前只有界面上的按钮和一个仪表）。四条全是短路类——它们回答的是"现在怎么样"、
"撤掉什么"，没有一条需要模型。执行都在协议层（``api/v1/chat.py`` 的
``_dispatch_builtin``），这一张表只登记"有什么、怎么用、什么时候要模型"。
"""

#: 内置命令的名字。**常量一处定义**：协议层按名字分流（``/help`` 列什么、
#: ``/mode`` 怎么执行），散着写字符串就会出现"某处写成 mode、某处写成 modes"
#: 这种只有真调起来才发现对不上的错。
NAME_HELP = "help"
NAME_COMPACT = "compact"
NAME_NEW = "new"
NAME_STOP = "stop"
NAME_MODE = "mode"
NAME_MODEL = "model"
NAME_PLAN = "plan"
NAME_SKILL = "skill"
NAME_REWIND = "rewind"
NAME_CONTEXT = "context"
NAME_STATUS = "status"
NAME_SKILLS = "skills"

_BUILTIN_BY_NAME = {item.name: item for item in BUILTIN_COMMANDS}

#: ``/mode`` 能接受的取值。四档的**唯一来源**在 ``services/modes``，这里不另抄一份。
MODE_VALUES: tuple[str, ...] = MODES


def builtin_names() -> tuple[str, ...]:
    return tuple(item.name for item in BUILTIN_COMMANDS)


def is_builtin(name: str) -> bool:
    return name in _BUILTIN_BY_NAME


def modes_text() -> str:
    """两档的一句话清单（``/mode`` 不带参数时回给用户看）。"""
    return "\n".join(
        f"- {MODE_DEFS[name].label}（{name}）：{MODE_DEFS[name].hint}——{MODE_DEFS[name].detail}"
        for name in MODES
    )


# --------------------------------------------------------------------- 解析与渲染


def parse(text: str) -> ParsedCommand | None:
    """把一条输入解析成命令；**不是命令就返回 ``None``**。

    判定只有一条：**第一个词以 ``/`` 开头**。开头那个 ``/`` 之后的部分就是命令名
    （大小写不敏感，照 ZCode 的归一化：``/Help`` 与 ``/help`` 是同一条）。

    认不出的 ``/xxx`` **也返回命令**（``name="xxx"``），由调用方回一句"没有这个命令"
    ——DSH 那条「``/`` 行永不静默降级为普通 prompt」就是这个意思：静默发给模型的话，
    用户以为自己在用命令，而模型在猜他想说什么；两种体验差得远，而"那句话到底被谁读了"
    用户根本看不出来。

    只有一个孤零零的 ``/`` 时按 ``/help`` 处理（与各家终端里的补全一致）。

    **本函数不查命令表**（纯函数、无 IO）：解析与"存不存在"分开，是为了让
    "以 ``/`` 开头的输入一律走命令这条路"这个决定只写一次。
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    name = head.strip().lower()
    if not name:
        return ParsedCommand(name="help", args="")
    return ParsedCommand(name=name, args=rest.strip())


def split_args(args: str) -> list[str]:
    """按空白切成位置参数（``$1``…``$N`` 用）。

    **不解析引号**：ZCode 的参数语义只有两条占位符、没有引号转义那一层，
    而多一层语法就多一类"为什么我的引号被吃掉了"。要带空格的整段就用 ``$ARGUMENTS``。
    """
    return [item for item in (args or "").split() if item]


def render(command: CommandDef, args: str) -> str:
    """把一条**自定义**命令渲染成这一轮的提示正文。

    五种占位符在**一次扫描**里替掉（``PLACEHOLDER_RE``，顺序见那里的说明）：

    1. ``$ARGUMENTS`` → 全部参数**原样**（唯一能带空格的写法）；
    2. ``$1``…``$N`` → 按空白切分的第 N 个（1 起，越界替成空串、不报错）；
    3. ``$0`` → 第一个；``$ARGUMENTS[N]`` → 第 N 个（**0 起**，照 Claude）——
       ``$0`` 与 ``$1`` 都指第一个：``$1`` 是老口径（ZCode），不能改成"第二个"，
       那会让所有已经写好的命令当场坏掉；
    4. ``$name`` → frontmatter 的 ``arguments:`` 里声明的命名参数，**按位置**取
       （第 i 个名字取第 i 个参数）；没声明的 ``$name`` 一律**原样留着**——
       否则正文里正常的美元符号会被吃掉；
    5. **一个占位符都没命中、但用户给了参数** → 末尾追加 ``用户参数：<原文>``
       （那个"命令没收到参数"的困惑就是靠它消掉的）。

    "有没有命中"在替换时**顺便记下来**（``used``），不再单拿正则猜一遍：
    前四条的判据各不相同（``$ARGUMENTS[``、``$0``、声明过的名字……），
    猜的口径与替的口径一旦有两份，"参数被吃掉"与"参数被追加了两遍"就会同时出现。

    内置命令不走这里：``/skill`` 与技能命令的正文由 ``ChatService.skill_prompt``
    渲染，其余十条的行为在协议层。
    """
    body = command.body or ""
    text = (args or "").strip()
    pieces = split_args(text)
    named = _named_positions(command.arguments)
    used = False

    def _substitute(match: re.Match[str]) -> str:
        nonlocal used
        groups = match.groupdict()
        if groups["whole"] is not None:
            used = True
            index = int(groups["whole"])
            return pieces[index] if index < len(pieces) else ""
        if groups["all"] is not None:
            used = True
            return text
        if groups["index"] is not None:
            used = True
            index = int(groups["index"])
            # $0 = 第一个；$N（N>=1）= 第 N 个——两种下标在这一处并存，见文档
            position = index if index == 0 else index - 1
            return pieces[position] if position < len(pieces) else ""
        position = named.get(groups["name"] or "")
        if position is None:
            return match.group(0)
        used = True
        return pieces[position] if position < len(pieces) else ""

    body = PLACEHOLDER_RE.sub(_substitute, body)
    if text and not used:
        body = f"{body.rstrip()}\n\n{USER_ARGUMENTS_LABEL}{text}"
    return body.strip()


def _named_positions(names: Iterable[str]) -> dict[str, int]:
    """命名参数 → 位置（第几个）。**同名取第一个**，重复的名字不报错。"""
    out: dict[str, int] = {}
    for index, name in enumerate(names):
        out.setdefault(name, index)
    return out


# --------------------------------------------------------------------- 发现


def _repo_commands_dir() -> Path:
    """仓库自带的 ``commands/``（与 ``skills/`` 同级）。

    与 ``services/skills._repo_skills_dir`` 同一套推法：**按代码位置推**而不是按
    ``cwd``——后端可能从任何目录启动，而"自带的那批命令在哪"不该随启动目录变。
    """
    return Path(__file__).resolve().parents[3] / COMMANDS_DIR


class CommandService:
    """发现并渲染命令。**命令目录只读**：这个类不往里写任何东西。

    **无状态地扫盘**（与技能、插件同一条理由）：用户可能刚往里丢了一个 md 文件，
    所以每次调用重新扫；缓存要处理失效，为省这点开销不划算。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        builtin_dir: Path | None = None,
        conversations=None,  # type: ignore[no-untyped-def]
        skills=None,  # type: ignore[no-untyped-def]
        skill_summaries: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._data_dir = data_dir
        # 显式注入 > 按代码位置推。显式注入排最前是为了测试能指到临时目录
        # （断言的对象不该被"机器上恰好有什么"改掉），与技能那一档逐字一致。
        self._builtin_dir = builtin_dir if builtin_dir is not None else _repo_commands_dir()
        #: 只用来读"这条会话上一轮是哪一档"（模式观测的基线，见 ``ModeWatch``）。
        #: 可选：不给就退回"进程内见过的档"，脚本与单测不必为此造一个服务。
        self._conversations = conversations
        #: 技能注册表（``services/skills.SkillService``）：每个技能注册一条命令。
        #: **可选**：不给就没有技能那批（单测与不接技能的部署照常工作），
        #: 而且这一层不 import 技能服务——两边的扫描口径不会互相绑死。
        self._skills = skills
        #: ``技能名 → 中文简介``（市场装的那些有，见 ``api/v1/skills.py`` 的同一份数据）。
        #: 可选：不给就用技能的 ``description``——与能力页"没有中文简介就显示原文"
        #: 是同一条口径。
        self._skill_summaries = skill_summaries
        self.mode_watch = ModeWatch(self._baseline_mode)
        #: "谁在跑 / 谁被叫停了"（``/stop`` 用它，见 ``TurnControl``）。
        self.turns = TurnControl()

    # ------------------------------------------------------------------ 发现源

    @property
    def user_dir(self) -> Path:
        """用户级命令目录（``<data_dir>/commands/``）——放进来就是一个命令。"""
        return self._data_dir / COMMANDS_DIR

    @property
    def builtin_dir(self) -> Path:
        """随代码发布的 ``commands/``。**不存在就跳过**（空目录也是跳过，不是错误）。"""
        return self._builtin_dir

    # ------------------------------------------------------------------ 读

    def list(self) -> list[CommandDef]:
        """全部命令（含加载失败的与被遮蔽的）。顺序：内置在前，其余按名字。

        **first match wins**：同名时以先出现的那条为准——内置 > 用户自定义 >
        仓库自带 > **技能**（与技能那一档的"内置优先"同一条，与插件的"用户优先"
        相反；照 ZCode 的命令去重顺序改写，见模块头）。先到的那条留下，后来的那条
        **以 ``shadowed_by`` 留在列表里**（与"生效的那条"同名、排在它后面，
        界面据此显示"被谁遮蔽"）。

        技能排在**最后**是这一轮定的：用户敲 ``/mode`` 要的是内置那条，而一个
        恰好叫 ``mode`` 的技能不该把它顶掉——让位，但**看得见**（``/skills`` 会说
        "这条被 /mode 遮蔽了，用 /skill mode 绕开"）。
        """
        found: dict[str, CommandDef] = {}
        shadowed: list[CommandDef] = []
        sources = (
            BUILTIN_COMMANDS,
            self._load_dir(self.user_dir),
            self._load_dir(self.builtin_dir),
            self._skill_commands(),
        )
        for candidates in sources:
            for record in candidates:
                current = found.get(record.name)
                if current is None:
                    found[record.name] = record
                    continue
                shadowed.append(_shadowed(record, by=current.name))
        return sorted(
            [*found.values(), *shadowed],
            # 同名的两条里**能用的那条排前面**（它才是 ``find`` 会给出来的那条）
            key=lambda item: (item.source != "builtin", item.name, bool(item.shadowed_by)),
        )

    def skill_commands(self) -> list[CommandDef]:
        """技能注册来的那批（含被遮蔽的与读不出来的）——``/skills`` 用它。

        被遮蔽/读不出来的**也要给**：``/skills`` 的用处之一正是"我明明装了，
        为什么敲不出来"，把不合格的那些藏掉等于让这句话永远没人回答。
        """
        return [item for item in self.list() if item.skill]

    def find(self, name: str) -> CommandDef | None:
        """按名字取**能用的**那一条（被遮蔽的与坏掉的取不到）。"""
        record = self.any_named(name)
        return record if record is not None and record.usable else None

    def any_named(self, name: str) -> CommandDef | None:
        """按名字取一条，**不分能不能用**（``/help`` 要能解释"它为什么没生效"）。

        同名的两条（生效的 + 被遮蔽的）里**优先给能用的那条**：``find`` 与 ``/help``
        要的都是"这条命令现在的样子"。
        """
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        matches = [item for item in self.list() if item.name == wanted]
        for item in matches:
            if item.usable:
                return item
        return matches[0] if matches else None

    def catalog(self) -> list[CommandDef]:
        """能用的那批（菜单与 ``/help`` 用），按名字排序。"""
        return sorted((item for item in self.list() if item.usable), key=lambda item: item.name)

    # ------------------------------------------------------------------ 内部

    def _load_dir(self, root: Path) -> list[CommandDef]:
        """扫一个目录下的 ``*.md``，**递归**（子目录进命令名，见 ``_command_name``）。

        照 Claude 与 ZCode 的命名空间：``commands/git/commit.md`` → ``/git:commit``。
        "一个文件就是一条命令"没变，变的只是命令名怎么拼——所以 ``.``/``_`` 开头的
        **目录**与文件一样跳过（``_drafts/`` 里放的是草稿，不是命令）。
        """
        if not root.is_dir():
            return []
        return [
            self._load(path, name=_command_name(path.relative_to(root)))
            for path in _command_files(root)
        ]

    def _load(self, path: Path, *, name: str) -> CommandDef:
        """读一个命令文件。**坏文件不抛错**：一条命令写坏了不该让整个列表 500。

        处置与技能那一档一致：失败的那条**留在列表里**，带着原因。

        ``name`` 由调用方给（``_load_dir`` 按**相对路径**拼出来，带命名空间）：
        文件名只决定"合法不合法"，命令名是"文件名 + 它所在的那几层目录"。
        """
        # 合法性判**原样的名字**（照 ZCode 的那条正则：它约束的是文件名本身），
        # 命令名再归一成小写——所以 ``Bash_Tool.md`` 是"丢弃并记原因"，
        # 而不是被悄悄改成 ``bash_tool``（那种"改了名就生效"最难查）
        raw = name.strip()
        name = raw.lower()
        reason = _name_error(raw)
        if reason:
            return self._failed(path, name=name, reason=reason)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            # ``UnicodeDecodeError`` 是 ``ValueError`` 的子类、**不是 OSError**：
            # 只写 ``except OSError`` 会让它直接穿透（技能那一档真踩过）
            return self._failed(path, name=name, reason=f"读不到文件：{_read_reason(error)}")
        meta, body = parse_frontmatter(text)
        description = meta.get("description")
        hint = meta.get("argument-hint")
        model = meta.get("model")
        hint_text = hint.strip() if isinstance(hint, str) else ""
        arguments = _argument_names(meta.get("arguments"))
        return CommandDef(
            name=name,
            summary=(description.strip() if isinstance(description, str) else "")
            or _first_line(body)
            or "（这条命令没写 description）",
            # 没写 `argument-hint` 时用声明的命名参数拼一个：菜单里那一行要能看出
            # "这条命令还得给我点什么"，否则 `/deploy` 与 `/deploy 生产` 看起来一样
            usage=f"/{name}{' ' + (hint_text or _arguments_hint(arguments))}"
            if (hint_text or arguments)
            else f"/{name}",
            details=_arguments_details(arguments),
            source=self._source_of(path),
            argument_hint=hint_text,
            arguments=arguments,
            allowed_tools=_string_tuple(meta.get("allowed-tools")),
            model=model.strip() if isinstance(model, str) else "",
            body=body.strip(),
            path=str(path),
        )

    def _failed(self, path: Path, *, name: str, reason: str) -> CommandDef:
        """名字不合法时的那条记录：**留在列表里**（带原因），但调不到。"""
        return CommandDef(
            name=name,
            summary=reason,
            source=self._source_of(path),
            path=str(path),
            error=reason,
        )

    def _source_of(self, path: Path) -> str:
        """这条命令来自哪一层。

        **按"在不在用户目录下"判**（而不是"父目录等于用户目录"）：子目录现在是
        命令名的一部分（``commands/git/commit.md``），只比父目录的话，
        带命名空间的自定义命令会被判成"随代码自带"。
        """
        return "user" if path.is_relative_to(self.user_dir) else "repo"

    def _skill_commands(self) -> list[CommandDef]:
        """把**技能注册成命令**：每个技能一条 ``/<技能名> [任务]``。

        照 Claude 的"命令＝技能"（``.claude/skills/x/SKILL.md`` 也得到 ``/x``）与
        QwenPaw 的 ``/<技能目录名> [input]``。**只登记、不执行**：真正的注入复用
        既有那条改写路径（``ChatService.skill_prompt``，见 ``api/v1/chat.py`` 的
        ``_rewrite_prompt``）——技能正文怎么拼只有一处实现。

        三条口径：

        1. **名字不能当命令的照样登记**，带着 ``error``（人话）：中文名、带空格的
           名字敲不出来，但"我装了它、为什么没有那条命令"要有地方回答（``/skills``）；
        2. **读不出正文的（被丢弃的）也登记**，同样带 ``error``——与能力页一致；
        3. ``source`` 取技能自己那条来源（仓库自带 → ``builtin``，数据目录与
           ``~/.agents`` → ``user``）：优先级与 ``is_builtin`` 读它；**菜单分组另走
           ``group``**（技能那批一律 ``skill``，见 ``CommandDef.group``）。
        """
        if self._skills is None:
            return []
        try:
            records = list(self._skills.list())
        except Exception:
            # 技能清单读不出来不该让整个命令表 500：没有技能那批，其余照常
            logger.warning("读技能清单失败（命令表里不会有技能那批）", exc_info=True)
            return []
        summaries = self._skill_summaries() if self._skill_summaries is not None else {}
        out: list[CommandDef] = []
        for record in records:
            # 技能名归一成小写当命令名（命令名一律小写，照 ZCode）；
            # 合法性判**原样的技能名**，报错时也说原样的那个（用户认得出是哪个技能）
            raw_name = str(getattr(record, "name", "") or "").strip()
            name = raw_name.lower()
            source = "builtin" if getattr(record, "source", "") == "builtin" else "user"
            problem = _skill_name_error(raw_name)
            if not problem and getattr(record, "discarded", False):
                flagged = list(getattr(record, "flagged", ()) or ())
                why = flagged[0] if flagged else "缺 name 或 description"
                problem = f"这个技能没通过校验：{why}"
            out.append(
                CommandDef(
                    name=name,
                    summary=_skill_summary(
                        (summaries.get(raw_name) or "").strip()
                        or str(getattr(record, "description", "") or "")
                    )
                    or "（这个技能没写 description）",
                    usage=f"/{name} [任务]",
                    details=(
                        f"技能「{raw_name}」——与 `/skill {raw_name} [任务]` 是同一条路：",
                        "把它的正文注入这一轮（照 Claude 的「命令＝技能」）。",
                        f"技能名从技能仓库来：{getattr(record, 'path', '')}",
                    ),
                    source=source,
                    skill=raw_name or name,
                    path=str(getattr(record, "path", "") or ""),
                    error=problem,
                )
            )
        return sorted(out, key=lambda item: item.name)

    def _baseline_mode(self, conversation_id: str) -> str | None:
        """这条会话**上一次跑的时候**是哪一档（日志里没有记录就 ``None``）。

        读的是日志里最后一条 ``turn/start`` 的 ``mode``——那条事件是"这一轮以哪一档
        开跑"的权威记录（见 ``session_events.turn_start_draft``）。**一条会话只读一次**：
        ``ModeWatch`` 记住之后就不再问日志。
        """
        if self._conversations is None or not conversation_id:
            return None
        try:
            events = self._conversations.session_events(conversation_id, kinds=[KIND_TURN_START])
        except Exception:
            logger.warning("读会话上一轮的模式失败：%s", conversation_id, exc_info=True)
            return None
        for event in reversed(list(events)):
            mode = event.payload.get("mode")
            if isinstance(mode, str) and mode:
                return mode
        return None


def _shadowed(record: CommandDef, *, by: str) -> CommandDef:
    """把一条记录标成"被遮蔽"。

    dataclasses.replace 在这里不够用（要显式控制哪些字段跟着走），但**逐字段抄一份**
    也容易漏——所以这里只在末尾多写一个字段，其余原样带过去。
    """
    return CommandDef(
        name=record.name,
        summary=record.summary,
        usage=record.usage,
        details=record.details,
        source=record.source,
        argument_hint=record.argument_hint,
        arguments=record.arguments,
        skill=record.skill,
        allowed_tools=record.allowed_tools,
        model=record.model,
        body=record.body,
        path=record.path,
        shadowed_by=by,
        error=record.error,
    )


def _name_error(name: str) -> str:
    """文件名能不能当命令名（照 ZCode 的那条正则）。"""
    if not name:
        return "文件名不能当命令名（空）"
    if not COMMAND_NAME_RE.match(name):
        return (
            f"命令名 {name!r} 不合法（要求：小写字母或数字开头，之后只允许小写字母、"
            "数字、下划线、冒号、连字符，最长 64 个字符）"
        )
    return ""


def _command_files(root: Path) -> list[Path]:
    """递归列出 ``root`` 下的 ``*.md``（**跳过 ``.``/``_`` 开头的文件与目录**）。

    "``_`` 开头是模板/片段（不是命令）、``.`` 开头是编辑器临时文件"这条约定
    **对目录一并成立**（``_drafts/`` 里放的是草稿）——所以是"下探之前先跳过"，
    而不是"进来了再过滤"。

    手写这一层（不用 ``rglob``）图的是两件事：能**在下探之前剪枝**、以及一个
    **深度上限**（``COMMAND_MAX_DEPTH``）——符号链接指回上级那种布局会让递归
    永远走不完，而那是"一次请求把进程挂住"，不是功能问题。读不了的目录跳过，
    与"坏文件不抛错"同一口径。
    """
    out: list[Path] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith((".", "_")):
                continue
            if entry.is_dir():
                if depth < COMMAND_MAX_DEPTH:
                    pending.append((entry, depth + 1))
                continue
            if entry.suffix.lower() == COMMAND_FILE_SUFFIX:
                out.append(entry)
    return sorted(out)


def _command_name(relative: Path) -> str:
    """子目录进命令名：``git/commit.md`` → ``git:commit``。

    分隔符用 ``:``（ZCode 的嵌套目录命令名就是它，而 ``COMMAND_NAME_RE`` 已经允许
    这个字符）而不是 ``/``：命令名是一个**词**，斜杠会让"``/git/commit``"看起来
    像一条路径，而解析器只切到第一个空格。

    **不在这里归一大小写**（只 strip）：合法性判的是原样的名字，``Bash_Tool.md``
    要"丢弃并记原因"而不是被悄悄改成 ``bash_tool``——归一那一步在 ``_load`` 里。
    """
    parts = [*relative.parts[:-1], relative.stem]
    return ":".join(parts).strip()


def _skill_name_error(name: str) -> str:
    """技能名能不能直接当命令（不能就是一个 ``error`` 记录）。

    比 ``_name_error`` 多一句出路：技能**还有 ``/skill <名字>`` 这条通用入口**，
    所以这里必须说清"不能用哪条、改用哪条"，否则用户只会以为技能没装上。

    与内置/自定义命令**重名不算错**：那是"让位"（first match wins，见 ``list``），
    由 ``shadowed_by`` 表达——同一件事不在这里说第二遍。
    """
    if not name:
        return "这个技能没有名字，用不了"
    if not COMMAND_NAME_RE.match(name):
        return (
            f"技能名 {name!r} 不能直接当命令（只允许小写字母、数字、下划线、"
            "冒号、连字符）：用 /skill <技能名> [任务] 一样能调它"
        )
    return ""


def _arguments_hint(names: tuple[str, ...]) -> str:
    """没写 ``argument-hint`` 时，用声明的命名参数拼出用法那一行。"""
    return " ".join(f"<{name}>" for name in names)


def _arguments_details(names: tuple[str, ...]) -> tuple[str, ...]:
    """``/help <命令>`` 里那一条"命名参数怎么用"的说明（没声明就是空）。

    没有它，``arguments: [topic, tone]`` 就只是"文件里的一行"：用户看不出
    ``$topic`` 到底取第几个参数——而那是这套语法唯一会猜错的地方。
    """
    if not names:
        return ()
    listed = "、".join(f"${name}（第 {index + 1} 个）" for index, name in enumerate(names))
    return (
        f"命名参数：{listed}。",
        "它在 frontmatter 的 `arguments:` 里声明、按位置取——"
        "`$ARGUMENTS`（全部）、`$0`/`$1`（第一个）、`$ARGUMENTS[0]`（0 起）照常能用。",
    )


def _argument_names(raw: object) -> tuple[str, ...]:
    """frontmatter 的 ``arguments:`` → 命名参数（保持书写顺序，去掉不合形状的）。

    与 ``_string_tuple`` 同一套宽容口径（列表／逗号分隔的字符串都认），但**多一道
    形状筛选**：``$name`` 的匹配只认 ASCII 标识符，把 ``arguments: [topic, "语 气"]``
    里的第二项也登记进来的话，正文里永远替不到它——那是一个查不出原因的哑坑。
    不合形状的**丢弃而不报错**：frontmatter 是手写的，为一个小笔误让整条命令打不开不值。
    """
    names = _string_tuple(raw)
    out: list[str] = []
    for name in names:
        if ARGUMENT_NAME_RE.match(name) and name not in out:
            out.append(name)
    return tuple(out)


def _one_line(text: str) -> str:
    """压成一行、截断（技能描述里可能是一段带换行的说明，菜单那一行放不下）。"""
    return " ".join((text or "").split())[:200]


#: 句子结束的判据：中文那三个直接算；英文的 ``. ! ?`` 只在**后面跟着空白或到头**时
#: 才算一句的结束——否则 ``v1.2`` / ``e.g.`` 里的点会把一句话从中间切开。
_SENTENCE_END_RE = re.compile(r"[。！？]|[.!?](?=\s|$)")


def _skill_summary(text: str) -> str:
    """技能命令那一行的摘要：**有中文简介就用它，没有就取英文描述的第一句**，再截短。

    技能的 ``description`` 是给**模型**看的触发文本（实测长这样：``Look something up
    on the live web and read the page — "查一下", "搜一下"…`` 一整段），整段塞进菜单
    那一行会被前端再 ``truncate`` 一次，读起来就是一句半。所以这里取第一句、再截到
    ``SKILL_SUMMARY_CHARS``（约 40 个字）——**截断只在后端做**，前端那道 ``truncate``
    从此几乎不生效。

    市场装的那些有中文 ``summary``（与能力页同一份数据，见 ``skill_blurb``）：
    它本来就是给人看的一句话，取第一句 + 截短只是兜住"有人写得很长"。
    """
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    match = _SENTENCE_END_RE.search(flat)
    if match:
        flat = flat[: match.end()].strip()
    return _clip(flat, SKILL_SUMMARY_CHARS)


def _clip(text: str, limit: int) -> str:
    """截到 ``limit`` 个字符，**尽量断在一个短语边界上**（截出来要还是人话）。

    边界只找后半段里的（下标 ``limit // 2`` 之后）：一句话开头就遇上逗号时在那儿
    断，等于把大半句丢掉。找不到边界就硬截——那也比让前端按像素切好。
    """
    if len(text) <= limit:
        return text
    head = text[: limit - 1].rstrip()
    for mark in ("，", "。", "；", "、", ",", ";", ":", "——", "—", " "):
        cut = head.rfind(mark)
        if cut >= limit // 2:
            return head[:cut].rstrip() + "…"
    return head + "…"


def _string_tuple(raw: object) -> tuple[str, ...]:
    """frontmatter 里的列表字段：字符串按逗号切，列表逐项转字符串。

    与 ``memory_files._tags_of`` 同一套宽容口径——frontmatter 是手写的，
    ``allowed-tools: [read, write]`` 与 ``allowed-tools: read, write`` 都要认。
    """
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(part).strip() for part in raw]
    else:
        return ()
    return tuple(part for part in parts if part)


def _first_line(body: str) -> str:
    """正文第一行非空文字（没写 ``description`` 时拿它当摘要）。"""
    for line in (body or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return ""


def _read_reason(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text[:200] if text else error.__class__.__name__


# --------------------------------------------------------------------- 模式观测


class ModeWatch:
    """**会话级的模式观测**：记一条会话上一次跑的是哪一档（P1-1 遗留第 6 条）。

    为什么需要它：我们的模式是**应用级配置**（``chat.mode``，见
    ``services/runtime_config``），而 ZCode 那条 ``SessionModeChanged`` 是**会话级**
    事件。设置页/输入框那个控件改档时手里没有会话，写不出事件；所以这里在每一轮开始时
    对照一下"这一轮是哪一档、上一轮是哪一档"，变了就补一条 ``mode/changed``
    （``source="settings"``）。**在会话里当场切的**（``/mode``）由协议层直接记
    （``source="command"``），并顺手 ``note`` 到这里，免得下一轮再补一条重复的。

    **进程内**（不持久化），基线在第一次遇到某条会话时从日志里读一次
    （见 ``CommandService._baseline_mode``）——重启之后也不会漏记第一条会话的那次切换。
    """

    def __init__(self, baseline: Callable[[str], str | None] | None = None) -> None:
        self._baseline = baseline or (lambda _conversation_id: None)
        self._seen: dict[str, str] = {}
        self._lock = threading.Lock()

    def observe(self, conversation_id: str, mode: str) -> str | None:
        """这一轮开跑前记一笔；**档变了就返回上一档**，没变（或无从比较）返回 ``None``。

        没有会话（脚本、无状态调用）时返回 ``None``：没有会话就没有可落的日志。
        ``None`` 也是"日志里查不到上一档"时唯一的答案——**宁可不记，也不写一个猜的
        previousMode**：审计日志里出现一个错的"上一档"，比缺一条难查得多。
        """
        if not conversation_id:
            return None
        with self._lock:
            previous = self._seen.get(conversation_id)
            if previous is None:
                previous = self._baseline(conversation_id)
                self._seen[conversation_id] = mode
                if previous is None or previous == mode:
                    return None
                return previous
            self._seen[conversation_id] = mode
            return previous if previous != mode else None

    def note(self, conversation_id: str, mode: str) -> None:
        """**不比较、只同步**：调用方已经自己记过这条切换了（``/mode`` 那条路）。"""
        if not conversation_id:
            return
        with self._lock:
            self._seen[conversation_id] = mode

    def forget(self, conversation_id: str) -> None:
        """忘掉一条会话（删除会话时调用；best-effort，留着只是多占一点内存）。"""
        with self._lock:
            self._seen.pop(conversation_id, None)


class TurnControl:
    """**哪条会话上正有一轮在跑**，以及"把它停掉"（P1-2 的 ``/stop``）。

    ``/stop`` 是三家共同的一条纪律（调研报告 §2.7 抄点第 5 条：「审批/停止也要是
    **一等命令**，没有前端按钮时仍能跑通」）。我们这边"这一轮"跑在**另一个请求**里
    （SSE 那条流，见 ``api/v1/chat.py``），所以这一层只做两件事：登记"谁在跑"、
    挂一个停止标记让那条流自己收工。

    **为什么不是直接杀线程**：那条流的线程可能正卡在一次工具调用里（抓网页、
    等模型），从外面掐掉它的结果是"这一轮没有任何收尾"——而后端要记的那条
    ``interrupted``（含"哪些调用没有结果"）恰恰是在收尾里写的。所以停止是**协作式**的：
    标记一下，那条流在两次事件之间自己看到、自己收尾。

    **进程内**（不持久化）：一轮的寿命就是这条流，进程重启之后本来也没有"在跑的一轮"了。
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._stopped: set[str] = set()
        self._lock = threading.Lock()

    def begin(self, conversation_id: str | None) -> None:
        """这一轮开始跑了（**清掉上一轮可能留下的停止标记**，否则新一轮一进来就被停）。"""
        if not conversation_id:
            return
        with self._lock:
            self._active.add(conversation_id)
            self._stopped.discard(conversation_id)

    def end(self, conversation_id: str | None) -> None:
        """这一轮收工了（无论成功、失败还是被停）。"""
        if not conversation_id:
            return
        with self._lock:
            self._active.discard(conversation_id)
            self._stopped.discard(conversation_id)

    def request_stop(self, conversation_id: str) -> bool:
        """请求停止；**返回"当时确实有一轮在跑"**（没有就是没有，不假装）。"""
        if not conversation_id:
            return False
        with self._lock:
            if conversation_id not in self._active:
                return False
            self._stopped.add(conversation_id)
            return True

    def stop_requested(self, conversation_id: str | None) -> bool:
        """这一轮该收工了吗（流在两次事件之间问它；**只读**）。

        非破坏性地读：一次停止请求对它之后的**每一次**检查都成立——流那边发现它之后
        还要走收尾（补事件、发 done），而收尾里又会看一眼，撕掉标记的话第二次就看不到了。
        标记由 ``begin`` / ``end`` 清。
        """
        if not conversation_id:
            return False
        with self._lock:
            return conversation_id in self._stopped

    def running(self, conversation_id: str | None) -> bool:
        """这条会话上有没有一轮在跑（界面/脚本查询用）。"""
        if not conversation_id:
            return False
        with self._lock:
            return conversation_id in self._active
