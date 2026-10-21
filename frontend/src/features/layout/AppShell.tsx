/**
 * 应用外壳 —— **侧栏 + 一张浮起来的内容卡**。
 *
 * ```
 * ┌──────────┬────────────────────────────────────────┐
 * │ SideNav  │ ╭────────────────────────────────────╮ │
 * │          │ │ TopBar：面包屑 · 在线 · 明暗        │ │
 * │          │ ├────────────────────────────────────┤ │
 * │          │ │ 内容（路由页面，进场时上浮淡入）    │ │
 * │          │ ╰────────────────────────────────────╯ │
 * └──────────┴────────────────────────────────────────┘
 * ```
 *
 * ## 为什么内容区是一张卡
 *
 * 纸色的画布与白面之间需要一条明确的"这是一扇窗"的边界：卡片把**界面**与
 * **背景**分开，滚动条只在卡片内部走，圆角让屏幕边缘松下来。它是 2026-10 重做里
 * 唯一一处结构性的大改——此前的"内容卡片"只差 6px 与一层几乎看不见的描边，
 * 读起来还是"一块铺满的白"。
 *
 * ## 三条职责
 *
 * 1. **登录页不套壳**：还没有身份，侧栏与顶栏都无从谈起；
 * 2. **换页播放一次进场动画**：`route-view` 以 pathname 为 key，换页即重新挂载，
 *    "页面在动"这件事就来自这里；同页内的 tab / 查询参数变化不会重播；
 * 3. **会话失效（401）送往登录页**：`api/client.ts` 在 401 时递增 `reloginCount`，
 *    壳把它变成"跳到登录页并记住原地址"——任何请求都可能失效，这条不能挂在某一页上。
 */
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'

import { initTheme } from '@/features/misc/settings/useTheme'
import { useSessionStore } from '@/lib/session'

import { onIdle, prewarmMisc } from '@/features/misc/prewarm'
import { SideNav } from './SideNav'
import { TopBar } from './TopBar'

/** 登录页的路径。 */
const LOGIN_PATH = '/login'

export function AppShell({ children }: { children?: React.ReactNode }) {
  const queryClient = useQueryClient()

  // 启动后**空闲预热**：任务列表与概览统计（进任何一页都先摸摸底）。
  const prewarmed = useRef(false)
  useEffect(() => {
    if (prewarmed.current) return
    prewarmed.current = true
    onIdle(() => prewarmMisc(queryClient))
  }, [queryClient])

  const location = useLocation()
  const navigate = useNavigate()
  const reloginCount = useSessionStore((state) => state.reloginCount)

  // 首屏脚本已经按存储设过主题与字号；这里把 composable 的状态与那两个值对齐。
  useEffect(() => {
    initTheme()
  }, [])

  /**
   * 会话失效（401）的统一出口：跳到登录页，并把原地址带在 `redirect` 上。
   * **只认"计数变了"这一件事**——计数不会在重新登录后归零，写成"非零就跳"
   * 会让每次点菜单都被弹回登录页。
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

  // 登录页没有槽位可放（它自己撑满窗口）
  if (location.pathname === LOGIN_PATH) {
    return <>{children ?? <Outlet />}</>
  }

  return (
    <div className="flex h-dvh overflow-hidden bg-canvas">
      <SideNav />
      <div className="flex min-w-0 flex-1 flex-col">
        {/* 内容卡：上下右各留 8px 露出纸底，左侧与侧栏相接。
            `--bg-sheet` 是"这一张窗"那一层：浅色下与卡片同色（靠投影分），
            深色下比卡 / 面板暗一档——三档明度让深色也能分出层级。 */}
        <div className="my-2 mr-2 flex min-h-0 flex-1 flex-col overflow-hidden rounded-[var(--radius-overlay)] border border-[var(--border-hairline)] bg-[var(--bg-sheet)] shadow-[var(--shadow-card)]">
          <TopBar />
          <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            <div key={location.pathname} className="route-view min-h-full">
              {children ?? <Outlet />}
            </div>
          </main>
        </div>
      </div>
    </div>
  )
}
