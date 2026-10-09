r"""握手端点 ``GET /backup/handshake``（M5 阶段 1）的契约。

镜像同构：``app/api/v1/backup.py`` + ``app/api/v1/schemas.py`` 的 Backup* 那一段 → 本文件。

五件事，一件都不能少：

1. **键集合**（方案 §7 阶段 1 的完成判据）：响应体的形状就是契约，多一个字段少一个字段
   客户端都得跟着改——而"多一个"在 JSON 里没人会报错；
2. **能力集字段齐**，且取值来自**真实契约本身**（``max_blob_bytes`` 是本模块那个常量、
   ``keep`` / ``quota_bytes`` 是引导级配置）——两处各写一份数字必然漂；
3. **桶缺失时握手仍然 200**，``capabilities.snapshot.available=false`` + 一句**可执行的
   下一步**（方案 R5 的判据）。这条是"启动不校验桶"（D6）在契约上的样子；
4. **枚举只认有 manifest 的**：孤儿 blob（上传中断）留着不删、**不列**（方案 §1.2 规矩 3）；
5. **额度如实算**：份数 / 字节 / keep / quota 四个数一个都不能编。

**本文件整份标 ``local``**：它只拼装响应（假 store）、只读路由表，**一个存储都不连**
——与本机档那台机器最相关的那几条断言（桶没了也不能把握手打挂）正是要在这条路上跑绿的。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.auth import current_caller
from app.api.v1 import backup
from app.api.v1.router import api_router, local_router
from app.core.config import API_VERSION, get_settings
from app.core.services import get_kb_services, get_services, reset_services
from app.core.storage import reset_stores
from app.main import create_app
from app.models.enums import ApiKeyPermission
from app.services.api_key import ApiKeyService, Caller
from app.services.backup_store import BLOB_NAME, MANIFEST_NAME, BackupBucketStatus, BackupObjectInfo
from app.storage.base import ApiKeyRecord

pytestmark = pytest.mark.local

#: 握手响应**恰好**是这几个键（方案 §1.3 的契约）。
CONTRACT_KEYS = {
    "provider",
    "protocol_version",
    "app_version",
    "api_version",
    "capabilities",
    "caller",
    "devices",
    "server_time",
}

CAPABILITY_GROUPS = {"snapshot", "restore", "retention"}

SNAPSHOT_KEYS = {
    "available",
    "unavailable_reason",
    "transport",
    "format",
    "manifest",
    "checksum",
    "max_blob_bytes",
    "max_snapshots_per_device",
    "encryption",
}

RESTORE_KEYS = {"point_in_time", "manifest_listing", "download", "partial_restore"}

RETENTION_KEYS = {"policy", "keep", "quota_bytes", "used_bytes", "snapshots"}

DEVICE_KEYS = {"device_id", "device_name", "snapshots", "bytes", "latest_snapshot_id", "latest_at"}


# ------------------------------------------------------------------- 假 store


class _FakeStore:
    """只实现握手摸到的那几个方法（**不是** ``BackupStore`` 的替身）。

    真实现（键布局、分片上传、异常翻译）在 ``services/backup_store.py`` 里，那一层不靠
    这里的小假货证明；这里要钉的是**契约**：握手从桶里读到了什么、如实报成了什么样。
    """

    def __init__(self, objects: dict[str, bytes], *, available: bool = True) -> None:
        self._objects = dict(objects)
        self._available = available

    def status(self) -> BackupBucketStatus:
        if self._available:
            return BackupBucketStatus(True)
        return BackupBucketStatus(False, "对象存储里还没有备份桶 kylab-backup：建它——`mc mb`")

    def all_prefix(self) -> str:
        return "backup/"

    def device_prefix(self, device_id: str) -> str:
        return f"backup/{device_id}/"

    def snapshot_prefix(self, device_id: str, snapshot_id: str) -> str:
        return f"backup/{device_id}/{snapshot_id}/"

    def blob_key(self, device_id: str, snapshot_id: str) -> str:
        return self.snapshot_prefix(device_id, snapshot_id) + BLOB_NAME

    def manifest_key(self, device_id: str, snapshot_id: str) -> str:
        return self.snapshot_prefix(device_id, snapshot_id) + MANIFEST_NAME

    def list_prefix(self, prefix: str) -> list[BackupObjectInfo]:
        return [
            BackupObjectInfo(key=key, size=len(data), modified=_STAMP)
            for key, data in self._objects.items()
            if key.startswith(prefix)
        ]

    def get_blob(self, key: str) -> bytes:
        if key not in self._objects:
            raise FileNotFoundError(key)
        return self._objects[key]


_STAMP = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _blob(body: bytes) -> bytes:
    return body


def _manifest(
    *,
    device_id: str = "dev-1",
    snapshot_id: str = "2026-10-05T12-00-00Z-ab12cd34",
    created_at: str = "2026-10-05T12:00:00Z",
    device_name: str = "小又的台式机",
    kind: str = "manual",
    counts: dict[str, int] | None = None,
    size: int = 5,
    body: bytes = b"hello",
) -> bytes:
    return json.dumps(
        {
            "format": "kylab-backup",
            "format_version": 1,
            "snapshot_id": f"{device_id}-{snapshot_id}",
            "device_id": device_id,
            "device_name": device_name,
            "created_at": created_at,
            "kind": kind,
            "app_version": "0.1.1",
            "schema_version": 2,
            "blob": {
                "name": BLOB_NAME,
                "bytes": size,
                "sha256": hashlib.sha256(body).hexdigest(),
            },
            "counts": counts if counts is not None else {"conversations": 12, "messages": 340},
            "skipped": [{"name": "大报告.pptx", "size_bytes": 90000000, "reason": "too_large"}],
            "encryption": "none",
        }
    ).encode()


def _point(
    device_id: str,
    snapshot_id: str,
    *,
    body: bytes = b"hello",
    created_at: str = "2026-10-05T12:00:00Z",
    device_name: str = "小又的台式机",
    **extra: Any,
) -> dict[str, bytes]:
    """一份完整的恢复点（blob + manifest），键就是桶里那两条。"""
    prefix = f"backup/{device_id}/{snapshot_id}/"
    return {
        f"{prefix}{BLOB_NAME}": body,
        f"{prefix}{MANIFEST_NAME}": _manifest(
            device_id=device_id,
            snapshot_id=snapshot_id,
            created_at=created_at,
            device_name=device_name,
            size=len(body),
            body=body,
            **extra,
        ),
    }


def _api_key_caller(permission: ApiKeyPermission = ApiKeyPermission.READWRITE) -> Caller:
    return Caller(
        api_key=ApiKeyRecord(id="key_1", name="本机后端", key_hash="x", permission=permission)
    )


class _FakeServices:
    """鉴权那一层只用得到 ``api_keys.check_access``。

    用**真实现**配一份空壳（``check_access`` 对管理员会话与 API Key 两种形态都不碰存储，
    见它自己的前两个分支）：这样"只读 key → 403"这条判据验的是真规则，而不是这里
    再抄一遍。换成一份手写的假判定，两处迟早会漂——而漂的方向正是"越权放行"。
    """

    def __init__(self) -> None:
        self.api_keys = ApiKeyService(None)  # type: ignore[arg-type]


def _override(app: Any, store: Any, caller: Caller | None) -> None:
    """把"用哪个桶"与"谁在调用"换掉，其余保持生产那条路。

    **两个根都要换**（2026-10-08 拆组合根之后）：端点自己注入 `Services`，
    而鉴权那几件（`require_*` / `current_caller`，见 `api/auth.py`）注入的是 `KbServices`
    ——只换 `get_services` 的话，鉴权那条依赖会去建真的服务图（本文件标 local 就是为了
    不连 PG）。假对象是鸭子类型，两边都能塞。
    """
    app.dependency_overrides[backup.backup_store_dep] = lambda: store
    app.dependency_overrides[get_services] = lambda: _FakeServices()
    app.dependency_overrides[get_kb_services] = lambda: _FakeServices()
    if caller is not None:
        app.dependency_overrides[current_caller] = lambda: caller


# --------------------------------------------------------------------- 1. 键集合


def test_the_body_carries_exactly_the_contract_keys() -> None:
    """响应体的键集合**恰好等于**契约那八个：多一个少一个都是要改客户端的变更。"""
    body = backup.build_handshake(_FakeStore({}), _api_key_caller()).model_dump(
        mode="json", by_alias=True
    )

    assert set(body) == CONTRACT_KEYS
    assert set(body["capabilities"]) == CAPABILITY_GROUPS
    assert set(body["capabilities"]["snapshot"]) == SNAPSHOT_KEYS
    assert set(body["capabilities"]["restore"]) == RESTORE_KEYS
    assert set(body["capabilities"]["retention"]) == RETENTION_KEYS
    # `caller` 与知识库握手**同一份形状**（复用 `ProviderCallerOut`）：客户端可以共用解析
    assert set(body["caller"]) == {
        "kind",
        "permission",
        "is_admin",
        "can_write",
        "knowledge_base_ids",
    }


def test_the_protocol_version_is_a_growing_integer() -> None:
    """``protocol_version`` 是整数且当前为 1；``provider`` 是 ``backup``。

    与知识库那个 1 是**两件事**：两个提供者各自演进，客户端分开判。
    """
    body = backup.build_handshake(_FakeStore({}), _api_key_caller()).model_dump(
        mode="json", by_alias=True
    )

    assert backup.PROTOCOL_VERSION == 1
    assert body["protocol_version"] == 1
    assert isinstance(body["protocol_version"], int)
    assert body["provider"] == "backup"
    assert body["api_version"] == API_VERSION
    assert body["app_version"] == get_settings().app_version


# ------------------------------------------------------------- 2. 能力集的取值


def test_capabilities_take_their_numbers_from_the_real_contract() -> None:
    """能力集里的数字**不是在这里另写一份**：逐个比对它们的来源。

    ``max_blob_bytes`` 是本模块的上传上限常量，``keep`` / ``quota_bytes`` 是引导级配置——
    三处各写一份数字必然漂，而客户端会照着它们做打包与清理的取舍。
    """
    settings = get_settings()
    body = backup.build_handshake(_FakeStore({}), _api_key_caller()).model_dump(
        mode="json", by_alias=True
    )
    caps = body["capabilities"]
    snapshot = caps["snapshot"]

    assert snapshot["max_blob_bytes"] == backup.MAX_BLOB_BYTES
    assert snapshot["transport"] == backup.SNAPSHOT_TRANSPORT
    assert snapshot["format"] == backup.SNAPSHOT_FORMAT
    assert snapshot["manifest"] == backup.MANIFEST_FORMAT
    assert snapshot["checksum"] == backup.CHECKSUM
    assert snapshot["encryption"] == backup.ENCRYPTION
    assert snapshot["max_snapshots_per_device"] == settings.backup_keep
    assert caps["retention"]["keep"] == settings.backup_keep
    assert caps["retention"]["quota_bytes"] == settings.backup_quota_bytes
    # 顺带钉住当前值：上限换了数字是**契约变更**，不该悄悄发生
    assert snapshot["max_blob_bytes"] == 2 * 1024 * 1024 * 1024
    assert caps["retention"]["quota_bytes"] == 20 * 1024 * 1024 * 1024


def test_the_keep_number_appears_in_exactly_one_place() -> None:
    """``max_snapshots_per_device`` 与 ``retention.keep`` **是同一个数**（一份口径）。

    两处各算一次的表现是客户端拿到两份互相矛盾的上限（一个说还能传、一个说超了），
    而那种矛盾在界面上只会表现成"传得进去但列表不刷新"。
    """
    caps = backup.build_handshake(_FakeStore({}), _api_key_caller()).capabilities

    assert caps.snapshot.max_snapshots_per_device == caps.retention.keep


def test_every_capability_group_is_filled_in() -> None:
    """字段齐：每一组都回实值，没有一组是空对象或 ``None``。"""
    caps = backup.build_handshake(_FakeStore({}), _api_key_caller()).capabilities

    assert caps.snapshot.available is True
    assert caps.snapshot.unavailable_reason == ""
    assert caps.restore.model_dump() == {
        "point_in_time": True,
        "manifest_listing": True,
        "download": True,
        "partial_restore": True,
    }
    assert caps.retention.policy == "keep_n"
    assert caps.retention.snapshots == 0
    assert caps.retention.used_bytes == 0


# ------------------------------------------------------- 3. 桶缺失也照样握手


def test_a_missing_bucket_still_handshakes_but_reports_unavailable() -> None:
    """**桶没建出来时握手仍是 200**，``available=false`` + 一句可执行的下一步（方案 R5）。

    这是"启动不校验桶"（决策点 D6）的判据：配错了不该把 NAS 的启动搞崩，
    而该在**客户端问第一句话时**如实说清"这里还没准备好、下一步敲什么"。
    数值那两栏此时**不是零、是数不出来**——所以判据只能是 ``available``。
    """
    handshake = backup.build_handshake(_FakeStore({}, available=False), _api_key_caller())

    assert handshake.capabilities.snapshot.available is False
    reason = handshake.capabilities.snapshot.unavailable_reason
    assert reason, "不可用时必须给一句可执行的下一步"
    assert "mc mb" in reason, reason
    assert handshake.devices == []
    assert handshake.capabilities.retention.used_bytes == 0
    assert handshake.capabilities.retention.snapshots == 0
    # 配了多少仍然如实报（它们来自配置，不依赖桶在不在）
    settings = get_settings()
    assert handshake.capabilities.retention.keep == settings.backup_keep
    assert handshake.capabilities.retention.quota_bytes == settings.backup_quota_bytes


# ------------------------------------------------------------- 4. 枚举与额度


def test_devices_summarise_their_recovery_points() -> None:
    """每台设备一段摘要：几份、多大、最近一份是什么时候（方案 §1.4）。"""
    store = _FakeStore(
        {
            **_point("dev-1", "2026-10-05T12-00-00Z-aaaaaaaa", body=b"12345"),
            **_point(
                "dev-1",
                "2026-10-05T13-00-00Z-bbbbbbbb",
                body=b"1234567890",
                created_at="2026-10-05T13:00:00Z",
                device_name="小又的笔记本",
            ),
        }
    )

    handshake = backup.build_handshake(store, _api_key_caller())

    assert [device.device_id for device in handshake.devices] == ["dev-1"]
    brief = handshake.devices[0]
    assert brief.snapshots == 2
    assert brief.bytes == 15
    assert brief.latest_snapshot_id == "2026-10-05T13-00-00Z-bbbbbbbb"
    assert brief.latest_at == datetime(2026, 10, 5, 13, 0, tzinfo=UTC)
    assert brief.device_name == "小又的笔记本"
    # 额度那一段算的是**全部设备**（与可见范围同一口径）
    assert handshake.capabilities.retention.snapshots == 2
    assert handshake.capabilities.retention.used_bytes == 15


def test_devices_are_ordered_by_their_latest_snapshot() -> None:
    """设备按"最近一份"倒序：界面上第一眼看到的就是刚备过的那台。"""
    store = _FakeStore(
        {
            **_point("dev-old", "2026-10-01T09-00-00Z-aaaaaaaa", created_at="2026-10-01T09:00:00Z"),
            **_point("dev-new", "2026-10-06T09-00-00Z-bbbbbbbb", created_at="2026-10-06T09:00:00Z"),
        }
    )

    handshake = backup.build_handshake(store, _api_key_caller())

    assert [device.device_id for device in handshake.devices] == ["dev-new", "dev-old"]


def test_an_orphan_blob_is_not_enumerated() -> None:
    """**枚举只认有 manifest 的**（方案 §1.2 规矩 3）。

    上传中断会留下一份没有 manifest 的 blob：它**留着不删**（append-only），但谁也恢复不了
    它——所以它不是一个恢复点，不列、也不算进额度。这条同时满足"append-only"
    与"完整才可见"。
    """
    store = _FakeStore(
        {
            **{f"backup/dev-1/2026-10-05T14-00-00Z-deadbeef/{BLOB_NAME}": b"half-uploaded"},
            **_point("dev-1", "2026-10-05T12-00-00Z-ab12cd34", body=b"12345"),
        }
    )

    handshake = backup.build_handshake(store, _api_key_caller())

    assert [device.latest_snapshot_id for device in handshake.devices] == [
        "2026-10-05T12-00-00Z-ab12cd34"
    ]
    assert handshake.devices[0].snapshots == 1
    assert handshake.devices[0].bytes == 5
    assert handshake.capabilities.retention.snapshots == 1


def test_a_manifest_without_a_blob_is_not_enumerated_either() -> None:
    """只有 manifest、没有 blob 的怪状态同样不算一个恢复点（它取不出东西）。"""
    prefix = "backup/dev-1/2026-10-05T12-00-00Z-ab12cd34/"
    store = _FakeStore({f"{prefix}{MANIFEST_NAME}": _manifest()})

    handshake = backup.build_handshake(store, _api_key_caller())

    assert handshake.devices == []
    assert handshake.capabilities.retention.snapshots == 0


def test_a_broken_manifest_makes_the_point_invisible() -> None:
    """清单读不出来（不是 JSON / 不是对象）时**不算一个可用的恢复点**——不猜着读。"""
    prefix = "backup/dev-1/2026-10-05T12-00-00Z-ab12cd34/"
    store = _FakeStore(
        {f"{prefix}{BLOB_NAME}": b"hello", f"{prefix}{MANIFEST_NAME}": b"{ not json"}
    )

    handshake = backup.build_handshake(store, _api_key_caller())

    assert handshake.devices == []


def test_counts_and_skipped_come_from_the_manifest() -> None:
    """计数与被跳过项来自清单（服务端只归一类型、不解释业务字段）。"""
    store = _FakeStore(_point("dev-1", "2026-10-05T12-00-00Z-ab12cd34"))

    points = backup._points(store)
    row = backup._snapshot_out(points[0])

    assert row.counts == {"conversations": 12, "messages": 340}
    assert row.skipped == [{"name": "大报告.pptx", "size_bytes": 90000000, "reason": "too_large"}]
    assert row.kind == "manual"
    assert row.schema_version == 2
    assert row.encryption == "none"
    assert row.sha256 == hashlib.sha256(b"hello").hexdigest()


# ------------------------------------------------------------------ 5. 调用者


def test_a_read_only_key_is_reported_as_unable_to_write() -> None:
    """只读 key：``caller.can_write=false``、``is_admin`` 如实报 false（方案 R9 那条边界）。

    界面据此隐藏上传与删除入口，而不是摆出来等着 403。
    """
    handshake = backup.build_handshake(_FakeStore({}), _api_key_caller(ApiKeyPermission.READONLY))

    assert handshake.caller.kind == "api_key"
    assert handshake.caller.permission is ApiKeyPermission.READONLY
    assert handshake.caller.is_admin is False
    assert handshake.caller.can_write is False


def test_server_time_is_timezone_aware_utc() -> None:
    """``server_time`` 带时区（规范 §1.2）：客户端拿它显示"上次确认是什么时候"。"""
    handshake = backup.build_handshake(_FakeStore({}), _api_key_caller())

    assert handshake.server_time.tzinfo is not None
    assert handshake.server_time.utcoffset() == UTC.utcoffset(None)


# --------------------------------------------- 6. 挂在服务器档、鉴权在它前面


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """按指定档位现建一个 app（**不碰模块级 ``app``**，与知识库握手那份用例同一手法）。"""

    def _build(deployment: str):
        monkeypatch.setenv("KYLAB_DEPLOYMENT", deployment)
        monkeypatch.setenv("KYLAB_DATABASE_URL", "")
        get_settings.cache_clear()
        reset_services()
        reset_stores()
        return create_app()

    yield _build
    get_settings.cache_clear()
    reset_services()
    reset_stores()


def test_the_server_deployment_serves_the_family(build_app: Any) -> None:
    """服务器档：七条端点真的都在（**由 OpenAPI 判据，而不是读内存里的 router 对象**）。

    顺带钉住每一条的 summary——没有说明的端点，读文档的人只知道"有这么个地址"。
    """
    paths = build_app("server").openapi()["paths"]

    assert {"get", "put", "delete"} & set(
        paths["/api/v1/backup/snapshots/{device_id}/{snapshot_id}"]
    )
    for path in (
        "/api/v1/backup/handshake",
        "/api/v1/backup/snapshots",
        "/api/v1/backup/snapshots/{device_id}/{snapshot_id}",
        "/api/v1/backup/snapshots/{device_id}/{snapshot_id}/blob",
        "/api/v1/backup/snapshots/{device_id}/{snapshot_id}/manifest",
    ):
        assert path in paths, path
    assert paths["/api/v1/backup/handshake"]["get"]["summary"]


def test_the_local_deployment_does_not_serve_the_family(build_app: Any) -> None:
    """本机档**不挂**它（方案 §1.5 的边界、`api/v1/router.py` 的不挂清单）。

    本机档是备份提供者的**客户端**：它调这些端点，不提供它们（那台持 S3 凭据的是 NAS）。
    """
    paths = build_app("local").openapi()["paths"]

    assert "/api/v1/local/status" in paths, "本机档那张表本身要在这（自检）"
    assert "/api/v1/backup/handshake" not in paths
    assert "/api/v1/backup/snapshots" not in paths


def test_the_backup_router_is_only_in_the_server_table() -> None:
    """路由表对象级的那一眼：``backup.router`` 只在 ``api_router`` 的挂载清单里。"""
    assert id(backup.router) in {id(entry.original_router) for entry in api_router.routes}
    assert id(backup.router) not in {id(entry.original_router) for entry in local_router.routes}


def test_no_credentials_is_a_401_and_carries_no_handshake_body(build_app: Any) -> None:
    """没有凭据 → **401 + 统一错误信封**，响应体里没有握手那八位。

    与知识库握手同一条口径：凭据问题**不进响应体**，客户端据此把"改钥匙"（401/403）
    与"改地址"（连不上）分成两档。

    用 ``dependency_overrides`` 换掉 ``get_services``：本用例验的是鉴权挡在前面，
    不该为了它去连一台 PG（也正是本文件标 ``local`` 的理由）。
    """
    app = build_app("server")
    _override(app, _FakeStore({}), caller=None)
    try:
        response = TestClient(app).get("/api/v1/backup/handshake")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401, response.text
    body = response.json()
    assert body["code"] == "unauthorized"
    assert set(body) == {"code", "message"}
    assert not (CONTRACT_KEYS & set(body))


def test_the_endpoint_is_wired_to_the_store_dependency(build_app: Any) -> None:
    """真打一次 HTTP：``backup_store_dep`` 换掉之后，响应体就是那份契约。

    这条补的是"纯拼装"用例够不着的一格：**依赖注入真的接上了**（把假 store 换成真的
    就是生产那条路）。鉴权在这一层同样是真的——只把"谁在调用"与"用哪个桶"换掉。
    """
    app = build_app("server")
    _override(
        app,
        _FakeStore(_point("dev-1", "2026-10-05T12-00-00Z-ab12cd34")),
        _api_key_caller(),
    )
    try:
        response = TestClient(app).get("/api/v1/backup/handshake")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert [device["device_id"] for device in body["devices"]] == ["dev-1"]
