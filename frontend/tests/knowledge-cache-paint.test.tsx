/**
 * 「先画一帧」的用例（M4 阶段 5）——**冷启动二阶**与 §4.3 那五条里能机械判的部分。
 *
 * 口径按方案 §8 那条更难走的（ii）：**冷启动 / 刷新后第一次进入**（内存空、
 * 本机留着一份）——"二进知识库页秒开"兑现的就是这一条。
 *
 * 四条主线：
 *
 * 1. **冷启动二阶**（列表页与文档列表各一遍）：实时接口还挂着时，屏幕上**已经有内容**、
 *    管理/上传入口**不在**（权限位没确认）、行上没有进度条；实时结果一落地就变成实时那份；
 * 2. **时序规则**：先画只发生一次；晚到的旧响应不许盖新结果；带筛选的视图一个请求都不发；
 * 3. **断连**：内容还在 + 那句"现在连不上，这是上次看到的内容（X）" + 写入口收起；
 * 4. **焦点再验证**（§4.4）：焦点回来先确认**当前视图**那一键，随后自己再拉一次实时。
 *
 * 网络一律替身：快照那一族与实时那一族都是 mock（这些用例一条真请求都不该发出去）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { KbCacheSnapshot } from '@/api/kbCache'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/kbCache', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/kbCache')>()),
  getKbCacheKnowledgeBases: vi.fn(),
  getKbCacheKnowledgeBase: vi.fn(),
  getKbCacheDocuments: vi.fn(),
  getKbCacheFolders: vi.fn(),
  getKbCacheDocument: vi.fn(),
  revalidateKbCache: vi.fn(),
}))

vi.mock('@/api/knowledgeBases', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/knowledgeBases')>()),
  listKnowledgeBases: vi.fn(),
  createKnowledgeBase: vi.fn(),
  updateKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
  getKnowledgeBaseImpact: vi.fn(),
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  listDocuments: vi.fn(),
  listDocumentParts: vi.fn(),
  getDocument: vi.fn(),
}))

vi.mock('@/api/folders', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/folders')>()),
  listFolders: vi.fn(),
  createFolder: vi.fn(),
  renameFolder: vi.fn(),
  deleteFolder: vi.fn(),
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

import { KnowledgeBaseView, KnowledgeBasesView } from '@/features/knowledge'
import {
  LIVE_FRAME,
  createSnapshotGate,
  docListViewOf,
  snapshotNote,
  withoutPermissions,
} from '@/features/knowledge/snapshot'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import { listDocuments, type DocumentSummary } from '@/api/documents'
import { listFolders } from '@/api/folders'
import {
  getKbCacheDocuments,
  getKbCacheFolders,
  getKbCacheKnowledgeBases,
  revalidateKbCache,
} from '@/api/kbCache'
import { listKnowledgeBases, type KnowledgeBase } from '@/api/knowledgeBases'
import { resetProviderStore, setProviderStatusForTest } from '@/api/provider'

const listKbsMock = vi.mocked(listKnowledgeBases)
const kbCacheListMock = vi.mocked(getKbCacheKnowledgeBases)
const kbCacheDocsMock = vi.mocked(getKbCacheDocuments)
const kbCacheFoldersMock = vi.mocked(getKbCacheFolders)
const revalidateMock = vi.mocked(revalidateKbCache)
const listDocsMock = vi.mocked(listDocuments)
const listFoldersMock = vi.mocked(listFolders)

/** 快照那份内容是什么时候看到的：**取"3 分钟前"**，两句话里的 X 就逐字可钉。 */
const FETCHED_AT = new Date(Date.now() - 3 * 60 * 1000).toISOString()
const ONLINE_NOTE = '上次更新于 3 分钟前'
const OFFLINE_NOTE = '现在连不上，这是上次看到的内容（3 分钟前）'

/* ------------------------------------------------------------------ 夹具 */

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

/** 一份"有内容"的快照（形状对着后端 `KbCacheSnapshotOut` 抄）。 */
function snapshot(
  resource: string,
  items: Record<string, unknown>[],
  overrides: Partial<KbCacheSnapshot> = {},
): KbCacheSnapshot {
  return {
    available: true,
    resource,
    scope_key: '',
    reason: '',
    items,
    payload: { items, total: items.length, limit: 20, offset: 0 },
    version: 'sha256:abc',
    source: 'reader',
    fetched_at: FETCHED_AT,
    checked_at: FETCHED_AT,
    stale: false,
    last_error: '',
    revalidating: false,
    ...overrides,
  }
}

/**
 * 库列表那一份快照：**没有权限位**（后端进快照前就剥掉了，D-B）——
 * 所以这里也刻意不写 `can_write` / `can_manage`。
 */
const SNAPSHOT_KBS: Record<string, unknown>[] = [
  { id: 'kb-a', name: '论文库', document_count: 12, last_activity: null },
  { id: 'kb-b', name: '专利库', document_count: 3, last_activity: null },
]

function makeKb(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: 'kb-live',
    name: '实时的库',
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
    wiki_enabled: false,
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
    id: 'doc-live',
    knowledge_base_id: 'kb-1',
    name: '实时的文档.pdf',
    source_kind: 'upload',
    stage: 'indexed',
    size_bytes: 2048,
    mime_type: 'application/pdf',
    page_count: 12,
    is_split: false,
    error: null,
    chunk_count: 42,
    uploaded_by: null,
    uploaded_by_name: '',
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

/**
 * 文档列表那一份快照的行。
 *
 * 两个刻意的写法：①**没有 `progress`**（后端进快照前剥掉了，§1.1）；②第一行**仍然带一个
 * `progress`**——那一份是"万一漏进来"的兜底：界面自己也必须把它抹掉（冻结的进度条
 * 是最糟的假象），所以这里拿它会画的进度条当反证。
 */
const SNAPSHOT_DOCS: Record<string, unknown>[] = [
  {
    ...makeDoc({ id: 'doc-a', name: '说明书.pdf', stage: 'vectorizing' as never }),
    progress: { status: 'running', step_index: 2, step_total: 6, step_label: '向量化' },
  },
  { ...makeDoc({ id: 'doc-b', name: '白皮书.docx', stage: 'indexed' }) },
]

function renderList() {
  return render(
    <MemoryRouter initialEntries={['/knowledge-bases']}>
      <KnowledgeBasesView />
    </MemoryRouter>,
  )
}

function renderDetail(initialEntry = '/kb/kb-1') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/kb/:kbId" element={<KnowledgeBaseView />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  resetProviderStore()
  listKbsMock.mockResolvedValue({ items: [makeKb({ id: 'kb-1' })] })
  listFoldersMock.mockResolvedValue({ items: [] })
  kbCacheFoldersMock.mockResolvedValue(snapshot('folders', []))
  kbCacheListMock.mockResolvedValue(snapshot('kb_list', []))
  listDocsMock.mockImplementation(async (_kbId, filter = {}) => {
    if (filter.root && filter.limit === 1) return { items: [], total: 0, limit: 1, offset: 0 }
    return { items: [makeDoc()], total: 1, limit: filter.limit ?? 20, offset: filter.offset ?? 0 }
  })
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
  resetProviderStore()
})

/* ------------------------------------------------------------------ 列表页：冷启动二阶 */

describe('列表页：冷启动二阶', () => {
  it('实时那条还挂着时，屏幕上已经有"上次看到的那两个库"；管理入口不在', async () => {
    const live = deferred<{ items: KnowledgeBase[] }>()
    listKbsMock.mockReturnValue(live.promise)
    kbCacheListMock.mockResolvedValue(snapshot('kb_list', SNAPSHOT_KBS))
    const { container } = renderList()

    // ① 实时接口**还没 resolve**：内容已经在屏幕上了（不是骨架屏）
    expect(await screen.findByText('论文库')).toBeInTheDocument()
    expect(screen.getByText('专利库')).toBeInTheDocument()
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0)
    // 计数也来自同一份（不另打一次接口）
    expect(kbCacheListMock).toHaveBeenCalledTimes(1)
    // 权限位没确认 → 管理入口**晚一步**出现（D-B：宁可晚一步，也不给一个点下去 403 的入口）
    expect(screen.queryByRole('button', { name: '论文库 的设置' })).toBeNull()
    // 顶上那一行如实说这是什么时候看到的
    expect(screen.getByText(ONLINE_NOTE)).toBeInTheDocument()

    // ② 实时结果落地：换成实时那份，权限位亮起来，小字那一行收掉
    live.resolve({ items: [makeKb({ id: 'kb-live', name: '实时的库' })] })
    expect(await screen.findByText('实时的库')).toBeInTheDocument()
    expect(screen.queryByText('论文库')).toBeNull()
    expect(screen.queryByText(ONLINE_NOTE)).toBeNull()
    expect(screen.getByRole('button', { name: '实时的库 的设置' })).toBeInTheDocument()

    // ③ 先画只发生一次
    expect(kbCacheListMock).toHaveBeenCalledTimes(1)
  })

  it('晚到的快照不许盖住已经落地的实时结果（§4.3-②）', async () => {
    const paint = deferred<KbCacheSnapshot | null>()
    kbCacheListMock.mockReturnValue(paint.promise)
    listKbsMock.mockResolvedValue({ items: [makeKb({ name: '实时的库' })] })
    renderList()

    // 实时先回来：屏幕上就是实时那一份
    expect(await screen.findByText('实时的库')).toBeInTheDocument()

    // 快照后到：**丢掉**，不许把它画上去
    paint.resolve(snapshot('kb_list', SNAPSHOT_KBS))
    await waitFor(() => expect(screen.queryByText(ONLINE_NOTE)).toBeNull())
    expect(screen.getByText('实时的库')).toBeInTheDocument()
    expect(screen.queryByText('论文库')).toBeNull()
  })

  it('同一个视图只画一次：二次进入不再问（哪怕实时那份是"一个库都没有"）', async () => {
    kbCacheListMock.mockResolvedValue(snapshot('kb_list', SNAPSHOT_KBS))
    const live = deferred<{ items: KnowledgeBase[] }>()
    listKbsMock.mockReturnValue(live.promise)
    const first = renderList()
    expect(await screen.findByText('论文库')).toBeInTheDocument()
    expect(kbCacheListMock).toHaveBeenCalledTimes(1)

    // 实时结果落地：它说"一个库都没有"——那也是**结论**，内存空了也不许再画快照
    // （否则一份更旧的清单会在下一次进页面时盖回来）
    live.resolve({ items: [] })
    expect(await screen.findByText('还没有知识库')).toBeInTheDocument()
    first.unmount()

    renderList()
    expect(await screen.findByText('还没有知识库')).toBeInTheDocument()
    expect(kbCacheListMock).toHaveBeenCalledTimes(1)
  })

  it('断连：列表还在 + 那句"现在连不上，这是上次看到的内容（X）" + 写入口收起', async () => {
    kbCacheListMock.mockResolvedValue(snapshot('kb_list', SNAPSHOT_KBS))
    listKbsMock.mockRejectedValue(new Error('连接超时'))
    renderList()

    expect(await screen.findByText('论文库')).toBeInTheDocument()
    expect(await screen.findByText(OFFLINE_NOTE)).toBeInTheDocument()
    // 实时读的报错语义不变：原因照旧摆出来（多出来的只是"这是上次看到的内容"这一句）
    expect(screen.getByText('连接超时')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '论文库 的设置' })).toBeNull()
  })

  it('没有副本（available:false）→ 照旧骨架屏，一个字都不多说', async () => {
    kbCacheListMock.mockResolvedValue(
      snapshot('kb_list', [], { available: false, payload: null, reason: '这台还没看过它' }),
    )
    const live = deferred<{ items: KnowledgeBase[] }>()
    listKbsMock.mockReturnValue(live.promise)
    const { container } = renderList()

    await waitFor(() => expect(kbCacheListMock).toHaveBeenCalledTimes(1))
    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(screen.queryByText(/上次更新于/)).toBeNull()
  })

  it('本机留着的是一份**空清单**：不置帧（不一边摆骨架屏一边说"上次更新于 X"）', async () => {
    kbCacheListMock.mockResolvedValue(snapshot('kb_list', []))
    const live = deferred<{ items: KnowledgeBase[] }>()
    listKbsMock.mockReturnValue(live.promise)
    const { container } = renderList()

    await waitFor(() => expect(kbCacheListMock).toHaveBeenCalledTimes(1))
    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(screen.queryByText(/上次更新于/)).toBeNull()
  })
})

/* ------------------------------------------------------------------ 文档列表：冷启动二阶 */

describe('文档列表：冷启动二阶', () => {
  it('快照先出（不摆骨架屏、行上没有进度条），实时落地后阶段与进度替换', async () => {
    // 实时那两条都先挂着：这样"快照那一帧"才是稳定可断言的
    const live = deferred<{
      items: DocumentSummary[]
      total: number
      limit: number
      offset: number
    }>()
    const kbLive = deferred<{ items: KnowledgeBase[] }>()
    listKbsMock.mockReturnValue(kbLive.promise)
    listDocsMock.mockImplementation(async (_kbId, filter = {}) => {
      if (filter.root && filter.limit === 1) return { items: [], total: 2, limit: 1, offset: 0 }
      return live.promise
    })
    kbCacheListMock.mockResolvedValue(
      // 库本身那一份也在：标题与权限位从它来（快照里**没有**权限位，D-B）
      snapshot('kb_list', [
        { id: 'kb-1', name: '产品手册', document_count: 2, last_activity: null },
      ]),
    )
    kbCacheDocsMock.mockImplementation(async (_kbId, view = {}) => {
      // 「未归档」计数那一份就是同一个资源的 size:1 视图
      if (view.root && view.size === 1) {
        return snapshot('doc_list', [], { payload: { items: [], total: 2, limit: 1, offset: 0 } })
      }
      return snapshot('doc_list', SNAPSHOT_DOCS, {
        scope_key: 'kb-1|folder:all|page:1|size:20',
        payload: { items: SNAPSHOT_DOCS, total: 2, limit: 20, offset: 0 },
      })
    })
    const { container } = renderDetail()

    // ① 实时那条还挂着：两行已经在屏幕上了（标题也来自快照那一份）
    expect(await screen.findByText('说明书.pdf')).toBeInTheDocument()
    expect(screen.getByText('白皮书.docx')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: '产品手册' })).toBeInTheDocument()
    // **画快照期间不显示骨架屏**（内容已在）
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0)
    // 行上**没有进度条**：冻结的进度条是最糟的假象（§1.1 / §4.3-④）
    expect(container.querySelector('.kb-doc-progress')).toBeNull()
    // 没确认权限之前不摆写入口（上传 / 新建目录 / 勾选那一列 / 设置齿轮）
    expect(screen.queryByRole('button', { name: /上传文档/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /新建目录/ })).toBeNull()
    expect(screen.queryByRole('button', { name: '产品手册 的设置' })).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    // 那是"未确认"，不是"确认过是只读"：那句话也不许提前说
    expect(screen.queryByText(/你是只读权限/)).toBeNull()
    expect(screen.getByText(ONLINE_NOTE)).toBeInTheDocument()
    // 三个视图各画一次（文档列表 / 未归档计数 / 目录）——目录那一份同屏
    expect(kbCacheDocsMock).toHaveBeenCalledWith('kb-1', { page: 1, size: 20 })
    expect(kbCacheDocsMock).toHaveBeenCalledWith('kb-1', { root: true, page: 1, size: 1 })
    expect(kbCacheFoldersMock).toHaveBeenCalledWith('kb-1')

    // ② 实时结果落地：阶段与进度都从实时那一份来，权限位亮起来
    live.resolve({
      items: [makeDoc({ id: 'doc-live', name: '实时的文档.pdf', stage: 'embedding' })],
      total: 1,
      limit: 20,
      offset: 0,
    })
    expect(await screen.findByText('实时的文档.pdf')).toBeInTheDocument()
    expect(screen.queryByText('说明书.pdf')).toBeNull()
    kbLive.resolve({ items: [makeKb({ id: 'kb-1', name: '产品手册' })] })
    expect(await screen.findByRole('button', { name: /上传文档/ })).toBeInTheDocument()
    expect(screen.queryByText(ONLINE_NOTE)).toBeNull()
  })

  it('带搜索的视图**一个快照请求都不发**（D-D：这一档没有副本）', async () => {
    const user = userEvent.setup()
    renderDetail()
    expect(await screen.findByText('实时的文档.pdf')).toBeInTheDocument()
    kbCacheDocsMock.mockClear()

    await user.type(screen.getByLabelText('按文件名过滤'), '说明')
    await waitFor(() =>
      expect(listDocsMock).toHaveBeenCalledWith('kb-1', expect.objectContaining({ q: '说明' })),
    )
    expect(kbCacheDocsMock).not.toHaveBeenCalled()
  })
})

/* ------------------------------------------------------------------ 焦点再验证 */

describe('焦点再验证（§4.4）', () => {
  it('列表页：焦点回来先确认库列表那一份，随后自己再拉一次实时', async () => {
    setProviderStatusForTest({ state: 'ready', available: true })
    kbCacheListMock.mockResolvedValue(snapshot('kb_list', SNAPSHOT_KBS))
    revalidateMock.mockResolvedValue(snapshot('kb_list', SNAPSHOT_KBS))
    renderList()
    // 首屏那次实时读跑完之后（两条线都落地了）才点焦点：这样数的是焦点这一趟
    expect(await screen.findByText('实时的库')).toBeInTheDocument()

    revalidateMock.mockClear()
    listKbsMock.mockClear()
    fireEvent(window, new Event('focus'))

    await waitFor(() => expect(revalidateMock).toHaveBeenCalledWith({ resource: 'kb_list' }))
    // 随后页面自己再拉一次实时（可见更新）
    await waitFor(() => expect(listKbsMock).toHaveBeenCalledTimes(1))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('详情页：确认的是**当前视图那一键**（文档列表 + 当前页码），随后自己再拉一次实时', async () => {
    setProviderStatusForTest({ state: 'ready', available: true })
    revalidateMock.mockResolvedValue(snapshot('doc_list', []))
    renderDetail()
    expect(await screen.findByText('实时的文档.pdf')).toBeInTheDocument()

    revalidateMock.mockClear()
    listDocsMock.mockClear()
    fireEvent(window, new Event('focus'))

    await waitFor(() =>
      expect(revalidateMock).toHaveBeenCalledWith({
        resource: 'doc_list',
        kb_id: 'kb-1',
        page: 1,
        size: 20,
      }),
    )
    await waitFor(() => expect(listDocsMock).toHaveBeenCalled())
  })

  it('不是本机档（浏览器 / NAS 网页端）时一个字都不做：不打扰那一档', async () => {
    // 这一档要的是"**还没探过提供者**"这个**状态**，不是"探测恰好还没回来"这个**时刻**：
    // 把网络钉住——本机 8765 上真有一台边车时，这条读会真的成功，用例就变成看环境的脸色
    // （真机物证期间抓到过一次：那一台上正跑着临时边车）。
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(() => {})),
    )
    // 没探过提供者 = 不是本机档（`providerGateApplies` 不成立）
    renderList()
    expect(await screen.findByText('实时的库')).toBeInTheDocument()

    revalidateMock.mockClear()
    listKbsMock.mockClear()
    fireEvent(window, new Event('focus'))

    await Promise.resolve()
    expect(revalidateMock).not.toHaveBeenCalled()
    expect(listKbsMock).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})

/* ------------------------------------------------------------------ 纯判据 */

describe('视图与文案：能机械判的那几条', () => {
  it('带搜索 / 阶段 / 来源的视图没有副本（D-D）；目录与未归档两档互斥', () => {
    expect(docListViewOf({}, 1, 20)).toEqual({ page: 1, size: 20 })
    expect(docListViewOf({ root: true }, 1, 1)).toEqual({ root: true, page: 1, size: 1 })
    expect(docListViewOf({ folderId: 'f-9' }, 2, 20)).toEqual({ folder: 'f-9', page: 2, size: 20 })
    // `folder` 与 `root` 同时给时按 `folder` 算（与 `listDocuments` 的 if/else if 同一口径）
    expect(docListViewOf({ folderId: 'f-9', root: true }, 1, 20)).toEqual({
      folder: 'f-9',
      page: 1,
      size: 20,
    })
    expect(docListViewOf({ q: '说明书' }, 1, 20)).toBeNull()
    expect(docListViewOf({ stage: 'indexed' }, 1, 20)).toBeNull()
    expect(docListViewOf({ sourceKind: 'upload' }, 1, 20)).toBeNull()
  })

  it('两句话是唯一出口：在线一句、断连一句；实时帧没有这一行', () => {
    expect(snapshotNote({ fromSnapshot: true, snapshotAt: FETCHED_AT }, false)).toBe(ONLINE_NOTE)
    expect(snapshotNote({ fromSnapshot: true, snapshotAt: FETCHED_AT }, true)).toBe(OFFLINE_NOTE)
    expect(snapshotNote(LIVE_FRAME, false)).toBe('')
    expect(snapshotNote(LIVE_FRAME, true)).toBe('')
    // 时间戳没带（后端没给）时也要说得出话，不能变成"上次更新于 —"
    expect(snapshotNote({ fromSnapshot: true, snapshotAt: '' }, false)).toBe('上次更新于 —')
    // 界面文案里**不许出现"缓存"二字**（U2 硬门禁）：这两句是唯一出口，就在这一条盯着
    for (const sentence of [ONLINE_NOTE, OFFLINE_NOTE]) {
      expect(sentence).not.toContain('缓存')
    }
  })

  it('快照行只读：权限位置假（而不是"没有这一位"）', () => {
    const row = withoutPermissions({ id: 'kb-a', name: '论文库' })
    expect(row.can_write).toBe(false)
    expect(row.can_manage).toBe(false)
  })

  it('闸门：画过（或那份内容本来空的）/ 已有实时结果 → 不再画；实时一到，在飞的那一帧作废', () => {
    const gate = createSnapshotGate()
    expect(gate.claim('v')).toBe(true)
    // 正在飞：第二次认领不成立（不重复发请求）
    expect(gate.claim('v')).toBe(false)

    // 没画上就还回去：下一次还有机会
    gate.release('v')
    expect(gate.claim('v')).toBe(true)
    gate.settle('v')
    // 这一次了结了：这个视图不再画第二次
    expect(gate.claim('v')).toBe(false)

    const other = createSnapshotGate()
    expect(other.claim('v')).toBe(true)
    expect(other.stillWanted('v')).toBe(true)
    // 实时结果落地：在飞的那一帧作废，之后也不许再画（"实时结果永远赢"）
    other.live('v')
    expect(other.stillWanted('v')).toBe(false)
    expect(other.claim('v')).toBe(false)
  })
})
