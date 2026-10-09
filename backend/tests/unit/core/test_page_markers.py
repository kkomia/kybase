"""页标记（``app/core/page_markers.py``）。

**为什么值得单测**：页码是"答案可核查"的最后一环，而它的两种失败方式都不报错——
插错了，读者翻到那一页发现对不上，从此不再信任引用；没插上，引用里就永远没有页码。
所以这里把"宁缺勿错"的两条性质钉死：**顺序锚定只往前走**、**定位不到就跳过**。
"""

from __future__ import annotations

from app.core.page_markers import (
    PAGE_MARKER_RE,
    insert_page_markers,
    locate_block_offsets,
    page_marker,
    strip_page_markers,
)

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
