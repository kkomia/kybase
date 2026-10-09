"""Agent 的文件面：两个根、三种动作（列 / 读 / 搜）。

**为什么补这一层**：模型此前**能写不能读**——导出 docx / xlsx / pptx / pdf 都行、
上传入库也行，但工作区里已有的那个文件它看不见；唯一的路是先把文件"入库"再检索，
而"我就是想让你看看这个文件"不该先过一次解析与向量化。这一层补上**只读**的那半边。

两个根（与 ``services/sandbox.py`` 的区分一一对应）：

| 根 | 在哪 | 里面是什么 |
| --- | --- | --- |
| **工作区** | 会话挂的那个工作区目录（``workspaces.root_path``） | 用户真实的项目文件 |
| **沙箱** | ``data/sandbox/<conversation_id>/`` | 它自己跑命令时产生的临时文件 |

**两个根都过 ``sandbox.resolve_in``**：路径必须相对、不许 ``..``、不许碰
``.env`` / ``.ssh`` / 私钥这类凭据文件、解析之后必须落在根之内。模型写的路径是
"它推测这个项目的结构"，会写出 ``../../backend/.env``——**不是恶意，是它在猜**，
所以这条判定一次都不能省（见 ``resolve_in`` 的四道检查）。

**这里没有任何写接口**，这是刻意的：写由两条已有的路承担——``export_*``（产出新文件）
与 ``run_command``（执行）。把读写混在一个工具里，等于开出一条我们没设计过、
也没审过的写路径。

**为什么不给外部 MCP 客户端**：工作区是**会话级**的概念（哪条会话挂了哪个工作区），
而外部门没有会话上下文——给它两个凭据不明的根，等于让它读一份它说不清是谁的目录。
这条与技能工具同一条理由（见 ``agent_tools`` 模块头）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.caller import Caller
from app.core.exceptions import InvalidRequestError
from app.services.sandbox import resolve_in, sandbox_for

if TYPE_CHECKING:
    # 只为类型标注：**运行时不导入**，免得与组合根（core.services）成环
    from app.core.services import Services

__all__ = [
    "MAX_READ_CHARS",
    "MAX_READ_LINES",
    "Roots",
    "describe_roots",
    "list_files",
    "read_file",
    "resolve_roots",
    "search_files",
]

logger = logging.getLogger(__name__)

WHERE_WORKSPACE = "workspace"
WHERE_SANDBOX = "sandbox"

#: 一看扩展名就知道**读不成文本**的那些（D36，2026-09-28 走查）。
#:
#: 为什么光看 NUL 不够：**PDF 这类文件的头部没有 NUL**（实测那个 397 B 的样例：
#: 前 4KB 里 0 个 NUL，还能按 UTF-8 解出来），于是它被当成文本，"读"出来是
#: `1: %PDF-1.4 …` 这种原始字节——模型会把它当内容接着猜。
#:
#: 这是**黑名单**，不是文本白名单：`.log` / `.yaml` / `.toml` / `.sql` / `Dockerfile`
#: 这类自造或小众文本格式一个都不在里面，所以不会出现"把能读的文件判成二进制"。
BINARY_SUFFIXES = frozenset(
    {
        # 文档
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        # 图片
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".tif",
        ".tiff",
        ".heic",
        # 压缩包
        ".zip",
        ".gz",
        ".tar",
        ".7z",
        ".rar",
        ".xz",
        ".bz2",
        # 音视频
        ".mp3",
        ".wav",
        ".flac",
        ".ogg",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        # 字体
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        # 二进制产物 / 可执行
        ".pyc",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".class",
        ".jar",
        ".wasm",
        ".dylib",
        ".sqlite",
        ".db",
    }
)

#: 文件头（魔数）：没有扩展名、或扩展名被改过时兜底。顺序无所谓，逐条比。
BINARY_MAGIC = (
    b"%PDF-",
    b"PK\x03\x04",  # zip / docx / xlsx / pptx
    b"PK\x05\x06",  # 空 zip
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",  # jpeg
    b"GIF87a",
    b"GIF89a",
    b"\x1f\x8b",  # gzip
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"OggS",
    b"RIFF",  # wav / avi / webp
    b"\xd0\xcf\x11\xe0",  # 老式 Office（doc/xls/ppt）
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",  # java class
    b"SQLite format 3\x00",
    b"\x00asm",  # wasm
    b"wOFF",
    b"wOF2",
)


def looks_binary_name(name: str) -> bool:
    """按**名字**（扩展名）判二进制。

    上传件那条路只有名字与字节（没有真实路径），所以这一条得单独能调——
    `agent_tools` 读会话文件时就先问它（D36）。
    """
    return Path(name).suffix.lower() in BINARY_SUFFIXES


def looks_binary_bytes(content: bytes) -> bool:
    """按**字节**判二进制：魔数命中、或前 4KB 里有 NUL。

    **不做 UTF-8 解码**——那是"到底能不能当文本读"的最后一步，在
    `agent_tools._text_or_none` 那一侧（它多一条 `UnicodeDecodeError` 的判据）。
    """
    if content.startswith(BINARY_MAGIC):
        return True
    return b"\x00" in content[:4096]


#: 一次最多列多少条。与其它列表类工具同一量级（见 ``tools.MAX_DOC_PAGE``）：
#: 再多就是拿上下文换"我全都要"，而模型真正需要的通常是"这里有什么"。
MAX_LIST_ENTRIES = 200

#: 一次最多读多少行 / 多少字符。**分行分页**是刻意的（而不是按字节截断）：
#: 截断在半个字中间会让内容看起来像乱码，而"还剩 N 行、用 offset 接着读"
#: 是模型能自己执行的下一步。
MAX_READ_LINES = 2000
DEFAULT_READ_LINES = 400
MAX_READ_CHARS = 60000

#: 搜索的上限：扫多少文件、回多少条命中、单个文件读多大。
#: 三条都有存在的理由——一个没被 ignore 的 ``node_modules`` 能让"搜一遍"变成
#: 几万次文件读，而那是几十秒的纯等待。
MAX_SEARCH_FILES = 2000
MAX_SEARCH_HITS = 60
MAX_SEARCH_BYTES = 512 * 1024

#: 列目录时**默认跳过**的目录。它们要么是依赖、要么是缓存、要么是构建产物：
#: 内容多、对"这个项目里有什么"几乎没有信息量，而列出来会把真正的文件挤掉。
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
    }
)


@dataclass(frozen=True, slots=True)
class Roots:
    """这一轮能读的两个根。``workspace`` 为 ``None`` = 这条会话没挂工作区。"""

    workspace: Path | None
    sandbox: Path

    def pick(self, where: str | None) -> tuple[str, Path]:
        """把 ``where`` 参数解析成 ``(名字, 根)``。

        **不给 ``where`` 时**：有工作区就用工作区（绝大多数"看看这个文件"指的是
        他自己的项目），没有工作区就用沙箱。写死成"默认工作区"不行——没挂工作区的
        会话会白撞一次报错，而那一轮里模型本来只想看自己刚才跑出来的日志。
        """
        if where in (None, "", "auto"):
            return (
                (WHERE_WORKSPACE, self.workspace)
                if self.workspace
                else (WHERE_SANDBOX, self.sandbox)
            )
        key = str(where).strip().lower()
        if key == WHERE_WORKSPACE:
            if self.workspace is None:
                raise InvalidRequestError(
                    "这条会话没有挂工作区（工作区在「工作区」页里挂）。"
                    '要看试错目录就用 where="sandbox"'
                )
            return WHERE_WORKSPACE, self.workspace
        if key == WHERE_SANDBOX:
            return WHERE_SANDBOX, self.sandbox
        raise InvalidRequestError(f"where 只能是 workspace 或 sandbox，收到：{where}")


def resolve_roots(services: Services, *, conversation_id: str | None, caller: Caller) -> Roots:
    """这次会话的两个根。

    工作区**按会话取**（不是按调用者随便挑一个）：会话挂到哪个工作区，
    这一轮的文件面就是哪儿——"在这个项目里干活"是从会话建立时定下的事。

    取不到工作区（没挂 / 工作区已被删 / 越权看不到）时**不是错误**，
    只是没有那个根：沙箱始终可用，而"没挂工作区"本身是要如实告诉模型的一件事
    （它会据此改用 sandbox，而不是反复撞同一个错）。
    """
    # ``.ensure()` 不是顺手加的：``sandbox_for`` 返回的是 ``Sandbox`` 数据类，
    # 而 ``Roots.sandbox`` 的类型是 ``Path``（后面 ``list_files`` / ``read_file``
    # 直接拿它调 ``.exists()`` / ``.iterdir()``）。原先这里漏了 ``.ensure()``，
    # 于是没挂工作区的会话里 ``list_files`` 必抛
    # ``AttributeError: 'Sandbox' object has no attribute 'exists'``
    # （2026-09-28 走查 D33 实测），而那条异常又被工具层原样写进了上下文。
    # 另外两处取沙箱的地方（`api/v1/sandbox.py`、`agent_exec.py`）一直都是
    # ``sandbox_for(...).ensure()``——同一个东西只有一种取法。
    box = sandbox_for(services.runtime.data_dir, conversation_id or "adhoc").ensure()
    workspace: Path | None = None
    if conversation_id:
        try:
            record = services.conversations.get(conversation_id)
            if record.workspace_id:
                # **设备维度不过滤**（``any_device=True``，v0.59）：这里拿的是"这条
                # 会话挂着的项目"的根，不是请求参数指向的工作区——文件面（Agent 干活
                # 的地方）里的路径本来就该按记录走，设备隔离管的是界面上看得见哪些项目
                workspace = Path(
                    services.workspaces.get(
                        record.workspace_id, user_id=caller.owner_id, any_device=True
                    ).root_path
                )
        except Exception:
            # 会话或工作区读不到时降级成"只有沙箱"：文件面少一个根，
            # 而整轮对话不该因此起不来
            logger.info("取会话工作区失败，本轮只有沙箱根：%s", conversation_id, exc_info=True)
    return Roots(workspace=workspace, sandbox=box)


def describe_roots(roots: Roots) -> str:
    """给模型看的"你在哪儿"。**绝对路径必须给**：它下一步要让 ``run_command``
    去碰这些文件，而命令是在沙箱目录里起的（cwd = 沙箱），它得知道绝对路径才走得过去。
    """
    lines = [f"沙箱目录（命令的工作目录）：{roots.sandbox}"]
    if roots.workspace is not None:
        lines.append(f"工作区目录（用户的真实项目）：{roots.workspace}")
    else:
        lines.append("这条会话没有挂工作区，只有沙箱可用。")
    return "\n".join(lines)


# --------------------------------------------------------------------- 列


def list_files(
    roots: Roots,
    *,
    where: str | None = None,
    path: str = "",
    pattern: str = "",
    limit: int = MAX_LIST_ENTRIES,
) -> dict[str, object]:
    """列一个目录。默认跳过依赖与缓存目录（见 ``_IGNORED_DIRS``）。"""
    label, root = roots.pick(where)
    target = resolve_in(root, path) if path else root
    if not target.exists():
        raise InvalidRequestError(f"没有这个目录：{path or '.'}")
    if not target.is_dir():
        raise InvalidRequestError(f"这不是目录：{path}（读文件用 read_file）")

    matcher = _matcher(pattern)
    entries: list[dict[str, object]] = []
    ignored = 0
    for item in sorted(target.iterdir(), key=_sort_key):
        if item.is_dir() and item.name in _IGNORED_DIRS:
            ignored += 1
            continue
        if matcher is not None and not matcher(item.name):
            continue
        entries.append(_entry(root, item))

    clipped = len(entries) > limit
    return {
        "where": label,
        "path": path or ".",
        "total": len(entries),
        "entries": entries[:limit],
        "skipped_dirs": ignored,
        "note": _join_notes(
            [
                f"只列了前 {limit} 条（共 {len(entries)} 条）" if clipped else "",
                f"跳过了 {ignored} 个依赖/缓存目录（要列它们请直接把 path 指进去）"
                if ignored
                else "",
                describe_roots(roots),
            ]
        ),
    }


def _entry(root: Path, item: Path) -> dict[str, object]:
    try:
        stat = item.stat()
        size: int | None = stat.st_size
        modified: datetime | None = datetime.fromtimestamp(stat.st_mtime)
    except OSError:  # 并发删除、权限不足
        size, modified = None, None
    return {
        # 相对路径：模型拿到的这个字符串可以直接回传给 read_file / run_command
        "path": item.relative_to(root).as_posix(),
        "type": "dir" if item.is_dir() else "file",
        "size_bytes": size,
        "modified_at": modified.isoformat(timespec="seconds") if modified else None,
    }


# --------------------------------------------------------------------- 读


def read_file(
    roots: Roots,
    *,
    where: str | None = None,
    path: str,
    offset: int = 1,
    limit: int = DEFAULT_READ_LINES,
) -> dict[str, object]:
    """读一个文本文件（按行分页）。

    ``offset`` 是**行号**（从 1 开始），与用户在编辑器里看到的行号一致——
    模型引用"第 42 行有问题"时，两边说的必须是同一种数字。
    """
    label, root = roots.pick(where)
    target = resolve_in(root, path)
    if not target.exists():
        raise InvalidRequestError(f"没有这个文件：{path}")
    if target.is_dir():
        raise InvalidRequestError(f"这是一个目录：{path}（列目录用 list_files）")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise InvalidRequestError(f"读不了这个文件：{exc}") from exc
    if _looks_binary(target):
        raise InvalidRequestError(
            f"{path} 看起来是二进制文件（图片 / 压缩包 / Office 文档）。"
            "要读它的内容，把它加进知识库再检索；要看它的元信息，用 run_command 跑 file / ls"
        )

    start = max(1, int(offset))
    count = max(1, min(MAX_READ_LINES, int(limit)))
    lines: list[str] = []
    total_lines = 0
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                total_lines = index
                if index < start:
                    continue
                if len(lines) < count:
                    lines.append(line.rstrip("\n"))
    except OSError as exc:
        raise InvalidRequestError(f"读不了这个文件：{exc}") from exc

    text = "\n".join(lines)
    clipped_chars = len(text) > MAX_READ_CHARS
    if clipped_chars:
        text = text[:MAX_READ_CHARS]

    end = start + len(lines) - 1 if lines else start - 1
    more_lines = total_lines > end
    # 这一条**不带 describe_roots**：读文件时模型已经知道自己在读哪个根，
    # 每一条结果都附两行路径是白花的上下文（`list_files` 与 `run_command` 才需要它）
    notes: list[str] = []
    if more_lines:
        notes.append(
            f"这个文件共 {total_lines} 行，上面是第 {start}–{end} 行；接着读用 offset={end + 1}"
        )
    if clipped_chars:
        notes.append(f"这一页超过 {MAX_READ_CHARS} 字，已截断——把 limit 调小、按页读")
    return {
        "where": label,
        "path": path,
        "size_bytes": size,
        "offset": start,
        "lines": len(lines),
        "total_lines": total_lines,
        "text": text,
        "note": _join_notes(notes),
    }


def read_bytes(
    roots: Roots,
    *,
    where: str | None = None,
    path: str,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    """读一份文件的**原始字节**与文件名（v0.55）：给"把它加进知识库"那条路用。

    与 :func:`read_file` 共用同一套根解析与越界闸（``pick`` + ``resolve_in``），
    差别只有一处，而那一处是刻意的：**它不判文本还是二进制**。入库这条路上二进制
    才是常态（PDF / 图片 / Office），而 ``read_file`` 是给模型"看内容"的，二进制必须拦
    ——少了这道拦，一屏乱码会把上下文吃掉。

    ``max_bytes`` 由调用方给（摄入那条路有它自己的上限，见 ``ingest``）：
    在这里只做一次"先看大小再读"，免得把一个几百 MB 的文件整个读进内存。
    """
    _, root = roots.pick(where)
    target = resolve_in(root, path)
    if not target.exists():
        raise InvalidRequestError(f"没有这个文件：{path}")
    if target.is_dir():
        raise InvalidRequestError(f"这是一个目录：{path}（列目录用 list_files）")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise InvalidRequestError(f"读不了这个文件：{exc}") from exc
    if max_bytes is not None and size > max_bytes:
        raise InvalidRequestError(
            f"文件太大（{size} 字节，上限 {max_bytes} 字节）。"
            "更大的文件请在界面上传，或者先用 run_command 把它拆小"
        )
    try:
        return target.read_bytes(), target.name
    except OSError as exc:
        raise InvalidRequestError(f"读不了这个文件：{exc}") from exc


def _looks_binary(path: Path) -> bool:
    """这个文件能不能当文本读？**先看名字，再看字节**（D36）。

    为什么两条都要：
    - **名字**挡住"确实叫 .pdf / .png"的（绝大多数）——它们的内容可能不含 NUL，
      光看字节会放过去（走查实测：397 B 的 PDF 前 4KB 里 0 个 NUL）；
    - **字节**（魔数 + NUL）挡住"改了名 / 没有扩展名"的。

    判据是**黑名单**，不会误伤 `.log` / `.yaml` / `.toml` / `.sql` 这类自造文本格式
    （它们不在 `BINARY_SUFFIXES` 里），所以"把能读的文件判成二进制"这种反向错误
    仍然不会发生——这正是当初只敢看 NUL 的那个顾虑。
    """
    if looks_binary_name(path.name):
        return True
    try:
        with path.open("rb") as handle:
            return looks_binary_bytes(handle.read(4096))
    except OSError:
        return False


# --------------------------------------------------------------------- 搜


def search_files(
    roots: Roots,
    *,
    where: str | None = None,
    path: str = "",
    pattern: str,
    ignore_case: bool = True,
    limit: int = MAX_SEARCH_HITS,
) -> dict[str, object]:
    """在工作区里按**正则**搜内容，回"哪个文件第几行是什么"。

    为什么值得单独一个工具（而不是"让模型自己 list + read"）：一个几十个文件的目录，
    逐个读进去是几十次往返、几十份文件内容进上下文；而它通常只想回答
    "这个函数在哪儿定义的"。一次搜完是这一层里省得最多的一步。
    """
    text = (pattern or "").strip()
    if not text:
        raise InvalidRequestError("缺少参数：pattern（要搜的内容，支持正则）")
    if len(text) > 200:
        raise InvalidRequestError("pattern 太长了（上限 200 字符）——搜的是关键词，不是一段话")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(text, flags)
    except re.error as exc:
        raise InvalidRequestError(f"pattern 不是合法的正则：{exc}") from exc

    label, root = roots.pick(where)
    base = resolve_in(root, path) if path else root
    if not base.exists():
        raise InvalidRequestError(f"没有这个目录：{path or '.'}")

    max_hits = max(1, min(MAX_SEARCH_HITS, int(limit)))
    hits: list[dict[str, object]] = []
    scanned = 0
    skipped_big = 0
    truncated = False
    for item in _walk(base):
        if len(hits) >= max_hits:
            truncated = True
            break
        if scanned >= MAX_SEARCH_FILES:
            truncated = True
            break
        scanned += 1
        try:
            if item.stat().st_size > MAX_SEARCH_BYTES:
                skipped_big += 1
                continue
        except OSError:
            continue
        if _looks_binary(item):
            continue
        try:
            with item.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if regex.search(line):
                        hits.append(
                            {
                                "path": item.relative_to(root).as_posix(),
                                "line": number,
                                "text": line.rstrip("\n")[:300],
                            }
                        )
                        if len(hits) >= max_hits:
                            truncated = True
                            break
        except OSError:
            continue
    return {
        "where": label,
        "pattern": text,
        "scanned_files": scanned,
        "total": len(hits),
        "hits": hits,
        "note": _join_notes(
            [
                f"命中已达上限 {max_hits} 条，可能还有更多——把 pattern 写细一点"
                if truncated
                else "",
                f"跳过了 {skipped_big} 个超过 {MAX_SEARCH_BYTES // 1024}KB 的文件"
                if skipped_big
                else "",
            ]
        ),
    }


def _walk(base: Path):  # type: ignore[no-untyped-def]
    """遍历目录，跳过 ``_IGNORED_DIRS``。顺序稳定（排序）便于"结果可复现"。"""
    if base.is_file():
        yield base
        return
    for item in sorted(base.iterdir(), key=_sort_key):
        try:
            if item.is_dir():
                if item.name in _IGNORED_DIRS:
                    continue
                yield from _walk(item)
            else:
                yield item
        except OSError:  # 符号链接断了 / 权限不足
            continue


# --------------------------------------------------------------------- 小工具


def _sort_key(item: Path) -> tuple[int, str]:
    """目录在前、随后按名字（大小写不敏感）。"""
    try:
        is_dir = item.is_dir()
    except OSError:
        is_dir = False
    return (0 if is_dir else 1, item.name.lower())


def _matcher(pattern: str):  # type: ignore[no-untyped-def]
    """``pattern`` 支持 ``*.py`` 这种通配（用 ``fnmatch`` 的语义，但用 ``re`` 实现）。"""
    text = (pattern or "").strip()
    if not text:
        return None
    if len(text) > 120:
        raise InvalidRequestError("pattern 太长了（上限 120 字符）")
    # fnmatch 的转义规则比 re 严格（`[` 之类会被当字面量），而这里只需要 `*` 与 `?`，
    # 所以自己拼一小段正则比引 fnmatch 更容易说清"支持什么"
    body = re.escape(text).replace(r"\*", ".*").replace(r"\?", ".")
    regex = re.compile(f"^{body}$", re.IGNORECASE)
    return regex.match


def _join_notes(parts: list[str]) -> str:
    return " ".join(item for item in parts if item)
