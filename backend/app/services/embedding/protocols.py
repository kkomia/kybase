"""嵌入协议：名字、默认值、以及"哪个协议对应哪个实现"。

**为什么单列一个模块**：协议名要在三处出现——设置页给的取值、工厂建哪个实现、
解析路由判断"这个实现有没有媒体能力"——三处各写一遍字符串迟早对不上
（一处改成 ``WeMM``、另一处还认 ``wemm``，表现就是"设置保存了但没生效"）。
这里一处定义、一处解析（未知值一律退回默认），别处只允许 import。

**刻意不 import ``runtime_config``**：那个模块要读这里的 ``normalize_protocol``
来解析设置值，反过来 import 就成环。本模块只依赖各实现自己。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.services.embedding.base import EmbeddingProvider
from app.services.embedding.openai_compat import OpenAICompatEmbedder
from app.services.embedding.wemm import WeMMEmbedder

__all__ = [
    "DEFAULT_PROTOCOL",
    "OPENAI_PROTOCOL",
    "PROTOCOL_OPTIONS",
    "PROTOCOL_OPTION_KEY",
    "WEMM_PROTOCOL",
    "implementation_for",
    "normalize_protocol",
    "protocol_for_model",
    "supports_media",
]

OPENAI_PROTOCOL = "openai"
"""OpenAI 兼容（``POST {base_url}/embeddings``）——云端各家与本地 Ollama / vLLM 都走它。"""

WEMM_PROTOCOL = "wemm"
"""局域网 WeMM-Embedding-2B（llama.cpp）：文本同 OpenAI 兼容，**另有图片/视频**那条媒体路。"""

DEFAULT_PROTOCOL = OPENAI_PROTOCOL
"""默认**一位不改既有行为**：没动过这个设置时，所有知识库走的还是 OpenAI 兼容那条路。"""

PROTOCOL_OPTIONS: tuple[tuple[str, str], ...] = (
    (OPENAI_PROTOCOL, "OpenAI 兼容（默认）"),
    (WEMM_PROTOCOL, "WeMM 多模态（llama.cpp：图片/视频也进同一向量空间）"),
)
"""设置页下拉项（``(值, 显示名)``）。顺序即展示顺序，默认项排第一。"""

_IMPLEMENTATIONS: dict[str, type[EmbeddingProvider]] = {
    OPENAI_PROTOCOL: OpenAICompatEmbedder,
    WEMM_PROTOCOL: WeMMEmbedder,
}


def normalize_protocol(raw: str | None) -> str:
    """把设置里的原始值解析成协议名；**不认识的一律当默认**。

    当默认而不是报错：这一位来自"用户手写过的设置行"，写错一个字母就让整条向量通道
    不可用没有道理；而"退回默认"这件事在设置页里看得见（那一栏就显示着生效值）。
    """
    value = (raw or "").strip().lower()
    return value if value in _IMPLEMENTATIONS else DEFAULT_PROTOCOL


def implementation_for(protocol: str) -> type[EmbeddingProvider]:
    """这个协议用哪个实现。**构造参数是一致的**（base_url / api_key / model_id / dim /
    max_batch），所以调用方可以直接 ``implementation_for(p)(**kwargs)``，不必为每个协议
    各写一条构造分支。"""
    return _IMPLEMENTATIONS[normalize_protocol(protocol)]


def supports_media(protocol: str) -> bool:
    """这个协议下的实现有没有媒体（图片/视频）能力。

    问**实现**而不是在这里再列一张表：能力是 `EmbeddingProvider.supports_media` 的属性，
    这里只是把它翻出来。解析路由据此决定"要不要挂上媒体直通解析器"（见
    `services/parser_router.build_parsers`）。
    """
    return bool(implementation_for(protocol).supports_media)


#: 模型登记里声明协议的那一栏（`RegisteredModelRecord.options` 的一个键）。
#: 值是 `PROTOCOL_OPTIONS` 里的取值（``openai`` / ``wemm``）。
PROTOCOL_OPTION_KEY = "protocol"


def protocol_for_model(options: Mapping[str, object] | None, setting: str) -> str:
    """这个**模型**用哪套协议：模型自己声明了就用它的，否则用全局设置。

    为什么协议要能按模型定：一个进程里接两家不同协议的服务是常态（云端 OpenAI 兼容
    做主力 + 局域网 WeMM 做多模态），而"协议"是**服务端的说话方式**，不是这台机器的
    属性。全局设置因此降级成**默认值**（没声明这一栏的模型都用它）。
    设置页暂时还没有这一栏（只在 API/配置层能设），但后端这条路径已经是"按模型"的。

    声明的值不认识时**退回全局设置**而不是报错：与 `normalize_protocol` 同一条分寸
    （写错一个字母不该让某个库的向量通道整个不可用）。
    """
    declared = ""
    if options:
        raw = options.get(PROTOCOL_OPTION_KEY)
        declared = str(raw).strip().lower() if raw is not None else ""
    if declared in _IMPLEMENTATIONS:
        return declared
    return normalize_protocol(setting)
