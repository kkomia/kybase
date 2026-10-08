/**
 * 登录会话的本地状态（React 版；逻辑与旧前端 `composables/useSessionToken.ts` 逐条对应）。
 *
 * **为什么要单独一层**：`api/client.ts` 要读令牌、并在 401 时发"请重新登录"信号，
 * 而 `lib/session.ts` 不能反过来依赖 api 层（那就是循环依赖）。旧前端把这条依赖
 * 拉成直线（api/auth → api/client → 会话状态），这里沿用同一条。
 *
 * 用 **zustand** 而不是 React Context：`client.ts`（非组件代码）也要读它，
 * Context 只在组件树里可用；zustand 的 `getState()` 在模块里随手可调。
 */
import { create } from 'zustand'

import type { Account, AuthBootstrapStatus } from '@/api/auth'

export const SESSION_TOKEN_STORAGE_KEY = 'kylab-session-token'

function read(): string {
  try {
    return window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY) ?? ''
  } catch {
    // 隐私模式下 localStorage 不可写：退化成"本次会话有效"，而不是崩掉
    return ''
  }
}

interface SessionState {
  token: string
  /** 当前登录账号（会话恢复完成前为 null）。 */
  currentUser: Account | null
  /** 后端认证状态（是否需初始化）。 */
  authStatus: AuthBootstrapStatus | null
  /** 「请重新登录」的**计数器**：连续多个请求同时 401 时，布尔值只会跳一次。 */
  reloginCount: number
}

export const useSessionStore = create<SessionState>(() => ({
  token: typeof window === 'undefined' ? '' : read(),
  currentUser: null,
  authStatus: null,
  reloginCount: 0,
}))

/** 当前令牌（**每个请求都现取**：刚登录拿到的会话下一次请求就该生效）。 */
export function sessionToken(): string {
  return useSessionStore.getState().token
}

export function setSessionToken(next: string): void {
  const token = next.trim()
  useSessionStore.setState({ token })
  try {
    if (token) window.localStorage.setItem(SESSION_TOKEN_STORAGE_KEY, token)
    else window.localStorage.removeItem(SESSION_TOKEN_STORAGE_KEY)
  } catch {
    // 存不上就只在本次会话生效
  }
}

/** 清除会话与账号缓存（退出登录、会话失效都走它）。 */
export function clearSessionToken(): void {
  setSessionToken('')
  useSessionStore.setState({ currentUser: null })
}

/** 是否持有可用凭据。v0.11 起只有一种：登录会话。 */
export function hasCredential(): boolean {
  return Boolean(sessionToken())
}

export function requestRelogin(): void {
  useSessionStore.setState((state) => ({ reloginCount: state.reloginCount + 1 }))
}

/**
 * 这个失败是不是"**凭据失效**"（服务端明确回 401）。
 *
 * 为什么值得单列一条判据（D07，2026-09-28 走查）：**只有 401 才该清令牌**。
 * 网络不通、请求超时、后端 5xx 都不是"你的登录过期了"——把它们当成过期，
 * 用户会被强制登出，而且**本地令牌被删掉**（重连之后还得重新输密码）。
 * 判据就一条：`api/client.ts` 给错误标过 `status`（`error.status = response.status`），
 * 只有 401 算失效；没有 `status` 的（fetch 直接抛的 TypeError、超时）一律不算。
 *
 * 放在这里而不是各调用点各写一遍：`restoreSession`、登录页那条探活、
 * 以及以后任何"拿令牌去试一下"的地方，都该用同一条口径。
 */
export function isUnauthorized(error: unknown): boolean {
  return (error as { status?: number } | null | undefined)?.status === 401
}
