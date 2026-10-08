/**
 * 知识库接口（对应后端 /api/v1/knowledge-bases）。
 * 字段与后端 `api/v1/schemas.py` 一一对应，改后端时同步改这里。
 */

import { request } from './client'
import type { components } from './schema'
import type { ImpactReport } from './documents'

/**
 * 知识库：契约来自后端的 OpenAPI（见 `conversations.ts` 头注的三条约定）。
 *
 * 几个字段的口径（原来写在这里、现在由后端 schema 带过来）：
 * `can_manage` / `can_write` 是**按当前登录人算的**权限位（读写两档）；
 * `document_count` 由后端 `GROUP BY` 一次算出；`embedding_model_id` 与 `embedding_dim`
 * 是建库时定下的**库属性**——不同库可以用不同嵌入模型，各自的向量空间互不污染。
 */
export type KnowledgeBase = Required<components['schemas']['KnowledgeBaseOut']>

export interface KnowledgeBaseCreate {
  name: string
  chunk_size?: number
  chunk_overlap?: number
  /** 建库时选定的嵌入模型（注册表主键）。留空 = 用服务端默认。
   *  嵌入模型是知识库属性：库内向量化之后不可更换（换模型要新建库）。 */
  embedding_model_pk?: string
  /** 分段问题生成（v19/v23）。建库时就能定，之后在「知识库设置 → 切块策略」里改。 */
  suggested_enabled?: boolean
  suggested_count?: number
  /** 出题模型；空串 / 不传 = 跟随对话模型。 */
  suggested_model_pk?: string | null
  suggested_prompt?: string
  /** 是否开启 Wiki（库形态）。不传 = 关，与后端默认一致。 */
  wiki_enabled?: boolean
}

export function listKnowledgeBases(): Promise<{ items: KnowledgeBase[] }> {
  return request('/knowledge-bases')
}

export function createKnowledgeBase(payload: KnowledgeBaseCreate): Promise<KnowledgeBase> {
  return request('/knowledge-bases', { method: 'POST', body: JSON.stringify(payload) })
}

export function getKnowledgeBase(kbId: string): Promise<KnowledgeBase> {
  return request(`/knowledge-bases/${kbId}`)
}

/**
 * 修改知识库的名称 / 简介 / 切分参数 / 推荐问题设置。**各项都可选**，只传要改的那个
 * （传 `{ description: '' }` 是清空简介；`{ suggested_model_pk: '' }` 是"跟随对话模型"）。
 *
 * 切分参数（v17）只影响**之后摄入**的文档：已切好的块不会自己变，
 * 所以界面要提示"已有文档需要重新摄入"。推荐问题设置（v19）不同，**立刻生效**。
 */
export function updateKnowledgeBase(
  kbId: string,
  patch: {
    name?: string
    description?: string
    chunk_size?: number
    chunk_overlap?: number
    suggested_enabled?: boolean
    suggested_count?: number
    suggested_model_pk?: string | null
    suggested_prompt?: string
    /** 库级提示词（v0.19）。空串 = 清除，回到只剩内置提示词。 */
    system_prompt?: string
    /** 库形态（v24）：开启后可以在 Wiki 页面用已录入内容生成页面。 */
    wiki_enabled?: boolean
  },
): Promise<KnowledgeBase> {
  return request(`/knowledge-bases/${kbId}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

/**
 * 切分参数的可用区间。**与后端 `services/chunking.py` 的常量一一对应**。
 *
 * 前端先校验是为了让用户当场看到中文原因，而不是等一个 422 回来；
 * 权威仍在后端（服务层会再校验一次，两处都改才算改完）。
 */
export const CHUNK_SIZE_MIN = 128
export const CHUNK_SIZE_MAX = 2048
export const CHUNK_OVERLAP_RATIO_MAX = 0.5

/**
 * 后端默认值（`services/chunking.py` 的 `DEFAULT_CHUNK_SIZE` / `DEFAULT_OVERLAP`）。
 * 界面上它有两个用处：新建弹窗的初值，以及滑杆轨道上那个「默认」刻度点。
 * 两处都从这里取——写死在两个组件里，迟早有一个忘了改。
 */
export const CHUNK_DEFAULT_SIZE = 512
export const CHUNK_DEFAULT_OVERLAP = 64

/**
 * 滑杆轨道上的刻度点（常用值）。`primary` = 默认值，画成强调色。
 *
 * 放这里而不是各自的组件里：新建弹窗与知识库设置用的是**同两个参数**，
 * 刻度给得不一样会让人以为它们是两回事。越界的点由 `RangeField` 丢掉——
 * 重叠的上限跟着块长走，块长调小时 128/256 必须自动消失。
 *
 * 两块长都**不标最小值**（128 / 0 那种）：线性轴上 128 与 256 只差 6.7%，
 * 新建弹窗（轨道约 400px）里两个数值标签只剩 3px 间隙，挤成一团；
 * 而轴的两端本来就是"范围"，说明文字里已经写了。最大值可以标——它离前一个点足够远。
 */
export const CHUNK_SIZE_MARKS: { value: number; primary?: boolean }[] = [
  { value: 256 },
  { value: CHUNK_DEFAULT_SIZE, primary: true },
  { value: 1024 },
  { value: CHUNK_SIZE_MAX },
]

export const CHUNK_OVERLAP_MARKS: { value: number; primary?: boolean }[] = [
  { value: 0 },
  { value: 32 },
  { value: CHUNK_DEFAULT_OVERLAP, primary: true },
  { value: 128 },
  { value: 256 },
]

/** 给定块长时，重叠的上限。默认块长（512）下就是 256。 */
export function chunkOverlapMax(size: number): number {
  return Math.max(1, Math.floor(size * CHUNK_OVERLAP_RATIO_MAX))
}

/** 删除这个知识库会波及什么（界面要在动手之前把它显示出来）。 */
export function getKnowledgeBaseImpact(kbId: string): Promise<ImpactReport> {
  return request(`/knowledge-bases/${kbId}/impact`)
}

/**
 * 删除知识库。**不可恢复**（不进回收站），返回影响清单当回执。
 */
export function deleteKnowledgeBase(kbId: string): Promise<ImpactReport> {
  return request(`/knowledge-bases/${kbId}`, { method: 'DELETE' })
}

/**
 * 分段问题生成的可用区间与默认值。**与后端 `services/suggested_questions.py`
 * 的常量一一对应**（那里是权威，这里只是让用户当场看到中文原因）。
 *
 * `SUGGESTED_COUNT_*` 指的是**每个分段生成几条**（v23 起；v22 时曾是"空状态显示几条"）。
 */
export const SUGGESTED_COUNT_MIN = 1
export const SUGGESTED_COUNT_MAX = 5
export const SUGGESTED_COUNT_DEFAULT = 3
export const SUGGESTED_PROMPT_MAX_CHARS = 2000

/**
 * 生成时用到的一篇文档摘要（**只作展示**：让用户看见这段提示词依据了什么）。
 *
 * v0.41 起没有"这篇有没有被引用"这个字段了：引用口径改成编号式（`[1]` `[2]`，
 * 与检索结果对应）之后，逐篇归属不可知——与其留一个永远为假的勾，不如删掉。
 */
export interface KBPromptSource {
  document_id: string
  name: string
  summary: string
}

/** 按文档摘要生成的提示词**草稿**（不落库，用户确认后再 PATCH 保存）。 */
export interface KBPromptDraft {
  prompt: string
  sources: KBPromptSource[]
  /**
   * 这段提示词里**仍在要求把文件名写进正文**的地方（旧口径）。
   *
   * 非空 = 模型没听那句"别把文件名写进正文"。它会与系统提示词第 5 条打架，
   * 而正文里铺一串文件名正是"回答一大半都是引用"的来源——界面据此提示核对后再保存。
   */
  filename_style_citations: string[]
}

/**
 * 让对话模型**只依据库里已生成的文档摘要**写一版库提示词（v0.19）。
 *
 * 不落库：返回草稿由用户过目、修改、再保存。它要花一次模型调用，
 * 所以是写权限的接口（能改这个库配置的人才能为它花钱）。
 */
export function generateKBPrompt(kbId: string, modelPk?: string | null): Promise<KBPromptDraft> {
  return request<KBPromptDraft>(`/knowledge-bases/${kbId}/prompt/generate`, {
    method: 'POST',
    body: JSON.stringify({ model_pk: modelPk ?? null }),
  })
}
