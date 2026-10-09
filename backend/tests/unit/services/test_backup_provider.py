"""备份提供者客户端的契约用例（M5 阶段 4A，方案 §1.2 / §3.3 / §5 R5-R12）。

镜像同构：``app/services/backup_provider.py`` → ``tests/unit/services/test_backup_provider.py``。

**这一份用例钉四件事**（每一条都能对回服务端那半边 ``api/v1/backup.py`` 的契约）：

1. **地址与凭据的四路解析**（纯函数，不打网络）：覆盖 → 继承 → 显式关 → 没地址；
2. **三态 + 30s 缓存 + ``refresh`` 强制重探**，而且**探针绝不抛**（连不上 / 401 /
   版本更高 → 都是 ``unavailable`` + 一句原因）；R5 那一位（``snapshot_available``）
   与三态**分开**：NAS 活着但桶没建出来时前者为真、后者为假；
3. **上传两次 PUT 的逐字契约**：先 blob 后 manifest、``?sha256=&bytes=`` 逐字正确、
   201/200 都算成功（200 = 同内容 no-op，是重试路径的答案）、409/403/413 抛、
   **流式**（大件不进内存，粗量判据）；
4. **列表 / 删除 / 下载**：列表 30s 缓存与"不可用不发请求"；删除 404 如实回 0；
   下载带上 ``X-Kylab-Sha256`` 校验、不符**删掉刚写的那份**（绝不落位）。

**假的是什么、真的是什么**：真的客户端（真 httpx 的请求构造、真分档、真缓存）、
真的本机文件；假的是**传输**（``httpx.MockTransport``，一次真网络都不打）与**时钟**。
"""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services import backup_provider
from app.services.backup_provider import (
    PROTOCOL_VERSION,
    STATE_READY,
    STATE_UNAVAILABLE,
    STATE_UNCONFIGURED,
    BackupProviderClient,
    backup_enabled,
    resolve_backup_target,
)
from app.services.backup_queue import PendingUpload
from app.services.backup_snapshot import SnapshotBlob, SnapshotManifest
from app.services.remote_clients import (
    RemoteClientError,
    RemoteRejectedError,
    RemoteUnavailableError,
)

pytestmark = pytest.mark.local

NAS = "http://nas.test/api/v1"
OTHER = "http://other.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_must_not_leak"
DEVICE = "3f1c8b2e-0a4d-4a77-9d55-2c6a1b7e9f01"
SNAPSHOT_SEGMENT = "2026-10-05T08-03-00Z-d69e6898"


class FakeClock:
    """可拨的单调钟（TTL 那两条只能拨时间，不能真等 30 秒）。"""

    def __init__(self) -> None:
        self.moment = 0.0

    def __call__(self) -> float:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += seconds


#: 这份替身**最多**把多大的请求体读进内存（用它自己的那条路判）。
#:
#: 为什么要有这一条：``MockTransport`` 的 handler 里一句 ``request.read()`` 就会把整份
#: 上传读进内存——那会让"上传是不是流式的"这条用例量到**替身**的内存，而不是被测实现的。
#: 所以大件只按 ``Content-Length`` 报数、不读内容（小件照读，好断言字节一模一样）。
_FAKE_MAX_BODY = 1 << 20


class FakeNas:
    """一台**假 NAS**：按路径回答 + 记下每一次请求。

    ``fail`` 一置就是"连不上"（每一发都抛 ``ConnectError``）；各条 ``*_status`` 用来
    摆出 409 / 403 / 413 / 404 那几档；``capabilities`` 用来摆出 R5 那一位
    （桶没建出来 → 握手 200 但 ``snapshot.available=false``）。
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.fail: Exception | None = None
        self.capabilities: dict[str, Any] = {"snapshot": {"available": True}}
        self.blob_status = 201
        self.manifest_status = 201
        self.list_payload: dict[str, Any] = {"items": [], "total": 0, "quota": {"keep": 30}}
        self.list_status = 200
        self.delete_status = 200
        self.delete_removed = 2
        self.blob_body = b"snapshot-bytes"
        self.blob_sha_header = hashlib.sha256(b"snapshot-bytes").hexdigest()
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
                    "capabilities": self.capabilities,
                    "caller": {"kind": "api_key", "is_admin": False},
                    "devices": [{"device_id": DEVICE, "snapshots": 1, "bytes": 1024}],
                    "server_time": "2026-10-05T08:00:00Z",
                },
            )
        if path.endswith("/blob") and request.method == "PUT":
            if self.blob_status >= 400:
                return httpx.Response(self.blob_status, text="服务端说不行")
            size = _declared_length(request)
            body = _read_small(request)
            return httpx.Response(
                self.blob_status,
                json={
                    "snapshot_id": SNAPSHOT_SEGMENT,
                    "bytes": size,
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
            )
        if path.endswith("/manifest") and request.method == "PUT":
            if self.manifest_status >= 400:
                return httpx.Response(self.manifest_status, text="先传快照体再传清单")
            return httpx.Response(
                self.manifest_status,
                json={"snapshot_id": SNAPSHOT_SEGMENT, "bytes": len(request.read()), "sha256": "x"},
            )
        if path.endswith("/blob") and request.method == "GET":
            return httpx.Response(
                200, content=self.blob_body, headers={"X-Kylab-Sha256": self.blob_sha_header}
            )
        if path.endswith("/backup/snapshots"):
            if self.list_status >= 400:
                return httpx.Response(self.list_status, text="列表不行")
            return httpx.Response(self.list_status, json=self.list_payload)
        if request.method == "DELETE":
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status, text="这条路径上没有恢复点可删")
            return httpx.Response(self.delete_status, json={"removed": self.delete_removed})
        return httpx.Response(404, text="没有这个端点")

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        self.bodies.append(_read_small(request))
        return self.handler(request)

    # ------------------------------------------------------------------ 用例那一侧
    def paths(self) -> list[str]:
        return [request.url.path for request in self.seen]

    def list_calls(self) -> int:
        """打到 ``/backup/snapshots`` 上的请求数（"列表那一读发了几次"的判据）。"""
        return sum(1 for request in self.seen if request.url.path.endswith("/backup/snapshots"))

    def method_of(self, index: int) -> tuple[str, str]:
        request = self.seen[index]
        return request.method, request.url.path


def _declared_length(request: httpx.Request) -> int:
    """请求声明的字节数（大件不读内容时用它报数；没有就回 0）。"""
    raw = request.headers.get("content-length") or "0"
    return int(raw) if raw.isdigit() else 0


def _read_small(request: httpx.Request) -> bytes:
    """小件读内容、大件**不读**（见 ``_FAKE_MAX_BODY``：替身不该成为内存峰值的来源）。"""
    return request.read() if _declared_length(request) <= _FAKE_MAX_BODY else b""


class DrainingTransport(httpx.BaseTransport):
    """一台上传**逐块收、不落脚**的传输（真网络正是这样一块一块走的）。

    为什么这一条用例不能借 ``httpx.MockTransport``：它在调 handler 之前会
    ``request.read()``，那一下就把整份上传读进了内存——于是量到的是**替身**的内存，
    而不是"我们是不是流式发的"。这个替身按 ``request.stream`` 逐块过一遍，
    顺手记下每块多大（块的大小本身就是"流式"最直接的证据）。
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, int, int]] = []
        """每一发：``(路径, 收到多少字节, 单块最大多少字节)``。"""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        total = 0
        biggest = 0
        for chunk in request.stream:
            total += len(chunk)
            biggest = max(biggest, len(chunk))
        self.requests.append((request.url.path, total, biggest))
        return httpx.Response(
            201, json={"snapshot_id": SNAPSHOT_SEGMENT, "bytes": total, "sha256": "x"}
        )


@pytest.fixture
def nas() -> FakeNas:
    return FakeNas()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_client(
    nas: FakeNas,
    clock: FakeClock,
    *,
    get_setting: Any = None,
    server_url: str = NAS,
    token: str = TOKEN,
) -> BackupProviderClient:
    """真客户端 + 假传输（``transport`` 是它自己的注入接缝）。"""
    settings = Settings(
        _env_file=None, deployment="local", server_url=server_url, token=token, device_id=DEVICE
    )
    return BackupProviderClient(
        settings=settings,
        get_setting=get_setting,
        transport=nas.transport,
        clock=clock,
    )


def make_pending(
    data_dir: Path,
    *,
    device_id: str = DEVICE,
    payload: bytes = b"snapshot-bytes",
    drop_device: bool = False,
) -> PendingUpload:
    """造一份"已经打好"的待传（清单用真的 ``SnapshotManifest`` 序列化）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    blob_path = data_dir / "snap.tar.gz"
    blob_path.write_bytes(payload)
    manifest = SnapshotManifest(
        snapshot_id=f"{device_id}-{SNAPSHOT_SEGMENT}",
        device_id="" if drop_device else device_id,
        created_at="2026-10-05T08:03:00Z",
        kind="manual",
        schema_version=3,
        blob=SnapshotBlob(bytes=len(payload), sha256=digest),
    )
    return PendingUpload(
        snapshot_id=manifest.snapshot_id,
        blob_path=blob_path,
        blob_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        manifest_json=manifest.to_bytes().decode("utf-8"),
    )


# ------------------------------------------------------------------ 四路解析


def test_the_target_inherits_the_shell_nas_by_default() -> None:
    """没配覆盖 → 地址是**壳里那台 NAS**（``Settings.server_url``），凭据是 ``token``。"""
    settings = Settings(
        _env_file=None, deployment="local", server_url=NAS, token=TOKEN, device_id=DEVICE
    )
    target = resolve_backup_target(settings, lambda key: "")

    assert target.configured is True
    assert target.base_url == NAS
    assert target.token == TOKEN
    assert target.credential == "configured"


def test_the_runtime_override_wins_over_the_inherited_address() -> None:
    """运行期覆盖（设置页那个键）优先于继承；尾斜杠被归一掉。"""
    settings = Settings(
        _env_file=None, deployment="local", server_url=NAS, token=TOKEN, device_id=DEVICE
    )
    target = resolve_backup_target(
        settings,
        lambda key: f"{OTHER}/" if key == backup_provider.SETTING_BASE_URL else "",
    )

    assert target.base_url == OTHER and target.configured is True


def test_an_explicit_off_beats_an_address() -> None:
    """显式关掉 → **不配**（连地址都不再解析），而且理由是"被关掉了"那一句。"""
    settings = Settings(
        _env_file=None, deployment="local", server_url=NAS, token=TOKEN, device_id=DEVICE
    )
    target = resolve_backup_target(
        settings, lambda key: "0" if key == backup_provider.SETTING_ENABLED else ""
    )

    assert target.configured is False
    assert target.reason == backup_provider.DISABLED_REASON
    assert "本机打快照" in target.reason, "关掉只影响传不传得出去，这话要说清"


def test_no_address_means_unconfigured() -> None:
    """一个地址都没有 → 不配 + 那句人话（**不假装连上了**）。"""
    settings = Settings(_env_file=None, deployment="local", token=TOKEN, device_id=DEVICE)
    target = resolve_backup_target(settings, lambda key: "")

    assert target.configured is False
    assert target.reason == backup_provider.UNCONFIGURED_REASON
    assert ("provider.backup.base_url" in target.reason) or ("server" in target.reason)


# ------------------------------------------------------------------ 三态与缓存


def test_a_healthy_handshake_reports_ready_with_capabilities(
    nas: FakeNas, clock: FakeClock
) -> None:
    """握手通了 → ``ready`` + 能力集 + 设备清单（形状原样透传）。"""
    client = make_client(nas, clock)

    status = client.status()

    assert status.state == STATE_READY and status.available is True
    assert status.protocol_version == PROTOCOL_VERSION
    assert status.app_version == "0.1.1"
    assert status.devices and status.devices[0]["device_id"] == DEVICE
    assert status.snapshot_available is True and status.snapshot_reason == ""
    payload = status.to_payload()
    assert payload["available"] is True and payload["devices"]
    assert "token" not in json.dumps(payload), "凭据一个字都不许出现在响应里"


def test_the_bucket_bit_is_separate_from_the_state(nas: FakeNas, clock: FakeClock) -> None:
    """R5：NAS 活着但**桶没建出来** → ``ready`` 仍是真，而 ``snapshot_available`` 是假 + 那句话。"""
    nas.capabilities = {
        "snapshot": {"available": False, "unavailable_reason": "去 NAS 上跑 mc mb kylab-backup"}
    }
    client = make_client(nas, clock)

    status = client.status()

    assert status.state == STATE_READY and status.available is True
    assert status.snapshot_available is False
    assert "mc mb" in status.snapshot_reason


def test_the_handshake_is_cached_for_thirty_seconds(nas: FakeNas, clock: FakeClock) -> None:
    """30s 内不重探；过 30s 自动重探；``refresh=True`` 立刻重探。"""
    client = make_client(nas, clock)

    client.status()
    client.status()
    assert len(nas.seen) == 1, "TTL 内第二次问不该再发请求"

    clock.advance(29.0)
    client.status()
    assert len(nas.seen) == 1

    clock.advance(2.0)
    client.status()
    assert len(nas.seen) == 2, "过 30s 该重探一次"

    client.status(refresh=True)
    assert len(nas.seen) == 3


def test_a_probe_never_raises(nas: FakeNas, clock: FakeClock) -> None:
    """连不上 → ``unavailable`` + 原因（**不是异常**：状态页该打得开）。"""
    nas.fail = httpx.ConnectError("NAS 连不上（用例）")
    client = make_client(nas, clock)

    status = client.status()

    assert status.state == STATE_UNAVAILABLE
    assert "连不上" in status.reason
    assert status.snapshot_available is False
    assert client.status().state == STATE_UNAVAILABLE, "缓存里也是那个结论"


def test_a_rejected_credential_is_unavailable_with_a_reason(
    nas: FakeNas, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401（钥匙不对）→ ``unavailable`` + 一句"核对地址与网络"，凭据那一位照实说。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="令牌无效")

    monkeypatch.setattr(nas, "transport", httpx.MockTransport(handler))
    client = make_client(nas, clock)

    status = client.status()

    assert status.state == STATE_UNAVAILABLE
    assert "凭据" in status.reason or "401" in status.reason
    assert status.credential == "configured", "有没有配是一件事、认不认是另一件事"


def test_a_newer_protocol_is_refused_not_guessed(
    nas: FakeNas, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对方报的协议版本更高 → **绝不硬试**（原因里带版本号）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"provider": "backup", "protocol_version": 99})

    monkeypatch.setattr(nas, "transport", httpx.MockTransport(handler))
    client = make_client(nas, clock)

    status = client.status()

    assert status.state == STATE_UNAVAILABLE
    assert "99" in status.reason and str(PROTOCOL_VERSION) in status.reason


def test_unconfigured_does_not_touch_the_network(nas: FakeNas, clock: FakeClock) -> None:
    """没配 → ``unconfigured`` + **零请求**（没地址就没有可打的地方）。"""
    client = make_client(nas, clock, server_url="", token="")

    status = client.status()

    assert status.state == STATE_UNCONFIGURED
    assert status.reason == backup_provider.UNCONFIGURED_REASON
    assert nas.seen == []


# ------------------------------------------------------------------ 上传


def test_upload_puts_the_blob_then_the_manifest(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """**先 blob 后 manifest**，且两段的路径 / 声明 / 内容逐字对得上（方案 §3.3）。"""
    client = make_client(nas, clock)
    pending = make_pending(tmp_path / "in")

    receipt = client.upload(pending)

    assert [method for method, _ in (nas.method_of(0), nas.method_of(1))] == ["PUT", "PUT"]
    blob_request, manifest_request = nas.seen[0], nas.seen[1]
    assert blob_request.url.path == f"/api/v1/backup/snapshots/{DEVICE}/{SNAPSHOT_SEGMENT}/blob"
    assert dict(blob_request.url.params) == {
        "sha256": pending.sha256,
        "bytes": str(pending.blob_bytes),
    }, "声明必须逐字（服务端边收边算来核对）"
    assert blob_request.headers["content-type"] == "application/octet-stream"
    assert blob_request.headers.get("content-length") == str(pending.blob_bytes), (
        "文件对象交给 httpx 时该给出 Content-Length（服务端据此早退 413）"
    )
    assert blob_request.headers["authorization"] == f"Bearer {TOKEN}"
    assert nas.bodies[0] == pending.blob_path.read_bytes()

    assert manifest_request.url.path == (
        f"/api/v1/backup/snapshots/{DEVICE}/{SNAPSHOT_SEGMENT}/manifest"
    )
    assert "sha256" not in manifest_request.url.params, "清单不声明摘要（它在响应里给）"
    assert nas.bodies[1] == pending.manifest_bytes, "清单原样（包里的第一成员那串字节）"

    assert receipt is not None
    assert receipt.device_id == DEVICE and receipt.snapshot_id == SNAPSHOT_SEGMENT


def test_a_replay_is_a_no_op_not_a_failure(nas: FakeNas, clock: FakeClock, tmp_path: Path) -> None:
    """201（新建）与 **200（同内容 no-op）都算成功**——重放安全是重试路径的前提。"""
    client = make_client(nas, clock)
    pending = make_pending(tmp_path / "in")

    nas.blob_status = 200
    nas.manifest_status = 200
    receipt = client.upload(pending)

    assert receipt is not None and len(nas.seen) == 2


def test_a_conflict_is_raised_not_swallowed(nas: FakeNas, clock: FakeClock, tmp_path: Path) -> None:
    """409（同路径不同内容 / 超保留 / 超配额）→ **抛**（append-only：绝不覆盖）。"""
    nas.blob_status = 409
    client = make_client(nas, clock)

    with pytest.raises(RemoteRejectedError) as excinfo:
        client.upload(make_pending(tmp_path / "in"))

    assert "409" in str(excinfo.value) and "append-only" in str(excinfo.value)
    assert len(nas.seen) == 1, "第一段就失败了，不该再去传清单（它要求快照体先到）"


def test_a_read_only_key_is_raised(nas: FakeNas, clock: FakeClock, tmp_path: Path) -> None:
    """403（只读 key）→ 抛，句子里说清是**权限**那一档（R9）。"""
    nas.blob_status = 403
    client = make_client(nas, clock)

    with pytest.raises(RemoteRejectedError) as excinfo:
        client.upload(make_pending(tmp_path / "in"))

    assert "权限" in str(excinfo.value)


def test_an_oversized_snapshot_is_raised(nas: FakeNas, clock: FakeClock, tmp_path: Path) -> None:
    """413（超单份上限）→ 抛（R6：客户端别把 NAS 的内存与带宽打满）。"""
    nas.blob_status = 413
    client = make_client(nas, clock)

    with pytest.raises(RemoteRejectedError) as excinfo:
        client.upload(make_pending(tmp_path / "in"))

    assert "413" in str(excinfo.value)


def test_an_unconfigured_provider_refuses_to_upload(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """没配 → 上传抛一句能读懂的话（队列把它记进 ``last_error``）。"""
    client = make_client(nas, clock, server_url="", token="")

    with pytest.raises(RemoteUnavailableError):
        client.upload(make_pending(tmp_path / "in"))
    assert nas.seen == []


def test_a_snapshot_without_a_device_identity_is_refused(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """R12：清单里没有 ``device_id`` → **如实拒**（绝不编一个坐标发出去）。"""
    client = make_client(nas, clock)

    with pytest.raises(RemoteRejectedError) as excinfo:
        client.upload(make_pending(tmp_path / "in", drop_device=True))

    assert "设备身份" in str(excinfo.value)
    assert nas.seen == [], "发出去之前就该拦住"


def test_a_missing_local_package_is_reported(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """盘上那份包不见了 → 抛一句能读懂的话（队列照旧记账并重试）。"""
    pending = make_pending(tmp_path / "in")
    pending.blob_path.unlink()
    client = make_client(nas, clock)

    with pytest.raises(RemoteClientError):
        client.upload(pending)


def test_a_large_snapshot_is_streamed_not_buffered(clock: FakeClock, tmp_path: Path) -> None:
    """**流式**（R6/R10）：24 MiB 的包不进 Python 内存（粗量判据：峰值远小于它）。

    三条判据一起看：整份都发出去了（``total``）、**单块很小**（真实网络就是一块一块走的）、
    而峰值远小于包本身。真把整份读进 ``bytes`` 的实现，峰值会立刻超过 24 MiB。
    """
    size = 24 * 1024 * 1024
    payload = b"\0" * size  # 一整块**没有换行**的数据：按行迭代的实现会当场吃掉整份
    transport = DrainingTransport()
    settings = Settings(
        _env_file=None, deployment="local", server_url=NAS, token=TOKEN, device_id=DEVICE
    )
    client = BackupProviderClient(settings=settings, transport=transport, clock=clock)
    pending = make_pending(tmp_path / "in", payload=payload)

    tracemalloc.start()
    try:
        client.upload(pending)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert transport.requests, "一发都没发出去"
    path, sent, biggest = transport.requests[0]
    assert path.endswith("/blob") and sent == size, "整份都发出去了（不然「省内存」只是漏传）"
    assert biggest <= 1024 * 1024, f"单块最大 {biggest} 字节：真流式该是一块一块走的"
    assert peak < size // 4, f"上传峰值内存 {peak}（{size} 字节的包）说明被整份读进了内存"


# ------------------------------------------------------------------ 列表 / 删除


def test_the_point_list_is_cached_and_never_called_when_unavailable(
    nas: FakeNas, clock: FakeClock
) -> None:
    """列表 30s 缓存 + **不可用时一个请求都不发**（连不上的 NAS 不该被每开一次页面就打）。"""
    nas.list_payload = {"items": [{"snapshot_id": SNAPSHOT_SEGMENT}], "total": 1, "quota": {}}
    client = make_client(nas, clock)

    first = client.list_snapshots()
    assert nas.list_calls() == 1
    second = client.list_snapshots()

    assert first == second and first is not None and first["total"] == 1
    assert nas.list_calls() == 1, "TTL 内第二次读不该再打 NAS"

    clock.advance(31.0)
    client.list_snapshots()
    assert nas.list_calls() == 2

    # 不可用那一档：状态说连不上，于是连列表都不去试
    nas.fail = httpx.ConnectError("断了")
    clock.advance(31.0)
    assert client.status(refresh=True).state == "unavailable"  # 这一发是探针
    before = nas.list_calls()
    assert client.list_snapshots() is None
    assert nas.list_calls() == before, "状态已经不 ready，列表不该再发请求"


def test_a_failed_list_is_reported_as_none_not_an_exception(nas: FakeNas, clock: FakeClock) -> None:
    """状态通、列表这条路出错 → ``None``（调用方如实回三态，**不是 500**）。"""
    nas.list_status = 500
    client = make_client(nas, clock)

    assert client.list_snapshots() is None


def test_delete_reports_a_missing_point_as_zero(nas: FakeNas, clock: FakeClock) -> None:
    """服务端 404（本来就没有）→ 如实回 ``0``（调用方据此回"这条路径上没有"）。"""
    nas.delete_status = 404
    client = make_client(nas, clock)

    assert client.delete_snapshot(DEVICE, SNAPSHOT_SEGMENT) == 0


def test_delete_returns_the_object_count(nas: FakeNas, clock: FakeClock) -> None:
    """整份删（快照体 + 清单）→ 服务端回几个就报几个。"""
    client = make_client(nas, clock)

    assert client.delete_snapshot(DEVICE, SNAPSHOT_SEGMENT) == 2
    assert nas.seen[0].method == "DELETE"
    assert nas.seen[0].url.path == f"/api/v1/backup/snapshots/{DEVICE}/{SNAPSHOT_SEGMENT}"


def test_delete_of_a_read_only_key_is_raised(nas: FakeNas, clock: FakeClock) -> None:
    """只读 key 删不了（R9：写操作 403）→ 抛。"""
    nas.delete_status = 403
    client = make_client(nas, clock)

    with pytest.raises(RemoteRejectedError):
        client.delete_snapshot(DEVICE, SNAPSHOT_SEGMENT)


# ------------------------------------------------------------------ 下载（阶段 5 用）


def test_download_verifies_the_sha256_from_the_header(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """下载**流式落盘**并用 ``X-Kylab-Sha256`` 校验（阶段 5 第 4 步的那道闸）。"""
    nas.blob_body = b"x" * 5000
    nas.blob_sha_header = hashlib.sha256(nas.blob_body).hexdigest()
    client = make_client(nas, clock)
    dest = tmp_path / "out" / "snap.tar.gz"

    report = client.download_snapshot(DEVICE, SNAPSHOT_SEGMENT, dest)

    assert dest.read_bytes() == nas.blob_body
    assert report["bytes"] == 5000 and report["verified"] is True
    assert report["sha256"] == nas.blob_sha_header


def test_download_discards_a_mismatched_body(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """校验不符 → **删掉刚写的那份**并抛（绝不落位：坏快照比没有更危险）。"""
    nas.blob_sha_header = "0" * 64
    client = make_client(nas, clock)
    dest = tmp_path / "out" / "snap.tar.gz"

    with pytest.raises(RemoteUnavailableError) as excinfo:
        client.download_snapshot(DEVICE, SNAPSHOT_SEGMENT, dest)

    assert "sha256" in str(excinfo.value)
    assert not dest.exists(), "不符的那一份必须删掉"


def test_download_without_a_header_reports_unverified(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """服务端没给 ``X-Kylab-Sha256`` → 照样落盘，但**如实说没校验**（不假装）。"""
    nas.blob_sha_header = ""
    client = make_client(nas, clock)
    dest = tmp_path / "out" / "snap.tar.gz"

    report = client.download_snapshot(DEVICE, SNAPSHOT_SEGMENT, dest)

    assert dest.exists() and report["verified"] is False


def test_download_of_a_missing_point_leaves_nothing_behind(
    nas: FakeNas, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 → 抛，而且**盘上不留半个文件**。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/backup/handshake"):
            return httpx.Response(200, json={"protocol_version": PROTOCOL_VERSION})
        return httpx.Response(404, text="这个恢复点没有快照体")

    monkeypatch.setattr(nas, "transport", httpx.MockTransport(handler))
    client = make_client(nas, clock)
    dest = tmp_path / "out" / "snap.tar.gz"

    with pytest.raises(RemoteRejectedError):
        client.download_snapshot(DEVICE, SNAPSHOT_SEGMENT, dest)

    assert not dest.exists()


def test_the_provider_satisfies_the_uploader_protocol(
    nas: FakeNas, clock: FakeClock, tmp_path: Path
) -> None:
    """队列那份 ``SnapshotUploader`` 协议由本类**结构上**满足（组合根就是拿它注入的）。"""
    from app.services.backup_queue import SnapshotUploader

    client = make_client(nas, clock)

    assert isinstance(client, SnapshotUploader)
    receipt = client.upload(make_pending(tmp_path / "in"))
    assert receipt is not None and receipt.device_id == DEVICE


def test_the_receipt_is_none_when_the_server_reports_no_coordinates(
    nas: FakeNas, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端没回可解析的坐标 → ``None``（队列那两个远端列留空，**本机不编**）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/backup/handshake"):
            return httpx.Response(200, json={"protocol_version": PROTOCOL_VERSION})
        return httpx.Response(201, json={"bytes": 1, "sha256": "x"})

    monkeypatch.setattr(nas, "transport", httpx.MockTransport(handler))
    client = make_client(nas, clock)

    assert client.upload(make_pending(tmp_path / "in")) is None


def test_the_status_payload_hides_the_credentials(nas: FakeNas, clock: FakeClock) -> None:
    """整份响应里不许出现 token（R3/R14：凭据不回显）。"""
    client = make_client(nas, clock)

    text = json.dumps(client.status().to_payload(), ensure_ascii=False)

    assert TOKEN not in text and "Bearer" not in text


def test_the_enabled_judgement_is_one_function_for_both_callers() -> None:
    """``backup_enabled`` 是那一栏的**唯一判据**：地址解析与端点各调它，不各写一份。

    判据表（口径与知识库那条一致）：**没有这个键 = 开**；``0`` / ``false`` / ``no`` /
    ``off`` 才算关，大小写与两端空白不管。这张表原来只活在 ``resolve_backup_target``
    的 if 里，阶段 7 的界面要"读得到这一栏"之后就抽出来了——抄第二份的典型后果是
    "界面说开着、解析说关着"。
    """
    assert backup_enabled(lambda key: "") is True, "没有这个键 = 开"
    assert backup_enabled(lambda key: "1") is True
    assert backup_enabled(lambda key: "  TRUE ") is True
    for off in ("0", "false", "no", "off", " OFF ", "False"):
        assert backup_enabled(lambda key, raw=off: raw) is False, off

    # 与地址解析同一处：同一个入参下，两者对"开 / 关"的结论必须一致
    settings = Settings(
        _env_file=None, deployment="local", server_url=NAS, token=TOKEN, device_id=DEVICE
    )
    for raw in ("", "1", "0", "off"):
        getter = lambda key, raw=raw: raw if key == backup_provider.SETTING_ENABLED else ""  # noqa: E731
        enabled = backup_enabled(getter)
        configured = resolve_backup_target(settings, getter).configured
        assert configured is enabled, f"enabled={raw!r} 时两处结论不一致"


# --------------------------------------------------- 提示语只拼一处（阶段 8 的截图 08/09）
#
# 现场那句话（界面状态块）：
#   `握手：连不上备份提供者（http://127.0.0.1:9）——[WinError 10061] 由于目标计算机积极拒绝，
#     无法连接。。下一步：核对「备份」里的地址与网络。 。下一步：核对「备份」里的地址与网络。`
# 两个毛病：① `_send` 与 `_probe` **各拼了一遍**同一句提示；② 系统错误文本自带句号，
# 后面又接一句，于是中间多出一个空句号（`。。`）。
#
# 这一节把"那句话只在一处拼、且句号不叠"钉成判据。提示语**只数关键词**（不把整句抄
# 第二遍）：整句抄一遍就等于在用例里又维护了一份文案。

TIP = "下一步"
DOUBLE_PERIOD = "。。"
#: Windows 自己的错误文本**自带句号**——真机上连不上时就是这一句（截图里那句）。
WIN_ERROR = "[WinError 10061] 由于目标计算机积极拒绝，无法连接。"


def _offline(nas: FakeNas) -> None:
    """全面断网：每一发都连不上，错误文本自带句号。"""
    nas.fail = httpx.ConnectError(WIN_ERROR)


def _fails_after_handshake(nas: FakeNas) -> None:
    """握手照常、**之后每一发都连不上**。

    为什么要这个更细的替身：全面断网时 `list_snapshots` 会先卡在状态那一层（它先看
    `status().available`），根本走不到列表请求那一次 `_send`——那样"列表失败"这条路的
    消息就测不到了。这个替身让握手过得去，其余每一条都真的走到失败分支。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/backup/handshake"):
            return nas.handler(request)
        raise httpx.ConnectError(WIN_ERROR)

    nas.transport = httpx.MockTransport(handler)


def _fails_at_handshake(nas: FakeNas, flavour: str) -> None:
    """把"握手那一步怎么失败"摆出来（401/404/500 / 回的不是握手体）。

    这个替身只用在这一条用例里：`FakeNas` 上没有"握手状态码"那一个旋钮（它有的是
    列表 / 上传那几档），而这里要的是状态那一层**四种成因各一条**的文案。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/backup/handshake"):
            if flavour == "not-a-handshake":
                return httpx.Response(200, json={"hello": "world"})
            return httpx.Response(int(flavour), text="握手不行")
        return nas.handler(request)

    nas.transport = httpx.MockTransport(handler)


def test_a_connection_failure_says_the_next_step_exactly_once(
    nas: FakeNas, clock: FakeClock
) -> None:
    """① 连不上时那句提示在 ``reason`` 里**恰好出现一次**（原来两处各拼一遍）。"""
    _offline(nas)
    reason = make_client(nas, clock).status(refresh=True).reason

    assert TIP in reason, "该有的提示不能一起删掉"
    assert reason.count(TIP) == 1, reason
    assert DOUBLE_PERIOD not in reason, reason
    assert reason.endswith("核对「备份」里的地址与网络。"), "提示语仍然收尾在那一句上"


@pytest.mark.parametrize(
    ("flavour", "expected"),
    [
        ("connect", ""),
        ("timeout", ""),
        ("401", "凭据"),
        ("404", "握手端点"),
        ("500", "出错了"),
        ("not-a-handshake", "协议版本"),
    ],
)
def test_no_status_reason_carries_a_double_period(
    nas: FakeNas, clock: FakeClock, flavour: str, expected: str
) -> None:
    """② 六档状态原因里**都不出现 `。。`**（含连不上那一档：系统错误文本自带句号）。

    参数化的是"哪一种失败"，不是"哪一条路径"：状态这一层要经得起地址打错、对面是别的
    服务、协议版本不认识这几种情形（它们各自的文案都不该被拼出空句号）。
    """
    if flavour == "connect":
        _offline(nas)
    elif flavour == "timeout":
        nas.fail = httpx.ReadTimeout("timed out")
    else:
        _fails_at_handshake(nas, flavour)

    reason = make_client(nas, clock).status(refresh=True).reason

    assert DOUBLE_PERIOD not in reason, reason
    if expected:
        assert expected in reason, reason


@pytest.mark.parametrize("where", ["handshake", "upload", "list", "download", "delete"])
def test_no_failure_path_repeats_the_next_step(
    nas: FakeNas,
    clock: FakeClock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    """③ 五条失败路径各一次：**提示语不重复、句号不叠**。

    "用户会看到的那句话"在这几条路上形态不同——四条抛出来（上传 / 下载 / 删除 / 握手），
    列表那一条**只记日志**（读面按约定回 ``None``），所以那一档从日志里取。
    """
    if where == "handshake":
        _offline(nas)  # 这一条路要的就是"握手那一步连不上"
    else:
        _fails_after_handshake(nas)
    client = make_client(nas, clock)
    with caplog.at_level("WARNING", logger="app.services.backup_provider"):
        message = _failure_message(client, tmp_path, where, caplog)

    assert message, where
    assert message.count(TIP) <= 1, message
    assert DOUBLE_PERIOD not in message, message


def _failure_message(
    client: BackupProviderClient,
    tmp_path: Path,
    where: str,
    caplog: pytest.LogCaptureFixture,
) -> str:
    """那条路失败时留给用户的那句话（抛出来的原文，或日志里那一行）。"""
    if where == "handshake":
        return client.status(refresh=True).reason
    if where == "upload":
        with pytest.raises(RemoteClientError) as raised:
            client.upload(make_pending(tmp_path / "in"))
        return str(raised.value)
    if where == "list":
        assert client.list_snapshots() is None, "列表失败按约定回 None"
        return caplog.records[-1].getMessage()
    if where == "download":
        with pytest.raises(RemoteClientError) as raised:
            client.download_snapshot(DEVICE, SNAPSHOT_SEGMENT, tmp_path / "pkg.tar.gz")
        return str(raised.value)
    with pytest.raises(RemoteClientError) as raised:
        client.delete_snapshot(DEVICE, SNAPSHOT_SEGMENT)
    return str(raised.value)
