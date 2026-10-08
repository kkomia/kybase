/**
 * 主题（《前端设计规范》§2、§8）——与旧前端 `composables/useTheme.ts` 对应。
 *
 * 约定：
 * - 主题落在 `document.documentElement.dataset.theme`，与 index.html 的首屏脚本
 *   共用 localStorage 键 `kylab-theme`（这一条不能改，否则首屏脚本读不到）；
 * - 未手动选择时跟随系统，手动选择后以用户选择为准；
 * - 切换只改 CSS 变量，无过渡动画（§8：切换无闪烁）。
 *
 * 为什么放在这一域而不是等应用壳：设置 → 外观这一节今天就要能改主题，
 * 而它读写的就是那一个属性 + 一个 localStorage 键——与壳将来实现的是同一份约定。
 */
import { useSyncExternalStore } from 'react'

export type ThemeName = 'light' | 'dark'

/** 用户可选的三档：显式浅色 / 显式深色 / 跟随系统。 */
export type ThemeMode = ThemeName | 'system'

export const THEME_STORAGE_KEY = 'kylab-theme'

function readStoredTheme(): ThemeName | null {
  try {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY)
    return saved === 'light' || saved === 'dark' ? saved : null
  } catch {
    return null
  }
}

function systemTheme(): ThemeName {
  if (typeof window === 'undefined' || !window.matchMedia) return 'light'
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

let mode: ThemeMode = 'system'

const listeners = new Set<() => void>()

function emit(): void {
  for (const listener of listeners) listener()
}

/** 把主题写到根节点，CSS 变量随之整体切换。 */
function applyTheme(next: ThemeName): void {
  if (typeof document !== 'undefined') document.documentElement.dataset.theme = next
}

/**
 * 选择主题档位（设置 → 外观）。
 *
 * `system` 要**清掉本地选择**而不只是当前生效——否则"跟随系统"会被下一次启动
 * 读到的旧值覆盖，看起来像没生效。
 */
export function setTheme(next: ThemeMode): void {
  mode = next
  try {
    if (next === 'system') window.localStorage.removeItem(THEME_STORAGE_KEY)
    else window.localStorage.setItem(THEME_STORAGE_KEY, next)
  } catch {
    // 隐私模式下不可写：不记忆即可，本次会话仍然即时生效
  }
  applyTheme(next === 'system' ? systemTheme() : next)
  emit()
}

/** 当前档位。**初值直接读本地那份**（首屏脚本已经把它落到 `data-theme` 上了）。 */
function currentMode(): ThemeMode {
  return mode
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** 应用启动时调用一次：优先用户选择，其次系统偏好。 */
export function initTheme(): ThemeName {
  const stored = readStoredTheme()
  mode = stored ?? 'system'
  const initial = stored ?? systemTheme()
  applyTheme(initial)
  return initial
}

export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, currentMode, currentMode)
}
