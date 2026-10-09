"""``/local/provider``（判定源 + 那两个运行期键）的整条链（M3 §7 阶段 5）。

这条用例要回答的是"**界面问一句「知识库能用吗」会得到什么**"：本机档的真 app
（`app.main.create_app`，本机档挂的就是 `local_router` 那张白名单）+ 假 NAS
（``httpx.MockTransport``，绝不打真网络）+ 真的本机库（SQLite，落在 ``tmp_path``）。
**不需要 PostgreSQL**（`local` 这个 marker 的全部意义）。

判据分四层，逐一对上方案：

① **三态与原因**（§3.1）：没配 → ``unconfigured``；配了但连不上 / 地址指错 →
   ``unavailable`` + 一句带下一步的原因；假 NAS 握手成功 → ``ready`` + 能力集 + 库清单；
② **响应键集合逐字段对得上 §3.1**（不 ready 六键、ready 再加五段，且不 ready 时那五段
   **根本不在**响应里——回空对象就会逼界面去猜"没探到还是真没有"）；
③ **PATCH 两个键**（§4.1）：白名单只有 ``base_url`` / ``enabled``，未知键与**凭据类键**
   一律 422；写完**立刻重探**并把最新状态整个回给前端（§3.4 的"保存后立刻重渲染"）；
④ **R3**：凭据（``KYLAB_TOKEN`` 那把钥匙）不落本机库、不进日志、不回显——它只从引导级来。

假 NAS 的用法是"**换掉对面现在怎么了**"（``FakeNas.serve``）：连不上 / 404（地址指错）/
200（真握手）。``seen`` 记下每一次请求——"PATCH 之后立刻重探"与"``refresh=1`` 强制重探"
这两条判据都靠请求条数说话，而不是靠"看起来对"。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.services import get_services, reset_services
from app.core.storage import LOCAL_DB_NAME, reset_stores
from app.services.knowledge_provider import (
    PROTOCOL_VERSION,
    SETTING_BASE_URL,
    SETTING_ENABLED,
    STATE_READY,
    STATE_UNAVAILABLE,
    STATE_UNCONFIGURED,
    KnowledgeProviderClient,
)

pytestmark = pytest.mark.local

NAS = "http://nas.test/api/v1"
OTHER = "http://other-nas.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_must_not_leak"

#: 方案 §3.1 的两组键：六键常驻 + 五段只有 ready 时在。**这就是契约本身**，
#: 所以照那一节逐字抄一遍（而不是从响应里反推）。
PUBLIC_KEYS = {"state", "available", "reason", "checked_at", "base_url", "credential"}
READY_ONLY_KEYS = {
    "protocol_version",
    "app_version",
    "capabilities",
    "caller",
    "knowledge_bases",
}


def _handshake_body() -> dict[str, Any]:
    """一个**过得去**的握手响应（八键照方案 §1.2；能力集与库清单填满，界面上要用的）。"""
    return {
        "provider": "knowledge",
        "protocol_version": PROTOCOL_VERSION,
        "app_version": "0.1.1",
        "api_version": "v1",
        "capabilities": {
            "retrieval": {"modes": ["hybrid", "vector", "fulltext"], "top_k_max": 100},
            "ingest": {"transport": "multipart", "max_bytes": 209715200},
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
            },
            {
                "id": "kb_b",
                "name": "手册",
                "document_count": 0,
                "can_write": True,
                "embedding_model_id": "bge-m3",
                "embedding_dim": 1024,
                "wiki_enabled": False,
            },
        ],
        "server_time": "2026-10-03T09:00:00Z",
    }


class FakeNas:
    """一台**假 NAS**：``serve`` 换掉"对面现在怎么了"，``seen`` 记下每一次请求。

    三种形态就是这一节要分的三档：``refused``（连不上 → 不可用）、``not_a_provider``
    （404：地址指到了别的服务 → 不可用，但原因不同）、``ok``（真握手 → ready）。
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self._handler: Callable[[httpx.Request], httpx.Response] = self.ok
        self.transport = httpx.MockTransport(self._dispatch)

    # ---------------------------------------------------------------- 三种形态
    @staticmethod
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_handshake_body())

    @staticmethod
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("NAS 连不上（这条用例要的就是这个）")

    @staticmethod
    def not_a_provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"no such route")

    # ------------------------------------------------------------------ 操作
    def serve(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def only(self, host: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        """只对某一台**主机**这样答（用来验"改了地址之后真的打到新地址"）。

        别的地址一律 404 —— 于是"地址没改成功"这件事会**如实**表现成不可用，
        而不是被一个到处都答 200 的假 NAS 掩盖过去。
        """

        def dispatch(request: httpx.Request) -> httpx.Response:
            if request.url.host == host:
                return handler(request)
            return httpx.Response(404, content=b"not this nas")

        self.serve(dispatch)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self._handler(request)


@pytest.fixture
def nas() -> FakeNas:
    """假 NAS（**用例里一次真网络都不打**）。"""
    return FakeNas()


@contextlib.contextmanager
def _local_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_url: str,
    token: str = TOKEN,
) -> Iterator[TestClient]:
    """本机档的真 app（`local_router` 那张白名单就挂在这个进程上）。

    ``KYLAB_KB_URL`` / ``KYLAB_KB_TOKEN`` 显式置空：它们是**覆盖**，而开发机的 ``.env``
    里可能真配了——用例不看 ``.env``（与 `test_knowledge_provider._settings` 同一手法）。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    # 壳那两件（权威）：连哪台 NAS、用哪把钥匙。提供者默认继承它们（§4.2）
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

    接的是 ``_transport``（真客户端的注入接缝，`test_client_seams` / 阶段 2 同一手法）：
    跑的仍是真客户端代码——鉴权头、超时、状态机、缓存，全都是它发出来的。
    """
    provider = get_services().provider
    assert provider is not None, "本机档必须装配出提供者客户端（组合根那一段）"
    monkeypatch.setattr(provider, "_transport", nas.transport)
    return provider


def _app_settings(tmp_path: Path) -> dict[str, str]:
    """**直接开本机库**读 ``app_settings``（用例不适用 L2 那道分层纪律）。

    "值真的落库了"与"凭据没落库"这两句话都必须**在文件里**成立，所以读的是那张表，
    而不是服务对象上的一份快照。
    """
    database = tmp_path / "data" / LOCAL_DB_NAME
    assert database.is_file(), database
    with sqlite3.connect(database) as conn:
        rows = conn.execute("select key, value from app_settings")
        return {str(key): str(value) for key, value in rows}


def _get(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/local/provider", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ ① 三态


def test_without_an_address_it_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """壳里没配 NAS、``kb_url`` 也空 → ``unconfigured``，而且**一次都不探**。

    "没配"与"配了但连不上"是两句不同的话：前者的下一步是"去配一个"，后者是"去看看它
    怎么了"。而没配时连探测都不该发（R1：不在交互路径上等一次注定失败的往返）。
    """
    with _local_app(tmp_path, monkeypatch, server_url="", token="") as client:
        _attach(nas, monkeypatch)

        body = _get(client)

        assert body["state"] == STATE_UNCONFIGURED
        assert body["available"] is False
        assert body["base_url"] == ""
        assert body["credential"] == "missing"
        # 有人话，也有下一步（方案 §3.1 对两种"不在"的要求）
        assert "知识库" in body["reason"] and "「知识库连接」" in body["reason"]
        assert nas.seen == [], "没配地址就不该发探测（一次都不该）"
        assert set(body) == PUBLIC_KEYS
        assert datetime.fromisoformat(body["checked_at"])


def test_a_wrong_address_is_unavailable_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """地址指到了一个**不是提供者**的地方（404）→ ``unavailable``，原因说得清是哪个地址。"""
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)
        nas.serve(FakeNas.not_a_provider)

        body = _get(client)

        assert body["state"] == STATE_UNAVAILABLE
        assert body["available"] is False
        assert body["base_url"] == NAS  # 解析后的实际地址照实给你（它不是秘密）
        assert body["credential"] == "configured"  # 凭据只看有没有，不回显
        assert "404" in body["reason"] and "握手端点" in body["reason"]
        assert "下一步" in body["reason"]
        assert nas.seen[-1].url.path.endswith("/provider/handshake")
        assert set(body) == PUBLIC_KEYS


def test_a_nas_that_cannot_be_reached_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """连不上（``ConnectError``）→ 也是 ``unavailable``，但原因是"连不上"这一档。"""
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)
        nas.serve(FakeNas.refused)

        body = _get(client)

        assert body["state"] == STATE_UNAVAILABLE
        assert "连不上" in body["reason"] and NAS in body["reason"]
        assert set(body) == PUBLIC_KEYS


def test_a_real_handshake_is_ready_with_capabilities_and_bases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """假 NAS 握手成功 → ``ready`` + 五段（协议版本 / 能力集 / 调用者 / 库清单）。"""
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)

        body = _get(client)

        assert body["state"] == STATE_READY
        assert body["available"] is True and body["reason"] == ""
        assert body["base_url"] == NAS and body["credential"] == "configured"
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert body["app_version"] == "0.1.1"
        assert body["capabilities"]["ingest"]["max_bytes"] == 209715200
        assert body["capabilities"]["retrieval"]["modes"] == ["hybrid", "vector", "fulltext"]
        # 调用者段如实带出来（页面用会话、边车用钥匙，这把钥匙在 NAS 侧不是管理员）
        assert body["caller"]["is_admin"] is False and body["caller"]["can_write"] is True
        assert [item["id"] for item in body["knowledge_bases"]] == ["kb_a", "kb_b"]
        assert body["knowledge_bases"][0]["document_count"] == 3
        assert set(body) == PUBLIC_KEYS | READY_ONLY_KEYS
        # 凭据真的带着走了（真客户端发的），但它**只在请求头里**、不进响应体
        assert nas.seen[-1].headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in json.dumps(body)


# ------------------------------------------------------------- ③ 那两个运行期键


def test_patching_the_base_url_lands_in_the_local_db_and_takes_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``PATCH base_url``：落本机库 ``app_settings`` → 立刻重探 → 下一次 GET 就是新结论。

    这一条是设置面板「保存」那一下的全部依据（§3.4）：写完**不等 30s、不重启边车**，
    页面按返回的状态重渲染（方案 §4.2 的"每次调用现取目标"）。
    """
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)
        # 壳里那台不通，另一台通：于是"地址改成了没有"这件事在结果里看得出来
        nas.only("other-nas.test", FakeNas.ok)
        assert _get(client)["state"] == STATE_UNAVAILABLE
        probes = len(nas.seen)

        patched = client.patch("/api/v1/local/provider", json={"base_url": OTHER})

        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["state"] == STATE_READY, body
        assert body["base_url"] == OTHER
        assert set(body) == PUBLIC_KEYS | READY_ONLY_KEYS
        assert len(nas.seen) == probes + 1, "写完必须**强制重探一次**（不是等下一次询问）"
        assert nas.seen[-1].url.host == "other-nas.test"

        # 值真的落进了本机库（不是只改了内存里的一个快照）
        stored = _app_settings(tmp_path)
        assert stored[SETTING_BASE_URL] == OTHER

        # **同一进程里下一次 GET 即生效**：拿到的就是新地址的结论，而且没再探一次
        # （探是在 PATCH 那一下做的，这就是"那份 30s 缓存"的意义）
        again = _get(client)
        assert again["state"] == STATE_READY and again["base_url"] == OTHER
        assert len(nas.seen) == probes + 1

        # 空串 = **清掉覆盖、回继承**（面板上的「恢复默认」）：又回到壳里那台（不通）
        cleared = client.patch("/api/v1/local/provider", json={"base_url": ""})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["state"] == STATE_UNAVAILABLE
        assert cleared.json()["base_url"] == NAS
        assert _app_settings(tmp_path)[SETTING_BASE_URL] == ""


def test_patching_enabled_turns_the_provider_off_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``PATCH enabled``：关掉 → ``unconfigured`` + **"被关掉了"那句**（不是"没填地址"）。

    两种成因各一句是有意的（`DISABLED_REASON` / `UNCONFIGURED_REASON`）：下一步不一样
    ——一个是"打开它"，一个是"填一个地址"。
    """
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)
        assert _get(client)["state"] == STATE_READY

        off = client.patch("/api/v1/local/provider", json={"enabled": False})

        assert off.status_code == 200, off.text
        assert off.json()["state"] == STATE_UNCONFIGURED
        assert "被关掉" in off.json()["reason"]
        assert _app_settings(tmp_path)[SETTING_ENABLED] == "0"
        # 关掉之后**连探测都不发**：关就是关，不用去猜它本来会连哪儿
        probes = len(nas.seen)
        assert _get(client)["state"] == STATE_UNCONFIGURED
        assert len(nas.seen) == probes

        on = client.patch("/api/v1/local/provider", json={"enabled": True})

        assert on.status_code == 200, on.text
        assert on.json()["state"] == STATE_READY
        assert _app_settings(tmp_path)[SETTING_ENABLED] == "1"


@pytest.mark.parametrize(
    "key",
    [
        "token",  # 壳里那把长期钥匙
        "kb_token",  # 提供者的凭据覆盖
        "api_key",
        "server_url",  # 权威那一处归壳，不归本机库
        "provider.knowledge.base_url",  # 写对了但用错了键名（白名单是两个短名）
    ],
)
def test_patch_rejects_unknown_and_credential_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas, key: str
) -> None:
    """未知键与**凭据类键**一律 422，而且一个字节都不写进库（§4.1 / R3）。

    "凭据类键拒掉"不是靠端点里另列一份黑名单，而是靠白名单本身（``extra="forbid"``）
    ——多一处黑名单就多一处会漂的清单。
    """
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)

        response = client.patch("/api/v1/local/provider", json={key: "x"})

        assert response.status_code == 422, response.text
        envelope = response.json()
        assert envelope["code"] == "invalid_request"
        assert key in envelope["message"], envelope  # 哪个键被拒要说得出
        # 库里没有它（settings 那一族键一个都没被写进去）
        assert key not in _app_settings(tmp_path)


def test_patch_with_an_empty_body_only_reprobes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """空 body = 不改动，**只重探一次**（等价于 ``refresh=1``）——不是错误。"""
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)
        _get(client)
        probes = len(nas.seen)
        # 判据是"PATCH 前后**没有多出任何键**"，而不是"库里一条都没有"：
        # 本机档组合根自己会在装配时补一条 `auth.url_signing_secret`
        # （见 `services/auth.ensure_url_signing_secret`），那不是这个 PATCH 写的。
        before = _app_settings(tmp_path)

        response = client.patch("/api/v1/local/provider", json={})

        assert response.status_code == 200, response.text
        assert response.json()["state"] == STATE_READY
        assert len(nas.seen) == probes + 1
        assert _app_settings(tmp_path) == before, "空 body 不该写任何键"


# --------------------------------------------------- ④ refresh / 缓存 / R3


def test_refresh_forces_a_new_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``refresh=1`` 强制重探；不 refresh 时那 30s 的缓存**真的在挡住往返**（R1）。

    三条一起验：① 探测结论进缓存；② TTL 内不重探（请求条数不动）；③ ``refresh=1``
    立刻重探并把 NAS 回来了这件事如实报出来（"恢复了要能自己回来"，§3.2）。
    """
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        _attach(nas, monkeypatch)
        nas.serve(FakeNas.refused)
        assert _get(client)["state"] == STATE_UNAVAILABLE
        assert len(nas.seen) == 1

        # NAS 回来了，但缓存还在：不 refresh 就**不重探**（"一次检索超时不该让导航消失"
        # 的另一面——结论只由握手探测改写，而它按 30s 的节奏走）
        nas.serve(FakeNas.ok)
        assert _get(client)["state"] == STATE_UNAVAILABLE
        assert len(nas.seen) == 1, "TTL 内不该重探"

        fresh = _get(client, refresh=1)

        assert fresh["state"] == STATE_READY
        assert len(nas.seen) == 2
        # 重探的结论也进了缓存（下一次不 refresh 就是它）
        assert _get(client)["state"] == STATE_READY


def test_the_credential_never_lands_in_the_local_db_or_the_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nas: FakeNas,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """凭据**不落库、不进日志、不回显**（R3）——它只从引导级（壳 / 环境变量）来。

    这一条守的是 R3 那条纪律的**可观察结果**：``PATCH`` 能改地址（白名单里允许），
    但库里永远不会出现"token / api_key"这类键；响应里只有 ``configured`` / ``missing``
    这两个词；日志里没有明文（那把钥匙只在 ``Authorization`` 头里，而请求头不进日志）。
    """
    with _local_app(tmp_path, monkeypatch, server_url=NAS) as client:
        provider = _attach(nas, monkeypatch)

        with caplog.at_level(logging.DEBUG):
            body = _get(client)
            client.patch("/api/v1/local/provider", json={"base_url": OTHER, "enabled": True})

        stored = _app_settings(tmp_path)
        assert body["credential"] == "configured"  # 只说"配了"，不回显
        assert TOKEN not in json.dumps(body)
        # 库里只有白名单那两个键：任何含 token / key / secret 的键都不该出现
        assert not [key for key in stored if {"token", "key", "secret"} & set(key.split("."))]
        assert TOKEN not in json.dumps(stored)
        assert TOKEN not in caplog.text
        # 地址改了（白名单里允许改的那一项），钥匙没动：它仍从引导级来
        assert stored[SETTING_BASE_URL] == OTHER
        assert provider.target().token == TOKEN
