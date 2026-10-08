/**
 * 导航侧栏（《前端设计规范》§5）——从 kylab 前端的侧栏裁出来的**知识库版**。
 *
 * 结构（壳做减法之后只剩这三块）：
 *
 * ```
 * 品牌位（环行星标 + 折叠开关）
 * 知识库▸（所有知识库 / 概览 / 任务中心）
 * 页脚：账号（头像 + 名字 → 向上展开的菜单）
 * ```
 *
 * ## 与 kylab 那份的差别：agent 语义的入口一律不要
 *
 * 这一份前端是知识库服务的独立管理台，**没有**会话 / 笔记 / 记忆 / 能力 / 项目 / 工作区，
 * 所以上游侧栏里那几段整块摘掉了（不是"按条件隐藏"——它们在这里根本不存在）：
 *
 * | 上游有 | 这里 | 为什么 |
 * | --- | --- | --- |
 * | 「新建会话」+ 快捷键盘提示 | 无 | 没有对话页；`/chat` 在这份路由表里不存在 |
 * | 主导航：笔记 / 记忆 / 能力 | 无 | agent 语义，页面与数据面都留在 kylab |
 * | 项目节 + 对话节（含会话行、行菜单、展开/归档） | 无 | 会话、项目、工作区都是 agent 本机数据 |
 * | 历史会话面板（`ConversationHistoryPanel`） | 无 | 同上；`AppShell` 里那一段也一并摘掉 |
 * | 全局快捷键（`chat.new` / `layout.toggleSidebar`） | 无 | 那段绑定住在 kylab 的 chat 域与设置域的快捷键注册表里，随会话面一起留下；折叠开关照旧在品牌位那颗按钮上（`useSidebar`） |
 *
 * 留下的三件事一件没少：**品牌位（含折叠）**、**知识库那一组（固定三条子项）**、
 * **账号区**（头像 / 主题 / 退出登录，见 `AccountMenu`）。
 *
 * ## 知识库组：固定三条子项、**无条件渲染**（上游 R5 的口径照搬）
 *
 * 组**不按提供者状态显隐**：连不上的处置在页面自己身上（路由守卫仍在
 * `features/knowledge/ProviderRoute.tsx`，而这一档里它直接放行）。
 * 三条子项的顺序照旧：所有知识库 / 概览（`/`，exact）/ 任务中心（`/tasks`）；
 * 组头的归属因此覆盖 `/` 与 `/tasks`——站在概览或任务中心时组头点亮，
 * 正是"用户看得出自己在哪一节里"要的那件事。
 *
 * ## 图标：Remix Icon（与上游同一套）
 *
 * | 位置 | 图标 |
 * | --- | --- |
 * | 知识库 / 所有知识库 | `book-2-line` |
 * | 概览 | `dashboard-line` |
 * | 任务中心 | `task-line` |
 * | 分节箭头 | `arrow-right-s-line` |
 * | 品牌标 | 环行星（`features/chat/ui/Logo.tsx` 的 `mark`） |
 * | 折叠开关 | 自绘（`./icons.tsx`，用户给的 iconfont 原稿） |
 *
 * ## 折叠态的两处取值**必须在调用点写**
 *
 * 图标居中那两值（`gap-0 px-[14px]`）原先写在 `layout.css` 的
 * `.ly-sidebar-collapsed .ly-nav-item` 里，靠"那份文件没有 `@layer`"才压得过工具类；
 * 收层之后**层序与优先级无关**，层里的声明压不过工具类，留在 CSS 里等于从来没生效
 * （图标会偏左、gap 也不归零）。所以它们表达在 `NAV_ROW_COLLAPSED`。
 */
import { useState } from 'react'
import { Link, useLocation } from 'react-router'

import { preloadPage } from '@/app/routes'
import { RiArrowRightSLine, RiBook2Line, RiDashboardLine, RiTaskLine } from '@remixicon/react'

import { Logo } from '@/features/chat/ui/Logo'
import { useKnowledgeProviderStatus } from '@/api/provider'
import { warmKnowledgeBases } from '@/features/knowledge/store'
import { cn } from '@/lib/utils'

import { AccountMenu } from './AccountMenu'
import { IconSidebar } from './icons'
import { useSidebar } from './useSidebar'
import './layout.css'

/**
 * 导航行：40px 高、圆角 12、图标 18、gap 6（都来自 `tokens.css` 的侧栏那一组）。
 * 静止态就用主文字色——它们是主导航，读起来不该像"次要信息"。
 */
const NAV_ROW =
  'ly-nav-item flex min-h-[var(--nav-height)] items-center gap-1.5 overflow-hidden rounded-[var(--radius-nav)] px-2 text-[length:var(--text-meta-size)] leading-5 text-text-primary no-underline transition-[gap,padding] duration-[var(--motion-slow)] ease-in-out hover:bg-[var(--bg-hover)]'

/**
 * 折叠态的导航行：图标居中。
 *
 * 左右 14px = (44 − 16) ÷ 2：44 是折叠栏（60）减掉 nav 的 `px-2` 两侧，16 是图标盒。
 * 折叠开关的过渡靠 `transition-[gap,padding]`（在 `NAV_ROW` 上），换类名就能平滑滑过去。
 */
const NAV_ROW_COLLAPSED = 'gap-0 px-[14px]'

/**
 * 知识库组：**三条固定子项**（所有知识库 / 概览 / 任务中心），**无条件渲染**。
 *
 * 子项**不列每个库的名字**：库可能几十个，全铺在侧栏上正是"知识库占的地方太多"的根源
 * （上游原话）。这一项照抄 Kimi 的原样动作（右侧滑入 + 放大落定）：它是这一栏里唯一的分组头，
 * 动作与"把一叠东西从右边推上来"的语义对得上。
 */
const KNOWLEDGE_GROUP = {
  label: '知识库',
  icon: RiBook2Line,
  motion: 'slide',
  children: [
    {
      to: '/knowledge-bases',
      label: '所有知识库',
      icon: RiBook2Line,
      exact: true,
      page: 'knowledgeBases',
    },
    // 「概览」= 驾驶舱，住 `/`（与上游一致：落地页就是概览，书签不用改）。
    // `/dashboard` 只是同一页的旧入口，在路由表里是一条重定向。
    {
      to: '/',
      label: '概览',
      icon: RiDashboardLine,
      exact: true,
      motion: 'drop',
      page: 'dashboard',
    },
    {
      to: '/tasks',
      label: '任务中心',
      icon: RiTaskLine,
      exact: false,
      motion: 'spring',
      page: 'tasks',
    },
  ],
} as const

export function SideNav() {
  const location = useLocation()
  const { collapsed, toggleSidebar } = useSidebar()

  /**
   * 知识库那一组收着还是开着。
   *
   * 上游这一项要按"这一份有没有本机后端"取默认档（没有本机后端时这一组是整条栏里
   * **唯一**的内容，默认收起等于把唯一的入口藏起来），而这份界面**恒无本机后端**——
   * 默认档因此是一个常量：**开着**。用户点过一次之后听用户的，那颗箭头不能点了没反应。
   */
  const [knowledgeOpen, setKnowledgeOpen] = useState(true)

  /**
   * 知识库提供者的状态：这里**只用来做预取的那半扇门**（见 `preloadKnowledgeBases`）。
   *
   * 它**不决定**那一组显不显示——组无条件渲染（上游 R5 的口径）。
   */
  const provider = useKnowledgeProviderStatus()

  /**
   * 知识库那一项被划过/聚焦时顺手做的两件事（照上游 M4 阶段 5 的预取先例）：
   *
   * ① 把那一页的代码拉下来（点进去不必等 chunk，与导航项同一条做法）；
   * ② 把「上次看到的库列表」先取回来（**本机回环**，个位数毫秒）——点进去标题与卡片
   *    就已经在了。
   *
   * ②只在有"本机留的那份"时才做（`provider.gate`）：这一档知识库就是服务端自己，
   * 没有"本机留的那份"，所以这一跳**在这一档恒不发生**。
   * 叫一次**不等于**打一次请求：内存已有、或那一份已经画过/实时到了，它就地返回。
   */
  function preloadKnowledgeBases(): void {
    preloadPage('knowledgeBases')
    if (!provider.gate) return
    void warmKnowledgeBases()
  }

  function isActive(to: string, exact: boolean): boolean {
    return exact ? location.pathname === to : location.pathname.startsWith(to)
  }

  /**
   * 「知识库」这一组里的当前项。
   *
   * **按组里那三个子项自己算**（`isActive`），外加库详情 / Wiki（`/kb/:id`，它们不在
   * 侧栏里单列）——手写一条条前缀的那一版漏了 `/` 与 `/tasks`：站在概览或任务中心时
   * 整组一声不响（默认又是收起的），用户看不出自己在哪一节里。
   */
  const knowledgeActive =
    KNOWLEDGE_GROUP.children.some((item) => isActive(item.to, item.exact)) ||
    location.pathname.startsWith('/kb/')

  return (
    <aside
      className={
        collapsed
          ? 'ly-sidebar ly-sidebar-collapsed flex w-[var(--sidebar-collapsed-width)] flex-[0_0_var(--sidebar-collapsed-width)] flex-col bg-[var(--bg-canvas)]'
          : 'ly-sidebar flex w-[var(--sidebar-width)] flex-[0_0_var(--sidebar-width)] flex-col overflow-hidden bg-[var(--bg-canvas)]'
      }
      aria-label="侧栏"
    >
      {/* 品牌位只放那颗**环行星**：完整的 "kylab" 字标在登录页与欢迎态上——
          这一格只有 24px 宽的位置，字标在这么小的地方既读不出来、又跟下方的导航抢宽度。
          右侧是折叠开关：折叠后行星标收起，只留那颗面板图标。
          **两者都不用条件渲染摘掉**——摘掉是瞬时的、没法过渡；改用 max-width 收缩
          （`.ly-collapsible`），宽度动画才连得上。 */}
      <div className="relative flex min-h-[var(--sidebar-header-height)] items-center gap-2 pt-[15px] pr-4 pb-[9px] pl-4 text-text-primary">
        <span className="ly-collapsible flex">
          <Logo variant="mark" size={22} />
        </span>
        <button
          type="button"
          className="absolute top-4 right-4 inline-flex size-7 items-center justify-center rounded-control text-text-tertiary transition-colors hover:bg-[var(--bg-hover)] hover:text-text-primary"
          aria-label={collapsed ? '展开侧栏' : '收缩侧栏'}
          aria-expanded={!collapsed}
          title={collapsed ? '展开侧栏' : '收缩侧栏'}
          onClick={toggleSidebar}
        >
          <IconSidebar size={17} />
        </button>
      </div>

      <nav className="flex flex-col px-2 pb-2" aria-label="主导航">
        {/* 知识库组：**三条固定子项**（所有知识库 / 概览 / 任务中心），**不列库名**；
         **无条件渲染**；选中态只在收起时亮：展开之后"当前在这一组里"由子项自己说。 */}
        <div className="flex flex-col" data-testid="nav-knowledge-group">
          <button
            type="button"
            className={cn(
              NAV_ROW,
              collapsed && NAV_ROW_COLLAPSED,
              'w-full cursor-pointer border-0 text-left',
              knowledgeActive && !knowledgeOpen ? 'bg-[var(--bg-selected)]' : 'bg-transparent',
            )}
            aria-expanded={knowledgeOpen}
            title={collapsed ? KNOWLEDGE_GROUP.label : undefined}
            onClick={() => setKnowledgeOpen((open) => !open)}
          >
            <KNOWLEDGE_GROUP.icon
              size={18}
              className={`ly-nav-motion-${KNOWLEDGE_GROUP.motion} shrink-0`}
              aria-hidden="true"
            />
            <span className="ly-collapsible">{KNOWLEDGE_GROUP.label}</span>
            {!collapsed && (
              <RiArrowRightSLine
                size={13}
                aria-hidden="true"
                className={
                  knowledgeOpen
                    ? 'shrink-0 rotate-90 text-text-tertiary transition-transform'
                    : 'shrink-0 text-text-tertiary transition-transform'
                }
              />
            )}
          </button>
          {knowledgeOpen && !collapsed && (
            <ul className="mt-0 mb-1 list-none p-0 pl-6">
              {KNOWLEDGE_GROUP.children.map((item) => (
                <li key={item.to}>
                  <Link
                    to={item.to}
                    /* 划过就先把知识库那一页的代码、以及"上次看到的那份"一起拿回来（照上游）。 */
                    onMouseEnter={() => preloadKnowledgeBases()}
                    onFocus={() => preloadKnowledgeBases()}
                    aria-current={isActive(item.to, item.exact) ? 'page' : undefined}
                    className={
                      isActive(item.to, item.exact)
                        ? 'flex h-[var(--row-height-compact)] items-center gap-1.5 rounded-control bg-[var(--bg-selected)] px-2 text-[length:var(--text-meta-size)] text-text-primary no-underline'
                        : 'flex h-[var(--row-height-compact)] items-center gap-1.5 rounded-control px-2 text-[length:var(--text-meta-size)] text-text-secondary no-underline transition-colors hover:bg-[var(--bg-hover)] hover:text-text-primary'
                    }
                  >
                    <item.icon size={14} aria-hidden="true" />
                    <span className="truncate">{item.label}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </nav>

      {/* 页脚：**没有上分隔线**，内边距 8px——侧栏里一条分割线都没有，区块之间靠留白分块。 */}
      <div className="ly-sidebar-foot mt-auto p-2">
        <AccountMenu />
      </div>
    </aside>
  )
}
