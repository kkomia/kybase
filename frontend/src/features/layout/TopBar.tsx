/**
 * 顶栏：**这一份界面的"仪表框"**。
 *
 * 结构（从左到右）：
 *
 * ```
 * 工作台 / 概览                         ● 在线   │   ◐
 * └ 面包屑：分区 + 当前页                └ 服务状态   └ 明暗切换
 * ```
 *
 * 三件事，都只放"全局成立"的东西：
 *
 * 1. **面包屑**：告诉人"我在哪"。分区名用三级墨色、当前页用主墨色——
 *    这一条与左侧导航的选中态是同一个信息的两种表达，在窄屏（侧栏收起）时它是唯一的定位；
 * 2. **服务状态**：一个会呼吸的绿点。它是这一份管理台里**唯一常驻的实时信号**，
 *    也是"界面在活着"最便宜的表达——服务断了、令牌过期了，这里先变色；
 * 3. **明暗切换**：账号菜单里也有一条，但那是二级入口；顶栏这一颗是给
 *    "现在就想换个亮度的眼睛"的一次点击。
 *
 * 顶栏是**内容卡片内部**的 sticky 头（不是盖在卡片外面的浮层）：
 * 卡片是"一扇窗户"，顶栏是窗框上那道横档——它跟着卡片一起有圆角，滚动时内容从它下面过。
 */
import { RiArrowRightSLine, RiMoonLine, RiSunLine } from '@remixicon/react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router'

import { getHealth } from '@/api/health'
import { setTheme, useThemeMode } from '@/features/misc/settings/useTheme'
import { cn } from '@/lib/utils'

/**
 * 路由 →（分区, 页面名）。顺序即优先级：先匹配到的赢。
 *
 * 这一份清单与 `App.tsx` 的 `TITLES`（浏览器标签名）是同一份信息的两个出口：
 * 那一条给标签页，这一条给人看。**没有做成一份共享常量**是刻意的——
 * 标签名里带产品名后缀，面包屑里不带，硬合成一份反而要写一堆"这里去掉那里加上"。
 */
const CRUMBS: Array<[RegExp, string, string]> = [
  [/^\/login/, '账户', '登录'],
  [/^\/$/, '工作台', '概览'],
  [/^\/dashboard/, '工作台', '概览'],
  [/^\/knowledge-bases/, '工作台', '知识库'],
  [/^\/kb\/[^/]+\/wiki/, '知识库', 'Wiki'],
  [/^\/kb\//, '知识库', '库详情'],
  [/^\/documents\//, '知识库', '文档详情'],
  [/^\/tasks/, '工作台', '任务中心'],
  [/^\/tools/, '接入与配置', '接入'],
  [/^\/models/, '接入与配置', '模型'],
  [/^\/api-keys/, '接入与配置', 'API 密钥'],
]

function crumbOf(pathname: string): [string, string] {
  for (const [pattern, section, page] of CRUMBS) {
    if (pattern.test(pathname)) return [section, page]
  }
  return ['工作台', '未知页面']
}

/** 当前**生效**的主题（`system` 档要落到系统实际上是什么）。 */
function useResolvedDark(): boolean {
  const mode = useThemeMode()
  if (mode === 'dark') return true
  if (mode === 'light') return false
  return Boolean(window.matchMedia?.('(prefers-color-scheme: dark)').matches)
}

/** 服务状态：一次探活，60 秒内不重复问；失败就是"离线"，不重试。 */
function useServiceStatus(): 'online' | 'offline' | 'checking' {
  const { status } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    retry: 0,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
  })
  if (status === 'pending') return 'checking'
  return status === 'success' ? 'online' : 'offline'
}

export function TopBar() {
  const location = useLocation()
  const [section, page] = crumbOf(location.pathname)
  const dark = useResolvedDark()
  const service = useServiceStatus()

  return (
    <header className="ly-topbar">
      <nav className="flex min-w-0 items-center gap-1.5" aria-label="当前位置">
        <span className="text-[length:var(--text-micro-size)] text-text-tertiary">{section}</span>
        <RiArrowRightSLine size={13} className="shrink-0 text-text-quaternary" aria-hidden="true" />
        <span className="truncate text-[length:var(--text-meta-size)] font-medium text-text-primary">
          {page}
        </span>
      </nav>

      <div className="flex items-center gap-1">
        <span
          className={cn(
            'flex items-center gap-1.5 rounded-pill px-2 py-1 text-[length:var(--text-micro-size)] transition-colors',
            service === 'online' && 'text-status-success',
            service === 'offline' && 'text-status-danger',
            service === 'checking' && 'text-text-quaternary',
          )}
          title={
            service === 'online'
              ? '知识库服务运行中'
              : service === 'offline'
                ? '连不上知识库服务'
                : '正在探活…'
          }
        >
          <span
            className={cn(
              'status-dot',
              service === 'online' && 'status-dot-live',
              service === 'checking' && 'animate-pulse',
            )}
            aria-hidden="true"
          />
          {service === 'online' ? '在线' : service === 'offline' ? '离线' : '…'}
        </span>

        <span className="mx-1 h-4 w-px bg-[var(--border-hairline)]" aria-hidden="true" />

        <button
          type="button"
          onClick={() => setTheme(dark ? 'light' : 'dark')}
          className="inline-flex size-8 items-center justify-center rounded-[var(--radius-control)] text-text-tertiary transition-[color,background-color,transform] duration-[var(--motion-fast)] hover:bg-[var(--bg-hover)] hover:text-text-primary active:scale-95"
          aria-label={dark ? '切换为浅色' : '切换为深色'}
          title={dark ? '切换为浅色' : '切换为深色'}
        >
          {dark ? <RiSunLine size={16} /> : <RiMoonLine size={16} />}
        </button>
      </div>
    </header>
  )
}
