"""Webhook 的 HTTP 行为（M4 / T4.6）。

镜像同构：``app/api/v1/webhooks.py`` → 本文件。

服务层的投递与签名已由 ``tests/unit/services/test_webhook.py`` 覆盖（27 项），
这里只管**接口边界**：谁能调、返回什么、密钥怎么回显、错误怎么报。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:
        yield test_client


def _create(client: TestClient, **overrides) -> dict:
    payload = {"url": "https://example.com/hook", "secret": "whsec_abcdefghijkl"}
    payload.update(overrides)
    response = client.post("/api/v1/webhooks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ 事件清单


def test_events_are_discoverable(client: TestClient) -> None:
    """事件清单必须是**接口**而不是只写在文档里。

    接收端要靠它配置订阅，而拼错一个事件名的表现是"订阅成功但永远收不到"——
    那种失败最难查，所以让它可以被程序读到。
    """
    body = client.get("/api/v1/webhooks/events").json()
    assert "document.indexed" in body["events"]
    assert "document.failed" in body["events"]
    assert "document.deleted" in body["events"]
    assert body["signature_header"] == "X-Kylab-Signature"
    assert body["max_attempts"] >= 1


def test_delivery_semantics_is_declared(client: TestClient) -> None:
    """**至少一次**必须被说出来。

    不说的话，接收端会把重复投递当成 bug 去查——而它是契约的一部分。
    """
    body = client.get("/api/v1/webhooks/events").json()
    assert body["delivery_semantics"] == "at-least-once"


# ------------------------------------------------------------------ 订阅管理


def test_create_returns_secret_once_then_masks_it(client: TestClient) -> None:
    """明文只在新建成的那一次返回。

    能反复读到签名密钥就等于签名没有意义——任何能列出订阅的人都能伪造载荷。
    """
    created = _create(client)
    assert created["secret"] == "whsec_abcdefghijkl"
    assert created["secret_masked"] == "whse…ijkl"
    assert created["has_secret"] is True

    listed = client.get("/api/v1/webhooks").json()["items"]
    assert listed[0]["secret"] is None
    assert listed[0]["secret_masked"] == "whse…ijkl"


def test_short_secret_is_fully_masked(client: TestClient) -> None:
    # 短串既没信息量又容易被拼出来，整体打码
    created = _create(client, secret="abc")
    assert created["secret_masked"] == "…"


def test_no_secret_means_no_mask(client: TestClient) -> None:
    created = _create(client, secret=None)
    assert created["secret"] is None
    assert created["secret_masked"] is None
    assert created["has_secret"] is False


def test_empty_events_means_all(client: TestClient) -> None:
    created = _create(client)
    assert set(created["events"]) >= {"document.indexed", "document.failed"}


def test_unknown_event_is_422(client: TestClient) -> None:
    # 调用方给错了东西 → invalid_request（422），不是 500；且走统一信封
    response = client.post(
        "/api/v1/webhooks",
        json={"url": "https://example.com/hook", "events": ["document.done"]},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    assert "不支持的事件" in body["message"]


def test_bad_scheme_is_422(client: TestClient) -> None:
    response = client.post("/api/v1/webhooks", json={"url": "ftp://example.com/hook"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_toggle_enabled(client: TestClient) -> None:
    created = _create(client)
    response = client.patch(
        f"/api/v1/webhooks/{created['id']}", json={"enabled": False}
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert client.get("/api/v1/webhooks").json()["items"][0]["enabled"] is False


def test_delete(client: TestClient) -> None:
    created = _create(client)
    assert client.delete(f"/api/v1/webhooks/{created['id']}").status_code == 204
    assert client.get("/api/v1/webhooks").json()["items"] == []


def test_missing_subscription_is_404(client: TestClient) -> None:
    patched = client.patch(
        "/api/v1/webhooks/wh_nope", json={"enabled": False}
    )
    assert patched.status_code == 404
    assert client.delete("/api/v1/webhooks/wh_nope").status_code == 404


# ------------------------------------------------------------------ 鉴权档位


def test_subscriptions_are_console_level(client: TestClient) -> None:
    """订阅管理是控制台级，不是普通读写。

    一个订阅意味着"这个服务会主动往某个地址发文档内容"。
    读写 Key 若能建订阅，持有它的人就能把知识库内容转发到自己的服务器——
    那是数据外泄，不是普通写操作。
    """
    readonly = _issue(client, permission="readonly")
    readwrite = _issue(client, permission="readwrite")

    for issued in (readonly, readwrite):
        external = {"Authorization": f"Bearer {issued['token']}"}
        response = client.post(
            "/api/v1/webhooks", json={"url": "https://example.com/hook"}, headers=external
        )
        assert response.status_code == 403, f"{issued['permission']} 不该能建订阅"
        # 连列出来都不行：列表里虽然没有明文密钥，但它暴露了"知识库内容被推到哪"
        assert client.get("/api/v1/webhooks", headers=external).status_code == 403


def test_events_list_allows_readonly(client: TestClient) -> None:
    """读事件清单是安全的：它只说明支持哪些事件，不含任何库内数据。

    **要允许只读**：集成方要用它来配置订阅，而配置订阅这件事本身
    需要管理员权限——但那不意味着"读清单"也该要。
    """
    readonly = _issue(client, permission="readonly")
    response = client.get(
        "/api/v1/webhooks/events",
        headers={"Authorization": f"Bearer {readonly['token']}"}
    )
    assert response.status_code == 200
    assert "document.indexed" in response.json()["events"]


def _issue(client: TestClient, *, permission: str) -> dict:
    response = client.post(
        "/api/v1/api-keys",
        json={"name": f"给集成方的 {permission} 钥匙", "permission": permission,
              "knowledge_base_ids": []},

    )
    assert response.status_code == 201, response.text
    return response.json()
