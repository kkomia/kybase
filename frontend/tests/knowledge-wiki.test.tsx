/**
 * Wiki 页（`WikiView`）的用例。
 *
 * 对应旧用例 `frontend/tests/unit/views/WikiView.test.ts` 的逻辑型条目：
 * 库没开 Wiki 时给引导、总览与单页分开拉、点树切页、正文里的 `[n]` 跳到出处、
 * 已有页面时重新生成先确认、清除后把选中页从地址里摘掉。
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL as NodeURL } from 'node:url'
import { MemoryRouter, Route, Routes } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { WikiView } from '@/features/knowledge'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import type { KnowledgeBase } from '@/api/knowledgeBases'
import type { WikiOverview, WikiPageDetail } from '@/api/wiki'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/wiki', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/wiki')>()),
  getWiki: vi.fn(),
  getWikiPage: vi.fn(),
  generateWiki: vi.fn(),
  clearWiki: vi.fn(),
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

import { clearWiki, generateWiki, getWiki, getWikiPage } from '@/api/wiki'
import { listKnowledgeBases } from '@/api/knowledgeBases'

const overviewMock = vi.mocked(getWiki)
const pageMock = vi.mocked(getWikiPage)
const generateMock = vi.mocked(generateWiki)
const clearMock = vi.mocked(clearWiki)
const listKbMock = vi.mocked(listKnowledgeBases)
const successToast = vi.mocked(toast.success)

const KB: KnowledgeBase = {
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
  document_count: 5,
  last_activity: null,
}

function makeOverview(overrides: Partial<WikiOverview> = {}): WikiOverview {
  return {
    enabled: true,
    status: 'ready',
    page_count: 2,
    generated_at: '2026-09-20T10:00:00Z',
    model: 'k3',
    last_error: null,
    pages: [
      {
        id: 'page-1',
        parent_id: null,
        level: 0,
        ord: 0,
        title: '总览',
        brief: '一句话摘要',
        status: 'ready',
        generated_at: '2026-09-20T10:00:00Z',
      },
      {
        id: 'page-2',
        parent_id: 'page-1',
        level: 1,
        ord: 1,
        title: '安装',
        brief: '',
        status: 'generating',
        generated_at: null,
      },
    ],
    ...overrides,
  }
}

function makePage(overrides: Partial<WikiPageDetail> = {}): WikiPageDetail {
  return {
    id: 'page-1',
    kb_id: 'kb-1',
    parent_id: null,
    level: 0,
    title: '总览',
    brief: '一句话摘要',
    content_md: '正文第一句[1]。\n\n另见 [[安装]]。',
    status: 'ready',
    model: 'k3',
    generated_at: '2026-09-20T10:00:00Z',
    updated_at: null,
    sources: [
      {
        index: 1,
        chunk_id: 'chunk-1',
        document_id: 'doc-1',
        document_name: '说明书.pdf',
        heading_path: '第一章',
        page: 12,
      },
    ],
    ...overrides,
  }
}

function renderWiki(entry = '/kb/kb-1/wiki') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/kb/:kbId/wiki" element={<WikiView />} />
        <Route path="/kb/:kbId" element={<div>知识库主页</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  listKbMock.mockResolvedValue({ items: [KB] })
  overviewMock.mockResolvedValue(makeOverview())
  pageMock.mockImplementation(async (pageId: string) =>
    pageId === 'page-2'
      ? makePage({ id: 'page-2', title: '安装', status: 'generating', content_md: '安装步骤。' })
      : makePage(),
  )
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
})

describe('Wiki 页', () => {
  it('左侧是页面树、右侧是正文与出处；总览与单页分成两次请求', async () => {
    renderWiki()

    expect(await screen.findByRole('heading', { name: /总览/ })).toBeInTheDocument()
    // 总览只带标题/摘要，正文按选中页单独拉（书厚了也不会"等一整本传完"）
    expect(overviewMock).toHaveBeenCalledWith('kb-1')
    expect(pageMock).toHaveBeenCalledWith('page-1')
    expect(await screen.findByText('第一章 · 第 12 页')).toBeInTheDocument()
    expect(screen.getByText('页面 · 2')).toBeInTheDocument()
  })

  it('库没开 Wiki：给"去哪儿开"的引导，不是一个空树', async () => {
    overviewMock.mockResolvedValue(makeOverview({ enabled: false, status: 'idle', pages: [] }))
    renderWiki()

    expect(await screen.findByText('这个知识库还没有开启 Wiki')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '回到知识库' })).toBeInTheDocument()
    expect(pageMock).not.toHaveBeenCalled()
  })

  it('整库失败时把后端给的失败原因原样显示出来', async () => {
    overviewMock.mockResolvedValue(
      makeOverview({ status: 'failed', last_error: '模型额度不足', pages: [] }),
    )
    renderWiki()

    expect(await screen.findByText('模型额度不足')).toBeInTheDocument()
  })

  it('点树换页：只拉那一页的正文，并把它写进地址（刷新/分享都能回到同一页）', async () => {
    const user = userEvent.setup()
    renderWiki()
    await screen.findByRole('heading', { name: /总览/ })

    await user.click(screen.getByTitle('安装'))

    await waitFor(() => expect(pageMock).toHaveBeenCalledWith('page-2'))
    expect(await screen.findByText('安装步骤。')).toBeInTheDocument()
    // 子页在树里被缩进一级；这一页自己的状态（还在生成）在文章标题上露出来
    const rows = document.querySelectorAll('.kb-wiki-row')
    expect(rows[1]).toHaveStyle({ paddingLeft: 'calc(1 * var(--space-3))' })
    expect(within(rows[1] as HTMLElement).getByText('安装')).toBeInTheDocument()
    expect((await screen.findAllByText('生成中…')).length).toBeGreaterThan(0)
  })

  it('正文里的 [n] 是可点徽标：点了滚到对应出处并闪一下', async () => {
    const user = userEvent.setup()
    renderWiki()

    // 正文里的徽标与下面出处列表里的文件名同名，所以按类名取正文那一颗
    await screen.findByText(/正文第一句/)
    const cite = document.querySelector('.kb-cite') as HTMLElement
    expect(cite).toHaveAttribute('data-cite-index', '1')

    await user.click(cite)
    const source = document.querySelector('[data-source="1"]') as HTMLElement
    await waitFor(() => expect(source.className).toContain('kb-source-flash'))
  })

  it('站内双链 [[安装]] 直接换页（标题对不上时原样留着，不给死链）', async () => {
    const user = userEvent.setup()
    renderWiki()

    await screen.findByText(/另见/)
    const link = document.querySelector('.kb-wikilink') as HTMLElement
    expect(link).toHaveAttribute('data-wiki-page', 'page-2')
    await user.click(link)

    await waitFor(() => expect(pageMock).toHaveBeenCalledWith('page-2'))
  })

  it('已有页面时「重新生成」先确认会覆盖几篇，确认后才发起', async () => {
    generateMock.mockResolvedValue({ task_id: 't-1' })
    const user = userEvent.setup()
    renderWiki()
    await screen.findByRole('heading', { name: /总览/ })

    await user.click(screen.getByRole('button', { name: '重新生成' }))
    expect(await screen.findByText(/重新生成会覆盖现有的 2 篇页面/)).toBeInTheDocument()
    expect(generateMock).not.toHaveBeenCalled()

    // 确认弹窗里的那一颗（页头上那颗同名按钮还在）
    // 确认弹窗是 `@/ui/alert-dialog`（Radix）：role 是 alertdialog，不是 dialog
    const dialog = screen.getByRole('alertdialog', { name: '重新生成 Wiki' })
    await user.click(within(dialog).getByRole('button', { name: '重新生成' }))
    await waitFor(() => expect(generateMock).toHaveBeenCalledWith('kb-1'))
    expect(successToast).toHaveBeenCalledWith('已开始生成 Wiki，页面会陆续出现')
  })

  it('从未生成过：直接开始，不弹确认（没有可失去的）', async () => {
    overviewMock.mockResolvedValue(
      makeOverview({ status: 'idle', page_count: 0, pages: [], model: null, generated_at: null }),
    )
    generateMock.mockResolvedValue({ task_id: 't-1' })
    const user = userEvent.setup()
    renderWiki()

    // 空状态里那颗（页头上也有一颗同名按钮）
    await screen.findByText('还没有 Wiki 页面')
    const empty = document.querySelector('.kb-wiki-empty') as HTMLElement
    await user.click(within(empty).getByRole('button', { name: '生成 Wiki' }))
    await waitFor(() => expect(generateMock).toHaveBeenCalledWith('kb-1'))
  })

  it('生成中：状态标签说"生成中…"，按钮关掉，轮询只认落定后的状态', async () => {
    overviewMock.mockResolvedValue(makeOverview({ status: 'generating', pages: [] }))
    renderWiki()

    expect((await screen.findAllByText('生成中…')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '生成中…' })).toBeDisabled()
    expect(screen.getByText(/正在整理库里的内容/)).toBeInTheDocument()
  })

  it('清除：二次确认后调接口，并把已选中的页从地址里摘掉', async () => {
    clearMock.mockResolvedValue(undefined)
    const user = userEvent.setup()
    renderWiki('/kb/kb-1/wiki?page=page-1')
    await screen.findByRole('heading', { name: /总览/ })

    await user.click(screen.getByRole('button', { name: '清除' }))
    expect(await screen.findByText('确定清除这个知识库已生成的 Wiki 页面？')).toBeInTheDocument()

    const dialog = screen.getByRole('alertdialog', { name: '清除 Wiki 页面' })
    await user.click(within(dialog).getByRole('button', { name: '清除' }))

    await waitFor(() => expect(clearMock).toHaveBeenCalledWith('kb-1'))
    expect(successToast).toHaveBeenCalledWith('已清除 Wiki 页面')
  })

  it('面包屑不许折行：库名可压缩到省略号，箭头与 `Wiki` 永远留着', async () => {
    renderWiki()

    // 页标题这一行就是"库名 › Wiki"这条路径，折成两行路径语义就没了（评审 K18）
    const crumb = await screen.findByRole('heading', { level: 1 })
    expect(crumb).toHaveStyle({ display: 'flex', whiteSpace: 'nowrap' })
    const name = within(crumb).getByRole('link', { name: '产品手册' })
    expect(crumb.firstElementChild).toBe(name)
    expect(name).toHaveStyle({ overflow: 'hidden', textOverflow: 'ellipsis' })
    expect(within(crumb).getByText('Wiki')).toBeInTheDocument()
  })

  it('正文第一个标题与页面标题是同一句话时跳过它（评审 K19）', async () => {
    pageMock.mockResolvedValue(makePage({ content_md: '# 总览\n\n正文第一句[1]。' }))
    renderWiki()

    expect(await screen.findByText(/正文第一句/)).toBeInTheDocument()
    // 页头一行 + 正文一行本来是同一句话，现在只剩页头那一行
    expect(screen.getAllByRole('heading', { name: '总览' })).toHaveLength(1)
  })

  it('正文首个标题与页面标题不同（或开篇不是标题）时原样渲染，不吃正文', async () => {
    pageMock.mockResolvedValue(makePage({ content_md: '## 本页要点\n\n正文第一句[1]。' }))
    renderWiki()

    expect(await screen.findByText(/正文第一句/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '本页要点' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { name: '总览' })).toHaveLength(1)
  })

  it('「主题」一节是真正的列表：条目各自成行，折行对齐条目首行（评审 K20）', async () => {
    pageMock.mockResolvedValue(
      makePage({ content_md: '## 主题\n\n- [[安装]]：一句话说明\n- 另一条说明\n' }),
    )
    const { container } = renderWiki()

    expect(await screen.findByText(/一句话说明/)).toBeInTheDocument()
    const list = container.querySelector('ul.kb-md-list') as HTMLElement
    expect(list).not.toBeNull()
    expect(list.querySelectorAll('li.kb-md-li')).toHaveLength(2)

    // 条目能不能被看成"列表"全靠符号，而符号是 CSS 给的：Tailwind 的 preflight 把
    // `ol, ul, menu` 的 `list-style` 去掉了，这两条规则是"别再把符号弄丢"的护栏
    // （jsdom 不做布局，量不到符号，只能读源码——同 `notes-editor.test.tsx` 的做法）
    const css = stripComments(styleText())
    expect(cssRule(css, 'ul.kb-md-list')).toMatch(/list-style:\s*disc/)
    expect(cssRule(css, 'ol.kb-md-list')).toMatch(/list-style:\s*decimal/)
  })
})

/* --------------------------------------------------------------- 源码小工具 */

function styleText(): string {
  return readFileSync(
    fileURLToPath(new NodeURL('../src/features/knowledge/knowledge.css', import.meta.url)),
    'utf8',
  )
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '')
}

function cssRule(source: string, selector: string): string {
  const rules = [...source.matchAll(/\n\s*([^{}]+?)\s*\{([^{}]*)\}/g)]
  const found = rules.find((rule) =>
    (rule[1] ?? '').split(',').some((part) => part.trim() === selector),
  )
  expect(found, `没在源码里找到 CSS 规则 ${selector}`).toBeTruthy()
  return found?.[2] ?? ''
}
