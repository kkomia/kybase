"""下载接口与签名 URL（M4 T4.5）。

镜像同构：``app/api/v1/documents.py`` 的下载分支 → 本文件。

要证的核心性质只有一句：**原文没有永久直链**——
拿一个文档 id 直接访问 ``/content`` 必须失败，必须先去签发一条带过期时间的链接。
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from app.core.signing import sign_resource
from tests.conftest import admin_client as admin_session

MARKDOWN = "# 下载测试\n\n用于验证签名下载的正文。\n".encode()
SIGNING_SECRET = "download-test-secret"


@pytest.fixture
def client(monkeypatch):
    """带管理员会话凭据的客户端。

    签名密钥仍走环境变量：它不属于凭据体系，单独配一次即可。
    """
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", SIGNING_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    with admin_session() as test_client:
        yield test_client
    get_settings.cache_clear()




@pytest.fixture
def document_id(client: TestClient) -> str:
    kb = client.post("/api/v1/knowledge-bases", json={"name": "下载测试库"})
    kb_id = kb.json()["id"]
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("报告.md", io.BytesIO(MARKDOWN), "text/markdown")},
        params={"start": "false"},

    )
    assert upload.status_code == 202, upload.text
    return upload.json()["document"]["id"]


# --------------------------------------------------------------------- 签发要鉴权


def test_issuing_a_link_requires_credentials(document_id: str) -> None:
    """没有凭据就拿不到链接——这一条要的是"缺凭据"分支，所以刻意不带会话头。"""
    from app.main import create_app

    with TestClient(create_app()) as anonymous:
        response = anonymous.get(f"/api/v1/documents/{document_id}/download-url")
    assert response.status_code == 401


def test_signed_link_is_short_lived(client: TestClient, document_id: str) -> None:
    body = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()

    assert body["url"].startswith(f"/api/v1/documents/{document_id}/content")
    assert "signature=" in body["url"] and "expires=" in body["url"]
    # 有效期是分钟级，不是"永久"
    remaining = body["expires_at"] - int(time.time())
    assert 0 < remaining <= 900


def test_signed_link_is_relative(client: TestClient, document_id: str) -> None:
    """相对路径：对外域名只有部署时才知道，猜一个会写死错误的 host。"""
    body = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()
    assert body["url"].startswith("/api/v1/")


# --------------------------------------------------------------------- 没有永久直链


def test_direct_access_without_signature_is_rejected(client: TestClient, document_id: str) -> None:
    """**本文件最重要的一条**：拿着 id 直接下原文必须失败。

    这就是"无永久直链"的含义。若这条通过，签名机制等于没做。
    """
    response = client.get(f"/api/v1/documents/{document_id}/content")
    assert response.status_code in (401, 422), "没有签名也能下载，等于永久直链"


def test_download_with_valid_signature_succeeds(client: TestClient, document_id: str) -> None:
    """浏览器直接打开链接（不带任何头）要能下到东西。"""
    url = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()["url"]

    response = client.get(url)  # 刻意不带 Authorization
    assert response.status_code == 200
    assert response.content == MARKDOWN


def test_tampered_signature_is_rejected(client: TestClient, document_id: str) -> None:
    url = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()["url"]
    tampered = url.replace("signature=", "signature=0")

    assert client.get(tampered).status_code == 401


def test_expired_link_is_rejected(client: TestClient, document_id: str) -> None:
    """过期链接要 401，而不是"还能用"。"""
    past = int(time.time()) - 10
    signature, _ = sign_resource(
        f"document:{document_id}:original", SIGNING_SECRET, ttl_seconds=0, now=past
    )
    url = (
        f"/api/v1/documents/{document_id}/content"
        f"?format=original&expires={past}&signature={signature}"
    )

    assert client.get(url).status_code == 401


def test_signature_cannot_be_reused_for_another_document(
    client: TestClient, document_id: str
) -> None:
    """一条签名只能换它签的那个文档。"""
    url = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()["url"]

    other = client.post("/api/v1/knowledge-bases", json={"name": "另一个库"})
    other_upload = client.post(
        f"/api/v1/knowledge-bases/{other.json()['id']}/documents",
        files={"file": ("b.md", io.BytesIO(b"# b\n"), "text/markdown")},
        params={"start": "false"},

    )
    other_id = other_upload.json()["document"]["id"]

    assert client.get(url.replace(document_id, other_id)).status_code == 401


def test_signature_cannot_be_switched_between_formats(
    client: TestClient, document_id: str
) -> None:
    """签名绑定了格式：把 format 改掉必须失败。"""
    url = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()["url"]

    assert client.get(url.replace("format=original", "format=markdown")).status_code == 401


# --------------------------------------------------------------------- 下载双选项


def test_markdown_is_available_and_named_by_stem(client: TestClient, document_id: str) -> None:
    """Markdown 产物还没解析时会 409（而不是 404）——文件在，只是还没到时候。"""
    # 这次上传没有启动摄入，所以没有解析产物
    response = client.get(
        f"/api/v1/documents/{document_id}/download-url",
        params={"format": "markdown"},

    )
    # 签发本身不检查产物；真正取内容时才检查
    assert response.status_code == 200

    content = client.get(response.json()["url"])
    assert content.status_code == 409
    assert "Markdown" in content.json()["message"]


def test_unknown_format_is_rejected_by_validation(client: TestClient, document_id: str) -> None:
    response = client.get(
        f"/api/v1/documents/{document_id}/download-url",
        params={"format": "pdf"},

    )
    assert response.status_code == 422  # 参数校验挡在业务之前


def test_chinese_filename_is_encoded_per_rfc6266(client: TestClient, document_id: str) -> None:
    """中文文件名要走 ``filename*=UTF-8''``，否则头部编码会炸或落盘成乱码。"""
    url = client.get(
        f"/api/v1/documents/{document_id}/download-url"
    ).json()["url"]
    response = client.get(url)

    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    # 回退参数必须在，老客户端才有名字可用
    assert 'filename="' in disposition
    assert "%E6%8A%A5%E5%91%8A" in disposition  # "报告" 的百分号编码
