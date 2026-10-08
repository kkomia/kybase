/**
 * 本机档的**导入账**（M2 阶段 6）：`api/local.ts` 那三个字段与两句人话。
 *
 * 这一份钉两件事：
 *
 * 1. **`/local/status` 打的是本机那台**（不是服务器）：`/local` 在本机权威面的前缀表里，
 *    而这三笔账只有本机档有——打错地方的后果是 404，用户看到的是"没有导入这回事"；
 * 2. **摘要文字把三笔账都说到**：`imports`（最近一批的状态与新建数）、
 *    `unfinished_imports`（没跑完的批次——R1 那条最要紧）、
 *    `unimported_file_references`（未随导入的文件引用——R4 那条）。
 *    文字抽成纯函数正是为了在这里逐字钉（写在组件里就只能靠渲染快照去验）。
 *
 * 没导过（`imports` 为空）也要说得清：**"还没导过"不是"导了 0 条"**。
 *
 * ## ③ 那一节是 2026-10-05（NAS 网页端退役）加的
 *
 * "这一份界面**有没有**本机后端"从那天起不只是一个数据选址问题，它决定**哪几页存在**
 * （服务器档不再挂会话面，见 `backend/app/api/v1/router.py` 的模块头）。
 * 所以判据本身要逐档钉住，尤其是那两条最容易被写错的分支：
 * **404 才算"没有"**（探不通不算）、**壳里恒真有且一次都不问**。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  batchStateLabel,
  getLocalStatus,
  importAccountsText,
  localBackendView,
  probeLocalBackend,
  resetLocalBackendForTest,
  type LocalStatus,
} from '@/api/local'
import { resetSidecarProbe, setLocalDataForTest } from '@/api/sidecar'

const SHELL_BASE = 'http://127.0.0.1:8766'
/** 浏览器开发形态那份构建期常量（壳问不到时的兜底，见 `sidecar.ts`）。 */
const BUILDTIME_BASE = 'http://127.0.0.1:8765'

function status(overrides: Partial<LocalStatus> = {}): LocalStatus {
  return {
    deployment: 'local',
    data_dir: 'C:\\Users\\me\\AppData\\Roaming\\com.kylab.desktop',
    database: 'C:\\Users\\me\\AppData\\Roaming\\com.kylab.desktop\\kylab.db',
    database_exists: true,
    database_bytes: 1024,
    database_wal_bytes: 0,
    server_url: 'http://nas:8000/api/v1',
    imports: [],
    unfinished_imports: 0,
    unimported_file_references: 0,
    note: '会话 / 消息 / 事件 / 产物 / 笔记 / 设置 / 工作区落本机 SQLite',
    ...overrides,
  }
}

beforeEach(() => {
  resetSidecarProbe()
  setLocalDataForTest(undefined)
  resetLocalBackendForTest()
  vi.stubGlobal('__TAURI__', {
    core: { invoke: vi.fn(async () => ({ port: 8766, base: SHELL_BASE })) },
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetSidecarProbe()
  setLocalDataForTest(undefined)
  resetLocalBackendForTest()
})

describe('① 读的是本机那台', () => {
  it('打 壳里那个基址 的 /api/v1/local/status，并把三笔账解析出来', async () => {
    const payload = status({
      imports: [
        {
          batch_id: 'imp_1',
          state: 'done',
          source: 'http://nas:8000/api/v1',
          counts: { created: 302, skipped: 0 },
          error: '',
          updated_at: '2026-10-01T10:10:04.807000Z',
        },
      ],
      unfinished_imports: 0,
      unimported_file_references: 128,
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(JSON.stringify(url.endsWith('/health') ? { ok: true } : payload), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
      ),
    )

    const result = await getLocalStatus()

    expect(result.imports[0]?.batch_id).toBe('imp_1')
    expect(result.unfinished_imports).toBe(0)
    expect(result.unimported_file_references).toBe(128)
    const calls = vi.mocked(fetch).mock.calls.map((call) => String(call[0]))
    expect(calls).toContain(`${SHELL_BASE}/health`)
    expect(calls).toContain(`${SHELL_BASE}/api/v1/local/status`)
    // **一次都不许**打到服务器那条链（这三笔账只在本机档存在）
    expect(calls.some((url) => url.includes('/api/v1/local/status') && !url.includes('8766'))).toBe(
      false,
    )
  })

  it('响应形状不认识时抛错（界面比边车新：宁可说"读不到"，不要崩在 undefined 上）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(JSON.stringify(url.endsWith('/health') ? { ok: true } : { ok: true }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
      ),
    )

    await expect(getLocalStatus()).rejects.toThrow('本机状态响应不认识')
  })
})

describe('② 摘要文字：三笔账都要说到', () => {
  it('导过一批：状态、新建数、没跑完的、未随导入的', () => {
    const text = importAccountsText(
      status({
        imports: [
          {
            batch_id: 'imp_1',
            state: 'done',
            source: 'http://nas:8000/api/v1',
            counts: { created: 302, skipped: 0 },
            error: '',
            updated_at: null,
          },
        ],
        unfinished_imports: 1,
        unimported_file_references: 128,
      }),
    )

    expect(text).toContain('导入 1 批')
    expect(text).toContain('最近一批已完成')
    expect(text).toContain('新建 302 / 跳过 0')
    expect(text).toContain('1 批没跑完')
    expect(text).toContain('128 个文件引用没随导入')
  })

  it('没导过：说"还没导过"，不说"导了 0 条"', () => {
    const text = importAccountsText(status())

    expect(text).toContain('还没导过旧会话')
    expect(text).toContain('没有没跑完的批次')
    expect(text).toContain('没有未随导入的文件引用')
    expect(text).not.toContain('导入 0 批')
  })

  it('失败与回滚都有中文名，认不出来的状态原样写出来（后端加了新状态界面不该崩）', () => {
    expect(batchStateLabel('failed')).toBe('失败')
    expect(batchStateLabel('rolled_back')).toBe('已回滚')
    expect(batchStateLabel('paused')).toBe('paused')
  })

  it('保留字面量的批次状态不被翻译成"已完成"（`planned` / `running` 是**没跑完**那两档）', () => {
    const text = importAccountsText(
      status({
        imports: [
          {
            batch_id: 'imp_2',
            state: 'running',
            source: 'http://nas:8000/api/v1',
            counts: { created: 12 },
            error: '',
            updated_at: null,
          },
        ],
        unfinished_imports: 1,
      }),
    )

    expect(text).toContain('最近一批进行中')
    expect(text).toContain('1 批没跑完')
  })
})

/* ==================================================== ③ 这一档有没有本机后端 */

/** 没有壳（浏览器 / NAS 网页端）：把 `__TAURI__` 摘掉即可（`inDesktopShell` 读的就是它）。 */
function withoutShell(): void {
  vi.stubGlobal('__TAURI__', undefined)
}

/**
 * 浏览器形态下的网络替身：`/health` 一律连不上（本机后端不在这一档），
 * 而 `/api/v1/local/status` 由调用方给一条**真响应**（服务器档是 404）。
 *
 * 为什么要这么像真的：这条判据的输入就是那两个 HTTP 结论（`resolveLocalBase` 那三支），
 * 拿假的返回值去喂它等于把要验的东西绕过去。
 */
function browserNetwork(answer: () => Response): string[] {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(String(url))
      if (String(url).includes('/health')) throw new TypeError('本机后端不在这一档')
      return answer()
    }),
  )
  return calls
}

function notFound(): Response {
  return new Response(JSON.stringify({ code: 'not_found', message: 'Not Found' }), {
    status: 404,
    headers: { 'content-type': 'application/json' },
  })
}

describe('③ 这一档有没有本机后端（NAS 网页端退役之后，它决定会话面那几页在不在）', () => {
  it('壳里：恒真有，而且**一次都不问**（判据是同步的，主产品形态不许先闪一帧"没有"）', async () => {
    const fetchMock = vi.fn(async () => notFound())
    vi.stubGlobal('fetch', fetchMock)

    expect(localBackendView()).toEqual({ present: true, settled: true })

    await probeLocalBackend()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(localBackendView()).toEqual({ present: true, settled: true })
  })

  it('浏览器里答 404（服务器档没有这条端点）→ **没有**本机后端，会话面那几页不该存在', async () => {
    withoutShell()
    const calls = browserNetwork(notFound)

    expect(localBackendView().settled).toBe(false)
    await probeLocalBackend()

    expect(localBackendView()).toEqual({ present: false, settled: true })
    // 走的是"问壳没有壳 → 退回构建期常量 → 那条链探不通 → 打服务器"这一支：
    // 真正下结论的那条请求落在**服务器**的 `/api/v1/local/status` 上
    expect(calls).toContain(`${BUILDTIME_BASE}/health`)
    expect(calls).toContain('/api/v1/local/status')
    expect(
      calls.some((url) => url.startsWith(BUILDTIME_BASE) && url.includes('/local/status')),
    ).toBe(false)
  })

  it('浏览器直连本机后端（开发形态）→ 答出 `deployment: local`，算**有**', async () => {
    withoutShell()
    browserNetwork(
      () =>
        new Response(JSON.stringify(status()), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    )

    await probeLocalBackend()

    expect(localBackendView()).toEqual({ present: true, settled: true })
  })

  it('**探不通不等于没有**：连不上（不是 404）时按"不拆界面"处理', async () => {
    withoutShell()
    // /health 与 /local/status 都连不上 → 一次网络抖动
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )

    await probeLocalBackend()

    expect(localBackendView()).toEqual({ present: true, settled: true })
  })

  it('后端答了却说自己不是本机档 → 也算"没有"（那一档的数据面在服务器上）', async () => {
    withoutShell()
    browserNetwork(
      () =>
        new Response(JSON.stringify(status({ deployment: 'server' })), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    )

    await probeLocalBackend()

    expect(localBackendView()).toEqual({ present: false, settled: true })
  })

  it('还没探过时不拆界面（宁可按完整产品渲染，等那一条结论回来）', () => {
    withoutShell()

    expect(localBackendView()).toEqual({ present: true, settled: false })
  })

  it('并发探只打一趟（切页、开面板不该各问一次）', async () => {
    withoutShell()
    const calls = browserNetwork(notFound)

    await Promise.all([probeLocalBackend(), probeLocalBackend(), probeLocalBackend()])

    expect(calls.filter((url) => url.includes('/local/status')).length).toBe(1)
  })

  it('显式关掉本机数据面（`VITE_LOCAL_DATA=0`）→ 没有（那一档读的就是服务器上的数据）', () => {
    setLocalDataForTest(false)

    expect(localBackendView()).toEqual({ present: false, settled: true })
  })
})
