"""HTML 网页连接器（M6 / T6.2）。

一个 HTML 数据源 = **一个网页 URL**，每次拉取把它当一篇文档入库。

**与 RSS 的分工**：RSS 是"订阅一个会不断出新内容的源"，
HTML 是"盯着某一个页面"（比如一份持续更新的规范、一个状态页）。
两者的共同部分（条件 GET、变更检测、入库）在 ``base.py``，
这里只管"把响应变成一篇 Markdown"。

**为什么不做"抓整站"**：那要爬链接、判重、限速、robots——是一整套爬虫工程，
而本项目要解决的是"把某一份资料纳入知识库"。盯着一个页面足够，
也**不会因为误抓把别人的站点打垮**。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from app.models.enums import DataSourceKind
from app.parsers.html_format import extract_article
from app.services.connectors.base import Connector, FetchedItem

__all__ = ["HtmlConnector"]

logger = logging.getLogger(__name__)


class HtmlConnector(Connector):
    """把一个网页抓成一篇 Markdown。"""

    kind = DataSourceKind.HTML

    def parse(self, response: httpx.Response) -> list[FetchedItem]:
        body = extract_article(response.text)
        if not body.strip():
            # 抓不到正文就**不产出条目**：入库一篇空文档比不入库更糟——
            # 界面上会显示"已索引"，用户以为成功了
            logger.warning("页面没有提取到正文：%s", response.url)
            return []

        title = str(response.url).rstrip("/").rsplit("/", 1)[-1] or "网页"
        heading = _first_heading(body)
        if heading:
            title = heading
        elif response.text:
            title = _title_tag(response.text) or title

        # 抬头记下原文地址与抓取时刻：用户回看时要能判断"这是什么时候的版本"
        fetched_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        header = f"# {title}\n\n来源：{response.url}  抓取：{fetched_at}\n\n"

        return [
            FetchedItem(
                title=title,
                content=(header + body.strip() + "\n").encode("utf-8"),
                filename=_filename_for(title, response.url),
                mime_type="text/markdown",
                source_url=str(response.url),
            )
        ]


def _first_heading(markdown: str) -> str:
    """正文里的一级标题——比 URL slug 更像人话。"""
    match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _title_tag(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _filename_for(title: str, url: str) -> str:
    """文件名用标题；标题取不到时退回 URL 的最后一段。"""
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "-", title).strip("-")[:80]
    if not safe:
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "-", url).strip("-")[-40:] or "page"
    return f"{safe}.md"
