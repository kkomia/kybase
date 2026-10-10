/**
 * 检索面板的「过滤条件」区：条件下沉到检索层之后加的界面入口。
 *
 * 这一批钉的是四件**行为**，不是版式：
 *
 * 1. 勾了文档就把 `document_ids` 发出去（用户最要的"只查某本书"），页码/标题/入库时间
 *    一起进同一个 `filters` 对象——条件是**一组**，不是几次检索；
 * 2. 页码/日期填错就地拦下：**不发请求**、给字段级提示（后端页码那一项是 `ge=1`，
 *    让它打一趟 422 再回来解释，比在这里说一句慢得多）；
 * 3. 条件要留下看得见的痕迹：过滤区收起时摘要与清除仍在（否则用户只看到"命中怎么少了"），
 *    清除之后请求里**不再带 `filters`**；
 * 4. `filtered_out` 恒为 0 这件事要如实反映：带条件时那句"元数据过滤掉 N 条"换成
 *    "已按条件限定范围"——报 0 等于说"一条都没筛掉"，而命中却少了一大截。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { KbSearchPanel } from '@/features/knowledge'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/search', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/search')>()),
  search: vi.fn(),
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  listDocuments: vi.fn(),
}))

import { listDocuments, type DocumentSummary } from '@/api/documents'
import { search, type SearchResponse } from '@/api/search'

const searchMock = vi.mocked(search)
const listDocumentsMock = vi.mocked(listDocuments)
const warningToast = vi.mocked(toast.warning)

/** 多选只要 id 与名字，其余字段与这几条用例无关。 */
const DOCUMENT_ROWS = [
  { id: 'doc-1', name: '内科学.pdf' },
  { id: 'doc-2', name: '外科手册.docx' },
] as unknown as DocumentSummary[]

function emptyResponse(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return {
    summary: '',
    distribution: null,
    decision: null,
    hits: [],
    mode: 'hybrid',
    reranked: false,
    filtered_out: 0,
    offset: 0,
    next_offset: null,
    stats: [],
    embedding_configured: true,
    embedding_is_development: false,
    ...overrides,
  }
}

function renderPanel() {
  return render(
    <MemoryRouter>
      <KbSearchPanel open kbId="kb-1" kbName="产品手册" onClose={vi.fn()} />
    </MemoryRouter>,
  )
}

/** 展开过滤区（默认收起）。 */
async function openFilters(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(screen.getByRole('button', { name: /过滤条件/ }))
}

beforeEach(() => {
  vi.clearAllMocks()
  listDocumentsMock.mockResolvedValue({
    items: DOCUMENT_ROWS,
    total: DOCUMENT_ROWS.length,
    limit: 200,
    offset: 0,
  })
})

describe('检索面板的过滤条件', () => {
  it('勾了文档就把 document_ids 发出去，页码/标题/入库时间同走一个 filters', async () => {
    searchMock.mockResolvedValue(emptyResponse())
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    // 候选来自当前库的文档清单（显示名在这里，值是 document_id）
    await user.click(await screen.findByLabelText('内科学.pdf'))
    await user.type(screen.getByLabelText('起始页'), '3')
    await user.type(screen.getByLabelText('结束页'), '20')
    await user.type(screen.getByLabelText('章节标题包含'), '第3章')
    // 日期输入只能整段设值（原生 `type=date` 不接受逐个字符的中途值）
    fireEvent.change(screen.getByLabelText('入库时间起'), { target: { value: '2025-01-01' } })
    fireEvent.change(screen.getByLabelText('入库时间止'), { target: { value: '2025-01-31' } })

    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    await waitFor(() =>
      expect(searchMock).toHaveBeenCalledWith(
        expect.objectContaining({
          query: '近视',
          kb_ids: ['kb-1'],
          filters: {
            document_ids: ['doc-1'],
            page_from: 3,
            page_to: 20,
            heading_contains: '第3章',
            // 日期只到"天"，但边界要落到当天首尾：直接发 `2025-01-31` 的话上界在当天零点，
            // "起止填同一天"就会一条都查不到（边界错误在界面上看不出来）
            created_after: '2025-01-01T00:00:00',
            created_before: '2025-01-31T23:59:59.999999',
          },
        }),
      ),
    )
  })

  it('只填一头也发：只填起 = 从这页往后，只填止 = 到这页为止', async () => {
    searchMock.mockResolvedValue(emptyResponse())
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    await user.type(screen.getByLabelText('起始页'), '40')
    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    await waitFor(() => expect(searchMock).toHaveBeenCalledOnce())
    expect(searchMock.mock.calls[0]?.[0]?.filters).toEqual({ page_from: 40 })
    expect(screen.getByText('第 40 页起')).toBeInTheDocument()

    // 反过来只填"止"：这一头就只发 page_to
    await user.clear(screen.getByLabelText('起始页'))
    await user.type(screen.getByLabelText('结束页'), '25')
    await user.click(screen.getByRole('button', { name: '检索' }))

    await waitFor(() => expect(searchMock).toHaveBeenCalledTimes(2))
    expect(searchMock.mock.calls[1]?.[0]?.filters).toEqual({ page_to: 25 })
    expect(screen.getByText('到第 25 页')).toBeInTheDocument()
  })

  it('结束页小于起始页：不发请求，提示挂在页码那一格', async () => {
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    await user.type(screen.getByLabelText('起始页'), '20')
    await user.type(screen.getByLabelText('结束页'), '3')
    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(await screen.findByText('结束页不能小于起始页')).toBeInTheDocument()
    expect(warningToast).toHaveBeenCalledWith('结束页不能小于起始页')
    expect(searchMock).not.toHaveBeenCalled()
  })

  it('页码填 0 也拦下（后端那一项是 ≥1），不换一次 422 回来', async () => {
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    await user.type(screen.getByLabelText('起始页'), '0')
    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(await screen.findByText('起始页要填 1 或更大的整数')).toBeInTheDocument()
    expect(searchMock).not.toHaveBeenCalled()
  })

  it('入库时间的止早于起：同样不发请求', async () => {
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    fireEvent.change(screen.getByLabelText('入库时间起'), { target: { value: '2025-03-01' } })
    fireEvent.change(screen.getByLabelText('入库时间止'), { target: { value: '2025-01-01' } })
    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(await screen.findByText('结束日期不能早于起始日期')).toBeInTheDocument()
    expect(searchMock).not.toHaveBeenCalled()
  })

  it('摘要与一键清除：收起过滤区之后仍看得出"带着条件在查"', async () => {
    searchMock.mockResolvedValue(emptyResponse())
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    await user.click(await screen.findByLabelText('内科学.pdf'))
    await user.type(screen.getByLabelText('章节标题包含'), '第3章')

    // 收起：格子收起来了，摘要（含文档名）与清除还在
    await openFilters(user)
    expect(document.querySelector('.kb-filter-body')).toBeNull()
    expect(screen.getByText('文档：内科学.pdf')).toBeInTheDocument()
    expect(screen.getByText('标题含「第3章」')).toBeInTheDocument()

    // 清除：摘要随即消失
    await user.click(screen.getByRole('button', { name: '清除' }))
    expect(document.querySelector('.kb-filter-chips')).toBeNull()

    // 清掉之后那一趟请求里**不再带 filters**（不筛与"筛了个空"是两件事）
    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))
    await waitFor(() => expect(searchMock).toHaveBeenCalledOnce())
    expect(searchMock.mock.calls[0]?.[0]?.filters).toBeUndefined()
  })

  it('页码与标题那两格常驻一句取舍提示（没有页码/标题的块会被排除）', async () => {
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)

    expect(
      screen.getByText('没有页码的块会被排除（Markdown、Office 来源常见）'),
    ).toBeInTheDocument()
    expect(screen.getByText('标题为空的块会被排除')).toBeInTheDocument()
    // 填错时那一行换成错误提示（两句话不叠在一起）
    await user.type(screen.getByLabelText('起始页'), '20')
    await user.type(screen.getByLabelText('结束页'), '3')
    expect(screen.queryByText('没有页码的块会被排除（Markdown、Office 来源常见）')).toBeNull()
  })

  it('带条件那一轮：命中摘要说"已按条件限定范围"，不再报"过滤掉 0 条"', async () => {
    searchMock.mockResolvedValue(emptyResponse({ filtered_out: 0 }))
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    await user.click(await screen.findByLabelText('内科学.pdf'))
    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(await screen.findByText('已按条件限定范围')).toBeInTheDocument()
    expect(screen.queryByText(/过滤掉/)).toBeNull()
    // 一条都没命中时的出路也要跟着换：这时"换个关键词"不是唯一的原因
    expect(screen.getByText('清除过滤条件，或换关键词重试。')).toBeInTheDocument()

    // 清掉条件再查一轮：老口径照旧（`filtered_out` > 0 才报，报的是后端给的数）
    searchMock.mockResolvedValue(emptyResponse({ filtered_out: 2 }))
    await user.click(screen.getByRole('button', { name: '清除' }))
    await user.click(screen.getByRole('button', { name: '检索' }))

    expect(await screen.findByText('元数据过滤掉 2 条')).toBeInTheDocument()
    expect(screen.queryByText('已按条件限定范围')).toBeNull()
  })

  it('文档清单读不到：那一格如实说明，检索照旧能用', async () => {
    listDocumentsMock.mockRejectedValue(new TypeError('Failed to fetch'))
    searchMock.mockResolvedValue(emptyResponse())
    const user = userEvent.setup()
    renderPanel()

    await openFilters(user)
    // 网络层失败翻成人话（与检索失败那一行同一口径），不是端给用户一句英文
    expect(await screen.findByText('网络没连上')).toBeInTheDocument()

    await user.type(screen.getByLabelText('检索内容'), '近视')
    await user.click(screen.getByRole('button', { name: '检索' }))
    await waitFor(() => expect(searchMock).toHaveBeenCalledOnce())
  })
})
