"""下载签名密钥：本机档自己生成 + **没配好是 503（不是 401）**（修那次"点预览被踢到登录页"）。

## 现场

壳里点产物卡片的「预览」→ 前端要签名链接 → 边车回 **401** → 前端把 401 当"登录失效"
→ 弹到登录页。三条事实叠在一起才成那个现场：

1. 密钥住在 ``app_settings['auth.url_signing_secret']``（键名常量在 ``services/auth.py``），
   回落 ``KYLAB_URL_SIGNING_SECRET``，两者都没有就是"没配"；
2. **只有服务器档会生成它**（``POST /auth/setup`` → ``AuthService._ensure_signing_secret``），
   而本机档（边车）没有任何初始化流程、库是全新的 → 永远是"没配"；
3. "没配"那一档回的是 **401**（前端据此跳登录页）——而这件事**用户没做错任何事**。

## 这一份用例钉三件

- **本机档装配时自己生成一条**，而且是**幂等**的（第二次装配不许换值：换了的话
  已经发出去的链接会一起失效）；
- **没配 → 503 + 那句话**（挑三条代表路由：产物签名链接 / 笔记配图 / 头像）；
- **配好之后同一路由 200**，并且那次签发的链接**真的能取到内容**（走一遍
  ``download_file_content`` 的校验，而不是只看状态码）。

快照那条（密钥不随包走）在 ``tests/unit/storage/sqlite_impl/test_backup_archive.py``
与 ``tests/unit/services/test_backup_snapshot.py`` 各自的哨兵串用例里。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.services import reset_services
from app.core.storage import get_stores, reset_stores
from app.services import runtime_config
from app.services.auth import URL_SIGNING_SECRET_SETTING
from app.storage.base import (
    ARTIFACT_IN_OBJECTS,
    ConversationArtifactRecord,
    ConversationRecord,
    ConversationTransfer,
)

pytestmark = pytest.mark.local

DEVICE = "dev-signing-0001"
KEY = "art_signing_0001"
BODY = b"signed-content-bytes"
CONVERSATION = "conv_signing_0001"


@contextlib.contextmanager
def _local_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """本机档的真 app（与 ``test_local_backup_api._local_app`` 同一手法）。

    环境变量显式置空/置值：``.env`` 里真有可能配着别的东西，而用例不该看它——
    尤其 ``KYLAB_URL_SIGNING_SECRET``：**它一旦有值，"没配"那一档就测不到了**。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", "")
    monkeypatch.setenv("KYLAB_SERVER_URL", "")
    monkeypatch.setenv("KYLAB_TOKEN", "")
    monkeypatch.setenv("KYLAB_DEVICE_ID", DEVICE)
    # 关掉自动快照（组合根起来时队列线程会跑一轮，这里不需要它出场）
    monkeypatch.setitem(runtime_config.DEFAULTS, "provider.backup.every_hours", "0")
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        reset_services()
        reset_stores()
        get_settings.cache_clear()


def _seed_conversation_with_file() -> None:
    """造一条会话 + 一份对象档产物，并把它的字节放进文件区（``data_dir/<Key>``）。

    签名链接那条路要走到"签发"这一步，就得先有一份**真的读得到**的文件
    （``file_download_url`` 会先读一次确认它存在）。
    """
    stores = get_stores()
    assert stores.ledger is not None
    stores.ledger.write_imported_conversation(
        ConversationTransfer(
            conversation=ConversationRecord(
                id=CONVERSATION,
                title="带一份产物",
                created_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
                updated_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            ),
            artifacts=[
                ConversationArtifactRecord(
                    id=KEY,
                    conversation_id=CONVERSATION,
                    name="报告.txt",
                    format="txt",
                    size_bytes=len(BODY),
                    storage=ARTIFACT_IN_OBJECTS,
                    location=f"files/{KEY}.txt",
                )
            ],
        )
    )
    target = Path(get_settings().data_dir) / f"files/{KEY}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(BODY)


def _download_url_url(**params: Any) -> str:
    base = f"/api/v1/conversations/{CONVERSATION}/files/download-url"
    query = "&".join(f"{name}={value}" for name, value in params.items())
    return f"{base}?{query}" if query else base


# ------------------------------------------------------------------ ① 本机档自己生成


def test_the_local_deployment_generates_the_secret_and_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机档装配时生成一条；**第二次装配是同一把**（幂等，不许每次启动换一把）。

    换一把的后果不是"多了一条记录"，而是"所有已经发出去的签名链接一起失效"——
    那正是这条键独立存在的原因（凭据会轮换，链接不该跟着死）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/local/status").status_code == 200
        first = get_stores().meta.get_setting(URL_SIGNING_SECRET_SETTING)
        assert first, "本机档装配完就该有这条密钥"
        assert len(first) >= 32, "是 token_urlsafe(32) 那一档的随机串"

    with _local_app(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/local/status").status_code == 200
        assert get_stores().meta.get_setting(URL_SIGNING_SECRET_SETTING) == first, (
            "第二次装配不许换一把"
        )


# ------------------------------------------------------------------ ② 没配好 = 503


def test_a_missing_secret_is_503_not_401_on_the_artifact_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """产物签名链接：没配密钥 → **503 + 那句话**（不是 401）。

    401 的语义是"你的凭据不对"，前端据此跳登录页——而这次是**我们这边没配好**：
    用户点一张产物卡片的「预览」，不该被弹到登录页。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _seed_conversation_with_file()
        get_stores().meta.delete_setting(URL_SIGNING_SECRET_SETTING)  # 摆出"没配"那一档

        response = client.get(_download_url_url(key=KEY))

        assert response.status_code == 503, response.text
        assert response.status_code != 401, "不是用户的凭据问题"
        body = response.json()
        assert body["code"] == "service_unavailable"
        assert "签名密钥" in body["message"], "人话保留：说清缺的是什么、下一步做什么"


def test_a_missing_secret_is_503_on_the_note_image_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """笔记配图那条（上传时签发 + 取图时校验）同样是 503。

    上传那一处原来抛的是 ``InvalidRequestError``（422）——"把字段改对再来"，
    可用户没有任何字段可以改（缺的是服务端的一条密钥）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        stores = get_stores()
        get_stores().meta.delete_setting(URL_SIGNING_SECRET_SETTING)
        note = client.post("/api/v1/notes", json={"title": "带一张图"}).json()

        upload = client.post(
            f"/api/v1/notes/{note['id']}/images",
            files={"file": ("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )

        assert upload.status_code == 503, upload.text
        assert upload.json()["code"] == "service_unavailable"
        assert stores.meta.get_setting(URL_SIGNING_SECRET_SETTING) is None


def test_a_missing_secret_is_503_on_the_avatar_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """头像那条（取图时校验）也是 503。

    **为什么单独起一个 app**：``avatars`` 那张 router 不在本机档的白名单里
    （``api/v1/router.py`` 的"明确不挂"清单里有它），所以这里把它挂在一个裸 app 上——
    依赖注入用的是**同一个** ``get_services``（进程级那一个），端点的行为一模一样。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/local/status").status_code == 200
        get_stores().meta.delete_setting(URL_SIGNING_SECRET_SETTING)
        probe = FastAPI()
        register_exception_handlers(probe)
        from app.api.v1 import avatars

        probe.include_router(avatars.router, prefix="/api/v1")

        with TestClient(probe) as client:
            response = client.get("/api/v1/avatars/user_1?expires=0&signature=x")

        assert response.status_code == 503, response.text
        assert response.json()["code"] == "service_unavailable"
        assert "签名密钥" in response.json()["message"]


# ------------------------------------------------------------------ ③ 配好之后照常能用


def test_the_signed_link_works_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配好密钥之后：签发 **200**，而且那条链接**真的能取到内容**（走一遍校验）。

    只断言"签发回 200"是不够的：签发与校验是同一把密钥的两头，报文里少一个参数、
    或者校验那边用了另一把，都会让"签发成功"变成一个空承诺。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _seed_conversation_with_file()
        stores = get_stores()
        stores.meta.delete_setting(URL_SIGNING_SECRET_SETTING)

        # 没配 → 503（同一条路由的另一档）
        assert client.get(_download_url_url(key=KEY)).status_code == 503

        # 配上（走真实那条读路径：运行期配置读的就是这一条键）
        stores.meta.set_setting(URL_SIGNING_SECRET_SETTING, "signing-secret-for-the-test")

        signed = client.get(_download_url_url(key=KEY, disposition="inline"))
        assert signed.status_code == 200, signed.text
        url = signed.json()["url"]

        content = client.get(url)

        assert content.status_code == 200, content.text
        assert content.content == BODY, "取回来的就是那份文件的字节"
        assert content.headers["X-Content-Type-Options"] == "nosniff"

        # 篡改签名 → 401（**那一档才是 401**：链接的凭据不对，重签一条就好）
        tampered = client.get(url.replace("signature=", "signature=00"))
        assert tampered.status_code == 401, tampered.text
        assert tampered.json()["code"] == "unauthorized"
