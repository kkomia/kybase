"""按页取原文的纯函数：切片、范围校验、页码可信度（``app/services/document_pages.py``）。

这一半**不碰存储**：切片只吃一段 markdown，范围校验只看两个整数，页码可信度只比
几个数，所以整份用例标
``local``——没有 PG 的机器上也该能跑。它们管的是"最容易被当成小事"的那几处：
缺页、超出总页数、没有页码信息。那几种情况下**如实给空**比报错重要，因为界面要
照着它说话（"这份文档没有页码信息"）。

**页码可信度那一节是 2026-10 补的**：产物里的页码由上游解析器写下，而它写错过
（存量切分文档里 ``<!-- page:1 -->`` 反复出现）。读侧不能把"标着第 1 页"的那一段
当第 1 页交出去——本文件要钉住的就是那个判据的三值，尤其是**不可信那一档**。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.document_pages import (
    MAX_PAGES_PER_REQUEST,
    check_page_range,
    page_image_kind,
    page_marker_stats,
    read_pages,
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


# --------------------------------------------------------------------- 页码统计


def test_stats_count_distinct_page_numbers_not_markers() -> None:
    """统计的是**不同页号**与**最大页号**，不是标记出现的次数。

    同一页号标了两次只算一个页号——"这份产物写了几个页号"才是判断页码可信度的
    那个数（重复标记恰恰是坏产物的症状，见下面那条复现式用例）。
    """
    markdown = "<!-- page:1 -->\n甲\n\n<!-- page:1 -->\n乙\n\n<!-- page:7 -->\n丙\n"

    assert page_marker_stats(markdown) == (2, 7)


def test_stats_of_a_product_without_markers_are_zero() -> None:
    assert page_marker_stats("# 说明\n\n正文。\n") == (0, 0)


# --------------------------------------------------------------------- 页码可信度

#: "总页数很大、产物里却只有零星几个页号"的产物：619 页只定位到 19 个页号。
#: 存量数据里那批切分文档就是这个形状（见下面那条复现式用例）。
SPARSE = "\n\n".join(f"<!-- page:{page} -->\n正文。" for page in [1, *range(2, 19), 200])


def test_pages_are_reliable_when_they_agree_with_the_page_count() -> None:
    """有页标记、且与文档自称的总页数自洽（最大页号不超、定位到的页数够多）→ 可信。"""
    reading = read_pages(MARKED, from_page=1, to_page=2, page_count=4)

    assert reading.text_available is True
    assert reading.pages_located == 4
    assert reading.max_page == 4
    assert reading.reliable is True
    assert [item.page for item in reading.pages] == [1, 2]
    assert "第一页正文" in reading.pages[0].text


def test_far_fewer_pages_than_the_page_count_is_unreliable() -> None:
    """619 页的文档、产物里只定位到 19 个页号：**页码不可信**。

    这就是要防的那件事：不判的话，"第 1 页"那一段会被当成第 1 页交出去，
    而它实际横跨十几章。
    """
    reading = read_pages(SPARSE, from_page=1, to_page=2, page_count=619)

    assert reading.text_available is True
    assert reading.pages_located == 19
    assert reading.reliable is False


def test_without_markers_the_reliability_is_unknown() -> None:
    """没有页标记时是 ``None``：**没有页码可判，不是"页码错了"**。

    这一档必须与 ``False`` 分开——纯文本来源的文档落在这里，界面该说"没有页码信息"，
    说成"页码不可信"就是冤枉它（见 ``PageReading.reliable`` 那条三值说明）。
    """
    reading = read_pages("# 说明\n\n正文。\n", from_page=1, to_page=2, page_count=619)

    assert reading.text_available is False
    assert reading.pages_located is None
    assert reading.max_page is None
    assert reading.reliable is None


def test_a_page_number_beyond_the_page_count_is_unreliable() -> None:
    """产物里出现超过总页数的页号：对不上，不可信。

    这份产物只差在最大页号上（4 页的标记 vs 自称 3 页），所以这条钉的是判据的
    前半截（``max_page <= page_count``），与上面那条"页数远少于总页数"互补。
    """
    reading = read_pages(MARKED, from_page=1, to_page=2, page_count=3)

    assert reading.max_page == 4
    assert reading.reliable is False


def test_a_missing_page_count_is_not_a_reason_to_cry_wolf() -> None:
    """总页数拿不到（``None``）时判不了，回 ``True``：**没有依据就不喊狼来了**。"""
    reading = read_pages(MARKED, from_page=1, to_page=2, page_count=None)

    assert reading.page_count is None
    assert reading.reliable is True


def test_a_one_page_document_is_not_called_unreliable() -> None:
    """``page_count <= 1`` 的文档**没有"页码错位"这回事**，别判成不可信。

    1 页的文档上，"定位到的页数远少于总页数"与"页号超过总页数"两条判据都没有意义；
    用户能做的补救（重新解析）也不会让结果有任何不同。
    """
    markdown = "<!-- page:1 -->\n只有一页的正文。\n"

    for page_count in (0, 1):
        reading = read_pages(markdown, from_page=1, to_page=1, page_count=page_count)
        assert reading.reliable is True


def test_a_merged_product_with_a_repeated_page_one_is_unreliable() -> None:
    """**复现式回归**：Wills 那份产物的形状（同一页号反复出现、总页数很大）判成不可信。

    形状照实测的 ``doc_a5e837b84bac``（Wills 眼科手册第 7 版）复刻：文档自称 619 页，
    产物里 ``<!-- page:1 -->`` 在合并点反复出现（342 块里有 118 块标在 ``page=1``），
    不同页号只有 19 个、最大 200。来路是**存量数据的毛病**：2026-09-15..28 入库的那批
    切分文档合并时"把各段的相对页码抬成绝对页码"还没修好，读侧只能照着把后面的正文
    全标成第 1 页。

    这份产物的最大页号（200）**没超过**总页数，所以它只可能栽在"定位到的页数"那半截
    判据上——把判据去掉，这条用例必须变红。
    """
    markdown = "\n\n".join(
        f"<!-- page:{page} -->\n第 {page} 页的正文。"
        for page in [1] * 100 + list(range(2, 19)) + [200]
    )
    reading = read_pages(markdown, from_page=1, to_page=2, page_count=619)

    assert reading.pages_located == 19
    assert reading.max_page == 200
    assert reading.reliable is False, "这一页的文字横跨十几章，不能当成「第 1 页」交出去"
    # 文字本身照给：它是文档的真实内容，只是**不能当页码引用**（这条口径也钉一下）
    assert "第 1 页的正文" in reading.pages[0].text


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
