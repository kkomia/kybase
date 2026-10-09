/**
 * 驾驶舱（旧 `views/DashboardView.vue`）的用例。
 *
 * 三条自我约束各一条用例：
 * 1. **数据没到也先把六个槽位画出来**（流式渲染：骨架立刻可见）；
 * 2. **用量还没回来时显示占位符而不是 0**——0 的含义是"确实没有调用"；
 * 3. **窗口太长时按周聚合**（那条趋势的聚合口径是纯函数，单独钉住）。
 *
 * `EChart` 在这里被替掉：jsdom 没有 canvas，真去 init 会直接把用例打挂。
 * 图表本身的"按需异步加载"由结构保证（`React.lazy` + 本文件只关心数据）。
 */
import { cleanup, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/stats', () => ({
  getDashboard: vi.fn(),
  getUsage: vi.fn(),
}))

vi.mock('@/features/misc/dashboard/EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    const series = (option.series as { type?: string }[] | undefined)?.[0]
    const xAxis = option.xAxis as
      { data?: unknown[]; axisLabel?: { interval?: number } } | undefined
    return (
      <div
        data-testid={`chart-${series?.type ?? 'unknown'}`}
        data-series={JSON.stringify(series ?? {})}
        data-labels={JSON.stringify(xAxis?.data ?? [])}
        data-interval={String(xAxis?.axisLabel?.interval ?? '')}
      />
    )
  },
}))

import { getDashboard, getUsage, type Dashboard } from '@/api/stats'
import {
  checkableTrend,
  DashboardPage,
  trendLabelInterval,
} from '@/features/misc/dashboard/DashboardPage'
import { HEAT_STEPS } from '@/features/misc/dashboard/ActivityHeatmap'
import { renderMisc } from '@/features/misc/testing/harness'
import { resetToasts } from '@/features/misc/shared/toast'

const getDashboardMock = vi.mocked(getDashboard)
const getUsageMock = vi.mocked(getUsage)

function dashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return {
    generated_at: '2026-09-23T10:00:00Z',
    window_days: 365,
    total_knowledge_bases: 2,
    total_documents: 10,
    total_chunks: 240,
    indexed_documents: 9,
    failed_documents: 1,
    running_tasks: 0,
    failed_tasks: 1,
    storage_bytes: 2048,
    recent_documents: 4,
    activity: [
      { day: '2026-09-21', documents: 1, chunks: 10, tasks: 2 },
      { day: '2026-09-22', documents: 3, chunks: 30, tasks: 4 },
    ],
    by_stage: { indexed: 9, failed: 1 },
    by_suffix: { pdf: 7, docx: 3 },
    by_source_kind: { upload: 10 },
    knowledge_bases: [
      {
        id: 'kb-1',
        name: '产品手册',
        embedding_model_id: 'BAAI/bge-m3',
        embedding_dim: 1024,
        documents: 10,
        chunks: 240,
        last_activity: '2026-09-22T10:00:00Z',
      },
    ],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  globalThis.fetch = (async (url: string) => {
    throw new TypeError(`这一份用例不发真请求：${String(url)}`)
  }) as unknown as typeof fetch
  getDashboardMock.mockResolvedValue(dashboard())
  getUsageMock.mockResolvedValue({
    days: 30,
    total: {
      calls: 120,
      prompt_tokens: 30000,
      completion_tokens: 8000,
      items: 240,
      unreported: 0,
      estimated: 60,
    },
    by_day: [],
    by_kind: [
      {
        kind: 'chat',
        label: '对话',
        calls: 100,
        prompt_tokens: 30000,
        completion_tokens: 8000,
        items: 0,
        unreported: 0,
        estimated: 0,
      },
      {
        kind: 'embed',
        label: '向量化',
        calls: 20,
        prompt_tokens: 0,
        completion_tokens: 0,
        items: 240,
        unreported: 0,
        estimated: 20,
      },
    ],
    by_model: [{ model: 'deepseek-chat', calls: 100 } as never],
    reported_calls: 100,
    estimated_calls: 20,
    unreported_calls: 0,
    estimated_tokens: 5000,
  } as never)
})

describe('驾驶舱', () => {
  it('大数按"结论在前"排列：文档与索引合并成一张卡，索引状态只说一次', async () => {
    renderMisc(<DashboardPage />)

    expect(await screen.findByText('产品手册')).toBeInTheDocument()
    // 文档与索引：主数字是文档总数，注解只说索引的**状态**（失败 / 待索引）
    expect(screen.getByText('文档与索引')).toBeInTheDocument()
    expect(screen.getByText('1 篇失败')).toBeInTheDocument()
    // 原来那张"索引完成率"卡已经并进去了：同一件事不再占两个首屏位置
    expect(screen.queryByText('索引完成率')).toBeNull()
    // 「近 365 天入库」**不再挂注解**（2026-09-24 删解释小字）：N 篇更早入库等于
    // 「文档与索引」的总数减这一格的数（两个数就在同一行），"全部在窗口内"则只是复述
    const recentCard = screen.getByText('近 365 天入库').closest('li') as HTMLElement
    expect(recentCard.querySelector('.m-figure-note')).toBeNull()
    expect(screen.queryByText(/更早入库|全部在窗口内/)).toBeNull()
    expect(screen.queryByText('占全部 10 篇')).toBeNull()
    // 知识库规模表的最近活动走相对时间
    expect(screen.getByText(/天前|小时前|刚刚|2026-/)).toBeInTheDocument()
  })

  it('大数与图例不再挂"这一页是什么"式的小字（用户点名的那几条，删了就不许长回来）', async () => {
    renderMisc(<DashboardPage />)
    await screen.findByText('产品手册')

    for (const gone of [
      '相互隔离的检索范围',
      '向量化的最小单位',
      '不含向量与索引',
      '全部在窗口内',
      '颜色越深表示当天入库越多',
    ]) {
      expect(screen.queryByText(gone), `这句解释小字不该在：${gone}`).toBeNull()
    }
    // 留下的注解都有判据：口径（失败数）与加载态（正在统计…），不是同义复述
    expect(screen.getByText('1 篇失败')).toBeInTheDocument()
  })

  it('文档与索引：没有失败时注解报"还差多少"，全都索完则**什么都不说**', async () => {
    getDashboardMock.mockResolvedValue(
      dashboard({ total_documents: 10, indexed_documents: 7, failed_documents: 0 }),
    )
    renderMisc(<DashboardPage />)
    expect(await screen.findByText('3 篇待索引')).toBeInTheDocument()

    cleanup()
    getDashboardMock.mockResolvedValue(
      dashboard({ total_documents: 10, indexed_documents: 10, failed_documents: 0 }),
    )
    renderMisc(<DashboardPage />)
    // 全都索完：注解那一行不出现——"全部已索引"是"没有异常"的复述，
    // 卡片上已经有标签与大数字（2026-09-24 删解释小字那一批）
    const indexCard = (await screen.findByText('文档与索引')).closest('li') as HTMLElement
    await waitFor(() => expect(indexCard.querySelector('.m-figure-note')).toBeNull())
    expect(screen.queryByText('全部已索引')).toBeNull()
    expect(screen.queryByText('0%')).toBeNull()
  })

  it('大数走千分位（四位数以上不分位读不出量级）', async () => {
    getDashboardMock.mockResolvedValue(
      dashboard({ total_chunks: 21606, total_documents: 1200, indexed_documents: 1200 }),
    )

    renderMisc(<DashboardPage />)

    expect(await screen.findByText('21,606')).toBeInTheDocument()
    expect(screen.getByText('1,200')).toBeInTheDocument()
    // 原样输出（不带分隔符）的形态一个都不该在
    expect(screen.queryByText('21606')).toBeNull()
    expect(screen.queryByText('1200')).toBeNull()
  })

  it('热力图图例给出 4 级色阶样本（此前只有一句"越深越多"，没有任何样本）', async () => {
    renderMisc(<DashboardPage />)

    await screen.findByText('产品手册')
    const swatches = document.querySelectorAll('.m-heat-swatch')
    expect(swatches).toHaveLength(HEAT_STEPS.length)
    // 最浅那一档是**低透明度 token**（空格子），不是实色
    expect(HEAT_STEPS[0]).toContain('color-mix')
    expect((swatches[0] as HTMLElement).style.background).toContain('color-mix')
  })

  it('模型用量只留四个大数与相对量条：估算口径与按模型明细都不上屏（2026-09-24 删）', async () => {
    renderMisc(<DashboardPage />)

    // 四个大数在（用量本身是结论）
    expect(await screen.findByText('调用次数')).toBeInTheDocument()
    // 估算说明（"其中约 … 是按字符数估算的"）、按模型那行（含供应商 URL）、
    // 每条进度行右侧那串数（`100 次 · 共 240 条`）全部删掉
    expect(screen.queryByText(/是按字符数估算的/)).toBeNull()
    expect(screen.queryByText(/供应商没有返回用量/)).toBeNull()
    expect(screen.queryByText(/按模型：/)).toBeNull()
    expect(screen.queryByText(/共 240 条/)).toBeNull()
    expect(screen.queryByText(/向量化部分按字符数估算/)).toBeNull()
  })

  it('趋势能在三个维度之间切换，切换只改取值口径', async () => {
    renderMisc(<DashboardPage />)
    await screen.findByText('产品手册')

    const before = (await screen.findByTestId('chart-line')).getAttribute('data-series')
    await userEvent.click(screen.getByRole('button', { name: '新增切块' }))

    await waitFor(() =>
      expect(screen.getByTestId('chart-line').getAttribute('data-series')).not.toBe(before),
    )
    // 切到"新增切块"之后趋势读的是 chunks（两天各 10 / 30）
    expect(screen.getByTestId('chart-line').getAttribute('data-series')).toContain('[10,30]')
  })

  it('没有文档时给"去知识库"的出口，而不是一张空表', async () => {
    getDashboardMock.mockResolvedValue(
      dashboard({
        total_documents: 0,
        knowledge_bases: [],
        indexed_documents: 0,
        failed_documents: 0,
      }),
    )

    renderMisc(<DashboardPage />)

    // 大数卡片的注解与空态标题是同一句话，两处都要在
    expect((await screen.findAllByText('还没有文档')).length).toBeGreaterThan(0)
    expect(screen.getByRole('link', { name: '去知识库' })).toHaveAttribute(
      'href',
      '/knowledge-bases',
    )
  })

  it('趋势 x 轴标签抽稀：27 个标签糊成一条，最多留 7 个', async () => {
    renderMisc(<DashboardPage />)

    const chart = await screen.findByTestId('chart-line')
    const labels = JSON.parse(chart.getAttribute('data-labels') ?? '[]') as string[]
    const interval = Number(chart.getAttribute('data-interval'))
    // 两天数据（各一个点）本来就不挤，不抽
    expect(labels).toHaveLength(2)
    expect(interval).toBe(0)
    // 抽稀步长：27 个标签 → 每 4 个显一个（7 个）
    expect(trendLabelInterval(27)).toBe(3)
    expect(trendLabelInterval(7)).toBe(0)
  })

  it('窗口超过 60 天时按周聚合（折线不该全是锯齿）', () => {
    const points = Array.from({ length: 70 }, (_, index) => ({
      day: `2026-07-${String((index % 28) + 1).padStart(2, '0')}`,
      documents: 1,
      chunks: 2,
      tasks: 0,
    }))

    // 70 天 → 按周聚合：10 周，每周 7 篇
    const weekly = checkableTrend(points, 'documents')
    expect(weekly.labels).toHaveLength(10)
    expect(weekly.values).toEqual(Array.from({ length: 10 }, () => 7))

    // 10 天 → 一天一个点
    const daily = checkableTrend(points.slice(0, 10), 'chunks')
    expect(daily.labels).toHaveLength(10)
    expect(daily.values).toEqual(Array.from({ length: 10 }, () => 2))
  })
  it('五个大数与三块图一次画齐（dashboard 那条一直会发）', async () => {
    renderMisc(<DashboardPage />)

    expect(await screen.findByText('产品手册')).toBeInTheDocument()
    expect(getDashboardMock).toHaveBeenCalledTimes(1)
    const labels = [...document.querySelectorAll('.m-figure-label')].map((node) => node.textContent)
    expect(labels).toEqual(['知识库', '文档与索引', '切块', '原文体积', '近 365 天入库'])
    expect(screen.getByText('活跃度')).toBeInTheDocument()
    expect(screen.getByText('构成')).toBeInTheDocument()
    expect(screen.getByText('知识库规模')).toBeInTheDocument()
    expect(await screen.findByTestId('chart-line')).toBeInTheDocument()
  })
})
