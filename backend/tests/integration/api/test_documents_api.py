"""文档列表的筛选（v13 之后）。

镜像同构：``app/api/v1/documents.py`` → 本文件。

服务层/存储层的过滤分支在这里做端到端验证：文件名模糊搜（含通配符转义）、
状态过滤、来源过滤、非法枚举值被挡在 422。目录维度的过滤在
``test_folders_api.py`` 已覆盖，这里只补新加的三个参数。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from tests.conftest import admin_client as admin_session


@pytest.fixture
def client():
    with admin_session() as test_client:
        yield test_client


@pytest.fixture
def kb_id(client: TestClient) -> str:
    response = client.post("/api/v1/knowledge-bases", json={"name": "筛选测试库"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client: TestClient, kb_id: str, name: str) -> dict:
    # 内容按文件名区分：同内容会被内容去重挡下，第二次上传拿到的是同一份文档
    content = f"# {name}\n\n正文。\n"
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (name, io.BytesIO(content.encode()), "text/markdown")},
        params={"start": "false"},
    )
    assert response.status_code == 202, response.text
    return response.json()["document"]


def _list_names(client: TestClient, kb_id: str, **params: str) -> list[str]:
    response = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", params=params)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


def test_search_by_name_substring(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id, "2026 合同 A.md")
    _upload(client, kb_id, "2026 合同 B.md")
    _upload(client, kb_id, "预算表.md")

    assert sorted(_list_names(client, kb_id, q="合同")) == ["2026 合同 A.md", "2026 合同 B.md"]
    assert _list_names(client, kb_id, q="预算") == ["预算表.md"]
    assert _list_names(client, kb_id, q="不存在") == []


def test_search_is_case_insensitive_for_ascii(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id, "Report.DOCX")

    assert _list_names(client, kb_id, q="report") == ["Report.DOCX"]
    assert _list_names(client, kb_id, q="REPORT") == ["Report.DOCX"]


def test_search_treats_wildcards_literally(client: TestClient, kb_id: str) -> None:
    """搜 "a_b" 不该匹配到 "aXb"——LIKE 通配符必须转义成字面量。"""
    _upload(client, kb_id, "a_b.md")
    _upload(client, kb_id, "aXb.md")
    _upload(client, kb_id, "100%完成.md")

    assert _list_names(client, kb_id, q="a_b") == ["a_b.md"]
    assert _list_names(client, kb_id, q="100%") == ["100%完成.md"]


def test_blank_search_is_no_filter(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id, "甲.md")
    _upload(client, kb_id, "乙.md")

    assert len(_list_names(client, kb_id, q="   ")) == 2


def test_filter_by_stage(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id, "待处理.md")

    # 只登记未启动：全部停在 uploaded
    assert _list_names(client, kb_id, stage="uploaded") == ["待处理.md"]
    assert _list_names(client, kb_id, stage="failed") == []


def test_filter_by_source_kind(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id, "上传的.md")

    assert _list_names(client, kb_id, source_kind="upload") == ["上传的.md"]
    assert _list_names(client, kb_id, source_kind="rss") == []


def test_combined_filters(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id, "合同 2026.md")
    _upload(client, kb_id, "合同 2025.md")
    _upload(client, kb_id, "预算.md")

    # 文件名 + 状态同时收窄；两者都命中才返回
    assert sorted(_list_names(client, kb_id, q="合同", stage="uploaded")) == [
        "合同 2025.md",
        "合同 2026.md",
    ]
    assert _list_names(client, kb_id, q="合同", stage="failed") == []


def test_unknown_stage_is_422(client: TestClient, kb_id: str) -> None:
    """拼错的枚举值必须报错，而不是静默返回空列表。"""
    response = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents", params={"stage": "no_such_stage"}
    )

    assert response.status_code == 422


def test_filter_composes_with_folder(client: TestClient, kb_id: str) -> None:
    folder = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": "合同"}
    ).json()
    inside = _upload(client, kb_id, "合同 2026.md")
    _upload(client, kb_id, "合同 2025.md")
    client.patch(f"/api/v1/documents/{inside['id']}/folder", json={"folder_id": folder["id"]})

    assert _list_names(client, kb_id, folder_id=folder["id"], q="2026") == ["合同 2026.md"]
    assert _list_names(client, kb_id, folder_id=folder["id"], q="2025") == []


# --------------------------------------------------------------------- 分页


def _list_page(client: TestClient, kb_id: str, **params: str) -> dict:
    response = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_list_returns_one_page_with_total(client: TestClient, kb_id: str) -> None:
    """默认一页 50 篇；``total`` 是总数而不是本页条数。"""
    for index in range(5):
        _upload(client, kb_id, f"第 {index} 篇.md")

    page = _list_page(client, kb_id, limit="2")

    assert len(page["items"]) == 2
    assert (page["total"], page["limit"], page["offset"]) == (5, 2, 0)

    # 无参：全量（少于默认页大小）且 total 与 items 对得上
    whole = _list_page(client, kb_id)
    assert len(whole["items"]) == 5 and whole["total"] == 5


def test_pages_do_not_overlap_or_drop(client: TestClient, kb_id: str) -> None:
    """逐页翻完，id 集合必须恰好等于全集——分页最常见的错就是漏/重。"""
    for index in range(5):
        _upload(client, kb_id, f"分页 {index}.md")

    collected: list[str] = []
    offset = 0
    while True:
        page = _list_page(client, kb_id, limit="2", offset=str(offset))
        collected.extend(item["id"] for item in page["items"])
        offset += 2
        if offset >= page["total"]:
            break

    assert len(collected) == len(set(collected)) == 5


def test_total_follows_the_filter_not_the_page(client: TestClient, kb_id: str) -> None:
    """``total`` 用的是与 items 相同的过滤条件：搜出来的 3 篇就是 3，不是全库 5。"""
    _upload(client, kb_id, "合同 2026.md")
    _upload(client, kb_id, "合同 2025.md")
    _upload(client, kb_id, "合同 2024.md")
    _upload(client, kb_id, "预算表.md")
    _upload(client, kb_id, "会议纪要.md")

    page = _list_page(client, kb_id, q="合同", limit="1", offset="1")

    assert len(page["items"]) == 1
    assert page["total"] == 3


def test_offset_past_the_end_returns_empty_but_keeps_total(client: TestClient, kb_id: str) -> None:
    """越界页回空列表而不是报错；总数照旧——前端据此把页码夹回最后一页。"""
    _upload(client, kb_id, "只有一篇.md")

    page = _list_page(client, kb_id, limit="10", offset="50")

    assert page["items"] == []
    assert page["total"] == 1


def test_pagination_params_are_bounded(client: TestClient, kb_id: str) -> None:
    """``limit=0`` 会让前端陷入"永远翻不动"，直接 422；上限挡住一次拉全库。"""
    assert (
        client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", params={"limit": "0"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", params={"limit": "201"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", params={"offset": "-1"}
        ).status_code
        == 422
    )


# --------------------------------------------------------------------- 重命名


def _issue(client: TestClient, permission: str) -> dict:
    response = client.post(
        "/api/v1/api-keys",
        json={"name": f"{permission} 钥匙", "permission": permission, "knowledge_base_ids": []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_rename_document(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "旧名字.md")

    response = client.patch(f"/api/v1/documents/{document['id']}", json={"name": "新名字.md"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "新名字.md"
    # 详情与列表都要读到新名字（改名不能只改响应不回库）
    assert client.get(f"/api/v1/documents/{document['id']}").json()["name"] == "新名字.md"
    assert _list_names(client, kb_id) == ["新名字.md"]


def test_rename_trims_and_rejects_blank(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "原.md")

    trimmed = client.patch(
        f"/api/v1/documents/{document['id']}", json={"name": "  带空格的名字.md  "}
    )
    assert trimmed.json()["name"] == "带空格的名字.md"

    # 全空白：schema 的 min_length 拦不住，必须由服务层拒
    blank = client.patch(f"/api/v1/documents/{document['id']}", json={"name": "   "})
    assert blank.status_code == 422


def test_rename_missing_document_is_404(client: TestClient, kb_id: str) -> None:
    response = client.patch("/api/v1/documents/doc_nope", json={"name": "x.md"})
    assert response.status_code == 404


def test_readonly_key_cannot_rename_or_cancel(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "只读的.md")
    issued = _issue(client, "readonly")
    readonly = {"Authorization": f"Bearer {issued['token']}"}

    assert (
        client.patch(
            f"/api/v1/documents/{document['id']}", json={"name": "改不了.md"}, headers=readonly
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/documents/{document['id']}/cancel", headers=readonly).status_code
        == 403
    )


# --------------------------------------------------------------------- 取消解析


def test_cancel_marks_document_and_tasks_canceled(client: TestClient, kb_id: str) -> None:
    # start=true 会排一个摄入任务；没有 worker 时它停在 pending，正好用来验证取消把它收掉
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("取消我.md", io.BytesIO(b"# x\n"), "text/markdown")},
        params={"start": "true"},
    )
    document = response.json()["document"]
    task_id = response.json()["task_id"]
    assert task_id

    canceled = client.post(f"/api/v1/documents/{document['id']}/cancel")

    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["stage"] == "canceled"
    tasks = client.get("/api/v1/tasks").json()["items"]
    task = next(item for item in tasks if item["id"] == task_id)
    assert task["state"] == "canceled"


def test_cancel_is_idempotent(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "再取消一次.md")

    assert client.post(f"/api/v1/documents/{document['id']}/cancel").status_code == 200
    assert client.post(f"/api/v1/documents/{document['id']}/cancel").status_code == 200


def test_cancel_missing_document_is_404(client: TestClient, kb_id: str) -> None:
    assert client.post("/api/v1/documents/doc_nope/cancel").status_code == 404


# --------------------------------------------------------------------- 停用 / 恢复


def test_disable_document_stops_retrieval_but_keeps_everything(
    client: TestClient, kb_id: str
) -> None:
    """停用=只动标记：列表还在、可下载，只是检索不再命中。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={
            "file": (
                "停用我.md",
                io.BytesIO("# 停用测试\n\n独特关键词青稞酒。\n".encode()),
                "text/markdown",
            )
        },
        params={"start": "false"},
    )
    document = response.json()["document"]
    assert document["disabled"] is False

    disabled = client.patch(f"/api/v1/documents/{document['id']}/disabled", json={"disabled": True})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["disabled"] is True

    # 列表仍看得到（停用不是删除），并带停用标记
    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    target = next(item for item in listed if item["id"] == document["id"])
    assert target["disabled"] is True

    # 恢复
    restored = client.patch(
        f"/api/v1/documents/{document['id']}/disabled", json={"disabled": False}
    )
    assert restored.json()["disabled"] is False


def test_batch_enable_and_disable(client: TestClient, kb_id: str) -> None:
    a = _upload(client, kb_id, "批停甲.md")
    b = _upload(client, kb_id, "批停乙.md")

    disabled = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "disable", "document_ids": [a["id"], b["id"]]},
    )
    assert disabled.json()["succeeded"] == 2
    items = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert all(item["disabled"] for item in items if item["id"] in {a["id"], b["id"]})

    enabled = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "enable", "document_ids": [a["id"], b["id"]]},
    )
    assert enabled.json()["succeeded"] == 2
    items = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert not any(item["disabled"] for item in items if item["id"] in {a["id"], b["id"]})


def test_readonly_key_cannot_disable(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "只读停.md")
    issued = _issue(client, "readonly")

    response = client.patch(
        f"/api/v1/documents/{document['id']}/disabled",
        json={"disabled": True},
        headers={"Authorization": f"Bearer {issued['token']}"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------- 批量动作


def _batch(client: TestClient, kb_id: str, action: str, ids: list[str]):
    return client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": action, "document_ids": ids},
    )


def test_batch_delete_removes_all_and_reports_each(client: TestClient, kb_id: str) -> None:
    a = _upload(client, kb_id, "甲.md")
    b = _upload(client, kb_id, "乙.md")
    _upload(client, kb_id, "丙.md")

    response = _batch(client, kb_id, "delete", [a["id"], b["id"]])

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (2, 0)
    assert [item["ok"] for item in body["items"]] == [True, True]
    assert _list_names(client, kb_id) == ["丙.md"]


def test_batch_delete_reports_partial_failure_without_stopping(
    client: TestClient, kb_id: str
) -> None:
    """一篇失败不该带走其余：批量结果逐条给，成功的照旧生效。"""
    good = _upload(client, kb_id, "在的.md")

    response = _batch(client, kb_id, "delete", [good["id"], "doc_nope"])

    body = response.json()
    assert (body["succeeded"], body["failed"]) == (1, 1)
    failed = next(item for item in body["items"] if not item["ok"])
    assert failed["document_id"] == "doc_nope"
    assert _list_names(client, kb_id) == []


def test_batch_rejects_documents_of_another_kb(client: TestClient, kb_id: str) -> None:
    other_kb = client.post("/api/v1/knowledge-bases", json={"name": "别的库"}).json()["id"]
    foreign = _upload(client, other_kb, "别人的.md")

    response = _batch(client, kb_id, "delete", [foreign["id"]])

    assert response.json()["failed"] == 1
    # 别人的文档必须原封不动
    assert _list_names(client, other_kb) == ["别人的.md"]


def test_batch_reprocess_enqueues_tasks(client: TestClient, kb_id: str) -> None:
    a = _upload(client, kb_id, "重跑甲.md")
    b = _upload(client, kb_id, "重跑乙.md")

    response = _batch(client, kb_id, "reprocess", [a["id"], b["id"]])

    assert response.json()["succeeded"] == 2


def test_batch_move_into_folder_and_back_to_root(client: TestClient, kb_id: str) -> None:
    folder = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/folders", json={"name": "批量目录"}
    ).json()
    a = _upload(client, kb_id, "移甲.md")
    b = _upload(client, kb_id, "移乙.md")

    moved = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "move", "document_ids": [a["id"], b["id"]], "folder_id": folder["id"]},
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["succeeded"] == 2
    assert _list_names(client, kb_id, folder_id=folder["id"]) == ["移乙.md", "移甲.md"]

    # folder_id 省略/为 null = 移回根目录
    root = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "move", "document_ids": [a["id"]], "folder_id": None},
    )
    assert root.json()["succeeded"] == 1
    assert _list_names(client, kb_id, root="true") == ["移甲.md"]


def test_batch_move_to_folder_of_another_kb_reports_failure(client: TestClient, kb_id: str) -> None:
    other_kb = client.post("/api/v1/knowledge-bases", json={"name": "另一个库"}).json()["id"]
    foreign = client.post(
        f"/api/v1/knowledge-bases/{other_kb}/folders", json={"name": "别人的目录"}
    ).json()
    document = _upload(client, kb_id, "我的.md")

    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "move", "document_ids": [document["id"]], "folder_id": foreign["id"]},
    )

    body = response.json()
    assert body["failed"] == 1
    assert "不属于" in body["items"][0]["error"]


def test_batch_rejects_bad_action_and_empty_list(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "x.md")

    assert _batch(client, kb_id, "explode", [document["id"]]).status_code == 422
    assert _batch(client, kb_id, "delete", []).status_code == 422


def test_readonly_key_cannot_batch(client: TestClient, kb_id: str) -> None:
    document = _upload(client, kb_id, "只读.md")
    issued = _issue(client, "readonly")

    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "delete", "document_ids": [document["id"]]},
        headers={"Authorization": f"Bearer {issued['token']}"},
    )

    assert response.status_code == 403



# --------------------------------------------------- 整库批量（v17，切分参数改后重跑）


def test_batch_all_skips_documents_still_in_the_pipeline(client: TestClient, kb_id: str) -> None:
    """`all=true` 不重复排队正在跑的文档——那会让同一篇被解析两遍（要花钱）。

    这一条同时也是"全部都在跑"时的出口：给的是"都在处理中"，不是"没有文档"。
    """
    _upload(client, kb_id, "还在跑.md")
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "reprocess", "all": True},
    )

    assert response.status_code == 422
    assert "处理中" in response.json()["message"]


def test_batch_requires_a_target(client: TestClient, kb_id: str) -> None:
    """既不给 ids 也不给 all：当场 422，而不是执行一个空批次看起来像成功。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/batch",
        json={"action": "reprocess"},
    )
    assert response.status_code == 422


def test_batch_all_on_empty_kb_is_rejected(client: TestClient) -> None:
    """空库与"都在跑"要分开说：前者是"还没有文档"，后者是"再等等"。"""
    kb = client.post("/api/v1/knowledge-bases", json={"name": "空库"}).json()
    response = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents/batch",
        json={"action": "reprocess", "all": True},
    )
    assert response.status_code == 422
    assert "还没有文档" in response.json()["message"]


# --------------------------------------------------------------- 生成问题（v24）


def test_list_reports_question_status_fields(client: TestClient, kb_id: str) -> None:
    """列表要能回答"这份出没出题、出了多少"：新字段缺省是 0 / false，不是缺字段。"""
    _upload(client, kb_id, "待出题.md")

    item = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"][0]

    assert item["question_count"] == 0
    assert item["questioned_chunk_count"] == 0
    assert item["questions_pending"] is False


def test_questions_action_rejects_documents_not_indexed_yet(
    client: TestClient, kb_id: str
) -> None:
    """还没索引完就出题，问题会被随后的重切覆盖——逐条记成失败并说清原因。"""
    document = _upload(client, kb_id, "还没索引.md")

    response = _batch(client, kb_id, "questions", [document["id"]])

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (0, 1)
    assert "已索引" in body["items"][0]["error"]


def test_questions_action_is_accepted_by_the_schema(client: TestClient, kb_id: str) -> None:
    """动作名进了 Literal：不会再被 422 当成拼错的动作。"""
    document = _upload(client, kb_id, "动作名.md")

    response = _batch(client, kb_id, "questions", [document["id"]])

    assert response.status_code != 422


def test_detail_and_list_report_the_same_question_status(
    client: TestClient, kb_id: str
) -> None:
    """详情与列表必须给出同一份出题统计。

    v24 的 bug 就在这里：详情路由自己拼 ``_to_out`` 而漏传统计，于是列表显示"4 题"、
    详情一直显示 0 条。两处共用 ``document_out`` 才不会漂。
    """
    from app.core.services import get_services
    from app.storage.base import ChunkRecord

    document = _upload(client, kb_id, "有题的.md")
    # 用**应用自己的**仓储，而不是另建一个：存储迁到 PG 之后，另建的 SQLite 库
    # 只会把数据写到另一个库里，API 那边纹丝不动，断言必然失败
    store = get_services().auth._stores.meta
    store.replace_chunks(
        document["id"],
        [
            ChunkRecord(
                chunk_id=f"{document['id']}#00000",
                document_id=document["id"],
                knowledge_base_id=kb_id,
                part_id=None,
                ordinal=0,
                text="正文一",
                content_hash="h-0",
                heading_path=None,
                page=None,
                questions=("第一个问题？", "第二个问题？"),
            ),
            ChunkRecord(
                chunk_id=f"{document['id']}#00001",
                document_id=document["id"],
                knowledge_base_id=kb_id,
                part_id=None,
                ordinal=1,
                text="正文二",
                content_hash="h-1",
                heading_path=None,
                page=None,
            ),
        ],
    )

    detail = client.get(f"/api/v1/documents/{document['id']}").json()
    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"][0]

    assert detail["question_count"] == 2
    assert detail["questioned_chunk_count"] == 1
    assert (detail["question_count"], detail["questioned_chunk_count"]) == (
        listed["question_count"],
        listed["questioned_chunk_count"],
    )


# ----------------------------------------------------- 处理进度时间线（v24）


def test_timeline_reports_steps_and_current_position(client: TestClient, kb_id: str) -> None:
    """给列表的分段进度条喂数据：共几步、现在第几步、每步多久。

    **不给百分比**：环节耗时不均，百分比只会编出一个对不上的数字。
    """
    document = _upload(client, kb_id, "走一遍.md")

    timeline = client.get(f"/api/v1/documents/{document['id']}/timeline").json()

    assert timeline["step_total"] == 6
    assert timeline["current_index"] == 1  # 刚上传：停在第一个环节
    assert timeline["status"] == "running"
    first = timeline["steps"][0]
    assert (first["key"], first["status"], first["visits"]) == ("uploaded", "running", 1)


def test_timeline_follows_the_whole_pipeline(client: TestClient, kb_id: str) -> None:
    """跑完一遍真实摄入，时间线要把 6 个环节都记上——**这是它的全部数据来源**。

    事件写在存储层唯一那处阶段推进点（``update_document_stage``），所以这里
    真的把摄入跑完（测试里 worker 是关的，直接调 ingest），而不是直接改库——
    直接改库恰好绕过事件写入，等于什么都没验证。
    """
    from app.core.services import get_services

    document = _upload(client, kb_id, "推进.md")
    get_services().ingest.ingest(document["id"])

    timeline = client.get(f"/api/v1/documents/{document['id']}/timeline").json()

    assert timeline["status"] == "done"
    assert timeline["current_index"] == timeline["step_total"] == 6
    # 每一步都被真的进入过（有事件），且没有一个停在 pending
    assert all(step["visits"] >= 1 for step in timeline["steps"])
    assert all(step["status"] != "pending" for step in timeline["steps"])
    # 总耗时 = 各环节耗时之和（口径一致，界面上的数字才对得上）
    assert timeline["total_ms"] == sum(step["duration_ms"] for step in timeline["steps"])


def test_timeline_missing_document_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/documents/doc_nope/timeline").status_code == 404


def test_document_list_carries_the_progress_summary(client: TestClient, kb_id: str) -> None:
    """列表每行都带 progress（§12.115）——**一次请求算完整页**，不是逐篇再问一次。

    逐篇拉时间线意味着 20 篇文档 = 20 次往返，而列表要的只是"第几步"。
    完整那棵树仍然只在 ``/timeline`` 里（抽屉用）。
    """
    from app.core.services import get_services

    first = _upload(client, kb_id, "排队中.md")
    second = _upload(client, kb_id, "跑完.md")
    get_services().ingest.ingest(second["id"])

    items = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    by_id = {item["id"]: item for item in items}

    queued = by_id[first["id"]]["progress"]
    assert queued["status"] == "running"
    assert (queued["step_index"], queued["step_total"]) == (1, 6)
    assert queued["step_label"] == "排队等待"
    assert queued["stalled"] is False  # 刚入队，租约还在
    assert queued["elapsed_ms"] >= 0

    done = by_id[second["id"]]["progress"]
    assert done["status"] == "done"
    assert done["step_index"] == done["step_total"] == 6
    assert done["step_label"] == "完成索引"


def test_document_detail_carries_progress_too(client: TestClient, kb_id: str) -> None:
    """详情与列表同一口径：两处各拼一遍必然漂（踩过一次：详情少了出题统计）。

    **不比较 ``elapsed_ms``**：它是"到现在为止"，两次请求之间本来就会涨几毫秒。
    比的是那些该稳定的字段——它们才代表口径。
    """
    document = _upload(client, kb_id, "详情.md")

    detail = client.get(f"/api/v1/documents/{document['id']}").json()["progress"]
    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"][0][
        "progress"
    ]

    stable = ("status", "step_index", "step_total", "step_label", "retries", "stalled")
    assert {key: detail[key] for key in stable} == {key: listed[key] for key in stable}
    assert detail["elapsed_ms"] >= 0
