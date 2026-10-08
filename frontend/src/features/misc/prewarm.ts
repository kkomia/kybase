/**
 * 启动后的**空闲预热**（旧 `SideNav.vue` 的 idle 预热口径；审计 F18 的最后一截）。
 *
 * 旧版在启动后把"最可能被点的两页"的数据先拉回来：任务列表与概览的统计
 * （外加模型注册表）。新版此前只有 hover 预热（导航项/hover 会话行），
 * 于是**直接点**「任务中心」或「概览」时还要等一次往返——观感上就是"点进去先空白一下"。
 *
 * 三点纪律：
 * 1. **只在空闲时做**（`requestIdleCallback`，没有就退化成 `setTimeout(0)`）：
 *    预热不能跟首屏抢带宽与主线程；
 * 2. **失败静默**：预热不是功能。真正进页该报的错由那一页自己报；
 * 3. **键与页面共用同一份常量**：都从 `queryKeys.ts` 取（纯值模块）。**不能从页面模块
 *    import**——那会让那两个页面的动态 import 失效（构建期 `INEFFECTIVE_DYNAMIC_IMPORT`，
 *    两页被打进主 chunk，首屏白白变大；实测踩到过）。
 *
 * 与页面的关系是**单向的**：这里 import 纯值常量与 api，页面不 import 这个文件。
 *
 * ## 预热的东西也得**这一档真在服务**（2026-10-05，NAS 网页端退役）
 *
 * 定时任务那一族（`/scheduled-tasks`）只挂在本机档上（`backend/app/api/v1/router.py`），
 * 而任务中心里那一段也按同一个判据藏掉了（`features/misc/tasks/TasksPage.tsx`）——
 * 没有本机后端的那一份界面里，这一条预热**没有任何页面会用到它**：预热本来就"失败静默"，
 * 看不出来它白打了一趟 404，而它每次启动都打。
 *
 * ## `dashboard` / `tasks` 那两条要等提供者结论（本机档）
 *
 * 这两条数的是**知识库那边的家当**（`/stats/dashboard` 与 `/tasks`，见 `router.py` 的
 * "明确不挂"那一段），而本机档里知识库在**提供者**那台——页面那一侧同样按提供者状态分流
 * （`DashboardPage.tsx` / `TasksPage.tsx` 的文件头写着那套判据）。预热口径得跟着页面走：
 * 没接上时那两条请求必然 404，而预热"失败静默"，于是它只会白打一趟。
 *
 * 分档读的是 `providerGateApplies()`（与页面同一条判据），但**判定时机在结论之后**：
 * 两种形态都先 `await loadProviderStatus()`（单飞 + TTL，不额外打请求），到手再判——
 * **服务器档**（知识库就是它自己）照旧预热，**本机档**要 `providerView().ready` 才预热。
 *
 * ⚠️ 这一条是**先等、再判**，不是"先判档、为假就立刻预热"：后者在浏览器 + 本机后端那一档
 * 会抢跑——预热跑在 `requestIdleCallback` 上，那一帧 `store.status` 还是 `null`（握手探测
 * 没回来）⇒ 判成服务器档 ⇒ 本机档里当场白打 `GET /api/v1/stats/dashboard` 与
 * `GET /api/v1/tasks` 两条 404（真机日志抓过）。
 *
 * `usage` 与 `schedules` 不受它管：用量走本机的 `/stats/usage`（`local.stats_reads`），
 * 定时任务的数据也在本机（它的判据仍是"有没有本机后端"，见上面那一节）。
 */
import type { QueryClient } from '@tanstack/react-query'

import { localBackendPresent } from '@/api/local'
import { loadProviderStatus, providerGateApplies, providerView } from '@/api/provider'
import { getDashboard, getUsage } from '@/api/stats'
import { listScheduledTasks } from '@/api/schedules'
import { listTasks } from '@/api/tasks'
import {
  DASHBOARD_WINDOW_DAYS,
  SCHEDULES_QUERY_KEY,
  TASKS_QUERY_KEY,
  USAGE_WINDOW_DAYS,
} from '@/features/misc/queryKeys'

/** 空闲时拉回"最可能被点的两页"的数据。调用方只管调，不等它。 */
export function prewarmMisc(client: QueryClient): void {
  /** 知识库那两条（概览的统计 + 任务列表）：只在判定"接上了"之后才排。 */
  const prewarmKnowledgeSide = (): void => {
    const jobs: Array<Promise<unknown>> = [
      client.prefetchQuery({
        queryKey: ['stats', 'dashboard', DASHBOARD_WINDOW_DAYS],
        queryFn: () => getDashboard(DASHBOARD_WINDOW_DAYS),
      }),
      client.prefetchQuery({ queryKey: TASKS_QUERY_KEY, queryFn: () => listTasks() }),
    ]
    for (const job of jobs) void job.catch(() => undefined)
  }

  // 模型用量走**本机**的 `/stats/usage`：与知识库接没接上无关，照旧预热
  void client
    .prefetchQuery({
      queryKey: ['stats', 'usage', USAGE_WINDOW_DAYS],
      queryFn: () => getUsage(USAGE_WINDOW_DAYS),
    })
    .catch(() => undefined)

  // 定时任务只在本机档服务（见文件头）：没有本机后端时这一条**不排**，那一趟必然 404
  if (localBackendPresent()) {
    void client
      .prefetchQuery({ queryKey: SCHEDULES_QUERY_KEY, queryFn: listScheduledTasks })
      .catch(() => undefined)
  }

  // **先等结论，再判档**（见文件头那一节）。两件都在这一支里，不再按"先判档"分两支：
  // 浏览器 + 本机后端那一档里，第一帧的 `providerGateApplies()` 还是假（`store.status`
  // 要等握手探测回来才有），那时按服务器档抢先预热，本机档里就是
  // `GET /api/v1/stats/dashboard` 与 `GET /api/v1/tasks` 两条 404（真机日志抓过）。
  // `loadProviderStatus()` 是单飞 + TTL 的，启动时已在飞的那一次会被并进来，不额外打请求。
  void (async () => {
    try {
      await loadProviderStatus()
    } catch {
      // 预热不是功能：读状态这一步的异常也不许跑出来
    }
    // 结论到手之后才是"这一档到底是不是服务器档"能判准的时刻
    if (!providerGateApplies() || providerView().ready) prewarmKnowledgeSide()
  })()
}

/** 在浏览器空闲时跑（jsdom/Safari 没有 `requestIdleCallback` 时退化成 `setTimeout`）。 */
export function onIdle(task: () => void): void {
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number })
    .requestIdleCallback
  if (typeof idle === 'function') idle(task)
  else setTimeout(task, 0)
}
