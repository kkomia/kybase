"""页标记（``<!-- page:N -->``）的写入与读取约定。

一份多页文档的解析产物是**一整段 Markdown**，"这条命中在第几页"这个问题只能靠
标记来回答。标记有三个使用方，必须共用同一个格式：

- **写入**：解析器逐页产出时（PaddleOCR 天然逐页；MinerU 靠 ``content_list.json``
  的 ``page_idx`` 定位）；大文件切分器按段合并时（``services/splitting.py``）。
- **读取**：``services/chunking.py`` 把标记当切块的硬边界，把页码落到 ``chunk.page``。

**放在 ``app/core`` 而不是 ``services``**：解析器是插件层，禁止反向依赖 services
（分层规则 L4），而写侧读侧又必须共用同一个格式——各写一份必然漂。

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
    "shift_page_markers",
    "strip_page_markers",
]

#: 标记模板。改它要同时改读取侧的 ``PAGE_MARKER_RE``（同一份文件里，不会拆分）。
PAGE_MARKER_TEMPLATE = "<!-- page:{page} -->"

#: 读侧：整行匹配一个页标记。
PAGE_MARKER_RE = re.compile(r"^<!--\s*page:(\d+)\s*-->$")

#: 整行替换用的变体：``PAGE_MARKER_RE`` 是给"逐行 match"用的（调用方自己 strip），
#: 这里要在一整段文本里找，所以自带行首行尾锚点与空白容忍（含 ``\r``）。
_PAGE_MARKER_LINE_RE = re.compile(r"^[^\S\n]*<!--\s*page:(\d+)\s*-->[^\S\n]*$", re.MULTILINE)

#: 定位块时锚点取"块的第一行"，长度在这个区间里。
#:
#: **为什么要放长**：只取前 48 个字符时，参考文献里两条"作者、刊名都相同、只有年份
#: 不同"的条目会共用一个锚点，顺序锚定就会把后一条记到前一条那一行上——实测一份
#: 37 页的指南，三个页边界因此全落进相邻的八行里。放到整行长度（有上限）就能把
#: 这类条目分开。上限那一头同样重要：上游做了转义/换行处理时，锚点越长越可能整条
#: 都找不到。两个方向都只会**少插**标记，不会插错（见 :func:`locate_block_offsets`）。
_ANCHOR_CHARS = 48
_ANCHOR_MAX_CHARS = 160

#: 位置先验允许偏出期望位置的范围，单位是"局部页长"（从上一个已定位块往后内插得出）。
#:
#: 先验要挡的是**整页级别以上的错配**——实测的错法是"第 8 页的标记落到 95% 处"
#: 这种偏出几十页的假锚点。页长会因排版浮动，前几页特别短（封面、目录）也会让
#: 内插出来的期望偏出一页上下，所以容差给到 1.5 个页长；但两页以上的漂移必须被拒，
#: 那已经不能再用"页长不匀"解释。
_SLACK_PAGES = 1.5

#: 先验窗口的字符下限。文档极短时页长只有几个字符，纯按比例会给出一个空窗口，
#: 把位置其实合理的候选也拒掉。
_MIN_SLACK_CHARS = 8

#: 同一个锚点在一次定位里最多看几个候选。锚点退化成"整行都是空格"这类输入时，
#: 不加这道闸就会把定位变成整篇扫描。
_MAX_CANDIDATES = 64

#: 「行首」容许的前缀：空白、列表/引用/标题记号、Markdown 化的参考文献序号。
_LINE_PREFIX_RE = re.compile(r"(?:[-*+>]|#{1,6}|\d+[.)]|\[\d+\])?")


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


def shift_page_markers(markdown: str, offset: int) -> str:
    """把一份产物里的页标记整体平移 ``offset`` 页（``offset`` 为 0 时原样返回）。

    **大文件切分必须用它**：每段是**单独提交**给解析引擎的，解析器眼中的页码
    是这一段自己的（第 1 页起）。合并回整份文档时若不把相对页号抬成绝对页号，
    合并产物里就会出现 ``…200, 201, 1, 2…`` 这种"页码倒着走"的序列——
    读侧只能照着它把第 202 页的正文标成第 1 页。实测一份 585 页的 PDF
    就是这样：1047 块里有 713 块被标成第 1 页。

    只动标记行本身，正文一个字符都不碰（标记是我们自己插的独占行）。
    """
    if offset == 0 or "<!--" not in markdown:
        return markdown
    return _PAGE_MARKER_LINE_RE.sub(
        lambda match: page_marker(int(match.group(1)) + offset), markdown
    )


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
    markdown: str,
    blocks: Iterable[tuple[str | None, int | None]],
    *,
    page_count: int | None = None,
) -> list[tuple[int, int]]:
    """把"按阅读顺序排列的块"映射成"页码 → 在 Markdown 里的偏移"。

    用于 MinerU 这类**只给合并 Markdown、页码藏在 ``content_list.json``** 的解析器。
    做法是顺序锚定：每个块给一段可用于检索的文本（``needle``），从上一个命中位置
    往后找；找到就记下它的偏移与页码。

    **"宁缺勿错"是这一层的总原则**：页码标错比没有更糟——读者会照着去翻一页，
    然后发现对不上，从此不再信任引用。所以每条候选命中都要过三道判据，任何一道
    过不了就放弃这条边界（不将就）：

    1. **行首优先**：页眉、标题在 Markdown 里独占一行，而正文提到它们时总是行内
       出现（``详见《…》``）。只认"行首（容许列表/标题/引用序号前缀）"的命中，
       就能把"正文里提了一嘴页眉"这类假命中挡在外面；实在没有行首命中时（表格
       HTML、行内图片）才退回任意位置。
    2. **锚点够长**：锚点取块的第一行，最长到 :data:`_ANCHOR_MAX_CHARS`。短锚点会
       撞车——重复的页眉、参考文献里只在年份上不同的条目都只和短前缀相同。整行都还
       找不到时，再退回首 :data:`_ANCHOR_CHARS` 个字符试试（上游改了行尾时还有救）。
    3. **位置先验**：页大致等长，所以"块在第 N 页、全文共 P 页"给出了一个期望位置：
       从**上一个已定位的块**往后局部内插（拿整篇线性内插会被前言/目录这类"开头几页
       特别短"的偏斜带跑）。候选偏出期望超过 :data:`_SLACK_PAGES` 个页长就是假锚点，
       不是页边界。

    **被拒的候选绝不推进游标**。顺序锚定的游标一旦被假命中推到真正的页之前，后面
    所有真边界都落在游标后面、再也找不到——实测的错法正是如此：假命中把游标拖到文末
    的参考文献区，剩下的边界全挤在相邻几行里。所以只有**接受**才推进游标；被拒、
    找不到，游标都原地不动，后面的块还有机会找到自己的位置。

    ``page_count`` 是文档自己声明的总页数（``content_list.json`` 里最大的
    ``page_idx`` + 1）。不传就是"不知道总页数"：第 3 条判据失效，退化成"从游标往后
    第一个命中"——调用方手上有页数时应当传进来。

    第一个定位到的块也会产出边界（页码从它开始），这样首页内容也有页码。
    """
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    last_page: int | None = None
    # 位置先验的基准：最近一次**成功定位**的块。初始视作"偏移 0 就是第 1 页"——
    # 解析产物是从文档第一页开始的。
    anchor_page = 1
    anchor_offset = 0
    total = len(markdown)
    known_pages = page_count if page_count and page_count > 0 else None

    for needle, page in blocks:
        if page is None:
            continue
        located = _locate(
            markdown,
            needle,
            cursor=cursor,
            page=page,
            page_count=known_pages,
            anchor_page=anchor_page,
            anchor_offset=anchor_offset,
            total=total,
        )
        if located is None:
            continue
        found, anchor_length = located
        if last_page is None or page != last_page:
            boundaries.append((found, page))
        last_page = page
        cursor = found + anchor_length
        anchor_page, anchor_offset = page, found
    return boundaries


def _locate(
    markdown: str,
    needle: str | None,
    *,
    cursor: int,
    page: int,
    page_count: int | None,
    anchor_page: int,
    anchor_offset: int,
    total: int,
) -> tuple[int, int] | None:
    """定位一个块，返回 ``(偏移, 实际用掉的锚点长度)``；定位不到返回 ``None``。"""
    line = _anchor(needle)
    if not line:
        return None
    found = _first_plausible(
        markdown,
        line,
        cursor=cursor,
        page=page,
        page_count=page_count,
        anchor_page=anchor_page,
        anchor_offset=anchor_offset,
        total=total,
    )
    if found is not None:
        return found, len(line)
    # 长锚点在游标之后整条都找不到，才退回首 :data:`_ANCHOR_CHARS` 个字符碰运气
    # （上游把行尾转义/改写过时还有救）。**只在"整条找不到"时退**：如果长锚点找得到
    # 却被位置先验拒了，说明它多半是别处的重复文本，这时退回短锚点只会让它更容易撞上
    # 另一行——那是把假命中请回来，违背"宁缺勿错"。
    if len(line) > _ANCHOR_CHARS and markdown.find(line, cursor) < 0:
        short = line[:_ANCHOR_CHARS]
        found = _first_plausible(
            markdown,
            short,
            cursor=cursor,
            page=page,
            page_count=page_count,
            anchor_page=anchor_page,
            anchor_offset=anchor_offset,
            total=total,
        )
        if found is not None:
            return found, len(short)
    return None


def _anchor(needle: str | None) -> str:
    """把一段块文本收成锚点：取第一行非空内容，放到 :data:`_ANCHOR_MAX_CHARS` 为止。"""
    if not needle:
        return ""
    for line in needle.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:_ANCHOR_MAX_CHARS]
    return ""


def _first_plausible(
    markdown: str,
    anchor: str,
    *,
    cursor: int,
    page: int,
    page_count: int | None,
    anchor_page: int,
    anchor_offset: int,
    total: int,
) -> int | None:
    """在游标之后、位置先验之内的命中里挑一个：行首优先，其次取最早出现的一个。"""
    if not anchor:
        return None
    low, high = _plausible_window(
        page=page,
        page_count=page_count,
        anchor_page=anchor_page,
        anchor_offset=anchor_offset,
        total=total,
    )
    low = max(low, cursor)
    if high < low:
        return None

    hits: list[int] = []
    scanned = 0
    at = markdown.find(anchor, cursor)
    while at >= 0 and at <= high and scanned < _MAX_CANDIDATES:
        scanned += 1
        if at >= low:
            hits.append(at)
        at = markdown.find(anchor, at + 1)
    if not hits:
        return None
    line_starts = [offset for offset in hits if _at_line_start(markdown, offset)]
    return (line_starts or hits)[0]


def _plausible_window(
    *,
    page: int,
    page_count: int | None,
    anchor_page: int,
    anchor_offset: int,
    total: int,
) -> tuple[float, float]:
    """本块的期望位置与容差：从上一个已定位块往后内插，容差是若干个页长。

    不传 ``page_count`` 时给整篇窗口——没有页数就没有先验，退化成"顺序命中即可"。
    """
    if not page_count:
        return 0.0, float(total)
    # +1 而不是 +0：内插的是**页的起点**。基准块在第 anchor_page 页，它所在页之后
    # 还剩 page_count - anchor_page + 1 个页起点（含它自己），剩下的文本由这些页分。
    # 少这个 1，最后一页的期望会外推到文末（把第 P 页的起点算成全篇 100%），
    # 而真实起点在 (P-1)/P 处——一个自己的公式就会把最后一页的边界拒掉。
    remaining = page_count - anchor_page + 1
    span = max(total - anchor_offset, 0)
    if remaining <= 0:
        expected = float(anchor_offset)
        page_length = total / page_count
    else:
        expected = anchor_offset + (page - anchor_page) / remaining * span
        page_length = span / remaining
    slack = max(_SLACK_PAGES * page_length, _MIN_SLACK_CHARS)
    return expected - slack, expected + slack


def _at_line_start(markdown: str, offset: int) -> bool:
    """``offset`` 处的匹配是不是"从行首开始"（行首容许列表/标题/引用序号前缀）。

    页眉、标题在 Markdown 里独占一行，而正文提到它们时总是行内出现。只认行首命中，
    就能把"提了一嘴"的假命中挡在外面，且这条判据不依赖任何阈值。
    """
    line_start = markdown.rfind("\n", 0, offset) + 1
    return _LINE_PREFIX_RE.fullmatch(markdown[line_start:offset].strip()) is not None
