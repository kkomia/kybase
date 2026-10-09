"""上传 HTML 的本地解析（v17 补的兜底）。

原先 `.html` 落到纯文本直通：`<script>`、导航、页脚原样进库，
用户问什么都能匹配到菜单里的"首页 关于 联系方式"。这里钉住三件事：
正文提取生效、提取不出来时退回整页、真的没有正文时报错而不是产出空文档。
"""

from __future__ import annotations

import pytest

from app.parsers.base import ParseError
from app.parsers.html_upload import HtmlUploadParser

PAGE = """<!doctype html>
<html><head>
  <title>眼轴监测</title>
  <script>var tracking = "should-not-appear";</script>
  <style>.nav { color: red }</style>
</head><body>
  <nav>首页 关于我们 联系方式 产品价格</nav>
  <article>
    <h1>眼轴长度监测</h1>
    <p>眼轴长度是近视防控的核心指标，建议每三个月测量一次。</p>
    <p>测量结果应与屈光度、角膜曲率一起综合判断。</p>
  </article>
  <footer>版权所有 2026 某某公司</footer>
</body></html>
"""


@pytest.fixture
def parser() -> HtmlUploadParser:
    return HtmlUploadParser()


def test_supports_html_suffixes_and_mime(parser: HtmlUploadParser) -> None:
    probe = None  # supports 不看探测结论
    assert parser.supports(filename="a.html", mime_type=None, probe=probe)  # type: ignore[arg-type]
    assert parser.supports(filename="a.HTM", mime_type=None, probe=probe)  # type: ignore[arg-type]
    assert parser.supports(filename="download", mime_type="text/html", probe=probe)  # type: ignore[arg-type]
    assert not parser.supports(filename="a.md", mime_type="text/markdown", probe=probe)  # type: ignore[arg-type]


def test_strips_script_nav_and_footer(parser: HtmlUploadParser) -> None:
    result = parser.parse(content=PAGE.encode(), filename="page.html")

    assert result.parser_name == "HtmlUploadParser"
    assert "眼轴长度是近视防控的核心指标" in result.markdown
    # 这三样都是"进了库会污染检索"的典型噪声
    assert "should-not-appear" not in result.markdown
    assert "首页 关于我们" not in result.markdown
    assert "版权所有" not in result.markdown
    assert "color: red" not in result.markdown


def test_falls_back_to_whole_page_when_no_article_container(parser: HtmlUploadParser) -> None:
    """没有可识别的正文容器时退整页转换：宁可带点噪声，也不能产出空文档。"""
    fragment = "<html><body><div><span>只有一句正文。</span></div></body></html>"

    result = parser.parse(content=fragment.encode(), filename="frag.html")

    assert "只有一句正文" in result.markdown


def test_decode_ladder_handles_gb18030(parser: HtmlUploadParser) -> None:
    """中文网页常见 GB18030：复用纯文本那套解码阶梯，不自己再写一遍。"""
    html = "<html><body><p>中文正文内容。</p></body></html>"

    result = parser.parse(content=html.encode("gb18030"), filename="cn.html")

    assert "中文正文内容" in result.markdown


def test_empty_file_is_rejected(parser: HtmlUploadParser) -> None:
    with pytest.raises(ParseError):
        parser.parse(content=b"", filename="empty.html")


def test_html_without_any_content_is_rejected(parser: HtmlUploadParser) -> None:
    """只有脚本样式的 html：报错，而不是产出一份空文档让用户以为收下了。"""
    with pytest.raises(ParseError):
        parser.parse(
            content=b"<html><head><style>b{}</style></head><body></body></html>",
            filename="x.html",
        )
