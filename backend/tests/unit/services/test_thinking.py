"""思考模式方言适配（``app/services/thinking.py``）。

**为什么值得单测**：这一层的产物是"发给供应商的字段名"，而字段名错了**不会报错**——
DeepSeek 会安静地忽略 ``enable_thinking``（实测：关掉后 reasoning_content 照样有 86 字），
表现成"开关看上去有效、其实没生效"。所以每个方言发什么字段，必须钉死在测试里。
"""

from __future__ import annotations

import pytest

from app.services.thinking import (
    build_thinking_payload,
    detect_dialect,
    normalize_effort,
)

# --------------------------------------------------------------------- 方言识别


@pytest.mark.parametrize(
    ("base_url", "model_id", "expected"),
    [
        ("https://api.deepseek.com", "deepseek-flash", "deepseek"),
        ("https://api.deepseek.com/v1", "deepseek-chat", "deepseek"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-max", "qwen"),
        ("https://api.openai.com/v1", "gpt-5", "openai"),
        ("https://open.bigmodel.cn/api/paas/v4", "glm-4.6", "glm"),
        ("https://api.moonshot.cn/v1", "kimi-k2.6", "kimi"),
        ("https://api.anthropic.com/v1", "claude-sonnet-4-5", "anthropic"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-pro", "gemini"),
        # 地址看不出（聚合站/自建网关）时靠模型名认领
        ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3.2", "deepseek"),
        ("https://gateway.example.com/v1", "Qwen/Qwen3-32B", "qwen"),
        ("https://gateway.example.com/v1", "totally-unknown", "generic"),
    ],
)
def test_detect_dialect(base_url: str, model_id: str, expected: str) -> None:
    assert detect_dialect(base_url, model_id) == expected


def test_explicit_override_wins() -> None:
    """识别猜错时，注册模型里写一个 dialect 就能把它掰回来。"""
    assert detect_dialect("https://api.deepseek.com", "deepseek-flash", "qwen") == "qwen"
    # 不认识的覆盖值忽略，退回自动识别，而不是变成一个发不出去的怪值
    assert detect_dialect("https://api.deepseek.com", "deepseek-flash", "no-such") == "deepseek"


# --------------------------------------------------------------------- 载荷


def test_deepseek_uses_thinking_type_and_effort() -> None:
    """DeepSeek 只认 ``thinking.type``；``enable_thinking`` 它接受但忽略（实测）。"""
    payload = build_thinking_payload(
        base_url="https://api.deepseek.com",
        model_id="deepseek-flash",
        enabled=True,
        effort="high",
    )
    assert payload == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert "enable_thinking" not in payload


def test_deepseek_disable_is_a_real_disable() -> None:
    payload = build_thinking_payload(
        base_url="https://api.deepseek.com", model_id="deepseek-flash", enabled=False
    )
    assert payload == {"thinking": {"type": "disabled"}}
    # 关着的时候不该再发强度，免得两家语义打架
    assert "reasoning_effort" not in payload


def test_qwen_uses_enable_thinking_and_budget() -> None:
    payload = build_thinking_payload(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_id="qwen3-max",
        enabled=True,
        effort="low",
    )
    assert payload == {"enable_thinking": True, "thinking_budget": 1024}


def test_qwen_budget_scales_with_effort() -> None:
    def budget(effort: str) -> int:
        return build_thinking_payload(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_id="qwen3-max",
            enabled=True,
            effort=effort,
        )["thinking_budget"]

    assert budget("low") < budget("medium") < budget("high")


def test_glm_and_kimi_only_get_the_switch() -> None:
    for url, model in (
        ("https://open.bigmodel.cn/api/paas/v4", "glm-4.6"),
        ("https://api.moonshot.cn/v1", "kimi-k2.6"),
    ):
        assert build_thinking_payload(
            base_url=url, model_id=model, enabled=True
        ) == {"thinking": {"type": "enabled"}}
        assert build_thinking_payload(
            base_url=url, model_id=model, enabled=False
        ) == {"thinking": {"type": "disabled"}}


def test_kimi_k3_maps_effort_into_its_own_scale() -> None:
    """k3 只认 low|high|max，我们的 medium 要映射成 high，否则是非法值。"""
    payload = build_thinking_payload(
        base_url="https://api.moonshot.cn/v1",
        model_id="kimi-k3",
        enabled=True,
        effort="medium",
    )
    assert payload["reasoning_effort"] == "high"


def test_openai_has_no_true_off_so_we_send_nothing() -> None:
    assert build_thinking_payload(
        base_url="https://api.openai.com/v1", model_id="gpt-5", enabled=True, effort="medium"
    ) == {"reasoning_effort": "medium"}
    assert (
        build_thinking_payload(
            base_url="https://api.openai.com/v1", model_id="gpt-5", enabled=False
        )
        == {}
    )


def test_anthropic_budget_stays_below_max_tokens() -> None:
    payload = build_thinking_payload(
        base_url="https://api.anthropic.com/v1",
        model_id="claude-sonnet-4-5",
        enabled=True,
        effort="high",
        max_tokens=2048,
    )
    assert payload["thinking"]["type"] == "enabled"
    # budget_tokens 必须小于 max_tokens，否则 Anthropic 直接报错
    assert payload["thinking"]["budget_tokens"] < 2048
    # 关闭即不传（Anthropic 没有 disabled 取值）
    assert (
        build_thinking_payload(
            base_url="https://api.anthropic.com/v1", model_id="claude-sonnet-4-5", enabled=False
        )
        == {}
    )


def test_generic_falls_back_to_the_old_enable_thinking() -> None:
    """未知供应商退回旧行为：只发 ``enable_thinking``，绝不发它可能不认识的字段。"""
    assert build_thinking_payload(
        base_url="https://unknown.example.com/v1", model_id="mystery", enabled=True
    ) == {"enable_thinking": True}


# --------------------------------------------------------------------- 强度归一


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("low", "low"), ("HIGH", "high"), (" medium ", "medium"), ("max", "medium"), (None, "medium")],
)
def test_normalize_effort(raw: object, expected: str) -> None:
    assert normalize_effort(raw) == expected
