/**
 * 身份恢复的**三态**（D07，2026-09-28 走查）。
 *
 * 病灶：`restoreSession` 原先是个布尔，`catch` 一律 `clearSessionToken()` ——
 * **网络抖动 / 超时 / 后端 5xx** 与"登录真过期"被当成同一件事：用户被强制登出，
 * 而且本地令牌被删掉（网好了还得重新输密码）。而 `api/client.ts` 其实早就给错误
 * 标了 `status`（`error.status = response.status`），没人看它。
 *
 * 这三条用例把三种失败**钉成三种结果**，其中"网络失败要留下令牌"那条是核心：
 * 它反着写（`clearSessionToken()` 无条件调用）就会红。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { me } from '@/api/auth'
import { clearSessionToken, sessionToken, setSessionToken } from '@/lib/session'
import { restoreSession } from '@/lib/sessionActions'

vi.mock('@/api/auth', () => ({
  me: vi.fn(),
  // 其余导出在这一份用例里用不到，但模块是整体 mock 的，留空函数免得 import 时炸
  getAuthBootstrapStatus: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  setup: vi.fn(),
  changePassword: vi.fn(),
  uploadAvatar: vi.fn(),
  clearAvatar: vi.fn(),
}))

/** 造一个"像 `client.ts` 抛出来的那种"错误：带 `status`。 */
function httpError(status: number): Error & { status: number } {
  const error = new Error(`请求失败（HTTP ${status}）`) as Error & { status: number }
  error.status = status
  return error
}

const ACCOUNT = { id: 'u1', username: 'kkomia', name: 'kkomia', is_admin: true }

beforeEach(() => {
  vi.clearAllMocks()
  clearSessionToken()
  setSessionToken('tok-1')
})

describe('restoreSession 的三态（D07）', () => {
  it('拿到了账号 → ok，令牌留着', async () => {
    vi.mocked(me).mockResolvedValue(ACCOUNT as never)
    await expect(restoreSession()).resolves.toBe('ok')
    expect(sessionToken()).toBe('tok-1')
  })

  it('401 → expired，并且清令牌（这才是真过期）', async () => {
    vi.mocked(me).mockRejectedValue(httpError(401))
    await expect(restoreSession()).resolves.toBe('expired')
    expect(sessionToken()).toBe('')
  })

  it('网络不通（fetch 直接抛 TypeError）→ unreachable，**令牌必须留着**', async () => {
    // 这一条是 D07 的核心：客户端连不上时抛的是 TypeError，**没有 status**
    vi.mocked(me).mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(restoreSession()).resolves.toBe('unreachable')
    expect(sessionToken()).toBe('tok-1')
  })

  it('后端 5xx → unreachable，令牌同样留着', async () => {
    vi.mocked(me).mockRejectedValue(httpError(503))
    await expect(restoreSession()).resolves.toBe('unreachable')
    expect(sessionToken()).toBe('tok-1')
  })

  it('本来就没有令牌 → expired（没什么可恢复的，也不必清）', async () => {
    clearSessionToken()
    await expect(restoreSession()).resolves.toBe('expired')
    expect(sessionToken()).toBe('')
  })
})
