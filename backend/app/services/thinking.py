"""思考模式（reasoning / thinking）的供应商方言适配。

**为什么需要这一层**：主流对话模型现在都支持"开/关思考"，多数还支持"思考强度"，
但**参数名与取值没有任何统一**——同一个 OpenAI 兼容协议下，各家自己加字段：

===========================  ==========================================================
方言                          请求字段
===========================  ==========================================================
DeepSeek                     ``thinking: {type: enabled|disabled}`` + ``reasoning_effort``
Qwen / 百炼（DashScope）      ``enable_thinking: bool`` + ``thinking_budget: int``
OpenAI（GPT-5 / o 系）        ``reasoning_effort: minimal|low|medium|high``
智谱 GLM                     ``thinking: {type: enabled|disabled}``
月之暗面 Kimi                ``thinking: {type: enabled|disabled}``（k3 另有 ``reasoning_effort``）
Google Gemini                ``thinking_config.thinking_budget`` / Gemini 3 的 ``thinking_level``
Anthropic Claude             ``thinking: {type: enabled, budget_tokens: int}``
===========================  ==========================================================

**实测教训（本模块存在的直接原因）**：DeepSeek 接受 ``enable_thinking`` 却**完全忽略它**——
传 ``false`` 时 ``reasoning_content`` 照样有 86 字。也就是说旧版那个"深度思考"开关
对 DeepSeek 根本不生效；真正能关掉的是 ``thinking: {type: disabled}``。
所以"统一发一个字段"这条路走不通，必须按方言发对应的字段。

**识别策略**：显式覆盖（模型 ``options.thinking_dialect``）> 主机名 > 模型名特征 > 兜底。
兜底只发 ``enable_thinking``（旧行为），**不给不认识它的端点发新字段**——宁可这个
开关对未知供应商无效，也不要因为多发一个字段把请求打成 400。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_EFFORT",
    "DIALECTS",
    "ECHO_DIALECTS",
    "EFFORTS",
    "build_thinking_payload",
    "detect_dialect",
    "echoes_reasoning",
    "normalize_effort",
]

#: 归一化后的三档强度。各家取值不同，界面只暴露这三档。
EFFORTS: tuple[str, ...] = ("low", "medium", "high")
DEFAULT_EFFORT = "medium"

#: 知道的方言。``generic`` 是兜底（只发 ``enable_thinking``）。
DIALECTS: tuple[str, ...] = (
    "generic",
    "qwen",
    "deepseek",
    "openai",
    "glm",
    "kimi",
    "gemini",
    "anthropic",
)

#: 强度 → 思考预算 token 数。给用 ``thinking_budget`` 的方言（Qwen / Gemini / Anthropic）。
#: 数值取整千档：够拉开差距，又不至于让一次"低强度"也烧掉几万 token。
_EFFORT_BUDGET: dict[str, int] = {"low": 1024, "medium": 4096, "high": 16384}

#: Anthropic 要求 ``budget_tokens`` 小于 ``max_tokens``，留一点给正文。
_ANTHROPIC_MIN_ANSWER_TOKENS = 1024


def normalize_effort(value: object, default: str = DEFAULT_EFFORT) -> str:
    """把任意来源的强度值收进三档；不认识就退回默认值。"""
    text = str(value or "").strip().lower()
    return text if text in EFFORTS else default


#: 哪些方言**要求把上一轮的推理原样回传**（不带就整条请求 400）。
#:
#: **实测（2026-09-19，api.deepseek.com / deepseek-flash）**：思考模式下，凡是带
#: ``tool_calls`` 的助手消息，只要缺 ``reasoning_content`` 这个**字段**就 400
#: （``The `reasoning_content` in the thinking mode must be passed back to the API.``）；
#: 补一个空串就 200（同一条请求原样重放，只加这一个字段，两次都能复现）。
#: 也就是说它要的不是内容，是"字段在"——**每一个**带工具调用的助手消息都要有，
#: 只补前几条、漏掉最后一条同样 400（实测）。
#:
#: 代价对比很悬殊：多发一个字段的成本是零，不回传的成本是整轮对话直接失败
#: （工具已经调完了，用户只看到一句 400）。所以宁可宽一点——但**只限有证据的方言**：
#: 未知供应商多发字段可能被打成 400（见模块头那条"宁可开关无效，也不要请求非法"）。
ECHO_DIALECTS = frozenset({"deepseek"})


def echoes_reasoning(base_url: str, model_id: str, dialect: str | None = None) -> bool:
    """这个端点要不要把助手消息里的推理回传给下一轮请求（见 ``ECHO_DIALECTS``）。"""
    return detect_dialect(base_url, model_id, dialect) in ECHO_DIALECTS


def detect_dialect(base_url: str, model_id: str, override: str | None = None) -> str:
    """猜这家端点说哪种"思考方言"。

    ``override``（模型 ``options.thinking_dialect``）优先——识别猜错时，
    用户可以在注册模型时写一个明确值把它掰回来，不必等我们改代码。
    """
    if override:
        cleaned = str(override).strip().lower()
        if cleaned in DIALECTS:
            return cleaned

    host = (base_url or "").lower()
    name = (model_id or "").lower()

    # 主机名是最可靠的线索：同一个网关通常只代理一家的模型。
    # **顺序有讲究**：Gemini 的 OpenAI 兼容地址是 ``.../v1beta/openai``，
    # 含 "openai" 子串——把 gemini 排前面才不会把它认成 OpenAI。
    host_map = (
        ("generativelanguage", "gemini"),
        ("vertex", "gemini"),
        ("deepseek", "deepseek"),
        ("dashscope", "qwen"),
        ("aliyuncs", "qwen"),
        ("bigmodel", "glm"),
        ("zhipu", "glm"),
        ("moonshot", "kimi"),
        ("anthropic", "anthropic"),
        ("openai", "openai"),
    )
    for needle, dialect in host_map:
        if needle in host:
            return dialect

    # 主机名看不出（自建网关、聚合站）：退回模型名特征。
    # 顺序有讲究——"deepseek-r1" 同时含 "qwen" 的情形不存在，但 "glm" 别被别的词误伤。
    name_map = (
        ("deepseek", "deepseek"),
        ("qwen", "qwen"),
        ("glm", "glm"),
        ("kimi", "kimi"),
        ("moonshot", "kimi"),
        ("claude", "anthropic"),
        ("gemini", "gemini"),
        ("gpt-5", "openai"),
        ("o1", "openai"),
        ("o3", "openai"),
        ("o4", "openai"),
    )
    for needle, dialect in name_map:
        if needle in name:
            return dialect
    return "generic"


def build_thinking_payload(
    *,
    base_url: str,
    model_id: str,
    enabled: bool,
    effort: str = DEFAULT_EFFORT,
    dialect: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """按方言拼出这一轮要附加的思考参数。

    只返回**该方言认识的字段**；空字典表示"这家没有对应开关，什么都不发"。
    """
    resolved = detect_dialect(base_url, model_id, dialect)
    strength = normalize_effort(effort)
    budget = _EFFORT_BUDGET[strength]
    name = (model_id or "").lower()

    if resolved == "deepseek":
        # 只有它懂 "关了就是关了"；开着才谈强度。
        payload: dict[str, Any] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if enabled:
            payload["reasoning_effort"] = strength
        return payload

    if resolved == "qwen":
        # ``enable_thinking`` 是官方字段；预算超上限时服务端会自己截断。
        return {"enable_thinking": enabled, "thinking_budget": budget if enabled else 0}

    if resolved == "glm":
        return {"thinking": {"type": "enabled" if enabled else "disabled"}}

    if resolved == "kimi":
        payload = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        # k3 才收 reasoning_effort，且只认 low|high|max；别的模型发了会 400。
        if enabled and "k3" in name:
            payload["reasoning_effort"] = {"low": "low", "medium": "high", "high": "max"}[strength]
        return payload

    if resolved == "openai":
        # OpenAI 系没有真正的"关闭"：o 系只能降 effort，GPT-5 最低到 minimal。
        # 关闭时不发这个字段（等同模型默认），而不是硬塞一个可能被 400 的值。
        return {"reasoning_effort": strength} if enabled else {}

    if resolved == "gemini":
        if "3" in name:
            # Gemini 3 用档位而不是 token 预算；medium 在 Pro 上不接受，落到 high。
            level = "high" if strength == "medium" else strength
            return {"thinking_level": level}
        return {"thinking_config": {"thinking_budget": budget if enabled else 0}}

    if resolved == "anthropic":
        if not enabled:
            # Anthropic 的思考是"不传即关"，没有 disabled 取值。
            return {}
        # budget_tokens 必须小于 max_tokens，否则报错；留一段给正文。
        cap = budget
        if max_tokens:
            cap = min(budget, max(1024, max_tokens - _ANTHROPIC_MIN_ANSWER_TOKENS))
        return {"thinking": {"type": "enabled", "budget_tokens": cap}}

    # generic：旧行为。SiliconFlow 等网关认 enable_thinking；不认的会忽略未知字段。
    return {"enable_thinking": enabled}
