/**
 * 「近 30 天活动」迷你趋势图的用例（`features/knowledge/ingestTrend.ts` + `Sparkline.tsx`
 * + 卡片底部那一条线）。
 *
 * 四块：
 *
 * 1. **聚合**（纯函数）：按天计数、窗口对齐今天（**本地日**）、窗口外与解析不出的不计；
 * 2. **空数据形态**：一条都没有时读数是「入库 0 篇 · 检索 0 次」、线是一条压在底部的平线，
 *    而且**每张卡都占着同一块 `.kb-card-activity`**——趋势图不许把卡片撑得忽高忽低；
 * 3. **卡片上的接线**：每库一次请求（`limit=200` 起手，倒序下够到窗口就停）、
 *    行形态一个请求都不发、读不到时读数写「—」而不是 0（0 是一句结论，读不到不是）；
 * 4. **被截断的入库数带 `≥`**：翻到上限也只统计了最近 600 篇，读数必须说"只多不少"。
 *
 * ## 2026-10 界面重做改了口径
 *
 * 旧卡片上摆两根 90 天的线（入库 / 检索命中）、读数四行、颜色两种；现在合成**一条
 * 30 天的活动线**、一行读数「入库 N 篇 · 检索 M 次」。同一文件里聚合与取数的断言
 * （按天计数、分页次数、行形态不发请求）**一个字没改**——行为没变，只有呈现换了。
 * 检索命中趋势自己的口径在 `knowledge-search-trend.test.tsx`；这里把它钉成空结果。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeBasesView } from '@/features/knowledge'
import {
  INGEST_TREND_DAYS,
  ingestSeries,
  resetIngestTrendCache,
  seriesTotal,
} from '@/features/knowledge/ingestTrend'
import { resetSearchTrendCache } from '@/features/knowledge/searchTrend'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import type { DocumentSummary } from '@/api/documents'
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

// 趋势图的取数口径全在这里：每库一次 `GET /knowledge-bases/{id}/documents`
vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  listDocuments: vi.fn(),
}))

// 检索命中趋势（卡片上第二根线）走 `GET /stats/search-hits`：这里给空结果，
// 让本文件只钉入库那根（那一根单独一套口径，见 `knowledge-search-trend.test.tsx`）
vi.mock('@/api/stats', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/stats')>()),
  getSearchHits: vi.fn(),
}))

import { listKnowledgeBases } from '@/api/knowledgeBases'
import { listDocuments } from '@/api/documents'
import { getRegistry } from '@/api/modelRegistry'
import { getSearchHits } from '@/api/stats'

const listKbsMock = vi.mocked(listKnowledgeBases)
const registryMock = vi.mocked(getRegistry)
const listDocsMock = vi.mocked(listDocuments)
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

function makeDoc(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: 'doc-1',
    knowledge_base_id: 'kb-1',
    name: '说明书.pdf',
    source_kind: 'upload',
    stage: 'indexed',
    size_bytes: 2048,
    mime_type: 'application/pdf',
    page_count: 12,
    is_split: false,
    error: null,
    chunk_count: 8,
    uploaded_by: null,
    uploaded_by_name: '',
    folder_id: null,
    disabled: false,
    original_kind: 'pdf',
    created_at: null,
    updated_at: null,
    question_count: 0,
    questioned_chunk_count: 0,
    questions_pending: false,
    summary: '',
    progress: null,
    ...overrides,
  }
}

/** 本地时间的 ISO 串：固定 `now` 的用例靠它把"哪一天"钉死在日历上。 */
function localISO(year: number, monthIndex: number, day: number, hour = 12): string {
  return new Date(year, monthIndex, day, hour).toISOString()
}

/** `days` 天前的本地正午：正午不怕夏令时那两天的零点偏移。 */
function daysAgoISO(days: number): string {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - days, 12).toISOString()
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
  // 卡片上第二根线（检索命中）：窗口内一条记录都没有
  searchHitsMock.mockResolvedValue({ days: 90, candidates: 0, kept: 0, searches: 0, by_kb: [] })
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
  resetIngestTrendCache()
  resetSearchTrendCache()
})

/* ------------------------------------------------------------------ 按天聚合 */

describe('入库节奏的聚合', () => {
  /** 固定"今天"：2026-10-08（本地）。窗口 = 7/11 … 10/8 这 90 天。 */
  const NOW = new Date(2026, 9, 8, 15, 30)

  it('按天计数：末位是今天，首位是 89 天前，同一天的多条累加', () => {
    const series = ingestSeries(
      [
        localISO(2026, 9, 8, 1),
        localISO(2026, 9, 8, 23),
        localISO(2026, 9, 5, 12),
        localISO(2026, 6, 11, 12),
      ],
      NOW,
    )

    expect(series).toHaveLength(INGEST_TREND_DAYS)
    expect(series[INGEST_TREND_DAYS - 1]).toBe(2)
    expect(series[INGEST_TREND_DAYS - 4]).toBe(1)
    expect(series[0]).toBe(1)
    expect(seriesTotal(series)).toBe(4)
    // 其余各天一条都没有（不造数、不平滑）
    expect(series.filter((value) => value > 0)).toHaveLength(3)
  })

  it('窗口是闭区间：今天零点那条算今天，90 天前那条已经在窗口外', () => {
    // 本地零点：日界按本地日历切，而不是跟着 UTC 走
    const series = ingestSeries(
      [localISO(2026, 9, 8, 0), localISO(2026, 6, 10, 23), localISO(2026, 6, 11, 0)],
      NOW,
    )

    expect(series[INGEST_TREND_DAYS - 1]).toBe(1)
    expect(series[0]).toBe(1)
    expect(seriesTotal(series)).toBe(2)
  })

  it('空值、解析不出的时间、以及"明天"（时钟偏差）一概不计', () => {
    const series = ingestSeries([null, undefined, '', '不是时间', localISO(2026, 9, 9, 1)], NOW)

    expect(series).toHaveLength(INGEST_TREND_DAYS)
    expect(seriesTotal(series)).toBe(0)
    expect(series.every((value) => value === 0)).toBe(true)
  })
})

/* ------------------------------------------------------------------ 卡片上的那一段 */

describe('卡片上的趋势图', () => {
  it('每库各拉一次，读数写「入库 N 篇 · 检索 M 次」', async () => {
    listKbsMock.mockResolvedValue({
      items: [
        makeKB({ id: 'kb-1', name: '产品手册' }),
        makeKB({ id: 'kb-2', name: '没有简介的库' }),
      ],
    })
    listDocsMock.mockImplementation(async (kbId, filter = {}) => ({
      items:
        kbId === 'kb-1'
          ? [
              makeDoc({ id: 'd1', created_at: daysAgoISO(0) }),
              makeDoc({ id: 'd2', created_at: daysAgoISO(0) }),
              makeDoc({ id: 'd3', created_at: daysAgoISO(12) }),
              // 窗口外的老文档：拉回来了也不该算进这 30 天
              makeDoc({ id: 'd4', created_at: daysAgoISO(200) }),
            ]
          : [makeDoc({ id: 'd5', created_at: daysAgoISO(2) })],
      total: 4,
      limit: filter.limit ?? 200,
      offset: filter.offset ?? 0,
    }))

    renderView()

    // 入库与检索命中合成一行：入库是"这个库在长"，检索命中是"这个库在被用"
    expect(await screen.findByText('入库 3 篇 · 检索 0 次')).toBeInTheDocument()
    expect(await screen.findByText('入库 1 篇 · 检索 0 次')).toBeInTheDocument()
    expect(screen.getAllByText('近 30 天活动')).toHaveLength(2)
    // 每库一次，起手一页 200 篇（十几个库就是十几次）
    expect(listDocsMock).toHaveBeenCalledTimes(2)
    expect(listDocsMock).toHaveBeenNthCalledWith(1, 'kb-1', { limit: 200, offset: 0 })
    expect(listDocsMock).toHaveBeenNthCalledWith(2, 'kb-2', { limit: 200, offset: 0 })
  })

  it('一条活动都没有：读数全零、平线，而且每张卡**都占着同一块位置**', async () => {
    listKbsMock.mockResolvedValue({
      items: [makeKB({ id: 'kb-1', name: 'A 库' }), makeKB({ id: 'kb-2', name: 'B 库' })],
    })
    // 后端给不出创建时间（老数据 / 未记录）：如实说 0，不编几条出来
    listDocsMock.mockResolvedValue({
      items: [makeDoc({ id: 'd1', created_at: null }), makeDoc({ id: 'd2', created_at: null })],
      total: 2,
      limit: 200,
      offset: 0,
    })

    const { container } = renderView()

    await waitFor(() => expect(screen.getAllByText('入库 0 篇 · 检索 0 次')).toHaveLength(2))
    // 每张卡一块活动区、一条 30 天 × 40 高 的线（图高由 CSS 定死，卡片不会被撑高）
    expect(container.querySelectorAll('.kb-card-activity')).toHaveLength(2)
    const spans = Array.from(container.querySelectorAll('.kb-spark'))
    expect(spans).toHaveLength(2)
    for (const svg of spans) expect(svg.getAttribute('viewBox')).toBe('0 0 30 40')
    // 全零时线是"空的"，但仍画出来（一根压在底部的平线），不是一块空白
    expect(container.querySelectorAll('.kb-spark-empty')).toHaveLength(2)
  })

  it('这一读失败时入库数写「—」（读不到不是 0），列表页也不再摆一行红字', async () => {
    listKbsMock.mockResolvedValue({ items: [makeKB({ id: 'kb-1', name: '产品手册' })] })
    listDocsMock.mockRejectedValue(new Error('服务暂时不可用'))

    renderView()

    expect(await screen.findByText('产品手册')).toBeInTheDocument()
    // 只有入库这一根读失败（命中趋势那一份给的是空结果）：入库数写「—」，检索仍是真读数
    expect(await screen.findByText(/入库 — 篇 · 检索 0 次/)).toBeInTheDocument()
    expect(screen.queryByText('服务暂时不可用')).toBeNull()
  })

  it('一页盖不住窗口就继续翻（倒序），翻到上限时读数带「≥」', async () => {
    listKbsMock.mockResolvedValue({ items: [makeKB({ id: 'kb-1', name: '产品手册' })] })
    // 90 天内的文档远超上限：三页（600 篇）全是今天的，怎么也盖不住窗口
    listDocsMock.mockImplementation(async (_kbId, filter = {}) => {
      const offset = filter.offset ?? 0
      return {
        items: Array.from({ length: 200 }, (_, index) =>
          makeDoc({ id: `doc-${offset + index}`, created_at: daysAgoISO(0) }),
        ),
        total: 1200,
        limit: 200,
        offset,
      }
    })

    renderView()

    expect(await screen.findByText('入库 ≥600 篇 · 检索 0 次')).toBeInTheDocument()
    expect(listDocsMock.mock.calls.map((call) => call[1])).toEqual([
      { limit: 200, offset: 0 },
      { limit: 200, offset: 200 },
      { limit: 200, offset: 400 },
    ])
  })

  it('倒序下够到窗口之前就停：第二页已经不必要，就不再发', async () => {
    listKbsMock.mockResolvedValue({ items: [makeKB({ id: 'kb-1', name: '产品手册' })] })
    listDocsMock.mockImplementation(async (_kbId, filter = {}) => {
      const offset = filter.offset ?? 0
      // 第一页满 200 篇，最后一条是 200 天前 → 窗口已经盖住，收工
      if (offset === 0) {
        return {
          items: Array.from({ length: 200 }, (_, index) =>
            makeDoc({
              id: `doc-${index}`,
              created_at: index === 199 ? daysAgoISO(200) : daysAgoISO(10),
            }),
          ),
          total: 400,
          limit: 200,
          offset: 0,
        }
      }
      return { items: [makeDoc({ id: 'doc-late' })], total: 400, limit: 200, offset }
    })

    renderView()

    expect(await screen.findByText('入库 199 篇 · 检索 0 次')).toBeInTheDocument()
    expect(listDocsMock).toHaveBeenCalledTimes(1)
  })

  it('行形态（> 12 个库）不画活动线，也就一个文档请求都不发', async () => {
    listKbsMock.mockResolvedValue({
      items: Array.from({ length: 13 }, (_, index) =>
        makeKB({ id: `kb-${index}`, name: `库 ${index}` }),
      ),
    })

    const { container } = renderView()

    await waitFor(() => expect(container.querySelectorAll('.kb-row-link')).toHaveLength(13))
    expect(container.querySelectorAll('.kb-card-activity')).toHaveLength(0)
    expect(container.querySelectorAll('.kb-spark')).toHaveLength(0)
    expect(listDocsMock).not.toHaveBeenCalled()
    // 命中趋势也一样：行形态连那一趟请求都不该发
    expect(searchHitsMock).not.toHaveBeenCalled()
  })
})
