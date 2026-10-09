"""数据源服务与端点（M6 / T6.1–T6.3）。

镜像同构：``app/services/sources.py`` + ``app/api/v1/data_sources.py`` → 本文件。

**T6.3 的验收条件是"重复拉取不产生重复文档"**，这个性质落在摄入侧的内容 hash
去重上，所以必须端到端验一次——只看连接器用例会漏掉它。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.models.enums import DataSourceKind
from app.services.sources import SourceService
from tests.conftest import admin_client as admin_session

RSS_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>测试源</title>
  <item><title>第一条</title><link>https://example.com/1</link>
    <description><![CDATA[<p>第一条的正文内容，长度足够以便作为正文入库使用。</p>]]></description></item>
  <item><title>第二条</title><link>https://example.com/2</link>
    <description><![CDATA[<p>第二条的正文内容，长度足够以便作为正文入库使用。</p>]]></description></item>
</channel></rss>
"""


@pytest.fixture
def client():
    """带管理员会话凭据的客户端（v0.11 起 /api/v1 一律要凭据）。"""
    with admin_session() as test_client:

        yield test_client



@pytest.fixture
def kb_id(client: TestClient) -> str:
    return client.post("/api/v1/knowledge-bases", json={"name": "订阅库"}).json()["id"]


def _register(client: TestClient, kb_id: str, kind: str = "rss", **extra) -> dict:  # type: ignore[no-untyped-def]
    payload = {"kind": kind, "name": "测试源", "url": "https://example.com/feed.xml", **extra}
    response = client.post(f"/api/v1/knowledge-bases/{kb_id}/data-sources", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- 登记


def test_register_and_list(client: TestClient, kb_id: str) -> None:
    created = _register(client, kb_id)

    assert created["kind"] == "rss"
    assert created["enabled"] is True
    assert created["last_pulled_at"] is None, "刚登记时不该显示已拉取过"

    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/data-sources").json()["items"]
    assert [item["id"] for item in listed] == [created["id"]]


def test_register_rejects_webdav(client: TestClient, kb_id: str) -> None:
    """WebDAV 明确不做（架构 §14 缓做）。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/data-sources",
        json={"kind": "webdav", "name": "网盘", "url": "https://example.com/dav"}
    )
    assert response.status_code == 422


def test_register_rejects_non_http_url(client: TestClient, kb_id: str) -> None:
    """只允许 http(s)：``file://`` 之类会变成任意文件读取。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/data-sources",
        json={"kind": "rss", "name": "本地", "url": "file:///etc/passwd"}
    )
    assert response.status_code == 422


def test_register_needs_an_existing_kb(client: TestClient) -> None:
    response = client.post(
        "/api/v1/knowledge-bases/kb_不存在/data-sources",
        json={"kind": "rss", "name": "x", "url": "https://example.com/f"}
    )
    assert response.status_code == 404


def test_toggle_and_delete(client: TestClient, kb_id: str) -> None:
    created = _register(client, kb_id)

    off = client.patch(
        f"/api/v1/data-sources/{created['id']}/enabled", params={"enabled": False}
    ).json()
    assert off["enabled"] is False

    assert client.delete(f"/api/v1/data-sources/{created['id']}").status_code == 204
    assert client.get(f"/api/v1/knowledge-bases/{kb_id}/data-sources").json()["items"] == []


def test_delete_keeps_already_fetched_documents(client: TestClient, kb_id: str) -> None:
    """删数据源**不该删已抓来的文档**——它们是知识库的正式内容，可能已被引用。
    停掉订阅不等于要撤销已经收集的资料。
    """
    created = _register(client, kb_id)
    # 直接传一份文档，模拟"已经抓过"
    upload = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", "# 标题\n\n正文。".encode(), "text/markdown")},
        params={"start": "false"}
    )
    assert upload.status_code == 202

    client.delete(f"/api/v1/data-sources/{created['id']}")

    docs = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert len(docs) == 1, "文档被连带删掉了"


# --------------------------------------------------------------------- 拉取


def _source_service() -> SourceService:
    from app.core.services import get_services

    return get_services().sources


def test_sync_now_ingests_the_entries(client: TestClient, kb_id: str, fake_feed) -> None:  # type: ignore[no-untyped-def]
    created = _register(client, kb_id)
    service = _source_service()
    fake_feed(RSS_FEED)

    outcome = service.sync_now(created["id"])

    assert outcome.fetched == 2
    assert outcome.created == 2
    assert outcome.duplicates == 0
    docs = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert len(docs) == 2
    assert {item["name"] for item in docs} == {"第一条.md", "第二条.md"}


def test_second_sync_creates_no_duplicates(client: TestClient, kb_id: str, fake_feed) -> None:  # type: ignore[no-untyped-def]
    """**T6.3 的验收条件。**

    重复拉取不产生重复文档——靠的是摄入侧的内容 hash 去重，
    所以这条必须端到端验一次，只看连接器用例会漏掉它。
    """
    created = _register(client, kb_id)
    service = _source_service()
    fake_feed(RSS_FEED)

    first = service.sync_now(created["id"])
    second = service.sync_now(created["id"])

    assert first.created == 2
    assert second.created == 0
    assert second.duplicates == 2, "第二次应当全部判为重复"
    docs = client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()["items"]
    assert len(docs) == 2, "重复拉取后文档数变了"


def test_sync_records_pull_time_and_etag(client: TestClient, kb_id: str, fake_feed) -> None:  # type: ignore[no-untyped-def]
    """304 也要记时间：否则界面上"上次拉取"会一直停在很久以前，
    用户以为定时任务坏了。"""
    created = _register(client, kb_id)
    service = _source_service()
    fake_feed(RSS_FEED, etag='W/"v1"')

    service.sync_now(created["id"])

    listed = client.get(f"/api/v1/knowledge-bases/{kb_id}/data-sources").json()["items"][0]
    assert listed["last_pulled_at"] is not None
    assert listed["etag"] == 'W/"v1"'


def test_not_modified_is_reported(client: TestClient, kb_id: str, fake_feed) -> None:  # type: ignore[no-untyped-def]
    created = _register(client, kb_id)
    service = _source_service()
    fake_feed(RSS_FEED, status=304)

    outcome = service.sync_now(created["id"])

    assert outcome.not_modified is True
    assert outcome.fetched == 0


def test_sync_endpoint_enqueues_by_default(client: TestClient, kb_id: str) -> None:
    """默认入队：一个源几十条、每条都要向量化，同步做会把请求挂几分钟。"""
    created = _register(client, kb_id)

    body = client.post(f"/api/v1/data-sources/{created['id']}/sync").json()

    assert body["task_id"], "默认应当入队并返回任务 id"
    assert body["fetched"] == 0


def test_sync_endpoint_wait_runs_it(client: TestClient, kb_id: str, fake_feed) -> None:  # type: ignore[no-untyped-def]
    created = _register(client, kb_id)
    fake_feed(RSS_FEED)

    body = client.post(
        f"/api/v1/data-sources/{created['id']}/sync", params={"wait": True}
    ).json()

    assert body["task_id"] is None
    assert body["fetched"] == 2
    assert body["created"] == 2


def test_sync_unknown_source_is_404(client: TestClient) -> None:
    assert client.post("/api/v1/data-sources/ds_不存在/sync").status_code == 404


# --------------------------------------------------------------------- 服务层直测


def _bare_service(bundle):  # type: ignore[no-untyped-def]
    """服务层单测用的最小装配：只要参数校验，不需要真的能跑摄入。"""
    from app.services.documents import DocumentService

    return SourceService(
        bundle,
        type("Ingest", (), {"submit": lambda **kwargs: None})(),
        DocumentService(bundle)
    )


def test_service_rejects_unknown_kind(bundle) -> None:  # type: ignore[no-untyped-def]
    service = _bare_service(bundle)
    with pytest.raises(InvalidRequestError):
        service.create(
            knowledge_base_id="kb_1",
            kind=DataSourceKind.WEBDAV,
            name="x",
            url="https://example.com"
    )


def test_service_rejects_missing_kb(bundle) -> None:  # type: ignore[no-untyped-def]
    service = _bare_service(bundle)
    with pytest.raises(NotFoundError):
        service.create(
            knowledge_base_id="kb_不存在",
            kind=DataSourceKind.RSS,
            name="x",
            url="https://example.com/feed"
    )


def test_single_item_failure_does_not_abort_the_batch(
    client: TestClient, kb_id: str, monkeypatch, fake_feed
) -> None:  # type: ignore[no-untyped-def]
    """单条失败不放弃整批：其余条目照样入库，失败的记进 errors。"""
    created = _register(client, kb_id)
    service = _source_service()
    fake_feed(RSS_FEED)

    real_submit = service._ingest.submit
    calls = {"n": 0}

    def flaky_submit(**kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟第一条失败")
        return real_submit(**kwargs)

    monkeypatch.setattr(service._ingest, "submit", flaky_submit)
    outcome = service.sync_now(created["id"])

    assert outcome.created == 1
    assert len(outcome.errors) == 1
    assert "模拟第一条失败" in outcome.errors[0]


@pytest.fixture
def fake_feed(monkeypatch):
    """把连接器要用的 HTTP 换成假的。

    **monkeypatch 模块级 ``build_connector`` 而不是往 service 里注入 client**：
    ``SourceService`` 自己造连接器（生产路径就是这样），
    注入 client 会让用例测的是一条生产上不存在的路径。
    """

    def install(
        feed: str = RSS_FEED, *, etag: str | None = None, status: int = 200
    ) -> None:
        import app.services.sources as sources_module
        from app.services.connectors.base import build_connector as real_build

        def handler(request: httpx.Request) -> httpx.Response:
            if status == 304:
                return httpx.Response(304)
            headers = {"etag": etag} if etag else {}
            return httpx.Response(200, content=feed.encode(), headers=headers)

        fake_client = httpx.Client(transport=httpx.MockTransport(handler))

        def fake_build(kind, *, client=None):  # type: ignore[no-untyped-def]
            return real_build(kind, client=fake_client)

        monkeypatch.setattr(sources_module, "build_connector", fake_build)

    return install
