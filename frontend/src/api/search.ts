/**
 * 检索接口（对应后端 POST /api/v1/search）。
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
  rerank?: boolean
  filters?: SearchFilters
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
