"""头像的 HTTP 行为（v0.29）。

镜像同构：``app/api/v1/avatars.py`` + ``/auth/avatar`` → 本文件。

这一层要验的是**协议层那两处最容易错的地方**：

1. 图片走的是**签名链接**——``<img src>`` 带不了 Authorization 头，
   而签名绑的是"谁的哪张图"（换一个 user id 用同一份签名要取不到）；
2. 账号里那个 ``avatar_url`` 必须与取图的端点对得上（上传、``/auth/me``、
   名册三条路都得给同一种链接）。
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（与名册那组同一个夹具写法）。"""
    with admin_session() as test_client:
        yield test_client


#: 一张 1×1 的 PNG。**用 base64 写进来**：直接把字节写在源码里过不了
#: 编辑器的编码往返（实测写出过真的 NUL），而这里只需要"前 8 位是 PNG 魔数"。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_avatar_round_trip(client: TestClient) -> None:
    upload = client.post("/api/v1/auth/avatar", files={"file": ("a.png", PNG, "image/png")})

    assert upload.status_code == 200, upload.text
    url = upload.json()["avatar_url"]
    assert url.startswith("/api/v1/avatars/")

    # 取图**不带 Authorization**：这正是签名链接存在的理由
    image = client.get(url)
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.content == PNG

    # 换一个 user id 用同一份签名：取不到。
    # 那个 id 不存在 → 404（"没有这个人"本身不是秘密，id 是可枚举的）；
    # 真正要守住的是下一行：**本人 + 篡改过的签名 → 401**。
    forged = url.replace("/avatars/user_", "/avatars/user_x", 1)
    assert client.get(forged).status_code == 404
    tampered = url.replace("signature=", "signature=0", 1)
    assert client.get(tampered).status_code == 401

    # `/auth/me` 也要带链接：刷新之后侧栏那颗头像就靠它。
    #
    # **比的是路径不是整串**：签名里带着过期时间戳（`int(time.time()) + TTL`），
    # 两次请求正好跨过一秒边界时会得到两个都合法、但签名不同的链接——
    # 原来这里写的是整串相等，于是这条用例大约每几十次跑就红一次（实测撞到过）。
    # 要守住的是"指向同一张图、而且取得到"，那就照这个断言。
    me_url = client.get("/api/v1/auth/me").json()["avatar_url"]
    assert me_url.split("?")[0] == url.split("?")[0]
    assert client.get(me_url).content == PNG

    # 名册那一份同样要有（成员列表里也要显示头像）
    roster = client.get("/api/v1/users").json()["items"]
    assert any(item["avatar_url"].split("?")[0] == url.split("?")[0] for item in roster)

    cleared = client.delete("/api/v1/auth/avatar")
    assert cleared.status_code == 200
    assert cleared.json()["avatar_url"] == ""


def test_uploading_a_non_image_is_rejected(client: TestClient) -> None:
    """按**魔数**认格式：声明成 ``image/png`` 的一段脚本不该被存下来。"""
    response = client.post(
        "/api/v1/auth/avatar",
        files={"file": ("evil.png", b"#!/bin/sh\nrm -rf /", "image/png")},
    )

    # 422 是这个项目对"请求参数不合法"的统一口径（见 core/exceptions.py）
    assert response.status_code == 422, response.text
    assert "不像一张图片" in response.json()["message"]


def test_avatar_upload_needs_a_session() -> None:
    """头像是"自己的东西"：没有登录会话（例如 API Key 通道）就不能设。"""
    from fastapi.testclient import TestClient as Client

    from app.main import create_app

    with Client(create_app()) as bare:
        assert (
            bare.post(
                "/api/v1/auth/avatar", files={"file": ("a.png", PNG, "image/png")}
            ).status_code
            == 401
        )
