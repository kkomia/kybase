/**
 * misc 域的测试夹具。
 *
 * 放在 `src/features/misc/testing/` 而不是 `tests/` 下：那一层的文件所有权是
 * `tests/misc*.test.tsx`（只允许用例文件），夹具跟着被测代码走更不容易漂。
 *
 * 三件事：
 * 1. **每个用例一份新的 QueryClient**：缓存串味是这类测试最常见的假绿——
 *    上一个用例的 `listTasks` 结果会让下一个用例根本不打请求；
 * 2. `retry: false`：默认的重试会让"错误态"用例多等两轮才断言；
 * 3. **路由用 MemoryRouter**：这些页面都会 `useNavigate` / 读 `?focus=`。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router'

import { ToastStack } from '../shared/toast'

export function createTestClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 30_000, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  })
}

export interface RenderOptions {
  /** 初始地址（`?new=1`、`?focus=` 这类） */
  route?: string
  client?: QueryClient
}

export function renderMisc(ui: ReactElement, options: RenderOptions = {}) {
  const client = options.client ?? createTestClient()
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[options.route ?? '/']}>
        {ui}
        {/* 通知栈由应用壳挂载（这里替它挂上）：页面本身只推通知，不负责渲染它们 */}
        <ToastStack />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...result, client }
}
