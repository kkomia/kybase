"""笔记端点的集成测试（真实本机 SQLite，**本机档**）。

镜像同构：``app/api/v1/notes.py`` → ``tests/integration/api/test_notes_api.py``。

## 为什么这一份打**本机档**（NAS 网页端退役，2026-10-05）

``/notes`` 那一族**只在本机档存在**：笔记落本机库（见 `api/v1/router.py` 的
`local_router` 那一段），服务器档那张表里已经没有它——NAS 上那份笔记数据不迁移、
直接丢。所以 `client` 就是 `conftest.local_client`：**不带凭据**（本机档不设门禁，
调用主体由 `api/auth.py::current_caller` 短路成"本机主人"）。

**摘掉的三条**（原判据与理由都留在下面那一节）：
``test_attach_note_to_knowledge_base`` / ``test_attach_unknown_kb_is_404`` /
``test_notes_require_credentials``。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.local


@pytest.fixture
def client(local_client: TestClient) -> TestClient:
    """本机档客户端（`conftest.local_client`；笔记只在本机档存在）。"""
    return local_client


def test_notes_crud_roundtrip(client: TestClient) -> None:
    created = client.post(
        "/api/v1/notes",
        json={"title": "眼轴监测", "content_md": "# 眼轴\n每三个月测一次", "tags": ["眼科"]},
    )
    assert created.status_code == 201, created.text
    note = created.json()
    assert note["id"].startswith("note_")
    assert note["title"] == "眼轴监测"
    assert note["tags"] == ["眼科"]

    listed = client.get("/api/v1/notes").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == note["id"]
    # 列表项不带正文，但有预览
    assert listed["items"][0]["content_md"] == ""
    assert "眼轴" in listed["items"][0]["preview"]

    updated = client.patch(
        f"/api/v1/notes/{note['id']}", json={"title": "改名", "pinned": True}
    ).json()
    assert updated["title"] == "改名" and updated["pinned"] is True

    assert client.delete(f"/api/v1/notes/{note['id']}").status_code == 204
    assert client.get(f"/api/v1/notes/{note['id']}").status_code == 404


def test_title_is_derived_when_omitted(client: TestClient) -> None:
    note = client.post("/api/v1/notes", json={"content_md": "## 散瞳验光\n正文"}).json()
    assert note["title"] == "散瞳验光"


def test_list_preview_strips_markdown_markers(client: TestClient) -> None:
    """列表预览不该把 ``##`` / ``- [ ]`` / ``**`` 原样摆出来——那是源码不是摘要。"""
    body = "# 标题\n\n- [ ] 待办\n\n**重点**内容"
    client.post("/api/v1/notes", json={"title": "T", "content_md": body})

    item = client.get("/api/v1/notes").json()["items"][0]

    assert "#" not in item["preview"]
    assert "[ ]" not in item["preview"] and "**" not in item["preview"]
    assert "重点内容" in item["preview"]


def test_list_preview_drops_image_and_its_size_marker(client: TestClient) -> None:
    """配图**连同尺寸后缀**一起丢。

    前端拖拽缩放后，图片在正文里是 ``![图](url){width=460}``（见
    frontend/src/features/notes/noteImage.ts）。只丢图片语法的话，每条缩过图的
    笔记，列表预览末尾都会挂一段 ``{width=460}``——那是给编辑器看的，不是内容。
    """
    body = (
        "看图\n\n"
        "![图](/api/v1/notes/n1/images/a.png?expires=1&signature=x){width=460}\n\n"
        "图后面的正文"
    )
    client.post("/api/v1/notes", json={"title": "T", "content_md": body})

    preview = client.get("/api/v1/notes").json()["items"][0]["preview"]

    assert "{width" not in preview
    assert "images/a.png" not in preview
    assert "看图" in preview and "图后面的正文" in preview


def test_unknown_note_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/notes/note_missing").status_code == 404


def test_list_search_and_tags(client: TestClient) -> None:
    client.post(
        "/api/v1/notes", json={"title": "眼轴监测", "content_md": "三个月", "tags": ["眼科"]}
    )
    client.post("/api/v1/notes", json={"title": "无关", "content_md": "其它", "tags": ["杂记"]})

    searched = client.get("/api/v1/notes", params={"q": "眼轴"}).json()
    assert searched["total"] == 1

    by_tag = client.get("/api/v1/notes", params={"tag": "杂记"}).json()
    assert by_tag["total"] == 1 and by_tag["items"][0]["title"] == "无关"

    tags = client.get("/api/v1/notes/tags").json()["items"]
    assert {item["tag"] for item in tags} == {"眼科", "杂记"}


# ------------------------------------------------- 摘掉的三条（2026-10-05）
#
# 1. ``test_attach_note_to_knowledge_base``（原判据：`POST /notes/{id}/attach` 走现有摄入
#    流水线生成一份 Markdown 文档、回填 ``doc_id``，并能在
#    `GET /knowledge-bases/{kb}/documents` 里看到它）——**本机档无法验证**：知识库那一整族
#    不在本机档（那几张表都不在本机库里，读它们整体抛 `KnowledgeBaseUnavailable`），
#    attach 那一半要经提供者客户端打 NAS。本机档里没有可用的 NAS，所以这条判据在
#    "本机档"里没有观察点；覆盖由 `tests/unit/services/test_notes.py`（笔记 → 摄入那条
#    服务链）与 `tests/integration/api/test_local_kb_cache_api.py`（本机 → 提供者那条链）承接。
# 2. ``test_attach_unknown_kb_is_404``：同一条（不存在的库在本机档由"KB 域不可用"先拦下，
#    拿不到 404 这个观察点）。"库不存在 → 404"的判据由服务器档那条 KB 端点自己覆盖
#    （`tests/integration/api/test_rest_api.py::test_upload_to_unknown_kb_fails`）。
# 3. ``test_notes_require_credentials``（原判据：没有令牌访问笔记应当 401）——**只对
#    有账号体系的那一档成立**：本机档不挂 `/auth/*`，`users` / `sessions` / `api_keys`
#    三张表都不在本机库里，主体恒为"本机主人"，没有"没有令牌"这个状态。而笔记只在本机档
#    存在，所以这条没有一个装机形态能承接——鉴权本身由 `test_auth_api.py` 覆盖。


# ------------------------------------------------- AI 处理与配图（v20.2）


class _FakeChat:
    """只实现 complete：笔记 AI 走的是 `ask_raw`（不检索、不拼资料）。"""

    def __init__(self, answer: str = "整理后的正文") -> None:
        self.answer = answer

    def complete(self, messages):  # type: ignore[no-untyped-def]
        return self.answer


def _install_fake_chat(answer: str = "整理后的正文") -> None:
    from app.core.services import get_services
    from tests.conftest import bind_model

    services = get_services()
    bind_model(services.models, "chat", model_id="fake-model", capabilities=["chat"])
    services.chat._chat_factory = lambda config: _FakeChat(answer)


def _png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
        "05570a2e0000000049454e44ae426082"
    )


def test_ai_transform_returns_processed_markdown(client: TestClient) -> None:
    _install_fake_chat("## 标题\n\n- 要点一\n- 要点二")
    note = client.post(
        "/api/v1/notes", json={"title": "T", "content_md": "标题 要点一 要点二"}
    ).json()

    response = client.post(f"/api/v1/notes/{note['id']}/ai", json={"action": "format"})

    assert response.status_code == 200, response.text
    assert response.json()["content_md"].startswith("## 标题")
    # 不自动落库：用户先看结果再决定存不存
    assert client.get(f"/api/v1/notes/{note['id']}").json()["content_md"] == "标题 要点一 要点二"


def test_ai_transform_rejects_unknown_action(client: TestClient) -> None:
    note = client.post("/api/v1/notes", json={"title": "T", "content_md": "正文"}).json()

    response = client.post(f"/api/v1/notes/{note['id']}/ai", json={"action": "translate"})

    assert response.status_code == 422  # 字面量校验在协议层挡掉


def test_ai_transform_on_empty_note_is_a_readable_400(client: TestClient) -> None:
    _install_fake_chat()
    note = client.post("/api/v1/notes", json={"title": "空的"}).json()

    response = client.post(f"/api/v1/notes/{note['id']}/ai", json={"action": "polish"})

    # InvalidRequestError 统一映射 422（与全仓口径一致）
    assert response.status_code == 422
    assert "为空" in response.json()["message"]


def test_upload_and_serve_note_image(client: TestClient) -> None:
    note = client.post("/api/v1/notes", json={"title": "带图", "content_md": "正文"}).json()

    upload = client.post(
        f"/api/v1/notes/{note['id']}/images",
        files={"file": ("shot.png", _png(), "image/png")},
    )

    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["name"].endswith(".png")
    assert body["url"].startswith(f"/api/v1/notes/{note['id']}/images/")
    assert "signature=" in body["url"]

    served = client.get(body["url"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content == _png()


def test_note_image_rejects_a_tampered_signature(client: TestClient) -> None:
    note = client.post("/api/v1/notes", json={"title": "带图", "content_md": "正文"}).json()
    url = client.post(
        f"/api/v1/notes/{note['id']}/images",
        files={"file": ("shot.png", _png(), "image/png")},
    ).json()["url"]

    tampered = url.split("signature=")[0] + "signature=deadbeef"
    assert client.get(tampered).status_code == 401


def test_note_image_rejects_svg(client: TestClient) -> None:
    """SVG 能内嵌脚本，而图片是按 URL 直接加载的——不收。"""
    note = client.post("/api/v1/notes", json={"title": "带图", "content_md": "正文"}).json()

    response = client.post(
        f"/api/v1/notes/{note['id']}/images",
        files={"file": ("x.svg", b"<svg onload=alert(1)/>", "image/svg+xml")},
    )

    assert response.status_code == 422


# ------------------------------------------------------- 文件夹（v14）


def test_folder_crud_roundtrip(client: TestClient) -> None:
    empty = client.get("/api/v1/notes/folders").json()
    assert empty == {"items": [], "unfiled_count": 0, "total_count": 0}

    created = client.post("/api/v1/notes/folders", json={"name": "工作"})
    assert created.status_code == 201, created.text
    folder = created.json()
    assert folder["id"].startswith("fld_")
    assert folder["parent_id"] is None and folder["note_count"] == 0

    renamed = client.patch(f"/api/v1/notes/folders/{folder['id']}", json={"name": "工作台"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "工作台"

    assert client.delete(f"/api/v1/notes/folders/{folder['id']}").status_code == 204
    assert client.get("/api/v1/notes/folders").json()["items"] == []
    assert (
        client.patch(f"/api/v1/notes/folders/{folder['id']}", json={"name": "x"}).status_code == 404
    )


def test_folder_tree_counts_and_nesting(client: TestClient) -> None:
    work = client.post("/api/v1/notes/folders", json={"name": "工作"}).json()
    meetings = client.post(
        "/api/v1/notes/folders", json={"name": "会议", "parent_id": work["id"]}
    ).json()
    assert meetings["parent_id"] == work["id"]

    client.post("/api/v1/notes", json={"title": "在文件夹里", "folder_id": meetings["id"]})
    client.post("/api/v1/notes", json={"title": "没归档的"})

    body = client.get("/api/v1/notes/folders").json()
    counts = {item["id"]: item["note_count"] for item in body["items"]}
    assert counts == {work["id"]: 0, meetings["id"]: 1}  # 子文件夹的条数不算在父上
    assert body["unfiled_count"] == 1
    assert body["total_count"] == 2


def test_folder_name_conflicts_and_validation(client: TestClient) -> None:
    client.post("/api/v1/notes/folders", json={"name": "工作"})

    duplicate = client.post("/api/v1/notes/folders", json={"name": "工作"})
    assert duplicate.status_code == 409
    # 给人话：说得出是哪一个重名，而不是一句"资源冲突"
    assert "工作" in duplicate.json()["message"]

    assert client.post("/api/v1/notes/folders", json={"name": ""}).status_code == 422
    assert (
        client.post(
            "/api/v1/notes/folders", json={"name": "x", "parent_id": "fld_missing"}
        ).status_code
        == 404
    )


def test_folder_cannot_move_into_its_own_subtree(client: TestClient) -> None:
    root = client.post("/api/v1/notes/folders", json={"name": "工作"}).json()
    child = client.post(
        "/api/v1/notes/folders", json={"name": "会议", "parent_id": root["id"]}
    ).json()

    into_self = client.patch(
        f"/api/v1/notes/folders/{root['id']}/parent", json={"parent_id": root["id"]}
    )
    assert into_self.status_code == 422
    assert "它自己" in into_self.json()["message"]

    into_child = client.patch(
        f"/api/v1/notes/folders/{root['id']}/parent", json={"parent_id": child["id"]}
    )
    assert into_child.status_code == 422
    assert "子文件夹" in into_child.json()["message"]

    # 反向（子 → 根）是合法移动
    assert (
        client.patch(
            f"/api/v1/notes/folders/{child['id']}/parent", json={"parent_id": None}
        ).json()["parent_id"]
        is None
    )


def test_move_note_into_folder_and_filter_the_list(client: TestClient) -> None:
    work = client.post("/api/v1/notes/folders", json={"name": "工作"}).json()
    note = client.post("/api/v1/notes", json={"title": "周会"}).json()
    assert note["folder_id"] is None

    moved = client.patch(f"/api/v1/notes/{note['id']}/folder", json={"folder_id": work["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_id"] == work["id"]

    in_folder = client.get("/api/v1/notes", params={"folder": work["id"]}).json()
    assert in_folder["total"] == 1 and in_folder["items"][0]["id"] == note["id"]
    assert client.get("/api/v1/notes", params={"folder": "unfiled"}).json()["total"] == 0

    back = client.patch(f"/api/v1/notes/{note['id']}/folder", json={"folder_id": None})
    assert back.json()["folder_id"] is None
    assert client.get("/api/v1/notes", params={"folder": "unfiled"}).json()["total"] == 1


def test_moving_a_note_does_not_touch_its_content(client: TestClient) -> None:
    """归属走单独的端点：移动不是编辑，正文/标签/置顶一个都不该动。"""
    folder = client.post("/api/v1/notes/folders", json={"name": "工作"}).json()
    note = client.post(
        "/api/v1/notes", json={"title": "周会", "content_md": "正文", "tags": ["会议"]}
    ).json()
    client.patch(f"/api/v1/notes/{note['id']}", json={"pinned": True})
    before = client.get(f"/api/v1/notes/{note['id']}").json()

    after = client.patch(
        f"/api/v1/notes/{note['id']}/folder", json={"folder_id": folder["id"]}
    ).json()

    assert after["content_md"] == before["content_md"]
    assert after["tags"] == before["tags"] and after["pinned"] is True
    assert after["updated_at"] == before["updated_at"]


def test_move_note_to_unknown_folder_is_404(client: TestClient) -> None:
    note = client.post("/api/v1/notes", json={"title": "t"}).json()

    response = client.patch(f"/api/v1/notes/{note['id']}/folder", json={"folder_id": "fld_missing"})

    assert response.status_code == 404


def test_deleting_a_folder_keeps_its_notes(client: TestClient) -> None:
    """删文件夹 → 里面的笔记回到未归档，**一篇都不许少**（本轮的核心纪律）。"""
    work = client.post("/api/v1/notes/folders", json={"name": "工作"}).json()
    meetings = client.post(
        "/api/v1/notes/folders", json={"name": "会议", "parent_id": work["id"]}
    ).json()
    note = client.post(
        "/api/v1/notes", json={"title": "周会纪要", "folder_id": meetings["id"]}
    ).json()

    assert client.delete(f"/api/v1/notes/folders/{work['id']}").status_code == 204

    folders = client.get("/api/v1/notes/folders").json()
    assert folders["items"] == [], "子文件夹应当跟着父级一起删"
    assert folders["unfiled_count"] == 1 and folders["total_count"] == 1

    survived = client.get(f"/api/v1/notes/{note['id']}")
    assert survived.status_code == 200, "笔记不该跟着文件夹消失"
    assert survived.json()["title"] == "周会纪要" and survived.json()["folder_id"] is None


def test_unknown_folder_list_filter_is_empty_not_404(client: TestClient) -> None:
    client.post("/api/v1/notes", json={"title": "无关"})

    response = client.get("/api/v1/notes", params={"folder": "fld_missing"})

    assert response.status_code == 200 and response.json()["total"] == 0


def test_folder_routes_do_not_shadow_the_note_detail_route(client: TestClient) -> None:
    """``GET /notes/folders`` 不能被 ``/{note_id}`` 抢走（路由声明顺序的守卫）。"""
    client.delete("/api/v1/notes/nonexistent")  # 顺手确认细节路由还在（404 也算走通）

    assert client.get("/api/v1/notes/folders").status_code == 200
