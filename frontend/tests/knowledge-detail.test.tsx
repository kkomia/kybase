/**
 * 知识库详情页（`KnowledgeBaseView`）的用例：文档列表、筛选、多选与批量、
 * 目录树、删除前的影响清单、分页、只读权限。
 *
 * 对应旧用例：`frontend/tests/unit/views/KnowledgeBaseView.test.ts`（若有）与
 * `api/documents.test.ts` 里"分页参数下推"、`batchDocuments` 那两条。
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeBaseView } from '@/features/knowledge'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import { useOperatorStore } from '@/lib/operator'
import type { Folder } from '@/api/folders'
import type { DocumentSummary } from '@/api/documents'
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
  cancelDocument: vi.fn(),
  downloadDocument: vi.fn(),
  getDocument: vi.fn(),
  listDocumentChunks: vi.fn(),
  getDocumentPreview: vi.fn(),
  getDocumentTimeline: vi.fn(),
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

// M4 阶段 5：本机留的那一份（`/local/kb-cache/*`）。默认不回答（= 没有副本）——
// 那些用例只关心"这一读有没有发、以什么形状发"。
vi.mock('@/api/kbCache', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/kbCache')>()),
  getKbCacheKnowledgeBases: vi.fn(),
  getKbCacheDocuments: vi.fn(),
  getKbCacheFolders: vi.fn(),
  revalidateKbCache: vi.fn(),
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

import {
  batchDocuments,
  deleteDocument,
  getDocument,
  getDocumentImpact,
  listDocumentParts,
  listDocuments,
  setDocumentDisabled,
} from '@/api/documents'
import { createFolder, listFolders } from '@/api/folders'
import { getKbCacheDocuments, getKbCacheFolders, getKbCacheKnowledgeBases } from '@/api/kbCache'
import { listKnowledgeBases } from '@/api/knowledgeBases'

const listDocsMock = vi.mocked(listDocuments)
const listFoldersMock = vi.mocked(listFolders)
const createFolderMock = vi.mocked(createFolder)
const docMock = vi.mocked(getDocument)
const batchMock = vi.mocked(batchDocuments)
const deleteMock = vi.mocked(deleteDocument)
const impactMock = vi.mocked(getDocumentImpact)
const partsMock = vi.mocked(listDocumentParts)
const disabledMock = vi.mocked(setDocumentDisabled)
const listKbMock = vi.mocked(listKnowledgeBases)
const kbCacheDocsMock = vi.mocked(getKbCacheDocuments)
const kbCacheFoldersMock = vi.mocked(getKbCacheFolders)
const kbCacheListMock = vi.mocked(getKbCacheKnowledgeBases)
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
    document_count: 2,
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
    uploaded_by_name: '小又',
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

/** 列表接口被两次用途共用：文档清单与"未归档"计数（`root: true, limit: 1`）。 */
function mockList(docs: DocumentSummary[], total = docs.length, unfiled = docs.length): void {
  listDocsMock.mockImplementation(async (_kbId, filter = {}) => {
    if (filter.root && filter.limit === 1) {
      return { items: [], total: unfiled, limit: 1, offset: 0 }
    }
    return { items: docs, total, limit: filter.limit ?? 20, offset: filter.offset ?? 0 }
  })
}

function renderView(initialEntry = '/kb/kb-1') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/kb/:kbId" element={<KnowledgeBaseView />} />
      </Routes>
    </MemoryRouter>,
  )
}

const FOLDER: Folder = {
  id: 'f-1',
  kb_id: 'kb-1',
  name: '合同',
  document_count: 3,
  created_at: null,
}

/** 名册里的一条：上传者那一列只在名册拉到之后才出现（行与列头同一条条件）。 */
const ROSTER_USER = {
  id: 'u-1',
  name: '小又',
  note: '',
  created_at: null,
  document_count: 2,
  avatar_url: '',
  username: null,
  role: 'member' as const,
  disabled: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  useOperatorStore.setState({ roster: [] })
  listKbMock.mockResolvedValue({ items: [makeKB()] })
  listFoldersMock.mockResolvedValue({ items: [FOLDER] })
  mockList([
    makeDoc(),
    makeDoc({ id: 'doc-2', name: '白皮书.docx', chunk_count: 8, stage: 'enriched' }),
  ])
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
})

describe('页头', () => {
  it('设置齿轮**贴在标题右侧**（同一行），不是页面右上角', async () => {
    renderView()

    const title = await screen.findByRole('heading', { level: 1, name: '产品手册' })
    // 与标题同处一行的那个容器：旧 `PageHeader.vue` 的 `title-suffix` 槽就是这个位置
    // （"这类控件属于这个对象本身，放在右侧动作区会读成页面的动作"）。
    // 给的 `h1` 一旦带上 `flex: 1`，齿轮就会被推到页面右缘——对照记录 §3 第 3 条。
    const row = title.parentElement as HTMLElement
    expect(row.className).toContain('kb-title-row')
    expect(within(row).getByRole('button', { name: '产品手册 的设置' })).toBeInTheDocument()
    expect(row.style.flex).toBe('')
  })

  it('只读分享的库没有设置齿轮（与旧版同一条 `can_write` 判定）', async () => {
    listKbMock.mockResolvedValue({ items: [makeKB({ can_write: false, can_manage: false })] })
    renderView()

    const title = await screen.findByRole('heading', { level: 1, name: '产品手册' })
    expect(within(title.parentElement as HTMLElement).queryByRole('button')).toBeNull()
  })
})

describe('文档列表', () => {
  it('七列都有表头，表头与行内共用同一组列宽（评审 K3 / K6）', async () => {
    useOperatorStore.setState({ roster: [ROSTER_USER] })
    const { container } = renderView()

    await screen.findByText('说明书.pdf')
    const head = container.querySelector('.kb-doc-head') as HTMLElement
    const row = container.querySelector('.kb-doc-row') as HTMLElement
    const headFile = head.querySelector('.kb-head-file') as HTMLElement

    // 列头与数据列一一对应，顺序就是列顺序（原先只有五个，状态与上传者有内容没列头）
    expect((head.textContent ?? '').replace(/\s+/g, '')).toBe('文件状态上传者切块问题大小更新时间')
    expect(within(row).getByText('小又')).toBeInTheDocument()

    // jsdom 不做布局，"对齐"只能守到这一层：列头与行内那两格用的是同一组宽度，
    // 左边缘才有可能落在同一条竖线上（宽度随文案走的话，列头只能对上其中一行）
    const rowTag = within(row).getByText('已索引')
    const rowUploader = row.querySelector('.kb-col-uploader') as HTMLElement
    expect(within(headFile).getByText('状态').style.flex).toBe(rowTag.parentElement?.style.flex)
    expect(within(headFile).getByText('上传者').style.flex).toBe(rowUploader.style.flex)
  })

  it('名册没拉到时不摆"上传者"这一列：行里没有，列头也不能有', async () => {
    const { container } = renderView()

    await screen.findByText('说明书.pdf')
    const head = container.querySelector('.kb-doc-head') as HTMLElement
    expect(head.textContent ?? '').not.toContain('上传者')
    expect(container.querySelector('.kb-col-uploader')).toBeNull()
  })

  it('一篇都没有时不画装饰补白行：空态自己就是这块区域的答案', async () => {
    mockList([], 0, 0)
    const { container } = renderView()

    expect(await screen.findByText('这个知识库里还没有文档')).toBeInTheDocument()
    // 空列表底下原先铺着一屏 16 行装饰行，看起来像"列表还没渲染完"（评审 57 号图）
    expect(container.querySelectorAll('.kb-doc-filler')).toHaveLength(0)
  })

  it('有文件但填不满一屏时照旧补白（这一条只服务空列表）', async () => {
    mockList([makeDoc()], 1, 1)
    const { container } = renderView()

    await screen.findByText('说明书.pdf')
    expect(container.querySelectorAll('.kb-doc-filler').length).toBeGreaterThan(0)
  })

  it('渲染行：名称、状态、切块数与大小都在一行里，分页参数下推给后端', async () => {
    const { container } = renderView()

    expect(await screen.findByText('说明书.pdf')).toBeInTheDocument()
    // 行内断言：筛选下拉里也有同名的状态文案，按名字全局找会撞上
    const row = container.querySelector('.kb-doc-row') as HTMLElement
    expect(within(row).getByText('说明书.pdf')).toBeInTheDocument()
    expect(within(row).getByText('已索引')).toBeInTheDocument()
    expect(within(row).getByText('42')).toBeInTheDocument()
    expect(within(row).getByText('2.0 KB')).toBeInTheDocument()
    expect(listDocsMock.mock.calls[0][1]).toMatchObject({ limit: 20, offset: 0 })
    expect(await screen.findByText('共 2 篇')).toBeInTheDocument()
  })

  // **放宽这一处**（不是放宽全局 `testTimeout`）：两轮 Radix 下拉开合 + 300ms 防抖 + 三轮清单
  // 查询都是 CPU 型重活，全量并发时被挤过默认 5s（实测 5226ms）；单跑 1127ms，15s 是并发余量。
  it('状态筛选与搜索（防抖后）都把条件下推给接口，并且回第 1 页', { timeout: 15_000 }, async () => {
    renderView()
    await screen.findByText('说明书.pdf')

    // 列表接口同时服务"文档清单"与"未归档计数"，所以按特征匹配调用而不是比对整份参数
    const calledWith = (predicate: (filter: Record<string, unknown>) => boolean) =>
      listDocsMock.mock.calls.some(([, filter]) =>
        predicate((filter ?? {}) as Record<string, unknown>),
      )

    const user = userEvent.setup()
    // 筛选下拉是 `@/ui/select`（Radix）：先点开触发器，再点选项（不再是原生 select 的 change）
    await user.click(screen.getByRole('combobox', { name: '按状态筛选' }))
    await user.click(await screen.findByRole('option', { name: '失败' }))
    await waitFor(() => expect(calledWith((filter) => filter.stage === 'failed')).toBe(true))

    // 选回「全部状态」：Radix 的 Item 不收空串值，界面上用的是一个哨兵值，
    // 换回 `''` 之后筛选条件必须真的从请求里**消失**（而不是发一个空串下去）
    await user.click(screen.getByRole('combobox', { name: '按状态筛选' }))
    await user.click(await screen.findByRole('option', { name: '全部状态' }))
    await waitFor(() => {
      // 文档清单那一轮（`limit: 20`）里不该再有 stage——另外那一轮是"未归档计数"（`limit: 1`）
      const listCall = listDocsMock.mock.calls
        .map(([, filter]) => (filter ?? {}) as Record<string, unknown>)
        .filter((filter) => filter.limit === 20)
        .at(-1)
      expect(listCall).not.toHaveProperty('stage')
    })

    // 就地的那个入口按 §工作台 "过滤" 口径命名（与开弹层的「按内容检索」区分开）
    await user.type(screen.getByLabelText('按文件名过滤'), '白皮书')
    // 防抖 300ms 之后才发请求
    await waitFor(() => expect(calledWith((filter) => filter.q === '白皮书')).toBe(true), {
      timeout: 2000,
    })
    // 换了筛选条件就回第 1 页（offset 归零）
    const last = listDocsMock.mock.calls.at(-1)?.[1]
    expect(last?.offset).toBe(0)
  })

  it('目录树给「全部文档 / 未归档 / 各目录」，点目录按 folder_id 过滤', async () => {
    mockList([makeDoc()], 1, 4)
    renderView()

    expect(await screen.findByText('合同')).toBeInTheDocument()
    expect(screen.getByText('未归档')).toBeInTheDocument()
    // 全部 = 未归档 + 各目录计数（不与当前筛选耦合）
    expect(screen.getByText('7')).toBeInTheDocument()

    await userEvent.setup().click(screen.getByTitle('合同'))
    await waitFor(() =>
      expect(listDocsMock).toHaveBeenCalledWith('kb-1', { folderId: 'f-1', limit: 20, offset: 0 }),
    )
  })

  it('还没有目录时目录栏摊成一行（不再常驻 216px），「新建目录」是按钮形态', async () => {
    listFoldersMock.mockResolvedValue({ items: [] })
    const { container } = renderView()
    await screen.findByText('说明书.pdf')

    // 「全部文档 / 未归档」此刻筛的是同一批文档——整栏不渲染，列表拿回那 216px
    expect(container.querySelector('.kb-tree-flat')).not.toBeNull()
    expect(container.querySelector('.kb-tree-list')).toBeNull()
    expect(screen.queryByText('全部文档')).not.toBeInTheDocument()
    expect(screen.getByText('还没有目录')).toBeInTheDocument()

    // 建目录的入口还在，而且是"图标 + 文字"的按钮（原先是 24px 的一颗「+」）
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '新建目录' }))
    await user.type(screen.getByLabelText('目录名'), '合同')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(createFolderMock).toHaveBeenCalledWith('kb-1', '合同'))
  })

  it('抽屉开着时：底下一层轻遮罩，当前那一行有选中态（`--bg-selected` 那一档）', async () => {
    const opened = makeDoc({ id: 'doc-2', name: '白皮书.docx' })
    mockList([makeDoc(), opened])
    docMock.mockResolvedValue(opened)
    const { container } = renderView('/kb/kb-1?doc=doc-2')
    await screen.findByText('说明书.pdf')

    // 遮罩与抽屉是两层：遮罩在下（z-index 小一档），点它收起
    expect(container.querySelector('.kb-drawer-scrim')).not.toBeNull()
    expect(container.querySelector('.kb-drawer')).not.toBeNull()

    // 抽屉里那一篇在列表里留着选中态；另一行没有
    const rows = [...container.querySelectorAll('.kb-doc-row')]
    const on = rows.filter((row) => row.classList.contains('kb-doc-row-on'))
    expect(on).toHaveLength(1)
    expect(on[0].textContent).toContain('白皮书.docx')
    expect(container.querySelector('.kb-row-name-link[aria-current="page"]')).not.toBeNull()
  })

  it('分页：上一页/下一页按页码禁用，翻页只换内容不清空勾选', async () => {
    mockList([makeDoc()], 45)
    renderView()
    await screen.findByText('说明书.pdf')

    expect(screen.getByText('第 1 / 3 页')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()

    const user = userEvent.setup()
    await user.click(screen.getByRole('checkbox', { name: '选择 说明书.pdf' }))
    await user.click(screen.getByRole('button', { name: '下一页' }))

    await waitFor(() =>
      expect(listDocsMock).toHaveBeenCalledWith('kb-1', { limit: 20, offset: 20 }),
    )
    // 翻页保留选择：批量条还在，且如实说明跨页
    expect(await screen.findByText(/已选 1 篇/)).toBeInTheDocument()
  })
})

describe('多选与批量', () => {
  it('表头全选只吃当前页，批量删除走确认弹窗并把 id 原样送出', async () => {
    batchMock.mockResolvedValue({
      action: 'delete',
      succeeded: 2,
      failed: 0,
      items: [
        { document_id: 'doc-1', ok: true, error: null },
        { document_id: 'doc-2', ok: true, error: null },
      ],
    })
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('checkbox', { name: /全选本页/ }))
    expect(await screen.findByText(/已选 2 篇/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^删除$/ }))
    expect(await screen.findByText('删除选中的 2 篇文档？')).toBeInTheDocument()
    // 确认弹窗是 `@/ui/alert-dialog`（Radix）：role 是 alertdialog，不是 dialog
    const dialog = screen.getByRole('alertdialog', { name: '删除文档' })
    await user.click(within(dialog).getByRole('button', { name: '确定' }))

    await waitFor(() =>
      expect(batchMock).toHaveBeenCalledWith('kb-1', 'delete', ['doc-1', 'doc-2'], null, false),
    )
    expect(successToast).toHaveBeenCalledWith('已删除 2 篇')
    // 成功后清空选择，批量条消失
    await waitFor(() => expect(screen.queryByText(/已选 2 篇/)).not.toBeInTheDocument())
  })

  it('批量部分失败：逐条报账，并只保留失败的那些（方便直接重试）', async () => {
    batchMock.mockResolvedValue({
      action: 'reprocess',
      succeeded: 1,
      failed: 1,
      items: [
        { document_id: 'doc-1', ok: true, error: null },
        { document_id: 'doc-2', ok: false, error: '文件已删除' },
      ],
    })
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('checkbox', { name: /全选本页/ }))
    await user.click(screen.getByRole('button', { name: /重新摄入/ }))

    await waitFor(() => expect(batchMock).toHaveBeenCalledTimes(1))
    expect(errorToast).toHaveBeenCalledWith('重新摄入：1 篇成功、1 篇失败（文件已删除）')
    expect(await screen.findByText(/已选 1 篇/)).toBeInTheDocument()
  })

  it('跨页选择如实说明"另有 N 篇不在本页"（翻页不清空勾选）', async () => {
    // 两页各一篇不同的文档
    listDocsMock.mockImplementation(async (_kbId, filter = {}) => {
      if (filter.root && filter.limit === 1) return { items: [], total: 45, limit: 1, offset: 0 }
      const offset = filter.offset ?? 0
      const items = offset === 0 ? [makeDoc()] : [makeDoc({ id: 'doc-2', name: '白皮书.docx' })]
      return { items, total: 45, limit: 20, offset }
    })
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('checkbox', { name: '选择 说明书.pdf' }))
    await user.click(screen.getByRole('button', { name: '下一页' }))
    await user.click(await screen.findByRole('checkbox', { name: '选择 白皮书.docx' }))

    // 两篇都在选中集里，而当前页只有后一篇 → 必须如实说明跨页
    expect(await screen.findByText(/已选 2 篇/)).toBeInTheDocument()
    expect(screen.getByText(/另有 1 篇不在本页/)).toBeInTheDocument()
  })

  it('批量停用检索：走同一条逐条回成败的路径', async () => {
    batchMock.mockResolvedValue({
      action: 'disable',
      succeeded: 1,
      failed: 0,
      items: [{ document_id: 'doc-1', ok: true, error: null }],
    })
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('checkbox', { name: '选择 说明书.pdf' }))
    await user.click(screen.getByRole('button', { name: '停用检索' }))

    await waitFor(() =>
      expect(batchMock).toHaveBeenCalledWith('kb-1', 'disable', ['doc-1'], null, false),
    )
    expect(successToast).toHaveBeenCalledWith('已停用 1 篇的检索')
  })
})

describe('单篇动作', () => {
  it('删除前先把影响清单摆出来（切块 / 空间 / 进行中的任务）', async () => {
    impactMock.mockResolvedValue({
      kind: 'document',
      id: 'doc-1',
      name: '说明书.pdf',
      documents: 1,
      chunks: 412,
      parts: 0,
      size_bytes: 1024 * 1024,
      running_tasks: 2,
      document_names: [],
      restorable: true,
    })
    deleteMock.mockResolvedValue({ id: 't-1', document_id: 'doc-1', expires_at: '' })
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('button', { name: '说明书.pdf 的操作' }))
    await user.click(screen.getByRole('menuitem', { name: /删除/ }))

    expect(await screen.findByText('412')).toBeInTheDocument()
    expect(screen.getByText('1.0 MB')).toBeInTheDocument()
    expect(screen.getByText(/原文进回收站保留 7 天/)).toBeInTheDocument()
    expect(impactMock).toHaveBeenCalledWith('doc-1')

    const dialog = screen.getByRole('alertdialog', { name: '删除文档' })
    await user.click(within(dialog).getByRole('button', { name: '确定' }))
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith('doc-1'))
  })

  it('行菜单里停用检索只动标记：文档与内容都保留', async () => {
    disabledMock.mockResolvedValue(makeDoc({ disabled: true }))
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('button', { name: '说明书.pdf 的操作' }))
    await user.click(screen.getByRole('menuitem', { name: /停用检索/ }))

    await waitFor(() => expect(disabledMock).toHaveBeenCalledWith('doc-1', true))
    expect(successToast).toHaveBeenCalledWith('已停用检索：文档与内容都保留')
  })

  it('大文件（is_split）可展开子文件树，按需拉一次 parts', async () => {
    mockList([makeDoc({ is_split: true })])
    partsMock.mockResolvedValue({
      items: [
        {
          id: 'p-1',
          part_index: 0,
          page_start: 1,
          page_end: 10,
          stage: 'indexed',
          error: null,
        },
      ],
    })
    const user = userEvent.setup()
    renderView()
    await screen.findByText('说明书.pdf')

    await user.click(screen.getByRole('button', { name: '展开子文件' }))
    expect(await screen.findByText('分片 P1')).toBeInTheDocument()
    expect(screen.getByText('第 1–10 页')).toBeInTheDocument()
    expect(partsMock).toHaveBeenCalledWith('doc-1')
  })

  it('还在跑的文档多显示一行分段进度与「处理明细」入口', async () => {
    mockList([
      makeDoc({
        stage: 'chunking',
        progress: {
          status: 'running',
          step_index: 3,
          step_total: 6,
          step_label: '切分内容',
          elapsed_ms: 134_000,
          total_ms: 134_000,
          retries: 1,
          stalled: false,
        },
      }),
    ])
    renderView()

    // 配比斜杠两侧带空格：全站一种写法
    expect(await screen.findByText(/第 3 \/ 6 步 · 切分内容/)).toBeInTheDocument()
    expect(screen.getByText(/重试 1 次/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '处理明细' })).toBeInTheDocument()
  })

  it('只读分享的库：没有勾选框与上传入口，但能下载与检索', async () => {
    listKbMock.mockResolvedValue({ items: [makeKB({ can_write: false, can_manage: false })] })
    renderView()

    expect(await screen.findByText(/只读权限/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /全选本页/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /上传文档/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '按内容检索' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '说明书.pdf 的操作' })).toBeInTheDocument()
  })
})

describe('先画一帧（M4 阶段 5）', () => {
  it('挂载时三个视图各画一次：文档列表（当前视图那一键）、目录树、未归档计数', async () => {
    renderView()
    await screen.findByText('说明书.pdf')

    // 文档列表：当前视图（第 1 页、一页 20 篇）那一个键——与实时读同一次筛选
    expect(kbCacheDocsMock).toHaveBeenCalledWith('kb-1', { page: 1, size: 20 })
    // 「未归档」计数就是同一个资源的 `root` + `size:1` 视图（后端那边也落成同一个键）
    expect(kbCacheDocsMock).toHaveBeenCalledWith('kb-1', { root: true, page: 1, size: 1 })
    // 目录树与文档列表**同屏**，所以也要画（不做就是"半屏"）
    expect(kbCacheFoldersMock).toHaveBeenCalledWith('kb-1')
  })

  it('快照那一帧上不说"你是只读权限"：未确认不等于确认过是只读', async () => {
    let resolveLive: (value: { items: KnowledgeBase[] }) => void = () => {}
    listKbMock.mockReturnValue(new Promise((done) => (resolveLive = done)))
    kbCacheListMock.mockResolvedValue({
      // 本机留的那一份：**没有权限位**（后端进快照前就剥掉了，D-B）
      available: true,
      resource: 'kb_list',
      scope_key: '',
      reason: '',
      items: [{ id: 'kb-1', name: '产品手册', document_count: 2, last_activity: null }],
      payload: null,
      version: 'sha256:abc',
      source: 'reader',
      fetched_at: new Date().toISOString(),
      checked_at: new Date().toISOString(),
      stale: false,
      last_error: '',
      revalidating: false,
    })
    renderView()

    // 标题这一份已经先画上了（库列表是本机留的那一份）
    expect(await screen.findByRole('heading', { level: 1, name: '产品手册' })).toBeInTheDocument()
    expect(await screen.findByText('说明书.pdf')).toBeInTheDocument()
    // 权限位还没确认：既不摆写入口，也**不许**把它说成"你是只读权限"
    expect(screen.queryByText(/只读权限/)).toBeNull()
    expect(screen.queryByRole('button', { name: '产品手册 的设置' })).toBeNull()

    // 实时那一份落地：确认它**确实**是只读的，这句话才出现
    resolveLive({ items: [makeKB({ can_write: false, can_manage: false })] })
    expect(await screen.findByText(/只读权限/)).toBeInTheDocument()
  })
})
