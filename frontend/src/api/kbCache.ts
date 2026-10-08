/**
 * 知识库元数据快照（M4 阶段 5）——页面「先画一帧」读的那一族只读接口。
 *
 * ## 它是什么
 *
 * 本机后端（边车）留了一份知识库元数据的副本（M4 方案 §2 的形状 C）。这一族
 * `/local/kb-cache/*` 是**本机回环**（个位数毫秒）：页面进页面/换库时先读它，
 * 把「上次看到的内容」摆上屏幕；页面自己那条**实时读**
 * （`api/knowledgeBases.ts` / `api/documents.ts`，会话身份直连 NAS）一落地就如实替换。
 *
 * ## 三条与实时读的关系（缺一条就会写出「快照盖掉实时结果」）
 *
 * 1. 这一族**永远只是第一帧**：调用方只在「这个视图还没有实时结果」时读它，
 *    画法与判据在 `features/knowledge/snapshot.ts`（时序规则五条见 M4 方案 §4.3）；
 * 2. 快照里的行**没有权限位**（`can_write` / `can_manage` 在本机那一层就被剥掉了）——
 *    写/管理入口因此晚一步出现，这是刻意的（决策点 D-B：宁可晚一步，也不给一个
 *    点下去 403 的入口）；
 * 3. **失败一律静默**（见最后一节）：它是「顺手快一点」的那一下，
 *    页面自己那条实时读才是权威，报错的位置在那一条线上。
 *
 * ## 为什么类型是**手写**的（不读 `schema.d.ts`）
 *
 * 与 `api/provider.ts:11-20` 同一条理由：`/local/*` 这一族**不进服务器档的 OpenAPI**
 * （`local_router` 只挂在边车那台，而 `gen_api_types.py` 生成的是服务器档那份契约），
 * 生成物里没有它们。字段名逐条对着 `backend/app/api/v1/local.py::KbCacheSnapshotOut`
 * 抄（那是形状的唯一作者），改了后端就回来改这里。
 *
 * ## 404 = 这一档没有本机后端（静默跳过）
 *
 * 服务器档（浏览器 / NAS 网页端）里这一族**根本不存在**：`/local/kb-cache/…` 一律 404
 * ——那正是「知识库就是它自己」的那一档。所以这一族的**读**从不抛：
 *
 * - **404** → 记一笔「这一档没有」，之后的调用**连请求都不发**
 *   （与 `api/provider.ts` 的 `unsupported` 同一手法）；
 * - 其余失败（边车没起来 / 响应形状不认识）→ 也返回 `null`，页面照旧骨架屏 +
 *   自己那条实时读；一次「顺手快一点」的失败不该在界面上说话。
 *
 * **唯一的例外是「清除」**（`clearKbCache`，M4 阶段 6）：那是用户在设置里明确点的动作，
 * 失败必须说出来（静默失败会让用户以为"清干净了"）——所以它抛，由调用方提示。
 */
import { requestLocal } from './client'

/* ------------------------------------------------------------------ 类型（手写） */

/** 进快照的五种资源（与后端 `services/kb_cache.CACHEABLE_RESOURCES` 同名）。 */
export type KbCacheResource = 'kb_list' | 'kb_detail' | 'doc_list' | 'document' | 'folders'

/**
 * 一份快照（`KbCacheSnapshotOut`）。
 *
 * 形状**恒定**：没有副本时也是这几个键（`available:false` + `reason`），不是 4xx/5xx
 * ——「这台还没看过它」是一个正常答案，页面据此照旧走自己那条实时读。
 */
export interface KbCacheSnapshot {
  /** 本机有没有这份内容（false 时看 `reason`：还没看过 / 超龄 / 换了地址 / 带筛选）。 */
  available: boolean
  resource: string
  /** 资源内的键（库 id / 文档 id / 视图指纹）；带筛选的那一档没有键（空串）。 */
  scope_key: string
  /** `available:false` 时为什么。**界面上不显示它**（页面照旧骨架屏）。 */
  reason: string
  /** 列表型资源的行（别的资源是空表）：`payload.items` 的视图。 */
  items: Record<string, unknown>[]
  /** 这份内容的**原样**（NAS 那边的形状）；没有副本时是 `null`。 */
  payload: Record<string, unknown> | null
  /** 内容哈希（`sha256:…`）——变没变看它。 */
  version: string
  /** `reader` / `revalidate`：这行是怎么来的（只作排障）。 */
  source: string
  /** 这份**内容**是什么时候看到的（界面上那句「上次更新于 X」）。 */
  fetched_at: string | null
  /** 最近一次**确认**（含「确认过没变」）。与上面那个是**两句话**，不许混。 */
  checked_at: string | null
  /** 上次再确认失败了：内容照旧可读，但没被确认（界面据此换成「现在连不上…」）。 */
  stale: boolean
  /** 那次失败的原因（`stale` 时才非空）。 */
  last_error: string
  /** 刚刚顺带排了一次后台再确认。 */
  revalidating: boolean
}

/**
 * 文档列表的**规格视图**（快照这一面的说法）。
 *
 * 与实时读那一条的 `DocumentListFilter` 是同一件事的两种说法：
 * 那边按 `limit` / `offset` 说，这边按 `size` / `page` 说（`offset = (page - 1) * size`）。
 * `folder` 与 `root` **互斥**（一个说「这个目录里的」，一个说「未归档的」）。
 */
export interface DocListView {
  /** 只看这个目录（NAS 那边的 `folder_id`）。 */
  folder?: string
  /** 只看未归档的。 */
  root?: boolean
  /** 第几页（从 1 起）。 */
  page?: number
  /** 一页几篇。 */
  size?: number
}

/**
 * `POST /local/kb-cache/revalidate` 的请求体：只说要再确认**哪一份**。
 *
 * **筛选参数在这里没有位置**（`q` / `stage` / `source_kind` 换不出快照键，D-D）：
 * 带它们的那一读本来就不留副本，也就没有「这一份」可以再确认。
 */
export interface KbCacheRevalidate {
  resource: KbCacheResource
  /** `kb_detail` / `doc_list` / `folders` 要给。 */
  kb_id?: string
  /** `document` 那一族要给。 */
  document_id?: string
  /** `doc_list`：只看这个目录（空 = 整个库）。 */
  folder?: string
  /** `doc_list`：只看未归档的（与 `folder` 互斥）。 */
  root?: boolean
  page?: number
  size?: number
}

/* ------------------------------------------------------------------ 视图指纹 */

/** 后端文档列表的默认页大小（`page` 默认 1），这里只作兜底。 */
export const DEFAULT_DOC_PAGE_SIZE = 50
const DEFAULT_DOC_PAGE = 1

/**
 * 文档列表**规范视图**的键：`kb_id|folder:<id|root|all>|page:<n>|size:<n>`。
 *
 * **必须与后端 `services/kb_cache.doc_list_scope_key()` 逐字相同**（连分隔符也是同一个
 * `|`）——两边任意一侧改了格式，这一份快照就永远读不中，而且**不报错**（表现是
 * 「每次都像没有副本」）。所以它是这个文件里第一条被用例逐字钉住的契约
 * （`tests/api-kb-cache.test.ts`）。
 *
 * `folder` 与 `root` 同时给时按 `folder` 算：与 `api/documents.ts::listDocuments` 的
 * `if / else if` 同一口径（那边不会同时给，这里只是不许拼出一个后端不认的串）。
 */
export function docListViewKey(kbId: string, view: DocListView = {}): string {
  const page = view.page ?? DEFAULT_DOC_PAGE
  const size = view.size ?? DEFAULT_DOC_PAGE_SIZE
  const where = view.folder ? `folder:${view.folder}` : view.root ? 'folder:root' : 'folder:all'
  return [kbId, where, `page:${page}`, `size:${size}`].join('|')
}

/* ------------------------------------------------------------------ 读一份快照 */

/** 这一档有没有这一族（一次 404 之后就不必再问了）。 */
let unsupported = false

/**
 * **这一笔被复位过几次**（`resetKbCacheSupport()` 每次 +1）——与 `api/backup.ts` 的
 * `generation`、`api/provider.ts` 的那一位、`api/sidecar.ts` 的 `probeGeneration`
 * **同形、同一条理由**（那几处的注释写得更细）。
 *
 * 这一位管的是 `unsupported` 那一笔的**晚到写入**：`requestLocal()` 的第一次 `fetch`
 * 不在调用点上（要先问壳 + 探一次活），所以"那一刻发出去的那条读"可能**在复位之后**
 * 才带着 404 回来。它一旦把 `unsupported` 记回真，**下一条用例的读就一次请求都不发了**
 * （`readQuiet` 开头那句直接返回 `null`）——表现是"某一条用例悄悄不读快照了"，
 * 红的时候还看不出与谁有关（2026-10-02 实测过：复位后放行旧 404，后面那次读的
 * `fetch` 调用数纹丝不动）。所以：**复位之后，之前发出去的那一笔不许写回来**。
 */
let supportGeneration = 0

/** 用例用：把「这一档没有这一族」记的那一笔清掉（模块级状态必须靠调用方复位）。 */
export function resetKbCacheSupport(): void {
  supportGeneration += 1
  unsupported = false
}

/**
 * 响应过一道**形状检查**：类型是手写的，而这条链上有真实的版本错配可能。
 * 不检查的代价是把「版本对不上」变成界面上一行 `Cannot read properties of undefined`。
 */
function looksLikeSnapshot(payload: unknown): payload is KbCacheSnapshot {
  if (!payload || typeof payload !== 'object') return false
  const record = payload as Partial<KbCacheSnapshot>
  return typeof record.available === 'boolean' && Array.isArray(record.items)
}

/**
 * 打一条这一族的请求，**从不抛**（见模块头最后一节）：不适用的那一档返回 `null`。
 *
 * 404 认作「这一档没有本机后端」并**记住**——服务器档页面不该每次挂载都白打一条
 * （那是每次进知识库页都多一个必然失败的请求）。
 *
 * 读快照、用量读数与再确认共用它：三者只差路径、`init` 与那道形状检查，
 * 而静默口径、404 那一笔**必须**是同一份——各写一套的话迟早只有一套被改。
 */
async function readQuiet<T>(
  path: string,
  looks: (payload: unknown) => payload is T,
  init?: RequestInit,
): Promise<T | null> {
  if (unsupported) return null
  // 这一读是**那一刻**发出去的：结论回来时对一下复位计数（见 `supportGeneration`）
  const gen = supportGeneration
  try {
    const payload = await requestLocal<unknown>(path, init)
    return looks(payload) ? payload : null
  } catch (error) {
    // 复位之后回来的 404 不作数：那一笔属于上一次生命周期
    if ((error as { status?: number } | undefined)?.status === 404 && gen === supportGeneration) {
      unsupported = true
    }
    return null
  }
}

/** 一份快照的读（五条读与再确认都走它）。 */
function readSnapshot(path: string, init?: RequestInit): Promise<KbCacheSnapshot | null> {
  return readQuiet(path, looksLikeSnapshot, init)
}

/** 库列表快照（列表页与卡片/行两态的骨架）。 */
export function getKbCacheKnowledgeBases(): Promise<KbCacheSnapshot | null> {
  return readSnapshot('/local/kb-cache/knowledge-bases')
}

/** 库详情快照（详情页那一份，也是 reader 面每轮每库读的那一份）。 */
export function getKbCacheKnowledgeBase(kbId: string): Promise<KbCacheSnapshot | null> {
  return readSnapshot(`/local/kb-cache/knowledge-bases/${kbId}`)
}

/**
 * 文档列表快照（页面挂载那几次往返里最重的一次）。
 *
 * **只认规范视图**：带 `q` / `stage` / `source_kind` 的视图不该走到这里
 * （`docListViewOf` 会返回 `null`）；即便走到了，后端也是如实回 `available:false`，
 * 不是错误。`page` / `size` 一律显式发——键由这两个值与 `docListViewKey()` 对齐。
 */
export function getKbCacheDocuments(
  kbId: string,
  view: DocListView = {},
): Promise<KbCacheSnapshot | null> {
  const params = new URLSearchParams()
  if (view.folder) params.set('folder', view.folder)
  else if (view.root) params.set('root', 'true')
  params.set('page', String(view.page ?? DEFAULT_DOC_PAGE))
  params.set('size', String(view.size ?? DEFAULT_DOC_PAGE_SIZE))
  return readSnapshot(`/local/kb-cache/knowledge-bases/${kbId}/documents?${params.toString()}`)
}

/** 目录树快照——与文档列表**同屏**，所以不做就成了「半屏缓存」。 */
export function getKbCacheFolders(kbId: string): Promise<KbCacheSnapshot | null> {
  return readSnapshot(`/local/kb-cache/knowledge-bases/${kbId}/folders`)
}

/** 文档条目快照（详情页 / 抽屉的入口帧；进度不在里面）。 */
export function getKbCacheDocument(documentId: string): Promise<KbCacheSnapshot | null> {
  return readSnapshot(`/local/kb-cache/documents/${documentId}`)
}

/* ------------------------------------------------------------------ 再确认一份 */

/**
 * **主动再确认**一份快照（焦点回到窗口 / 设置里的「立即刷新」）。
 *
 * 它是**显式动作**，不是每次挂载都发：本机后端整取一次远端 → 比内容哈希 → 相同只推
 * 「上次确认」、不同才换内容（§4.1）。同一份的单飞、15 秒最短间隔、失败 60 秒退避
 * 都在本机后端那一边（§4.4 的防风暴），这里只管发一条。
 *
 * 请求体是**恒定形状**（后端 `KbCacheRevalidateIn` 是 `extra="forbid"`）：不用的键
 * 给默认值，省得两边各记一套「什么时候带哪个键」。
 */
export function revalidateKbCache(target: KbCacheRevalidate): Promise<KbCacheSnapshot | null> {
  const body: Required<KbCacheRevalidate> = {
    resource: target.resource,
    kb_id: target.kb_id ?? '',
    document_id: target.document_id ?? '',
    folder: target.folder ?? '',
    // `folder` 与 `root` 互斥：两个都给的话后端会判「说不清清的是哪一片」
    root: target.folder ? false : target.root === true,
    page: target.page ?? DEFAULT_DOC_PAGE,
    size: target.size ?? DEFAULT_DOC_PAGE_SIZE,
  }
  return readSnapshot('/local/kb-cache/revalidate', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/* ------------------------------------------------------------------ 用量读数与清理 */

/**
 * 本机留的那一份的**用量读数**（`GET /local/kb-cache/stats`，M4 阶段 6）。
 *
 * 字段名对着后端 `KbCacheStatsOut`（那个模型的作者是 `storage.base.KbMetaCacheStats`）。
 * 设置面板那一块只用得上两个：`rows`（留着几项）与 `newest_fetched_at`（「最近更新」）；
 * 另外两个是排障用的（这一份是不是很久以前留的、占了多大）。
 */
export interface KbCacheStats {
  /** 留着几项（库列表 / 每库详情 / 每个文档清单视图各一项）。 */
  rows: number
  /** 这些内容合计多少字节。 */
  payload_bytes: number
  /** 最旧那一项是什么时候看到的（没行就是 null）。 */
  oldest_fetched_at: string | null
  /** 最新那一项是什么时候看到的（界面上的「最近更新」就是它）。 */
  newest_fetched_at: string | null
}

/** 响应过一道形状检查：类型手写，而"后端比界面老"会把要显示的数变成 `undefined`。 */
function looksLikeStats(payload: unknown): payload is KbCacheStats {
  if (!payload || typeof payload !== 'object') return false
  return typeof (payload as Partial<KbCacheStats>).rows === 'number'
}

/**
 * 读一次用量读数（设置面板「本机留了一份」那一块）。
 *
 * **失败返回 `null`**（与这一族其余读同一条口径：它是"顺手说个数字"，不是权威数据），
 * 由调用方如实写一句「读不到」——静默摆一个 0 会让用户以为"本机什么都没留"。
 */
export function getKbCacheStats(): Promise<KbCacheStats | null> {
  return readQuiet('/local/kb-cache/stats', looksLikeStats)
}

/**
 * 清掉本机留的那一份（设置面板那颗「清除」；**全清**：所有地址、所有资源）。
 *
 * 与这一族其余调用**刻意不同**：它是用户明确点的动作，所以**失败要抛**
 * （调用方据此说一句出错的话）。静默失败在这里比别处更糟——用户会以为"清干净了"。
 *
 * 返回清掉了几项（0 = 本来就没有，不是错误）。
 */
export async function clearKbCache(): Promise<number> {
  const payload = await requestLocal<{ removed?: number }>('/local/kb-cache', { method: 'DELETE' })
  return typeof payload?.removed === 'number' ? payload.removed : 0
}
