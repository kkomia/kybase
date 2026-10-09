"""Embedding 抽象（M2 T2.9）。

两种实现：

- :class:`~app.services.embedding.openai_compat.OpenAICompatEmbedder` —— 唯一的产品实现，
  指向任何 OpenAI 兼容端点（硅基流动 / 本地 Ollama / vLLM 均可）；
- :class:`~app.services.embedding.deterministic.DeterministicEmbedder` —— 无语义的词面哈希，
  **只在显式打开 ``KYLAB_DEV_EMBEDDING`` 时使用**（离线开发与测试）。

模型锁定规则（架构 §6.4）由知识库记录承担：``embedding_model_id`` + ``embedding_dim``
一旦入库即冻结，换 endpoint 可以、换模型必须新建库。

**v0.8：取消静默兜底。** 以前"没配 key"会自动退回哈希实现，界面上标一个"开发兜底"
继续跑——那会让用户以为检索是有效的，而它只反映词面重合。现在没配就是没配：
建库被拒、向量通道跳过，界面如实说明。哈希实现仍保留，但必须显式开关。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.embedding.base import (
    EmbeddingError,
    EmbeddingNotConfiguredError,
    EmbeddingProvider,
    fit_dimension,
    l2_normalize,
)
from app.services.embedding.deterministic import DeterministicEmbedder
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

if TYPE_CHECKING:  # 只为标注 `build_embedder` 的参数类型
    # **不能**写在模块级：`runtime_config` 反过来要 import 本包的 `protocols`
    # （协议那张表要进设置页），于是成环 —— 包 `__init__` → `runtime_config` →
    # `embedding.protocols` → 又回到包 `__init__`，而那一刻它还没执行完 →
    # 任何 `import app.services.embedding.*` 都会抛
    # `ImportError: cannot import name 'RuntimeConfigService' from partially initialized module`。
    # 注解本身因为有 `from __future__ import annotations` 而是字符串，运行时不需要这个名字。
    from app.services.runtime_config import RuntimeConfigService

logger = logging.getLogger(__name__)

#: 没配模型时给用户的下一步动作。文案出现在设置页、建库失败与接口错误里，
#: 三处必须一致——否则用户在不同入口看到不同说法，只能靠猜。
NOT_CONFIGURED_HINT = (
    "未配置嵌入模型：请在「设置 → 模型注册」添加供应商并登记向量化模型，"
    "再到「设置 → 向量化」把它选为默认嵌入模型"
)

__all__ = [
    "DEFAULT_PROTOCOL",
    "NOT_CONFIGURED_HINT",
    "OPENAI_PROTOCOL",
    "PROTOCOL_OPTIONS",
    "WEMM_PROTOCOL",
    "DeterministicEmbedder",
    "EmbeddingError",
    "EmbeddingNotConfiguredError",
    "EmbeddingProvider",
    "OpenAICompatEmbedder",
    "WeMMEmbedder",
    "build_embedder",
    "fit_dimension",
    "implementation_for",
    "l2_normalize",
    "normalize_protocol",
    "supports_media",
]


def build_embedder(
    runtime: RuntimeConfigService, *, dev_embedding: bool = False
) -> EmbeddingProvider:
    """按**运行期配置**选实现。没配嵌入模型时抛错，不退回兜底。

    每次调用都重新读配置，因此用户在设置页选完模型 / 换了协议立刻生效，不必重启进程。

    **协议决定用哪个实现**（`services/embedding/protocols.py` 那张表）：默认
    ``openai``——没动过这一位时，走的还是从前那条 OpenAI 兼容实现，行为一位不变。

    ``dev_embedding`` 由 ``KYLAB_DEV_EMBEDDING`` 显式打开（组合根传入）：
    测试与离线开发要一条不联网的链路，但它**必须是有人主动开的一步**，
    而不是"配错了也照样跑"的隐形降级。
    """
    config = runtime.embedding()
    if config.is_configured:
        # 两个实现的构造参数**刻意保持一致**（见 protocols.implementation_for）：
        # 换协议只换类，不换接线
        return implementation_for(config.protocol)(
            base_url=config.base_url,
            api_key=config.api_key,
            model_id=config.model_id,
            dim=config.dim,
            max_batch=config.batch_size,
        )

    if dev_embedding:
        logger.warning(
            "已显式启用开发用确定性嵌入（KYLAB_DEV_EMBEDDING，%s，%d 维）："
            "向量召回只反映词面重合、没有语义，仅用于离线开发与测试。",
            DeterministicEmbedder.model_id,
            config.dim or 256,
        )
        return DeterministicEmbedder(dim=config.dim or 256)

    raise EmbeddingNotConfiguredError(NOT_CONFIGURED_HINT)
