/**
 * 模型注册器（旧 `components/settings/ModelRegistryPanel.vue`）的用例。
 *
 * 四条与密钥纪律、归属整理直接相关的口径：
 * 1. **只显示掩码**，不回显密钥；编辑时那一栏留空（留空 = 不改）；
 * 2. **模型行上仍显示「用于向量化」**（只读状态：删它之前要知道会影响什么）；
 * 3. **预设只填表单、不落库**：地址与名称以表单里的为准；
 * 4. **加模型的候选来自上游探测 + 预设建议**，探测失败要给"可手写"的出路。
 *
 * 本仓补的一组（`/models` 那一页的「用途」一节）钉另外几件事：换绑 / 解绑发的是哪个
 * 槽位端点、测槽的结论就地摊出来、以及**成员看得到现状但看不到写入口**。
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/modelRegistry', () => ({
  getRegistry: vi.fn(),
  listAvailableModels: vi.fn(async () => ({ models: [], count: 0 })),
  createProvider: vi.fn(),
  updateProvider: vi.fn(),
  deleteProvider: vi.fn(),
  registerModel: vi.fn(),
  updateModel: vi.fn(),
  deleteModel: vi.fn(),
  testProvider: vi.fn(),
  bindSlot: vi.fn(),
  testSlot: vi.fn(),
}))

import {
  bindSlot,
  createProvider,
  deleteModel,
  getRegistry,
  listAvailableModels,
  registerModel,
  testProvider,
  testSlot,
} from '@/api/modelRegistry'
import { ModelRegistryPanel } from '@/features/misc/settings/ModelRegistryPanel'
import { ModelsPage } from '@/features/misc/settings/ModelsPage'
import { renderMisc } from '@/features/misc/testing/harness'
import { resetToasts } from '@/features/misc/shared/toast'
import { useSessionStore } from '@/lib/session'

const getRegistryMock = vi.mocked(getRegistry)
const createProviderMock = vi.mocked(createProvider)
const registerModelMock = vi.mocked(registerModel)
const listAvailableMock = vi.mocked(listAvailableModels)
const testProviderMock = vi.mocked(testProvider)
const deleteModelMock = vi.mocked(deleteModel)
const bindSlotMock = vi.mocked(bindSlot)
const testSlotMock = vi.mocked(testSlot)

function registry() {
  return {
    providers: [
      {
        id: 'p1',
        kind: 'llm',
        name: '深度求索',
        base_url: 'https://api.deepseek.com',
        enabled: true,
        created_at: null,
        updated_at: null,
        api_key_configured: true,
        api_key_hint: 'sk-xu…ten',
        model_count: 1,
      },
    ],
    models: [
      {
        id: 'm1',
        provider_id: 'p1',
        provider_name: '深度求索',
        provider_kind: 'llm',
        model_id: 'deepseek-chat',
        label: '对话主力',
        dim: null,
        capabilities: ['chat'],
        options: {},
        created_at: null,
        updated_at: null,
        bound_slots: ['chat'],
      },
    ],
    slots: [
      {
        slot: 'chat',
        label: '对话',
        capability: 'chat',
        bound_model_pk: 'm1',
        bound_model_label: '对话主力',
        provider_name: '深度求索',
        configured: true,
        source: 'registry' as const,
      },
    ],
    provider_kinds: { llm: '对话 / 通用' },
    capabilities: { chat: '对话', embedding: '向量化' },
    provider_presets: [
      {
        id: 'deepseek',
        label: '深度求索',
        kind: 'llm',
        base_url: 'https://api.deepseek.com',
        hint: '在开放平台 → API Keys 里拿一把',
        models: [
          { model_id: 'deepseek-chat', label: '对话主力', capabilities: ['chat'], dim: null },
        ],
      },
    ],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  getRegistryMock.mockResolvedValue(registry() as never)
  listAvailableMock.mockResolvedValue({ models: [], count: 0 })
  createProviderMock.mockResolvedValue(registry().providers[0] as never)
})

describe('模型注册器', () => {
  it('供应商卡片只留名字 + 掩码密钥，模型行标出「用于对话」', async () => {
    renderMisc(<ModelRegistryPanel />)

    expect(await screen.findByText('深度求索')).toBeInTheDocument()
    expect(screen.getByText('sk-xu…ten')).toBeInTheDocument()
    // 2026-09-27（界面优化计划 5.8）：卡片上原先还有「类别」与「N 个模型」，
    // 下面另起一行印 base_url——三样都没有区分度（这一列所有供应商同一类、
    // 数量下面数得出来），base_url 还是实现细节。用户圈着这块说
    // "同一个东西说两遍"，所以这里改成**钉"它们不在了"**：
    expect(screen.queryByText('1 个模型')).not.toBeInTheDocument()
    expect(screen.queryByText('对话模型服务')).not.toBeInTheDocument()
    expect(screen.queryByText('https://api.deepseek.com')).not.toBeInTheDocument()
    // 掩码密钥留着：同一个供应商配了几把钥匙时，它是唯一能分辨的那一条
    // 删模型之前要知道会影响什么：这一行是**只读状态**
    expect(screen.getByText('用于对话')).toBeInTheDocument()
  })

  it('选预设只填表单，提交时把预设填好的名称与地址一起发出去', async () => {
    renderMisc(<ModelRegistryPanel />)
    await userEvent.click(await screen.findByRole('button', { name: '添加供应商' }))

    // 下拉已经是 @/ui/select（Radix）：先点开触发器，再点选项
    const presetTrigger = screen.getByLabelText('供应商预设')
    expect(presetTrigger).toHaveAttribute('data-slot', 'select-trigger')
    await userEvent.click(presetTrigger)
    await userEvent.click(await screen.findByRole('option', { name: '深度求索' }))

    expect(screen.getByLabelText('名称')).toHaveValue('深度求索')
    expect(screen.getByLabelText('接口地址')).toHaveValue('https://api.deepseek.com')
    expect(screen.getByText('在开放平台 → API Keys 里拿一把')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '添加' }))

    await waitFor(() =>
      expect(createProviderMock).toHaveBeenCalledWith({
        kind: 'llm',
        name: '深度求索',
        base_url: 'https://api.deepseek.com',
        api_key: '',
      }),
    )
  })

  it('加模型：上游探测失败也能手写，并带上勾选的能力', async () => {
    listAvailableMock.mockRejectedValueOnce(new Error('连接超时'))
    registerModelMock.mockResolvedValue(registry().models[0] as never)

    renderMisc(<ModelRegistryPanel />)
    await userEvent.click(await screen.findByRole('button', { name: /深度求索 的操作/ }))
    // 行内「⋯」菜单已经是 @/ui/dropdown-menu（Radix）：点开才有菜单，项按 menuitem 查
    expect(await screen.findByRole('menu')).toHaveAttribute('data-slot', 'dropdown-menu-content')
    await userEvent.click(await screen.findByRole('menuitem', { name: '添加模型' }))

    // 探测失败不弹错误通知，只就地给一句提示，并指明手写这条出路
    expect(
      await screen.findByText(/拉取候选失败：连接超时。可直接输入模型 ID。/),
    ).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('模型 ID'), 'BAAI/bge-m3')
    await userEvent.click(screen.getByRole('checkbox', { name: '向量化' }))
    await userEvent.click(screen.getByRole('button', { name: '登记' }))

    await waitFor(() =>
      expect(registerModelMock).toHaveBeenCalledWith({
        provider_id: 'p1',
        model_id: 'BAAI/bge-m3',
        label: '',
        dim: null,
        capabilities: ['embedding'],
      }),
    )
  })

  it('测试连接失败：把供应商探活的结论原样报出来', async () => {
    testProviderMock.mockRejectedValueOnce(new Error('域名解析失败'))

    renderMisc(<ModelRegistryPanel />)
    await userEvent.click(await screen.findByRole('button', { name: /深度求索 的操作/ }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /测试连接/ }))

    expect(await screen.findByText('域名解析失败')).toBeInTheDocument()
  })

  it('删除模型前说清"它正被几个用途使用"（删除会自动解绑）', async () => {
    deleteModelMock.mockResolvedValue(undefined)

    renderMisc(<ModelRegistryPanel />)
    await userEvent.click(await screen.findByRole('button', { name: /对话主力 的操作/ }))
    await userEvent.click(await screen.findByRole('menuitem', { name: '删除' }))

    const dialog = await screen.findByRole('alertdialog')
    // 二次确认走 @/ui/alert-dialog：role 是 alertdialog，正文说明仍在
    expect(dialog).toHaveAttribute('data-slot', 'alert-dialog-content')
    expect(within(dialog).getByText('删除模型「对话主力」？')).toBeInTheDocument()
    expect(within(dialog).getByText(/它正被 1 个用途使用，删除后会自动解绑。/)).toBeInTheDocument()
    await userEvent.click(within(dialog).getByRole('button', { name: '删除' }))

    await waitFor(() => expect(deleteModelMock).toHaveBeenCalledWith('m1'))
  })
})

// ------------------------------------------------------------------ 用途（`/models` 那一页）

/**
 * 用途那一节要的注册表：三个槽位（对话生成已绑，另两个未绑）+ 两家的模型。
 *
 * 那位**停用供应商**（`p2`）底下的模型是有意放的：绑定候选必须把停用供应商的模型挡在外面
 * （绑上去只会换来一句 422），这条用例钉的就是它不在候选里。
 */
function registryWithSlots() {
  const base = registry()
  return {
    ...base,
    providers: [
      base.providers[0],
      { ...base.providers[0], id: 'p2', kind: 'embedding', name: '本地向量库', enabled: false },
    ],
    models: [
      base.models[0],
      {
        id: 'm2',
        provider_id: 'p1',
        provider_name: '深度求索',
        provider_kind: 'llm',
        model_id: 'BAAI/bge-m3',
        label: '向量主力',
        dim: 1024,
        capabilities: ['embedding'],
        options: {},
        created_at: null,
        updated_at: null,
        bound_slots: [],
      },
      {
        id: 'm3',
        provider_id: 'p2',
        provider_name: '本地向量库',
        provider_kind: 'embedding',
        model_id: 'bge-small',
        label: '',
        dim: 512,
        capabilities: ['embedding'],
        options: {},
        created_at: null,
        updated_at: null,
        bound_slots: [],
      },
    ],
    slots: [
      {
        slot: 'chat',
        label: '对话生成',
        capability: 'chat',
        bound_model_pk: 'm1',
        bound_model_label: '对话主力',
        provider_name: '深度求索',
        configured: true,
        source: 'registry' as const,
      },
      {
        slot: 'embedding',
        label: '向量化',
        capability: 'embedding',
        bound_model_pk: null,
        bound_model_label: '',
        provider_name: '',
        configured: false,
        source: 'none' as const,
      },
      {
        slot: 'rerank',
        label: '重排（可选）',
        capability: 'rerank',
        bound_model_pk: null,
        bound_model_label: '',
        provider_name: '',
        configured: false,
        source: 'none' as const,
      },
    ],
  }
}

/** 写端点只认管理员（后端 `require_admin`），两档视角各摆一次。 */
function asAdmin(): void {
  useSessionStore.setState({
    token: 'st',
    currentUser: { id: 'u1', username: 'admin', name: '管理员', role: 'admin', avatar_url: '' },
  })
}

function asMember(): void {
  useSessionStore.setState({
    token: 'st',
    currentUser: { id: 'u2', username: 'bob', name: '小博', role: 'member', avatar_url: '' },
  })
}

describe('模型页：用途绑定', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetToasts()
    useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
    asAdmin()
    getRegistryMock.mockResolvedValue(registryWithSlots() as never)
    bindSlotMock.mockResolvedValue(undefined as never)
    testSlotMock.mockResolvedValue({ ok: true, detail: '模型答话了' })
  })

  it('三个槽位各一行：当前绑定、未选定，以及「出题与摘要」那句都在', async () => {
    renderMisc(<ModelsPage />)

    expect(await screen.findByText('对话生成')).toBeInTheDocument()
    // chat 那一格必须点出它管出题与摘要——用户要设的就是这两件事用哪个大模型
    expect(screen.getByText('出题与摘要也走这一格')).toBeInTheDocument()
    expect(screen.getByText('向量化')).toBeInTheDocument()
    expect(screen.getByText('重排（可选）')).toBeInTheDocument()
    // 已绑的那一格写清「模型 · 供应商」（这一串同时出现在那枚标签与下拉的当前值上）；
    // 没绑的两格明说「未选定」，不留空
    expect(screen.getAllByText('对话主力 · 深度求索').length).toBeGreaterThan(0)
    expect(screen.getAllByText('未选定')).toHaveLength(2)
    // 没选模型的槽位测不了（后端也是这么判的：得先选定）
    expect(screen.getByRole('button', { name: '测试「向量化」' })).toBeDisabled()
  })

  it('换绑发的是那个槽位的端点；解绑发 null', async () => {
    renderMisc(<ModelsPage />)
    await screen.findByText('对话生成')

    await userEvent.click(screen.getByLabelText('向量化用哪个模型'))
    // 候选只有能力匹配、且供应商没停用的那一个
    expect(screen.queryByRole('option', { name: /bge-small/ })).not.toBeInTheDocument()
    await userEvent.click(await screen.findByRole('option', { name: '向量主力 · 深度求索' }))
    await waitFor(() => expect(bindSlotMock).toHaveBeenCalledWith('embedding', 'm2'))

    await userEvent.click(screen.getByLabelText('对话生成用哪个模型'))
    await userEvent.click(await screen.findByRole('option', { name: '不指定' }))
    await waitFor(() => expect(bindSlotMock).toHaveBeenCalledWith('chat', null))
  })

  it('测槽结果就地摊在那一行下面：通过与失败各一种', async () => {
    renderMisc(<ModelsPage />)
    await screen.findByText('对话生成')

    await userEvent.click(screen.getByRole('button', { name: '测试「对话生成」' }))
    await waitFor(() => expect(testSlotMock).toHaveBeenCalledWith('chat'))
    const ok = (await screen.findAllByText('模型答话了')).find((node) =>
      node.classList.contains('m-test-ok'),
    )
    expect(ok).toBeDefined()

    testSlotMock.mockRejectedValueOnce(new Error('模型没答话'))
    await userEvent.click(screen.getByRole('button', { name: '测试「对话生成」' }))
    const bad = (await screen.findAllByText('模型没答话')).find((node) =>
      node.classList.contains('m-test-bad'),
    )
    expect(bad).toBeDefined()
  })

  it('成员：现状照旧看得见，写入口一个都不渲染', async () => {
    asMember()
    renderMisc(<ModelsPage />)

    // 现状：槽位名、当前绑定、供应商与模型清单都还在（读端点成员也打得开）
    expect(await screen.findByText('对话生成')).toBeInTheDocument()
    expect(screen.getByText('对话主力 · 深度求索')).toBeInTheDocument()
    expect(screen.getByText('深度求索')).toBeInTheDocument()
    expect(screen.getByText('向量主力')).toBeInTheDocument()
    // 绑定下拉与测槽按钮：没有
    expect(screen.queryByLabelText('对话生成用哪个模型')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '测试「对话生成」' })).not.toBeInTheDocument()
    // 面板里的写入口（添加供应商 + 两处行内菜单）同样不渲染
    expect(screen.queryByRole('button', { name: '添加供应商' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /深度求索 的操作/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /对话主力 的操作/ })).not.toBeInTheDocument()
    // 也没打写请求
    expect(bindSlotMock).not.toHaveBeenCalled()
    expect(testSlotMock).not.toHaveBeenCalled()
  })
})
