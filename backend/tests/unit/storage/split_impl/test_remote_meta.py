"""``RemoteMetaStore``：两个真映射 + 其余照旧抛（M3 阶段 4，方案 §2.1 / §2.3）。

镜像同构：``app/storage/split_impl/remote_meta.py`` → 本文件。

四块内容：

1. **映射**：reader 给的 dict → ``KnowledgeBaseRecord``——两边共有的那几栏照搬，
   远端没有的那几栏留在记录类型自己的默认值上（**不在这里编一个值出来**）；
2. **一档答案**：reader 回 ``None``（远端 404）= **没有这个库**，不是出错；
3. **一档错误**：远端 5xx / 连不上 / 没配 → ``KnowledgeBaseUnavailable``。这一块**故意用
   真客户端 + ``httpx.MockTransport``**（不打真网络）：它要证的正是**跨层的那一条契约**
   ——远端失败到了 ``stores.meta`` 那一面必须是 storage 认得的错误，而不是"工具循环"
   那一面的 ``RemoteUnavailableError``（那是给循环分档用的，``stores.meta`` 的调用方
   只认"现在取不到"这一个答案）；
4. **接缝**：``bind_reader`` 之后从 ``RouterMetaStore`` 走一遍（services 走的就是这条），
   KB 域确实发往远端那一侧。

本文件标 ``local``：它只拼请求、只读常量的字段，一个远端都不连。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services.knowledge_provider import KnowledgeProviderClient
from app.storage.base import KnowledgeBaseRecord, KnowledgeBaseUnavailable
from app.storage.split_impl import RemoteMetaStore, RouterMetaStore, UnavailableMetaStore
from app.storage.split_impl import remote_meta as remote_meta_module

pytestmark = pytest.mark.local

BASE = "http://nas.test/api/v1"
TOKEN = "kylab_sk_not_a_real_key_but_from_the_shell"

#: NAS 那份 ``KnowledgeBaseOut`` 的形状（照 ``api/v1/schemas.py`` 抄全：它是个 pydantic
#: 响应模型，所以远端**每一栏都会给**——这里多出来的 ``can_write`` / ``document_count`` /
#: ``last_activity`` 是远端有、记录类型没有的三栏，正好顺手钉住"多余的键被忽略"）。
KB_PAYLOAD: dict[str, object] = {
    "id": "kb_a",
    "name": "论文",
    "description": "眼轴那些事",
    "embedding_model_id": "bge-m3",
    "embedding_dim": 1024,
    "chunk_strategy": "fixed",
    "chunk_size": 800,
    "chunk_overlap": 80,
    "suggested_enabled": True,
    "suggested_count": 5,
    "suggested_model_pk": "m_1",
    "suggested_prompt": "多出几道难题",
    "system_prompt": "只答这个库里的东西",
    "wiki_enabled": True,
    "created_at": "2026-10-03T09:00:00Z",
    "can_manage": True,
    "can_write": True,
    "document_count": 12,
    "last_activity": "2026-10-04T09:00:00Z",
}


class _Reader:
    """假的 ``KnowledgeMetaReader``：只回我让它回的东西（两件事，与协议逐字一致）。"""

    def __init__(
        self,
        *,
        one: dict[str, object] | None = None,
        many: list[dict[str, object]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.one = one
        self.many: list[dict[str, object]] = many if many is not None else []
        self.raises = raises
        self.asked: list[str] = []

    def get_knowledge_base(self, kb_id: str) -> dict[str, object] | None:
        self.asked.append(kb_id)
        if self.raises is not None:
            raise self.raises
        return self.one

    def list_knowledge_bases(self) -> list[dict[str, object]]:
        if self.raises is not None:
            raise self.raises
        return self.many


def _bound(reader: object) -> RemoteMetaStore:
    store = RemoteMetaStore()
    store.bind_reader(reader)  # type: ignore[arg-type]  # 假的也满足那个协议
    return store


def _settings(**overrides: Any) -> Settings:
    """一份与当前机器无关的引导级配置（``_env_file=None`` 挡掉开发机的 ``.env``）。"""
    base: dict[str, Any] = {
        "deployment": "local",
        "database_url": "",
        "server_url": "",
        "token": "",
        "kb_url": "",
        "kb_token": "",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def _provider_store(handler: Any, *, settings: Settings | None = None) -> RemoteMetaStore:
    """真客户端（假传输）+ ``bind_reader``——第 3 块用的那条链。"""
    client = KnowledgeProviderClient(
        settings=settings if settings is not None else _settings(server_url=BASE, token=TOKEN),
        get_setting=lambda key: "",
        transport=httpx.MockTransport(handler),
    )
    return _bound(client.knowledge_meta())


# ------------------------------------------------------------------ 1 映射


def test_the_reader_payload_becomes_a_record() -> None:
    """两边共有的那几栏照搬（含远端有、记录类型没有的多余键：忽略，不认识就不装）。"""
    record = _bound(_Reader(one=dict(KB_PAYLOAD))).get_knowledge_base("kb_a")

    assert record is not None
    assert record.id == "kb_a"
    assert record.name == "论文"
    assert record.description == "眼轴那些事"
    assert record.embedding_model_id == "bge-m3"
    assert record.embedding_dim == 1024
    assert record.chunk_strategy == "fixed"
    assert record.chunk_size == 800
    assert record.chunk_overlap == 80
    assert record.suggested_enabled is True
    assert record.suggested_count == 5
    assert record.suggested_model_pk == "m_1"
    assert record.suggested_prompt == "多出几道难题"
    assert record.system_prompt == "只答这个库里的东西"
    assert record.wiki_enabled is True
    assert record.created_at is not None and record.created_at.year == 2026


def test_missing_columns_keep_the_record_types_own_defaults() -> None:
    """远端没给的那几栏**留记录类型的默认值**（别在这里抄第二份默认值）。

    判据用**整体相等**而不是逐栏比字面量：于是这条用例不会因为"记录类型的默认值改了"
    而变红，它守的是"远端没给 = 不覆盖"这件事本身。
    """
    payload = {"id": "kb_a", "name": "论文", "embedding_model_id": "bge-m3", "embedding_dim": 1024}

    record = _bound(_Reader(one=payload)).get_knowledge_base("kb_a")

    assert record == KnowledgeBaseRecord(
        id="kb_a", name="论文", embedding_model_id="bge-m3", embedding_dim=1024
    )


def test_a_payload_without_an_id_falls_back_to_the_asked_id() -> None:
    """残缺体（少了 id）：用**问的那个 id**兜底——凭空回一个空 id 只会让上游看不懂。"""
    record = _bound(_Reader(one={"name": "论文"})).get_knowledge_base("kb_a")

    assert record is not None
    assert record.id == "kb_a"
    assert record.name == "论文"


def test_values_of_an_unexpected_shape_do_not_blow_up_the_read() -> None:
    """某一栏变形了（对面换了一版）：表现是"这一栏没有"，不是把调用方炸掉。

    这条路上它只是增强（``ChatService`` 读库级提示词，失败只记一句 warning），
    所以宽容是对的——但**不能编值**：认不出的回空/默认。
    """
    payload: dict[str, object] = {
        "id": "kb_a",
        "name": "论文",
        "embedding_model_id": "bge-m3",
        "embedding_dim": "1024",
        "chunk_size": "八百",
        "wiki_enabled": "true",
        "created_at": "2026-10-03 09:00:00",
    }

    record = _bound(_Reader(one=payload)).get_knowledge_base("kb_a")

    assert record is not None
    assert record.embedding_dim == 1024  # 数字串认得出
    assert record.chunk_size == KnowledgeBaseRecord.__dataclass_fields__["chunk_size"].default
    assert record.wiki_enabled is True  # "true" 认得
    assert record.created_at is not None  # 非 ISO 后缀的写法也认


# ------------------------------------------------------------------ 2 没有这个库


def test_none_from_the_reader_means_there_is_no_such_library() -> None:
    """``None`` 是**答案**（没有这个库），原样传出去——它不是"取不到"（那会抛）。"""
    reader = _Reader(one=None)

    assert _bound(reader).get_knowledge_base("kb_missing") is None
    assert reader.asked == ["kb_missing"]


# ------------------------------------------------------------------ 3 取不到 = 抛


def test_a_reader_failure_is_passed_through_untouched() -> None:
    """reader 抛什么就抛什么（不吞、不改类型）：那一族的口径是 reader 的事。"""
    failure = KnowledgeBaseUnavailable("知识库提供者现在用不了（连不上 nas.test）")

    with pytest.raises(KnowledgeBaseUnavailable) as excinfo:
        _bound(_Reader(raises=failure)).get_knowledge_base("kb_a")

    assert excinfo.value is failure


def test_remote_five_hundred_becomes_knowledge_base_unavailable() -> None:
    """远端 5xx → ``KnowledgeBaseUnavailable``（**storage 那一面认得的错误**，见模块头）。"""
    store = _provider_store(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(KnowledgeBaseUnavailable) as get_error:
        store.get_knowledge_base("kb_a")
    assert "HTTP 500" in str(get_error.value)
    with pytest.raises(KnowledgeBaseUnavailable) as list_error:
        store.list_knowledge_bases()
    assert "HTTP 500" in str(list_error.value)


def test_a_connection_failure_becomes_knowledge_base_unavailable() -> None:
    """连不上（DNS / 拒绝 / 超时都是这一类）→ 同一族错误，原因在句子里。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上", request=request)

    store = _provider_store(handler)

    with pytest.raises(KnowledgeBaseUnavailable) as get_error:
        store.get_knowledge_base("kb_a")
    assert "连不上" in str(get_error.value)
    with pytest.raises(KnowledgeBaseUnavailable):
        store.list_knowledge_bases()


def test_an_unconfigured_provider_is_the_same_answer() -> None:
    """没配地址（本机档最常见的那一态）也是这一族：对 ``stores.meta`` 就是"没有知识库"。"""
    store = _provider_store(lambda request: httpx.Response(200, json={}), settings=_settings())

    with pytest.raises(KnowledgeBaseUnavailable):
        store.get_knowledge_base("kb_a")
    with pytest.raises(KnowledgeBaseUnavailable):
        store.list_knowledge_bases()


# ------------------------------------------------------------------ 4 清单与接缝


def test_the_list_maps_every_item_in_the_order_the_provider_gave() -> None:
    """库清单：一项一条记录，顺序照远端给的顺序（不排序、不过滤）。"""
    many = [
        {"id": "kb_b", "name": "乙", "embedding_model_id": "m", "embedding_dim": 8},
        {"id": "kb_a", "name": "甲", "embedding_model_id": "m", "embedding_dim": 8},
    ]

    records = _bound(_Reader(many=list(many))).list_knowledge_bases()

    assert [record.id for record in records] == ["kb_b", "kb_a"]
    assert all(isinstance(record, KnowledgeBaseRecord) for record in records)
    assert _bound(_Reader()).list_knowledge_bases() == []


def test_the_router_sends_kb_reads_to_the_remote_side() -> None:
    """挂上路由之后（services 走的就是这条路）：KB 域发往远端那一侧。

    本机域那一半的转发由 ``tests/unit/storage/test_split_impl.py`` 逐方法核对，
    这里只要证明"KB 侧那个槽位上放的确实是这个类、调用真的到了 reader"。
    """
    reader = _Reader(one=dict(KB_PAYLOAD), many=[{"id": "kb_a", "name": "论文"}])
    router = RouterMetaStore(local=UnavailableMetaStore(), kb=_bound(reader))

    assert router.route_of("get_knowledge_base") is not router.route_of("get_setting")
    record = router.get_knowledge_base("kb_a")
    assert record is not None and record.name == "论文"
    assert [item.id for item in router.list_knowledge_bases()] == ["kb_a"]
    assert reader.asked == ["kb_a"]


def test_the_store_module_imports_neither_http_nor_services() -> None:
    """这一层**不认识 HTTP，也不认识 services**（方案 §2.3）：reader 是参数，不是依赖。

    判据落在 **import 图**上（不是"跑起来没炸"，也不是"文件里没出现过这几个字"）：
    真 import 了 `httpx` 或 `app.services` 的那一天，storage 层就已经不认识自己了——
    而它正是"storage 只被组合根装配、只依赖接口层"这条纪律在这一模块上的形态。
    """
    tree = ast.parse(Path(remote_meta_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert not {name for name in imported if name.split(".")[0] in {"httpx", "requests"}}
    assert not {name for name in imported if name.split(".")[:2] == ["app", "services"]}
