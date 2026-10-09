"""知识库元数据快照的 SWR 用例（M4 阶段 2）。

镜像同构：``app/services/kb_cache.py`` → 本文件。

**假 NAS 用 ``httpx.MockTransport``**（手法照 ``test_knowledge_provider.py``）：reader 面的
``inner`` 就是**真的** ``KnowledgeProviderClient.knowledge_meta()``，所以「命中零网络」
断言的是**真的没发 HTTP**（请求计数就是这个假 NAS 的 ``requests``），而不是「没调某个对象」。

覆盖（方案 §7 阶段 2 的完成判据 + §8 B 的前四行）：

① 命中零网络 / 过期后台恰好一次 / 15s 内再读不再排 / 失败后 60s 不再排（§4.4）；
② 版本戳：同 payload → 只推 ``checked_at``；改一栏 → version、payload、fetched_at 都换（§4.1）；
③ 失败降级：``stale=1`` + ``last_error`` 含原因 + 快照照旧可读 + 日志 D-C（§4.5）；
④ 404 删行回 ``None``、两个地址不串（R2）、超龄 / 坏 payload 当没有并删行；
⑤ 分类守卫：``KnowledgeProviderClient`` 的每个公开方法都在 ``PROVIDER_SURFACE`` 里分过类（§1.3）；
⑥ 行为用例：``retrieve_sources`` / ``submit`` 前后 ``kb_meta_cache`` **整表一字不变**（§1.3）；
⑦ ``_for_cache()``：权限位 / 进度 / 凭据类键一个都不落（R5）；
⑧ ``kb_list`` 顺带派生 ``kb_detail``（同一份内容拆开写，§3.1）。

时间不用真等：注入一对假钟（单调 + 墙上），「15 秒」「60 秒」都是一次 ``advance()``。
标 ``local``：只用本机 SQLite 与假传输，一个远端都不连。
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services.kb_cache import (
    CACHEABLE_RESOURCES,
    DOC_LIST,
    DOCUMENT,
    FOLDERS,
    KB_DETAIL,
    KB_LIST,
    NEVER_CACHED,
    NO_SNAPSHOT_REASON,
    PROVIDER_SURFACE,
    RETRY_BACKOFF_SECONDS,
    REVALIDATE_AFTER_SECONDS,
    SOURCE_READER,
    SOURCE_REVALIDATE,
    STRIPPED_FIELDS,
    CachedKnowledgeMetaReader,
    KbMetaCacheService,
    _for_cache,
    cache_version,
    canonical_json,
    doc_list_scope_key,
)
from app.services.knowledge_provider import KnowledgeProviderClient
from app.storage.base import (
    MAX_PAYLOAD_BYTES,
    SNAPSHOT_MAX_AGE_SECONDS,
    KbMetaCacheRecord,
)
from app.storage.sqlite_impl.connection import Database
from app.storage.sqlite_impl.meta_store import SqliteMetaStore
from app.storage.sqlite_impl.schema import prepare

pytestmark = pytest.mark.local

#: 一台「家里的 NAS」与一台「单位的 NAS」：**同一个库 id**，名字不同（R2 的判据）。
HOME = "http://nas-home.test/api/v1"
OFFICE = "http://nas-office.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_must_not_leak"


# ------------------------------------------------------------------ 夹具：家里那份数据


def _kb(kb_id: str = "kb_a", *, name: str = "论文", **overrides: Any) -> dict[str, Any]:
    """一份库元数据（形状照 ``KnowledgeBaseOut``）：**带**两个权限位——进快照前必须被剥掉。"""
    payload: dict[str, Any] = {
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
        "last_activity": "2026-10-03T09:00:00Z",
    }
    payload.update(overrides)
    return payload


def _document(document_id: str = "doc_1", **overrides: Any) -> dict[str, Any]:
    """一份文档行（``DocumentOut``）：**带** ``progress``——冻结的进度条是最糟的假象（§1.1）。"""
    payload: dict[str, Any] = {
        "id": document_id,
        "knowledge_base_id": "kb_a",
        "name": "指南.pdf",
        "source_kind": "local",
        "stage": "ready",
        "size_bytes": 448444,
        "progress": {"status": "running", "step_index": 3, "elapsed_ms": 214_000},
    }
    payload.update(overrides)
    return payload


class _Nas:
    """假 NAS：路由 + **请求计数**（「零网络」那条判据的全部）。

    ``fail`` 让每一次请求都抛（连不上那种）；``missing`` 里的库 id 回 404（「没有这个库」
    与「取不到」是两件事）。两个开关都由用例在跑的中间改——SWR 的用例本来就长这样。
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.libraries: dict[str, list[dict[str, Any]]] = {
            HOME: [_kb()],
            OFFICE: [_kb(name="单位的库")],
        }
        self.missing: set[str] = set()
        self.fail: Exception | None = None

    # ---- 路由（假的 NAS 那一侧）
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self.fail is not None:
            raise self.fail
        base = OFFICE if url.startswith(OFFICE) else HOME
        path = request.url.path
        if request.method == "POST" and path.endswith("/search"):
            return httpx.Response(200, json={"hits": []})
        if request.method == "POST" and path.endswith("/documents"):
            return httpx.Response(202, json={"document": {"id": "doc_9", "name": "笔记.md"}})
        if path.endswith("/knowledge-bases"):
            return httpx.Response(200, json={"items": self.libraries[base]})
        if "/knowledge-bases/" in path:
            kb_id = path.rsplit("/", 1)[-1]
            for item in self.libraries[base]:
                if item["id"] == kb_id and kb_id not in self.missing:
                    return httpx.Response(200, json=item)
            return httpx.Response(404, text="没有这个库")
        return httpx.Response(404, text="没有这个端点")

    # ---- 页面面用的取数闭包（**走同一个假传输**，所以请求计数照样算得上）
    def fetch_json(self, path: str, base: str = HOME) -> Callable[[], Any]:
        """等价于阶段 4 端点里的那一次 HTTP 读：``None`` = 远端说没有这个东西。"""

        def fetch() -> Any:
            with httpx.Client(
                base_url=base,
                transport=httpx.MockTransport(self.handler),
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client:
                response = client.get(path)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()

        return fetch


@dataclass
class _Time:
    """一对假钟：单调（排程间隔与退避）+ 墙上（快照时间戳）。

    两把钟一起推——SWR 的判据跨越两者：「15 秒内确认过」问的是墙上时间，
    「15 秒内不再排」问的是「离上次发起多久」。
    """

    monotonic: float = 1000.0
    wall: datetime = field(default_factory=lambda: datetime(2026, 10, 4, 9, 0, 0, tzinfo=UTC))

    def clock(self) -> float:
        return self.monotonic

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def _settings(base: str) -> Settings:
    """一份**与当前机器无关**的引导级配置（``_env_file=None`` 挡掉开发机的 ``.env``）。"""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        deployment="local",
        database_url="",
        server_url=base,
        token=TOKEN,
        kb_url="",
        kb_token="",
    )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "kylab.db")
    prepare(db)
    yield db
    db.close()


@pytest.fixture
def store(database: Database) -> SqliteMetaStore:
    return SqliteMetaStore(database)


@pytest.fixture
def nas() -> _Nas:
    return _Nas()


@pytest.fixture
def clock() -> _Time:
    return _Time()


@pytest.fixture
def provider(nas: _Nas, clock: _Time) -> KnowledgeProviderClient:
    """真的客户端 + 假的传输：`retrieve_sources` / `submit` 那两条路要它（行为用例）。"""
    return KnowledgeProviderClient(
        settings=_settings(HOME),
        transport=httpx.MockTransport(nas.handler),
        clock=clock.clock,
        now=clock.now,
    )


@pytest.fixture
def service(store: SqliteMetaStore, clock: _Time) -> KbMetaCacheService:
    return KbMetaCacheService(store, provider_key=lambda: HOME, clock=clock.clock, now=clock.now)


def _reader_for(
    store: SqliteMetaStore, nas: _Nas, clock: _Time, base: str
) -> CachedKnowledgeMetaReader:
    """一台地址 = ``base`` 的 reader 面（真的客户端 + 真的假传输 + 真的快照服务）。"""
    client = KnowledgeProviderClient(
        settings=_settings(base),
        transport=httpx.MockTransport(nas.handler),
        clock=clock.clock,
        now=clock.now,
    )
    service = KbMetaCacheService(
        store, provider_key=lambda base=base: base, clock=clock.clock, now=clock.now
    )
    return CachedKnowledgeMetaReader(inner=client.knowledge_meta(), cache=service)


@pytest.fixture
def reader(store: SqliteMetaStore, nas: _Nas, clock: _Time) -> CachedKnowledgeMetaReader:
    return _reader_for(store, nas, clock, HOME)


def _row(
    store: SqliteMetaStore, resource: str, scope_key: str = "", provider: str = HOME
) -> KbMetaCacheRecord | None:
    return store.get_kb_meta_cache(provider, resource, scope_key)


def _rows(store: SqliteMetaStore) -> int:
    return store.kb_meta_cache_stats().rows


def _stale(store: SqliteMetaStore, resource: str, scope_key: str = "") -> bool:
    """这一行现在标着「上次没确认成」吗（行不在就是 False）。"""
    row = _row(store, resource, scope_key)
    return row is not None and row.stale


def _fresh(store: SqliteMetaStore, resource: str, scope_key: str = "") -> bool:
    """这一行现在**在、且没被标成没确认**（等后台那次跑完时用的判据）。"""
    row = _row(store, resource, scope_key)
    return row is not None and not row.stale


def _raw_rows(database: Database) -> list[tuple[Any, ...]]:
    """直接读表（绕开仓储）：验「整表一字不变」这类判据时只能看库。"""
    with database.read() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM kb_meta_cache ORDER BY provider, resource, scope_key"
            )
        ]


def _put_row(
    store: SqliteMetaStore,
    resource: str,
    scope_key: str,
    *,
    payload: str,
    version: str = "sha256:手工放进去的",
    checked_at: datetime | None = None,
    fetched_at: datetime | None = None,
    provider: str = HOME,
) -> None:
    """直接放一行（超龄 / 坏 payload 那两条用例要的就是「库里本来有这么一行」）。"""
    moment = checked_at or datetime.now(UTC).replace(microsecond=0)
    store.put_kb_meta_cache(
        KbMetaCacheRecord(
            provider=provider,
            resource=resource,
            scope_key=scope_key,
            payload=payload,
            version=version,
            source=SOURCE_READER,
            fetched_at=fetched_at or moment,
            checked_at=moment,
        )
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    """等后台那一件事真的做完（真线程，不能靠 sleep 猜）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ------------------------------------------------------------------ ① 命中零网络


def test_a_hit_serves_the_snapshot_with_zero_http(
    reader, nas: _Nas, store: SqliteMetaStore
) -> None:
    """② 命中零网络：第二次读**一个请求都不发**（第一次是未命中，必须现在给答案）。"""
    first = reader.get_knowledge_base("kb_a")

    assert first is not None
    assert first["name"] == "论文"
    # 权限位**不进快照**，所以回给 stores.meta 的那份里也没有（D-B / R5）
    assert "can_write" not in first
    assert "can_manage" not in first
    assert len(nas.requests) == 1
    assert _row(store, KB_DETAIL, "kb_a").source == SOURCE_READER  # type: ignore[union-attr]

    assert reader.get_knowledge_base("kb_a") == first
    assert len(nas.requests) == 1


def test_listing_the_libraries_reads_once_and_then_never(
    reader, nas: _Nas, store: SqliteMetaStore
) -> None:
    """库清单同款：未命中取一次，之后命中零网络；而且那一次顺带派生了 ``kb_detail``。"""
    assert [item["name"] for item in reader.list_knowledge_bases()] == ["论文"]
    assert len(nas.requests) == 1
    assert _rows(store) == 2  # kb_list 一行 + 派生的 kb_detail 一行

    assert [item["name"] for item in reader.list_knowledge_bases()] == ["论文"]
    assert len(nas.requests) == 1
    assert "can_write" not in reader.list_knowledge_bases()[0]
    assert _row(store, KB_LIST).source == SOURCE_READER  # type: ignore[union-attr]


# ------------------------------------------------- ① 过期 → 立即回 + 后台恰好一次


def test_an_expired_snapshot_comes_back_at_once_and_revalidates_exactly_once(
    service: KbMetaCacheService, nas: _Nas, clock: _Time, store: SqliteMetaStore
) -> None:
    """§4.2：命中但过了 15 秒 → **立即回旧的那份** + 后台恰好一次再验证（不再多排）。"""
    detail = nas.fetch_json("/knowledge-bases/kb_a")
    first = service.refresh(KB_DETAIL, "kb_a", fetch=detail)
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)

    hit = service.snapshot(KB_DETAIL, "kb_a", fetch=detail)

    assert hit.available is True
    assert hit.revalidating is True  # 这一读触发了刷新
    assert hit.fetched_at == first.fetched_at  # 回的是**旧的那份**，不是等一次网络
    # 后台那一次：等它真的发出去（不能断言"此刻还没发"——那是后台线程的自由）
    assert _wait_until(lambda: len(nas.requests) == 2)

    # 15 秒内再读几次：一次都不再排（R3 的判据）
    for _ in range(3):
        assert service.snapshot(KB_DETAIL, "kb_a", fetch=detail).revalidating is False
    assert len(nas.requests) == 2
    # 后台那次跑完：内容没变 ⇒ version 与 fetched_at 都不动，只推 checked_at
    assert _wait_until(
        lambda: (_row(store, KB_DETAIL, "kb_a") or first).checked_at > first.checked_at
    )
    row = _row(store, KB_DETAIL, "kb_a")
    assert row is not None
    assert row.version == first.version
    assert row.payload == canonical_json(hit.payload)
    assert row.fetched_at == first.fetched_at
    assert row.source == SOURCE_REVALIDATE


def test_the_reader_revalidates_in_the_background_when_the_snapshot_gets_old(
    reader, nas: _Nas, clock: _Time
) -> None:
    """reader 面同款（§4.2 右边那一列）：过期 → 立即回旧快照，后台**用 ``inner``** 再取一次。

    "后台"是这一层的全部价值：读的人（``ChatService._kb_prompt`` 每轮每库）从不等待网络。
    """
    assert reader.get_knowledge_base("kb_a")["name"] == "论文"
    assert len(nas.requests) == 1
    nas.libraries[HOME][0]["name"] = "论文（改过名）"
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)

    assert reader.get_knowledge_base("kb_a")["name"] == "论文", "这一读回的是旧的那份"
    assert _wait_until(lambda: len(nas.requests) == 2), "后台应当自己取了一次"
    assert _wait_until(lambda: reader.get_knowledge_base("kb_a")["name"] == "论文（改过名）"), (
        "再验证落地之后，下一次读如实更新（列表那句「后台再验证后如实更新」）"
    )


def test_a_read_within_fifteen_seconds_does_not_schedule_again(
    service: KbMetaCacheService, nas: _Nas, clock: _Time
) -> None:
    """15 秒内命中 → 连排都不排（读触发再验证的最短间隔，§4.4）。"""
    detail = nas.fetch_json("/knowledge-bases/kb_a")
    service.refresh(KB_DETAIL, "kb_a", fetch=detail)
    clock.advance(REVALIDATE_AFTER_SECONDS - 1.0)

    hit = service.snapshot(KB_DETAIL, "kb_a", fetch=detail)

    assert hit.available is True
    assert hit.revalidating is False
    assert len(nas.requests) == 1


def test_one_daemon_thread_serves_every_key(
    service: KbMetaCacheService, nas: _Nas, clock: _Time
) -> None:
    """后台**一个**守护线程（§4.4）：排了两个键也只多它一个，而且它不许吊住进程退出。"""
    before = set(threading.enumerate())
    nas.libraries[HOME] = [_kb("kb_a"), _kb("kb_b")]
    detail_a = nas.fetch_json("/knowledge-bases/kb_a")
    detail_b = nas.fetch_json("/knowledge-bases/kb_b")
    service.refresh(KB_DETAIL, "kb_a", fetch=detail_a)
    service.refresh(KB_DETAIL, "kb_b", fetch=detail_b)
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)

    assert service.snapshot(KB_DETAIL, "kb_a", fetch=detail_a).revalidating is True
    assert service.snapshot(KB_DETAIL, "kb_b", fetch=detail_b).revalidating is True

    started = [thread for thread in threading.enumerate() if thread not in before]
    assert len(started) == 1, "两个键也只有一个后台线程"
    assert started[0].daemon is True, "它得是守护线程（边车退出不被一次再验证吊住）"
    assert _wait_until(lambda: len(nas.requests) == 4), "两件活都由那一个线程做完了"


def test_a_failed_revalidation_backs_off_for_sixty_seconds(
    service: KbMetaCacheService, nas: _Nas, clock: _Time, store: SqliteMetaStore
) -> None:
    """失败 → 60 秒退避（§4.4）：断着的时候不许每读一次就打一次。"""
    detail = nas.fetch_json("/knowledge-bases/kb_a")
    service.refresh(KB_DETAIL, "kb_a", fetch=detail)
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)
    nas.fail = httpx.ConnectError("用例造的断连")

    assert service.snapshot(KB_DETAIL, "kb_a", fetch=detail).revalidating is True
    assert _wait_until(lambda: _stale(store, KB_DETAIL, "kb_a"))
    assert len(nas.requests) == 2  # 只有那一次失败的尝试

    # 过了 15 秒、但还在 60 秒退避里：不再排
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)
    assert service.snapshot(KB_DETAIL, "kb_a", fetch=detail).revalidating is False
    assert len(nas.requests) == 2

    # 退避过了：再排（放通假 NAS，顺带验「成功一次就把 stale 清掉」）
    clock.advance(RETRY_BACKOFF_SECONDS)
    assert service.snapshot(KB_DETAIL, "kb_a", fetch=detail).revalidating is True
    nas.fail = None
    assert _wait_until(lambda: _fresh(store, KB_DETAIL, "kb_a"))
    assert len(nas.requests) == 3


# ------------------------------------------------------------------ ② 版本戳（§4.1）


def test_the_same_payload_only_advances_checked_at(
    service: KbMetaCacheService, nas: _Nas, clock: _Time, store: SqliteMetaStore
) -> None:
    """哈希相同 ⇒ **只推 checked_at**：payload / version / fetched_at 都不许动。"""
    fetch = nas.fetch_json("/knowledge-bases/kb_a")
    first = service.refresh(KB_DETAIL, "kb_a", fetch=fetch)
    before = _row(store, KB_DETAIL, "kb_a")
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)

    second = service.refresh(KB_DETAIL, "kb_a", fetch=fetch)
    after = _row(store, KB_DETAIL, "kb_a")

    assert before is not None and after is not None
    assert second.version == first.version == before.version
    assert after.payload == before.payload
    assert after.fetched_at == before.fetched_at, "「上次更新于 X」不许被一次确认推着走"
    assert after.checked_at > before.checked_at, "确认过就要如实推 checked_at"
    assert after.version == cache_version(json.loads(after.payload))


def test_a_changed_field_swaps_the_payload_and_the_version(
    service: KbMetaCacheService, nas: _Nas, clock: _Time, store: SqliteMetaStore
) -> None:
    """改一栏 ⇒ version 变、payload 与 fetched_at 都换（两句时间戳各说各的）。"""
    fetch = nas.fetch_json("/knowledge-bases/kb_a")
    service.refresh(KB_DETAIL, "kb_a", fetch=fetch)
    before = _row(store, KB_DETAIL, "kb_a")
    nas.libraries[HOME][0]["name"] = "论文（改过名）"
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)

    second = service.refresh(KB_DETAIL, "kb_a", fetch=fetch)
    after = _row(store, KB_DETAIL, "kb_a")

    assert before is not None and after is not None
    assert second.payload["name"] == "论文（改过名）"
    assert after.version != before.version
    assert after.payload != before.payload
    assert after.fetched_at == clock.now() > before.fetched_at
    assert after.checked_at == clock.now()


# ------------------------------------------------------------------ ③ 失败降级（§4.5）


def test_a_failed_revalidation_keeps_the_snapshot_readable(
    reader, service: KbMetaCacheService, nas: _Nas, store: SqliteMetaStore, caplog
) -> None:
    """远端失败 ⇒ ``stale=1`` + ``last_error`` 含原因，**快照照旧可读**，而且不再打一次。

    读取这一下没有等网络、也没有抛：它拿的就是那份旧快照——这正是 D-C 的语义
    （「恰好没有这个库」仍是另一件事，见 404 那条）。
    """
    assert reader.get_knowledge_base("kb_a") is not None
    nas.fail = httpx.ConnectError("连不上（用例造的断连）")
    before = len(nas.requests)

    with caplog.at_level(logging.WARNING):
        failed = service.refresh(KB_DETAIL, "kb_a", fetch=nas.fetch_json("/knowledge-bases/kb_a"))
        assert failed.available is True  # 快照照旧可读
        assert failed.stale is True
        assert "连不上" in failed.last_error
        row = _row(store, KB_DETAIL, "kb_a")
        assert row is not None
        assert row.stale is True
        assert "连不上" in row.last_error
        assert row.checked_at == failed.checked_at, "一次失败不是一次确认"
        # 这一次读**零网络**：答案来自快照，而 D-C 的那句日志也在
        assert reader.get_knowledge_base("kb_a")["name"] == "论文"
        assert len(nas.requests) == before + 1  # 只多了那次失败的再验证本身
    assert "读知识库元数据失败，用的是" in caplog.text
    assert "秒前" in caplog.text
    assert caplog.text.count("知识库元数据再验证失败") == 1


def test_a_remote_404_drops_the_row_and_answers_none(
    reader, service: KbMetaCacheService, nas: _Nas, clock: _Time, store: SqliteMetaStore
) -> None:
    """远端 404 = **没有这个库**（答案，不是错误）：删掉那一行，``None`` 照原样传出去。"""
    assert reader.get_knowledge_base("kb_a") is not None
    assert _rows(store) == 1
    nas.missing.add("kb_a")
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)

    gone = service.refresh(KB_DETAIL, "kb_a", fetch=nas.fetch_json("/knowledge-bases/kb_a"))

    assert gone.available is False
    assert gone.reason == "远端说没有这个东西"
    assert _rows(store) == 0, "远端说没有这一行就不作数了"
    assert reader.get_knowledge_base("kb_a") is None


# ------------------------------------------------------------------ ④ 边界（R2 / 超龄 / 坏行）


def test_two_addresses_never_share_a_row(store: SqliteMetaStore, nas: _Nas, clock: _Time) -> None:
    """R2：**同一个库 id** 在两台 NAS 上互不覆盖（键空间第一列是地址）。"""
    home = _reader_for(store, nas, clock, HOME)
    office = _reader_for(store, nas, clock, OFFICE)

    assert home.get_knowledge_base("kb_a")["name"] == "论文"
    assert office.get_knowledge_base("kb_a")["name"] == "单位的库"

    assert _rows(store) == 2
    assert store.kb_meta_cache_stats(provider=HOME).rows == 1
    assert store.kb_meta_cache_stats(provider=OFFICE).rows == 1
    # 再各读一次：各自命中，谁也没被谁覆盖
    assert home.get_knowledge_base("kb_a")["name"] == "论文"
    assert office.get_knowledge_base("kb_a")["name"] == "单位的库"


def test_switching_the_address_gives_a_new_keyspace_without_touching_the_old(
    store: SqliteMetaStore, nas: _Nas, clock: _Time
) -> None:
    """§5：改了地址之后**第一次进页面 = 没有快照**（``available:false``，照旧骨架屏），

    而**旧地址那一行不清**——切回去还能秒开。清理由"上限 + 手动"决定，
    不该由"改了一个地址"隐式决定。
    """
    home = _reader_for(store, nas, clock, HOME)
    assert home.get_knowledge_base("kb_a")["name"] == "论文"
    office = KbMetaCacheService(
        store, provider_key=lambda: OFFICE, clock=clock.clock, now=clock.now
    )

    snapshot = office.snapshot(KB_DETAIL, "kb_a")

    assert snapshot.available is False
    assert snapshot.reason == NO_SNAPSHOT_REASON
    assert _rows(store) == 1, "旧地址那一行还在（不清旧行）"
    assert home.get_knowledge_base("kb_a")["name"] == "论文"


def test_an_aged_out_snapshot_is_treated_as_missing(
    service: KbMetaCacheService, clock: _Time, store: SqliteMetaStore
) -> None:
    """超龄（30 天没被确认）⇒ 当没有，并**顺手删行**（§3.4-3，判据在存储层）。

    超龄这台钟是**真的那一台**（存储层的判据对着 ``datetime.now``）：所以这个时间戳按
    真实现在往前推，而不是假钟——假钟只用来推「15 秒 / 60 秒」那两条排程规则。
    """
    _put_row(
        store,
        KB_DETAIL,
        "kb_a",
        payload=canonical_json(_kb()),
        checked_at=datetime.now(UTC).replace(microsecond=0)
        - timedelta(seconds=SNAPSHOT_MAX_AGE_SECONDS + 60),
    )

    snapshot = service.snapshot(KB_DETAIL, "kb_a")

    assert snapshot.available is False
    assert snapshot.reason == NO_SNAPSHOT_REASON
    assert _rows(store) == 0


def test_a_broken_snapshot_is_treated_as_missing_and_the_row_is_dropped(
    reader, service: KbMetaCacheService, nas: _Nas, store: SqliteMetaStore
) -> None:
    """坏 payload（形状不对）⇒ 当没有并删行；reader 面照旧同步取一次把答案给出来。"""
    _put_row(store, KB_DETAIL, "kb_a", payload='["不是这个资源要的形状"]')
    before = len(nas.requests)

    assert reader.get_knowledge_base("kb_a")["name"] == "论文"
    assert len(nas.requests) == before + 1
    assert json.loads(_row(store, KB_DETAIL, "kb_a").payload)["name"] == "论文"  # type: ignore[union-attr]

    # 列表那一档同款：items 不是数组 ⇒ 形状不对
    _put_row(store, KB_LIST, "", payload='{"items": "不是数组"}')
    broken = service.snapshot(KB_LIST, fetch=nas.fetch_json("/knowledge-bases"))
    assert broken.available is True  # 当没有 ⇒ 同步取一次，答案照旧给得出来
    assert json.loads(_row(store, KB_LIST).payload)["items"][0]["id"] == "kb_a"  # type: ignore[union-attr]


# ------------------------------------------------------------------ ⑤ 分类守卫（§1.3）


def test_every_public_client_method_is_classified() -> None:
    """枚举 ``KnowledgeProviderClient`` 的公开方法，**每个都必须在表里表过态**。

    这是 §1.3 的第二道防线：出现一个新方法而未分类（CACHE / NOT_CACHE）即红——
    「顺手缓存一下」这条路必须先过这一关。
    """
    public = {name for name in dir(KnowledgeProviderClient) if not name.startswith("_")}

    assert public == set(PROVIDER_SURFACE), "新公开方法必须先在 PROVIDER_SURFACE 里分类"
    for method, resources in PROVIDER_SURFACE.items():
        assert resources <= set(CACHEABLE_RESOURCES), f"{method} 指向了不在正向清单里的资源"
    # 三件「绝不进」的事各自表态（检索 / 写 / 进度）
    for method in ("retrieve_sources", "submit", "document_status", "status"):
        assert PROVIDER_SURFACE[method] == frozenset(), method
    assert PROVIDER_SURFACE["knowledge_meta"] == frozenset({KB_LIST, KB_DETAIL})


def test_the_negative_list_names_the_forbidden_faces_with_reasons() -> None:
    """负向清单逐条带理由（§1.2 那六行）：翻新它的时候不许把理由丢了。"""
    assert len(NEVER_CACHED) == 6
    assert all(reason for reason in NEVER_CACHED.values())
    joined = " ".join(NEVER_CACHED)
    for face in ("/search", "写类动作", "timeline", "/chunks", "/api-keys", "handshake"):
        assert face in joined, face
    # 剥字段那张表与正向清单**对齐**（漏一个资源就会静默地什么都不剥）
    assert set(STRIPPED_FIELDS) == set(CACHEABLE_RESOURCES)


def test_an_unknown_resource_is_rejected_everywhere(service: KbMetaCacheService) -> None:
    """非法资源名当场 ``ValueError``（写入口只有正向清单一条路）。"""
    for call in (
        lambda: service.snapshot("search"),
        lambda: service.refresh("search", fetch=lambda: {"hits": []}),
        lambda: service.invalidate("search"),
        lambda: service.purge(resource="search"),
    ):
        with pytest.raises(ValueError, match="search"):
            call()
    # ``views`` 只对文档列表有意义（别的资源一个键就是它自己）
    with pytest.raises(ValueError):
        service.invalidate(KB_DETAIL, "kb_a", views=True)


# ------------------------------------------------- ⑥ 行为用例：检索与写不碰这张表


def test_search_and_ingest_leave_the_snapshot_table_untouched(
    provider: KnowledgeProviderClient,
    reader,
    nas: _Nas,
    store: SqliteMetaStore,
    database: Database,
) -> None:
    """§1.3 的第三道防线：``retrieve_sources`` / ``submit`` 前后**整表一字不变**。

    先让表里有东西（不然"没变"这句话没有分量），再走那两条路，最后逐行比对。
    """
    assert reader.get_knowledge_base("kb_a") is not None
    before = _raw_rows(database)
    assert before

    provider.retrieve_sources(query="储能", kb_ids=["kb_a"], top_k=3)
    provider.submit(knowledge_base_id="kb_a", filename="笔记.md", content=b"x")

    assert [url for url in nas.requests if url.endswith("/search")], "检索真的发出去了"
    assert _raw_rows(database) == before
    assert _rows(store) == len(before)


# --------------------------------------------------------- ⑦ 进快照前的收口（R5）


def test_the_cached_form_never_carries_permission_bits_progress_or_credentials() -> None:
    """``_for_cache()`` 的机械断言：权限位、进度、凭据类键一个都不落（R5）。"""
    kb = _kb() | {"api_key": "sk-live", "nested": {"refresh_token": "t", "keep": 1}}

    item = _for_cache(KB_LIST, {"items": [kb]})["items"][0]
    assert "can_write" not in item and "can_manage" not in item
    assert "api_key" not in item
    assert item["nested"] == {"keep": 1}, "兜底剥键要钻到底，但只剥凭据类的那几个"

    detail = _for_cache(KB_DETAIL, kb)
    assert "can_write" not in detail and "can_manage" not in detail
    assert "refresh_token" not in json.dumps(detail)

    # 活数据一律不落：文档列表与文档条目都不留 progress
    for resource in (DOC_LIST, DOCUMENT):
        payload = (
            _for_cache(resource, {"items": [_document()]})
            if resource == DOC_LIST
            else (_for_cache(resource, _document()))
        )
        row = payload["items"][0] if resource == DOC_LIST else payload
        assert "progress" not in row

    # 目录树没有要剥的（但它在表里表过态：空元组，不是"漏了"）
    assert _for_cache(FOLDERS, {"items": [{"id": "f1", "name": "2026"}]}) == {
        "items": [{"id": "f1", "name": "2026"}]
    }


def test_what_lands_in_the_table_is_already_stripped(
    service: KbMetaCacheService, store: SqliteMetaStore, nas: _Nas
) -> None:
    """再把上面那条钉在**库里那一份文本**上：表里的 payload 里没有权限位与凭据。"""
    service.refresh(KB_LIST, fetch=lambda: {"items": [_kb() | {"api_key": "sk-live"}]})

    text = _row(store, KB_LIST).payload  # type: ignore[union-attr]
    assert "can_write" not in text and "can_manage" not in text
    assert "api_key" not in text and "sk-live" not in text


def test_a_payload_over_the_single_row_limit_is_not_kept(
    service: KbMetaCacheService, store: SqliteMetaStore
) -> None:
    """单条过大 ⇒ **不缓存**（判据在存储层，这里钉住「调用方那条链照样拿到答案」）。"""
    huge = _kb(description="x" * (MAX_PAYLOAD_BYTES + 1))

    written = service.refresh(KB_DETAIL, "kb_a", fetch=lambda: huge)

    assert written.available is True  # 答案照旧给得出来
    assert written.payload["id"] == "kb_a"
    assert _rows(store) == 0, "过大的那一份不留副本"


# ------------------------------------------------- ⑧ kb_list 顺带派生 kb_detail（§3.1）


def test_a_kb_list_revalidation_derives_one_detail_row_per_library(
    service: KbMetaCacheService, store: SqliteMetaStore, nas: _Nas, clock: _Time
) -> None:
    """一次「列库」的确认同时确认每个库：**同一份 payload 拆开写**（§3.1）。"""
    nas.libraries[HOME] = [_kb("kb_a", name="论文"), _kb("kb_b", name="年报")]
    fetch = nas.fetch_json("/knowledge-bases")

    listed = service.refresh(KB_LIST, fetch=fetch)

    assert _rows(store) == 3, "kb_list 一行 + 每个库一行 kb_detail"
    for item in listed.payload["items"]:
        row = _row(store, KB_DETAIL, item["id"])
        assert row is not None, item["id"]
        # 同一份内容拆开写：剥完权限位之后逐字相同
        assert json.loads(row.payload) == _for_cache(KB_DETAIL, item)
        assert row.version == cache_version(_for_cache(KB_DETAIL, item))
        assert row.fetched_at == listed.checked_at
        assert "can_write" not in row.payload

    # 内容没变的第二次：派生那几行也只推 checked_at（fetched_at 不动）
    before = _row(store, KB_DETAIL, "kb_a")
    clock.advance(REVALIDATE_AFTER_SECONDS + 1)
    service.refresh(KB_LIST, fetch=fetch)
    after = _row(store, KB_DETAIL, "kb_a")
    assert before is not None and after is not None
    assert after.fetched_at == before.fetched_at
    assert after.checked_at > before.checked_at
    assert _rows(store) == 3


# ------------------------------------------------------------- 清理三档与视图指纹


def test_invalidate_and_purge_cover_the_three_granularities(
    service: KbMetaCacheService, store: SqliteMetaStore, nas: _Nas
) -> None:
    """§3.4：就地失效（一个键 / 这一库的全部视图）与按档清空（全清）。"""
    nas.libraries[HOME] = [_kb("kb_a"), _kb("kb_b")]
    service.refresh(KB_LIST, fetch=nas.fetch_json("/knowledge-bases"))
    for page in (1, 2):
        service.refresh(
            DOC_LIST,
            doc_list_scope_key("kb_a", page=page),
            fetch=lambda: {"items": [], "total": 0},
        )
    assert _rows(store) == 5  # kb_list 一行 + 两库各一行 kb_detail + 两个视图
    assert service.stats().rows == 5

    assert service.invalidate(DOC_LIST, "kb_a", views=True) == 2  # 这一库的全部视图
    assert service.invalidate(KB_DETAIL, "kb_a") == 1
    assert service.invalidate(KB_LIST) == 1
    assert _rows(store) == 1  # 只剩 kb_b 那一行

    assert service.purge() == 1  # 全清
    assert _rows(store) == 0
    assert service.stats().rows == 0


def test_the_doc_list_key_is_canonical_and_has_nowhere_to_put_a_filter() -> None:
    """D-D 的机械形态：视图指纹里**没有筛选项的位置**（签名里就没有那几个参数）。"""
    assert doc_list_scope_key("kb_a") == "kb_a|folder:all|page:1|size:50"
    assert doc_list_scope_key("kb_a", root=True, size=1) == "kb_a|folder:root|page:1|size:1"
    assert (
        doc_list_scope_key("kb_a", folder="f1", page=3, size=20) == "kb_a|folder:f1|page:3|size:20"
    )

    assert set(inspect.signature(doc_list_scope_key).parameters) == {
        "kb_id",
        "folder",
        "root",
        "page",
        "size",
    }
    with pytest.raises(ValueError):
        doc_list_scope_key("kb_a", folder="f1", root=True)
    with pytest.raises(ValueError):
        doc_list_scope_key("kb_a", page=0)


# --------------------------------------------------------------- 页面面与退化档位


def test_the_page_face_never_fetches_on_a_miss(service: KbMetaCacheService, nas: _Nas) -> None:
    """页面面未命中 → ``available=False`` + 原因，**一个请求都不发**（实时那条读自己会赢）。"""
    snapshot = service.snapshot(KB_LIST)

    assert snapshot.available is False
    assert snapshot.reason == NO_SNAPSHOT_REASON
    assert nas.requests == []


@pytest.mark.parametrize(
    ("resource", "scope_key", "payload"),
    [
        (KB_LIST, "", {"items": [_kb()]}),
        (KB_DETAIL, "kb_a", _kb()),
        (DOC_LIST, "kb_a|folder:all|page:1|size:50", {"items": [_document()], "total": 1}),
        (DOCUMENT, "doc_1", _document()),
        (FOLDERS, "kb_a", {"items": [{"id": "f1", "kb_id": "kb_a", "name": "2026"}]}),
    ],
)
def test_every_cacheable_resource_round_trips(
    service: KbMetaCacheService, resource: str, scope_key: str, payload: dict[str, Any]
) -> None:
    """五个资源都能写能读（阶段 4 的端点族各用其中几个；这里先把路走通）。"""
    written = service.refresh(resource, scope_key, fetch=lambda: payload)

    again = service.snapshot(resource, scope_key)

    assert written.available is True
    assert again.available is True
    assert again.version == written.version
    assert again.payload == _for_cache(resource, payload)


def test_without_a_store_every_read_goes_to_the_network(nas: _Nas, clock: _Time) -> None:
    """``cache=None``（服务器档那一支）⇒ 每次都取、什么都不留。如实，不装成有缓存。"""
    client = KnowledgeProviderClient(
        settings=_settings(HOME),
        transport=httpx.MockTransport(nas.handler),
        clock=clock.clock,
        now=clock.now,
    )
    service = KbMetaCacheService(None, provider_key=lambda: HOME, clock=clock.clock, now=clock.now)
    reader = CachedKnowledgeMetaReader(inner=client.knowledge_meta(), cache=service)

    assert reader.get_knowledge_base("kb_a")["name"] == "论文"
    assert reader.get_knowledge_base("kb_a")["name"] == "论文"

    assert len(nas.requests) == 2
    assert service.stats().rows == 0
    assert service.purge() == 0
    assert service.invalidate(KB_DETAIL, "kb_a") == 0
    assert service.refresh(KB_DETAIL, "kb_a", fetch=lambda: _kb()).available is True


def test_the_provider_key_is_normalised_so_one_address_is_one_keyspace(
    store: SqliteMetaStore, clock: _Time
) -> None:
    """地址末尾的斜杠不该造出第二片键空间（归一化只在服务这一处做）。"""
    trailing = KbMetaCacheService(
        store, provider_key=lambda: f"{HOME}/", clock=clock.clock, now=clock.now
    )
    plain = KbMetaCacheService(store, provider_key=lambda: HOME, clock=clock.clock, now=clock.now)

    trailing.refresh(KB_DETAIL, "kb_a", fetch=lambda: _kb())

    assert plain.snapshot(KB_DETAIL, "kb_a").available is True
    assert _rows(store) == 1
