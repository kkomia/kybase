/**
 * 检索接口（对应后端 POST /api/v1/search）。
 *
 * 响应**判定在前、原文在后**（v0.55 起）：`summary` 一句话说清候选多少、收敛后留了
 * 几条、契合程度如何；`distribution` 与 `decision` 是那句话的两个数据来源
 * （前者是命中分布，后者是"实际按什么收敛的"）；`hits` 才是原文，排在它们后面。
 * 调用方要显示的是这批命中**是什么状态**，原文是它的下文。
 *
 * `embedding_is_development=true` 时必须提示用户"向量召回不可信"：
 * 无 API Key 时后端用确定性哈希兜底，只有词面重叠、没有语义，
 * 此时真正可信的信号是 BM25（架构 6.2）。
 *
 * **一页不是全部**（v0.63 起可翻页）：`keep` 现在只管"这一页几条"（上限从 20 抬到 50），
 * `top_k` 是这次能翻到的总量（上限也是 50），`next_offset` 指下一页的起点。判定
 * （`summary` / `distribution` / `decision`）在同一个池子的每一页上逐字相同——
 * 调用方正是靠它决定要不要接着翻。
 */

import { request } from './client'

/**
 * 检索范围的条件（后端 `MetadataFilterIn`，字段说明见 `./schema.d.ts`）。
 *
 * **条件在候选生成之前生效**（下推到 SQL），不是事后筛：候选池本来就小了，所以
 * "这次命中少"是过滤的直接后果。响应里的 `filtered_out` 因此恒为 0——被条件挡掉的块
 * 压根没进候选，调用方不能再拿它当"筛掉几条"报账。
 *
 * 每项都可选、`None`/空 = 不设限。两条**排除性**的取舍照实写在字段上：
 * 给了 `page_from`/`page_to` 或 `heading_contains` 时，`page` / `heading_path`
 * 为 `NULL` 的块（`.md`、Office 来源）会被整体排除——这是取舍不是故障。
 */
export interface SearchFilters {
  /** 只在这些文档里查（"只查某本书"）。 */
  document_ids?: string[]
  source_kinds?: string[]
  /** 文档入库时间的下界（含边界）。 */
  created_after?: string
  /** 文档入库时间的上界（含边界）。 */
  created_before?: string
  /** 只要 `page >= page_from` 的块（"某章"，按页码）。没有页码的块一条都取不到。 */
  page_from?: number
  /** 只要 `page <= page_to` 的块。 */
  page_to?: number
  /** 标题路径包含这个子串（大小写不敏感），如 `"第3章"`。标题为空的块一条都取不到。 */
  heading_contains?: string
}

export interface SearchRequest {
  query: string
  kb_ids: string[]
  /**
   * 这次**能翻到的总量**（1..50，默认 8）。
   *
   * 它不是"这一次返回几条"——那个数由 `keep` 说。从前两者是同一个数，v0.63 拆开：
   * `top_k` 封顶收敛后剩下的那批候选（翻页翻的就是这个池子），上限与 MCP `search`
   * 广告的是同一个常量。
   */
  top_k?: number
  mode?: 'hybrid' | 'vector' | 'fulltext'
  candidate_k?: number
  score_threshold?: number
  /**
   * **这一页几条**（1..50）。
   *
   * **v0.63 起它是页大小，不是总数**：一共能翻到多少由 `top_k` 封顶，下一页的起点
   * 在响应的 `next_offset` 里。`offset=0` 时与从前一致（返回的就是前 `keep` 条）。
   *
   * 不给时由后端**按分布建议**（`distribution.suggested`）：那是"这批命中该怎么读"
   * 的判断，调用方自己报一个数只会覆盖它。
   */
  keep?: number
  /** 相关度下限（0..1）：比的是余弦相似度，不是融合分。 */
  min_score?: number
  /** 每篇文档最多留几条（1..10）——治"同一个问题被同一本手册答好几遍"。 */
  per_doc?: number
  /**
   * 翻页的起点（≥0）：跳过前 N 条**幸存者**（收敛之后留下的那一批）。
   *
   * 负数不会报错，后端折成 0（顺手给的调节量，不值得一次 422）；越过末尾也不是错，
   * 回空 `hits` 加 `next_offset: null`。
   */
  offset?: number
  rerank?: boolean
  filters?: SearchFilters
}

/** 拟合度：`strong` 很对得上 / `weak` 只对上一部分 / `none` 资料里没有 / `unknown` 判不了。 */
export type SearchFit = 'strong' | 'weak' | 'none' | 'unknown'

/** 程序建议的收敛参数（`distribution.suggested`）。 */
export interface SearchSuggested {
  keep: number
  /** `null` = 不建议再按分数切。 */
  min_score: number | null
  /** `null` = 不限每篇条数。 */
  per_doc: number | null
}

/** 分数带：只有向量分可比、且窗口里有候选时才给。 */
export interface SearchScores {
  max: number
  p90: number
  p75: number
  median: number
  p25: number
  min: number
  /** `max - min`：分布有多"平"。带宽窄 = 这批东西差不多，切哪都一样。 */
  band: number
}

/**
 * 兜底阈值之上那批命中的分布（与 MCP `search` 工具的 `distribution` 同一形状）。
 *
 * 判据是 `fit`：`floor`（统计窗口下沿）与 `baseline`（标定的噪声天花板）只是它的依据。
 * `basis="none"` = 这次没有向量分（没配嵌入模型 / 纯关键词档）——那时只有条数可信，
 * 契合程度判不了（`fit=unknown`）。
 */
export interface SearchDistribution {
  floor: number
  baseline: number
  basis: string
  /** 兜底阈值之上的候选条数。 */
  count: number
  /** 其中高于基线的条数（"真的对得上"的那一批）。 */
  above_baseline: number
  fit: SearchFit
  suggested: SearchSuggested
  /** 给调用方看的一句话：这批命中现在该怎么读。 */
  note: string
  scores?: SearchScores
  /** 命中落在几篇文档上（窗口里有候选时才给）。 */
  documents?: number
  /** 条数最多的前几篇 `[文档名, 条数]`——冗余一眼可见。 */
  per_document?: [string, number][]
}

/**
 * 这次**实际**按什么收敛的。
 *
 * 用户问"为什么只回了 3 条"时，答案必须是一个数加一个来源
 * （`keep` + `decided_by`），而不是一句"系统自动"。
 */
export interface SearchDecision {
  /** 实际生效的**一页条数**上限（v0.63：它不随 `offset` 变，翻页也不改它）。 */
  keep: number
  min_score: number | null
  per_doc: number | null
  /** 这些参数是调用方给的（`model`）还是按分布建议走的（`suggested`）。 */
  decided_by: 'model' | 'suggested'
  fit: SearchFit
  /** 收敛砍掉的条数。 */
  dropped: number
  /**
   * 返回的命中里**没被 `min_score` 切**的条数：它们没有余弦可比（词面通道捞上来的），
   * 所以那一刀对它们不成立。
   *
   * 与 `dropped` 是一对：`dropped` 说切掉了几条，它说有几条**没被切**、以及为什么。
   * 缺了它，"阈值提到 0.99 却回来一批 `similarity: null`"就只剩"看不懂"这一种解释。
   */
  unscored_kept: number
}

export interface SearchHit {
  chunk_id: string
  document_id: string
  document_name: string | null
  knowledge_base_id: string
  text: string
  /**
   * **融合分**（RRF 家族：`1/(k+rank)` 量级）：只反映名次、随排名走。
   *
   * 向量档第 1 名恒为 0.0164、hybrid 档第 1 名恒为 0.0328——**它不是相似度**，
   * 也不该拿来跟相关度下限比（那是 `similarity` 的活）。
   */
  score: number
  /** **真实余弦相似度**（向量通道的原始分）；`null` = 这条只被词面通道捞到。 */
  similarity: number | null
  /** 这条是哪条通道捞上来的：`"vector"` / `"fulltext"`（就是 `raw_scores` 的键名）。 */
  basis: string
  /**
   * 词面（全文）通道的原始分，**与 `similarity` 不同量纲、不可互相比较**（越大越相关）；
   * `null` = 这条不是词面捞上来的。交出来是让调用方自己筛：相关度下限只认余弦。
   */
  lexical_score: number | null
  page: number | null
  heading_path: string | null
  image_ids: string[]
  channels: string[]
  ranks: Record<string, number>
  raw_scores: Record<string, number>
  rerank_score: number | null
}

export interface ChannelStat {
  channel: string
  count: number
  elapsed_ms: number
}

export interface SearchResponse {
  /**
   * 一行中文判定：候选多少、收敛后留了几条、契合程度如何。
   *
   * **判定在前**要照原样显示：它是这个服务的产品口径，也是"资料里没有"这句最要紧的话
   * 唯一说得准的地方（空查询 / 没有库那种提前返回是空串）。
   *
   * 同一批候选的每一页上它逐字相同（翻页不改判定）。
   */
  summary: string
  /** 兜底阈值之上的命中分布；空查询 / 没有库时是 `null`。 */
  distribution: SearchDistribution | null
  /** 这次实际按什么收敛的；与 `distribution` 同去同留。 */
  decision: SearchDecision | null
  /** **这一页**的命中（一共能翻到多少由 `top_k` 封顶，见 `next_offset`）。 */
  hits: SearchHit[]
  mode: string
  reranked: boolean
  filtered_out: number
  /** 这一页从第几条幸存者开始（**回显实际生效的那个数**：请求负数时折成 0）。 */
  offset: number
  /** 下一页的起点；`null` = 没有更多了（空页也是 `null`）。照它填回 `offset` 继续翻。 */
  next_offset: number | null
  stats: ChannelStat[]
  /** 为假时本次只做了全文通道，界面要如实说明。 */
  embedding_configured: boolean
  embedding_is_development: boolean
}

export function search(payload: SearchRequest): Promise<SearchResponse> {
  return request('/search', { method: 'POST', body: JSON.stringify(payload) })
}
