/**
 * 壳的**减法**（kybase 专有的一页用例；上游那份 `layout-shell.test.tsx` 测的是会话 / 项目，
 * 那些东西没有搬过来）。
 *
 * 这一份钉的是"老板拍板的那件事真的落到了代码里"，而不是某个组件的内部行为：
 *
 * 1. **侧栏只剩三块**：品牌位（含折叠开关）+「知识库」那一组（所有知识库 / 概览 / 任务中心）
 *    + 账号区。**agent 语义的入口一个都不许出现**——新建会话、笔记、记忆、能力、项目、对话
 *    这几项在 kylab 那份侧栏里都在，在这里必须在（`SideNav` 文件头那张减法表）；
 * 2. **路由入口清单**（`app/routes.ts` 的 `PAGES`）只含登录 / 概览 / 知识库三页 / 任务中心 / 404
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
  it('侧栏在，且「知识库」那一组的三条子项都在', () => {
    mountShell()
    expect(screen.getByLabelText('侧栏')).toBeInTheDocument()
    expect(screen.getByTestId('nav-knowledge-group')).toBeInTheDocument()
    for (const label of ['所有知识库', '概览', '任务中心']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
    // 账号区也在（头像 / 名字 / 退出登录那一片），否则这一栏就只剩导航了
    expect(screen.getByRole('button', { name: /账号/ })).toBeInTheDocument()
  })

  it('agent 语义的入口一个都没有', () => {
    mountShell()
    for (const label of AGENT_ENTRIES) {
      // 「概览」与「任务中心」是知识库那一组的子项，**不在**这份禁令里（它们在上一用例里被断言存在）
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
