/**
 * 文档详情抽屉（`DocumentDrawer`）的用例。
 *
 * 对应旧用例 `frontend/tests/unit/components/DocumentDrawer.test.ts` 的逻辑型条目：
 * 打开时拉哪三个接口、元信息与切块渲染、两个下载按钮走签名链接、页码落进 PDF 地址、
 * 换文档重新加载、Esc 先滑回去再通知宿主、切块的编辑/禁用/删除。
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentDrawer } from '@/features/knowledge'
import type { DocumentChunk, DocumentSummary } from '@/api/documents'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  getDocument: vi.fn(),
  listDocumentChunks: vi.fn(),
  getDocumentPreview: vi.fn(),
  getDocumentTimeline: vi.fn(),
  downloadDocument: vi.fn(),
  updateChunk: vi.fn(),
  setChunkDisabled: vi.fn(),
  deleteChunk: vi.fn(),
  listDocumentParts: vi.fn(),
}))

import {
  deleteChunk,
  downloadDocument,
  getDocument,
  getDocumentPreview,
  getDocumentTimeline,
  listDocumentChunks,
  setChunkDisabled,
  updateChunk,
} from '@/api/documents'

const docMock = vi.mocked(getDocument)
const chunksMock = vi.mocked(listDocumentChunks)
const previewMock = vi.mocked(getDocumentPreview)
const timelineMock = vi.mocked(getDocumentTimeline)
const downloadMock = vi.mocked(downloadDocument)
const updateChunkMock = vi.mocked(updateChunk)
const disableChunkMock = vi.mocked(setChunkDisabled)
const deleteChunkMock = vi.mocked(deleteChunk)
const successToast = vi.mocked(toast.success)

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
    question_count: 6,
    questioned_chunk_count: 3,
    questions_pending: false,
    summary: '讲了安装与卸载',
    progress: null,
    ...overrides,
  }
}

function makeChunk(overrides: Partial<DocumentChunk> = {}): DocumentChunk {
  return {
    chunk_id: 'chunk-1',
    document_id: 'doc-1',
    ordinal: 0,
    text: '第一块的正文',
    heading_path: '第一章',
    page: 3,
    image_ids: [],
    disabled: false,
    questions: ['怎么安装？'],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  docMock.mockResolvedValue(makeDoc())
  chunksMock.mockResolvedValue({ items: [makeChunk()], total: 137 })
  previewMock.mockResolvedValue({
    kind: 'pdf',
    filename: '说明书.pdf',
    text: null,
    url: '/api/v1/documents/doc-1/content?sig=abc',
    expires_at: 1_800_000_000,
    original_kind: 'pdf',
  })
  timelineMock.mockResolvedValue({
    document_id: 'doc-1',
    status: 'done',
    current_index: 6,
    step_total: 6,
    total_ms: 134_000,
    stalled: false,
    steps: [
      {
        key: 'parse',
        label: '解析内容',
        status: 'done',
        duration_ms: 120_000,
        visits: 1,
        error: null,
      },
      { key: 'chunk', label: '切分', status: 'done', duration_ms: 14_000, visits: 2, error: null },
    ],
  })
})

afterEach(() => {
  vi.clearAllMocks()
})

function renderDrawer(props: Partial<Parameters<typeof DocumentDrawer>[0]> = {}) {
  return render(
    <MemoryRouter>
      <DocumentDrawer documentId="doc-1" onClose={vi.fn()} {...props} />
    </MemoryRouter>,
  )
}

describe('抽屉的基本信息与预览', () => {
  it('打开时把元信息、切块与阅读视角三个接口一次拉齐，并按分区渲染', async () => {
    renderDrawer()

    expect(await screen.findByText('说明书.pdf')).toBeInTheDocument()
    expect(docMock).toHaveBeenCalledWith('doc-1')
    expect(chunksMock).toHaveBeenCalledWith('doc-1', 5)
    // 能渲染原件时默认看原件（用户打开一份 PDF 想看的首先是那个文件本身）
    expect(previewMock).toHaveBeenCalledWith('doc-1', 'original')

    expect(screen.getByText('基本信息')).toBeInTheDocument()
    expect(screen.getByText('4.0 KB')).toBeInTheDocument()
    expect(screen.getByText('137')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('共 137 个切块')).toBeInTheDocument()
    expect(screen.getByText('讲了安装与卸载')).toBeInTheDocument()
  })

  it('「来源」写中文而不是裸的 source_kind；「上传者」是另一个字段，两者各写各的', async () => {
    renderDrawer()

    await screen.findByText('说明书.pdf')
    // 这一格以前直接把 `upload` 摆出来（列表的筛选下拉同一份取值写作"本地上传"）
    expect(await screen.findByText('本地上传')).toBeInTheDocument()
    expect(screen.queryByText('upload')).toBeNull()
    // 上传者与来源是两个字段：`uploaded_by_name`（谁传的）vs `source_kind`（怎么进来的）
    expect(screen.getByText('小又')).toBeInTheDocument()
  })

  it('两个下载按钮的文字不一样，且都走"先签发链接再下载"那条路', async () => {
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('button', { name: /下载原文/ }))
    await waitFor(() => expect(downloadMock).toHaveBeenCalledWith('doc-1', 'original'))

    await user.click(screen.getByRole('button', { name: /下载 Markdown/ }))
    await waitFor(() => expect(downloadMock).toHaveBeenCalledWith('doc-1', 'markdown'))
  })

  it('外部传入的页码直接落进 PDF 地址（引用抽屉走这条，那条路径上没有 ?page=）', async () => {
    renderDrawer({ page: 7 })
    await screen.findByText('说明书.pdf')

    const frame = await screen.findByTitle('说明书.pdf')
    expect(frame).toHaveAttribute('src', '/api/v1/documents/doc-1/content?sig=abc#page=7')
  })

  it('没有可渲染的原件时回落到解析文本，并且不给「原文版式」的假入口', async () => {
    docMock.mockResolvedValue(makeDoc({ original_kind: 'markdown' }))
    previewMock.mockResolvedValue({
      kind: 'markdown',
      filename: '笔记.md',
      text: '# 标题\n\n正文',
      url: null,
      expires_at: null,
      original_kind: 'markdown',
    })
    renderDrawer()

    // 这一等要跨**两次 mock 的异步链**（文档详情 → 原文预览），是本用例唯一的等待。
    // 默认 1s 的 `findBy*` 在单跑时富余（实测 515ms），**全量并发下会擦线**
    // （实测这条用例整条 1444ms ✗ ≈ 1s 等待超时 + 渲染）。只放宽这一处，
    // 并写明理由：**不动** vitest 的 `testTimeout`（那是掩盖，不是修）。
    expect(await screen.findByText('标题', {}, { timeout: 3000, interval: 50 })).toBeInTheDocument()
    expect(previewMock).toHaveBeenCalledWith('doc-1', 'auto')
    expect(screen.queryByRole('tab', { name: '原文版式' })).not.toBeInTheDocument()
  })

  it('阅读视角接口失败：说清为什么并给重试，而不是留一块空白', async () => {
    previewMock.mockRejectedValueOnce(new Error('服务内部错误'))
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')

    // 旧实现把这里 catch 成 `null`：整块空白、连"正在加载原文"都收了（评审 52b 号图）
    expect(await screen.findByText(/预览失败（服务内部错误）/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    // 下载入口在工具条上，失败态里不重复放一枚（规范：主操作不重复）
    expect(screen.queryByRole('button', { name: '下载原文' })).toBeInTheDocument()

    // 重试就地重取这一份：通了就把阅读区画出来
    previewMock.mockResolvedValue({
      kind: 'markdown',
      filename: '说明书.pdf',
      text: '# 回来了',
      url: null,
      expires_at: null,
      original_kind: 'pdf',
    })
    await user.click(screen.getByRole('button', { name: '重试' }))

    expect(await screen.findByText('回来了', { selector: 'h1' })).toBeInTheDocument()
    expect(screen.queryByText(/预览失败/)).toBeNull()
  })

  it('切块视角：说清"只显示前 5 块"、列出分段问题，并给出补出题的入口位置', async () => {
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('tab', { name: '切块' }))

    expect(screen.getByText('该文档共 137 块，这里只显示前 1 块。')).toBeInTheDocument()
    // 配比斜杠两侧带空格、出题数用「题」（与列表那一列同一个单位词）：全站一种写法
    expect(screen.getByText(/已为 3 \/ 137 段出题，共 6 题/)).toBeInTheDocument()
    expect(screen.getByText('第一块的正文')).toBeInTheDocument()
    expect(screen.getByText('怎么安装？')).toBeInTheDocument()
    expect(screen.getByText(/第 1 块/)).toBeInTheDocument()
    expect(screen.getByText(/第一章/)).toBeInTheDocument()
  })

  it('处理明细页签才去拉时间线（另外两个页签的读者不关心这些数）', async () => {
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')
    expect(timelineMock).not.toHaveBeenCalled()

    await user.click(screen.getByRole('tab', { name: '处理明细' }))

    await waitFor(() => expect(timelineMock).toHaveBeenCalledWith('doc-1'))
    expect(await screen.findByText(/共 6 个环节 · 当前第 6 个/)).toBeInTheDocument()
    expect(screen.getByText('进入 2 次')).toBeInTheDocument()
  })

  it('从列表带过来的 tab=progress 直接落在处理明细上', async () => {
    renderDrawer({ initialTab: 'progress' })

    await waitFor(() => expect(timelineMock).toHaveBeenCalledWith('doc-1'))
    expect(await screen.findByText('各环节耗时')).toBeInTheDocument()
  })

  it('换文档会重新加载（组件被复用，不能只靠挂载那一次）', async () => {
    const { rerender } = render(
      <MemoryRouter>
        <DocumentDrawer documentId="doc-1" onClose={vi.fn()} />
      </MemoryRouter>,
    )
    await screen.findByText('说明书.pdf')

    docMock.mockResolvedValue(makeDoc({ id: 'doc-2', name: '白皮书.docx' }))
    chunksMock.mockResolvedValue({ items: [], total: 0 })
    rerender(
      <MemoryRouter>
        <DocumentDrawer documentId="doc-2" onClose={vi.fn()} />
      </MemoryRouter>,
    )

    expect(await screen.findByText('白皮书.docx')).toBeInTheDocument()
    expect(docMock).toHaveBeenLastCalledWith('doc-2')
  })

  it('Esc 先滑回去、动画跑完才通知宿主（不是啪地消失）', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const onClose = vi.fn()
    render(
      <MemoryRouter>
        <DocumentDrawer documentId="doc-1" onClose={onClose} />
      </MemoryRouter>,
    )
    await screen.findByText('说明书.pdf')

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
    // 收起动画期间还在 DOM 里
    expect(document.querySelector('.kb-drawer-closing')).not.toBeNull()

    vi.advanceTimersByTime(200)
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    vi.useRealTimers()
  })
})

describe('切块的人工干预', () => {
  it('编辑正文要显式保存，并用返回的记录替换本地那一条', async () => {
    updateChunkMock.mockResolvedValue(makeChunk({ text: '改过的正文' }))
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')
    await user.click(screen.getByRole('tab', { name: '切块' }))

    await user.click(screen.getByRole('button', { name: '切块操作' }))
    await user.click(screen.getByRole('menuitem', { name: '编辑正文' }))

    const textarea = screen.getByLabelText('第 1 块正文')
    await user.clear(textarea)
    await user.type(textarea, '改过的正文')
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(updateChunkMock).toHaveBeenCalledWith('doc-1', 0, '改过的正文'))
    expect(await screen.findByText('改过的正文')).toBeInTheDocument()
    // 文案跟着改：toast 后半句"检索会按新内容生效"是在解释机制，已按用户要求删除
    expect(successToast).toHaveBeenCalledWith('切块已更新')
  })

  it('禁用是"先藏起来"（可恢复），撤销入口就在同一个菜单里', async () => {
    disableChunkMock.mockResolvedValue(makeChunk({ disabled: true }))
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')
    await user.click(screen.getByRole('tab', { name: '切块' }))

    await user.click(screen.getByRole('button', { name: '切块操作' }))
    await user.click(screen.getByRole('menuitem', { name: '禁用（不参与检索）' }))

    await waitFor(() => expect(disableChunkMock).toHaveBeenCalledWith('doc-1', 0, true))
    expect(await screen.findByText('已禁用')).toBeInTheDocument()
    expect(successToast).toHaveBeenCalledWith('已禁用，该块不再参与检索')
  })

  it('删除切块要二次确认，并说清"其实想禁用"的那条替代方案', async () => {
    deleteChunkMock.mockResolvedValue(undefined)
    const user = userEvent.setup()
    renderDrawer()
    await screen.findByText('说明书.pdf')
    await user.click(screen.getByRole('tab', { name: '切块' }))

    await user.click(screen.getByRole('button', { name: '切块操作' }))
    await user.click(screen.getByRole('menuitem', { name: '删除' }))

    expect(await screen.findByText('确定删除第 1 块？')).toBeInTheDocument()
    expect(screen.getByText(/只想让它暂时不出现在检索里，用「禁用」/)).toBeInTheDocument()

    // 确认弹窗是 `@/ui/alert-dialog`（Radix）：role 是 alertdialog，不是 dialog
    const dialog = screen.getByRole('alertdialog', { name: '删除切块' })
    await user.click(within(dialog).getByRole('button', { name: '确定' }))
    await waitFor(() => expect(deleteChunkMock).toHaveBeenCalledWith('doc-1', 0))
  })
})
