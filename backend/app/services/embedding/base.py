"""Embedding 提供方协议。

错误类（``EmbeddingError`` / ``EmbeddingNotConfiguredError``）**定义在
`app/core/exceptions.py`**（2026-10-08 剥离阶段 0 搬过去）——它们要被两侧按类型分档，
而"领域异常一律住 core"是本仓既有约定；这里 import 再导出，调用点一行没改。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.core.exceptions import EmbeddingError, EmbeddingNotConfiguredError

__all__ = [
    "EmbeddingError",
    "EmbeddingNotConfiguredError",
    "EmbeddingProvider",
    "fit_dimension",
    "l2_normalize",
]


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """L2 归一化。

    归一化后内积等价于余弦相似度，量纲统一；零向量原样返回（全零文本不该让流程炸掉）。
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def fit_dimension(vector: Sequence[float], dim: int, *, model_id: str = "") -> list[float]:
    """把模型的原生向量对齐到**配置的维度**：先截断，再重新 L2 归一化。

    这是 Matryoshka 那一套（``WeMM-Embedding-2B`` 原生 2048 维，取前 N 维仍是有效的
    嵌入，代价是精度）。两个动作**必须成对**：

    - 只截断不归一化 → 模长不再是 1，而检索这一侧算的是余弦 / 点积，量纲一歪排序就跟着歪。
      这是"维度缩了、相似度悄悄不对"的典型来源：不报错、只是结果变差，最难查；
    - 只归一化不截断 → 维度对不上，仓储那一层会拒（``VectorDimensionMismatch``）。

    比配置维度**短**的向量直接报错而不是补零：补零会造出一个与任何文本都不相似的假向量，
    而那意味着"模型没按声明输出"或"端点点错了"——这两件事都必须让人看见。
    """
    if dim <= 0:
        raise ValueError("维度必须为正整数")
    if len(vector) < dim:
        raise EmbeddingError(
            f"模型 {model_id or '未知'} 实际输出 {len(vector)} 维，少于配置的 {dim} 维；"
            "截断只支持「从长到短」（Matryoshka），请改正模型登记里的维度"
        )
    return l2_normalize(list(vector)[:dim])


class EmbeddingProvider(ABC):
    """向量化提供方。

    ``model_id`` 与 ``dim`` 是模型锁定的依据：知识库记录它们，**不一致就必须拒绝写入**
    （架构 §6.4：维度相同不等于向量空间兼容）。
    """

    model_id: str = "unnamed"
    dim: int = 0
    max_batch: int = 32
    is_development: bool = False
    """开发兜底实现为 True，接口层应据此提示用户"检索质量不代表真实效果"。"""

    supports_media: bool = False
    """这个实现能不能把**图片 / 视频**也变成向量（同一向量空间）。

    默认 False，只有多模态那一家（``WeMMEmbedder``）为 True。它单独一位而不是靠
    ``isinstance`` 判断：摄入那一层据此决定"这份媒体文件要不要走媒体接口"，
    而按类名判会把摄入链路与某个具体实现绑死（换一家多模态服务就得改摄入）。
    """

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """把文本批量转成向量，返回顺序与入参一致。"""

    def embed_media(self, data: bytes) -> list[float]:
        """把**一份媒体文件**（图片 / 视频的原始字节）转成向量。

        默认实现是拒绝：一个只认文本的服务拿到图片字节，唯一正确的反应是说清楚
        "这件事我做不到"，而不是拿文件名当文本嵌进去——那会让检索结果看起来正常、
        实际答非所问。调用方要先问 ``supports_media``。
        """
        raise EmbeddingError(f"嵌入实现 {type(self).__name__} 不支持图片 / 视频向量")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} model_id={self.model_id!r} dim={self.dim}>"
