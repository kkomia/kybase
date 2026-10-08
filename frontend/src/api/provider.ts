/**
 * 「知识库连接」的状态层（M3 阶段 6）——本机档的**唯一判定源**。
 *
 * ## 它在回答什么问题
 *
 * 从 M3 起，本机档里的知识库**在别处**（NAS 上）：页面能不能进、导航里有没有那几项、
 * 对话输入框摆不摆 KB 开关、设置里那一节说什么，全都只读它一处
 * （方案 §3.1「唯一判定源」）。答案由本机后端给出（`GET /api/v1/local/provider`，
 * 边车每次现取目标 + 30s 进程内缓存），这一层负责**问它、记住、广播**。
 *
 * ## 为什么类型是**手写**的（不读 `schema.d.ts`）
 *
 * `/local/*` 这一族**不进服务器档的 OpenAPI**（`local_router` 只挂在边车那台，
 * 而 `gen_api_types.py` 生成的是服务器档那份契约），所以生成物里没有它们。
 * 先例与本条纪律都在 `api/local.ts` 的文件头：**手写 + 写清出处**，
 * 字段名逐条对着 `backend/app/api/v1/local.py::ProviderStatusOut` 与
 * `services/knowledge_provider.ProviderStatus.to_payload()` 抄
 * （后者是形状的唯一作者：`ready` 才有协议版本 / 能力集 / 调用者 / 库清单那五段，
 * 不 ready 时它们**根本不在响应里**——所以下面这些都写成可选）。
 * 改了后端就回来改这里。
 *
 * ## 三态与"未探过不猜"（方案 §3.2）
 *
 * | `state` | 页面在不在 | 这一层怎么表现 |
 * | --- | --- | --- |
 * | `ready` | 在 | `ready = true` |
 * | `unavailable` | 不在（连不上 / 凭据错 / 版本不认识） | `blocked = true`，`reason` 有人话有下一步 |
 * | `unconfigured` | 不在（没配地址 / 被显式关掉） | 同上，两句的下一步不同（后端给） |
 * | **还没探过** | **不在**（按缺席处理，首屏不闪一个点进去报错的页面） | `status = null`、`settled = false` |
 *
 * 「不猜」是这一层最容易写错的一条：`status` 初值必须是 `null`，
 * 而不是一个编出来的 `unavailable`——后者会让首屏先闪一句"连不上"再说"好着呢"。
 *
 * ## 什么时候探（三条失效路径，§3.2）
 *
 * 1. **首次被问到才探**（不在启动时挡路：`App` 挂载处那一次 `refresh()` 是唯一一处"主动"）；
 * 2. `state != ready` 时**每 30s 轮询一次**（只为"恢复了要能自己回来"）；`ready` 时**不轮询**；
 * 3. **窗口重新获得焦点**时 `refresh=1`（revalidate-on-focus，与 §6.3 同一条口径）；
 * 4. 设置面板保存地址 / 点「测试连接」→ `refresh()` 强制重探。
 *
 * 轮询与焦点监听挂在**模块级**（按订阅者计数开关，与 `features/knowledge/store.ts`
 * 的单飞 + 订阅同一套写法）：组件卸载了就不该继续打接口，而同一时刻只该有一份计时器。
 *
 * ## 单次调用失败**不改状态**
 *
 * 一次检索超时不该让导航消失（会抖）。这条状态只由**握手探测的结论**改，
 * 而探测本身在服务端绝不抛（连不上就是 `unavailable` + 一句原因）。
 *
 * ## 改了状态就**广播**（订阅者立刻重渲染）
 *
 * `patchLocalProvider` 用 PATCH 的响应整个写回状态（后端保存后会立刻重探并回最新状态），
 * 于是"保存地址 → 侧栏当场长出知识库组"是同一帧内的事（§3.4）。
 */
import { useEffect, useState } from 'react'

import { requestLocal } from './client'
import { inDesktopShell, localDataEnabled } from './sidecar'

/* ------------------------------------------------------------------ 类型（手写） */

/**
 * 三态（**没有第四种**）。后端回的就是这三个字面量之一；这里留 `string`
 * 的兜底只为了让"后端加了新状态"时界面**不崩**（按"不在"处理）。
 */
export type ProviderState = 'unconfigured' | 'unavailable' | 'ready'

/** 握手里的库摘要（`ProviderKbBriefOut`：够界面判断"这个库是什么、我能不能写"）。 */
export interface ProviderKbBrief {
  id: string
  name: string
  /** 库内文档数（与列表端点同一个数，后端一次聚合查出来）。 */
  document_count?: number
  last_activity?: string | null
  /** 这把凭据能不能往这个库里写（**由后端按会话/钥匙算**，不是前端猜的）。 */
  can_write?: boolean
  embedding_model_id?: string
  embedding_dim?: number
  wiki_enabled?: boolean
}

export interface ProviderRetrievalCaps {
  modes?: string[]
  default_mode?: string
  rerank?: boolean
  filters?: boolean
  top_k_max?: number
  candidate_k_max?: number
}

export interface ProviderIngestCaps {
  transport?: string
  /** 后端那个键就叫 `async`（`ProviderIngestCapsOut` 的序列化别名）。 */
  async?: boolean
  dedup?: string
  /**
   * 单文件上限（字节）——**上传前的校验口径就是它**（R8：前端不再硬编码 200MB）。
   * 与 `features/knowledge/uploadLimits.ts` 那份兜底常量是"握手优先、常量兜底"的关系。
   */
  max_bytes?: number
  /** 收的扩展名（后端从项目状态判断机械导出，不含点、小写）。 */
  extensions?: string[]
}

export interface ProviderTrackingCaps {
  document?: boolean
  timeline?: boolean
}

export interface ProviderKbCaps {
  create?: boolean
  delete?: boolean
  folders?: boolean
  shares?: boolean
  wiki?: boolean
}

export interface ProviderEmbeddingCaps {
  /** 向量化配没配好：**建库入口按它决定可用性**（方案 §7 阶段 6）。 */
  configured?: boolean
  /** 开发档（检索结果不代表真实效果）：面板要**如实显示**，不当正常档。 */
  is_development?: boolean
  model_id?: string
  dim?: number
}

export interface ProviderCapabilities {
  retrieval?: ProviderRetrievalCaps
  ingest?: ProviderIngestCaps
  tracking?: ProviderTrackingCaps
  knowledge_bases?: ProviderKbCaps
  embedding?: ProviderEmbeddingCaps
}

/** 这把凭据**在 NAS 侧**被认成谁（同一台 NAS 两套身份是事实：页面用会话、边车用钥匙）。 */
export interface ProviderCaller {
  kind?: 'session' | 'api_key' | string
  permission?: string
  /** 本机后端那把长期 API Key **不是管理员**（管理员专属端点对它 403）——如实报。 */
  is_admin?: boolean
  can_write?: boolean
  knowledge_base_ids?: string[]
}

/** `GET /local/provider` 的响应（`ProviderStatusOut`，形状由 `to_payload()` 给）。 */
export interface ProviderStatus {
  state: ProviderState | string
  /** `state == ready` 的别名（**页面显隐只看它**）。 */
  available: boolean
  /** 两种「不在」各一句人话 + 下一步；`ready` 时为空串。 */
  reason?: string
  /** 这个结论是什么时候得到的（ISO 时间）。 */
  checked_at?: string
  /** 解析后的实际地址（**不是秘密**，不必脱敏；空 = 没配）。 */
  base_url?: string
  /** `configured` / `missing`——凭据只看有没有，**永不回显**。 */
  credential?: 'configured' | 'missing' | string
  protocol_version?: number | null
  app_version?: string
  capabilities?: ProviderCapabilities
  caller?: ProviderCaller
  knowledge_bases?: ProviderKbBrief[]
}

/** 两个运行期键（`PATCH /local/provider` 的白名单，**凭据类键一个都不收**）。 */
export interface ProviderPatch {
  /** 空串 = 清掉覆盖、回继承（壳里那台 NAS）。 */
  base_url?: string
  /** `false` = 显式关掉（页面与 KB 工具都不摆）。 */
  enabled?: boolean
}

/* ------------------------------------------------------------------ 常量与文案 */

/** 握手结论在**服务端**缓存 30s（进程内存）。前端这一侧的 TTL 与它同值。 */
export const PROVIDER_TTL_MS = 30_000

/** `state != ready` 时的轮询间隔（§3.2 第三条；`ready` 时**不轮询**）。 */
export const PROVIDER_POLL_MS = 30_000

/** 三态的人话名字（认不出来就原样返回：后端加了新状态界面不该崩）。 */
const STATE_LABELS: Record<string, string> = {
  unconfigured: '未配置',
  unavailable: '不可用',
  ready: '已连接',
}

export function providerStateLabel(state: string): string {
  return STATE_LABELS[state] ?? state
}

/** 凭据那句话（**永不回显**，只说有没有）。 */
export function credentialLabel(credential: string): string {
  if (credential === 'configured') return '已配置（桌面壳里的那把钥匙）'
  if (credential === 'missing') return '未配置'
  return credential
}

/**
 * 悬停看到的**排障细节**（地址 / 协议版本 / 库数 / 上次确认）。
 *
 * 地址不是秘密，协议版本与上次确认是"到底有没有连上"的第一手证据——
 * 这条状态条的作用就是**不让人去翻控制台**（与顶栏那条"我的数据在哪"同一条纪律）。
 */
export function providerDetailLines(status: ProviderStatus | null): string[] {
  if (!status) return []
  const lines = [`地址：${status.base_url || '（没配）'}`]
  if (status.protocol_version != null) {
    lines.push(
      `协议版本：${status.protocol_version}${status.app_version ? `（应用 ${status.app_version}）` : ''}`,
    )
  }
  lines.push(`看得见的库：${(status.knowledge_bases ?? []).length} 个`)
  lines.push(`凭据：${credentialLabel(status.credential ?? 'missing')}`)
  lines.push(`上次确认：${status.checked_at || '（没有时间戳）'}`)
  if (status.reason) lines.push(`原因：${status.reason}`)
  return lines
}

/* ------------------------------------------------------------------ 读一次 */

/**
 * 读一次提供者状态（**纯请求**：不写模块状态、不订阅）。
 *
 * `refresh = true` 时带 `?refresh=1` **强制重探**（服务端那份 30s 缓存被跳过）；
 * 不带就是读缓存——所以这条请求**不在渲染路径上等一次 NAS 往返**（R1）。
 *
 * 响应过一道**形状检查**（`state` 与 `available` 必须在）：类型是手写的
 * （没有生成物替我们守契约），而这条链上有真实的版本错配可能。不检查的代价是把
 * "版本对不上"变成界面上一行 `Cannot read properties of undefined`。
 */
export async function getLocalProvider(refresh = false): Promise<ProviderStatus> {
  const payload = await requestLocal<ProviderStatus>(
    refresh ? '/local/provider?refresh=1' : '/local/provider',
  )
  if (
    !payload ||
    typeof payload !== 'object' ||
    typeof payload.state !== 'string' ||
    typeof payload.available !== 'boolean'
  ) {
    throw new Error('知识库提供者响应不认识（没有 state / available：是不是本机后端比界面老？）')
  }
  return payload
}

/**
 * 改两个运行期键（地址 / 开关），**并把后端返回的最新状态整个写回本模块**。
 *
 * 后端写完会立刻重探一次（`PATCH /local/provider` 的契约），所以这一下同时完成
 * "保存"与"重渲染"：侧栏的知识库组、对话里的 KB 开关、这一节自己全都读同一份状态
 * （§3.4 那条"保存 → 订阅者立刻重渲染"靠的就是这里）。
 *
 * **凭据不在这里**：`base_url` / `enabled` 是白名单，`token` 类的键会被后端 422 拒掉。
 */
export async function patchLocalProvider(patch: ProviderPatch): Promise<ProviderStatus> {
  return run(async (token, gen) => {
    const status = await requestLocal<ProviderStatus>('/local/provider', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
    if (!status || typeof status !== 'object' || typeof status.state !== 'string') {
      throw new Error('知识库提供者响应不认识（没有 state：是不是本机后端比界面老？）')
    }
    apply(token, gen, status)
    return status
  }, true)
}

/* ------------------------------------------------------------------ 模块级状态 */

interface ProviderStore {
  /** 最后一次握手的结论；`null` = **还没探过**（不猜，按缺席渲染）。 */
  status: ProviderStatus | null
  /** 有没有过结论（成功、或"这一档没有这个概念"）。守卫靠它决定"等"还是"跳"。 */
  settled: boolean
  /** 这一档**根本没有** `/local/provider`（浏览器 / NAS 网页端：知识库就是它自己）。 */
  unsupported: boolean
  /** 读这一次的失败原因（网络不通 / 边车没起来 / 形状不认识）。**没有就为空串**。 */
  error: string
  /** 正在飞一次请求（界面据此显示"测试中…"，不参与显隐判定）。 */
  loading: boolean
  /** 上次拿到结论的时刻（毫秒）——TTL 判据用。 */
  fetchedAt: number
}

let store: ProviderStore = {
  status: null,
  settled: false,
  unsupported: false,
  error: '',
  loading: false,
  fetchedAt: 0,
}

const listeners = new Set<() => void>()

/** 正在飞的那一次（单飞：并发调用合并成一次，与 `features/knowledge/store.ts` 同一写法）。 */
let inflight: Promise<unknown> | null = null

/** 正在飞的那一次是不是**强制重探**（决定"再来一次"能不能并进来，见 `run`）。 */
let inflightForced = false

/** 单调递增的请求号：只让**最新**那一次的结论写进状态（旧的晚到不许盖新的）。 */
let newest = 0
let applied = 0

/**
 * **状态被复位过几次**（`resetProviderStore()` / `setProviderStatusForTest()` 各 +1）。
 *
 * 与 `api/backup.ts` 里那一位**同形、同一条理由**（那一份的注释写得更细，这里只留结论）：
 * `token`/`applied` 管的是"**同一次生命周期内**的新旧"，可这条链上有一个**跨越复位的
 * 空档**——`requestLocal()` 的第一次 `fetch` 不在调用点上（要先问壳要基址、再探一次活），
 * 所以"C 那一刻发出去的读"完全可能在**复位之后**才回来。那时 `applied` 已经被复位成 0，
 * 旧请求的 `token` 反而更大 → 水位那道闸门拦不住它 → 它把复位之后才摆好的那份状态盖掉。
 *
 * 真机上的表现是"复位之后界面又闪回旧结论"；用例里的表现是**跨用例干扰**——
 * 上一条用例（或本文件里某个没打桩的挂载）发出去的那条读，把这一条用例注入的状态盖掉，
 * 于是"红哪一条"随机器快慢变（2026-10-02 门禁上抓到过：设置弹窗那一份时而红在这条、
 * 时而红在那条，单跑必绿）。所以加这一位：**复位（含用例直接摆状态）之后，
 * 之前发出去的一律不算数**。
 */
let generation = 0

function setStore(patch: Partial<ProviderStore>): void {
  store = { ...store, ...patch }
  for (const listener of listeners) listener()
}

/**
 * 单飞执行一次请求。
 *
 * 三种搭配各有理由：
 *
 * - 普通读 + 普通读 → 合并（切页、开面板不该各打一次）；
 * - 普通读 + **强制重探**（`refresh=1`）→ 不合并：那一次读可能是"用户改地址之前"
 *   发出去的，拿它的结论当重探结果等于没重探；
 * - 强制重探 + 强制重探 → 合并（同一次重探的重复触发：连点「测试连接」、
 *   焦点事件与轮询撞在一起）。
 *
 * 无论哪种，只有**最新**那一次的结论写进状态（`applied` 那个水位）——
 * 否则一个慢的旧请求回来会把刚保存的新地址又盖回去。
 */
function run<T>(work: (token: number, gen: number) => Promise<T>, force = false): Promise<T> {
  if (inflight && (!force || inflightForced)) return inflight as Promise<T>
  const token = ++newest
  const gen = generation
  const promise = work(token, gen).finally(() => {
    if (token === newest) {
      inflight = null
      inflightForced = false
    }
  })
  inflight = promise
  inflightForced = force
  return promise
}

/** 把一次请求的结论写进状态（**只有最新的那一次算数**，且**复位之前发出去的不算**）。 */
function apply(token: number, gen: number, status: ProviderStatus): void {
  // 复位（或用例直接摆状态）之后，**之前发出去的一律作废**（见 `generation`）
  if (gen !== generation) return
  if (token < applied) return
  applied = token
  store = {
    ...store,
    status,
    settled: true,
    unsupported: false,
    error: '',
    loading: false,
    fetchedAt: Date.now(),
  }
  for (const listener of listeners) listener()
}

function fail(token: number, gen: number, cause: unknown): void {
  if (gen !== generation) return
  const error = cause as (Error & { status?: number }) | undefined
  // 404 = 这一档**没有**这个端点（服务器档：浏览器 / NAS 网页端），
  // 与"本机档但读不到"是两件事——后者要让界面如实报"读不到"（不许静默）。
  const unsupported = error?.status === 404
  if (token < applied && !unsupported) return
  applied = token
  store = {
    ...store,
    settled: true,
    unsupported: unsupported || store.unsupported,
    loading: false,
    // 失败也记时刻：组件来来去去（切页、开面板）不该各失败一次——
    // "恢复了吗"由 30s 轮询与焦点重验那两条**强制**的路径负责（它们不看 TTL）。
    fetchedAt: Date.now(),
    // **不清 `status`**：一次读失败不该把上一次的结论抹掉（会闪），
    // 显隐仍然以最近一次握手为准（方案 §3.2：只有握手结论改状态）。
    error: error instanceof Error && error.message ? error.message : String(cause),
  }
  for (const listener of listeners) listener()
}

/** 真去读一次（`loadProviderStatus` / `refresh` 的实现）。 */
function read(token: number, gen: number, force: boolean): Promise<void> {
  setStore({ loading: true })
  return getLocalProvider(force).then(
    (status) => apply(token, gen, status),
    (cause: unknown) => fail(token, gen, cause),
  )
}

/**
 * 读一次状态，**TTL 内不重探**（拿到结论后 30s 内再问同一个问题没有新信息）。
 *
 * 这是"挂载时探一次"用的那一条：组件来来去去（切页、开面板）不该每次都打后端。
 */
export async function loadProviderStatus(): Promise<void> {
  if (store.fetchedAt > 0 && Date.now() - store.fetchedAt < PROVIDER_TTL_MS) return
  await run((token, gen) => read(token, gen, false))
}

/**
 * **强制重探**（`refresh=1`）：窗口重新获得焦点、点「测试连接」、`state != ready`
 * 时的 30s 轮询都走它。单飞保证同一次"重探"只打一个请求。
 */
export function refresh(): Promise<void> {
  return run((token, gen) => read(token, gen, true), true)
}

/* ------------------------------------------------------------------ 一档一判：本机档才看它 */

/**
 * 这一档**要不要**按提供者状态显隐（"本机档"的判据）。
 *
 * - 服务器档（浏览器 / NAS 网页端）：**不要**——那一档的知识库就是它自己
 *   （进程内检索、本机那几张表），"提供者在不在"没有第二个东西可问；
 * - 本机档：**要**。两种情形认作本机档：
 *   ① 跑在**桌面壳**里（本机后端由壳拉起，那是"本机档"成立的地方）；
 *   ② 这一档**真答过一次** `/local/provider`（开发形态里浏览器直连边车那一条）。
 *   而"服务器档"的特征是那个端点**根本不存在**（404）→ 永远答不上来 → 不显隐。
 *
 * 显式关掉本机数据面（`VITE_LOCAL_DATA=0`，排障用的逃生门）时也**不要**：
 * 那一档界面读的就是服务器上的数据，与"本机提供者"无关。
 */
export function providerGateApplies(): boolean {
  if (!localDataEnabled()) return false
  if (store.unsupported) return false
  return inDesktopShell() || store.status !== null
}

/* ------------------------------------------------------------------ 给界面读的那一份 */

/** 组件要用的那份（`useKnowledgeProviderStatus` 的返回值）。 */
export interface ProviderView {
  /** 最后一次握手的结论；`null` = 还没探过（**不猜**）。 */
  status: ProviderStatus | null
  /** 这一档要不要按提供者状态显隐（本机档才要，见 `providerGateApplies`）。 */
  gate: boolean
  /** `state == ready`（页面在不在只看它）。 */
  ready: boolean
  /** 有过结论（成功或失败）；守卫靠它决定"再等一下"还是"跳走"。 */
  settled: boolean
  /** **本机档且不可用**：导航、对话里的 KB 开关、路由守卫据此摘掉知识库入口。 */
  blocked: boolean
  state: string
  reason: string
  /** 读失败的原因（网络 / 边车没起来 / 形状不认识）；成功时为空串。 */
  error: string
  loading: boolean
  capabilities: ProviderCapabilities | null
  knowledgeBases: ProviderKbBrief[]
  /** 强制重探（与模块函数同一个引用，可以直接放进 effect 依赖）。 */
  refresh: () => Promise<void>
}

/** 当前快照（订阅之外的地方也能读，比如事件处理里）。 */
export function providerView(): ProviderView {
  const status = store.status
  const gate = providerGateApplies()
  const ready = status?.available === true
  return {
    status,
    gate,
    ready,
    settled: store.settled,
    blocked: gate && !ready,
    state: status?.state ?? '',
    reason: status?.reason ?? '',
    error: store.error,
    loading: store.loading,
    capabilities: status?.capabilities ?? null,
    knowledgeBases: status?.knowledge_bases ?? [],
    refresh,
  }
}

/* ------------------------------------------------------------------ 订阅、轮询与焦点 */

/** 正在用它的组件数（减到 0 就把计时器与监听摘掉）。 */
let watchers = 0
let timer: number | null = null
let onFocus: (() => void) | null = null

function startWatch(): void {
  if (typeof window === 'undefined') return
  onFocus = () => {
    // revalidate-on-focus（方案 §3.2 第二条）：回到这个窗口就重问一次。
    // **这一档没有这个概念时一句话都不问**（浏览器 / NAS 网页端：打过去只会是 404）。
    if (store.unsupported) return
    void refresh()
  }
  window.addEventListener('focus', onFocus)
  timer = window.setInterval(() => {
    // **`ready` 时不轮询**：状态是好的，没有"恢复了要能自己回来"这件事可做
    if (store.status?.available === true) return
    // 这一档根本没有提供者这个概念：轮询也不会问出结论来（同上）
    if (store.unsupported) return
    void refresh()
  }, PROVIDER_POLL_MS)
}

function stopWatch(): void {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
  if (onFocus) {
    window.removeEventListener('focus', onFocus)
    onFocus = null
  }
}

/**
 * 订阅提供者状态（模块级单份：侧栏、设置面板、顶栏读的是同一个结论）。
 *
 * `enabled = false` 时**不订阅、不探**（顶栏那条状态条靠它做到"非本机档一次都不探"）。
 */
export function useKnowledgeProviderStatus(options: { enabled?: boolean } = {}): ProviderView {
  const enabled = options.enabled ?? true
  const [, bump] = useState(0)

  useEffect(() => {
    if (!enabled) return undefined
    const listener = () => bump((value) => value + 1)
    listeners.add(listener)
    watchers += 1
    if (watchers === 1) startWatch()
    // **挂上订阅之后再对一次表**：状态可能在"这次渲染"与"挂上订阅"之间变了，
    // 而那一瞬间的广播没人听见——组件不会自己重渲染（`useState` + 订阅的经典缺口，
    // 真机上抓到过：冷启动直接进知识库页、提供者不可用时，守卫有时**不挡**，
    // 要等下一次广播才纠正）。这一句等价于 `useSyncExternalStore` 订阅后那次重读。
    bump((value) => value + 1)
    // **首次被问到才探**（不在启动时挡路）：已经有结论时这一次是空操作（TTL 判在这里面）
    void loadProviderStatus()
    return () => {
      listeners.delete(listener)
      watchers -= 1
      if (watchers <= 0) {
        watchers = 0
        stopWatch()
      }
    }
  }, [enabled])

  return providerView()
}

/* ------------------------------------------------------------------ 用例用的窄接口 */

/**
 * 用例用：把模块状态清干净（模块级缓存必须靠调用方复位——`features/knowledge/store.ts`
 * 的 `resetKnowledgeBaseCache` 同一条纪律）。
 */
export function resetProviderStore(): void {
  // 复位也**作废所有在飞的请求**（见 `generation`）：复位之后回来的结论不许写进来
  generation += 1
  store = {
    status: null,
    settled: false,
    unsupported: false,
    error: '',
    loading: false,
    fetchedAt: 0,
  }
  inflight = null
  inflightForced = false
  newest = 0
  applied = 0
  watchers = 0
  stopWatch()
  listeners.clear()
}

/**
 * 用例用：直接摆一份结论（不经过网络）。
 *
 * `fetchedAt` 一并写成"刚刚"：这样挂载时的 `loadProviderStatus()` 会按 TTL 跳过，
 * 用例里不会因为渲染而多打一条请求。
 */
export function setProviderStatusForTest(
  status: ProviderStatus | null,
  options: { unsupported?: boolean; error?: string } = {},
): void {
  // 直接摆状态同样是"从此刻起，之前发出去的都作废"
  generation += 1
  store = {
    status,
    settled: options.unsupported === true || status !== null,
    unsupported: options.unsupported === true,
    error: options.error ?? '',
    loading: false,
    fetchedAt: Date.now(),
  }
  for (const listener of listeners) listener()
}
