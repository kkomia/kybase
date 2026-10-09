"""常见供应商预设（``app/services/provider_presets.py``）。

**为什么值得测**：这是一份会被人手改的数据，改错的后果是用户照着填完之后
探活失败、甚至到建库/对话时才报错——那时已经离"填表单"很远，很难往回追。
所以把"结构上必须成立"的几条钉死，让错误在 CI 就暴露。
"""

from __future__ import annotations

from app.services.model_registry import CAPABILITIES, PROVIDER_KINDS
from app.services.provider_presets import PROVIDER_PRESETS


def test_preset_ids_are_unique() -> None:
    ids = [preset.id for preset in PROVIDER_PRESETS]
    assert len(ids) == len(set(ids))


def test_kinds_and_capabilities_are_known_values() -> None:
    for preset in PROVIDER_PRESETS:
        assert preset.kind in PROVIDER_KINDS, preset.id
        for model in preset.models:
            assert model.capabilities, f"{preset.id}/{model.model_id} 没声明能力"
            for capability in model.capabilities:
                assert capability in CAPABILITIES, f"{preset.id}/{model.model_id}: {capability}"


def test_every_preset_has_a_url() -> None:
    """地址必须填好——预设的全部价值就是不用用户去抄地址（"自定义"由选择器承担）。"""
    for preset in PROVIDER_PRESETS:
        assert preset.base_url.startswith("http"), preset.id


def test_targeted_presets_carry_expected_base_urls() -> None:
    """几个最容易抄错的兼容地址，钉死一个版本，改了就会被看到。"""
    by_id = {preset.id: preset for preset in PROVIDER_PRESETS}
    assert by_id["deepseek"].base_url == "https://api.deepseek.com"
    assert by_id["dashscope"].base_url.endswith("/compatible-mode/v1")
    assert by_id["gemini"].base_url.endswith("/v1beta/openai/")


def test_embedding_presets_declare_a_dim() -> None:
    """向量化模型没有维度就没法建库，预设里既然给了就得给全。"""
    for preset in PROVIDER_PRESETS:
        for model in preset.models:
            if "embedding" in model.capabilities:
                assert model.dim is not None, f"{preset.id}/{model.model_id} 缺 dim"
