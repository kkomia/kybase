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
 */
import type { QueryClient } from '@tanstack/react-query'

import { getDashboard, getUsage } from '@/api/stats'
import { listTasks } from '@/api/tasks'
import {
  DASHBOARD_WINDOW_DAYS,
  TASKS_QUERY_KEY,
  USAGE_WINDOW_DAYS,
} from '@/features/misc/queryKeys'

/** 空闲时拉回"最可能被点的两页"的数据。调用方只管调，不等它。 */
export function prewarmMisc(client: QueryClient): void {
  const jobs: Array<Promise<unknown>> = [
    client.prefetchQuery({
      queryKey: ['stats', 'dashboard', DASHBOARD_WINDOW_DAYS],
      queryFn: () => getDashboard(DASHBOARD_WINDOW_DAYS),
    }),
    client.prefetchQuery({ queryKey: TASKS_QUERY_KEY, queryFn: () => listTasks() }),
    client.prefetchQuery({
      queryKey: ['stats', 'usage', USAGE_WINDOW_DAYS],
      queryFn: () => getUsage(USAGE_WINDOW_DAYS),
    }),
  ]
  for (const job of jobs) void job.catch(() => undefined)
}

/** 在浏览器空闲时跑（jsdom/Safari 没有 `requestIdleCallback` 时退化成 `setTimeout`）。 */
export function onIdle(task: () => void): void {
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number })
    .requestIdleCallback
  if (typeof idle === 'function') idle(task)
  else setTimeout(task, 0)
}
