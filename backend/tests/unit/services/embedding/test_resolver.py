"""按库解析嵌入模型（v11 设计调整）。

**最要紧的一条**：嵌入模型是知识库属性——"小文档库用高精度模型、大文档库用小模型
提速"这个场景，全靠"每个库各自解析出自己的 embedder"成立。这里把这条性质钉住，
外加两条回退行为：没选（老库）与模型坏掉，都不能让检索/摄入直接失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.services.embedding.base import EmbeddingProvider
from app.services.embedding.resolver import EmbeddingResolver
from app.services.model_registry import ModelRegistryService
from app.storage.base import KnowledgeBaseRecord, ModelProviderRecord, RegisteredModelRecord


class _FakeEmbedder(EmbeddingProvider):
    """只记身份、不做网络：本文件验的是"选对了哪个模型"，不是嵌入质量。"""

    def __init__(self, *, model_id: str, dim: int) -> None:
        self.model_id = model_id
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


@pytest.fixture
def registry(bundle) -> ModelRegistryService:  # type: ignore[no-untyped-def]
    return ModelRegistryService(bundle)


def _register_embedding_model(registry: ModelRegistryService, *, model_id: str, dim: int) -> str:
    provider: ModelProviderRecord = registry.create_provider(
        kind="embedding",
        name=f"供应商-{model_id}",
        base_url="https://api.example.com",
        api_key="sk-x",
    )
    model: RegisteredModelRecord = registry.register_model(
        provider_id=provider.id, model_id=model_id, capabilities=["embedding"], dim=dim
    )
    return model.id


def _resolver(
    registry: ModelRegistryService, *, fallback: EmbeddingProvider
) -> tuple[EmbeddingResolver, list[str]]:
    built: list[str] = []

    def factory(*, model_id: str, dim: int, **_: object) -> EmbeddingProvider:
        built.append(model_id)
        return _FakeEmbedder(model_id=model_id, dim=dim)

    return EmbeddingResolver(registry, fallback=fallback, factory=factory), built


def test_resolves_the_model_chosen_by_the_knowledge_base(
    registry: ModelRegistryService,
) -> None:
    pk = _register_embedding_model(registry, model_id="bge-m3-large", dim=1024)
    fallback = _FakeEmbedder(model_id="global-default", dim=256)
    resolver, built = _resolver(registry, fallback=fallback)

    embedder = resolver.for_kb(
        KnowledgeBaseRecord(
            id="kb_1", name="小库", embedding_model_id="x", embedding_dim=1, embedding_model_pk=pk
        )
    )

    assert embedder.model_id == "bge-m3-large"
    assert embedder.dim == 1024
    assert built == ["bge-m3-large"]


def test_two_knowledge_bases_can_use_different_models(
    registry: ModelRegistryService,
) -> None:
    """这就是这次设计调整要支持的场景：不同库、不同嵌入模型并存。"""
    small = _register_embedding_model(registry, model_id="tiny", dim=384)
    large = _register_embedding_model(registry, model_id="precise", dim=1024)
    resolver, _ = _resolver(registry, fallback=_FakeEmbedder(model_id="fallback", dim=256))

    tiny_model = resolver.for_kb(
        KnowledgeBaseRecord(
            id="kb_a",
            name="大库",
            embedding_model_id="x",
            embedding_dim=1,
            embedding_model_pk=small,
        )
    )
    precise_model = resolver.for_kb(
        KnowledgeBaseRecord(
            id="kb_b",
            name="小库",
            embedding_model_id="x",
            embedding_dim=1,
            embedding_model_pk=large,
        )
    )

    assert (tiny_model.model_id, tiny_model.dim) == ("tiny", 384)
    assert (precise_model.model_id, precise_model.dim) == ("precise", 1024)


def test_knowledge_base_without_a_choice_falls_back(registry: ModelRegistryService) -> None:
    """老库（或建库时没挑模型）走全局默认——升级不打断既有部署。"""
    fallback = _FakeEmbedder(model_id="global-default", dim=256)
    resolver, built = _resolver(registry, fallback=fallback)

    embedder = resolver.for_kb(
        KnowledgeBaseRecord(id="kb_old", name="老库", embedding_model_id="x", embedding_dim=256)
    )

    assert embedder is fallback
    assert built == []


def test_a_broken_reference_falls_back_instead_of_failing(
    registry: ModelRegistryService,
) -> None:
    """模型被删/供应商停用：回退并告警，而不是让这个库彻底无法检索。"""
    fallback = _FakeEmbedder(model_id="global-default", dim=256)
    resolver, _ = _resolver(registry, fallback=fallback)

    embedder = resolver.for_kb(
        KnowledgeBaseRecord(
            id="kb_x",
            name="库",
            embedding_model_id="x",
            embedding_dim=1,
            embedding_model_pk="pk_already_deleted",
        )
    )

    assert embedder is fallback


def test_instances_are_cached_per_model(registry: ModelRegistryService) -> None:
    """一次摄入要分批嵌入几百个 chunk，每批重建客户端是浪费。"""
    pk = _register_embedding_model(registry, model_id="cached", dim=768)
    resolver, built = _resolver(registry, fallback=_FakeEmbedder(model_id="f", dim=256))
    kb = KnowledgeBaseRecord(
        id="kb_c", name="库", embedding_model_id="x", embedding_dim=1, embedding_model_pk=pk
    )

    first = resolver.for_kb(kb)
    second = resolver.for_kb(kb)

    assert first is second
    assert built == ["cached"]


# --------------------------------------------------------------------- 批大小与失效（v0.12 review）


def _resolver_with_batch(
    registry: ModelRegistryService, batch: int | object
) -> tuple[EmbeddingResolver, list[int]]:
    seen: list[int] = []

    def factory(*, model_id: str, dim: int, max_batch: int, **_: object) -> EmbeddingProvider:
        seen.append(max_batch)
        return _FakeEmbedder(model_id=model_id, dim=dim)

    return EmbeddingResolver(registry, fallback=_FakeEmbedder(model_id="f", dim=1),
                             batch_size=batch, factory=factory), seen  # type: ignore[arg-type]


def test_batch_size_comes_from_the_runtime_provider(registry: ModelRegistryService) -> None:
    """批大小要取运行期配置——否则设置页那个项对"按库选模型"是 no-op。"""
    pk = _register_embedding_model(registry, model_id="m", dim=8)
    current = {"batch": 7}
    resolver, seen = _resolver_with_batch(registry, lambda: current["batch"])

    resolver.for_model_pk(pk)
    assert seen == [7]

    # 设置改了 → 下一次解析就用新值
    current["batch"] = 3
    embedded = resolver.for_model_pk(pk)
    assert seen == [7, 3]
    assert embedded is not None


def test_provider_change_invalidates_the_cached_embedder(registry: ModelRegistryService) -> None:
    """改了供应商地址不必重启进程——缓存按"生效配置"而不是只按 model_pk。"""
    provider = registry.create_provider(
        kind="embedding", name="换地址", base_url="https://old.example.com", api_key="sk-1"
    )
    model = registry.register_model(
        provider_id=provider.id, model_id="m", capabilities=["embedding"], dim=8
    )
    resolver, seen = _resolver_with_batch(registry, 32)

    resolver.for_model_pk(model.id)
    registry.update_provider(provider.id, base_url="https://new.example.com")
    resolver.for_model_pk(model.id)

    assert len(seen) == 2, "改了地址之后仍命中旧缓存"


def test_batch_change_invalidates_the_cached_embedder(registry: ModelRegistryService) -> None:
    pk = _register_embedding_model(registry, model_id="m", dim=8)
    current = {"batch": 32}
    resolver, seen = _resolver_with_batch(registry, lambda: current["batch"])

    resolver.for_model_pk(pk)
    current["batch"] = 8
    resolver.for_model_pk(pk)

    assert seen == [32, 8]


# ---------------------------------------------------------------------- 协议（WeMM）


class _FakeRegistry:
    """只回答一件事：这个 ``model_pk`` 指向哪个供应商与模型。

    下面两条用例问的是"**协议**决定建哪个客户端"，与"注册表里存了什么"无关，
    所以不必真的建库（本机没有测试库时也能跑）。
    """

    def __init__(self, provider: ModelProviderRecord, model: RegisteredModelRecord) -> None:
        self._provider = provider
        self._model = model

    def embedding_target(
        self, model_pk: str
    ) -> tuple[ModelProviderRecord, RegisteredModelRecord]:
        return self._provider, self._model


def _lan_registry() -> _FakeRegistry:
    provider = ModelProviderRecord(
        id="p1",
        kind="embedding",
        name="局域网 WeMM",
        base_url="http://192.168.31.18:8234",
        api_key="",  # 那台没有鉴权
    )
    model = RegisteredModelRecord(
        id="m1",
        provider_id="p1",
        model_id="WeMM-Embedding-2B-Q4_K_M.gguf",
        dim=2048,
        capabilities=["embedding"],
    )
    return _FakeRegistry(provider, model)


def test_protocol_wemm_builds_the_media_capable_client() -> None:
    """协议切到 WeMM 之后**按库解析**这条主路径也要换：否则媒体那条路被绕过去。

    （"按库选模型"是嵌入模型的主路径，设置页那个全局协议如果不在这里生效，
    表现就是"文本换了模型、图片还是没人嵌"。）
    """
    from app.services.embedding.wemm import WeMMEmbedder

    resolver = EmbeddingResolver(
        _lan_registry(),  # type: ignore[arg-type]
        fallback=_FakeEmbedder(model_id="fallback", dim=8),
        protocol=lambda: "wemm",
    )

    embedder = resolver.for_model_pk("m1")

    assert isinstance(embedder, WeMMEmbedder)
    assert embedder.model_id == "WeMM-Embedding-2B-Q4_K_M.gguf"
    assert embedder.dim == 2048
    assert embedder.supports_media is True


def test_switching_protocol_invalidates_the_cached_embedder() -> None:
    """协议进缓存键：切了协议还复用旧客户端，就会"设置改了但媒体那条路没接上"。"""
    from app.services.embedding.openai_compat import OpenAICompatEmbedder
    from app.services.embedding.wemm import WeMMEmbedder

    current = {"protocol": "openai"}
    resolver = EmbeddingResolver(
        _lan_registry(),  # type: ignore[arg-type]
        fallback=_FakeEmbedder(model_id="fallback", dim=8),
        protocol=lambda: current["protocol"],
    )

    assert isinstance(resolver.for_model_pk("m1"), OpenAICompatEmbedder)

    current["protocol"] = "wemm"

    assert isinstance(resolver.for_model_pk("m1"), WeMMEmbedder)


def test_default_protocol_keeps_building_the_openai_client() -> None:
    """没注入协议取值函数时（既有调用点）行为不变：还是 OpenAI 兼容那一个。"""
    from app.services.embedding.openai_compat import OpenAICompatEmbedder

    resolver = EmbeddingResolver(
        _lan_registry(),  # type: ignore[arg-type]
        fallback=_FakeEmbedder(model_id="fallback", dim=8),
    )

    assert isinstance(resolver.for_model_pk("m1"), OpenAICompatEmbedder)


def _registry_with_options(options: dict[str, object]) -> _FakeRegistry:
    """同一个局域网供应商，但模型登记里带了 ``options``（按模型声明协议那一栏）。"""
    provider = ModelProviderRecord(
        id="p1", kind="embedding", name="局域网 WeMM", base_url="http://192.168.31.18:8234"
    )
    model = RegisteredModelRecord(
        id="m1",
        provider_id="p1",
        model_id="WeMM-Embedding-2B-Q4_K_M.gguf",
        dim=1024,
        capabilities=["embedding"],
        options=options,
    )
    return _FakeRegistry(provider, model)


def test_model_declared_protocol_wins_over_the_global_default() -> None:
    """协议按**模型**定：全局默认是 openai，这个模型声明了 wemm 就该用它。

    这是"一个进程里同时接两家不同协议的服务"那条需求的关键一步。
    """
    from app.services.embedding.wemm import WeMMEmbedder

    resolver = EmbeddingResolver(
        _registry_with_options({"protocol": "wemm"}),  # type: ignore[arg-type]
        fallback=_FakeEmbedder(model_id="fallback", dim=8),
        protocol=lambda: "openai",  # 全局默认还是 OpenAI 兼容
    )

    embedder = resolver.for_model_pk("m1")

    assert isinstance(embedder, WeMMEmbedder)
    # 维度是**库记录里冻结的那个**：WeMM 客户端按 1024 做 Matryoshka 截断 + 重归一化
    assert embedder.dim == 1024


def test_model_declared_protocol_falls_back_to_the_setting_when_unknown() -> None:
    """声明了一个不认识的值：退回全局设置，而不是让这个库的向量通道整个不可用。"""
    from app.services.embedding.wemm import WeMMEmbedder

    resolver = EmbeddingResolver(
        _registry_with_options({"protocol": "wemmm"}),  # type: ignore[arg-type]
        fallback=_FakeEmbedder(model_id="fallback", dim=8),
        protocol=lambda: "wemm",
    )

    assert isinstance(resolver.for_model_pk("m1"), WeMMEmbedder)


def test_model_without_the_option_uses_the_setting() -> None:
    """没声明这一栏的模型（绝大多数）走全局设置——这是"默认值"的含义。"""
    from app.services.embedding.openai_compat import OpenAICompatEmbedder

    resolver = EmbeddingResolver(
        _registry_with_options({}),  # type: ignore[arg-type]
        fallback=_FakeEmbedder(model_id="fallback", dim=8),
        protocol=lambda: "openai",
    )

    assert isinstance(resolver.for_model_pk("m1"), OpenAICompatEmbedder)
