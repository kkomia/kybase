/**
 * 文档接口（对应后端 /api/v1/knowledge-bases/{kb}/documents 与 /documents/{id}）。
 *
 * `stage` 是后端流水线状态机（架构 §4），前端只做展示映射，不自己发明状态值。
 */

import { request, upload } from './client'
import type { components } from './schema'

/**
 * 处理阶段与来源：**取值直接取后端 schema**（`./schema.d.ts`）。
 *
 * 以前这两个联合类型是手抄的，值集合与后端一一对应——但"对应"靠的是人记得同步。
 * 别名之后后端加一个阶段，前端的 `switch`/映射表会立刻提示漏了分支，
 * 而不是安静地把新阶段显示成空白。
 */
export type DocumentStage = components['schemas']['DocumentStage']
export type DataSourceKind = components['schemas']['DataSourceKind']

type DocumentOut = Required<components['schemas']['DocumentOut']>
type DocumentProgressOut = Required<components['schemas']['DocumentProgressOut']>

/**
 * 列表行上的文档：契约来自后端的 OpenAPI（见 `conversations.ts` 头注的三条约定）。
 *
 * `original_kind` **显式收窄**：schema 里它是 `string`（后端按 content-kind 判的），
 * 而界面只认那几种渲染器。收窄在这里是有用的——`PreviewKind` 是"这页能不能渲染"
 * 的判断依据，放成 string 会让每个用它的地方自己再判一遍。
 */
export type DocumentSummary = Omit<DocumentOut, 'original_kind'> & {
  original_kind: PreviewKind
}

/**
 * 列表行上那条分段进度所需要的信息（§12.115）——契约同样来自后端。
 *
 * 字段含义（为什么**没有百分比**、`stalled` 为什么是最可信的那条判据等）
 * 写在 `./schema.d.ts` 里带过来的说明中，后端改一处两边都变。
 */
export type DocumentProgress = DocumentProgressOut

export interface DocumentPart {
  id: string
  part_index: number
  page_start: number
  page_end: number
  stage: DocumentStage
  error: string | null
}

export interface DocumentChunk {
  chunk_id: string
  document_id: string
  ordinal: number
  text: string
  heading_path: string | null
  page: number | null
  image_ids: string[]
  /** 被禁用的块不再参与检索，但仍在库里（§G3）。 */
  disabled: boolean
  /** 入库时为这一段生成的问题（v23）。只读展示——用来判断"出题质量如何"。 */
  questions: string[]
}

export interface ChunkList {
  items: DocumentChunk[]
  /** 该文档的切块总数；`items` 可能被 limit 截断，界面必须能说清"这是前 N 块"。 */
  total: number
}

export interface UploadAccepted {
  document: DocumentSummary
  is_duplicate: boolean
  task_id: string | null
}

/** 一页文档：``total`` 是这套筛选条件下的总数，不是 ``items`` 的长度。 */
export interface DocumentListPage {
  items: DocumentSummary[]
  total: number
  limit: number
  offset: number
}

/**
 * 文档列表每页取几篇。
 *
 * 后端默认 50、上限 200；这里取 **20**：一屏大致能放下十几行，
 * 20 篇是"多翻一页"与"一页塞满到看不见页码"之间的折中
 * （页码另有 sticky 兜底，始终贴在屏幕底部，见 KnowledgeBaseView 的 `.pager`）。
 * 前端固定用这一个值，翻页的 ``offset`` 才与"第几页 × 每页几篇"对得上。
 */
export const DOCUMENT_PAGE_SIZE = 20

export interface DocumentListFilter {
  /** 只看这个目录；与 ``root`` 互斥。 */
  folderId?: string
  /** 只看未归档（根目录）的文档。 */
  root?: boolean
  /** 按文件名模糊搜（大小写不敏感）。 */
  q?: string
  /** 只保留这个流水线阶段。 */
  stage?: DocumentStage
  /** 只保留这个来源类型。 */
  sourceKind?: DataSourceKind
  /** 分页：取几篇（不传 = 后端默认 50）。 */
  limit?: number
  /** 分页：跳过几篇。 */
  offset?: number
}

export function listDocuments(
  kbId: string,
  filter: DocumentListFilter = {},
): Promise<DocumentListPage> {
  const params = new URLSearchParams()
  if (filter.folderId) params.set('folder_id', filter.folderId)
  else if (filter.root) params.set('root', 'true')
  if (filter.q) params.set('q', filter.q)
  if (filter.stage) params.set('stage', filter.stage)
  if (filter.sourceKind) params.set('source_kind', filter.sourceKind)
  if (filter.limit !== undefined) params.set('limit', String(filter.limit))
  if (filter.offset !== undefined) params.set('offset', String(filter.offset))
  const query = params.toString()
  return request(`/knowledge-bases/${kbId}/documents${query ? `?${query}` : ''}`)
}

/** 把文档移进目录；``folderId=null`` 表示移回根目录（v13）。 */
export function moveDocument(
  documentId: string,
  folderId: string | null,
): Promise<DocumentSummary> {
  return request(`/documents/${documentId}/folder`, {
    method: 'PATCH',
    body: JSON.stringify({ folder_id: folderId }),
  })
}

export function getDocument(documentId: string): Promise<DocumentSummary> {
  return request(`/documents/${documentId}`)
}

export function listDocumentParts(documentId: string): Promise<{ items: DocumentPart[] }> {
  return request(`/documents/${documentId}/parts`)
}

/**
 * 处理进度时间线（§12.115 的「处理明细」）。
 *
 * 与列表行上的 `progress` 是**同一份数据的两种粒度**：列表要"第几步"，
 * 抽屉要"每一步各花了多久、重试过几次、在哪一步停的"。
 */
export interface TimelineStep {
  key: string
  label: string
  status: 'done' | 'running' | 'pending' | 'failed' | 'canceled'
  duration_ms: number
  /** 进入过几次。>1 = 重试或重新摄入过——"卡住"与"反复重试"要看的东西不同。 */
  visits: number
  error: string | null
}

export interface DocumentTimeline {
  document_id: string
  status: 'running' | 'done' | 'failed' | 'canceled'
  /** 当前第几个环节（1-based；失败时是**停下那一步**）。 */
  current_index: number
  /** 一共几个环节。 */
  step_total: number
  /** 总耗时（毫秒），含重试与重新摄入。 */
  total_ms: number
  steps: TimelineStep[]
  /** 租约已过期 = 没有 worker 在续约。唯一能确定说"卡住"的判据。 */
  stalled: boolean
}

export function getDocumentTimeline(documentId: string): Promise<DocumentTimeline> {
  return request(`/documents/${documentId}/timeline`)
}

/** 切块正文（文档详情页的预览）。limit 只截断 items，total 始终是全量。 */
export function listDocumentChunks(documentId: string, limit = 5): Promise<ChunkList> {
  return request(`/documents/${documentId}/chunks?limit=${limit}`)
}

/**
 * 切块的人工干预（调研报告 G3）——改正文、禁用、删除。
 *
 * **刻意用 `(documentId, ordinal)` 而不是 `chunk_id` 寻址**：chunk_id 形如
 * `doc_xxx#00000`，里面的 `#` 是 URL 的片段分隔符。直接拼进路径会被截断，
 * 而截断后的路径恰好落到 `/documents/{id}` 上，后端报的是"文档不存在"——
 * 一个与真正原因毫不相干的错误（实测踩到，后端也留了用例把这个坑钉住）。
 * 文档 ID 与序号都是 URL 安全的。
 */
function chunkPath(documentId: string, ordinal: number): string {
  return `/documents/${documentId}/chunks/by-ordinal/${ordinal}`
}

export function updateChunk(
  documentId: string,
  ordinal: number,
  text: string,
): Promise<DocumentChunk> {
  return request(chunkPath(documentId, ordinal), {
    method: 'PATCH',
    body: JSON.stringify({ text }),
  })
}

/** 禁用/恢复。用 PUT 语义：重复设置同一个状态不会累积副作用。 */
/** 删除会波及什么（M6 / T6.3）。**先看清单再动手**，否则二次确认没有意义。 */
export interface ImpactReport {
  kind: string
  id: string
  name: string
  documents: number
  chunks: number
  parts: number
  size_bytes: number
  running_tasks: number
  document_names: string[]
  /** 能否从回收站恢复。知识库级删除不可恢复，界面据此换警示文案。 */
  restorable: boolean
}

export function getDocumentImpact(documentId: string): Promise<ImpactReport> {
  return request(`/documents/${documentId}/impact`)
}

/**
 * 删除文档。
 *
 * 返回回收站条目：原文进了回收站、7 天内可恢复，而切块与向量已立即清除。
 * 所以删除后**立刻搜不到**，这一点要在界面上说清。
 */
export function deleteDocument(documentId: string): Promise<{
  id: string
  document_id: string
  expires_at: string
}> {
  return request(`/documents/${documentId}`, { method: 'DELETE' })
}

/** 回收站条目。 */
export interface TrashEntry {
  id: string
  document_id: string
  kind: string
  expires_at: string
  created_at: string | null
}

export function listTrash(): Promise<{ items: TrashEntry[] }> {
  return request('/trash')
}

/** 恢复：**需要重新摄入**才有检索能力，所以后端返回 202 与任务 id。 */
export function restoreFromTrash(
  trashId: string,
): Promise<{ document_id: string; task_id: string }> {
  return request(`/trash/${trashId}/restore`, { method: 'POST' })
}

export function dropTrash(trashId: string): Promise<void> {
  return request(`/trash/${trashId}`, { method: 'DELETE' })
}

export function setChunkDisabled(
  documentId: string,
  ordinal: number,
  disabled: boolean,
): Promise<DocumentChunk> {
  return request(`${chunkPath(documentId, ordinal)}/disabled`, {
    method: 'PUT',
    body: JSON.stringify({ disabled }),
  })
}

export function deleteChunk(documentId: string, ordinal: number): Promise<void> {
  return request(chunkPath(documentId, ordinal), { method: 'DELETE' })
}

export function uploadDocument(
  kbId: string,
  file: File,
  folderId?: string,
): Promise<UploadAccepted> {
  const query = folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ''
  return upload(`/knowledge-bases/${kbId}/documents${query}`, file)
}

export function reprocessDocument(documentId: string): Promise<UploadAccepted> {
  return request(`/documents/${documentId}/reprocess`, { method: 'POST' })
}

/** 下载格式：原文件（默认）或解析产物 Markdown。 */
export type DownloadFormat = 'original' | 'markdown'

/**
 * 阅读视角的内容形态。
 *
 * `pdf` / `image` 交给浏览器原生渲染（一条签名链接就够）；`docx` / `pptx` / `excel`
 * 要在前端用库渲染——浏览器不会原生显示它们。`binary` = 只能下载。
 */
export type PreviewKind = 'markdown' | 'pdf' | 'image' | 'docx' | 'pptx' | 'excel' | 'binary'

/** 可以在这页里渲染出来的原件类型（前端库支持的那几种）。 */
export const RENDERABLE_KINDS: readonly PreviewKind[] = ['pdf', 'image', 'docx', 'pptx', 'excel']

export interface DocumentPreview {
  kind: PreviewKind
  filename: string
  /** 文本类内联返回（Markdown / 纯文本）。 */
  text: string | null
  /** 需要自己渲染的类型给一条签名链接。 */
  url: string | null
  expires_at: number | null
  /**
   * 原件本身的类型（与本次返回的 kind 无关）。
   *
   * 界面据此决定要不要给「原文版式 / 解析文本」这个切换：`binary` 或 `null`
   * 表示原件没有可渲染的版式，就别给用户一个点开是空的入口。
   */
  original_kind: PreviewKind | null
}

/**
 * 取「阅读」视角。
 *
 * 与切块预览是两个视角：切块回答"解析成了什么"（调试用，等宽带块号），
 * 这个回答"原文长什么样"（日常用，渲染件）。
 *
 * `source='original'` 强制看原件版式：默认（auto）在解析完成后会给归一化文本，
 * 但用户点「原文版式」时要的是那个文件本身。
 */
export function getDocumentPreview(
  documentId: string,
  source: 'auto' | 'original' = 'auto',
): Promise<DocumentPreview> {
  const query = source === 'original' ? '?source=original' : ''
  return request(`/documents/${documentId}/preview${query}`)
}

export interface DownloadUrl {
  /** **相对路径**：对外域名只有部署时才知道，后端不猜。 */
  url: string
  /** Unix 秒。到期后这条链接就失效，要重新签发。 */
  expires_at: number
  format: DownloadFormat
}

/**
 * 取一条短期下载链接。
 *
 * 为什么不直接拼 `/documents/{id}/content`：那个地址需要签名才放行（无永久直链），
 * 而签名只有后端签得出来。
 */
export function getDownloadUrl(
  documentId: string,
  format: DownloadFormat = 'original',
): Promise<DownloadUrl> {
  return request(`/documents/${documentId}/download-url?format=${format}`)
}

/**
 * 触发浏览器下载。
 *
 * 走一个临时 `<a download>` 而不是 `window.open`：前者不会留下一个可能被拦的弹窗，
 * 也不会把 URL 顶到地址栏（里面带着签名）。用完立即移除，避免在 DOM 里留下痕迹。
 */
export async function downloadDocument(
  documentId: string,
  format: DownloadFormat = 'original',
): Promise<void> {
  const { url } = await getDownloadUrl(documentId, format)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

/**
 * 停用 / 恢复检索。只动标记：切块与向量保留，恢复零成本。
 */
export function setDocumentDisabled(
  documentId: string,
  disabled: boolean,
): Promise<DocumentSummary> {
  return request(`/documents/${documentId}/disabled`, {
    method: 'PATCH',
    body: JSON.stringify({ disabled }),
  })
}

/** 重命名：只改显示名，不重跑解析。 */
export function renameDocument(documentId: string, name: string): Promise<DocumentSummary> {
  return request(`/documents/${documentId}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  })
}

/**
 * 取消解析：叫停还在跑的摄入。
 *
 * **不是删除**——已产出的切块与解析产物留着，之后可以重新摄入。
 */
export function cancelDocument(documentId: string): Promise<DocumentSummary> {
  return request(`/documents/${documentId}/cancel`, { method: 'POST' })
}

export type DocumentBatchAction =
  'delete' | 'reprocess' | 'move' | 'enable' | 'disable' | 'questions'

export interface DocumentBatchItem {
  document_id: string
  ok: boolean
  error: string | null
}

export interface DocumentBatchResult {
  action: DocumentBatchAction
  succeeded: number
  failed: number
  items: DocumentBatchItem[]
}

/**
 * 批量删除 / 重新摄入。
 *
 * **返回逐条结果**：批量操作里"10 篇删掉 9 篇"是正常结果，界面要能指出
 * 剩下那篇为什么没成。所以这个调用不会因为个别失败而 reject。
 *
 * `all = true`（v17）表示**整库**：服务端自己解析目标集合（会跳过还在跑的文档），
 * 传进来的 `documentIds` 被忽略。它服务的是"改了切分参数要整库重跑"——
 * 界面不必先翻页取一遍 id 再回传。
 */
export function batchDocuments(
  kbId: string,
  action: DocumentBatchAction,
  documentIds: string[],
  folderId: string | null = null,
  all = false,
): Promise<DocumentBatchResult> {
  return request(`/knowledge-bases/${kbId}/documents/batch`, {
    method: 'POST',
    body: JSON.stringify({ action, document_ids: documentIds, folder_id: folderId, all }),
  })
}
