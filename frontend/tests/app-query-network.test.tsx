/**
 * 全站查询的**网络策略**：`app/App.tsx` 里那份 `QueryClient` 的默认值。
 *
 * 钉住的是一类**静默挂起**（2026-10-04）。react-query 的默认 `networkMode` 是 `'online'`，
 * 看的是 `navigator.onLine`：浏览器/系统一旦自认为没有外网，查询就停在 `paused`——
 * 那时 `isLoading` 与 `isError` **同为 false**，界面既不报错也不加载，看着就是
 * "一直没有数据"（`data ?? []` 的写法还会把它画成一个 0）。
 *
 * 而这个前端主要跟**本机后端**说话（`/api` 反代到 127.0.0.1:8000，桌面壳里是
 * 127.0.0.1:8100）：**"有没有外网"与"本机后端在不在"根本不是一回事**，断网时本机后端
 * 照常在跑。所以查询与写入都必须照发，成败交给响应说话。
 *
 * 三条用例：读照发（不停在 `paused`）、写照发（`mutate` 不挂起）、以及策略本身就有
 * `networkMode: 'always'`——前两条证明行为，第三条让"改回默认值"立刻变红。
 *
 * 这里读的是壳真正挂给 `QueryClientProvider` 的那一个 `queryClient`（同一个绑定），
 * 不是另建一份同样配置的客户端：后者在"有人把策略改回去"时会照样绿。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { onlineManager, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { queryClient } from '@/app/App'

/**
 * 一条**把自己的状态念出来**的读：`status/fetchStatus/data`。
 *
 * 为什么把 `fetchStatus` 一起念：这条用例的病根正是"界面上看不出差别"（`paused` 时
 * `status` 仍是 `pending`）——念出来，"被挂起"与"还在路上"才分得开。
 */
function ReadProbe({ read }: { read: () => Promise<string> }) {
  const { data, status, fetchStatus } = useQuery({
    queryKey: ['app-network', 'read'],
    queryFn: read,
  })
  return <span data-testid="read-probe">{`${status}/${fetchStatus}/${data ?? ''}`}</span>
}

/** 一条写入：点一下走一次 `mutate`。 */
function WriteProbe({ write }: { write: () => Promise<string> }) {
  const { mutate } = useMutation({ mutationFn: write })
  return (
    <button type="button" onClick={() => mutate()}>
      写一笔
    </button>
  )
}

describe('全站查询的网络策略（浏览器报离线时也不停摆）', () => {
  beforeEach(() => {
    // 缓存串味是这类用例最常见的假绿：上一条的结论会让这一条根本不打请求
    queryClient.clear()
  })

  afterEach(() => {
    // `onlineManager` 是模块级单例：别的用例要的是正常的在线态（见 `misc-capabilities`）
    onlineManager.setOnline(true)
  })

  it('浏览器报离线时读照样发出去，窗口里读得成功（不是 paused）', async () => {
    const read = vi.fn(async () => '本机后端在')
    onlineManager.setOnline(false)

    render(
      <QueryClientProvider client={queryClient}>
        <ReadProbe read={read} />
      </QueryClientProvider>,
    )

    // 默认策略下这一句就红：查询停在 `paused`，`queryFn` 一次都不跑
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('read-probe')).toHaveTextContent('success/idle/本机后端在')
  })

  it('浏览器报离线时写入照样发出去，不是挂在那儿等联网', async () => {
    const write = vi.fn(async () => '写成功')
    onlineManager.setOnline(false)

    render(
      <QueryClientProvider client={queryClient}>
        <WriteProbe write={write} />
      </QueryClientProvider>,
    )

    await userEvent.click(screen.getByRole('button', { name: '写一笔' }))

    // 默认策略下这一句同样红：`mutate` 被挂起，promise 不落地、界面既不成功也不报错
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
  })

  it('策略本身就写在默认值上：读与写都是 always（改回默认值即红）', () => {
    const defaults = queryClient.getDefaultOptions()
    expect(defaults.queries?.networkMode).toBe('always')
    expect(defaults.mutations?.networkMode).toBe('always')
  })
})
