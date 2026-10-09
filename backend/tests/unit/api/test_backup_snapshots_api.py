r"""快照那一族端点（M5 阶段 1）的契约：上传两段、幂等三态、拒收、下载、删除。

镜像同构：``app/api/v1/backup.py`` 的六条快照端点 → 本文件。

**append-only 的四条硬规矩在这里逐条变成断言**（方案 §1.2，四条都要有用例）：

1. **不覆盖**：同路径同 sha256 → 201 之后 200 no-op；同路径不同 sha256 → **409**
   （``test_replaying_the_same_bytes_is_a_200_no_op`` / ``..._different_bytes_...``）；
2. **只能整份删**：删除的粒度是一个恢复点（``test_deleting_a_point_takes_both_objects``），
   没有"删某个对象"的入口——桶里那两个对象每次一起走；
3. **枚举只认有 manifest 的**：孤儿 blob 留着不删、不列
   （``test_an_orphan_blob_is_invisible_until_its_manifest_lands``）；
4. **服务端绝不替用户删**：保留份数 / 配额超限**报错**（409），
   ``test_the_keep_limit_refuses_instead_of_evicting`` / ``..._quota_...``。

**假 store，不打真 MinIO**：本文件整份标 ``local``，一个网络请求都不出机器。
假的那份只实现端点摸到的表面（键布局 + 读写 + 列举 + 删除），真实现那层
（``services/backup_store.py``：分片上传、异常翻译、桶探测）另有它的用例守着。
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
from app.core.config import get_settings
from app.core.services import get_kb_services, get_services, reset_services
from app.core.storage import reset_stores
from app.main import create_app
from app.models.enums import ApiKeyPermission
from app.services.api_key import ApiKeyService, Caller
from app.services.backup_store import BLOB_NAME, MANIFEST_NAME, BackupBucketStatus, BackupObjectInfo
from app.storage.base import ApiKeyRecord

pytestmark = pytest.mark.local

DEVICE = "6f3a1c2e-1111-4222-8333-444455556666"
SNAPSHOT = "2026-10-05T12-00-00Z-ab12cd34"
OTHER_SNAPSHOT = "2026-10-05T13-00-00Z-deadbeef"

_STAMP = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------- 假 store


class FakeBackupStore:
    """假备份桶：``键 → 字节`` + ``键 → 上传时写进元数据的 sha256``。

    键布局照真实现抄（``backup/<device>/<snapshot>/<名字>``），因为**路径即幂等键**
    这条规矩就是靠布局成立的；布局写错了，这一整批断言会以"看起来都对但抓不住东西"的方式飘。
    """

    def __init__(self, objects: dict[str, bytes] | None = None, *, available: bool = True) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.digests: dict[str, str] = {}
        self.available = available
        self.ensure_calls = 0

    # -- 探测与建桶 ------------------------------------------------------

    def status(self) -> BackupBucketStatus:
        if self.available:
            return BackupBucketStatus(True)
        return BackupBucketStatus(False, "对象存储里还没有备份桶：建它——`mc mb`")

    def ensure_bucket(self) -> None:
        self.ensure_calls += 1
        self.available = True

    # -- 键布局（照真实现） ----------------------------------------------

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

    # -- 读写 ------------------------------------------------------------

    def put_blob(self, key: str, fileobj: Any, *, sha256: str) -> BackupObjectInfo:
        fileobj.seek(0)
        data = fileobj.read()
        self.objects[key] = data
        self.digests[key] = sha256
        return BackupObjectInfo(key=key, size=len(data), modified=_STAMP, sha256=sha256)

    def put_manifest(self, key: str, data: bytes, *, sha256: str) -> BackupObjectInfo:
        self.objects[key] = data
        self.digests[key] = sha256
        return BackupObjectInfo(key=key, size=len(data), modified=_STAMP, sha256=sha256)

    def get_blob(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    def head(self, key: str) -> BackupObjectInfo | None:
        if key not in self.objects:
            return None
        return BackupObjectInfo(
            key=key,
            size=len(self.objects[key]),
            modified=_STAMP,
            sha256=self.digests.get(key, ""),
        )

    def open_stream(self, key: str) -> tuple[Any, BackupObjectInfo]:
        if key not in self.objects:
            raise FileNotFoundError(key)
        data = self.objects[key]
        return _Body(data), BackupObjectInfo(
            key=key, size=len(data), modified=_STAMP, sha256=self.digests.get(key, "")
        )

    def list_prefix(self, prefix: str) -> list[BackupObjectInfo]:
        return [
            BackupObjectInfo(key=key, size=len(data), modified=_STAMP)
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]

    def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self.objects if key.startswith(prefix)]
        for key in keys:
            del self.objects[key]
            self.digests.pop(key, None)
        return len(keys)


class _Body:
    """假的对象存储响应体：只实现 ``read`` / ``close``（端点就用到这两个）。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._data[self._at : self._at + size]
        self._at += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------ 用例脚手架


class _FakeServices:
    """鉴权那一层只用得到 ``api_keys.check_access``（真实现，配一份空壳）。

    ``check_access`` 对管理员会话与 API Key 两种形态都不碰存储（见它自己的前两个分支），
    所以"只读 key → 403"这条判据验的是**真规则**，而不是这里再抄一遍。
    """

    def __init__(self) -> None:
        self.api_keys = ApiKeyService(None)  # type: ignore[arg-type]


def _api_key_caller(permission: ApiKeyPermission) -> Caller:
    return Caller(
        api_key=ApiKeyRecord(id="key_1", name="本机后端", key_hash="x", permission=permission)
    )


class _Env:
    """一套装好的环境：真路由 + 真鉴权 + 假桶 + 假调用者。"""

    def __init__(self, app: Any, store: FakeBackupStore) -> None:
        self.app = app
        self.store = store
        self.client = TestClient(app)

    def as_caller(self, permission: ApiKeyPermission) -> None:
        caller = _api_key_caller(permission)
        self.app.dependency_overrides[current_caller] = lambda: caller


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """按指定档位现建一个 app（**不碰模块级 ``app``**，与知识库握手那份用例同一手法）。"""

    def _build(deployment: str = "server"):
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


@pytest.fixture
def env(build_app: Any) -> Iterator[_Env]:
    app = build_app()
    store = FakeBackupStore()
    app.dependency_overrides[backup.backup_store_dep] = lambda: store
    app.dependency_overrides[get_services] = lambda: _FakeServices()
    # 鉴权那几件注入的是 KB 根（`api/auth.py` 的 `KbServices`）——两个根都换掉，
    # 否则鉴权那条依赖会去建真的服务图（本文件标 local 就是为了不连 PG）。
    app.dependency_overrides[get_kb_services] = lambda: _FakeServices()
    app.dependency_overrides[current_caller] = lambda: _api_key_caller(ApiKeyPermission.READWRITE)
    yield _Env(app, store)
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ 小工具


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _blob_url(device: str = DEVICE, snapshot: str = SNAPSHOT) -> str:
    return f"/api/v1/backup/snapshots/{device}/{snapshot}/blob"


def _manifest_url(device: str = DEVICE, snapshot: str = SNAPSHOT) -> str:
    return f"/api/v1/backup/snapshots/{device}/{snapshot}/manifest"


def _point_url(device: str = DEVICE, snapshot: str = SNAPSHOT) -> str:
    return f"/api/v1/backup/snapshots/{device}/{snapshot}"


def _put_blob(
    env: _Env,
    body: bytes,
    *,
    device: str = DEVICE,
    snapshot: str = SNAPSHOT,
    sha256: str | None = None,
    bytes_: int | None = None,
) -> Any:
    return env.client.put(
        _blob_url(device, snapshot),
        params={
            "sha256": _sha(body) if sha256 is None else sha256,
            "bytes": len(body) if bytes_ is None else bytes_,
        },
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )


def _manifest_body(*, device: str = DEVICE, snapshot: str = SNAPSHOT, **extra: Any) -> bytes:
    payload: dict[str, Any] = {
        "format": "kylab-backup",
        "format_version": 1,
        "snapshot_id": f"{device}-{snapshot}",
        "device_id": device,
        "device_name": "小又的台式机",
        "created_at": "2026-10-05T12:00:00Z",
        "kind": "manual",
        "app_version": "0.1.1",
        "schema_version": 2,
        "blob": {"name": BLOB_NAME, "bytes": 5, "sha256": _sha(b"hello")},
        "counts": {"conversations": 12, "messages": 340},
        "skipped": [{"name": "大报告.pptx", "size_bytes": 90000000, "reason": "too_large"}],
        "encryption": "none",
    }
    payload.update(extra)
    return json.dumps(payload).encode()


def _put_manifest(env: _Env, body: bytes, *, device: str = DEVICE, snapshot: str = SNAPSHOT) -> Any:
    return env.client.put(
        _manifest_url(device, snapshot),
        content=body,
        headers={"Content-Type": "application/json"},
    )


def _upload_point(
    env: _Env,
    body: bytes = b"hello",
    *,
    device: str = DEVICE,
    snapshot: str = SNAPSHOT,
    created_at: str = "2026-10-05T12:00:00Z",
) -> None:
    """传一份完整的恢复点（先 blob 后 manifest——这条顺序本身也是契约，见用例）。"""
    assert _put_blob(env, body, device=device, snapshot=snapshot).status_code == 201
    manifest = _manifest_body(device=device, snapshot=snapshot, created_at=created_at)
    assert _put_manifest(env, manifest, device=device, snapshot=snapshot).status_code == 201


# --------------------------------------------------------------- 幂等三态


def test_a_new_snapshot_blob_is_a_201(env: _Env) -> None:
    """第一份：201，响应体就是契约那三位（``snapshot_id`` / ``bytes`` / ``sha256``）。"""
    response = _put_blob(env, b"hello")

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"snapshot_id", "bytes", "sha256"}
    assert body == {"snapshot_id": SNAPSHOT, "bytes": 5, "sha256": _sha(b"hello")}
    assert env.store.objects[env.store.blob_key(DEVICE, SNAPSHOT)] == b"hello"


def test_replaying_the_same_bytes_is_a_200_no_op(env: _Env) -> None:
    """重放同一份内容：**200 no-op**（不是 409，也不是再存一份）。

    这条同时接住"上次 blob 传完了、manifest 没传成"的重试——那条路上**幂等是必需的**，
    否则客户端永远修不回来（它手里还是那一份内容）。
    """
    assert _put_blob(env, b"hello").status_code == 201
    before = dict(env.store.objects)

    response = _put_blob(env, b"hello")

    assert response.status_code == 200, response.text
    assert response.json()["sha256"] == _sha(b"hello")
    assert env.store.objects == before, "同内容重放不该改动桶里那份"


def test_the_same_path_with_different_bytes_is_a_409(env: _Env) -> None:
    """同路径不同内容：**409，永不覆盖**（方案 R7 的判据）。

    客户端那份内容换了（或名字撞了），服务端绝不猜"哪份更新"——报错，让它换一个
    ``snapshot_id``（恢复点的名字里本来就带内容哈希前 8 位，撞上就说明有别的东西不对）。
    """
    assert _put_blob(env, b"hello").status_code == 201
    before = dict(env.store.objects)

    response = _put_blob(env, b"changed")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert env.store.objects == before, "409 之后桶里那份必须原封不动"


def test_an_object_without_a_stored_digest_is_treated_as_different(env: _Env) -> None:
    """桶里那份**没有元数据**（别人手工放的）时：宁可 409，也不覆盖。

    "内容未知"与"内容相同"是两件事，把前者当后者就等于让一次重放悄悄换掉一份快照。
    """
    key = env.store.blob_key(DEVICE, SNAPSHOT)
    env.store.objects[key] = b"someone-elses-object"  # digests 里没有它

    response = _put_blob(env, b"hello")

    assert response.status_code == 409, response.text
    assert env.store.objects[key] == b"someone-elses-object"


# ------------------------------------------------------------- 400 / 413


def test_a_sha256_mismatch_is_a_400_and_nothing_lands(env: _Env) -> None:
    """sha256 与实收不符 → **400，且一个字节都不落桶**（方案 §1.3 / R6 的判据）。

    "服务端边收边算"这条能力的价值就在这一条断言上：路 A 那种"客户端自报"的写法，
    服务端根本无法证伪。
    """
    response = _put_blob(env, b"hello", sha256=_sha(b"bye"))

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"
    assert env.store.objects == {}, "校验没过就不该有对象进桶"


def test_a_byte_count_mismatch_is_a_400_and_nothing_lands(env: _Env) -> None:
    """声明的字节数与实收不符 → 400，同样不落桶。"""
    response = _put_blob(env, b"hello", bytes_=999)

    assert response.status_code == 400, response.text
    assert env.store.objects == {}


def test_a_malformed_sha256_is_rejected_before_reading_the_body(env: _Env) -> None:
    """``sha256`` 形状不对（长度/字符）→ 400，连读都不读。"""
    response = _put_blob(env, b"hello", sha256="not-a-hash")

    assert response.status_code == 400, response.text
    assert env.store.objects == {}


def test_a_declared_size_over_the_cap_is_a_413(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """声明的字节数超上限 → **413**，不落桶（方案 R6 的判据）。"""
    monkeypatch.setattr(backup, "MAX_BLOB_BYTES", 10)

    response = _put_blob(env, b"0123456789ab")

    assert response.status_code == 413, response.text
    assert response.json()["code"] == "payload_too_large"
    assert env.store.objects == {}


def test_a_body_over_the_cap_is_a_413_mid_stream(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实收超上限 → 413（边收边数，读到头才发现的那些也算），同样不落桶。"""
    monkeypatch.setattr(backup, "MAX_BLOB_BYTES", 10)

    response = _put_blob(env, b"0123456789ab", bytes_=9, sha256=_sha(b"012345678"))

    assert response.status_code == 413, response.text
    assert env.store.objects == {}


def test_a_bad_path_segment_is_a_400(env: _Env) -> None:
    """路径段不合法 → 400。

    ``snapshot_id`` 里的冒号是**故意**拒掉的：命名规则是 ``YYYY-MM-DDTHH-MM-SSZ``
    （冒号已经换成短横），同一条时间戳写成两种样子会让"路径即幂等键"这条规矩失效
    ——同一份快照在桶里长出两个恢复点。
    """
    colon = _put_blob(env, b"hello", snapshot="2026-10-05T12:00:00Z-ab12cd34")
    assert colon.status_code == 400, colon.text

    spaces = env.client.put(
        f"/api/v1/backup/snapshots/dev 1/{SNAPSHOT}/blob",
        params={"sha256": _sha(b"hello"), "bytes": 5},
        content=b"hello",
    )
    assert spaces.status_code == 400, spaces.text
    assert env.store.objects == {}


# ------------------------------------------------------------------ 只读 key


def test_a_read_only_key_cannot_upload(env: _Env) -> None:
    """只读 key 上传 → **403**（方案 R9 的判据），且桶里什么都不变。"""
    env.as_caller(ApiKeyPermission.READONLY)

    response = _put_blob(env, b"hello")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert env.store.objects == {}


def test_a_read_only_key_cannot_delete(env: _Env) -> None:
    """只读 key 删除 → 403，恢复点还在（"能下载、不能传、不能删"这条边界）。"""
    _upload_point(env)
    env.as_caller(ApiKeyPermission.READONLY)

    response = env.client.delete(_point_url())

    assert response.status_code == 403, response.text
    assert env.store.objects, "403 之后那份快照必须还在"


def test_a_read_only_key_can_still_download(env: _Env) -> None:
    """只读 key 下载与列举照常（受限只读 = 能看不能改）。"""
    _upload_point(env)
    env.as_caller(ApiKeyPermission.READONLY)

    assert env.client.get(_point_url()).status_code == 200
    assert env.client.get(_blob_url()).status_code == 200
    assert env.client.get("/api/v1/backup/snapshots").status_code == 200


def test_no_credentials_is_a_401(env: _Env) -> None:
    """没有凭据 → 401（鉴权挡在前面，与握手同一条口径）。"""
    env.app.dependency_overrides.pop(current_caller, None)

    response = env.client.get("/api/v1/backup/snapshots")

    assert response.status_code == 401, response.text


# ------------------------------------------------------------------ 清单


def test_an_orphan_blob_is_invisible_until_its_manifest_lands(env: _Env) -> None:
    """**枚举只认有 manifest 的**：传完 blob 还没传清单时，它不是一个恢复点。

    这是"上传中断"在契约上的样子：blob 留着不删（append-only），但谁也不列它——
    谁也恢复不了它。清单落地的那一刻，它才出现在列表里。
    """
    assert _put_blob(env, b"hello").status_code == 201

    listed = env.client.get("/api/v1/backup/snapshots").json()
    assert listed["items"] == []
    assert listed["total"] == 0
    assert listed["quota"]["snapshots"] == 0, "孤儿 blob 不该算进额度"

    assert _put_manifest(env, _manifest_body()).status_code == 201

    listed = env.client.get("/api/v1/backup/snapshots").json()
    assert listed["total"] == 1
    assert listed["items"][0]["snapshot_id"] == SNAPSHOT
    assert listed["items"][0]["counts"] == {"conversations": 12, "messages": 340}
    assert listed["items"][0]["skipped"] == [
        {"name": "大报告.pptx", "size_bytes": 90000000, "reason": "too_large"}
    ]
    assert listed["quota"]["snapshots"] == 1


def test_a_manifest_before_its_blob_is_a_409(env: _Env) -> None:
    """清单先于快照体 → 409（**清单是完成标记**，落空标记只会造出一个取不到东西的恢复点）。"""
    response = _put_manifest(env, _manifest_body())

    assert response.status_code == 409, response.text
    assert env.store.objects == {}


def test_the_manifest_must_belong_to_its_own_path(env: _Env) -> None:
    """清单里的 ``device_id`` / ``snapshot_id`` 与路径不符 → 400。

    一份"描述别的设备"的清单落在这条路径上，恢复时会把两边的账搅在一起。
    """
    assert _put_blob(env, b"hello").status_code == 201

    wrong_device = _put_manifest(env, _manifest_body(device="other-device"))
    assert wrong_device.status_code == 400, wrong_device.text

    wrong_snapshot = _put_manifest(env, _manifest_body(snapshot="2026-10-05T12-00-00Z-99999999"))
    assert wrong_snapshot.status_code == 400, wrong_snapshot.text
    assert env.store.manifest_key(DEVICE, SNAPSHOT) not in env.store.objects


def test_a_manifest_that_is_not_a_json_object_is_a_400(env: _Env) -> None:
    """清单必须是 JSON **对象**：坏 JSON 与数组都是 400。"""
    assert _put_blob(env, b"hello").status_code == 201

    assert _put_manifest(env, b"{ not json").status_code == 400
    assert _put_manifest(env, b"[1, 2, 3]").status_code == 400
    assert env.store.manifest_key(DEVICE, SNAPSHOT) not in env.store.objects


def test_a_manifest_over_the_cap_is_a_413(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """清单超过 1 MiB 那一档（这里把它压小）→ 413。"""
    monkeypatch.setattr(backup, "MAX_MANIFEST_BYTES", 64)
    assert _put_blob(env, b"hello").status_code == 201

    response = _put_manifest(env, _manifest_body(padding="x" * 200))

    assert response.status_code == 413, response.text
    assert env.store.manifest_key(DEVICE, SNAPSHOT) not in env.store.objects


def test_replaying_the_same_manifest_is_a_200_and_a_changed_one_is_a_409(env: _Env) -> None:
    """清单同样幂等：同内容 200、不同内容 409（它描述的是已经落桶的那一份，不能改写）。"""
    assert _put_blob(env, b"hello").status_code == 201
    assert _put_manifest(env, _manifest_body()).status_code == 201

    again = _put_manifest(env, _manifest_body())
    assert again.status_code == 200, again.text

    changed = _put_manifest(env, _manifest_body(kind="auto"))
    assert changed.status_code == 409, changed.text
    assert (
        json.loads(env.store.objects[env.store.manifest_key(DEVICE, SNAPSHOT)])["kind"] == "manual"
    )


def test_the_manifest_comes_back_verbatim(env: _Env) -> None:
    """取清单：**原样回**（不认识的字段不许被裁掉）。

    服务端不解释清单的业务字段——它是客户端写、客户端读的那份自描述文件。
    用响应模型收一遍字段看着更"类型化"，代价是把将来新增的字段悄悄删掉。
    """
    assert _put_blob(env, b"hello").status_code == 201
    written = _manifest_body(future_field={"nested": [1, 2]})
    assert _put_manifest(env, written).status_code == 201

    response = env.client.get(_point_url())

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == json.loads(written)
    assert response.json()["future_field"] == {"nested": [1, 2]}


def test_a_missing_manifest_is_a_404(env: _Env) -> None:
    """取不存在的清单 → 404（不是空对象：空对象会让客户端以为"这份什么都没备"）。"""
    response = env.client.get(_point_url())

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


# ------------------------------------------------------------------ 下载


def test_the_blob_downloads_streaming_with_length_and_digest(env: _Env) -> None:
    """流式下载：内容一致，且头里带 ``Content-Length`` 与 ``X-Kylab-Sha256``。

    恢复流程要求"下载 → 校验 sha256（不符即中止，绝不落位）"，所以下载这一条必须
    把该校验的那份哈希一起给出去——否则客户端只能先去取清单，多一次请求、也可能对不上。
    """
    body = b"kylab-snapshot-bytes" * 100
    assert _put_blob(env, body).status_code == 201

    response = env.client.get(_blob_url())

    assert response.status_code == 200, response.text
    assert response.content == body
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-length"] == str(len(body))
    assert response.headers["x-kylab-sha256"] == _sha(body)


def test_a_missing_blob_is_a_404(env: _Env) -> None:
    assert env.client.get(_blob_url()).status_code == 404


# ------------------------------------------------------------------ 删除


def test_deleting_a_point_takes_both_objects(env: _Env) -> None:
    """删除的粒度是**一个恢复点**：两个对象一起走，返回 ``removed``（方案 §1.2 规矩 2）。"""
    _upload_point(env)

    response = env.client.delete(_point_url())

    assert response.status_code == 200, response.text
    assert response.json() == {"removed": 2}
    assert env.store.objects == {}


def test_deleting_an_orphan_blob_clears_the_half_upload(env: _Env) -> None:
    """只剩孤儿 blob 时删它 → ``removed=1``。

    这是清掉"上传中断留下的半截"的唯一入口（枚举看不见它，但用户知道那份没传完）。
    """
    assert _put_blob(env, b"hello").status_code == 201

    response = env.client.delete(_point_url())

    assert response.status_code == 200, response.text
    assert response.json() == {"removed": 1}
    assert env.store.objects == {}


def test_deleting_twice_is_a_404(env: _Env) -> None:
    """再删一次 → 404：想删的那一份不在那儿了，值得知道（不是假装成功）。"""
    _upload_point(env)
    assert env.client.delete(_point_url()).status_code == 200

    response = env.client.delete(_point_url())

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


# --------------------------------------------------------- 保留份数与配额


def test_the_keep_limit_refuses_instead_of_evicting(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留份数超限 → **409**（方案 §1.2 规矩 4：服务端绝不替用户删）。

    静默淘汰最坏的地方不是丢数据，是**用户不知道丢了**：他以为每月一份都留着，
    直到要恢复的那天。所以这里只报错，删永远是用户的一次显式动作。
    """
    monkeypatch.setenv("KYLAB_BACKUP_KEEP", "1")
    get_settings.cache_clear()
    _upload_point(env)

    response = _put_blob(env, b"another", snapshot=OTHER_SNAPSHOT)

    assert response.status_code == 409, response.text
    assert "保留份数" in response.json()["message"]
    assert env.store.blob_key(DEVICE, OTHER_SNAPSHOT) not in env.store.objects
    # 旧那一份**一个字节都没被碰**
    assert env.store.blob_key(DEVICE, SNAPSHOT) in env.store.objects


def test_the_quota_refuses_instead_of_evicting(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """配额超限 → 409，同样不静默淘汰旧的。"""
    monkeypatch.setenv("KYLAB_BACKUP_QUOTA_BYTES", "8")
    get_settings.cache_clear()

    response = _put_blob(env, b"0123456789")

    assert response.status_code == 409, response.text
    assert "配额" in response.json()["message"]
    assert env.store.objects == {}


def test_replaying_an_existing_snapshot_does_not_count_against_the_keep(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已经传过的那一份重放时**不判保留份数**（它没多占一份）——否则 keep=1 时永远补不上清单。"""
    monkeypatch.setenv("KYLAB_BACKUP_KEEP", "1")
    get_settings.cache_clear()
    assert _put_blob(env, b"hello").status_code == 201

    again = _put_blob(env, b"hello")

    assert again.status_code == 200, again.text


# ------------------------------------------------------------------ 列举


def test_the_list_is_ordered_recent_first_and_paginates(env: _Env) -> None:
    """列表：**最近在前**，``limit`` + ``offset`` + ``total``（规范 §1.5）。"""
    for stamp, snapshot in (
        ("2026-10-01T09:00:00Z", "2026-10-01T09-00-00Z-aaaaaaaa"),
        ("2026-10-03T09:00:00Z", "2026-10-03T09-00-00Z-bbbbbbbb"),
        ("2026-10-05T09:00:00Z", "2026-10-05T09-00-00Z-cccccccc"),
    ):
        _upload_point(env, snapshot=snapshot, created_at=stamp)

    first = env.client.get("/api/v1/backup/snapshots", params={"limit": 2}).json()

    assert first["total"] == 3
    assert [item["snapshot_id"] for item in first["items"]] == [
        "2026-10-05T09-00-00Z-cccccccc",
        "2026-10-03T09-00-00Z-bbbbbbbb",
    ]

    second = env.client.get("/api/v1/backup/snapshots", params={"limit": 2, "offset": 2}).json()
    assert [item["snapshot_id"] for item in second["items"]] == ["2026-10-01T09-00-00Z-aaaaaaaa"]


def test_the_list_can_filter_by_device_but_the_quota_does_not(env: _Env) -> None:
    """按设备过滤只改 ``items`` / ``total``；``quota`` 永远是**这把钥匙看得见的全局用量**。"""
    _upload_point(env, device=DEVICE, snapshot=SNAPSHOT)
    _upload_point(env, device="other-device", snapshot=SNAPSHOT)

    filtered = env.client.get("/api/v1/backup/snapshots", params={"device_id": DEVICE}).json()

    assert filtered["total"] == 1
    assert [item["device_id"] for item in filtered["items"]] == [DEVICE]
    assert filtered["quota"]["snapshots"] == 2, "额度与设备过滤无关"


def test_a_page_beyond_the_server_cap_is_a_422(env: _Env) -> None:
    """``limit`` 超服务端上限 → 422（规范 §1.5：**不静默截断**）。"""
    response = env.client.get(
        "/api/v1/backup/snapshots", params={"limit": backup.MAX_LIST_LIMIT + 1}
    )

    assert response.status_code == 422, response.text


def test_the_list_reports_the_configured_quota(env: _Env) -> None:
    """额度那一段报的是**配置里配了多少**（不是这套假桶自己编的）。"""
    settings = get_settings()
    _upload_point(env, body=b"0123456789")

    quota = env.client.get("/api/v1/backup/snapshots").json()["quota"]

    assert quota == {
        "policy": "keep_n",
        "keep": settings.backup_keep,
        "quota_bytes": settings.backup_quota_bytes,
        "used_bytes": 10,
        "snapshots": 1,
    }


def test_a_write_makes_sure_the_bucket_exists(env: _Env) -> None:
    """写之前**懒 ensure 桶**（缺桶时第一次上传顺手建出来，而不是等运维）。

    注意与握手那条的分工：握手只**探测**（``available=false`` + 下一步），
    不该替运维把桶建了——建桶是一次有意识的部署动作；写入路径才顺手补上。
    """
    assert env.store.ensure_calls == 0

    assert _put_blob(env, b"hello").status_code == 201

    assert env.store.ensure_calls == 1
