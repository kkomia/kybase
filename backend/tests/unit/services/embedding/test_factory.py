"""embedding 工厂的单元测试。

镜像同构：``app/services/embedding/__init__.py``
→ ``tests/unit/services/embedding/test_factory.py``。

v0.8 起这条分支只有三种结果，且**没有隐式兜底**：
绑定了注册模型 → 真实端点；显式开开发开关 → 无语义哈希；两者都没有 → 抛错。
选错了会让检索质量静默失真，所以三条都要钉住。
"""

from __future__ import annotations

import logging

import pytest

from app.services.embedding import (
    NOT_CONFIGURED_HINT,
    EmbeddingNotConfiguredError,
    build_embedder,
)
from app.services.embedding.deterministic import DeterministicEmbedder
from app.services.embedding.openai_compat import OpenAICompatEmbedder
from app.services.model_registry import ModelRegistryService
from app.services.runtime_config import RuntimeConfigService


def _bound_runtime(bundle, *, api_key: str = "sk-test", dim: int | None = 1024):
    """造一个"注册表里绑定了向量化模型"的运行期配置。"""
    registry = ModelRegistryService(bundle)
    provider = registry.create_provider(
        kind="embedding",
        name="某供应商",
        base_url="https://api.example.com/v1",
        api_key=api_key,
    )
    model = registry.register_model(
        provider_id=provider.id,
        model_id="BAAI/bge-m3",
        dim=dim,
        capabilities=["embedding"],
    )
    registry.bind("embedding", model.id)
    return RuntimeConfigService(bundle, registry=registry), registry, model


def test_unconfigured_raises_instead_of_falling_back(runtime) -> None:
    """没绑模型就是没配：**不许**退回无语义的哈希实现。"""
    with pytest.raises(EmbeddingNotConfiguredError) as excinfo:
        build_embedder(runtime)

    assert NOT_CONFIGURED_HINT in str(excinfo.value)
    # 报错文案必须给出下一步动作，而不是一句"配置错误"
    assert "模型注册" in str(excinfo.value)


def test_dev_flag_uses_deterministic_explicitly(runtime, caplog) -> None:
    """显式打开开发开关才用哈希实现，并且必须留下告警。"""
    with caplog.at_level(logging.WARNING, logger="app.services.embedding"):
        embedder = build_embedder(runtime, dev_embedding=True)

    assert isinstance(embedder, DeterministicEmbedder)
    assert embedder.is_development is True
    assert any("KYLAB_DEV_EMBEDDING" in record.getMessage() for record in caplog.records)


def test_uses_openai_compat_when_bound(bundle) -> None:
    runtime, _, _ = _bound_runtime(bundle)
    runtime.set({"embedding.batch_size": "16"})

    embedder = build_embedder(runtime)

    assert isinstance(embedder, OpenAICompatEmbedder)
    assert embedder.model_id == "BAAI/bge-m3"
    assert embedder.dim == 1024
    assert embedder.max_batch == 16
    assert embedder.is_development is False


def test_dev_flag_does_not_override_a_real_model(bundle) -> None:
    """配好了就用真模型：开发开关只是"没配时的退路"，不该把真实配置顶掉。"""
    runtime, _, _ = _bound_runtime(bundle)

    embedder = build_embedder(runtime, dev_embedding=True)

    assert isinstance(embedder, OpenAICompatEmbedder)


def test_bound_provider_without_key_still_counts_as_unconfigured(bundle) -> None:
    """绑了一个没填 Key 的供应商不算配好——否则首次摄入才会炸。"""
    runtime, _, _ = _bound_runtime(bundle, api_key="")

    assert runtime.embedding().is_configured is False
    with pytest.raises(EmbeddingNotConfiguredError):
        build_embedder(runtime)


def test_bound_model_without_dim_still_counts_as_unconfigured(bundle) -> None:
    """维度是模型属性：没登记维度就不知道向量空间有多大，不能建库。"""
    runtime, _, _ = _bound_runtime(bundle, dim=None)

    assert runtime.embedding().is_configured is False
    with pytest.raises(EmbeddingNotConfiguredError):
        build_embedder(runtime)


def test_masked_secret_is_never_written_back(runtime) -> None:
    """界面上显示的是 ``sk-…est`` 这种掩码，把它当新值回写就等于把密钥改坏。

    模型凭据已归注册表（那里的掩码纪律另有测试），这里钉住剩下的设置页密钥。
    """
    runtime.set({"mineru.token": "sk-test-0123456789"})
    group = next(item for item in runtime.describe()["groups"] if item["key"] == "mineru")
    masked = group["fields"][0]["value"]
    assert "…" in masked

    runtime.set({"mineru.token": masked})

    assert runtime.get("mineru.token") == "sk-test-0123456789"


def test_env_only_bootstraps_until_user_overrides(runtime, bundle) -> None:
    """.env 只是引导值：网页上写过一次之后就以数据库为准。

    模型身份不在 .env 里（v0.8），所以这里用行为参数举例。
    """
    from app.core.config import Settings
    from app.services.runtime_config import RuntimeConfigService

    boot = RuntimeConfigService(bundle, Settings(_env_file=None, llm_temperature=0.9))
    assert boot.get("llm.temperature") == "0.9"

    boot.set({"llm.temperature": "0.2"})
    assert boot.get("llm.temperature") == "0.2"
