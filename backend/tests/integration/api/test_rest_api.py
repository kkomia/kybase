"""REST API 的集成测试（M4 核心端点）。

通过真实 HTTP 打整条链路：建库 → 上传 → 手动驱动 worker 摄入 → 检索 → 查任务。
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from app.models.enums import DocumentStage, TaskState
from tests.conftest import admin_client as admin_session

MARKDOWN = (
    "# 知识库设计\n\n"
    "向量检索与全文检索混合召回，用于验证 REST 链路。\n\n"
    "## 部署\n\n"
    "Docker Compose 一键起，默认端口 8000。\n"
)


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:

        yield test_client



@pytest.fixture
def kb_id(client: TestClient) -> str:
    response = client.post("/api/v1/knowledge-bases", json={"name": "技术库"})
    assert response.status_code == 201
    return response.json()["id"]


def _upload(client: TestClient, kb_id: str, *, name: str = "kb.md", content: str = MARKDOWN):
    return client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (name, io.BytesIO(content.encode()), "text/markdown")}
    )


def _drain_worker() -> None:
    """手动把队列跑空：测试里关掉了内嵌消费者，时序才可控。"""
    import asyncio

    worker = get_services().worker

    async def drain() -> None:
        while await worker.run_once():
            pass

    asyncio.run(drain())


# --------------------------------------------------------------------- 知识库


def test_create_and_list_knowledge_bases(client: TestClient) -> None:
    created = client.post("/api/v1/knowledge-bases", json={"name": "研究库", "chunk_size": 256})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "研究库"
    assert body["chunk_size"] == 256
    assert body["embedding_dim"] > 0

    listed = client.get("/api/v1/knowledge-bases")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [body["id"]]


def test_get_knowledge_base_detail(client: TestClient, kb_id: str) -> None:
    response = client.get(f"/api/v1/knowledge-bases/{kb_id}")
    assert response.status_code == 200
    assert response.json()["id"] == kb_id


def test_unknown_knowledge_base_returns_error_envelope(client: TestClient) -> None:
    """错误信封格式由前端消费，状态码与 code 都要稳定。"""
    response = client.get("/api/v1/knowledge-bases/kb_none")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_create_knowledge_base_validates_input(client: TestClient) -> None:
    assert client.post("/api/v1/knowledge-bases", json={"name": ""}).status_code == 422
    assert client.post(
        "/api/v1/knowledge-bases", json={"name": "x", "chunk_overlap": 99999}
    ).status_code == 422


def test_rename_knowledge_base(client: TestClient, kb_id: str) -> None:
    response = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"name": "改过的名字"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "改过的名字"
    # 详情与列表都要读到新名字（改名不能只改响应不回库）
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}").json()["name"] == "改过的名字"
    assert client.get("/api/v1/knowledge-bases").json()["items"][0]["name"] == "改过的名字"


def test_rename_knowledge_base_rejects_blank(client: TestClient, kb_id: str) -> None:
    """空字符串由 schema 拦（min_length）；全空白由服务层拦。"""
    assert (
        client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"name": ""}).status_code == 422
    )
    assert (
        client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"name": "   "}).status_code == 422
    )


def test_rename_unknown_knowledge_base_is_404(client: TestClient) -> None:
    assert client.patch("/api/v1/knowledge-bases/kb_none", json={"name": "x"}).status_code == 404


def test_kb_list_carries_document_count_and_last_activity(client: TestClient) -> None:
    """计数随列表一次返回：前端不必再逐库拉文档列表只为数数（N 次请求 → 1 次）。"""
    kb_id = client.post("/api/v1/knowledge-bases", json={"name": "计数库"}).json()["id"]

    empty = client.get("/api/v1/knowledge-bases").json()["items"][0]
    assert empty["document_count"] == 0
    assert empty["last_activity"] is None

    client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(b"# a\n"), "text/markdown")},
        params={"start": "false"},
    )

    filled = client.get("/api/v1/knowledge-bases").json()["items"][0]
    assert filled["document_count"] == 1
    assert filled["last_activity"] is not None
    # 详情接口给同一份数字，两个入口不能各说各话
    detail = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
    assert detail["document_count"] == 1


# --------------------------------------------------------------------- 上传


def test_upload_returns_accepted_with_task(client: TestClient, kb_id: str) -> None:
    response = _upload(client, kb_id)
    assert response.status_code == 202

    body = response.json()
    assert body["is_duplicate"] is False
    assert body["task_id"]
    assert body["document"]["stage"] == "uploaded"


def test_update_knowledge_base_description(client: TestClient, kb_id: str) -> None:
    """简介是库属性：可写、可清空，且与改名互不影响（两者都可选，只改传的那个）。"""
    response = client.patch(
        f"/api/v1/knowledge-bases/{kb_id}", json={"description": "  产品手册与常见问题  "}
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "产品手册与常见问题"
    # 只传 name 时简介保持不变
    renamed = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"name": "改名后"})
    assert renamed.json()["name"] == "改名后"
    assert renamed.json()["description"] == "产品手册与常见问题"
    # 空串是合法值 = 清空
    cleared = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"description": ""})
    assert cleared.json()["description"] == ""


def test_description_over_limit_is_422(client: TestClient, kb_id: str) -> None:
    response = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"description": "x" * 201})
    assert response.status_code == 422


def test_task_list_carries_knowledge_base_id(client: TestClient, kb_id: str) -> None:
    """任务带所属库 id：任务中心的"按知识库筛选"靠它，不必逐库拉文档反查。"""
    _upload(client, kb_id)

    task = client.get("/api/v1/tasks").json()["items"][0]
    assert task["knowledge_base_id"] == kb_id


def test_upload_can_skip_ingest(client: TestClient, kb_id: str) -> None:
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents?start=false",
        files={"file": ("kb.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")}
    )
    assert response.status_code == 202
    assert response.json()["task_id"] is None


def test_duplicate_upload_is_flagged(client: TestClient, kb_id: str) -> None:
    """架构 §6.3：命中相同 hash 要提醒"检测到相同文件"，且不重复入库。"""
    first = _upload(client, kb_id).json()
    second = _upload(client, kb_id, name="copy.md").json()

    assert second["is_duplicate"] is True
    assert second["document"]["id"] == first["document"]["id"]
    assert second["task_id"] is None


def test_upload_to_unknown_kb_fails(client: TestClient) -> None:
    response = _upload(client, "kb_none")
    assert response.status_code == 404


def test_upload_rejects_oversized_file(client: TestClient, kb_id: str, monkeypatch) -> None:
    """上限在入口挡住，不要等跑到云端解析才失败。

    P1（2026-09-29 能力实测第 10 条）复核过一遍：**闸本来就在服务端**
    （`app/api/v1/documents.py` 的 `MAX_UPLOAD_BYTES` = 200 MB，与前端
    `knowledge/uploadLimits.ts` 同一个数字）。那条"30 MB 被 202 收下"不是缺闸，
    而是 30 MB **本来就小于 200 MB**——实测那次是 `31,457,280` 字节。
    这条用例按同一常量验判定与**人话**（把上限压到 8 字节来跑，不必造 200 MB）。
    """
    import app.api.v1.documents as documents_api

    monkeypatch.setattr(documents_api, "MAX_UPLOAD_BYTES", 8)
    response = _upload(client, kb_id)

    assert response.status_code == 413
    body = response.json()
    assert body["code"] == "payload_too_large"
    assert "上限" in body["message"], "4xx 要说人话：告诉用户是大小限制"


def test_upload_limit_is_the_documented_one() -> None:
    """上限就是那个**既有常量**：两条上传路（文档 / 会话文件）与前端三处同值。

    钉住它是为了防"改一处漏一处"：D21 的结论是"超限要等传到服务端才报错"，
    前端预校验补上之后，两边的数字必须是同一个，否则会出现"前端放行、后端 413"
    这种最难解释的组合。
    """
    import app.api.v1.conversations as conversations_api
    import app.api.v1.documents as documents_api

    assert documents_api.MAX_UPLOAD_BYTES == 200 * 1024 * 1024
    assert conversations_api.MAX_UPLOAD_BYTES == documents_api.MAX_UPLOAD_BYTES


def test_upload_keeps_chinese_filename_readable(client: TestClient, kb_id: str) -> None:
    """中文文件名必须原样存下来。

    浏览器把 UTF-8 文件名的**原始字节**塞进只允许 latin-1 的 Content-Disposition 头，
    服务端若照 latin-1 解就会得到乱码。这里手工构造那种报文体，
    确保文件名在"入库"这一步已经被还原成可读中文。
    """
    boundary = "----kylabtest"
    filename_bytes = "架构设计.md".encode()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="',
            filename_bytes,
            b'"\r\n',
            b"Content-Type: text/markdown\r\n\r\n",
            MARKDOWN.encode(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )

    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )

    assert response.status_code == 202
    assert response.json()["document"]["name"] == "架构设计.md"
    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert [item["name"] for item in listed] == ["架构设计.md"]


# --------------------------------------------------------------------- 列表与详情


def test_document_list_shows_stage(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id)

    items = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "kb.md"
    assert items[0]["stage"] == DocumentStage.UPLOADED.value
    assert items[0]["size_bytes"] > 0


def test_document_detail_and_missing(client: TestClient, kb_id: str) -> None:
    document_id = _upload(client, kb_id).json()["document"]["id"]

    assert client.get(f"/api/v1/documents/{document_id}").status_code == 200
    assert client.get("/api/v1/documents/doc_none").status_code == 404


def test_parts_endpoint_returns_empty_for_unsplit_document(client: TestClient,
                                                           kb_id: str) -> None:
    document_id = _upload(client, kb_id).json()["document"]["id"]
    response = client.get(f"/api/v1/documents/{document_id}/parts")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_chunks_endpoint_returns_ingested_text(client: TestClient, kb_id: str) -> None:
    """文档详情页的正文预览：接口得给出真实切块，而不是让前端编内容。"""
    document_id = _upload(client, kb_id).json()["document"]["id"]
    _drain_worker()

    body = client.get(f"/api/v1/documents/{document_id}/chunks").json()
    assert body["total"] == len(body["items"]) > 0
    assert body["items"][0]["ordinal"] == 0
    # 标题会进 heading_path，正文块里是段落文本
    assert "向量检索" in body["items"][0]["text"]
    assert body["items"][0]["document_id"] == document_id


def test_chunks_endpoint_respects_limit_and_reports_total(client: TestClient,
                                                          kb_id: str) -> None:
    """limit 只截断 items，total 必须仍是全量：否则界面会把预览说成全文。"""
    # 每段都超过半块（512），保证切分器不会把它们并进同一个块
    paragraph = "这一段用来撑出独立的切块。" * 30
    long_markdown = "\n\n".join(f"## 第 {i} 节\n\n{paragraph}" for i in range(12))
    upload = _upload(client, kb_id, name="long.md", content=long_markdown)
    document_id = upload.json()["document"]["id"]
    _drain_worker()

    full = client.get(f"/api/v1/documents/{document_id}/chunks").json()
    limited = client.get(f"/api/v1/documents/{document_id}/chunks?limit=2").json()

    assert full["total"] > 2
    assert len(limited["items"]) == 2
    assert limited["total"] == full["total"]


def test_chunks_endpoint_rejects_bad_limit_and_unknown_document(client: TestClient,
                                                                kb_id: str) -> None:
    document_id = _upload(client, kb_id).json()["document"]["id"]

    assert client.get(f"/api/v1/documents/{document_id}/chunks?limit=0").status_code == 422
    # 文档不存在要给 404，而不是空列表：空列表会让调用方以为"只是还没切块"
    assert client.get("/api/v1/documents/doc_none/chunks").status_code == 404


def test_chunks_endpoint_is_empty_before_ingest(client: TestClient, kb_id: str) -> None:
    document_id = _upload(client, kb_id, name="pending.md").json()["document"]["id"]

    body = client.get(f"/api/v1/documents/{document_id}/chunks").json()
    assert body == {"items": [], "total": 0}


def test_reprocess_is_accepted(client: TestClient, kb_id: str) -> None:
    document_id = _upload(client, kb_id).json()["document"]["id"]
    response = client.post(f"/api/v1/documents/{document_id}/reprocess")
    assert response.status_code == 202
    assert response.json()["task_id"]


# --------------------------------------------------------------------- 任务


def test_task_list_exposes_retry_metadata(client: TestClient, kb_id: str) -> None:
    """任务中心要靠这些字段显示"重试了几次、为什么卡住"。"""
    _upload(client, kb_id)

    items = client.get("/api/v1/tasks").json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "parse"
    assert items[0]["state"] == TaskState.PENDING.value
    assert items[0]["max_attempts"] >= 1


def test_task_list_filters_by_state(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id)
    assert client.get("/api/v1/tasks?state=running").json()["items"] == []
    assert len(client.get("/api/v1/tasks?state=pending").json()["items"]) == 1


def test_invalid_task_state_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/tasks?state=bogus").status_code == 422


# --------------------------------------------------------------------- 检索（含端到端）


def test_search_before_ingest_returns_empty(client: TestClient, kb_id: str) -> None:
    response = client.post(
        "/api/v1/search", json={"query": "向量检索", "kb_ids": [kb_id]}
    )
    assert response.status_code == 200
    assert response.json()["hits"] == []


def test_search_validates_input(client: TestClient) -> None:
    assert client.post("/api/v1/search", json={"query": "", "kb_ids": ["kb_1"]}).status_code == 422
    assert client.post("/api/v1/search", json={"query": "x", "kb_ids": []}).status_code == 422


def test_full_upload_to_search_flow(client: TestClient, kb_id: str) -> None:
    """端到端：上传 → worker 摄入 → 检索命中。这是产品主链路的最小验收。"""
    _upload(client, kb_id)
    _drain_worker()

    document = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"][0]
    assert document["stage"] == DocumentStage.INDEXED.value
    assert document["chunk_count"] > 0

    response = client.post(
        "/api/v1/search",
        json={"query": "混合召回", "kb_ids": [kb_id], "top_k": 5}
    )
    body = response.json()
    assert body["hits"], "摄入完成后应当能检索到内容"
    hit = body["hits"][0]
    assert hit["document_id"] == document["id"]
    assert hit["text"]
    assert hit["channels"]
    assert body["stats"]


def test_search_reports_development_embedding(client: TestClient, kb_id: str) -> None:
    """没配 embedding key 时必须如实告知：界面要提示"检索质量不代表真实效果"。"""
    _upload(client, kb_id)
    _drain_worker()

    body = client.post("/api/v1/search", json={"query": "部署", "kb_ids": [kb_id]}).json()
    assert body["embedding_is_development"] is True


def test_search_modes_and_filters(client: TestClient, kb_id: str) -> None:
    _upload(client, kb_id)
    _drain_worker()

    only_text = client.post(
        "/api/v1/search",
        json={"query": "部署", "kb_ids": [kb_id], "mode": "fulltext"}
    ).json()
    assert [stat["channel"] for stat in only_text["stats"]] == ["fulltext"]

    filtered = client.post(
        "/api/v1/search",
        json={
            "query": "部署",
            "kb_ids": [kb_id],
            "filters": {"document_ids": ["doc_not_exist"]},
        }
    ).json()
    assert filtered["hits"] == []
    assert filtered["filtered_out"] > 0


def test_create_kb_without_an_embedding_model_is_rejected(monkeypatch) -> None:
    """没配嵌入模型时建库要被**明确拒绝**，而不是退回无语义的哈希实现。

    v0.8 之前这里会静默兜底，界面上还标"开发兜底"——用户会以为检索是有效的。
    现在返回 422 + 一句"去哪儿配"，前端据此在弹窗里就能给出下一步。
    """
    from app.core.config import get_settings

    # 关掉测试默认打开的开发兜底：这一条要验的正是"没有兜底会怎样"
    monkeypatch.setenv("KYLAB_DEV_EMBEDDING", "false")
    get_settings.cache_clear()

    with admin_session() as isolated:
        response = isolated.post("/api/v1/knowledge-bases", json={"name": "无模型库"})
    get_settings.cache_clear()

    assert response.status_code == 422
    assert "模型注册" in response.json()["message"]


# **搬走一条**（2026-10-05）：``test_settings_reports_whether_embedding_is_configured``
# （原判据：`GET /settings` 要能区分"没配"与"配了"，前端据此决定建库入口能不能点）——
# 它打的是 ``/settings``，而那一族随 NAS 网页端退役**只在本机档**了（运行期配置表
# 在本机 SQLite 里）。所以它搬去 `test_settings_api.py`（本机档那一份，同一条判据
# 一个字没改）。


# ------------------------------------------------- 切分参数可调（v17）


def test_patch_knowledge_base_chunking(client: TestClient, kb_id: str) -> None:
    """改切分参数要落库，且只传一个字段时另一个保持不变。"""
    response = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"chunk_size": 256})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chunk_size"] == 256
    assert body["chunk_overlap"] == 64  # 没传的字段不动
    # 详情里也要是新值（不能只改响应）
    detail = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
    assert (detail["chunk_size"], detail["chunk_overlap"]) == (256, 64)


def test_patch_chunking_rejects_overlap_not_smaller_than_size(
    client: TestClient, kb_id: str
) -> None:
    """重叠必须小于块长的一半——这是**跨字段**约束，只能由服务层判。

    只传 chunk_size（把块长调小）时，库里已有的重叠可能就超标了，同样要拦住。
    """
    first = client.patch(
        f"/api/v1/knowledge-bases/{kb_id}", json={"chunk_size": 512, "chunk_overlap": 200}
    )
    assert first.status_code == 200, first.text
    # 块长调到 256 之后，200 的重叠超过一半 → 拒绝，且带上可读的下一步
    rejected = client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"chunk_size": 256})
    assert rejected.status_code == 422, rejected.text
    assert "一半" in rejected.json()["message"]
    # 拒绝之后原值不变
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}").json()["chunk_size"] == 512


def test_patch_chunking_validates_single_field_range(client: TestClient, kb_id: str) -> None:
    """单个字段越界由 schema 拦（范围与 services/chunking.py 的常量同源）。"""
    assert (
        client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"chunk_size": 10}).status_code
        == 422
    )
    assert (
        client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"chunk_size": 99999}).status_code
        == 422
    )
    assert (
        client.patch(f"/api/v1/knowledge-bases/{kb_id}", json={"chunk_overlap": 99999}).status_code
        == 422
    )


def test_create_knowledge_base_accepts_custom_chunking(client: TestClient) -> None:
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "自定义切分", "chunk_size": 256, "chunk_overlap": 32},
    )
    assert created.status_code == 201, created.text
    assert (created.json()["chunk_size"], created.json()["chunk_overlap"]) == (256, 32)


def test_search_is_recorded_in_usage(client: TestClient, kb_id: str) -> None:
    """检索次数要落库（v17）。

    原先它只在日志里，于是仪表盘上"检索量"这个最该有的数没有——只有一张
    "模型用量"的表。现在 `/stats/usage` 的 by_kind 里会出现 search。
    """
    client.post("/api/v1/search", json={"query": "眼轴长度", "kb_ids": [kb_id]})

    usage = client.get("/api/v1/stats/usage").json()

    kinds = {item["kind"]: item for item in usage["by_kind"]}
    assert "search" in kinds
    assert kinds["search"]["label"] == "检索"
    assert kinds["search"]["calls"] >= 1


def test_empty_search_is_not_counted(client: TestClient, kb_id: str) -> None:
    """空库上的一次检索仍是一次"调用"（它真的跑了一趟），但空查询不算。"""
    client.post("/api/v1/search", json={"query": "   ", "kb_ids": [kb_id]})

    usage = client.get("/api/v1/stats/usage").json()

    assert {item["kind"] for item in usage["by_kind"]} == set()



# ------------------------------------------------- 推荐问题设置（v19）


def test_create_and_patch_suggested_settings(client: TestClient) -> None:
    """建库时能定推荐问题，之后 PATCH 也能改，且详情里读得到。"""
    created = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "出题库", "suggested_count": 3, "suggested_prompt": "按诊断标准出题"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # 建库没传 suggested_enabled → 默认**关**（生成要花钱，必须显式开）
    assert (body["suggested_enabled"], body["suggested_count"]) == (False, 3)
    assert body["suggested_prompt"] == "按诊断标准出题"
    kb_id = body["id"]

    patched = client.patch(
        f"/api/v1/knowledge-bases/{kb_id}",
        json={"suggested_enabled": True, "suggested_count": 5},
    )
    assert patched.status_code == 200, patched.text
    updated = patched.json()
    assert (updated["suggested_enabled"], updated["suggested_count"]) == (True, 5)
    # 没传的字段不动
    assert updated["suggested_prompt"] == "按诊断标准出题"


def test_patch_suggested_defaults_are_returned_on_a_plain_kb(
    client: TestClient, kb_id: str
) -> None:
    """建库时没填时的默认值：**生成开关关**、每段 3 条、跟随对话模型、内置提示词。"""
    body = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()

    assert body["suggested_enabled"] is False
    assert body["suggested_count"] == 3
    assert body["suggested_model_pk"] is None
    assert body["suggested_prompt"] == ""


def test_patch_suggested_validates_range(client: TestClient, kb_id: str) -> None:
    assert (
        client.patch(
            f"/api/v1/knowledge-bases/{kb_id}", json={"suggested_count": 99}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/knowledge-bases/{kb_id}", json={"suggested_count": 0}
        ).status_code
        == 422
    )


def test_patch_suggested_model_rejects_a_bogus_pk(client: TestClient, kb_id: str) -> None:
    """出题也是真发一次模型调用：存一个不能对话的 pk，会让这个库永远出不了题。"""
    response = client.patch(
        f"/api/v1/knowledge-bases/{kb_id}", json={"suggested_model_pk": "mdl_不存在"}
    )

    assert response.status_code in (400, 404, 422), response.text


def test_patch_suggested_model_empty_string_clears_it(client: TestClient, kb_id: str) -> None:
    """空串 = 清除（回到跟随对话模型），与简介空串表示清空同一套约定。"""
    response = client.patch(
        f"/api/v1/knowledge-bases/{kb_id}", json={"suggested_model_pk": ""}
    )

    assert response.status_code == 200, response.text
    assert response.json()["suggested_model_pk"] is None
