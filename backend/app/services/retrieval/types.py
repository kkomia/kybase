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
    "FULLTEXT_CHANNEL",
    "VECTOR_CHANNEL",
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


#: 通道名**就是** ``RetrievalHit.raw_scores`` 里的键名。放在这里（而不是 ``service.py``）
#: 是因为命中自己要用它回答"我是哪条通道捞上来的"（见 ``RetrievalHit.basis``）——
#: 定义在服务实现里的话，类型那一侧只能另抄一份字面量，而两套名字迟早对不上。
VECTOR_CHANNEL = "vector"
FULLTEXT_CHANNEL = "fulltext"


@dataclass(frozen=True, slots=True)
class MetadataFilter:
    """元数据过滤（架构 §5）。**在候选生成之前下推到两条召回通道**。

    从前它只被事后应用（取完 ``top_k`` 再逐条丢），于是"只查某本书"这类**落在少数文档
    上**的过滤常常筛成 0 条或很少条——名额早被全库的块占满了。现在这些条件由
    ``service._store_filter`` 翻成存储层的 ``SearchFilter``，跟着查询一起进 SQL
    （见 ``storage/base.py::SearchFilter`` 与 ``PostgresVectorStore.search``）；
    事后那一道只留作兜底（``_passes_filters``）。

    两类字段的**排除语义**要分清：

    - ``document_ids`` / ``source_kinds`` / ``created_after`` / ``created_before``
      筛的是**文档**，文档不满足就整篇的块都不要；
    - ``page_from`` / ``page_to`` / ``heading_contains`` 筛的是**块自己**。块没有
      页码（``page`` 为 ``None``）或没有标题路径时，**被页码/标题条件整体排除**——
      "我要第 40–60 页"这句话里没有"页码未知的也要"的位置。代价是 ``.md`` / Office
      这类来源本来就没有页码（``page`` 恒为 ``None``）与扫描版 PDF 的不可信页码，
      一旦按页码筛，它们会一条都不剩——对外说明里必须写清这一点。
    """

    document_ids: Sequence[str] | None = None
    source_kinds: Sequence[DataSourceKind] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    page_from: int | None = None
    """只要 ``chunk.page >= page_from`` 的块（"某章"按页码的那一半）。"""
    page_to: int | None = None
    """只要 ``chunk.page <= page_to`` 的块。"""
    heading_contains: str | None = None
    """``chunk.heading_path`` **包含**这个子串（大小写不敏感）。

    找"某章"比页码更准：标题路径是切块时按原文标题层级拼出来的（``第3章 > 3.2节``），
    页码只在 PDF 来源上存在、还可能不可信。空串按"没给"处理。
    """

    def is_empty(self) -> bool:
        """一件要筛的事都没有。

        页码那两项判 ``is not None`` 而不是真假：``page_from = 0`` 是个有意义的条件
        （"只要有页码的块"），按真假判会把它当成"没给"。
        """
        return not (
            self.document_ids
            or self.source_kinds
            or self.created_after is not None
            or self.created_before is not None
            or self.page_from is not None
            or self.page_to is not None
            or self.heading_contains
        )


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """一次检索请求。"""

    query: str
    kb_ids: Sequence[str]
    top_k: int = 8
    """**这次能翻到的总量**（v0.63 起它才单独有意义）。

    从前它与 ``keep`` 是同一个数（都是"返回几条"），两处上限重合。v0.63 起 ``keep``
    是**一页几条**，收敛之后留下的那一批由 ``top_k`` 封顶——名单再拿 ``keep`` 剪，
    ``offset`` 就无处可翻（剪到 ≤ keep，第二页必然是空的）。所以要"把某本书关于 X 的
    内容看全"时开大的就是这个数（对外的上限见 ``services.tools.MAX_TOP_K``）。
    """
    mode: str = RetrievalMode.HYBRID
    candidate_k: int = 40
    """每一路的候选数。融合前多召回一些，给 RRF 留出排序空间。

    **小于 ``top_k`` 时被抬到 ``top_k``**（见 ``__post_init__``）：翻页翻的是候选池，
    召回来得比池子还浅，后面几页只会是空的。
    """
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

    刻意压低（默认 :data:`distribution.DEFAULT_STATS_FLOOR` = **0.37**，低于实测噪声带
    **0.460–0.520**）：相关度下限那道闸（bge-m3 **0.531**）会把"可能相关"的那一段也剪掉，
    而**判断契合程度靠的正是"这条答案周围围着多少噪声"**。三个数都是**真余弦**刻度
    （旧刻度的 0.80 / 0.854–0.885 / 0.89 按 ``v = 1 - √(2(1 - 旧值))`` 换算而来）。

    **这一位没有"关闭"这个取值**（v0.55）：正的数 = 覆盖默认窗口，``None`` / ``0`` /
    任何非正数 = 用默认窗口（``service.resolve_stats_floor``）。分布与随之而来的收敛
    是这套机制的产品特征，"传个 0 就别算了"这种口径只会让某些入口悄悄少了它。
    """
    baseline: float | None = None
    """标定基线（噪声天花板），判拟合度的参照线；``None`` = 按嵌入模型标定（见 ``service``）。

    ``0`` 与负值同样按"没给"处理（读设置的那一侧折成 ``None``，服务那一侧也再兜一次）：
    基线是**参照线**，一条 0 的基线会把整批候选都判成"过线"，那不是"关掉判定"而是"全放行"。
    """
    keep: int | None = None
    """**调用方（模型）决定**的一页几条；``None`` = 用分布给出的建议。

    **v0.63 起它是"一页几条"，不再是"这一轮一共几条"**：一共能翻到多少由 ``top_k``
    封顶（见 :attr:`offset`）。``offset=0`` 时两者恰好一致——原来那些调用一个字都不用改。
    """
    min_score: float | None = None
    """**调用方（模型）决定**的分数下限（绝对余弦）；``None`` = 用建议。"""
    per_doc: int | None = None
    """**调用方（模型）决定**的"每篇文档最多留几条"；``None`` = 用建议。"""
    offset: int = 0
    """**翻页的起点**（v0.63）：跳过前 N 条**幸存者**（收敛之后留下的那一批）。

    返回的是 ``幸存者[offset : offset + 解析后的 keep]``。**判定与收敛不跟着它走**：
    统计窗口、``summarize``、三个收敛旋钮一律作用在同一批候选上——于是同一批候选的
    ``summary`` / ``distribution`` / ``decision`` 在任何一页上**逐字相同**。判定随翻页
    变动是没用的：调用方正是靠它决定"要不要继续翻"。

    负数/给不出数当 0，越过末尾当空页，都不报错（见 ``service.resolve_offset``）——
    它是"模型顺手给的调节量"，给错一个数就让整次检索失败不划算。
    """
    rerank: bool = False
    filters: MetadataFilter | None = None

    def __post_init__(self) -> None:
        if self.mode not in RetrievalMode.ALL:
            raise ValueError(f"未知检索模式：{self.mode}")
        if self.top_k <= 0:
            raise ValueError("top_k 必须为正整数")
        if self.candidate_k < self.top_k:
            # **召回深度跟着池子走**（v0.63 起是"抬上来"而不是报错）：``top_k`` 从
            # "返回条数"变成"能翻到的总量"之后，一次要 50 条池子的调用很可能压根没传
            # ``candidate_k``（默认 40）——报错就等于"把上限抬到 50"这件事只有显式传了
            # candidate_k 的人叫得动，而多出来的那一档恰恰是这次要开的能力。
            # 要 50 条却没有 50 个候选是自相矛盾的，替它补上就是；少召回的后果
            # （翻到一半就空了）不报错，只会让人以为"库里就这么多内容"。
            object.__setattr__(self, "candidate_k", self.top_k)


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
    """各路原始分：向量侧是**余弦相似度**（已由距离换算），全文侧是全文排序分（越大越相关）。
    调试台要显示"相似度"，靠的就是它——融合分只反映名次，不是相似度。
    逐条读它容易拿错路（全文那一路的量纲完全不同），所以命中上另给了 ``similarity``
    与 ``lexical_score``，以及"这条是哪一路捞上来的" ``basis``。"""
    rerank_score: float | None = None

    @property
    def basis(self) -> str:
        """这条命中**是哪条通道捞上来的**：``"vector"`` / ``"fulltext"``。

        取的就是 :attr:`raw_scores` 的键名（:data:`VECTOR_CHANNEL` / :data:`FULLTEXT_CHANNEL`），
        不另起一套说法：同一件事两个名字，必然有一处先漂。两条通道都命中时算
        ``"vector"``（余弦那一侧的信息更多）。兜底的 ``"none"`` 理论上到不了
        （融合出来的候选至少来自一条通道），写它只是因为"说不出是哪一路"时**不该编一个**。

        **产品上的用处**：``min_score`` 只切向量通道的命中（词面通道没有余弦可比，
        见 ``service._apply_distribution``），所以调用方要逐条看这一位才知道手里这条
        受不受阈值约束——``similarity`` 为 ``null`` 时它必然是 ``"fulltext"``。
        """
        if VECTOR_CHANNEL in self.raw_scores:
            return VECTOR_CHANNEL
        if FULLTEXT_CHANNEL in self.raw_scores:
            return FULLTEXT_CHANNEL
        return "none"

    @property
    def lexical_score(self) -> float | None:
        """词面（全文）通道的原始分：全文检索的排序分，**越大越相关**。

        ``None`` = 这条不是词面捞上来的。**量纲与 :attr:`similarity` 无关，两个数不能
        互相比较**：相关度下限只认余弦，词面那一路"够不够像"由实词覆盖率判
        （见 ``coverage.py`` 与 ``service._passes_relevance``）。

        为什么要单独给这一位：``min_score`` 管不着词面通道的命中，调用方要自己筛
        就只能读原始分——而逐条去翻 :attr:`raw_scores` 容易拿错路（文案与 REST
        的响应里也是这么说的）。
        """
        return self.raw_scores.get(FULLTEXT_CHANNEL)


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
    """兜底阈值之上的分布（v0.54）。**v0.55 起无条件**：只有"空查询 / 没有库"那种
    提前返回里才是 ``None``——正常的一次检索一定有分布（见 ``distribution.py`` 的模块头）。"""
    decision: dict[str, object] | None = None
    """这次**实际**按什么切的：``{"keep": n, "min_score": x, "per_doc": y, "decided_by": ...}``。

    调试台与工具结果都读它——用户问"为什么只回了 3 条"时，答案必须是一个数而不是猜测。

    ``keep`` 是**一页几条**（v0.63）：它不随 ``offset`` 变，翻页也不改它。
    """
    offset: int = 0
    """这一页是从第几条幸存者开始的（**回显实际生效的那个数**：请求负数时折成 0）。

    回显而不是回传请求值：调用方据此知道自己那一页到底从哪儿开始——"我传了 -3"与
    "系统按 0 走的"必须是同一份载荷里的同一句话。
    """
    next_offset: int | None = None
    """下一页的起点；**没有更多了就是 ``None``**（空页也是 ``None``）。

    有了它，调用方不必猜"翻完没有"：把 ``next_offset`` 填回 ``offset`` 接着翻即可。
    空页必须是 ``None`` 而不是 ``offset + 0``——``keep`` 收敛成 0（``fit=none`` 那一档）
    时照公式回一个数会把调用方送进死循环。
    """
    kept: int = 0
    """**收敛后剩下多少条**（= 这次能翻到的总量 = :attr:`hits` 那一份名单的长度）。

    与 ``len(hits)`` 不是一回事：``hits`` 只是这一页。翻页**不许**改变"收敛留下了多少"
    ——否则翻三页就把按库命中趋势记成三倍（见 ``service._record_search_hits``），
    而 ``summary`` 那句"收敛后留 N 条"也会一页一个样。这个字段与那句话同源。
    """
