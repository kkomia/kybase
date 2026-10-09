/**
 * 「近 90 天检索命中」——知识库卡片上第二根迷你趋势图的数据层。
 *
 * ## 与入库那根的分工
 *
 * 入库那根（`ingestTrend.ts`）量的是"这个库在长"，这一根量的是"这个库在被用"：
 * 每天有多少条命中是它贡献的。两件事，所以口径、取数方式与配色都不共用
 * （颜色见 `knowledge.css` 的 `.kb-trend-hits`）。
 *
 * ## 一次请求覆盖全部卡片
 *
 * 后端 v0.61 起有按库的命中趋势（`GET /stats/search-hits`），一次就把窗口内所有库
 * 都给回来——所以这里**不发每库一次**的请求（入库那根不得不那样：创建时间只存在于
 * 文档行上）。因此本模块的缓存不是"每库一份"，而是"这一趟读"：一页十来个库，
 * 也只多打一次请求。
 *
 * ## 稀疏要自己补零
 *
 * `by_kb[].series` 是**稀疏**的（只有真有记录的那些天，升序），而趋势图要的是
 * 天数恒定的曲线：窗口里没有记录的那天就是 0 条。补零在这里做（`searchHitSeries`），
 * 所以卡片拿到的永远是长度 = 窗口天数的数组。
 *
 * **没出现在 `by_kb` 里的库不是出错**：后端只给"窗口内真有记录"的库，缺条 = 这 90 天
 * 一次都没被查过（或一次都没命中）——那是「无命中」，不是「—」。
 *
 * ## 空数据不造数
 *
 * 与入库那根同一条纪律：**读不到**给「—」（`status !== 'ready'`），**读到了但没有命中**
 * 给「无命中」+ 一根压在底部的平线。两句不同的话，不能混成一个。
 */
import { useEffect, useRef, useState } from 'react'

import { getSearchHits, type SearchHitPoint } from '@/api/stats'
import { dayKeyOf } from '@/features/knowledge/ingestTrend'
import { formatCount } from '@/lib/format'

/** 窗口长度（天）。卡片上的文案写的就是它，改这里等于改文案。 */
export const SEARCH_TREND_DAYS = 90

/* ---------------------------------------------------------------- 聚合（纯函数） */

/**
 * 把后端的**稀疏**曲线补成"窗口天数恒定"的一条（缺的那些天 = 0）。
 *
 * 返回的一串数长度恒为 `days`：末位是"今天"，首位是 `days - 1` 天前。日界口径与
 * 入库那根**共用** `dayKeyOf` 与 `new Date(y, m, d - n)`（同一天的两根线不能各算各的
 * 一天）；`day` 直接用后端给的那串 `YYYY-MM-DD` 比，**不经过 `new Date()`**——
 * 那会把一个纯日期按 UTC 零点解析，负时区里就退回前一天。
 *
 * 只数 `candidates`（收敛**之前**那个库贡献的条数）：卡片量的是"这个库被问出了多少
 * 东西"，而 `kept` 是收敛的结果（每次调用方要几条都会变），拿它画线量的是调用方的口味。
 * `kept` 留在读数的悬浮说明里。
 */
export function searchHitSeries(
  points: readonly SearchHitPoint[],
  now: Date = new Date(),
  days: number = SEARCH_TREND_DAYS,
): number[] {
  const indexOf = new Map<string, number>()
  for (let index = 0; index < days; index += 1) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (days - 1 - index))
    indexOf.set(dayKeyOf(day), index)
  }

  const series = new Array<number>(days).fill(0)
  for (const point of points) {
    const index = indexOf.get(point.day)
    if (index === undefined) continue
    series[index] = point.candidates
  }
  return series
}

/* ---------------------------------------------------------------- 这一趟读的状态 */

export interface SearchTrend {
  status: 'loading' | 'ready' | 'error'
  /** 按天的**候选**条数（长度 = 窗口天数，末位是今天）；`ready` 之外是空数组。 */
  series: number[]
  /** 窗口内的候选总数（线旁边那个读数）。 */
  total: number
  /** 窗口内收敛后真的返回的条数（读数的悬浮说明用它）。 */
  kept: number
}

/** 某一座库在窗口内的读数（`by_kb` 里那一份，已经补成连续曲线）。 */
interface KbTrend {
  series: number[]
  total: number
  kept: number
}

/**
 * 这一趟读的状态。
 *
 * `ready` 里的 `byKb` 只含**真有记录**的库（后端就是这么给的），缺条由
 * `trendOf` 当场翻成"全零 + 无命中"——不在缓存里塞一堆 90 长度的零数组。
 */
type FetchState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; byKb: Map<string, KbTrend> }

/** 全部卡片共用这一份（见头注"一次请求覆盖全部卡片"）。 */
let fetchState: FetchState = { status: 'idle' }
/** 正在飞的那一趟（单飞：几座库同一批挂上来只发一次）。 */
let inflight: Promise<void> | null = null
const listeners = new Set<() => void>()

const LOADING_TREND: SearchTrend = { status: 'loading', series: [], total: 0, kept: 0 }
const ERROR_TREND: SearchTrend = { status: 'error', series: [], total: 0, kept: 0 }

function setState(next: FetchState): void {
  fetchState = next
  for (const listener of listeners) listener()
}

/** 一条压在底部的平线（读到了、但窗口里一条都没有）。 */
function zeroSeries(): number[] {
  return new Array<number>(SEARCH_TREND_DAYS).fill(0)
}

/** 这一趟取数（见头注"一次请求覆盖全部卡片"）。 */
function load(): Promise<void> {
  if (inflight) return inflight

  setState({ status: 'loading' })
  inflight = (async () => {
    try {
      const data = await getSearchHits(SEARCH_TREND_DAYS)
      const byKb = new Map<string, KbTrend>()
      for (const kb of data.by_kb) {
        byKb.set(kb.kb_id, {
          series: searchHitSeries(kb.series),
          // 读数用后端数的那个总数，而不是把补零后的曲线再加一遍：窗口边界那一天
          // 的归属可能在两台机器的时区之间错开，而"窗口内一共几条"只有一个答案
          total: kb.candidates,
          kept: kb.kept,
        })
      }
      setState({ status: 'ready', byKb })
    } catch {
      // **静默降级**：这一份是卡片上的补充读数，读不到就在卡片上写「—」，
      // 不在列表页上多摆一行红字（那一行留给库列表自己那条读，见 `kb-error-line`）。
      setState({ status: 'error' })
    } finally {
      inflight = null
    }
  })()
  return inflight
}

/** 某一座库当前的读数（缓存里没有 = 这 90 天没有记录 = 无命中）。 */
function trendOf(kbId: string): SearchTrend {
  if (fetchState.status === 'ready') {
    const entry = fetchState.byKb.get(kbId)
    if (!entry) return { status: 'ready', series: zeroSeries(), total: 0, kept: 0 }
    return { status: 'ready', series: entry.series, total: entry.total, kept: entry.kept }
  }
  if (fetchState.status === 'error') return ERROR_TREND
  return LOADING_TREND
}

/**
 * 订阅这几座库的检索命中趋势（**一趟请求**，见头注）。
 *
 * `kbIds` 传空数组 = 一个请求都不发（行形态才画趋势图，行形态不画）。
 * 与入库那根一样**不做失效重取**：库列表刷新不重拉——那张图量的是"近 90 天的节奏"，
 * 几分钟的延迟不影响它要回答的问题，而每刷一次列表就重打一趟是另一回事。
 */
export function useSearchTrends(kbIds: readonly string[]): Record<string, SearchTrend> {
  const [, bump] = useState(0)
  const wanted = useRef(kbIds)
  wanted.current = kbIds
  // 数组每次渲染都是新的，直接进依赖会让 effect 每帧重跑；id 里不会有换行，
  // 所以拼成一行当"这一批库变没变"的键
  const key = kbIds.join('\n')

  useEffect(() => {
    const listener = () => bump((value) => value + 1)
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }, [])

  useEffect(() => {
    // 已经有结论（ready / error）就不重发：这是一份 90 天的趋势，不是实时数
    if (wanted.current.length > 0 && fetchState.status === 'idle') void load()
  }, [key])

  const trends: Record<string, SearchTrend> = {}
  for (const kbId of kbIds) trends[kbId] = trendOf(kbId)
  return trends
}

/**
 * 读数那一行的文字。
 *
 * **读不到**（还在路上 / 这一读失败）写「—」——与别处"没有结论"是同一个记号，
 * 而不是 0；**读到了但没有命中**写「无命中」：那才是 0 该说的话
 * （这 90 天里没人问过，或问了但一条都没命中）。
 */
export function searchTrendTextOf(trend: SearchTrend | undefined): string {
  if (trend?.status !== 'ready') return '—'
  if (trend.total === 0) return '无命中'
  return `${formatCount(trend.total)} 条`
}

/**
 * 读数被收敛砍掉一部分时的悬浮说明（卡片上放不下第二行字）。
 *
 * 读数是**候选**（收敛前），用户真正拿到的是收敛后的那些——两个数不一样时这件事
 * 必须能查，所以给一句；一样时不给（那是同义复述）。
 */
export function searchTrendTitleOf(trend: SearchTrend | undefined): string | undefined {
  if (trend?.status !== 'ready' || trend.total === 0 || trend.kept >= trend.total) return undefined
  return `候选 ${formatCount(trend.total)} 条，返回 ${formatCount(trend.kept)} 条`
}

/** 测试用：把模块级状态清干净（生产代码不该调它）。 */
export function resetSearchTrendCache(): void {
  fetchState = { status: 'idle' }
  inflight = null
  for (const listener of listeners) listener()
}
