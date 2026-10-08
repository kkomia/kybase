/**
 * 会话令牌与 401（`api/client.ts`）——**从旧 Vue 版 `tests/unit/api/client.test.ts` 整份搬来的**
 * （实现同一份代码，只把会话令牌的 import 路径换到 `@/lib/session`）。
 *
 * 新前端的其它用例都把这层 mock 掉了，这一份是**真发请求、真解析响应**的那条链路。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { API_BASE, request, requestLocal, resetSessionProbeForTest } from '@/api/client'
import { resetSidecarProbe, setLocalDataForTest } from '@/api/sidecar'
import { SESSION_TOKEN_STORAGE_KEY, useSessionStore } from '@/lib/session'
import { clearSessionToken, sessionToken, setSessionToken } from '@/lib/session'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('api/client', () => {
  beforeEach(() => {
    window.localStorage.clear()
    clearSessionToken()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('401 清掉会话并请求重新登录（唯一的恢复路径）', async () => {
    // 会话过期是常态（滑动续期到期 / 改密 / 被吊销）。此时该做的就是回登录页。
    setSessionToken('kylab_st_expired')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(401, { code: 'UNAUTHORIZED', message: '会话已失效' })),
    )
    const reloginBefore = useSessionStore.getState().reloginCount

    const failure = await request('/knowledge-bases').catch((error: unknown) => error)

    expect((failure as Error).message).toContain('重新登录')
    expect((failure as Error & { status?: number }).status).toBe(401)
    expect(sessionToken()).toBe('')
    expect(useSessionStore.getState().reloginCount).toBe(reloginBefore + 1)
  })

  it('非 401 的错误保留后端文案，也不触发重新登录', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(409, { code: 'CONFLICT', message: '内容不符' })),
    )
    const before = useSessionStore.getState().reloginCount

    const failure = await request('/knowledge-bases').catch((error: unknown) => error)

    expect((failure as Error).message).toBe('内容不符')
    expect(useSessionStore.getState().reloginCount).toBe(before)
  })

  it('带上会话令牌后会加 Authorization 头', async () => {
    setSessionToken('kylab_st_abc')
    // 泛型给出 fetch 的签名，实现里就不必写用不到的形参（eslint 不许下划线占位）
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async () => jsonResponse(200, { ok: true }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await request('/knowledge-bases')

    const headers = (fetchMock.mock.calls[0]?.[1]?.headers ?? {}) as Record<string, string>
    expect(headers.Authorization).toBe('Bearer kylab_st_abc')
  })

  it('**FormData 请求不自己写 Content-Type**（上传全靠这一条）', async () => {
    // 手写 `application/json` 会让后端解析不出任何字段——实测的表现是
    // `422 {"message": "file: Field required"}`，看着像"请求里没带文件"。
    // 浏览器的规矩：`FormData` 的 Content-Type（带 boundary）只能由它自己写，
    // 而**作者显式设过的那个头它不会覆盖**。
    const calls: RequestInit[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        calls.push(init)
        return jsonResponse(200, { ok: true })
      }),
    )

    const form = new FormData()
    form.append('file', new Blob(['x']), 'a.png')
    await request('/auth/avatar', { method: 'POST', body: form })
    await request('/auth/login', { method: 'POST', body: JSON.stringify({}) })

    const headersOf = (init: RequestInit) => (init.headers ?? {}) as Record<string, string>
    expect(headersOf(calls[0])['Content-Type']).toBeUndefined()
    // JSON 那条路照旧带上（后端也靠它认 body）
    expect(headersOf(calls[1])['Content-Type']).toBe('application/json')
  })

  it('没有令牌时不加 Authorization 头', async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
      async () => jsonResponse(200, { ok: true }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await request('/knowledge-bases')

    const headers = (fetchMock.mock.calls[0]?.[1]?.headers ?? {}) as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })
})

/* ------------------- 401 的两条路（2026-10-02 收窄：401 ≠ 登录过期） ------------------- */

/**
 * 用户踩到的真 bug：壳里点产物「预览」→ 要签名链接 → 服务端自己**没配下载签名密钥**，
 * 回了一个 **401** → 前端把**任何** 401 都当"凭据失效"，直接把人弹到登录页。
 *
 * 那一类 401 与用户的凭据无关（后端正把"服务端没配好"改成 503，但这条"401 = 登出"的
 * 判据本身太宽——同类问题以后还会以别的形式回来），所以这一组钉的是**判据本身**：
 *
 * 1. 业务请求 401 → **先核一次会话**（`/auth/me`）；
 * 2. 会话还在 → **不登出、不清令牌**，把后端那句话原样抛给调用方（预览里就地显示原因）；
 * 3. 会话真失效（`/auth/me` 也 401）→ 照旧清令牌 + 落登录页（**原行为一个字没改**）；
 * 4. 探活问不出结论（网络错 / 5xx）→ 不登出（与 `App.tsx` 的"网络抖动不该把人强制登出"同一句）；
 * 5. **一次 401 风暴只探一趟**（单飞）；`authFailure: 'throw'` 那族（登录 / `/auth/me` 自己）不探。
 */
describe('401：先核会话，只有真失效才登出', () => {
  /** 探活那一趟的地址（"是不是真打了 `/auth/me`"只看它）。 */
  const PROBE = `${API_BASE}/auth/me`

  // 这一组自备一份干净的起点（上面那个 describe 的同名钩子只管它自己那几条用例）：
  // 没有它，"⑤ 手上没有令牌"会吃到上一条用例留下的令牌
  beforeEach(() => {
    window.localStorage.clear()
    clearSessionToken()
    resetSessionProbeForTest()
  })

  /**
   * 一条"**业务请求 401**、而会话探活另有说法"的替身。
   *
   * 业务那半固定回后端在真机上那句话（`尚未配置下载签名密钥…`）——这一组要钉的正是
   * "这句话能不能原样到调用方手里"。
   */
  function stubBusiness401(probe: () => Response | Promise<Response>): string[] {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const target = String(url)
        calls.push(target)
        if (target.endsWith(PROBE)) return probe()
        return jsonResponse(401, {
          code: 'unauthorized',
          message: '尚未配置下载签名密钥，请联系管理员',
        })
      }),
    )
    return calls
  }

  /** 会话还活着时 `/auth/me` 回的那一份（只用到几个字段，形状够断言即可）。 */
  function meOk(): Response {
    return jsonResponse(200, {
      id: 'u1',
      username: 'kkomia',
      name: '管理员',
      role: 'admin',
      avatar_url: '',
    })
  }

  it('① 业务请求 401、会话还在 → **不登出、令牌留着**，错误就是后端那句话', async () => {
    setSessionToken('kylab_st_alive')
    const calls = stubBusiness401(meOk)
    const before = useSessionStore.getState().reloginCount

    // 真机的调用点就是这一族（预览 / 下载换签名链接）
    const failure = (await request('/conversations/c1/files/download-url').catch(
      (error: unknown) => error,
    )) as Error & { status?: number }

    // 后端那句话原样到调用方手里（预览里会显示成"预览失败（尚未配置下载签名密钥，请联系管理员）"）
    expect(failure.message).toBe('尚未配置下载签名密钥，请联系管理员')
    expect(failure.status).toBe(401)
    // **不登出**：令牌留着（内存 + localStorage 两处），也没有请求重新登录
    expect(sessionToken()).toBe('kylab_st_alive')
    expect(window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY)).toBe('kylab_st_alive')
    expect(useSessionStore.getState().reloginCount).toBe(before)
    // 确实核了一次会话，而且核的是**服务器**那条、带上了这把令牌
    expect(calls.filter((call) => call.endsWith(PROBE))).toHaveLength(1)
    const probeCall = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith(PROBE))
    expect((probeCall?.[1]?.headers as Record<string, string>).Authorization).toBe(
      'Bearer kylab_st_alive',
    )
  })

  it('② 会话真的失效（`/auth/me` 也 401）→ 清令牌 + 请求重新登录（**原行为**）', async () => {
    setSessionToken('kylab_st_expired')
    stubBusiness401(() => jsonResponse(401, { code: 'unauthorized', message: '会话已失效' }))
    const before = useSessionStore.getState().reloginCount

    const failure = (await request('/knowledge-bases').catch((error: unknown) => error)) as Error

    expect(failure.message).toBe('登录已过期，请重新登录')
    expect((failure as Error & { status?: number }).status).toBe(401)
    expect(sessionToken()).toBe('')
    expect(window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY)).toBeNull()
    expect(useSessionStore.getState().reloginCount).toBe(before + 1)
  })

  it('③ 探活**连不上**（网络错）→ 不登出、不清令牌，抛原错误', async () => {
    setSessionToken('kylab_st_alive')
    const calls = stubBusiness401(() => {
      throw new TypeError('fetch failed')
    })
    const before = useSessionStore.getState().reloginCount

    const failure = (await request('/knowledge-bases').catch((error: unknown) => error)) as Error

    expect(failure.message).toBe('尚未配置下载签名密钥，请联系管理员')
    expect(sessionToken()).toBe('kylab_st_alive')
    expect(window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY)).toBe('kylab_st_alive')
    expect(useSessionStore.getState().reloginCount).toBe(before)
    expect(calls.filter((call) => call.endsWith(PROBE))).toHaveLength(1)
  })

  it('③ 探活回 **5xx** → 同样不登出（对方自己出错，不是你的登录过期了）', async () => {
    setSessionToken('kylab_st_alive')
    stubBusiness401(() => jsonResponse(500, { code: 'internal_error', message: '炸了' }))
    const before = useSessionStore.getState().reloginCount

    const failure = (await request('/knowledge-bases').catch((error: unknown) => error)) as Error

    expect(failure.message).toBe('尚未配置下载签名密钥，请联系管理员')
    expect(sessionToken()).toBe('kylab_st_alive')
    expect(useSessionStore.getState().reloginCount).toBe(before)
  })

  it('④ **并发 12 个 401 只探一次会话**（单飞；令牌留着、12 条都拿到后端那句话）', async () => {
    setSessionToken('kylab_st_alive')
    const calls = stubBusiness401(async () => {
      // 探活故意慢一拍：让 12 条 401 都落在同一次探活上（真机上就是这么撞上的）
      await new Promise((resolve) => setTimeout(resolve, 10))
      return meOk()
    })
    const before = useSessionStore.getState().reloginCount

    const failures: Error[] = await Promise.all(
      Array.from({ length: 12 }, async (_, index) => {
        try {
          await request(`/knowledge-bases/${index}`)
          return new Error('这一条本该 401，却成功了')
        } catch (error) {
          return error as Error
        }
      }),
    )

    expect(calls.filter((call) => call.endsWith(PROBE))).toHaveLength(1)
    expect(failures.every((error) => error.message === '尚未配置下载签名密钥，请联系管理员')).toBe(
      true,
    )
    expect(sessionToken()).toBe('kylab_st_alive')
    expect(useSessionStore.getState().reloginCount).toBe(before)
  })

  it('⑤ 手上没有令牌时**不做探活**（那就是"没登录"，直接走原路）', async () => {
    const calls = stubBusiness401(meOk)
    const before = useSessionStore.getState().reloginCount

    const failure = (await request('/knowledge-bases').catch((error: unknown) => error)) as Error

    expect(failure.message).toBe('登录已过期，请重新登录')
    expect(calls.filter((call) => call.endsWith(PROBE))).toHaveLength(0)
    expect(useSessionStore.getState().reloginCount).toBe(before + 1)
  })

  it('⑤ `authFailure: throw` 那族（登录自己）**不探活**，行为不变', async () => {
    setSessionToken('kylab_st_alive')
    const calls = stubBusiness401(meOk)
    const before = useSessionStore.getState().reloginCount

    // 登录失败本身也是 401：此刻不能触发"重新登录"（用户本来就在登录页）
    const failure = (await request(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({}) },
      { authFailure: 'throw' },
    ).catch((error: unknown) => error)) as Error & { status?: number }

    expect(failure.message).toBe('尚未配置下载签名密钥，请联系管理员')
    expect(failure.status).toBe(401)
    expect(calls.filter((call) => call.endsWith(PROBE))).toHaveLength(0)
    expect(sessionToken()).toBe('kylab_st_alive')
    expect(useSessionStore.getState().reloginCount).toBe(before)
  })

  it('⑥ 真机那条路（本机档：预览要签名链接打在边车上）也不把人弹走', async () => {
    // 壳里 `/conversations/**` 走边车（`requestLocal`），而边车那条 401 按后端的设计
    // **永远不是"你的会话失效了"**（`api/auth.py::current_caller`：本机档不看 Authorization）。
    setSessionToken('kylab_st_alive')
    resetSidecarProbe()
    vi.stubGlobal('__TAURI__', {
      core: { invoke: vi.fn(async () => ({ port: 8765, base: 'http://127.0.0.1:8765' })) },
    })
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const target = String(url)
        calls.push(target)
        if (target.endsWith('/health')) return jsonResponse(200, { ok: true })
        if (target.includes('/files/download-url')) {
          return jsonResponse(401, {
            code: 'unauthorized',
            message: '尚未配置下载签名密钥，请联系管理员',
          })
        }
        if (target.endsWith(PROBE)) return meOk()
        return jsonResponse(404, { message: '用例没预备这条请求' })
      }),
    )
    const before = useSessionStore.getState().reloginCount

    const failure = (await requestLocal('/conversations/c1/files/download-url').catch(
      (error: unknown) => error,
    )) as Error

    expect(failure.message).toBe('尚未配置下载签名密钥，请联系管理员')
    expect(sessionToken()).toBe('kylab_st_alive')
    expect(window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY)).toBe('kylab_st_alive')
    expect(useSessionStore.getState().reloginCount).toBe(before)
    // 业务那条打的是**边车**，探活打的是**服务器**（凭据的权威在服务器）
    expect(
      calls.some((call) => call.startsWith('http://127.0.0.1:8765/api/v1/conversations/')),
    ).toBe(true)
    expect(calls.filter((call) => call.endsWith(PROBE))).toHaveLength(1)
  })

  afterEach(() => {
    resetSessionProbeForTest()
    resetSidecarProbe()
    setLocalDataForTest(undefined)
  })
})
