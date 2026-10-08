/**
 * 知识库元数据快照那一族接口（M4 阶段 5，`src/api/kbCache.ts`）。
 *
 * 这一份钉三件事——它们都是"界面上看不见、错了会静默"的：
 *
 * 1. **视图指纹逐字**：`docListViewKey()` 必须与后端 `services/kb_cache.py::doc_list_scope_key()`
 *    给出**同一个串**。不一致的表现不是报错，而是"每次读都像没有副本"——页面永远先摆骨架屏，
 *    验收要的"秒开"无声无息地没了；
 * 2. **请求形状逐条**：五条读的路径与参数、`revalidate` 那一条的请求体
 *    （后端 `KbCacheRevalidateIn` 是 `extra="forbid"`，多一个键就是 422）；
 * 3. **404 静默跳过**：服务器档（浏览器 / NAS 网页端）里这一族根本不存在，
 *    页面照旧走实时读；而且**记一笔之后就不再问**（不然每次进知识库页都白打一条）。
 *
 * 网络一律替身（`fetch` + 壳的 IPC）：这些用例一条真请求都不该发出去。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearKbCache,
  DEFAULT_DOC_PAGE_SIZE,
  docListViewKey,
  getKbCacheDocument,
  getKbCacheDocuments,
  getKbCacheFolders,
  getKbCacheKnowledgeBase,
  getKbCacheKnowledgeBases,
  getKbCacheStats,
  resetKbCacheSupport,
  revalidateKbCache,
} from '@/api/kbCache'
import { resetSidecarProbe, setLocalDataForTest } from '@/api/sidecar'

const SHELL_BASE = 'http://127.0.0.1:8766'
const KB_CACHE = `${SHELL_BASE}/api/v1/local/kb-cache`

function json(payload: unknown, code = 200): Response {
  return new Response(JSON.stringify(payload), {
    status: code,
    headers: { 'content-type': 'application/json' },
  })
}

/** 一份"有内容"的快照（形状对着后端 `KbCacheSnapshotOut` 抄）。 */
function snapshot(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    available: true,
    resource: 'kb_list',
    scope_key: '',
    reason: '',
    items: [{ id: 'kb_1', name: '论文' }],
    payload: { items: [{ id: 'kb_1', name: '论文' }] },
    version: 'sha256:abc',
    source: 'reader',
    fetched_at: '2026-10-05T09:00:00Z',
    checked_at: '2026-10-05T09:00:00Z',
    stale: false,
    last_error: '',
    revalidating: false,
    ...overrides,
  }
}

/** 打出去的那些请求（用例只看方法与 URL 与请求体）。 */
let calls: string[] = []
/** 预备好的回答（取空之后再被问到 = 用例写错了，直接炸）。 */
let answers: Array<Response | (() => Response)> = []

function stubNetwork(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const target = String(url)
      calls.push(`${init?.method ?? 'GET'} ${target}${init?.body ? ` ${init.body}` : ''}`)
      if (target.endsWith('/health')) return json({ ok: true })
      if (!target.includes('/local/kb-cache')) throw new Error(`用例没预备这条请求：${target}`)
      const next = answers.shift()
      if (!next) throw new Error(`用例没预备这条回答：${target}`)
      return typeof next === 'function' ? next() : next
    }),
  )
}

/** 只看这一族的那几条（`/health` 那几趟是基址判定的副作用，不看）。 */
function kbCacheCalls(): string[] {
  return calls.filter((call) => call.includes('/local/kb-cache'))
}

beforeEach(() => {
  calls = []
  answers = []
  resetSidecarProbe()
  resetKbCacheSupport()
  setLocalDataForTest(undefined)
  vi.stubGlobal('__TAURI__', {
    core: { invoke: vi.fn(async () => ({ port: 8766, base: SHELL_BASE })) },
  })
  stubNetwork()
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetSidecarProbe()
  resetKbCacheSupport()
})

describe('① 视图指纹与后端逐字一致', () => {
  it('四档取值：整个库 / 某个目录 / 未归档 / 带页码', () => {
    // 与后端 `doc_list_scope_key()` 的拼接逐字相同（分隔符也是同一个 `|`）
    expect(docListViewKey('kb_1', { page: 1, size: 20 })).toBe('kb_1|folder:all|page:1|size:20')
    expect(docListViewKey('kb_1', { folder: 'f_9', page: 2, size: 50 })).toBe(
      'kb_1|folder:f_9|page:2|size:50',
    )
    expect(docListViewKey('kb_1', { root: true, page: 1, size: 1 })).toBe(
      'kb_1|folder:root|page:1|size:1',
    )
    expect(docListViewKey('kb_1', { root: true, folder: 'f_9', page: 3, size: 20 })).toBe(
      'kb_1|folder:f_9|page:3|size:20',
    )
  })

  it('不给页码时用后端文档列表的默认（page=1 / size=50）', () => {
    expect(docListViewKey('kb_1')).toBe(`kb_1|folder:all|page:1|size:${DEFAULT_DOC_PAGE_SIZE}`)
  })
})

describe('② 请求形状逐条钉', () => {
  it('库列表与库详情', async () => {
    answers = [json(snapshot()), json(snapshot({ resource: 'kb_detail', scope_key: 'kb_1' }))]
    await getKbCacheKnowledgeBases()
    await getKbCacheKnowledgeBase('kb_1')
    expect(kbCacheCalls()).toEqual([
      `GET ${KB_CACHE}/knowledge-bases`,
      `GET ${KB_CACHE}/knowledge-bases/kb_1`,
    ])
  })

  it('文档列表：目录 / 未归档两档的参数，页码与页大小一律显式发', async () => {
    answers = [json(snapshot({ resource: 'doc_list' })), json(snapshot({ resource: 'doc_list' }))]
    await getKbCacheDocuments('kb_1', { folder: 'f_9', page: 2, size: 20 })
    await getKbCacheDocuments('kb_1', { root: true, page: 1, size: 1 })
    expect(kbCacheCalls()).toEqual([
      `GET ${KB_CACHE}/knowledge-bases/kb_1/documents?folder=f_9&page=2&size=20`,
      `GET ${KB_CACHE}/knowledge-bases/kb_1/documents?root=true&page=1&size=1`,
    ])
  })

  it('目录与文档条目', async () => {
    answers = [
      json(snapshot({ resource: 'folders', scope_key: 'kb_1' })),
      json(snapshot({ resource: 'document', scope_key: 'doc_9' })),
    ]
    await getKbCacheFolders('kb_1')
    await getKbCacheDocument('doc_9')
    expect(kbCacheCalls()).toEqual([
      `GET ${KB_CACHE}/knowledge-bases/kb_1/folders`,
      `GET ${KB_CACHE}/documents/doc_9`,
    ])
  })

  it('再确认：POST 一条，请求体是**恒定形状**（后端 extra=forbid，多一个键就 422）', async () => {
    answers = [
      json(snapshot({ resource: 'doc_list', scope_key: 'kb_1|folder:f_9|page:2|size:20' })),
    ]
    await revalidateKbCache({
      resource: 'doc_list',
      kb_id: 'kb_1',
      folder: 'f_9',
      page: 2,
      size: 20,
    })
    expect(kbCacheCalls()).toEqual([
      `POST ${KB_CACHE}/revalidate ` +
        JSON.stringify({
          resource: 'doc_list',
          kb_id: 'kb_1',
          document_id: '',
          folder: 'f_9',
          // `folder` 与 `root` 互斥：两个都给的话后端会判"说不清清的是哪一片"
          root: false,
          page: 2,
          size: 20,
        }),
    ])
  })

  it('再确认：库列表那一份只要一个资源名', async () => {
    answers = [json(snapshot())]
    await revalidateKbCache({ resource: 'kb_list' })
    expect(kbCacheCalls()).toEqual([
      `POST ${KB_CACHE}/revalidate ` +
        JSON.stringify({
          resource: 'kb_list',
          kb_id: '',
          document_id: '',
          folder: '',
          root: false,
          page: 1,
          size: DEFAULT_DOC_PAGE_SIZE,
        }),
    ])
  })
})

describe('③ 404 与其它失败都静默（这一族读从不抛）', () => {
  it('404 → 回 null（这一档没有本机后端），而且**之后不再问**', async () => {
    answers = [json({ detail: 'Not Found' }, 404)]
    expect(await getKbCacheKnowledgeBases()).toBeNull()
    expect(kbCacheCalls()).toHaveLength(1)

    // 记过一笔之后：连请求都不发（服务器档每次进知识库页不该白打一条必然失败的）
    expect(await getKbCacheKnowledgeBases()).toBeNull()
    expect(await getKbCacheDocuments('kb_1', { page: 1, size: 20 })).toBeNull()
    expect(kbCacheCalls()).toHaveLength(1)
  })

  it('500 → 也回 null（"顺手快一点"的失败不在界面上说话；报错的位置在实时那条线）', async () => {
    answers = [json({ detail: 'boom' }, 500)]
    expect(await getKbCacheKnowledgeBases()).toBeNull()
    // 这一档**有**这个端点，只是这次没成：下一次还该再问
    answers = [json(snapshot())]
    expect(await getKbCacheKnowledgeBases()).not.toBeNull()
    expect(kbCacheCalls()).toHaveLength(2)
  })

  it('available:false 是一个正常答案（不是错误）：原样回给调用方', async () => {
    answers = [
      json(snapshot({ available: false, items: [], payload: null, reason: '这台还没看过它' })),
    ]
    const body = await getKbCacheKnowledgeBases()
    expect(body?.available).toBe(false)
    expect(body?.reason).toBe('这台还没看过它')
  })

  it('带筛选的那一读：后端如实回"不留副本"，界面按"没有"处理（不当错误）', async () => {
    answers = [
      json(
        snapshot({
          available: false,
          items: [],
          payload: null,
          scope_key: '',
          reason: '这个筛选条件下的内容不留副本',
        }),
      ),
    ]
    const body = await getKbCacheDocuments('kb_1', { folder: 'f_9', page: 1, size: 20 })
    expect(body?.available).toBe(false)
  })

  it('形状不认识（后端比界面旧/新）→ 回 null，不让界面去读 undefined', async () => {
    answers = [json({ ok: true })]
    expect(await getKbCacheKnowledgeBases()).toBeNull()
  })
})

describe('④ 用量读数与清除（M4 阶段 6：设置面板那一块）', () => {
  it('读数：GET 一条，形状照后端 `KbCacheStatsOut`', async () => {
    answers = [
      json({
        rows: 12,
        payload_bytes: 4096,
        oldest_fetched_at: '2026-10-01T08:00:00Z',
        newest_fetched_at: '2026-10-01T09:00:00Z',
      }),
    ]
    const stats = await getKbCacheStats()
    expect(stats?.rows).toBe(12)
    expect(stats?.newest_fetched_at).toBe('2026-10-01T09:00:00Z')
    expect(kbCacheCalls()).toEqual([`GET ${KB_CACHE}/stats`])
  })

  it('读数读不到就回 null（界面据此写"读不到"，不静默摆一个 0）', async () => {
    answers = [json({ detail: 'boom' }, 500)]
    expect(await getKbCacheStats()).toBeNull()
    // 形状不认识也算读不到（`rows` 不在 = 后端比界面旧）
    answers = [json({ items: [] })]
    expect(await getKbCacheStats()).toBeNull()
  })

  it('清除：DELETE 一条（**全清**），回清掉了几项', async () => {
    answers = [json({ removed: 7 })]
    expect(await clearKbCache()).toBe(7)
    expect(kbCacheCalls()).toEqual([`DELETE ${KB_CACHE}`])
  })

  it('清除失败**要抛**（用户明确点的动作，静默失败会被读成"清干净了"）', async () => {
    answers = [json({ detail: '本机后端没起来' }, 503)]
    await expect(clearKbCache()).rejects.toThrow()
  })

  /**
   * **复位之后，晚到的 404 不许把"这一档没有这一族"记回来**（2026-10-02 收口那轮加的闸门，
   * 与 `api/provider.ts` / `api/backup.ts` / `api/sidecar.ts` 的那几位**同形**）。
   *
   * 为什么值得一条用例：`readQuiet` 开头那句 `if (unsupported) return null` 会让**后面所有**
   * 读一次请求都不发。所以这一笔一旦被上一条用例的晚到 404 写回来，表现就是"某一条用例
   * 悄悄不读快照了"——而那与"本机后端没这一族"长得一模一样，红的时候看不出与谁有关。
   */
  it('复位之后：上一条那笔晚到的 404 不许把 unsupported 记回来（后面的读照旧发请求）', async () => {
    let open = false
    let openGate: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      openGate = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const target = String(url)
        // 探活那一趟照旧放行（不然这条链走不到 kb-cache 那一步）
        if (target.endsWith('/health')) return json({ ok: true })
        if (!target.includes('/local/kb-cache')) throw new Error(`用例没预备：${target}`)
        if (!open) await gate
        return json({ message: 'Not Found' }, 404)
      }),
    )
    const cacheCalls = (): number =>
      vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/local/kb-cache')).length

    const pending = getKbCacheKnowledgeBases()
    await new Promise((resolve) => setTimeout(resolve, 0))

    resetKbCacheSupport() // 下一条用例的常态
    open = true
    openGate()
    await pending
    await new Promise((resolve) => setTimeout(resolve, 0))

    // 复位之后必须**真的再打一次**（而不是被上一条那笔 404 记成"这一档没有"）
    const before = cacheCalls()
    await getKbCacheKnowledgeBases()
    expect(cacheCalls()).toBe(before + 1)
  })
})
