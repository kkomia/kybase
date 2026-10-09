/**
 * 应用壳：Provider + 路由 + 登录守卫 + 全局 Toast。
 *
 * 这一份前端是知识库服务的**独立管理台**（kybase，自带一份纯 web 界面，不进桌面壳）：
 * 路由表只有知识库语义的那几条 + 登录，agent 语义的页面（对话 / 笔记 / 记忆 / 能力 /
 * 备份 / 工作区）不在这里——它们留在 kylab 前端壳里。
 *
 * 三条与上游（kylab `app/App.tsx`）**逐条对应**的约定：
 *
 * 1. **页面懒加载**：首屏只需要当前那一页；驾驶舱的 ECharts 再单独异步（见 misc 域）；
 * 2. **登录守卫的顺序不能换**：**先看 `needs_setup`**（还没有账号 → 首次设置），
 *    再看有没有凭据（无 → 登录页 + `redirect`）。`ensureAuthStatus` 失败**不拦**，
 *    否则后端起不来会在守卫里死循环，用户连"后端没起"都看不到；
 * 3. **令牌还在但已失效要落登录页，网络抖动不落**：只有 `restoreSession()` 明确回
 *    `expired` 才跳（D07）——一次网络抖动就把人强制登出、还删掉本地令牌，
 *    那是最难补救的后果。
 *
 * ## 与上游的两处不同（本仓库的减法，逐条说清）
 *
 * 1. **没有 `/chat/:conversationId?` 与 `/notes/:noteId?` 那两条"可选参数路由"**：
 *    上游要靠它避免选中会话/笔记时卸载重建，这里没有会话与笔记页；
 * 2. **登录守卫没有"本机档"那条逃生门**（上游的 R13 分支）：那一条是给"边车在本机、
 *    远端连不上也能进界面"准备的，而本仓库**没有本机档**——服务端就是知识库本身，
 *    `/auth/*` 也在它上面。远端问不出来（`status === null`）时照旧落登录页，
 *    那里会如实报"读不到"，比放一个人进一页什么都做不了的界面清楚。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Suspense, lazy, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router'

import { Toaster } from '@/ui/sonner'
import { ensureAuthStatus, restoreSession } from '@/lib/sessionActions'
import { hasCredential, sessionToken, useSessionStore } from '@/lib/session'
import { loadRoster } from '@/lib/operator'
import { PAGES } from '@/app/routes'
import { AppShell } from '@/features/layout'

// 懒加载的**入口函数**都在 `app/routes.ts` 里（侧栏的 hover 预热要用同一批函数，
// 同一个函数引用 React 才会复用同一个 chunk）。
const KnowledgeBasesView = lazy(PAGES.knowledgeBases)
const KnowledgeBaseView = lazy(PAGES.knowledgeBase)
const WikiView = lazy(PAGES.wiki)
const DocumentView = lazy(PAGES.document)
const LoginPage = lazy(PAGES.login)
const TasksPage = lazy(PAGES.tasks)
const ToolsPage = lazy(PAGES.tools)
const ApiKeysPage = lazy(PAGES.apiKeys)
const DashboardPage = lazy(PAGES.dashboard)
const NotFoundPage = lazy(PAGES.notFound)

/**
 * 查询客户端。**导出是为了让用例钉住这份网络策略**（`tests/app-query-network.test.tsx`）：
 * 策略写在这里，用例读的必须是**壳真正挂给 Provider 的那一个**，否则改回默认值也能绿。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 自托管的局域网服务：失败多半是"服务没起来"或"令牌过期"，两次足够让抖动自愈
      retry: 2,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      /*
       * **不因浏览器自报"离线"就停摆**（2026-10-04 修，上游同一条）。
       *
       * react-query 的默认值 `'online'` 看的是 `navigator.onLine`：浏览器/系统只要自认为
       * 没有外网，查询就停在 `paused`——那时 `isLoading` 与 `isError` **同为 false**，
       * 界面既不报错也不加载，看上去就是"一直没有数据"。
       *
       * 而这一份前端跟**局域网里那台知识库服务**说话（开发形态是 `/api` 反代，生产形态是
       * 同一个域下的 `/api`）：**"有没有外网"与"那台服务在不在"根本不是一回事**。
       * 查询照发，成败交给响应说话——连不上就是一条真的错误态。
       */
      networkMode: 'always',
    },
    // 写路径是同一个病：默认值下离线时 `mutate` 挂起——调了、promise 不落地、
    // 界面既不成功也不报错（保存就是这样静默丢失的）。同一台局域网服务照发为上。
    mutations: { networkMode: 'always' },
  },
})

/** 浏览器的标签标题（旧前端 `afterEach` 的口径）。 */
const TITLES: Array<[RegExp, string]> = [
  [/^\/login/, '登录'],
  [/^\/($|\?)/, '概览'],
  [/^\/dashboard/, '概览'],
  [/^\/knowledge-bases/, '知识库'],
  [/^\/kb\/[^/]+\/wiki/, 'Wiki'],
  [/^\/kb\//, '文档列表'],
  [/^\/documents\//, '文档详情'],
  [/^\/tasks/, '任务中心'],
  [/^\/tools/, '工具'],
  [/^\/api-keys/, 'API 密钥'],
]

/**
 * 登录守卫（写在组件里而不是路由 `loader`）：`ensureAuthStatus` 是异步的，
 * 而在渲染之前必须知道"是放行还是跳登录页"，否则会先闪一下目标页再跳走。
 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let alive = true
    void (async () => {
      const status = await ensureAuthStatus()
      if (!alive) return
      const onLogin = location.pathname === '/login'
      if (onLogin) {
        // 已经有了可用凭据就别再让人登一次：验一遍再跳（验不过就留在这一页）
        if (!status?.needs_setup && sessionToken() && (await restoreSession()) === 'ok') {
          navigate('/', { replace: true })
          return
        }
        setReady(true)
        return
      }
      // `status === null`（远端问不出来）也落登录页：那一页会把原因写出来，
      // 比放人进一页什么都读不到的界面清楚（见文件头第 2 条）。
      if (status?.needs_setup || !hasCredential()) {
        navigate(`/login?redirect=${encodeURIComponent(location.pathname + location.search)}`, {
          replace: true,
        })
        return
      }
      // **恢复身份**：令牌可能还在但过期/被吊销（改密、管理员踢掉）。
      // 不验一次的话，侧栏账号区会空着（`currentUser` 一直是 null）、
      // 而每个请求各报一次 401。
      //
      // 但**只有"凭据真失效"才落登录页**：网络不通 / 超时 / 后端 5xx 时令牌留着、
      // 界面照常起来（各请求自己报它们各自的错）。
      const restored = await restoreSession()
      if (restored === 'expired') {
        navigate(`/login?redirect=${encodeURIComponent(location.pathname + location.search)}`, {
          replace: true,
        })
        return
      }
      setReady(true)
    })()
    return () => {
      alive = false
    }
    // 只在路径变化时重跑：把 location 整个放进依赖会导致每次查询参数变化都验一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])

  if (!ready) return <BootSkeleton />
  return <>{children}</>
}

/**
 * **启动期的骨架**：`AuthGate` 还没判完身份、以及路由懒加载那一段的兜底。
 *
 * 形状照着真实的应用壳摆（左侧一栏 + 右侧内容），所以内容到位时**不跳**：
 * 两块的位置与尺寸都对得上，只是从灰块换成真东西。用 `animate-pulse` 表示"在动"，
 * `motion-reduce:animate-none` 尊重系统里那个"减少动态效果"（与样式层同一条口径）。
 *
 * `index.html` 里还有一份**挂载前**的同形骨架（React 还没跑起来那一段的白屏），
 * 两份形状要一致，不然会闪一下。
 */
function BootSkeleton() {
  return (
    <div
      data-testid="app-boot-skeleton"
      aria-hidden="true"
      className="flex h-dvh bg-[var(--bg-canvas)]"
    >
      {/* 侧栏那一栏：宽屏才有（窄屏下真实的侧栏也是收起的）。与真实侧栏一样**不带分隔线**
          ——边界靠"两块不同颜色的面"。 */}
      <div className="hidden w-[var(--sidebar-width)] shrink-0 flex-col gap-[var(--space-2)] p-[var(--space-3)] sm:flex">
        {[0, 1, 2, 3, 4].map((row) => (
          <span
            key={row}
            className="block h-[28px] animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-active)] motion-reduce:animate-none"
          />
        ))}
      </div>
      {/* 内容那一栏：与真实内容区**同形**（抬起的卡片：上/右/下 6px、圆角 16、surface 底）——
          内容到位时从灰块换成真东西不跳。 */}
      <div className="my-[6px] mr-[6px] flex min-w-0 flex-1 flex-col gap-[var(--space-3)] overflow-hidden rounded-[var(--radius-panel)] bg-[var(--bg-surface)] p-[var(--page-gutter)]">
        <span className="block h-[24px] w-[40%] animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-active)] motion-reduce:animate-none" />
        <span className="block h-[120px] animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-active)] motion-reduce:animate-none" />
        <span className="block h-[120px] w-[70%] animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-active)] motion-reduce:animate-none" />
      </div>
    </div>
  )
}

/** 会话恢复之后拉一次名册：库列表里的上传者、归属标注都要它（拿不到不影响使用）。 */
function RosterBoot() {
  const user = useSessionStore((state) => state.currentUser)
  useEffect(() => {
    if (user) void loadRoster()
  }, [user])
  return null
}

/** 标题跟随路由（旧前端 `router.afterEach`）。 */
function TitleSync() {
  const location = useLocation()
  useEffect(() => {
    const hit = TITLES.find(([pattern]) => pattern.test(location.pathname))
    document.title = hit ? `${hit[1]} · KYLAB 知识库` : 'KYLAB 知识库'
  }, [location.pathname])
  return null
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <TitleSync />
        <RosterBoot />
        <AuthGate>
          <Suspense fallback={<BootSkeleton />}>
            <Routes>
              {/* 登录页在壳外：它没有侧栏（旧前端 `/login` 也是独立一页） */}
              <Route path="/login" element={<LoginPage />} />
              {/* 其余全在壳里：侧栏 + 内容区（`features/layout`） */}
              <Route element={<AppShell />}>
                {/* 落地页与上游一致：`/` 是**概览**（驾驶舱），不是任何清单页 */}
                <Route path="/" element={<DashboardPage />} />
                <Route path="/dashboard" element={<Navigate to="/" replace />} />
                {/*
                  四条知识库路由**直接挂页面**：v0.29 起只有服务器一套，知识库就是服务端
                  自己，没有"提供者接没接上"这回事——原先那层守卫（`ProviderRoute`）
                  在这一档是恒放行的摆设，已随本机档一起删掉。深链接照旧可达。
                */}
                <Route path="/knowledge-bases" element={<KnowledgeBasesView />} />
                <Route path="/kb/:kbId" element={<KnowledgeBaseView />} />
                <Route path="/kb/:kbId/wiki" element={<WikiView />} />
                <Route path="/documents/:documentId" element={<DocumentView />} />
                {/* 任务中心：流水线任务（摄入 / 解析 / 索引）与运行负载 */}
                <Route path="/tasks" element={<TasksPage />} />
                {/* 对外能力：工具目录（含 MCP 地址与随附技能）——任何登录会话都读得到 */}
                <Route path="/tools" element={<ToolsPage />} />
                {/* API 密钥：端点只认管理员；成员进来看到的是页面自己的受限态 */}
                <Route path="/api-keys" element={<ApiKeysPage />} />
                {/* 旧地址保留成重定向，免得旧书签变 404（与上游同一处置） */}
                <Route path="/search" element={<Navigate to="/knowledge-bases" replace />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Routes>
          </Suspense>
        </AuthGate>
        {/* 全局 Toast：各域只调 toast()，容器只此一处 */}
        <Toaster />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
