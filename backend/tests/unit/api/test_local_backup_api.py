"""``/local/backup`` 那一族的整条链（M5 阶段 4A / 5，方案 §7 A 的前两行 + R12）。

这条用例回答的是"**界面上按一下「立即备份」会得到什么**"：本机档的真 app
（``app.main.create_app``，本机档挂的就是 ``local_router`` 那张白名单）+ 假 NAS
（``httpx.MockTransport``，绝不打真网络）+ 真的本机库与真的打包器（SQLite 落在
``tmp_path``）。**不需要 PostgreSQL**（``local`` 这个 marker 的全部意义）。

覆盖：

① **断网也能打**（§7 A 第一行）：传输全失败 → 202 + 队列多一行 + ``last_error`` 有原因 +
   包落在 ``backup/pending/``。那一行的状态是 ``failed``（端点在请求线程里顺手试了一次），
   而**入队那一步是 ``pending``**——两档都算入队成功（阶段 3 定案的口径）；
② **连不上也要给本机那一半**（§7 A 第二行）：``GET /local/backup`` 的 ``provider`` 说
   ``unavailable`` + 原因，而 ``backlog`` / ``snapshots`` 如实报队里那几份；
③ **联网后补传**：把地址改好（或传输修好）之后跑一轮队列节拍 → 那一行变 ``uploaded``、
   本地包删掉（§7 A 第三行的端到端那一半由 ``test_backup_queue.py`` 逐格钉）；
④ **五个端点的错误映射**：没有设备身份 → 400 + 那句人话（R12）；PATCH 白名单四键
   （凭据类键与未知键 422）；恢复点 404 如实回；删不掉（连不上）→ 503 而不是 500；
⑤ **恢复点清单是透传**：ready 时 ``items`` / ``total`` / ``quota`` 原样给，且**30s 缓存**
   （两次 GET 只打一次 NAS）；
⑥ **这一族只挂在本机档**（服务器档那一档自己就是备份的目的地）；
⑦ **按点恢复那一端**（阶段 5）：dry-run 同步回报告且一个字节不写、真恢复 202 + 批次 id
   并能用**既有**的 ``GET /local/import/{id}`` 轮询、``extra="forbid"``、连不上 → 502。

逐格的恢复判据（会话合并规则、记忆 / 设置 / 产物落位、五条安全栏）在
``tests/unit/services/test_backup_restore.py``；这一份只在"界面上按一下"那一层钉一遍。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.services import get_services, reset_services
from app.core.storage import get_stores, reset_stores
from app.services import backup_provider, runtime_config
from app.services.backup_provider import PROTOCOL_VERSION

pytestmark = pytest.mark.local

NAS = "http://nas.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_must_not_leak"
DEVICE = "3f1c8b2e-0a4d-4a77-9d55-2c6a1b7e9f01"
BASE = "/api/v1/local/backup"
SEGMENT = "2026-10-05T08-03-00Z-ab12cd34"


def _snapshot_item() -> dict[str, Any]:
    """NAS 侧一份恢复点的形状（``BackupSnapshotOut``，只挑界面要的几列）。"""
    return {
        "snapshot_id": SEGMENT,
        "device_id": DEVICE,
        "device_name": "小又的笔记本",
        "created_at": "2026-10-05T08:03:00Z",
        "bytes": 11477,
        "sha256": "a" * 64,
        "kind": "manual",
        "schema_version": 3,
        "app_version": "0.1.1",
        "counts": {"conversations": 2, "messages": 9},
        "skipped": [],
        "encryption": "none",
    }


class FakeNas:
    """一台**假 NAS**：按路径回答 + 记下每一次请求（与 ``test_local_kb_cache_api`` 同一手法）。

    ``fail`` 一置就是"连不上"（每一发都抛 ``ConnectError``）——那正是"断网也能打快照"
    那两条用例要的那一档。``seen`` 是"缓存有没有省下请求"的唯一判据。
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.fail: Exception | None = None
        self.delete_status = 200
        self.package: bytes | None = None
        """那一份恢复点的**包体**（阶段 5 的按点恢复下它；没给就是"这条路径上没有"。）"""
        self.manifest_doc: dict[str, Any] | None = None
        """那一份恢复点的**清单**（同上）。"""
        self.list_payload: dict[str, Any] = {
            "items": [_snapshot_item()],
            "total": 1,
            "quota": {
                "policy": "keep_n",
                "keep": 30,
                "quota_bytes": 10,
                "used_bytes": 11477,
                "snapshots": 1,
            },
        }
        self.transport = httpx.MockTransport(self._dispatch)

    # ---------------------------------------------------------------- 假 NAS 的答法
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            raise self.fail
        path = request.url.path
        if path.endswith("/backup/handshake"):
            return httpx.Response(
                200,
                json={
                    "provider": "backup",
                    "protocol_version": PROTOCOL_VERSION,
                    "app_version": "0.1.1",
                    "api_version": "v1",
                    "capabilities": {
                        "snapshot": {"available": True, "max_blob_bytes": 2147483648},
                        "retention": {"keep": 30, "quota_bytes": 10, "used_bytes": 11477},
                    },
                    "caller": {"kind": "api_key", "is_admin": False},
                    "devices": [{"device_id": DEVICE, "snapshots": 1, "bytes": 11477}],
                    "server_time": "2026-10-05T08:00:00Z",
                },
            )
        if path.endswith("/backup/snapshots"):
            return httpx.Response(200, json=self.list_payload)
        if path.endswith("/blob") and request.method == "PUT":
            return httpx.Response(
                201,
                json={
                    "snapshot_id": SEGMENT,
                    "bytes": len(request.read()),
                    "sha256": hashlib.sha256(request.read()).hexdigest(),
                },
            )
        if path.endswith("/manifest") and request.method == "PUT":
            return httpx.Response(201, json={"snapshot_id": SEGMENT, "bytes": 1, "sha256": "x"})
        if path.endswith("/blob") and request.method == "GET":
            # 按点恢复取包体（``download_snapshot`` 会核对这个头；不符就中止）
            if self.package is None:
                return httpx.Response(404, text="这条路径上没有快照体")
            return httpx.Response(
                200,
                content=self.package,
                headers={"X-Kylab-Sha256": hashlib.sha256(self.package).hexdigest()},
            )
        if request.method == "GET" and "/backup/snapshots/" in path:
            # 按点恢复先取清单（几 KB）：格式 / 版本判据与"包里有什么"都靠它。
            # 这一支匹配的是**明细路径**（列表那条在上面已经答过了）。
            if self.manifest_doc is None:
                return httpx.Response(404, text="这条路径上没有清单")
            return httpx.Response(200, json=self.manifest_doc)
        if request.method == "DELETE":
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status, text="这条路径上没有恢复点可删")
            return httpx.Response(self.delete_status, json={"removed": 2})
        return httpx.Response(404, text="没有这个端点")

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.handler(request)

    def list_calls(self) -> int:
        return sum(1 for request in self.seen if request.url.path.endswith("/backup/snapshots"))


@pytest.fixture
def nas() -> FakeNas:
    return FakeNas()


@contextlib.contextmanager
def _local_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_url: str = NAS,
    token: str = TOKEN,
    device_id: str = DEVICE,
) -> Iterator[TestClient]:
    """本机档的真 app（``local_router`` 那张白名单就挂在这个进程上）。

    那几个环境变量显式置空/置值：``.env`` 里真有可能配着别的东西，而用例不该看它
    （与 ``test_local_kb_cache_api._local_app`` 同一手法）。
    """
    monkeypatch.setenv("KYLAB_DEPLOYMENT", "local")
    monkeypatch.setenv("KYLAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYLAB_DATABASE_URL", "")
    monkeypatch.setenv("KYLAB_SERVER_URL", server_url)
    monkeypatch.setenv("KYLAB_TOKEN", token)
    monkeypatch.setenv("KYLAB_KB_URL", "")
    monkeypatch.setenv("KYLAB_KB_TOKEN", "")
    monkeypatch.setenv("KYLAB_DEVICE_ID", device_id)
    # **把自动快照关掉**（只在这一族用例里）：组合根起来时队列线程会立刻跑一轮，
    # 而"从没有过备份"按判据是到期——于是它会和下面手动那一份抢同一个 pending 目录，
    # 让"目录里只有我这一份"这类断言时红时绿。自动快照那几条判据在
    # `tests/unit/services/test_backup_queue.py` 里逐格钉着，这里不需要它出场。
    #
    # 关的是**默认值那一格**（`every_hours` 的回落链是"库里的键 → 代码默认值"）：
    # 只改这一个字典就够，键名与白名单一个字不动——于是 PATCH 那几条用例仍然在
    # 真的那个键上验（先前改过一次键名，那会让"PATCH 写进去了吗"当场问错地方）。
    monkeypatch.setitem(runtime_config.DEFAULTS, "provider.backup.every_hours", "0")
    get_settings.cache_clear()
    reset_services()
    reset_stores()
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        # reset_services 会把那一个 backup-upload 守护线程停掉（它按节拍碰数据目录）
        reset_services()
        reset_stores()
        get_settings.cache_clear()


def _attach(nas: FakeNas, monkeypatch: pytest.MonkeyPatch) -> backup_provider.BackupProviderClient:
    """把假 NAS 接到**进程级那一个**提供者客户端上（真客户端代码、假传输）。"""
    provider = get_services().backup_provider
    assert provider is not None, "本机档必须装配出备份提供者客户端（组合根那一段）"
    monkeypatch.setattr(provider, "_transport", nas.transport)
    return provider


def _take_snapshot(client: TestClient) -> dict[str, Any]:
    response = client.post(f"{BASE}/snapshots")
    assert response.status_code == 202, response.text
    return response.json()


# ------------------------------------------------------------------ ① 断网也能打


def test_a_snapshot_can_be_taken_while_the_transport_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """§7 A 第一行：传输全失败 → **202** + 队列多一行 + 原因可见 + 包落在 pending 里。

    那一行落到 ``failed`` 是"端点顺手试了一次"的结果（阶段 3 定案：``pending`` =
    还没试过、``failed`` = 试过且失败，两档都算入队成功）。而**包确实在盘上**——
    这正是"断网也不丢"的全部意义。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")

        body = _take_snapshot(client)

        row = body["snapshot"]
        assert row["state"] == "failed", "试过一次、失败了——那也是入队成功"
        assert row["kind"] == "manual"
        assert "连不上" in row["last_error"]
        assert row["attempts"] == 1 and row["next_attempt_at"]
        assert body["backlog"]["queued"] == 1 and body["backlog"]["bytes"] > 0

        pending = Path(get_settings().data_dir) / "backup" / "pending"
        assert [item.name for item in pending.iterdir()] == [f"{row['id']}.tar.gz"]

        # 队列那一读看得到同一行（一个来源）
        listed = client.get(BASE).json()
        assert [item["id"] for item in listed["snapshots"]] == [row["id"]]
        assert listed["backlog"]["queued"] == 1


def test_the_local_half_is_served_when_the_provider_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """§7 A 第二行：``state=unavailable`` **且**队列计数如实（"备份是本地动作"）。

    提供者那一段是"连不上 + 原因"，本机那一段是"有一份还没传上去"——两件事互不掩盖。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")
        _take_snapshot(client)

        body = client.get(BASE).json()

        assert body["provider"]["state"] == "unavailable"
        assert "连不上" in body["provider"]["reason"]
        assert body["provider"]["available"] is False
        assert body["backlog"]["queued"] == 1 and body["backlog"]["failed"] == 1
        assert len(body["snapshots"]) == 1
        assert "capabilities" not in body["provider"], (
            "不 ready 时那几段键都不出现（回空对象会让界面去猜）"
        )


def test_the_snapshot_uploads_after_the_network_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """§7 A 第三行的端到端那一半：断网入队 → 网络回来 → 下一轮节拍就传上去。

    （逐格那几张表在 ``test_backup_queue.py``；这里是"两个组合根对象真的接在一起了"。）
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        stores = get_stores()
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")
        row = _take_snapshot(client)["snapshot"]
        pending = Path(get_settings().data_dir) / "backup" / "pending"
        assert (pending / f"{row['id']}.tar.gz").exists()

        nas.fail = None  # 网络回来了
        services = get_services()
        assert services.backup_queue is not None
        # 第一次失败已经排了 60 秒退避——把"下一次"清掉，等价于"刚好 60 秒过去了"
        # （真等 60 秒不是这条用例要证明的事；退避那几档由 test_backup_queue.py 逐格钉）
        assert stores.backup_queue is not None
        stores.backup_queue.mark_backup_snapshot(row["id"], "pending", clear_next_attempt=True)
        report = services.backup_queue.sweep()

        assert report.uploaded == 1
        assert not (pending / f"{row['id']}.tar.gz").exists(), "传成之后本地那份包要删掉"
        after = client.get(BASE).json()
        assert after["snapshots"][0]["state"] == "uploaded"
        assert after["snapshots"][0]["remote_device_id"] == DEVICE
        assert after["backlog"]["queued"] == 0


# ------------------------------------------------------------------ ② PATCH 白名单


def test_patch_writes_the_four_whitelisted_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """四个键逐个落进 ``app_settings``，并且写完**立刻重探**（这一下就回最新整包）。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        body = client.patch(
            f"{BASE}",
            json={
                "base_url": NAS,
                "enabled": True,
                "include_workspace": True,
                "every_hours": 6,
            },
        ).json()

        runtime = get_services().runtime
        assert runtime.get("provider.backup.base_url") == NAS
        assert runtime.get("provider.backup.enabled") == "1"
        assert runtime.get("provider.backup.include_workspace") == "1"
        assert runtime.get("provider.backup.every_hours") == "6"
        assert body["provider"]["state"] == "ready"
        assert body["provider"]["capabilities"]["snapshot"]["available"] is True


def test_patch_refuses_credentials_and_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """R3/R14：**凭据类键一个都不收**（``extra="forbid"``），未知键与非法值同样 422。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        for payload in (
            {"token": "kylab_sk_x"},
            {"api_key": "x"},
            {"whatever": 1},
            {"every_hours": -1},
            {"enabled": "也许"},
        ):
            response = client.patch(f"{BASE}", json=payload)
            assert response.status_code == 422, f"{payload} 该被 422 挡住"

        runtime = get_services().runtime
        assert runtime.get("provider.backup.enabled") in ("", "1", "0")
        assert "kylab_sk_x" not in json.dumps(client.get(BASE).json(), ensure_ascii=False), (
            "凭据不会出现在响应里"
        )


def test_patch_can_turn_the_provider_off_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``enabled=false`` → ``unconfigured`` + 那句"关掉只影响传不传得出去"。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        body = client.patch(f"{BASE}", json={"enabled": False}).json()

        assert body["provider"]["state"] == "unconfigured"
        assert "被关掉了" in body["provider"]["reason"]
        assert "本机打快照" in body["provider"]["reason"]


# ------------------------------------------------------------------ ③ 设备身份（R12）


def test_a_missing_device_identity_refuses_with_that_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """R12：壳没传 ``--device-id`` → **400 + 那句人话**，绝不编一个 id。"""
    with _local_app(tmp_path, monkeypatch, device_id="") as client:
        _attach(nas, monkeypatch)

        response = client.post(f"{BASE}/snapshots")

        assert response.status_code == 400, response.text
        body = response.json()
        assert "设备身份" in body["message"] and "登录一次" in body["message"]
        assert body["code"] == "invalid_request"
        # 没有设备身份时**一份都不该打出来**
        pending = Path(get_settings().data_dir) / "backup" / "pending"
        assert not pending.exists() or not list(pending.iterdir())


# ------------------------------------------------------------------ ④ 恢复点清单


def test_points_pass_through_the_nas_and_are_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """ready 时 ``items`` / ``total`` / ``quota`` **原样透传**，且 30s 缓存省下一次往返。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        first = client.get(f"{BASE}/points").json()
        second = client.get(f"{BASE}/points").json()

        assert first["available"] is True and first["state"] == "ready"
        assert [item["snapshot_id"] for item in first["items"]] == [SEGMENT]
        assert first["total"] == 1
        assert first["quota"]["keep"] == 30 and first["quota"]["used_bytes"] == 11477
        assert first == second
        assert nas.list_calls() == 1, "30s 内第二次读不该再打 NAS"

        refreshed = client.get(f"{BASE}/points", params={"refresh": 1}).json()
        assert refreshed["available"] is True and nas.list_calls() == 2


def test_points_report_the_state_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """连不上 → **200 + available=false + 原因**（不是 500，也不是"还没有恢复点"）。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")

        response = client.get(f"{BASE}/points")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "unavailable" and body["available"] is False
        assert "连不上" in body["reason"]
        assert body["items"] == [] and body["total"] == 0
        assert nas.list_calls() == 0, "状态已经不 ready，列表一个请求都不该发"


def test_points_say_unconfigured_before_anything_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """没配地址 → ``unconfigured`` + 那句"去哪儿填"（零请求）。"""
    with _local_app(tmp_path, monkeypatch, server_url="", token="") as client:
        _attach(nas, monkeypatch)

        body = client.get(f"{BASE}/points").json()

        assert body["state"] == "unconfigured" and body["available"] is False
        assert "provider.backup.base_url" in body["reason"] or "server" in body["reason"]
        assert nas.seen == []


# ------------------------------------------------------------------ ⑤ 删除


def test_delete_passes_through_and_reports_the_object_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """删一个恢复点：服务端回几个就报几个（整份删 = 2）。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        response = client.delete(f"{BASE}/points/{DEVICE}/{SEGMENT}")

        assert response.status_code == 200, response.text
        assert response.json() == {"removed": 2}
        assert nas.seen[-1].method == "DELETE"
        assert nas.seen[-1].url.path.endswith(f"/backup/snapshots/{DEVICE}/{SEGMENT}")


def test_delete_reports_a_missing_point_as_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """服务端 404（本来就没什么可删）→ 本机也 **404 如实回**（不伪造成功）。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        nas.delete_status = 404

        response = client.delete(f"{BASE}/points/{DEVICE}/{SEGMENT}")

        assert response.status_code == 404
        assert "没有恢复点可删" in response.json()["message"]


def test_delete_when_unreachable_is_502_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """连不上 → **502 + 原因**（"外部依赖出错"，不是 500 也不是"你请求写错了"）。

    为什么是 502 而不是知识库那条 503：那一条的映射住在 ``app.storage.base``，而
    **L1 规则禁止协议层 import 存储**（`check_layering.py` 的 L1 判据之一）。
    要一条"备份提供者不可用 → 503"，得在 ``core/exceptions.py`` 里加一类异常
    （那份文件在白名单之外，见阶段 4 报告的偏离）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")

        response = client.delete(f"{BASE}/points/{DEVICE}/{SEGMENT}")

        assert response.status_code == 502, response.text
        assert "连不上" in response.json()["message"]


# ------------------------------------------------------------------ ⑥ 装配与服务器档


def test_the_three_backup_services_are_one_process_wide_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组合根建的那三件都在，而且队列的上传者**就是**那个提供者客户端（同一个对象）。"""
    with _local_app(tmp_path, monkeypatch) as client:
        assert client.get(f"{BASE}").status_code == 200
        services = get_services()

        assert services.backup_snapshot is not None
        assert services.backup_provider is not None
        assert services.backup_queue is not None
        assert services.backup_queue is get_services().backup_queue, "进程级单例"
        assert services.backup_queue.worker is not None, "队列的线程在组合根里起好了"
        assert services.backup_queue.worker.name == "backup-upload"


def test_the_backup_family_lives_only_on_the_local_router() -> None:
    """这一族**只挂在本机档那张白名单上**（服务器档那一档自己就是备份的目的地）。

    不建 app 来判：服务器档要有 PG 才能起来，而这条判据问的是**路由归属**——把 router
    装进一个空 ``FastAPI`` 再读 OpenAPI 就够了（`api/v1/router.py` 的"不挂清单"是同一
    条纪律，那条清单一改这里就红）。
    """
    from fastapi import FastAPI

    from app.api.v1.router import api_router, local_router

    def paths_of(router: Any) -> set[str]:
        probe = FastAPI()
        probe.include_router(router, prefix="/api/v1")
        return set(probe.openapi()["paths"])

    local_paths = paths_of(local_router)
    assert {f"{BASE}", f"{BASE}/points", f"{BASE}/snapshots"} <= local_paths
    assert f"{BASE}/points/{{device_id}}/{{snapshot_id}}" in local_paths
    assert f"{BASE}/restore" in local_paths, "按点恢复（阶段 5）也只挂在本机档那张白名单上"
    assert {path for path in paths_of(api_router) if path.startswith("/api/v1/local/")} == set()


# ------------------------------------------------------------------ ⑦ 按点恢复（阶段 5）
#
# 这一节走**端到端**那条路：真包（app 自己的打包器打的）→ 假 NAS 当"那台机器" →
# 端点 → 后台线程 → 既有的 `GET /local/import/{batch}` 轮询。它要证明的是**接线对了**：
# 请求形状、dry-run 那份报告、202 + 批次 id、"失败也有一份记录"。
#
# 逐格的判据（会话怎么合并、记忆/设置/产物怎么落位、五条安全栏）在
# `tests/unit/services/test_backup_restore.py`；这里只在"界面上按一下"那一层钉一遍。


RESTORE = f"{BASE}/restore"


def _serve_the_local_package(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    nas: FakeNas,
    *,
    extra_setting: str = "llm.temperature",
) -> dict[str, Any]:
    """造一份"**那台机器上的**恢复点"：本机先有一条会话 / 一个设置 + 一份记忆文件，
    打一份真包交给假 NAS，再把本机那三样**删掉**——于是它看起来就像"另一台机器"。

    这样造现场的好处：包与清单都是**产品自己的**路径产出的（真打包器 + 真清单），
    不手写形状；而"本机没有、包里有一份"正是恢复最该被验的那一档。
    """
    from app.services.backup_snapshot import read_archive_manifest
    from app.storage.sqlite_impl.backup_archive import local_schema_version

    stores = get_stores()
    assert stores.ledger is not None
    transfer = _restore_transfer()
    stores.ledger.write_imported_conversation(transfer)
    stores.meta.set_setting(extra_setting, "0.3")
    memory = Path(get_settings().data_dir) / "memory" / "usr_owner"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "MEMORY.md").write_text("# 记忆\n要跟着恢复回来的东西。\n", encoding="utf-8")

    # 打这份包的时候**让传输断着**：这一份要留在 ``backup/pending/`` 当"那台机器上的那一份"，
    # 传出去就被删了（队列传成之后会删本地包，那条判据在 ①）。
    nas.fail = httpx.ConnectError("先不上传（这一份要留在盘上）")
    try:
        row = _take_snapshot(client)["snapshot"]
    finally:
        nas.fail = None
    package = Path(get_settings().data_dir) / "backup" / "pending" / f"{row['id']}.tar.gz"
    manifest = read_archive_manifest(package, local_schema_version=local_schema_version())
    nas.package = package.read_bytes()
    nas.manifest_doc = json.loads(manifest.to_bytes())

    stores.meta.delete_conversation(transfer.conversation.id)
    stores.meta.delete_setting(extra_setting)
    (memory / "MEMORY.md").unlink()
    return {
        "snapshot_id": row["id"],
        "conversation_id": transfer.conversation.id,
        "setting": extra_setting,
        "memory": "usr_owner/MEMORY.md",
    }


def _restore_transfer() -> Any:
    """包里那一条会话（形状照 `ConversationTransfer`，只挑恢复该保住的字段）。"""
    from datetime import UTC, datetime

    from app.storage.base import (
        ChatMessageRecord,
        ConversationRecord,
        ConversationTransfer,
        SessionEventRecord,
    )

    moment = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    return ConversationTransfer(
        conversation=ConversationRecord(
            id="conv_restore_1",
            title="要恢复回来的会话",
            kb_ids=(),
            created_at=moment,
            updated_at=moment,
        ),
        summary="压缩过的上下文",
        summary_upto=None,
        messages=[
            ChatMessageRecord(
                id="msg_restore_1",
                conversation_id="conv_restore_1",
                role="assistant",
                content="这句正文要原样回来",
                created_at=moment,
            )
        ],
        events=[
            SessionEventRecord(
                conversation_id="conv_restore_1",
                seq=1,
                kind="turn/complete",
                payload={"status": "ok"},
                created_at=moment,
            )
        ],
    )


def _await_restore(client: TestClient, batch_id: str, *, seconds: float = 20.0) -> dict[str, Any]:
    """等后台那一批**真的**跑完（轮询既有的那个端点——这本身就是"复用"的证据）。

    "跑完"的判据是 ``done`` **且**报告里有 ``restore`` 那一段：恢复这一批有两位写者
    （M2 的导入器写"会话那一段完了"，恢复器补完记忆 / 设置 / 产物再写终态），所以
    只看到 ``done`` 就返回会读到"少了恢复那一段"的中间态。
    """
    deadline = time.monotonic() + seconds
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/local/import/{batch_id}").json()
        if body["state"] == "failed" or (body["state"] == "done" and "restore" in body["counts"]):
            return body
        time.sleep(0.02)
    raise AssertionError(f"这批没在 {seconds} 秒内跑完：{body}")


def test_restore_dry_run_returns_the_report_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``dry_run=true``：**同步**回那份报告（四段 + 要重配的凭据），本机一个字节不写。

    "不写"的判据落在三件事上：那条会话仍然不在、那个设置仍然没有、台账里没有批次行。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        scene = _serve_the_local_package(client, monkeypatch, nas)
        stores = get_stores()

        response = client.post(
            RESTORE,
            json={
                "device_id": DEVICE,
                "snapshot_id": scene["snapshot_id"],
                "dry_run": True,
            },
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["dry_run"] is True and body["batch_id"] == ""
        plan = body["plan"]
        assert [item["conversation_id"] for item in plan["created"]] == [scene["conversation_id"]]
        assert plan["skipped"] == [] and plan["replaced"] == []
        assert plan["counts"] == {"created": 1, "replaced": 0, "skipped": 0}
        assert plan["package"]["counts"]["conversations"] == 1
        assert scene["setting"] in plan["settings"]["will_fill"]
        assert plan["memory"]["will_copy"] == 1, "只补回被删掉的那一份（模板那几份本机已经有了）"
        assert scene["memory"] not in plan["memory"]["already_here_files"]
        assert plan["artifacts"]["in_package"] == 0, "这条会话没有产物"
        assert any("NAS" in line for line in plan["credentials_to_configure"])

        assert stores.meta.get_conversation(scene["conversation_id"]) is None, "预演不写库"
        assert stores.meta.get_setting(scene["setting"]) is None
        data_dir = Path(get_settings().data_dir)
        assert not (data_dir / "memory" / "usr_owner" / "MEMORY.md").exists(), "预演不落记忆"
        assert stores.ledger.list_import_batches(limit=10) == [], "预演不产批次"
        assert any(
            request.method == "GET" and request.url.path.endswith("/blob") for request in nas.seen
        ), "包体是从 NAS 读回来的"


def test_restore_starts_a_batch_that_the_existing_endpoints_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """真恢复：**202 + 批次 id** → 轮询既有的 ``GET /local/import/{id}`` → 状态与报告。

    这条用例同时是"**不新开端点**"那句话的证据：进度与报告都在 M2 那张台账上
    （恢复特有的那几段并进 ``counts["restore"]``）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        scene = _serve_the_local_package(client, monkeypatch, nas)
        stores = get_stores()

        response = client.post(
            RESTORE, json={"device_id": DEVICE, "snapshot_id": scene["snapshot_id"]}
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["state"] == "planned" and body["batch_id"].startswith("rst_")
        assert body["source"] == f"backup://{DEVICE}/{scene['snapshot_id']}"
        # **拿到 id 立刻就能查**（端点先同步建行再去后台跑）
        first = client.get(f"/api/v1/local/import/{body['batch_id']}")
        assert first.status_code == 200, first.text

        done = _await_restore(client, body["batch_id"])

        assert done["state"] == "done", done
        assert done["counts"]["created"] == 1
        assert done["counts"]["restore"]["memory"]["copied"] == 1
        assert scene["setting"] in done["counts"]["restore"]["settings"]["filled"]
        assert stores.meta.get_conversation(scene["conversation_id"]) is not None, "会话回来了"
        assert stores.meta.get_setting(scene["setting"]) == "0.3"
        data_dir = Path(get_settings().data_dir)
        assert (data_dir / "memory" / "usr_owner" / "MEMORY.md").is_file()
        # 恢复前那份本地兜底：只落本机、**不在队列里**
        assert list((data_dir / "restore-backup").glob("*/*.tar.gz"))
        kinds = [row.kind for row in stores.backup_queue.list_backup_snapshots(limit=50)]
        assert "pre_restore" not in kinds, "兜底那份不入队（队列那边也有一条显式的拒绝）"


def test_restore_refuses_unknown_keys_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """``extra="forbid"``：凭据类键与未知键都不收（R3/R14），缺哪一段也说得出是哪个字段。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        for payload in (
            {"device_id": DEVICE, "snapshot_id": SEGMENT, "token": "kylab_sk_x"},
            {"device_id": DEVICE, "snapshot_id": SEGMENT, "server": NAS},
            {"device_id": "", "snapshot_id": SEGMENT},
            {"snapshot_id": SEGMENT},
        ):
            response = client.post(RESTORE, json=payload)
            assert response.status_code == 422, f"{payload} 该被 422 挡住"


def test_restore_reports_a_broken_provider_the_way_the_family_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """连不上 NAS：预演 → **502 + 原因**（外部依赖出错）；真恢复 → **202 + 一条 failed 台账**。

    两档的差别是有意的：预演那条路要"当场告诉用户为什么看不到"，而真恢复那条路
    （后台线程 / CLI）要的是**失败也是一份记录**——轮询的人从台账里读到原因。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        scene = _serve_the_local_package(client, monkeypatch, nas)
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")

        dry = client.post(
            RESTORE,
            json={"device_id": DEVICE, "snapshot_id": scene["snapshot_id"], "dry_run": True},
        )
        assert dry.status_code == 502, dry.text
        assert "连不上" in dry.json()["message"]

        response = client.post(
            RESTORE, json={"device_id": DEVICE, "snapshot_id": scene["snapshot_id"]}
        )
        assert response.status_code == 202, response.text

        failed = _await_restore(client, response.json()["batch_id"])

        assert failed["state"] == "failed" and "连不上" in failed["error"]
        assert get_stores().meta.get_conversation(scene["conversation_id"]) is None


# ------------------------------------------------- 收口：备份那四个键的登记口径（阶段 4A 收口 1）


def test_the_backup_keys_live_in_defaults_but_not_in_setting_groups() -> None:
    """四个备份运行期键在 ``DEFAULTS`` 里、**不在** ``SETTING_GROUPS`` 里。

    口径与"知识库连接"那一节（``frontend/.../KnowledgeConnectionSection.tsx``）一致：
    ``SETTING_GROUPS`` 是一份**服务器档也会渲染**的注册表，而备份这一族（含 ``pre_restore``
    兜底、待传队列）是本机档独有的——它的界面是**本机档前端自绘**的，不进那份注册表。

    键本身必须留在 ``DEFAULTS`` 里：``/local/backup`` 的 PATCH 白名单元数据从这儿取回落值，
    ``resolve_backup_target`` 也靠它读"用户改过没有"。所以这条用例钉的是**两件事同时成立**：
    运行期键还在（功能不回退），而登记表里没有它（不再骗服务器档的设置页）。
    """
    from app.services.runtime_config import DEFAULTS, SETTING_GROUPS

    registered = {
        field["key"] for group in SETTING_GROUPS.values() for field in group.get("fields", ())
    }
    for key in (
        "provider.backup.every_hours",
        "provider.backup.include_workspace",
    ):
        assert key in DEFAULTS, f"{key} 要在 DEFAULTS 里（PATCH 白名单与回落链靠它）"
        assert key not in registered, f"{key} 不该进 SETTING_GROUPS（那一节由本机档前端自绘）"
    assert "backup" not in SETTING_GROUPS, "整个分组都不该在（本机档独有的那一节）"
    # ``base_url`` / ``enabled`` 与知识库那一对同一口径：本来就没有 DEFAULTS 条目
    assert "provider.backup.base_url" not in DEFAULTS
    assert "provider.backup.enabled" not in DEFAULTS


# ------------------------------------------------------------ ⑧ 钥匙串（阶段 6）
#
# 两条端点只回答两件事：**这台机器有没有钥匙串**、**还有几处明文等着收编**。
# 秘密的**值**一个字节都不许出现在响应里——这里用哨兵串在整份 JSON 里 grep 来钉它。
#
# 用例把钥匙串换成 `InMemorySecretStore`（monkeypatch 组合根里那个 `platform_store`）：
# 在开发机上跑用例**不该往真的 Windows 凭据管理器里写东西**，而"写进去了"这件事
# 由 `test_secrets.py` 那条真机用例（配 `cmdkey /list`）负责。


SECRETS = "/api/v1/local/secrets"
PLAINTEXT_SENTINEL = "tavily_SENTINEL_endpoint_2c71"


def _provider_with_plaintext() -> Any:
    """一家带明文凭据的供应商（收编的第二个目标：``model_providers.api_key``）。"""
    from app.storage.base import ModelProviderRecord

    return ModelProviderRecord(
        id="prov_endpoint", kind="llm", name="端点用例供应商", api_key="kylab_sk_endpoint"
    )


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把组合根建的钥匙串换成进程内那份（用例自己拿着它核对写进去了什么）。"""
    from app.core import services as services_module
    from app.services.secrets import InMemorySecretStore

    store = InMemorySecretStore()
    monkeypatch.setattr(services_module, "platform_store", lambda: store)
    return store


def test_secrets_status_reports_numbers_without_any_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas, keychain: Any
) -> None:
    """``GET /local/secrets``：只报可用性与"还有几处明文"，**一个秘密都不回显**。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        stores = get_stores()
        stores.meta.set_setting("web.search_api_key", PLAINTEXT_SENTINEL)
        stores.meta.create_model_provider(
            _provider_with_plaintext(),
        )

        response = client.get(SECRETS)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {"store": "available", "pending_migration": 2}
        assert PLAINTEXT_SENTINEL not in response.text, "秘密的值一个字节都不出去"


def test_migrate_endpoint_moves_the_plaintext_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas, keychain: Any
) -> None:
    """``POST /local/secrets/migrate``：逐项报告 + 库里那份真的没了 + 钥匙串里真的有了。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        stores = get_stores()
        stores.meta.set_setting("web.search_api_key", PLAINTEXT_SENTINEL)
        provider = stores.meta.create_model_provider(_provider_with_plaintext())

        response = client.post(f"{SECRETS}/migrate")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["store"] == "available"
        assert [item["item"] for item in body["migrated"]] == [
            "setting:web.search_api_key",
            f"model_provider:{provider.id}",
        ]
        assert body["failed"] == [] and body["pending_migration"] == 0
        assert PLAINTEXT_SENTINEL not in response.text

        assert keychain.get("kylab:setting:web.search_api_key") == PLAINTEXT_SENTINEL
        assert keychain.get(f"kylab:model_provider:{provider.id}") == "kylab_sk_endpoint"
        assert stores.meta.get_setting("web.search_api_key") is None
        assert stores.meta.get_model_provider(provider.id).api_key == ""
        assert client.get(SECRETS).json()["pending_migration"] == 0, "再查一次是 0（可重跑）"


def test_migrate_says_503_when_the_machine_has_no_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """没有钥匙串（Linux 桌面 / 容器 / CI）→ **503 + 一句人话**，而且**明文不动**。"""
    from app.core import services as services_module
    from app.services.secrets import NullSecretStore

    monkeypatch.setattr(services_module, "platform_store", NullSecretStore)
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        stores = get_stores()
        stores.meta.set_setting("web.search_api_key", PLAINTEXT_SENTINEL)

        status = client.get(SECRETS)
        response = client.post(f"{SECRETS}/migrate")

        assert status.status_code == 200, status.text
        assert status.json() == {"store": "unavailable", "pending_migration": 0}
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["code"] == "secret_store_unavailable", "前端按 code 分支"
        assert "钥匙串" in body["message"]
        assert stores.meta.get_setting("web.search_api_key") == PLAINTEXT_SENTINEL, "原样留着"


# --------------------------------------------- ⑨ 三栏本机配置读得回来（阶段 7 报的缺口）
#
# `GET /local/backup` 的 `provider` 块原来只回 `base_url`，于是「远端开关 / 间隔小时 /
# 含不含工作区」三栏**只写得进去、读不回来**——界面只好摆动作按钮、从那句"被关掉了"
# 反推当前态（退化方向是"读不到"）。这一节钉四件事。
#
# 三栏的口径都不在这里重写：`enabled` 调 `backup_provider.backup_enabled`（地址解析用的
# 同一个判据）、`every_hours` 调 `BackupQueueService.every_hours`、`include_workspace`
# 读同一个 `INCLUDE_WORKSPACE_KEY`（读不到按关，与打包器同一条口径）。

CONFIG_COLUMNS = ("enabled", "include_workspace", "every_hours")


def test_the_three_config_columns_are_always_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """① 三栏在**每一档**都出现：远端不可用、未配置、可用——它是本机配置事实。

    三档各自的取法（顺序是有意的）：先在传输断着的时候读一次（第一次探测就成了
    ``unavailable``），再用 PATCH 强制重探（GET 读 30s 缓存，中间切档会被缓存挡住）。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        nas.fail = httpx.ConnectError("NAS 连不上（用例）")

        unavailable = client.get(BASE).json()["provider"]
        assert unavailable["state"] == "unavailable"
        assert all(key in unavailable for key in CONFIG_COLUMNS), unavailable

        nas.fail = None
        unconfigured = client.patch(f"{BASE}", json={"enabled": False}).json()["provider"]
        assert unconfigured["state"] == "unconfigured"
        assert all(key in unconfigured for key in CONFIG_COLUMNS), unconfigured

        ready = client.patch(f"{BASE}", json={"enabled": True}).json()["provider"]
        assert ready["state"] == "ready"
        assert all(key in ready for key in CONFIG_COLUMNS), ready

        again = client.get(BASE).json()["provider"]
        assert all(key in again for key in CONFIG_COLUMNS), again


def test_the_three_config_columns_read_back_what_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """② 设过的值读得回来（这一条就是那个缺口本身：**写得进去、读不回来**）。"""
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        patched = client.patch(
            f"{BASE}",
            json={
                "base_url": NAS,
                "enabled": True,
                "include_workspace": True,
                "every_hours": 6,
            },
        ).json()["provider"]

        assert patched["enabled"] is True
        assert patched["include_workspace"] is True
        assert patched["every_hours"] == 6
        # 再 GET 一次（走的是缓存的三态 + 实时读的配置，两件事互不影响）
        fresh = client.get(BASE).json()["provider"]
        assert (fresh["enabled"], fresh["include_workspace"], fresh["every_hours"]) == (
            True,
            True,
            6,
        )

        client.patch(f"{BASE}", json={"every_hours": 0, "include_workspace": False})
        off = client.get(BASE).json()["provider"]
        assert off["every_hours"] == 0 and off["include_workspace"] is False


def test_the_three_config_columns_default_to_the_product_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """③ 没设过时是产品默认：``every_hours=24`` / ``include_workspace=false`` / ``enabled=true``。

    这个文件的夹具为了让自动快照别来抢 ``backup/pending/``，把 ``DEFAULTS`` 里
    ``every_hours`` 那一格改成了 0；这一条先把它恢复成产品默认
    （``DEFAULT_EVERY_HOURS``），否则测的是夹具而不是产品行为。
    """
    from app.services.backup_queue import DEFAULT_EVERY_HOURS

    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)
        monkeypatch.setitem(
            runtime_config.DEFAULTS, "provider.backup.every_hours", str(DEFAULT_EVERY_HOURS)
        )

        provider = client.get(BASE).json()["provider"]

        assert provider["every_hours"] == 24
        assert provider["include_workspace"] is False
        assert provider["enabled"] is True


def test_the_enabled_column_agrees_with_the_unconfigured_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nas: FakeNas
) -> None:
    """④ 关掉那一档：``enabled is False`` **且** ``state == "unconfigured"`` + 两句人话。

    这一栏就是"为什么未配置"的答案（另一种成因是"没填地址"）。界面据此把开关画对，
    不必再从 ``reason`` 那句话里反推——那是这一组键要消掉的东西。
    """
    with _local_app(tmp_path, monkeypatch) as client:
        _attach(nas, monkeypatch)

        body = client.patch(f"{BASE}", json={"enabled": False}).json()
        provider = body["provider"]

        assert provider["enabled"] is False
        assert provider["state"] == "unconfigured"
        assert "被关掉了" in provider["reason"]
        assert "本机打快照" in provider["reason"], "关掉只影响传不传得出去（那句话仍在）"
        assert body["provider"]["base_url"] == "", "关掉时不解析地址（与解析口径一致）"
