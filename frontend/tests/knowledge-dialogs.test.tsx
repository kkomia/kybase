/**
 * 其余弹窗与面板的用例：上传、分享、数据源、库内检索、`/documents/:id` 跳板。
 *
 * 对应旧用例里的逻辑型条目：`UploadDialog.test.ts` 的上限/去重/串行/结果清单、
 * `ShareDialog`（若有）、`SourcePanel` 的"登记与拉取分开"、
 * `KbSearchPanel` 的检索参数与"向量召回不可信"提示。
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DocumentView,
  KbSearchPanel,
  ShareDialog,
  SourcePanel,
  UploadDialog,
} from '@/features/knowledge'
import { MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from '@/features/knowledge/uploadLimits'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  uploadDocument: vi.fn(),
  getDocument: vi.fn(),
}))

vi.mock('@/api/shares', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/shares')>()),
  listShares: vi.fn(),
  grantShare: vi.fn(),
  revokeShare: vi.fn(),
}))

vi.mock('@/api/dataSources', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/dataSources')>()),
  listDataSources: vi.fn(),
  createDataSource: vi.fn(),
  setDataSourceEnabled: vi.fn(),
  deleteDataSource: vi.fn(),
  syncDataSource: vi.fn(),
}))

vi.mock('@/api/search', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/search')>()),
  search: vi.fn(),
}))

import { getDocument, uploadDocument } from '@/api/documents'
import { grantShare, listShares, revokeShare } from '@/api/shares'
import { resetProviderStore, setProviderStatusForTest } from '@/api/provider'
import {
  createDataSource,
  deleteDataSource,
  listDataSources,
  setDataSourceEnabled,
  syncDataSource,
} from '@/api/dataSources'
import { search } from '@/api/search'

const uploadMock = vi.mocked(uploadDocument)
const getDocMock = vi.mocked(getDocument)
const listSharesMock = vi.mocked(listShares)
const grantMock = vi.mocked(grantShare)
const revokeMock = vi.mocked(revokeShare)
const listSourcesMock = vi.mocked(listDataSources)
const createSourceMock = vi.mocked(createDataSource)
const toggleSourceMock = vi.mocked(setDataSourceEnabled)
const deleteSourceMock = vi.mocked(deleteDataSource)
const syncSourceMock = vi.mocked(syncDataSource)
const searchMock = vi.mocked(search)
const successToast = vi.mocked(toast.success)
const warningToast = vi.mocked(toast.warning)

/** 造一个指定大小的文件（不真占内存）。 */
function makeFile(name: string, size: number): File {
  const file = new File(['x'], name, { type: 'text/plain' })
  Object.defineProperty(file, 'size', { value: size })
  return file
}

function renderDialog(node: React.ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>)
}

beforeEach(() => {
  vi.clearAllMocks()
  resetProviderStore()
})

afterEach(() => {
  vi.clearAllMocks()
  resetProviderStore()
})

describe('上传弹窗', () => {
  it('**上限与格式从握手取**（M3 阶段 6，R8）：提供者说 5MB / 收 epub+md，界面就按它走', async () => {
    const user = userEvent.setup()
    setProviderStatusForTest({
      state: 'ready',
      available: true,
      reason: '',
      checked_at: '2026-10-03T10:00:00Z',
      base_url: 'http://nas:8000/api/v1',
      credential: 'configured',
      protocol_version: 1,
      capabilities: { ingest: { max_bytes: 5 * 1024 * 1024, extensions: ['epub', 'md'] } },
    })

    renderDialog(<UploadDialog open kbId="kb-1" onClose={vi.fn()} onUploaded={vi.fn()} />)

    // 提示跟着握手变（不再是硬编码的 200MB / "PDF、Word…"）
    expect(await screen.findByText(/支持 EPUB 与 Markdown。单个文件不超过 5MB/)).toBeInTheDocument()
    // 本地拒收的判据也用它
    await user.upload(screen.getByLabelText('选择文件'), makeFile('大.epub', 6 * 1024 * 1024))
    expect(await screen.findByText('超过 5MB 上限，请先压缩或切分')).toBeInTheDocument()
  })

  it('超过单文件上限：本地就标为「未接收」且不发请求（不等后端读完才回 413）', async () => {
    const user = userEvent.setup()
    renderDialog(<UploadDialog open kbId="kb-1" onClose={vi.fn()} onUploaded={vi.fn()} />)

    await user.upload(screen.getByLabelText('选择文件'), makeFile('超大.pdf', MAX_UPLOAD_BYTES + 1))

    expect(await screen.findByText('未接收')).toBeInTheDocument()
    expect(screen.getByText(`超过 ${MAX_UPLOAD_MB}MB 上限，请先压缩或切分`)).toBeInTheDocument()
    // 没有待上传项 → 主按钮不可用，也就发不出请求
    expect(screen.getByRole('button', { name: /开始上传/ })).toBeDisabled()
  })

  it('恰好等于上限要放行——边界上的 `>` 与 `>=` 差一个字节，写错了没人看得出来', async () => {
    const user = userEvent.setup()
    renderDialog(<UploadDialog open kbId="kb-1" onClose={vi.fn()} onUploaded={vi.fn()} />)

    await user.upload(screen.getByLabelText('选择文件'), makeFile('刚好.pdf', MAX_UPLOAD_BYTES))

    expect(await screen.findByText('待上传')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始上传（1）/ })).toBeEnabled()
  })

  it('同一批里的同名同大小只进一次，并说一句免得用户以为界面吞了文件', async () => {
    const user = userEvent.setup()
    renderDialog(<UploadDialog open kbId="kb-1" onClose={vi.fn()} onUploaded={vi.fn()} />)

    await user.upload(screen.getByLabelText('选择文件'), [
      makeFile('a.txt', 10),
      makeFile('a.txt', 10),
    ])

    expect(await screen.findByText('「a.txt」重复选择，只加入清单一次')).toBeInTheDocument()
    expect(screen.getAllByText('a.txt')).toHaveLength(1)
  })

  it('逐文件记录三种结果（成功 / 重复 / 失败），并且串行发出', async () => {
    const order: string[] = []
    uploadMock.mockImplementation(async (_kbId, file) => {
      order.push(file.name)
      if (file.name === 'dup.txt') {
        return { document: {} as never, is_duplicate: true, task_id: null }
      }
      if (file.name === 'bad.txt') throw new Error('解析服务不可用')
      return { document: {} as never, is_duplicate: false, task_id: 't-1' }
    })
    const onUploaded = vi.fn()
    const user = userEvent.setup()
    renderDialog(
      <UploadDialog open kbId="kb-1" folderId="f-1" onClose={vi.fn()} onUploaded={onUploaded} />,
    )

    await user.upload(screen.getByLabelText('选择文件'), [
      makeFile('ok.txt', 10),
      makeFile('dup.txt', 20),
      makeFile('bad.txt', 30),
    ])
    await user.click(screen.getByRole('button', { name: /开始上传（3）/ }))

    await waitFor(() => expect(uploadMock).toHaveBeenCalledTimes(3))
    // 串行而不是并发：后端摄入是 CPU 密集的，同时跑只会让每个都更慢
    expect(order).toEqual(['ok.txt', 'dup.txt', 'bad.txt'])
    // 上传目标目录原样带上（选中目录时新文档要落在那个目录里）
    expect(uploadMock).toHaveBeenNthCalledWith(1, 'kb-1', expect.any(File), 'f-1')

    expect(await screen.findByText('已提交')).toBeInTheDocument()
    expect(screen.getByText('重复，已跳过')).toBeInTheDocument()
    expect(screen.getByText('解析服务不可用')).toBeInTheDocument()
    // 结果清单留在屏幕上；宿主被通知去刷新列表
    expect(onUploaded).toHaveBeenCalled()
  })

  it('失败的可以重试，清单可以清空；不提供逐文件的切块参数（那是库级属性）', async () => {
    uploadMock.mockRejectedValue(new Error('网络抖动'))
    const user = userEvent.setup()
    renderDialog(<UploadDialog open kbId="kb-1" onClose={vi.fn()} onUploaded={vi.fn()} />)

    await user.upload(screen.getByLabelText('选择文件'), makeFile('a.txt', 10))
    await user.click(screen.getByRole('button', { name: /开始上传（1）/ }))

    expect(await screen.findByRole('button', { name: /重试失败项（1）/ })).toBeInTheDocument()
    // 刻意不做的事：这里没有块长/重叠之类的输入（切块策略是知识库级的）
    expect(screen.queryByRole('slider')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/块长/)).not.toBeInTheDocument()

    // 清空清单：传过结果之后才出现（这份清单本身就是给人逐个处理的）
    await user.click(screen.getByRole('button', { name: '清空清单' }))
    expect(screen.getByText('还没有选择文件')).toBeInTheDocument()

    // 重试：把失败项放回待上传（本地就拒的不进重试，这里只有网络失败那一条）
    await user.upload(screen.getByLabelText('选择文件'), makeFile('a.txt', 10))
    await user.click(screen.getByRole('button', { name: /开始上传（1）/ }))
    await user.click(await screen.findByRole('button', { name: /重试失败项（1）/ }))
    expect(await screen.findByText('待上传')).toBeInTheDocument()
  })
})

describe('分享弹窗', () => {
  it('打开时拉一次名单：显示名 + 登录名 + 当前档位', async () => {
    listSharesMock.mockResolvedValue({
      items: [
        { user_id: 'u-1', username: 'lin', name: '林工', permission: 'read', created_at: null },
      ],
    })
    renderDialog(<ShareDialog open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)

    expect(await screen.findByText('林工')).toBeInTheDocument()
    expect(screen.getByText('lin')).toBeInTheDocument()
    expect(listSharesMock).toHaveBeenCalledWith('kb-1')
    // 档位下拉是 `@/ui/select`（Radix）：触发器是 combobox，当前值显示在它的文本里
    expect(screen.getByRole('combobox', { name: '调整 林工 的访问档位' })).toHaveTextContent('只读')
  })

  it('按登录名授出：空名字给中文原因，填了就把档位一起发出去', async () => {
    listSharesMock.mockResolvedValue({ items: [] })
    grantMock.mockResolvedValue({
      user_id: 'u-2',
      username: 'zhao',
      name: '赵工',
      permission: 'write',
      created_at: null,
    })
    const user = userEvent.setup()
    renderDialog(<ShareDialog open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)
    await screen.findByText('还没有分享给任何人。这个库目前只有你自己（和管理员）能看到。')

    await user.click(screen.getByRole('button', { name: '分享' }))
    expect(await screen.findByText('请填写对方的登录名')).toBeInTheDocument()
    expect(grantMock).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('对方的登录名'), 'zhao')
    await user.click(screen.getByRole('combobox', { name: '访问档位' }))
    await user.click(await screen.findByRole('option', { name: '可写' }))
    await user.click(screen.getByRole('button', { name: '分享' }))

    await waitFor(() => expect(grantMock).toHaveBeenCalledWith('kb-1', 'zhao', 'write'))
    expect(successToast).toHaveBeenCalledWith('已分享给 赵工（zhao）')
  })

  it('档位在行内改（不产生权限真空），收回不做二次确认', async () => {
    const share = {
      user_id: 'u-1',
      username: 'lin',
      name: '林工',
      permission: 'read' as const,
      created_at: null,
    }
    listSharesMock.mockResolvedValue({ items: [share] })
    grantMock.mockResolvedValue({ ...share, permission: 'write' })
    revokeMock.mockResolvedValue(undefined)
    const user = userEvent.setup()
    renderDialog(<ShareDialog open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)
    await screen.findByText('林工')

    // 行内改档位：点开下拉、选「可写」，与首次授出走同一条接口
    await user.click(screen.getByRole('combobox', { name: '调整 林工 的访问档位' }))
    await user.click(await screen.findByRole('option', { name: '可写' }))
    await waitFor(() => expect(grantMock).toHaveBeenCalledWith('kb-1', 'lin', 'write'))

    await user.click(screen.getByRole('button', { name: '收回' }))
    await waitFor(() => expect(revokeMock).toHaveBeenCalledWith('kb-1', 'u-1'))
    expect(successToast).toHaveBeenCalledWith('已收回「林工」的访问')
  })
})

describe('数据源面板', () => {
  it('登记与拉取分开：登记只建源，不顺手抓一遍', async () => {
    listSourcesMock.mockResolvedValue({ items: [] })
    createSourceMock.mockResolvedValue({
      id: 's-1',
      knowledge_base_id: 'kb-1',
      kind: 'rss',
      name: '周刊',
      url: 'https://example.com/feed.xml',
      max_items: null,
      enabled: true,
      etag: null,
      last_pulled_at: null,
    })
    const user = userEvent.setup()
    renderDialog(<SourcePanel kbId="kb-1" canWrite onChanged={vi.fn()} />)
    await screen.findByText('还没有数据源')

    await user.click(screen.getByRole('button', { name: '添加数据源' }))
    await user.type(
      screen.getByLabelText('名称（可选）').parentElement
        ? screen.getByLabelText('名称（可选）')
        : screen.getByLabelText('名称（可选）'),
      '周刊',
    )
    await user.type(screen.getByLabelText('地址'), '  https://example.com/feed.xml ')
    await user.click(screen.getByRole('button', { name: '登记' }))

    // 全量并发下这条要等一次**真实的数据源拉取**（单跑实测 984ms ✓，
    // 已经贴着 `waitFor`/`findBy*` 默认的 1000ms 等待器 ✗ → 全量并行时擦线超时 ✗）。
    // 所以只把**这一处**等待放宽到 3s ✓ —— **不动全量 `testTimeout`** ✗、
    // 其余 `findBy*` 一律不动 ✗（这条用例的定性是"抖动"，不是断言过期 ✗）。
    await waitFor(
      () =>
        expect(createSourceMock).toHaveBeenCalledWith('kb-1', {
          kind: 'rss',
          name: '周刊',
          url: 'https://example.com/feed.xml',
        }),
      { timeout: 3000, interval: 50 },
    )
    expect(syncSourceMock).not.toHaveBeenCalled()
  })

  it('立即拉取：报"取回 / 新入库 / 重复"三个数，并通知宿主刷新文档', async () => {
    listSourcesMock.mockResolvedValue({
      items: [
        {
          id: 's-1',
          knowledge_base_id: 'kb-1',
          kind: 'rss',
          name: '周刊',
          url: 'https://example.com/feed.xml',
          max_items: null,
          enabled: true,
          etag: null,
          last_pulled_at: null,
        },
      ],
    })
    syncSourceMock.mockResolvedValue({
      task_id: null,
      source_id: 's-1',
      fetched: 20,
      created: 3,
      duplicates: 17,
      not_modified: false,
      errors: [],
    })
    const onChanged = vi.fn()
    const user = userEvent.setup()
    renderDialog(<SourcePanel kbId="kb-1" canWrite onChanged={onChanged} />)
    await screen.findByText('周刊')

    await user.click(screen.getByRole('button', { name: /立即拉取/ }))

    await waitFor(() => expect(syncSourceMock).toHaveBeenCalledWith('s-1', true))
    expect(successToast).toHaveBeenCalledWith('取回 20 条，新入库 3 条，跳过重复 17 条')
    expect(onChanged).toHaveBeenCalled()
  })

  it('停用/启用与删除：删除要确认，并说明已抓的文档保留', async () => {
    listSourcesMock.mockResolvedValue({
      items: [
        {
          id: 's-1',
          knowledge_base_id: 'kb-1',
          kind: 'html',
          name: '文档页',
          url: 'https://example.com/page',
          max_items: null,
          enabled: true,
          etag: null,
          last_pulled_at: null,
        },
      ],
    })
    toggleSourceMock.mockResolvedValue({
      id: 's-1',
      knowledge_base_id: 'kb-1',
      kind: 'html',
      name: '文档页',
      url: 'https://example.com/page',
      max_items: null,
      enabled: false,
      etag: null,
      last_pulled_at: null,
    })
    deleteSourceMock.mockResolvedValue(undefined)
    const user = userEvent.setup()
    renderDialog(<SourcePanel kbId="kb-1" canWrite onChanged={vi.fn()} />)
    await screen.findByText('文档页')

    await user.click(screen.getByRole('button', { name: '停用' }))
    await waitFor(() => expect(toggleSourceMock).toHaveBeenCalledWith('s-1', false))

    await user.click(screen.getByRole('button', { name: '删除 文档页' }))
    expect(await screen.findByText(/已抓进来的文档会保留/)).toBeInTheDocument()
    // 确认弹窗是 `@/ui/alert-dialog`（Radix）：role 是 alertdialog
    const dialog = screen.getByRole('alertdialog', { name: '删除数据源' })
    await user.click(within(dialog).getByRole('button', { name: '确定' }))
    await waitFor(() => expect(deleteSourceMock).toHaveBeenCalledWith('s-1'))
  })

  it('只读分享：说明为什么没有操作入口，而不是让按钮点了才报 403', async () => {
    listSourcesMock.mockResolvedValue({ items: [] })
    renderDialog(<SourcePanel kbId="kb-1" canWrite={false} onChanged={vi.fn()} />)

    expect(await screen.findByText(/只读分享：你可以查看这里的数据源/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '添加数据源' })).not.toBeInTheDocument()
  })
})

describe('库内检索面板', () => {
  it('检索参数按面板上的选择送出，并把命中按"来自哪条通道"摊开', async () => {
    searchMock.mockResolvedValue({
      hits: [
        {
          chunk_id: 'c-1',
          document_id: 'doc-1',
          document_name: '说明书.pdf',
          knowledge_base_id: 'kb-1',
          text: '这是命中的一段',
          score: 0.812,
          page: 3,
          heading_path: '第一章',
          image_ids: [],
          channels: ['vector', 'fulltext'],
          ranks: { vector: 2, fulltext: 5 },
          raw_scores: { vector: 0.71, fulltext: 12.5 },
          rerank_score: null,
        },
      ],
      mode: 'hybrid',
      reranked: false,
      filtered_out: 2,
      stats: [{ channel: 'vector', count: 40, elapsed_ms: 12.5 }],
      embedding_configured: false,
      embedding_is_development: false,
    })
    const user = userEvent.setup()
    renderDialog(<KbSearchPanel open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)

    await user.type(screen.getByLabelText('检索内容'), '怎么安装')
    await user.click(screen.getByRole('button', { name: '检索' }))

    await waitFor(() =>
      expect(searchMock).toHaveBeenCalledWith({
        query: '怎么安装',
        kb_ids: ['kb-1'],
        mode: 'hybrid',
        top_k: 8,
        candidate_k: 40,
        rerank: false,
      }),
    )
    await waitFor(() =>
      expect(document.querySelector('.kb-hit-summary')?.textContent).toContain('1 条命中'),
    )
    expect(screen.getByText('说明书.pdf')).toBeInTheDocument()
    expect(screen.getByText('0.812')).toBeInTheDocument()
    expect(screen.getByText(/元数据过滤掉 2 条/)).toBeInTheDocument()
    // 没配嵌入模型：必须明说"向量召回不可信"，否则用户会把兜底结果当成真实效果
    expect(screen.getByText(/本次只做了关键词检索/)).toBeInTheDocument()
  })

  it('检索失败：一行人话 + 一个重试按钮；重试按那一轮的原词再发一次', async () => {
    searchMock.mockRejectedValueOnce(new Error('服务内部错误'))
    const user = userEvent.setup()
    renderDialog(<KbSearchPanel open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)

    const box = screen.getByLabelText('检索内容')
    await user.type(box, '怎么安装')
    await user.click(screen.getByRole('button', { name: '检索' }))

    // 说清"哪一步没成" + 后端给的原因，不是孤零零一个词（评审 51 号图：只有红字、没有出路）
    expect(await screen.findByText('检索失败：服务内部错误')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()

    // 用户已经把框里的字改了：重试仍按**那一条历史**的词发（失败的那条指着原来那个问题）
    await user.clear(box)
    await user.type(box, '换个词')
    searchMock.mockResolvedValue({
      hits: [],
      mode: 'hybrid',
      reranked: false,
      filtered_out: 0,
      stats: [],
      embedding_configured: true,
      embedding_is_development: false,
    })
    await user.click(screen.getByRole('button', { name: '重试' }))

    await waitFor(() =>
      expect(searchMock).toHaveBeenLastCalledWith(expect.objectContaining({ query: '怎么安装' })),
    )
    expect(await screen.findByText('没有命中')).toBeInTheDocument()
  })

  it('网络层失败：把浏览器那句英文翻成人话，不端给用户', async () => {
    searchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const user = userEvent.setup()
    renderDialog(<KbSearchPanel open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)

    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    // "服务挂了"与"网断了"是两件事：重试的意义也不同（一个可能等一下就好，一个多半立刻能成）
    expect(await screen.findByText('检索失败：网络没连上')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('空内容不发请求，给一句中文提示', async () => {
    const user = userEvent.setup()
    renderDialog(<KbSearchPanel open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(warningToast).toHaveBeenCalledWith('请输入检索内容')
    expect(searchMock).not.toHaveBeenCalled()
  })
})

describe('/documents/:id 跳板', () => {
  it('查出所属知识库后换到列表页并带上抽屉参数（引用与旧书签都靠这条不断链）', async () => {
    getDocMock.mockResolvedValue({
      id: 'doc-1',
      knowledge_base_id: 'kb-1',
      name: '说明书.pdf',
      source_kind: 'upload',
      stage: 'indexed',
      size_bytes: 1,
      mime_type: null,
      page_count: null,
      is_split: false,
      error: null,
      chunk_count: 0,
      uploaded_by: null,
      uploaded_by_name: '',
      folder_id: null,
      disabled: false,
      original_kind: 'pdf',
      created_at: null,
      updated_at: null,
      question_count: 0,
      questioned_chunk_count: 0,
      questions_pending: false,
      summary: '',
      progress: null,
    })
    render(
      <MemoryRouter initialEntries={['/documents/doc-1?page=4']}>
        <Routes>
          <Route path="/documents/:documentId" element={<DocumentView />} />
          <Route path="/kb/:kbId" element={<div>知识库列表页 doc=doc-1 page=4</div>} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByText(/知识库列表页 doc=doc-1 page=4/)).toBeInTheDocument()
    expect(getDocMock).toHaveBeenCalledWith('doc-1')
  })

  it('打不开时把原因显示出来，而不是永远停在"正在打开文档…"', async () => {
    getDocMock.mockRejectedValue(new Error('文档不存在'))
    render(
      <MemoryRouter initialEntries={['/documents/doc-9']}>
        <Routes>
          <Route path="/documents/:documentId" element={<DocumentView />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByText('文档不存在')).toBeInTheDocument()
  })
})
