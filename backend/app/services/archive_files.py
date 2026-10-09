"""记忆**档案**的文件层（期一，见 ``docs/设计/记忆档案-设计-v0.1.md`` §3、§4、§8）。

档案制把对象从"多份按时间堆叠的流水"换成**一份四区画像**（§1.2）。这一层只做
文件那一半：解析、渲染、写字节；"这一条该进哪、预算够不够、顶替谁"在
``archive.py`` 里。

四件刻意的事：

1. **按行解析，纯标准库**（§2.3）：档案是固定分区 + ``- `` 条目，正则扫一遍就够。
   连 ``markdown-it-py`` 都不碰——旧实现惰性导入它，本来就不在闭包里，而"闭包不许涨"
   是硬约束，凭"它反正不在闭包里"去用它，迟早把别的解析需求也引进来；
2. **写字节**（§3.5 第 3 条）：``Path.write_text`` 在 Windows 上会把 ``\\n`` 翻成
   ``\\r\\n``，文件里本来有 ``\\r\\n`` 时结果是 ``\\r\\r\\n``——每存一次长一个 ``\\r``。
   所以渲染一律产 ``\\n`` + ``write_bytes``，读也按字节读再解码；
3. **frontmatter 只放 ``updated``**（§3.5 第 1 条）。这里用最小手写解析而不是 ``yaml``：
   格式只有一行键值，为它引一个解析器没有收益，而"纯标准库"这条纪律要能一眼看出来；
4. **未知分区照解析、不静默丢**（§3.5 第 4 条）：不认识的 ``## 某区`` 照样收成条目、
   照样进预算，只在 ``Archive.unknown_sections`` 里如实标记，交给上面报"分区不认识"。

**条目不携带 id**（§3.5 第 2 条）：正文里只有 ``- 一句话``，内部标识用
``(分区, 归一化文本)`` 的短指纹，只在变更流记账时用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ARCHIVE_FILENAME",
    "CHANGES_FILENAME",
    "IMPORT_DRAFT_FILENAME",
    "KNOWN_SECTIONS",
    "METADATA_DIRNAME",
    "MIGRATION_WATERMARK_PATH",
    "SECTION_IDENTITY",
    "SECTION_PREFERENCES",
    "SECTION_PROJECTS",
    "SECTION_TOOLS",
    "Archive",
    "ArchiveEntry",
    "ChangeRecord",
    "append_changes",
    "bullet_lines",
    "fingerprint",
    "known_section",
    "normalize",
    "normalize_entry",
    "parse_archive",
    "parse_changes",
    "parse_frontmatter",
    "read_archive",
    "read_changes",
    "read_text",
    "render_archive",
    "render_change",
    "strip_bullet",
    "write_archive",
    "write_bytes",
    "write_changes",
]

#: 档案文件名。**沿用 ``PROFILE.md``**（§7.2、§10 第 2 条的建议口径）：它本来就是
#: "每轮注入 + 不进检索"的那一份，正文从散文改成四区条目即可，名与语义都还对得上。
ARCHIVE_FILENAME = "PROFILE.md"

#: 变更流。**不进注入**，只服务"改过什么 / 可还原 / 查证以前那条"（§3.4）。
CHANGES_FILENAME = "changes.md"

#: 迁移时没挤进档案的旧条目（§8.2 第 3、4 类）。只在迁移后存在。
IMPORT_DRAFT_FILENAME = "import-draft.md"

#: 迁移水位所在目录。与旧记忆工作区里那份派生物目录同名（``mem_metadata``），
#: 于是它天然落在 ``memory_files._SKIP_DIRS`` 里，不会被当成记忆文件扫到。
METADATA_DIRNAME = "mem_metadata"

#: 迁移水位（§8.4）：源文件指纹 + 已折叠条目指纹，重跑不重复搬。
MIGRATION_WATERMARK_PATH = f"{METADATA_DIRNAME}/archive-migration.json"

SECTION_IDENTITY = "身份与称呼"
SECTION_PREFERENCES = "长期偏好与风格"
SECTION_PROJECTS = "进行中的项目"
SECTION_TOOLS = "工具与环境"

#: 四个分区的**固定顺序**（§3.1）：解析与渲染都按它，注入也按它。
KNOWN_SECTIONS: tuple[str, ...] = (
    SECTION_IDENTITY,
    SECTION_PREFERENCES,
    SECTION_PROJECTS,
    SECTION_TOOLS,
)

#: 变更流的五个动作（§3.4）。"撤回" = 一次被拒绝的写入。
ACTION_ADDED = "新增"
ACTION_REPLACED = "顶替"
ACTION_FORGOTTEN = "忘掉"
ACTION_RESTORED = "还原"
ACTION_REVERTED = "撤回"

#: ``- x`` / ``* x`` / ``+ x`` / ``1. x``（允许前置缩进）。与旧 ``_BULLET`` 同一口径：
#: 用户拿别的编辑器改档案时，四种写法都可能出现。
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+")

#: ``## 区名``（二级，正好两个 ``#`` 后跟空格）。三级 ``### 项目名`` 不算。
_H2 = re.compile(r"^##\s+(.+?)\s*$")
#: ``### 项目名``。
_H3 = re.compile(r"^###\s+(.+?)\s*$")

#: frontmatter 开头的 ``---`` 块。与 ``memory_files._FRONTMATTER`` 同一形状，
#: 但这里**不用 yaml**（见模块头第 3 条）。
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

#: 比对用：去掉全部非字母数字（``\\w`` 在 Python 3 里含汉字与下划线，下划线单独去掉）。
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)

#: 数字串。**"数字不同就不是同一件事"**这条否决靠它（§4.3）——版本号、地址、
#: 数量、日期都是数字，数字变了就是变了。
_DIGIT_RUN = re.compile(r"\d+")

#: 比对时统一的全角/中日韩标点 → 半角。``NFKC`` 已经管住全角 ASCII，
#: 管不住 ``。`` ``、`` ``「」`` 这些；不统一的话，"同一句话只差标点"会漏。
_PUNCT_MAP = str.maketrans(
    {
        "。": ".",
        "，": ",",
        "、": ",",
        "；": ";",
        "：": ":",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "～": "~",
        "－": "-",
    }
)


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """档案里的一条。

    ``section`` 是**文件里原样的区名**（未知分区也就地保留，见 §3.5 第 4 条）；
    ``group`` 是项目段 ``### 项目名`` 的分组名，其余分区为空串。
    """

    text: str
    section: str
    group: str = ""


@dataclass(frozen=True, slots=True)
class Archive:
    """一份解析后的档案。

    ``unknown_sections`` 单独留一份（即使对应的条目为空）：解析时按出现顺序收，
    渲染时排在四个已知分区之后——于是"认识"与"不认识"在文件里都稳定。
    """

    entries: tuple[ArchiveEntry, ...] = ()
    updated: str = ""
    unknown_sections: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    """变更流里的一条（§3.4）。``old`` / ``new`` 各占一行，逐字节保存。

    **不拼成一行**是有意的：还原要的是逐字节的旧文本，而条目正文里可能出现任何
    标点，拼行就还原不回去了。
    """

    at: str
    action: str
    section: str
    source: str = ""
    old: str = ""
    new: str = ""


# --------------------------------------------------------------------- 文本


def normalize_entry(text: str) -> str:
    """写入前的规范化：**只压空白与换行**，不动标点与大小写。

    用户写的全角标点、书名号、代码里的 ``::`` 都是他的内容，不该被我们改写。
    """
    return " ".join((text or "").split())


def normalize(text: str) -> str:
    """比对用的规范化形式：``NFKC`` + 统一中日韩标点 + 压空白 + 小写。

    §4.3 的"归一化后完全一致"用这个形态判。
    """
    flat = unicodedata.normalize("NFKC", text or "").translate(_PUNCT_MAP)
    return " ".join(flat.split()).casefold()


def fingerprint(text: str) -> str:
    """去空白、标点之后的指纹（只留字母数字），给"完全一致"与数字集合用。"""
    return _NON_ALNUM.sub("", normalize(text))


def digit_runs(text: str) -> list[str]:
    """指纹里的数字串（按出现顺序）。"""
    return _DIGIT_RUN.findall(fingerprint(text))


def known_section(title: str) -> bool:
    """这个区名是不是四个固定分区之一。"""
    return (title or "").strip() in KNOWN_SECTIONS


# --------------------------------------------------------------------- 读


def read_text(path: Path) -> str:
    """按字节读再解码（与写对称）。读不了返回空串——缺文件是"还没写过"，不是错误。"""
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def write_bytes(path: Path, text: str) -> None:
    """写字节（§3.5 第 3 条），必要时建目录。

    先把行尾统一成 ``\\n`` 再编码：用户拿别的编辑器改过的文件里可能有 ``\\r\\n``，
    我们重写一遍时把 ``\\r`` 带回去，下一次往返就会变成 ``\\r\\r\\n``（实测坑）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(clean.encode("utf-8"))


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 frontmatter 与正文。

    **只认最简单的 ``key: value``**（档案的 frontmatter 只有 ``updated``）：
    坏掉时退回空 frontmatter、正文原样返回，不抛错——用户拿外部编辑器改坏了它，
    下一件事应该是让他能打开来修，而不是整份读不出来。
    """
    match = _FRONTMATTER.match(text or "")
    if not match:
        return {}, text or ""
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[match.end() :]


# --------------------------------------------------------------------- 解析


def parse_archive(text: str) -> Archive:
    """``PROFILE.md`` 正文 → :class:`Archive`。

    规则就三条（§3.5 的格式）：

    - ``## 区名`` 开一个分区；``### 项目名`` 开一个项目组；
    - ``- 一句话`` 是一条；不在任何分区下的条目**不收**（它的位置本身就没有含义）；
    - 其余行（``# 用户档案``、空行、误写进来的散文）忽略。

    未知分区不收进 ``KNOWN_SECTIONS``，但**照样收条目**，并把区名记进
    ``unknown_sections``——静默丢掉用户写在自己档案里的东西，比多花几十个 token 糟。
    """
    meta, body = parse_frontmatter(text)
    entries: list[ArchiveEntry] = []
    unknown: list[str] = []
    section = ""
    group = ""
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        head2 = _H2.match(line)
        if head2:
            section = head2.group(1).strip()
            group = ""
            if section and section not in KNOWN_SECTIONS and section not in unknown:
                unknown.append(section)
            continue
        head3 = _H3.match(line)
        if head3:
            group = head3.group(1).strip() if section else ""
            continue
        if _BULLET.match(line):
            if not section:
                continue
            item = _BULLET.sub("", line).strip()
            if item:
                entries.append(ArchiveEntry(text=item, section=section, group=group))
            continue
        # 其余行（H1、散文、缩进说明）不进模型：档案是"每行一条"的固定形状。
    return Archive(
        entries=tuple(entries),
        updated=meta.get("updated", ""),
        unknown_sections=tuple(unknown),
    )


def render_archive(archive: Archive) -> str:
    """``Archive`` → 文本，**四个固定分区按 §3.1 的顺序**（空区也渲染）。

    空区也留下骨架，是为了让"四区固定"在磁盘上看得见：用户拿编辑器打开时，
    看到的是四个标题而不是一份需要猜结构的文件；注入侧也只是多四行标题。
    未知分区排在四个已知分区**之后**，按首次出现顺序。
    """
    lines = ["---", f"updated: {archive.updated}", "---", "", "# 用户档案", ""]
    for title in KNOWN_SECTIONS:
        lines.extend(_render_section(title, [e for e in archive.entries if e.section == title]))
        lines.append("")
    for title in archive.unknown_sections:
        if title in KNOWN_SECTIONS:
            continue
        lines.extend(
            _render_section(title, [e for e in archive.entries if e.section == title])
        )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_section(title: str, entries: list[ArchiveEntry]) -> list[str]:
    """一个分区 → 若干行（不带尾随空行，空行由调用方在块之间补）。

    未分组的条目在前，``### 组名`` 按**首次出现顺序**在后。
    """
    out = [f"## {title}"]
    ungrouped = [entry.text for entry in entries if not entry.group]
    if ungrouped:
        out.append("")
        out.extend(f"- {text}" for text in ungrouped)
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        if entry.group:
            grouped.setdefault(entry.group, []).append(entry.text)
    for group, texts in grouped.items():
        out.append("")
        out.append(f"### {group}")
        out.append("")
        out.extend(f"- {text}" for text in texts)
    return out


def read_archive(workspace: Path) -> Archive:
    """读工作区里的档案；文件不存在 → 空档案（不是错误）。"""
    return parse_archive(read_text(workspace / ARCHIVE_FILENAME))


def write_archive(workspace: Path, archive: Archive) -> None:
    """把档案渲染后写字节。"""
    write_bytes(workspace / ARCHIVE_FILENAME, render_archive(archive))


# --------------------------------------------------------------------- 变更流


def render_change(record: ChangeRecord) -> str:
    """一条变更记录 → 文本（§3.4 的形态：旧值/新值各占一行）。"""
    head = f"- {record.at} · {record.action} · {record.section}"
    if record.source:
        head += f" · 来源：{record.source}"
    lines = [head]
    if record.old:
        lines.append(f"  - 旧：{record.old}")
    if record.new:
        lines.append(f"  - 新：{record.new}")
    return "\n".join(lines)


def parse_changes(text: str) -> list[ChangeRecord]:
    """变更流文本 → 记录列表（保序，最旧在前）。

    记录头以顶格 ``- `` 开头，取值行以 ``  - 旧：`` / ``  - 新：`` 开头。
    取值行**不 strip**：还原要的是逐字节的旧文本，首尾空白也是它的一部分。
    """
    records: list[ChangeRecord] = []
    current: dict[str, str] | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if line.startswith("  - 旧："):
            if current is not None:
                current["old"] = line[len("  - 旧：") :]
            continue
        if line.startswith("  - 新："):
            if current is not None:
                current["new"] = line[len("  - 新：") :]
            continue
        if line.startswith("- "):
            if current is not None:
                records.append(_record_from(current))
            current = {"head": line[2:]}
            continue
        # 其余（空行、说明文字）不属于任何记录。
    if current is not None:
        records.append(_record_from(current))
    return records


def _record_from(parts: dict[str, str]) -> ChangeRecord:
    fields = [part.strip() for part in parts.get("head", "").split(" · ")]
    at = fields[0] if fields else ""
    action = fields[1] if len(fields) > 1 else ""
    section = fields[2] if len(fields) > 2 else ""
    source = ""
    for extra in fields[3:]:
        if extra.startswith("来源："):
            source = extra[len("来源：") :]
    return ChangeRecord(
        at=at,
        action=action,
        section=section,
        source=source,
        old=parts.get("old", ""),
        new=parts.get("new", ""),
    )


def read_changes(workspace: Path) -> list[ChangeRecord]:
    """读变更流；文件不存在 → 空列表。"""
    return parse_changes(read_text(workspace / CHANGES_FILENAME))


def render_changes(records: list[ChangeRecord]) -> str:
    """整份变更流 → 文本（末尾一个换行）。"""
    if not records:
        return ""
    return "\n".join(render_change(record) for record in records) + "\n"


def write_changes(workspace: Path, records: list[ChangeRecord]) -> None:
    """整份重写变更流（裁剪时用）。"""
    write_bytes(workspace / CHANGES_FILENAME, render_changes(records))


def append_changes(
    workspace: Path, additions: list[ChangeRecord], *, keep: int
) -> list[ChangeRecord]:
    """追加若干条变更记录，**超过 ``keep`` 从最旧开始丢**（§3.4）。

    追加路径不重写已有文本：先把新记录接到原文件尾部（字节级追加），只有真的超了
    保留数才整份重渲染——这样"旧值逐字节可找回"不依赖渲染器的往返保真度。
    """
    path = workspace / CHANGES_FILENAME
    existing = read_changes(workspace)
    block = "\n".join(render_change(record) for record in additions)
    if not block:
        return existing
    body = read_text(path).rstrip("\n")
    text = f"{body}\n{block}\n" if body else f"{block}\n"
    merged = parse_changes(text)
    if len(merged) > keep:
        merged = merged[-keep:]
        write_changes(workspace, merged)
    else:
        write_bytes(path, text)
    return merged


# ----------------------------------------------------------------- 迁移用工具


def bullet_lines(text: str) -> list[tuple[str, str]]:
    """``(当前二级标题, 条目正文)``：把任意 Markdown 里的 ``- `` 条目连标题一起取出。

    迁移要按内容归区，而旧文件的"这一条属于哪一节"写在标题里（``MEMORY.md`` 的
    ``## 工具设置``、``digest/procedure/…`` 的 ``## 做法``），只看条目正文会丢线索。
    """
    _, body = parse_frontmatter(text)
    out: list[tuple[str, str]] = []
    heading = ""
    for raw in body.splitlines():
        line = raw.rstrip()
        head2 = _H2.match(line)
        if head2:
            heading = head2.group(1).strip()
            continue
        if _H3.match(line):
            continue
        if _BULLET.match(line):
            item = _BULLET.sub("", line).strip()
            if item:
                out.append((heading, item))
    return out


def body_text(text: str) -> str:
    """去掉 frontmatter 之后的正文（迁移读散文用）。"""
    return parse_frontmatter(text)[1]


def strip_bullet(line: str) -> str:
    """去掉行首的列表标记（``- `` / ``* `` / ``1. ``）；不是列表项就只 strip。"""
    return _BULLET.sub("", line).strip() if _BULLET.match(line) else line.strip()
