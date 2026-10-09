/**
 * 驾驶舱统计接口（对应 /api/v1/stats/dashboard）。
 *
 * 一个接口给全量数字：驾驶舱要同时渲染五六块图，分开请求既慢，
 * 又容易出现"上半页是新的、下半页是旧的"。
 */

import { request } from './client'

export interface ActivityPoint {
  day: string
  documents: number
  chunks: number
  tasks: number
  /**
   * 这一天的检索命中**候选数**（各库之和，v0.61）。
   *
   * 「候选」= 分布收敛**之前**那批：拿收敛后的条数当活跃度会量到"调用方要了几条"，
   * 而不是"这个库被问出了多少东西"。概览趋势图上的「检索命中」一档读的就是它。
   */
  hits: number
}

export interface KbStat {
  id: string
  name: string
  embedding_model_id: string
  embedding_dim: number
  documents: number
  chunks: number
  last_activity: string | null
}

export interface Dashboard {
  generated_at: string
  window_days: number
  total_knowledge_bases: number
  total_documents: number
  total_chunks: number
  indexed_documents: number
  failed_documents: number
  running_tasks: number
  failed_tasks: number
  storage_bytes: number
  recent_documents: number
  activity: ActivityPoint[]
  by_stage: Record<string, number>
  by_suffix: Record<string, number>
  by_source_kind: Record<string, number>
  knowledge_bases: KbStat[]
}

export function getDashboard(windowDays = 120): Promise<Dashboard> {
  return request(`/stats/dashboard?window_days=${windowDays}`)
}

// --------------------------------------------------------------------- 模型用量（G7）

/** 一档用量。 */
export interface UsageBucket {
  calls: number
  prompt_tokens: number
  completion_tokens: number
  items: number
  /** 这一档里有多少次调用**供应商没报用量**。 */
  unreported: number
  /** 这一档里有多少次调用是**我们自己估算**的。 */
  estimated: number
}

export interface UsageDay extends UsageBucket {
  day: string
}

export interface UsageKind extends UsageBucket {
  kind: string
  label: string
}

export interface UsageModel extends UsageBucket {
  model: string
}

export interface Usage {
  days: number
  total: UsageBucket
  by_day: UsageDay[]
  by_kind: UsageKind[]
  by_model: UsageModel[]
  /** 实测调用数（供应商真的回了 usage）。 */
  reported_calls: number
  /** 估算调用数（按字符数估的，主要是向量化）。 */
  estimated_calls: number
  /** 既没实测也没得估的调用数。 */
  unreported_calls: number
  /**
   * 估算出来的 token 总数（已包含在 total 里）。
   *
   * **必须单独说清**：假精度比没数字更糟——用户会拿估算值做成本判断，
   * 而它可能偏离好几倍。
   */
  estimated_tokens: number
}

/**
 * 取模型用量。
 *
 * **只有 token 与调用量，没有费用**：单价随供应商、版本、缓存命中与折扣不断变，
 * 内置价目表必然过期——而过期的价钱比不给更糟，用户会照着它做决定。
 */
export function getUsage(days = 30): Promise<Usage> {
  return request(`/stats/usage?days=${days}`)
}

// --------------------------------------------------------------- 检索命中趋势（v0.61）

/** 某一天、某个库的命中量。后端给的是**稀疏**曲线（只有真有记录的那些天）。 */
export interface SearchHitPoint {
  day: string
  /** 分布收敛**之前**这个库贡献的条数。 */
  candidates: number
  /** 收敛之后真的返回给调用方的条数。 */
  kept: number
}

/** 单个知识库在窗口内的命中情况 + 它那条稀疏曲线。 */
export interface KbSearchHits {
  kb_id: string
  name: string
  candidates: number
  kept: number
  /** 这个库在窗口内被检索了几次（一次跨两库的检索算两次）。 */
  searches: number
  series: SearchHitPoint[]
}

export interface SearchHits {
  days: number
  candidates: number
  kept: number
  searches: number
  /**
   * **只含窗口内真有记录**的库：一次都没被查过的库没有曲线可画。
   *
   * "被查了但一条没命中"的库**会**在里面（candidates=0）——"没人查过"与
   * "查了但答不上来"是两件事，那正是这份数据的一半价值。
   */
  by_kb: KbSearchHits[]
}

/**
 * 按知识库的检索命中趋势。
 *
 * **一次请求覆盖全部库**（与逐库取文档的入库趋势不同），所以它值得单独一条；
 * 卡片上第二根趋势线读的就是它，见 `features/knowledge/searchTrend.ts` 的头注。
 */
export function getSearchHits(days = 90): Promise<SearchHits> {
  return request(`/stats/search-hits?days=${days}`)
}
