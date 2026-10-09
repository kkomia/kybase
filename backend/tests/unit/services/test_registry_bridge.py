"""注册器与配置层的桥接（G1 / v0.8 归属整理）。

镜像同构：``runtime_config.py`` 的 ``_bound`` 桥接 + 各快照方法 → 本文件。

**v0.8 起注册表是模型身份的唯一来源**：地址 / 密钥 / 模型名 / 维度都只从
绑定里取。没绑定就是没配（``is_configured`` 为假），设置页那些
``embedding.model_id`` / ``llm.api_key`` 之类的键已经不再参与解析——
留着半生效的第二个入口比没有更糟。
"""

from __future__ import annotations

import pytest

from app.services.model_registry import ModelRegistryService
from app.services.runtime_config import RuntimeConfigService


@pytest.fixture
def registry(bundle) -> ModelRegistryService:  # type: ignore[no-untyped-def]
    return ModelRegistryService(bundle)


@pytest.fixture
def runtime(bundle, registry) -> RuntimeConfigService:  # type: ignore[no-untyped-def]
    return RuntimeConfigService(bundle, None, registry=registry)


def _bind_chat(registry: ModelRegistryService, *, model_id="bound-chat-model", **provider):  # type: ignore[no-untyped-def]
    base = {"kind": "llm", "name": "注册表供应商", "base_url": "https://registry.example.com"}
    base.update(provider)
    owner = registry.create_provider(**base)
    model = registry.register_model(
        provider_id=owner.id, model_id=model_id, capabilities=["chat"]
    )
    registry.bind("chat", model.id)
    return owner, model


# --------------------------------------------------------------------- 未绑定


def test_without_a_registry_everything_is_unconfigured(bundle) -> None:  # type: ignore[no-untyped-def]
    """注册器缺席时拿不到任何模型——**不再退回设置页**。

    ``llm()`` 仍然可调用（要能安全地问"配好了没"），但快照是空的。
    """
    runtime = RuntimeConfigService(bundle, None)  # 不传 registry
    bundle.meta.set_setting("llm.model_id", "from-settings")
    bundle.meta.set_setting("llm.api_key", "sk-settings")

    snapshot = runtime.llm()

    assert snapshot.is_configured is False
    assert snapshot.model_id == ""


def test_unbound_slot_is_unconfigured(bundle, runtime) -> None:  # type: ignore[no-untyped-def]
    """**没绑用途就是没配**：设置页里写过的旧键不再参与解析。

    这是 v0.8 归属整理的核心——"注册了哪个模型"只有一个入口，
    否则两处各写一遍必然漂。
    """
    bundle.meta.set_setting("llm.model_id", "from-settings")
    bundle.meta.set_setting("llm.api_key", "sk-settings")

    snapshot = runtime.llm()

    assert snapshot.is_configured is False
    assert runtime.embedding().is_configured is False
    assert runtime.rerank().is_configured is False


# --------------------------------------------------------------------- 绑定


def test_bound_slot_supplies_the_identity(runtime, registry) -> None:  # type: ignore[no-untyped-def]
    _bind_chat(registry, api_key="sk-registry")

    snapshot = runtime.llm()

    assert snapshot.is_configured is True
    assert snapshot.model_id == "bound-chat-model"
    assert snapshot.api_key == "sk-registry"
    assert snapshot.base_url == "https://registry.example.com"


def test_embedding_binding_drives_model_and_dim(bundle, runtime, registry) -> None:  # type: ignore[no-untyped-def]
    """**dim 以注册表登记的为准**：它是模型属性，登记一次就不该让用户在两处各填一遍、
    然后两边不一致（维度不一致会让向量空间对不上，检索结果会静默变差）。"""
    bundle.meta.set_setting("embedding.dim", "768")
    owner = registry.create_provider(
        kind="embedding", name="向量家", base_url="https://emb.example.com", api_key="sk-emb"
    )
    model = registry.register_model(
        provider_id=owner.id, model_id="bge-m3", dim=1024, capabilities=["embedding"]
    )
    registry.bind("embedding", model.id)

    snapshot = runtime.embedding()

    assert snapshot.model_id == "bge-m3"
    assert snapshot.dim == 1024, "绑定了模型却仍用设置页的 768，两边就漂了"
    assert snapshot.api_key == "sk-emb"


def test_embedding_without_a_registered_dim_is_unconfigured(
    runtime, registry
) -> None:  # type: ignore[no-untyped-def]
    """注册表没登记 dim 就是配不好——**不从设置页补一个**。

    维度决定向量空间大小，猜错了检索会静默变差；宁可让用户去补登记。
    """
    owner = registry.create_provider(kind="embedding", name="向量家", api_key="sk-emb")
    model = registry.register_model(
        provider_id=owner.id, model_id="no-dim-model", capabilities=["embedding"]
    )
    registry.bind("embedding", model.id)

    snapshot = runtime.embedding()

    assert snapshot.dim == 0
    assert snapshot.is_configured is False


def test_rerank_binding_supplies_the_identity(runtime, registry) -> None:  # type: ignore[no-untyped-def]
    owner = registry.create_provider(
        kind="rerank", name="重排家", base_url="https://rerank.example.com", api_key="sk-rr"
    )
    model = registry.register_model(
        provider_id=owner.id, model_id="bge-reranker", capabilities=["rerank"]
    )
    registry.bind("rerank", model.id)

    snapshot = runtime.rerank()

    assert snapshot.model_id == "bge-reranker"
    assert snapshot.api_key == "sk-rr"


# --------------------------------------------------------------------- 采样参数


def test_sampling_parameters_stay_on_the_settings_page(bundle, runtime, registry) -> None:  # type: ignore[no-untyped-def]
    """**采样参数始终来自设置页**，不随模型走。

    它们是"这次怎么问"而不是"用哪家模型"——换个模型通常不想重新调一遍
    temperature。模型的身份（base_url / key / model_id）才由注册表决定。

    回复长度不在其中：**它已经不是设置项了**，默认压根不发这个字段
    （见 ``services/llm.py`` 的 ``LLMConfig.max_tokens``）。
    """
    bundle.meta.set_setting("llm.temperature", "0.7")
    _bind_chat(registry)

    snapshot = runtime.llm()

    assert snapshot.temperature == pytest.approx(0.7)
    assert snapshot.max_tokens is None


def test_model_options_can_override_sampling(bundle, runtime, registry) -> None:  # type: ignore[no-untyped-def]
    """但模型自带默认值可以覆盖：不同模型对采样参数的最优区间不同，
    推理模型通常要更低的 temperature。"""
    bundle.meta.set_setting("llm.temperature", "0.7")
    owner = registry.create_provider(kind="llm", name="推理家", base_url="https://r.example.com")
    model = registry.register_model(
        provider_id=owner.id,
        model_id="reasoner",
        capabilities=["chat"],
        options={"temperature": 0.1, "max_tokens": 4096, "enable_thinking": True},
    )
    registry.bind("chat", model.id)

    snapshot = runtime.llm()

    assert snapshot.temperature == pytest.approx(0.1)
    assert snapshot.max_tokens == 4096
    assert snapshot.enable_thinking is True


def test_unparseable_option_does_not_break_the_chat(bundle, runtime, registry) -> None:  # type: ignore[no-untyped-def]
    """登记时把 max_tokens 填成非数字是人之常情，不该让整次对话失败。

    解析不出来就退回"不发这个字段"（``None``），而不是让 ``int("一千")`` 抛出去。
    """
    owner = registry.create_provider(kind="llm", name="手滑家", base_url="https://x.example.com")
    model = registry.register_model(
        provider_id=owner.id,
        model_id="typo-model",
        capabilities=["chat"],
        options={"max_tokens": "一千"},
    )
    registry.bind("chat", model.id)

    assert runtime.llm().max_tokens is None


# --------------------------------------------------------------------- 换模型


def test_switching_models_does_not_lose_the_previous_credentials(
    bundle, runtime, registry
) -> None:  # type: ignore[no-untyped-def]
    """**这正是做注册器的动机之一**：想临时切到另一家对比效果，
    回来时不必重新填一遍 key。两家的凭据同时留在库里。"""
    first, first_model = _bind_chat(registry, name="甲家", api_key="sk-first", model_id="model-a")
    second, second_model = _bind_chat(
        registry, name="乙家", api_key="sk-second", model_id="model-b"
    )

    # 切到乙家
    registry.bind("chat", second_model.id)
    assert runtime.llm().api_key == "sk-second"

    # 切回甲家：凭据还在，不用重填
    registry.bind("chat", first_model.id)
    assert runtime.llm().api_key == "sk-first"
    assert registry.get_provider(first.id).api_key == "sk-first"
    assert registry.get_provider(second.id).api_key == "sk-second"
