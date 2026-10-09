"""嵌入协议：名字、默认值、以及"哪个协议建哪个实现"。

镜像同构：``app/services/embedding/protocols.py`` → 本文件（工厂那一半也在这里）。

**不依赖夹具**：``build_embedder`` 只向运行期配置要一份 ``EmbeddingSettings`` 快照，
所以这里用一个最小替身回答那一件事——本机没有测试库时也能真跑（有库时 pytest 也会跑）。
"""

from __future__ import annotations

import pathlib

from app.services.embedding import build_embedder
from app.services.embedding.openai_compat import OpenAICompatEmbedder
from app.services.embedding.protocols import (
    DEFAULT_PROTOCOL,
    OPENAI_PROTOCOL,
    PROTOCOL_OPTIONS,
    WEMM_PROTOCOL,
    implementation_for,
    normalize_protocol,
    supports_media,
)
from app.services.embedding.wemm import WeMMEmbedder
from app.services.runtime_config import EmbeddingSettings

_BASE = {
    "base_url": "http://192.168.31.18:8234",
    "api_key": "",
    "model_id": "WeMM-Embedding-2B-Q4_K_M.gguf",
    "dim": 2048,
    "batch_size": 32,
}


class _Runtime:
    """只回答一件事：当前的向量化配置快照（`build_embedder` 要的全部）。"""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings

    def embedding(self) -> EmbeddingSettings:
        return self._settings


def _settings(**overrides: object) -> EmbeddingSettings:
    params = dict(_BASE)
    params.update(overrides)
    return EmbeddingSettings(**params)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- 协议表


def test_unknown_protocol_falls_back_to_the_default() -> None:
    """写错一个字母不该让整条向量通道不可用；退回默认（设置页里看得见生效值）。"""
    assert normalize_protocol(None) == OPENAI_PROTOCOL
    assert normalize_protocol("") == OPENAI_PROTOCOL
    assert normalize_protocol("WeMM") == WEMM_PROTOCOL
    assert normalize_protocol(" wemm ") == WEMM_PROTOCOL
    assert normalize_protocol("ollama") == OPENAI_PROTOCOL


def test_default_protocol_keeps_the_existing_behaviour() -> None:
    """默认必须是 OpenAI 兼容：没动过这一位时既有部署一位不变。"""
    assert DEFAULT_PROTOCOL == OPENAI_PROTOCOL
    assert implementation_for(DEFAULT_PROTOCOL) is OpenAICompatEmbedder
    assert implementation_for(WEMM_PROTOCOL) is WeMMEmbedder


def test_only_wemm_advertises_media() -> None:
    assert supports_media(WEMM_PROTOCOL) is True
    assert supports_media(OPENAI_PROTOCOL) is False
    assert supports_media("不认识的协议") is False  # 退回默认 → 没有媒体


def test_setting_options_match_the_implementation_table() -> None:
    """设置页的候选项与实现表**同一份事实**：少一项就是"设置了但不生效"。"""
    values = [value for value, _label in PROTOCOL_OPTIONS]
    assert values == [OPENAI_PROTOCOL, WEMM_PROTOCOL]
    assert DEFAULT_PROTOCOL in values
    for value in values:
        assert implementation_for(value) is not None


# ---------------------------------------------------------------------- 工厂


def test_factory_builds_wemm_when_the_protocol_says_so() -> None:
    embedder = build_embedder(_Runtime(_settings(protocol=WEMM_PROTOCOL)))  # type: ignore[arg-type]

    assert isinstance(embedder, WeMMEmbedder)
    assert embedder.model_id == "WeMM-Embedding-2B-Q4_K_M.gguf"
    assert embedder.dim == 2048
    assert embedder.max_batch == 32
    assert embedder.supports_media is True


def test_factory_still_builds_openai_compat_by_default() -> None:
    """没有协议这一位时（老配置）走的还是从前的实现。"""
    embedder = build_embedder(
        _Runtime(_settings(api_key="sk-x", model_id="BAAI/bge-m3", dim=1024))  # type: ignore[arg-type]
    )

    assert isinstance(embedder, OpenAICompatEmbedder)
    assert embedder.dim == 1024
    assert embedder.supports_media is False


def test_wemm_config_needs_no_api_key() -> None:
    """局域网那台没有鉴权：要求填 key 等于逼用户编一个假值。"""
    assert _settings(protocol=WEMM_PROTOCOL).is_configured is True
    # 但没有地址就不行（无从调用）
    assert _settings(protocol=WEMM_PROTOCOL, base_url="").is_configured is False
    # OpenAI 兼容那一档仍然要求 key（没 key 基本就是没配好，401 会一直失败）
    assert _settings(api_key="").is_configured is False
    assert _settings(api_key="sk-x").is_configured is True
    # 维度是模型属性，任何协议下都不能缺
    assert _settings(protocol=WEMM_PROTOCOL, dim=0).is_configured is False


def test_matryoshka_target_dimension_flows_into_the_client() -> None:
    """想在 1024 维的库里用它，就在模型登记里填 1024：客户端按目标维度截断 + 重归一化。"""
    embedder = build_embedder(
        _Runtime(_settings(protocol=WEMM_PROTOCOL, dim=1024))  # type: ignore[arg-type]
    )

    assert embedder.dim == 1024


def test_lan_address_is_not_hardcoded_in_the_source() -> None:
    """那台机器的地址走配置（模型注册里的供应商 base_url），**不进源码**。

    文档与 `.env.example` 里写着 `192.168.31.18:8234` 当示例；这几个文件里出现它就说明
    有人把一处部署事实焊死了——换个网段就得改代码。**只查这条 lane 碰的文件**：
    仓库别处早有文档性质的示例地址（那是另一件事，不该被这条用例拦）。
    """
    app = pathlib.Path(__file__).resolve().parents[4] / "app"
    watched = [
        app / "core" / "config.py",
        app / "parsers" / "media_direct.py",
        *sorted((app / "services" / "embedding").glob("*.py")),
    ]
    offenders = [
        path.relative_to(app).as_posix()
        for path in watched
        if "192.168." in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_unrecognized_protocol_value_still_builds_the_default_client() -> None:
    """设置里存了个不认识的值（手写、老版本遗留）：归一化成默认，**不是**悄悄建成 WeMM。"""
    runtime = _Runtime(_settings(protocol="wemmm", api_key="sk-x"))  # type: ignore[arg-type]

    embedder = build_embedder(runtime)

    assert isinstance(embedder, OpenAICompatEmbedder)
    assert not isinstance(embedder, WeMMEmbedder)
