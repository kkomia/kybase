"""``/web`` 那一族的 HTTP 行为（本机档「阅读模式」）。

镜像同构：``app/api/v1/web.py`` → ``tests/unit/api/test_web.py``。

要紧的几条：**内网 / 本机地址 400**（那句话就是 ``services/web.py::check_public_url``
的原话，而且**一个请求都不该发出去**——闸在发请求之前）、**正文正常返回**（标题 / 正文 /
截断标记 / 带时区的时间）、**上游失败 502**、**不做缓存**（同一页第二次仍然真去取）、
``embed-check`` 的判定各档（``DENY`` / ``SAMEORIGIN`` / ``frame-ancestors 'none'`` /
空名单里的我们 / 两条头都没有）、**只拿响应头、不读 body**、
**探测失败 200 + embeddable=false**（不抛 502），以及**这一族只挂本机档**
（服务器档 OpenAPI 里没有它——摆在那儿就是一条没人调、却能被外部打进来的抓取口）。

一个真网络请求都不发：抓取与探测走 ``respx`` 造的响应（同
``tests/unit/services/test_web.py``），"不读 body"那一条用一段**读了就炸**的流来钉。
用例标 ``local``：它们只碰本机 SQLite 与文件系统，不需要 PostgreSQL（见 conftest.py）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.http import shared_client

pytestmark = pytest.mark.local

PAGE = "https://news.example.com/a"
PAGE_ENDPOINT = "/api/v1/web/page"
EMBED_ENDPOINT = "/api/v1/web/embed-check"


def _html(title: str, body: str) -> str:
    """一页带导航与页脚的 HTML（正文抽取该只留下 ``<article>`` 里那一段）。"""
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<nav><a href='/'>首页</a><a href='/a'>关于</a></nav>"
        f"<article><h1>{title}</h1><p>{body}</p></article>"
        f"<footer>版权所有</footer></body></html>"
    )


def _serve(
    headers: dict[str, str] | None = None, *, title: str = "今日要闻", body: str = "第一段正文。"
):
    """在这一页上挂一个假响应（默认是正常的 HTML 页）。"""
    return respx.get(PAGE).mock(
        return_value=httpx.Response(
            200,
            html=_html(title, body),
            headers={"content-type": "text/html", **(headers or {})},
        )
    )


def _get(client: TestClient, endpoint: str, url: str, **kwargs: Any) -> httpx.Response:
    return client.get(endpoint, params={"url": url}, **kwargs)


# ------------------------------------------------------------------ GET /web/page


@respx.mock
def test_a_page_comes_back_as_markdown(local_client: TestClient) -> None:
    """正常那一档：标题、正文（Markdown）、截断标记、时间都在，形状就是契约里那六个键。"""
    _serve()

    response = _get(local_client, PAGE_ENDPOINT, PAGE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"url", "final_url", "title", "text", "truncated", "fetched_at"}
    assert body["url"] == PAGE
    # 服务层没把最终地址传出来（见 `api/v1/web.py` 模块头那一节）⇒ 先等于请求的那个
    assert body["final_url"] == PAGE
    assert body["title"] == "今日要闻"
    assert "第一段正文" in body["text"]
    assert "版权所有" not in body["text"], "页脚不该进正文"
    assert body["truncated"] is False
    # 《API 接口规范》§1.2：时间一律 ISO 8601 带时区
    assert datetime.fromisoformat(body["fetched_at"]).tzinfo is not None


@respx.mock
def test_a_long_page_is_flagged_as_truncated(local_client: TestClient) -> None:
    """截断要说出来：悄悄截断会让用户以为文章就那么长（服务层也在正文尾部留了那句话）。"""
    _serve(body="字" * 40_000)

    body = _get(local_client, PAGE_ENDPOINT, PAGE).json()

    assert body["truncated"] is True
    assert "已截断" in body["text"]


@respx.mock
def test_the_page_is_fetched_again_on_the_next_call(local_client: TestClient) -> None:
    """**不做缓存**（产品层已拍板）：第二次要的是"这一页现在长什么样"，不是上一份副本。"""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(
            200,
            html=_html("今日要闻", f"第 {len(seen)} 次取到的正文。"),
            headers={"content-type": "text/html"},
        )

    respx.get(PAGE).mock(side_effect=handler)

    first = _get(local_client, PAGE_ENDPOINT, PAGE).json()
    second = _get(local_client, PAGE_ENDPOINT, PAGE).json()

    assert len(seen) == 2, "同一个地址第二次也要真去取"
    assert "第 1 次" in first["text"] and "第 2 次" in second["text"]


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/health",  # 本机（KYLAB 自己）
        "http://192.168.31.18:54321/",  # 局域网里的 PG
        "http://169.254.169.254/latest/meta-data/",  # 云厂商元数据（凭据）
        "file:///etc/passwd",  # 不是 http/https
    ],
)
def test_a_refused_address_is_400_with_the_original_sentence(
    local_client: TestClient, url: str
) -> None:
    """闸在发请求之前，拒绝时回 **400 + ``check_public_url`` 的原话**。

    为什么是 400 而不是 422：422 的语义是"把字段改对再来"，而"这个地址指向本机 / 内网"
    没有字段可改——用户要做的动作是**换一个地址**（与 `web.py::_public_url` 那条注释同一口径）。
    """
    response = _get(local_client, PAGE_ENDPOINT, url)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "invalid_request"
    refused = ("本机", "内网", "只支持 http/https")
    assert any(word in body["message"] for word in refused), body["message"]
    assert respx.calls.call_count == 0, "闸在发请求之前：一个请求都不该发出去"


@respx.mock
def test_the_same_gate_guards_embed_check(local_client: TestClient) -> None:
    """``embed-check`` 走的是**同一道**闸：内网地址同样 400、同样一个请求都不发。"""
    response = _get(local_client, EMBED_ENDPOINT, "http://127.0.0.1:8000/api/v1/health")

    assert response.status_code == 400, response.text
    assert "本机" in response.json()["message"] or "内网" in response.json()["message"]
    assert respx.calls.call_count == 0


@respx.mock
def test_a_failing_upstream_is_502(local_client: TestClient) -> None:
    """上游那点事**原样报**：对方 5xx → 502 ``upstream_error``（不是 500，也不是 4xx）。

    用户看到文案就知道该重试、还是该换个地址——混进 500 里就只剩一句"服务内部错误"。
    """
    respx.get(PAGE).mock(return_value=httpx.Response(503, text="网关崩了"))

    response = _get(local_client, PAGE_ENDPOINT, PAGE)

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["code"] == "upstream_error"
    assert "503" in body["message"]


@respx.mock
def test_a_non_textual_payload_is_502_too(local_client: TestClient) -> None:
    """图片 / 压缩包不是"网页正文"：解码出来的东西毫无意义（服务层的原话照转）。"""
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200, content=b"PK\x03\x04", headers={"content-type": "application/zip"}
        )
    )

    response = _get(local_client, PAGE_ENDPOINT, PAGE)

    assert response.status_code == 502, response.text
    assert "不是网页正文" in response.json()["message"]


# ------------------------------------------------------------- GET /web/embed-check


@respx.mock
@pytest.mark.parametrize(
    ("headers", "embeddable", "x_frame_options", "in_reason"),
    [
        # ① 最老也最硬的那条头
        ({"X-Frame-Options": "DENY"}, False, "DENY", "X-Frame-Options: DENY"),
        ({"X-Frame-Options": "SAMEORIGIN"}, False, "SAMEORIGIN", "X-Frame-Options: SAMEORIGIN"),
        # ② CSP 那一档
        (
            {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"},
            False,
            "",
            "frame-ancestors 'none'",
        ),
        (
            {"Content-Security-Policy": "frame-ancestors https://other.example.com"},
            False,
            "",
            "不在允许嵌的名单里",
        ),
        # ③ 没有这两条头 = 没拦
        ({}, True, "", "可以嵌"),
        ({"Content-Security-Policy": "frame-ancestors *"}, True, "", "允许我们嵌"),
    ],
)
def test_embed_check_judges_the_two_headers(
    local_client: TestClient,
    headers: dict[str, str],
    embeddable: bool,
    x_frame_options: str,
    in_reason: str,
) -> None:
    """判定只有一处（`web.py::_judge_embeddable`），``reason`` 用**对方的原话**。

    表里每一行对应一条口径：``DENY`` / ``SAMEORIGIN`` / ``frame-ancestors 'none'`` /
    frame-ancestors 里是别人的地址 → **不能嵌**；两条头都没有、或 ``*`` → **能嵌**。
    """
    respx.get(PAGE).mock(return_value=httpx.Response(200, headers=headers, text="<html></html>"))

    response = _get(local_client, EMBED_ENDPOINT, PAGE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["embeddable"] is embeddable, body
    assert body["x_frame_options"] == x_frame_options
    assert in_reason in body["reason"], body["reason"]


@respx.mock
def test_our_own_origin_in_frame_ancestors_is_allowed(local_client: TestClient) -> None:
    """``frame-ancestors`` 里写着**我们这个源**时是能嵌的——那一栏就是为此存在的。

    "我们这个源"取的是调用方给的 ``Origin`` 头（界面与后端不同源时浏览器一定会带）。
    这一条同时钉住"不是一律不放行"：漏了它，白名单形式的 frame-ancestors 会被误判成不能嵌。
    """
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Security-Policy": "frame-ancestors http://localhost:1420"},
            text="<html></html>",
        )
    )

    body = _get(
        local_client, EMBED_ENDPOINT, PAGE, headers={"Origin": "http://localhost:1420"}
    ).json()

    assert body["embeddable"] is True, body
    assert body["frame_ancestors"] == "http://localhost:1420"
    assert "允许我们嵌" in body["reason"]


@respx.mock
def test_two_csp_policies_must_both_allow_us(local_client: TestClient) -> None:
    """**两份 CSP 是"都生效"**：一句放行、另一句不让，那就是不能嵌（不是"挑一句算"）。

    这条是 ``_frame_ancestors`` 只回一句时最危险的那个分支：多看一条头就少一次
    "界面说能嵌、点下去白屏"。"""
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200,
            headers=[
                ("content-security-policy", "default-src 'self'; frame-ancestors *"),
                ("content-security-policy", "frame-ancestors 'none'"),
            ],
            text="<html></html>",
        )
    )

    body = _get(local_client, EMBED_ENDPOINT, PAGE).json()

    assert body["embeddable"] is False, body
    assert "'none'" in body["reason"]
    assert body["frame_ancestors"] == "*；'none'", "两句原话都照给，中间用「；」连"


@respx.mock
def test_a_repeated_directive_in_one_policy_counts_once(local_client: TestClient) -> None:
    """同一条头里写两次 ``frame-ancestors``：按规范**第一次**生效（重复指令被忽略）。

    这条与上一条是一对：那里的"都生效"说的是**两条头**，这里的"取第一次"说的是
    **一条头内部**——两件事不能互相套用。
    """
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200,
            headers={
                "Content-Security-Policy": "frame-ancestors *; default-src 'none'; "
                "frame-ancestors 'none'"
            },
            text="<html></html>",
        )
    )

    body = _get(local_client, EMBED_ENDPOINT, PAGE).json()

    assert body["embeddable"] is True, body
    assert body["frame_ancestors"] == "*"


def test_embed_check_never_reads_the_body(local_client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**只拿响应头就断流**：读 body 会把一张首页整份拉下来，而这条端点要的只有那两条头。

    判据是"读了就炸"的一段流：真读了 body，那条 ``AssertionError`` 会顺着端点冒出来
    （500 或直接抛出），用例当场红。
    """

    class _Boom(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("embed-check 读了响应体：它只该拿到响应头就断流")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Frame-Options": "SAMEORIGIN"}, stream=_Boom())

    monkeypatch.setattr(shared_client(), "_transport", httpx.MockTransport(handler))

    body = _get(local_client, EMBED_ENDPOINT, PAGE).json()

    assert body["embeddable"] is False
    assert body["x_frame_options"] == "SAMEORIGIN"


@respx.mock
def test_a_dead_probe_is_reported_not_raised(local_client: TestClient) -> None:
    """**连不上 → 200 + embeddable=false**，不是 502：探不到对方不该把整条阅读模式挡住。

    reason 里写清是哪一档（"没连上对方"），并带上异常原文——用户看得见"是网断了，
    不是这一页不让嵌"。
    """
    respx.get(PAGE).mock(side_effect=httpx.ConnectError("连接被拒"))

    response = _get(local_client, EMBED_ENDPOINT, PAGE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["embeddable"] is False
    assert "没连上对方" in body["reason"]
    assert "连接被拒" in body["reason"], "异常原文要带出来（用户据此知道是网断了）"
    assert body["x_frame_options"] == "" and body["frame_ancestors"] == ""


@respx.mock
def test_a_redirect_is_probed_but_not_followed(local_client: TestClient) -> None:
    """对方跳走时**不追投**：那一跳的头说明不了最终那一页，按不能嵌回（追下去就是一次爬取）。"""
    respx.get(PAGE).mock(
        return_value=httpx.Response(301, headers={"location": "https://real.example.com/final"})
    )

    body = _get(local_client, EMBED_ENDPOINT, PAGE).json()

    assert body["embeddable"] is False
    assert "跳到了别处" in body["reason"]
    assert respx.calls.call_count == 1, "只发一次：不跟第二条"


# ------------------------------------------------------------------ 挂在哪一档


def test_the_web_family_lives_only_on_the_local_router() -> None:
    """这一族**只挂本机档那张白名单上**（出口在本机）。

    不建 app 来判：服务器档要有 PG 才能起来，而这条判据问的是**路由归属**——把 router
    装进一个空 ``FastAPI`` 再读 OpenAPI 就够了（同 ``test_local_backup_api.py`` 那条）。
    """
    from fastapi import FastAPI

    from app.api.v1.router import api_router, local_router

    def paths_of(router: Any) -> set[str]:
        probe = FastAPI()
        probe.include_router(router, prefix="/api/v1")
        return set(probe.openapi()["paths"])

    local_paths = paths_of(local_router)
    assert {PAGE_ENDPOINT, EMBED_ENDPOINT} <= local_paths
    server_paths = paths_of(api_router)
    assert {PAGE_ENDPOINT, EMBED_ENDPOINT}.isdisjoint(server_paths), (
        "服务器档不该有这两条：那边没有「用户正在读一页」这个场景"
    )
