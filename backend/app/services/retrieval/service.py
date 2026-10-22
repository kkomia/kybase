"""混合检索服务（ –）。

流程：**过滤条件下推 → 多路召回 → RRF 融合 → 元数据过滤（兜底） → 可选 rerank → 截断**。

几处刻意的决定：

- **先按 kb_id 收敛再检索**（架构 8.3）：向量表本就按库分区，全文侧也带 kb 过滤；
- **元数据过滤下推到候选生成之前**（v0.64）：条件先由 :func:`_store_filter` 翻成
  ``SearchFilter``，再交给两条通道的 ``search(filter=…)``，由 SQL 在 **top_k 名额分配
  之前**裁掉不合条件的块。这条改动的由来是"只查某本书"用不了：从前是"取满全局
  ``top_k`` 再一条条丢"，而那本书的块压根没进过候选，于是经常筛成 0 条。事后那一道
  仍然留着（``_passes_filters`` / ``_passes_chunk_filters``），但已降级为**兜底守卫**
  ——见它们的说明；
- **阈值作用于相对融合分**（``该条分 / 最高分``）：向量距离与 BM25 分数量纲不同，
  绝对阈值没法跨模式通用；相对阈值在任何模式下语义一致（1.0 = 只保留并列第一）；
- **相关度下限作用于通道原始分**（v0.53，见 ``_passes_relevance``）：相对阈值剪不掉
  "整批都不相关"那一类——噪声查询的最高分本身就是噪声，比例永远接近 1（实测：传 0.9
  仍剩 7 条噪声、0.99 只砍掉名次靠后的）。要判断"这批东西压根不相关"，只能看绝对值；
- **分布与收敛无条件执行**（v0.55，见 ``distribution.py`` 的模块头）：统计窗口
  （``DEFAULT_STATS_FLOOR``）、报出分布、按分布收敛这三步在**每一次**检索里都跑，
  ``stats_floor`` 只用来**调**窗口（正数覆盖默认，非正数回落默认）——**没有"关掉"
  这个取值**。窗口还必须放宽到统计下沿（``_relevance_floors``），因为"报出契合程度"
  的前提是那批噪声也在窗口里；这两件事是一体的，拆开就只剩"答案那一小撮"可比；
- **rerank 失败降级为不重排**：日志告警即可，绝不因此让整次检索失败（架构 5 把它定义为可选增强）；
- **一个库的嵌入服务坏掉，只降级那个库**（v0.65，见 ``_vector_channel``）：它的向量通道
  这次用不了就跳过（全文通道照旧参与召回），并把"哪个库、为什么"记进
  ``decision.embedding_unavailable_kbs``——跨库检索因此不会因为其中**一个**库的嵌入端点
  连不上而整体失败（实测：7 个库里只有 1 个用 WeMM，那个连不上时整次检索全灭）；
- **翻页只切"返回哪一段"**（v0.63）：``keep`` 是**一页几条**、``top_k`` 是**能翻到的总量**，
  返回的是 ``幸存者[offset : offset + 解析后的 keep]``。窗口、``summarize``、三个收敛旋钮
  一律作用在同一批候选上，于是同一 pool 的判定在任何一页上**逐字相同**——判定若随翻页
  浮动，调用方拿它做的"要不要接着翻"这个判断就没有依据了。
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Sequence

from app.services.embedding.base import (
    EmbeddingError,
    EmbeddingNotConfiguredError,
    EmbeddingProvider,
)
from app.services.embedding.resolver import EmbeddingResolver
from app.services.retrieval.coverage import content_terms, term_coverage
from app.services.retrieval.distribution import (
    DEFAULT_STATS_FLOOR,
    ScoreDistribution,
    summarize,
)
from app.services.retrieval.fusion import DEFAULT_RRF_K, rrf_fuse
from app.services.retrieval.rerank import RerankError, RerankProvider
from app.services.retrieval.types import (
    FULLTEXT_CHANNEL,
    VECTOR_CHANNEL,
    ChannelStat,
    MetadataFilter,
    RetrievalHit,
    RetrievalMode,
    RetrievalQuery,
    RetrievalResponse,
)
from app.storage.base import ChunkRecord, DocumentRecord, SearchFilter, StoreBundle

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MIN_TERM_COVERAGE",
    "DEFAULT_STATS_FLOOR",
    "MIN_VECTOR_SCORE_BY_MODEL",
    "RetrievalService",
    "calibrated_vector_floor",
    "effective_stats_window",
    "resolve_offset",
    "resolve_stats_floor",
    "similarity_from_distance",
]

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
#:   查视频「一朵正在开放的黄色花朵」**+0.4203**；不相关是 +0.0684 至 +0.1989 √。
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
    ``0.883237`` ×。后果是**所有"相似度"都虚高**，界面把 0.70 的真实重合显示成 0.96，
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


def resolve_stats_floor(stats_floor: float | None) -> float:
    """这次检索的**统计窗口下沿**：非正数（含 ``None``）一律回落到默认窗口。

    这一位**没有"关闭"这个取值**（v0.55 的口径）。早先 ``None`` = 不统计、
    ``0`` = 从零开始收，于是"关掉这个特色"只需要一个 0——而 ``retrieval.floor_score``
    的默认值是 ``"0"`` 的那些部署、以及压根不传这一项的 REST / Wiki 两条路，
    就是这样悄悄少了分布（同一份命中在 MCP 里带分布、在网页里没有）。

    现在只剩一件事要判："调用方给了个正数没有"。给了就用它的（设置页那一项是**调**窗口），
    没给或给了非正数就用 :data:`DEFAULT_STATS_FLOOR`——**默认窗口永远在**。
    """
    if stats_floor is not None and stats_floor > 0.0:
        return float(stats_floor)
    return DEFAULT_STATS_FLOOR


def resolve_offset(offset: int | None) -> int:
    """这一页从第几条**幸存者**开始：负数、给不出数一律当 0。

    与三个收敛旋钮同一条取舍（见 ``tools._int_or_none``）：它是"模型顺手给的调节量"，
    给错一个数就让整次检索失败不划算——而"从第几条开始给"给错了顶多是多翻一页。
    **越过末尾不是错**：那时回一页空的（见 ``RetrievalService.search`` 的分页那一段），
    判定照旧——把"翻到底了"报成一次失败，只会让调用方以为检索坏了。
    """
    try:
        value = int(offset)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def effective_stats_window(
    stats_floor: float | None, explicit_vector_score: float | None = None
) -> float:
    """这次检索**实际按哪条线收候选**：载荷里报给模型的 ``floor`` 就是它。

    两个来源，**显式的那个优先**：

    - 调用方显式给了 ``min_vector_score``（调试台、精细调用方）：它就是窗口。判定下限
      本来就是"从哪条线开始收"，报一个更低的数等于让载荷替我们背书一句没做过的事
      （"0.37 之上的都看了"）；而显式抬高它只是少收几条，不会改变别的行为。
      显式传 0 = 判定整套关掉、候选全收，这时窗口如实报 0；
    - 否则 :func:`resolve_stats_floor`——非正数一律回落到默认窗口（永远关不掉）。

    **只放宽、不收紧**说的是窗口对**标定值**的作用（见 ``_relevance_floors``）：
    窗口比标定值低时按窗口收，比它高时按标定值收。
    """
    if explicit_vector_score is not None:
        return max(0.0, float(explicit_vector_score))
    return resolve_stats_floor(stats_floor)


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
        search_recorder=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._stores = stores
        # 用量回调（可选）：**检索次数原先只在日志里**，于是"检索到底跑了多少次、
        # 平均命中几块"这类问题没有数字可查。缺席时什么都不记，功能照常
        self._usage_recorder = usage_recorder
        # 按库命中回调（可选，v0.61）：一次检索按库交出一份
        # ``{kb_id: (候选数, 留存数)}``。**检索层不认识存储**——它只说"这次每个库
        # 贡献了多少、留了多少、整批判成了什么"，落库由 ``services/search_stats.py``
        # 负责。缺席时什么都不记，检索照常。
        self._search_recorder = search_recorder
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

        # **统计窗口只解析一次**（v0.55）：下面每一处用它的人都拿这个值——放宽相关度下限
        # （`_relevance_floors`）、统计（`summarize`）、收敛（`_apply_distribution`）。
        # 三处各自去读一次 `request.stats_floor` 的结果是"报告的窗口不是实际用的窗口"。
        stats_floor = effective_stats_window(request.stats_floor, request.min_vector_score)

        started_all = time.perf_counter()
        raw: dict[str, dict[str, float]] = {}
        ordered: list[tuple[str, Sequence[str]]] = []
        stats: list[ChannelStat] = []
        # **这次检索里向量通道用不了的库**（`{kb_id: 一句原因}`）：由 `_vector_channel`
        # 逐个库判出来，返回前并进 ``decision``（见下面那一段）。它是**这次调用的局部量**
        # 而不是实例状态——`RetrievalService` 是进程级单例，记在 `self` 上会让下一次检索
        # 读到上一次的降级名单。
        embedding_unavailable: dict[str, str] = {}
        # 这次检索里"每个库用哪个嵌入实现"只解析一次：向量通道要它本身，
        # 相关度下限要它的身份（模型 id → 标定表）。各解析一次会让每个库多一次
        # resolver 往返（用例 `test_vector_channel_resolves_an_embedder_per_knowledge_base`
        # 盯着这个次数）。
        embedders: dict[str, EmbeddingProvider] = {}

        # **过滤条件只翻译一次**（v0.64）：两条通道要的是同一份下推条件，各翻一次
        # 是同一件事的两个副本；而"两路筛得不一样"正是最该避免的那种不一致。
        store_filter = _store_filter(request.filters)

        if request.mode in (RetrievalMode.HYBRID, RetrievalMode.VECTOR):
            started = time.perf_counter()
            vector_ranked, vector_scores, embedding_unavailable = self._vector_channel(
                request, query_vector, embedders, store_filter
            )
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
            text_ranked, text_scores = self._fulltext_channel(request, store_filter)
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
        hits, filtered_out = self._materialize(fused, raw, request, embedders, stats_floor)
        hits, reranked = self._maybe_rerank(hits, request)

        # —— 动态返回（v0.54；v0.55 起**无条件**）：先看分布，再决定切在哪 ——
        # 这里没有 if：分布与收敛**永远**跑。理由见模块头与 `_relevance_floors`——
        # 统计窗口的放宽与"报出分布"是同一件事的两半：先把噪声带一起收进来，
        # 才谈得上"报出契合程度、再按它切掉"。让调用方传个 0 / None 就能跳过它，
        # 等于允许某条入口悄悄退回"没有分布"的旧行为，而那正是这次要拆掉的东西。
        hits, distribution, decision, dropped, candidates_by_kb = self._apply_distribution(
            hits, request, embedders, stats_floor
        )
        filtered_out += dropped

        # **降级名单并进 ``decision``**（v0.65）：调用方与模型都该知道"这次哪些库的
        # 向量通道用不了"——不说的话，"跨库检索少了一路召回"在载荷里毫无痕迹。
        #
        # 为什么放 ``decision``：它已经是 ``dict | None``，**REST 与 MCP 两条路都原样
        # 透传**（``api/v1/search.py`` 与 ``tools._search`` 各是一个键，MCP 的
        # ``structuredContent`` 也是同一份），调用方本来就在读它（``decided_by`` /
        # ``unscored_kept``）。改一处，两条路都看得见，也不必动 ``tools.py`` 的载荷形状
        # 与 ``outputSchema``。
        #
        # **全部库都正常时不出这个键**：一个恒在的空映射会让每个调用方多写一次
        # "它是不是空的"，而且"这次每个库都正常"与"这次压根没走到向量通道"会长得一样。
        if embedding_unavailable:
            decision["embedding_unavailable_kbs"] = embedding_unavailable

        # —— 翻页（v0.63）：判定与收敛已经落定，这里只决定"交出哪一段" ——
        # 顺序是刻意的：窗口、``summarize``、三个收敛旋钮全在 `_apply_distribution` 里跑完，
        # 而它们**看不到 offset**——于是同一 pool 的判定在任何一页上都逐字相同
        # （``summary`` / ``distribution`` / ``decision`` 不随翻页变）。
        # 让 offset 参与判定（比如"这一页没命中就重裁一次"）会把判定变成随翻页浮动的东西，
        # 而调用方正是靠它决定要不要接着翻。
        offset = resolve_offset(request.offset)
        # 一页几条 = 解析后的 keep（见 `_apply_distribution`：keep 现在只管窗口、不剪名单）
        page_size = int(decision["keep"])
        kept = len(hits)  # 收敛之后剩下多少 = 这次能翻到的总量
        page = hits[offset : offset + page_size]
        # **空页一律说"没有下一页"**：``page_size`` 收敛成 0（``fit=none`` 的建议就是 0）时
        # 照公式回 ``offset + 0`` 会把调用方送进死循环——翻一百次还是同一页空结果。
        next_offset = offset + len(page) if page and offset + len(page) < kept else None

        # 记在**返回之前**：返回给调用方的条数才是"命中数"，与 stats 里的分路召回数
        # 不是一回事（后者是融合前的候选量）
        self._record_usage(request, hits=page, started=started_all)
        # **交出去的是一整份幸存者名单，不是这一页**：按库命中趋势记的是"这次收敛留下了
        # 多少"——记这一页的话，翻三页就把那条曲线记成三倍（见 `_record_search_hits`）。
        self._record_search_hits(
            request,
            hits=hits,
            candidates_by_kb=candidates_by_kb,
            fit=distribution.fit,
            started=started_all,
        )

        return RetrievalResponse(
            hits=page,
            mode=request.mode,
            reranked=reranked,
            stats=stats,
            filtered_out=filtered_out,
            distribution=distribution,
            decision=decision,
            offset=offset,
            next_offset=next_offset,
            kept=kept,
        )

    def _apply_distribution(
        self,
        hits: list[RetrievalHit],
        request: RetrievalQuery,
        embedders: dict[str, EmbeddingProvider] | None = None,
        stats_floor: float | None = None,
    ) -> tuple[list[RetrievalHit], ScoreDistribution, dict[str, object], int, Counter[str]]:
        """按分布与调用方的决定**收敛**幸存者名单（见 ``distribution.py`` 的模块头）。

        三件事按顺序做，顺序不能反：

        1. **统计**（兜底阈值之上的全批）：条数、分位数、带宽、落在几篇文档上、拟合度；
        2. **切分数**：``min_score``（模型给的 → 否则建议值）。这一步用的是**余弦**，
           不是融合分——融合分只反映名次，拿它当阈值是把"第几名"当成了"多相关"。
           **只有向量通道的命中能被它切**：没有余弦的那些（词面通道捞上来的）照旧留下，
           条数记在 ``decision.unscored_kept`` 里——留是对的（没证据就不拦），
           不说清就是骗人；
        3. **按文档收敛**：``per_doc``（模型给的 → 否则建议值）。
           **按文档**这一步才是治"上下文太长"的旋钮：实测的浪费是同一个问题被多本手册
           各答一遍（3–4 条来自同一本），而不是分数没切开。

        返回值的第一项是**收敛之后的整份名单**（v0.63 起；从前它是"按 ``keep`` 剪过的
        返回集"）。名单由 ``top_k`` 封顶，``keep`` 只决定一页几条——剪名单的活交给
        ``search`` 分页那一段做（在那里剪才不会让 ``offset`` 无处可翻）。
        名单上的条数就是"这次能翻到的总量"，与返回的那一页是两回事。

        ``decided_by`` 记清这次是"模型定的"还是"按建议走的"——用户问"为什么只回 3 条"
        时，答案必须是一个数加一个来源，而不是一句"系统自动"。上面三个旋钮**任意一个**
        由调用方给出，这一位就是 ``"model"``。

        ``stats_floor`` 是 ``search`` 解析好的统计窗口下沿，**必须**原样传给
        ``summarize``：载荷里的 ``floor`` 是给模型看的"这批候选是按哪条线收上来的"，
        与 ``_relevance_floors`` 实际用的那条线对不上的话，那个数就没有意义了。

        返回值最后一项是**按库的收敛前条数**（``{kb_id: 候选数}``）：它必须在这里数，
        因为"候选"的定义就是"``summarize`` 看到的那一批"——回 ``search`` 再从头数一遍
        等于对同一件事有第二个定义，而两处迟早会对不上（v0.61 的按库命中趋势要它）。
        收敛**之后**的条数由 ``search`` 从这份名单的长度读（``len(hits)``，不是那一页）。
        """
        basis = VECTOR_CHANNEL if any(VECTOR_CHANNEL in hit.raw_scores for hit in hits) else "none"
        # 收敛前的按库条数：**就是下面传给 summarize 的同一批 hits**，所以在这里数
        candidates_by_kb: Counter[str] = Counter(hit.knowledge_base_id for hit in hits)
        # 基线：显式给了正数就用（用户配的那一项），否则按嵌入模型标定——**都没标定就是
        # None**，那时判不了契合度、也不收敛（见 `distribution.FIT_UNKNOWN`：安全阀）。
        # 非正数与"没给"同义（设置页那一侧也是这么折的，见 `retrieval_thresholds`）：
        # 一条 0 的基线不是"关掉判定"，而是"整批都过线"，那与不判是两回事。
        baseline = (
            request.baseline
            if request.baseline is not None and request.baseline > 0.0
            else self._calibrated_baseline(request, embedders)
        )
        scored: list[tuple[str, float | None, str]] = [
            (
                hit.document_name or hit.document_id,
                # **没有向量分就交 None**：拿融合分（RRF）当相似度算分位数会把整份分布
                # 污染掉（实测 min 被拉到 0.007、带宽拉到 0.974）。条数与文档冗余照旧统计。
                float(hit.raw_scores[VECTOR_CHANNEL]) if VECTOR_CHANNEL in hit.raw_scores else None,
                hit.document_id,
            )
            for hit in hits
        ]
        distribution = summarize(
            scored,
            floor=effective_stats_window(
                stats_floor if stats_floor is not None else request.stats_floor,
                request.min_vector_score,
            ),
            baseline=baseline,
            basis=basis,
            keep_cap=request.top_k,
        )

        # 三个旋钮**都算调用方自己的决定**：`per_doc` 也是他按分布算出来的收敛方式。
        # 早先只认 keep / min_score，于是传了 per_doc 的那次照样记 `decided_by="suggested"`
        # ——一边照他的数收敛、一边说"这是系统建议的"，是同一份载荷里的自相矛盾
        # （实测被用户抓到的就是这一条）。
        model_decided = (
            request.keep is not None or request.min_score is not None or request.per_doc is not None
        )
        keep = request.keep if request.keep is not None else distribution.suggested_keep
        min_score = (
            request.min_score if request.min_score is not None else distribution.suggested_min_score
        )
        per_doc = request.per_doc if request.per_doc is not None else distribution.suggested_per_doc

        dropped = 0
        # 没有余弦可比、因而**绕过 ``min_score`` 那一刀**的条数（见下面的分支）：
        # 这一位就是给"我把阈值提到 0.99，回来的却是一批 `similarity: null`"那个现象的答案。
        unscored_kept = 0
        # 兜底阈值已经是统计窗口的下沿，这里只可能再往上切：``min_score`` 低于它就等于不切
        if min_score is not None:
            kept: list[RetrievalHit] = []
            for hit in hits:
                similarity = hit.raw_scores.get(VECTOR_CHANNEL)
                # **没有余弦的（只有词面通道）照旧留下**：这一刀切的是"不够像"，
                # 而它的证据不在余弦那一侧（词面覆盖已经判过了）——没有证据就不拦，
                # 与 `_passes_relevance` 同一条纪律。留下归留下，**数出来**：
                # 不然调用方以为阈值生效了。
                if similarity is None:
                    unscored_kept += 1
                    kept.append(hit)
                elif float(similarity) >= min_score:
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
        if limit == 0 and hits:
            # ``keep == 0`` 是 ``fit=none`` 的建议值：**这一轮一条都不要**。这时连名单都
            # 不留——没有"第几页"可言，那几条也不该被记成"收敛后留下的"。
            dropped += len(hits)
            hits = []
        elif len(hits) > request.top_k:
            # **封顶的是 ``top_k``，不再是 ``keep``**（v0.63）：``keep`` 改成页大小之后，
            # 再用它剪名单的话 ``offset`` 就无处可翻——名单被剪到 ≤ keep，第二页必然为空。
            # ``top_k`` 才是"这次能翻到的总量"（所以它从 20 抬到了 50）。
            dropped += len(hits) - request.top_k
            hits = hits[: request.top_k]

        decision = {
            # 这一位是**一页几条**（v0.63）：判定与它无关，翻页也不改它
            "keep": limit,
            "min_score": min_score,
            "per_doc": per_doc,
            "decided_by": "model" if model_decided else "suggested",
            "fit": distribution.fit,
            "dropped": dropped,
            # 这一位与 `dropped` 是一对：`dropped` 说"切掉了几条"，它说"有几条**没被切**，
            # 因为手里根本没有可比的那个数"。缺了它，"阈值提到 0.99 却还有命中"就只剩
            # "看不懂"这一种解释。为 0 = 这次要么没有这一刀，要么每个候选都有余弦。
            "unscored_kept": unscored_kept,
        }
        return hits, distribution, decision, dropped, candidates_by_kb

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

    def _record_search_hits(
        self,
        request: RetrievalQuery,
        *,
        hits: Sequence[RetrievalHit],
        candidates_by_kb: Counter[str],
        fit: str,
        started: float,
    ) -> None:
        """记一次检索的**按库命中**（v0.61）：候选、留存、契合度。

        交给回调的是并集 ``{kb_id: (候选数, 留存数)}``：

        - ``candidates_by_kb`` 是收敛**之前**各库贡献的条数（``_apply_distribution`` 里数的）；
        - 留存数是**收敛之后留下的那一批**（= 这一批幸存者）里各库的条数。**它不是某一页**：
          v0.63 起一次检索可以翻页，而"这次收敛留下了多少"是这次检索的属性，与调用方翻到
          第几页无关——照那一页记的话，翻三页就把同一次检索在曲线上记成三倍，趋势就不再是
          趋势了。所以翻到第二页时它**可以**与这次返回的 ``hits`` 不等，而且必须不等才对；
        - 再并上 ``request.kb_ids``——**"这个库被检索了但一条没命中"也要记一行**
          （候选 0）。少了它，"这个库最近没人查"与"查了但查不到"在曲线上长得一样，
          而那正是这条曲线要回答的一半问题。

        并集在这里算而不是在回调里：只有检索层知道"这次检索了哪些库"。
        ``request.kb_ids`` 空的那一档（"全库检索"）在 ``search`` 开头就返回了，
        真走到这里又一条命中都没有的话，也没有任何一个库可以被归因——
        **那就什么都不写**（回调收到空映射会直接返回）。
        """
        if self._search_recorder is None:
            return
        try:
            kept_by_kb: Counter[str] = Counter(hit.knowledge_base_id for hit in hits)
            kb_ids = set(candidates_by_kb) | set(kept_by_kb) | set(request.kb_ids)
            if not kb_ids:
                return
            self._search_recorder(
                hits_by_kb={
                    kb_id: (candidates_by_kb.get(kb_id, 0), kept_by_kb.get(kb_id, 0))
                    for kb_id in kb_ids
                },
                fit=fit,
                # 与 `_record_usage` 用同一个时钟（同为 perf_counter）：见那里的说明，
                # 混用 monotonic 会算出负数时长
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception:
            logger.exception("检索命中记录失败（不影响本次检索）")

    # ------------------------------------------------------------------ 召回通道

    def _vector_channel(
        self,
        request: RetrievalQuery,
        query_vector: Sequence[float] | None,
        embedders: dict[str, EmbeddingProvider] | None = None,
        store_filter: SearchFilter | None = None,
    ) -> tuple[list[str], dict[str, float], dict[str, str]]:
        """向量通道的召回（**逐库**）。

        返回的第三项是**这次检索里向量通道用不了的库**（``{kb_id: 一句原因}``）：
        它空着就是"每个库都算出了查询向量"。交给 ``search`` 并进 ``decision``——
        调用方据此知道"这次少了一路召回、少在哪个库"。

        为什么是**返回值**而不是 ``self`` 上的一位：``RetrievalService`` 是进程级单例
        （``core/services.py`` 一张图只建一次），把"这次哪几个库坏了"记在实例上，
        下一次检索就会读到上一次的降级名单——那是"上一次的告警出现在这一次载荷里"
        这类最难查的错。（``_unconfigured_warned`` 是唯一住在 ``self`` 上的那位，
        它的语义本来就跨调用：同一条日志只打一次。）
        """
        best: dict[str, float] = {}
        # 这次用不了向量通道的库（见方法说明）
        unavailable: dict[str, str] = {}
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
                        # "当前只做了关键词检索"（v0.8 取消哈希兜底）。
                        #
                        # **不记进 `unavailable`**：那是**配置状态**（没选模型），不是
                        # "这个库这次坏了"——把它混进 `decision` 的降级名单，会让
                        # "去设置里选个模型"与"去修那个嵌入端点"这两件完全不同的事
                        # 长得一模一样。告警也照旧只打一次（见 `_unconfigured_warned`）。
                        if not self._unconfigured_warned:
                            logger.warning("未配置嵌入模型，检索跳过向量通道，只做全文检索")
                            self._unconfigured_warned = True
                        continue
                    except EmbeddingError as exc:
                        # **一个库的嵌入服务坏掉，不该把整次跨库检索带走**（v0.65）：
                        # 超时 / 5xx / 凭据不对都是**这个库的向量通道这次用不了**，
                        # 而它的全文通道照旧参与召回、跨库融合照旧——那一路本来就不
                        # 依赖嵌入服务。实测的现场：7 个库里只有 1 个用 WeMM，它连不上
                        # 时整次检索全灭（调用方连"别的库查得到"这部分也拿不到）。
                        #
                        # 子类必须排在前面判（`EmbeddingNotConfiguredError` 是
                        # `EmbeddingError` 的子类）：两件事的处置不同，见上一条。
                        #
                        # 告警**带 kb_id**（不带就不知道去修哪个库）、**带原因**，
                        # 但**不打查询文本**——查的是用户自己的内容。
                        reason = str(exc).strip() or "嵌入服务调用失败"
                        unavailable[kb_id] = reason
                        logger.warning(
                            "知识库 %s 的嵌入服务不可用，跳过它的向量通道：%s", kb_id, reason
                        )
                        continue
                    embedded[id(embedder)] = vector
            # 有停用文档的库才 2 倍超采：KNN 扫描时没法按"文档是否停用"过滤，
            # 停用的命中会在下游 _materialize 被裁掉——多召回一倍作补偿。
            # 没有停用文档时保持原深度（candidate_k 语义不漂，也省一次放大）。
            #
            # **与 `store_filter` 是两回事**：那一份是调用方给的过滤条件，在 SQL 里
            # 已经裁掉了（连名额都不占）；这一份说的是"停用的文档"，它仍然只能在下游裁
            # ——停用是**用户随时会翻回来的状态**，而不是这次检索的范围。
            for match in self._stores.vectors.search(
                kb_id,
                query_vector=vector,
                top_k=request.candidate_k * recall_factor,
                filter=store_filter,
            ):
                # 同一条 chunk 不应出现在多个库，但真出现时取更近的那个距离
                if match.chunk_id not in best or match.distance < best[match.chunk_id]:
                    best[match.chunk_id] = match.distance

        ranked = sorted(best.items(), key=lambda pair: (pair[1], pair[0]))
        scores = {chunk_id: similarity_from_distance(distance) for chunk_id, distance in ranked}
        return [chunk_id for chunk_id, _ in ranked], scores, unavailable

    def _embedder_for(self, kb_id: str) -> EmbeddingProvider:
        """这个库该用哪个嵌入实现：显式选了模型就用它，否则全局默认。"""
        if self._embedders is None:
            return self._embedder
        kb = self._stores.meta.get_knowledge_base(kb_id)
        if kb is None:
            return self._embedder
        return self._embedders.for_kb(kb)

    def _fulltext_channel(
        self, request: RetrievalQuery, store_filter: SearchFilter | None = None
    ) -> tuple[list[str], dict[str, float]]:
        scored: dict[str, float] = {}
        for kb_id in request.kb_ids:
            hits = self._stores.fulltext.search(
                query=request.query,
                top_k=request.candidate_k,
                kb_id=kb_id,
                filter=store_filter,
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
        stats_floor: float | None = None,
    ) -> tuple[list[RetrievalHit], int]:
        if not fused:
            return [], 0

        chunk_ids = [chunk_id for chunk_id, _, _, _ in fused]
        chunks = {chunk.chunk_id: chunk for chunk in self._stores.meta.get_chunks(chunk_ids)}
        documents = self._documents_of(chunks.values())

        top_score = fused[0][1] or 1.0
        threshold = request.score_threshold
        # 相关度下限（每库一对）：按模型标定，见 `MIN_VECTOR_SCORE_BY_MODEL` 与 `coverage.py`
        floors = self._relevance_floors(request, embedders, stats_floor)
        # **这次检索有没有语义证据**：向量通道一条候选都没返回时（没配嵌入模型、
        # 或选了纯关键词那一档）判定层不下结论——理由见 `_passes_relevance`
        semantic = bool(raw.get(VECTOR_CHANNEL))
        # 查询实词**懒算**：正常查询（真余弦 0.60+）一条都不会走到词面那一支，
        # 不该为它们多切一次词
        terms: tuple[str, ...] | None = None

        hits: list[RetrievalHit] = []
        # **`filtered_out` 数的是"这一步之后丢掉的"**（脏数据 / 禁用 / 阈值 / 相关度下限）。
        # v0.64 起元数据过滤在 SQL 里就生效了，被它挡掉的块**压根没进候选**、也就数不到
        # ——所以"过滤很严时这个数是 0、结果却是空的"是正常现象，不是漏报。
        # 字段语义没变（仍是"有几条被丢掉了"），变的只是过滤发生的位置。
        filtered_out = 0
        for chunk_id, score, channels, ranks in fused:
            chunk = chunks.get(chunk_id)
            if chunk is None:
                # chunk 已被删除但索引还没清干净：跳过而不是崩
                filtered_out += 1
                continue

            if chunk.disabled:
                # 用户手动禁用的块（）。**在检索侧过滤而不是删向量**：
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
            if not _passes_chunk_filters(chunk, request.filters):
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
        self,
        request: RetrievalQuery,
        embedders: dict[str, EmbeddingProvider] | None = None,
        stats_floor: float | None = None,
    ) -> dict[str, tuple[float, float]]:
        """这次检索各库的（余弦下限, 词面覆盖下限）。

        ``None`` = 按标定默认，``0`` = 显式关闭（调用方能关掉整套判定）。

        **默认整套跟着模型标定走**：余弦的尺度是模型属性，没标定过的模型上我们说不出
        "多少算相关"，此时连词面那一半也一起关掉——它是"或"里的第二条，单独留着只会
        在没有语义判定兜底时把召回砍掉一截。这正是"宁可少拦"的落点：本机（bge-m3）
        两条件都开，别的模型保持既有行为，要收紧请显式传参。

        ``stats_floor`` 是这次检索的**统计窗口下沿**（``search`` 解析一次后传下来；
        直接调本方法的用例不传也行——那时按默认窗口走，语义一致）。

        ``embedders`` 是这次检索的**每库解析一次**的缓存（见 ``_embedder_cached``）：
        这里要的只是"这个库用的是哪个模型"（查标定表），但它与向量通道要的是**同一个
        对象**——分开解析会让每个库多一次 resolver 往返（用例盯着这个次数）。
        """
        explicit_vector = request.min_vector_score
        explicit_coverage = request.min_term_coverage
        # 窗口由 ``search`` 解析好传下来；直接调本方法（用例）时可省，
        # 那时按请求里那两项（含显式下限）重新算一遍——两种调用不能有两种窗口
        window = effective_stats_window(
            stats_floor if stats_floor is not None else request.stats_floor,
            explicit_vector,
        )
        floors: dict[str, tuple[float, float]] = {}
        for kb_id in request.kb_ids:
            vector = (
                calibrated_vector_floor(self._embedding_model_id(kb_id, embedders))
                if explicit_vector is None
                else max(0.0, explicit_vector)
            )
            if vector > 0.0:
                # **把窗口放宽到统计下沿**（v0.54 起；v0.55 起**无条件**，原来是
                # `if request.stats_floor is not None` 才做）。放宽不是顺手为之，
                # 它和"报出分布"是同一件事的两半：相关度下限（bge-m3 0.531）会把
                # "可能相关"的那一段也剪掉，而判断契合程度靠的正是"这条答案周围围着
                # 多少噪声"——窗口被剪掉了，分布就只剩答案那一小撮，``baseline`` 无从比较，
                # ``fit`` 永远是 strong/weak、永远判不出 none（"资料里没有"就说不出口）。
                #
                # 只放宽、不收紧：``min`` 只会把线往下拉。显式给了 ``min_vector_score``
                # 时窗口就是那个数（见 ``effective_stats_window``），这一行于是是个恒等式
                # ——留着它是为了让"窗口"这一处只有一个说法。
                vector = min(vector, window)
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


def _store_filter(filters: MetadataFilter | None) -> SearchFilter | None:
    """把调用方的过滤条件翻成存储层能下推的那一份；``None`` = 一件要筛的事都没有。

    **为什么要翻而不是直接传 ``MetadataFilter``**：``storage/`` 不得依赖 ``services/``
    （分层规则 3.3 的分层纪律），而过滤又偏偏只有在存储层做才有意义——见
    ``SearchFilter`` 与 ``VectorStore.search`` 的说明。翻译是逐字段的搬运，两边的
    字段名与语义**刻意保持同名**：多一层改名就多一处会漂的地方。

    ``is_empty()`` 的一侧在这里折成 ``None``：空条件意味着"连 JOIN 都不加"，
    那正是"不带过滤时查询逐字不变"的前提。
    """
    if filters is None or filters.is_empty():
        return None
    return SearchFilter(
        document_ids=filters.document_ids,
        source_kinds=filters.source_kinds,
        created_after=filters.created_after,
        created_before=filters.created_before,
        page_from=filters.page_from,
        page_to=filters.page_to,
        heading_contains=filters.heading_contains,
    )


def _passes_filters(document: DocumentRecord | None, filters: MetadataFilter | None) -> bool:
    """**兜底守卫**：文档级条件（``document_ids`` / ``source_kinds`` / 创建时间）。

    主要机制已经不在这一层了（v0.64 起条件在 SQL 里就生效，见 ``_store_filter``），
    留着它守的是"下推不完整"这一类情况：新加的过滤维度只写进了 ``MetadataFilter``
    而忘了加进 ``SearchFilter`` 的翻译、或将来某个仓储实现没有能力在候选生成前过滤。
    真拦到东西说明下推漏了——**宁可少给一条，也不能把用户明确排除掉的文档交出去**。

    两条纪律与从前一致：拿不到文档记录时（脏数据）判不通过；条件为"没给"时放行。
    """
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


def _passes_chunk_filters(chunk: ChunkRecord, filters: MetadataFilter | None) -> bool:
    """兜底守卫的另一半：**块自己的**条件（``page_from`` / ``page_to`` / ``heading_contains``）。

    与 ``_passes_filters`` 同一分工（下推为主、这里是最后一道），拆成两个函数只是因为
    它们读的记录不同：那三个条件住在 ``chunk`` 上，而 ``_passes_filters`` 拿的是文档。

    **没有页码的块被页码条件排除**，与 SQL 那一侧（``page >= %s`` 对 ``NULL`` 不为真）
    **必须同口径**：同一份条件在两条路径上给出不同答案，就是"下推生效与否结果不一样"
    这类最难查的 bug。``heading_path`` 同理。大小写不敏感这一半用 ``casefold``——
    它比 SQL 的 ``lower`` 更狠一点，方向上只会"多拦"、不会放行。
    """
    if filters is None or filters.is_empty():
        return True
    if filters.page_from is not None and (chunk.page is None or chunk.page < filters.page_from):
        return False
    if filters.page_to is not None and (chunk.page is None or chunk.page > filters.page_to):
        return False
    if filters.heading_contains:
        heading = chunk.heading_path or ""
        if filters.heading_contains.casefold() not in heading.casefold():
            return False
    return True
