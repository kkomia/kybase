/**
 * 知识库 Wiki 接口（对应后端 `/api/v1` 的 wiki 一组路由）。
 *
 * Wiki 是挂在某个知识库上的**一套百科式页面**：把库里已录入的内容整理成分层页面，
 * 每个要点都带原文出处。它有两种粒度，对应两组接口：
 * - **总览**（`getWiki`）：页面树 + 整体状态，页面只带标题/摘要，供左侧导航用；
 * - **单页**（`getWikiPage`）：某一页的完整正文与出处，按需拉取，避免一次传整本书。
 *
 * 生成是异步任务（后端返回 202 + `task_id`），所以界面拿到的状态里会有
 * `generating` 这一档，需要轮询总览直到它落定。字段与后端 schema 一一对应，
 * 改后端时同步改这里（与 `knowledgeBases.ts` 同一约定）。
 */

import { request } from './client'

/** 页面在导航树里的摘要。正文不在这里——树只负责"有哪些页、谁在谁下面"。 */
export interface WikiPageSummary {
  id: string
  /** 父页面 id；null = 顶层页面。`level` 与它是同一套层级信息的两种表达。 */
  parent_id: string | null
  /** 层级，从 0 开始（0 = 总览页，1 = 主题页）。后端已经算好，前端只按它缩进。 */
  level: number
  /** 同级排序序号，后端已按它排过序；前端再用它兜一次底。 */
  ord: number
  title: string
  /** 一句话摘要，列表与页头都用它。可能为空串。 */
  brief: string
  /**
   * 单页状态。整库生成时逐页落库，所以树里可能出现"有的页好了、有的页还在生成"。
   * 这是**页面级**状态，与总览的整库状态不是一回事。
   */
  status: 'ready' | 'generating' | 'failed'
  generated_at: string | null
}

/** 整库 Wiki 总览。 */
export interface WikiOverview {
  /** 这个库有没有开 Wiki（老库默认关）。关的时候整页应该给引导而不是空树。 */
  enabled: boolean
  /** 整库状态：idle 未生成 / generating 生成中 / ready 已生成 / failed 失败。 */
  status: 'idle' | 'generating' | 'ready' | 'failed'
  page_count: number
  generated_at: string | null
  /** 生成用的对话模型名。null = 还没生成过。 */
  model: string | null
  /** 最近一次失败原因。非空时界面要原样显示出来，而不是笼统说"失败了"。 */
  last_error: string | null
  pages: WikiPageSummary[]
}

/** 一条出处：正文里的 `[n]` 与它一一对应。 */
export interface WikiSource {
  /** 与正文里的 `[n]` 对应。 */
  index: number
  chunk_id: string
  document_id: string
  document_name: string
  /** 在原文里的标题路径（如「第三章 › 并发症」）。可能为空。 */
  heading_path: string | null
  /** 页码，PDF 才有；其它格式为 null。 */
  page: number | null
}

/** 单页详情。 */
export interface WikiPageDetail {
  id: string
  kb_id: string
  parent_id: string | null
  level: number
  title: string
  brief: string
  /** 正文 Markdown，含 `[n]` 引用标记（由 `renderAnswerWithCitations` 变成可点徽标）。 */
  content_md: string
  status: 'ready' | 'generating' | 'failed'
  model: string | null
  generated_at: string | null
  updated_at: string | null
  sources: WikiSource[]
}

/** 这个库的 Wiki 总览（页面树 + 状态）。 */
export function getWiki(kbId: string): Promise<WikiOverview> {
  return request(`/knowledge-bases/${kbId}/wiki`)
}

/** 单页正文与出处。 */
export function getWikiPage(pageId: string): Promise<WikiPageDetail> {
  return request(`/wiki/pages/${pageId}`)
}

/**
 * 触发整库生成/重新生成。
 *
 * 后端是**异步**的：立刻返回 202 与任务 id，真正的生成在后台跑。
 * 界面拿到 task_id 后不该傻等——重新拉一次总览拿到 `generating`，然后开始轮询。
 */
export function generateWiki(kbId: string): Promise<{ task_id: string }> {
  return request(`/knowledge-bases/${kbId}/wiki/generate`, { method: 'POST' })
}

/** 清空这个库已生成的 Wiki 页面（不删库、不动文档）。 */
export function clearWiki(kbId: string): Promise<void> {
  return request(`/knowledge-bases/${kbId}/wiki`, { method: 'DELETE' })
}
