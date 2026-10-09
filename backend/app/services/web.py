"""上网：抓一个网页 / 搜一次网（v0.22）。

**为什么需要它**：在这之前，Agent 的工具面**只有知识库那一侧**（检索、文档、
笔记、记忆、导出）。于是任何问题——包括"查今天的新闻"——都只能往知识库里找，
找不到就回一句"你的库里没有"。用户看到的症状是"这还是个检索框架"，
而真正的原因是**它没有别的事可做**。这一层补的就是"别的事"。

三家主流预装技能里，"联网检索"与"抓网页"都是共识项（见《预装技能选型》§2 的
P0 第 3、4 项），也都在这里落地。

## 两条边界，都写在明处

1. **抓取要挡内网。** 这里的 URL 是**模型给的**，而它可能被网页内容带着走。
   只允许 http/https 不够——`http://192.168.31.18:54321/` 是这台机器上的
   PG，`http://127.0.0.1:8000/api/v1/...` 是 KYLAB 自己。
   所以：**解析主机名、拒绝环回 / 私有 / 链路本地 / 保留地址**，
   并在**每一次重定向之后重判**（重定向是绕过这类检查最常用的手法）。
   残余风险写在 :func:`_host_is_public` 上（DNS rebinding 没覆盖）。
2. **搜索要一个供应商。** 没有免密钥又稳定的通用搜索接口，
   所以它是一项**配置**：没配就明确说"去哪配"，而不是返回空结果
   ——空结果会被模型读成"网上没有这件事"。

抓回来的正文**当资料用**：它进对话上下文，所以在工具结果里标注了来源 URL，
模型引用时能说清是哪一页。它**不进知识库**：入库是另一个动作（`upload_document`
或数据源），把"看过一眼"和"收进库里"分开——否则库会被一次点击攒下的东西占满。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.exceptions import InvalidRequestError, UpstreamError
from app.core.html_format import extract_article
from app.core.http import shared_client
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）

__all__ = [
    "MAX_FETCH_BYTES",
    "MAX_FETCH_CHARS",
    "SearchHit",
    "check_public_url",
    "fetch_url",
    "search_web",
]

logger = logging.getLogger(__name__)

#: 一次抓多少字节。网页正文通常几十 KB，2 MB 是为了**挡住意外的大文件**
#: （视频、打包资源）——它们读进来只是浪费上下文。
MAX_FETCH_BYTES = 2 * 1024 * 1024

#: 抽出来的正文最多回给模型多少字。工具结果是要进上下文的，而一篇长文
#: 会把预算吃光；超了**截断并说明**（悄悄截断会让模型以为文章就那么长）。
MAX_FETCH_CHARS = 30_000

#: 搜索结果条数上限与单条摘要长度。
#:
#: 摘要上限从 500 提到 1200（v0.39）：**Tavily 原生就给 560–1390 字**，
#: 500 那道刀口把 5 条里的 4 条切掉了（实测）——那是白扔的材料。
#: 而条数对延迟没有影响：同一查询 `max_results=5` 与 `10` 都是 1.2–2.2 秒
#: （各两次实测）。材料多给一点，模型就少一次"没看清、再搜一遍/再抓一页"的往返。
MAX_HITS = 10
MAX_SNIPPET_CHARS = 1200

_TIMEOUT_SECONDS = 20.0

#: 搜索接口的超时（v0.26 起；v0.39 从 10 秒放宽到 20，与抓页齐平）。
#:
#: 原来定 10 秒的账是"搜索是回 JSON 的接口，Tavily 中位 2.2 秒，20 秒只会在它挂了时白等"。
#: 2026-09-21 复测把这条账推翻了：同一个查询、同一份代码，
#: **6 次实测是 2.55 / 3.28 / 3.58 / 3.84 / 11.38 / 12.36 秒**（中位 3.84、最慢 12.36），
#: 而同一条请求**绕过我们全部代码**直发也只要 4.30–7.75 秒、DNS 只要 0–16 毫秒——
#: 也就是说慢在 provider 与到它的链路，跟我们的解析、条数、渲染都无关。
#: 10 秒的闸门会**误杀活着但慢的搜索**：那一刀砍掉的不只是这 10 秒，
#: 还有模型接下来那一轮（它会换个词重搜或干脆放弃）。
#: 所以放宽到与抓页同一个预算；一道更粗的闸在工具循环那边（一轮 300 秒墙钟）。
_SEARCH_TIMEOUT_SECONDS = 20.0

#: 抓取 UA。与数据源连接器同一口径：不少站点对 httpx 默认 UA 直接 403。
_USER_AGENT = "kylab/0.1 (+agent web tool)"

#: 认这三种内容类型。其余（图片、压缩包、PDF）不是"网页正文"，
#: 而把它们当文本解码出来的东西毫无意义。
_TEXTUAL = ("text/html", "text/plain", "application/xhtml", "application/xml", "text/xml")

#: 一次抓取最多跟几次重定向。跟得越多，绕开内网检查的机会越多（每次都重判）。
_MAX_REDIRECTS = 5


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一条搜索结果。``url`` 是给 :func:`fetch_url` 用的下一步。"""

    title: str
    url: str
    snippet: str = ""
    published: str = ""


def check_public_url(url: str) -> str:
    """校验一个 URL 能不能抓；能就回规范化后的地址，不能就抛。

    挡的是**内网探测**：这个 URL 来自模型，而模型可能正被一段网页内容牵着走。
    检查三件事：协议、主机名解析出来的地址、以及"解析不出来"这个情况。

    **没覆盖 DNS rebinding**：这里查的是解析结果，而实际连接时 httpx 会**再解析一次**，
    两次之间可以变（攻击者控制 DNS 时）。要堵死得把连接钉在已校验的那个 IP 上
    （`httpx` 的 transport 层改写），代价是 TLS SNI 与 Host 头都要自己处理。
    在当前这个形态下（单机自用、工具调用是用户自己按下去的一轮对话）
    不值得，但**必须写出来**——不然读代码的人会以为这一层是完备的。
    """
    raw = (url or "").strip()
    if not raw:
        raise InvalidRequestError("缺少参数：url")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        # file:// 会变成任意文件读取，其它协议更没边
        raise InvalidRequestError(f"只支持 http/https 地址：{raw}")
    host = parsed.hostname or ""
    if not host:
        raise InvalidRequestError(f"地址里没有主机名：{raw}")
    if not _host_is_public(host):
        raise InvalidRequestError(
            f"这个地址指向本机或内网（{host}），不能抓。"
            "联网工具只访问公网地址——内网里有数据库和管理接口，"
            "而模型不该有能力去探它们"
        )
    return raw


def _host_is_public(host: str) -> bool:
    """主机名解析出来的地址**全都**是公网地址才算通过。

    全都要是：一个域名可以解析出多个 A 记录，只查第一个的话，
    攻击者把内网地址放在第二位就能过。
    """
    literal = _ip_or_none(host)
    if literal is not None:
        return _ip_is_public(literal)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 解析不出来 = 抓不到，但**不能当成通不过**：断网环境下这会变成
        # "所有地址都被拒"，而用户看到的是一句"内网地址"，完全对不上原因。
        # 交给真正的请求去失败，错误信息更准。
        return True
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return True
    return all(_ip_is_public(ipaddress.ip_address(address)) for address in addresses)


def _ip_or_none(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _ip_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """环回、私有、链路本地、保留、组播一律不算公网。

    `is_global` 一条就够（它把这些都包含了），但**逐条写出来**是为了让人看得见
    到底挡了什么：`is_global` 的语义在 Python 版本之间调整过，
    而这些类别是稳定的常识。
    """
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def fetch_url(
    url: str, *, limit: int = MAX_FETCH_CHARS, timeout: float = _TIMEOUT_SECONDS
) -> tuple[str, str]:
    """抓一个网页，回 ``(标题, Markdown 正文)``。

    只做"取回并变成可读文本"，不做摘要（那是模型的事）、不落库（那是另一个动作）。

    ``timeout`` 是**这一页的墙钟上限**，不是"每次读写操作的上限"——两者不一样，
    见 ``_read_capped``。搜索顺带抓节选用的是一个更短的预算（5 秒）：
    一次搜索不该为了某一页的开头等上二十秒。
    """
    target = check_public_url(url)
    try:
        # 共享客户端 + **不跟随重定向**（与以前逐字一致：httpx 默认就是不跟随，
        # 而 `_get_with_checks` 自己按跳校验地址）
        text, final_url = _get_with_checks(shared_client(), target, timeout=timeout)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"抓取失败：{exc}") from exc

    content_type = ""
    markdown = ""
    if "<" in text[:2000]:  # 粗判是否 HTML：省一次解析，也让纯文本原样返回
        markdown = extract_article(text)
        content_type = "html"
    if not markdown.strip():
        markdown = text
        content_type = content_type or "text"
    title = _title_of(text) or final_url
    body = markdown.strip()
    if len(body) > limit:
        body = body[:limit] + f"\n\n（正文过长已截断，以上是前 {limit} 字）"
    logger.info("抓取 %s（%s）：%d 字", final_url, content_type or "text", len(body))
    return title, body


def _get_with_checks(client: httpx.Client, target: str, *, timeout: float) -> tuple[str, str]:
    """手工跟重定向，**每一跳都重新校验地址**。

    不用 ``follow_redirects=True`` 就是因为那样没有"每一跳"这个位置：
    公网地址 302 到 ``http://127.0.0.1:8000`` 是最常见的绕过手法。

    ``timeout`` 同时用作 httpx 的单次操作超时与**整页的墙钟上限**：后者才是真的闸门
    （前者会被"一直慢慢吐字节"的页面反复重置，实测一页能拖到 33 秒，
    见 ``_read_capped``）。整个抓取共用一个 deadline，重定向也算在里面——
    跟五跳就是五次机会，不该变成五倍的等待。
    """
    deadline = time.monotonic() + timeout
    current = target
    for _ in range(_MAX_REDIRECTS + 1):
        with client.stream(
            "GET",
            current,
            headers={"User-Agent": _USER_AGENT},
            # 超时按调用点给：共享客户端自带的那个只是兜底（见 app/core/http.py）
            timeout=timeout,
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location") or ""
                if not location:
                    raise UpstreamError(f"重定向没有 Location 头：{current}")
                current = check_public_url(str(httpx.URL(current).join(location)))
                continue
            if response.status_code >= 400:
                raise UpstreamError(f"抓取失败：HTTP {response.status_code}")
            content_type = (response.headers.get("content-type") or "").lower()
            if content_type and not any(item in content_type for item in _TEXTUAL):
                raise UpstreamError(
                    f"这个地址返回的是 {content_type.split(';')[0]}，不是网页正文。"
                    "要把它收进知识库请用 upload_document / 数据源"
                )
            body = _read_capped(response, deadline=deadline, timeout=timeout)
            return body, current
    raise UpstreamError(f"重定向次数过多（超过 {_MAX_REDIRECTS} 次）")


def _read_capped(response: httpx.Response, *, deadline: float, timeout: float) -> str:
    """按上限读，**边读边停**：等整个响应下完再截断，等于把那个大文件也下载了。

    **两道闸**：字节数（`MAX_FETCH_BYTES`）与墙钟（``deadline``）。
    墙钟这道是必须的：httpx 的 ``timeout`` 是**每次读操作**的上限，一个页只要
    一直慢慢吐字节就能把它无限刷新——实测某新闻站首页抓了 **33 秒**（2026-09-21，
    8 条结果里最慢的三条是 25/24/23 秒）。那种页面在搜索顺带抓节选时是灾难：
    一次搜索为了某一页的开头等三十秒，比它想省下的那次模型往返（1.2–4.4 秒）贵得多。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= MAX_FETCH_BYTES:
            logger.info("响应超过 %d 字节，提前停止读取", MAX_FETCH_BYTES)
            break
        if time.monotonic() > deadline:
            # **不返回半截正文**：读了一半的页面看起来像完整的，而模型不会去猜
            # "后面是不是被截了"。抛出去，由调用方就地写一行"这一页没读成"。
            raise UpstreamError(f"这一页读得太慢（超过 {timeout:g} 秒），已放弃")
    raw = b"".join(chunks)
    encoding = response.encoding or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:  # 服务端报了一个我们不认识的编码
        return raw.decode("utf-8", errors="replace")


def _title_of(html: str) -> str:
    """``<title>`` 里的文本（取不到就空）。够用，不值得为它写一个解析器。"""
    start = html.lower().find("<title")
    if start < 0:
        return ""
    start = html.find(">", start)
    end = html.lower().find("</title>", start)
    if start < 0 or end < 0:
        return ""
    return " ".join(html[start + 1 : end].split())[:200]


# ------------------------------------------------------------------ 搜索


#: 认识的搜索供应商：键是配置里填的名字，值是（地址、读结果的两个字段名）。
#:
#: 只做两家的原因：它们的返回**都是 JSON**（不引第三方 SDK），
#: 而且各覆盖一个常见选择（Tavily 面向 agent、博查在国内可直连）。
#: 加第三家就是再加一行——但**不预置一堆用不上的**：没配的就是没配。
SEARCH_PROVIDERS = {
    "tavily": {
        "url": "https://api.tavily.com/search",
        "label": "Tavily",
        "key_field": "api_key",
    },
    "bocha": {
        "url": "https://api.bochaai.com/v1/web-search",
        "label": "博查 Bocha",
        "key_field": "Authorization",
    },
}


def search_web(
    query: str, *, api_key: str, provider: str = "tavily", limit: int = 8
) -> list[SearchHit]:
    """搜一次网，回若干条结果。

    **没有免密钥的通用搜索**，所以调用方必须给 key（见模块头第 2 条）。
    """
    text = (query or "").strip()
    if not text:
        raise InvalidRequestError("缺少参数：query")
    name = (provider or "tavily").strip().lower()
    spec = SEARCH_PROVIDERS.get(name)
    if spec is None:
        raise InvalidRequestError(
            f"不认识的搜索供应商：{provider}（可用：{'、'.join(SEARCH_PROVIDERS)}）"
        )
    if not (api_key or "").strip():
        raise InvalidRequestError(
            "没有配置联网搜索的密钥。到「能力」页的「联网」里填一个搜索服务商的密钥"
            "（支持 Tavily 与博查），或者改用 web_fetch 直接抓一个你知道的网址"
        )
    count = max(1, min(int(limit), MAX_HITS))
    payload = _search_payload(name, text, api_key.strip(), count)
    try:
        response = shared_client().post(spec["url"], json=payload, timeout=_SEARCH_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"搜索失败：{exc}") from exc
    if response.status_code >= 400:
        # 把状态码与响应体前一段带出来：401 是密钥错、429 是额度，
        # 这两类模型改不了但**用户能**——所以话要说全
        raise UpstreamError(f"搜索服务返回 {response.status_code}：{response.text[:200]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise UpstreamError("搜索服务返回的不是 JSON") from exc
    return _hits_of(body, name)[:count]


def _search_payload(provider: str, query: str, api_key: str, limit: int) -> dict:
    """两家请求体的差别（**只有这两处**，所以不值得为它做一层抽象）。"""
    if provider == "bocha":
        return {"query": query, "count": limit, "summary": True}
    return {"query": query, "max_results": limit, "api_key": api_key}


def _hits_of(body: dict, provider: str) -> list[SearchHit]:
    """把两家的返回摊成同一个形状。

    认不出结构时**回空列表**（与记忆层相反）：搜索结果是"网上有哪些页"，
    取不到时"没搜到"与"格式变了"对模型的下一步（换个词再搜 / 自己抓）没区别，
    而抛错会把整轮打断。
    """
    if provider == "bocha":
        pages = (((body or {}).get("data") or {}).get("webPages") or {}).get("value") or []
    else:
        pages = (body or {}).get("results") or []
    hits: list[SearchHit] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = str(page.get("url") or "")
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(page.get("title") or url),
                url=url,
                snippet=str(page.get("content") or page.get("snippet") or "")[:MAX_SNIPPET_CHARS],
                published=str(page.get("published_date") or page.get("datePublished") or ""),
            )
        )
    return hits
