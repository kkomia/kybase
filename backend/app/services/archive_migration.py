"""记忆档案的**一次性折叠迁移**（期一，见 ``docs/设计/记忆档案-设计-v0.1.md`` §8）。

把旧的两层（``PROFILE.md`` 散文 + ``MEMORY.md`` 条目 + ``digest/`` 长期知识）折成
一份四区档案初稿。四条纪律（§8.4）：

- **只读旧文件、只写新文件**：``MEMORY.md`` / ``digest/**`` / ``daily/**`` 一字不动
  （``PROFILE.md`` 例外，它**就是档案本身**，见 §7.2 与 §10 第 2 条的建议口径）；
- **可重跑**：水位 ``mem_metadata/archive-migration.json`` 记源文件指纹与已折叠条目指纹，
  连跑两次第二次净改动为零（验收判据 §9.2 第 10 条）；
- **默认机械折叠、零模型调用**（§8.3）：按来源归区、逐条原样搬，只做归一化去重与预算裁剪，
  不改写文字。代价是语气不统一，换来的是事实齐全、零成本、可重跑；
- **"模型整理初稿"是可选项、在别处**（§8.3）：本模块只做机械折叠；那一次模型改写是
  用户在界面上点一下才发生的（``MemoryService.organize_draft``，**只给建议**，
  落盘仍然走 ``remember`` 那条正规的路）。所以**这里一次模型都不调**。

折叠来源与落区（§8.1）：

===============  ==========================================  ==========================
来源             落区                                        说明
===============  ==========================================  ==========================
``PROFILE.md``   身份与称呼（偏好拆去偏好区）                它本来就是"对方是谁"
``MEMORY.md``    偏好 / 工具与环境 / 项目段（按内容归）        ``remember`` 写的那一节
``digest/personal/*``  长期偏好与风格                        个人类长期知识
``digest/procedure/*`` 工具与环境 / 项目段（产出规范）         做法类
``digest/wiki/*``      不折叠                                 概念类更可能属于知识库
``daily/**``           不折叠（定级丢弃）                      现场是流水，正是要丢掉的那层
===============  ==========================================  ==========================

丢弃与降级分四类，每类的**条数都进报告**（§8.2）：定级丢弃（``daily`` 整层）、
记录丢弃（敏感形状，**只报条数、不留明细**）、降级不丢（超长条目进 ``import-draft.md``）、
裁剪不丢（超预算的按"项目段 > 偏好（含雷点）> 身份 > 工具"保留，落选的进草稿）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.services import archive_files as af
from app.services.archive import (
    CHANGELOG_KEEP,
    SECTION_IDENTITY,
    SECTION_PREFERENCES,
    SECTION_PROJECTS,
    SECTION_TOOLS,
    SINGLE_ENTRY_CHARS,
    SOURCE_MIGRATION,
    ArchiveEntry,
    classify_action,
    is_sensitive,
    overrun,
)

__all__ = [
    "DIGEST_BUCKETS",
    "DRAFT_FILENAME",
    "MEMORY_FILENAME",
    "MigrationDraft",
    "MigrationReport",
    "apply_draft",
    "classify_text",
    "collect",
    "read_watermark",
    "run_migration",
    "source_fingerprints",
]

#: 旧文件。``MEMORY.md`` 折叠；``PROFILE.md`` 是档案目的地（读它的散文、写回四区）。
MEMORY_FILENAME = "MEMORY.md"

#: ``digest/`` 下的三个桶（照旧实现的 ``digest/{personal,procedure,wiki}``）。
DIGEST_BUCKETS = ("personal", "procedure", "wiki")

#: 迁移草稿文件名（与 :data:`archive_files.IMPORT_DRAFT_FILENAME` 同源）。
DRAFT_FILENAME = af.IMPORT_DRAFT_FILENAME

#: 裁剪优先级（§8.2 第 4 类）：数字越大越先保。
SECTION_PRIORITY: dict[str, int] = {
    SECTION_PROJECTS: 4,
    SECTION_PREFERENCES: 3,
    SECTION_IDENTITY: 2,
    SECTION_TOOLS: 1,
}

#: 归区用的词表。**它是启发式，不是判据**：机械折叠是可重跑的初稿，
#: 用户与模型的整理都可以改它（模型那一次只给建议，落盘走 `remember`）。
#: 命中计分、取分最高的一区；全零时落到偏好区——"长期事实"最像的默认位置。
_PROJECT_WORDS = (
    "项目",
    "目标",
    "交付",
    "需求",
    "方案",
    "计划",
    "在做",
    "正在做",
    "约束",
    "决定",
    "已经定",
    "里程碑",
    "排期",
    "上线",
    "评审",
    "背景",
)
_PREFERENCE_WORDS = (
    "偏好",
    "喜欢",
    "习惯",
    "风格",
    "要求",
    "不要",
    "不爱",
    "不写",
    "讨厌",
    "语气",
    "格式",
    "排版",
    "先给",
    "简短",
    "详细",
    "客套",
    "雷点",
    "忌讳",
    "必须",
    "尽量",
)
_TOOL_WORDS = (
    "工具",
    "环境",
    "机器",
    "系统",
    "路径",
    "目录",
    "服务器",
    "显卡",
    "显存",
    "部署",
    "版本",
    "软件",
    "命令行",
    "设备",
    "模型",
    "插件",
    "依赖",
    "python",
    "node",
    "docker",
    "windows",
    "linux",
    "mac",
    "nas",
    "内网",
)
_IDENTITY_WORDS = (
    "叫",
    "称呼",
    "名字",
    "代词",
    "语言",
    "中文",
    "英文",
    "角色",
    "身份",
    "单位",
    "团队",
    "公司",
    "所在",
)

#: ``名字：`` / ``**代词：**`` 这类"只有标签没有值"的行（模板占位）。
_LABEL_ONLY = re.compile(r"^\*{0,2}[^：:*]{1,15}[：:]\*{0,2}$")


def classify_text(text: str, *, heading: str = "", default: str = SECTION_PREFERENCES) -> str:
    """按内容把一条旧条目归到某一区（启发式，见词表说明）。

    计分取最高；同分时按词表顺序（项目 > 偏好 > 工具 > 身份）定。标题也参与计分——
    ``MEMORY.md`` 的 ``## 工具设置`` 就是最直接的线索。
    """
    hay = f"{heading} {text}".casefold()
    best = default
    best_score = 0
    for section, words in (
        (SECTION_PROJECTS, _PROJECT_WORDS),
        (SECTION_PREFERENCES, _PREFERENCE_WORDS),
        (SECTION_TOOLS, _TOOL_WORDS),
        (SECTION_IDENTITY, _IDENTITY_WORDS),
    ):
        score = sum(1 for word in words if word in hay)
        if score > best_score:
            best, best_score = section, score
    return best


# --------------------------------------------------------------------- 草稿


@dataclass(frozen=True, slots=True)
class MigrationDraft:
    """机械折叠出的候选（**模型整理初稿那一步的接口位**，见模块头）。"""

    candidates: tuple[ArchiveEntry, ...] = ()
    """已按裁剪优先级排好序（项目段在前、工具在后），同一来源内保持原顺序。"""

    dropped_long: tuple[ArchiveEntry, ...] = ()
    """超过单条上限、进 ``import-draft.md`` 的（降级不丢）。"""

    dropped_sensitive: int = 0
    """命中敏感形状、直接丢的**条数**（只报数，不留明细）。"""

    sources: tuple[tuple[str, str], ...] = ()
    """源文件相对路径 → sha256（水位里的那一份，重跑不重复搬）。"""

    per_source: tuple[tuple[str, int], ...] = ()
    """每个来源折叠出多少条（迁移报告用）。"""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """执行一次迁移之后的计数（§8.4 的"迁移报告"）。"""

    added: int = 0
    replaced: int = 0
    existing: int = 0
    dropped_sensitive: int = 0
    downgraded: int = 0
    trimmed: int = 0
    skipped: bool = False
    archive_changed: bool = False
    per_source: tuple[tuple[str, int], ...] = ()
    draft_entries: int = 0


# --------------------------------------------------------------------- 采集


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_paths(workspace: Path) -> list[Path]:
    """要折叠的源文件：``MEMORY.md`` + ``digest/{personal,procedure}/*.md``。

    ``digest/wiki/*`` 与 ``daily/**`` **不在这里**：前者交用户逐条决定，后者是流水。
    它们一个字节都不会被碰——所以"旧文件 md5 前后一致"这条判据对它们天然成立。
    """
    found: list[Path] = []
    memory = workspace / MEMORY_FILENAME
    if memory.is_file():
        found.append(memory)
    for bucket in ("personal", "procedure"):
        directory = workspace / "digest" / bucket
        if directory.is_dir():
            found.extend(sorted(directory.glob("*.md")))
    return found


def source_fingerprints(workspace: Path) -> dict[str, str]:
    """源文件指纹（相对路径 → sha256）。"""
    workspace = Path(workspace)
    out: dict[str, str] = {}
    for path in _source_paths(workspace):
        out[path.relative_to(workspace).as_posix()] = _sha256(path)
    return out


def _label_only(text: str) -> bool:
    """模板里"只有标签没有值"的行（``- **名字：**``），不是条目。"""
    body = text.strip("*_ ").strip()
    if not body:
        return True
    return bool(_LABEL_ONLY.match(body))


def _profile_entries(text: str) -> list[tuple[str, str]]:
    """``PROFILE.md`` 散文 → ``(分区, 条目)``（偏好拆去偏好区，§8.1）。"""
    out: list[tuple[str, str]] = []
    for raw in af.body_text(text).splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "<!--", ">", "|", "```")):
            continue
        # 模板里的引导斜体（``*（挑个你喜欢的）*``）：是说明，不是内容。
        if line.startswith(("*", "_")):
            continue
        cleaned = af.strip_bullet(line).strip("*_ ").strip()
        # 模板用 ``**标签：** 值`` 写成一条；去掉粗体标记只是**归一化**，
        # 不改变它说的是什么（机械折叠的允许动作，§8.3）。
        cleaned = cleaned.replace("**", "").strip()
        if len(cleaned) < 4 or _label_only(cleaned):
            continue
        out.append((classify_text(cleaned, default=SECTION_IDENTITY), cleaned))
    return out


def _paragraph_entries(text: str) -> list[str]:
    """没有 ``- `` 条目的文件退化成"逐行散文"（digest 文件两种形状都有）。"""
    out: list[str] = []
    for raw in af.body_text(text).splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "<!--", ">", "|", "```", "*", "_")):
            continue
        out.append(line)
    return out


def _digest_candidates(workspace: Path, bucket: str) -> list[tuple[ArchiveEntry, str]]:
    """一个 ``digest/`` 桶 → 候选 ``(条目, 来源相对路径)``。"""
    directory = workspace / "digest" / bucket
    if not directory.is_dir():
        return []
    out: list[tuple[ArchiveEntry, str]] = []
    for path in sorted(directory.glob("*.md")):
        rel = path.relative_to(workspace).as_posix()
        text = af.read_text(path)
        items = af.bullet_lines(text) or [("", item) for item in _paragraph_entries(text)]
        for heading, item in items:
            clean = af.normalize_entry(item)
            if not clean:
                continue
            if bucket == "personal":
                section, group = SECTION_PREFERENCES, ""
            else:
                section = classify_text(clean, heading=heading, default=SECTION_TOOLS)
                if section not in (SECTION_TOOLS, SECTION_PROJECTS):
                    section = SECTION_TOOLS
                group = "产出规范" if section == SECTION_PROJECTS else ""
            out.append((ArchiveEntry(text=clean, section=section, group=group), rel))
    return out


def collect(workspace: Path) -> MigrationDraft:
    """把旧文件机械折叠成一份候选草稿（**零模型调用**）。"""
    workspace = Path(workspace)
    raw: list[tuple[ArchiveEntry, str]] = []

    # **第一次迁移时才有"散文源"**：迁完之后 PROFILE.md 已经就是档案本身，
    # 再把它当旧散文折一遍是白做功（而且会把它自己的条目算进报告）。
    profile = workspace / af.ARCHIVE_FILENAME
    if profile.is_file() and not read_watermark(workspace).get("version"):
        for section, text in _profile_entries(af.read_text(profile)):
            raw.append(
                (
                    ArchiveEntry(text=af.normalize_entry(text), section=section),
                    af.ARCHIVE_FILENAME,
                )
            )

    memory = workspace / MEMORY_FILENAME
    if memory.is_file():
        for heading, item in af.bullet_lines(af.read_text(memory)):
            clean = af.normalize_entry(item)
            if clean:
                raw.append(
                    (
                        ArchiveEntry(text=clean, section=classify_text(clean, heading=heading)),
                        MEMORY_FILENAME,
                    )
                )

    raw.extend(_digest_candidates(workspace, "personal"))
    raw.extend(_digest_candidates(workspace, "procedure"))

    dropped_sensitive = 0
    dropped_long: list[ArchiveEntry] = []
    seen: set[str] = set()
    ranked: list[tuple[int, int, ArchiveEntry, str]] = []
    for order, (entry, source) in enumerate(raw):
        if is_sensitive(entry.text):
            dropped_sensitive += 1
            continue
        if len(entry.text) > SINGLE_ENTRY_CHARS:
            dropped_long.append(entry)
            continue
        key = af.fingerprint(entry.text)
        if key in seen:
            continue
        seen.add(key)
        priority = SECTION_PRIORITY.get(entry.section, SECTION_PRIORITY[SECTION_PREFERENCES])
        ranked.append((priority, order, entry, source))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    per_source: dict[str, int] = {}
    for _, _, _, source in ranked:
        per_source[source] = per_source.get(source, 0) + 1
    return MigrationDraft(
        candidates=tuple(entry for _, _, entry, _ in ranked),
        dropped_long=tuple(dropped_long),
        dropped_sensitive=dropped_sensitive,
        sources=tuple(sorted(source_fingerprints(workspace).items())),
        per_source=tuple(sorted(per_source.items())),
    )


# --------------------------------------------------------------------- 落盘


def _watermark_path(workspace: Path) -> Path:
    return workspace / af.MIGRATION_WATERMARK_PATH


def read_watermark(workspace: Path) -> dict[str, object]:
    """读水位；坏文件按"没有水位"处理（宁可重折一遍，也不要因为坏 JSON 卡死）。"""
    try:
        raw = _watermark_path(Path(workspace)).read_bytes().decode("utf-8")
        loaded = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_watermark(workspace: Path, payload: dict[str, object]) -> None:
    af.write_bytes(
        _watermark_path(Path(workspace)),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _render_draft(losers: list[ArchiveEntry], updated: str) -> str:
    """``import-draft.md``：人可读，界面提示"还有 N 条旧条目没进档案"（§8.2 第 3、4 类）。"""
    lines = [
        "---",
        f"updated: {updated}",
        "source: archive-migration",
        "---",
        "",
        "# 迁移草稿",
        "",
    ]
    lines.extend(f"- {entry.text}" for entry in losers)
    return "\n".join(lines).rstrip("\n") + "\n"


def apply_draft(
    workspace: Path, draft: MigrationDraft, *, now: Callable[[], datetime] | None = None
) -> MigrationReport:
    """把草稿落成档案、变更流、草稿文件与水位（只 add / 只顶替自己搬进去的）。

    与 :func:`collect` 之间就是"模型整理初稿"那一步的接口位（模块头）；
    本期调用方只有 :func:`run_migration`，传的是未经整理的机械草稿。
    """
    workspace = Path(workspace)
    clock = now or (lambda: datetime.now().astimezone())
    stamp = clock()
    updated = stamp.date().isoformat()
    at = stamp.strftime("%Y-%m-%d %H:%M")

    archive = af.read_archive(workspace)
    watermark = read_watermark(workspace)
    # **第一次迁移时 PROFILE.md 还是旧散文**，不能把它的 `## 身份` / `## 用户资料`
    # 当成"已有的档案条目与分区"——那些是折叠的来源，不是目的地。水位存在（``version``）
    # 才说明这份 PROFILE.md 已经是档案，此时的条目与分区都要原样保留（用户可能改过）。
    migrated = bool(watermark.get("version"))
    start = list(archive.entries) if migrated else []
    entries = list(start)
    owned = {str(item) for item in watermark.get("folded", []) if isinstance(item, str)}

    records: list[af.ChangeRecord] = []
    trimmed: list[ArchiveEntry] = []
    added = replaced = existing = 0
    for entry in draft.candidates:
        match = classify_action(entry.text, entries)
        if match.action == "existing":
            existing += 1
            continue
        if match.action == "replace" and match.index >= 0:
            target = entries[match.index]
            if af.fingerprint(target.text) not in owned:
                # 命中用户自己写的条目：**不覆盖**（§8.4：只顶替它自己搬进去的）。
                existing += 1
                continue
            candidate_list = list(entries)
            candidate_list.pop(match.index)
            candidate_list.append(entry)
            if overrun(candidate_list) is not None:
                trimmed.append(entry)
                continue
            entries = candidate_list
            replaced += 1
            owned.add(af.fingerprint(entry.text))
            records.append(
                af.ChangeRecord(
                    at=at,
                    action=af.ACTION_REPLACED,
                    section=entry.section,
                    source=SOURCE_MIGRATION,
                    old=target.text,
                    new=entry.text,
                )
            )
            continue
        candidate_list = [*entries, entry]
        if overrun(candidate_list) is not None:
            trimmed.append(entry)
            continue
        entries = candidate_list
        added += 1
        owned.add(af.fingerprint(entry.text))
        records.append(
            af.ChangeRecord(
                at=at,
                action=af.ACTION_ADDED,
                section=entry.section,
                source=SOURCE_MIGRATION,
                new=entry.text,
            )
        )

    changed = entries != start
    if changed or not (workspace / af.ARCHIVE_FILENAME).exists():
        af.write_archive(
            workspace,
            af.Archive(
                entries=tuple(entries),
                updated=updated,
                unknown_sections=archive.unknown_sections if migrated else (),
            ),
        )
    if records:
        af.append_changes(workspace, records, keep=CHANGELOG_KEEP)

    losers = [*draft.dropped_long, *trimmed]
    if losers:
        af.write_bytes(workspace / DRAFT_FILENAME, _render_draft(losers, updated))

    report = MigrationReport(
        added=added,
        replaced=replaced,
        existing=existing,
        dropped_sensitive=draft.dropped_sensitive,
        downgraded=len(draft.dropped_long),
        trimmed=len(trimmed),
        archive_changed=changed,
        per_source=draft.per_source,
        draft_entries=len(losers),
    )
    _write_watermark(
        workspace,
        {
            "version": 1,
            "at": stamp.isoformat(),
            "sources": dict(draft.sources),
            "folded": sorted(owned),
            "report": {
                "added": report.added,
                "replaced": report.replaced,
                "existing": report.existing,
                "dropped_sensitive": report.dropped_sensitive,
                "downgraded": report.downgraded,
                "trimmed": report.trimmed,
            },
        },
    )
    return report


def run_migration(workspace: Path, *, now: Callable[[], datetime] | None = None) -> MigrationReport:
    """跑一遍折叠迁移（幂等：源文件指纹没变且档案已存在时直接跳过）。"""
    workspace = Path(workspace)
    watermark = read_watermark(workspace)
    current = source_fingerprints(workspace)
    stored = watermark.get("sources")
    archive_exists = (workspace / af.ARCHIVE_FILENAME).exists()
    if isinstance(stored, dict) and stored == current and archive_exists:
        previous = watermark.get("report")
        counts = previous if isinstance(previous, dict) else {}
        return MigrationReport(
            added=int(counts.get("added", 0) or 0),
            replaced=int(counts.get("replaced", 0) or 0),
            existing=int(counts.get("existing", 0) or 0),
            dropped_sensitive=int(counts.get("dropped_sensitive", 0) or 0),
            downgraded=int(counts.get("downgraded", 0) or 0),
            trimmed=int(counts.get("trimmed", 0) or 0),
            skipped=True,
        )

    draft = collect(workspace)
    return apply_draft(workspace, draft, now=now)
