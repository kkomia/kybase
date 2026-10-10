"""按页取原文的纯函数：切片与范围校验（``app/services/document_pages.py``）。

这一半**不碰存储**：切片只吃一段 markdown，范围校验只看两个整数，所以整份用例标
``local``——没有 PG 的机器上也该能跑。它们管的是"最容易被当成小事"的那几处：
缺页、超出总页数、没有页码信息。那几种情况下**如实给空**比报错重要，因为界面要
照着它说话（"这份文档没有页码信息"）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.document_pages import (
    MAX_PAGES_PER_REQUEST,
    check_page_range,
    page_image_kind,
    slice_pages,
)

pytestmark = pytest.mark.local

MARKED = """<!-- page:1 -->
# 眼轴共识

第一页正文。

<!-- page:2 -->
## 测量

第二页正文。

<!-- page:3 -->

<!-- page:4 -->
第四页正文。
"""


# --------------------------------------------------------------------- 切片


def test_pages_are_split_on_markers() -> None:
    pages, available = slice_pages(MARKED, from_page=1, to_page=2)

    assert available is True
    assert [item.page for item in pages] == [1, 2]
    assert "第一页正文" in pages[0].text
    assert "第二页正文" not in pages[0].text
    assert "第二页正文" in pages[1].text


def test_marker_lines_never_reach_the_reader() -> None:
    """标记是内部锚点，与 ``/preview`` 给用户看的正文一样必须剥干净。"""
    pages, _ = slice_pages(MARKED, from_page=1, to_page=4)

    for item in pages:
        assert "<!--" not in item.text
        assert "page:" not in item.text


def test_a_page_without_content_comes_back_empty() -> None:
    """第 3 页在两个标记之间什么都没有：**出一条空文字，而不是少一条**。

    页图那一半照样有第 3 页的图，界面按请求的页数铺格子——少一条会让页码错位。
    """
    pages, available = slice_pages(MARKED, from_page=3, to_page=4)

    assert available is True
    assert [item.page for item in pages] == [3, 4]
    assert pages[0].text == ""
    assert "第四页正文" in pages[1].text


def test_pages_beyond_the_document_are_listed_but_empty() -> None:
    """要的页超出产物里出现过的页号：仍然逐页给条目，只是文字为空。

    "这一页没有正文"与"这一页不存在"是两件事，前者界面照样要画出格子。
    """
    pages, available = slice_pages(MARKED, from_page=10, to_page=12)

    assert available is True
    assert [item.page for item in pages] == [10, 11, 12]
    assert [item.text for item in pages] == ["", "", ""]


def test_without_markers_there_is_no_page_information() -> None:
    """**没有页标记 = 这份文档没有页码信息**（空列表 + False）。

    纯文本类文档（.md / .txt）本来就不带标记；回一个空列表而不是抛异常，
    界面才有机会说清"没有页码信息"。
    """
    pages, available = slice_pages("# 说明\n\n这是一份没有页标记的产物。\n", from_page=1, to_page=3)

    assert pages == []
    assert available is False


def test_empty_markdown_has_no_page_information() -> None:
    assert slice_pages("", from_page=1, to_page=1) == ([], False)


def test_a_single_page_range_works() -> None:
    pages, available = slice_pages(MARKED, from_page=2, to_page=2)

    assert available is True
    assert len(pages) == 1
    assert pages[0].page == 2
    assert "第二页正文" in pages[0].text


def test_text_before_the_first_marker_belongs_to_page_one() -> None:
    """标记是插在"第一个定位到的块"上的，它前面的标题/前言属于第 1 页。

    丢了这一段，第一页会从一个标题的中间开始。
    """
    markdown = "文档标题\n\n<!-- page:1 -->\n\n第一页正文。\n"

    pages, available = slice_pages(markdown, from_page=1, to_page=1)

    assert available is True
    assert "文档标题" in pages[0].text
    assert "第一页正文" in pages[0].text


def test_text_before_a_later_first_marker_is_dropped() -> None:
    """第一个标记不是第 1 页时，它前面那段文本的页码**是未知的**：宁可少一段，
    也不安到某一页头上（同 ``core/page_markers.py`` 的"宁缺勿错"）。
    """
    markdown = "封面上的字\n\n<!-- page:3 -->\n\n第三页正文。\n"

    pages, available = slice_pages(markdown, from_page=1, to_page=3)

    assert available is True
    assert "封面上的字" not in "".join(item.text for item in pages)
    assert "第三页正文" in pages[2].text


def test_repeated_page_numbers_are_concatenated() -> None:
    """同一页号出现两次（上游插标记出过毛病）时两段都给，不丢内容。"""
    markdown = "<!-- page:1 -->\n前半\n\n<!-- page:2 -->\n某段\n\n<!-- page:1 -->\n后半\n"

    pages, _ = slice_pages(markdown, from_page=1, to_page=2)

    assert "前半" in pages[0].text and "后半" in pages[0].text
    assert "某段" in pages[1].text


def test_crlf_markers_are_recognized() -> None:
    """Windows 上落盘的产物可能是 CRLF：行尾的 ``\\r`` 不能让标记匹配落空。"""
    markdown = "<!-- page:1 -->\r\n第一页正文。\r\n\r\n<!-- page:2 -->\r\n第二页正文。\r\n"

    pages, available = slice_pages(markdown, from_page=1, to_page=2)

    assert available is True
    assert "第一页正文" in pages[0].text
    assert "第二页正文" in pages[1].text


# --------------------------------------------------------------------- 范围校验


def test_range_must_start_at_page_one() -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        check_page_range(0, 3)

    assert "从 1 开始" in str(excinfo.value)


def test_range_must_not_be_reversed() -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        check_page_range(5, 3)

    assert "不能小于起始页" in str(excinfo.value)


def test_range_span_is_capped() -> None:
    """单页上限：页图是现场渲染的，一次要几百页会把解析机与对象存储按住。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        check_page_range(1, MAX_PAGES_PER_REQUEST + 1)

    assert str(MAX_PAGES_PER_REQUEST) in str(excinfo.value)

    # 正好等于上限是允许的（边界不能一起被拒掉）
    check_page_range(1, MAX_PAGES_PER_REQUEST)


def test_a_valid_range_passes() -> None:
    check_page_range(1, 1)
    check_page_range(7, 12)


# --------------------------------------------------------------------- 出图种类


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("报告.pdf", "pdf"),
        ("扫描件.png", "image"),
        ("照片.JPEG", "image"),  # 大小写不敏感
        # 其余格式没有"页"可切，签链接只会得到一片 409（见端点里那段说明）
        ("合同.docx", None),
        ("说明.md", None),
        ("资料.zip", None),
        ("没有后缀", None),
        # SVG 是能带 <script> 的文本格式：内联渲染等于在自家 origin 下执行上传者的脚本
        ("向量图.svg", None),
    ],
)
def test_page_image_kind(filename: str, expected: str | None) -> None:
    assert page_image_kind(filename) == expected
