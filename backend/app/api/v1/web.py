"""本机档的网页取页端点（对话面板「阅读模式」用）。

**它解决什么**：对话里出现一个链接，用户想"在这一侧把那一页读进来"。两种读法——
① 把正文抓回来（``GET /web/page``）；② 让浏览器把原页整张嵌进来（``GET /web/embed-check``
先问一句"对方让不让嵌"）。这一组就是那两件事的出口。

## 为什么出口在本机（所以只挂本机档）

两个请求都是**这台机器**替界面发出去的：桌面壳里读到哪一页，由这台机器去取。
服务器档不挂它（挂法见 ``api/v1/router.py`` 的 ``local_router`` 那一段）——
NAS 那一侧没有"某个用户正在读一页"这个场景，挂上去只会多出一条
"能被外部打进来、却没人调用"的抓取口。

## 两条硬口径

1. **SSRF 闸只有一道**：``services/web.py`` 的 ``check_public_url``（内网 / 本机 /
   云元数据一律拒，返回的地址就是规范化后的那个）。这一层**不重写、不绕开**它——
   那条路里连"每一次重定向之后重判"都已经有了（见 ``_get_with_checks``），
   抓正文也整条走 ``fetch_url``，这里一行抓取逻辑都没有；
2. **不做缓存**（产品层已拍板）：阅读模式读的是"这一页现在长什么样"，缓存会让用户
   看到上一次的内容——"看起来对、其实是旧的"比慢一点糟得多。

## 这一层刻意不做的事（写在这里，免得下一个人以为是漏了）

- ``embed-check`` 只发**一次** GET：拿到响应头就断流、不读 body、不用 HEAD
  （HEAD 兼容性差，一堆站点回 405 或直接掐断）。跳转后的那一页**不追投**——
  追下去就变成一次爬取，而这条端点的全部价值在"快"；
- 判定**只在这一处**（``_judge_embeddable``）：前端不再自己对着那两条头判一遍——
  两处判必然分叉，而分叉的表现是"界面说能嵌、浏览器拒载"；
- 探测失败（连不上 / 超时 / 跳走了）一律 **200 + embeddable=false**，不抛 502：
  探不到对方不该把整条阅读模式挡住，它只是"这一页改用正文读"。

## ``final_url`` 的实情（**如实写清**）

响应里有 ``final_url``，可服务层的 ``fetch_url`` 只回 ``(标题, 正文)``——它内部算出了
最终地址（``_get_with_checks`` 返回的那个 ``current``）却没有往外传。这一层**不为了
一个字段去改那个签名**：它是工具循环（``services/tools.py`` 的 ``web_fetch``）与这一组
端点共用的那条链，动它要连带核一遍别处，而那不属于这次改动。所以 ``final_url``
**现在等于请求的那个地址**；要拿真的最终地址，得让 ``fetch_url`` 多回一个值。
（间接线索是有的：没有 ``<title>`` 的页面，``title`` 会回落成最终地址——见 ``fetch_url``。）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.auth import require_read
from app.core.caller import Caller
from app.core.exceptions import BadRequestError, InvalidRequestError
from app.core.http import shared_client
from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.services.web import MAX_FETCH_CHARS, check_public_url, fetch_url

router = APIRouter(prefix="/web", tags=["web"])

#: 探测嵌入用的 UA。与 ``services/web.py`` 那个同一条口径（不少站点对 httpx 默认 UA
#: 直接 403），但**不复用它的私有常量**：那条链的标识是"抓正文"，这条是"只讨响应头"。
_USER_AGENT = "kylab/0.1 (+local reading mode)"

#: 探测的墙钟上限。比抓正文那个 20 秒短：它只等响应头，而用户正盯着界面等这一下。
#: 超了按"不能嵌"回（见 ``_probe_framing_headers``），比让面板干转 20 秒合算。
_PROBE_TIMEOUT_SECONDS = 10.0

#: 失败原因里带进来的异常原文上限（与 ``services/web.py`` 搜索报错那条同一手法）：
#: 异常原文可能很长，截断免得一句话变成一屏。
_REASON_DETAIL_CHARS = 200


class WebPageOut(BaseModel):
    """``GET /web/page``：一页的正文——**给阅读模式渲染的那一份**。

    ``text`` 是 Markdown（服务层用 ``extract_article`` 抽过正文），最多
    ``MAX_FETCH_CHARS`` 字；``truncated`` 是"被截过"这件事本身，界面据此说一句
    "只读了前一段"，而不是让用户以为文章就那么长（服务层在正文尾部也留了那句话）。
    """

    url: str = Field(description="请求的那个地址")
    final_url: str = Field(description="真正读到的那一页（跳转之后）；当前等于 url，见模块头")
    title: str = Field(description="页面标题（取不到时是地址本身）")
    text: str = Field(description="正文（Markdown；超长已截断）")
    truncated: bool = Field(description="正文是不是被截断了")
    fetched_at: datetime = Field(description="取回的时刻（UTC）")


class EmbedCheckOut(BaseModel):
    """``GET /web/embed-check``：这一页能不能被浏览器嵌进来。

    ``embeddable=false`` 时**不抛错**：那是"阅读模式该走另一条路"的正常答案
    （``reason`` 是对方的原话 + 下一步），不是这次请求失败。
    ``x_frame_options`` / ``frame_ancestors`` 都是**对方原话**，没有就是空串——
    界面要显示"对方为什么不让嵌"时直接用它，不必再解析一遍。
    """

    url: str = Field(description="请求的那个地址")
    embeddable: bool = Field(description="浏览器会不会拒载这一页")
    reason: str = Field(description="为什么（对方的原话 + 下一步）")
    x_frame_options: str = Field(default="", description="对方设的那条头（没有就是空串）")
    frame_ancestors: str = Field(
        default="",
        description="CSP 里 frame-ancestors 那几句的值（没有就是空串；两条策略用「；」连）",
    )


@router.get(
    "/page",
    response_model=WebPageOut,
    summary="取一个网页的正文（本机代取；内网 / 本机地址一律拒）",
)
def web_page(
    caller: Annotated[Caller, Depends(require_read)],
    url: str = Query(description="要读的绝对地址（http/https，公网）"),
) -> WebPageOut:
    """抓一页的正文（Markdown）。

    **闸在发请求之前**（先 ``check_public_url`` 再 ``fetch_url``）：内网 / 本机 /
    云元数据地址回 **400 + 那句原话**，一个请求都不发出去。

    上游那点事**原样报**：对方 4xx/5xx、超时、不是网页正文（图片 / 压缩包）都是
    ``fetch_url`` 抛的 ``UpstreamError`` → **502**（"外部依赖出错"，不是"我们出错了"，
    也不是"你请求写错了"）——用户看到文案就知道该重试还是该换个地址。

    **不落库、不进知识库**：这里只是"读一眼"，入库是另一个动作（上传 / 数据源）。
    """
    target = _public_url(url)
    try:
        title, text = fetch_url(target)
    except InvalidRequestError as exc:
        # 这一处盖的是**重定向**：每一跳都重新校验地址（`services/web.py::_get_with_checks`），
        # 公网地址 302 到 127.0.0.1 那种就从这儿出来。理由与 `_public_url` 逐字相同：
        # 没有字段可改，用户要做的动作是"换一个地址"，那就是 400。
        raise BadRequestError(str(exc)) from exc
    return WebPageOut(
        url=target,
        # 服务层没把最终地址传出来（见模块头"final_url 的实情"）：先如实回请求的那个
        final_url=target,
        title=title,
        text=text,
        # 服务层的截断口径就是"正文超过 limit 就截"（`fetch_url` 结尾那两行），而这里
        # 用的正是它默认的那个 limit ⇒ 长度超过 MAX_FETCH_CHARS 就等于被截过。
        truncated=len(text) > MAX_FETCH_CHARS,
        fetched_at=datetime.now(UTC),
    )


@router.get(
    "/embed-check",
    response_model=EmbedCheckOut,
    summary="这一页能不能嵌进 iframe（探一次响应头；连不上按不能嵌回，不报错）",
)
def web_embed_check(
    request: Request,
    caller: Annotated[Caller, Depends(require_read)],
    url: str = Query(description="要嵌的绝对地址（http/https，公网）"),
) -> EmbedCheckOut:
    """问一句"对方让不让嵌"：读一次响应头，回 ``embeddable`` + 那句话。

    **判据与状态码**：SSRF 闸同样是 ``check_public_url``（内网 / 本机 → 400）；
    探得到响应就按 ``X-Frame-Options`` 与 CSP 的 ``frame-ancestors`` 判（判定只有
    ``_judge_embeddable`` 一处）；**探不到就 200 + false**（连不上 / 超时 / 对方跳走了），
    reason 里写清是哪一档——探测失败不该把阅读模式挡住。

    ``embeddable=true`` 只表示"那两条头没拦我们"：对方还可能用 JS 自检、
    或者干脆是个登录页。这条端点的用途是**少走一次白等**，不是保证。
    """
    target = _public_url(url)
    x_frame_options, ancestors, failure = _probe_framing_headers(target)
    if failure:
        return EmbedCheckOut(url=target, embeddable=False, reason=failure)
    embeddable, reason = _judge_embeddable(x_frame_options, ancestors, _embedder_origin(request))
    return EmbedCheckOut(
        url=target,
        embeddable=embeddable,
        reason=reason,
        x_frame_options=x_frame_options,
        # 句子原话照给；两条策略时用「；」连起来（少见，但连起来才说得清是"都写"）
        frame_ancestors="；".join(ancestors),
    )


def _public_url(url: str) -> str:
    """过一遍 SSRF 闸；被拒时折成 **400**，并把 ``check_public_url`` 的原话带出去。

    为什么不是它默认的 422：``InvalidRequestError`` 在共享的状态码表里是 422（语义是
    "把字段改对再来"），而"这个地址指向本机 / 内网"**没有字段可改**——它是请求内容与
    事实对不上，也就是 400。判据与文案一个字不改，只换状态码（与 ``api/v1/local.py``
    那条"没有设备身份"端点同一手法）。
    """
    try:
        return check_public_url(url)
    except InvalidRequestError as exc:
        raise BadRequestError(str(exc)) from exc


def _probe_framing_headers(target: str) -> tuple[str, list[str], str]:
    """发一次 GET，回 ``(X-Frame-Options, frame-ancestors 那几句, 失败原因)``。

    **拿到响应头就断流**：``with`` 退出即 ``close``，响应体一个字节都不读——我们要的
    只有那两条头，而把一张首页读完是 ``/web/page`` 那一边的事（也别用 HEAD：它兼容性差，
    一堆站点回 405 或直接掐断）。

    成功时第三项是空串；失败时前两项是空、第三项是那句原因，调用方据此按"不能嵌"
    回 200（**不抛**：探测失败只是"这一页改用正文读"）。
    """
    try:
        with shared_client().stream(
            "GET", target, headers={"User-Agent": _USER_AGENT}, timeout=_PROBE_TIMEOUT_SECONDS
        ) as response:
            if response.is_redirect:
                # 浏览器会自己跟过去，而那一页的头我们没看到 ⇒ 不能拿这一跳的头下结论
                # （跳转后的页面往往更严）。追着跳就变成一次爬取，这一条不值当。
                return (
                    "",
                    [],
                    (
                        f"对方的地址跳到了别处（HTTP {response.status_code}），"
                        "最终那一页没探到，按不能嵌处理"
                    ),
                )
            return (
                (response.headers.get("x-frame-options") or "").strip(),
                _frame_ancestors(response.headers),
                "",
            )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        detail = " ".join(str(exc).split())[:_REASON_DETAIL_CHARS]
        return "", [], f"没连上对方，按不能嵌处理（{detail}）"


def _frame_ancestors(headers: httpx.Headers) -> list[str]:
    """CSP 里每一句 ``frame-ancestors`` 的**原话**（没有这条指令就是空列表）。

    **可能不止一句，而这不是"随便取一句"就能算的**：

    - **一条 CSP 头里写两次**：按 CSP 规范**第一次**生效（重复的指令被忽略），
      所以一条头内部只认第一次出现的那句；
    - **发了两条 CSP 头**（合法写法，CDN 用自己的那一份加一条时常见）：两份策略
      **都生效**，能不能嵌要**两份都同意**。所以这里**每条头各回一句**，
      判定那边按"有一句不放行就是不放行"算（见 ``_judge_embeddable``）——
      只挑其中一句会得出相反的答案，而那个答案的代价是"界面说能嵌、点下去白屏"。
    """
    policies = headers.get_list("content-security-policy")
    values: list[str] = []
    for policy in policies:
        for directive in policy.split(";"):
            parts = directive.split(maxsplit=1)
            if parts and parts[0].lower() == "frame-ancestors":
                values.append(parts[1].strip() if len(parts) > 1 else "")
                break  # 同一条头里重复的指令被浏览器忽略（第一次生效）
    return values


def _judge_embeddable(
    x_frame_options: str, frame_ancestors: list[str], origin: str
) -> tuple[bool, str]:
    """能不能嵌的**唯一**判定（前端不再自己判一遍，见模块头）。

    三条口径，按浏览器实际执行的顺序：

    1. ``X-Frame-Options`` 里出现 ``DENY`` / ``SAMEORIGIN`` → false。它是最老、也最硬
       的那条（现代浏览器仍是先看它）。``ALLOW-FROM`` 那一档**不算拦**——浏览器早就
       不认这条头了，照实说，免得用户以为那份白名单还生效；
    2. CSP 的 ``frame-ancestors``：**每一句都要放行**才算能嵌（两条策略是"都生效"，
       不是"取一条"）；一句放行的判据是含 ``*``、或含**我们这个 origin**，
       其余（``'none'`` / ``'self'`` / 别人的地址）→ 不放行；
    3. 两条头都没有 → true（"没有"就是没拦）。

    ``reason`` 一律是**对方的原话** + 下一步：用户看到的是"对方不让嵌"，
    而不是我们的一句结论。
    """
    tokens = set(_split_tokens(x_frame_options))
    if tokens & {"deny", "sameorigin"}:
        return False, (
            f"对方设置了 X-Frame-Options: {x_frame_options}，浏览器会拒载；这一页改用正文读"
        )

    if frame_ancestors:
        refusing = [value for value in frame_ancestors if not _ancestors_allow(value, origin)]
        if refusing:
            return False, (
                f"对方的 Content-Security-Policy 写着 frame-ancestors {refusing[0]}，"
                "我们不在允许嵌的名单里，浏览器会拒载；这一页改用正文读"
            )
        return True, f"对方的 frame-ancestors 写着 {'；'.join(frame_ancestors)}，允许我们嵌"

    if x_frame_options:
        return True, (
            f"对方设了 X-Frame-Options: {x_frame_options}，"
            "这条只有 DENY / SAMEORIGIN 会拦住浏览器，可以嵌"
        )
    return True, "对方没设 X-Frame-Options，也没设 frame-ancestors，可以嵌"


def _split_tokens(value: str) -> list[str]:
    """把一条头拆成小写词（逗号、分号、空白都算分隔符）。

    有些站点写的是 ``SAMEORIGIN, SAMEORIGIN`` 或一条头里塞两个取值，
    按整串比对会漏，按词比对才不会。
    """
    return [token.strip().lower() for token in value.replace(",", " ").replace(";", " ").split()]


def _ancestors_allow(value: str, origin: str) -> bool:
    """这一句 ``frame-ancestors`` 放我们进来吗（含 ``*``，或名单里就有我们这个源）。

    ``'none'`` / ``'self'`` 这些关键字也走同一条比较：去引号之后它们谁都不等于外面的
    某个 origin，于是自然落进"不放行"那一支 ✓（我们不是那一页自己，也不是它的"同源"）。
    """
    allowed = {_normalize_source(source) for source in value.split()}
    return "*" in allowed or _normalize_source(origin) in allowed


def _normalize_source(source: str) -> str:
    """CSP 里的一个来源写成比较形式：去引号、去尾斜杠、小写。

    ``'none'`` / ``'self'`` 这些关键字走同一条（去引号后就是 ``none`` / ``self``）——
    它们与某个 origin 的比较规则在 ``_ancestors_allow`` 那一处，这里只管写法。
    """
    return source.strip().strip("'\"").rstrip("/").lower()


def _embedder_origin(request: Request) -> str:
    """谁在问"能不能嵌"——那个准备把这一页装进 iframe 的页面的源。

    首选 ``Origin`` 头：界面与后端不同源时（壳里那条链、网页端那条链）浏览器都会带上
    它 ✓。取不到（同源请求不带这个头）时退回**这条请求自己的源**——那正是"界面由这个
    后端发出去"那种形态下的答案。
    """
    origin = (request.headers.get("origin") or "").strip()
    return origin.rstrip("/") if origin else str(request.base_url).rstrip("/")
