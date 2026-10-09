"""会话级对话模型：按 pk 解析与透传（v12）。

镜像同构：``app/services/runtime_config.py`` 的 ``llm_for`` + ``app/services/chat.py``
的 ``model_pk`` 透传 + ``app/services/model_registry.py`` 的 ``chat_target``。

**为什么单独一个文件**：这三处是同一条链（注册表校验 → 运行期配置 → 对话服务），
拆到各自文件里测反而看不出"换了个模型之后，真正用的地址与模型名变了没有"。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.services.chat import ChatService
from app.services.model_registry import ModelRegistryService
from tests.conftest import bind_model


class _FakeChat:
    """假的对话客户端：记录收到的 messages，返回固定回答。"""

    def complete(self, messages):  # type: ignore[no-untyped-def]
        return "回答"


def _provider_with_model(registry: ModelRegistryService, **model_kwargs):  # type: ignore[no-untyped-def]
    provider = registry.create_provider(
        kind="llm",
        name=str(model_kwargs.pop("provider_name", "另一家")),
        base_url=str(model_kwargs.pop("base_url", "https://b.example.com/v1")),
        api_key=str(model_kwargs.pop("api_key", "sk-b")),
        enabled=bool(model_kwargs.pop("enabled", True)),
    )
    return registry.register_model(provider_id=provider.id, **model_kwargs)


# --------------------------------------------------------------------- llm_for


def test_llm_for_empty_falls_back_to_the_global_default(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    """``None`` / 空串都等价于 ``llm()``——"没选"不该报错，走全局默认。"""
    bind_model(ModelRegistryService(bundle), "chat", model_id="m-default", capabilities=["chat"])

    assert runtime.llm_for(None).model_id == "m-default"
    assert runtime.llm_for("").model_id == "m-default"


def test_llm_for_uses_the_named_model_identity(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    """指定模型后，地址 / 密钥 / 模型名都要换成它自己的——这是这个功能存在的理由。"""
    registry = ModelRegistryService(bundle)
    model = _provider_with_model(
        registry,
        model_id="m-other",
        capabilities=["chat"],
        base_url="https://b.example.com/v1",
        api_key="sk-b",
    )

    config = runtime.llm_for(model.id)

    assert (config.base_url, config.api_key, config.model_id) == (
        "https://b.example.com/v1",
        "sk-b",
        "m-other",
    )
    assert config.is_configured


def test_llm_for_model_options_override_sampling(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    """模型自带的采样参数优先于设置页（不同模型的最优区间不同）。"""
    registry = ModelRegistryService(bundle)
    model = _provider_with_model(
        registry, model_id="m-tuned", capabilities=["chat"], options={"temperature": 0.05}
    )

    assert runtime.llm_for(model.id).temperature == pytest.approx(0.05)


def test_llm_for_unknown_model_is_rejected(runtime) -> None:  # type: ignore[no-untyped-def]
    """不存在的 pk 走注册表原本的 404「模型不存在」，不另造一种错误码。"""
    with pytest.raises(NotFoundError, match="不存在"):
        runtime.llm_for("mdl_does_not_exist")


def test_llm_for_model_without_chat_capability_is_rejected(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    registry = ModelRegistryService(bundle)
    model = _provider_with_model(registry, model_id="m-emb", capabilities=["embedding"])

    with pytest.raises(InvalidRequestError, match="对话能力"):
        runtime.llm_for(model.id)


def test_llm_for_disabled_provider_is_rejected(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    registry = ModelRegistryService(bundle)
    model = _provider_with_model(
        registry, model_id="m-off", capabilities=["chat"], enabled=False
    )

    with pytest.raises(InvalidRequestError, match="停用"):
        runtime.llm_for(model.id)


def test_llm_for_provider_without_key_is_rejected(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    registry = ModelRegistryService(bundle)
    model = _provider_with_model(
        registry, model_id="m-nokey", capabilities=["chat"], api_key=""
    )

    with pytest.raises(InvalidRequestError, match="API Key"):
        runtime.llm_for(model.id)


# --------------------------------------------------------------------- 透传


def test_chat_answer_uses_the_selected_model(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    """对话服务要真的把选中的模型交给客户端工厂，而不是继续用全局默认。"""
    registry = ModelRegistryService(bundle)
    model = _provider_with_model(registry, model_id="m-chosen", capabilities=["chat"], base_url="https://chosen/v1")
    seen: dict[str, str] = {}

    def factory(config):  # type: ignore[no-untyped-def]
        seen["model_id"] = config.model_id
        seen["base_url"] = config.base_url
        return _FakeChat()

    service = ChatService(object(), runtime, chat_factory=factory)  # type: ignore[arg-type]
    service.answer(query="问一句", sources=[], model_pk=model.id)

    assert seen == {"model_id": "m-chosen", "base_url": "https://chosen/v1"}


def test_chat_answer_without_model_pk_uses_the_global_default(runtime, bundle) -> None:  # type: ignore[no-untyped-def]
    bind_model(
        ModelRegistryService(bundle),
        "chat",
        model_id="m-default",
        capabilities=["chat"],
        base_url="https://default/v1",
    )
    seen: dict[str, str] = {}

    def factory(config):  # type: ignore[no-untyped-def]
        seen["model_id"] = config.model_id
        return _FakeChat()

    service = ChatService(object(), runtime, chat_factory=factory)  # type: ignore[arg-type]
    service.answer(query="问一句", sources=[])

    assert seen["model_id"] == "m-default"
