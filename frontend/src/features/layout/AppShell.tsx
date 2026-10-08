/**
 * 应用外壳（《前端设计规范》§5 布局骨架）——从 kylab 前端的 `AppShell` 裁出来的知识库版。
 *
 * ```
 * ┌──────────┬───────────────────────────────┐
 * │ SideNav  │ 内容区（路由页面）             │
 * └──────────┴───────────────────────────────┘
 * ```
 *
 * ## 三条职责（上游是四条，少的那一条见下）
 *
 * 1. **登录页不套侧栏**：还没有身份，侧栏上的账号区无从谈起；
 * 2. **换页不做额外动作**：上游在这里挂着一块"历史会话面板"并在换页时把它关掉
 *    ——**本仓库没有会话**，那块面板连同它的开合状态、宽度让位、换页关闭那一段
 *    一并摘掉了（面板的代码留在 kylab 前端壳里）。壳这一层因此只剩"侧栏 + 内容区"；
 * 3. **会话失效（401）送往登录页**：`api/client.ts` 在 401 时递增 `reloginCount`，
 *    壳负责把它变成"跳到登录页并记住原地址"——任何请求都可能失效，所以这条不能
 *    挂在某一个页面上（旧 `App.vue` 的 `watch(reloginCount)`）。
 *
 * 顶上那条「我的数据在哪」状态带（上游 M2 阶段 4 的 `LocalDataStrip`）上游 R5 就删掉了，
 * 这里自然也没有：会话 / 笔记 / 设置落本机是**那一边**的事。
 *
 * ## 主控怎么接（两种都行）
 *
 * ```tsx
 * // ① 布局路由（推荐：登录页天然在壳外）
 * <Route element={<AppShell />}>
 *   <Route path="/knowledge-bases" element={<KnowledgeBasesView />} />
 *   …其余业务路由…
 * </Route>
 *
 * // ② 直接包住路由表
 * <AppShell>
 *   <Routes>…</Routes>
 * </AppShell>
 * ```
 *
 * 两种都由 `BrowserRouter` 之内渲染（`useLocation` / `Link` / `Outlet` 都依赖路由上下文）。
 * 壳自己还会在 `/login` 上退化成"只有内容区"——即使用第二种写法把登录页包进来，
 * 也不会在登录页长出一条侧栏（与旧 `App.vue` 的 `shell-bare` 同一条）。
 */
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'

import { initTheme } from '@/features/misc/settings/useTheme'
import { useSessionStore } from '@/lib/session'

import { onIdle, prewarmMisc } from '@/features/misc/prewarm'
import { SideNav } from './SideNav'

/** 登录页的路径（旧 `App.vue` 判的是路由名 `login`）。 */
const LOGIN_PATH = '/login'

export function AppShell({ children }: { children?: React.ReactNode }) {
  const queryClient = useQueryClient()

  // 启动后**空闲预热**：任务列表与概览统计（旧 `SideNav.vue` 的 idle 预热口径）。
  // 每个 client 只做一次（`useRef` 挡 StrictMode 的二次挂载）。
  const prewarmed = useRef(false)
  useEffect(() => {
    if (prewarmed.current) return
    prewarmed.current = true
    onIdle(() => prewarmMisc(queryClient))
  }, [queryClient])

  const location = useLocation()
  const navigate = useNavigate()
  const reloginCount = useSessionStore((state) => state.reloginCount)

  // 首屏脚本已经按存储设过主题与字号；这里把 composable 的状态与那两个值对齐，
  // 好让"当前是哪一档"在两处（首屏、账号菜单那句"切换为浅色/深色"）一致。
  useEffect(() => {
    initTheme()
  }, [])

  /**
   * 会话失效（401）的统一出口：跳到登录页，并把原地址带在 `redirect` 上。
   *
   * 放在壳里而不是每个页面里，是因为**任何请求都可能失效**：`request()` 发现 401 会
   * 递增这个计数，壳一旦看到就送人去登录页——令牌过期于是变成"当场跳走、回来还在原处"，
   * 而不是"原地报一句登录已过期、用户只能自己刷新"（旧 `App.vue` 的 `watch(reloginCount)`）。
   *
   * **只认"计数变了"这一件事**（`handledRelogin` 那个 ref）：计数**不会**在重新登录后归零，
   * 所以不能写成"计数非零就跳"——那样用户重新登录、再点任何一个菜单都会被弹回登录页
   * （与接入层的守卫来回拉锯）。旧版的 `watch` 天然只在变化时触发，这里把那件事显式写出来；
   * 顺便也解决了"组件带着一个非零计数挂载"的情形（挂载不跳，只跟变化）。
   *
   * 已经在登录页就不动（否则会自己把自己再跳一次）。用 `replace` 与接入层的
   * `AuthGate` 同一口径：历史里不留一条"过期页面"，免得按返回又弹回来。
   */
  const handledRelogin = useRef(reloginCount)
  const fullPath = `${location.pathname}${location.search}`
  const target = useRef(fullPath)
  useEffect(() => {
    target.current = fullPath
  }, [fullPath])
  useEffect(() => {
    if (reloginCount === handledRelogin.current) return
    handledRelogin.current = reloginCount
    if (location.pathname === LOGIN_PATH) return
    void navigate(`/login?redirect=${encodeURIComponent(target.current)}`, { replace: true })
  }, [reloginCount, location.pathname, navigate])

  // 侧栏在登录页不渲染（登录页撑满整个窗口）
  const bare = location.pathname === LOGIN_PATH

  return (
    <div className="flex h-full">
      {!bare && <SideNav />}
      {/* 内容区那一列 = 抬起来的卡片（M2 阶段 4）。
          卡片是 Kimi 的层级方向（§8）：上/右/下留 6px 露出画布底，左侧与侧栏相接
          —— 边界就是"两块不同颜色的面"，没有分隔线（Kimi 实测无 border-right）。
          登录页（bare）没有侧栏，卡片四边都留 6px。 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <main
          className={`${bare ? 'm-[6px]' : 'mb-[6px] mr-[6px]'} min-h-0 min-w-0 flex-1 overflow-y-auto rounded-[var(--radius-panel)] bg-[var(--bg-surface)]`}
        >
          {children ?? <Outlet />}
        </main>
      </div>
    </div>
  )
}
