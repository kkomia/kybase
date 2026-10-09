/**
 * 空闲预热（`features/misc/prewarm.ts`）。
 *
 * 它跑在启动后的第一个空闲帧上，把"最可能被点的两页"（任务列表 / 概览统计）与模型用量
 * 先拉回来——直接点进去时不必再等一次往返。
 *
 * 这份用例钉的就是"那三条 `prefetchQuery` 确实排了出去、键与页面共用同一份"。
 * 按档取舍那些分支（本机档 / 提供者结论）已随本机档与提供者一起删掉（v0.29 只有服务器一套）。
 */
import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/stats', () => ({ getDashboard: vi.fn(), getUsage: vi.fn() }))
vi.mock('@/api/tasks', () => ({ listTasks: vi.fn() }))

import { getDashboard, getUsage } from '@/api/stats'
import { listTasks } from '@/api/tasks'
import { prewarmMisc } from '@/features/misc/prewarm'
import {
  DASHBOARD_WINDOW_DAYS,
  TASKS_QUERY_KEY,
  USAGE_WINDOW_DAYS,
} from '@/features/misc/queryKeys'

const listTasksMock = vi.mocked(listTasks)
const getDashboardMock = vi.mocked(getDashboard)
const getUsageMock = vi.mocked(getUsage)

function testClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

beforeEach(() => {
  vi.clearAllMocks()
  listTasksMock.mockResolvedValue({ items: [] } as never)
  getDashboardMock.mockResolvedValue({} as never)
  getUsageMock.mockResolvedValue({} as never)
})

describe('空闲预热', () => {
  it('三条都排出去：驾驶舱统计、任务列表、模型用量（窗口与页面同一份常量）', async () => {
    const client = testClient()

    prewarmMisc(client)

    await vi.waitFor(() => expect(listTasksMock).toHaveBeenCalledTimes(1))
    expect(getDashboardMock).toHaveBeenCalledTimes(1)
    expect(getUsageMock).toHaveBeenCalledTimes(1)
    expect(client.getQueryState(TASKS_QUERY_KEY)).toBeDefined()
    // 键与页面逐字共用（写错一个数字就是"预热的那一份页面读不到"）
    expect(client.getQueryState(['stats', 'dashboard', DASHBOARD_WINDOW_DAYS])).toBeDefined()
    expect(client.getQueryState(['stats', 'usage', USAGE_WINDOW_DAYS])).toBeDefined()
  })
})
