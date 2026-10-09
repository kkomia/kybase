/**
 * 「近 90 天入库节奏」——知识库卡片上那根迷你趋势图的数据层。
 *
 * ## 这根线量什么
 *
 * 入库时间在文档行上就有（`created_at`），所以这里画**按天的新增文档数**，
 * 卡片文案照实写「近 90 天入库」——一个对得上真数据的数，比一个好听但假的名字有用。
 *
 * **它与检索命中那根是两件事，不互相顶替**：这一根量"这个库在长"，检索命中那根
 * （`searchTrend.ts`）量"这个库在被用"。两根线各有真数据源，也在卡片上各占一行。
 * 早期版本只有这一根，那时这里确实写着"检索命中趋势今天做不出真的"；v0.61 后端给了
 * 按库的命中趋势（`GET /stats/search-hits`）之后那句话不再成立，那根线就补上了。
 *
 * ## 数据层走哪一套
 *
 * 跟着本域的 `./store.ts`：模块级缓存 + 订阅 + 单飞，**不引 `@tanstack/react-query`**。
 * 那个文件的头注写了这条纪律（Provider 属于本域的外部依赖，而清单要的是"就地更新"
 * 而不是"失效重取"），卡片这一份没有理由另开一套。
 *
 * ## 取数
 *
 * 每库一次 `GET /knowledge-bases/{id}/documents`（`limit=200`），十几个库就是十几次，
 * 并行发、各自缓存（检索命中那根不必这样：那条接口一次给全部库，见 `searchTrend.ts`）。
 * 文档列表**按时间倒序**（`KnowledgeBaseView.tsx` 里两处注释都按这一条写），所以
 * "这一页已经出现窗口之前的时间"就等于"窗口内已经取全了"——多数库一页（= 一次请求）
 * 就到底。只有"90 天内超过 200 篇"的库才继续翻，最多 `TREND_MAX_PAGES` 页；
 * 真翻到上限时读数照实写成 `≥ N 篇`（`truncated`），不假装那就是全部。
 *
 * ## 空数据不造数
 *
 * 一条 `created_at` 都取不出来时给的就是一串 0（画出来是一根压在底部的平线），
 * 卡片上写「无入库」。**不补占位、不平滑、不按库龄编几笔**。
 */
import { useEffect, useRef, useState } from 'react'

import { listDocuments } from '@/api/documents'

/** 窗口长度（天）。卡片上的文案写的就是它，改这里等于改文案。 */
export const INGEST_TREND_DAYS = 90

/** 每页取几篇。200 是接口的上限，取满一页让"多数库一次请求到底"成立。 */
const TREND_PAGE_LIMIT = 200

/** 最多翻几页。翻到上限还盖不住窗口的库，读数写 `≥ N 篇`。 */
const TREND_MAX_PAGES = 3

/** 单库最多拉多少行（页上限 × 页数）。卡片上的悬浮说明要如实说这个数。 */
export const TREND_MAX_ROWS = TREND_PAGE_LIMIT * TREND_MAX_PAGES

/* ---------------------------------------------------------------- 聚合（纯函数） */

/** 本地时区的一天：`YYYY-MM-DD`。 */
export function dayKeyOf(value: Date): string {
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
}

/**
 * 按天聚合入库节奏。
 *
 * 返回的一串数**长度恒为 `days`**：末位是"今天"，首位是 `days - 1` 天前。
 *
 * 分桶用**本地日**而不是 UTC 日：日历上写着"今天"的那一天，才是用户扫卡片时
 * 心里的那一天（后端给的是带时区的 ISO 串，只在这里落成日历日一次）。
 *
 * 不计入的三种：窗口之外的（更早，或时间戳偏到了明天——那是时钟偏差）、
 * 解析不出来的、以及空值。窗口的边界由 `new Date(y, m, d - n)` 走本地日历算，
 * 跨月、跨年、夏令时都交给 `Date` 自己。
 */
export function ingestSeries(
  createdAt: readonly (string | null | undefined)[],
  now: Date = new Date(),
  days: number = INGEST_TREND_DAYS,
): number[] {
  const indexOf = new Map<string, number>()
  for (let index = 0; index < days; index += 1) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1 - index))
    indexOf.set(dayKeyOf(day), index)
  }

  const series = new Array<number>(days).fill(0)
  for (const value of createdAt) {
    if (!value) continue
    const moment = new Date(value)
    if (Number.isNaN(moment.getTime())) continue
    const index = indexOf.get(dayKeyOf(moment))
    if (index === undefined) continue
    series[index] += 1
  }
  return series
}

/** 窗口内的总篇数（线旁边那个读数）。 */
export function seriesTotal(series: readonly number[]): number {
  return series.reduce((sum, value) => sum + value, 0)
}

/* ---------------------------------------------------------------- 缓存与订阅 */

export interface IngestTrend {
  status: 'loading' | 'ready' | 'error'
  /** 按天的入库篇数（长度 = 窗口天数，末位是今天）；`ready` 之外是空数组。 */
  series: number[]
  /** 窗口内的总篇数。 */
  total: number
  /** 翻到页数上限还没盖住窗口（90 天内的文档超过 `TREND_PAGE_LIMIT * TREND_MAX_PAGES` 篇）。 */
  truncated: boolean
}

const LOADING_TREND: IngestTrend = { status: 'loading', series: [], total: 0, truncated: false }

/** 每库一份。key = 知识库 id；缺条 = 这一座还没问过。 */
const trendCache = new Map<string, IngestTrend>()
/** 正在飞的那几次（同一库并发问只发一次）。 */
const trendInflight = new Map<string, Promise<void>>()
const trendListeners = new Set<() => void>()

function setTrend(kbId: string, value: IngestTrend): void {
  trendCache.set(kbId, value)
  for (const listener of trendListeners) listener()
}

/** 窗口起点（本地零点）：翻页时拿它判"已经够到窗口之前了"。 */
function windowStartOf(now: Date, days: number): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1))
}

/** 把一座库的入库时间取到"能盖住窗口"为止（见文件头那段"取数"）。 */
async function fetchWindowCreatedAt(
  kbId: string,
): Promise<{ values: (string | null)[]; truncated: boolean }> {
  const start = windowStartOf(new Date(), INGEST_TREND_DAYS).getTime()
  const values: (string | null)[] = []

  for (let page = 0; page < TREND_MAX_PAGES; page += 1) {
    const list = await listDocuments(kbId, {
      limit: TREND_PAGE_LIMIT,
      offset: page * TREND_PAGE_LIMIT,
    })
    values.push(...list.items.map((item) => item.created_at))
    // 没取满一页 = 库里一共就这些（到底了）
    if (list.items.length < TREND_PAGE_LIMIT) return { values, truncated: false }
    // 倒序下"最后一条已经早于窗口起点" = 再翻也不会有窗口内的了
    const oldest = list.items[list.items.length - 1]?.created_at
    if (oldest && new Date(oldest).getTime() < start) return { values, truncated: false }
  }
  return { values, truncated: true }
}

function loadTrend(kbId: string): Promise<void> {
  const flying = trendInflight.get(kbId)
  if (flying) return flying

  setTrend(kbId, LOADING_TREND)
  const task = (async () => {
    try {
      const { values, truncated } = await fetchWindowCreatedAt(kbId)
      const series = ingestSeries(values)
      setTrend(kbId, { status: 'ready', series, total: seriesTotal(series), truncated })
    } catch {
      // **静默降级**：这一份是卡片上的补充读数，读不到就在卡片上写「—」，
      // 不在列表页上多摆一行红字（那一行留给库列表自己那条读，见 `kb-error-line`）。
      setTrend(kbId, { status: 'error', series: [], total: 0, truncated: false })
    } finally {
      trendInflight.delete(kbId)
    }
  })()
  trendInflight.set(kbId, task)
  return task
}

/**
 * 订阅这几座库的入库节奏：缺的那几座当场发起，已有缓存的不再问。
 *
 * `kbIds` 传空数组 = 一个请求都不发（卡片形态才画趋势图，行形态不画）。
 * 这一份**不做失效重取**：库列表刷新（建库、改名、焦点回来）不重拉趋势——
 * 那张图量的是"近 90 天的节奏"，几分钟的延迟不影响它要回答的问题，
 * 而每刷一次列表就多打十几次请求是另一回事。整页重新挂载时缓存仍在，
 * 同一个库的库里也不会重复问。
 */
export function useIngestTrends(kbIds: readonly string[]): Record<string, IngestTrend> {
  const [, bump] = useState(0)
  const wanted = useRef(kbIds)
  wanted.current = kbIds
  // 数组每次渲染都是新的，直接进依赖会让 effect 每帧重跑；id 里不会有换行，
  // 所以拼成一行当"这一批库变没变"的键
  const key = kbIds.join('\n')

  useEffect(() => {
    const listener = () => bump((value) => value + 1)
    trendListeners.add(listener)
    return () => {
      trendListeners.delete(listener)
    }
  }, [])

  useEffect(() => {
    for (const kbId of wanted.current) {
      if (!trendCache.has(kbId)) void loadTrend(kbId)
    }
  }, [key])

  const trends: Record<string, IngestTrend> = {}
  for (const kbId of kbIds) {
    const entry = trendCache.get(kbId)
    if (entry) trends[kbId] = entry
  }
  return trends
}

/** 测试用：把模块级缓存清干净（生产代码不该调它）。 */
export function resetIngestTrendCache(): void {
  trendCache.clear()
  trendInflight.clear()
  for (const listener of trendListeners) listener()
}
