"""Wiki 端点（v24）：库形态开关、目录与状态、读一页带出处、生成入队、清空。

生成本身是异步任务（真跑在 `tests/unit/services/test_wiki.py` 里覆盖）；
这里测协议层：形状、鉴权、以及"页面 → 出处"的组装。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


def _create_kb(client: TestClient, *, wiki: bool = True) -> str:
    response = client.post(
        "/api/v1/knowledge-bases", json={"name": "Wiki 库", "wiki_enabled": wiki}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_page(kb_id: str) -> str:
    """直接往库里写一页带出处的 Wiki（生成路径由服务层用例覆盖）。"""
    from app.core.services import get_services
    from app.storage.base import WikiPageRecord, WikiSourceRecord

    # 用**应用自己的**仓储：存储迁到 PG 后，另建 SQLite 库只会写到另一个库里
    store = get_services().auth._stores.meta
    page_id = f"wpage_{kb_id}_overview"
    store.replace_wiki_pages(
        kb_id,
        [
            WikiPageRecord(
                id=page_id,
                kb_id=kb_id,
                title="总览",
                content_md="开篇[1]。",
                brief="一句话",
            )
        ],
        [
            WikiSourceRecord(
                page_id=page_id,
                chunk_id="chunk_1",
                document_id="doc_missing",
                rank=1,
                index=1,
                heading_path="第一章 > 1.1",
                page=3,
            )
        ],
    )
    return page_id


def test_kb_reports_and_updates_the_wiki_flag(client: TestClient) -> None:
    kb_id = _create_kb(client, wiki=True)
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}").json()["wiki_enabled"] is True

    patched = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"wiki_enabled": False})

    assert patched.status_code == 200, patched.text
    assert patched.json()["wiki_enabled"] is False
    # 不传该字段时不能被"顺手改成默认值"
    assert client.patch("/api/v1/knowledge-bases/" + kb_id, json={"name": "改名"}).json()[
        "wiki_enabled"
    ] is False


def test_wiki_overview_starts_idle_and_generate_enqueues(client: TestClient) -> None:
    kb_id = _create_kb(client)

    overview = client.get(f"/api/v1/knowledge-bases/{kb_id}/wiki").json()
    assert (overview["enabled"], overview["status"], overview["pages"]) == (True, "idle", [])
    assert overview["page_count"] == 0

    accepted = client.post(f"/api/v1/knowledge-bases/{kb_id}/wiki/generate")

    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["kb_id"] == kb_id
    assert accepted.json()["task_id"]
    # 入队之后状态立刻变"生成中"，界面据此开始轮询
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/wiki").json()["status"] == "generating"


def test_generate_is_refused_when_the_flag_is_off(client: TestClient) -> None:
    kb_id = _create_kb(client, wiki=False)

    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/wiki/generate")

    assert response.status_code == 409
    assert "Wiki" in response.json()["message"]


def test_page_detail_carries_content_and_sources(client: TestClient) -> None:
    kb_id = _create_kb(client)
    page_id = _seed_page(kb_id)

    detail = client.get(f"/api/v1/wiki/pages/{page_id}").json()

    assert detail["title"] == "总览" and detail["content_md"] == "开篇[1]。"
    assert detail["kb_id"] == kb_id
    source = detail["sources"][0]
    assert source["index"] == 1
    # 文档没了也照常回：出处是生成时的快照，名字退化回 id
    assert source["document_name"] == "doc_missing"
    assert (source["heading_path"], source["page"]) == ("第一章 > 1.1", 3)


def test_missing_page_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/wiki/pages/wpage_nope_overview").status_code == 404


def test_clear_removes_pages_but_keeps_the_flag(client: TestClient) -> None:
    kb_id = _create_kb(client)
    _seed_page(kb_id)

    cleared = client.delete(f"/api/v1/knowledge-bases/{kb_id}/wiki")

    assert cleared.status_code == 204
    overview = client.get(f"/api/v1/knowledge-bases/{kb_id}/wiki").json()
    assert overview["pages"] == [] and overview["page_count"] == 0
    assert overview["enabled"] is True


def test_readonly_key_cannot_generate_or_clear(client: TestClient) -> None:
    kb_id = _create_kb(client)
    issued = client.post(
        "/api/v1/api-keys",
        json={"name": "只读", "permission": "readonly", "knowledge_base_ids": []},
    ).json()
    readonly = {"Authorization": f"Bearer {issued['token']}"}

    # 看得见
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/wiki", headers=readonly).status_code == 200
    # 写不动
    assert (
        client.post(
            f"/api/v1/knowledge-bases/{kb_id}/wiki/generate", headers=readonly
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/knowledge-bases/{kb_id}/wiki", headers=readonly
        ).status_code
        == 403
    )
