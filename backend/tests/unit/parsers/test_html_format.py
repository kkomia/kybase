"""HTML 正文提取与 Markdown 转换（M6 / T6.2）。

镜像同构：``app/parsers/html_format.py`` → 本文件。

**这是这一层最该有测试的地方**：提取质量全靠一堆启发式规则，
而没有测试的启发式规则改一行就可能开始抓导航栏——那种退化在界面上
只表现为"检索结果变怪"，很难归因。
"""

from __future__ import annotations

import pytest

from app.parsers.html_format import (
    extract_article,
    html_to_markdown,
    html_to_text,
)

#: 一个结构典型的中文文章页：导航、广告、正文、页脚、脚本齐全。
ARTICLE_PAGE = """
<html><head>
  <title>页面标题 - 站点名</title>
  <style>.ad { color: red }</style>
  <script>window.track = function () {}</script>
</head><body>
  <header><nav>
    <a href="/">首页</a><a href="/about">关于我们</a><a href="/contact">联系方式</a>
  </nav></header>
  <aside class="sidebar"><div class="ad">立即购买，限时优惠</div></aside>
  <main>
    <article>
      <h1>眼轴长度与近视防控</h1>
      <p>眼轴长度是评估儿童青少年眼球发育情况的重要指标，持续监测有助于判断近视进展速度。</p>
      <h2>测量方法</h2>
      <p>应在散瞳后进行测量，取三次读数取平均值，并记录设备型号与软件版本以保证可比性。</p>
      <ul><li>使用同一台设备</li><li>固定每天同一时段</li></ul>
      <pre><code>AL = 眼轴长度(mm)</code></pre>
      <blockquote>参考国家卫健委发布的适宜技术指南。</blockquote>
    </article>
  </main>
  <footer><p>版权所有 2026 某某机构</p></footer>
</body></html>
"""


# --------------------------------------------------------------------- 正文提取


def test_extracts_the_article_body() -> None:
    body = extract_article(ARTICLE_PAGE)

    assert "眼轴长度是评估儿童青少年眼球发育情况的重要指标" in body
    assert "应在散瞳后进行测量" in body


@pytest.mark.parametrize(
    "noise",
    ["首页", "关于我们", "联系方式", "立即购买", "版权所有", "window.track"],
)
def test_strips_navigation_ads_and_scripts(noise: str) -> None:
    """**剥离噪声是这一层存在的理由。**

    导航与广告进了库会污染检索：用户问什么都能匹配到菜单里的"首页 关于 联系方式"。
    """
    assert noise not in extract_article(ARTICLE_PAGE)


def test_keeps_markdown_structure() -> None:
    """结构不能丢——丢了就退化成一大段纯文本，标题层级与列表全没了。"""
    body = extract_article(ARTICLE_PAGE)

    assert "# 眼轴长度与近视防控" in body
    assert "## 测量方法" in body
    assert "- 使用同一台设备" in body
    assert "```" in body, "代码块围栏丢了"
    assert "AL = 眼轴长度(mm)" in body
    assert "> 参考国家卫健委发布的适宜技术指南。" in body


def test_list_items_are_not_double_spaced() -> None:
    """相邻列表项之间只留单换行——它们是一组，不该被当成两个段落。"""
    body = extract_article(ARTICLE_PAGE)
    assert "- 使用同一台设备\n- 固定每天同一时段" in body


def test_prefers_the_article_container_over_a_single_paragraph() -> None:
    """**不能只按"文本密度"挑。**

    密度最高的永远是单个 ``<p>``（一个标签、几十个字），而正文是一棵子树。
    只按密度选的结果是"只抓到第一个段落"——实测踩到过。
    """
    body = extract_article(ARTICLE_PAGE)
    # 两个段落与列表都在，说明取的是整篇而不是某一段
    assert "测量方法" in body
    assert "固定每天同一时段" in body


def test_falls_back_to_full_text_when_no_article_container() -> None:
    """没有语义容器时也要有产出。

    **返回空文档比返回带噪声的文档更糟**：界面上会显示"已索引"，
    用户以为抓到了，实际什么都没。
    """
    html = (
        "<html><body><div><div>"
        "<p>一段没有 article 包裹的正文内容。</p>"
        "</div></div></body></html>"
    )

    assert "一段没有 article 包裹的正文内容" in extract_article(html)


def test_empty_page_returns_empty() -> None:
    assert extract_article("<html><body></body></html>").strip() == ""


def test_collapses_source_whitespace() -> None:
    """HTML 源码里的换行与缩进是**给人看源码用的**，不是内容的换行。"""
    html = "<html><body><article><p>第一句。\n        第二句。</p></article></body></html>"

    body = extract_article(html)

    assert "第一句。 第二句。" in body
    assert "\n        " not in body


def test_handles_unclosed_paragraphs() -> None:
    """真实网页里 `<p>` 常常不写 `</p>`，解析器必须容忍。"""
    html = "<html><body><article><p>第一段<p>第二段<p>第三段</article></body></html>"

    body = extract_article(html)

    assert "第一段" in body and "第二段" in body and "第三段" in body


def test_handles_uppercase_tags() -> None:
    html = "<HTML><BODY><ARTICLE><H1>标题</H1><P>正文内容。</P></ARTICLE></BODY></HTML>"

    body = extract_article(html)

    assert "# 标题" in body
    assert "正文内容。" in body


# --------------------------------------------------------------------- 纯转换


def test_html_to_markdown_keeps_all_body_content() -> None:
    """不做正文提取时，**正文区里的所有块**都保留（不做"挑一个容器"的判断）。

    注意那只限于"内容"：``nav`` / ``footer`` / ``script`` 这类噪声标签
    **在解析阶段就被丢弃**（见 ``_DROP_TAGS``），所以任何入口都不会把它们
    带出来——那是刻意的，不是这条用例的疏漏。
    """
    body = html_to_markdown(
        "<html><body><div>第一段</div><div>第二段</div><p>第三段</p></body></html>"
    )

    assert "第一段" in body
    assert "第二段" in body
    assert "第三段" in body


def test_markdown_conversion_also_drops_noise_tags() -> None:
    """噪声标签在解析阶段就被丢掉，与走哪个入口无关。"""
    body = html_to_markdown("<html><body><nav>导航</nav><p>正文</p></body></html>")

    assert "正文" in body
    assert "导航" not in body


def test_html_to_text_flattens_markup() -> None:
    text = html_to_text("<p>第一段</p><p>第二段</p>")

    assert "第一段" in text
    assert "第二段" in text
    assert "<p>" not in text


def test_entities_are_decoded() -> None:
    """``&amp;`` 之类不解码的话，正文里会到处是实体名。"""
    html = (
        "<html><body><article>"
        "<p>甲 &amp; 乙 &lt;丙&gt;</p>"
        "</article></body></html>"
    )

    body = extract_article(html)

    assert "甲 & 乙 <丙>" in body


def test_nbsp_is_normalized() -> None:
    body = extract_article("<html><body><article><p>甲\u00a0乙</p></article></body></html>")

    assert "甲 乙" in body
