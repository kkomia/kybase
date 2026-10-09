"""常见供应商预设（"添加供应商"时一键填好地址与常见模型）。

**解决的是什么**：登记一个供应商，最会填错的是**接口地址**——各家都不一样，
而且同一家的"原生地址"和"OpenAI 兼容地址"常常不是一个（百炼要加
``/compatible-mode/v1``、Gemini 要加 ``/v1beta/openai/``）。让用户去翻文档抄地址，
是纯粹的摩擦。

**只放事实，不放代码**：这里是一条条"名称 + 地址 + 常见模型 ID"的数据。
模型清单是**便利快照**（取自 models.dev 的公开数据，2026-09），会过时——
所以它只是候选建议，**真正的权威来源仍然是上游探测**（``/providers/{id}/available-models``）：
用户点开「添加模型」时既能选这里的建议，也能选探测回来的，还能直接手写。

**只列 OpenAI 兼容端点**：我们的对话客户端只实现 ``/chat/completions``
（见 ``services/llm.py``）。原生协议（Anthropic Messages、Gemini generateContent）
不在这里——列出来用户配了也用不了，比不列更糟。走兼容层的会在 ``hint`` 里说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["PROVIDER_PRESETS", "PresetModel", "ProviderPreset"]


@dataclass(frozen=True, slots=True)
class PresetModel:
    """预设里的一条模型建议。``dim`` 只有向量化模型才有意义。"""

    model_id: str
    label: str = ""
    capabilities: tuple[str, ...] = ("chat",)
    dim: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """一家常见供应商。``base_url`` 一律是 **OpenAI 兼容**地址。"""

    id: str
    label: str
    kind: str
    base_url: str
    hint: str = ""
    models: tuple[PresetModel, ...] = field(default_factory=tuple)


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="deepseek",
        label="深度求索",
        kind="llm",
        base_url="https://api.deepseek.com",
        hint="密钥：platform.deepseek.com",
        models=(
            PresetModel("deepseek-flash", "DeepSeek Flash"),
            PresetModel("deepseek-v4-pro", "DeepSeek V4 Pro"),
            PresetModel("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ),
    ),
    ProviderPreset(
        id="dashscope",
        label="阿里云百炼",
        kind="llm",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        hint="密钥：bailian.console.aliyun.com",
        models=(
            PresetModel("qwen3.8-max", "Qwen3.8 Max"),
            PresetModel("qwen3.8-flash", "Qwen3.8 Flash"),
            PresetModel("qwen3.7-plus", "Qwen3.7 Plus"),
        ),
    ),
    ProviderPreset(
        id="moonshot",
        label="月之暗面",
        kind="llm",
        base_url="https://api.moonshot.cn/v1",
        hint="密钥：platform.moonshot.cn",
        models=(
            PresetModel("kimi-k3", "Kimi K3"),
            PresetModel("kimi-k2.6", "Kimi K2.6"),
        ),
    ),
    ProviderPreset(
        id="zhipu",
        label="智谱 AI",
        kind="llm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        hint="密钥：open.bigmodel.cn",
        models=(
            PresetModel("glm-5.3", "GLM-5.3"),
            PresetModel("glm-5.3-flash", "GLM-5.3 Flash"),
            PresetModel("glm-5.2", "GLM-5.2"),
        ),
    ),
    ProviderPreset(
        id="siliconflow",
        label="硅基流动",
        kind="llm",
        base_url="https://api.siliconflow.cn/v1",
        hint="密钥：cloud.siliconflow.cn",
        models=(
            PresetModel("deepseek-ai/DeepSeek-V4-Flash", "DeepSeek V4 Flash"),
            PresetModel("zai-org/GLM-5.2", "GLM-5.2"),
            PresetModel("Qwen/Qwen3.6-27B", "Qwen3.6 27B"),
            PresetModel("BAAI/bge-m3", "BGE-M3", ("embedding",), 1024),
            PresetModel("BAAI/bge-reranker-v2-m3", "BGE Reranker v2 M3", ("rerank",), None),
        ),
    ),
    ProviderPreset(
        id="openai",
        label="OpenAI",
        kind="llm",
        base_url="https://api.openai.com/v1",
        hint="密钥：platform.openai.com",
        models=(
            PresetModel("gpt-5.6", "GPT-5.6"),
            PresetModel("gpt-5.5", "GPT-5.5"),
            PresetModel("text-embedding-3-small", "Embedding 3 Small", ("embedding",), 1536),
            PresetModel("text-embedding-3-large", "Embedding 3 Large", ("embedding",), 3072),
        ),
    ),
    ProviderPreset(
        id="anthropic",
        label="Anthropic",
        kind="llm",
        base_url="https://api.anthropic.com/v1",
        hint="密钥：console.anthropic.com",
        models=(
            PresetModel("claude-opus-4-8", "Claude Opus 4.8"),
            PresetModel("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ),
    ),
    ProviderPreset(
        id="gemini",
        label="Google Gemini",
        kind="llm",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        hint="密钥：aistudio.google.com",
        models=(
            PresetModel("gemini-3.8-flash", "Gemini 3.8 Flash"),
            PresetModel("gemini-3.7-flash", "Gemini 3.7 Flash"),
        ),
    ),
    ProviderPreset(
        id="xai",
        label="xAI",
        kind="llm",
        base_url="https://api.x.ai/v1",
        hint="密钥：console.x.ai",
        models=(
            PresetModel("grok-4.6", "Grok 4.6"),
            PresetModel("grok-4.5", "Grok 4.5"),
        ),
    ),
    ProviderPreset(
        id="ollama",
        label="Ollama",
        kind="llm",
        base_url="http://localhost:11434/v1",
    ),
    # 「自定义」不在这里：选择器本身就有一项「自定义」，再加一个同义条目只是噪声。
)
