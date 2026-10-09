"""使用者名册的 HTTP 行为（G6）。

镜像同构：``app/api/v1/users.py`` + 上传时的归属标注 → 本文件。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session
from tests.conftest import login_admin

MARKDOWN = "# 眼轴\n\n眼轴长度是主要监测指标。\n"


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:

        yield test_client



@pytest.fixture
def kb_id(client: TestClient) -> str:
    return client.post("/api/v1/knowledge-bases", json={"name": "协作库"}).json()["id"]


def _user(client: TestClient, name: str) -> dict:
    response = client.post("/api/v1/users", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- 名册


def test_create_and_list(client: TestClient) -> None:
    _user(client, "小王")

    listed = client.get("/api/v1/users").json()
    # conftest 已经建了管理员账号（它也在名册里），这里只看其余条目
    others = [item for item in listed["items"] if item["username"] != "admin"]
    assert [item["name"] for item in others] == ["小王"]
    assert others[0]["document_count"] == 0
    # 请求头名由后端给出，免得两边各写一份会漂
    assert listed["header"] == "X-Kylab-Operator"


def test_duplicate_name_is_409(client: TestClient) -> None:
    _user(client, "小王")
    assert client.post("/api/v1/users", json={"name": "小王"}).status_code == 409


def test_delete_user(client: TestClient) -> None:
    created = _user(client, "小王")
    assert client.delete(f"/api/v1/users/{created['id']}").status_code == 204
    remaining = client.get("/api/v1/users").json()["items"]
    # 只剩 conftest 建的管理员账号
    assert [item["username"] for item in remaining] == ["admin"]


def test_blank_name_is_rejected(client: TestClient) -> None:
    # min_length 挡在业务之前；全空白由服务层挡（见服务层用例）
    assert client.post("/api/v1/users", json={"name": ""}).status_code == 422


# --------------------------------------------------------------------- 归属


def test_upload_records_the_operator(client: TestClient, kb_id: str) -> None:
    """带上请求头时，文档要记下是谁传的。"""
    created = _user(client, "小王")

    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "false"},
        # **必须发 id 而不是名字**：HTTP 头只能是 ASCII，而"小王"是中文。
        # 发名字会让浏览器/httpx 直接抛 UnicodeEncodeError（实测踩到）
        headers={"X-Kylab-Operator": created["id"]}
    )

    assert response.status_code == 202, response.text
    body = response.json()["document"]
    assert body["uploaded_by_name"] == "小王"
    assert body["uploaded_by"], "应当记下使用者 id"


def test_upload_without_operator_leaves_it_unrecorded(client: TestClient, kb_id: str) -> None:
    """名册是可选功能：不带请求头就照旧工作，归属显示"未记录"。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "false"}
    )

    body = response.json()["document"]
    assert body["uploaded_by"] is None
    assert body["uploaded_by_name"] == ""


def test_unknown_operator_name_does_not_create_a_user(client: TestClient, kb_id: str) -> None:
    """**拼错的名字不该静默建出一个使用者**——名册很快就脏了。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "false"},
        headers={"X-Kylab-Operator": "user_nobody"}
    )

    assert response.status_code == 202
    assert response.json()["document"]["uploaded_by"] is None
    # 只有 conftest 建的管理员账号，没有因为拼错的名字多出一条
    assert [item["username"] for item in client.get("/api/v1/users").json()["items"]] == ["admin"]


def test_document_list_shows_the_uploader(client: TestClient, kb_id: str) -> None:
    """列表里要能看出是谁传的——那正是名册存在的理由。"""
    created = _user(client, "小王")
    client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "false"},
        headers={"X-Kylab-Operator": created["id"]}
    )

    items = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert items[0]["uploaded_by_name"] == "小王"


def test_user_document_count_reflects_uploads(client: TestClient, kb_id: str) -> None:
    """删使用者之前要能说清"会影响几份文档"。"""
    created = _user(client, "小王")
    client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "false"},
        headers={"X-Kylab-Operator": created["id"]}
    )

    listed = client.get("/api/v1/users").json()["items"]
    assert next(item for item in listed if item["id"] == created["id"])["document_count"] == 1


def test_deleting_the_user_keeps_the_document(client: TestClient, kb_id: str) -> None:
    created = _user(client, "小王")
    client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        params={"start": "false"},
        headers={"X-Kylab-Operator": created["id"]}
    )

    client.delete(f"/api/v1/users/{created['id']}")

    items = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert len(items) == 1, "文档被连带删掉了——那是数据丢失"
    assert items[0]["uploaded_by"] is None
    assert items[0]["uploaded_by_name"] == ""


# --------------------------------------------------------------------- 鉴权


def test_roster_writes_need_an_admin_session() -> None:
    """名册是控制台级配置（与 API Key 同一档），**只有管理员会话能改**。

    **但要说清楚**：名册本身不是鉴权边界——伪造名字只会让归属记错。
    管理名册需要管理员，是因为"谁在名册里"属于部署配置。
    """
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        login_admin(client)
        admin = dict(client.headers)
        created = client.post("/api/v1/users", json={"name": "小王"}, headers=admin)
        assert created.status_code == 201

        issued = client.post(
            "/api/v1/api-keys",
            json={"name": "读写", "permission": "readwrite", "knowledge_base_ids": []},
            headers=admin,
        ).json()
        apikey = {"Authorization": f"Bearer {issued['token']}"}

        # 读：可以（下拉要显示名册）
        assert client.get("/api/v1/users", headers=apikey).status_code == 200
        # 写：不行
        assert (
            client.post("/api/v1/users", json={"name": "小李"}, headers=apikey).status_code == 403
        )
        user_id = created.json()["id"]
        assert client.delete(f"/api/v1/users/{user_id}", headers=apikey).status_code == 403
    get_settings.cache_clear()


