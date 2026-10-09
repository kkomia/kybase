/**
 * 「近 90 天入库」迷你趋势图的用例（`features/knowledge/ingestTrend.ts` + `Sparkline.tsx`
 * + 卡片上那一段）。
 *
 * 三块：
 *
 * 1. **聚合**（纯函数）：按天计数、窗口对齐今天（**本地日**）、窗口外与解析不出的不计；
 * 2. **空数据形态**：一条都没有时给的是全零平线 + 「无入库」，而且**每张卡都占着同一块
 *    `.kb-trend`**——趋势图不许把卡片撑得忽高忽低；
 * 3. **卡片上的接线**：每库一次请求（`limit=200` 起手，倒序下够到窗口就停）、行形态
 *    一个请求都不发、读不到时写「—」而不是 0（0 是一句结论，读不到不是）。
 *
 * 卡片上**第二根**趋势线（近 90 天检索命中）有自己的用例文件
 * （`knowledge-search-trend.test.tsx`）：它的口径、取数与这根完全不同。这里只把
 * 命中趋势的取数钉成空结果——不为断言它，只为让这一页**不落到没预备的请求上**
 * （判定与计数才不被那边带偏）。
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
  it('每库各拉一次、如实写「近 90 天入库 N 篇」（入库这一根不叫检索命中）', async () => {
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
              // 窗口外的老文档：拉回来了也不该算进这 90 天
              makeDoc({ id: 'd4', created_at: daysAgoISO(200) }),
            ]
          : [makeDoc({ id: 'd5', created_at: daysAgoISO(2) })],
      total: 4,
      limit: filter.limit ?? 200,
      offset: filter.offset ?? 0,
    }))

    renderView()

    expect(await screen.findByText('3 篇')).toBeInTheDocument()
    expect(await screen.findByText('1 篇')).toBeInTheDocument()
    // 两根线各写各的口径：这是**入库**那一根（「N 篇」），检索命中那根是另一行、
    // 另一个单位（「N 条」），也走另一条接口（见 `knowledge-search-trend.test.tsx`）
    expect(screen.getAllByText(`近 ${INGEST_TREND_DAYS} 天入库`)).toHaveLength(2)
    expect(screen.getAllByText(`近 ${INGEST_TREND_DAYS} 天检索命中`)).toHaveLength(2)
    expect(screen.getAllByText('无命中')).toHaveLength(2)
    expect(screen.queryByText('无入库')).toBeNull()
    // 每库一次，起手一页 200 篇（十几个库就是十几次）
    expect(listDocsMock).toHaveBeenCalledTimes(2)
    expect(listDocsMock).toHaveBeenNthCalledWith(1, 'kb-1', { limit: 200, offset: 0 })
    expect(listDocsMock).toHaveBeenNthCalledWith(2, 'kb-2', { limit: 200, offset: 0 })
  })

  it('一条入库都没有：全零平线 + 「无入库」，而且每张卡**都占着同一块位置**', async () => {
    listKbsMock.mockResolvedValue({
      items: [makeKB({ id: 'kb-1', name: 'A 库' }), makeKB({ id: 'kb-2', name: 'B 库' })],
    })
    // 后端给不出创建时间（老数据 / 未记录）：如实说"无入库"，不编几条出来
    listDocsMock.mockResolvedValue({
      items: [makeDoc({ id: 'd1', created_at: null }), makeDoc({ id: 'd2', created_at: null })],
      total: 2,
      limit: 200,
      offset: 0,
    })

    const { container } = renderView()

    await waitFor(() => expect(screen.getAllByText('无入库')).toHaveLength(2))
    // 两根线各占一块 `.kb-trend`（图高由 CSS 定死，卡片不会被撑高）
    expect(container.querySelectorAll('.kb-trend')).toHaveLength(4)
    expect(container.querySelectorAll('.kb-spark-empty')).toHaveLength(4)
    // 空数据也把 90 天那一格画全（一条压在底部的平线），而不是一块空白
    const spans = Array.from(container.querySelectorAll('.kb-spark'))
    expect(spans).toHaveLength(4)
    for (const svg of spans) expect(svg.getAttribute('viewBox')).toBe(`0 0 ${INGEST_TREND_DAYS} 28`)
  })

  it('这一读失败时写「—」（读不到不是 0），列表页也不再摆一行红字', async () => {
    listKbsMock.mockResolvedValue({ items: [makeKB({ id: 'kb-1', name: '产品手册' })] })
    listDocsMock.mockRejectedValue(new Error('服务暂时不可用'))

    renderView()

    expect(await screen.findByText('产品手册')).toBeInTheDocument()
    // 只有入库这一根读失败（命中趋势那一份给的是空结果）：'—' 只该出现一次
    expect(await screen.findByText('—')).toBeInTheDocument()
    expect(screen.queryByText('无入库')).toBeNull()
    expect(screen.queryByText('服务暂时不可用')).toBeNull()
  })

  it('一页盖不住窗口就继续翻（倒序），翻到上限才把读数写成「≥N 篇」', async () => {
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

    expect(await screen.findByText('≥600 篇')).toBeInTheDocument()
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

    expect(await screen.findByText('199 篇')).toBeInTheDocument()
    expect(listDocsMock).toHaveBeenCalledTimes(1)
  })

  it('行形态（> 12 个库）不画趋势图，也就一个文档请求都不发', async () => {
    listKbsMock.mockResolvedValue({
      items: Array.from({ length: 13 }, (_, index) =>
        makeKB({ id: `kb-${index}`, name: `库 ${index}` }),
      ),
    })

    const { container } = renderView()

    await waitFor(() => expect(container.querySelectorAll('.kb-row-link')).toHaveLength(13))
    expect(container.querySelectorAll('.kb-trend')).toHaveLength(0)
    expect(listDocsMock).not.toHaveBeenCalled()
    // 命中趋势也一样：行形态连那一趟请求都不该发
    expect(searchHitsMock).not.toHaveBeenCalled()
  })
})
