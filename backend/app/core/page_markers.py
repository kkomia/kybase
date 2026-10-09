"""页标记（``<!-- page:N -->``）的写入与读取约定。

一份多页文档的解析产物是**一整段 Markdown**，"这条命中在第几页"这个问题只能靠
标记来回答。标记有三个使用方，必须共用同一个格式：

- **写入**：解析器逐页产出时（PaddleOCR 天然逐页；MinerU 靠 ``content_list.json``
  的 ``page_idx`` 定位）；大文件切分器按段合并时（``services/splitting.py``）。
- **读取**：``services/chunking.py`` 把标记当切块的硬边界，把页码落到 ``chunk.page``。

**放在 ``app/core`` 而不是 ``services``**：解析器是插件层，禁止反向依赖 services
（工程规范 L4），而写侧读侧又必须共用同一个格式——各写一份必然漂。

标记长这样：``<!-- page:3 -->``，**独占一行**。用 HTML 注释是因为它对 Markdown
渲染器不可见，且天然不参与正文检索；读取侧把它整行剥掉，不进入 chunk 文本。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

__all__ = [
    "PAGE_MARKER_RE",
    "PAGE_MARKER_TEMPLATE",
    "insert_page_markers",
    "locate_block_offsets",
    "page_marker",
    "strip_page_markers",
]

#: 标记模板。改它要同时改读取侧的 ``PAGE_MARKER_RE``（同一份文件里，不会分家）。
PAGE_MARKER_TEMPLATE = "<!-- page:{page} -->"

#: 读侧：整行匹配一个页标记。
PAGE_MARKER_RE = re.compile(r"^<!--\s*page:(\d+)\s*-->$")

#: 定位块时用作锚点的最长前缀。太短会撞车（同一份文档里重复的短句），
#: 太长则一旦上游对正文做了转义/换行处理就定位不到——两个极端都只会**少插**几个标记，
#: 不会插错（见 ``locate_block_offsets`` 的约束）。
_ANCHOR_CHARS = 48


def page_marker(page: int) -> str:
    """一行页标记。"""
    return PAGE_MARKER_TEMPLATE.format(page=page)


def strip_page_markers(markdown: str) -> str:
    """剥掉所有页标记行，并顺手把多余的空行收一收。

    给**面向前端/用户的文本**用（阅读视角、下载的 Markdown）：标记是我们内部的
    页码锚点，不该出现在用户读到的正文里。
    """
    if "<!--" not in markdown:
        return markdown
    kept = [line for line in markdown.splitlines() if not PAGE_MARKER_RE.match(line.strip())]
    text = "\n".join(kept)
    # 标记原来占一行，剥掉后会留下连续空行；压回最多一个空行，免得正文到处是空洞
    return re.sub(r"\n{3,}", "\n\n", text)


def insert_page_markers(markdown: str, boundaries: Sequence[tuple[int, int]]) -> str:
    """在给定的字符偏移处插入页标记。

    ``boundaries`` 是 ``(offset, page)``，按 offset 升序；offset 必须是原串里的
    合法切点（一般来自 :func:`locate_block_offsets`）。重复页码会被跳过——
    同一页里出现两个标记没有意义，只会把 chunker 的硬边界切得更碎。
    """
    if not boundaries:
        return markdown

    pieces: list[str] = []
    cursor = 0
    last_page: int | None = None
    for offset, page in sorted(boundaries, key=lambda item: item[0]):
        if page == last_page:
            continue
        cut = max(cursor, min(offset, len(markdown)))
        pieces.append(markdown[cursor:cut])
        # 标记独占一行：前面补换行，后面补一个换行，保证正则的整行匹配成立
        prefix = "" if not pieces[-1] or pieces[-1].endswith("\n") else "\n"
        pieces.append(f"{prefix}{page_marker(page)}\n")
        cursor = cut
        last_page = page
    pieces.append(markdown[cursor:])
    return "".join(pieces)


def locate_block_offsets(
    markdown: str, blocks: Iterable[tuple[str | None, int | None]]
) -> list[tuple[int, int]]:
    """把"按阅读顺序排列的块"映射成"页码 → 在 Markdown 里的偏移"。

    用于 MinerU 这类**只给合并 Markdown、页码藏在 ``content_list.json``** 的解析器。
    做法是顺序锚定：每个块给一段可用于检索的文本（``needle``），从上一个命中位置
    往后找；找到就记下它的偏移与页码。

    **只往前后找、且定位不到就跳过**——这两条是"宁缺勿错"的保证：

    - 顺序推进保证不会回头匹配到更早的同名文本；
    - 定位不到（上游转义了正文、块被合并进表格 HTML 等）就放弃这个边界，
      结果是这一段没有页码，而不是标了一个错的页码。页码标错比没有更糟——
      读者会照着去翻一页，然后发现对不上，从此不再信任引用。

    第一个定位到的块也会产出边界（页码从它开始），这样首页内容也有页码。
    """
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    last_page: int | None = None
    for needle, page in blocks:
        if page is None:
            continue
        anchor = _anchor(needle)
        if not anchor:
            continue
        found = markdown.find(anchor, cursor)
        if found < 0:
            continue
        if last_page is None or page != last_page:
            boundaries.append((found, page))
        last_page = page
        cursor = found + len(anchor)
    return boundaries


def _anchor(needle: str | None) -> str:
    """把一段块文本收成可检索的锚点：取第一行非空内容的前若干字符。"""
    if not needle:
        return ""
    for line in needle.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:_ANCHOR_CHARS]
    return ""
