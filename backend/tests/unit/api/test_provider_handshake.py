r"""握手端点 ``GET /provider/handshake``（M3 阶段 1）的契约。

镜像同构：``app/api/v1/provider.py`` + ``app/api/v1/schemas.py`` 的 Provider* 那一段 → 本文件。

四件事，一件都不能少：

1. **键集合**（方案 §7 阶段 1 的完成判据）：响应体的形状就是契约，多一个字段少一个字段
   客户端都得跟着改——而"多一个"在 JSON 里没人会报错 ✗；
2. **能力集字段齐**，且取值来自**真实契约本身**（``max_bytes`` 就是上传端点那个常量、
   ``top_k_max`` 就是 ``SearchRequest`` 的 ``le=``）——这两条各写一份数字必然漂 ✗；
3. **归属过滤**：受限 key 只看到范围内的库，且 ``can_write=false``（方案 R9：
   握手上泄露库元信息）；
4. **401/403 与"连不上"分成两档**：这条在**响应形状**上落实——凭据问题走 HTTP 状态码 +
   统一错误信封，**不进握手体**。客户端据此决定"改钥匙"还是"改地址"。

**另加一条挂载面的判据**（文件末那四条）：这两条端点各挂在哪张路由表上，用**建出来的
app 的 OpenAPI** 说话（不是读内存里的 router 对象）——阶段 1 的 `/provider/handshake`
只在服务器档，阶段 5 的 `/local/provider`（判定源 + 改地址）只在本机档。

**本文件整份标 ``local``**：它只拼装响应（假 services）、只读路由表，**一个存储都不连**。
标记在这里的含义就是 `conftest.isolated_data_dir` 的那条判据——"不需要 PostgreSQL"：
没有 PG 的机器上，未标 ``local`` 的用例会被**整批跳过**，而"跳过"对这几条断言等于没测
（本机档那台机器正是最需要它跑绿的地方）。

**真 PG 那半（真钥匙 + 真库 + 打真 HTTP）不在本文件**：方案 §7 阶段 1 把它列在
``pytest -m "not local"`` 那一侧，而本次施工的环境没有 ``KYLAB_TEST_DATABASE_URL``
——写下来也只能是"跳过"，跳过在这里等于没测（没有验证过的用例不进仓库）。
所以归属过滤在本文件验的是**协议层的接线**：我们确实调了 ``visible_kb_ids`` 并把范围外
的剔除、``can_write`` 确实取自 ``api_keys.can_write``；那把钥匙自己的判定
（``check_access`` / ``visible_kb_ids`` 的真身）由它那份用例钉住，不需要在这里重演。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import provider
from app.api.v1.documents import MAX_UPLOAD_BYTES
from app.api.v1.knowledge_bases import _out
from app.api.v1.router import api_router, local_router
from app.api.v1.schemas import SearchRequest
from app.core.config import API_VERSION, get_settings
from app.core.services import get_kb_services, get_services, reset_services
from app.core.storage import reset_stores
from app.main import create_app
from app.models.enums import ApiKeyPermission, UserRole
from app.services.api_key import Caller
from app.storage.base import ApiKeyRecord, KnowledgeBaseRecord, UserRecord

pytestmark = pytest.mark.local

#: 握手响应**恰好**是这几个键（方案 §1.2 的契约）。
CONTRACT_KEYS = {
    "provider",
    "protocol_version",
    "app_version",
    "api_version",
    "capabilities",
    "caller",
    "knowledge_bases",
    "server_time",
}


# ------------------------------------------------------------------- 假 services


class _FakeKnowledgeBases:
    def __init__(self, records: list[KnowledgeBaseRecord], stats: dict[str, Any]) -> None:
        self._records = records
        self._stats = stats

    def list_all(self) -> list[KnowledgeBaseRecord]:
        return list(self._records)

    def document_stats(self) -> dict[str, Any]:
        return dict(self._stats)


class _FakeApiKeys:
    """只实现协议层用到的那两个判定（真判定在 `services/api_key.py`，另有它的用例）。"""

    def __init__(
        self, *, visible: list[str] | None, writable: frozenset[str] = frozenset()
    ) -> None:
        self._visible = visible
        self._writable = writable

    def visible_kb_ids(self, caller: Caller) -> list[str] | None:
        _ = caller
        return None if self._visible is None else list(self._visible)

    def can_write(self, caller: Caller, kb_id: str) -> bool:
        _ = caller
        return kb_id in self._writable


class _FakeEmbedding:
    def __init__(self, configured: bool) -> None:
        self.is_configured = configured


class _FakeRuntime:
    def __init__(self, configured: bool) -> None:
        self._embedding = _FakeEmbedding(configured)

    def embedding(self) -> _FakeEmbedding:
        return self._embedding


class _FakeEmbedder:
    def __init__(
        self, *, model_id: str = "bge-m3", dim: int = 1024, is_development: bool = False
    ) -> None:
        self.model_id = model_id
        self.dim = dim
        self.is_development = is_development


class _FakeServices:
    """协议层用到的四个协作者（**不是** ``Services`` 的替身，只实现握手摸到的那几个方法）。"""

    def __init__(
        self,
        *,
        records: list[KnowledgeBaseRecord] | None = None,
        stats: dict[str, Any] | None = None,
        visible: list[str] | None = None,
        writable: frozenset[str] = frozenset(),
        embedding_configured: bool = True,
        embedder: _FakeEmbedder | None = None,
    ) -> None:
        self.knowledge_bases = _FakeKnowledgeBases(records or [], stats or {})
        self.api_keys = _FakeApiKeys(visible=visible, writable=writable)
        self.runtime = _FakeRuntime(embedding_configured)
        self.embedder = embedder or _FakeEmbedder()


def _kb(kb_id: str, name: str, *, owner_id: str | None = None) -> KnowledgeBaseRecord:
    return KnowledgeBaseRecord(
        id=kb_id,
        name=name,
        embedding_model_id="bge-m3",
        embedding_dim=1024,
        owner_id=owner_id,
        wiki_enabled=False,
    )


def _api_key_caller(
    permission: ApiKeyPermission = ApiKeyPermission.READWRITE,
    kb_ids: tuple[str, ...] = (),
) -> Caller:
    return Caller(
        api_key=ApiKeyRecord(
            id="key_1",
            name="本机后端",
            key_hash="x",
            permission=permission,
            knowledge_base_ids=kb_ids,
        )
    )


def _session_caller(role: UserRole = UserRole.MEMBER, user_id: str = "user_1") -> Caller:
    return Caller(
        is_admin=role is UserRole.ADMIN,
        user=UserRecord(id=user_id, name="某人", role=role),
        session_id="sess_1",
    )


# --------------------------------------------------------------------- 1. 键集合


def test_the_body_carries_exactly_the_contract_keys() -> None:
    """响应体的键集合 **恰好等于** 契约那八个：多一个少一个都是要改客户端的变更。

    而且**里面没有任何一位是"凭据怎么了"**——401/403 走 HTTP 状态码与统一错误信封
    （`api/v1/provider.py` 的模块头裁量 2）。这是"两档分得开"的**形状**那一半：
    一次握手能回来，就说明连通与凭据**都已经过了**。
    """
    body = provider.build_handshake(_FakeServices(), _api_key_caller()).model_dump(by_alias=True)

    assert set(body) == CONTRACT_KEYS
    assert set(body["capabilities"]) == {
        "retrieval",
        "ingest",
        "tracking",
        "knowledge_bases",
        "embedding",
    }
    assert set(body["caller"]) == {
        "kind",
        "permission",
        "is_admin",
        "can_write",
        "knowledge_base_ids",
    }
    # 入库那一段的对外名字是 `async`（Python 关键字只能当别名，见 models 的说明）
    assert set(body["capabilities"]["ingest"]) == {
        "transport",
        "async",
        "dedup",
        "max_bytes",
        "extensions",
    }


def test_the_protocol_version_is_a_growing_integer() -> None:
    """``protocol_version`` 是整数且当前为 1（客户端**不认识即判不可用**，方案 §1.2 裁量 3）。"""
    body = provider.build_handshake(_FakeServices(), _api_key_caller()).model_dump(by_alias=True)

    assert provider.PROTOCOL_VERSION == 1
    assert body["protocol_version"] == 1
    assert isinstance(body["protocol_version"], int)
    assert body["provider"] == "knowledge"
    assert body["api_version"] == API_VERSION
    assert body["app_version"] == get_settings().app_version


# ------------------------------------------------------------- 2. 能力集的取值


def test_capabilities_take_their_numbers_from_the_real_contract() -> None:
    """能力集里的数字**不是在这里另写一份**：逐个比对它们的来源。

    ``max_bytes`` / ``top_k_max`` / ``candidate_k_max`` 三处各写一份数字必然漂，
    而界面会照着它们做校验——漂了就是"上传前说可以、传完 413"。
    """
    body = provider.build_handshake(_FakeServices(), _api_key_caller()).model_dump(by_alias=True)
    caps = body["capabilities"]

    assert caps["ingest"]["max_bytes"] == MAX_UPLOAD_BYTES
    assert caps["retrieval"]["top_k_max"] == _le(SearchRequest, "top_k")
    assert caps["retrieval"]["candidate_k_max"] == _le(SearchRequest, "candidate_k")
    # 顺带钉住当前值：两个上界换了数字是**契约变更**，不该悄悄发生
    assert caps["retrieval"]["top_k_max"] == 100
    assert caps["retrieval"]["candidate_k_max"] == 500


def _le(model: type[Any], field: str) -> int:
    return next(
        int(bound.le) for bound in model.model_fields[field].metadata if hasattr(bound, "le")
    )


def test_every_capability_group_is_filled_in() -> None:
    """字段齐：每一组都回实值，没有一组是空对象或 ``None``。"""
    body = provider.build_handshake(_FakeServices(), _api_key_caller()).model_dump(by_alias=True)
    caps = body["capabilities"]

    assert caps["retrieval"]["modes"] == ["hybrid", "vector", "fulltext"]
    assert caps["retrieval"]["default_mode"] == "hybrid"
    assert caps["retrieval"]["rerank"] is True
    assert caps["retrieval"]["filters"] is True

    assert caps["ingest"]["transport"] == "multipart"
    assert caps["ingest"]["async"] is True
    assert caps["ingest"]["dedup"] == "content_hash"
    # 格式清单：不带点号、不重复、非空（阶段 6 的「格式提示」改从它读）
    extensions = caps["ingest"]["extensions"]
    assert extensions and len(extensions) == len(set(extensions))
    assert all(not item.startswith(".") for item in extensions)
    assert extensions == list(provider.INGEST_EXTENSIONS)

    assert caps["tracking"] == {"document": True, "timeline": True}
    assert caps["knowledge_bases"] == {
        "create": True,
        "delete": True,
        "folders": True,
        "shares": True,
        "wiki": True,
    }


def test_embedding_reports_the_runtime_state_not_the_code() -> None:
    """向量化那一组**如实报当前状态**：没配就是 false，开发嵌入要标出来。

    报"代码里支持嵌入"没有意义——客户端要据此决定建库入口能不能点。
    """
    unconfigured = provider.build_handshake(
        _FakeServices(embedding_configured=False), _api_key_caller()
    ).capabilities.embedding
    assert unconfigured.configured is False

    development = _FakeEmbedder(model_id="dev-hash", dim=8, is_development=True)
    configured = provider.build_handshake(
        _FakeServices(embedder=development), _api_key_caller()
    ).capabilities.embedding
    assert configured.configured is True
    assert configured.is_development is True
    assert configured.model_id == "dev-hash"
    assert configured.dim == 8


# ------------------------------------------------------------------ 3. 归属过滤


def test_a_restricted_key_only_sees_the_knowledge_bases_in_its_scope() -> None:
    """受限 key 只看到范围内的库（方案 R9：握手不能变成元信息泄露的出口）。"""
    services = _FakeServices(
        records=[_kb("kb_a", "论文"), _kb("kb_b", "别人的库")],
        stats={"kb_a": (12, datetime(2026, 9, 1, tzinfo=UTC)), "kb_b": (3, None)},
        visible=["kb_a"],
        writable=frozenset({"kb_a"}),
    )

    briefs = provider.build_handshake(services, _api_key_caller()).knowledge_bases

    assert [brief.id for brief in briefs] == ["kb_a"]
    assert briefs[0].name == "论文"
    assert briefs[0].document_count == 12
    assert briefs[0].last_activity == datetime(2026, 9, 1, tzinfo=UTC)
    assert briefs[0].embedding_model_id == "bge-m3"


def test_a_read_only_restricted_key_is_reported_as_unable_to_write() -> None:
    """只读档的受限 key：``caller.can_write=false``、每库的 ``can_write`` 也是 false。

    ``is_admin`` **如实报 false**（方案 §1.4：本机后端那把钥匙在 NAS 侧不是管理员）——
    界面据此隐藏管理员入口，而不是摆出来等着 403。
    """
    services = _FakeServices(
        records=[_kb("kb_a", "论文")],
        stats={"kb_a": (1, None)},
        visible=["kb_a"],
        writable=frozenset(),
    )
    caller = _api_key_caller(ApiKeyPermission.READONLY, kb_ids=("kb_a",))

    handshake = provider.build_handshake(services, caller)

    assert handshake.caller.kind == "api_key"
    assert handshake.caller.permission is ApiKeyPermission.READONLY
    assert handshake.caller.is_admin is False
    assert handshake.caller.can_write is False
    assert handshake.caller.knowledge_base_ids == ["kb_a"]
    assert [brief.can_write for brief in handshake.knowledge_bases] == [False]


def test_an_unrestricted_key_sees_every_knowledge_base() -> None:
    """不限范围的钥匙（壳领的那把）：库清单是全部，``knowledge_base_ids`` 空 = 不限。"""
    services = _FakeServices(
        records=[_kb("kb_a", "论文"), _kb("kb_b", "笔记库")],
        stats={"kb_a": (2, None), "kb_b": (0, None)},
        visible=None,
        writable=frozenset({"kb_a", "kb_b"}),
    )

    handshake = provider.build_handshake(services, _api_key_caller())

    assert [brief.id for brief in handshake.knowledge_bases] == ["kb_a", "kb_b"]
    assert handshake.caller.knowledge_base_ids == []
    assert handshake.caller.can_write is True
    assert [brief.can_write for brief in handshake.knowledge_bases] == [True, True]


def test_a_member_session_is_not_admin_but_may_write() -> None:
    """登录成员：``kind=session``、``is_admin=false``，但**能写**（自己的库）。

    这两件事必须分开："不是管理员"（碰不了设置页）与"能不能写"（能不能上传）是
    两条独立的判定，混成一个布尔值就会把成员的建库入口一起收掉。
    """
    caller = _session_caller(UserRole.MEMBER)

    brief = provider.caller_brief(caller)

    assert brief.kind == "session"
    assert brief.is_admin is False
    assert brief.can_write is True
    assert brief.permission is ApiKeyPermission.READWRITE
    assert brief.knowledge_base_ids == []


def test_an_admin_session_is_reported_as_admin() -> None:
    """管理员会话：``is_admin=true``（管理员入口该摆出来）。"""
    brief = provider.caller_brief(_session_caller(UserRole.ADMIN))

    assert brief.kind == "session"
    assert brief.is_admin is True
    assert brief.can_write is True


def test_the_brief_and_the_list_share_one_write_rule() -> None:
    """**同一批评分字段口径**（方案 §7 阶段 1）：握手与 ``GET /knowledge-bases`` 一致。

    这是这条改动里最值得钉住的一条 ✗——两处各写一份的表现是**界面自相矛盾**
    （列表上说能传、点进去 403，或反过来把能用的入口收起来）。
    四种主体形状各比一遍：管理员会话、成员（自己的库 / 别人的库）、受限 key。
    """
    own = _kb("kb_a", "我的库", owner_id="user_1")
    other = _kb("kb_b", "别人的库", owner_id="user_2")
    services = _FakeServices(
        records=[own, other],
        stats={"kb_a": (1, None), "kb_b": (1, None)},
        visible=["kb_a"],
        writable=frozenset(),
    )

    cases: list[Caller] = [
        _session_caller(UserRole.ADMIN),
        _session_caller(UserRole.MEMBER, user_id="user_1"),
        _session_caller(UserRole.MEMBER, user_id="user_2"),
        _api_key_caller(ApiKeyPermission.READONLY, kb_ids=("kb_a",)),
        _api_key_caller(ApiKeyPermission.READWRITE, kb_ids=("kb_a",)),
    ]

    for caller in cases:
        for record in (own, other):
            listed = _out(record, caller, services).can_write
            brief = provider._kb_brief(record, caller, services).can_write
            assert listed == brief, f"{caller.kind}/{record.id} 两处口径不一致"


def test_document_counts_come_from_one_aggregated_query() -> None:
    """计数与最近活动取自 ``document_stats()``（一次聚合），不是逐库数。"""
    calls: list[str] = []

    class _Counting(_FakeKnowledgeBases):
        def document_stats(self) -> dict[str, Any]:
            calls.append("stats")
            return super().document_stats()

    services = _FakeServices(records=[_kb("kb_a", "a"), _kb("kb_b", "b")], visible=None)
    services.knowledge_bases = _Counting(
        [_kb("kb_a", "a"), _kb("kb_b", "b")], {"kb_a": (7, None), "kb_b": (0, None)}
    )

    briefs = provider.kb_briefs(services, _api_key_caller())

    assert calls == ["stats"]
    assert [brief.document_count for brief in briefs] == [7, 0]


def test_server_time_is_timezone_aware_utc() -> None:
    """``server_time`` 带时区（规范 §1.2）：客户端拿它显示"上次确认是什么时候"。"""
    handshake = provider.build_handshake(_FakeServices(), _api_key_caller())

    assert handshake.server_time.tzinfo is not None
    assert handshake.server_time.utcoffset() == UTC.utcoffset(None)


# --------------------------------------------- 4. 挂在服务器档、鉴权在它前面


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """按指定档位现建一个 app（**不碰模块级 ``app``**）。

    模块级那个 app 的档位取决于"谁先 import 了 `app.main`"——测试进程里那是
    会话级的事实，用它来判"本机档有没有这条路由"会随用例顺序漂。所以这里逐次现建，
    并在收尾把这几个单例缓存清掉（它们是 `lru_cache`，档位换过就必须重读）。
    """

    def _build(deployment: str):
        monkeypatch.setenv("KYLAB_DEPLOYMENT", deployment)
        # 本机档不接受引导级库连接串（`Settings` 的校验器会直接抛），压成空串
        monkeypatch.setenv("KYLAB_DATABASE_URL", "")
        get_settings.cache_clear()
        reset_services()
        reset_stores()
        return create_app()

    yield _build
    get_settings.cache_clear()
    reset_services()
    reset_stores()


def test_the_server_deployment_serves_the_handshake(build_app: Any) -> None:
    """服务器档：这条路径真的在（**由 OpenAPI 判据，而不是读内存里的 router 对象**）。"""
    paths = build_app("server").openapi()["paths"]

    assert "/api/v1/provider/handshake" in paths
    assert paths["/api/v1/provider/handshake"]["get"]["summary"]


def test_the_local_deployment_does_not_serve_the_handshake(build_app: Any) -> None:
    """本机档**不挂**它（方案 §9-1、`api/v1/router.py` 的不挂清单）。

    本机档是提供者的**客户端**：它该调这个端点，而不是提供它。判据同样走 OpenAPI ——
    本机档那张白名单里没有这条路径，所以客户端打过去会是 404。
    """
    paths = build_app("local").openapi()["paths"]

    assert "/api/v1/local/status" in paths, "本机档那张表本身要在这（自检）"
    assert "/api/v1/provider/handshake" not in paths
    assert "/api/v1/knowledge-bases" not in paths


def test_the_local_deployment_serves_the_provider_status_and_patch(build_app: Any) -> None:
    """本机档：``/local/provider`` 的 **GET 与 PATCH 都在**（M3 阶段 5 的判定源）。

    判据走 OpenAPI（与相邻那两条同一手法；不读内存里的 router 对象）：这张白名单是
    "本机运行时到底能做什么"的唯一定义，而"端点没挂上"这一类问题要在**这里**被看见，
    不能等界面点进去撞 404。顺手钉住 ``refresh`` 那个查询参数——它是"强制重探"
    那一条失效路径的入口（方案 §3.2），少了它就只剩 30s 的 TTL 一条路。
    """
    paths = build_app("local").openapi()["paths"]

    assert "/api/v1/local/provider" in paths
    methods = paths["/api/v1/local/provider"]
    assert methods["get"]["summary"] and methods["patch"]["summary"]
    parameters = [item["name"] for item in methods["get"].get("parameters", [])]
    assert "refresh" in parameters, parameters


def test_the_server_deployment_does_not_serve_the_provider_status(build_app: Any) -> None:
    """服务器档**不挂** ``/local/provider``：那一档的知识库就是它自己（没有第二个东西可问）。

    "本机档专属"这句话在两处成立：路由表上（这条）与组合根上（那一档的
    ``Services.provider`` 是 ``None``）——客户端打过去会是 404，而不是一句
    看着像"还没配"的空 ``unconfigured``。
    """
    paths = build_app("server").openapi()["paths"]

    assert "/api/v1/provider/handshake" in paths, "服务器档那张表本身要在这（自检）"
    assert "/api/v1/local/provider" not in paths
    assert "/api/v1/local/status" not in paths


def test_the_provider_router_is_only_in_the_server_table() -> None:
    """路由表对象级的那一眼：`provider.router` 只在 `api_router` 的挂载清单里。

    与上一条互为佐证——上一条验的是"建出来的应用服务不服务它"，
    这条验的是"它是被 include 到哪张表里的"（改错了这里，一眼能看出是挂号挂错了）。
    """
    assert id(provider.router) in {id(entry.original_router) for entry in api_router.routes}
    assert id(provider.router) not in {id(entry.original_router) for entry in local_router.routes}


def test_no_credentials_is_a_401_and_carries_no_handshake_body(build_app: Any) -> None:
    """没有凭据 → **401 + 统一错误信封**，响应体里没有握手那八位。

    这条就是"客户端能把两档分开"的**协议证据**：401/403 是"改钥匙"，
    连不上（超时 / 连接被拒 / 404）是"改地址"。所以凭据问题**不进握手体** ✗ ——
    它要是进了，客户端就得先解析一个可能根本不存在的响应体才能判类别。

    用 ``dependency_overrides`` 换掉**两个根**：本用例验的是鉴权挡在前面，
    不该为了它去连一台 PG（也正是本文件标 ``local`` 的理由）。
    鉴权那条链注入的是 KB 根（`api/auth.py` 的 `KbServices`），所以两个都要换。
    """
    app = build_app("server")
    app.dependency_overrides[get_services] = lambda: _FakeServices()
    app.dependency_overrides[get_kb_services] = lambda: _FakeServices()
    try:
        response = TestClient(app).get("/api/v1/provider/handshake")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401, response.text
    body = response.json()
    assert body["code"] == "unauthorized"
    assert set(body) == {"code", "message"}
    assert not (CONTRACT_KEYS & set(body))
