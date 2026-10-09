r"""知识库提供者客户端（M3 阶段 2）的契约与失败分档。

镜像同构：``app/services/knowledge_provider.py`` → 本文件。

原则：**测试里不打真网络** ✗ —— 全部用 ``httpx.MockTransport`` 注入假传输 ✓
（手法照 ``tests/unit/services/test_remote_clients.py``：那里是 P2 两个远端客户端的
契约，这里是它们的收编者）。

覆盖（方案 §7 阶段 2 的完成判据逐条 + §8 B 的前三行）：

① 握手 200 / 401 / 连不上 / 超时 / 版本更高；
② 检索命中 / 空 / 500；
③ 入库的 multipart 形状（含 ``start=true``）与去重；
④ 未配时两面的错误类型（``submit`` → ``KnowledgeBaseUnavailable``，
   ``retrieve_sources`` → ``RemoteUnavailableError``）；
⑤ 地址解析纯函数各分支（``enabled`` / 运行期覆盖 / ``kb_url`` / ``server_url``）；
⑥ 握手缓存：TTL 内不重探、``refresh=True`` 强制重探；
⑦ 凭据**不落库、不进日志**（R3）；
⑧ reader（``knowledge_meta()``，M3 阶段 4 起是 ``stores.meta`` 的读口）：404 → ``None``、
   其余失败**一律折成 ``KnowledgeBaseUnavailable``**（5xx / 4xx / 连不上，见那里的说明）。

**本文件标 ``local``**：它只拼请求、只读本机 SQLite（最后那条 R3 用例），一个远端都不连。
没标的话，没有 PG 的机器上整份会被 ``conftest.py`` 静默跳过——而"断 NAS 的机器上
这一层该怎么表现"正是它要证明的事。
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.storage import LOCAL_DB_NAME, build_stores
from app.services import remote_clients
from app.services.ingest import IngestService
from app.services.knowledge_client import KnowledgeClient
from app.services.knowledge_provider import (
    HANDSHAKE_TIMEOUT_SECONDS,
    HANDSHAKE_TTL_SECONDS,
    PROTOCOL_VERSION,
    SETTING_BASE_URL,
    SETTING_ENABLED,
    STATE_READY,
    STATE_UNAVAILABLE,
    STATE_UNCONFIGURED,
    KnowledgeProviderClient,
    resolve_provider_target,
)
from app.services.remote_clients import RemoteUnavailableError
from app.storage.base import KnowledgeBaseUnavailable

pytestmark = pytest.mark.local

BASE = "http://nas.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_must_not_leak"

#: 一个**真的能过**的握手响应（八键照方案 §1.2 的契约；能力集这里不必填满）。
HANDSHAKE: dict[str, Any] = {
    "provider": "knowledge",
    "protocol_version": PROTOCOL_VERSION,
    "app_version": "0.1.1",
    "api_version": "v1",
    "capabilities": {
        "retrieval": {"modes": ["hybrid"], "default_mode": "hybrid", "top_k_max": 100},
        "ingest": {"transport": "multipart", "async": True, "max_bytes": 2048},
    },
    "caller": {
        "kind": "api_key",
        "permission": "readwrite",
        "is_admin": False,
        "can_write": True,
        "knowledge_base_ids": [],
    },
    "knowledge_bases": [
        {
            "id": "kb_a",
            "name": "论文",
            "document_count": 3,
            "can_write": True,
            "embedding_model_id": "bge-m3",
            "embedding_dim": 1024,
            "wiki_enabled": False,
        }
    ],
    "server_time": "2026-10-03T09:00:00Z",
}


# ------------------------------------------------------------------ 假传输与装配


def _settings(**overrides: Any) -> Settings:
    """一份**与当前机器无关**的引导级配置。

    ``_env_file=None`` 挡掉开发机的 ``.env``，相关字段一律显式给 —— 于是这几条用例
    既不看环境变量、也不看仓库里那份 ``.env``（与 ``test_storage`` 的
    ``_local_settings`` 同一手法）。
    """
    base: dict[str, Any] = {
        "deployment": "local",
        "database_url": "",
        "server_url": "",
        "token": "",
        "kb_url": "",
        "kb_token": "",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def _client(
    handler: Any,
    *,
    settings: Settings | None = None,
    get_setting: Any = None,
    clock: Any = None,
) -> KnowledgeProviderClient:
    """默认目标 = ``BASE`` + 这把钥匙；地址解析的分支各自给 settings / get_setting。"""
    return KnowledgeProviderClient(
        settings=settings if settings is not None else _settings(server_url=BASE, token=TOKEN),
        get_setting=get_setting if get_setting is not None else (lambda key: ""),
        transport=httpx.MockTransport(handler),
        clock=clock if clock is not None else time.monotonic,
    )


def _no_requests():  # type: ignore[no-untyped-def]
    """一个"绝不该被调用"的传输（没配就不该探：这条断言比"没打某个 URL"强）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"这个用例不该发任何请求：{request.method} {request.url}")

    return handler


# ---------------------------------------------------------------------- ① 握手


def test_a_successful_handshake_is_ready_with_capabilities_and_bases() -> None:
    """200 → ``ready``：能力集、调用者、库清单、协议版本都照实带出来。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=HANDSHAKE)

    status = _client(handler).status()

    assert status.state == STATE_READY
    assert status.available is True
    assert status.reason == ""
    assert seen["url"] == f"{BASE}/provider/handshake"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert status.base_url == BASE
    assert status.credential == "configured"
    assert status.protocol_version == PROTOCOL_VERSION
    assert status.app_version == "0.1.1"
    assert status.capabilities["ingest"]["max_bytes"] == 2048
    assert status.caller["is_admin"] is False
    assert [item["id"] for item in status.knowledge_bases] == ["kb_a"]
    assert status.checked_at.tzinfo is not None


def test_a_rejected_credential_says_so_instead_of_unreachable() -> None:
    """401 → ``unavailable``、原因说**凭据**（方案 §8 B 的判据；R7 的两档之一）。

    "改钥匙"与"改地址"必须能被一句话分开：合成一句"连不上"会让人去查网络，
    而网络根本没问题。
    """
    status = _client(lambda request: httpx.Response(401, text="bad key")).status()

    assert status.state == STATE_UNAVAILABLE
    assert "凭据" in status.reason
    assert "连不上" not in status.reason
    # 地址解析出来了、凭据也确实配了（配了但被拒 ≠ 没配）
    assert status.base_url == BASE
    assert status.credential == "configured"


@pytest.mark.parametrize("code", [403, 404, 500, 503])
def test_every_other_failure_is_unavailable_without_pretending(code: int) -> None:
    """403 / 404 / 5xx 一律 ``unavailable``（`ready` 只有握手真的成功才给）。"""
    status = _client(lambda request: httpx.Response(code, text="nope")).status()

    assert status.state == STATE_UNAVAILABLE
    assert status.reason
    assert status.available is False


def test_a_connect_error_says_unreachable() -> None:
    """连不上 → 原因里是"连不上"（不是"凭据"，也不是"没命中"）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    status = _client(handler).status()

    assert status.state == STATE_UNAVAILABLE
    assert "连不上" in status.reason


def test_the_probe_timeout_is_three_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针超时 **3s**（方案 §3.2）：这里连"传下去的那个数"一起钉住。

    只断言常量等于 3.0 不够 —— 真正会坏的是"某个调用点忘了传它"（那会退回 httpx 的
    默认 5s/无限等），所以这里把假传输换成**记录 kwargs 的那个**。
    """
    seen: dict[str, Any] = {}

    def fake_httpx():  # type: ignore[no-untyped-def]
        def _client(**kwargs: Any) -> httpx.Client:
            seen["timeout"] = kwargs.get("timeout")
            seen["base_url"] = kwargs.get("base_url")
            raise httpx.ConnectTimeout("timed out")

        return type("FakeHttpx", (), {"Client": _client, "HTTPError": httpx.HTTPError})

    monkeypatch.setattr(remote_clients, "_httpx", fake_httpx)

    status = _client(_no_requests()).status()

    assert HANDSHAKE_TIMEOUT_SECONDS == 3.0
    assert seen["timeout"] == HANDSHAKE_TIMEOUT_SECONDS
    assert seen["base_url"] == BASE
    assert status.state == STATE_UNAVAILABLE
    assert "连不上" in status.reason


def test_a_newer_protocol_version_is_unavailable_and_says_the_version() -> None:
    """协议版本更高 → **不可用 + 句子里带版本号**（方案 §1.2 裁量 3：绝不硬试）。"""
    body = {**HANDSHAKE, "protocol_version": PROTOCOL_VERSION + 1}

    status = _client(lambda request: httpx.Response(200, json=body)).status()

    assert status.state == STATE_UNAVAILABLE
    assert str(PROTOCOL_VERSION + 1) in status.reason
    assert str(PROTOCOL_VERSION) in status.reason


def test_a_response_without_a_protocol_version_is_unavailable() -> None:
    """回的不是握手体（连协议版本都没有）→ 不可用，且指出"地址可能指错了"。"""
    status = _client(lambda request: httpx.Response(200, json={"hello": "world"})).status()

    assert status.state == STATE_UNAVAILABLE
    assert "协议版本" in status.reason


def test_a_broken_probe_degrades_to_unavailable_instead_of_raising() -> None:
    """**探针绝不抛**：认不出的异常也按不可用处理（原因带类型名，全文进日志）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise ValueError("这不是 httpx 的错误（模拟我们自己的 bug）")

    status = _client(handler).status()

    assert status.state == STATE_UNAVAILABLE
    assert "ValueError" in status.reason


# ------------------------------------------------------ ⑤ 地址解析（纯函数）


def test_an_unconfigured_provider_never_probes() -> None:
    """一个地址都没有 → ``unconfigured``，而且**一个请求都不发**。"""
    status = _client(_no_requests(), settings=_settings()).status()

    assert status.state == STATE_UNCONFIGURED
    assert status.available is False
    assert status.base_url == ""
    assert "地址" in status.reason  # 原因里说清下一步（去配一个地址）
    assert status.credential == "missing"


def test_an_explicitly_disabled_provider_is_unconfigured_even_with_an_address() -> None:
    """``provider.knowledge.enabled`` 显式关 → 不配（连地址都不再解析）。"""
    settings = _settings(server_url=BASE, token=TOKEN)
    get_setting = lambda key: "0" if key == SETTING_ENABLED else ""  # noqa: E731

    status = _client(_no_requests(), settings=settings, get_setting=get_setting).status()

    assert status.state == STATE_UNCONFIGURED
    # "关掉了"与"没填地址"是两句不同的话（面板各给一个下一步）
    assert "关掉" in status.reason
    assert "地址" not in status.reason


def test_the_runtime_base_url_wins_over_the_bootstrap_one() -> None:
    """运行期覆盖键 > ``kb_url`` > ``server_url``（§4.1 的三层，逐层验）。"""
    settings = _settings(server_url="http://bootstrap.test/api/v1", token="t1")

    # ① 只有引导级：地址就是 server_url
    assert resolve_provider_target(settings, lambda key: "").base_url == (
        "http://bootstrap.test/api/v1"
    )
    # ② kb_url 覆盖它
    with_kb_url = _settings(
        server_url="http://bootstrap.test/api/v1",
        kb_url="http://kb-override.test/api/v1",
        token="t1",
    )
    assert resolve_provider_target(with_kb_url, lambda key: "").base_url == (
        "http://kb-override.test/api/v1"
    )
    # ③ 运行期那一处覆盖上面两个（设置页改的就是它）
    get_setting = lambda key: (  # noqa: E731
        "http://runtime.test/api/v1" if key == SETTING_BASE_URL else ""
    )
    assert resolve_provider_target(with_kb_url, get_setting).base_url == (
        "http://runtime.test/api/v1"
    )
    # 末尾的斜杠会去掉（拼出来不能是 //provider/handshake）
    assert (
        resolve_provider_target(_settings(server_url=f"{BASE}/"), lambda key: "").base_url == BASE
    )


def test_the_token_falls_back_from_kb_token_to_token() -> None:
    """凭据：``kb_token`` > ``token``；两个都没有 = ``missing``（面板只显示配没配）。"""
    fallback = resolve_provider_target(_settings(server_url=BASE, token="t1"), lambda key: "")
    assert (fallback.token, fallback.credential) == ("t1", "configured")

    override = resolve_provider_target(
        _settings(server_url=BASE, token="t1", kb_token="t2"), lambda key: ""
    )
    assert override.token == "t2"

    none = resolve_provider_target(_settings(server_url=BASE), lambda key: "")
    assert (none.token, none.credential) == ("", "missing")
    # **有地址就能用**：凭据缺了是另一档（面板上有 credential 那一位，与 configured 分开）
    assert none.configured is True


def test_a_disabled_value_is_recognised_in_several_spellings() -> None:
    """``0 / false / no / off`` 都算关（大小写与空白不敏感）。"""
    settings = _settings(server_url=BASE)
    for raw in ("0", "false", "FALSE", " no ", "off"):
        get_setting = lambda key, raw=raw: raw if key == SETTING_ENABLED else ""  # noqa: E731
        assert resolve_provider_target(settings, get_setting).configured is False
    for raw in ("", "1", "true", "yes", "on"):
        get_setting = lambda key, raw=raw: raw if key == SETTING_ENABLED else ""  # noqa: E731
        assert resolve_provider_target(settings, get_setting).configured is True


# ---------------------------------------------------------------- ⑥ 握手缓存


def test_the_handshake_is_cached_for_the_ttl_and_refresh_forces_a_probe() -> None:
    """TTL **30s** 内不重探；``refresh=True`` 强制重探（方案 §3.2 与 §8 B 的判据）。"""
    calls: list[str] = []
    now = {"t": 1000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=HANDSHAKE)

    client = _client(handler, clock=lambda: now["t"])

    assert client.status().state == STATE_READY
    assert len(calls) == 1
    # TTL 之内：还是那份缓存（**没有第二次往返** —— R1 要的正是这件事）
    now["t"] += HANDSHAKE_TTL_SECONDS - 0.5
    assert client.status().state == STATE_READY
    assert len(calls) == 1
    # 过 TTL：重探
    now["t"] += 1.0
    assert client.status().available is True
    assert len(calls) == 2
    # refresh=1：立刻重探（设置面板保存地址之后走的就是这一条）
    assert client.status(refresh=True).available is True
    assert len(calls) == 3


def test_a_failed_handshake_is_cached_too() -> None:
    """**失败也缓存**：`state != ready` 时是前端每 30s 轮一次，不是每次调用都打一遍。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, text="boom")

    client = _client(handler)

    assert client.status().state == STATE_UNAVAILABLE
    assert client.status().state == STATE_UNAVAILABLE
    assert len(calls) == 1


def test_the_status_payload_has_no_credential_and_the_contract_keys() -> None:
    """给阶段 5 的响应体（§3.1）：键集合对，而且**没有凭据**（只有"配没配"）。"""
    ready = _client(lambda request: httpx.Response(200, json=HANDSHAKE)).status().to_payload()

    assert set(ready) == {
        "state",
        "available",
        "reason",
        "checked_at",
        "base_url",
        "credential",
        "protocol_version",
        "app_version",
        "capabilities",
        "caller",
        "knowledge_bases",
    }
    assert ready["credential"] == "configured"
    assert TOKEN not in json.dumps(ready, ensure_ascii=False)

    # 不 ready 时那五段**根本不在**（不是空值：界面不该去猜"没探到还是真没有"）
    offline = _client(_no_requests(), settings=_settings()).status().to_payload()
    assert set(offline) == {
        "state",
        "available",
        "reason",
        "checked_at",
        "base_url",
        "credential",
    }


# ---------------------------------------------------------------------- ② 检索


def test_retrieve_sources_maps_hits_to_numbered_source_refs() -> None:
    """命中 → 带编号的 ``SourceRef``（1..N，编号口径与进程内实现一致）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search")
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_name": "指南.pdf",
                        "heading_path": "第 3 章",
                        "page": 7,
                        "score": 0.5,
                        "preview": "原文",
                    },
                    {"chunk_id": "c2", "document_id": "d2", "document_name": "年报.pdf"},
                ]
            },
        )

    sources = _client(handler).retrieve_sources(query="储能", kb_ids=["kb_1"], top_k=3)

    assert [source.index for source in sources] == [1, 2]
    assert sources[0].document_name == "指南.pdf"
    assert sources[0].page == 7


def test_retrieve_sources_returns_empty_when_nothing_matched() -> None:
    """200 且没有命中 = **空列表**（这才是"没命中"，不是"不可用"）。"""
    client = _client(lambda request: httpx.Response(200, json={"hits": []}))

    assert client.retrieve_sources(query="没有这条", kb_ids=["kb_1"]) == []


def test_retrieve_sources_fails_loudly_when_the_provider_errors() -> None:
    """500 → **抛**（不是空结果：空结果会被读成"库里没有这条"）。"""
    client = _client(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(RemoteUnavailableError):
        client.retrieve_sources(query="q", kb_ids=["kb_1"])


def test_retrieve_sources_on_an_unconfigured_provider_raises_unavailable() -> None:
    """未配 → ``RemoteUnavailableError``（工具循环那一面；**绝不回退进程内检索**）。"""
    client = _client(_no_requests(), settings=_settings())

    with pytest.raises(RemoteUnavailableError) as excinfo:
        client.retrieve_sources(query="q", kb_ids=["kb_1"])

    assert "知识库提供者" in str(excinfo.value)


def test_retrieve_sources_signature_matches_the_protocol_verbatim() -> None:
    """签名与 ``KnowledgeClient`` 协议**逐字一致**（方案 §1.3 的"逐字"判据）。

    这一条是机械判据：将来谁给其中一边加一个参数，这里就红 ——
    而那种漂在运行期只表现为"某个调用点 TypeError"。
    """
    assert inspect.signature(KnowledgeProviderClient.retrieve_sources) == inspect.signature(
        KnowledgeClient.retrieve_sources
    )


# ---------------------------------------------------------------------- ③ 入库


def test_submit_posts_multipart_with_start_true_and_sends_the_operator() -> None:
    """入库的 multipart 形状：``file`` 一段 + ``start=true`` + 归属头（G6）。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        seen["operator"] = request.headers.get("x-kylab-operator")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            202,
            json={
                "document": {"id": "doc_9", "name": "笔记.md"},
                "is_duplicate": False,
                "task_id": "task_1",
            },
        )

    outcome = _client(handler).submit(
        knowledge_base_id="kb_1",
        filename="笔记.md",
        content="正文".encode(),
        mime_type="text/markdown",
        uploaded_by="user_1",
    )

    assert seen["method"] == "POST"
    assert seen["url"].startswith(f"{BASE}/knowledge-bases/kb_1/documents")
    assert "start=true" in seen["url"]
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["operator"] == "user_1"
    # multipart 三段齐：文件名、内容、以及**调用方给的 mime_type**（服务器按它登记）
    assert 'filename="' in seen["body"].decode("utf-8", "replace")
    assert "正文".encode() in seen["body"]
    assert b"text/markdown" in seen["body"]
    # 返回形状与 `IngestService.submit` 的最小读法同形
    assert outcome.document.id == "doc_9"
    assert outcome.document.name == "笔记.md"
    assert outcome.is_duplicate is False


def test_submit_reports_a_duplicate_upload() -> None:
    """同一份内容重复上传 → ``is_duplicate=True``（去重由 NAS 的 content_hash 兜）。"""
    client = _client(
        lambda request: httpx.Response(
            202, json={"document": {"id": "doc_1", "name": "a.md"}, "is_duplicate": True}
        )
    )

    outcome = client.submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")

    assert outcome.is_duplicate is True
    assert outcome.document.id == "doc_1"


def test_submit_lets_the_content_type_be_guessed_when_the_caller_gives_none() -> None:
    """``mime_type`` 留空 → **按文件名猜**（不写死 ``application/octet-stream``）。

    服务器把 multipart 里那一段的 content-type 记成文档的 ``mime_type``，
    而解析路由凭后缀 / MIME / 内容三样一起判 —— 写死"未知"是白丢一条线索。
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(202, json={"document": {"id": "doc_1", "name": "a.md"}})

    _client(handler).submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")

    assert b"text/markdown" in seen["body"]


def test_submit_carries_the_folder_and_uses_the_document_id_as_an_idempotency_key() -> None:
    """``folder_id`` 进查询串；``document_id`` 当 ``Idempotency-Key``（签名那两处差异）。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("idempotency-key")
        return httpx.Response(202, json={"document": {"id": "doc_2", "name": "a.md"}})

    _client(handler).submit(
        knowledge_base_id="kb_1",
        filename="a.md",
        content=b"x",
        folder_id="folder_7",
        document_id="doc_local_3",
    )

    assert "folder_id=folder_7" in seen["url"]
    assert seen["key"] == "doc_local_3"


def test_submit_on_an_unconfigured_provider_raises_the_503_error() -> None:
    """未配 → ``KnowledgeBaseUnavailable``（**不是** RuntimeError：HTTP 面要 503 + 原因）。"""
    client = _client(_no_requests(), settings=_settings())

    with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
        client.submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")

    assert "知识库提供者" in str(excinfo.value)


def test_submit_folds_a_remote_failure_into_the_503_error() -> None:
    """连不上 / 5xx 也折成 ``KnowledgeBaseUnavailable``：两个入库端点报 503 而不是 500。"""

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    for handler in (offline, lambda request: httpx.Response(503, text="down")):
        with pytest.raises(KnowledgeBaseUnavailable):
            _client(handler).submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")


def test_submit_rejects_a_response_without_a_document_id() -> None:
    """回包认得出来但少了 id → **如实报**（不造一个假的：调用方要拿它回填笔记/产物）。"""
    client = _client(lambda request: httpx.Response(202, json={"is_duplicate": False}))

    with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
        client.submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")

    assert "id" in str(excinfo.value)


def test_submit_signature_matches_ingest_service_verbatim() -> None:
    """签名与 ``IngestService.submit`` **逐字一致**（含 ``mime_type`` / ``folder_id``）。

    笔记与产物那两条路是照那个签名调的：少一个参数就是 ``TypeError``
    —— M2 那份边车实现正是这么坏的（方案 §5.1 点名的那件事）。

    比的是**每个形参的名字 / 种类 / 默认值 / 注解**（不比返回注解：一个是
    ``IngestOutcome``，一个是同形的最小读法 —— 那是**有意**的差异，本模块头上写着）。
    """
    provider = inspect.signature(KnowledgeProviderClient.submit)
    ingest = inspect.signature(IngestService.submit)

    assert [
        (item.name, item.kind, item.default, item.annotation)
        for item in provider.parameters.values()
    ] == [
        (item.name, item.kind, item.default, item.annotation) for item in ingest.parameters.values()
    ]


def test_the_ingest_gateway_exposes_submit_only() -> None:
    """网关是**窄视图**：只该有 ``submit`` 这一件事（别把整个客户端摆到 ``Services.ingest``）。"""
    gateway = _client(lambda request: httpx.Response(202, json={"document": {"id": "d"}}))
    view = gateway.ingest_gateway()

    assert not hasattr(view, "retrieve_sources")
    assert not hasattr(view, "status")
    outcome = view.submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")
    assert outcome.document.id == "d"


def test_the_enqueue_gateway_is_a_no_op_that_never_touches_the_network() -> None:
    """``enqueue_gateway`` 是**空操作**：队已经在 NAS 那一侧排上了（本机没有队列）。"""
    gateway = _client(_no_requests()).enqueue_gateway()

    assert gateway.enqueue_ingest("doc_1").id is None


# ------------------------------------------------------------ 进度 / 元数据（读）


def test_document_status_reads_the_document_and_its_timeline() -> None:
    """进度：文档现状 + 阶段时间线两段（§1.3 那条"进度"行）。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json={"steps": [{"name": "parsing", "done": True}]})
        return httpx.Response(200, json={"id": "doc_1", "stage": "parsing"})

    result = _client(handler).document_status("doc_1")

    assert paths == ["/api/v1/documents/doc_1", "/api/v1/documents/doc_1/timeline"]
    assert result["document"]["stage"] == "parsing"
    assert result["timeline"]["steps"][0]["name"] == "parsing"


def test_the_meta_reader_maps_a_missing_base_to_none_and_lists_items() -> None:
    """``knowledge_meta()`` 的 reader（**阶段 4 用**）：404 → ``None``，列表取 ``items``。"""
    reader = _client(
        lambda request: (
            httpx.Response(404, text="没有这个库")
            if request.url.path.endswith("/knowledge-bases/kb_missing")
            else httpx.Response(200, json={"items": [{"id": "kb_a", "name": "论文"}]})
        )
    ).knowledge_meta()

    assert reader.get_knowledge_base("kb_missing") is None
    assert [item["id"] for item in reader.list_knowledge_bases()] == ["kb_a"]


def test_the_meta_reader_has_exactly_the_two_plan_methods() -> None:
    """reader 的形状就是方案 §2.3 那两件（多一个方法就是多一条没人接的路）。"""
    reader = _client(_no_requests()).knowledge_meta()

    public = {name for name in dir(reader) if not name.startswith("_")}
    assert public == {"get_knowledge_base", "list_knowledge_bases"}
    assert list(inspect.signature(reader.get_knowledge_base).parameters) == ["kb_id"]
    assert list(inspect.signature(reader.list_knowledge_bases).parameters) == []


def test_the_meta_reader_is_unavailable_when_the_provider_is_not_configured() -> None:
    """未配 → ``KnowledgeBaseUnavailable``：对 ``stores.meta`` 那一面，"没配"就是"没有"。"""
    reader = _client(_no_requests(), settings=_settings()).knowledge_meta()

    with pytest.raises(KnowledgeBaseUnavailable):
        reader.list_knowledge_bases()
    with pytest.raises(KnowledgeBaseUnavailable):
        reader.get_knowledge_base("kb_a")


def test_the_meta_reader_folds_every_remote_failure_into_knowledge_base_unavailable() -> None:
    """取不到 → **同一族**错误（5xx / 4xx / 连不上都折成 ``KnowledgeBaseUnavailable``）。

    为什么必须折（M3 阶段 4 起它是 ``stores.meta`` 的读口）：分档
    （``RemoteUnavailableError`` / ``RemoteRejectedError``）是**工具循环**那一面的事，
    而 reader 的调用方在 storage 层——它认得的只有"现在取不到"这一个答案。原样抛出去，
    ``stores.meta`` 上就会冒出 storage 不认识的错误类型，503 映射也接不住它
    （``core/exceptions.py`` 只认 ``KnowledgeBaseUnavailable``）。

    **404 不在这一族里**：那是"没有这个库"，是答案（上一条用例）。
    """

    def five_hundred(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="这把钥匙不对")

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上", request=request)

    for name, handler, expected in (
        ("5xx", five_hundred, "HTTP 500"),
        ("403", rejected, "HTTP 403"),
        ("连不上", offline, "连不上"),
    ):
        reader = _client(handler).knowledge_meta()
        for method in ("get_knowledge_base", "list_knowledge_bases"):
            args = ("kb_a",) if method == "get_knowledge_base" else ()
            with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
                getattr(reader, method)(*args)
            assert expected in str(excinfo.value), f"{method}（{name}）"


# ------------------------------------------------------------------ ⑦ R3 凭据


def _app_settings(database: Path) -> list[tuple[str, str]]:
    """直连本机库读 ``app_settings``（**测试不适用 L2 那道分层纪律**，同 test_local_backend）。"""
    with sqlite3.connect(database) as conn:
        return [
            (str(key), str(value))
            for key, value in conn.execute("SELECT key, value FROM app_settings")
        ]


def test_the_token_never_lands_in_the_local_database_or_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3：凭据**不落库、不进日志** —— 走一遍握手与入库之后，两处都得干净。

    这条用例的价值在"将来"：谁要是为了"少探一次"把钥匙缓存进 ``app_settings``，
    或者把请求头打进日志，这里立刻红（那两件事都真的发生过，见 M2 R6 / M5 的迁钥匙串）。
    """
    settings = _settings(server_url=BASE, token=TOKEN, data_dir=tmp_path / "data")
    stores = build_stores(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/provider/handshake"):
            return httpx.Response(200, json=HANDSHAKE)
        return httpx.Response(202, json={"document": {"id": "doc_1", "name": "a.md"}})

    client = KnowledgeProviderClient(settings=settings, transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        assert client.status().state == STATE_READY
        client.submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")
        # 失败那条路也走一遍（探针的日志分支里最容易把上下文打出去）
        broken = KnowledgeProviderClient(
            settings=settings,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(ValueError("boom"))
            ),
        )
        assert broken.status().state == STATE_UNAVAILABLE

    rows = _app_settings(Path(settings.data_dir) / LOCAL_DB_NAME)
    assert not [key for key, _ in rows if "token" in key.lower()], rows
    assert TOKEN not in json.dumps(rows, ensure_ascii=False)
    assert TOKEN not in caplog.text
    # 顺带钉住：提供者那一档**什么都不写库**（两个运行期键是设置页写的，不是它写的）
    assert [key for key, _ in rows if key.startswith("provider.")] == []
    # 库文件真的建起来了（不是在内存里跑的假路径）
    assert (Path(settings.data_dir) / LOCAL_DB_NAME).is_file()
    assert stores.meta is not None


def test_both_error_faces_are_distinct_on_purpose() -> None:
    """两种错误类型是**刻意的**（方案 §5.1）：HTTP 端点面 503、工具面 RuntimeException。

    合成一种的表现是二者之一：入库端点报 500（"内部错误"把"没这个能力"藏起来），
    或者工具循环把"知识库没配"当成一次网络抖动。
    """
    client = _client(_no_requests(), settings=_settings())

    with pytest.raises(KnowledgeBaseUnavailable):
        client.submit(knowledge_base_id="kb_1", filename="a.md", content=b"x")
    with pytest.raises(RemoteUnavailableError):
        client.retrieve_sources(query="q", kb_ids=["kb_1"])
