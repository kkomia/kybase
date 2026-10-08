/**
 * 知识库页面的路由守卫（M3 阶段 6，`features/knowledge/ProviderRoute.tsx`）。
 *
 * 方案 §3.4「断连后消失」要求的是：**正停在 KB 页面（或直接敲地址进来）时**，
 * 不可用要变成"回到对话页 + 一条含原因的 toast"，而不是白屏、也不是一个点进去报错的死页面。
 * 这一份就把那三句话钉住：
 *
 * 1. `ready` → 页面照常渲染（守卫一个字都不加）；
 * 2. 不可用**且本机没有**上次看到的内容 → **重定向到 `/chat`**，而且 toast 里**必须有
 *    后端给的原因**，还要说清"本机也没留上次看到的内容"（"不静默"这条纪律的全部价值
 *    就在那几句话上：没有它，用户只知道坏了，不知道坏在哪、还能不能看到旧的那一份）；
 * 3. 不可用**但本机留着**上次看到的内容 → **放行**（M4 阶段 6 / 决策点 D-A）：页顶一条
 *    说明里**原因与"上次看到的是什么时候"都在**，而且**不发** toast、不重定向——
 *    "离线快照"要看得见，就得让人进得去这扇门；
 * 4. **还没结论**（首屏那一次握手还没回来）→ 先等一下，不把一条正常的深链接弹回去；
 * 5. 服务器档（浏览器 / NAS 网页端）→ 守卫**不管**（那一档知识库就是它自己）。
 */
import { useEffect } from 'react'

import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

// 这一族是"本机有没有上次看到的那一份"的判据（D-A 分支）：替身，用例逐档摆答案
vi.mock('@/api/kbCache', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/kbCache')>()),
  getKbCacheKnowledgeBases: vi.fn(),
}))

import { toast } from 'sonner'

import { getKbCacheKnowledgeBases, resetKbCacheSupport, type KbCacheSnapshot } from '@/api/kbCache'
import { resetProviderStore, setProviderStatusForTest, type ProviderStatus } from '@/api/provider'
import { ProviderRoute, providerBlockedMessage } from '@/features/knowledge/ProviderRoute'
import { offlineSnapshotNote } from '@/features/knowledge/snapshot'

const warning = vi.mocked(toast.warning)
const kbCacheListMock = vi.mocked(getKbCacheKnowledgeBases)

/** 那份内容是什么时候看到的：**取"5 分钟前"**，说明里那个 X 就逐字可钉。 */
const FETCHED_AT = new Date(Date.now() - 5 * 60 * 1000).toISOString()
const KEPT_AT_TEXT = '5 分钟前'

/** 一份"本机留着"的库列表快照（只用到 `available` 与 `fetched_at`）。 */
function snapshot(overrides: Partial<KbCacheSnapshot> = {}): KbCacheSnapshot {
  return {
    available: true,
    resource: 'kb_list',
    scope_key: '',
    reason: '',
    items: [{ id: 'kb_1', name: '论文' }],
    payload: { items: [{ id: 'kb_1', name: '论文' }] },
    version: 'sha256:abc',
    source: 'reader',
    fetched_at: FETCHED_AT,
    checked_at: FETCHED_AT,
    stale: false,
    last_error: '',
    revalidating: false,
    ...overrides,
  }
}

function status(overrides: Partial<ProviderStatus> = {}): ProviderStatus {
  return {
    state: 'ready',
    available: true,
    reason: '',
    checked_at: '2026-10-03T10:00:00Z',
    base_url: 'http://nas:8000/api/v1',
    credential: 'configured',
    ...overrides,
  }
}

/** 让用例能读到"现在在哪个地址"（重定向要断言它）。 */
function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

function renderGuarded(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/chat" element={<div>对话页</div>} />
        <Route
          path="/knowledge-bases"
          element={
            <ProviderRoute>
              <div>知识库列表页</div>
            </ProviderRoute>
          }
        />
        <Route
          path="/kb/:kbId"
          element={
            <ProviderRoute>
              <div>知识库详情页</div>
            </ProviderRoute>
          }
        />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  warning.mockClear()
  resetProviderStore()
  resetKbCacheSupport()
  // 默认：本机**没有**留那份内容（第三/四条分支按原样走"重定向 + toast"）
  kbCacheListMock.mockReset()
  kbCacheListMock.mockResolvedValue(null)
  // 壳替身（这一档是本机档）＋一个**永不回答**的 fetch：挂载时那一次探测挂在半路，
  // 于是"还没结论"那一档是可复现的（用例自己摆结论，不许网络参与）。
  vi.stubGlobal('__TAURI__', {
    core: { invoke: vi.fn(async () => ({ port: 8766, base: 'http://127.0.0.1:8766' })) },
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise(() => {})),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetProviderStore()
})

describe('提供者不可用时：知识库那四条路由走不去', () => {
  it('`unavailable`：重定向到 /chat，toast 里带后端那句原因', async () => {
    setProviderStatusForTest(
      status({ state: 'unavailable', available: false, reason: '连不上这台 NAS（3 秒超时）' }),
    )

    renderGuarded('/kb/kb_1')

    await waitFor(() => expect(screen.getByText('对话页')).toBeInTheDocument())
    expect(screen.getByTestId('location')).toHaveTextContent('/chat')
    expect(screen.queryByText('知识库详情页')).not.toBeInTheDocument()
    // 不静默：原因必须出现在 toast 里（而且说清本机后端还在）
    const message = String(warning.mock.calls[0]?.[0] ?? '')
    expect(message).toContain('连不上这台 NAS（3 秒超时）')
    expect(message).toContain('本机后端仍在运行')
  })

  it('`unconfigured`：同样重定向，但句子说的是"还没接上"（下一步是填地址）', async () => {
    setProviderStatusForTest(
      status({ state: 'unconfigured', available: false, reason: '壳里的 server 是空的' }),
    )

    renderGuarded('/knowledge-bases')

    await waitFor(() => expect(screen.getByText('对话页')).toBeInTheDocument())
    const message = String(warning.mock.calls[0]?.[0] ?? '')
    expect(message).toContain('还没接上知识库提供者')
    expect(message).toContain('壳里的 server 是空的')
  })

  it('toast 只说一遍（状态每次广播都会重渲染，刷屏会把原因淹掉）', async () => {
    setProviderStatusForTest(status({ state: 'unavailable', available: false, reason: '连不上' }))

    renderGuarded('/knowledge-bases')
    await waitFor(() => expect(warning).toHaveBeenCalledTimes(1))

    // 再来一次广播（轮询拿到同一个结论）：不再多说一遍
    setProviderStatusForTest(status({ state: 'unavailable', available: false, reason: '连不上' }))
    await waitFor(() => expect(warning).toHaveBeenCalledTimes(1))
  })

  it('原因缺失时也不许静默：兜底句子写"没给出原因"，但"回到对话页"那句仍在', () => {
    expect(providerBlockedMessage('unavailable', '')).toContain('本机后端没给出原因')
    expect(providerBlockedMessage('unavailable', '')).toContain('已回到对话页')
  })
})

/* --------------------- 非 ready 但本机留着上次看到的内容（M4 阶段 6 / D-A） --------------------- */

describe('非 ready 且本机留着上次看到的：放行（只读 + 页顶说明）', () => {
  it('有那一份：**不**重定向、页顶说明里原因与"上次看到的是什么时候"都在、不发 toast', async () => {
    const reason = '连不上这台 NAS（3 秒超时）'
    setProviderStatusForTest(status({ state: 'unavailable', available: false, reason }))
    kbCacheListMock.mockResolvedValue(snapshot())

    renderGuarded('/knowledge-bases')

    const note = await screen.findByTestId('provider-offline-note')
    // 三件事都要在：原因（原话）、那份内容是什么时候看到的、以及"只能看"那句
    expect(note.textContent).toContain(reason)
    expect(note.textContent).toContain(KEPT_AT_TEXT)
    expect(note.textContent).toContain('只能看，不能改')
    // 与纯函数逐字对齐（文案只有一个出口）
    expect(note.textContent).toBe(offlineSnapshotNote(reason, FETCHED_AT))
    // 放行：页面在、地址没变成 /chat、**没有**那条"回到对话页"的 toast
    expect(screen.getByText('知识库列表页')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/knowledge-bases')
    expect(screen.queryByText('对话页')).not.toBeInTheDocument()
    expect(warning).not.toHaveBeenCalled()
  })

  it('`unconfigured` 同样放行：说明里说的是"还没接上"那半句（下一步是填地址）', async () => {
    setProviderStatusForTest(
      status({ state: 'unconfigured', available: false, reason: '壳里的 server 是空的' }),
    )
    kbCacheListMock.mockResolvedValue(snapshot())

    renderGuarded('/kb/kb_1')

    const note = await screen.findByTestId('provider-offline-note')
    expect(note.textContent).toContain('壳里的 server 是空的')
    expect(screen.getByText('知识库详情页')).toBeInTheDocument()
    expect(warning).not.toHaveBeenCalled()
  })

  it('本机没有那一份：照旧重定向 + toast，而且那句说清"看不到上次的内容"', async () => {
    setProviderStatusForTest(
      status({ state: 'unavailable', available: false, reason: '连不上这台 NAS（3 秒超时）' }),
    )
    kbCacheListMock.mockResolvedValue(snapshot({ available: false, reason: '还没看过它' }))

    renderGuarded('/knowledge-bases')

    await waitFor(() => expect(screen.getByText('对话页')).toBeInTheDocument())
    expect(screen.queryByTestId('provider-offline-note')).not.toBeInTheDocument()
    const message = String(warning.mock.calls[0]?.[0] ?? '')
    expect(message).toContain('连不上这台 NAS（3 秒超时）')
    expect(message).toContain('本机没留上次看到的内容')
  })

  it('本机后端答不上来（这一族读失败一律静默）：与"本机没有"同一条处置', async () => {
    setProviderStatusForTest(status({ state: 'unavailable', available: false, reason: '连不上' }))
    kbCacheListMock.mockResolvedValue(null)

    renderGuarded('/knowledge-bases')

    await waitFor(() => expect(screen.getByText('对话页')).toBeInTheDocument())
    expect(warning).toHaveBeenCalledTimes(1)
  })
})

/* --------------------- 挂载期那一次"错过广播"（真机阶段 7 抓到的缺口） --------------------- */

/**
 * 在**兄弟组件**的 effect 里把状态播出去：它排在守卫前面，effect 因此先跑
 * （React 按树的完成顺序跑 passive effect），而那正是真机上发生过的时序——
 * 冷启动直接进知识库页时，提供者状态是**别的组件**先探到的（`ProviderBoot` / 顶栏那条）。
 *
 * 这时"这一档算不算本机档"（`providerGateApplies`：浏览器形态下看 `status !== null`）
 * 在守卫**第一次渲染时还是假**，于是它先按"不管"把页面渲染出来；紧接着订阅才挂上……
 * 旧写法在那之后没有任何一次广播会被它听见，守卫就**停在页面上**（不挡、也不给说明），
 * 要等 30 秒轮询或焦点那一次才纠正。物证：真机 `.shots/m4-phase7/`（阶段 7 抓到的）。
 */
function StatusSeeder({ when }: { when: ProviderStatus }) {
  useEffect(() => {
    setProviderStatusForTest(when)
  }, [when])
  return null
}

function guardedWithSeeder(when: ProviderStatus) {
  return render(
    <MemoryRouter initialEntries={['/knowledge-bases']}>
      <Routes>
        <Route path="/chat" element={<div>对话页</div>} />
        <Route
          path="/knowledge-bases"
          element={
            <>
              <StatusSeeder when={when} />
              <ProviderRoute>
                <div>知识库列表页</div>
              </ProviderRoute>
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('状态在「第一次渲染」与「挂上订阅」之间变了：守卫也要跟上', () => {
  beforeEach(() => {
    // 浏览器形态（没有壳）：那一档的"算不算本机档"完全看有没有探到结论
    vi.stubGlobal('__TAURI__', undefined)
  })

  it('不可用 + 本机没有那份内容 → 照旧重定向（不许停在页面上）', async () => {
    const unavailable = status({ state: 'unavailable', available: false, reason: '连不上这台 NAS' })
    kbCacheListMock.mockResolvedValue(null)

    guardedWithSeeder(unavailable)

    await waitFor(() => expect(screen.getByText('对话页')).toBeInTheDocument())
    expect(screen.queryByText('知识库列表页')).not.toBeInTheDocument()
    expect(warning).toHaveBeenCalledTimes(1)
    expect(String(warning.mock.calls[0]?.[0] ?? '')).toContain('连不上这台 NAS')
  })

  it('不可用 + 本机留着那份内容 → 放行并给页顶说明', async () => {
    const unavailable = status({ state: 'unavailable', available: false, reason: '连不上这台 NAS' })
    kbCacheListMock.mockResolvedValue(snapshot())

    guardedWithSeeder(unavailable)

    expect(await screen.findByTestId('provider-offline-note')).toBeInTheDocument()
    expect(screen.getByText('知识库列表页')).toBeInTheDocument()
    expect(warning).not.toHaveBeenCalled()
  })
})

describe('提供者可用 / 这一档不管它时：页面照常', () => {
  it('`ready`：四条路由照常渲染，没有任何 toast', async () => {
    setProviderStatusForTest(status())

    renderGuarded('/kb/kb_1')

    expect(await screen.findByText('知识库详情页')).toBeInTheDocument()
    expect(warning).not.toHaveBeenCalled()
  })

  it('还没结论：先显示"正在确认知识库连接…"，**不**把深链接弹走', async () => {
    // 模块初始态就是"还没探过"（status=null、settled=false）
    renderGuarded('/knowledge-bases')

    expect(await screen.findByTestId('provider-pending')).toBeInTheDocument()
    expect(screen.queryByText('对话页')).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/knowledge-bases')
    expect(warning).not.toHaveBeenCalled()
  })

  it('服务器档（这一档没有 /local/provider）：守卫一个字都不管', async () => {
    setProviderStatusForTest(null, { unsupported: true })

    renderGuarded('/knowledge-bases')

    expect(await screen.findByText('知识库列表页')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/knowledge-bases')
    expect(warning).not.toHaveBeenCalled()
  })
})
