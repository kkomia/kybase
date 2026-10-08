/**
 * misc 域里**被两处以上共用**的查询键与窗口常量（纯值，**不 import 任何页面模块**）。
 *
 * 为什么单独一个文件：`prewarm.ts`（启动后空闲预热）也要用它们，而它一旦**静态 import**
 * 页面模块（`DashboardPage` / `TasksPage`），那两个页面的**动态 import 就失效**——
 * 构建期会报 `INEFFECTIVE_DYNAMIC_IMPORT`，两页连同它们的依赖被打进主 chunk，
 * 首屏白多几十 KB（实测踩到：预演构建时冒出来，见开发计划 §12.247）。
 *
 * 纪律：**只放值**（字符串/数字/只读数组）。放函数或组件进来，就等于把这条纪律重新破坏掉。
 */

/** 任务中心：任务列表。 */
export const TASKS_QUERY_KEY = ['tasks', 'list'] as const

/** 任务中心：定时任务列表。 */
export const SCHEDULES_QUERY_KEY = ['scheduled-tasks'] as const

/** 概览：统计窗口（天）。 */
export const DASHBOARD_WINDOW_DAYS = 365

/** 概览：模型用量窗口（天）。 */
export const USAGE_WINDOW_DAYS = 30
