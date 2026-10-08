/**
 * 壳的**减法**（kybase 专有的一页用例；上游那份 `layout-shell.test.tsx` 测的是会话 / 项目，
 * 那些东西没有搬过来）。
 *
 * 这一份钉的是"老板拍板的那件事真的落到了代码里"，而不是某个组件的内部行为：
 *
 * 1. **侧栏只剩三块**：品牌位（环行星标 + 折叠开关）+ 三个顶层导航项
 *    （所有知识库 / 概览 / 任务中心）+ 账号区。**agent 语义的入口一个都不许出现**——
 *    新建会话、笔记、记忆、能力、项目、对话这几项在 kylab 那份侧栏里都在，在这里必须在
 *    （`SideNav` 文件头那张减法表）；
 * 2. **导航项是拍平的**：三条**直接挂在「主导航」下面**，中间不许有「知识库」父分组
 *    （这一栏整个就是知识库控制台，父分组是纯冗余——`SideNav` 文件头）；
 * 3. **路由入口清单**（`app/routes.ts` 的 `PAGES`）只含登录 / 概览 / 知识库三页 / 任务中心 / 404
 *    ——`chat` / `notes` / `memory` / `capabilities` / `backup` 一个都不许有。
 *    这条比"渲染出来没有"更硬：`PAGES` 的键就是懒加载入口的**全部**，
 *    多一个键就意味着将来有人能把那一页接回路由表而没人察觉。
 *
 * 反过来的那些（"没有的东西渲染出来了吗"）用 `queryByText` 判：**空 = 对**。
 * 侧栏是纯展示件，断言文案比断言类名稳（类名会随样式层改）。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { PAGES } from '@/app/routes'
import { AppShell } from '@/features/layout'

/** 侧栏里**不许**出现的那几项（agent 语义的入口，见文件头）。 */
const AGENT_ENTRIES = ['新建会话', '笔记', '记忆', '能力', '项目', '对话']

/** 主导航那三条（顺序即渲染顺序），**顶层**——没有父分组。 */
const NAV_LABELS = ['所有知识库', '概览', '任务中心']

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
  it('侧栏在：品牌位 + 三个顶层项 + 账号区', () => {
    mountShell()
    expect(screen.getByLabelText('侧栏')).toBeInTheDocument()
    // 品牌位：环行星标 + 折叠开关（那颗开关的文案随折叠态变，所以两条都收）
    expect(screen.getByRole('img', { name: 'KYLAB 知识库' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /(收缩|展开)侧栏/ })).toBeInTheDocument()
    // 三条导航项：顶层项是**链接**（不是某个分组下的行）
    for (const label of NAV_LABELS) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
    // 账号区也在（头像 / 名字 / 退出登录那一片），否则这一栏就只剩导航了
    expect(screen.getByRole('button', { name: /账号/ })).toBeInTheDocument()
  })

  it('三个导航项直接挂在「主导航」下面——中间没有父分组', () => {
    mountShell()
    const nav = screen.getByRole('navigation', { name: '主导航' })
    // 拍平 = 这三条是 nav 的**直接子元素**，且 nav 里不再有分组用的 ul/li
    expect(nav.children).toHaveLength(NAV_LABELS.length)
    expect(nav.querySelector('ul')).toBeNull()
    for (const label of NAV_LABELS) {
      const link = screen.getByRole('link', { name: label })
      expect(nav).toContainElement(link)
      expect(link.parentElement).toBe(nav)
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

  it('路由入口清单只含登录 + 知识库那几页 + 404', () => {
    expect(Object.keys(PAGES).sort()).toEqual([
      'dashboard',
      'document',
      'knowledgeBase',
      'knowledgeBases',
      'login',
      'notFound',
      'tasks',
      'wiki',
    ])
  })
})
