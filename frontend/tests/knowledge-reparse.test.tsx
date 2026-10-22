/**
 * 文档动作里的「重新解析」用例（`KnowledgeBaseView` 行菜单）。
 *
 * 这里要钉住的不是"按钮存在"，而是**它与「重新摄入」是两件事**：
 * - 重新摄入只重切块、重向量，解析产物照旧复用（点下去直接走，不确认）；
 * - 重新解析从原件重新解析一遍，切块与向量都跟着重做——所以先过一道后果确认，
 *   且确认弹窗的那句话不能与上面那条共用（共用就等于告诉用户两者一样）。
 *
 * 另外两条边界：**取消**不调接口；后端拒绝（有任务在跑是 409）时把后端那句话原样显示。
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeBaseView } from '@/features/knowledge'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import { useOperatorStore } from '@/lib/operator'
import type { DocumentSummary } from '@/api/documents'
import type { Folder } from '@/api/folders'
import type { KnowledgeBase } from '@/api/knowledgeBases'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  listDocuments: vi.fn(),
  listDocumentParts: vi.fn(),
  batchDocuments: vi.fn(),
  deleteDocument: vi.fn(),
  getDocumentImpact: vi.fn(),
  renameDocument: vi.fn(),
  moveDocument: vi.fn(),
  setDocumentDisabled: vi.fn(),
  reprocessDocument: vi.fn(),
  reparseDocument: vi.fn(),
  cancelDocument: vi.fn(),
  downloadDocument: vi.fn(),
  getDocument: vi.fn(),
  listDocumentChunks: vi.fn(),
  getDocumentPreview: vi.fn(),
  getDocumentTimeline: vi.fn(),
  getDocumentPages: vi.fn(),
}))

vi.mock('@/api/folders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/folders')>()),
  listFolders: vi.fn(),
  createFolder: vi.fn(),
  renameFolder: vi.fn(),
  deleteFolder: vi.fn(),
}))

vi.mock('@/api/knowledgeBases', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/knowledgeBases')>()),
  listKnowledgeBases: vi.fn(),
  updateKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
  getKnowledgeBaseImpact: vi.fn(),
  createKnowledgeBase: vi.fn(),
}))

vi.mock('@/api/modelRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/modelRegistry')>()),
  getRegistry: vi.fn().mockResolvedValue({
    providers: [],
    models: [],
    slots: [],
    provider_kinds: {},
    capabilities: {},
    provider_presets: [],
  }),
}))

import { listDocuments, reparseDocument, reprocessDocument } from '@/api/documents'
import { listFolders } from '@/api/folders'
import { listKnowledgeBases } from '@/api/knowledgeBases'

const listDocsMock = vi.mocked(listDocuments)
const listFoldersMock = vi.mocked(listFolders)
const listKbMock = vi.mocked(listKnowledgeBases)
const reparseMock = vi.mocked(reparseDocument)
const reprocessMock = vi.mocked(reprocessDocument)
const successToast = vi.mocked(toast.success)
const errorToast = vi.mocked(toast.error)

function makeKB(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: 'kb-1',
    name: '产品手册',
    description: '',
    embedding_model_id: 'bge-m3',
    embedding_dim: 1024,
    chunk_strategy: 'recursive',
    chunk_size: 512,
    chunk_overlap: 64,
    suggested_enabled: false,
    suggested_count: 3,
    suggested_model_pk: null,
    suggested_prompt: '',
    system_prompt: '',
    wiki_enabled: true,
    created_at: null,
    can_manage: true,
    can_write: true,
    document_count: 1,
    last_activity: null,
    ...overrides,
  }
}

function makeDoc(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: 'doc-1',
    knowledge_base_id: 'kb-1',
    name: '说明书.pdf',
    source_kind: 'upload',
    stage: 'indexed',
    size_bytes: 2048,
    mime_type: 'application/pdf',
    page_count: 12,
    is_split: false,
    error: null,
    chunk_count: 42,
    uploaded_by: null,
    uploaded_by_name: '测试用户',
    folder_id: null,
    disabled: false,
    original_kind: 'pdf',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
    question_count: 0,
    questioned_chunk_count: 0,
    questions_pending: false,
    summary: '',
    progress: null,
    ...overrides,
  }
}

const FOLDER: Folder = {
  id: 'f-1',
  kb_id: 'kb-1',
  name: '合同',
  document_count: 1,
  created_at: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  useOperatorStore.setState({ roster: [] })
  listKbMock.mockResolvedValue({ items: [makeKB()] })
  listFoldersMock.mockResolvedValue({ items: [FOLDER] })
  listDocsMock.mockImplementation(async (_kbId, filter = {}) => ({
    items: filter.root && filter.limit === 1 ? [] : [makeDoc()],
    total: 1,
    limit: filter.limit ?? 20,
    offset: filter.offset ?? 0,
  }))
  reparseMock.mockResolvedValue({ document: makeDoc(), is_duplicate: false, task_id: 't-1' })
  reprocessMock.mockResolvedValue({ document: makeDoc(), is_duplicate: false, task_id: 't-2' })
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
})

function renderView() {
  return render(
    <MemoryRouter initialEntries={['/kb/kb-1']}>
      <Routes>
        <Route path="/kb/:kbId" element={<KnowledgeBaseView />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** 打开这一行的操作菜单。 */
async function openRowMenu(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('说明书.pdf')
  await user.click(screen.getByRole('button', { name: '说明书.pdf 的操作' }))
}

describe('重新解析', () => {
  it('行菜单里与「重新摄入」并列，两条都在（是两件事，不是同一个动作的两个名字）', async () => {
    const user = userEvent.setup()
    renderView()
    await openRowMenu(user)

    expect(screen.getByRole('menuitem', { name: '重新摄入' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '重新解析' })).toBeInTheDocument()
  })

  it('先确认后果再动手：弹窗说清解析产物/切块/向量都重做，取消则一个请求都不发', async () => {
    const user = userEvent.setup()
    renderView()
    await openRowMenu(user)
    await user.click(screen.getByRole('menuitem', { name: '重新解析' }))

    // 点菜单只是开确认框，接口还没动
    const dialog = await screen.findByRole('alertdialog', { name: '重新解析文档' })
    expect(within(dialog).getByText('重新解析「说明书.pdf」？')).toBeInTheDocument()
    expect(
      within(dialog).getByText(/会从原件重新解析一遍，切块与向量都按这次的结果重做/),
    ).toBeInTheDocument()
    expect(reparseMock).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(screen.queryByRole('alertdialog', { name: '重新解析文档' })).toBeNull(),
    )
    expect(reparseMock).not.toHaveBeenCalled()
  })

  it('确认后调接口、报成功，并把文档列表重新拉一遍', async () => {
    const user = userEvent.setup()
    renderView()
    await openRowMenu(user)
    const listCallsBefore = listDocsMock.mock.calls.length

    await user.click(screen.getByRole('menuitem', { name: '重新解析' }))
    const dialog = await screen.findByRole('alertdialog', { name: '重新解析文档' })
    await user.click(within(dialog).getByRole('button', { name: '重新解析' }))

    await waitFor(() => expect(reparseMock).toHaveBeenCalledWith('doc-1'))
    expect(successToast).toHaveBeenCalledWith('已重新解析「说明书.pdf」，处理完成后列表会自动刷新')
    // 刷新：列表（含每行的阶段与进度）重新取一次
    await waitFor(() => expect(listDocsMock.mock.calls.length).toBeGreaterThan(listCallsBefore))
    // 两条路各调各的接口：重新解析不该顺手触发重新摄入
    expect(reprocessMock).not.toHaveBeenCalled()
  })

  it('文档还在跑时后端拒绝（409）：把后端那句话显示出来，不当成功', async () => {
    reparseMock.mockRejectedValueOnce(new Error('该文档已有正在处理的任务'))
    const user = userEvent.setup()
    renderView()
    await openRowMenu(user)

    await user.click(screen.getByRole('menuitem', { name: '重新解析' }))
    const dialog = await screen.findByRole('alertdialog', { name: '重新解析文档' })
    await user.click(within(dialog).getByRole('button', { name: '重新解析' }))

    await waitFor(() => expect(errorToast).toHaveBeenCalledWith('该文档已有正在处理的任务'))
    expect(successToast).not.toHaveBeenCalled()
  })

  it('「重新摄入」照旧直接走（不弹确认），两条不会互相触发', async () => {
    const user = userEvent.setup()
    renderView()
    await openRowMenu(user)

    await user.click(screen.getByRole('menuitem', { name: '重新摄入' }))

    await waitFor(() => expect(reprocessMock).toHaveBeenCalledWith('doc-1'))
    expect(screen.queryByRole('alertdialog', { name: '重新解析文档' })).toBeNull()
    expect(reparseMock).not.toHaveBeenCalled()
  })
})
