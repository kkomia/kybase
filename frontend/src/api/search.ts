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
 * 此时真正可信的信号是 BM25（架构 §6.2）。
 */

import { request } from './client'

export interface SearchFilters {
  document_ids?: string[]
  source_kinds?: string[]
  created_after?: string
  created_before?: string
}

export interface SearchRequest {
  query: string
  kb_ids: string[]
  top_k?: number
  mode?: 'hybrid' | 'vector' | 'fulltext'
  candidate_k?: number
  score_threshold?: number
  /**
   * 收敛后的条数上限（1..20）。
   *
   * 不给时由后端**按分布建议**（`distribution.suggested`）：那是"这批命中该怎么读"
   * 的判断，调用方自己报一个数只会覆盖它。
   */
  keep?: number
  /** 相关度下限（0..1）：比的是余弦相似度，不是融合分。 */
  min_score?: number
  /** 每篇文档最多留几条（1..10）——治"同一个问题被同一本手册答好几遍"。 */
  per_doc?: number
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
  /** 实际生效的条数上限。 */
  keep: number
  min_score: number | null
  per_doc: number | null
  /** 这些参数是调用方给的（`model`）还是按分布建议走的（`suggested`）。 */
  decided_by: 'model' | 'suggested'
  fit: SearchFit
  /** 收敛砍掉的条数。 */
  dropped: number
}

export interface SearchHit {
  chunk_id: string
  document_id: string
  document_name: string | null
  knowledge_base_id: string
  text: string
  score: number
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
   */
  summary: string
  /** 兜底阈值之上的命中分布；空查询 / 没有库时是 `null`。 */
  distribution: SearchDistribution | null
  /** 这次实际按什么收敛的；与 `distribution` 同去同留。 */
  decision: SearchDecision | null
  hits: SearchHit[]
  mode: string
  reranked: boolean
  filtered_out: number
  stats: ChannelStat[]
  /** 为假时本次只做了全文通道，界面要如实说明。 */
  embedding_configured: boolean
  embedding_is_development: boolean
}

export function search(payload: SearchRequest): Promise<SearchResponse> {
  return request('/search', { method: 'POST', body: JSON.stringify(payload) })
}
