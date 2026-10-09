"""混合检索服务（M3 T3.1–T3.6）。

流程：**多路召回 → RRF 融合 → 元数据过滤 → 可选 rerank → 截断**。

几处刻意的决定：

- **先按 kb_id 收敛再检索**（架构 §8.3）：向量表本就按库分区，全文侧也带 kb 过滤；
- **元数据过滤放在召回之后**：MVP 用"多召回再筛"实现，因为过滤维度（类型、时间）不在
  检索索引里。代价是过滤很严时可能筛空，故把 ``filtered_out`` 一并返回，让界面能解释
  为什么结果为空；后续可把常用过滤下沉到索引；
- **阈值作用于相对融合分**（``该条分 / 最高分``）：向量距离与 BM25 分数量纲不同，
  绝对阈值没法跨模式通用；相对阈值在任何模式下语义一致（1.0 = 只保留并列第一）；
- **相关度下限作用于通道原始分**（v0.53，见 ``_passes_relevance``）：相对阈值剪不掉
  "整批都不相关"那一类——噪声查询的最高分本身就是噪声，比例永远接近 1（实测：传 0.9
  仍剩 7 条噪声、0.99 只砍掉名次靠后的）。要判断"这批东西压根不相关"，只能看绝对值；
- **rerank 失败降级为不重排**：日志告警即可，绝不因此让整次检索失败（架构 §5 把它定义为可选增强）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from app.services.embedding.base import EmbeddingNotConfiguredError, EmbeddingProvider
from app.services.embedding.resolver import EmbeddingResolver
from app.services.retrieval.coverage import content_terms, term_coverage
from app.services.retrieval.distribution import ScoreDistribution, summarize
from app.services.retrieval.fusion import DEFAULT_RRF_K, rrf_fuse
from app.services.retrieval.rerank import RerankError, RerankProvider
from app.services.retrieval.types import (
    ChannelStat,
    MetadataFilter,
    RetrievalHit,
    RetrievalMode,
    RetrievalQuery,
    RetrievalResponse,
)
from app.storage.base import DocumentRecord, StoreBundle

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MIN_TERM_COVERAGE",
    "MIN_VECTOR_SCORE_BY_MODEL",
    "RetrievalService",
    "calibrated_vector_floor",
    "similarity_from_distance",
]

VECTOR_CHANNEL = "vector"
FULLTEXT_CHANNEL = "fulltext"

#: 向量通道（余弦）的相关度下限，**按嵌入模型标定**。
#:
#: 余弦的绝对值是**模型属性**，不是通用量（同一个"相关"在不同模型上是 0.4 还是 0.95
#: 完全看它的训练目标），所以这张表只写本机实测标定过的模型：bge-m3 上语料内两段任意
#: 文本的余弦底就很高，用别的模型的尺度写死一个数会把它的真命中全拦掉。
#:
#: bge-m3 的 0.531 来源（2026-09-24，本机四库实测；**旧刻度 0.89 的逐点换算**）：
#:
#: - 那次标定记的数是**旧刻度**（当时"相似度"还按欧氏公式算，见
#:   `similarity_from_distance` 的说明）：噪声查询（库里没有答案）的头部在
#:   0.844–0.882——「今天北京的天气怎么样」0.882、「真空镀膜机的靶材更换周期」0.864、
#:   「红烧肉的做法步骤」0.844；真查询的常态是 0.92–0.97；取值 0.89 = 刚好高过实测
#:   最高那条噪声头；
#: - 换算到真余弦（``v = 1 - √(2(1 - 旧值))``）：0.89 → **0.531**，
#:   噪声头 0.882 → 0.514，真查询 0.92–0.97 → 0.60–0.755。**同一个单调变换**，
#:   所以拦住/放行的候选与改动前**一模一样**，只是这个数现在说的是真余弦；
#: - 代价仍然真实：短查询/罕见词会低于它（「眼科」→0.571、「郭正茂」那条真命中 →0.456、
#:   「Hallermann」→0.473）——这一类靠**词面覆盖**那一路救回（见 ``coverage.py``）。
#:   因此这个数不往上抬：再抬只会开始拦真查询里较弱的那些，而噪声并不会少拦一条。
#:
#: **0.33 的来源（``WeMM-Embedding-2B``，2026-09-29，本机真库实测，真余弦）**：
#:
#: 样本是**真实的那个多模态库**（`kb_wemm_multimodal`：图片 / 视频 / docx / xlsx 混在
#: 一个向量空间里，走服务自己的链路跑 `RetrievalService.search`）：
#:
#: - **不相关那一侧**：查询「有人在弹钢琴」在 `top_k=8` 里的真余弦是
#:   0.131 / 0.128 / 0.305 / 0.170 / 0.164 / 0.163 / 0.157 / 0.156 —— **噪声头 0.305**；
#: - **相关那一侧**："文案描述画面"的真命中是 **0.386–0.717**（0.386 那条是
#:   `probe-shapes` 截图，擦边但确实是命中的那一条）；
#: - 取 **0.33**：比噪声头高 0.025、比测到过的最弱真命中低 0.056 —— 方向与
#:   bge-m3 那条一致（0.531 高于它的噪声头 0.514），**偏向"宁可漏、也别误杀真命中"**；
#:   先前的 0.35 与 0.386 只差 0.036，擦边命中容易被漏掉，故再降 0.02。
#: - 服务侧直测也吻合：文本查图「一片切开的西柚放在白色盘子上」真余弦 **+0.5168**、
#:   查视频「一朵正在开放的黄色花朵」**+0.4203**；不相关是 +0.0684 至 +0.1989 ✓。
#:
#: **旧刻度（0.89 / 0.78）是怎么来的**：那之前 `similarity_from_distance` 把余弦距离
#: 当成欧氏距离算（``1 - d²/2``，见那个函数的说明），所以"相似度"整体虚高：
#: 真余弦 ``v`` 会被显示成 ``1 - (1-v)²/2``。bge-m3 的 0.89 与 WeMM 的 0.78 都是
#: 在**那个虚高刻度**上标出来的，换算回真余弦分别是 **0.531** 与 **0.337**：
#: ``v = 1 - √(2(1 - 旧值))``。
#:
#: - bge-m3 取换算值 **0.531**：这是**逐点等价**的换算（同一个单调变换），
#:   所以它拦住/放行的候选与改动前**完全一样**，只是现在这个数说的是真余弦；
#: - WeMM 取 **0.33**（略低于换算值 0.337）：两边都试过，取 0.33 是为了把 0.386
#:   那类擦边真命中也捞回来——"宁可漏也别误杀"优先。
MIN_VECTOR_SCORE_BY_MODEL: dict[str, float] = {
    "bge-m3": 0.531,
    "wemm-embedding-2b": 0.33,
}

#: 词面覆盖下限（见 ``coverage.py``）：查询实词里至少 **2/3** 出现在候选正文或它的文档名里。
#:
#: 取值的根据同样是实测（2026-09-24，本机四库，逐条见交付记录）：噪声查询的最高覆盖是
#: 2/4（「合唱团的排练时间安排」蹭到「时间」「安排」，「如何用 Rust 写一个 HTTP 服务器」
#: 蹭到「一个」「服务」），而真命中那一侧——人名「郭正茂」1/1、按文档名找的
#: 「0.1.0版本体验修了什么」3/4 = 0.75——都在这条线之上。取 0.67 就是"刚高过实测噪声的最高覆盖"。
DEFAULT_MIN_TERM_COVERAGE = 0.67


def similarity_from_distance(distance: float) -> float:
    """**余弦距离 → 余弦相似度**（``cos = 1 - d``）。

    仓储给的 ``distance`` 是 pgvector 的 ``<=>``，也就是**余弦距离**（``1 - cos``，
    见 `storage/postgres_impl/vector_store`：索引建在 ``vector_cosine_ops`` 上）。
    所以换算就是一句减法。

    **曾经这里写的是 ``1 - d²/2``**（那是**欧氏距离**在单位向量上的公式）：
    两个公式在 ``d = 0``（完全相同）处一致，所以"精确命中距离 0"的用例一直是绿的，
    但只要有一点差异就开始虚高——实测 ``d = 0.483245``（真余弦 0.5168）被算成
    ``0.883237`` ✗。后果是**所有"相似度"都虚高**，界面把 0.70 的真实重合显示成 0.96，
    而按这份虚高分标定的相关度下限也就跟着失去了意义。

    反向也一样：单位向量之间的欧氏距离最大是 2（``d = 2`` 才是"完全相反"），
    余弦距离最大是 2，两个区间恰好重合，所以**光看数值范围分辨不出用的是哪个**——
    只能盯住"仓储给的是哪个算子"。这里与 ``VectorMatch.distance`` 的文档绑在一起。
    """
    similarity = 1.0 - distance
    return max(-1.0, min(1.0, similarity))


def calibrated_vector_floor(model_id: str) -> float:
    """这个嵌入模型上"算相关"的余弦下限；**没标定过的模型返回 0 = 不设限**。

    为什么按模型查表而不是写一个全局默认：见 ``MIN_VECTOR_SCORE_BY_MODEL``。
    匹配用"包含"而不是相等：注册表里的模型 id 带供应商前缀（``BAAI/bge-m3``）
    或版本后缀（``bge-m3:latest``），要求用户把名字写得分毫不差才会生效，
    那等于这个默认对大多数人是不生效的。
    """
    lowered = (model_id or "").casefold()
    for name, floor in MIN_VECTOR_SCORE_BY_MODEL.items():
        if name in lowered:
            return floor
    return 0.0


def _passes_relevance(
    raw_scores: dict[str, float],
    text: str,
    *,
    title: str = "",
    vector_floor: float,
    coverage_floor: float,
    terms: Sequence[str],
    semantic: bool = True,
) -> bool:
    """这条候选与查询之间有没有**足够强的证据**（v0.53）。

    两条证据，满足其一即可：

    1. **语义够近**：向量通道的原始分（余弦）达到该模型标定的下限；
    2. **词面够像**：查询实词里至少 ``coverage_floor`` 的比例出现在**这段正文或它所属
       文档的名字**里（见 ``coverage.py``：为什么这件事不交给全文通道的原始分）。

    ``title`` 也要算：用户常常是拿文件名/版本号在找东西（实测「0.1.0版本体验修了什么」
    命中的就是同名那份文档，而正文里并没有"版本""体验"这些字样）。不算它就会把
    这类真命中拦掉——**误杀比噪声更糟**。

    为什么是"或"而不是"与"：两者各自对应一类真实命中——换个说法的问法是语义命中、
    连词面都对不上的；索引号/人名/缩写这类是词面命中、余弦反而低（实测「郭正茂」
    那条真命中只有 0.852，低于任何安全的向量下限）。要求"与"会把这两类各砍一半。

    **两条证据都不足的候选会被拦掉**，包括全文通道单独捞上来、而词面覆盖又不到下限
    的那些（原始分高也没用，理由见 ``coverage.py`` 的实测：无关查询也能拿到 9.9）。

    但**只有这一次检索真有语义证据时才判**（``semantic``）：向量通道一条候选都没返回时
    （没配嵌入模型、或用户选了纯关键词那一档），"不相关"这个判断我们下不了——
    那时全文通道的产出照旧原样返回。纯关键词那一档本来就是**调试台**用来分辨
    "召回不行"还是"融合不行"的，不该被判定层改写。

    两条下限都为 0 时直接放行——**没标定过的嵌入模型上默认不改变任何既有行为**。
    """
    if vector_floor <= 0.0 and coverage_floor <= 0.0:
        return True
    vector = raw_scores.get(VECTOR_CHANNEL)
    if vector_floor > 0.0 and vector is not None and vector >= vector_floor:
        return True
    if not semantic:
        # 这次检索压根没有语义证据（向量通道空着）：不下"不相关"的判断，照旧放行
        return True
    if coverage_floor <= 0.0:
        return False
    haystack = f"{title}\n{text}" if title else text
    return term_coverage(terms, haystack) >= coverage_floor


class RetrievalService:
    """混合检索。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        embedder: EmbeddingProvider,
        reranker: RerankProvider,
        rrf_k: int = DEFAULT_RRF_K,
        embedders: EmbeddingResolver | None = None,
        usage_recorder=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._stores = stores
        # 用量回调（可选）：**检索次数原先只在日志里**，于是"检索到底跑了多少次、
        # 平均命中几块"这类问题没有数字可查。缺席时什么都不记，功能照常
        self._usage_recorder = usage_recorder
        self._embedder = embedder
        # 按库解析嵌入模型（v11）：不同库可能用不同模型，查询向量必须按库算
        self._embedders = embedders
        self._reranker = reranker
        # "未配置嵌入模型"只告警一次：否则每次检索都刷一行同样的日志
        self._unconfigured_warned = False
        self._rrf_k = rrf_k

    # ------------------------------------------------------------------ 入口

    def search(
        self,
        request: RetrievalQuery,
        *,
        query_vector: Sequence[float] | None = None,
    ) -> RetrievalResponse:
        if not request.query.strip() or not request.kb_ids:
            return RetrievalResponse(hits=[], mode=request.mode, reranked=False)

        started_all = time.perf_counter()
        raw: dict[str, dict[str, float]] = {}
        ordered: list[tuple[str, Sequence[str]]] = []
        stats: list[ChannelStat] = []
        # 这次检索里"每个库用哪个嵌入实现"只解析一次：向量通道要它本身，
        # 相关度下限要它的身份（模型 id → 标定表）。各解析一次会让每个库多一次
        # resolver 往返（用例 `test_vector_channel_resolves_an_embedder_per_knowledge_base`
        # 盯着这个次数）。
        embedders: dict[str, EmbeddingProvider] = {}

        if request.mode in (RetrievalMode.HYBRID, RetrievalMode.VECTOR):
            started = time.perf_counter()
            vector_ranked, vector_scores = self._vector_channel(request, query_vector, embedders)
            raw[VECTOR_CHANNEL] = vector_scores
            ordered.append((VECTOR_CHANNEL, vector_ranked))
            stats.append(
                ChannelStat(
                    channel=VECTOR_CHANNEL,
                    count=len(vector_ranked),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            )

        if request.mode in (RetrievalMode.HYBRID, RetrievalMode.FULLTEXT):
            started = time.perf_counter()
            text_ranked, text_scores = self._fulltext_channel(request)
            raw[FULLTEXT_CHANNEL] = text_scores
            ordered.append((FULLTEXT_CHANNEL, text_ranked))
            stats.append(
                ChannelStat(
                    channel=FULLTEXT_CHANNEL,
                    count=len(text_ranked),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            )

        fused = rrf_fuse(ordered, k=self._rrf_k)
        hits, filtered_out = self._materialize(fused, raw, request, embedders)
        hits, reranked = self._maybe_rerank(hits, request)

        # —— 动态返回（v0.54）：先看分布，再决定切在哪 ——
        distribution = None
        decision: dict[str, object] | None = None
        if request.stats_floor is not None:
            hits, distribution, decision, dropped = self._apply_distribution(
                hits, request, embedders
            )
            filtered_out += dropped
        # 记在**返回之前**：返回给调用方的条数才是"命中数"，与 stats 里的分路召回数
        # 不是一回事（后者是融合前的候选量）
        self._record_usage(request, hits=hits[: request.top_k], started=started_all)

        return RetrievalResponse(
            hits=hits[: request.top_k],
            mode=request.mode,
            reranked=reranked,
            stats=stats,
            filtered_out=filtered_out,
            distribution=distribution,
            decision=decision,
        )

    def _apply_distribution(
        self,
        hits: list[RetrievalHit],
        request: RetrievalQuery,
        embedders: dict[str, EmbeddingProvider] | None = None,
    ) -> tuple[list[RetrievalHit], ScoreDistribution, dict[str, object], int]:
        """按分布与调用方的决定**收敛**返回集（见 ``distribution.py`` 的模块头）。

        三件事按顺序做，顺序不能反：

        1. **统计**（兜底阈值之上的全批）：条数、分位数、带宽、落在几篇文档上、拟合度；
        2. **切分数**：``min_score``（模型给的 → 否则建议值）。这一步用的是**余弦**，
           不是融合分——融合分只反映名次，拿它当阈值是把"第几名"当成了"多相关"；
        3. **按文档收敛**：``per_doc``（模型给的 → 否则建议值）与 ``keep`` 封顶。
           **按文档**这一步才是治"上下文太长"的旋钮：实测的浪费是同一个问题被多本手册
           各答一遍（3–4 条来自同一本），而不是分数没切开。

        ``decided_by`` 记清这次是"模型定的"还是"按建议走的"——用户问"为什么只回 3 条"
        时，答案必须是一个数加一个来源，而不是一句"系统自动"。
        """
        basis = "vector" if any("vector" in hit.raw_scores for hit in hits) else "none"
        # 基线：显式给了就用（用户配的那一项），否则按嵌入模型标定——**都没标定就是 None**，
        # 那时判不了契合度、也不收敛（见 `distribution.FIT_UNKNOWN`：安全阀）。
        baseline = (
            request.baseline
            if request.baseline is not None
            else self._calibrated_baseline(request, embedders)
        )
        scored: list[tuple[str, float | None, str]] = [
            (
                hit.document_name or hit.document_id,
                # **没有向量分就交 None**：拿融合分（RRF）当相似度算分位数会把整份分布
                # 污染掉（实测 min 被拉到 0.007、带宽拉到 0.974）。条数与文档冗余照旧统计。
                float(hit.raw_scores["vector"]) if "vector" in hit.raw_scores else None,
                hit.document_id,
            )
            for hit in hits
        ]
        distribution = summarize(
            scored,
            floor=request.stats_floor or 0.0,
            baseline=baseline,
            basis=basis,
            keep_cap=request.top_k,
        )

        model_decided = request.keep is not None or request.min_score is not None
        keep = request.keep if request.keep is not None else distribution.suggested_keep
        min_score = (
            request.min_score if request.min_score is not None else distribution.suggested_min_score
        )
        per_doc = request.per_doc if request.per_doc is not None else distribution.suggested_per_doc

        dropped = 0
        # 兜底阈值已经是统计窗口的下沿，这里只可能再往上切：``min_score`` 低于它就等于不切
        if min_score is not None:
            kept: list[RetrievalHit] = []
            for hit in hits:
                similarity = hit.raw_scores.get("vector")
                # **没有相似度的（只有全文通道）照旧留下**：这一刀切的是"不够像"，
                # 而它的证据不在相似度那一侧（词面覆盖已经判过了）——没有证据就不拦，
                # 与 `_passes_relevance` 同一条纪律。
                if similarity is None or float(similarity) >= min_score:
                    kept.append(hit)
                else:
                    dropped += 1
            hits = kept

        if per_doc is not None and per_doc > 0:
            seen: dict[str, int] = {}
            kept = []
            for hit in hits:
                used = seen.get(hit.document_id, 0)
                if used >= per_doc:
                    dropped += 1
                    continue
                seen[hit.document_id] = used + 1
                kept.append(hit)
            hits = kept

        limit = max(0, min(int(keep), request.top_k))
        if len(hits) > limit:
            dropped += len(hits) - limit
            hits = hits[:limit]

        decision = {
            "keep": limit,
            "min_score": min_score,
            "per_doc": per_doc,
            "decided_by": "model" if model_decided else "suggested",
            "fit": distribution.fit,
            "dropped": dropped,
        }
        return hits, distribution, decision, dropped

    def _calibrated_baseline(
        self, request: RetrievalQuery, embedders: dict[str, EmbeddingProvider] | None = None
    ) -> float | None:
        """这一次检索的基线（噪声天花板）；**一个标定值都没有时返回 ``None``**。

        多库混检时取最高而不是最低：基线是"多高才算真的对上"，用最低的那条会让
        一个标定宽松的库把另一个库的噪声也判成"对得上"。

        返回 ``None`` 是这套机制的**安全阀**：标定表只覆盖 bge-m3，别的模型上套它的基线去判
        "契合程度"会把它们的真命中全判成噪声（实测过：确定性嵌入的用例里整库被判成
        `fit=none`、返回 0 条）。``None`` 时照报分布，但不判、不收敛。
        """
        values = [
            calibrated_vector_floor(self._embedding_model_id(kb_id, embedders))
            for kb_id in request.kb_ids
        ]
        values = [value for value in values if value > 0.0]
        return max(values) if values else None

    def _record_usage(
        self, request: RetrievalQuery, *, hits: Sequence[object], started: float
    ) -> None:
        """记一次检索：条数与耗时。

        **不记 mode**：用量事件表没有能放它的字段，把 "hybrid" 塞进 ``model_id``
        会让"按模型聚合"那张表出现一行假模型。分模式的差异看 ``/search`` 响应里的
        分路统计（那本来就是给调试台用的）。
        ``items`` 记的是**返回给调用方的条数**（命中数），不是融合前的候选量。
        """
        if self._usage_recorder is None:
            return
        try:
            self._usage_recorder(
                kind="search",
                items=len(hits),
                # **必须与 started 用同一个时钟**：`perf_counter` 与 `monotonic` 的
                # 起点不同（Windows 上一个是 QPC、一个是开机毫秒），混用会算出
                # 负数时长——实测在整套测试里真的写出了 duration_ms=-19。
                # 本文件的渠道统计也一直用 perf_counter，跟它保持一致。
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception:
            # 与对话/嵌入同一条纪律：**统计失败绝不能反过来影响检索本身**
            logger.exception("检索用量记录失败（不影响本次检索）")

    # ------------------------------------------------------------------ 召回通道

    def _vector_channel(
        self,
        request: RetrievalQuery,
        query_vector: Sequence[float] | None,
        embedders: dict[str, EmbeddingProvider] | None = None,
    ) -> tuple[list[str], dict[str, float]]:
        best: dict[str, float] = {}
        recall_factor = 2 if self._stores.meta.any_disabled_documents(request.kb_ids) else 1
        # **查询向量按模型缓存**：跨库检索时"每个库各算一次查询向量"是浪费——
        # 而多库共用同一个嵌入模型恰恰是常态（默认配置就是这样），
        # `embed()` 又是一次真实的远端调用。缓存键用实现对象的身份：
        # `EmbeddingResolver` 按（供应商, 模型, 维度, 批大小）缓存实例，同模型必得同一对象，
        # 不同模型必得不同对象，且这些对象在一次检索期间都被 resolver 持有（不会被回收后
        # 让 id 复用）。用 id() 而不是自己拼一个模型键，是为了不假设每种嵌入实现都暴露
        # 同样的属性（远端实现与确定性实现长得并不一样）。
        embedded: dict[int, Sequence[float]] = {}
        for kb_id in request.kb_ids:
            # 每个库用自己的嵌入模型算查询向量：跨库混用一个向量是错的——
            # 向量空间不同，相似度没有意义（v11 起嵌入模型是库属性）
            if query_vector is not None:
                vector = list(query_vector)
            else:
                embedder = self._embedder_cached(kb_id, embedders)
                cached = embedded.get(id(embedder))
                if cached is not None:
                    vector = list(cached)
                else:
                    try:
                        vector = embedder.embed([request.query])[0]
                    except EmbeddingNotConfiguredError:
                        # 没配嵌入模型不是"调用失败"，是"这个通道现在用不了"：跳过它，
                        # 全文通道照常返回——库里已有内容仍可检索，界面也能如实说明
                        # "当前只做了关键词检索"（v0.8 取消哈希兜底）
                        if not self._unconfigured_warned:
                            logger.warning("未配置嵌入模型，检索跳过向量通道，只做全文检索")
                            self._unconfigured_warned = True
                        continue
                    embedded[id(embedder)] = vector
            # 有停用文档的库才 2 倍超采：KNN 扫描时没法按"文档是否停用"过滤，
            # 停用的命中会在下游 _materialize 被裁掉——多召回一倍作补偿。
            # 没有停用文档时保持原深度（candidate_k 语义不漂，也省一次放大）。
            for match in self._stores.vectors.search(
                kb_id, query_vector=vector, top_k=request.candidate_k * recall_factor
            ):
                # 同一条 chunk 不应出现在多个库，但真出现时取更近的那个距离
                if match.chunk_id not in best or match.distance < best[match.chunk_id]:
                    best[match.chunk_id] = match.distance

        ranked = sorted(best.items(), key=lambda pair: (pair[1], pair[0]))
        return [chunk_id for chunk_id, _ in ranked], {
            chunk_id: similarity_from_distance(distance) for chunk_id, distance in ranked
        }

    def _embedder_for(self, kb_id: str) -> EmbeddingProvider:
        """这个库该用哪个嵌入实现：显式选了模型就用它，否则全局默认。"""
        if self._embedders is None:
            return self._embedder
        kb = self._stores.meta.get_knowledge_base(kb_id)
        if kb is None:
            return self._embedder
        return self._embedders.for_kb(kb)

    def _fulltext_channel(
        self, request: RetrievalQuery
    ) -> tuple[list[str], dict[str, float]]:
        scored: dict[str, float] = {}
        for kb_id in request.kb_ids:
            hits = self._stores.fulltext.search(
                query=request.query, top_k=request.candidate_k, kb_id=kb_id
            )
            for hit in hits:
                if hit.chunk_id not in scored or hit.score > scored[hit.chunk_id]:
                    scored[hit.chunk_id] = hit.score

        ranked = sorted(scored.items(), key=lambda pair: (-pair[1], pair[0]))
        return [chunk_id for chunk_id, _ in ranked], dict(ranked)

    # ------------------------------------------------------------------ 组装与过滤

    def _materialize(
        self,
        fused: Sequence[tuple[str, float, tuple[str, ...], dict[str, int]]],
        raw: dict[str, dict[str, float]],
        request: RetrievalQuery,
        embedders: dict[str, EmbeddingProvider] | None = None,
    ) -> tuple[list[RetrievalHit], int]:
        if not fused:
            return [], 0

        chunk_ids = [chunk_id for chunk_id, _, _, _ in fused]
        chunks = {chunk.chunk_id: chunk for chunk in self._stores.meta.get_chunks(chunk_ids)}
        documents = self._documents_of(chunks.values())

        top_score = fused[0][1] or 1.0
        threshold = request.score_threshold
        # 相关度下限（每库一对）：按模型标定，见 `MIN_VECTOR_SCORE_BY_MODEL` 与 `coverage.py`
        floors = self._relevance_floors(request, embedders)
        # **这次检索有没有语义证据**：向量通道一条候选都没返回时（没配嵌入模型、
        # 或选了纯关键词那一档）判定层不下结论——理由见 `_passes_relevance`
        semantic = bool(raw.get(VECTOR_CHANNEL))
        # 查询实词**懒算**：正常查询（真余弦 0.60+）一条都不会走到词面那一支，
        # 不该为它们多切一次词
        terms: tuple[str, ...] | None = None

        hits: list[RetrievalHit] = []
        filtered_out = 0
        for chunk_id, score, channels, ranks in fused:
            chunk = chunks.get(chunk_id)
            if chunk is None:
                # chunk 已被删除但索引还没清干净：跳过而不是崩
                filtered_out += 1
                continue

            if chunk.disabled:
                # 用户手动禁用的块（§G3）。**在检索侧过滤而不是删向量**：
                # 恢复时零成本，也不必重新 embedding。
                # 计入 filtered_out，让界面能解释"结果为什么变少了"。
                filtered_out += 1
                continue

            document = documents.get(chunk.document_id)
            if document is not None and document.disabled:
                # 文档级停用（v14）：与切块禁用同一套语义，只是范围是整份文档。
                # KNN 没法在扫描时按文档过滤，所以这里兜底（向量通道已按 2 倍超采缓解）。
                filtered_out += 1
                continue

            if not _passes_filters(document, request.filters):
                filtered_out += 1
                continue
            if threshold is not None and (score / top_score) < threshold:
                filtered_out += 1
                continue

            raw_scores = {
                channel: values[chunk_id] for channel, values in raw.items() if chunk_id in values
            }
            vector_floor, coverage_floor = floors.get(chunk.knowledge_base_id, (0.0, 0.0))
            if vector_floor > 0.0 or coverage_floor > 0.0:
                if terms is None:
                    terms = content_terms(request.query)
                if not _passes_relevance(
                    raw_scores,
                    chunk.text,
                    title=document.name if document else "",
                    vector_floor=vector_floor,
                    coverage_floor=coverage_floor,
                    terms=terms,
                    semantic=semantic,
                ):
                    # 与元数据过滤共用一个计数：界面上那句"N 条被过滤"要说得出
                    # "为什么变少了"，多一个计数就要多一处界面改动
                    filtered_out += 1
                    continue

            hits.append(
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    knowledge_base_id=chunk.knowledge_base_id,
                    text=chunk.text,
                    score=score,
                    # 真实余弦（向量通道的原始分）：这一位是给"相似度"用的，
                    # `score` 只反映名次——两者混用是"地板永远不触发"的根源
                    similarity=raw_scores.get(VECTOR_CHANNEL),
                    page=chunk.page,
                    heading_path=chunk.heading_path,
                    image_ids=tuple(chunk.image_ids),
                    document_name=document.name if document else None,
                    channels=channels,
                    ranks=ranks,
                    raw_scores=raw_scores,
                )
            )
        return hits, filtered_out

    def _relevance_floors(
        self, request: RetrievalQuery, embedders: dict[str, EmbeddingProvider] | None = None
    ) -> dict[str, tuple[float, float]]:
        """这次检索各库的（余弦下限, 词面覆盖下限）。

        ``None`` = 按标定默认，``0`` = 显式关闭（调用方能关掉整套判定）。

        **默认整套跟着模型标定走**：余弦的尺度是模型属性，没标定过的模型上我们说不出
        "多少算相关"，此时连词面那一半也一起关掉——它是"或"里的第二条，单独留着只会
        在没有语义判定兜底时把召回砍掉一截。这正是"宁可少拦"的落点：本机（bge-m3）
        两条件都开，别的模型保持既有行为，要收紧请显式传参。

        ``embedders`` 是这次检索的**每库解析一次**的缓存（见 ``_embedder_cached``）：
        这里要的只是"这个库用的是哪个模型"（查标定表），但它与向量通道要的是**同一个
        对象**——分开解析会让每个库多一次 resolver 往返（用例盯着这个次数）。
        """
        explicit_vector = request.min_vector_score
        explicit_coverage = request.min_term_coverage
        floors: dict[str, tuple[float, float]] = {}
        for kb_id in request.kb_ids:
            vector = (
                calibrated_vector_floor(self._embedding_model_id(kb_id, embedders))
                if explicit_vector is None
                else max(0.0, explicit_vector)
            )
            if request.stats_floor is not None and vector > 0.0:
                # **动态返回时把窗口放宽到兜底阈值**（v0.54）：相关度下限（bge-m3 0.531）会把
                # "可能相关"的那一段也剪掉，而判断契合程度靠的正是"这条答案周围围着多少
                # 噪声"——窗口被剪掉了，分布就只剩答案那一小撮，``baseline`` 也就无从比较。
                # 只放宽、不收紧：兜底阈值比标定值高时按标定值走。
                vector = min(vector, max(0.0, request.stats_floor))
            if explicit_coverage is None:
                # 语向下限开着才有词面下限：两者是一套（见方法说明）
                coverage = DEFAULT_MIN_TERM_COVERAGE if vector > 0.0 else 0.0
            else:
                coverage = max(0.0, explicit_coverage)
            floors[kb_id] = (vector, coverage)
        return floors

    def _embedder_cached(
        self, kb_id: str, cache: dict[str, EmbeddingProvider] | None
    ) -> EmbeddingProvider:
        """这次检索期间"这个库用哪个嵌入实现"只解析一次。

        ``cache`` 为 ``None``（脚本、单次调用）时不缓存，照旧每次解析。
        """
        if cache is None:
            return self._embedder_for(kb_id)
        if kb_id not in cache:
            cache[kb_id] = self._embedder_for(kb_id)
        return cache[kb_id]

    def _embedding_model_id(
        self, kb_id: str, embedders: dict[str, EmbeddingProvider] | None = None
    ) -> str:
        """这个库用的嵌入模型 id；拿不到就返回空串（= 未标定 = 不设限）。"""
        try:
            return str(getattr(self._embedder_cached(kb_id, embedders), "model_id", "") or "")
        except Exception:
            # 取模型身份失败不该让检索失败：退回"未标定"，只是不设下限
            logger.debug("取嵌入模型身份失败（本次不设相关度下限）：%s", kb_id, exc_info=True)
            return ""

    def _documents_of(self, chunks) -> dict[str, DocumentRecord]:
        """批量取回命中的文档元信息（用于过滤与展示来源文档名）。

        **一条查询取回全部**：逐篇 ``get_document`` 是 N+1，而跨多个库检索时
        命中的文档数可能不小（每个库 candidate_k 条候选，去重后仍可能有几十篇）。
        """
        wanted = list({chunk.document_id for chunk in chunks})
        if not wanted:
            return {}
        return self._stores.meta.get_documents_by_ids(wanted)

    # ------------------------------------------------------------------ rerank

    def _maybe_rerank(
        self, hits: list[RetrievalHit], request: RetrievalQuery
    ) -> tuple[list[RetrievalHit], bool]:
        if not request.rerank or not self._reranker.enabled or len(hits) <= 1:
            return hits, False

        try:
            ranked = self._reranker.rerank(
                query=request.query,
                documents=[hit.text for hit in hits],
                top_n=len(hits),
            )
        except RerankError as exc:
            logger.warning("rerank 失败，降级为 RRF 排序：%s", exc)
            return hits, False

        reordered: list[RetrievalHit] = []
        for index, score in ranked:
            if 0 <= index < len(hits):
                hit = hits[index]
                hit.rerank_score = score
                reordered.append(hit)
        # rerank 只给了部分结果时，剩下的按融合分补在后面，避免凭空丢结果
        returned = {id(hit) for hit in reordered}
        reordered.extend(hit for hit in hits if id(hit) not in returned)
        return reordered, True


def _passes_filters(document: DocumentRecord | None, filters: MetadataFilter | None) -> bool:
    if filters is None or filters.is_empty():
        return True
    if document is None:
        return False
    if filters.document_ids and document.id not in filters.document_ids:
        return False
    if filters.source_kinds and document.source_kind not in filters.source_kinds:
        return False
    if filters.created_after and (document.created_at is None
                                  or document.created_at < filters.created_after):
        return False
    return not (
        filters.created_before
        and (document.created_at is None or document.created_at > filters.created_before)
    )
