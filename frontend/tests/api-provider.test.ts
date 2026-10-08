/**
 * 知识库提供者的状态层（M3 阶段 6，`src/api/provider.ts`）。
 *
 * 这一份钉五件事——它们都是"界面上看不见、但错了会处处不对"的：
 *
 * 1. **三态迁移与"未探过不猜"**：`unconfigured → unavailable → ready` 每一次结论都落在
 *    模块状态上；而**还没探过时 `status` 必须是 `null`**（界面按缺席渲染，
 *    不许先编一个"不可用"再改口）；
 * 2. **TTL 内不重探 / `refresh()` 强制**：不 refresh 的那一条读的是服务端那份 30s 缓存
 *    （所以**不能**次次都带 `refresh=1`，那会把 NAS 往返摊到每次切页上）；点了「测试连接」
 *    或窗口重新获得焦点时必须**真的重探**（带 `refresh=1`）；
 * 3. **轮询只在没连上时**（`ready` 时不轮询：没有"恢复了要能自己回来"这件事可做）；
 * 4. **保存（PATCH）把回来的状态立刻写进模块**：侧栏那一组靠这一次广播当场显隐；
 * 5. **这一档有没有这个概念**：服务器档（浏览器 / NAS 网页端）里 `/local/provider`
 *    是 404 —— 那时**不按本机档显隐**（那一档知识库就是它自己）。
 *
 * 网络一律替身（`fetch` + 壳的 IPC）：这些用例一条真请求都不该发出去。
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  PROVIDER_POLL_MS,
  PROVIDER_TTL_MS,
  getLocalProvider,
  loadProviderStatus,
  patchLocalProvider,
  providerGateApplies,
  providerView,
  refresh,
  resetProviderStore,
  setProviderStatusForTest,
  useKnowledgeProviderStatus,
  type ProviderStatus,
} from '@/api/provider'
import { resetSidecarProbe, setLocalDataForTest } from '@/api/sidecar'

const SHELL_BASE = 'http://127.0.0.1:8766'
const PROVIDER_URL = `${SHELL_BASE}/api/v1/local/provider`

function status(overrides: Partial<ProviderStatus> = {}): ProviderStatus {
  return {
    state: 'ready',
    available: true,
    reason: '',
    checked_at: '2026-10-03T10:00:00Z',
    base_url: SHELL_BASE,
    credential: 'configured',
    protocol_version: 1,
    app_version: '0.1.1',
    capabilities: { ingest: { max_bytes: 1024, extensions: ['pdf'] } },
    caller: { kind: 'api_key' },
    knowledge_bases: [{ id: 'kb_1', name: '论文', document_count: 12, can_write: true }],
    ...overrides,
  }
}

function json(payload: unknown, code = 200): Response {
  return new Response(JSON.stringify(payload), {
    status: code,
    headers: { 'content-type': 'application/json' },
  })
}

/** 预备给 `/local/provider` 的回答队列（取空之后再被问到 = 用例写错了，直接炸）。 */
let answers: Array<Response | (() => Response)> = []
/** 打出去的那些请求（用例只看 URL）。 */
let calls: string[] = []

/** 网络替身：`/health` 一律 200（壳里那台活着），提供者那两条按队列回答。 */
function stubNetwork(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const target = String(url)
      calls.push(`${init?.method ?? 'GET'} ${target}`)
      if (target.endsWith('/health')) return json({ ok: true })
      if (!target.includes('/local/provider')) throw new Error(`用例没预备这条请求：${target}`)
      const next = answers.shift()
      if (!next) throw new Error(`用例没预备这条回答：${target}`)
      return typeof next === 'function' ? next() : next
    }),
  )
}

/** 只有提供者那一条（`/health` 那几趟是基址判定的副作用，不看）。 */
function providerCalls(): string[] {
  return calls.filter((call) => call.includes('/local/provider'))
}

beforeEach(() => {
  calls = []
  answers = []
  resetSidecarProbe()
  resetProviderStore()
  setLocalDataForTest(undefined)
  vi.stubGlobal('__TAURI__', {
    core: { invoke: vi.fn(async () => ({ port: 8766, base: SHELL_BASE })) },
  })
  stubNetwork()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  resetSidecarProbe()
  resetProviderStore()
})

describe('① 三态与"未探过不猜"', () => {
  it('还没探过：status 是 null、settled 是假（界面按缺席渲染，不先编一个"不可用"）', () => {
    expect(providerView().status).toBeNull()
    expect(providerView().settled).toBe(false)
    expect(providerView().ready).toBe(false)
    // 壳里 = 本机档：还没有结论时按**缺席**处理（导航里不闪那一组）
    expect(providerView().gate).toBe(true)
    expect(providerView().blocked).toBe(true)
  })

  it('没有壳、也没有过结论 → 不算本机档（浏览器 / NAS 网页端的知识库就是它自己）', () => {
    delete (globalThis as { __TAURI__?: unknown }).__TAURI__

    expect(providerGateApplies()).toBe(false)
    expect(providerView().blocked).toBe(false)
  })

  it('unconfigured → unavailable → ready 三次迁移都落在模块状态上', async () => {
    answers.push(
      json(status({ state: 'unconfigured', available: false, reason: '还没接提供者' })),
      json(status({ state: 'unavailable', available: false, reason: '连不上' })),
      json(status()),
    )

    await refresh()
    expect(providerView().state).toBe('unconfigured')
    expect(providerView().blocked).toBe(true)

    await refresh()
    expect(providerView().state).toBe('unavailable')
    expect(providerView().reason).toBe('连不上')

    await refresh()
    expect(providerView().ready).toBe(true)
    expect(providerView().blocked).toBe(false)
    // 库清单随握手回来（不必再单发一次列表）
    expect(providerView().knowledgeBases[0]?.name).toBe('论文')
  })

  it('不 refresh 的那一次读的是服务端缓存：请求里**没有** refresh=1', async () => {
    answers.push(json(status()))

    await loadProviderStatus()

    expect(providerCalls()).toHaveLength(1)
    expect(providerCalls()[0]).not.toContain('refresh')
  })

  it('响应形状不认识时当"读不到"处理（不把版本错配变成崩溃）', async () => {
    answers.push(json({ ok: true }))

    await loadProviderStatus()

    expect(providerView().status).toBeNull()
    expect(providerView().error).toContain('响应不认识')
  })
})

describe('② TTL 内不重探 / refresh 强制', () => {
  it('TTL 内连着问两次只打一个请求（省的是每次切页的 NAS 往返）', async () => {
    answers.push(json(status()))

    await loadProviderStatus()
    await loadProviderStatus()

    expect(providerCalls()).toHaveLength(1)
  })

  it(`TTL（${PROVIDER_TTL_MS / 1000}s）过了之后才肯重探一次`, async () => {
    vi.useFakeTimers()
    answers.push(json(status()), json(status()))

    await loadProviderStatus()
    await vi.advanceTimersByTimeAsync(PROVIDER_TTL_MS + 1_000)
    await loadProviderStatus()

    expect(providerCalls()).toHaveLength(2)
    // 两次都是"读缓存"那一档：过期之后重问一次，但仍然不强制重探
    expect(providerCalls()[1]).not.toContain('refresh')
  })

  it('refresh() 一定带 refresh=1（缓存里那份旧结论不算数）', async () => {
    answers.push(json(status()), json(status({ state: 'unavailable', available: false })))

    await loadProviderStatus()
    await refresh()

    expect(providerCalls()).toHaveLength(2)
    expect(providerCalls()[1]).toContain('refresh=1')
    expect(providerView().state).toBe('unavailable')
  })

  it('单飞：同一次重探并发发起也只打一个请求', async () => {
    answers.push(json(status()))

    await Promise.all([refresh(), refresh(), refresh()])

    expect(providerCalls()).toHaveLength(1)
  })
})

describe('③ 轮询与焦点重验', () => {
  it('没连上时每 30s 轮询一次（只为"恢复了要能自己回来"）', async () => {
    vi.useFakeTimers()
    answers.push(
      json(status({ state: 'unavailable', available: false, reason: '连不上' })),
      json(status()),
    )
    const view = renderHook(() => useKnowledgeProviderStatus())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(providerCalls()).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROVIDER_POLL_MS)
    })

    expect(providerCalls()).toHaveLength(2)
    expect(providerCalls()[1]).toContain('refresh=1')
    expect(view.result.current.ready).toBe(true)
    view.unmount()
  })

  it('ready 时**不轮询**（状态是好的，没有可"恢复"的事）', async () => {
    vi.useFakeTimers()
    answers.push(json(status()))
    const view = renderHook(() => useKnowledgeProviderStatus())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(providerCalls()).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROVIDER_POLL_MS * 3)
    })

    expect(providerCalls()).toHaveLength(1)
    view.unmount()
  })

  it('窗口重新获得焦点 → revalidate-on-focus（refresh=1）', async () => {
    answers.push(json(status()), json(status()))
    const view = renderHook(() => useKnowledgeProviderStatus())
    await waitFor(() => expect(providerCalls()).toHaveLength(1))

    await act(async () => {
      window.dispatchEvent(new Event('focus'))
    })

    await waitFor(() => expect(providerCalls()).toHaveLength(2))
    expect(providerCalls()[1]).toContain('refresh=1')
    view.unmount()
  })

  it('enabled=false 时不订阅也不探（顶栏那条非本机档"一次都不探"靠它）', async () => {
    const view = renderHook(() => useKnowledgeProviderStatus({ enabled: false }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(providerCalls()).toHaveLength(0)
    view.unmount()
  })

  it('最后一个订阅者走了就把计时器摘掉（卸载之后不再打接口）', async () => {
    vi.useFakeTimers()
    answers.push(json(status({ state: 'unavailable', available: false })))
    const view = renderHook(() => useKnowledgeProviderStatus())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    view.unmount()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROVIDER_POLL_MS * 2)
    })

    expect(providerCalls()).toHaveLength(1)
  })
})

describe('④ 保存（PATCH）当场改状态', () => {
  it('PATCH 的 body 只有白名单那两键，回来的状态立刻写进模块（订阅者当场看到）', async () => {
    answers.push(json(status({ base_url: 'http://other-nas:8000/api/v1' })))
    setProviderStatusForTest(status({ state: 'unavailable', available: false, reason: '旧结论' }))
    /** 订阅者每次重渲染都记一笔：广播必须发生（侧栏那一组就靠它显隐）。 */
    const seen: string[] = []
    const view = renderHook(() => {
      const current = useKnowledgeProviderStatus()
      seen.push(current.state)
      return current
    })
    const before = providerCalls().length

    await act(async () => {
      await patchLocalProvider({ base_url: 'http://other-nas:8000/api/v1' })
    })

    expect(providerCalls()[before]).toBe(`PATCH ${PROVIDER_URL}`)
    expect(providerView().ready).toBe(true)
    expect(providerView().status?.base_url).toBe('http://other-nas:8000/api/v1')
    // 广播到了订阅者（新状态出现在它拿到的那一份里）
    expect(seen).toContain('ready')
    view.unmount()
  })

  it('恢复默认：base_url 空串照发（空 = 清掉覆盖、回继承）', async () => {
    answers.push(json(status({ base_url: '' })))
    const fetchMock = vi.mocked(fetch)
    await patchLocalProvider({ base_url: '' })

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ base_url: '' })
  })

  it('关掉提供者（enabled=false）也走同一条 PATCH，结论立刻变成"没配"', async () => {
    answers.push(json(status({ state: 'unconfigured', available: false, reason: '被关掉了' })))
    const fetchMock = vi.mocked(fetch)
    const next = await patchLocalProvider({ enabled: false })

    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ enabled: false })
    expect(next.state).toBe('unconfigured')
    // 结论当场生效：导航那一组该消失（blocked = 本机档且没连上）
    expect(providerView().blocked).toBe(true)
  })
})

describe('⑤ 这一档有没有"提供者"这个概念', () => {
  it('404（服务器档 / NAS 网页端）→ 不按提供者状态显隐：知识库就是它自己', async () => {
    answers.push(json({ message: 'Not Found' }, 404))

    await loadProviderStatus()

    expect(providerView().status).toBeNull()
    expect(providerView().settled).toBe(true)
    expect(providerGateApplies()).toBe(false)
    expect(providerView().gate).toBe(false)
    // 但错误本身**如实留着**（顶栏那一行要能说清"读不到"）
    expect(providerView().error).not.toBe('')
  })

  it('本机档真答过一次之后，一次读失败也不改上一次的结论（不闪）', async () => {
    answers.push(json(status()))
    await loadProviderStatus()
    expect(providerGateApplies()).toBe(true)

    // 下一次读失败（这一份用例没预备回答 = 网络那一层炸了）
    await refresh()

    expect(providerView().ready).toBe(true)
    expect(providerView().error).not.toBe('')
    expect(providerGateApplies()).toBe(true)
  })

  it('getLocalProvider 是纯请求：不写模块状态', async () => {
    answers.push(json(status()))
    const payload = await getLocalProvider(true)

    expect(payload.available).toBe(true)
    expect(providerView().status).toBeNull()
  })
})

/* ------------------- 复位之后，晚到的结论不许写进来（`generation`） ------------------- */

/**
 * 与 `api/backup.ts` 的 `generation` 同形、同一条理由：`requestLocal()` 的第一次 `fetch`
 * 不在调用点上（要先问壳 + 探一次活），所以"那一刻发出去的读"可能**在复位之后**才回来。
 * 水位（`token`/`applied`）拦不住它——复位把 `applied` 清成 0，旧请求的 `token` 反而更大。
 *
 * 这一条是 2026-10-02 那次前端全量抖动的**确定性版本**（门禁上时而红"知识库连接那一节"、
 * 时而红"连不上时显示原因"，单跑必绿）：把那条读攥在手里，复位 + 注入之后才放行——
 * 拿到的结论（这里故意给真实边车那种 `ready`）**一个字都不许写进状态**。
 */
describe('⑨ 复位之后，晚到的结论不许写进状态（generation）', () => {
  it('复位 + 注入"浏览器档"之后，旧请求的 ready 结论不许把 gate 翻回真', async () => {
    let open = false
    let openGate: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      openGate = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const target = String(url)
        if (target.endsWith('/health')) {
          if (!open) await gate
          return json({ ok: true })
        }
        // 真实边车那种"有提供者"的答案
        return json({ state: 'ready', available: true, reason: '', base_url: 'http://nas:8000' })
      }),
    )
    const pending = loadProviderStatus()
    await new Promise((resolve) => setTimeout(resolve, 0))

    resetProviderStore()
    setProviderStatusForTest(null, { unsupported: true }) // 浏览器档：这一档不按提供者显隐
    open = true
    openGate()
    await pending.catch(() => undefined)
    for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))

    expect(providerGateApplies()).toBe(false)
    expect(providerView().status).toBeNull()
    // 注入那份的"已安顿"也在（说明没人把状态整个换掉）
    expect(providerView().settled).toBe(true)
  })
})
