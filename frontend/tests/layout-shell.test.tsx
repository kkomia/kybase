/**
 * 壳的**减法**（kybase 专有的一页用例；上游那份 `layout-shell.test.tsx` 测的是会话 / 项目，
 * 那些东西没有搬过来）。
 *
 * 这一份钉的是"老板拍板的那件事真的落到了代码里"，而不是某个组件的内部行为：
 *
 * 1. **侧栏只剩三块**：品牌位（环行星标 + 折叠开关）+ 六个顶层导航项
 *    （概览 / 所有知识库 / 任务中心 / 接入 / 模型 / API 密钥）+ 账号区。**agent 语义的入口一个都不许出现**——
 *    新建会话、笔记、记忆、能力、项目、对话这几项在 kylab 那份侧栏里都在，在这里必须在
 *    （`SideNav` 文件头那张减法表）；
 * 2. **没有可折叠的父分组**：2026-10 重做按「工作台 / 接入与配置」分了组，但分组名是
 *    **纯文本标签**——nav 里没有 aria-expanded 的控件、也没有 ul/li 的层级包装。
 *    （上一轮删掉的是那个可折叠的「知识库」父分组：它只把"这里是知识库"再说一遍。）
 * 3. **路由入口清单**（`app/routes.ts` 的 `PAGES`）只含登录 / 概览 / 知识库三页 / 任务中心 /
 *    接入 / 模型 / API 密钥 / 404——`chat` / `notes` / `memory` / `capabilities` / `backup` 一个都不许有。
 *    这条比"渲染出来没有"更硬：`PAGES` 的键就是懒加载入口的**全部**，
 *    多一个键就意味着将来有人能把那一页接回路由表而没人察觉。
 *
 * 反过来的那些（"没有的东西渲染出来了吗"）用 `queryByText` 判：**空 = 对**。
 * 侧栏是纯展示件，断言文案比断言类名稳（类名会随样式层改）。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { PAGES } from '@/app/routes'
import { AppShell } from '@/features/layout'

/** 侧栏里**不许**出现的那几项（agent 语义的入口，见文件头）。 */
const AGENT_ENTRIES = ['新建会话', '笔记', '记忆', '能力', '项目', '对话']

/**
 * 主导航那几项（顺序即渲染顺序）。
 *
 * 2026-10 界面重做把它们按「工作台 / 接入与配置」分成两组：前三项是日常看什么，
 * 后三项是外面怎么接进来。分组只是**静态标签**，不提供折叠——所以"拍平"
 * 这条产品约束（不许出现可折叠的父分组）仍然成立，见下面第二个用例。
 */
const NAV_LABELS = ['概览', '所有知识库', '任务中心', '接入', '模型', 'API 密钥']

/** 那个被删掉的父分组的名字。 */
const REMOVED_GROUP = '知识库'

function mountShell(): void {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <AppShell>
          <p>内容区</p>
        </AppShell>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('壳只留知识库语义', () => {
  it('侧栏在：品牌位 + 六个顶层项 + 账号区', () => {
    mountShell()
    expect(screen.getByLabelText('侧栏')).toBeInTheDocument()
    // 品牌位：环行星标 + 折叠开关（那颗开关的文案随折叠态变，所以两条都收）
    expect(screen.getByRole('img', { name: 'KYBASE 知识库' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /(收缩|展开)侧栏/ })).toBeInTheDocument()
    // 几条导航项：顶层项是**链接**（不是某个分组下的行）
    for (const label of NAV_LABELS) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
    // 账号区也在（头像 / 名字 / 退出登录那一片），否则这一栏就只剩导航了
    expect(screen.getByRole('button', { name: /账号/ })).toBeInTheDocument()
  })

  it('导航项挂在「主导航」里，分组是静态标签而不是可折叠的父分组', () => {
    mountShell()
    const nav = screen.getByRole('navigation', { name: '主导航' })
    // 2026-10 重做之后仍是"拍平"的：六条链接都在 nav 内，nav 里没有 ul/li，
    // 也没有任何 aria-expanded 的控件（分组名是纯文本，点它不收起任何东西）
    expect(nav.querySelector('ul')).toBeNull()
    expect(nav.querySelector('[aria-expanded]')).toBeNull()
    expect(within(nav).getByText('工作台')).toBeInTheDocument()
    expect(within(nav).getByText('接入与配置')).toBeInTheDocument()
    expect(within(nav).getAllByRole('link')).toHaveLength(NAV_LABELS.length)
    for (const label of NAV_LABELS) {
      const link = screen.getByRole('link', { name: label })
      expect(nav).toContainElement(link)
    }
  })

  it('「知识库」父分组连同它的折叠按钮一并消失了', () => {
    mountShell()
    // 分组壳（上一轮用 testid 钉过它）不许回来
    expect(screen.queryByTestId('nav-knowledge-group')).not.toBeInTheDocument()
    // 也不许换一种写法回来：无论是可点的分组头（button）还是分组链接（link）
    expect(screen.queryByRole('button', { name: REMOVED_GROUP })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: REMOVED_GROUP })).not.toBeInTheDocument()
  })

  it('agent 语义的入口一个都没有', () => {
    mountShell()
    for (const label of AGENT_ENTRIES) {
      // 「概览」与「任务中心」是顶层导航项，**不在**这份禁令里（它们在上一用例里被断言存在）
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument()
    }
  })

  it('路由入口清单只含登录 + 知识库那几页 + 接入 + 模型 + API 密钥 + 404', () => {
    expect(Object.keys(PAGES).sort()).toEqual([
      'apiKeys',
      'dashboard',
      'document',
      'knowledgeBase',
      'knowledgeBases',
      'login',
      'models',
      'notFound',
      'tasks',
      'tools',
      'wiki',
    ])
  })
})
