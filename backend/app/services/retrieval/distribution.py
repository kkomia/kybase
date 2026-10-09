"""召回的**分数分布统计**：把"这一批命中长什么样"量化出来，交给模型去决定切在哪。

## 为什么要有这一层（本机医学库实测，2026-09-28）

用户在 21580 个 chunk 的医学库上问「活动性巩膜炎患者大多数需要治疗吗？」，得到的
是一大段上下文。把两道相关度下限关掉后把分布打出来，看到的是三件事：

1. **相关问题的分数是一条"平台"**：37 条候选全挤在 **0.593–0.805**（换算自旧刻度的
   0.918–0.981），相邻落差最大只有 **0.027**——**没有自然切点**，靠"最大断崖"切根本切不出来；
2. **无关问题整条分布往下挪**：「今天北京的天气怎么样？」30 条落在 **0.460–0.520**
   （旧刻度 0.854–0.885），而标定基线（噪声天花板）是 0.531——**整批被拦住**（返回 0 条）。
   也就是说，"这个问题库答不答得了"能从**分布的位置**读出来，而不是从单条最高分读出来；
3. **撑长上下文的不是条数，是每条的体量**：默认 6 条 = 12133 字、8 条 = 14541 字，
   而每条都被切成 ~2000 字；同一个问题还被 4–5 本教科书各答一遍（3 条来自同一本）。

**上面的数字都是"真余弦"**（2026-09-29 换了口径：在那之前"相似度"按欧氏公式算，
整体虚高——真余弦 ``v`` 会显示成 ``1 - (1-v)²/2``，见
`services/retrieval/service.py` 的 `similarity_from_distance`）。旧刻度 → 真余弦：
``v = 1 - √(2(1 - 旧值))``；本文档里凡是引旧刻度的地方都加了"旧刻度"标注。

所以这一层给模型的是**三样它自己算不出来的事实**：

- **分布的形状**（条数、分位数、带宽）——用来判断"这是一大批差不多的，还是只有两条";
- **相对基线的位置**（最高分/中位数比噪声天花板高多少）——用来判断**契合程度**；
- **落在几篇文档上、每篇几条**——冗余在这里，而不是在分数里。

判定仍然只有一处：``summarize`` 给出**建议**（``suggested_*``），调用方要么照它走，
要么用模型自己给的数覆盖它（见 ``services/tools._search`` 与 ``service.search``）。

## 拟合度和它的阈值是怎么来的

基线 0.531 就是 ``service.MIN_VECTOR_SCORE_BY_MODEL`` 里那个标定值（实测噪声头
0.514 之上一档，旧刻度 0.89 / 0.882）。实测的两条真查询最高分是 0.805 / 0.665、
中位数 0.623 / 0.623，噪声查询是 0.520 / 0.476（旧刻度 0.981/0.944、0.929/0.929、
0.885/0.8625）——**两者之间隔着 0.06 以上**，所以：

- ``max < baseline`` → ``none``：库答不了这个问题（实测那类查询整批在基线之下）；
- ``max ≥ baseline + STRONG_MARGIN`` 且 ``median ≥ baseline`` → ``strong``；
- 其余 → ``weak``（有东西，但不那么对得上）。

这三个档不是装饰：``search`` 工具会把它们写进工具结果，``none`` 那一路的处置是
**如实告诉用户"资料里没有"**，而不是把一堆噪声当依据编答案。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "FIT_NONE",
    "FIT_STRONG",
    "FIT_UNKNOWN",
    "FIT_WEAK",
    "MAX_PER_DOCUMENT_SHOWN",
    "STRONG_MARGIN",
    "ScoreDistribution",
    "percentile",
    "summarize",
]

FIT_STRONG = "strong"
FIT_WEAK = "weak"
FIT_NONE = "none"
FIT_UNKNOWN = "unknown"
"""**判不了**：这个库的嵌入模型没标定过（余弦的绝对尺度是模型属性，我们没有它的参照线）。

与 ``service.calibrated_vector_floor`` 同一条纪律：没标定 = 不下结论。这时分布照样报
（条数、文档冗余是尺度无关的），但**不做任何收敛**——退回"按调用方要的条数返回"，
也就是这个机制上线之前的既有行为。**这一条是安全阀**：标定值只覆盖 bge-m3，
别的模型上拿它的基线去判"契合程度"会把它们的真命中全判成 noise。
"""

#: ``strong`` 要求最高分比基线高出的量。**在真余弦刻度上**：旧刻度取 0.04
#: （"实测真查询比噪声头高 0.03+，留一点余量"），换算到真余弦 —— 同一条变换在
#: bge-m3 基线处（0.89 → 0.531）把 0.89+0.04 映到 0.626，于是余量是 **0.095**。
#: 这个量随基线走（变换是非线性的）：基线 0.531 处为 0.095、0.337 处为 0.063，
#: 取 0.095（旧刻度上唯一有实测记录的那条基线的等价量）——宁可让 ``strong`` 难判一点，
#: 也不要把"刚过线"的一批误升成 strong。
STRONG_MARGIN = 0.095

#: 分布里回报前几篇文档（再多对"要不要按文档去重"这个判断没有增量）。
MAX_PER_DOCUMENT_SHOWN = 5

#: 文档数到这一档时，**建议**按"每篇最多 1 条"收敛——同一个问题被多本手册各答一遍
#: 是本机实测到的冗余形态（3 条来自同一本、4 条来自同一本）。
MANY_DOCUMENTS = 3
SUGGESTED_PER_DOC = 1

#: 建议的条数上限（**刻意保守**）。实测依据：医学库上同一个常见问题，「每篇 1 条 + 共 4 条」
#: 是 8132 字，「每篇 2 条 + 共 6 条」是 12129 字——而分数带只有 0.03 宽（切哪都是同一批内容），
#: 多要的那两条并不带来新信息。模型想要更多可以自己传 `keep` 覆盖。
STRONG_KEEP_LIMIT = 4
WEAK_KEEP_LIMIT = 2


@dataclass(frozen=True, slots=True)
class ScoreDistribution:
    """兜底阈值之上那一批命中的分布（``basis="none"`` 时只有条数可信）。"""

    floor: float
    """兜底低阈值：统计窗口的下沿（**故意压低**，见模块头）。"""
    baseline: float
    """标定基线（噪声天花板）：判断"契合程度"的参照线。"""
    basis: str
    """"vector" = 分数是余弦（可比、能判拟合度）；"none" = 没有向量分，只有条数可信。"""
    count: int
    """兜底之上的候选条数。"""
    above_baseline: int
    """其中高于基线的条数（"真的对得上"的那一批）。"""
    maximum: float
    minimum: float
    median: float
    mean: float
    p25: float
    p75: float
    p90: float
    band: float
    """``maximum - minimum``：分布有多"平"。带宽窄 = 这批东西差不多，切哪都一样。"""
    documents: int
    """命中落在几篇文档上。"""
    per_document: tuple[tuple[str, int], ...]
    """条数最多的前几篇 ``(文档名, 条数)``——冗余一眼可见。"""
    fit: str
    """``strong`` / ``weak`` / ``none``（见模块头）。"""
    suggested_keep: int
    """程序建议的返回条数（模型没给 ``keep`` 时用它）。"""
    suggested_min_score: float | None
    """程序建议的分数下限（``None`` = 不建议再按分数切）。"""
    suggested_per_doc: int | None
    """程序建议的"每篇最多几条"（``None`` = 不限）。"""
    note: str
    """给模型看的一句话：它现在该怎么读这批命中。"""

    def as_payload(self) -> dict[str, object]:
        """塞进工具结果的紧凑形状（**别把整份分布塞进上下文**：字段名短、只留有用的）。

        分位数取三位小数就够判断了——四位小数在提示词里只是噪声（实测的分辨率是 0.03 级）。
        """
        payload: dict[str, object] = {
            "floor": round(self.floor, 3),
            "baseline": round(self.baseline, 3),
            "basis": self.basis,
            "count": self.count,
            "above_baseline": self.above_baseline,
            "fit": self.fit,
            "suggested": {
                "keep": self.suggested_keep,
                "min_score": (
                    round(self.suggested_min_score, 3)
                    if self.suggested_min_score is not None
                    else None
                ),
                "per_doc": self.suggested_per_doc,
            },
            "note": self.note,
        }
        if self.basis == "vector" and self.count:
            payload["scores"] = {
                "max": round(self.maximum, 3),
                "p90": round(self.p90, 3),
                "p75": round(self.p75, 3),
                "median": round(self.median, 3),
                "p25": round(self.p25, 3),
                "min": round(self.minimum, 3),
                "band": round(self.band, 3),
            }
        if self.count:
            payload["documents"] = self.documents
            payload["per_document"] = [list(item) for item in self.per_document]
        return payload


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """线性插值分位数（``fraction`` 取 0–1）。空序列返回 0.0。

    用插值而不是"最近秩"：样本量小的时候（几条命中）最近秩会让 p90 与 max 永远相等，
    而"p90 比 max 低多少"正是判断"是不是只有一条特别高"的那半点信息。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = max(0.0, min(1.0, fraction)) * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[int(position)])
    weight = position - lower
    return float(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight)


def summarize(
    scored: Sequence[tuple[str, float | None, str]],
    *,
    floor: float,
    baseline: float | None,
    basis: str = "vector",
    keep_cap: int,
) -> ScoreDistribution:
    """把 ``(文档名, 相似度或 None, 文档 id)`` 这一批候选统计成一份分布。

    ``baseline`` 为 ``None`` = **这个库的嵌入模型没标定过** → 判不了契合度、也不做收敛
    （见 ``FIT_UNKNOWN``）。判定只有一处，见模块头。

    ``scored`` **已经按兜底阈值筛过**（调用方筛的，这样"筛掉多少"能记在 filtered_out 里），
    这里只做统计与建议，不再丢东西。

    **相似度为 ``None`` 的候选**（只有全文通道捞上来、没有向量分）照旧计入条数与文档冗余，
    但**不参与分数统计**：拿融合分（RRF）当相似度算 min/p25/band 是错的——它是名次的
    事后换算（实测混进来会把 min 拉到 0.007、带宽拉到 0.974，整份分布就废了）。
    """
    count = len(scored)
    scores = sorted((s for _, s, _ in scored if s is not None), reverse=True)  # type: ignore[type-var]
    if count == 0:
        return ScoreDistribution(
            floor=floor,
            # 字段按构造保证是 float（``None`` = 没标定过 → 0.0）：载荷那边直接 round 它，
            # 留一个 None 进来会在"空窗口 + 没标定"这个组合上炸（集成用例抓到过）
            baseline=baseline if baseline is not None else 0.0,
            basis=basis,
            count=0,
            above_baseline=0,
            maximum=0.0,
            minimum=0.0,
            median=0.0,
            mean=0.0,
            p25=0.0,
            p75=0.0,
            p90=0.0,
            band=0.0,
            documents=0,
            per_document=(),
            fit=FIT_NONE,
            suggested_keep=0,
            suggested_min_score=None,
            suggested_per_doc=None,
            note="兜底阈值之上一条都没有：这份资料里没有相关内容，如实说明，不要凭常识编。",
        )

    documents = Counter(doc_id for _, _, doc_id in scored)
    names: dict[str, str] = {}
    for name, _, doc_id in scored:
        names.setdefault(doc_id, name or doc_id)
    per_document = tuple(
        (names.get(doc_id, doc_id), n)
        for doc_id, n in documents.most_common(MAX_PER_DOCUMENT_SHOWN)
    )

    if not scores:
        # 一条向量分都没有（纯关键词那一档 / 没配嵌入模型）：**不判拟合度**，
        # 与 `_passes_relevance` 同一条纪律——没有语义证据就不下结论
        return ScoreDistribution(
            floor=floor,
            baseline=0.0,
            basis="none",
            count=count,
            above_baseline=0,
            maximum=0.0,
            minimum=0.0,
            median=0.0,
            mean=0.0,
            p25=0.0,
            p75=0.0,
            p90=0.0,
            band=0.0,
            documents=len(documents),
            per_document=per_document,
            fit=FIT_UNKNOWN,
            suggested_keep=min(max(1, keep_cap), count),
            suggested_min_score=None,
            suggested_per_doc=None,
            note=(
                "这次没有语义分数（只做了关键词检索）：条数可信，**契合程度判不了**，"
                "所以不做收敛。照关键词命中回答，并说明依据的是词面命中。"
            ),
        )

    maximum = scores[0]
    minimum = scores[-1]
    median = percentile(scores, 0.5)

    if baseline is None:
        # **没标定过 = 判不了**（见 FIT_UNKNOWN）：照报分布，但不收敛、不切分数。
        # 退回"按调用方要的条数返回"，也就是这个机制上线之前的行为。
        return ScoreDistribution(
            floor=floor,
            baseline=0.0,
            basis=basis,
            count=count,
            above_baseline=0,
            maximum=maximum,
            minimum=minimum,
            median=median,
            mean=sum(scores) / len(scores),
            p25=percentile(scores, 0.25),
            p75=percentile(scores, 0.75),
            p90=percentile(scores, 0.9),
            band=maximum - minimum,
            documents=len(documents),
            per_document=per_document,
            fit=FIT_UNKNOWN,
            suggested_keep=min(max(1, keep_cap), count),
            suggested_min_score=None,
            suggested_per_doc=None,
            note=(
                f"这个库的嵌入模型没有标定过（最高分 {maximum:.3f}）："
                "**契合程度判不了**，所以不做收敛，按你要的条数返回。"
                "条数与每篇的分布仍然可信，仍可按它们自己传 `keep` / `per_doc` 收敛。"
            ),
        )

    if maximum < baseline:
        fit = FIT_NONE
        suggested_min_score: float | None = None
    elif maximum >= baseline + STRONG_MARGIN and median >= baseline:
        fit = FIT_STRONG
        suggested_min_score = baseline
    else:
        fit = FIT_WEAK
        suggested_min_score = baseline
    above_baseline = sum(1 for value in scores if value >= baseline)

    # 建议的条数：**先按"每篇最多几条"收敛，再封顶**（两处上限都刻意保守，见常量处的实测）。
    # 为什么不是"取前 N"：实测的浪费来自同一个问题被多本手册各答一遍（3 条 / 4 条来自同一本），
    # 按文档收敛才治得动它；而分数带只有 0.03 宽时，多要几条并不带来新信息。
    suggested_per_doc = SUGGESTED_PER_DOC if len(documents) >= MANY_DOCUMENTS else None
    limit = min(max(1, keep_cap), STRONG_KEEP_LIMIT if fit == FIT_STRONG else WEAK_KEEP_LIMIT)
    if fit == FIT_NONE:
        suggested_keep = 0
    elif suggested_per_doc is None:
        suggested_keep = min(limit, count)
    else:
        suggested_keep = min(limit, suggested_per_doc * len(documents), count)

    if fit == FIT_NONE:
        note = (
            f"最高分 {maximum:.3f} 低于基线 {baseline:.3f}：**这份资料大概答不了这个问题**。"
            "如实说明，不要把这几条当成依据编答案。"
        )
    elif fit == FIT_WEAK:
        note = (
            f"最高分 {maximum:.3f} 刚过基线 {baseline:.3f}：**对上了一部分**。"
            "只被问到的那一点用最相关的 1–2 条回答，其余的别铺开。"
        )
    else:
        note = (
            f"最高分 {maximum:.3f}、中位 {median:.3f}（基线 {baseline:.3f}）："
            f"**资料很对得上**，兜底阈值之上有 {count} 条、分布在 {len(documents)} 篇里，"
            f"带宽只有 {maximum - minimum:.3f}（都差不多，切哪都是同一批内容）。"
            "该收敛：按 `per_doc` 每篇留最相关的，别把同一件事的重复段落都列进去。"
        )

    return ScoreDistribution(
        floor=floor,
        baseline=baseline,
        basis=basis,
        count=count,
        above_baseline=above_baseline,
        maximum=maximum,
        minimum=minimum,
        median=median,
        mean=sum(scores) / len(scores),
        p25=percentile(sorted(scores), 0.25),
        p75=percentile(sorted(scores), 0.75),
        p90=percentile(sorted(scores), 0.9),
        band=maximum - minimum,
        documents=len(documents),
        per_document=per_document,
        fit=fit,
        suggested_keep=suggested_keep,
        suggested_min_score=suggested_min_score,
        suggested_per_doc=suggested_per_doc,
        note=note,
    )
