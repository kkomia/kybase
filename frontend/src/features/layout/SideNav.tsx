/**
 * 导航侧栏 —— 知识库管理台的**工作台分栏**。
 *
 * ```
 * ╭ kybase（字标）              ◧ ╮   ← 品牌位 + 折叠开关
 * │                              │
 * │  工作台                       │   ← 分区标签（12px、三级墨色、字距 .06em）
 * │   ▎概览                       │   ← 当前项：品牌蓝底 + 左侧那道 3px 指示条
 * │    所有知识库                  │
 * │    任务中心                    │
 * │                              │
 * │  接入与配置                    │
 * │    接入                       │
 * │    模型                       │
 * │    API 密钥                   │
 * │                              │
 * │  ╭ ● kkomia  管理员  ⌄ ╮      │   ← 账号（向上展开）
 * ╰──────────────────────────────╯
 * ```
 *
 * ## 三处设计决定
 *
 * 1. **分两组**：前三项是"日常看什么"，后三项是"外面怎么接进来"。
 *    旧版六项平铺，读起来是一串没有重心的入口；分组标签用 12px 的灰字，
 *    只做"这里换了一件事"的提示，不抢导航项的位置；
 * 2. **当前项用品牌蓝的 9% 底 + 一道指示条**：蓝在这一版里只出现在
 *    "动作 / 当前 / 数据"三处，导航当前项正是"当前"——这是它唯一一次
 *    以面积出现的地方（9% 底，仍然很轻）；
 * 3. **指示条是滑动的**：换页时那道 3px 的竖条**平滑滑到新位置**，
 *    而不是在旧位置消失、在新位置出现。这是"动感"里最容易被察觉的一处：
 *    它把"我换了一页"变成一次位移，而不是一次闪烁。
 *
 * ## 折叠态
 *
 * 宽度从 256 → 72，文字用 `max-width` 收到 0（不用 `display: none`：那是瞬时的，
 * 宽度动画会断在它身上）。分区标签换成一条发丝线；导航项只剩图标，整行居中。
 * 折叠开关自己跟着右缘滑到正中——它的绝对定位位置在两种状态下恰好都是右边距 16px。
 */
import { useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router'
import {
  RiBook2Line,
  RiCpuLine,
  RiDashboardLine,
  RiKey2Line,
  RiTaskLine,
  RiToolsLine,
} from '@remixicon/react'

import { preloadPage } from '@/app/routes'
import { Logo } from '@/features/chat/ui/Logo'
import { cn } from '@/lib/utils'

import { AccountMenu } from './AccountMenu'
import { IconSidebar } from './icons'
import { useSidebar } from './useSidebar'
import './layout.css'

/**
 * 主导航。`page` 是懒加载入口名（划过/聚焦时预热那一页的代码）。
 * 图标用 Remix Icon，与仓储里其它地方同一套。
 */
const NAV_GROUPS = [
  {
    label: '工作台',
    items: [
      { to: '/', label: '概览', icon: RiDashboardLine, exact: true, page: 'dashboard' },
      {
        to: '/knowledge-bases',
        label: '所有知识库',
        icon: RiBook2Line,
        exact: true,
        page: 'knowledgeBases',
      },
      { to: '/tasks', label: '任务中心', icon: RiTaskLine, exact: false, page: 'tasks' },
    ],
  },
  {
    label: '接入与配置',
    items: [
      { to: '/tools', label: '接入', icon: RiToolsLine, exact: false, page: 'tools' },
      { to: '/models', label: '模型', icon: RiCpuLine, exact: false, page: 'models' },
      { to: '/api-keys', label: 'API 密钥', icon: RiKey2Line, exact: false, page: 'apiKeys' },
    ],
  },
] as const

/** 指示条自身的高度（16px）与它在导航项里的垂直居中偏移。 */
const INDICATOR_HEIGHT = 16

export function SideNav() {
  const location = useLocation()
  const { collapsed, toggleSidebar } = useSidebar()

  const navRef = useRef<HTMLElement>(null)
  const [indicator, setIndicator] = useState<{ y: number } | null>(null)

  function isActive(to: string, exact: boolean): boolean {
    return exact ? location.pathname === to : location.pathname.startsWith(to)
  }

  /**
   * 量当前项的位置，让那道指示条滑过去。
   *
   * 用 `getBoundingClientRect` 的差值（而不是 `offsetTop`）：导航项的祖先里有
   * `position: relative` 的那一层是 nav 自己，差值法不依赖中间有几层定位上下文。
   * 依赖里带 `collapsed`：折叠时行高不变、但左右 padding 变了，位置也要重算。
   */
  useLayoutEffect(() => {
    const nav = navRef.current
    if (!nav) return
    const el = nav.querySelector<HTMLElement>('[data-nav-active="true"]')
    if (!el) {
      setIndicator(null)
      return
    }
    const navTop = nav.getBoundingClientRect().top
    const rect = el.getBoundingClientRect()
    setIndicator({ y: rect.top - navTop + (rect.height - INDICATOR_HEIGHT) / 2 })
  }, [location.pathname, collapsed])

  return (
    <aside
      className={cn(
        'ly-sidebar flex flex-col bg-canvas',
        collapsed
          ? 'ly-sidebar-collapsed w-[var(--sidebar-collapsed-width)] flex-[0_0_var(--sidebar-collapsed-width)]'
          : 'w-[var(--sidebar-width)] flex-[0_0_var(--sidebar-width)] overflow-hidden',
      )}
      aria-label="侧栏"
    >
      {/* 品牌位：展开时是字标（与登录页同一枚），折叠时只剩环行星 */}
      <div className="relative flex h-[var(--sidebar-header-height)] items-center gap-2 px-5 text-text-primary">
        <span className={cn('ly-collapsible flex items-center', collapsed && 'px-0')}>
          {collapsed ? <Logo variant="mark" size={22} /> : <Logo variant="wordmark" size={19} />}
        </span>
        <button
          type="button"
          className="absolute top-1/2 right-4 inline-flex size-7 -translate-y-1/2 items-center justify-center rounded-[var(--radius-control)] text-text-tertiary transition-[color,background-color] duration-[var(--motion-fast)] hover:bg-[var(--bg-hover)] hover:text-text-primary"
          aria-label={collapsed ? '展开侧栏' : '收缩侧栏'}
          aria-expanded={!collapsed}
          title={collapsed ? '展开侧栏' : '收缩侧栏'}
          onClick={toggleSidebar}
        >
          <IconSidebar size={17} />
        </button>
      </div>

      <nav ref={navRef} className="relative flex flex-col px-3 pb-2" aria-label="主导航">
        {/* 滑动指示条：没有当前项时（例如 404 页）隐藏 */}
        <span
          className="ly-nav-indicator"
          style={{
            transform: `translateY(${indicator?.y ?? 0}px)`,
            opacity: indicator ? 1 : 0,
          }}
          aria-hidden="true"
        />

        {NAV_GROUPS.map((group, groupIndex) => (
          <div key={group.label} className="flex flex-col">
            {collapsed ? (
              groupIndex > 0 && (
                <span className="mx-1 my-2 h-px bg-[var(--border-hairline)]" aria-hidden="true" />
              )
            ) : (
              <span className="ly-collapsible px-2.5 pt-3 pb-1.5 text-[length:var(--text-c2-size)] font-medium tracking-[0.08em] text-text-tertiary">
                {group.label}
              </span>
            )}

            {group.items.map((item) => {
              const active = isActive(item.to, item.exact)
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  onMouseEnter={() => preloadPage(item.page)}
                  onFocus={() => preloadPage(item.page)}
                  data-nav-active={active ? 'true' : undefined}
                  aria-current={active ? 'page' : undefined}
                  title={collapsed ? item.label : undefined}
                  className={cn(
                    'ly-nav-item group relative flex min-h-[38px] items-center gap-2.5 rounded-[var(--radius-nav)] px-2.5 text-[length:var(--text-meta-size)] font-medium no-underline transition-[background-color,color,padding,gap] duration-[var(--motion-slow)] ease-out',
                    active
                      ? 'bg-[var(--bg-accent-soft)] text-text-primary'
                      : 'text-text-secondary hover:bg-[var(--bg-hover)] hover:text-text-primary',
                    collapsed && 'justify-center gap-0 px-0',
                  )}
                >
                  <item.icon
                    size={18}
                    className={cn(
                      'shrink-0 transition-colors duration-[var(--motion-fast)]',
                      active ? 'text-accent' : 'text-text-tertiary group-hover:text-text-secondary',
                    )}
                    aria-hidden="true"
                  />
                  <span className="ly-collapsible truncate">{item.label}</span>
                </Link>
              )
            })}
          </div>
        ))}
      </nav>

      {/* 页脚：账号（向上展开的菜单）。没有分隔线——区块之间靠留白分块 */}
      <div className="mt-auto px-2 pb-2">
        <AccountMenu />
      </div>
    </aside>
  )
}
