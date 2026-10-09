"""连接器：RSS 与 HTML（M6 / T6.1–T6.3）。

镜像同构：``app/services/connectors/{base,rss,html}.py`` → 本文件。

**HTTP 全部用 `MockTransport` 拦掉**：这些用例要验的是解析与变更检测，
不是网络。真打网络会让用例变慢、变脆，还依赖别人的站点是否还活着。
"""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import UpstreamError
from app.models.enums import DataSourceKind
from app.services.connectors.base import build_connector
from app.storage.base import DataSourceRecord

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>眼科研究动态</title>
  <item>
    <title>眼轴监测的新进展</title>
    <link>https://example.com/a</link>
    <pubDate>Wed, 10 Sep 2026 08:00:00 GMT</pubDate>
    <content:encoded><![CDATA[
      <p>这是一篇带全文的文章，正文长度明显超过标题，因此应当被直接采用，不必再抓页面。</p>
      <p>第二段进一步说明监测眼轴长度对判断近视进展的价值。</p>
    ]]></content:encoded>
  </item>
  <item>
    <title>只有摘要的条目</title>
    <link>https://example.com/b</link>
    <description><![CDATA[<p>这条只有摘要，但也应当作为内容入库，而不是被当作噪声丢掉。</p>]]></description>
  </item>
</channel>
</rss>
"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom 源</title>
  <entry>
    <title>Atom 条目</title>
    <link rel="self" href="https://example.com/self"/>
    <link rel="alternate" href="https://example.com/entry"/>
    <published>2026-09-09T10:00:00Z</published>
    <summary>Atom 的摘要内容，句子够长以便被当作正文使用。</summary>
  </entry>
</feed>
"""

ARTICLE_HTML = """<html><head><title>站点标题</title></head><body>
  <nav><a href="/">首页</a></nav>
  <article><h1>网页正文标题</h1><p>这是网页的正文内容，足够长以便被提取出来作为文档正文使用。</p></article>
</body></html>
"""


def _source(  # type: ignore[no-untyped-def]
    kind: DataSourceKind,
    url: str = "https://example.com/feed.xml",
    **config,
) -> DataSourceRecord:
    return DataSourceRecord(
        id="ds_1",
        knowledge_base_id="kb_1",
        kind=kind,
        name="测试源",
        config={"url": url, **config},
    )


def _mock(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------- RSS


def test_rss_parses_items_and_metadata() -> None:
    client = _mock(
        lambda request: httpx.Response(
            200, content=RSS_FEED.encode(), headers={"etag": 'W/"v1"'}
        )
    )
    connector = build_connector(DataSourceKind.RSS, client=client)

    result = connector.fetch(_source(DataSourceKind.RSS))

    assert len(result.items) == 2
    assert result.etag == 'W/"v1"'
    assert result.not_modified is False


def test_rss_uses_full_content_when_present() -> None:
    """条目自带全文时**不该再去抓一次页面**——那是对别人站点的无谓请求。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=RSS_FEED.encode())

    connector = build_connector(DataSourceKind.RSS, client=_mock(handler))
    result = connector.fetch(_source(DataSourceKind.RSS))
    first = result.items[0].content.decode()

    assert "这是一篇带全文的文章" in first
    assert calls == ["https://example.com/feed.xml"], "不该为有全文的条目再抓页面"


def test_rss_uses_summary_when_there_is_no_full_content() -> None:
    """只有摘要也要入库。

    **只存标题等于没存**：检索时匹配不到正文，用户问什么都命中不了它。
    """
    client = _mock(lambda request: httpx.Response(200, content=RSS_FEED.encode()))
    connector = build_connector(DataSourceKind.RSS, client=client)

    result = connector.fetch(_source(DataSourceKind.RSS))
    second = result.items[1].content.decode()

    assert "这条只有摘要" in second


def test_rss_follows_the_link_when_neither_content_nor_summary() -> None:
    """大量订阅源只给标题与链接。不跟进的话，进库的就只有一个标题。"""
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>源</title>
    <item><title>外链条目</title><link>https://example.com/article</link></item>
    </channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "article" in str(request.url):
            return httpx.Response(200, text=ARTICLE_HTML)
        return httpx.Response(200, content=feed.encode())

    connector = build_connector(DataSourceKind.RSS, client=_mock(handler))
    result = connector.fetch(_source(DataSourceKind.RSS))

    assert len(result.items) == 1
    assert "这是网页的正文内容" in result.items[0].content.decode()


def test_rss_reports_the_source_and_date_in_the_header() -> None:
    """抬头让引用能指回原文，也是判断时效性的依据。"""
    client = _mock(lambda request: httpx.Response(200, content=RSS_FEED.encode()))
    connector = build_connector(DataSourceKind.RSS, client=client)

    first = connector.fetch(_source(DataSourceKind.RSS)).items[0].content.decode()

    assert "来源：眼科研究动态" in first
    assert "发布：2026-09-10" in first
    assert "原文：https://example.com/a" in first


def test_rss_parses_atom_feeds_too() -> None:
    """RSS 2.0 与 Atom 字段名不同但语义一一对应，两者都要认——
    只支持一种会让一半的源抓不到内容。"""
    client = _mock(lambda request: httpx.Response(200, content=ATOM_FEED.encode()))
    connector = build_connector(DataSourceKind.RSS, client=client)

    result = connector.fetch(_source(DataSourceKind.RSS))

    assert len(result.items) == 1
    body = result.items[0].content.decode()
    assert "Atom 的摘要内容" in body
    # rel="alternate" 才是给人看的页面，rel="self" 是订阅源自己
    assert "https://example.com/entry" in body
    assert "https://example.com/self" not in body


def test_rss_filename_is_readable() -> None:
    """文件名用标题而不是 guid：列表里要认出"这是哪一篇"。"""
    client = _mock(lambda request: httpx.Response(200, content=RSS_FEED.encode()))
    connector = build_connector(DataSourceKind.RSS, client=client)

    first = connector.fetch(_source(DataSourceKind.RSS)).items[0]

    assert "眼轴监测的新进展" in first.filename
    assert first.filename.endswith(".md")


def test_rss_skips_entries_without_usable_body() -> None:
    """没有正文、又抓不到原文的条目**跳过**，而不是入库一篇空文档。"""
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>源</title>
    <item><title>空条目</title></item></channel></rss>"""
    client = _mock(lambda request: httpx.Response(200, content=feed.encode()))
    connector = build_connector(DataSourceKind.RSS, client=client)

    assert connector.fetch(_source(DataSourceKind.RSS)).items == []


def test_rss_rejects_non_xml() -> None:
    client = _mock(lambda request: httpx.Response(200, text="这不是 XML"))
    connector = build_connector(DataSourceKind.RSS, client=client)

    with pytest.raises(ValueError):
        connector.fetch(_source(DataSourceKind.RSS))


# --------------------------------------------------------------------- 条件 GET


def test_sends_if_none_match_and_handles_304() -> None:
    """**条件 GET 是这套框架的核心**：没变的源直接回 304，
    省的不只是流量——解析与向量化才是大头。
    """
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(304)

    connector = build_connector(DataSourceKind.RSS, client=_mock(handler))
    source = _source(DataSourceKind.RSS)
    source.etag = 'W/"v1"'

    result = connector.fetch(source)

    assert result.not_modified is True
    assert result.items == []
    assert seen[0].get("if-none-match") == 'W/"v1"', "没有带上 If-None-Match"


def test_does_not_send_if_none_match_without_an_etag() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, content=RSS_FEED.encode())

    connector = build_connector(DataSourceKind.RSS, client=_mock(handler))
    connector.fetch(_source(DataSourceKind.RSS))

    assert "if-none-match" not in seen[0]


def test_http_error_becomes_upstream_error() -> None:
    client = _mock(lambda request: httpx.Response(500))
    connector = build_connector(DataSourceKind.RSS, client=client)

    with pytest.raises(UpstreamError) as excinfo:
        connector.fetch(_source(DataSourceKind.RSS))
    assert "500" in str(excinfo.value)


def test_missing_url_is_rejected() -> None:
    from app.core.exceptions import InvalidRequestError

    client = _mock(lambda request: httpx.Response(200))
    connector = build_connector(DataSourceKind.RSS, client=client)

    with pytest.raises(InvalidRequestError):
        connector.fetch(_source(DataSourceKind.RSS, url=""))


def test_non_http_scheme_is_rejected() -> None:
    """只允许 http(s)：``file://`` 之类会变成任意文件读取。"""
    from app.core.exceptions import InvalidRequestError

    client = _mock(lambda request: httpx.Response(200))
    connector = build_connector(DataSourceKind.RSS, client=client)

    with pytest.raises(InvalidRequestError):
        connector.fetch(_source(DataSourceKind.RSS, url="file:///etc/passwd"))


def test_max_items_limits_the_batch() -> None:
    """第一次订阅一个几年的博客时，一口气灌几千篇是不合适的。"""
    items = [
        f"<item><title>条目 {i}</title><description>第 {i} 条的摘要内容。</description></item>"
        for i in range(30)
    ]
    many = "".join(items)
    feed = (
        '<?xml version="1.0"?><rss version="2.0">'
        f"<channel><title>源</title>{many}</channel></rss>"
    )
    client = _mock(lambda request: httpx.Response(200, content=feed.encode()))
    connector = build_connector(DataSourceKind.RSS, client=client)

    result = connector.fetch(_source(DataSourceKind.RSS, max_items=5))

    assert len(result.items) == 5


# --------------------------------------------------------------------- HTML


def test_html_connector_extracts_the_page() -> None:
    client = _mock(lambda request: httpx.Response(200, text=ARTICLE_HTML))
    connector = build_connector(DataSourceKind.HTML, client=client)

    result = connector.fetch(_source(DataSourceKind.HTML, url="https://example.com/page"))

    assert len(result.items) == 1
    body = result.items[0].content.decode()
    assert "这是网页的正文内容" in body
    assert "首页" not in body, "导航没被剥离"
    assert "抓取：" in body, "应当记下抓取时刻，方便判断版本时效"


def test_html_connector_produces_nothing_for_an_empty_page() -> None:
    """抓不到正文时**不产出条目**：入库一篇空文档比不入库更糟——
    界面上会显示"已索引"，用户以为成功了。"""
    client = _mock(lambda request: httpx.Response(200, text="<html><body></body></html>"))
    connector = build_connector(DataSourceKind.HTML, client=client)

    assert connector.fetch(_source(DataSourceKind.HTML, url="https://example.com/x")).items == []


def test_webdav_is_explicitly_unimplemented() -> None:
    """WebDAV 只留扩展点（T6.6）。

    **明确报"未实现"而不是返回空实现**：空实现最容易被误当成做过了。
    """
    from app.core.exceptions import InvalidRequestError

    with pytest.raises(InvalidRequestError) as excinfo:
        build_connector(DataSourceKind.WEBDAV)
    assert "WebDAV" in str(excinfo.value)
