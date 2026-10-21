/**
 * 「近 90 天检索命中」迷你趋势图的用例（`features/knowledge/searchTrend.ts` +
 * `Sparkline.tsx` + 卡片上那一段）。
 *
 * 与入库那根分开一份，因为**口径完全不同**（那是另一条数据源、另一种取数方式）：
 *
 * 1. **稀疏补零**（纯函数）：后端只给真有记录的那些天，曲线要补成窗口天数恒定，
 *    窗口外的点丢掉、缺的天是 0；
 * 2. **一趟请求覆盖全部卡片**：`GET /stats/search-hits?days=90` 只发一次，
 *    不是每库一次（入库那根才是每库一次）；
 * 3. **两种"没有"分得开**：读不到写「—」，读到了但窗口里没记录写「无命中」——
 *    `by_kb` 里没有这座库就是"没人问过 / 问了一条没命中"，不是出错。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeBasesView } from '@/features/knowledge'
import { dayKeyOf, resetIngestTrendCache } from '@/features/knowledge/ingestTrend'
import {
  resetSearchTrendCache,
  searchHitSeries,
  searchTrendTextOf,
  searchTrendTitleOf,
  SEARCH_TREND_DAYS,
  type SearchTrend,
} from '@/features/knowledge/searchTrend'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import type { KnowledgeBase } from '@/api/knowledgeBases'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/knowledgeBases', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/knowledgeBases')>()),
  listKnowledgeBases: vi.fn(),
  createKnowledgeBase: vi.fn(),
  updateKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
  getKnowledgeBaseImpact: vi.fn(),
}))

vi.mock('@/api/modelRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/modelRegistry')>()),
  getRegistry: vi.fn(),
}))

// 入库那一根也在这张卡上（它每库一次）：这里给空结果，本文件只钉命中那根
vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  listDocuments: vi.fn(),
}))

vi.mock('@/api/stats', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/stats')>()),
  getSearchHits: vi.fn(),
}))

import { listKnowledgeBases } from '@/api/knowledgeBases'
import { listDocuments } from '@/api/documents'
import { getRegistry } from '@/api/modelRegistry'
import { getSearchHits, type SearchHits } from '@/api/stats'

const listKbsMock = vi.mocked(listKnowledgeBases)
const listDocsMock = vi.mocked(listDocuments)
const registryMock = vi.mocked(getRegistry)
const searchHitsMock = vi.mocked(getSearchHits)

function makeKB(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: 'kb-1',
    name: '产品手册',
    description: '',
    embedding_model_id: 'bge-m3',
    embedding_dim: 1024,
    chunk_strategy: 'recursive',
    chunk_size: 512,
    chunk_overlap: 64,
    suggested_enabled: false,
    suggested_count: 3,
    suggested_model_pk: null,
    suggested_prompt: '',
    system_prompt: '',
    wiki_enabled: false,
    created_at: '2026-09-01T10:00:00Z',
    can_manage: true,
    can_write: true,
    document_count: 7,
    last_activity: '2026-09-20T10:00:00Z',
    ...overrides,
  }
}

/** 窗口内某一天的读数（`days` 天前的本地日，与模块自己的日界口径同源）。 */
function daysAgoKey(days: number): string {
  const now = new Date()
  return dayKeyOf(new Date(now.getFullYear(), now.getMonth(), now.getDate() - days))
}

function makeHits(overrides: Partial<SearchHits> = {}): SearchHits {
  return { days: SEARCH_TREND_DAYS, candidates: 0, kept: 0, searches: 0, by_kb: [], ...overrides }
}

function renderView() {
  return render(
    <MemoryRouter initialEntries={['/knowledge-bases']}>
      <KnowledgeBasesView />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  resetIngestTrendCache()
  resetSearchTrendCache()
  registryMock.mockResolvedValue({
    providers: [],
    models: [],
    slots: [],
    provider_kinds: {},
    capabilities: {},
    provider_presets: [],
  })
  listDocsMock.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 })
  searchHitsMock.mockResolvedValue(makeHits())
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
  resetIngestTrendCache()
  resetSearchTrendCache()
})

/* ------------------------------------------------------------------ 稀疏补零 */

describe('检索命中曲线的补零', () => {
  /** 固定"今天"：2026-10-08（本地）。窗口 = 7/11 … 10/8 这 90 天。 */
  const NOW = new Date(2026, 9, 8, 15, 30)

  it('稀疏的几点补成窗口天数恒定的一条：末位是今天，首位是 89 天前', () => {
    const series = searchHitSeries(
      [
        { day: '2026-10-08', candidates: 3, kept: 1 },
        { day: '2026-10-05', candidates: 2, kept: 2 },
        { day: '2026-07-11', candidates: 7, kept: 7 },
      ],
      NOW,
    )

    expect(series).toHaveLength(SEARCH_TREND_DAYS)
    expect(series[SEARCH_TREND_DAYS - 1]).toBe(3)
    expect(series[SEARCH_TREND_DAYS - 4]).toBe(2)
    expect(series[0]).toBe(7)
    // 没有记录的那些天是 0（补零，不是插值也不是平滑）
    expect(series.filter((value) => value > 0)).toHaveLength(3)
    expect(series.reduce((sum, value) => sum + value, 0)).toBe(12)
  })

  it('窗口之外的点丢掉：90 天前那一天不在这一格上', () => {
    const series = searchHitSeries([{ day: '2026-07-10', candidates: 9, kept: 9 }], NOW)

    expect(series.reduce((sum, value) => sum + value, 0)).toBe(0)
  })

  it('读的是 candidates（收敛前），不是 kept：画的是"这个库被问出了多少"', () => {
    const series = searchHitSeries([{ day: '2026-10-08', candidates: 12, kept: 2 }], NOW)

    expect(series[SEARCH_TREND_DAYS - 1]).toBe(12)
  })
})

/* ------------------------------------------------------------------ 读数的两句 */

describe('读数的文字与悬浮说明', () => {
  const ready = (total: number, kept: number): SearchTrend => ({
    status: 'ready',
    series: [],
    total,
    kept,
  })

  it('读不到写「—」，读到了但没有命中写「无命中」（两句不同的话）', () => {
    expect(searchTrendTextOf(undefined)).toBe('—')
    expect(searchTrendTextOf({ status: 'loading', series: [], total: 0, kept: 0 })).toBe('—')
    expect(searchTrendTextOf({ status: 'error', series: [], total: 0, kept: 0 })).toBe('—')
    expect(searchTrendTextOf(ready(0, 0))).toBe('无命中')
    expect(searchTrendTextOf(ready(1200, 300))).toBe('1,200 条')
  })

  it('收敛砍掉一部分时才给悬浮说明：候选与返回不一样才需要解释', () => {
    expect(searchTrendTitleOf(ready(45, 4))).toBe('候选 45 条，返回 4 条')
    expect(searchTrendTitleOf(ready(4, 4))).toBeUndefined()
    expect(searchTrendTitleOf(ready(0, 0))).toBeUndefined()
    expect(searchTrendTitleOf(undefined)).toBeUndefined()
  })
})

/* ------------------------------------------------------------------ 卡片上的那一段 */

/**
 * 2026-10 界面重做之后，卡片上不再有"第二根检索命中线"：入库与检索命中是
 * **同一件事的两种**（库在长 / 库在被用），合成一条 30 天活动线 + 一行读数
 * 「入库 N 篇 · 检索 M 次」。所以这里断的是"命中数进了读数、进了同一条线"，
 * 取数口径（一趟请求覆盖全部库、带的是 90 天窗口）一个字没变。
 */
describe('卡片上的检索命中趋势', () => {
  it('一趟请求覆盖全部卡片，每座库的命中数进读数（没记录的写 0 次）', async () => {
    listKbsMock.mockResolvedValue({
      items: [makeKB({ id: 'kb-1' }), makeKB({ id: 'kb-2', name: '没人问过的库' })],
    })
    searchHitsMock.mockResolvedValue(
      makeHits({
        candidates: 5,
        kept: 2,
        searches: 3,
        by_kb: [
          {
            kb_id: 'kb-1',
            name: '产品手册',
            candidates: 5,
            kept: 2,
            searches: 3,
            // **稀疏**：只有真有记录的那些天
            series: [
              { day: daysAgoKey(0), candidates: 2, kept: 1 },
              { day: daysAgoKey(9), candidates: 3, kept: 1 },
            ],
          },
        ],
      }),
    )

    const { container } = renderView()

    // 入库这一读是成功空结果（0 篇），所以读数里能单独看出命中数：kb-1 5 次、kb-2 0 次
    expect(await screen.findByText('入库 0 篇 · 检索 5 次')).toBeInTheDocument()
    expect(await screen.findByText('入库 0 篇 · 检索 0 次')).toBeInTheDocument()
    expect(screen.getAllByText('近 30 天活动')).toHaveLength(2)

    // 一次请求，不是每库一次（入库那根才是每库一次）
    await waitFor(() => expect(searchHitsMock).toHaveBeenCalledTimes(1))
    expect(searchHitsMock).toHaveBeenCalledWith(SEARCH_TREND_DAYS)

    const sparks = Array.from(container.querySelectorAll('.kb-card-activity .kb-spark'))
    expect(sparks).toHaveLength(2)
    // 有命中的那座：补零后的连续曲线（30 天那一格画全）；没记录的那座：压在底部的平线
    expect(sparks[0].getAttribute('viewBox')).toBe('0 0 30 40')
    expect(sparks[0].classList.contains('kb-spark-empty')).toBe(false)
    expect(sparks[1].getAttribute('viewBox')).toBe('0 0 30 40')
    expect(sparks[1].classList.contains('kb-spark-empty')).toBe(true)
  })

  it('读数那一行挂悬浮说明：候选与返回不一样时把命中数也写清楚', async () => {
    listKbsMock.mockResolvedValue({ items: [makeKB({ id: 'kb-1' })] })
    searchHitsMock.mockResolvedValue(
      makeHits({
        candidates: 5,
        kept: 2,
        by_kb: [
          {
            kb_id: 'kb-1',
            name: '产品手册',
            candidates: 5,
            kept: 2,
            searches: 3,
            series: [{ day: daysAgoKey(0), candidates: 5, kept: 2 }],
          },
        ],
      }),
    )

    renderView()

    const value = await screen.findByTitle(/候选 5 条，返回 2 条/)
    expect(value.textContent).toBe('入库 0 篇 · 检索 5 次')
  })

  it('这一读失败时检索数写「—」（读不到不是 0），列表页也不再摆一行红字', async () => {
    listKbsMock.mockResolvedValue({ items: [makeKB({ id: 'kb-1' })] })
    searchHitsMock.mockRejectedValue(new Error('服务暂时不可用'))

    renderView()

    expect(await screen.findByText('产品手册')).toBeInTheDocument()
    // 命中那一份读失败 → 检索数写「—」；入库这一读是成功的空结果 → 0 篇
    expect(await screen.findByText(/入库 0 篇 · 检索 — 次/)).toBeInTheDocument()
    expect(screen.queryByText(/服务暂时不可用/)).toBeNull()
  })

  it('行形态（> 12 个库）不画活动线，命中趋势这一趟请求也不发', async () => {
    listKbsMock.mockResolvedValue({
      items: Array.from({ length: 13 }, (_, index) =>
        makeKB({ id: `kb-${index}`, name: `库 ${index}` }),
      ),
    })

    const { container } = renderView()

    await waitFor(() => expect(container.querySelectorAll('.kb-row-link')).toHaveLength(13))
    expect(container.querySelectorAll('.kb-card-activity')).toHaveLength(0)
    expect(searchHitsMock).not.toHaveBeenCalled()
  })
})
