"""技能注册表（v0.15，设计见 ``docs/设计/Agent-工作区与能力层设计-v0.1.md`` §6.1）。

**技能 = 磁盘上一个带 frontmatter 的 ``SKILL.md``**（与 QwenPaw / Claude Code 同格式，
仓库里自带的那份 ``skills/kylab-knowledge-base/SKILL.md`` 就是）。它不是代码插件，
而是**写给模型看的流程与索引**——这一点决定了它的加载方式。

**加载分两段，这是它省 token 的关键**：

1. **目录注入**（``catalog``）：把 ``name`` + 截断描述 + ``when_to_use`` + 文件路径拼进
   system prompt，每个技能一行、**每个请求都注入**（P0-3 之前要靠模型调 ``list_skills``
   才知道有什么，它经常不调，于是技能等于不存在）。模型据此知道"有哪些能力、什么时候该用"。
2. **按需展开**（``read``）：模型决定用某个技能时，才把正文读进来。正文里指向的
   ``references/*.md`` 再按需读——**目录里的细节不进上下文**。

于是"装二十个技能"的代价是二十行文本，而不是二十篇文档。

**来源目录**（按顺序合并，同名以**先出现的**为准，因为先出现的是随代码发布的）：

1. 仓库自带 ``skills/``：随代码走，如 ``kylab-knowledge-base``；
2. 数据目录 ``data/skills/``：用户自己放、或从市场装的；
3. ``~/.agents/skills``（P0-3）：**跨工具事实标准路径**——ZCode / Claude Code / Codex / Cursor
   都扫这一处（调研 §2.3："照抄，白捡生态"），放进去的技能这几个工具都能用。

顺序抄 ZCode 的发现顺序（``zcode-guide`` 的 ``diagnosing-skills`` §1：显式根 → 用户
``~/.zcode/skills`` → 用户 ``~/.agents/skills`` → 工作区… → 插件，first match wins）：
**工具自己的用户目录先于共享的 ``~/.agents``**。我们裁到三层——没有"显式根"（那是构造参数
``builtin_dir``）、没有工作区与插件这两档（技能是部署级能力，工作区那档见设计文档 §6.1）。
同一个名字**先出现的遮蔽后出现的**：仓库自带的那份是"我们调过的版本"。

**校验前置（P0-3，照 ZCode）**：``SKILL.md`` 的 frontmatter 里缺 ``name``、缺 ``description``、
或描述超过 1024 字符 → **整个技能被丢弃**（不进目录、也不给 ``read_skill`` 读），
理由记在记录里并出现在能力页上。识别 ``name`` / ``description`` / ``when_to_use`` /
``license`` / ``metadata`` 五个扁平 ``key: value`` 字段（同 ZCode，多出来的一律忽略，
不报错），外加我们自己扩展的 ``summary``（**给人看的一句中文简介**：市场装的技能那份
存在安装清单里，仓库自带的写在 frontmatter）。**丢弃 ≠ 静默**：坏技能照旧列在
``list()`` 里，带着"为什么不用它"。

> 一处**有据的偏离**：ZCode 的第二档是"**完全没有 frontmatter** 的技能仍然加载，
> name 退化成目录名、description 为空"（``diagnosing-skills`` §2「Loads but may not
> trigger」）。我们把它归进"丢弃"——目录里那一行是这个技能唯一的存在方式，
> 一行的 description 为空的技能在 KYLAB 里没有任何地方能用它，
> 留着只会让人以为装上了（旧行为就是这样）。所以口径是"**能用才留**"。

**渐进披露（P0-3）**：上面分两段的那条分工就是三家（ZCode / DSH / QwenPaw）的共同做法
（调研 §2.3）——目录每轮都在提示词里，正文按需取。

**预装与展示是两件事**（v0.61，产品口径）：库里几千条时，"哪些是产品能力""哪些只是
装进来的长尾"必须分开答，否则目录会被长尾占满。

1. **常驻名单**（`RESIDENT_SKILLS`）：**元能力 + 跨领域通用件**，总量压在 5–20 个。
   只有这一批享受"任何库规模下都进目录"的待遇；
2. **每类精选**（`featured_by_category`）：13 个分类各排前 2 条，判据机械可复现
   （内置 → 装进来的 → 描述完整度 → 名字可读性，见 `_featured_rank`），
   **只给界面默认展示用，不进提示词目录**——目录的 60 条 / 2 万字符预算一位不变，
   prompt 不因为这次口径而变大；
3. **长尾一条不删**：仍然在 `list()` / `list_skills` / 搜索里，只是默认不铺满界面。

同行的官方预装口径是同一路（OpenAI `.system` 5 个 + `.curated` 按需、Anthropic
`anthropics/skills` 19 个分 4 类）：**预装只放元能力与跨领域通用件，领域内的按任务拆，
长尾靠搜索可得**。

仓库自带那一份的**位置**有三种来源，优先级从高到低（v0.1.1）：

1. 构造时显式注入（测试与工具用：断言的对象不该被"机器上恰好设了环境变量"改掉）；
2. ``KYLAB_SKILLS_DIR`` 环境变量——**部署路径靠它**，镜像里由 Dockerfile 的 ENV 钉死；
3. ``Path(__file__).resolve().parents[3] / "skills"``——只对开发布局成立
   （``backend/app/services/skills.py`` 往上四层是仓库根），**兜底用**。
   容器里代码在 ``/app/app/services``，往上四层是 ``/``，数出来的目录并不存在
   （§12.224 第 9 条：这就是容器里一个预装技能都看不见的原因）。
   所以"数层数"不能是唯一的路，才加了第 2 条。

**安全**：技能是"会进模型上下文、并且能影响它怎么行动"的文本，所以它是注入面。
QwenPaw 为此有 Skill Scanner。这里做**最小必要**的两条：
可疑指令（"忽略之前的指令"这类）与疑似凭据（``sk-`` 开头的长串）会被标出来，
被标的那条**不进目录**（模型看不见 = 无法被它驱动），但**在接口里如实列出**并给出理由
——静默藏掉会让用户以为技能装失败了。技能本身**不因此获得任何工具权限**：
能不能读文件、能不能跑命令，由工作区与工具策略决定，与技能文本无关。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.core.exceptions import NotFoundError
from app.services.memory_files import parse_frontmatter
from app.services.runtime_config import SETTING_GROUPS
from app.services.skill_categories import CATEGORIES, category_of
from app.services.skill_tools import catalog_note, translate_tool_names

__all__ = [
    "AGENTS_SKILLS_SUBPATH",
    "BUILTIN_DIR_ENV",
    "CATALOG_BUDGET_CHARS",
    "CATALOG_DESCRIPTION_CHARS",
    "FEATURED_PER_CATEGORY",
    "MAX_DESCRIPTION_CHARS",
    "RESIDENT_SKILLS",
    "SKILL_FILE",
    "SkillRecord",
    "SkillService",
    "featured_by_category",
]

logger = logging.getLogger(__name__)

#: 技能文件名。大写是这一族的约定（Claude Code / QwenPaw 都是 `SKILL.md`）。
SKILL_FILE = "SKILL.md"

#: 单条技能的启停存在这个运行期配置里（D23）：**逗号分隔的 slug 列表**，关掉的在里面。
#: 用 slug 而不是显示名：slug 是 `normalize_name` 归一化过的（只含安全字符），
#: 逗号分隔因此不会有歧义，也不必为了一个列表去引 JSON。
DISABLED_SKILLS_KEY = "chat.disabled_skills"

#: 关掉一个技能之后它在列表里显示的理由（与"被安全扫描拦下"共用 `flagged` 那个字段）。
DISABLED_REASON = "已被你关掉：它不会进提示词，模型也不会知道有它"

#: "随代码发布的那批技能在哪"的环境变量。见模块头的三种来源。
BUILTIN_DIR_ENV = "KYLAB_SKILLS_DIR"

#: 跨工具共享的用户级技能目录（相对家目录）。ZCode / Claude Code / Codex / Cursor
#: 都扫这一处，是最接近"事实标准"的一条路径（调研 §2.3）。
AGENTS_SKILLS_SUBPATH = (".agents", "skills")

#: frontmatter 里 ``description`` 的**硬上限**：超过就把整个技能丢掉。
#: 抄 ZCode（``zcode-guide`` 的 ``diagnosing-skills`` §2「Fails to load (the skill is dropped)」：
#: "``description`` exceeds 1024 characters"；§4.8 的处置是"trim it, move detail into the body"）。
MAX_DESCRIPTION_CHARS = 1024

#: 目录里每条描述（含 ``when_to_use``）截断到多少字符。抄 ZCode 的 "~250 characters"
#: （``diagnosing-skills`` §2）：触发词要写在前 250 个字符里，否则模型看不到。
CATALOG_DESCRIPTION_CHARS = 250

#: 整段技能目录的字符预算。抄 ZCode 注入 system-reminder 时用的 2 万字符常数
#: （调研 §2.3："描述截断 250 字、整段预算 2 万字符"）。超预算就少给后面几条——
#: 目录是**增强**，不能挤掉对话本身。
CATALOG_BUDGET_CHARS = 20_000

#: 正文上限：技能是流程与索引，不是手册。超长的应该拆进 references/。
MAX_BODY_CHARS = 60_000

#: 目录注入时最多带几条。装几百个技能时，目录本身也会变成负担。
#: 与 ``CATALOG_BUDGET_CHARS`` 的关系：描述按上面的常数截断后，60 条 ≈ 1.8 万字符，
#: 所以**正常情况下先撞上的是这个条数上限**；字符预算是给"名字很长"这类病态目录兜底的。
MAX_CATALOG = 60

#: **整次扫描结果的复用窗口**（秒）。
#:
#: 为什么还需要它（两层缓存之后剩下的那点成本）：单条记录按 `SKILL.md` 的
#: mtime+size 校验，**每条都是一次 `stat`**；这个库有 6,000+ 条，在 Windows 上
#: 那 6,000 次 stat 就是几百毫秒——而"上一轮刚扫过、这一轮又扫"之间，
#: 没有任何人可能改文件（目录注入是每轮都做的事）。
#:
#: 到期之后**照样逐个校验**（记录缓存负责只重读变了的那几个），但那次校验本身
#: 在 Windows 上要 **2.7 秒**（2026-09-29 实测：12,337 条技能、每条一次 `stat`）——
#: 所以窗口不能太短：它是"每轮对话都会走的那条路"，不能每几秒就撞上一次。
#: 取 30 秒的账是：稳态每轮 **15–90 毫秒** ✓，最坏撞上窗口到期的那一次多花 2.7 秒
#: （30 秒里最多一次），而"改完立刻可见"由 `invalidate()` 与 `scan_ttl_seconds=0` 负责。
#:
#: 库再涨（上万条、几十万文件）时的下一步不是调这个数，而是**换失效信号**：
#: 让导入器在写完时调一次 `invalidate()`，或者挂一个文件系统监听——两者都能
#: 把"全量 stat"彻底从请求路径上拿掉。
SCAN_TTL_SECONDS = 30.0

#: **常驻名单**：产品能力 + 跨领域通用件，**任何时候都在目录里**。
#:
#: 库涨到几千条之后，"目录里露哪 60 条"成了一个真问题：原先按扫描顺序取前 60，
#: 于是模型很可能看不到"交付一个 PPT / 存一份文件到知识库"这类**产品自带**的动作。
#: 这几条是产品能力，不是"某个第三方技能"，**任何库规模下都得在**。
#:
#: **为什么不能再多**（v0.61 口径）：常驻名额是"每轮都进提示词"的固定税，只给两类——
#:
#: 1. **元能力**（`kylab-*` 那五条）：产品的自带件，它们本身就是"怎么用这个 agent"；
#: 2. **跨领域通用件**（四种格式各一条）：任何领域都会碰到的交付物形态
#:    （做幻灯片 / 表格 / 文档 / PDF），不是某个领域的活。
#:
#: 领域件（`excel-analyst-pro` / `slide-skill` / `pdf-pro` / `office-automation` 这类）
#: **从常驻降级**：它们各自落在某个分类里，由"每类精选 2 条 + 搜索"承担，
#: 用户真要常用某一条，装一次就进了列表（`data/skills/`），不必占所有人的提示词。
#: 名字写了但库里没有的直接跳过（清单写错不该让目录少一条或报错）。
RESIDENT_SKILLS: tuple[str, ...] = (
    "kylab-delegate",
    "kylab-knowledge-base",
    "kylab-memory",
    "kylab-office-export",
    "kylab-web",
    "pptx",
    "xlsx",
    "docx",
    "pdf",
)

#: **每个分类默认露几条**（v0.61）：给界面默认展示用，**不进提示词目录**。
#: 2 是"够看出这一类是什么"的最小值：一条看不出规律，三条以上就该翻了。
FEATURED_PER_CATEGORY = 2

#: ``requires`` 里认识的四个键。**与 OpenClaw 的门控字段是同一批**
#: （见《预装技能选型》§4.2）：它们都是"这个技能在这台机器上跑不跑得起来"的
#: 客观条件，而不是"我们想不想让它跑"。
REQUIRE_KEYS = ("config", "binaries", "env", "os")

#: ``sys.platform`` → 写在技能里的平台名。
_PLATFORMS = {"linux": "linux", "darwin": "darwin", "win32": "windows"}

#: 疑似"试图操纵模型"的写法。命中**不足以判断恶意**（安全文档里也可能出现这些词），
#: 所以处置是"不进目录 + 标出来给人看"，而不是拒绝加载或报错。
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("忽略先前指令", re.compile(r"忽略(之前|上面|前面|以上)的?(所有)?指令", re.I)),
    (
        "ignore previous",
        re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    ),
    (
        "要求泄露系统提示",
        re.compile(r"(泄露|输出|reveal|print).{0,10}(system\s*prompt|系统提示)", re.I),
    ),
    ("伪装成系统消息", re.compile(r"</?(system|assistant)>|\[\s*system\s*\]", re.I)),
)

#: 疑似凭据。技能里**不该**出现密钥——需要凭据的地方走环境变量或设置页。
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("疑似 API Key", re.compile(r"\b(sk|ak|ghp|xox[baprs])[-_][A-Za-z0-9_\-]{16,}")),
    ("疑似私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "疑似令牌赋值",
        re.compile(r"(api[_-]?key|token|secret)\s*[:=]\s*['\"][^'\"]{20,}['\"]", re.I),
    ),
)


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """一个技能。``body`` 只在 ``read`` 时取，列表里不带（省内存也省不住什么，
    但它提醒调用方"目录注入只用得到 name + description"）。"""

    name: str
    description: str
    path: str
    """``SKILL.md`` 的绝对路径（界面上要能点开看，排错时也要能找到它）。"""
    source: str
    """``builtin`` = 仓库自带；``user`` = 数据目录里用户放的；``agents`` = ``~/.agents/skills``
    （跨工具共享的那一层，见模块头的发现顺序）。"""
    directory: str
    """技能目录（``references/`` 相对它解析）。"""
    used_by_prompt: bool = True
    """会不会进 system prompt 的目录。被安全扫描拦下的、被门控挡住的、被丢弃的都是 ``False``。"""
    flagged: tuple[str, ...] = ()
    """没进目录的原因（人话）。空 = 没发现问题。"""
    when_to_use: str = ""
    """``SKILL.md`` 的 ``when_to_use``（可选，ZCode 的五个识别字段之一）。目录里跟在描述后面。"""
    summary: str = ""
    """``SKILL.md`` 的 ``summary``（可选，v0.53）：**给人看的一句中文简介**。

    它不进模型的技能目录——``description`` 才是给模型看的触发文本，两份各管一头。
    仓库自带的技能（内置那 5 个）就靠它显示中文；市场装的技能这份简介存在安装清单里
    （``data/installed.json``，来源是技能源的中文摘要），两条路的消费方是同一处
    （``api/v1/skills.py`` 与 ``core/services.py`` 的 ``_skill_summaries``）。
    """
    relative_path: str = ""
    """相对**发现根**的位置（``<组>/<名字>/SKILL.md``）。目录里那一行用它而不是绝对路径：
    绝对路径带用户名与机器布局，进提示词只是噪音（要排错时接口里有 ``path``）。"""
    category: str = ""
    """分类（v0.61）：`skill_categories.CATEGORIES` 里的 slug，取不到信号时是 ``"other"``。

    **它是算出来的，不是技能自己声明的**——技能格式里没有分类字段（第三方还常写
    `tags:`，解析器一律忽略），所以这里用的是那一套可复现的加权信号
    （`skill_categories.category_of`，同一份规则生成页面读的那张离线映射）。
    算在扫描这一步（`_parse`）是为了**跟着文件走**：分类与描述同源，
    `SKILL.md` 一改，记录缓存按 mtime 失效，分类跟着重算，不存在"缓存住旧分类"。
    空串 = 没经过分类（只有手工构造的记录会这样），接口上照实回空串。

    它**不进提示词目录**：目录里那一行是给模型看"何时该用"的，分类是给人分组用的。
    """
    discarded: bool = False
    """坏到**不该被使用**（缺 name / 缺 description / 描述超长，见 ``_drop_reason``）：
    不进目录、``read()`` 也读不出来。仍然留在 ``list()`` 里，好让能力页说清为什么。"""

    @property
    def slug(self) -> str:
        """``name`` 的规范化形式，用来做事后查找（大小写与空格不敏感）。"""
        return normalize_name(self.name)


def normalize_name(raw: str) -> str:
    return " ".join((raw or "").split()).strip().casefold()


def _builtin_dir_from_env() -> Path | None:
    """读 ``KYLAB_SKILLS_DIR``；没设或只是空白就返回 ``None``（= 交给下一条路）。

    目录不存在时**只警告不报错**：技能是增强不是依赖，少一批技能不该让服务起不来。
    但警告不能省——这条路径配错的表现就是"内置技能一个都不出现"，而那看起来
    和"仓库本来就没带技能"一模一样，没有一行日志根本查不到。
    """
    raw = (os.environ.get(BUILTIN_DIR_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        logger.warning("%s 指向的目录不存在：%s（内置技能会一个都扫不到）", BUILTIN_DIR_ENV, path)
    return path


def _repo_skills_dir() -> Path:
    """按代码位置推仓库自带的 ``skills/``（只对开发布局成立，见模块头）。"""
    return Path(__file__).resolve().parents[3] / "skills"


def _agents_skills_dir() -> Path:
    """``~/.agents/skills``——跨工具共享的用户级目录（见模块头的发现顺序）。

    读家目录这件事**只在构造服务时发生一次**，而且目录不存在就等于没有
    （``_skill_dirs`` 会返回空）：容器与 CI 里通常没有这一层，不该有任何影响。
    """
    return Path.home().joinpath(*AGENTS_SKILLS_SUBPATH)


def _clip(text: str, limit: int) -> str:
    """截断并留一个省略号（目录是"看一眼就够"的地方，不在这里做花活）。"""
    return text if len(text) <= limit else text[:limit] + "…"


def _relative_skill_path(directory: Path, root: Path) -> str:
    """``SKILL.md`` 相对**发现根**的位置（``<组>/<名字>/SKILL.md``）。数不出来就退回文件名。

    用相对路径而不是绝对路径写进提示词：绝对路径带用户名与机器的目录布局，
    对模型判断"这技能是什么"没有帮助，却会把它带进每一次请求。
    """
    try:
        return (directory.relative_to(root) / SKILL_FILE).as_posix()
    except ValueError:  # 理论上不会：directory 就是从 root 里走出来的
        return SKILL_FILE


def _drop_reason(*, name: str, description: str) -> str:
    """frontmatter 校验（**前置**）：返回非空 = 这个技能被丢弃、返回值就是理由。

    抄 ZCode 的两级失败模型（``zcode-guide`` 的 ``diagnosing-skills`` §2）：
    **缺 ``name``、缺 ``description``、``description`` 超过 1024 字符 → 整个技能不加载**。
    为什么是丢弃而不是"标一下照旧进目录"：目录里那一行是模型判断「何时该用」的
    唯一依据，缺字段的那一行要么是空白、要么没有触发条件——它进了提示词也只是
    占位。校验放**这里**（扫描磁盘时）而不是放在拼提示词时，是因为"坏技能"这件事
    要能出现在能力页上，而丢弃是加载阶段就能定的事。

    丢弃**不等于静默**：记录照旧进 ``list()``，理由就在 ``flagged`` 里。
    """
    if not name:
        return (
            "已丢弃：SKILL.md 的 frontmatter 里没有 name"
            "（照 ZCode 的规则：缺 name 的技能不加载）——请补一行 `name: 技能名`"
        )
    if not description:
        return (
            "已丢弃：SKILL.md 的 frontmatter 里没有 description"
            "（照 ZCode 的规则：缺 description 的技能不加载）——"
            "目录里那一行是模型判断「何时该用」的唯一依据，没有它技能永远不会被触发"
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        return (
            f"已丢弃：description 有 {len(description)} 字，超过上限 {MAX_DESCRIPTION_CHARS}"
            "（照 ZCode 的规则：超长描述整个技能不加载）——"
            "把触发条件压到前面，细节挪进正文"
        )
    return ""


def _pick_catalog(usable: list[SkillRecord]) -> list[SkillRecord]:
    """目录里露哪几条：**常驻固定 → 其余按名字**，最后截到 `MAX_CATALOG`。

    为什么不能"按扫描顺序取前 60"（2026-09-29 的现场）：这个库有几千条技能，
    扫描顺序取决于目录遍历与来源层，于是"哪 60 条被模型看见"其实是个**偶然**——
    产品自带的 5 条与那四个格式件都可能不在里面，而它们恰恰是最该常驻的。

    三条性质都是刻意要的：

    - **常驻永远在**：`RESIDENT_SKILLS` 里的名字只要库里真有就一定排在最前（库再大也一样）；
    - **可复现**：同样的库给同样的 60 条（其余按名字排序，不用随机/哈希）。
      库变了只影响尾部，不重排前面那些；
    - **看不见的那些是可发现的**：目录尾部会报"另有 N 个"，用 `list_skills` 按页看
      （那条提示在 `catalog()` 里拼，不在这里）。

    **v0.61 起这里不再有"策展"那一档**：领域件（办公那几套）退到 `featured_by_category`
    那条界面口径上。目录只认两件事——常驻名单 + 按名字兜底，
    于是"谁进了提示词"这件事只有一处判据，也不再随一份清单的长短浮动。
    """
    by_name: dict[str, SkillRecord] = {}
    for record in usable:
        by_name.setdefault(record.name, record)
    picked: list[SkillRecord] = []
    seen: set[str] = set()
    for name in RESIDENT_SKILLS:
        record = by_name.get(name)
        if record is not None and name not in seen:
            picked.append(record)
            seen.add(name)
    rest = sorted(
        (record for name, record in by_name.items() if name not in seen),
        key=lambda item: item.name,
    )
    return [*picked, *rest][:MAX_CATALOG]


def _featured_rank(
    record: SkillRecord, installed: set[str]
) -> tuple[int, int, int, int, int, int, int, str]:
    """精选排序键（**升序，越小越靠前**）：四档判据 + 一个兜底，全部机械可复现。

    1. **产品自带优先**（`source == "builtin"`）：我们自己的那份是审过的；
    2. **装进来的优先**（名字在安装清单里）：用户/市场**主动装**过一次的，
       比"随初始技能集一起躺在那儿的"更能说明有人要它；
    3. **描述完整度**：写了 `when_to_use` > 有中文简介（`summary`，界面那一行就靠它）>
       描述更长（截到目录口径 `CATALOG_DESCRIPTION_CHARS`，再长的不加分——
       目录里那一行只到 250 字，多出来的部分是正文该待的地方）；
    4. **名字可读性**：连字符少、更短的名字更像人起的（`pdf-pro` 优于
       `image-to-editable-ppt-tool-2024`），同分时按名字升序兜底。

    不用随机、不按时间、不看模型：**同样的库给同样的两条**，这样界面上那一屏
    下一次刷新不会换人，用例也能钉住它。
    """
    return (
        0 if record.source == "builtin" else 1,
        0 if record.slug in installed else 1,
        0 if record.when_to_use else 1,
        0 if record.summary else 1,
        -min(len(record.description), CATALOG_DESCRIPTION_CHARS),
        record.name.count("-"),
        len(record.name),
        record.name,
    )


def featured_by_category(
    records: Sequence[SkillRecord],
    *,
    installed: Sequence[str] = (),
    per_category: int = FEATURED_PER_CATEGORY,
) -> dict[str, list[SkillRecord]]:
    """**每个分类的前 N 条**（默认 2）——界面默认展示用的那一批（v0.61）。

    与 `_pick_catalog` 的分工：那个回答"模型这轮看到什么"（进提示词、有预算），
    这个回答"界面默认摆什么"（**不进提示词**，条数由 `per_category` 定）。
    两条口径共用同一份分类规则，但**互不影响**：改这里不会动 prompt 的大小。

    三条纪律：

    - **只从能用的里挑**：`used_by_prompt` 为假（被安全扫描拦下 / 依赖没满足 /
      被用户关掉）与被丢弃的一律不参选——"精选"推一条点不开的东西没有意义；
    - **顺序 = `CATEGORIES` 的顺序**：与页面分组顺序同一份来源，两处不会漂；
    - **分类是记录自带的**（`SkillRecord.category`，扫描时算好的），
      所以这条派生是纯排序，不在这里跑正则——列表端点每次都会调它。

    `installed` 是"装进来的"那批名字（市场/上传安装清单），不传就只有前三档判据。
    返回的字典**只含真有料的分类**（空分类不占位；某类不足 2 条就有几条给几条）。
    """
    marks = {normalize_name(name) for name in installed}
    buckets: dict[str, list[SkillRecord]] = {}
    for record in records:
        if record.discarded or not record.used_by_prompt or not record.category:
            continue
        buckets.setdefault(record.category, []).append(record)
    out: dict[str, list[SkillRecord]] = {}
    for item in CATEGORIES:  # 只认登记过的类别，顺序即页面分组顺序
        bucket = buckets.get(item.slug)
        if not bucket:
            continue
        bucket.sort(key=lambda record: _featured_rank(record, marks))
        out[item.slug] = bucket[:per_category]
    return out


def _catalog_line(item: SkillRecord) -> str:
    """目录里的一行。格式照 ZCode（调研 §2.3）：
    ``- {name}: {description - when_use} (file: {path})``，把"何时用"标了出来。

    **描述与"何时用"都先压平空白**（D16 P0）：第三方技能里有把换行写在 description 里的
    （实测：60 条技能渲染出 62 行）——多出来的那半行会被模型当成一个**技能名**去 read，
    而它根本不存在。一行就是一行。
    """
    description = " ".join(item.description.split())
    line = f"- {item.name}: {_clip(description, CATALOG_DESCRIPTION_CHARS)}"
    if item.when_to_use:
        when = " ".join(item.when_to_use.split())
        line += f"（何时用：{_clip(when, CATALOG_DESCRIPTION_CHARS)}）"
    elif item.relative_path:
        line += " "  # 没有"何时用"时要留一个空格，别和后面的路径粘在一起
    if item.relative_path:
        line += f"(file: {item.relative_path})"
    return line


# 文本卫生那一族（合并代理对 / 判非法 / 清洗）在 `app/core/text_hygiene.py`：
# 请求组装处（`services/llm.py`）也要用同一套判断，而 llm 与 skills 互相 import 会成环
# （`llm → skills → runtime_config → llm`，真踩过）→ 放到 core 那一层，一处实现两处用。
from app.core.text_hygiene import (  # noqa: E402  （放在这里是为了贴着用它的那几段）
    recombine_surrogates,
    text_problem,
)


class SkillService:
    """扫描并读取技能。**无状态**：每次调用重新扫磁盘。

    为什么不缓存：技能是文件，用户可能刚刚往目录里丢了一个（这正是它比"装插件"
    轻的地方）。扫一次是几十毫秒的重活里最轻的那种，而缓存要处理失效——
    为省这点开销引入一套失效逻辑不划算。真到几百个技能时再加 mtime 缓存。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        builtin_dir: Path | None = None,
        agents_dir: Path | None = None,
        config_value: Callable[[str], str] | None = None,
        config_set: Callable[[dict[str, str]], None] | None = None,
        binaries: Callable[[str], str | None] | None = None,
        scan_ttl_seconds: float = SCAN_TTL_SECONDS,
    ) -> None:
        #: 读一个运行期配置的值（``requires.config`` 用它判定）。
        #: **不给就是"读不到"**，于是带 requires 的技能不出现——那一侧的默认必须是
        #: "不宣称自己能跑"，反过来的话，一个没接配置的部署会把技能摆在目录里，
        #: 模型照它做然后失败在最后一步。
        self._config_value = config_value
        #: 写一个运行期配置的值（D23：单条技能的启停存在这里）。
        #: **不给就是"关不掉"**：`set_enabled` 会如实抛错，而不是假装写成功。
        #: 与 `config_value` 一样只注入 callable —— 技能服务不必认识设置服务本身。
        self._config_set = config_set
        self._binaries = binaries or shutil.which
        #: 整次扫描结果的复用窗口（见 `SCAN_TTL_SECONDS`）：窗口内直接交回上一条列表。
        #: 用例与"重新扫描"传 0 / 调 `invalidate()`，那时每次调用都会逐个 stat 校验。
        self._scan_ttl = max(0.0, float(scan_ttl_seconds))
        self._scan_memo: tuple[float, list[SkillRecord]] | None = None
        self._data_dir = data_dir
        # 仓库自带的技能目录（三种来源见模块头）：显式注入 > KYLAB_SKILLS_DIR >
        # 按代码位置推。显式注入排最前是为了测试与工具能指到临时目录——
        # 断言的对象不该被"机器上恰好设了环境变量"改掉。
        self._builtin_dir = builtin_dir or _builtin_dir_from_env() or _repo_skills_dir()
        # 跨工具共享的那一层（P0-3）：默认 ``~/.agents/skills``，可注入——
        # **用例不该去读真实的家目录**，那是"机器上恰好装了什么"就跟着变的断言。
        self._agents_dir = agents_dir or _agents_skills_dir()
        #: **技能目录清单的缓存**（`root → 签名 + 目录列表`）。
        #:
        #: 为什么必须缓存（2026-09-29 性能事故，用户可见）：这个库涨到 6,200+ 条之后，
        #: `GET /api/v1/skills` 要 **18.6 秒**，而**每一轮对话的目录注入走的是同一条路**。
        #: 6,000 次"read + 解析 frontmatter + 安全扫描 + 丢弃判据"就是那个 18 秒；
        #: 目录树没变时没有理由再走一遍。
        #:
        #: **签名只花两次 stat**（根目录 + 每个"一层分组的子目录"）：目录项增删都会改
        #: 父目录的 mtime（NTFS/ext4 都是），所以"技能装上/删掉"照样能被发现 ✓；
        #: 而"某个 SKILL.md 改了内容"不改任何目录的 mtime —— 那条由下一层的
        #: 单条记录缓存（按文件 mtime+size）负责 ✓。两层各管一件事，谁也不越界。
        self._dirs_cache: dict[Path, tuple[object, list[Path]]] = {}
        #: **单条技能处理链的缓存**（`SKILL.md 路径 → (文件 mtime+size, 记录, frontmatter)`）。
        #:
        #: 进缓存的**是整条链**：读文件、解析 frontmatter、安全扫描（`_scan`）、丢弃判据、
        #: 以及那一份 `meta`。唯一不进缓存的是 `requires` 的判定（`_unmet`）——
        #: 它看的是**运行期**（配置值、机器上有没有那个可执行文件），与文件一个字的关系都没有，
        #: 而缓存键里没有它们；所以每次调用都重判一遍（代价是几次字典查找）。
        #: 宁可多算这一下，也不要"用户刚配好 key，技能却还显示缺依赖"。
        self._record_cache: dict[Path, tuple[tuple[int, int], SkillRecord, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ 读

    def _roots(self) -> tuple[tuple[str, Path], ...]:
        """发现顺序（先出现的**遮蔽**后出现的，同名只留一份）。见模块头。

        裁成三层：仓库自带 → 数据目录（KYLAB 自己的用户池）→ ``~/.agents/skills``
        （跨工具共享池）。ZCode 是"工具自己的用户目录先于 ``~/.agents``"
        （``diagnosing-skills`` §1 的第 2、3 层），这里照同一条规则。
        没有"工作区"与"插件"这两档：技能是部署级能力，workspace 那档的设计见 §6.1。
        """
        return (
            ("builtin", self._builtin_dir),
            ("user", self._data_dir / "skills"),
            ("agents", self._agents_dir),
        )

    def list(self) -> list[SkillRecord]:
        """全部技能（含被拦下与被丢弃的）。顺序：仓库自带在前，然后按名字。"""
        records = self._scan()
        disabled = self._disabled()
        if not disabled:
            return list(records)
        # **用户关掉的 = 不进提示词**（D23）：折进 `used_by_prompt` 这个**已有**判据。
        # 被丢弃的技能不动——它本来就不进，而且"没通过校验"比"你关掉了"更该先说。
        return [
            replace(record, used_by_prompt=False, flagged=(*record.flagged, DISABLED_REASON))
            if record.slug in disabled and not record.discarded
            else record
            for record in records
        ]

    def invalidate(self) -> None:
        """把三层缓存全丢掉（"重新扫描"、用例、以及任何要立刻看到磁盘现状的地方）。

        目录清单与单条记录两层本身都有签名校验（改了就会被发现），清掉它们只是为了让
        "现在立刻重扫"这件事有一个明确入口。**不主动调用也没关系**：窗口一过就重扫。
        """
        self._dirs_cache.clear()
        self._record_cache.clear()
        self._scan_memo = None

    def _scan(self) -> list[SkillRecord]:
        """扫一遍三个来源（**带复用窗口**，见 `SCAN_TTL_SECONDS`）。

        窗口的key是**两个**东西：**各个根目录的 mtime 签名** + 时间。
        少了前者就会出现真回归（2026-09-29 用例抓住的）：市场那边刚把一个技能写进
        ``data/skills/``，同一个进程里的 `list()` 却因为"30 秒还没到"仍然交回旧列表，
        于是那条技能被判成 ``installed=False`` ✗。**目录项增删是廉价的**（每个根一次
        `stat`），所以它必须**立刻**生效；只有"某个 `SKILL.md` 的内容改了"这种
        不改任何目录 mtime 的变化，才交给窗口（与 `invalidate()`）。
        """
        now = time.monotonic()
        signature = self._roots_signature()
        if (
            self._scan_memo is not None
            and self._scan_memo[0] == signature
            and now - self._scan_memo[1] < self._scan_ttl
        ):
            return self._scan_memo[2]
        found: dict[str, SkillRecord] = {}
        for source, root in self._roots():
            for directory in self._skill_dirs(root):
                record = self._load(directory, source=source, root=root)
                if record is None:
                    continue
                current = found.get(record.slug)
                # 先出现的优先：随代码发布的那份是"我们调过的版本"。
                # 唯一例外：**被丢弃的不遮蔽能用的**同名技能——一个写坏的
                # ``data/skills/x`` 不该让 ``~/.agents/skills`` 里那份好用的 x 消失。
                if current is None or (current.discarded and not record.discarded):
                    found[record.slug] = record
        records = sorted(found.values(), key=lambda item: (item.source != "builtin", item.name))
        self._scan_memo = (signature, now, records)
        return records

    def _roots_signature(self) -> tuple[tuple[str, int], ...]:
        """各个根的 mtime 签名（**每个根一次 `stat`**）。

        目录项增删会改父目录的 mtime，所以这一层能"立刻"发现技能装上/删掉；
        而它便宜到可以每次调用都算（三个根三次 stat）。
        """
        out: list[tuple[str, int]] = []
        for source, root in self._roots():
            try:
                out.append((source, root.stat().st_mtime_ns))
            except OSError:
                out.append((source, 0))
        return tuple(out)

    def _disabled(self) -> set[str]:
        """用户关掉了哪些技能（D23）。**读不到就是空集**：没接配置的部署照旧全开。"""
        if self._config_value is None:
            return set()
        raw = self._config_value(DISABLED_SKILLS_KEY) or ""
        return {item.strip() for item in raw.split(",") if item.strip()}

    def is_enabled(self, name: str) -> bool:
        """这条技能没被用户关掉。"""
        return normalize_name(name) not in self._disabled()

    def set_enabled(self, name: str, enabled: bool) -> SkillRecord:
        """开/关一条技能（D23）。

        **关掉 = 不进提示词**（`used_by_prompt=False`），磁盘上的文件一个字都不动：
        技能是磁盘上的东西，"关"这件事的语义只是"这一轮不给模型看"。
        `used_by_prompt` 已经是提示词、`list_skills` 工具、能力页三处共用的判据，
        所以折进它一处，三处自动一致。
        """
        record = self.get(name)
        if self._config_set is None:
            raise RuntimeError("这个部署没有接设置服务，技能启停改不了")
        disabled = self._disabled()
        if enabled:
            disabled.discard(record.slug)
        else:
            disabled.add(record.slug)
        self._config_set({DISABLED_SKILLS_KEY: ",".join(sorted(disabled))})
        return self.get(name)

    def get(self, name: str) -> SkillRecord:
        wanted = normalize_name(name)
        for record in self.list():
            if record.slug == wanted:
                return record
        raise NotFoundError(f"没有这个技能：{name}")

    def read(
        self, name: str, *, allow_discarded: bool = False, translate_names: bool = True
    ) -> tuple[SkillRecord, str]:
        """取技能正文（按需展开那一步）。

        **被丢弃的技能读不出来**（``allow_discarded=True`` 只给人在界面上核对用，
        见 ``api/v1/skills.py`` 的详情端点）：丢弃的含义就是"这个技能不算数"，
        而模型手里的入口是 ``read_skill`` 工具——它必须也拿不到正文。

        ``translate_names``：默认把正文里的 Claude Code 工具名换成我们的
        （第三方技能全按那套名字写，见 ``services/skill_tools.py``）。
        **界面那一侧传 False**：人要对着上游原文核对时，看到的该是原文。
        """
        record = self.get(name)
        if record.discarded and not allow_discarded:
            reason = record.flagged[0] if record.flagged else "没有通过校验"
            raise NotFoundError(f"技能「{record.name}」已被丢弃：{reason}")
        text = (Path(record.directory) / SKILL_FILE).read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        # 读这一侧再兜一道（扫描与读取是两条路：技能可能是**装进来之后**才被写坏的）：
        # 先合并转义写坏的代理对，合并后仍非法就按"丢弃"拒绝——**绝不把非法字符
        # 送进提示词**（那会让整句对话编码失败，见 `text_problem`）。
        clean = recombine_surrogates(body.strip())
        problem = text_problem(clean)
        if problem:
            raise NotFoundError(f"技能「{record.name}」已被丢弃：{problem}")
        if translate_names:
            clean = translate_tool_names(clean)
        return record, clean

    def catalog(self) -> str:
        """拼成注入 system prompt 的**目录**；没有可用技能时是空串。

        **每个请求都注入**（P0-3）：以前模型得先调 ``list_skills`` 才知道有什么技能，
        而它经常不调——那些技能于是等于不存在。三家（ZCode / DSH / QwenPaw）都是
        每个请求给一份目录、正文按需取（调研 §2.3），这里照同一套。

        每行 ``- <name>: <描述（截断 250 字）>（何时用：<when_to_use>）(file: <相对路径>)``；
        整段有 ``CATALOG_BUDGET_CHARS`` 的字符预算，超了后面的条目不写。

        最后一句话必须点名 ``read_skill``：少了它，模型会凭那一行描述猜内容然后
        直接答——那正是技能最容易被用错的方式（它以为知道流程，其实细节在正文里）。
        而**正文一个字都不在这里**（这是"装很多技能也不贵"的原因，也是文档双分段的全部）。
        """
        usable = [item for item in self.list() if item.used_by_prompt]
        if not usable:
            return ""
        header = (
            "【可用技能】下面这些是本环境里可用的技能"
            "（名字：什么时候用；末尾是它的 SKILL.md 位置）。"
            "需要按某个技能的流程做事时，**先用 `read_skill` 把它的正文读出来**，"
            "不要只凭这一行描述就动手——细节在正文里。"
        )
        # 第三方技能全按 Claude Code 的工具名写（`Read`/`Bash`/`Skill`…）：
        # 这张对照表与目录一起给，模型调用时才会用我们的名字（见 services/skill_tools.py）。
        note = catalog_note()
        lines: list[str] = []
        used = len(header) + len(note) + 1
        for item in _pick_catalog(usable):
            line = _catalog_line(item)
            # 预算按字符算（与 ZCode 的常数同一口径）。超预算就**从这里截断**，
            # 不写半行、也不挤掉别的来源——目录是可选的增强，对话本身才是必需的。
            if used + len(line) + 1 > CATALOG_BUDGET_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        hidden = len(usable) - len(lines)
        tail = (
            ""
            if hidden <= 0
            else (
                f"\n（另有 {hidden} 个技能没在这里列出：用 `list_skills` 按页查看，"
                "或者用 `find_tools` 找具体能力。）"
            )
        )
        return f"{header}\n{note}\n" + "\n".join(lines) + tail

    # ------------------------------------------------------------------ 内部

    def _skill_dirs(self, root: Path) -> list[Path]:
        """找 ``<root>/<name>/SKILL.md`` 与 ``<root>/<group>/<name>/SKILL.md``。

        **只走两层**：再深就是"把别人的仓库整个拷进来"，那会让扫描变成遍历。
        跳过隐藏目录与 ``node_modules``／``__pycache__`` 这类明显的非技能目录。

        **按目录签名缓存**（见 `_dirs_cache` 的说明）：根目录 mtime + 每个"一层分组"
        的 mtime 都没变 ⇒ 目录项集合没变 ⇒ 直接交回上次那份列表。热路径因此只有
        **两次 stat**（根 + 分组），与库有多大无关。
        """
        out: list[Path] = []
        if not root.is_dir():
            return out
        try:
            root_mtime = root.stat().st_mtime_ns
        except OSError:  # pragma: no cover - 目录正好在这一刻被删掉
            return out
        cached = self._dirs_cache.get(root)
        if cached is not None:
            signature, dirs = cached
            if signature[0] == root_mtime and self._groups_unchanged(signature[1]):
                return dirs
        groups: list[tuple[Path, int]] = []
        for first in sorted(root.iterdir()):
            if not first.is_dir() or first.name.startswith((".", "_")):
                continue
            if (first / SKILL_FILE).is_file():
                out.append(first)
                continue
            try:
                # 一层分组：它自己也要进签名（往它里面加/删技能只改它的 mtime）
                groups.append((first, first.stat().st_mtime_ns))
            except OSError:  # pragma: no cover - 分组正好在这一刻被删掉
                continue
            for second in sorted(first.iterdir()):
                if not second.is_dir() or second.name.startswith((".", "_")):
                    continue
                if (second / SKILL_FILE).is_file():
                    out.append(second)
        self._dirs_cache[root] = ((root_mtime, tuple(groups)), out)
        return out

    @staticmethod
    def _groups_unchanged(groups: Sequence[tuple[Path, int]]) -> bool:
        """那几个"一层分组"的 mtime 还在不在原处（往分组里加技能就靠它发现）。"""
        for path, mtime in groups:
            try:
                if path.stat().st_mtime_ns != mtime:
                    return False
            except OSError:
                return False
        return True

    def _load(self, directory: Path, *, source: str, root: Path) -> SkillRecord | None:
        """读一个技能目录。**坏文件不抛错**：一个技能写坏了不该让整个列表 500。

        捕获里必须带上 ``UnicodeDecodeError``——它是 ``ValueError`` 的子类，
        **不是 ``OSError``**，只写 ``except OSError`` 会被它直接穿透（真踩过：
        一个非法 UTF-8 的 SKILL.md 让整个技能列表 500）。同一个坑在
        ``memory_files._entry_of`` 里也踩过一次，处置不同是有意的：
        那边是"替换成 U+FFFD 也要把文件打开"（用户是进去修它的），
        这里是"**跳过**"——技能描述会被注入提示词，带着乱码的描述比没有描述更糟。

        **frontmatter 校验在这里（前置）**：不合规的技能不返回"可用"记录，而是返回一条
        ``discarded=True`` 的记录（理由在 ``flagged`` 里）。这样"丢弃"与"为什么"
        是同一次扫描的产物，能力页与提示词两条路看到的是同一份判断。

        **整条链按 `SKILL.md` 的 mtime + size 缓存**（见 `_record_cache`）：
        命中时只重判一次 `requires`（它看运行期，与文件无关）。文件改一个字，
        mtime/size 就变 ⇒ 自动重读重解析 ⇒ 不存在"缓存住旧描述"这回事。
        """
        path = directory / SKILL_FILE
        try:
            stat = path.stat()
        except OSError:
            # 文件被删了/读不到：连缓存一起丢掉，别留一条指向不存在文件的记录
            self._record_cache.pop(path, None)
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._record_cache.get(path)
        if cached is not None and cached[0] == key:
            parsed, meta = cached[1], cached[2]
        else:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                logger.warning("读技能失败（跳过）：%s", path, exc_info=True)
                self._record_cache.pop(path, None)
                return None
            parsed, meta = self._parse(
                text, directory=directory, source=source, root=root, path=path
            )
            if parsed is not None:
                self._record_cache[path] = (key, parsed, meta)
        if parsed is None:
            return None
        if parsed.discarded:
            # 被丢弃的不再往下判 requires：要修的是 frontmatter（同下面那段说明）
            return parsed
        # `requires` 每次都重判：它看的是运行期（配置值 / 机器上有没有那个可执行文件）
        unmet = self._unmet(meta)
        if not unmet:
            return parsed
        return replace(parsed, used_by_prompt=False, flagged=(*parsed.flagged, *unmet))

    def _parse(
        self, text: str, *, directory: Path, source: str, root: Path, path: Path
    ) -> tuple[SkillRecord | None, dict[str, Any]]:
        """把一份 `SKILL.md` 解析成记录（**纯函数那一段**，可缓存的部分都在这里）。

        与运行期无关：只看这份文本与它所在的位置。`requires` 的判定**不在这里**
        （那是 `_unmet`，每次调用重判，理由见 `_load`）。
        """
        meta, body = parse_frontmatter(text)
        # 上游常把 emoji 写成**字面转义**（`"\ud83e\udd16"`）：YAML 解出来是两个孤立代理项。
        # 提示词里带上它，httpx 一编码就 `UnicodeEncodeError: surrogates not allowed` ——
        # **整句对话全废**（D16 P0，2026-09-29 两条真会话实测）。所以这里两道：
        # ① 先把成对的代理项合并回正常码位（那是"转义写坏了"，不是存心放非法字符）；
        # ② 合并后仍非法的（真·孤立代理项、控制字符、名字里有换行）→ 走既有的"丢弃 + 理由"。
        name = recombine_surrogates(str(meta.get("name") or "").strip())
        description = recombine_surrogates(str(meta.get("description") or "").strip())
        # 认 name / description / when_to_use / license / metadata 五个键（ZCode 同一批）
        # 加上我们自己扩展的 ``summary``；其余键一律忽略，不当错误——事实标准是"多写的不算错"。
        when_to_use = recombine_surrogates(str(meta.get("when_to_use") or "").strip())
        # summary 是**给人看的中文简介**（见 SkillRecord）：市场装的技能那份存在安装清单里，
        # 仓库自带的没有安装那一步，就写在 frontmatter 里——两条路的数据形状一样
        summary = recombine_surrogates(str(meta.get("summary") or "").strip())
        common = {
            # 缺 name 时退回目录名**只为了界面上指认得出来**（否则是一行空白），
            # 它照样是被丢弃的，不会进目录、也读不出正文。
            "name": name or directory.name,
            "description": description,
            "path": str(path),
            "source": source,
            "directory": str(directory),
            "when_to_use": when_to_use,
            "summary": summary,
            "relative_path": _relative_skill_path(directory, root),
            # 分类在这里算（而不是在接口里每次现算）：它只依赖这份文本，
            # 于是**跟着记录缓存走**——16k 条的库上一次全量分类要十几秒，
            # 放在请求路径上是灾难；放在扫描里则只在文件真的变了才重算。
            "category": category_of(
                normalize_name(name or directory.name), name or directory.name, description
            ),
        }
        dropped = _drop_reason(name=name, description=description)
        if not dropped:
            # 过了 frontmatter 那道之后再看"这段字能不能进提示词"：
            # name 是**单行**的（带换行会把目录多撑出一行，模型会把后半行当成另一个技能），
            # 其余三处与正文只要求没有非法字符。
            dropped = (
                text_problem(name, single_line=True)
                or text_problem(description)
                or text_problem(when_to_use)
                or text_problem(summary)
                or text_problem(body)
            )
        if dropped:
            # 丢弃的就不再往下判扫描与 requires：要修的是 frontmatter，
            # 一次给一条能动手的理由比堆四条更有用（它们都不进目录，没有风险差别）。
            logger.info("技能被丢弃（%s）：%s", directory.name, dropped)
            record = SkillRecord(
                **common, used_by_prompt=False, flagged=(dropped,), discarded=True
            )
            return record, meta
        # **只算"与文件有关"的那一半**：安全扫描。`requires` 由 `_load` 每次重判
        # （它看运行期），所以这里不能把它折进 `used_by_prompt` —— 那会把一个
        # "今天缺依赖、配好就该能用的技能"永久缓存成不可用。
        flagged = _scan(text)
        record = SkillRecord(
            **common,
            # 被安全扫描拦下的技能**不进目录但在列表里**：
            # 静默藏掉会让用户以为技能装失败了（见模块头的安全那一段）。
            used_by_prompt=not flagged,
            flagged=tuple(flagged),
        )
        return record, meta



    def _unmet(self, meta: dict[str, Any]) -> list[str]:
        """``requires`` 里**没满足**的那几项，回人话理由。

        为什么用具名键而不是一句自由文本：这些是**可以自动判的客观条件**
        （配置有没有值、命令在不在 PATH 上、平台对不对），而自由文本只能靠人读。
        调研 §4.2 抄的就是这个做法——预装 ≠ 默认开启，没装依赖的用户
        **根本看不到**那个技能，而不是看到一个点了就报错的技能。

        处置与"疑似注入"完全一致（那条路也是 ``used_by_prompt=False`` + 理由）：
        不进模型目录，但在界面里如实列出**并说明差什么**——静默藏掉会让用户
        以为技能装失败了。
        """
        raw = meta.get("requires")
        if not isinstance(raw, dict):
            return []
        reasons: list[str] = []
        for key in REQUIRE_KEYS:
            if key not in raw:
                continue
            if key == "config":
                reasons.extend(self._unmet_config(raw["config"]))
            elif key == "binaries":
                reasons.extend(_unmet_binaries(raw["binaries"], self._binaries))
            elif key == "env":
                reasons.extend(_unmet_env(raw["env"]))
            elif key == "os":
                reasons.extend(_unmet_os(raw["os"]))
        unknown = [key for key in raw if key not in REQUIRE_KEYS]
        if unknown:
            # 拼错的键（`require:` / `bins:`）**必须报出来**：不报的话，
            # 那个条件等于没写，而写它的人以为已经门控住了
            reasons.append(
                f"requires 里有不认识的键：{'、'.join(sorted(unknown))}"
                f"（只有 {'、'.join(REQUIRE_KEYS)}）"
            )
        return reasons

    def _unmet_config(self, raw: Any) -> list[str]:
        keys = [str(item) for item in _as_list(raw)]
        missing = [
            key for key in keys if not (self._config_value or (lambda _key: ""))(key).strip()
        ]
        if not missing:
            return []
        where = "、".join(_config_label(key) for key in missing)
        return [f"需要先配置：{where}（这个技能要用的能力还没接上）"]


def _as_list(raw: Any) -> list[Any]:
    """``config: web.search_api_key`` 与 ``config: [a, b]`` 都认。

    两种写法都收是有意的：只写一项时不必为其套一层列表（写技能的人会那么写），
    而写成自由字符串又必须能按**单个键**解析——按字符拆会把键名拆碎。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if str(item).strip()]
    text = str(raw).strip()
    return [text] if text else []


def _config_label(key: str) -> str:
    """配置键 → 界面上的说法（``「设置 → 联网」里的「搜索 API 密钥」``）。

    不写键名给用户看：``web.search_api_key`` 是代码里的东西，
    而他要找的是界面上那个输入框。找不到就退回键名——**宁可难看也别编**。
    """
    for group in SETTING_GROUPS.values():
        for field in group.get("fields", []):
            if field.get("key") == key:
                return f"「设置 → {group.get('label', '')}」里的「{field.get('label', key)}」"
    return key


def _unmet_binaries(raw: Any, which: Callable[[str], str | None]) -> list[str]:
    missing = [str(name) for name in _as_list(raw) if not which(str(name))]
    if not missing:
        return []
    return [f"需要这台机器上装了命令：{'、'.join(missing)}（没找到就说明这个技能跑不起来）"]


def _unmet_env(raw: Any) -> list[str]:
    import os

    missing = [str(name) for name in _as_list(raw) if not os.environ.get(str(name))]
    if not missing:
        return []
    return [f"需要环境变量：{'、'.join(missing)}"]


def _unmet_os(raw: Any) -> list[str]:
    wanted = {str(item).strip().lower() for item in _as_list(raw)}
    if not wanted:
        return []
    current = _PLATFORMS.get(sys.platform, sys.platform)
    if current in wanted:
        return []
    return [f"只在 {'、'.join(sorted(wanted))} 上用（当前是 {current}）"]


def _scan(text: str) -> list[str]:
    """安全扫描：返回人话理由（可能多条）。见模块头对"为什么只是标出来"的说明。"""
    reasons: list[str] = []
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            reasons.append(f"疑似提示注入（{label}）：这条技能不会进模型的技能目录")
    for label, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            reasons.append(f"{label}：技能里不该存凭据，请改成从设置或环境变量取")
    return reasons
