"""检索服务的类型定义（M3 T3.x）。

这些类型同时服务于**检索调试台**：界面要能展示"每一路召回了什么、融合后排第几"，
所以结果里必须带够中间信息，而不只是最终列表（架构 §3.3）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.models.enums import DataSourceKind
from app.services.retrieval.distribution import ScoreDistribution

__all__ = [
    "ChannelStat",
    "MetadataFilter",
    "RetrievalHit",
    "RetrievalMode",
    "RetrievalQuery",
    "RetrievalResponse",
]


class RetrievalMode:
    """检索模式。``VECTOR`` 与 ``FULLTEXT`` 用于把"召回不行"和"融合不行"分开定位。"""

    HYBRID = "hybrid"
    VECTOR = "vector"
    FULLTEXT = "fulltext"

    ALL = (HYBRID, VECTOR, FULLTEXT)


@dataclass(frozen=True, slots=True)
class MetadataFilter:
    """元数据过滤（架构 §5）。"""

    document_ids: Sequence[str] | None = None
    source_kinds: Sequence[DataSourceKind] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    def is_empty(self) -> bool:
        return not any(
            (self.document_ids, self.source_kinds, self.created_after, self.created_before)
        )


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """一次检索请求。"""

    query: str
    kb_ids: Sequence[str]
    top_k: int = 8
    mode: str = RetrievalMode.HYBRID
    candidate_k: int = 40
    """每一路的候选数。融合前多召回一些，给 RRF 留出排序空间。"""
    score_threshold: float | None = None
    """**相对**融合分的下限（该条分 / 最高分）：1.0 = 只保留并列第一。"""
    min_vector_score: float | None = None
    """向量余弦的**绝对**下限（v0.53）。``None`` = 按嵌入模型标定默认，``0`` = 关闭。

    必须绝对：``score_threshold`` 是相对最高分的比例，而"整批都不相关"时最高分
    本身就是噪声，比例永远接近 1（实测传 0.9 仍剩 7 条噪声）。``None`` 时按库的
    嵌入模型查 ``service.MIN_VECTOR_SCORE_BY_MODEL``——没标定过的模型 → 0（不设限）。
    """
    min_term_coverage: float | None = None
    """词面覆盖的绝对下限（v0.53）：查询实词里至少多少比例出现在候选正文里。

    ``None`` = 跟着向量下限走（向量下限 > 0 时为 ``DEFAULT_MIN_TERM_COVERAGE`` = 0.67，
    否则 0），``0`` = 关闭。为什么这一半不挂在全文通道的原始分上：见 ``coverage.py``。
    """
    stats_floor: float | None = None
    """**分布统计窗口的兜底低阈值**（绝对余弦，v0.54，见 ``distribution.py``）。

    刻意压低（默认 **0.37**，低于实测噪声带 **0.460–0.520**）：相关度下限那道闸
    （bge-m3 **0.531**）会把"可能相关"的那一段也剪掉，而**判断契合程度靠的正是
    "这条答案周围围着多少噪声"**。三个数都是**真余弦**刻度（旧刻度的
    0.80 / 0.854–0.885 / 0.89 按 ``v = 1 - √(2(1 - 旧值))`` 换算而来）。
    ``None`` = 不开启分布统计（保持既有行为），``0`` = 从零开始统计（什么都收）。
    """
    baseline: float | None = None
    """标定基线（噪声天花板），判拟合度的参照线；``None`` = 按嵌入模型标定（见 ``service``）。"""
    keep: int | None = None
    """**调用方（模型）决定**的返回条数；``None`` = 用分布给出的建议。"""
    min_score: float | None = None
    """**调用方（模型）决定**的分数下限（绝对余弦）；``None`` = 用建议。"""
    per_doc: int | None = None
    """**调用方（模型）决定**的"每篇文档最多留几条"；``None`` = 用建议。"""
    rerank: bool = False
    filters: MetadataFilter | None = None

    def __post_init__(self) -> None:
        if self.mode not in RetrievalMode.ALL:
            raise ValueError(f"未知检索模式：{self.mode}")
        if self.top_k <= 0:
            raise ValueError("top_k 必须为正整数")
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k 不能小于 top_k")


@dataclass(slots=True)
class RetrievalHit:
    """融合后的命中。``channels`` 与 ``ranks`` 是给调试台看的：这条是怎么被捞上来的。"""

    chunk_id: str
    document_id: str
    knowledge_base_id: str
    text: str
    score: float
    """**融合分**（RRF 家族：``1/(k+rank)`` 量级），只反映**名次**，不是相似度。

    它用来排序、也用来做"相对阈值"（``score / top_score``）那一档判断。
    **千万别把它当相似度**：向量通道第 1 名的融合分恒为一个很小的常数
    （实测 0.0164；hybrid 第 1 名 0.0328），拿它跟"相关度地板"比会永远不触发。
    要看真实相似度用 ``similarity``。
    """
    similarity: float | None = None
    """这条命中的**真实余弦相似度**（向量通道的原始分）。

    ``None`` = 这条只被全文通道捞上来、没有向量分（那时"像不像"我们说不出来）。
    相关度地板（``MIN_VECTOR_SCORE_BY_MODEL``）比的就是它——**不是** ``score``；
    界面要显示"相似度"或做"到此为止"的判断，也必须读它（见 ``raw_scores`` 的说明）。
    """
    page: int | None = None
    heading_path: str | None = None
    image_ids: Sequence[str] = field(default_factory=tuple)
    document_name: str | None = None
    channels: Sequence[str] = field(default_factory=tuple)
    ranks: dict[str, int] = field(default_factory=dict)
    raw_scores: dict[str, float] = field(default_factory=dict)
    """各路原始分：向量侧是**余弦相似度**（已由距离换算），全文侧是 -bm25。
    调试台要显示"相似度"，靠的就是它——融合分只反映名次，不是相似度。
    逐条读它容易拿错路（全文那一路的量纲完全不同），所以命中上另给了 ``similarity``。"""
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class ChannelStat:
    """单路召回的统计：命中数与耗时。慢在哪一路，一眼能看出来。"""

    channel: str
    count: int
    elapsed_ms: float


@dataclass(slots=True)
class RetrievalResponse:
    """检索结果 + 调试信息。"""

    hits: list[RetrievalHit]
    mode: str
    reranked: bool
    stats: list[ChannelStat] = field(default_factory=list)
    filtered_out: int = 0
    """被元数据过滤、相对阈值或**相关度下限**挡掉的候选数——避免"结果为空"时无从判断原因。"""
    distribution: ScoreDistribution | None = None
    """兜底阈值之上的分布（v0.54）。``None`` = 这次没开分布统计（``stats_floor`` 为空）。"""
    decision: dict[str, object] | None = None
    """这次**实际**按什么切的：``{"keep": n, "min_score": x, "per_doc": y, "decided_by": ...}``。

    调试台与工具结果都读它——用户问"为什么只回了 3 条"时，答案必须是一个数而不是猜测。
    """
