/**
 * 应用壳域（侧栏导航 / 用户区）的对外出口——从 kylab 前端那份裁出来的知识库版。
 *
 * ## 主控接线（`src/app/**` 归主控，本域不改它）
 *
 * 把业务路由放进壳里，**登录页留在壳外**：
 *
 * ```tsx
 * import { AppShell } from '@/features/layout'
 *
 * <BrowserRouter>
 *   <AuthGate>
 *     <Suspense fallback={<BootSkeleton />}>
 *       <Routes>
 *         <Route path="/login" element={<LoginPage />} />
 *         <Route element={<AppShell />}>
 *           ...业务路由原样搬进这条布局路由（内容经 <Outlet/> 落进内容区）...
 *         </Route>
 *       </Routes>
 *     </Suspense>
 *   </AuthGate>
 * </BrowserRouter>
 * ```
 *
 * 也支持"包住整张路由表"的写法：`<AppShell><Routes>…</Routes></AppShell>`
 * （`children` 给了就用它，没给就渲染 `<Outlet/>`）。
 * 两种写法都必须在 `BrowserRouter` 之内；壳自己在 `/login` 上也会退化成"只有内容区"。
 *
 * ## 本域提供的三件事
 *
 * | 出口 | 用途 |
 * | --- | --- |
 * | `AppShell` | 侧栏 + 内容区 |
 * | `SideNav` / `AccountMenu` | 拆分件的独立入口（测试与将来别的壳用） |
 * | `useSidebar()` / 那几个折叠函数 | 折叠态的读与写（存储键与广播事件见 `useSidebar`） |
 *
 * 上游那份还导出 `ConversationHistoryPanel` / `ConversationRowMenu` / 会话与工作区两个
 * store——**它们属于 agent 域，没有随这一份搬进来**（壳做减法，见 `SideNav` 文件头那张表）。
 *
 * **本域不挂 Toast**：`<Toaster/>` 由主控的 `App` 挂（各域只调 `toast.*`），
 * 壳里再挂一个会让同一条提示出现两遍。
 */
export { AppShell } from './AppShell'
export { SideNav } from './SideNav'
export { AccountMenu } from './AccountMenu'
export {
  SIDEBAR_COLLAPSED_STORAGE_KEY,
  SIDEBAR_TOGGLE_EVENT,
  isSidebarCollapsed,
  resolveSidebarWidth,
  setSidebarCollapsed,
  toggleSidebar,
  useSidebar,
  useSidebarStore,
} from './useSidebar'
export { useAutoHideScrollbar } from './useAutoHideScrollbar'
