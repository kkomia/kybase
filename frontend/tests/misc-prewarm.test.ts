/**
 * 空闲预热（`features/misc/prewarm.ts`）**按档取舍**（2026-10-05，NAS 网页端退役）。
 *
 * 这一份钉的是一件很轻、但每次启动都会发生的事：那几条 `prefetchQuery` 里，
 * **定时任务**（`/scheduled-tasks`）只挂在本机档上——服务器档没有这条路径，
 * 于是没有本机后端时那一条必然 404。而这正是最容易漏掉的那类请求：
 * 预热**失败静默**（它本来就不是功能），所以 404 不会有人看见，只会白打一趟。
 *
 * 判据用的是同一个（`api/local.ts`），这里逐档摆答案（`setLocalBackendForTest`）——
 * 判据本身的三态在 `tests/unit/api/local.test.ts` 那一份里，不在这里重复。
 *
 * ## 2026-10-05 又加一条：`dashboard` / `tasks` 要等提供者结论
 *
 * 那两条数的是**知识库**的家当（`/stats/dashboard` 与 `/tasks`，本机档同样不挂），
 * 而页面那一侧按提供者状态分流（`DashboardPage.tsx` / `TasksPage.tsx` 的文件头）。
 * 预热口径跟着走：`providerGateApplies()` 为真（本机档）时先 `await loadProviderStatus()`，
 * `providerView().ready` 才预热；为假（服务器档：知识库就是它自己）时照旧立刻预热。
 * 判据那一侧同样**直接摆结论**（`setProviderStatusForTest`）。
 */
import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/schedules', () => ({ listScheduledTasks: vi.fn() }))
vi.mock('@/api/stats', () => ({ getDashboard: vi.fn(), getUsage: vi.fn() }))
vi.mock('@/api/tasks', () => ({ listTasks: vi.fn() }))

import { resetLocalBackendForTest, setLocalBackendForTest } from '@/api/local'
import { resetProviderStore, setProviderStatusForTest } from '@/api/provider'
import { listScheduledTasks } from '@/api/schedules'
import { getDashboard, getUsage } from '@/api/stats'
import { listTasks } from '@/api/tasks'
import { prewarmMisc } from '@/features/misc/prewarm'
import { SCHEDULES_QUERY_KEY, TASKS_QUERY_KEY } from '@/features/misc/queryKeys'

const listSchedulesMock = vi.mocked(listScheduledTasks)
const listTasksMock = vi.mocked(listTasks)
const getDashboardMock = vi.mocked(getDashboard)
const getUsageMock = vi.mocked(getUsage)

function testClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

beforeEach(() => {
  vi.clearAllMocks()
  listSchedulesMock.mockResolvedValue({ items: [], timezone: 'CST UTC+08:00' })
  listTasksMock.mockResolvedValue({ items: [] } as never)
  getDashboardMock.mockResolvedValue({} as never)
  getUsageMock.mockResolvedValue({} as never)
})

afterEach(() => {
  // 模块级单份状态（`api/local.ts` 与 `api/provider.ts` 各有一份）：不复位会串到下一条用例
  //（表现是"档位时有时无"）
  resetLocalBackendForTest()
  resetProviderStore()
})

describe('空闲预热按档取舍', () => {
  it('没有本机后端：定时任务那一条**不排**（其余照旧预热）', async () => {
    setLocalBackendForTest('absent')
    const client = testClient()

    prewarmMisc(client)
    // 先等预热那几条真发出去（`prefetchQuery` 是异步的），再核"少的那一条"
    await vi.waitFor(() => expect(listTasksMock).toHaveBeenCalled())

    expect(listSchedulesMock).not.toHaveBeenCalled()
    expect(client.getQueryState(SCHEDULES_QUERY_KEY)).toBeUndefined()
    // 少的是那一条，不是整个预热：另外三条一条都没少
    expect(getDashboardMock).toHaveBeenCalled()
    expect(getUsageMock).toHaveBeenCalled()
    expect(listTasksMock).toHaveBeenCalledTimes(1)
  })

  it('有本机后端（桌面壳）：一条都不少', async () => {
    setLocalBackendForTest('local')
    const client = testClient()

    prewarmMisc(client)

    // 等**晚一步**的那两条（它们现在要等提供者结论，见文件头）——等的顺序不能反：
    // 定时任务那一条是同步排的，拿它当信号会在结论回来之前就先断言了
    await vi.waitFor(() => expect(listTasksMock).toHaveBeenCalledTimes(1))
    expect(getDashboardMock).toHaveBeenCalledTimes(1)
    expect(client.getQueryState(TASKS_QUERY_KEY)).toBeDefined()
    expect(listSchedulesMock).toHaveBeenCalledTimes(1)
    expect(client.getQueryState(SCHEDULES_QUERY_KEY)).toBeDefined()
  })
})

/*
 * `dashboard` / `tasks` 要等提供者结论（2026-10-05），**而且要先等、再判档**。
 *
 * 那两条打的是知识库那边的家当，而本机档里知识库在**提供者**那台——没接上时必然 404，
 * 而预热失败静默，所以它只会白打一趟。判据与页面同一套（`providerGateApplies()` +
 * `providerView().ready`）。
 *
 * ⚠️ 判档的**时机**与判据本身一样要紧：早先这里是"先判档"——`providerGateApplies()` 为假
 * 就立刻预热。而这一函数跑在 `requestIdleCallback` 上，浏览器 + 本机后端那一档里第一帧
 * `store.status` 还是 `null`（握手探测没回来）⇒ 判成服务器档 ⇒ 抢先打一趟
 * （真机日志：`GET /api/v1/stats/dashboard?window_days=365` 与 `GET /api/v1/tasks` 各一条
 * 404）。所以现在只有一支：先 `await loadProviderStatus()`（单飞 + TTL，启动时已在飞的
 * 那一次会被并进来，不额外打请求），结论到手之后再 `!providerGateApplies() ||
 * providerView().ready` 才预热。
 *
 * 判据的含义一个字没变：**服务器档**没有"提供者"这个概念（知识库就是它自己）⇒ 照旧预热
 * （早一步的 `unsupported` 结论也是那时才知道的）；**本机档**⇒ `ready` 才预热。
 *
 * ⚠️ 上面那两条"按档取舍"的用例为什么仍然绿：它们没有摆提供者结论，探一次之后
 * `providerGateApplies()` 在"不是桌面壳"这一档仍是**假**（服务器档那一支）⇒ 照旧预热。
 */
describe('dashboard / tasks 等提供者结论', () => {
  it('提供者还没 ready：这两条一次都不预热（用量照旧）', async () => {
    setLocalBackendForTest('local')
    setProviderStatusForTest({
      state: 'unconfigured',
      available: false,
      reason: '还没配知识库提供者的地址',
    })
    const client = testClient()

    prewarmMisc(client)
    // 用量那一条是**立刻**排的：先等它真发出去，再核少的那两条
    await vi.waitFor(() => expect(getUsageMock).toHaveBeenCalled())

    expect(getDashboardMock).not.toHaveBeenCalled()
    expect(listTasksMock).not.toHaveBeenCalled()
    // 那两条连 query 都没建（不是"发了但失败"）
    expect(client.getQueryState(TASKS_QUERY_KEY)).toBeUndefined()
  })

  it('提供者 ready：两条都预热', async () => {
    setLocalBackendForTest('local')
    setProviderStatusForTest({ state: 'ready', available: true })
    const client = testClient()

    prewarmMisc(client)

    await vi.waitFor(() => expect(listTasksMock).toHaveBeenCalledTimes(1))
    expect(getDashboardMock).toHaveBeenCalledTimes(1)
    expect(client.getQueryState(TASKS_QUERY_KEY)).toBeDefined()
  })

  /*
   * 抢跑那一条（真机上抓到的 404 就是它）。
   *
   * 摆法照着真机的时序来：调用那一瞬间**还没有结论**（不摆状态 ⇒ `providerGateApplies()`
   * 为假，与浏览器里第一帧完全一样），紧接着在**同一个同步块内**把结论摆成"本机档 +
   * 没接上"。旧实现（"gate 为假就立刻预热并 return"）在这里就会当场把那两条打出去
   * ——本用例要的正是它打不出去。
   *
   * 新旧两支的差别只在**判档的时机**：这一条里 `loadProviderStatus()` 在调用点上已经进了
   * TTL 之外的实读（`fetchedAt` 还是 0），而那次读的结论会被 `setProviderStatusForTest`
   * 的复位作废（`api/provider.ts` 的 `generation`）⇒ `await` 落地时读到的是**摆好的那一份**
   * （本机档 + 未 ready）⇒ 一条都不许预热。
   */
  it('结论还没回来时不许抢跑：这一帧看着像服务器档，也不许先把那两条打出去', async () => {
    setLocalBackendForTest('local')
    const client = testClient()

    prewarmMisc(client)
    // 同一批微任务里结论就到了：本机档 + 没接上（这一下会把上面那次实读作废）
    setProviderStatusForTest({
      state: 'unconfigured',
      available: false,
      reason: '还没配知识库提供者的地址',
    })

    // 先等那条"与知识库无关"的用量真发出去，再核少的那两条
    await vi.waitFor(() => expect(getUsageMock).toHaveBeenCalled())

    expect(getDashboardMock).not.toHaveBeenCalled()
    expect(listTasksMock).not.toHaveBeenCalled()
    expect(client.getQueryState(TASKS_QUERY_KEY)).toBeUndefined()
  })
})
