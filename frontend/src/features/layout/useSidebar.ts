/**
 * 侧栏折叠（本地偏好）——与旧前端 `composables/useSidebar.ts` 逐条对应。
 *
 * 与主题、字号是同一类东西：**"这台机器怎么显示"**，所以落在 `localStorage`，
 * 不进后端 `app_settings`——换台机器该重新选，它也不是知识库配置。
 *
 * 折叠态做成"图标栏"（60px）而不是整条隐藏：导航还在，只是省掉文字与会话列表。
 * 整条隐藏会把"回到对话/概览"变成一次额外的展开动作，省下的横向空间并不值这个价。
 *
 * ## 契约（存储键与广播事件，与上游一字不差）
 *
 * 把折叠偏好翻过来的那一侧要**广播 `kylab:sidebar-toggle`**——所以侧栏不只是"自己记着"，
 * 还要**听着**：`storage` 事件只在别的标签页之间派发，本页自己改的不算，
 * 缺了这条监听就会出现"那边偏好翻了、这边侧栏没动"。
 *
 * **本仓库里广播那一侧没有搬过来**（上游是对话页的快捷键，那段绑定住在 kylab 的 chat 域
 * 与设置域的快捷键注册表里，随会话面一起留下），写路径只剩品牌位那颗折叠按钮。
 * 监听照旧留着：同一份界面在另一个标签页里被改时（`storage` 事件）要跟得上。
 *
 * 用 zustand 而不是 React Context：侧栏与账号区（都在壳这一层）要读同一份状态，
 * 而且它是**模块级单例**——与旧版那个 `ref` 的地位一致。
 */
import { useEffect, useState } from 'react'
import { create } from 'zustand'

/** 折叠态的存储键（沿用 kylab 的键名，两套前端的这一栏表现一致）。 */
export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'kylab-sidebar-collapsed'

/** 折叠态变化的广播事件（`detail.collapsed`）。 */
export const SIDEBAR_TOGGLE_EVENT = 'kylab:sidebar-toggle'

/**
 * "窄屏"的阈值。
 *
 * 2026-10 界面重做把这一档从 760 提到 **1024**：管理台的主要宽度档里，
 * 900–1024 这一段展开侧栏后内容只剩 ~600px——热力图、文档表、仪表带都开始靠
 * "缩小硬塞"，而折叠成图标栏（72px）之后内容区回到 ~800px，读起来是正常的。
 * 420px 的实测口径没变：侧栏 240px 时内容区只剩 180px、每行 4.53 个字，
 * 折叠之后才有 360px。
 *
 * 早先这一条要在 kylab 前端里也写一份（两套前端同时开着时同一窗口宽度不能有两套判断）；
 * 那份前端已经不在这一版的范围里，本仓库只剩这一处。
 */
export const SIDEBAR_NARROW_QUERY = '(max-width: 1024px)'

/** 现在是不是窄屏（**非组件代码**也能问；组件用 `useNarrowViewport` 订阅变化）。 */
export function matchesNarrowViewport(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(SIDEBAR_NARROW_QUERY).matches
}

/**
 * 用户**表过态**的折叠偏好：`true` 折叠 / `false` 展开 / `null` **没表过态**（键不存在）。
 *
 * `null` 这一档是 D29 加的：以前"键不存在"与"显式展开"都是同一件事（`removeItem`），
 * 于是没法区分"用户说过要展开"和"用户从没管过"——而窄屏默认折叠只能在后者上生效。
 * 旧安装的键要么不存在（→ `null`，宽屏仍然默认展开，与旧行为一致）、要么是 `'1'`（折叠），
 * 所以这次改动**不改变任何既有用户看到的样子**（宽屏口径完全没动）。
 */
function readStored(): boolean | null {
  try {
    const raw = window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)
    if (raw === '1') return true
    if (raw === '0') return false
    return null
  } catch {
    // 隐私模式下 localStorage 不可读：当成"没表过态"
    return null
  }
}

function writeStored(next: boolean): void {
  try {
    // 展开也**写下去**（`'0'`），不再是删键：删键等于"没表过态"，
    // 那会让用户在窄屏上点开侧栏之后又被下一次渲染收回去
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, next ? '1' : '0')
  } catch {
    // 存不上就只在本次会话生效
  }
}

interface SidebarState {
  /** **偏好**（不是"当前显示成什么样"）：见 `readStored` 的三档说明。 */
  collapsed: boolean | null
  setCollapsed: (next: boolean) => void
  toggle: () => void
}

export const useSidebarStore = create<SidebarState>((set, get) => ({
  collapsed: typeof window === 'undefined' ? false : readStored(),
  setCollapsed: (next) => {
    writeStored(next)
    set({ collapsed: next })
  },
  // 从**当前实际显示的样子**翻，而不是从偏好翻：窄屏没表态时显示的是折叠，
  // 那这一下就该展开它（否则快捷键/按钮看着像失灵）
  toggle: () => get().setCollapsed(!isSidebarCollapsed()),
}))

/** 设置折叠态（落盘 + 上屏）。**不广播**：广播是快捷键那一侧的事。 */
export function setSidebarCollapsed(next: boolean): void {
  useSidebarStore.getState().setCollapsed(next)
}

export function toggleSidebar(): void {
  useSidebarStore.getState().toggle()
}

/** 当前折叠态（非组件代码用）：偏好优先，没表过态时窄屏算折叠。 */
export function isSidebarCollapsed(): boolean {
  return useSidebarStore.getState().collapsed ?? matchesNarrowViewport()
}

/**
 * 侧栏现在的宽度（CSS 值）。
 *
 * 盖住内容区的那些浮层（历史会话面板）要靠它算"左边让给侧栏多少"：
 * 固定写 `--sidebar-width` 的话，折叠之后会在两者之间留一条 180px 的内容区。
 */
export function resolveSidebarWidth(collapsed = isSidebarCollapsed()): string {
  return collapsed ? 'var(--sidebar-collapsed-width)' : 'var(--sidebar-width)'
}

/**
 * 订阅外部来的折叠信号（快捷键广播 / 别的标签页改动）。
 *
 * 细节：`storage` 事件只在**别的标签页**写这个键时触发，本页自己写的不会回环——
 * 所以两条监听互不重复。
 */
export function useSidebar() {
  const preference = useSidebarStore((state) => state.collapsed)
  const narrow = useNarrowViewport()
  // 偏好优先；**没表过态**时窄屏按折叠算（见 `SIDEBAR_NARROW_QUERY` 的说明）
  const collapsed = preference ?? narrow

  useEffect(() => {
    const onToggle = (event: Event): void => {
      const detail = (event as CustomEvent<{ collapsed?: boolean }>).detail
      // detail 里带着目标状态就用它；没带（第三方广播）就翻一下
      setSidebarCollapsed(detail?.collapsed ?? !isSidebarCollapsed())
    }
    const onStorage = (event: StorageEvent): void => {
      if (event.key !== null && event.key !== SIDEBAR_COLLAPSED_STORAGE_KEY) return
      useSidebarStore.setState({ collapsed: readStored() })
    }
    window.addEventListener(SIDEBAR_TOGGLE_EVENT, onToggle)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(SIDEBAR_TOGGLE_EVENT, onToggle)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  return { collapsed, narrow, toggleSidebar, setSidebarCollapsed }
}

/**
 * 窄屏？（D29）窗口拉伸时跟着变——不订阅的话，把窗口从宽拉到窄要刷新才认。
 *
 * 初始值取 `matches`（同步读一次），而不是先 `false` 再在 effect 里改：
 * 后者会让窄屏上的第一帧先按展开渲染一次（侧栏闪一下再收）。
 */
export function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(matchesNarrowViewport)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(SIDEBAR_NARROW_QUERY)
    const onChange = (): void => setNarrow(media.matches)
    // 挂载时对一次：`useState` 那次读与这里之间窗口可能已经变了
    onChange()
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])
  return narrow
}
