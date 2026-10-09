"""切块干预的 HTTP 行为（G3）。

镜像同构：``app/api/v1/chunks.py`` → 本文件。

服务层的三条性质（三处清理、重新向量化、检索侧过滤）已在
``tests/unit/services/test_chunk.py`` 覆盖；这里验的是**接进接口之后**仍然成立，
以及权限、范围判定与 URL 寻址没有漏。
"""

from __future__ import annotations

import io
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.core.services import get_services
from tests.conftest import admin_client as admin_session

# 刻意写成**多段且足够长**：切分器按固定长度切（默认 512 字），
# 一份两百来字的文档只会得到一个块，而"删块后重排序号"至少两块才测得到
# （踩过两次：先是单段、后是段数够但总长不够，用例都退化成了摆设）
_PARAGRAPHS = [
    "眼轴长度是衡量儿童青少年眼球发育情况的主要参数之一，不受调节能力影响，"
    "变化范围随年龄增长呈现较为稳定的规律性。",
    "应在散瞳后进行测量，取三次读数取平均值，并记录测量设备型号与软件版本，"
    "以保证不同时间点之间的数据可比。",
    "三至十八岁儿童青少年的眼轴长度随年龄增长呈现规律性变化，"
    "可据此建立各年龄段的参考区间用于筛查判定。",
    "国家卫健委鼓励有条件地区增加眼轴长度与角膜曲率等指标的检测，"
    "以提升近视防控工作的有效性。",
]
MARKDOWN = "# 眼轴共识\n\n" + "\n\n".join(_PARAGRAPHS * 3) + "\n"


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:

        yield test_client



def _drain_worker() -> None:
    """手动把队列跑空（测试里 worker 不常驻，时序才可控）。"""
    import asyncio

    worker = get_services().worker

    async def drain() -> None:
        while await worker.run_once():
            pass

    asyncio.run(drain())


@pytest.fixture
def document_id(client: TestClient) -> str:
    """上传一份文档并**驱动摄入跑完**，这样才有可干预的块。

    刻意不传 ``start=false``：那样只登记、不入队，drain 无从跑起（踩过）。
    """
    kb = client.post("/api/v1/knowledge-bases", json={"name": "干预库"})
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb.json()['id']}/documents",
        files={"file": ("眼轴.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")}
    )
    assert upload.status_code == 202, upload.text
    document_id = upload.json()["document"]["id"]
    _drain_worker()
    return document_id


def _chunks(client: TestClient, document_id: str) -> list[dict]:
    return client.get(f"/api/v1/documents/{document_id}/chunks?limit=50").json()["items"]


def _by_ordinal(document_id: str, ordinal: int) -> str:
    """界面实际用的寻址方式：文档 ID + 序号，都是 URL 安全的。"""
    return f"/api/v1/documents/{document_id}/chunks/by-ordinal/{ordinal}"


# --------------------------------------------------------------------- 读


def test_listed_chunks_carry_the_disabled_flag(client: TestClient, document_id: str) -> None:
    """界面要靠这个标记显示"已禁用"，所以它必须在列表响应里。"""
    items = _chunks(client, document_id)
    assert items, "摄入跑完后应当有块"
    assert items[0]["disabled"] is False


def test_get_chunk_by_ordinal(client: TestClient, document_id: str) -> None:
    body = client.get(_by_ordinal(document_id, 0)).json()
    assert body["ordinal"] == 0
    assert body["document_id"] == document_id
    assert body["text"]


def test_get_by_ordinal_out_of_range_is_404(client: TestClient, document_id: str) -> None:
    assert client.get(_by_ordinal(document_id, 9999)).status_code == 404


def test_get_unknown_chunk_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/chunks/chunk_不存在").status_code == 404


def test_chunk_id_contains_hash_and_needs_encoding(client: TestClient, document_id: str) -> None:
    """**chunk_id 里含 ``#``，直接拼进路径会被当成 URL 片段而截断。**

    截断之后的 ``/api/v1/chunks/doc_xxx`` 恰好会落到
    ``/documents/{document_id}`` 这条路由上，于是报的是"文档不存在"——
    一个和真正原因（``#`` 没编码）完全无关的报错，很难查。

    这条用例把坑钉下来：**不编码必须失败，编码后必须成功**。
    界面因此走 ``by-ordinal`` 那条路（文档 ID 与序号都 URL 安全）；
    调用方只有拿不到序号时，才需要自己百分号编码。
    """
    chunk_id = _chunks(client, document_id)[0]["chunk_id"]
    assert "#" in chunk_id, "切分器给的 chunk_id 含 #，这正是坑的来源"

    # 不编码：被截断，落到别的路由上
    assert client.get(f"/api/v1/chunks/{chunk_id}").status_code != 200
    # 编码后：正常
    assert client.get(f"/api/v1/chunks/{quote(chunk_id, safe='')}").status_code == 200


# --------------------------------------------------------------------- 改


def test_update_text_roundtrip(client: TestClient, document_id: str) -> None:
    response = client.patch(_by_ordinal(document_id, 0), json={"text": "改写后的正文。"})

    assert response.status_code == 200, response.text
    assert response.json()["text"] == "改写后的正文。"
    assert response.json()["ordinal"] == 0, "序号不该变"
    assert _chunks(client, document_id)[0]["text"] == "改写后的正文。"


def test_update_rejects_empty_text(client: TestClient, document_id: str) -> None:
    """空串被 pydantic 的 min_length 挡在业务之前。"""
    assert client.patch(_by_ordinal(document_id, 0), json={"text": ""}).status_code == 422


def test_update_rejects_whitespace_only(client: TestClient, document_id: str) -> None:
    """全空白能过 min_length，必须由服务层挡下——否则会写出一个空块。"""
    response = client.patch(_by_ordinal(document_id, 0), json={"text": "   "})
    assert response.status_code == 422
    assert "不能为空" in response.json()["message"]


# --------------------------------------------------------------------- 禁用


def test_disable_and_restore(client: TestClient, document_id: str) -> None:
    disabled = client.put(f"{_by_ordinal(document_id, 0)}/disabled", json={"disabled": True})
    assert disabled.status_code == 200
    assert disabled.json()["disabled"] is True

    restored = client.put(f"{_by_ordinal(document_id, 0)}/disabled", json={"disabled": False})
    assert restored.json()["disabled"] is False


def test_disabled_chunk_drops_out_of_search(client: TestClient, document_id: str) -> None:
    """**端到端确认禁用真的能影响检索**——这是禁用的全部意义。"""
    chunk_id = _chunks(client, document_id)[0]["chunk_id"]
    kb_id = client.get(f"/api/v1/documents/{document_id}").json()["knowledge_base_id"]

    before = client.post(
        "/api/v1/search", json={"query": "眼轴长度", "kb_ids": [kb_id], "top_k": 10}
    ).json()
    assert any(hit["chunk_id"] == chunk_id for hit in before["hits"]), "禁用前应当能搜到"

    client.put(f"{_by_ordinal(document_id, 0)}/disabled", json={"disabled": True})

    after = client.post(
        "/api/v1/search", json={"query": "眼轴长度", "kb_ids": [kb_id], "top_k": 10}
    ).json()
    assert not any(hit["chunk_id"] == chunk_id for hit in after["hits"]), "禁用后不该再被搜到"


def test_disable_does_not_remove_the_chunk(client: TestClient, document_id: str) -> None:
    """禁用是"藏起来"，块本身还在库里。"""
    client.put(f"{_by_ordinal(document_id, 0)}/disabled", json={"disabled": True})

    assert client.get(_by_ordinal(document_id, 0)).status_code == 200


# --------------------------------------------------------------------- 删


def test_delete_removes_the_chunk_and_renumbers(client: TestClient, document_id: str) -> None:
    items = _chunks(client, document_id)
    assert len(items) > 1, "这条用例需要至少两块"

    assert client.delete(_by_ordinal(document_id, 1)).status_code == 204

    after = _chunks(client, document_id)
    assert len(after) == len(items) - 1
    # ordinal 重排成连续的 0..n-1，否则界面会出现"第 1、2、4 块"
    assert [item["ordinal"] for item in after] == list(range(len(after)))


def test_delete_drops_it_out_of_search(client: TestClient, document_id: str) -> None:
    """删除必须连索引与向量一起清，否则会"召回得到、正文查不到"。"""
    kb_id = client.get(f"/api/v1/documents/{document_id}").json()["knowledge_base_id"]

    client.delete(_by_ordinal(document_id, 0))

    after = client.post(
        "/api/v1/search", json={"query": "眼轴长度", "kb_ids": [kb_id], "top_k": 10}
    ).json()
    for hit in after["hits"]:
        # 既然还能命中，就必须能取到正文——取不到就是幽灵
        found = client.get(f"/api/v1/chunks/{quote(hit['chunk_id'], safe='')}")
        assert found.status_code == 200, f"幽灵块：{hit['chunk_id']}"


def test_delete_by_ordinal_out_of_range_is_404(client: TestClient, document_id: str) -> None:
    assert client.delete(_by_ordinal(document_id, 9999)).status_code == 404


# --------------------------------------------------------------------- 鉴权


def test_write_operations_need_write_permission(client: TestClient) -> None:
    """三个动作都会改变检索结果，只读密钥不该能做。"""
    console = dict(client.headers)  # 管理员会话
    kb = client.post("/api/v1/knowledge-bases", json={"name": "鉴权库"}, headers=console)
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb.json()['id']}/documents",
        files={"file": ("a.md", io.BytesIO(MARKDOWN.encode()), "text/markdown")},
        headers=console,
    )
    doc_id = upload.json()["document"]["id"]
    _drain_worker()
    path = _by_ordinal(doc_id, 0)

    issued = client.post(
        "/api/v1/api-keys",
        json={"name": "只读", "permission": "readonly", "knowledge_base_ids": []},
        headers=console,
    ).json()
    readonly = {"Authorization": f"Bearer {issued['token']}"}

    # 读可以
    assert client.get(path, headers=readonly).status_code == 200
    # 写不行
    assert client.patch(path, json={"text": "偷改"}, headers=readonly).status_code == 403
    assert (
        client.put(f"{path}/disabled", json={"disabled": True}, headers=readonly).status_code == 403
    )
    assert client.delete(path, headers=readonly).status_code == 403
