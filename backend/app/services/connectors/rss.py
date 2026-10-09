"""RSS / Atom 订阅连接器（M6 / T6.3）。

**"重复拉取不产生重复文档"靠什么**：摄入侧的内容 hash 去重
（``IngestService.register`` 先查 ``get_document_by_hash``）。同一篇文章无论被
订阅源列出多少次，正文 hash 一样，第二份会被判为重复而不再入库。
所以这里**不需要**再维护一张"已抓过的条目"表——两张表迟早不一致，
而 hash 是唯一的真相。

**条目的正文从哪来**：

1. 条目自带 ``content:encoded``（全文）→ 直接用，最理想；
2. 只有 ``description``（摘要）→ 用它。**摘要也是内容**：很多源不给全文，
   而"只存标题"等于没存——检索时匹配不到正文；
3. 都没有 → 从 ``<link>`` 把那篇文章抓下来提正文（**这是关键一步**：
   大量订阅源只给标题与链接，不跟进就只剩一个标题）。
"""

from __future__ import annotations

import logging
import re
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from app.models.enums import DataSourceKind
from app.parsers.html_format import extract_article
from app.services.connectors.base import Connector, FetchedItem

__all__ = ["RssConnector"]

logger = logging.getLogger(__name__)

#: Atom 与 RSS 的命名空间
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

#: 单条正文的长度上限。订阅源偶尔会塞进整本书——截断而不是拒绝，
#: 因为它仍然是有效内容
MAX_ITEM_CHARS = 200_000


class RssConnector(Connector):
    """把 RSS 2.0 与 Atom 归一成同一批条目。"""

    kind = DataSourceKind.RSS

    def parse(self, response: httpx.Response) -> list[FetchedItem]:
        """解析订阅源。

        **RSS 2.0 与 Atom 都要认**：两者字段名不同（``item``/``entry``、
        ``pubDate``/``published``、``description``/``summary``），
        但语义一一对应。只支持其中一种会让一半的源抓不到内容。
        """
        try:
            root = ElementTree.fromstring(response.content)  # noqa: S314
        except ElementTree.ParseError as exc:
            raise ValueError(f"订阅源不是合法 XML：{exc}") from exc

        entries = root.findall(".//item") or root.findall(".//atom:entry", _NS)
        if not entries:
            logger.info("订阅源里没有条目")
            return []

        feed_title = _feed_title(root)
        items: list[FetchedItem] = []
        for entry in entries:
            item = self._build_item(entry, feed_title)
            if item is not None:
                items.append(item)
        return items

    # ------------------------------------------------------------------ 内部

    def _build_item(self, entry: ElementTree.Element, feed_title: str) -> FetchedItem | None:
        title = _text(entry, "title") or _text(entry, "atom:title") or "（无标题）"
        link = _text(entry, "link") or _atom_link(entry)

        body = (
            _text(entry, "content:encoded")
            or _text(entry, "atom:content")
            or _text(entry, "description")
            or _text(entry, "atom:summary")
        )

        if body and len(body.strip()) > len(title) + 20:
            # 有像样的正文（明显长于标题）就直接用，不必再抓一次页面
            content = _html_fragment_to_markdown(body)
        elif link:
            # 只给了标题与链接：**跟进原文**。不做这一步的话，大量订阅源
            # 进库的就只有一个标题——检索时毫无用处
            fetched = self._fetch_article(link)
            if fetched is None:
                # 抓不到原文就退回摘要，总比什么都没有强
                content = _html_fragment_to_markdown(body) if body else ""
            else:
                content = fetched
        else:
            content = _html_fragment_to_markdown(body) if body else ""

        if not content.strip():
            logger.info("条目「%s」没有可用正文，跳过", title)
            return None

        published = _published_at(entry)
        markdown = _with_title(title, content, source=feed_title, url=link, published=published)

        return FetchedItem(
            title=title,
            content=markdown.encode("utf-8"),
            filename=_filename_for(title, published),
            mime_type="text/markdown",
            source_url=link,
            published_at=published,
        )

    def _fetch_article(self, url: str) -> str | None:
        """把条目链接指向的网页抓下来提正文。

        失败**不抛**：一条抓不到不该让整个源失败——其余条目照样入库，
        而下一次拉取还会再试。
        """
        try:
            with self._open() as client:
                response = client.get(
                    url,
                    headers={"User-Agent": _user_agent()},
                    follow_redirects=True,
                )
            if response.status_code >= 400:
                return None
            return extract_article(response.text)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("条目原文抓取失败（跳过该条正文）：%s — %s", url, exc)
            return None


def _user_agent() -> str:
    from app.services.connectors.base import USER_AGENT

    return USER_AGENT


# --------------------------------------------------------------------- 解析助手


def _text(entry: ElementTree.Element, path: str) -> str:
    """取一个子元素的文本，找不到返回空串。"""
    found = entry.find(path, _NS)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _atom_link(entry: ElementTree.Element) -> str:
    """Atom 的链接在 ``href`` 属性上，而且可能有多个（alternate/self）。

    优先取 ``rel="alternate"``——那才是给人看的页面；``self`` 是订阅源自己。
    """
    fallback = ""
    for link in entry.findall("atom:link", _NS):
        href = link.get("href") or ""
        if not href:
            continue
        if link.get("rel") in (None, "alternate"):
            return href
        fallback = fallback or href
    return fallback


def _feed_title(root: ElementTree.Element) -> str:
    channel = root.find("channel")
    if channel is not None:
        return _text(channel, "title")
    return _text(root, "atom:title")


def _published_at(entry: ElementTree.Element):  # type: ignore[no-untyped-def]
    """发布时间。认不出就返回 ``None``——**不编一个当前时间**：
    那会让所有条目的时间都变成"抓取时刻"，按时间排序就失去意义。
    """
    for path in ("pubDate", "atom:published", "atom:updated", "dc:date"):
        raw = _text(entry, path)
        if not raw:
            continue
        try:
            if path == "pubDate":
                return parsedate_to_datetime(raw)
            return _parse_iso(raw)
        except (TypeError, ValueError):
            continue
    return None


def _parse_iso(raw: str):  # type: ignore[no-untyped-def]
    from datetime import datetime

    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # 只到日期（`2026-09-11`）也是合法的 Atom 时间
        return datetime.fromisoformat(text[:10])


def _html_fragment_to_markdown(raw: str) -> str:
    """条目正文常是 HTML 片段。

    片段里通常没有 ``<article>``，所以走整段转换而不是正文提取——
    提取的启发式规则在"只有一段"时会挑走半个片段（实测过）。
    """
    from app.parsers.html_format import html_to_markdown

    return html_to_markdown(raw)


def _with_title(title: str, content: str, *, source: str, url: str, published) -> str:  # type: ignore[no-untyped-def]
    """给正文加一层来源抬头。

    抬头让引用能指回原文（"这条来自哪个源、哪一天"），
    也是回看时判断时效性的依据。
    """
    lines = [f"# {title}", ""]
    meta = []
    if source:
        meta.append(f"来源：{source}")
    if published is not None:
        meta.append(f"发布：{published.strftime('%Y-%m-%d')}")
    if url:
        meta.append(f"原文：{url}")
    if meta:
        lines.extend(["  ".join(meta), ""])
    lines.append(content.strip())
    return "\n".join(lines).strip() + "\n"


def _filename_for(title: str, published) -> str:  # type: ignore[no-untyped-def]
    """给条目起个可读的文件名。

    用标题而不是 guid：用户在文档列表里要认出"这是哪一篇"，
    而 guid 通常是一串无意义的十六进制。
    """
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "-", title).strip("-")[:80] or "entry"
    if published is not None:
        return f"{published.strftime('%Y%m%d')}-{safe}.md"
    return f"{safe}.md"
