/**
 * 「按页」视角（`DocumentPages`，挂在文档抽屉的「阅读」档下）的用例。
 *
 * 钉住四件事（都对着后端 `GET /documents/{id}/pages` 的契约）：
 * 1. **默认档与默认范围**：原件是 PDF 时默认看页图，范围是"整份"（这里 12 页）；
 * 2. **两种档各取各的**：图片档 `images=true`、文字档 `images=false`，页图用相对路径
 *    的签名链接直接 `<img src>`；
 * 3. **如实的两处空态**：`page_text_available=false` 是"没有页码信息"（不铺一叠空页），
 *    `image_url=null` 的那一页给一句空态（不挂破图）；
 * 4. **输入夹在单次上限内**（后端超了回 422，不该让用户白等一趟）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentDrawer } from '@/features/knowledge'
import type { DocumentPages as DocumentPagesResult, DocumentSummary } from '@/api/documents'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  getDocument: vi.fn(),
  listDocumentChunks: vi.fn(),
  getDocumentPreview: vi.fn(),
  getDocumentTimeline: vi.fn(),
  getDocumentPages: vi.fn(),
  downloadDocument: vi.fn(),
}))

import {
  getDocument,
  getDocumentPages,
  getDocumentPreview,
  listDocumentChunks,
} from '@/api/documents'

const docMock = vi.mocked(getDocument)
const chunksMock = vi.mocked(listDocumentChunks)
const previewMock = vi.mocked(getDocumentPreview)
const pagesMock = vi.mocked(getDocumentPages)

function makeDoc(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: 'doc-1',
    knowledge_base_id: 'kb-1',
    name: '说明书.pdf',
    source_kind: 'upload',
    stage: 'indexed',
    size_bytes: 4096,
    mime_type: 'application/pdf',
    page_count: 12,
    is_split: false,
    error: null,
    chunk_count: 137,
    uploaded_by: null,
    uploaded_by_name: '小又',
    folder_id: null,
    disabled: false,
    original_kind: 'pdf',
    created_at: null,
    updated_at: '2026-09-20T10:00:00Z',
    question_count: 0,
    questioned_chunk_count: 0,
    questions_pending: false,
    summary: '',
    progress: null,
    ...overrides,
  }
}

/** 一页页图/文字：`image_url` 与后端给的一样是**相对路径**的签名链接。 */
function page(number: number, overrides: Partial<DocumentPagesResult['pages'][number]> = {}) {
  return {
    page: number,
    text: `第 ${number} 页的解析文字`,
    image_url: `/api/v1/documents/doc-1/pages/${number}/image?expires=1800000000&signature=sig`,
    image_expires_at: 1_800_000_000,
    image_kind: 'pdf',
    ...overrides,
  }
}

function makePages(overrides: Partial<DocumentPagesResult> = {}): DocumentPagesResult {
  return {
    document_id: 'doc-1',
    from_page: 1,
    to_page: 2,
    total_pages: 12,
    page_text_available: true,
    truncated: false,
    pages: [page(1), page(2)],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  docMock.mockResolvedValue(makeDoc())
  chunksMock.mockResolvedValue({ items: [], total: 137 })
  previewMock.mockResolvedValue({
    kind: 'pdf',
    filename: '说明书.pdf',
    text: null,
    url: '/api/v1/documents/doc-1/content?sig=abc',
    expires_at: 1_800_000_000,
    original_kind: 'pdf',
  })
  pagesMock.mockResolvedValue(makePages())
})

afterEach(() => {
  vi.clearAllMocks()
})

function renderDrawer() {
  return render(
    <MemoryRouter>
      <DocumentDrawer documentId="doc-1" onClose={vi.fn()} />
    </MemoryRouter>,
  )
}

/** 打开「阅读」里的「按页」档（抽屉进来默认落在「原文版式」上）。 */
async function openPages(user: ReturnType<typeof userEvent.setup>) {
  renderDrawer()
  await screen.findByText('说明书.pdf')
  await user.click(screen.getByRole('tab', { name: '按页' }))
}

describe('按页看原文', () => {
  it('原件是 PDF 时默认看页图，范围默认整份（总页数已知就一次看全）', async () => {
    const user = userEvent.setup()
    await openPages(user)

    await waitFor(() =>
      expect(pagesMock).toHaveBeenCalledWith('doc-1', {
        from_page: 1,
        to_page: 12,
        images: true,
      }),
    )
    // 页码竖排：一页一个标题，页图吃的是后端给的相对路径签名链接
    expect(await screen.findByText('第 1 页')).toBeInTheDocument()
    expect(screen.getByText('第 2 页')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '第 1 页' })).toHaveAttribute(
      'src',
      '/api/v1/documents/doc-1/pages/1/image?expires=1800000000&signature=sig',
    )
    expect(screen.getByText('文档共 12 页')).toBeInTheDocument()
  })

  it('切到「文字」档改按解析文字取（images=false），一页一段正文', async () => {
    const user = userEvent.setup()
    await openPages(user)
    await waitFor(() => expect(pagesMock).toHaveBeenCalledTimes(1))

    await user.click(screen.getByRole('tab', { name: '文字' }))

    await waitFor(() =>
      expect(pagesMock).toHaveBeenLastCalledWith('doc-1', {
        from_page: 1,
        to_page: 12,
        images: false,
      }),
    )
    expect(await screen.findByText('第 1 页的解析文字')).toBeInTheDocument()
    expect(screen.getByText('第 2 页的解析文字')).toBeInTheDocument()
    // 文字档不要页图：那一档的请求不带签名，别让浏览器去取一堆用不上的图
    expect(screen.queryByRole('img')).toBeNull()
  })

  it('改起止页并点「查看」：只取那一段', async () => {
    const user = userEvent.setup()
    await openPages(user)
    await waitFor(() => expect(pagesMock).toHaveBeenCalledTimes(1))

    await user.clear(screen.getByLabelText('起'))
    await user.type(screen.getByLabelText('起'), '3')
    await user.clear(screen.getByLabelText('止'))
    await user.type(screen.getByLabelText('止'), '5')
    await user.click(screen.getByRole('button', { name: '查看' }))

    await waitFor(() =>
      expect(pagesMock).toHaveBeenLastCalledWith('doc-1', {
        from_page: 3,
        to_page: 5,
        images: true,
      }),
    )
  })

  it('单次跨度夹在 50 页以内：输入 3–99 时实际只取 3–52，并把夹过的值写回输入框', async () => {
    const user = userEvent.setup()
    await openPages(user)
    await waitFor(() => expect(pagesMock).toHaveBeenCalledTimes(1))

    await user.clear(screen.getByLabelText('起'))
    await user.type(screen.getByLabelText('起'), '3')
    await user.clear(screen.getByLabelText('止'))
    await user.type(screen.getByLabelText('止'), '99')
    await user.click(screen.getByRole('button', { name: '查看' }))

    await waitFor(() =>
      expect(pagesMock).toHaveBeenLastCalledWith('doc-1', {
        from_page: 3,
        to_page: 52,
        images: true,
      }),
    )
    // 显示的与请求的必须是同一段（夹完写回），否则用户以为自己在看 99
    expect(screen.getByLabelText('止')).toHaveValue('52')
    expect(screen.getByText('单次最多 50 页')).toBeInTheDocument()
  })

  it('没有页码信息：如实说一句并指向图片档，不铺一叠空页', async () => {
    pagesMock.mockResolvedValue(
      makePages({
        page_text_available: false,
        pages: [page(1, { text: '' }), page(2, { text: '' })],
      }),
    )
    const user = userEvent.setup()
    await openPages(user)
    await waitFor(() => expect(pagesMock).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('tab', { name: '文字' }))

    expect(await screen.findByText('这份文档没有页码信息。')).toBeInTheDocument()
    expect(screen.getByText('切到「图片」档可以看原件。')).toBeInTheDocument()
    // "没有页码信息"不等于"文档是空的"：一页都不该铺出来
    expect(screen.queryByText('第 1 页')).toBeNull()
  })

  it('范围超出总页数：说清超出部分没有内容，空出来的那页给空态而不是破图', async () => {
    pagesMock.mockResolvedValue(
      makePages({
        from_page: 12,
        to_page: 13,
        truncated: true,
        pages: [
          page(12),
          // 超出总页数的那一页：后端不会给它签图（`image_*` 三个字段一起是 null）
          page(13, { image_url: null, image_expires_at: null, image_kind: null, text: '' }),
        ],
      }),
    )
    const user = userEvent.setup()
    await openPages(user)

    expect(await screen.findByText('超出的部分没有内容。')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '第 12 页' })).toBeInTheDocument()
    expect(screen.getByText('这一页没有原图。')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: '第 13 页' })).toBeNull()
  })

  it('取不到就说清为什么，并给「重试」就地重取这一段', async () => {
    pagesMock.mockRejectedValueOnce(new Error('服务内部错误'))
    const user = userEvent.setup()
    await openPages(user)

    expect(await screen.findByText(/预览失败（服务内部错误）/)).toBeInTheDocument()

    pagesMock.mockResolvedValue(makePages())
    await user.click(screen.getByRole('button', { name: '重试' }))

    expect(await screen.findByText('第 1 页')).toBeInTheDocument()
    expect(screen.queryByText(/预览失败/)).toBeNull()
  })

  it('原件不是逐页的就没有「按页」这一档：报了个页数也不算（Markdown 解析产物里没有页码）', async () => {
    // `page_count` 是探测阶段给的，Office / Markdown 来源也可能报一个页数——
    // 但它们的解析产物里没有页码信息，页图也签不出来，所以不能拿页数当"有页可看"
    docMock.mockResolvedValue(
      makeDoc({ original_kind: 'markdown', page_count: 12, chunk_count: 137 }),
    )
    renderDrawer()
    await screen.findByText('说明书.pdf')

    // 此时「阅读」里连来源档都不给（原件不可渲染），照旧默认看解析文本
    expect(screen.queryByRole('tab', { name: '按页' })).toBeNull()
    expect(previewMock).toHaveBeenCalledWith('doc-1', 'auto')
    expect(pagesMock).not.toHaveBeenCalled()
  })
})
