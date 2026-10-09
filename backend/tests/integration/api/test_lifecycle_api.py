"""数据生命周期的 HTTP 行为（M6 / T6.3、T6.4）。

镜像同构：``app/api/v1/lifecycle.py`` → 本文件。

服务层的性质（影响清单准确、索引清干净、原文可恢复、到期连磁盘一起清）已在
``tests/unit/services/test_lifecycle.py`` 覆盖；这里验的是接进接口之后仍然成立，
以及**删除真的可以从界面上做到**——这本来正是缺失的那一环。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from tests.conftest import admin_client as admin_session

MARKDOWN = "# 眼轴\n\n眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一。\n"


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:

        yield test_client



def _drain_worker() -> None:
    import asyncio

    worker = get_services().worker

    async def drain() -> None:
        while await worker.run_once():
            pass

    asyncio.run(drain())


@pytest.fixture
def ingested(client: TestClient) -> dict:
    """一份已摄入的文档（有切块、有向量、有原文）。"""
    kb = client.post("/api/v1/knowledge-bases", json={"name": "生命周期库"}).json()
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("眼轴.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")}
    )
    document_id = upload.json()["document"]["id"]
    _drain_worker()
    return {"kb_id": kb["id"], "document_id": document_id}


# --------------------------------------------------------------------- 影响清单


def test_document_impact(client: TestClient, ingested: dict) -> None:
    report = client.get(f"/api/v1/documents/{ingested['document_id']}/impact").json()

    assert report["kind"] == "document"
    assert report["chunks"] > 0
    assert report["restorable"] is True


def test_knowledge_base_impact(client: TestClient, ingested: dict) -> None:
    report = client.get(f"/api/v1/knowledge-bases/{ingested['kb_id']}/impact").json()

    assert report["kind"] == "knowledge_base"
    assert report["documents"] == 1
    assert report["restorable"] is False, "知识库级删除不可恢复，界面要据此换警示强度"


def test_impact_of_unknown_target_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/documents/doc_不存在/impact").status_code == 404
    assert client.get("/api/v1/knowledge-bases/kb_不存在/impact").status_code == 404


# --------------------------------------------------------------------- 删除


def test_delete_document_returns_a_trash_entry(client: TestClient, ingested: dict) -> None:
    """返回 200 而不是 204：正文里给出回收站条目与到期时间，
    界面据此提示"7 天内可恢复"——那正是用户最需要知道的一句话。"""
    response = client.delete(f"/api/v1/documents/{ingested['document_id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == ingested["document_id"]
    assert body["expires_at"]


def test_deleted_document_disappears_from_the_list(client: TestClient, ingested: dict) -> None:
    client.delete(f"/api/v1/documents/{ingested['document_id']}")

    items = client.get(f"/api/v1/knowledge-bases/{ingested['kb_id']}/documents").json()["items"]
    assert items == []
    assert client.get(f"/api/v1/documents/{ingested['document_id']}").status_code == 404


def test_deleted_document_is_no_longer_searchable(client: TestClient, ingested: dict) -> None:
    """**删除必须让检索立刻查不到。** 留着向量会造出"已删除却还能被搜到"的幽灵，
    那是最难查的一类状态。"""
    client.delete(f"/api/v1/documents/{ingested['document_id']}")

    hits = client.post(
        "/api/v1/search",
        json={"query": "眼轴长度", "kb_ids": [ingested["kb_id"]], "top_k": 10}
    ).json()["hits"]
    assert hits == []


def test_trash_lists_the_entry(client: TestClient, ingested: dict) -> None:
    client.delete(f"/api/v1/documents/{ingested['document_id']}")

    items = client.get("/api/v1/trash").json()["items"]
    assert [item["document_id"] for item in items] == [ingested["document_id"]]


def test_delete_knowledge_base_reports_the_impact(client: TestClient, ingested: dict) -> None:
    """返回影响清单，界面拿它拼"已删除 N 份文档"的回执——用户删完会想知道删了多少。"""
    report = client.delete(f"/api/v1/knowledge-bases/{ingested['kb_id']}").json()

    assert report["documents"] == 1
    assert client.get(f"/api/v1/knowledge-bases/{ingested['kb_id']}").status_code == 404
    # 知识库级删除不进回收站
    assert client.get("/api/v1/trash").json()["items"] == []


# --------------------------------------------------------------------- 回收站


def test_restore_brings_the_document_back(client: TestClient, ingested: dict) -> None:
    entry = client.delete(f"/api/v1/documents/{ingested['document_id']}").json()

    response = client.post(f"/api/v1/trash/{entry['id']}/restore")

    assert response.status_code == 202, response.text
    new_id = response.json()["document_id"]
    assert new_id != ingested["document_id"], "恢复用新 id，避免旧引用指向同一个文档"

    restored = client.get(f"/api/v1/documents/{new_id}").json()
    assert restored["stage"] == "uploaded", "恢复的是骨架+原文，要重新摄入才有检索能力"
    assert restored["chunk_count"] == 0


def test_restore_keeps_the_original_bytes(ingested: dict, monkeypatch) -> None:
    """恢复要**真的把原文找回来**——那是删除时唯一不可再生的东西。

    单独建 client：签名下载链接要签名密钥，而默认测试环境没配（v0.11 起
    没有密钥就**拒绝签发**，而不是发一条无效链接，见 §11.6）。
    密钥走环境变量；凭据仍由 conftest 的 admin 会话提供。
    """
    monkeypatch.setenv("KYLAB_URL_SIGNING_SECRET", "lifecycle-test-secret")

    from app.core.config import get_settings

    get_settings.cache_clear()
    with admin_session() as client:
        entry = client.delete(f"/api/v1/documents/{ingested['document_id']}").json()
        new_id = client.post(f"/api/v1/trash/{entry['id']}/restore").json()["document_id"]

        url = client.get(
            f"/api/v1/documents/{new_id}/download-url", params={"format": "original"}
        ).json()["url"]
        content = client.get(url)

        assert content.status_code == 200
        assert MARKDOWN.encode() in content.content
    get_settings.cache_clear()


def test_restore_clears_the_trash_entry(client: TestClient, ingested: dict) -> None:
    entry = client.delete(f"/api/v1/documents/{ingested['document_id']}").json()

    client.post(f"/api/v1/trash/{entry['id']}/restore")

    assert client.get("/api/v1/trash").json()["items"] == []


def test_restore_unknown_entry_is_404(client: TestClient) -> None:
    assert client.post("/api/v1/trash/trash_不存在/restore").status_code == 404


def test_drop_trash_is_permanent(client: TestClient, ingested: dict) -> None:
    entry = client.delete(f"/api/v1/documents/{ingested['document_id']}").json()

    assert client.delete(f"/api/v1/trash/{entry['id']}").status_code == 204

    assert client.get("/api/v1/trash").json()["items"] == []
    # 彻底删掉之后恢复不了
    assert client.post(f"/api/v1/trash/{entry['id']}/restore").status_code == 404


# --------------------------------------------------------------------- 鉴权


def test_delete_needs_write_permission(client: TestClient) -> None:
    """删除是破坏性动作，只读密钥不该能做。"""
    console = dict(client.headers)  # 管理员会话
    kb = client.post("/api/v1/knowledge-bases", json={"name": "鉴权库"}, headers=console)
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb.json()['id']}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        headers=console
    )
    doc_id = upload.json()["document"]["id"]

    issued = client.post(
        "/api/v1/api-keys",
        json={"name": "只读", "permission": "readonly", "knowledge_base_ids": []},
        headers=console
    ).json()
    readonly = {"Authorization": f"Bearer {issued['token']}"}

    # 读影响清单可以（界面要显示它）
    assert client.get(f"/api/v1/documents/{doc_id}/impact", headers=readonly).status_code == 200
    # 回收站是控制台专属（含所有人删过什么的跨库元信息），外部密钥进不去
    assert client.get("/api/v1/trash", headers=readonly).status_code == 403
    # 删除不行
    assert client.delete(f"/api/v1/documents/{doc_id}", headers=readonly).status_code == 403
    kb_id = kb.json()["id"]
    denied = client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=readonly)
    assert denied.status_code == 403
