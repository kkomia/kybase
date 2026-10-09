"""站点图标代理：任意域名 / 公网校验 / 位图嗅探 / 首页发现 / 磁盘缓存（D11-②）。

镜像同构：``app/services/site_icons.py`` → ``tests/unit/services/test_site_icons.py``。

要紧的五组：

1. **不许当跳板**：任何形态合法的域名都代为抓取（白名单已撤，表外域名不再 422），
   但每一跳（含重定向）都过一遍 ``web.check_public_url``（本仓唯一那处"是不是公网地址"
   的判断）——白名单撤了之后，这一条就是"这个域名该不该抓"的唯一判断。
   **首页里发现的地址也一样**（那是第三方页面说了算的字符串）；
2. **只收位图**：SVG 明确拒收（它和我们同源，被打开就是同源脚本执行面）；
3. **固定路径落空就读首页 HTML**：SPA 站点让 `/favicon.ico` 回 200 + `text/html` 或干脆 404，
   真图标写在 `<link rel="icon">` 里（相对地址按**最终页面地址**解析）；
4. **缓存**：抓一次、之后从磁盘发；失败记一段时间；过期重抓、抓不到继续用旧的；
5. **超限/超时不算图 / 不算页**：宁可没有图标（前端退字牌 / 地球），也不缓存半截 PNG。

一个外部请求都不发：抓取那一层全被替换成假的。
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.core.exceptions import InvalidRequestError
from app.services import site_icons
from app.services.site_icons import (
    CACHE_MAX_FILES,
    MAX_ICON_BYTES,
    SiteIcon,
    SiteIconService,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

DOMAIN = "github.com"


@pytest.fixture
def service(tmp_path) -> SiteIconService:  # type: ignore[no-untyped-def]
    return SiteIconService(tmp_path)


@pytest.fixture
def fetched(monkeypatch) -> list[str]:  # type: ignore[no-untyped-def]
    """把"真的去抓"换成假的，返回被请求过的域名（用来断言"没抓"）。"""
    calls: list[str] = []

    def fake_fetch(self, host: str) -> SiteIcon | None:  # type: ignore[no-untyped-def]
        calls.append(host)
        return SiteIcon(content=PNG, media_type="image/png", kind="png")

    monkeypatch.setattr(SiteIconService, "_fetch", fake_fetch)
    return calls


# ------------------------------------------------------------------ 域名校验


def test_normalize_domain_lowercases_and_trims() -> None:
    assert site_icons.normalize_domain(" GitHub.com. ") == "github.com"


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com",
        "github.com/favicon.ico",
        "github.com:443",
        "git hub.com",
        "github..com",
        "",
    ],
)
def test_normalize_domain_refuses_anything_that_is_not_a_host(raw: str) -> None:
    """入参要拼进 URL，宽松解析就是把外部字符串塞进请求里。"""
    with pytest.raises(InvalidRequestError):
        site_icons.normalize_domain(raw)


def test_outsider_domain_is_fetched_too(service: SiteIconService, fetched) -> None:  # type: ignore[no-untyped-def]
    """表外的域名照样代为抓取，缓存也按这个域名存。

    改前这里是"不在已知站点表 → 422，一次请求都不发"（白名单防跳板）。
    后果是真实结果里绝大多数站点只剩一枚字母圆——2026-10-01 用户：
    "这个为啥抓不到真实的图标呢，你放个字母标在这儿没意义啊"。
    白名单撤了之后，"该不该抓"由 `check_public_url` 逐跳兜住（下面几条钉着）。
    """
    assert service.icon("opendatalab.github.io") is not None

    assert fetched == ["opendatalab.github.io"]
    assert (service._root / "opendatalab.github.io.png").is_file()


def test_cache_is_keyed_by_the_domain_itself(service: SiteIconService, fetched) -> None:  # type: ignore[no-untyped-def]
    """`www.` 也好、子域也好，缓存键就是**入参域名本身**，各抓一次、各存一份。

    改前子域要归到白名单里的根域（`en.wikipedia.org` 与 `zh.wikipedia.org` 共用一份），
    那得先有一张"谁是根域"的表——随白名单一起撤了。归并本来也不准：
    同一个站点的两个主机未必共用一枚图标。
    """
    assert service.icon("en.wikipedia.org") is not None
    assert service.icon("zh.wikipedia.org") is not None
    assert service.icon("en.wikipedia.org") is not None, "第二次该吃缓存"

    assert fetched == ["en.wikipedia.org", "zh.wikipedia.org"]
    assert (service._root / "en.wikipedia.org.png").is_file()
    assert (service._root / "zh.wikipedia.org.png").is_file()


def test_malformed_domain_is_still_refused(service: SiteIconService, fetched) -> None:  # type: ignore[no-untyped-def]
    """撤的是白名单，不是入参校验：形态不合法的仍旧 422，而且一次请求都不发。"""
    with pytest.raises(InvalidRequestError):
        service.icon("https://github.com")

    assert fetched == []


# ------------------------------------------------------------------ 位图嗅探


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (PNG, "png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "jpeg"),
        (b"GIF89a" + b"\x00" * 8, "gif"),
        (b"\x00\x00\x01\x00" + b"\x00" * 8, "ico"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4, "webp"),
    ],
)
def test_sniff_recognises_bitmaps(raw: bytes, kind: str) -> None:
    assert site_icons._sniff(raw) == kind


@pytest.mark.parametrize("raw", [SVG, b"<html></html>", b"", b"not an image"])
def test_sniff_refuses_everything_else(raw: bytes) -> None:
    """**SVG 明确拒收**：它和我们同源，被直接打开就是一个同源脚本执行面。"""
    assert site_icons._sniff(raw) is None


def test_svg_from_the_site_is_thrown_away(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(SiteIconService, "_get_bytes", lambda self, url: SVG)
    assert service._fetch("github.com") is None


# ------------------------------------------------------------------ 公网校验


def test_internal_address_is_never_fetched(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """内网地址在**发请求之前**就被拒；连接那一层根本不会被调用。"""
    called: list[str] = []
    monkeypatch.setattr(SiteIconService, "_get_bytes", lambda self, url: called.append(url) or PNG)

    def refuse(url: str) -> str:
        raise InvalidRequestError("这个地址指向本机或内网")

    monkeypatch.setattr(site_icons, "check_public_url", refuse)

    assert service._fetch_one("https://github.com/favicon.ico") is None
    assert called == []


def test_host_that_never_passes_the_public_check_has_no_icon(  # type: ignore[no-untyped-def]
    service: SiteIconService, monkeypatch
) -> None:
    """过不了公网校验的域名回 ``None``（**不是 422、也不是异常**）。

    白名单撤了之后，"这个域名不该抓"这唯一的判断就落在这里：表外的域名现在也代为抓取，
    所以它必须是一条**正常的降级路径**（前端退字牌 / 地球），不能把错误甩给调用方。
    """
    called: list[str] = []
    monkeypatch.setattr(SiteIconService, "_get_bytes", lambda self, url: called.append(url) or PNG)

    def refuse(url: str) -> str:
        raise InvalidRequestError("这个地址指向本机或内网")

    monkeypatch.setattr(site_icons, "check_public_url", refuse)

    assert service.icon("evil.example.com") is None
    assert called == []
    assert (service._root / "evil.example.com.miss").is_file(), "记一次缺失，别每次渲染都去撞"


def test_redirect_to_an_internal_address_is_dropped(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """公网地址 302 到内网是最常见的绕过手法：**每一跳都重新校验**。

    这种候选按"没拿到"处理（不是调用方写错了），不该把 422 甩给前端。
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "http://127.0.0.1:8000/favicon.ico"}
        )
    )
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._fetch_one("https://github.com/favicon.ico") is None


def test_non_image_content_type_is_not_downloaded(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>"
        )
    )
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._get_bytes("https://github.com/favicon.ico") is None


def test_oversized_icon_is_dropped_not_truncated(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """半截 PNG 在界面上就是"图片坏了"，还会被缓存下来——宁可没有。"""
    big = PNG + b"\x00" * (MAX_ICON_BYTES + 10)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=big)
    )
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._get_bytes("https://github.com/favicon.ico") is None


def test_bitmap_is_fetched_through_the_real_path(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """正向：正常的 200 + image/png 一路走到 `SiteIcon`（这条走的是真 `_get_bytes`）。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
    )
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    icon = service._fetch_one("https://github.com/favicon.ico")

    assert icon is not None
    assert (icon.kind, icon.media_type) == ("png", "image/png")
    assert icon.content == PNG


# ------------------------------------------------------- 首页 HTML 里声明的图标
#
# 2026-10-01 真机复验：固定两条路径对 SPA 站点是瞎的（`docs.mthreads.com` 的
# `/favicon.ico` 回 200 + `text/html` 兜底页、`mineru.atomgit.com` 的干脆 404），
# 而两站首页里都写了真图标。以下是那一步的契约。

SPA_HTML = (
    "<!doctype html><html><head>"
    '<link rel="stylesheet" href="/app.css">'
    '<link rel="icon" href="/img/favico.ico">'
    "</head><body></body></html>"
)


def _stub_icons(monkeypatch, icons: dict[str, bytes]) -> list[str]:  # type: ignore[no-untyped-def]
    """把"抓图标"换成假的：按地址给字节，没给的一律当 404（同 `_get_bytes` 那几处的桩法）。"""
    asked: list[str] = []

    def fake(self, url: str) -> bytes | None:  # type: ignore[no-untyped-def]
        asked.append(url)
        return icons.get(url)

    monkeypatch.setattr(SiteIconService, "_get_bytes", fake)
    return asked


def _stub_page(monkeypatch, html: str, final_url: str) -> list[str]:  # type: ignore[no-untyped-def]
    """把"读首页"换成假的：返回被请求过的页面地址（同 `fetched` 夹具的桩法）。"""
    asked: list[str] = []

    def fake(self, url: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
        asked.append(url)
        return html, final_url

    monkeypatch.setattr(SiteIconService, "_get_text", fake)
    return asked


def test_icon_declared_in_the_home_page_is_used(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """固定路径全落空 → 读首页 HTML，按它声明的 `<link rel="icon">` 去抓。

    `docs.mthreads.com` 就是这个形态：`/favicon.ico` 回的是 **200 但 `text/html`** 的
    SPA 兜底页（按"只收位图"被拒），真图标写在首页里。
    """
    asked = _stub_icons(monkeypatch, {"https://docs.mthreads.com/img/favico.ico": PNG})
    pages = _stub_page(monkeypatch, SPA_HTML, "https://docs.mthreads.com/")

    icon = service._fetch("docs.mthreads.com")

    assert icon is not None
    assert (icon.kind, icon.content) == ("png", PNG)
    assert pages == ["https://docs.mthreads.com/"]
    # 顺序：先四条固定路径（host + www.host × 两条），再轮到首页里那一条
    assert asked == [
        "https://docs.mthreads.com/apple-touch-icon.png",
        "https://www.docs.mthreads.com/apple-touch-icon.png",
        "https://docs.mthreads.com/favicon.ico",
        "https://www.docs.mthreads.com/favicon.ico",
        "https://docs.mthreads.com/img/favico.ico",
    ]


def test_relative_icon_href_is_resolved_against_the_final_page_url(  # type: ignore[no-untyped-def]
    service: SiteIconService, monkeypatch
) -> None:
    """相对地址按**最终页面地址**解析（`mineru.atomgit.com` 写的就是那个相对路径）。

    那个站点的首页写的是 `<link rel="icon" href="./assets/images/favicon.png">`；
    最终地址未必是我们请求的那个（站点常把 `/` 跳到某个语言前缀下），相对地址得跟着那一页走。
    """
    html = '<link rel="icon" href="./assets/images/favicon.png">'
    asked = _stub_icons(
        monkeypatch, {"https://mineru.atomgit.com/zh/assets/images/favicon.png": PNG}
    )
    _stub_page(monkeypatch, html, "https://mineru.atomgit.com/zh/")

    assert service._fetch("mineru.atomgit.com") is not None
    assert asked[-1] == "https://mineru.atomgit.com/zh/assets/images/favicon.png"


def test_home_page_without_an_icon_link_is_a_miss(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """首页读得到、里面没有 link 声明 → **如实 miss**（`opendatalab.github.io` 就是这一档）。

    读到了就不再去 `www.` 那一份：同一个站点、同一条 path，没有图标就是没有。
    """
    pages = _stub_page(
        monkeypatch,
        "<html><head><title>OpenDataLab</title></head></html>",
        "https://opendatalab.github.io/",
    )
    asked = _stub_icons(monkeypatch, {})

    assert service._fetch("opendatalab.github.io") is None
    assert pages == ["https://opendatalab.github.io/"]
    assert len(asked) == 4, "只有那四条固定路径"


def test_svg_icon_is_rejected_and_the_next_candidate_wins(  # type: ignore[no-untyped-def]
    service: SiteIconService, monkeypatch
) -> None:
    """link 指向 svg：**嗅探本来就拒收**（同源脚本执行面），继续试下一个候选。"""
    html = (
        '<link rel="icon" type="image/svg+xml" href="/logo.svg">'
        '<link rel="apple-touch-icon" href="/touch.png">'
    )
    asked = _stub_icons(
        monkeypatch, {"https://example.com/logo.svg": SVG, "https://example.com/touch.png": PNG}
    )
    _stub_page(monkeypatch, html, "https://example.com/")

    icon = service._fetch("example.com")

    assert icon is not None and icon.kind == "png"
    assert asked[-2:] == ["https://example.com/logo.svg", "https://example.com/touch.png"]


def test_data_uri_icon_is_skipped(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`data:` 是内联图片、不是可抓的地址：跳过它（那条 base64 不该当 URL 发出去）。"""
    html = '<link rel="icon" href="data:image/png;base64,AAAA"><link rel="icon" href="/real.png">'
    asked = _stub_icons(monkeypatch, {"https://example.com/real.png": PNG})
    _stub_page(monkeypatch, html, "https://example.com/")

    assert service._fetch("example.com") is not None
    assert all(not url.startswith("data:") for url in asked)
    assert asked[-1] == "https://example.com/real.png"


def test_icon_href_pointing_inside_is_skipped(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """首页里写的地址**也照样过公网校验**：指向内网就跳过（SSRF 不松），继续下一个。"""
    html = (
        '<link rel="icon" href="http://127.0.0.1:8000/favicon.ico">'
        '<link rel="icon" href="/real.png">'
    )
    asked = _stub_icons(monkeypatch, {"https://example.com/real.png": PNG})
    _stub_page(monkeypatch, html, "https://example.com/")

    icon = service._fetch("example.com")

    assert icon is not None and icon.kind == "png"
    assert "http://127.0.0.1:8000/favicon.ico" not in asked
    assert asked[-1] == "https://example.com/real.png"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<link rel="icon" href="/a.ico">', ["/a.ico"]),
        ('<link rel="shortcut icon" href="/a.ico">', ["/a.ico"]),
        ('<link rel="apple-touch-icon" href="/touch.png">', ["/touch.png"]),
        ('<LINK REL="ICON" HREF="/A.ICO">', ["/A.ICO"]),  # 大小写不敏感
        ("<link rel='icon' href=/a.ico>", ["/a.ico"]),  # 单引号 / 裸值
        ('<link href="/a.ico" rel="icon">', ["/a.ico"]),  # 属性顺序反过来
        ('<link rel="stylesheet" href="/app.css">', []),  # 不是图标
        ('<link rel="icon" href="data:image/png;base64,AAAA">', []),  # 内联图片
        ('<link rel="icon" href="">', []),  # 空地址
        (
            '<link rel="icon" href="/a.ico"><link rel="icon" href="/a.ico">',
            ["/a.ico"],
        ),  # 去重
        (
            '<link rel="icon" href="/1.png"><link rel="icon" href="/2.png">'
            '<link rel="icon" href="/3.png"><link rel="icon" href="/4.png">'
            '<link rel="icon" href="/5.png">',
            ["/1.png", "/2.png", "/3.png", "/4.png"],
        ),  # 最多 4 个
    ],
)
def test_icon_hrefs_parses_the_link_tags(html: str, expected: list[str]) -> None:
    assert site_icons._icon_hrefs(html) == expected


def test_home_page_is_read_as_text_with_the_final_url(  # type: ignore[no-untyped-def]
    service: SiteIconService, monkeypatch
) -> None:
    """`_get_text` 回的是**正文 + 跟完重定向之后的地址**（相对图标地址就靠它解析）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/zh/"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=SPA_HTML.encode())

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._get_text("https://docs.mthreads.com/") == (
        SPA_HTML,
        "https://docs.mthreads.com/zh/",
    )


def test_home_page_without_a_declared_content_type_is_still_read(  # type: ignore[no-untyped-def]
    service: SiteIconService, monkeypatch
) -> None:
    """少数站点的首页就是不带 `content-type`：不能因此当它"不是 HTML"。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html></html>"))
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._get_text("https://example.com/") == ("<html></html>", "https://example.com/")


def test_non_html_page_is_not_read(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """首页这一档只收 HTML：拿到 JSON / 图片时不硬解码（那只会抽出一堆看不出错的候选）。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{}"
        )
    )
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._get_text("https://example.com/") is None


def test_oversized_home_page_is_dropped(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """半截 HTML 可能正好断在 `<link …>` 中间、抽出一个坏地址——宁可当"这页没读成"。"""
    big = b"<html>" + b"x" * (site_icons.MAX_PAGE_BYTES + 10)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=big)
    )
    monkeypatch.setattr(site_icons, "shared_client", lambda: httpx.Client(transport=transport))

    assert service._get_text("https://example.com/") is None


# ------------------------------------------------------------------ 磁盘缓存


def test_icon_is_fetched_once_then_served_from_disk(service: SiteIconService, fetched) -> None:  # type: ignore[no-untyped-def]
    first = service.icon(DOMAIN)
    second = service.icon(DOMAIN)

    assert first is not None and second is not None
    assert (first.content, first.media_type) == (PNG, "image/png")
    assert second.content == PNG
    assert fetched == [DOMAIN], "第二次应当直接吃磁盘缓存，不再抓"
    assert (service._root / f"{DOMAIN}.png").is_file()


def test_missing_icon_is_remembered(service: SiteIconService, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """抓不到记一段时间：否则每一次渲染都会去撞一个挂掉的站点。"""
    calls: list[str] = []

    def fake_fetch(self, host: str):  # type: ignore[no-untyped-def]
        calls.append(host)
        return None

    monkeypatch.setattr(SiteIconService, "_fetch", fake_fetch)

    assert service.icon(DOMAIN) is None
    assert service.icon(DOMAIN) is None
    assert calls == [DOMAIN]
    assert (service._root / f"{DOMAIN}.miss").is_file()


def test_expired_icon_is_refetched(service: SiteIconService, fetched, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert service.icon(DOMAIN) is not None
    target = service._root / f"{DOMAIN}.png"
    stale = time.time() - site_icons.CACHE_TTL_SECONDS - 60
    import os

    os.utime(target, (stale, stale))

    def jpeg(self, host: str) -> SiteIcon:  # type: ignore[no-untyped-def]
        return SiteIcon(content=b"\xff\xd8\xff\xe0newer", media_type="image/jpeg", kind="jpeg")

    monkeypatch.setattr(SiteIconService, "_fetch", jpeg)

    refreshed = service.icon(DOMAIN)

    assert refreshed is not None
    assert refreshed.kind == "jpeg"
    assert (service._root / f"{DOMAIN}.png").exists() is False, "换种类时旧文件要删掉"


def test_stale_icon_survives_a_failed_refetch(  # type: ignore[no-untyped-def]
    service: SiteIconService, fetched, monkeypatch
) -> None:
    """过期了也先留着旧的：抓失败时继续用手上那一份，比"突然没有 logo"好。"""
    assert service.icon(DOMAIN) is not None
    target = service._root / f"{DOMAIN}.png"
    stale = time.time() - site_icons.CACHE_TTL_SECONDS - 60
    import os

    os.utime(target, (stale, stale))
    monkeypatch.setattr(SiteIconService, "_fetch", lambda self, host: None)

    kept = service.icon(DOMAIN)

    assert kept is not None
    assert kept.content == PNG


def test_cache_is_capped_and_prunes_the_oldest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """这是缓存不是档案：超了就删最旧的，删掉最多再抓一次。"""
    service = SiteIconService(tmp_path)
    service._root.mkdir(parents=True, exist_ok=True)
    import os

    for index in range(CACHE_MAX_FILES + 5):
        path = service._root / f"site{index}.com.png"
        path.write_bytes(PNG)
        stamp = time.time() - (CACHE_MAX_FILES + 5 - index)
        os.utime(path, (stamp, stamp))

    service._prune()

    left = sorted(item.name for item in service._root.iterdir())
    assert len(left) == CACHE_MAX_FILES
    assert "site0.com.png" not in left, "最旧的那几个该被删"
    assert f"site{CACHE_MAX_FILES + 4}.com.png" in left
