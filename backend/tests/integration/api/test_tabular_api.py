"""表格双写的 HTTP 行为（M2 / T2.11）。

镜像同构：``app/api/v1/tabular.py`` + 摄入时的双写 → 本文件。

服务层与解析器的性质已在 ``tests/unit/services/test_tabular.py`` 覆盖；
这里验的是**接进摄入链路与接口之后仍然成立**，以及那条最要紧的端到端性质：
CSV 进库之后，**检索能命中"某人的年龄"**——那是纯文本直通做不到的事。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from tests.conftest import admin_client as admin_session

CSV = "姓名,年龄,城市\n张三,30,北京\n李四,25,上海\n王五,41,广州\n"


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
def csv_document(client: TestClient) -> dict:
    """上传一份 CSV 并**驱动摄入跑完**——结构化副本在解析阶段写入。"""
    kb = client.post("/api/v1/knowledge-bases", json={"name": "表格库"}).json()
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("成员名册.csv", io.BytesIO(CSV.encode()), "text/csv")}
    )
    assert upload.status_code == 202, upload.text
    document_id = upload.json()["document"]["id"]
    _drain_worker()
    return {"kb_id": kb["id"], "document_id": document_id}


# --------------------------------------------------------------------- 摄入


def test_csv_is_indexed(client: TestClient, csv_document: dict) -> None:
    document = client.get(f"/api/v1/documents/{csv_document['document_id']}").json()

    assert document["stage"] == "indexed"
    assert document["chunk_count"] > 0


def test_chunks_carry_column_names(client: TestClient, csv_document: dict) -> None:
    """**这是 T2.11 的核心。**

    纯文本直通给出的切块是 ``张三,30,北京``，列名只在第一行出现一次；
    检索"年龄"命中不到那一行的 ``30``。带列名之后，每一行都自解释。
    """
    chunks = client.get(
        f"/api/v1/documents/{csv_document['document_id']}/chunks?limit=50"
    ).json()["items"]
    text = "\n".join(chunk["text"] for chunk in chunks)

    assert "姓名=张三" in text
    assert "年龄=30" in text
    # 后面的行也要带列名，而不只是第一行
    assert "姓名=王五" in text


def test_retrieval_hits_a_cell_by_its_column_name(
    client: TestClient, csv_document: dict
) -> None:
    """端到端性质：**问"张三的年龄"要能命中那一行。**

    这条用例是 T2.11 存在的全部理由——纯文本直通下它会失败。
    """
    hits = client.post(
        "/api/v1/search",
        json={
            "query": "张三的年龄",
            "kb_ids": [csv_document["kb_id"]],
            "top_k": 3,
        }
    ).json()["hits"]

    assert hits, "带列名的行文本应当能被检索到"
    assert any("张三" in hit["text"] for hit in hits)


# --------------------------------------------------------------------- 读副本


def test_reads_the_structured_copy(client: TestClient, csv_document: dict) -> None:
    body = client.get(f"/api/v1/documents/{csv_document['document_id']}/table").json()

    assert body["columns"] == ["姓名", "年龄", "城市"]
    assert body["rows"] == [["张三", "30", "北京"], ["李四", "25", "上海"], ["王五", "41", "广州"]]
    assert body["total"] == 3


def test_values_are_strings_not_inferred(client: TestClient, csv_document: dict) -> None:
    """**不做类型推断**：``30`` 是字符串。推断会把 ``007`` 变成 ``7``、
    把长数字转成科学计数法——都是数据损失，且用户很难发现。
    """
    body = client.get(f"/api/v1/documents/{csv_document['document_id']}/table").json()

    assert all(isinstance(cell, str) for row in body["rows"] for cell in row)
    assert body["rows"][0][1] == "30"


def test_pagination(client: TestClient, csv_document: dict) -> None:
    body = client.get(
        f"/api/v1/documents/{csv_document['document_id']}/table",
        params={"limit": 1, "offset": 1}
    ).json()

    assert body["rows"] == [["李四", "25", "上海"]]
    assert body["total"] == 3, "total 是总行数，不受 limit 影响"


def test_non_tabular_document_explains_itself(client: TestClient) -> None:
    """非表格文档要给出**可理解的理由**，而不是空结果——
    空结果会让用户以为数据丢了。
    """
    kb = client.post("/api/v1/knowledge-bases", json={"name": "非表格库"}).json()
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("说明.md", io.BytesIO("# 标题\n\n正文。\n".encode()), "text/markdown")}
    )
    document_id = upload.json()["document"]["id"]
    _drain_worker()

    response = client.get(f"/api/v1/documents/{document_id}/table")

    assert response.status_code == 422
    assert "CSV" in response.json()["message"]


def test_unknown_document_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/documents/doc_不存在/table").status_code == 404


def test_limit_is_capped(client: TestClient, csv_document: dict) -> None:
    """上限由参数校验挡住，而不是让一次请求把整张表拉回来。"""
    response = client.get(
        f"/api/v1/documents/{csv_document['document_id']}/table", params={"limit": 99999}
    )

    assert response.status_code == 422


# --------------------------------------------------------------------- 删除


def test_deleting_the_document_drops_the_copy(client: TestClient, csv_document: dict) -> None:
    """**删文档要连带删结构化副本。**

    DuckDB 里的表不属于元数据级联的范围；留着会攒下一堆再也访问不到的表，
    而它们还占着磁盘——与"删除必须清三处"是同一类问题。
    """
    client.delete(f"/api/v1/documents/{csv_document['document_id']}")

    response = client.get(f"/api/v1/documents/{csv_document['document_id']}/table")
    # 文档没了 → 404（而不是还能读到一张孤儿表）
    assert response.status_code == 404

    # 直接问存储层：孤儿表确实被删了，而不只是接口查不到。
    # 用 conftest 之外的途径拿 store：测试进程与 TestClient 共享同一个
    # 开发库以外的 data_dir 配置，所以重建一次 stores 就是同一份。
    from app.core.config import get_settings
    from app.core.storage import build_stores

    stores = build_stores(get_settings())
    assert stores.tabular.table_exists(csv_document["document_id"]) is False
