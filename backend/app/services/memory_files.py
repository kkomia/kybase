"""记忆工作区的**文件层**（v0.14 三期；档案制见 `docs/设计/记忆档案-设计-v0.1.md`）。

这一层现在只做两件事：**列的出**（工作区里有哪些 Markdown、多大、什么时候改的）
与**读得进**（一个文件的原文）。它服务的是档案卡右下角那个「原文」、迁移草稿查看，
以及界面上那份只读的旧记忆。

**检索、切块、图谱、当天索引页已经随档案制退场**（设计 §1.3）：那些机制服务的是
``daily/`` / ``digest/`` 那一层流水（用 BM25 + 分词在几百 KB 里排准序、用 wikilink
把记忆连成图），而记忆的池子现在只有 ``changes.md``（变更流），排序走
``archive.search_changes`` 那套**纯字面判据**（归一化子串 + 二元组覆盖率）。
**旧文件留在磁盘上不动**——它们是用户数据，只是代码不再消费它们。

**全都在本地做，没有第二个进程**（v0.46 起）：文件是我们自己的（``data/memory/``，
见设计文档 §2.2 的数据落点），读取就是把这几份 Markdown 读进来——
没有外部服务可连、没有索引要追。
（原先这一层是 ReMe 的 HTTP 门面，退化的形态见设计文档 §3.5：
即使只想用它的文件操作与 BM25，它也要自己的进程、自己的 venv。）

**为什么直接读本地目录**：

1. 这个目录是**我们自己的**，档案服务早就直接读写它了——经别的进程绕一圈，
   等于"读一个自己的文本文件还得先有另一个进程活着"；
2. 界面是**随时要打开**的东西：记忆没启用、或者用户就是想看看上次记了什么，
   这时页面不该整个空掉。**能看**与**能召回**是两件事，前者不依赖开关；
3. 少一层转发就少一处别人改字段名我们要跟着改的地方。

**这一层不写文件**：文件级写入端点（``PUT``/``DELETE /memory/files/{path}``）
随档案制退场（设计 §6.3、§7.4）——留在那里就是一个绕过预算与变更流的后门。
档案的写入只有 `remember` / `forget` / 界面上的行内编辑这三条正统的路。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.exceptions import InvalidRequestError, NotFoundError

__all__ = [
    "CORE_FILES",
    "CORE_KIND",
    "DAILY_KIND",
    "DIGEST_KIND",
    "MAX_LISTED_FILES",
    "MAX_READ_BYTES",
    "MemoryFile",
    "MemoryFileDetail",
    "WorkspaceStats",
    "classify",
    "describe",
    "parse_frontmatter",
    "read_file",
    "safe_path",
    "scan",
    "stats",
    "to_relative",
]

logger = logging.getLogger(__name__)

#: 文件分类。**只按它在工作区里的位置分**（ReMe 的分层就是靠目录表达的，
#: 见设计文档 §1 的那张表），不去猜正文内容——猜出来的分类用户无法预期。
#:
#: ``daily`` / ``digest`` 这两个分类现在只描述**文件在磁盘上的位置**
#: （旧部署留下的现场与长期知识还在那儿，用户打开列表时看得到它们），
#: 不再对应任何检索或注入行为。
CORE_KIND = "core"
DAILY_KIND = "daily"
DIGEST_KIND = "digest"
OTHER_KIND = "other"

#: 扫描上限。工作区可能被塞进很多东西（``resource/`` 下可能有成百上千个
#: 解析产物），而这是**给人浏览**的列表，不是全量索引——超过就截断并在
#: 返回值里说明，而不是让一次页面请求去遍历一万个文件。
MAX_LISTED_FILES = 2000

#: 读文件的上限（编辑器要把它整个塞进 textarea）。记忆文件该是几 KB 量级，
#: 1 MB 已经远超"一条记忆"的合理体积；超了就截断并标记，而不是拒绝打开。
MAX_READ_BYTES = 1_000_000

#: 顶层目录 → 分类。``memory/`` 是每日现场，``digest/`` 是整理后的长期知识。
#: 两个名字都收（``daily`` 是 ReMe 的默认，``memory`` 是 QwenPaw 那族的写法，
#: 而我们的工作区可能被任一版本初始化过）。
_TOP_LEVEL = {"memory": DAILY_KIND, "daily": DAILY_KIND, "digest": DIGEST_KIND}

#: 这几个目录是**派生物**（原始对话、可重建索引、外部资料），
#: 里面就算有 .md 也不是记忆。列出来只会淹掉真正要改的东西。
_SKIP_DIRS = {"session", "mem_metadata", "mem_session", "resource", ".git", "node_modules"}

#: frontmatter 开头的 ``---`` 块。
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
#: 正文里的第一个一级/二级标题，作为"没有 title 时"的显示名。
_HEADING = re.compile(r"^#{1,2}\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class MemoryFile:
    """列表项。**body 不在这里**——列表可能有上千条，正文按需再读（渐进式）。"""

    path: str
    """相对工作区的路径，**统一用正斜杠**（Windows 的 ``\\`` 写进链接里就点不动了）。"""

    name: str
    title: str
    kind: str
    summary: str = ""
    tags: tuple[str, ...] = ()
    size_bytes: int = 0
    modified_at: str = ""

    @property
    def is_core(self) -> bool:
        return self.kind == CORE_KIND


@dataclass(frozen=True, slots=True)
class MemoryFileDetail:
    """单个文件的正文 + 元信息。``content`` 是**原文**（含 frontmatter），
    这样只读展示是逐字节还原的，不会因为我们"顺手格式化"而丢掉用户的手写内容。"""

    path: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    size_bytes: int = 0
    modified_at: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceStats:
    """工作区的本地状态（``stats``）。界面上"几份文件、上次改动"就是它。"""

    file_count: int = 0
    """工作区里的 Markdown 份数（不含 ``session/`` 那类派生物目录）。"""

    last_changed_at: str = ""
    """最近一次改动的时间（ISO，UTC）。没有索引也就没有"索引时间"，
    如实给"内容最后变更时间"——界面上写的是"上次更新"。"""


# --------------------------------------------------------------------- 路径


def safe_path(workspace: Path, path: str) -> Path:
    """把界面传来的相对路径解析成工作区内的绝对路径，**越界一律拒绝**。

    这是本模块唯一的安全边界：路径由前端给出（用户点的、或者手输的），
    不校验就等于把整个文件系统开放给这个端点（``../../backend/.env`` 一份就够糟）。

    四道检查，缺一不可：非空、非绝对路径、没有 ``..`` 段、解析后仍在工作区内。
    最后一道是**兜底**：前两道挡的是"写出来的坏路径"，而符号链接、Windows 的
    ``C:foo``、以及各种编码花样只有真解析一遍才能确认。
    """
    raw = (path or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise InvalidRequestError("缺少参数：path")
    if not raw.lower().endswith(".md"):
        raise InvalidRequestError("只能读 Markdown 文件（.md）")
    # 冒号单独拦一道：Windows 上 ``C:foo.md`` 是"驱动器相对路径"（会被解析到别处），
    # ``x.md:stream`` 则是 NTFS 的备用数据流——两者都能绕开下面基于 ``..`` 的判断。
    # 合法的记忆文件名里没有冒号，所以直接拒。
    if ":" in raw or any(ord(ch) < 32 for ch in raw):
        raise InvalidRequestError("路径里有非法字符（冒号或控制字符）")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise InvalidRequestError("路径不能包含 ..（不允许跳出记忆工作区）")

    workspace = workspace.resolve()
    target = (workspace / Path(*parts)).resolve()
    # Python 3.9+ 的 is_relative_to；相等也合法（但工作区本身不是文件，下面 read 会拒）
    if target != workspace and not target.is_relative_to(workspace):
        raise InvalidRequestError("路径超出记忆工作区")
    return target


def _relative(workspace: Path, target: Path) -> str:
    return target.resolve().relative_to(workspace.resolve()).as_posix()


def to_relative(workspace: Path, target: Path) -> str:
    """工作区相对路径（POSIX 分隔符）——**给工作区外面的调用方用的那一份**。

    越小的地方各自写一遍 ``relative_to(...).as_posix()``，越容易出现
    "一处 resolve 了、一处没 resolve"这种只在 Windows 上露头的分歧。
    """
    return _relative(workspace, target)


#: 核心文件：住在工作区根下、**靠注入生效**的那几份。
#:
#: 人设四件套（P1 起）：人格、身份、操作规程、长期记忆。它们必须是同一份清单——
#: 少列一个的后果是那份文件被当成普通文件，而用户看到的现象是
#: "我改了它，但助手好像没读到"。
#:
#: ``MEMORY.md`` 仍在这份清单里（它还在磁盘上、还在这个列表里），但它**不再注入**
#: （v0.56 起退场，见 §7.2）——界面按 ``injected`` 那个标记把它显示成"旧记忆（只读）"。
CORE_FILES: tuple[str, ...] = ("MEMORY.md", "SOUL.md", "PROFILE.md", "AGENTS.md")


def classify(path: str) -> str:
    """按位置分类。核心文件按**文件名**判（它们在根下，``head`` 会是空串），
    其余按顶层目录。"""
    if Path(path).name in CORE_FILES:
        return CORE_KIND
    head = path.split("/", 1)[0] if "/" in path else ""
    return _TOP_LEVEL.get(head, OTHER_KIND)


# ----------------------------------------------------------------- 解析纯函数


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 YAML frontmatter 与正文。

    **解析失败不抛错**：frontmatter 是给人看的约定，手写时少个引号很正常，
    而"因为 frontmatter 坏了就打不开这个文件"是最糟的处理——用户正是来修它的。
    退回空 frontmatter、正文原样返回（含那段 ``---``），让他能改。
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        logger.debug("frontmatter 解析失败，按无 frontmatter 处理", exc_info=True)
        return {}, text
    meta = loaded if isinstance(loaded, dict) else {}
    return meta, text[match.end() :]


def _title_of(body: str, meta: dict[str, Any], name: str, *, kind: str) -> str:
    """显示名，优先级：frontmatter.title → 正文首个标题 → 文件名。

    frontmatter 排在标题前面，是因为 ReMe 那族文件用它放"给检索读的一句话"，
    而正文标题可能只是 ``## 当前判断`` 这种片段标题。

    **核心文件例外，一律用文件名**：``MEMORY.md`` 正文里那个
    ``## 核心长期记忆`` 是它的章节名，不是它的名字——把章节名当标题，
    列表里就会出现两个都叫"核心长期记忆"的条目，而用户要找的是 MEMORY.md。
    """
    if kind != CORE_KIND:
        for key in ("title", "name"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        match = _HEADING.search(body)
        if match:
            return match.group(1).strip()
    return name


def _summary_of(meta: dict[str, Any]) -> str:
    value = meta.get("summary")
    return value.strip() if isinstance(value, str) else ""


def _tags_of(meta: dict[str, Any]) -> tuple[str, ...]:
    raw = meta.get("tags")
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(part).strip() for part in raw]
    else:
        return ()
    return tuple(part for part in parts if part)


# --------------------------------------------------------------------- 扫描


def _iter_markdown(workspace: Path) -> list[Path]:
    """遍历工作区里的 .md，跳过派生物目录与隐藏目录。结果**排序**，
    这样列表顺序稳定（否则 os.scandir 的顺序会让界面每次刷新都换一个样子）。"""
    found: list[Path] = []
    if not workspace.is_dir():
        return found
    for root, dirs, files in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.lower().endswith(".md"):
                found.append(Path(root) / name)
        if len(found) >= MAX_LISTED_FILES:
            break
    return found[:MAX_LISTED_FILES]


def _entry_of(workspace: Path, target: Path) -> MemoryFile:
    """一个文件 → 一条列表项。**``scan`` 与 ``describe`` 共用这一处字段装配**：
    两边各拼一份的话，以后加字段必然漏掉一边，而漏掉的那边是"读单个文件"
    ——它只在打开那一个文件时才走到。

    读法也与 ``read_file`` 对齐（按字节读再解码），这样两条路径看到的是同一份文本；
    解码失败用 ``errors="replace"`` 兜住——**语法错误的手工编辑不该让整个列表 500**，
    那个文件本来就该被打开来修。
    """
    raw = target.read_bytes().decode("utf-8", errors="replace")
    stat = target.stat()
    meta, body = parse_frontmatter(raw)
    rel = _relative(workspace, target)
    kind = classify(rel)
    return MemoryFile(
        path=rel,
        name=target.name,
        title=_title_of(body, meta, target.name, kind=kind),
        kind=kind,
        summary=_summary_of(meta),
        tags=_tags_of(meta),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    )


def scan(workspace: Path) -> list[MemoryFile]:
    """列出工作区里所有记忆文件（含解析出的 frontmatter 摘要与标签）。"""
    workspace = workspace.resolve()
    entries: list[MemoryFile] = []
    for target in _iter_markdown(workspace):
        try:
            entries.append(_entry_of(workspace, target))
        except OSError:
            logger.warning("读记忆文件失败：%s", target, exc_info=True)
    return entries


def describe(workspace: Path, path: str) -> MemoryFile:
    """单个文件的列表项（不含正文）。

    **单独有它，是为了不让"读一个文件"去扫整个工作区**：打开一个文件时走这条路，
    而工作区可能有上千个文件。
    """
    workspace = workspace.resolve()
    target = safe_path(workspace, path)
    try:
        return _entry_of(workspace, target)
    except FileNotFoundError as exc:
        raise NotFoundError(f"记忆文件不存在：{path}") from exc
    except OSError as exc:
        raise InvalidRequestError(f"读不了这个文件：{exc}") from exc


# --------------------------------------------------------------------- 读


def read_file(workspace: Path, path: str) -> MemoryFileDetail:
    """读一个文件的原文。文件不存在 → 404（而不是返回空内容）。"""
    target = safe_path(workspace, path)
    try:
        data = target.read_bytes()
    except FileNotFoundError as exc:
        raise NotFoundError(f"记忆文件不存在：{path}") from exc
    except OSError as exc:
        raise InvalidRequestError(f"读不了这个文件：{exc}") from exc

    stat = target.stat()
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    meta, _ = parse_frontmatter(text)
    return MemoryFileDetail(
        path=_relative(workspace.resolve(), target),
        content=text,
        meta=meta,
        truncated=truncated,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    )


# --------------------------------------------------------------------- 统计


def stats(workspace: Path) -> WorkspaceStats:
    """工作区的本地状态：几份文件、最后改动时间。

    没有索引也就没有"索引时间"：读取每次按需扫工作区，所以这里如实给
    "内容最后变更时间"（界面上写"上次更新"）。
    """
    workspace = workspace.resolve()
    targets = _iter_markdown(workspace)
    latest = 0.0
    for target in targets:
        try:
            latest = max(latest, target.stat().st_mtime)
        except OSError:
            logger.warning("记忆文件状态读取失败，跳过：%s", target, exc_info=True)

    return WorkspaceStats(
        file_count=len(targets),
        last_changed_at=datetime.fromtimestamp(latest, tz=UTC).isoformat() if latest else "",
    )
