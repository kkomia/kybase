"""连接器框架（M6 / T6.1；架构 §10）。

**共同抽象**：一个数据源就是"**定期去看某处有没有新内容，有就入库**"。
不管那处是 RSS 订阅、一个网页、还是将来的 WebDAV 目录，流程一样：

```
拉取（带条件 GET）→ 拿到若干「条目」→ 逐条交给摄入流水线
```

差别只在"怎么把响应拆成条目"与"怎么判断有没有变"，所以这两件事由子类实现，
其余（HTTP、条件 GET、变更检测、入库、记账）全在这一层。

**为什么条件 GET 是这套框架的核心**：带 ``If-None-Match``/``If-Modified-Since``
之后，没变的源直接回 304——省的不只是流量，**解析与向量化才是大头**。
所以 ``etag`` 存在数据源记录上，由本层统一维护。

**为什么不需要"已入库条目"表**：摄入侧已经按内容 hash 去重
（``IngestService.register`` → ``get_document_by_hash``）。同一篇文章无论被
RSS 重复列出多少次，hash 一样，第二份会被识别为重复而不重复入库。
再加一张表就是**多余的真相来源**——两张表迟早不一致。

**WebDAV 只留扩展点，不进主链路**（T6.6）：架构 §14 把它列入"缓做"，
这里只在 ``build_connector`` 里留一处明确的"未实现"提醒，而不是写一个
永远走不到的空实现（空实现最容易被误当成做过了）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.exceptions import InvalidRequestError, UpstreamError
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.models.enums import DataSourceKind
from app.storage.base import DataSourceRecord

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "USER_AGENT",
    "Connector",
    "FetchResult",
    "FetchedItem",
    "build_connector",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0

#: 抓取时用的 UA。**必须像个人**：不少站点对 python-httpx 的默认 UA 直接返 403。
USER_AGENT = "kylab/0.1 (+knowledge-base connector)"

#: 单个源一次最多处理多少条。防止第一次订阅一个几年的博客时一口气灌进几千篇。
DEFAULT_MAX_ITEMS = 50


@dataclass(slots=True)
class FetchedItem:
    """拉回来的一个条目，也是"入库单位"。"""

    title: str
    content: bytes
    filename: str
    mime_type: str = "text/markdown"
    source_url: str = ""
    published_at: datetime | None = None


@dataclass(slots=True)
class FetchResult:
    """一次拉取的结果。"""

    items: list[FetchedItem] = field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False
    """服务端回了 304：什么都没变，``items`` 必为空。"""


class Connector(ABC):
    """数据源连接器的共同部分。"""

    #: 该连接器处理的数据源类型。
    kind: DataSourceKind

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client

    # ------------------------------------------------------------------ 子类实现

    @abstractmethod
    def parse(self, response: httpx.Response) -> list[FetchedItem]:
        """把响应拆成条目。子类只需关心这一件事。"""

    # ------------------------------------------------------------------ 公共流程

    def fetch(self, source: DataSourceRecord) -> FetchResult:
        """拉取一个源。

        条件 GET 在这里统一做：子类不必各自处理 304——
        那是最容易漏的一处（漏了就成了"每次全量重下"）。
        """
        url = str(source.config.get("url") or "").strip()
        if not url:
            raise InvalidRequestError(f"数据源「{source.name}」没有配置 URL")
        if not url.startswith(("http://", "https://")):
            # 只允许 http(s)：file:// 之类会变成任意文件读取
            raise InvalidRequestError(f"只支持 http/https 地址：{url}")

        headers = {"User-Agent": USER_AGENT}
        if source.etag:
            headers["If-None-Match"] = source.etag

        try:
            with self._open() as client:
                response = client.get(url, headers=headers, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"拉取「{source.name}」失败：{exc}") from exc

        if response.status_code == 304:
            logger.info("数据源「%s」未变化（304）", source.name)
            return FetchResult(etag=source.etag, not_modified=True)
        if response.status_code >= 400:
            raise UpstreamError(
                f"拉取「{source.name}」失败：HTTP {response.status_code}"
            )

        items = self.parse(response)
        limit = int(source.config.get("max_items") or DEFAULT_MAX_ITEMS)
        if len(items) > limit:
            logger.info(
                "数据源「%s」本次取回 %d 条，按上限截到 %d 条",
                source.name,
                len(items),
                limit,
            )
            items = items[:limit]

        return FetchResult(items=items, etag=response.headers.get("etag"))

    def _open(self) -> httpx.Client:
        """复用外部传入的 client（测试用），否则每次新建。"""
        if self._client is not None:
            return _ReusedClient(self._client)
        return httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)


class _ReusedClient:
    """把外部 client 包成一个上下文管理器，**不负责关闭它**。

    测试注入的 client 由测试自己管生命周期；在这里 close 掉会让同一份
    client 的第二个请求直接失败。
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, *exc_info: object) -> None:
        return None


def build_connector(
    kind: DataSourceKind, *, client: httpx.Client | None = None
) -> Connector:
    """按数据源类型造连接器。

    导入放在函数里：``connectors/`` 下面几个模块互相独立，顶层互相 import
    会成环（也与 ``parsers/`` 同一套纪律，见工程规范 §3.3）。
    """
    if kind is DataSourceKind.RSS:
        from app.services.connectors.rss import RssConnector

        return RssConnector(client=client)
    if kind is DataSourceKind.HTML:
        from app.services.connectors.html import HtmlConnector

        return HtmlConnector(client=client)
    if kind is DataSourceKind.WEBDAV:
        # 明确不做，而不是留一个空实现——空实现最容易被误当成做过了（T6.6）
        raise InvalidRequestError("WebDAV 数据源尚未实现（架构 §14 缓做）")
    raise InvalidRequestError(f"不支持的数据源类型：{kind}")


def now_utc() -> datetime:
    return datetime.now(UTC)
