"""页标记（``app/core/page_markers.py``）。

**为什么值得单测**：页码是"答案可核查"的最后一环，而它的两种失败方式都不报错——
插错了，读者翻到那一页发现对不上，从此不再信任引用；没插上，引用里就永远没有页码。
所以这里把"宁缺勿错"的三条性质钉死：**行首优先**（页的起点不会落在行内）、
**顺序锚定只往前走且被拒的候选不推进游标**、**定位不到就跳过**。

定位用的合成样本照着实测的两种失败形态搭：重复页眉、共用前缀的参考文献尾。
真实的 ``content_list.json`` 不落盘，所以这里是"照形态复刻"，不是拿真实文档回归。
"""

from __future__ import annotations

from app.core.page_markers import (
    PAGE_MARKER_RE,
    insert_page_markers,
    locate_block_offsets,
    page_marker,
    shift_page_markers,
    strip_page_markers,
)
from app.services.chunking import trusted_pages

MARKDOWN = "# 标题\n\n第一段正文。\n\n# 第二节\n\n第二段正文。\n\n# 第三节\n\n第三段正文。"


# --------------------------------------------------------------------- 基本格式


def test_page_marker_is_a_whole_line_html_comment() -> None:
    marker = page_marker(7)
    assert marker == "<!-- page:7 -->"
    # 读取侧是按整行匹配的：前后有内容就认不出来
    assert PAGE_MARKER_RE.match(marker)
    assert not PAGE_MARKER_RE.match(f"正文 {marker}")


def test_strip_removes_markers_and_collapses_blank_lines() -> None:
    text = "A\n\n<!-- page:1 -->\n\nB\n\n<!-- page:2 -->\n\nC"

    stripped = strip_page_markers(text)

    assert "page:" not in stripped
    assert stripped == "A\n\nB\n\nC"


# --------------------------------------------------------------------- 定位


def test_locates_page_boundaries_in_reading_order() -> None:
    blocks = [
        ("# 标题", 1),
        ("第一段正文。", 1),
        ("# 第二节", 2),
        ("第二段正文。", 2),
        ("# 第三节", 3),
    ]

    boundaries = locate_block_offsets(MARKDOWN, blocks)

    # 第一个可定位的块也要产出边界，否则首页内容没有页码
    assert [page for _, page in boundaries] == [1, 2, 3]
    # 偏移必须递增且确实落在对应文本上
    offsets = [offset for offset, _ in boundaries]
    assert offsets == sorted(offsets)
    assert MARKDOWN[offsets[1] : offsets[1] + len("# 第二节")] == "# 第二节"


def test_same_page_yields_only_one_boundary() -> None:
    boundaries = locate_block_offsets(
        MARKDOWN, [("# 标题", 1), ("第一段正文。", 1), ("# 第二节", 2)]
    )

    assert [page for _, page in boundaries] == [1, 2]


def test_unlocatable_block_is_skipped_not_guessed() -> None:
    """锚点定位不到就跳过——**宁可这一段没有页码，也不能标错页**。"""
    blocks = [
        ("# 标题", 1),
        ("这段文字被上游转义过、md 里找不到", 2),
        ("# 第三节", 3),
    ]

    boundaries = locate_block_offsets(MARKDOWN, blocks)

    # 中间那个块被跳过，但它后面的第三节仍然按自己的页码标出
    assert [page for _, page in boundaries] == [1, 3]


def test_search_only_moves_forward() -> None:
    """同一个短句在文档里出现两次时，第二次必须匹配到**后面**那一处。

    只往前走是这套锚定能成立的关键：否则"第一段正文"会被下一次搜索重新命中，
    页码就会被标到文档开头去。
    """
    markdown = "重复句子\n\n中间\n\n重复句子"
    boundaries = locate_block_offsets(markdown, [("重复句子", 1), ("重复句子", 2)])

    second = markdown.rindex("重复句子")
    assert [offset for offset, _ in boundaries] == [0, second]
    assert second > 0


def test_blocks_without_page_are_ignored() -> None:
    boundaries = locate_block_offsets(MARKDOWN, [(None, None), ("# 标题", None)])

    assert boundaries == []


# --------------------------------------------------------------------- 插入


def test_insert_puts_each_marker_on_its_own_line() -> None:
    boundaries = locate_block_offsets(
        MARKDOWN, [("# 标题", 1), ("# 第二节", 2), ("# 第三节", 3)]
    )

    marked = insert_page_markers(MARKDOWN, boundaries)

    lines = marked.splitlines()
    assert lines[0] == "<!-- page:1 -->"
    assert "<!-- page:2 -->" in lines
    # 每一条标记都独占一行，读取侧才认得出
    for line in lines:
        if "page:" in line:
            assert PAGE_MARKER_RE.match(line)
    # 正文一个字都不能丢
    assert strip_page_markers(marked) == MARKDOWN


def test_insert_skips_duplicate_consecutive_pages() -> None:
    marked = insert_page_markers("A\nB", [(0, 1), (2, 1)])

    assert marked.count("page:1") == 1


def test_insert_with_no_boundaries_returns_input() -> None:
    assert insert_page_markers(MARKDOWN, []) == MARKDOWN


# --------------------------------------------------------------------- 平移


def test_shift_bumps_every_marker_by_the_offset() -> None:
    text = "<!-- page:1 -->\n\n甲\n\n<!-- page:2 -->\n\n乙"

    assert shift_page_markers(text, 200) == "<!-- page:201 -->\n\n甲\n\n<!-- page:202 -->\n\n乙"


def test_shift_zero_is_a_noop() -> None:
    text = "<!-- page:1 -->\n\n甲"
    assert shift_page_markers(text, 0) is text


def test_shift_leaves_body_untouched() -> None:
    """正文里出现"page:1"这样的字样不能被当成标记改掉——只认整行标记。"""
    text = "行内提到 page:1 不算标记\n\n<!-- page:1 -->\n\n正文"

    shifted = shift_page_markers(text, 5)

    assert "行内提到 page:1 不算标记" in shifted
    assert "<!-- page:6 -->" in shifted


def test_shift_survives_crlf() -> None:
    """Windows 换行的产物也要认得出标记（否则 PDF 页码会整体丢失）。"""
    text = "<!-- page:2 -->\r\n\r\n甲"

    assert shift_page_markers(text, 10).startswith("<!-- page:12 -->")


# ------------------------------------------------- 定位：重复页眉与共用前缀的参考文献
#
# 下面两组样本照着实测的两种失败形态搭（真实的 content_list.json 不落盘，
# 只能"照形态复刻"）：**重复的页眉/标题**、**共用前缀的参考文献条目**。


def _line_of(markdown: str, offset: int) -> str:
    return markdown.splitlines()[markdown.count("\n", 0, offset)]


def _running_header_document() -> tuple[str, list[tuple[str, int]]]:
    """每页都有同一行页眉；第 1 页的段落里还"提了一嘴"这行页眉。

    "提了一嘴"那行与它前面的锚点是**同一个块的两行**：锚点只覆盖块的第一行，
    所以那一行落在游标之后——顺序 ``find`` 会先撞上它，而不是撞上下一页的页眉。
    """
    pages: list[str] = []
    blocks: list[tuple[str, int]] = []
    for page in range(1, 9):
        body = f"这是第 {page} 页正文，长度与其它页一致，供位置先验参照。"
        if page == 1:
            body += "\n\n注意事项\n本文所称 KYLAB 用户手册 均指本手册。"
        pages.append(f"KYLAB 用户手册\n\n第{page}章 分节{page}\n\n{body}\n")
        blocks += [("KYLAB 用户手册", page), (f"第{page}章 分节{page}", page), (body, page)]
    return "\n".join(pages), blocks


def test_repeated_running_header_puts_the_boundary_on_the_header() -> None:
    """重复页眉是实测里最典型的假锚点来源：边界必须落在页眉那一行的**行首**。

    旧实现取首行前 48 字符、再无脑往后 ``find``，于是第 2 页的边界被标进第 1 页的
    正文（``均指本手册`` 那一行）。错得最坏的地方在于标记链看起来仍是 1..8 连续，
    切分器的自洽校验会把这个错页码当成可信页码报出去。
    """
    markdown, blocks = _running_header_document()

    boundaries = locate_block_offsets(markdown, blocks, page_count=8)

    assert [page for _, page in boundaries] == list(range(1, 9))
    for offset, _ in boundaries:
        # 页的起点一定在行首：行内的"提了一嘴"不可能是边界
        assert offset == 0 or markdown[offset - 1] == "\n"
    # 页眉行本身才是边界（第 2 页这一条正是旧实现标错的那条）
    offsets = {page: offset for offset, page in boundaries}
    assert markdown[offsets[2] :].startswith("KYLAB 用户手册")


def _reference_entry(year: int, volume: int, issue: int, pages: str) -> str:
    """一条参考文献：五条只在年份/卷期上不同，前 48 个字符完全一样。"""
    return (
        "王某某, 李某某, 张某某, 赵某某, 陈某某. 面向大规模知识库的检索增强生成方法"
        f"研究[J]. 计算机学报, {year}, {volume}({issue}): {pages}."
    )


def _reference_tail_document() -> tuple[str, list[tuple[str, int]]]:
    """正文三页 + 参考文献区五条；content_list 只把第 1/3/5 条吐成页首块。"""
    parts: list[str] = []
    blocks: list[tuple[str, int]] = []
    for page in range(1, 4):
        body = f"第 {page} 章正文段落，长度与其它页接近，供位置先验参照。"
        parts.append(f"# 第{page}章 分节{page}\n\n{body}\n")
        blocks += [(f"# 第{page}章 分节{page}", page), (body, page)]
    blocks.append(("参考文献", 3))
    parts.append("参考文献\n")
    entries = [
        _reference_entry(2020, 43, 5, "100-115"),
        _reference_entry(2021, 44, 6, "200-215"),
        _reference_entry(2022, 45, 7, "300-315"),
        _reference_entry(2023, 46, 8, "400-415"),
        _reference_entry(2024, 47, 9, "500-515"),
    ]
    for index, entry in enumerate(entries):
        parts.append(f"[{12 + index}] {entry}\n")
    # 条目正文（不含 ``[12] `` 前缀）才是 content_list 里的锚点文本
    blocks += [(entries[0], 4), (entries[2], 5), (entries[4], 6)]
    return "\n".join(parts), blocks


def test_reference_tail_with_shared_prefix_keeps_the_true_boundaries() -> None:
    """参考文献尾：共用长前缀的条目必须各自落回自己那一行。

    旧实现按 48 字符截断锚点，三条页首条目彼此不可分，顺序 ``find`` 把它们推到
    ``[12]/[13]/[14]`` 三行上——正是实测里"三个边界挤在相邻八行"的形态。
    新实现把锚点放到整行，各自落回 ``[12]/[14]/[16]``。
    """
    markdown, blocks = _reference_tail_document()

    boundaries = locate_block_offsets(markdown, blocks, page_count=6)

    assert [page for _, page in boundaries] == [1, 2, 3, 4, 5, 6]
    reference_lines = [_line_of(markdown, offset)[:4] for offset, page in boundaries if page >= 4]
    assert reference_lines == ["[12]", "[14]", "[16]"]


def test_clean_document_stays_exact_and_complete() -> None:
    """干净文档（块各出现一次、顺序一致）必须一个字都不差，也不能少标。"""
    parts: list[str] = []
    blocks: list[tuple[str, int]] = []
    for page in range(1, 6):
        body = f"第 {page} 页正文，干净、唯一。"
        parts.append(f"# 第{page}节\n\n{body}\n")
        blocks += [(f"# 第{page}节", page), (body, page)]
    markdown = "\n".join(parts)

    with_prior = locate_block_offsets(markdown, blocks, page_count=5)
    without_prior = locate_block_offsets(markdown, blocks)

    assert [page for _, page in with_prior] == [1, 2, 3, 4, 5]
    # 不传页数时先验失效、退化成"顺序命中即可"；干净文档本来无歧义，结果必须一样
    assert with_prior == without_prior


def test_unlocatable_page_is_skipped_and_the_net_reports_a_hole() -> None:
    """第 2 页唯一的块在上游被折成表格 HTML、md 里整条找不到——跳过，不猜。"""
    markdown = (
        "# 第一节\n\n第一节正文，长度与其它页一致。\n\n# 第三节\n\n第三节正文，长度与其它页一致。\n"
    )
    blocks = [
        ("# 第一节", 1),
        ("第一节正文，长度与其它页一致。", 1),
        ("第二页的表格被上游折成 HTML，这段文字在 md 里找不到", 2),
        ("# 第三节", 3),
        ("第三节正文，长度与其它页一致。", 3),
    ]

    boundaries = locate_block_offsets(markdown, blocks, page_count=3)

    # 第 2 页猜不出来就不给；它后面的第 3 页照样按自己的页码标出
    assert [page for _, page in boundaries] == [1, 3]
    # 标记链断了，切分器的自洽校验据此把它们判成"不可信"（第 1、2 页 -> None）
    trusted = trusted_pages(insert_page_markers(markdown, boundaries), 3)
    assert 1 not in trusted
    assert 2 not in trusted


def test_rejected_match_does_not_move_the_cursor() -> None:
    """被位置先验拒掉的假命中，不能让后面的真边界跟着丢。

    第 4 页的第一个块在 md 里根本不存在，只有文末附录里那句话能撞上。旧实现会在
    那里落下第 4 页的边界、把游标一并带到文末，于是第 5、6 页的边界全部丢失；
    新实现拒掉这条候选、**不推进游标**，第 4 页靠它自己的标题落回原位。
    """
    parts: list[str] = []
    blocks: list[tuple[str, int]] = []
    for page in range(1, 7):
        body = f"第 {page} 页正文，长度均匀，供位置先验参照。"
        if page == 4:
            blocks.append(("只在文末附录出现一次的第 4 页文本", 4))
        parts.append(f"# 第{page}节\n\n{body}\n")
        blocks += [(f"# 第{page}节", page), (body, page)]
    markdown = "\n".join(parts) + "\n附录：只在文末附录出现一次的第 4 页文本\n"

    boundaries = locate_block_offsets(markdown, blocks, page_count=6)

    assert [page for _, page in boundaries] == [1, 2, 3, 4, 5, 6]
    page4 = next(offset for offset, page in boundaries if page == 4)
    assert markdown[page4:].startswith("# 第4节")
