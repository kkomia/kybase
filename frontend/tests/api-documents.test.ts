/**
 * 文档接口（`api/documents.ts`）——**从旧 Vue 版 `tests/unit/api/documents.test.ts` 整份搬来的**
 * （实现同一份代码，只把会话令牌的 import 路径换到 `@/lib/session`）。
 *
 * 新前端的其它用例都把这层 mock 掉了，这一份是**真发请求、真解析响应**的那条链路。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  RENDERABLE_KINDS,
  batchDocuments,
  getDocumentPreview,
  listDocuments,
} from '@/api/documents'

function ok(): Response {
  return new Response(
    JSON.stringify({ kind: 'markdown', filename: 'a.md', original_kind: 'pdf' }),
    {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    },
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

/**
 * fetch 假实现。
 *
 * 用 `vi.fn<签名>()` 而不是给实现加参数：泛型只影响 `mock.calls` 的类型
 * （不然元素是空元组，读不到 URL），实现本身不必声明用不到的形参——
 * 声明了反而会被 `no-unused-vars` 拦下。
 */
function fetchStub() {
  return vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () => ok())
}

describe('getDocumentPreview', () => {
  it('默认不带 source：由后端按"有解析产物就给解析文本"决定', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)

    await getDocumentPreview('doc_1')

    const url = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(url).toContain('/documents/doc_1/preview')
    expect(url).not.toContain('source=')
  })

  it('source=original 时把参数带上（用户明确要看原件版式）', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)

    await getDocumentPreview('doc_1', 'original')

    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('source=original')
  })
})

describe('RENDERABLE_KINDS', () => {
  it('只含能在这页里画出来的原件类型，不含 markdown / binary', () => {
    expect([...RENDERABLE_KINDS].sort()).toEqual(['docx', 'excel', 'image', 'pdf', 'pptx'])
    expect(RENDERABLE_KINDS).not.toContain('binary')
    expect(RENDERABLE_KINDS).not.toContain('markdown')
  })
})

describe('listDocuments', () => {
  it('分页参数下推给后端：limit / offset 都带上', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)

    await listDocuments('kb_1', { limit: 50, offset: 100 })

    const url = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(url).toContain('/knowledge-bases/kb_1/documents')
    expect(url).toContain('limit=50')
    expect(url).toContain('offset=100')
  })

  it('不给分页参数时不拼 limit / offset：由后端用默认值', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)

    await listDocuments('kb_1', { q: '合同' })

    const url = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(url).toContain('q=')
    expect(url).not.toContain('limit=')
    expect(url).not.toContain('offset=')
  })
})

describe('batchDocuments', () => {
  it('把动作与目标 id 原样送出（含新的 questions 动作）', async () => {
    const fetchMock = fetchStub()
    vi.stubGlobal('fetch', fetchMock)

    await batchDocuments('kb_1', 'questions', ['doc_1', 'doc_2'])

    const [url, init] = fetchMock.mock.calls.at(-1) ?? []
    expect(String(url)).toContain('/knowledge-bases/kb_1/documents/batch')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      action: 'questions',
      document_ids: ['doc_1', 'doc_2'],
    })
  })
})
