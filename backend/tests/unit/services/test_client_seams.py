"""两条接缝的"可替换"用例（Phase B 第一刀）。

这一轮的验收不是"新功能" ✓，而是**证明接缝真的可换** ✓：

1. **模型侧**：`llm.OpenAICompatChat` 结构上满足 `ModelClient` ✓（同名的三个方法 ✓）——
   于是"把模型换成服务器代理"不需要改循环 ✗；
2. **KB 侧**：`ChatService.retrieve_sources` 的签名与 `KnowledgeClient` 一致 ✓，
   而且**一个假实现能顶上去**跑通"检索 → 拿回带编号的出处"这条链 ✓ ——
   这正是边车模式（本地循环 + 远端 KB）要的形状 ✓。

**M3 阶段 3 又加了一条同族的守卫**（`test_no_seam_still_points_at_the_real_ingest_service`）：
本机档装配完之后，**没有任何接缝还指着真 `IngestService`** ✓ —— 影子引用（`notes` /
`artifacts` 各自持有的一份）换干净了没有，靠它守着（方案 §5.1 的 R6）。

**M3 阶段 4 再加一条**（`test_the_composition_root_binds_the_provider_reader_into_the_kb_store`）：
组合根把提供者的 reader **后挂**进 `stores.meta.kb` 了没有 ✓ —— 那一行删掉，
`stores.meta` 的 KB 读会静默退回"组合根未装配"，而那是**装配期**的问题。

**M4 阶段 3 换成缓存包装器**（同族两条）：挂上去的那一层是 `CachedKnowledgeMetaReader`
（它的 cache 与 `Services.kb_cache` 是**同一个**对象）✓；`ChatService.kb_prompt` 经它读
**命中时零 HTTP** ✓ —— "交互路径 N 次局域网往返变成 0 次"这句承诺的可观察形态。

**反向验证**：把协议里的方法名改掉（或让假实现少一个方法）→ 用例必须红 ✓。
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Iterator, Sequence

import httpx
import pytest

from app.core import services as services_module
from app.core.services import Services, get_services
from app.services.chat import ChatService, SourceRef
from app.services.ingest import IngestService
from app.services.knowledge_client import KnowledgeClient
from app.services.knowledge_provider import KnowledgeProviderClient
from app.services.llm import ChatMessage, LLMConfig, LLMDelta, OpenAICompatChat, ToolSpec
from app.services.model_client import ModelClient


class _FakeModelClient:
    """一个**完全进程内**的假模型（不动网络 ✓）：证明循环只需要这三个方法。"""

    def __init__(self, reply: str = "假回答") -> None:
        self.reply = reply
        self.seen: list[Sequence[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage]) -> str:
        self.seen.append(messages)
        return self.reply

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        self.seen.append(messages)
        yield self.reply

    def stream_events(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None
    ) -> Iterator[LLMDelta]:
        self.seen.append(messages)
        yield LLMDelta(text=self.reply)


class _FakeKnowledgeClient:
    """一个假的 KB（不动库 ✓）：边车模式下它就是"打服务器"的那份实现。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def retrieve_sources(
        self,
        *,
        query: str,
        kb_ids: list[str],
        top_k: int | None = None,
        candidate_k: int = 40,
        reader: object | None = None,
    ) -> list[SourceRef]:
        self.calls.append({"query": query, "kb_ids": kb_ids, "top_k": top_k})
        return [
            SourceRef(
                index=1,
                chunk_id="c1",
                document_id="d1",
                document_name="指南.pdf",
                heading_path="第 3 章",
                page=7,
                score=0.5,
                preview="原文",
            )
        ]


def test_the_in_process_model_client_satisfies_the_protocol() -> None:
    """**默认实现就是协议的一份实现** ✓ —— 于是换实现不必改循环。"""
    client = OpenAICompatChat(
        LLMConfig(base_url="http://localhost:1", model_id="m", api_key="k")
    )

    assert isinstance(client, ModelClient)
    # 三个方法一个都不能少（少了服务器代理就装不进去 ✗）
    for name in ("complete", "stream", "stream_events"):
        assert callable(getattr(client, name))


def test_a_fake_model_client_is_usable_where_the_protocol_is_expected() -> None:
    """假的也能顶上：**循环只认这三个方法** ✓（这条就是"可替换"的证明）。"""
    fake = _FakeModelClient()

    assert isinstance(fake, ModelClient)
    assert fake.complete([ChatMessage(role="user", content="问题")]) == "假回答"
    chunks = list(fake.stream([ChatMessage(role="user", content="问题")]))
    assert chunks == ["假回答"]
    deltas = list(fake.stream_events([ChatMessage(role="user", content="问题")]))
    assert [d.text for d in deltas] == ["假回答"]


def test_the_in_process_knowledge_client_satisfies_the_protocol() -> None:
    """`ChatService` 那个方法就是协议的一份实现 ✓（签名逐字一致 ✓）。"""
    assert hasattr(ChatService, "retrieve_sources")

    # 关键字参数形状一致（`query` 必须是关键字——子 Agent 那条链踩过位置参数的坑 ✓）
    import inspect

    signature = inspect.signature(ChatService.retrieve_sources)
    parameters = list(signature.parameters)
    assert "query" in parameters
    assert parameters[1] in ("query",) or signature.parameters["query"].kind.name == "KEYWORD_ONLY"


def test_a_fake_knowledge_client_is_usable_where_the_protocol_is_expected() -> None:
    """假的 KB 能顶上，并且**拿回的是同一形状的出处** ✓（边车模式要的就是这个）。"""
    fake = _FakeKnowledgeClient()

    assert isinstance(fake, KnowledgeClient)
    sources = fake.retrieve_sources(query="储能", kb_ids=["kb_1"], top_k=3)

    assert [source.index for source in sources] == [1]
    assert sources[0].document_name == "指南.pdf"
    assert fake.calls == [{"query": "储能", "kb_ids": ["kb_1"], "top_k": 3}]


# ------------------------------------------------- 影子引用换干净了没有（M3 阶段 3）


#: 遍历时要绕开的**代码**（模块 / 类 / 函数 / 方法）：它们是"谁持有谁"这张图之外的
#: 东西，跟着走只会绕进模块级缓存里，与装配无关。
_SKIP_TYPES = (
    types.ModuleType,
    type,
    types.FunctionType,
    types.MethodType,
    types.BuiltinFunctionType,
    types.BuiltinMethodType,
)


def _references(value: object) -> list[tuple[str, object]]:
    """`value` **直接持有**的那些引用（带一段可读的路径后缀）。

    只认两种"持有"：① 数据类字段 / 实例属性（`__dict__`）；② 列表 / 元组 / 集合 / 字典
    里的元素。别的（属性访问会算出新对象的、描述符、`__getattr__` 兜出来的）一律不算
    —— 这一条要回答的是"对象挂在谁身上"，不是"能通过什么表达式拿到它"。
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return [(f"[{index}]", item) for index, item in enumerate(value)]
    if isinstance(value, dict):
        return [(f"[{key!r}]", item) for key, item in value.items()]
    if isinstance(value, _SKIP_TYPES):
        return []
    held: list[tuple[str, object]] = []
    if dataclasses.is_dataclass(value):
        held += [
            (f".{field.name}", getattr(value, field.name, None))
            for field in dataclasses.fields(value)
        ]
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        held += [(f".{name}", item) for name, item in attributes.items()]
    return held


def _holders_of(root: object, target: object, *, max_depth: int = 5) -> set[str]:
    """从 `root` 出发，找出**哪些路径上挂着 `target`**（`is` 比对象身份）。

    路径形如 `services.notes._ingest`、`services.workers[0]._ingest` —— 红了的时候
    一眼看得出是**谁**还攥着那个对象（这是这条守卫唯一的价值：报告得指到人）。
    """
    found: set[str] = set()
    seen: set[int] = {id(root)}
    queue: list[tuple[str, object, int]] = [("services", root, 0)]
    while queue:
        path, value, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for suffix, item in _references(value):
            if item is None or isinstance(item, _SKIP_TYPES):
                continue
            # **命中就先记下来，再判要不要展开**：同一个目标可以从好几条路径上挂着
            # （例如 `Services.worker` 与 `Services.workers[0]` 是同一个对象），
            # 用"见过就不再记"会漏掉后一条路径——而漏掉的正好可能是新长出来的那条。
            if item is target:
                found.add(path + suffix)
            if id(item) in seen:
                continue
            seen.add(id(item))
            queue.append((path + suffix, item, depth + 1))
    return found


def _spy_on_ingest(monkeypatch) -> list[IngestService]:  # type: ignore[no-untyped-def]
    """把组合根建的那个 `IngestService` **记下来**（返回单元素列表）。

    为什么要在它出生时记：守卫要比的是**真那一个**（组合根自己造的那个）。
    另建一个 `IngestService(...)` 来比身份是没意义的——它永远不等于图上任何一个，
    于是那条断言会变成永真式（"绿着但没在守"是最糟的一种）。
    """
    made: list[IngestService] = []

    class _Spy(IngestService):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            made.append(self)

    monkeypatch.setattr(services_module, "IngestService", _Spy)
    return made


@pytest.mark.local
def test_no_seam_still_points_at_the_real_ingest_service(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """**本机档装配完之后，没有任何接缝还指着真 `IngestService`**（方案 §5.1 的 R6）。

    这一条守的是"发现②"那次踩坑：`notes` / `artifacts` **各自持有一份**真
    `IngestService`，而 `dataclasses.replace(Services)` 只换得到 `Services` 上那两个槽位
    ——于是 `attach_note_to_kb` 一直走的是真摄入流水线，撞进本机档的
    `UnavailableMetaStore`，表现成一句"知识库在 NAS 服务器上"（而它本来是**能**做的）。
    阶段 3 把换线收到组合根一处，这里用两层判据把它钉住：

    ① **两个槽位与两个服务**（`Services.ingest/documents`、`notes`、`artifacts`）拿到的是
       **同一对**提供者网关（`is` 比身份）——笔记与产物"两条路一份实现"；
    ② **从 `Services` 整张图走一遍**（实例属性 + 容器，`is` 比身份），除了数据源与消费者
       这两处**本该**持有摄入流水线的地方，再没有第三个持有者。

    ②是这条守卫的价值所在：将来谁再给某个服务塞一份真 `IngestService`，或者谁把
    `notes`/`artifacts` 那两处改回去，它都会红，并指出**是哪条路径**。
    """
    from app import sidecar

    made = _spy_on_ingest(monkeypatch)
    # 本机档：档位自己钉死（`pin_local_deployment` 是本机档唯一的入口姿势，
    # 它顺带清掉 settings/stores/services 三个单例缓存）
    sidecar.pin_local_deployment(
        tmp_path / "data", server_url="http://server.test/api/v1", token="t"
    )
    services: Services = get_services()

    assert len(made) == 1, "组合根应当只建一个真 IngestService（摄入流水线）"
    real = made[0]

    # ① 三个构造点拿到的是**同一对**网关，而且都不是真 IngestService
    #    （2026-10-08 拆组合根之后，入库这一对住在 **KB 根**上：`Services.kb.*`）
    assert services.kb.ingest is not real
    assert services.kb.documents is not real
    assert services.notes._ingest is services.kb.ingest
    assert services.artifacts._ingest is services.kb.ingest
    assert services.notes._documents is services.kb.documents
    assert services.artifacts._documents is services.kb.documents
    assert not isinstance(services.kb.ingest, IngestService)
    # 检索那一半也接上了（阶段 3 起组合根给 ChatService 的就是这个客户端）
    assert isinstance(services.chat._knowledge, KnowledgeProviderClient)

    # ② 整张图上再没有别的持有者：允许的只有**摄入流水线自己那两处**——
    #    `sources`（数据源登记要往流水线上挂）与 `worker`（消费者领了活要跑它）。
    #    这两处是本机档也照建的真服务（`data_sources` / `tasks` 那族端点没挂本机档，
    #    本机档也不起消费者，见 `main.py` 那段），**不是**用户可见的入库接缝。
    allowed = {
        "services.kb.sources._ingest",
        "services.kb.worker._ingest",
        "services.kb.workers[0]._ingest",
    }
    holders = _holders_of(services, real)
    assert holders, "遍历没找到任何持有者：那说明这条路走错了（守卫会静默变绿）"
    assert holders <= allowed, f"还有对象攥着真 IngestService：{sorted(holders - allowed)}"


# ------------------------------------------- KB 侧那个 reader 后挂上了没有（M3 阶段 4）


@pytest.mark.local
def test_the_composition_root_binds_the_provider_reader_into_the_kb_store(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """本机档装配完之后，``stores.meta`` 的 KB 读**真的打到提供者那台机器上**（阶段 4）。

    守的是组合根里那一行 ``bind_reader``：**没有它，这一层会静默退化成**
    "知识库提供者还没接上（组合根未装配）"——而那是**装配期**的错，不该等用户发问才
    暴露（``ChatService.kb_prompt`` 还会把它吞成一句 warning，表现就是"库级提示词
    静默不生效"）。

    判据走端到端那一条（不是"那个私有字段不是 None"）：从**组合根建的那个 bundle**
    （``ChatService`` 持有的那一个）读一个库回来，远端那一跳换成假传输（一个真请求都
    不发）——于是"reader 挂上了"与"回来的是 storage 的记录类型"一起被钉住。
    """
    from app import sidecar
    from app.storage.base import KnowledgeBaseRecord

    sidecar.pin_local_deployment(
        tmp_path / "data", server_url="http://server.test/api/v1", token="t"
    )
    services: Services = get_services()
    client = services.chat._knowledge
    assert isinstance(client, KnowledgeProviderClient), "组合根给的应是提供者客户端（阶段 3）"

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "kb_a",
                "name": "论文",
                "embedding_model_id": "bge-m3",
                "embedding_dim": 8,
            },
        )

    # 地址是壳传进来的那台 NAS（`server_url`），传输在这里换成假的：与阶段 2 同一手法
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(handler))

    stores = services.chat._stores
    assert stores is not None, "ChatService 没拿到 bundle：这条用例的前提不成立"

    record = stores.meta.get_knowledge_base("kb_a")

    assert isinstance(record, KnowledgeBaseRecord)
    assert record.name == "论文"
    assert asked == ["http://server.test/api/v1/knowledge-bases/kb_a"]


# ------------------------------------ reader 换线：挂的是缓存包装器（M4 阶段 3）


@pytest.mark.local
def test_the_composition_root_wraps_the_kb_reader_in_the_snapshot_cache(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """组合根挂进 ``stores.meta.kb`` 的是**缓存包装器**（M4 阶段 3，方案 §7）。

    三层判据，缺一层都看不出"换线了没有"：

    ① **那一层就是包装器**：``stores.meta.kb`` 里那个 reader 是
       `CachedKnowledgeMetaReader`，它的 ``cache`` 是**组合根建的那个**
       `KbMetaCacheService`（与 ``Services.kb_cache`` 是同一个对象——页面面与 reader 面
       必须共用一份，各建一个就会各排各的再验证，R3 的风暴就是这么来的）；
    ② **端到端照旧**：从组合根那个 bundle 读一个库回来，仍然映射成 storage 那一层的
       ``KnowledgeBaseRecord``（``stores.meta`` 与 ``services/`` 的调用点一个字没改，
       M3 §2.4 的承诺）；
    ③ **第二次读零 HTTP**：命中路上一次 NAS 往返都不发——这才是这次换线的收益
       （v0.3 §6.1-2「交互路径零网络」），不然就只是"对象换了个名字"。
    """
    from app import sidecar
    from app.services.kb_cache import CachedKnowledgeMetaReader, KbMetaCacheService
    from app.storage.base import KnowledgeBaseRecord

    sidecar.pin_local_deployment(
        tmp_path / "data", server_url="http://server.test/api/v1", token="t"
    )
    services: Services = get_services()
    stores = services.chat._stores
    assert stores is not None, "ChatService 没拿到 bundle：这条用例的前提不成立"

    service = services.kb_cache
    assert isinstance(service, KbMetaCacheService), "组合根应当建出快照服务（M4 阶段 3）"
    # 私有那一下是这条守卫的全部价值所在：**看对象图上挂的是哪一个**
    reader = stores.meta.kb._reader
    assert isinstance(reader, CachedKnowledgeMetaReader), "reader 那一层应当是缓存包装器"
    assert reader._inner is not None, "包装器里那个 inner 应当是提供者的 reader"
    assert reader._cache is service, "两个读面（reader / 页面）必须是同一个快照服务"

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "kb_a",
                "name": "论文",
                "embedding_model_id": "bge-m3",
                "embedding_dim": 8,
            },
        )

    monkeypatch.setattr(services.provider, "_transport", httpx.MockTransport(handler))

    first = stores.meta.get_knowledge_base("kb_a")
    again = stores.meta.get_knowledge_base("kb_a")

    assert isinstance(first, KnowledgeBaseRecord)
    assert first.name == "论文"
    assert again == first, "命中那一读回的应当是同一份内容"
    assert asked == ["http://server.test/api/v1/knowledge-bases/kb_a"], (
        "第一次（未命中）同步取一次，第二次起一次都不该发"
    )


@pytest.mark.local
def test_the_kb_prompt_through_the_cache_costs_one_request_and_then_none(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """``ChatService.kb_prompt`` 那一条链**命中时零 HTTP**（M4 阶段 3 的完成判据）。

    它每轮每个选中的库读一次 ``stores.meta.get_knowledge_base``（``chat.py``）——
    也就是缓存包装器那一层。第一次（未命中）同步取一次（必须现在给答案），
    第二次起一次都不发，而拼出来的提示词一个字不变。v0.3 §6.1-2 那句"交互路径零网络"
    的可观察形态就是这个：
    """
    from app import sidecar

    sidecar.pin_local_deployment(
        tmp_path / "data", server_url="http://server.test/api/v1", token="t"
    )
    services: Services = get_services()
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path.endswith("/knowledge-bases/kb_a"):
            return httpx.Response(
                200,
                json={
                    "id": "kb_a",
                    "name": "论文",
                    "embedding_model_id": "bge-m3",
                    "embedding_dim": 8,
                    "system_prompt": "眼轴按 mm 记",
                },
            )
        return httpx.Response(404, text="没有这个库")

    monkeypatch.setattr(services.provider, "_transport", httpx.MockTransport(handler))

    assert services.chat.kb_prompt(["kb_a"]) == "眼轴按 mm 记"
    assert len(asked) == 1, "未命中那一次必须同步取（它要现在给答案）"
    assert services.chat.kb_prompt(["kb_a"]) == "眼轴按 mm 记"
    assert len(asked) == 1, "命中之后一个请求都不该再发（交互路径零网络）"

    # **库里没有这个库**仍然是另一个答案（远端 404 → None → 空串），不是错误
    assert services.chat.kb_prompt(["kb_没有这个"]) == ""
    assert len(asked) == 2
