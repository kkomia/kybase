"""知识库内目录的 HTTP 行为（v13）。

镜像同构：``app/api/v1/folders.py`` → 本文件。

这里要证的是端到端成立：建目录 → 把文档放进去 → 按目录/根过滤查得到 →
非空目录删不掉 → 只读凭据建不了目录。服务层的边界（重名、跨库、长度）在
``tests/unit/services/test_folder.py`` 已覆盖，这里只验接口契约与鉴权。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session

MARKDOWN = "# 标题\n\n正文内容。\n"


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


@pytest.fixture
def kb_id(client: TestClient) -> str:
    response = client.post("/api/v1/knowledge-bases", json={"name": "目录测试库"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client: TestClient, kb_id: str, name: str = "kb.md") -> dict:
    # 内容按文件名区分：同内容会被内容去重挡下，第二次上传返回的是**同一份文档**
    # （夹具这么写会让人以为"移动丢了文档"，实际是根本没传进去第二份）
    content = f"# {name}\n\n正文内容。\n"
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (name, io.BytesIO(content.encode()), "text/markdown")},
        params={"start": "false"},
    )
    assert response.status_code == 202, response.text
    return response.json()["document"]


def _create_folder(client: TestClient, kb_id: str, name: str) -> dict:
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _issue(client: TestClient, permission: str) -> dict:
    response = client.post(
        "/api/v1/api-keys",
        json={"name": f"{permission} 钥匙", "permission": permission, "knowledge_base_ids": []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_list_rename_delete_empty_folder(client: TestClient, kb_id: str) -> None:
    created = _create_folder(client, kb_id, "合同")
    assert created["document_count"] == 0

    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/folders").json()["items"]
    assert [item["name"] for item in listed] == ["合同"]

    renamed = client.patch(f"/api/v1/folders/{created['id']}", json={"name": "合同 2026"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "合同 2026"

    assert client.delete(f"/api/v1/folders/{created['id']}").status_code == 204
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/folders").json()["items"] == []


def test_duplicate_folder_name_is_409(client: TestClient, kb_id: str) -> None:
    _create_folder(client, kb_id, "合同")

    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": "合同"})

    assert response.status_code == 409
    assert "已存在" in response.json()["message"]


def test_blank_folder_name_is_422(client: TestClient, kb_id: str) -> None:
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": "   "})
    assert response.status_code == 422


def test_folders_of_unknown_kb_is_404(client: TestClient, kb_id: str) -> None:
    assert client.get("/api/v1/knowledge-bases/kb_nope/folders").status_code == 404


def test_move_document_and_filter_by_folder_and_root(client: TestClient, kb_id: str) -> None:
    folder = _create_folder(client, kb_id, "合同")
    document = _upload(client, kb_id, "合同 A.md")
    other = _upload(client, kb_id, "散装.md")

    moved = client.patch(
        f"/api/v1/documents/{document['id']}/folder", json={"folder_id": folder["id"]}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_id"] == folder["id"]

    # 目录里的
    in_folder = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents", params={"folder_id": folder["id"]}
    ).json()["items"]
    assert [item["id"] for item in in_folder] == [document["id"]]

    # 根目录（未归档）的只剩另一份
    root = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents", params={"root": "true"}
    ).json()["items"]
    assert [item["id"] for item in root] == [other["id"]]

    # 不带过滤 = 全部（老调用点行为不变）
    everything = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert len(everything) == 2

    # 目录计数跟着变
    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/folders").json()["items"]
    assert listed[0]["document_count"] == 1


def test_move_back_to_root(client: TestClient, kb_id: str) -> None:
    folder = _create_folder(client, kb_id, "合同")
    document = _upload(client, kb_id, "合同 A.md")
    client.patch(f"/api/v1/documents/{document['id']}/folder", json={"folder_id": folder["id"]})

    client.patch(f"/api/v1/documents/{document['id']}/folder", json={"folder_id": None})

    root = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents", params={"root": "true"}
    ).json()["items"]
    assert [item["id"] for item in root] == [document["id"]]


def test_filter_by_unknown_folder_is_404(client: TestClient, kb_id: str) -> None:
    response = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents", params={"folder_id": "fld_nope"}
    )
    assert response.status_code == 404


def test_delete_non_empty_folder_is_409(client: TestClient, kb_id: str) -> None:
    folder = _create_folder(client, kb_id, "合同")
    document = _upload(client, kb_id, "合同 A.md")
    client.patch(f"/api/v1/documents/{document['id']}/folder", json={"folder_id": folder["id"]})

    response = client.delete(f"/api/v1/folders/{folder['id']}")

    assert response.status_code == 409
    assert "还有 1 篇" in response.json()["message"]


def test_move_document_to_a_folder_of_another_kb_is_422(client: TestClient, kb_id: str) -> None:
    other_kb = client.post("/api/v1/knowledge-bases", json={"name": "另一个库"}).json()["id"]
    foreign = _create_folder(client, other_kb, "别人的目录")
    document = _upload(client, kb_id, "我的.md")

    response = client.patch(
        f"/api/v1/documents/{document['id']}/folder", json={"folder_id": foreign["id"]}
    )

    assert response.status_code == 422


def test_readonly_key_cannot_manage_folders(client: TestClient, kb_id: str) -> None:
    """只读凭据能看目录，但不能建/删/移动——那是改 owner 的库结构。"""
    folder = _create_folder(client, kb_id, "合同")
    document = _upload(client, kb_id, "我的.md")
    issued = _issue(client, "readonly")
    readonly = {"Authorization": f"Bearer {issued['token']}"}

    assert (
        client.get(f"/api/v1/knowledge-bases/{kb_id}/folders", headers=readonly).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": "偷偷建的"}, headers=readonly
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/documents/{document['id']}/folder",
            json={"folder_id": folder["id"]},
            headers=readonly,
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/folders/{folder['id']}", headers=readonly).status_code == 403


def test_move_missing_document_is_404(client: TestClient, kb_id: str) -> None:
    response = client.patch("/api/v1/documents/doc_nope/folder", json={"folder_id": None})
    assert response.status_code == 404


def test_upload_directly_into_a_folder(client: TestClient, kb_id: str) -> None:
    """在某个目录里点上传，文件就该落进那个目录——否则"传完就找不到"。"""
    folder = _create_folder(client, kb_id, "合同")

    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("合同 A.md", io.BytesIO("# 合同 A\n".encode()), "text/markdown")},
        params={"start": "false", "folder_id": folder["id"]},
    )

    assert response.status_code == 202, response.text
    assert response.json()["document"]["folder_id"] == folder["id"]
    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/folders").json()["items"]
    assert listed[0]["document_count"] == 1


def test_upload_into_a_folder_of_another_kb_is_422(client: TestClient, kb_id: str) -> None:
    other_kb = client.post("/api/v1/knowledge-bases", json={"name": "另一个库"}).json()["id"]
    foreign = _create_folder(client, other_kb, "别人的目录")

    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("x.md", io.BytesIO(b"# x\n"), "text/markdown")},
        params={"start": "false", "folder_id": foreign["id"]},
    )

    assert response.status_code == 422
