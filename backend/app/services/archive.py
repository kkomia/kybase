"""记忆**档案**的服务层（期一，见 ``docs/设计/记忆档案-设计-v0.1.md`` §3.3、§3.4、§4.3、§4.4）。

``archive_files.py`` 管"文件长什么样"，这一层管"写不写得进去、顶替谁、留下什么痕迹"。

四块，各自对应文档的一节：

1. **预算**（§3.3）：单条 120 字、全档 60 条 / 4000 字、每区软约束
   （身份 10 / 偏好 20 / 项目 8 组 × 每组 8 条 / 工具 12）。超限的行为是设计的一部分：
   **拒绝追加**（档案逐字节不变）、**允许净额不为正的顶替**、回执给两条出路；
2. **机械判据**（§4.3）：完全一致 → 不写；包含且数字集合相同、差值 ≤ 6 字 → 顶替；
   字符二元组重合 ≥ 0.7 且数字集合相同 → 顶替；**数字不同一律不顶替**；其余新增。
   那套阈值沿用旧记忆去重的口径，但**动作从"跳过"改成"顶替"**——
   旧口径判错的代价是丢掉一条真事实，现在的代价是变更流里多一条 + 界面上一次点击
   （§3.4 那段代价分析）。跨分区也查：同一句话不能同时住在两个分区；
3. **变更流**（§3.4）：``changes.md`` 追加记录，旧值/新值各占一行、逐字节保存，
   保留最近 300 条；``忘掉`` 与 ``顶替`` 都可**还原**；
4. **回执**（§4.4）：新增 / 顶替 / 已存在 / 被拒四种文案只有一个来源——模型从工具听到的
   与人在界面上看到的必须是同一种说法。

**变更流还是 recall 的唯一池子**（§5.3）：档案本身已经全量注入，再召回一次就是把同一段
内容进两次上下文；于是 `recall` 的池子只剩 `changes.md`，它回答的是"这条以前是什么、
什么时候改的"。排序用**纯字面判据**（归一化子串命中数 + 二元组覆盖率，不依赖分词，
见 :func:`search_changes`）。

**独立的一层**：档案服务不 import ``memory.py``（那边是它的门面），也就没有循环。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.exceptions import InvalidRequestError
from app.services.archive_files import (
    ACTION_ADDED,
    ACTION_FORGOTTEN,
    ACTION_REPLACED,
    ACTION_RESTORED,
    ACTION_REVERTED,
    CHANGES_FILENAME,
    KNOWN_SECTIONS,
    SECTION_IDENTITY,
    SECTION_PREFERENCES,
    SECTION_PROJECTS,
    SECTION_TOOLS,
    Archive,
    ArchiveEntry,
    ChangeRecord,
    append_changes,
    digit_runs,
    fingerprint,
    normalize,
    normalize_entry,
    parse_changes,
    read_archive,
    read_changes,
    read_text,
    render_change,
    write_archive,
)

__all__ = [
    "CHANGELOG_KEEP",
    "INJECTION_LIMIT_CHARS",
    "PROJECT_GROUP_ENTRIES",
    "PROJECT_GROUP_LIMIT",
    "RECEIPT_ADDED",
    "RECEIPT_EXISTING",
    "RECEIPT_FORGOTTEN",
    "RECEIPT_MISSING",
    "RECEIPT_REJECTED",
    "RECEIPT_REPLACED",
    "RECEIPT_RESTORED",
    "RECEIPT_SENSITIVE",
    "RECEIPT_TOO_LONG",
    "RECEIPT_UNKNOWN_SECTION",
    "SECTION_CHARS",
    "SECTION_ENTRY_LIMITS",
    "SINGLE_ENTRY_CHARS",
    "SOURCE_EXPLICIT",
    "SOURCE_IMPLICIT",
    "SOURCE_MIGRATION",
    "SOURCE_UI",
    "TOTAL_CHARS",
    "TOTAL_ENTRIES",
    "ArchiveService",
    "BudgetReport",
    "ChangeHit",
    "Match",
    "Overrun",
    "SectionBudget",
    "WriteResult",
    "budget_of",
    "classify_action",
    "display_counts",
    "is_sensitive",
    "overrun",
    "search_changes",
]

# --------------------------------------------------------------------- 预算
#
# 数值全部取设计文档 §3.3 的【建议】值，并按 §10 第 1 条"先按建议落地、拍板改数"，
# 所以它们只在这里写一次——预算写在文档里，也写在界面读数的同一份来源里（§5.2）。

#: 单条字符上限。一句话说清不了一件事，说明它该被拆成两条或降级成 AGENTS.md 里的一段。
SINGLE_ENTRY_CHARS = 120

#: 全档案条数上限。挡"碎"：20 个项目各写 5 条碎事实，与 3 个项目写清目标一样贵，
#: 换来的判断力却差得多。
TOTAL_ENTRIES = 60

#: 全档案字数上限。挡"长"：这是每轮固定的上下文成本，需要一个定价。
TOTAL_CHARS = 4000

#: 每区条数（§3.3 的软约束）。项目区例外，它按"组数 × 每组条数"算。
SECTION_ENTRY_LIMITS: dict[str, int] = {
    SECTION_IDENTITY: 10,
    SECTION_PREFERENCES: 20,
    SECTION_TOOLS: 12,
}

#: 项目区的两道限。8 组、每组 8 条 → 项目段最多 64 条，仍受全局 60 条约束。
PROJECT_GROUP_LIMIT = 8
PROJECT_GROUP_ENTRIES = 8

#: 注入硬顶（§5.2）。**写入侧永远碰不到它**——4000 字就拒了；它只兜"用户在外部编辑器里
#: 把档案改超了"这一态，那是期二注入块的事。这里只把数留在同一个地方，界面读数不另算。
INJECTION_LIMIT_CHARS = 6000

#: 变更流保留最近多少条（§3.4、§10 第 6 条建议值）。超出从最旧开始丢。
CHANGELOG_KEEP = 300

#: 每区推荐字数（界面读数用，**不是硬限**）。§6.1 的读数形如 ``7/20 条 · 320/800 字``：
#: 条数是硬限，字数是给用户的参考线。按"每区条数 × 单条 40 字"估，与 §3.3 的量级一致。
SECTION_CHARS: dict[str, int] = {
    SECTION_IDENTITY: 400,
    SECTION_PREFERENCES: 800,
    SECTION_PROJECTS: 400,
    SECTION_TOOLS: 480,
}

# --------------------------------------------------------------------- 来源

#: 三条路（§4.1）写的是**同一份档案、同一个服务、同一套预算**，只有来源标注不同。
SOURCE_EXPLICIT = "显式"
SOURCE_IMPLICIT = "隐式"
SOURCE_UI = "界面"
SOURCE_MIGRATION = "迁移"

# --------------------------------------------------------------------- 回执

RECEIPT_ADDED = "记下了：{text}"
RECEIPT_REPLACED = "改成：{text}（旧的已留档，可还原）"
RECEIPT_EXISTING = "档案里已经有了：{text}"
RECEIPT_REJECTED = "{scope}满了（{current}/{limit}{unit}）。要我删一条，还是顶替哪一条？"
RECEIPT_SENSITIVE = "这条含密码、令牌、密钥或证件号这类信息，不进档案。"
RECEIPT_TOO_LONG = "一条最多 {limit} 字（这条 {current} 字）。请拆成两条，或写进 AGENTS.md。"
RECEIPT_UNKNOWN_SECTION = (
    "「{section}」不是档案的分区（只有身份与称呼、长期偏好与风格、进行中的项目、工具与环境）。"
)
RECEIPT_FORGOTTEN = "忘掉了：{text}（旧的已留档，可还原）"
RECEIPT_RESTORED = "还原成：{text}"
RECEIPT_MISSING = "档案里没有这一条：{text}"
RECEIPT_AMBIGUOUS = "档案里有 {count} 条对得上「{topic}」的：{items}。请把要忘掉的那条原样给我。"

#: 变更流查证的命中判据：查询的**二元组**有多少比例在记录里出现过（§5.3）。
#:
#: 与旧召回的 ``MIN_TERM_COVERAGE`` 同一量级（1/3），但**不依赖分词**——池子只有
#: 几十到几百条记录，"字面重合够多"就够用了，而分词（``jieba``）本来就不在闭包里。
MIN_CHANGE_COVERAGE = 1 / 3

# ----------------------------------------------------------------- 机械判据
#
# 与旧的记忆去重同一套阈值与同一条"数字不同不顶替"的否决；差别只在**动作**：
# 旧的是"跳过"，现在是"顶替"（§4.3）。

#: 字符二元组重合度（Jaccard）下限。
DUP_SIMILARITY = 0.7
#: 包含关系算同一件事时，长的那条最多能比短的多几个字。
CONTAINMENT_SLACK = 6

#: 动作常量（与变更流的动作名不同：这些是**返回值**，变更流里写中文）。
ACT_ADD = "add"
ACT_REPLACE = "replace"
ACT_EXISTING = "existing"

#: 否定式偏好（雷点）在裁剪时与偏好同组（§8.2 第 4 类："偏好（含雷点）"）。
#: 这里不必单列，因为雷点本来就落在偏好区；留着这个说明是为了让裁剪优先级一眼可查。

# ------------------------------------------------------------------- 敏感信息
#
# §4.2 的第三条否决项：密码、令牌、密钥、证件号——**绝不写**，哪怕用户直接粘贴过来。
# 档案每轮进上下文，这条比旧口径更重。
#
# 判据是"凭据词"或"凭据形状"命中即否决。宁可误伤一句"用户的密码策略是 12 位"
# （它讲的是策略，不是凭据），也不放一条真凭据进每轮注入的上下文。

_SENSITIVE_WORDS: tuple[str, ...] = (
    "密码",
    "口令",
    "令牌",
    "密钥",
    "私钥",
    "passwd",
    "password",
    "secret",
    "token",
    "apikey",
    "api key",
    "api_key",
    "access key",
    "private key",
    "证件号",
    "身份证号",
    "护照号",
    "银行卡号",
    "信用卡号",
)

_SENSITIVE_SHAPES: tuple[re.Pattern[str], ...] = (
    # OpenAI 一族
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"),
    # GitHub 令牌
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    # 长十六进制串（私钥/摘要/哈希里最像凭据的那一类）
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    # 身份证号（15/18 位，末位可能是 X）
    re.compile(r"(?<!\d)\d{15,19}[Xx]?(?!\d)"),
)


def is_sensitive(text: str) -> bool:
    """这条能不能进档案（False = 含敏感信息，绝不写）。"""
    body = text or ""
    low = body.casefold()
    if any(word in low for word in _SENSITIVE_WORDS):
        return True
    return any(shape.search(body) for shape in _SENSITIVE_SHAPES)


# ------------------------------------------------------------------- 判据实现


def _contains(shorter: str, longer: str) -> bool:
    """``shorter`` 整段出现在 ``longer`` 里，且两端落在词边界上。

    边界这条是给 ASCII 词留的：「代号叫 kylab」是「代号叫 kylab2 代」的子串，
    但那是另一个版本，不是同一件事。沿用旧实现 ``memory.py::_contains``。
    """
    start = longer.find(shorter)
    while start >= 0:
        end = start + len(shorter)
        before = longer[start - 1] if start else " "
        after = longer[end] if end < len(longer) else " "
        if not (before.isalnum() or after.isalnum()):
            return True
        start = longer.find(shorter, start + 1)
    return False


def _bigram_similar(one: str, other: str) -> bool:
    """字符二元组重合度（Jaccard）够不够高。"""
    first, second = fingerprint(one), fingerprint(other)
    if len(first) < 2 or len(second) < 2:
        return False
    grams_first = {first[index : index + 2] for index in range(len(first) - 1)}
    grams_second = {second[index : index + 2] for index in range(len(second) - 1)}
    if not grams_first or not grams_second:
        return False
    return len(grams_first & grams_second) / len(grams_first | grams_second) >= DUP_SIMILARITY


@dataclass(frozen=True, slots=True)
class Match:
    """机械判据给出的定位结果。"""

    action: str
    """``add`` / ``replace`` / ``existing``。"""

    index: int = -1
    """``replace`` / ``existing`` 指向档案里的第几条（-1 = 没有）。"""

    kept: str = ""
    """最终留在档案里的文本。包含关系里是**更完整的那一条**（§4.3）。"""


def classify_action(new_text: str, existing: Sequence[ArchiveEntry]) -> Match:
    """新条目相对现有档案该做什么（§4.3 那张表）。

    顺序很重要：

    1. **指纹相同**（只差标点空白）→ 已存在，不写；
    2. **数字集合不同**的那一条直接跳过——版本号、地址、数量、日期是唯一能机械区分
       "同一个东西"与"两个东西"的证据，这条否决一个字都不放松；
    3. 包含且差值 ≤ 6 字 → 顶替：**留下更完整的那一条**。若现有那条更长，
       新条目并没有补上信息，按"已存在"回执；
    4. 二元组重合 ≥ 0.7 → 顶替；
    5. 其余 → 新增。
    """
    new_fp = fingerprint(new_text)
    if not new_fp:
        return Match(ACT_ADD, kept=new_text)
    for index, entry in enumerate(existing):
        if fingerprint(entry.text) == new_fp:
            return Match(ACT_EXISTING, index=index, kept=entry.text)

    new_digits = digit_runs(new_text)
    new_loose = normalize(new_text)
    for index, entry in enumerate(existing):
        if digit_runs(entry.text) != new_digits:
            continue
        entry_loose = normalize(entry.text)
        shorter, longer = sorted((new_loose, entry_loose), key=len)
        if shorter and len(longer) - len(shorter) <= CONTAINMENT_SLACK and _contains(
            shorter, longer
        ):
            if len(new_loose) >= len(entry_loose):
                return Match(ACT_REPLACE, index=index, kept=new_text)
            return Match(ACT_EXISTING, index=index, kept=entry.text)
    for index, entry in enumerate(existing):
        if digit_runs(entry.text) != new_digits:
            continue
        if _bigram_similar(new_text, entry.text):
            return Match(ACT_REPLACE, index=index, kept=new_text)
    return Match(ACT_ADD, kept=new_text)


# --------------------------------------------------------------------- 预算


@dataclass(frozen=True, slots=True)
class SectionBudget:
    """一个分区的读数（界面顶部那条读数与每区读数共用它，§5.2、§6.1）。"""

    name: str
    entries: int = 0
    chars: int = 0
    limit: int = 0
    """条数上限；0 = 这一区不按条数限（项目区按组限）。"""

    groups: int = 0
    group_limit: int = 0
    group_entries: tuple[tuple[str, int], ...] = ()
    suggested_chars: int = 0
    known: bool = True


@dataclass(frozen=True, slots=True)
class BudgetReport:
    """全档案预算读数。"""

    entries: int = 0
    chars: int = 0
    entry_limit: int = TOTAL_ENTRIES
    char_limit: int = TOTAL_CHARS
    sections: tuple[SectionBudget, ...] = field(default_factory=tuple)

    @property
    def over_entries(self) -> bool:
        return self.entries > self.entry_limit

    @property
    def over_chars(self) -> bool:
        return self.chars > self.char_limit


@dataclass(frozen=True, slots=True)
class Overrun:
    """一条被突破的限额（``kind`` 决定回执里说哪一句）。"""

    kind: str
    """``section`` / ``group`` / ``group-entry`` / ``count`` / ``chars``。"""

    section: str = ""
    group: str = ""
    current: int = 0
    limit: int = 0


def _group_counts(entries: Sequence[ArchiveEntry], section: str) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.section != section:
            continue
        name = entry.group or ""
        counts[name] = counts.get(name, 0) + 1
    return tuple(counts.items())


def budget_of(entries: Sequence[ArchiveEntry]) -> BudgetReport:
    """按现有条目算一份完整读数（含四个已知分区，**空区也报**，界面要画四个）。"""
    present: list[str] = []
    for entry in entries:
        if entry.section not in present:
            present.append(entry.section)
    order = list(KNOWN_SECTIONS) + [name for name in present if name not in KNOWN_SECTIONS]
    sections: list[SectionBudget] = []
    for name in order:
        mine = [entry for entry in entries if entry.section == name]
        groups = _group_counts(entries, name) if name == SECTION_PROJECTS else ()
        sections.append(
            SectionBudget(
                name=name,
                entries=len(mine),
                chars=sum(len(entry.text) for entry in mine),
                limit=SECTION_ENTRY_LIMITS.get(name, 0),
                groups=len(groups),
                group_limit=PROJECT_GROUP_LIMIT if name == SECTION_PROJECTS else 0,
                group_entries=groups,
                suggested_chars=SECTION_CHARS.get(name, 0),
                known=name in KNOWN_SECTIONS,
            )
        )
    return BudgetReport(
        entries=len(entries),
        chars=sum(len(entry.text) for entry in entries),
        sections=tuple(sections),
    )


def overrun(entries: Sequence[ArchiveEntry]) -> Overrun | None:
    """这份档案超出哪一条限额（``None`` = 没超）。

    **判据落在结果态上**：顶替是先移走旧的再放新的，所以"净额为负"这件事不用单独算
    ——把结果代进来，超了就是超了（§3.3：净增为正且会越线时同样拒绝）。
    """
    report = budget_of(entries)
    for section in report.sections:
        if not section.known:
            continue
        if section.name == SECTION_PROJECTS:
            if section.group_limit and section.groups > section.group_limit:
                return Overrun(
                    "group",
                    section=section.name,
                    current=section.groups,
                    limit=section.group_limit,
                )
            for group, count in section.group_entries:
                if count > PROJECT_GROUP_ENTRIES:
                    return Overrun(
                        "group-entry",
                        section=section.name,
                        group=group,
                        current=count,
                        limit=PROJECT_GROUP_ENTRIES,
                    )
            continue
        if section.limit and section.entries > section.limit:
            return Overrun(
                "section",
                section=section.name,
                current=section.entries,
                limit=section.limit,
            )
    if report.over_entries:
        return Overrun("count", current=report.entries, limit=report.entry_limit)
    if report.over_chars:
        return Overrun("chars", current=report.chars, limit=report.char_limit)
    return None


def display_counts(over: Overrun) -> tuple[int, int]:
    """回执里报的读数：``(current, limit)``。

    ``overrun`` 是在**结果态**上算的（"如果加进去会怎样"），所以追加一条越线时它给的是
    ``limit + 1``。但回执要说的是"这一区满了（10/10 条）"——报的是"现在就到顶了"，
    所以显示值取 ``min(current, limit)``：这也是 §4.4 那句示例的读法。
    """
    return min(over.current, over.limit), over.limit


def _reject_receipt(over: Overrun) -> str:
    """被拒回执：**必须给两条出路**（§4.4）——拒绝而不给出路，模型会反复重试。"""
    current, limit = display_counts(over)
    if over.kind == "chars":
        return RECEIPT_REJECTED.format(scope="档案", current=current, limit=limit, unit=" 字")
    if over.kind == "count":
        scope = "档案"
    elif over.kind == "group":
        scope = "项目区"
    elif over.kind == "group-entry":
        scope = f"「{over.group or '未分组'}」这一组"
    else:
        scope = "这一区"
    unit = " 组" if over.kind == "group" else " 条"
    return RECEIPT_REJECTED.format(scope=scope, current=current, limit=limit, unit=unit)


# --------------------------------------------------------------------- 变更流查证
#
# §5.3：``recall`` 保留但降级为"查证"——池子只剩变更流，而档案本身**不进召回池**
# （它已经全量注入）。这一节只做一件事：在 ``changes.md`` 上按**纯字面**判据排序。
#
# 三条刻意的选择：
#
# 1. **不依赖分词**（``jieba`` 不在闭包里）：判据是"归一化子串命中数 + 二元组覆盖率"，
#    两个都只看字符本身；
# 2. **不建索引、不缓存**：池子只有几十到几百条，每次现读现算比维护索引便宜；
# 3. **"没搜到"只有一个含义**：变更流里确实没有相关的话——没有"索引没建好"这种
#    中间态可供误读（这条性质比排序精度更重要）。


@dataclass(frozen=True, slots=True)
class ChangeHit:
    """变更流里的一条命中（形状与召回命中对齐：文本 + 出处 + 行号 + 分数）。"""

    text: str
    """这条记录的原样渲染（``时间 · 动作 · 分区`` + 旧值/新值两行）。"""

    path: str = CHANGES_FILENAME
    start_line: int = 1
    end_line: int = 1
    score: float = 0.0
    coverage: float = 0.0
    source: str = "text"


def _change_terms(query: str) -> list[str]:
    """查询的**相邻字对**（归一化后去重、保序）；单字查询退化成它自己。"""
    flat = normalize(query)
    if len(flat) < 2:
        return [flat] if flat else []
    terms: list[str] = []
    for index in range(len(flat) - 1):
        gram = flat[index : index + 2]
        if gram not in terms:
            terms.append(gram)
    return terms


def _bigram_coverage(query: str, text: str) -> float:
    """查询的二元组有多少比例在记录里出现过（0–1，归一化后比）。"""
    flat_query, flat_text = normalize(query), normalize(text)
    if not flat_query or not flat_text:
        return 0.0
    if len(flat_query) < 2:
        return 1.0 if flat_query in flat_text else 0.0
    wanted = {flat_query[index : index + 2] for index in range(len(flat_query) - 1)}
    found = {flat_text[index : index + 2] for index in range(len(flat_text) - 1)}
    return len(wanted & found) / len(wanted) if wanted else 0.0


def _records_with_lines(text: str) -> list[tuple[ChangeRecord, int, int]]:
    """解析变更流并补上每条记录**在文件里的行号区间**（界面/模型都要能指过去）。

    ``parse_changes`` 已经定下了记录边界（顶格 ``- `` 开一条），这里只是把
    那些行号按出现顺序数出来——两边判据同源，不会漂。
    """
    records = parse_changes(text)
    lines = text.splitlines()
    heads = [index for index, line in enumerate(lines, start=1) if line.startswith("- ")]
    out: list[tuple[ChangeRecord, int, int]] = []
    for position, record in enumerate(records):
        start = heads[position] if position < len(heads) else 1
        end = heads[position + 1] - 1 if position + 1 < len(heads) else max(start, len(lines))
        out.append((record, start, end))
    return out


def search_changes(text: str, query: str, *, limit: int = 20) -> list[ChangeHit]:
    """在变更流文本里查证（§5.3）。排序：``归一化子串命中数 + 二元组覆盖率``。

    命中判据两条，满足任意一条就算：
    **整条查询原样出现**，或**字对覆盖率 ≥** :data:`MIN_CHANGE_COVERAGE`。
    同分时**近的排前面**——"我刚改了什么"比"半年前改了什么"更常是问话的意图。
    """
    flat_query = normalize(query)
    terms = _change_terms(query)
    hits: list[ChangeHit] = []
    for record, start, end in _records_with_lines(text or ""):
        body = render_change(record)
        flat_body = normalize(body)
        matched = sum(1 for term in terms if term in flat_body)
        coverage = _bigram_coverage(query, body)
        if flat_query not in flat_body and (
            not terms or matched / len(terms) < MIN_CHANGE_COVERAGE
        ):
            continue
        hits.append(
            ChangeHit(
                text=body,
                start_line=start,
                end_line=end,
                score=float(matched) + coverage,
                coverage=coverage,
            )
        )
    hits.sort(key=lambda hit: (-hit.score, -hit.start_line))
    return hits[: max(1, limit)]


# --------------------------------------------------------------------- 结果


@dataclass(frozen=True, slots=True)
class WriteResult:
    """一次写入的结果与回执（§4.4）。"""

    action: str
    """``added`` / ``replaced`` / ``existing`` / ``rejected`` / ``forgotten`` / ``restored``。"""

    receipt: str
    text: str = ""
    section: str = ""
    group: str = ""
    replaced: str = ""
    reason: str = ""
    """``sensitive`` / ``single`` / ``section`` / ``group`` / ``group-entry`` / ``count``
    / ``chars`` / ``section-unknown`` / ``missing``。"""

    current: int = 0
    limit: int = 0


# --------------------------------------------------------------------- 服务

#: 写成功时的动作名（与 ``match.action`` 对齐的对外值）。
ACTION_ADDED_OUT = "added"
ACTION_REPLACED_OUT = "replaced"
ACTION_EXISTING_OUT = "existing"
ACTION_REJECTED_OUT = "rejected"
ACTION_FORGOTTEN_OUT = "forgotten"
ACTION_RESTORED_OUT = "restored"


class ArchiveService:
    """一份档案上的读写（按账号就是 ``data/memory/<user_id>/`` 那个工作区）。

    ``now`` 可注入，测试里要冻结时间（变更流带时间戳，而"还原后逐字节相等"要求
    同一天写的 ``updated`` 一致）。
    """

    def __init__(
        self,
        workspace: Path,
        *,
        now: Callable[[], datetime] | None = None,
        changelog_keep: int = CHANGELOG_KEEP,
    ) -> None:
        self._workspace = Path(workspace)
        self._now = now or (lambda: datetime.now().astimezone())
        self._keep = changelog_keep

    @property
    def workspace(self) -> Path:
        return self._workspace

    # ---------------------------------------------------------------- 读

    def read(self) -> Archive:
        """读整份档案（文件不存在 = 空档案）。"""
        return read_archive(self._workspace)

    def changes(self) -> list[ChangeRecord]:
        """读变更流（最旧在前）。"""
        return read_changes(self._workspace)

    def changes_hits(self, query: str, *, limit: int = 20) -> list[ChangeHit]:
        """在变更流里查证（§5.3 的 ``recall`` 新池子）：现读现算，无缓存、无索引。"""
        return search_changes(
            read_text(self._workspace / CHANGES_FILENAME), query, limit=limit
        )

    def budget(self, archive: Archive | None = None) -> BudgetReport:
        """预算读数。"""
        return budget_of((archive or self.read()).entries)

    # ---------------------------------------------------------------- 写

    def add(
        self,
        text: str,
        section: str,
        *,
        group: str = "",
        replaces: str | None = None,
        source: str = SOURCE_EXPLICIT,
    ) -> WriteResult:
        """新增或顶替一条（§4.3、§4.4）。

        ``replaces`` 是模型显式指认的顶替目标（§4.3 第 1 条：模型的判定优先）。
        它找不到时退回机械判据——显式指认错了不该让这轮写入直接失败。

        拒绝路径的纪律：**敏感信息一条记录都不留**（留痕本身就是泄漏），其余被拒
        （超预算、单条超长）写一条 ``撤回`` 进变更流，因为它解释了"我说了记住，怎么没记上"。
        """
        clean = normalize_entry(text)
        if not clean:
            raise InvalidRequestError("缺少参数：text")
        section = (section or "").strip()
        group = (group or "").strip()

        if section not in KNOWN_SECTIONS:
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_UNKNOWN_SECTION.format(section=section or "（空）"),
                text=clean,
                section=section,
                reason="section-unknown",
            )
        if is_sensitive(clean):
            # **不留痕**：变更流也是磁盘上的明文，把凭据记进去等于换个地方泄漏。
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_SENSITIVE,
                text=clean,
                section=section,
                reason="sensitive",
            )
        if len(clean) > SINGLE_ENTRY_CHARS:
            return self._reject(
                WriteResult(
                    action=ACTION_REJECTED_OUT,
                    receipt=RECEIPT_TOO_LONG.format(limit=SINGLE_ENTRY_CHARS, current=len(clean)),
                    text=clean,
                    section=section,
                    group=group,
                    reason="single",
                    current=len(clean),
                    limit=SINGLE_ENTRY_CHARS,
                ),
                source=source,
            )

        archive = self.read()
        entries = list(archive.entries)
        match = self._match(clean, entries, replaces)
        if match.action == ACT_EXISTING:
            kept = match.kept
            home = entries[match.index] if match.index >= 0 else None
            return WriteResult(
                action=ACTION_EXISTING_OUT,
                receipt=RECEIPT_EXISTING.format(text=kept),
                text=kept,
                section=home.section if home else section,
                group=home.group if home else group,
            )

        replaced = ""
        if match.action == ACT_REPLACE and match.index >= 0:
            replaced = entries.pop(match.index).text
        entries.append(ArchiveEntry(text=clean, section=section, group=group))

        over = overrun(entries)
        if over is not None:
            return self._reject(
                WriteResult(
                    action=ACTION_REJECTED_OUT,
                    receipt=_reject_receipt(over),
                    text=clean,
                    section=section,
                    group=group,
                    reason=over.kind,
                    current=display_counts(over)[0],
                    limit=over.limit,
                ),
                source=source,
            )

        self._save(archive, entries)
        if match.action == ACT_REPLACE:
            self._log(ACTION_REPLACED, section, old=replaced, new=clean, source=source)
            return WriteResult(
                action=ACTION_REPLACED_OUT,
                receipt=RECEIPT_REPLACED.format(text=clean),
                text=clean,
                section=section,
                group=group,
                replaced=replaced,
            )
        self._log(ACTION_ADDED, section, old="", new=clean, source=source)
        return WriteResult(
            action=ACTION_ADDED_OUT,
            receipt=RECEIPT_ADDED.format(text=clean),
            text=clean,
            section=section,
            group=group,
        )

    def forget(self, topic: str, *, source: str = SOURCE_EXPLICIT) -> WriteResult:
        """忘掉一条：从档案删除 + 变更流留痕 + 可还原（§9.2 第 6 条）。

        ``topic`` 先按**原样**找（指纹/归一化相等），找不到再按**唯一子串**兜一次：
        调这个工具的模型手里有整份档案，多数时候能原样抄出那一条；但用户说"忘掉
        那条关于 NAS 的"时，模型给的多半是半句话。

        **对得上不止一条时不猜**：宁可回执列出候选让它说清，也不删错一条——
        删错的代价（一条真事实没了）与多问一句完全不对等。
        """
        clean = normalize_entry(topic)
        if not clean:
            raise InvalidRequestError("缺少参数：topic")
        archive = self.read()
        entries = list(archive.entries)
        index = _locate(entries, clean)
        if index is None:
            candidates = _locate_candidates(entries, clean)
            if len(candidates) > 1:
                listed = "、".join(f"「{entries[item].text}」" for item in candidates[:5])
                return WriteResult(
                    action=ACTION_REJECTED_OUT,
                    receipt=RECEIPT_AMBIGUOUS.format(
                        count=len(candidates), topic=clean, items=listed
                    ),
                    text=clean,
                    reason="ambiguous",
                )
            index = candidates[0] if candidates else None
        if index is None:
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_MISSING.format(text=clean),
                text=clean,
                reason="missing",
            )
        entry = entries.pop(index)
        self._save(archive, entries)
        self._log(ACTION_FORGOTTEN, entry.section, old=entry.text, new="", source=source)
        return WriteResult(
            action=ACTION_FORGOTTEN_OUT,
            receipt=RECEIPT_FORGOTTEN.format(text=entry.text),
            text=entry.text,
            section=entry.section,
            group=entry.group,
            replaced=entry.text,
        )

    def restore(self, old_text: str, *, source: str = SOURCE_UI) -> WriteResult:
        """把一条旧值写回档案，新值作为一次新的顶替进流（§3.4、§6.2）。

        找的是**最近一条**仍能回溯的记录：``顶替`` 用它的新值定位当前条目再换回旧值，
        ``忘掉`` 直接把旧值放回它原来那一区。

        **已知边界**：变更流每条只有一个"分区"字段，跨分区的顶替（§4.3 第 5 条）
        还原时会落在**新分区**——同区顶替不受影响（验收判据 §9.2 第 5 条走的就是它）。
        """
        clean = normalize_entry(old_text)
        record = None
        for candidate in reversed(self.changes()):
            if candidate.action not in (ACTION_REPLACED, ACTION_FORGOTTEN):
                continue
            if normalize_entry(candidate.old) == clean:
                record = candidate
                break
        if record is None:
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_MISSING.format(text=clean),
                text=clean,
                reason="missing",
            )

        archive = self.read()
        entries = list(archive.entries)
        index = _locate(entries, record.new) if record.new else None
        if index is not None:
            current = entries.pop(index)
            entries.append(
                ArchiveEntry(text=record.old, section=current.section, group=current.group)
            )
        elif _locate(entries, record.old) is not None:
            return WriteResult(
                action=ACTION_EXISTING_OUT,
                receipt=RECEIPT_EXISTING.format(text=record.old),
                text=record.old,
                section=record.section,
            )
        elif self._revert_group(entries, record):
            pass
        else:
            entries.append(ArchiveEntry(text=record.old, section=record.section))

        over = overrun(entries)
        if over is not None:
            return self._reject(
                WriteResult(
                    action=ACTION_REJECTED_OUT,
                    receipt=_reject_receipt(over),
                    text=record.old,
                    section=record.section,
                    reason=over.kind,
                    current=display_counts(over)[0],
                    limit=over.limit,
                ),
                source=source,
            )
        self._save(archive, entries)
        self._log(ACTION_RESTORED, record.section, old=record.new, new=record.old, source=source)
        return WriteResult(
            action=ACTION_RESTORED_OUT,
            receipt=RECEIPT_RESTORED.format(text=record.old),
            text=record.old,
            section=record.section,
            replaced=record.new,
        )

    def rename_group(
        self,
        section: str,
        old: str,
        new: str,
        *,
        source: str = SOURCE_UI,
    ) -> WriteResult:
        """项目段的组改名（§3.1 第 2 条：改名 = 一次顶替，旧名进变更流）。

        组标题本身也是可顶替的对象。改的是**这一组全部条目的 ``group`` 字段**，而
        变更流只留一条记录（旧名 → 新名）——所以它不是"每条各顶替一次"，
        也与条目正文无关（正文一个字不改）。
        """
        section = (section or "").strip() or SECTION_PROJECTS
        old_clean = normalize_entry(old)
        new_clean = normalize_entry(new)
        if not old_clean or not new_clean:
            raise InvalidRequestError("缺少参数：old / new")
        if section not in KNOWN_SECTIONS:
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_UNKNOWN_SECTION.format(section=section),
                text=new_clean,
                section=section,
                reason="section-unknown",
            )
        if is_sensitive(new_clean):
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_SENSITIVE,
                text=new_clean,
                section=section,
                reason="sensitive",
            )
        if len(new_clean) > SINGLE_ENTRY_CHARS:
            return self._reject(
                WriteResult(
                    action=ACTION_REJECTED_OUT,
                    receipt=RECEIPT_TOO_LONG.format(
                        limit=SINGLE_ENTRY_CHARS, current=len(new_clean)
                    ),
                    text=new_clean,
                    section=section,
                    reason="single",
                    current=len(new_clean),
                    limit=SINGLE_ENTRY_CHARS,
                ),
                source=source,
            )

        archive = self.read()
        entries = list(archive.entries)
        targets = [
            index
            for index, entry in enumerate(entries)
            if entry.section == section
            and entry.group
            and normalize(entry.group) == normalize(old_clean)
        ]
        if not targets:
            return WriteResult(
                action=ACTION_REJECTED_OUT,
                receipt=RECEIPT_MISSING.format(text=old_clean),
                text=old_clean,
                section=section,
                reason="missing",
            )
        for index in targets:
            entries[index] = ArchiveEntry(
                text=entries[index].text,
                section=entries[index].section,
                group=new_clean,
            )
        self._save(archive, entries)
        self._log(ACTION_REPLACED, section, old=old_clean, new=new_clean, source=source)
        return WriteResult(
            action=ACTION_REPLACED_OUT,
            receipt=RECEIPT_REPLACED.format(text=new_clean),
            text=new_clean,
            section=section,
            group=new_clean,
            replaced=old_clean,
        )

    # ---------------------------------------------------------------- 内部

    def _revert_group(self, entries: list[ArchiveEntry], record: ChangeRecord) -> bool:
        """把一条**组改名**记录还原回去（``restore`` 的第三个分支）。

        组改名记录的 ``old`` / ``new`` 是**组名**而不是条目正文，所以在档案里按
        "有没有这条条目"是找不到的。这里换一个判据：当前还有没有条目的 ``group``
        等于记录的新名——有就把它们改回旧名。不是组改名就返回 ``False``，
        让 ``restore`` 走原来的兜底（把旧值当一条新条目放回）。
        """
        if not record.new:
            return False
        wanted = normalize(record.new)
        targets = [
            index
            for index, entry in enumerate(entries)
            if entry.group and normalize(entry.group) == wanted
        ]
        if not targets:
            return False
        for index in targets:
            entries[index] = ArchiveEntry(
                text=entries[index].text,
                section=entries[index].section,
                group=record.old,
            )
        return True

    def _match(self, clean: str, entries: list[ArchiveEntry], replaces: str | None) -> Match:
        """定位要顶替的那条：模型显式指认优先，机械判据兜底（§4.3）。"""
        if replaces:
            index = _locate(entries, replaces)
            if index is not None:
                if fingerprint(entries[index].text) == fingerprint(clean):
                    return Match(ACT_EXISTING, index=index, kept=entries[index].text)
                return Match(ACT_REPLACE, index=index, kept=clean)
        return classify_action(clean, entries)

    def _save(self, archive: Archive, entries: list[ArchiveEntry]) -> None:
        write_archive(
            self._workspace,
            Archive(
                entries=tuple(entries),
                updated=self._now().date().isoformat(),
                unknown_sections=archive.unknown_sections,
            ),
        )

    def _log(self, action: str, section: str, *, old: str, new: str, source: str) -> None:
        append_changes(
            self._workspace,
            [
                ChangeRecord(
                    at=self._now().strftime("%Y-%m-%d %H:%M"),
                    action=action,
                    section=section,
                    source=source,
                    old=old,
                    new=new,
                )
            ],
            keep=self._keep,
        )

    def _reject(self, result: WriteResult, *, source: str) -> WriteResult:
        """被拒的写入记一条 ``撤回``（§3.4：它解释"我说了要记住，怎么没记上"）。

        敏感信息走的是另一条路（``add`` 里直接返回）——**它绝不能进变更流**。
        """
        self._log(ACTION_REVERTED, result.section or "—", old="", new=result.text, source=source)
        return result


def _locate(entries: Sequence[ArchiveEntry], text: str) -> int | None:
    """在档案里找一条：先比指纹（只差标点空白也算），再比归一化文本。"""
    wanted = fingerprint(text)
    if wanted:
        for index, entry in enumerate(entries):
            if fingerprint(entry.text) == wanted:
                return index
    loose = normalize(text)
    if loose:
        for index, entry in enumerate(entries):
            if normalize(entry.text) == loose:
                return index
    return None


def _locate_candidates(entries: Sequence[ArchiveEntry], text: str) -> list[int]:
    """按**唯一子串**兜一次：哪些条目的归一化文本包含这段话题（``forget`` 用）。

    只给 ``forget`` 一个人用，因为"忘掉"的入参是**话题**而不是条目原文；
    ``remember``/``replaces`` 不该走这条——那里指错目标会静默顶替错一条。
    """
    loose = normalize(text)
    if not loose:
        return []
    return [index for index, entry in enumerate(entries) if loose in normalize(entry.text)]
