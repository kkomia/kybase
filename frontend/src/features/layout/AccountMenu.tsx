/**
 * 用户区（侧栏左下）：显示当前用户，点击向上展开菜单。
 *
 * 菜单包含头像、主题切换与退出登录。切换身份需要先退出再登录，
 * 避免界面身份与后端身份不一致。
 */
import { useState } from 'react'
import {
  RiArrowDownSLine,
  RiLogoutBoxRLine,
  RiMoonLine,
  RiSunLine,
  RiUserLine,
} from '@remixicon/react'
import { useNavigate } from 'react-router'

import { AvatarDialog } from '@/features/misc/settings/AvatarDialog'
import { setTheme, useThemeMode } from '@/features/misc/settings/useTheme'
import { cn } from '@/lib/utils'
import { logout } from '@/lib/sessionActions'
import { useSessionStore } from '@/lib/session'
import { setOperator, useOperatorStore } from '@/lib/operator'
import { Avatar, AvatarFallback, AvatarImage } from '@/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/ui/dropdown-menu'

import { useSidebar } from './useSidebar'

const ICON = 14

/** 「我是谁」的兜底：没有头像就用名字的第一个字（与旧 `AppAvatar` 同一口径）。 */
function initialOf(name: string): string {
  return name.trim().slice(0, 1) || '·'
}

/**
 * 当前**生效**的主题（`'system'` 档要落到系统实际上是什么）。
 *
 * `useThemeMode()` 是"用户选了哪一档"，而菜单里那句「切换为浅色 / 深色」问的是
 * "点下去会变成哪一边"——所以这里再落一层。
 */
function useResolvedDark(): boolean {
  const mode = useThemeMode()
  if (mode === 'dark') return true
  if (mode === 'light') return false
  return Boolean(window.matchMedia?.('(prefers-color-scheme: dark)').matches)
}

export function AccountMenu() {
  const navigate = useNavigate()
  const currentUser = useSessionStore((state) => state.currentUser)
  const dark = useResolvedDark()
  /**
   * 侧栏折叠态：这一行在折叠栏里只剩头像，于是**整行居中、gap 归零**。
   *
   * 这两个值原先写在 `layout.css` 的 `.ly-sidebar-collapsed .ly-account-row` 里，
   * 靠"那份文件没有 `@layer`"才压得过这里的 `gap-2`。收层之后**层序与优先级无关**，
   * 层里的声明压不过工具类——所以状态在这里表达（`useSidebar` 是模块级单例，
   * 与侧栏读的是同一份状态，不会出现"栏收了、行没动"）。
   */
  const { collapsed } = useSidebar()

  const [avatarOpen, setAvatarOpen] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)

  /** 这一行写谁：账号的名字（写不出来就留白，见文件头那一段）。 */
  const identityName = currentUser?.name ?? ''
  /** 这一格是**账号自己的角色**（屏幕上印的「管理员 / 成员」）。 */
  const accountIsAdmin = currentUser?.role === 'admin'
  const identityRole = currentUser ? (accountIsAdmin ? '管理员' : '成员') : ''

  function onToggleTheme(): void {
    // 切到**另一边**：所以文案要说清切过去是哪个
    setTheme(dark ? 'light' : 'dark')
  }

  /**
   * 退出登录。
   *
   * 三件事的顺序与上游一致：**先退会话，再清本地，最后跳登录页**。
   * `logout()` 内部已经把"服务端失败也清令牌"兜住了；这里再兜一层是给"状态刷新失败"
   * （`ensureAuthStatus` 那次请求）——本地那几件清理不能因为它被跳过。
   */
  async function onLogout(): Promise<void> {
    setLoggingOut(true)
    try {
      await logout()
    } catch {
      // 忽略：本地清理在下面照做
    } finally {
      useOperatorStore.setState({ roster: [] })
      setOperator('')
      setLoggingOut(false)
    }
    await navigate('/login')
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={cn(
              'ly-account-row flex min-h-11 w-full min-w-0 items-center gap-2 rounded-nav p-2 text-left transition-colors hover:bg-[var(--bg-hover)] data-[state=open]:bg-[var(--bg-hover)]',
              collapsed && 'justify-center gap-0',
            )}
            aria-label={identityName ? `账号：${identityName}` : '账号'}
          >
            {/* 头像是**一个 28px 的圆**（`--avatar-size`），不是一枚线稿图标：
                圆是"这里将来会是你的一张脸"，而灰色小人图标读起来像"一个叫『用户』的入口" */}
            <Avatar className="shrink-0">
              {currentUser?.avatar_url && <AvatarImage src={currentUser.avatar_url} alt="" />}
              <AvatarFallback>{initialOf(identityName)}</AvatarFallback>
            </Avatar>
            {/* 名字、角色、箭头都用 max-width 收（`.ly-collapsible`），
                折叠态收到 0 而不是 `display: none`——后者是瞬时的，没有过渡可接 */}
            {/* 行高**归 20px**（`leading-5`）：Kimi 那份对照表里是 `14px/20px 500`，
                而默认行高在这套 token 下算出 20.58px —— 差 0.58px，
                但它是"名字基线比左侧导航项低半像素"这类观感的来源 */}
            <span
              className="ly-collapsible flex-1 truncate text-[length:var(--text-meta-size)] leading-5 font-medium text-text-primary"
              title={identityName}
            >
              {identityName}
            </span>
            {identityRole && (
              <span className="ly-collapsible shrink-0 text-[length:var(--text-micro-size)] whitespace-nowrap text-text-tertiary">
                {identityRole}
              </span>
            )}
            {/* 行右端是**一个 44×44 的图标位**：视觉上仍是那枚小箭头，
                但命中区与行高同高（44）——点起来不再需要瞄 */}
            <span className="ly-collapsible flex h-11 w-11 shrink-0 items-center justify-center text-text-tertiary">
              <RiArrowDownSLine size={14} aria-hidden="true" />
            </span>
          </button>
        </DropdownMenuTrigger>
        {/* 向上弹：它挂在页脚底部，向下会出到屏幕外 */}
        <DropdownMenuContent side="top" align="start" className="w-[168px]">
          {/* 「头像」只给真账号（没有账号就没有头像那一族端点） */}
          {currentUser && (
            <DropdownMenuItem onSelect={() => setAvatarOpen(true)}>
              <RiUserLine size={ICON} aria-hidden="true" /> 头像
            </DropdownMenuItem>
          )}
          {/* 「API 密钥」不在这里：2026-10-09 起它是侧栏上的一页（`/api-keys`）。
              这一份菜单只留"我是谁"那三件事（见文件头那张表）。 */}
          <DropdownMenuItem onSelect={onToggleTheme}>
            {dark ? (
              <RiSunLine size={ICON} aria-hidden="true" />
            ) : (
              <RiMoonLine size={ICON} aria-hidden="true" />
            )}
            {dark ? '切换为浅色' : '切换为深色'}
          </DropdownMenuItem>
          {/* 退出登录只在真有账号时给：没有会话就没有可退的东西 */}
          {currentUser && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                disabled={loggingOut}
                onSelect={() => void onLogout()}
              >
                <RiLogoutBoxRLine size={ICON} aria-hidden="true" />{' '}
                {loggingOut ? '正在退出…' : '退出登录'}
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {/* 头像浮层由本组件持有开合：菜单一选项就收起（Radix 的默认行为），
          所以不会出现"两层浮层叠在一起"。 */}
      <AvatarDialog
        open={avatarOpen}
        name={identityName}
        url={currentUser?.avatar_url ?? ''}
        onClose={() => setAvatarOpen(false)}
      />
    </>
  )
}
