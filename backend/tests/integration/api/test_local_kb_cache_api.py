"""``/local/kb-cache/*``（本机快照族）的整条链（M4 阶段 4，方案 §7）。

这条用例要回答的是"**页面问一句「本机留的那份是什么」会得到什么**"：本机档的真 app
（``app.main.create_app``，本机档挂的就是 ``local_router`` 那张白名单）+ 假 NAS
（``httpx.MockTransport``，绝不打真网络）+ 真的本机库（SQLite，落在 ``tmp_path``）。
**不需要 PostgreSQL**（``local`` 这个 marker 的全部意义）。

覆盖（方案 §7 阶段 4 的完成判据 + §8 A 的前三行）：

① **命中**：200 + ``items`` + 两个时间戳，而且 ``can_write`` / ``can_manage`` **不在
   payload 里**（机械断言：整份响应文本里一个都不许出现，D-B）；
② **无快照**：200 + ``{available: false, reason}``（**不是 4xx/5xx**），而且**一个请求都不发**
   ——页面面那一读是本机回环，不是一次 NAS 往返（§2.3 / §4.2）；
③ **带筛选的文档列表**：``q`` / ``stage`` / ``source_kind`` 任一给出来都如实回
   ``available:false`` + 那句"不留副本"，**不报错**（D-D）；
④ **主动再验证**：``POST /revalidate`` 用**请求条数**说话（一次读一次），整取→比哈希
   （内容没变时 ``fetched_at`` 不动），失败时 200 + ``stale``（不抛给页面端点）；
⑤ **清理三档粒度**：全清 / 按地址 / 按库（按库 = 一次前缀清 + 两次精确清）；
⑥ **用量读数**（M4 阶段 6）：几项 / 多少字节 / 最旧最新那份是什么时候看到的，
   零网络 + 只算当前地址；
⑦ **服务器档 404**：那一档没有"抄一份 NAS 快照"这条动作，这一族一条都不该挂。

另外两条**跨面**的判据（M4 阶段 3 的那条读线在这里端到端复核）：

- reader 面（``stores.meta`` → ``ChatService``）写下的行，页面面读得到**同一个版本**，
  且这一读**零请求**——两个读面共用一张表、一份内容；
- 假 NAS 的 ``seen`` 记下每一次请求，凡"零网络"那几条都由它判，不靠"看起来对"。
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v1.local import FILTERED_VIEW_REASON
from app.core.config import get_settings
from app.core.services import get_services, reset_services
from app.core.storage import reset_stores
from app.services.kb_cache import DOC_LIST, DOCUMENT, FOLDERS, KB_DETAIL, KB_LIST
from app.services.knowledge_provider import (
    PROTOCOL_VERSION,
    KnowledgeProviderClient,
)

pytestmark = pytest.mark.local

NAS = "http://nas.test/api/v1"
OTHER = "http://other-nas.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_must_not_leak"

#: 这一族的根路径（`local.router` 的 prefix 是 `/local`）。
BASE = "/api/v1/local/kb-cache"

#: 权限位那两个名字：它们**一个都不许出现在响应里**（D-B / R5 的机械断言）。
PERMISSION_BITS = ("can_write", "can_manage")

#: 进快照前必须剥掉的那个活数据字段（§1.1：冻结的进度条是最糟的假象）。
LIVE_FIELDS = ("progress",)


def _kb(kb_id: str, *, name: str = "论文") -> dict[str, Any]:
    """一份库元数据（形状照 NAS 的 ``KnowledgeBaseOut``）：**带**两个权限位。"""
    return {
        "id": kb_id,
        "name": name,
        "description": "",
        "embedding_model_id": "bge-m3",
        "embedding_dim": 1024,
        "chunk_strategy": "fixed",
        "chunk_size": 512,
        "chunk_overlap": 64,
        "can_manage": True,
        "can_write": True,
        "document_count": 2,
    }


def _document(document_id: str) -> dict[str, Any]:
    """一份文档行（``DocumentOut``）：**带** ``progress``（那一栏不许进快照）。"""
    return {
        "id": document_id,
        "knowledge_base_id": "kb_a",
        "name": f"指南-{document_id}.pdf",
        "source_kind": "local",
        "stage": "ready",
        "size_bytes": 448444,
        "progress": {"status": "running", "step_index": 3, "elapsed_ms": 214_000},
    }


def _handshake() -> dict[str, Any]:
    """一个过得去的握手响应（``PATCH /local/provider`` 会顺手重探一次要用到）。"""
    return {
        "provider": "knowledge",
        "protocol_version": PROTOCOL_VERSION,
        "app_version": "0.1.1",
        "api_version": "v1",
        "capabilities": {"retrieval": {"modes": ["hybrid"], "top_k_max": 100}},
        "caller": {"kind": "api_key", "permission": "readwrite", "is_admin": False},
        "knowledge_bases": [{"id": "kb_a", "name": "论文", "can_write": True}],
        "server_time": "2026-10-04T09:00:00Z",
    }


class FakeNas:
    """一台**假 NAS**：按路径回答 + **记下每一次请求**。

    ``fail`` 一置，每一次请求都连不上（"NAS 断"那一档）；``seen`` 是"零网络"与"一次读
    一次"这两类判据的唯一来源。两个库、两篇文档、一个目录——够画出"先出一帧"要的那些行。
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.fail: Exception | None = None
        self.libraries: list[dict[str, Any]] = [_kb("kb_a"), _kb("kb_b", name="手册")]
        self.documents: list[dict[str, Any]] = [_document("doc_1"), _document("doc_2")]
        self.folders: list[dict[str, Any]] = [{"id": "f1", "kb_id": "kb_a", "name": "2026"}]
        self.transport = httpx.MockTransport(self._dispatch)

    # ---------------------------------------------------------------- 假 NAS 的答法
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            raise self.fail
        path = request.url.path
        if path.endswith("/provider/handshake"):
            return httpx.Response(200, json=_handshake())
        if path.endswith("/knowledge-bases"):
            return httpx.Response(200, json={"items": self.libraries})
        if path.endswith("/documents") and "/knowledge-bases/" in path:
            return httpx.Response(
                200,
                json={
                    "items": self.documents,
                    "total": len(self.documents),
                    "limit": int(request.url.params.get("limit", "50")),
                    "offset": int(request.url.params.get("offset", "0")),
                },
            )
        if path.endswith("/folders"):
            return httpx.Response(200, json={"items": self.folders})
        if "/knowledge-bases/" in path:
            kb_id = path.rsplit("/", 1)[-1]
            for item in self.libraries:
                if item["id"] == kb_id:
                    return httpx.Response(200, json=item)
            return httpx.Response(404, text="没有这个库")
        if "/documents/" in path:
            document_id = path.rsplit("/", 1)[-1]
            for item in self.documents:
                if item["id"] == document_id:
                    return httpx.Response(200, json=item)
            return httpx.Response(404, text="没有这篇文档")
        return httpx.Response(404, text="没有这个端点")

    # ------------------------------------------------------------------ 用例那一侧
    def requests_of(self, path: str) -> list[httpx.Request]:
        """打到这个路径上的请求（路径按**后缀**匹配，够用且不看主机）。"""
        return [request for request in self.seen if request.url.path.endswith(path)]

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.handler(request)


@pytest.fixture
def nas() -> FakeNas:
    """假 NAS（用例里一次真网络都不打）。"""
    return FakeNas()


@contextlib.contextmanager
def _local_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_url: str = NAS,
    token: str = TOKEN,
) -> Iterator[TestClient]:
    """本机档的真 app（``local_router`` 那张白名单就挂在这个进程上）。

    ``KYLAB_KB_URL`` / ``KYLAB_KB_TOKEN`` 显式置空：它们是**覆盖**，而开发机的 ``.env``
    里可能真配了——用例不看 ``.env``（与 ``test_local_provider_api._local_app`` 同一手法）。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    monkeypatch.setenv("KYLAB_SERVER_URL", server_url)
    monkeypatch.setenv("KYLAB_TOKEN", token)
    monkeypatch.setenv("KYLAB_KB_URL", "")
    monkeypatch.setenv("KYLAB_KB_TOKEN", "")
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        reset_services()
        reset_stores()
        get_settings.cache_clear()


def _attach(nas: FakeNas, monkeypatch: pytest.MonkeyPatch) -> KnowledgeProviderClient:
    """把假 NAS 接到**进程级那一个**提供者客户端上（M3 阶段 5：全进程只有它一个）。

    接的是 ``_transport``（真客户端的注入接缝）：跑的仍是真客户端代码——鉴权头、超时、
    错误分档、快照服务那一套排程，全都是它发出来的。
    """
    provider = get_services().provider
    assert provider is not None, "本机档必须装配出提供者客户端（组合根那一段）"
    monkeypatch.setattr(provider, "_transport", nas.transport)
    return provider


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    return _ok(client.get(f"{BASE}{path}", params=params))


def _revalidate(client: TestClient, **payload: Any) -> dict[str, Any]:
    return _ok(client.post(f"{BASE}/revalidate", json=payload))


def _prime_all(client: TestClient, *, kb_id: str = "kb_a") -> None:
    """把五个资源各留一份（页面进页面时会同时问的那几个视图）。"""
    _revalidate(client, resource=KB_LIST)
    _revalidate(client, resource=KB_DETAIL, kb_id=kb_id)
    for page in (1, 2):
        _revalidate(client, resource=DOC_LIST, kb_id=kb_id, page=page, size=50)
    _revalidate(client, resource=FOLDERS, kb_id=kb_id)
    _revalidate(client, resource=DOCUMENT, document_id="doc_1")


# --------------------------------------------------------------- ① 命中那一帧


def test_a_hit_serves_the_snapshot_without_permission_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """命中 → 200 + ``items`` + 两个时间戳，而且权限位**不在 payload 里**（D-B / §8 A）。

    三个判据各管一件事：① 页面拿得到行；② 两个时间戳都在（"上次更新于 X"要有话说，
       而 ``checked_at`` 是另一句话）；③ 那一份内容**剥干净了**——机械断言走整份响应文本，
       任何一层里出现 ``can_write`` / ``can_manage`` / ``progress`` 都算红。

    第二次读**零请求**：命中那一读是本机回环，这正是"页面先画一帧"的全部意义（§2.3 ①）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        written = _revalidate(client, resource=KB_LIST)
        assert written["available"] is True and len(written["items"]) == 2
        reads = len(nas.seen)

        body = _snapshot(client, "/knowledge-bases")

        assert body["available"] is True
        assert [item["id"] for item in body["items"]] == ["kb_a", "kb_b"]
        assert body["items"][0]["name"] == "论文"
        assert body["resource"] == KB_LIST and body["scope_key"] == ""
        assert body["version"] == written["version"]
        assert body["fetched_at"] and body["checked_at"]  # 两个时间戳都在，且不混
        assert body["stale"] is False and body["last_error"] == ""
        assert len(nas.seen) == reads, "命中那一读**一个请求都不该发**（本机回环）"
        # 机械断言：整份响应文本里不许出现权限位与活数据字段（任何一层都不许）
        text = json.dumps(body)
        for name in PERMISSION_BITS + LIVE_FIELDS:
            assert name not in text, f"{name} 不该出现在快照里（D-B / §1.1）"
        # 同样的判据落在这两条上：库详情（单个对象）与文档列表（行）
        detail = _snapshot(client, "/knowledge-bases/kb_a")
        assert detail["available"] is True and detail["payload"]["id"] == "kb_a"
        assert "can_write" not in json.dumps(detail)
        _revalidate(client, resource=DOC_LIST, kb_id="kb_a")
        listed = _snapshot(client, "/knowledge-bases/kb_a/documents")
        assert [item["id"] for item in listed["items"]] == ["doc_1", "doc_2"]
        assert "progress" not in json.dumps(listed), "冻结的进度条是最糟的假象"


def test_the_reader_face_and_the_page_face_share_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """**两个读面共用一张表**（M4 阶段 3 那条读线在这里端到端复核）。

    reader 面（``stores.meta`` → 每轮每库那一次读）写下的行，页面面读得到同一个 ``version``，
    而且这一读零请求——阶段 3 的换线就是为这个（"交互路径零网络" + "页面先画一帧"用的是
    同一份内容，不是两份各自过期的副本）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        stores = get_services().chat._stores
        assert stores is not None

        first = stores.meta.get_knowledge_base("kb_a")  # reader 面：未命中，同步取一次
        assert first is not None and first.name == "论文"
        reads = len(nas.seen)

        body = _snapshot(client, "/knowledge-bases/kb_a")

        assert body["available"] is True
        assert body["payload"]["id"] == "kb_a" and body["payload"]["name"] == "论文"
        assert body["source"] == "reader", "这一行是 reader 面写的，来源照实说"
        assert len(nas.seen) == reads, "页面面读的是**同一张表**，不该再打一次 NAS"


# --------------------------------------------------------------- ② 无快照


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/knowledge-bases", {}),
        ("/knowledge-bases/kb_a", {}),
        ("/knowledge-bases/kb_a/documents", {}),
        ("/knowledge-bases/kb_a/folders", {}),
        ("/documents/doc_1", {}),
    ],
)
def test_without_a_snapshot_it_answers_false_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas, path: str, params: dict
) -> None:
    """没有任何副本 → 200 + ``{available: false, reason}``，**一个请求都不发**（§4.2）。

    两条都重要：**不是 4xx/5xx**（页面照旧画骨架屏，不用处理"错误"），以及**不发请求**
    （未命中就去同步取一次的话，"先画一帧"就变成了"多一次 NAS 往返"，而且 NAS 不可达时
    这一条会挂在读超时上——那正是 D-A/§5 要避免的）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        body = _snapshot(client, path, **params)

        assert body["available"] is False
        assert body["reason"], "说'没有'也要给一句人话"
        assert body["items"] == [] and body["payload"] is None
        assert body["fetched_at"] is None and body["checked_at"] is None
        assert nas.seen == [], "无快照那一档不许自己造一次 NAS 往返（§4.2 的流程图）"


def test_a_nas_that_cannot_be_reached_still_answers_the_snapshot_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """NAS 断着也有快照可读（那份内容是上次看到的），而且**如实标** ``stale``（§4.5）。

    两半：① 有副本时再验证失败 → 内容照旧 + ``stale:true`` + 原因（"现在连不上，这是上次
    看到的内容"）；② 连副本都没有时 → ``available:false`` + 那句原因。两半都是 200。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        assert _revalidate(client, resource=KB_LIST)["available"] is True

        nas.fail = httpx.ConnectError("NAS 断了（这条用例要的就是这个）")
        failed = _revalidate(client, resource=KB_LIST)

        assert failed["available"] is True and failed["stale"] is True
        assert "连不上" in failed["last_error"] and failed["items"], "内容照旧可读"
        hit = _snapshot(client, "/knowledge-bases")
        assert hit["available"] is True and hit["stale"] is True
        assert "连不上" in hit["last_error"]

        # 没有副本的那一族：如实说没有 + 原因，不是 500
        assert _snapshot(client, "/knowledge-bases/kb_a/folders")["available"] is False
        body = _revalidate(client, resource=FOLDERS, kb_id="kb_a")
        assert body["available"] is False and body["reason"]


# --------------------------------------------------------------- ③ 带筛选


def test_a_filtered_document_list_says_there_is_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """带 ``q`` / ``stage`` / ``source_kind`` → ``available:false`` + 那句话，**不报错**（D-D）。

    这一档不是"没取到"，而是"这一档本来就不留副本"：搜索结果是"这一问的答案"，过期即误导。
    走 4xx 会让页面以为请求写错了——所以它是 200 + 一句人话。规范视图那一份照旧可读，
    两条一比就看出"变的只有筛选那几档"。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        _revalidate(client, resource=DOC_LIST, kb_id="kb_a")
        reads = len(nas.seen)

        for params in (
            {"q": "指南"},
            {"stage": "ready"},
            {"source_kind": "local"},
            {"folder": "f1", "q": "指"},  # 只要带了筛选项，就是同一档
        ):
            body = _snapshot(client, "/knowledge-bases/kb_a/documents", **params)
            assert body["available"] is False, params
            assert body["reason"] == FILTERED_VIEW_REASON, params
            assert body["scope_key"] == "", "带筛选的那一读没有键（它不给副本）"
        assert len(nas.seen) == reads, "这一档连请求都不该发（没有键可查）"

        plain = _snapshot(client, "/knowledge-bases/kb_a/documents")
        assert plain["available"] is True and len(plain["items"]) == 2


# --------------------------------------------------------------- ④ 主动再验证


def test_revalidate_reads_the_remote_once_and_lands_in_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``POST /revalidate``：**一次读一次请求**，写的是本机那张表（§4.4 的焦点那一行）。

    三条判据：① 每次再验证恰好一次远端读（用请求条数说话，不是"看起来对"）；
    ② 同一份内容再确认一次时**只推 ``checked_at``**（``fetched_at`` 不许动，§4.1——
    "没变"就是"如实更新"里的零变化）；③ ``kb_list`` 那一次顺带把每个库的库详情行按同一份
    payload 拆开写（§3.1），所以随后问页面要库详情是**命中**、零请求。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        listed = _revalidate(client, resource=KB_LIST)

        assert listed["available"] is True and listed["source"] == "revalidate"
        assert len(nas.requests_of("/knowledge-bases")) == 1, "一次再验证 = 一次远端读"
        fetched_at = listed["fetched_at"]

        again = _revalidate(client, resource=KB_LIST)

        assert len(nas.requests_of("/knowledge-bases")) == 2
        assert again["version"] == listed["version"]
        assert again["fetched_at"] == fetched_at, "内容没变 ⇒ 不推进 fetched_at（§4.1）"
        assert again["checked_at"] >= fetched_at

        # 派生出来的库详情行：命中，零请求
        reads = len(nas.seen)
        detail = _snapshot(client, "/knowledge-bases/kb_a")
        assert detail["available"] is True and detail["resource"] == KB_DETAIL
        assert len(nas.seen) == reads

        # 文档列表那一族：一条视图一个键，页签明明白白写在 scope_key 里
        view = _revalidate(client, resource=DOC_LIST, kb_id="kb_a", page=2, size=50)
        assert view["scope_key"] == "kb_a|folder:all|page:2|size:50"
        assert view["items"] and view["payload"]["offset"] == 50, "页换算成了 offset/limit"
        assert (
            _snapshot(client, "/knowledge-bases/kb_a/documents", page=2, size=50)["version"]
            == view["version"]
        )

        # 远端说"没有这个库"→ 删掉那一行（"恰好没有"是答案，与"取不到"两件事）
        nas.libraries = [_kb("kb_b", name="手册")]
        missing = _revalidate(client, resource=KB_DETAIL, kb_id="kb_a")
        assert missing["available"] is False and missing["reason"]


def test_reading_a_snapshot_that_needs_reconfirming_schedules_one_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """读一份"该再确认了"的快照 → **立即回本机那一份** + 后台恰好再确认一次（§2.3 ③ / §4.4）。

    这是 SWR 的本义，也是 ``_read_snapshot`` 为什么读两次 ``snapshot()`` 的全部理由：
    命中那一读走带 ``fetch`` 的分支（图的是"该再确认就排一次"），**未命中**那一读走不带
    ``fetch`` 的分支（一个请求都不发）。

    "那一读没用网络"这条**不能靠请求条数说**（后台那一次很快就发出来了，两条线在同一条
    假传输上计数）——所以换个说法：**先把 NAS 那边改一个库名**，那一读回的仍然是本机
    那一份（旧名字）。这正好也是"先画一帧"的语义：命中的那一读画的是本机有的东西。

    随后不该真等 15 秒：**两把钟一起往后拨**造出"该再确认了"那一刻（与阶段 2 的假钟同一
    手法），再轮询到看见后台把新的落进快照——那之后的下一次读就是新内容（§8 B 的标题：
    "后台再验证后列表如实更新"）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        service = get_services().kb_cache
        assert service is not None, "本机档必须装配出快照服务（组合根那一段）"
        assert _revalidate(client, resource=KB_LIST)["available"] is True
        # NAS 那边改一个名字：那一读若自己去取，回来就会是新的
        nas.libraries = [_kb("kb_a", name="论文（改过名）"), _kb("kb_b", name="手册")]
        reads = len(nas.seen)
        # 两把钟一起往后拨：`checked_at` 因此"过期"，而每键那 15 秒的最短间隔也不再挡着
        monkeypatch.setattr(service, "_now", lambda: datetime.now(UTC) + timedelta(minutes=5))
        monkeypatch.setattr(service, "_clock", lambda: time.monotonic() + 3600)

        body = _snapshot(client, "/knowledge-bases")

        assert body["available"] is True and body["revalidating"] is True
        assert body["items"][0]["name"] == "论文", "命中那一读回的是**本机那一份**（没去 NAS 取）"

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # 轮询的是**快照本身**（不是请求条数）：请求发出到落库之间还有一小段
            if _snapshot(client, "/knowledge-bases")["items"][0]["name"] == "论文（改过名）":
                break
            time.sleep(0.02)
        else:  # pragma: no cover - 只会在真的没排上时走到
            pytest.fail("后台那条线程没有把新的内容落进快照")

        assert len(nas.seen) == reads + 1, "后台应当**恰好**再确认一次（单飞 + 一个线程）"
        assert nas.requests_of("/knowledge-bases"), "补的那一次是这一条资源的整取"


def test_revalidate_rejects_a_filtered_view_and_a_bad_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """再验证也要过同一道门：筛选参数换不出键（422）、按库的那几族缺 ``kb_id``（422）。

    "顺手缓存一下搜索结果"这条路必须在那一步就撞墙——键都造不出来，就谈不上再确认
    （§1.3 的第一道防线；``extra="forbid"`` 与 ``PATCH /local/provider`` 同一条纪律）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        filtered = client.post(f"{BASE}/revalidate", json={"resource": DOC_LIST, "q": "指南"})

        assert filtered.status_code == 422, filtered.text
        missing_key = client.post(f"{BASE}/revalidate", json={"resource": DOC_LIST})
        assert missing_key.status_code == 422, missing_key.text
        assert "kb_id" in missing_key.json()["message"]
        unknown = client.post(f"{BASE}/revalidate", json={"resource": "search"})
        assert unknown.status_code == 422, unknown.text
        assert "NEVER_CACHED" in unknown.json()["message"], "说得出理由在哪一份清单上"
        assert nas.seen == [], "以上三种都不该发出任何请求"


# --------------------------------------------------------------- ⑤ 清理三档


def test_delete_covers_the_three_granularities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """清理三档（§3.4-4）：按库（一次前缀清 + 两次精确清）/ 按地址 / 全清。

    按库那一档是**这一条的重点**：``doc_list`` 的视图有几十个（每页每目录一个），按前缀
    ``<kb_id>|`` 一次清掉；``kb_detail`` / ``folders`` 一个键就是它自己，两次精确清。
    别的库、别的库的详情**一行都不动**——"这一库不作数了"不是"清空了重来"。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        _prime_all(client, kb_id="kb_a")  # kb_list 1 + 每库详情 2 + 视图 2 + 目录 1 + 条目 1

        by_kb = _ok(client.delete(f"{BASE}", params={"kb_id": "kb_a"}))

        assert by_kb["removed"] == 4, "视图 2 + 库详情 1 + 目录 1"
        assert _snapshot(client, "/knowledge-bases/kb_a")["available"] is False
        assert _snapshot(client, "/knowledge-bases/kb_a/documents")["available"] is False
        assert _snapshot(client, "/knowledge-bases/kb_a/folders")["available"] is False
        assert _snapshot(client, "/knowledge-bases/kb_b")["available"] is True, "别的库不动"
        assert _snapshot(client, "/knowledge-bases")["available"] is True, "库列表不动"
        # 只清一族（写后失效时页面知道自己动了哪一族）
        assert _snapshot(client, "/documents/doc_1")["available"] is True
        one_family = _ok(client.delete(f"{BASE}", params={"kb_id": "kb_b", "resource": KB_DETAIL}))
        assert one_family["removed"] == 1

        everything = _ok(client.delete(f"{BASE}"))

        assert everything["removed"] == 2, "剩下的：库列表 1 + 文档条目 1"
        assert _snapshot(client, "/knowledge-bases")["available"] is False
        assert _snapshot(client, "/documents/doc_1")["available"] is False
        assert _ok(client.delete(f"{BASE}"))["removed"] == 0, "重复清一次是 0，不是错误"


def test_delete_by_address_leaves_the_other_nas_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``?provider=`` 只清那一个地址留下的（§5：按地址隔离、不清旧行）。

    家里的 NAS 与单位的 NAS 各留一份，切回去还能秒开——所以"清"必须指得准：清了单位那台，
    家里那份**还在**。这也顺带把"键空间的第一列是地址"这件事从端点上验了一遍。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        _revalidate(client, resource=KB_LIST)  # 家里那台：2 个库 → 1 + 2 行
        assert len(nas.seen)

        patched = client.patch("/api/v1/local/provider", json={"base_url": OTHER})
        assert patched.status_code == 200, patched.text
        assert _revalidate(client, resource=KB_LIST)["available"] is True  # 单位那台
        assert client.patch("/api/v1/local/provider", json={"base_url": ""})

        assert _snapshot(client, "/knowledge-bases")["available"] is True  # 家里那份在
        cleared = _ok(client.delete(f"{BASE}", params={"provider": OTHER}))

        assert cleared["removed"] == 3, "单位那台的库列表 1 + 每个库详情 2"
        assert _snapshot(client, "/knowledge-bases")["available"] is True, "家里那份不许动"
        assert client.patch("/api/v1/local/provider", json={"base_url": OTHER})
        assert _snapshot(client, "/knowledge-bases")["available"] is False, "单位那份清掉了"
        # 服务器档那一节用的那个地址归一化：带尾斜杠的地址说的是同一片键空间
        assert _revalidate(client, resource=KB_LIST)["available"] is True
        assert _ok(client.delete(f"{BASE}", params={"provider": f"{OTHER}/"}))["removed"] == 3


def test_delete_refuses_a_combination_that_has_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """说不出"清哪一片"的组合一律 422（按库清只在**当前地址**上，见端点说明）。

    ``provider`` 与 ``kb_id`` 一起给 = "清这个库在那个地址上的"——而按库清走的是当前地址
    那片键空间，两个条件是冲突的；``resource`` 单独给 = 按族清但没说是哪个库。
    两种都当场拒掉：静默按其中一种执行，比报错糟得多。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        both = client.delete(f"{BASE}", params={"provider": NAS, "kb_id": "kb_a"})
        alone = client.delete(f"{BASE}", params={"resource": DOC_LIST})
        wrong_family = client.delete(f"{BASE}", params={"kb_id": "kb_a", "resource": DOCUMENT})

        assert both.status_code == 422, both.text
        assert alone.status_code == 422, alone.text
        assert wrong_family.status_code == 422, wrong_family.text
        assert "document" in wrong_family.json()["message"]


# --------------------------------------------------------------- ⑥ 用量读数


def test_stats_reports_what_is_kept_without_asking_the_nas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``GET /stats``：几项 / 多少字节 / 最旧最新那份是什么时候看到的（M4 阶段 6）。

    设置面板那一块（「本机留了一份」+ 行数 + 最近更新 + 两颗按钮）读的就是它，所以三条
    判据都要在：① **零网络**（报数不打 NAS，它是"本机留了多少"这个问题）；② 数字跟得上
    实际留的东西（留七项就报七项，且最新那个时间戳就是刚写进去的）；③ **只算当前地址**
    ——换过地址之后旧地址的行还在库里（§5 按地址隔离、不清旧行），但它们不是"现在连的
    这台 NAS"留的，报数不该把它们算进来。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        empty = _ok(client.get(f"{BASE}/stats"))

        assert empty["rows"] == 0, "还没看过任何东西"
        assert empty["payload_bytes"] == 0
        assert empty["oldest_fetched_at"] is None and empty["newest_fetched_at"] is None
        assert nas.seen == [], "报数不该打 NAS（这条读只碰本机那张表）"

        _prime_all(client, kb_id="kb_a")  # 库列表 1 + 每库详情 2 + 视图 2 + 目录 1 + 条目 1
        reads = len(nas.seen)

        stats = _ok(client.get(f"{BASE}/stats"))

        assert stats["rows"] == 7
        assert stats["payload_bytes"] > 0, "字节数用的是与淘汰同一把尺子（CAST AS BLOB）"
        assert stats["newest_fetched_at"] and stats["oldest_fetched_at"]
        assert len(nas.seen) == reads, "这一条一个请求都不发"

        # 换一个地址：那一片还没看过东西，于是报 0——旧地址那七行**还在**（按地址隔离）
        assert client.patch("/api/v1/local/provider", json={"base_url": OTHER}).status_code == 200
        assert _ok(client.get(f"{BASE}/stats"))["rows"] == 0
        assert client.patch("/api/v1/local/provider", json={"base_url": ""}).status_code == 200
        assert _ok(client.get(f"{BASE}/stats"))["rows"] == 7, "切回来还是那七项"


# --------------------------------------------------------------- ⑦ 服务器档


def test_the_server_deployment_does_not_serve_the_snapshot_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务器档**一条都不挂**（R8）：那一档没有"抄一份 NAS 快照"这条动作。

    判据走**建出来的应用**（不是读内存里的 router 对象）：本机档那几条路径在服务器档那张
    全量表里根本不存在，客户端打过去就是 **404**——与 ``/local/provider`` 同一条处置
    （``provider.ts`` 的 ``unsupported``）。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "server")
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    from app.main import create_app

    app = create_app()
    try:
        # 不建 PG 连接：直接用 TestClient 发一条请求（lifespan 只在那两个"没有快照"的用例里用得到）
        client = TestClient(app)
        assert client.get(f"{BASE}/knowledge-bases").status_code == 404
        assert client.get(f"{BASE}/knowledge-bases/kb_a/documents").status_code == 404
        assert client.get(f"{BASE}/stats").status_code == 404
        assert client.post(f"{BASE}/revalidate", json={"resource": KB_LIST}).status_code == 404
        assert client.delete(BASE).status_code == 404
    finally:
        reset_services()
        reset_stores()
        get_settings.cache_clear()
