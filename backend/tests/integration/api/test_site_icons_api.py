"""站点图标端点的 HTTP 行为（D11-②）。

镜像同构：``app/api/v1/site_icons.py`` → ``tests/integration/api/test_site_icons_api.py``。

要紧的几条：**要凭据**（`/api/v1` 一律要）、**任意合法域名都代为抓取**（白名单已撤，
表外域名不再 422；"是不是公网地址"由服务层每一跳的 `web.check_public_url` 兜住）、
**畸形域名 422**（写错了）、**取不到 404**（前端据此退回站点字牌 / 通用地球，不是错误）、
**取到时带正确的类型与缓存头**。

一个外部请求都不发：抓取那一层被替换成假的（同 `tests/unit/services/test_site_icons.py`）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import InvalidRequestError
from app.services import site_icons
from app.services.site_icons import SiteIcon, SiteIconService
from tests.conftest import admin_client as admin_session

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


@pytest.fixture
def offline(monkeypatch):
    """默认：这个站点有图标（内容是我们给的那几字节）。"""
    monkeypatch.setattr(
        SiteIconService,
        "_fetch",
        lambda self, host: SiteIcon(content=PNG, media_type="image/png", kind="png"),
    )


def test_icon_needs_credentials() -> None:
    """`<img>`/`fetch` 都要凭据：这一组端点没在 `/auth` 那半边。"""
    from app.main import create_app

    with TestClient(create_app()) as anonymous:
        response = anonymous.get("/api/v1/site-icons", params={"domain": "github.com"})
        assert response.status_code == 401


def test_icon_is_served_with_type_and_cache_headers(
    client: TestClient, offline, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/api/v1/site-icons", params={"domain": "github.com"})

    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400"
    # 收到的字节是我们嗅探过的那几种位图之一，明确禁止浏览器再猜一次
    assert response.headers["x-content-type-options"] == "nosniff"


def test_outsider_domain_is_proxied_too(client: TestClient, offline) -> None:  # type: ignore[no-untyped-def]
    """表外的域名也代为抓取（2026-10-01 用户："这个为啥抓不到真实的图标呢"）。

    改前这里是 422（"不在已知站点表里，不代为抓取"）：真实结果里的绝大多数站点
    （`opendatalab.github.io` 这种）因此只剩一枚字母圆。白名单撤了——
    **该不该抓**由服务层每一跳的 `check_public_url` 兜住（下一条钉着）。
    """
    response = client.get("/api/v1/site-icons", params={"domain": "opendatalab.github.io"})

    assert response.status_code == 200, response.text
    assert response.content == PNG


def test_domain_that_is_not_public_is_404(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SSRF 兜底：候选地址过不了公网校验 → **404**（"这个站点没有图标"那一档），不是 422。

    白名单撤了之后这条就是唯一那道闸：每一跳都在 `check_public_url` 上重新校验，
    过不去就按"没拿到"处理——不该把错误甩给前端，更不该去连内网。
    """

    def refuse(url: str) -> str:
        raise InvalidRequestError("这个地址指向本机或内网")

    monkeypatch.setattr(site_icons, "check_public_url", refuse)

    response = client.get("/api/v1/site-icons", params={"domain": "evil.example.com"})

    assert response.status_code == 404
    assert "取不到图标" in response.json()["message"]


@pytest.mark.parametrize("domain", ["https://github.com", "github.com/favicon.ico", "a b.com"])
def test_malformed_domain_is_refused(client: TestClient, domain: str) -> None:
    assert client.get("/api/v1/site-icons", params={"domain": domain}).status_code == 422


def test_missing_domain_parameter_is_refused(client: TestClient) -> None:
    assert client.get("/api/v1/site-icons").status_code == 422


def test_site_without_an_icon_is_404(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """取不到是**正常的降级路径**：前端退回站点字牌或通用地球，不是错误、也不留空位。"""
    monkeypatch.setattr(SiteIconService, "_fetch", lambda self, host: None)

    response = client.get("/api/v1/site-icons", params={"domain": "github.com"})

    assert response.status_code == 404
    assert "取不到图标" in response.json()["message"]


def test_icon_declared_in_the_home_page_is_served(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """端到端那一档：固定路径全落空、真图标写在首页里 → 照样 200（SPA 站点的真实形态）。

    夹具只换掉最下面两层（抓字节 / 读首页），顺序与解析都走真代码——
    `docs.mthreads.com` 那条路（`/favicon.ico` 是 200 + `text/html` 的兜底页）就是这么走通的。
    """
    monkeypatch.setattr(
        SiteIconService,
        "_get_bytes",
        lambda self, url: PNG if url == "https://docs.mthreads.com/img/favico.ico" else None,
    )
    monkeypatch.setattr(
        SiteIconService,
        "_get_text",
        lambda self, url: ('<link rel="icon" href="/img/favico.ico">', url),
    )

    response = client.get("/api/v1/site-icons", params={"domain": "docs.mthreads.com"})

    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"


def test_wwww_variant_is_tried_by_the_service(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """抓取顺序：`/apple-touch-icon.png` 在前，裸域名在前；都由服务层管，端点只看结果。"""
    tried: list[str] = []

    def fake_fetch_one(self, url: str):  # type: ignore[no-untyped-def]
        tried.append(url)
        if "www." in url or "apple-touch-icon" in url:
            return None
        return SiteIcon(content=PNG, media_type="image/png", kind="png")

    monkeypatch.setattr(SiteIconService, "_fetch_one", fake_fetch_one)

    assert client.get("/api/v1/site-icons", params={"domain": "github.com"}).status_code == 200
    assert tried[:2] == [
        "https://github.com/apple-touch-icon.png",
        "https://www.github.com/apple-touch-icon.png",
    ]
    assert "https://github.com/favicon.ico" in tried
