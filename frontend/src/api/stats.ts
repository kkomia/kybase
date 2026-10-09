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
