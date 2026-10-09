/**
 * 知识库列表页（`KnowledgeBasesView`）与新建弹窗的用例。
 *
 * 旧用例里"逻辑型"的那几条逐条翻译过来（`frontend/tests/unit/views/KnowledgeBasesView.test.ts`、
 * `stores/knowledgeBases.test.ts`）：空状态只有一个入口、没有嵌入模型时入口禁用、
 * 汇总随列表一次带回、新建只发与默认值不同的字段。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeBasesView } from '@/features/knowledge'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import type { KnowledgeBase } from '@/api/knowledgeBases'
import type { Registry, RegisteredModel } from '@/api/modelRegistry'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/knowledgeBases', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/knowledgeBases')>()),
  listKnowledgeBases: vi.fn(),
  createKnowledgeBase: vi.fn(),
  updateKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
  getKnowledgeBaseImpact: vi.fn(),
}))

vi.mock('@/api/modelRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/modelRegistry')>()),
  getRegistry: vi.fn(),
}))

import { createKnowledgeBase, listKnowledgeBases } from '@/api/knowledgeBases'
import { getRegistry } from '@/api/modelRegistry'

const listMock = vi.mocked(listKnowledgeBases)
const createMock = vi.mocked(createKnowledgeBase)
const registryMock = vi.mocked(getRegistry)
const successToast = vi.mocked(toast.success)

function makeKB(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: 'kb-1',
    name: '产品手册',
    description: '说明书与常见问题',
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
    created_at: '2026-09-01T10:00:00Z',
    can_manage: true,
    can_write: true,
    document_count: 7,
    last_activity: '2026-09-20T10:00:00Z',
    ...overrides,
  }
}

const EMBEDDING_MODEL: RegisteredModel = {
  id: 'm-emb',
  provider_id: 'p1',
  provider_name: '本地',
  provider_kind: 'openai',
  model_id: 'bge-m3',
  label: 'bge-m3',
  dim: 1024,
  capabilities: ['embedding'],
  options: {},
  created_at: null,
  updated_at: null,
  bound_slots: ['embedding'],
}

function makeRegistry(models: RegisteredModel[]): Registry {
  return {
    providers: [
      {
        id: 'p1',
        kind: 'openai',
        name: '本地',
        base_url: 'http://localhost',
        enabled: true,
        created_at: null,
        updated_at: null,
        api_key_configured: true,
        api_key_hint: 'sk-…',
        model_count: models.length,
      },
    ],
    models,
    slots: [
      {
        slot: 'embedding',
        label: '向量化',
        capability: 'embedding',
        bound_model_pk: models[0]?.id ?? null,
        bound_model_label: models[0]?.label ?? '',
        provider_name: '本地',
        configured: models.length > 0,
        source: models.length > 0 ? 'registry' : 'none',
      },
    ],
    provider_kinds: {},
    capabilities: {},
    provider_presets: [],
  }
}

function renderView() {
  return render(
    <MemoryRouter initialEntries={['/knowledge-bases']}>
      <KnowledgeBasesView />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  registryMock.mockResolvedValue(makeRegistry([EMBEDDING_MODEL]))
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
})

describe('知识库列表', () => {
  it('渲染卡片：名称、文档数与最近更新都来自列表接口一次带回；模型串与空简介不上卡', async () => {
    listMock.mockResolvedValue({
      items: [
        makeKB(),
        makeKB({ id: 'kb-2', name: '没有简介的库', description: '', document_count: 3 }),
      ],
    })
    renderView()

    expect(await screen.findByText('产品手册')).toBeInTheDocument()
    expect(screen.getByText('没有简介的库')).toBeInTheDocument()
    expect(screen.getAllByText('7')).toHaveLength(1)
    // 汇总（文档数 / 最近更新）来自同一个响应，不再逐库拉一次文档列表
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('link', { name: /产品手册/ })).toHaveAttribute('href', '/kb/kb-1')

    // 嵌入模型串**不在卡片上**：它由供应商与库形态决定，同批库里是同一个串
    //（四张卡四个 bge-m3），逐库看它去「知识库设置 → 基本」
    expect(screen.queryByText('bge-m3')).not.toBeInTheDocument()
    // 没写简介就整行不渲染（原先摆一句「暂无简介」替空字段占一行）
    expect(screen.getByText('说明书与常见问题')).toBeInTheDocument()
    expect(screen.queryByText('暂无简介')).not.toBeInTheDocument()
  })

  it('空库给空状态，且**只有页头那一个**新建入口', async () => {
    listMock.mockResolvedValue({ items: [] })
    renderView()

    expect(await screen.findByText('还没有知识库')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /新建知识库/ })).toHaveLength(1)
  })

  it('加载中先给骨架屏，不先闪一次空状态', async () => {
    let resolve: (value: { items: KnowledgeBase[] }) => void = () => {}
    listMock.mockReturnValue(new Promise((done) => (resolve = done)))
    const { container } = renderView()

    // 骨架屏现在是 `@/ui/skeleton` 的块（预设由 `composites.tsx` 拼出来），
    // 所以按它的 `data-slot` 断言，而不是旧实现的 `.kb-skeleton-row`
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(4)
    expect(screen.queryByText('还没有知识库')).not.toBeInTheDocument()

    resolve({ items: [makeKB()] })
    expect(await screen.findByText('产品手册')).toBeInTheDocument()
  })

  it('列表失败时把后端文案显示出来，而不是一个空页面', async () => {
    listMock.mockRejectedValue(new Error('服务暂时不可用'))
    renderView()

    expect(await screen.findByText('服务暂时不可用')).toBeInTheDocument()
  })

  it('没有可用的嵌入模型：页头就说清原因且新建入口禁用（不让用户白填一遍）', async () => {
    listMock.mockResolvedValue({ items: [] })
    registryMock.mockResolvedValue(makeRegistry([]))
    renderView()

    expect(await screen.findByText(/还没有可用的嵌入模型/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /新建知识库/ })).toBeDisabled()
  })

  it('超过 12 个容器切回行形态：格子被挤窄之后"挑一个"反而更慢', async () => {
    listMock.mockResolvedValue({
      items: Array.from({ length: 13 }, (_, index) =>
        makeKB({ id: `kb-${index}`, name: `库 ${index}` }),
      ),
    })
    const { container } = renderView()

    await waitFor(() => expect(container.querySelector('.kb-card')).toBeNull())
    expect(container.querySelectorAll('.kb-row-link')).toHaveLength(13)
  })
})

describe('新建知识库', () => {
  it('不勾不做：默认值一律不发，让服务端默认成为唯一的默认', async () => {
    listMock.mockResolvedValue({ items: [] })
    createMock.mockResolvedValue(makeKB({ name: '新库' }))
    const user = userEvent.setup()
    renderView()

    await user.click(await screen.findByRole('button', { name: /新建知识库/ }))
    await user.type(screen.getByLabelText('名称'), ' 新库 ')
    await waitFor(() => expect(screen.getByRole('button', { name: '创建' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: '创建' }))

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1))
    // 默认的块长/重叠/出题开关/库形态一个都不发
    const payload = createMock.mock.calls[0][0] as unknown as Record<string, unknown>
    expect(payload.name).toBe('新库')
    for (const key of ['chunk_size', 'chunk_overlap', 'suggested_enabled', 'wiki_enabled']) {
      expect(key in payload).toBe(false)
    }
    expect(successToast).toHaveBeenCalledWith('已创建知识库「新库」')
  })

  it('选了「向量检索 + Wiki」才发 wiki_enabled，动了块长才发 chunk_size', async () => {
    listMock.mockResolvedValue({ items: [] })
    createMock.mockResolvedValue(makeKB({ name: '带 Wiki 的库' }))
    const user = userEvent.setup()
    renderView()

    await user.click(await screen.findByRole('button', { name: /新建知识库/ }))
    await user.type(screen.getByLabelText('名称'), '带 Wiki 的库')
    await user.click(screen.getByLabelText(/向量检索 \+ Wiki/))
    // 折叠区里的滑杆一直在 DOM 里（不靠展开动作才拿得到）
    fireEvent.change(screen.getByRole('slider', { name: '块长' }), { target: { value: '1024' } })

    await waitFor(() => expect(screen.getByRole('button', { name: '创建' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: '创建' }))

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1))
    const payload = createMock.mock.calls[0][0] as unknown as Record<string, unknown>
    expect(payload).toMatchObject({ name: '带 Wiki 的库', chunk_size: 1024, wiki_enabled: true })
    // 没动过的项仍然不发（重叠与出题那几项）
    expect('chunk_overlap' in payload).toBe(false)
    expect('suggested_enabled' in payload).toBe(false)
  })

  it('每次打开都回到默认值：上一座库改过的参数不该被下一座继承', async () => {
    listMock.mockResolvedValue({ items: [] })
    const user = userEvent.setup()
    renderView()

    await user.click(await screen.findByRole('button', { name: /新建知识库/ }))
    fireEvent.change(screen.getByRole('slider', { name: '块长' }), { target: { value: '2048' } })
    expect(screen.getByRole('slider', { name: '块长' })).toHaveValue('2048')

    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: /新建知识库/ }))

    expect(screen.getByRole('slider', { name: '块长' })).toHaveValue('512')
  })
})
