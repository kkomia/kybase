/**
 * 知识库域的数据层。
 *
 * 三块：
 *
 * 1. **知识库清单**（对应旧前端的 `stores/knowledgeBases.ts`）：列表接口一次带回
 *    每个库的文档数与最近更新时间，所以「汇总」不再逐库再查一遍。整份清单在模块级
 *    只存一份、只拉一份——列表页、详情页、设置弹窗读的是同一份，不会"这边删了那边还在"。
 * 2. **模型注册表**（对应 `stores/modelRegistry.ts`）：建库要选嵌入模型、出题要选模型。
 *    缓存 + 单飞：进设置页不该每次都等一次冷启动。
 * 3. **轮询**（对应 `composables/usePolling.ts`）：只在"确实有活在跑"时开表，
 *    标签页隐藏时暂停，慢请求不叠加。
 *
 * 为什么不用 `@tanstack/react-query`：壳里的 Provider 是本域的**外部**依赖
 * （`src/app/**` 归主控），而这几个接口的缓存语义比 query 的默认行为更窄
 * （清单要"就地更新"而不是"失效重取"）。这里用最少的代码把语义写死，
 * 等壳稳定后再评估是否收编。
 */
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'

import { getKbCacheKnowledgeBases } from '@/api/kbCache'
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
  updateKnowledgeBase,
  type KnowledgeBase,
  type KnowledgeBaseCreate,
} from '@/api/knowledgeBases'
import { getRegistry, type RegisteredModel, type Registry } from '@/api/modelRegistry'
import type { DocStats } from '@/lib/format'

import {
  LIVE_FRAME,
  createSnapshotGate,
  snapshotFrame,
  snapshotRows,
  withoutPermissions,
  type SnapshotFrame,
} from './snapshot'

/* ---------------------------------------------------------------- 提示 */

/**
 * 提示语一律经这里出去（旧前端的 `useToast`）。
 *
 * 走 sonner：Toast 容器由应用壳挂载（`<Toaster/>`），本域只负责"说什么"。
 * 包一层是为了两件事：文案集中可查、测试里 mock 一个模块就够。
 */
export const notify = {
  success(message: string): void {
    toast.success(message)
  },
  error(message: string): void {
    toast.error(message)
  },
  warning(message: string): void {
    toast.warning(message)
  },
}

/** 把异常翻成一句给用户看的话：后端文案优先，兜底用调用方给的动作名。 */
export function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

/* ---------------------------------------------------------------- 知识库清单 */

interface KnowledgeBaseState {
  items: KnowledgeBase[]
  summaries: Record<string, DocStats>
  loading: boolean
  error: string
  /** 这一份清单的来历（§4.3-③：`fromSnapshot` 为真 = 屏幕上这份是「上次看到的内容」）。 */
  frame: SnapshotFrame
}

let kbState: KnowledgeBaseState = {
  items: [],
  summaries: {},
  loading: false,
  error: '',
  frame: LIVE_FRAME,
}
const kbListeners = new Set<() => void>()
/** 正在飞的那次列表请求：并发调用合并成一次（页面上曾把 `/knowledge-bases` 发三遍）。 */
let kbInflight: Promise<void> | null = null
/** 「先画一帧」的闸门（见 `snapshot.ts`）：库列表这一个视图。 */
let kbGate = createSnapshotGate()
/** 库列表这一个视图的键（本页自己的记账，不上接口）。 */
const KB_LIST_VIEW = 'kb_list'

function setKbState(patch: Partial<KnowledgeBaseState>): void {
  kbState = { ...kbState, ...patch }
  for (const listener of kbListeners) listener()
}

/** 一个库的汇总。`?? 0` 兜的是"后端比前端旧"的窗口，否则界面会出现 NaN。 */
function summaryOf(kb: KnowledgeBase): DocStats {
  return { count: kb.document_count ?? 0, updatedAt: kb.last_activity ?? null }
}

function summaryIndex(items: KnowledgeBase[]): Record<string, DocStats> {
  return Object.fromEntries(items.map((kb) => [kb.id, summaryOf(kb)]))
}

/**
 * 先画一帧：把本机留的那份**库列表**摆上来（M4 阶段 5 / §4.3 的时序规则）。
 *
 * 四条判据按顺序，一条都不能少：
 *
 * 1. **内存里有清单就不画**（可能是刚建的库）：这也是"内存已有 → 跳过"那条规则的原文；
 * 2. `claim` 一并算上"画过 / 已有实时结果 / 正在飞"——不满足就**连请求都不发**；
 * 3. 回来时实时那条线已经赢了 → 丢掉（`stillWanted`）：晚到的旧响应不许盖新结果；
 * 4. 内容与实时读**同形**，只少权限位（`withoutPermissions`）与进度那一类活数据。
 *
 * 失败**静默**：它是顺手快一点的那一下，页面自己那条实时读才是权威，
 * 报错的位置在那一条线上（本机后端没起来不该在知识库页上多一句红字）。
 */
async function paintKnowledgeBases(): Promise<void> {
  if (kbState.items.length > 0) return
  if (!kbGate.claim(KB_LIST_VIEW)) return
  try {
    const snapshot = await getKbCacheKnowledgeBases()
    if (!snapshot?.available || !kbGate.stillWanted(KB_LIST_VIEW)) return
    if (kbState.items.length > 0) return
    const items = snapshotRows<KnowledgeBase>(snapshot).map(withoutPermissions)
    kbGate.settle(KB_LIST_VIEW)
    // 空的（"一个库都没有"）摆不上任何东西：不写内存也不置帧——一边摆骨架屏一边说
    // "这是上次看到的内容"是自相矛盾的。等实时那条读自己说。
    if (items.length === 0) return
    setKbState({ items, summaries: summaryIndex(items), frame: snapshotFrame(snapshot.fetched_at) })
  } catch {
    // 见上：静默（这一条不是报错的位置）
  } finally {
    kbGate.release(KB_LIST_VIEW)
  }
}

/**
 * 预热：侧栏那一项被划过/聚焦时，先把「上次看到的库列表」取回来
 * （M4 阶段 5；照 12.55 任务中心的预取先例）。
 *
 * 叫一次**不等于**打一次请求：内存已有、或这个视图已经画过/实时到了，它就地返回
 * （判定全在上面那个闸门里）。所以挂在悬停/聚焦上不会把请求放大。
 */
export function warmKnowledgeBases(): Promise<void> {
  return paintKnowledgeBases()
}

async function loadKnowledgeBases(): Promise<void> {
  if (kbInflight) return kbInflight
  setKbState({ loading: true })
  // 与实时读**并行发起**：这一读打本机回环（个位数毫秒），一分钟都不挡实时那条线
  void paintKnowledgeBases()
  kbInflight = (async () => {
    try {
      const body = await listKnowledgeBases()
      // 实时结果到了：这个视图从此刻起不许再被快照盖（§4.3-②），小字那一行也就收了
      kbGate.live(KB_LIST_VIEW)
      setKbState({
        items: body.items,
        summaries: summaryIndex(body.items),
        error: '',
        frame: LIVE_FRAME,
      })
    } catch (cause) {
      // 失败**不清**已经画上的那一帧：内容还在屏幕上，顶上那句「现在连不上…」由它说
      setKbState({ error: messageOf(cause, '知识库列表加载失败') })
    } finally {
      setKbState({ loading: false })
      kbInflight = null
    }
  })()
  return kbInflight
}

async function createKnowledgeBaseInList(payload: KnowledgeBaseCreate): Promise<KnowledgeBase> {
  const created = await createKnowledgeBase(payload)
  setKbState({
    items: [...kbState.items, created],
    summaries: { ...kbState.summaries, [created.id]: summaryOf(created) },
  })
  return created
}

async function updateKnowledgeBaseInList(
  kbId: string,
  patch: Parameters<typeof updateKnowledgeBase>[1],
): Promise<KnowledgeBase> {
  const updated = await updateKnowledgeBase(kbId, patch)
  setKbState({
    items: kbState.items.map((item) => (item.id === kbId ? updated : item)),
    summaries: { ...kbState.summaries, [kbId]: summaryOf(updated) },
  })
  return updated
}

async function removeKnowledgeBaseInList(kbId: string): Promise<void> {
  await deleteKnowledgeBase(kbId)
  const summaries = { ...kbState.summaries }
  delete summaries[kbId]
  setKbState({ items: kbState.items.filter((item) => item.id !== kbId), summaries })
}

export interface KnowledgeBaseStore {
  items: KnowledgeBase[]
  summaries: Record<string, DocStats>
  loading: boolean
  error: string
  /** 这一份清单是「上次看到的内容」画的帧（实时结果一到即清，§4.3-②）。 */
  fromSnapshot: boolean
  /** 那份内容是什么时候看到的（`fetched_at`）；实时帧为空串。 */
  snapshotAt: string
  load: () => Promise<void>
  refreshSummaries: () => Promise<void>
  create: (payload: KnowledgeBaseCreate) => Promise<KnowledgeBase>
  update: (kbId: string, patch: Parameters<typeof updateKnowledgeBase>[1]) => Promise<KnowledgeBase>
  remove: (kbId: string) => Promise<void>
  byId: (kbId: string) => KnowledgeBase | undefined
}

/** 订阅模块级清单。任何组件改它，其它组件立刻看到同一份。 */
export function useKnowledgeBases(): KnowledgeBaseStore {
  const [, bump] = useState(0)

  useEffect(() => {
    const listener = () => bump((value) => value + 1)
    kbListeners.add(listener)
    // **挂上订阅之后再对一次表**（与 `api/provider.ts` 同一处理）：清单可能在
    // "这次渲染"与"挂上订阅"之间被改（比如侧栏悬停预热先画了一帧），而那一瞬间的
    // 广播没人听见——`useState` + 订阅的经典缺口，订阅后这一次重读把它补上。
    bump((value) => value + 1)
    return () => {
      kbListeners.delete(listener)
    }
  }, [])

  return {
    items: kbState.items,
    summaries: kbState.summaries,
    loading: kbState.loading,
    error: kbState.error,
    fromSnapshot: kbState.frame.fromSnapshot,
    snapshotAt: kbState.frame.snapshotAt,
    load: loadKnowledgeBases,
    refreshSummaries: loadKnowledgeBases,
    create: createKnowledgeBaseInList,
    update: updateKnowledgeBaseInList,
    remove: removeKnowledgeBaseInList,
    byId: (kbId) => kbState.items.find((item) => item.id === kbId),
  }
}

/** 测试用：把模块级缓存清干净（生产代码不该调它）。 */
export function resetKnowledgeBaseCache(): void {
  kbState = { items: [], summaries: {}, loading: false, error: '', frame: LIVE_FRAME }
  kbInflight = null
  kbGate = createSnapshotGate()
  for (const listener of kbListeners) listener()
}

/* ---------------------------------------------------------------- 模型注册表 */

interface RegistryState {
  registry: Registry | null
  loaded: boolean
  error: string
}

let registryState: RegistryState = { registry: null, loaded: false, error: '' }
const registryListeners = new Set<() => void>()
let registryInflight: Promise<void> | null = null

function setRegistryState(patch: Partial<RegistryState>): void {
  registryState = { ...registryState, ...patch }
  for (const listener of registryListeners) listener()
}

async function loadRegistry(): Promise<void> {
  if (registryInflight) return registryInflight
  registryInflight = (async () => {
    try {
      setRegistryState({ registry: await getRegistry(), loaded: true, error: '' })
    } catch (cause) {
      // 保留旧数据：刷新失败让下拉变空，比用一份稍旧的模型列表糟得多
      setRegistryState({ error: messageOf(cause, '模型注册表加载失败') })
    } finally {
      registryInflight = null
    }
  })()
  return registryInflight
}

/** 预取：只拉一次，失败静默（它是顺手多做的准备，不该在用户还没进页面时弹错误）。 */
async function prefetchRegistry(): Promise<void> {
  if (registryState.loaded || registryInflight) return
  const previous = registryState.error
  await loadRegistry()
  setRegistryState({ error: previous })
}

export interface RegistryStore {
  registry: Registry | null
  /** 是否已有结论（成功或失败）。**"还在加载"与"确实没有模型"必须分开**。 */
  modelOptionsLoaded: boolean
  error: string
  load: () => Promise<void>
  prefetch: () => Promise<void>
  /** 可用的嵌入模型：能力为空或声明了 embedding（旧前端同一口径）。 */
  embeddingModels: RegisteredModel[]
  /** 可对话的模型：供应商启用，且能力为空或含 chat。 */
  chatModels: RegisteredModel[]
  /** 注册表里为「向量化」绑定的默认模型 pk（可能为空）。 */
  defaultEmbeddingPk: string
}

export function useModelRegistry(): RegistryStore {
  const [, bump] = useState(0)

  useEffect(() => {
    const listener = () => bump((value) => value + 1)
    registryListeners.add(listener)
    return () => {
      registryListeners.delete(listener)
    }
  }, [])

  const registry = registryState.registry
  const embeddingModels = (registry?.models ?? []).filter(
    (model) => model.capabilities.length === 0 || model.capabilities.includes('embedding'),
  )
  const chatModels = (registry?.models ?? []).filter((model) => {
    const owner = registry?.providers.find((item) => item.id === model.provider_id)
    if (!owner || !owner.enabled) return false
    return model.capabilities.length === 0 || model.capabilities.includes('chat')
  })

  return {
    registry,
    // 加载失败也算"有结论"：那时按"没有模型"处理并给出提示，而不是永远停在加载中
    modelOptionsLoaded: registryState.loaded || registryState.error !== '',
    error: registryState.error,
    load: loadRegistry,
    prefetch: prefetchRegistry,
    embeddingModels,
    chatModels,
    defaultEmbeddingPk:
      registry?.slots.find((item) => item.slot === 'embedding')?.bound_model_pk ?? '',
  }
}

/** 测试用：清掉注册表缓存。 */
export function resetRegistryCache(): void {
  registryState = { registry: null, loaded: false, error: '' }
  registryInflight = null
  for (const listener of registryListeners) listener()
}

/* ---------------------------------------------------------------- 轮询 */

/** 标签页是否可见（node/jsdom 里 `document.hidden` 通常是 false）。 */
function isHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden === true
}

interface PollingOptions {
  /** 只在为真时开表：全绿之后停表，避免无意义的持续请求。 */
  active: boolean
  intervalMs: number
  /** 挂载时立刻跑一次（默认真）。 */
  immediate?: boolean
}

/**
 * 有活儿在跑时才轮询。
 *
 * 三条与旧实现一致的取舍：标签页隐藏时暂停（否则后台标签页一直在打接口）、
 * 上一次还没回来就不发下一次（慢请求不叠成雪崩）、停表时不留下定时器。
 */
export function usePolling(
  run: () => void | Promise<void>,
  { active, intervalMs, immediate = true }: PollingOptions,
): void {
  const latest = useRef(run)
  latest.current = run
  const busy = useRef(false)

  useEffect(() => {
    if (!active) return undefined
    const tick = () => {
      if (busy.current || isHidden()) return
      busy.current = true
      void Promise.resolve(latest.current()).finally(() => {
        busy.current = false
      })
    }
    if (immediate) tick()
    const timer = window.setInterval(tick, intervalMs)
    return () => window.clearInterval(timer)
  }, [active, intervalMs, immediate])
}
