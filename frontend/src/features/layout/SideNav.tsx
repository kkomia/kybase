/**
 * 导航侧栏（《前端设计规范》§5）——从 kylab 前端的侧栏裁出来的**知识库版**。
 *
 * 结构（壳做减法之后只剩这三块）：
 *
 * ```
 * 品牌位（环行星标 + 折叠开关）
 * 主导航：所有知识库 / 概览 / 任务中心 / 工具 / 模型 / API 密钥（**顶层**项，没有父分组）
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
 * 留下的三件事一件没少：**品牌位（含折叠）**、**主导航**、
 * **账号区**（头像 / 主题 / 退出登录，见 `AccountMenu`）。
 *
 * ## 主导航：固定几项、**拍平成顶层**
 *
 * 上游把知识库那几条挂在「知识库」这个可折叠父分组下面。而这一份界面**整个就是知识库控制台**，
 * 父分组除了把"这里是知识库"再说一遍之外不提供任何层级信息，还白占一行与一次折叠动作
 * ——所以分组头连同它的箭头、展开态、以及"收起时才点亮"的那个组级选中态一起删掉，
 * 子项提成顶层导航项（`NAV_ITEMS`），行样式沿用导航项那一套（`NAV_ROW`）。
 *
 * 各项**无条件渲染**：连不上的处置在页面自己身上（页面进页时那条读会报它自己的错）。
 * 顺序：所有知识库（`/knowledge-bases`）/ 概览（`/`，exact）/ 任务中心（`/tasks`）/
 * 工具（`/tools`）/ 模型（`/models`）/ API 密钥（`/api-keys`）；后三项是 2026-10-09 补的
 * 对外能力面（见 `NAV_ITEMS` 那张表）。选中态**各算各的**（`isActive`），不再有"整组亮起来"那一条。
 *
 * ## 图标：Remix Icon（与上游同一套）
 *
 * | 位置 | 图标 |
 * | --- | --- |
 * | 所有知识库 | `book-2-line` |
 * | 概览 | `dashboard-line` |
 * | 任务中心 | `task-line` |
 * | 工具 | `tools-line` |
 * | 模型 | `cpu-line` |
 * | API 密钥 | `key-2-line` |
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
import { Link, useLocation } from 'react-router'

import { preloadPage } from '@/app/routes'
import {
  RiBook2Line,
  RiCpuLine,
  RiDashboardLine,
  RiKey2Line,
  RiTaskLine,
  RiToolsLine,
} from '@remixicon/react'

import { Logo } from '@/features/chat/ui/Logo'
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
 * 主导航：**固定几项**，**无条件渲染**（上游 R5 的口径照搬）。
 *
 * | 位置 | 页面 | 为什么在这里 |
 * | --- | --- | --- |
 * | 所有知识库 | `/knowledge-bases` | 库清单，进来的第一站 |
 * | 概览 | `/` | 驾驶舱，与上游同一落地页 |
 * | 任务中心 | `/tasks` | 流水线任务与运行负载 |
 * | 工具 | `/tools` | 这个服务对外提供了什么（工具 / MCP 地址 / 技能） |
 * | 模型 | `/models` | 用途绑哪个模型（出题 / 摘要走「对话生成」那一格）+ 供应商与模型登记；写入口只认管理员 |
 * | API 密钥 | `/api-keys` | 管长期密钥；端点只认管理员，成员进去看到受限态 |
 *
 * 前三项是「知识库这个控制台」的日常面；后三项（工具 / 模型 / API 密钥）是**对外能力**
 * 那一面，2026-10-09 补进来时排在后面：它们回答的是"外面怎么接进来"与"模型从哪儿来"，
 * 与改库、看任务不同一件事。
 * 「API 密钥」原来挂在账号菜单上（`AccountMenu`）——它够日常、又要有自己的地址栏入口，
 * 所以搬成这一页；账号菜单里只剩头像与主题（翻转那一项）。
 *
 * 顺序与图标：前三条照上游「知识库」那一组原样搬过来，只是不再有父分组（见文件头）。
 * 这一栏**不列每个库的名字**：库可能几十个，全铺在侧栏上正是"知识库占的地方太多"的根源
 * （上游原话）。
 *
 * `motion` 是这一项的**悬停动效族**（v0.18：一项一个动作，取的是"这个图标画的是什么"
 * 该有的动作）。这一份只留下 `slide`（书脊滑进来）那一族——它是 `layout.css` 里唯一还有
 * 关键帧的：另外三族（气泡落下 / 便签放上 / 记忆歪头）服务的图标长在 agent 域，
 * 随那些条目一并删了。拍平之前它长在分组头那颗书脊图标上，现在**跟着那颗图标留在
 * 「所有知识库」这一项**；另外几项挂一个没有关键帧的类名等于"悬停没反应"，
 * 所以它们的 `motion` 是 `undefined`——不是漏了。
 *
 * `page` 是这一项对应的**懒加载入口名**（`app/routes.ts` 的 `PAGES`）：划过/聚焦时按它
 * 预热那一页的代码。**必须逐项写自己的**——写死某一个的话，划过「工具」预热的是知识库
 * 那一页，点进去照旧等一次 chunk。
 */
const NAV_ITEMS = [
  {
    to: '/knowledge-bases',
    label: '所有知识库',
    icon: RiBook2Line,
    exact: true,
    motion: 'slide',
    page: 'knowledgeBases',
  },
  // 「概览」= 驾驶舱，住 `/`（与上游一致：落地页就是概览，书签不用改）。
  // `/dashboard` 只是同一页的旧入口，在路由表里是一条重定向。
  {
    to: '/',
    label: '概览',
    icon: RiDashboardLine,
    exact: true,
    motion: undefined,
    page: 'dashboard',
  },
  {
    to: '/tasks',
    label: '任务中心',
    icon: RiTaskLine,
    exact: false,
    motion: undefined,
    page: 'tasks',
  },
  {
    to: '/tools',
    label: '工具',
    icon: RiToolsLine,
    exact: false,
    motion: undefined,
    page: 'tools',
  },
  {
    to: '/models',
    label: '模型',
    icon: RiCpuLine,
    exact: false,
    motion: undefined,
    page: 'models',
  },
  {
    to: '/api-keys',
    label: 'API 密钥',
    icon: RiKey2Line,
    exact: false,
    motion: undefined,
    page: 'apiKeys',
  },
] as const

export function SideNav() {
  const location = useLocation()
  const { collapsed, toggleSidebar } = useSidebar()

  function isActive(to: string, exact: boolean): boolean {
    return exact ? location.pathname === to : location.pathname.startsWith(to)
  }

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
        {/* **顶层**导航项（见 `NAV_ITEMS`），**不列库名**、**无条件渲染**：
            没有父分组、没有箭头、没有展开态——这一栏整个就是知识库控制台（见文件头）。 */}
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            /* 划过就把**这一项自己那一页**的代码拉下来（照上游）。列表数据由页面自己进页时拉，
               导航项不替它发请求。 */
            onMouseEnter={() => preloadPage(item.page)}
            onFocus={() => preloadPage(item.page)}
            /* 选中态用**中性 alpha 底**（不是品牌色底，Kimi 的实测值），而且是类名不是内联样式：
               内联样式会压过 `:hover`，鼠标划过当前项就没了反馈 */
            className={cn(
              NAV_ROW,
              collapsed && NAV_ROW_COLLAPSED,
              isActive(item.to, item.exact) && 'bg-[var(--bg-selected)]',
            )}
            aria-current={isActive(item.to, item.exact) ? 'page' : undefined}
            title={collapsed ? item.label : undefined}
          >
            <item.icon
              size={18}
              className={item.motion ? `ly-nav-motion-${item.motion} shrink-0` : 'shrink-0'}
              aria-hidden="true"
            />
            <span className="ly-collapsible">{item.label}</span>
          </Link>
        ))}
      </nav>

      {/* 页脚：**没有上分隔线**，内边距 8px——侧栏里一条分割线都没有，区块之间靠留白分块。 */}
      <div className="ly-sidebar-foot mt-auto p-2">
        <AccountMenu />
      </div>
    </aside>
  )
}
