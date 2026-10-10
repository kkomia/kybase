/**
 * 页面的**懒加载入口**集中在一处：路由表要用它们（`React.lazy`），
 * 侧栏的 hover/focus 预热也要用它们（旧 `SideNav.vue` 就是这么预载路由 chunk 的：
 * 悬停导航项先把那一页的代码拉下来，点进去就不必等）。
 *
 * 分成两份导出是刻意的：
 * - `PAGES['knowledgeBases']` 这类**函数**给 `React.lazy` 用（同一个函数引用，React 才会复用同一个 chunk）；
 * - `preloadPage(name)` 给预热用（内部吞掉失败——预热失败不该影响任何交互，真正点进去时
 *   该报的错照旧由那一页自己报）。
 *
 * ## 本仓库（kybase）的减法
 *
 * 这一份前端是知识库服务的**独立管理台**，不带 agent 语义：页面只有
 * 登录 / 概览 / 所有知识库 / 库详情 / Wiki / 文档详情 / 任务中心 / 工具 / 模型 /
 * API 密钥 / 404。
 * 对话、笔记、记忆、能力、备份、工作区那几页**不在本仓库**（它们留在 kylab 前端壳里），
 * 所以这里的条目比 kylab 那份少了一半——`PAGES` 与 `app/App.tsx` 的路由表是同一份清单的两面。
 */
export const PAGES = {
  login: () => import('@/features/misc/auth/LoginPage').then((m) => ({ default: m.LoginPage })),
  dashboard: () =>
    import('@/features/misc/dashboard/DashboardPage').then((m) => ({ default: m.DashboardPage })),
  knowledgeBases: () =>
    import('@/features/knowledge').then((m) => ({ default: m.KnowledgeBasesView })),
  knowledgeBase: () =>
    import('@/features/knowledge').then((m) => ({ default: m.KnowledgeBaseView })),
  wiki: () => import('@/features/knowledge').then((m) => ({ default: m.WikiView })),
  document: () => import('@/features/knowledge').then((m) => ({ default: m.DocumentView })),
  tasks: () => import('@/features/misc/tasks/TasksPage').then((m) => ({ default: m.TasksPage })),
  tools: () => import('@/features/misc/tools/ToolsPage').then((m) => ({ default: m.ToolsPage })),
  // 模型：用途绑定（出题 / 摘要走 chat 那一格）+ 供应商与模型登记
  models: () =>
    import('@/features/misc/settings/ModelsPage').then((m) => ({ default: m.ModelsPage })),
  apiKeys: () =>
    import('@/features/misc/settings/ApiKeysPage').then((m) => ({ default: m.ApiKeysPage })),
  notFound: () =>
    import('@/features/misc/auth/NotFoundPage').then((m) => ({ default: m.NotFoundPage })),
} as const

export type PageName = keyof typeof PAGES

/** 预热某一页的代码（失败静默：预热不是功能，别让它影响交互）。 */
export function preloadPage(name: PageName): void {
  void PAGES[name]().catch(() => undefined)
}
