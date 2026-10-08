/**
 * 存储维护接口（`api/maintenance.ts`）——**从旧 Vue 版 `tests/unit/api/maintenance.test.ts` 整份搬来的**
 * （实现同一份代码，只把会话令牌的 import 路径换到 `@/lib/session`）。
 *
 * 新前端的其它用例都把这层 mock 掉了，这一份是**真发请求、真解析响应**的那条链路。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { compactStorage, getStorageOverview } from '@/api/maintenance'

/** 造一个返回固定 JSON 的响应。 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const OVERVIEW = {
  file_bytes: 1024,
  data_bytes: 768,
  free_bytes: 256,
  partitions: 2,
  orphans: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('maintenance api', () => {
  it('概览走 GET /maintenance/storage', async () => {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url)
        return jsonResponse(OVERVIEW)
      }),
    )

    const result = await getStorageOverview()

    expect(calls[0]).toContain('/api/v1/maintenance/storage')
    expect(result.free_bytes).toBe(256)
  })

  it('整理走 POST /maintenance/compact', async () => {
    const methods: (string | undefined)[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        methods.push(init?.method)
        return jsonResponse({ ...OVERVIEW, free_bytes: 0, data_bytes: 1024 })
      }),
    )

    const result = await compactStorage()

    // 必须是 POST：GET 语义上"只读"，让一个整理动作走 GET 会被预取/重试意外触发
    expect(methods[0]).toBe('POST')
    expect(result.free_bytes).toBe(0)
  })

  it('后端报错时把 message 原样抛出（界面直接显示）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ code: 'forbidden', message: '存储维护需要管理员身份' }, 403),
      ),
    )

    await expect(getStorageOverview()).rejects.toThrow('存储维护需要管理员身份')
  })
})
