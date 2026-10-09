/**
 * API 密钥页（`features/misc/settings/ApiKeysPage`）的用例。
 *
 * 这一页原来是个弹窗（`ApiKeysDialog`，挂在 `AccountMenu` 上），2026-10-09 搬成侧栏一页。
 * 用例跟着搬，并加上第三条收窄轴（工具范围）。覆盖这几件"丢了就出事"的事：
 *
 * 1. **列表渲染**：名称 / 前缀 / 权限档 / 库范围 / 工具范围 / 两个时间；
 * 2. **明文只出现一次**：创建成功后能看到、能复制；点「我已保存」后从界面上消失；
 * 3. **工具范围**：候选来自 `GET /tools`；不勾就是空数组（= 不限，与后端口径一致）；
 * 4. **撤销是二次确认**：确认框里写清"立即生效、在用的 401"，确认后才发请求；
 * 5. **403 态**：成员账号不发表单也不发请求；管理员但端点回 403 时归到同一个态。
 *
 * 侧栏那个入口（「API 密钥」导航项）由 `layout-shell.test.tsx` 钉着——这里是页面本身的用例。
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/apiKeys', () => ({
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}))

vi.mock('@/api/knowledgeBases', () => ({
  listKnowledgeBases: vi.fn(async () => ({
    items: [
      { id: 'kb-1', name: '产品手册' },
      { id: 'kb-2', name: '研发 Wiki' },
    ],
  })),
}))

vi.mock('@/api/tools', () => ({ getToolCatalogue: vi.fn() }))

vi.mock('@/lib/clipboard', () => ({
  copyText: vi.fn(async () => true),
  selectNode: vi.fn(() => true),
}))

import { createApiKey, listApiKeys, revokeApiKey, type ApiKey } from '@/api/apiKeys'
import { getToolCatalogue, type ToolCatalog } from '@/api/tools'
import { resetToasts } from '@/features/misc/shared/toast'
import { ApiKeysPage } from '@/features/misc/settings/ApiKeysPage'
import { renderMisc } from '@/features/misc/testing/harness'
import { copyText } from '@/lib/clipboard'
import { useSessionStore } from '@/lib/session'

const listApiKeysMock = vi.mocked(listApiKeys)
const createApiKeyMock = vi.mocked(createApiKey)
const revokeApiKeyMock = vi.mocked(revokeApiKey)
const getToolCatalogueMock = vi.mocked(getToolCatalogue)
const copyTextMock = vi.mocked(copyText)

function apiKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: 'key-1',
    name: '报表脚本',
    permission: 'readonly',
    knowledge_base_ids: [],
    allowed_tool_names: [],
    created_at: '2026-09-20T02:00:00Z',
    last_used_at: null,
    prefix: 'kylab_sk_ab12…',
    ...overrides,
  }
}

/**
 * 工具目录。三把工具是有意挑的：一把只读带必填参数、一把只读无参数（覆盖"无参数"那一格）、
 * 一把要写权限（覆盖权限列的两档）。
 */
function toolCatalog(): ToolCatalog {
  return {
    items: [
      {
        name: 'search',
        label: '检索',
        summary: '按库检索原文段落',
        requires_write: false,
        params: [
          { name: 'query', required: true, summary: '要检索的问题' },
          { name: 'knowledge_base_ids', required: false, summary: '' },
        ],
      },
      {
        name: 'get_document_content',
        label: '取原文',
        summary: '取一篇文档的正文',
        requires_write: false,
        params: [],
      },
      {
        name: 'delete_document',
        label: '删除文档',
        summary: '删掉一篇文档',
        requires_write: true,
        params: [{ name: 'document_id', required: true, summary: '' }],
      },
    ],
    mcp_endpoint: 'http://nas:8000/mcp',
    skill: {
      name: 'kylab-knowledge-base',
      summary: '在你的知识库里检索原文段落',
      path: 'skills/kylab-knowledge-base',
      transports: ['mcp', 'rest'],
    },
  }
}

/** 管理员视角（这一族端点只认管理员会话，成员那一档由 403 用例专门摆）。 */
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

function renderPage(): void {
  renderMisc(<ApiKeysPage />)
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
  asAdmin()
  listApiKeysMock.mockResolvedValue({ items: [apiKey()] })
  getToolCatalogueMock.mockResolvedValue(toolCatalog())
})

describe('列表', () => {
  it('列出密钥：名称、前缀、权限档、库范围、工具范围与两个时间都在', async () => {
    listApiKeysMock.mockResolvedValue({
      items: [
        apiKey(),
        apiKey({
          id: 'key-2',
          name: '同步服务',
          permission: 'readwrite',
          knowledge_base_ids: ['kb-1', 'kb-404'],
          allowed_tool_names: ['search'],
          last_used_at: '2026-10-08T02:00:00Z',
          prefix: 'kylab_sk_xy98…',
        }),
      ],
    })

    renderPage()

    expect(await screen.findByText('报表脚本')).toBeInTheDocument()
    expect(screen.getByText('kylab_sk_ab12…')).toBeInTheDocument()
    expect(screen.getByText('只读')).toBeInTheDocument()
    expect(screen.getByText('读写')).toBeInTheDocument()
    // 范围空 = 不限（照实写"全部知识库"）；非空按库名列出，认不出的库照实标
    expect(screen.getByText(/范围 全部知识库/)).toBeInTheDocument()
    expect(await screen.findByText(/产品手册、已删除的库/)).toBeInTheDocument()
    // 工具范围：空 = 全部（与库范围同一个写法）；非空按工具**标签**列出
    expect(screen.getByText(/工具 全部/)).toBeInTheDocument()
    expect(screen.getByText(/工具 检索/)).toBeInTheDocument()
    // 两个时间列：创建是绝对时间，最近使用是相对口径（没用过就是 —）
    expect(screen.getAllByText(/创建于/).length).toBe(2)
    expect(screen.getByText(/最近使用 —/)).toBeInTheDocument()
    expect(screen.getByText(/最近使用 .*前/)).toBeInTheDocument()
    // 行内动作带名字（两行的「撤销」靠 aria-label 分得清是哪一把）
    expect(screen.getByRole('button', { name: '撤销「报表脚本」' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '撤销「同步服务」' })).toBeInTheDocument()
  })

  it('空列表给空态与新建入口', async () => {
    listApiKeysMock.mockResolvedValue({ items: [] })

    renderPage()

    expect(await screen.findByText('还没有 API 密钥')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建密钥' })).toBeInTheDocument()
  })
})

describe('新建', () => {
  it('默认只读 + 两条范围都不限；明文只在创建成功后出现一次，「我已保存」后消失', async () => {
    listApiKeysMock.mockResolvedValue({ items: [] })
    createApiKeyMock.mockResolvedValue({
      ...apiKey({ id: 'key-9', name: 'CI 机器人' }),
      token: 'kylab_sk_plaintext-only-once',
    })

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: '新建密钥' }))

    await userEvent.type(screen.getByLabelText('名称'), 'CI 机器人')
    // 权限档不动（默认只读），范围各勾一个：候选分别来自知识库列表与工具目录（都是异步到）
    await userEvent.click(await screen.findByRole('button', { name: /产品手册/ }))
    await userEvent.click(await screen.findByRole('button', { name: /检索（search）/ }))
    await userEvent.click(screen.getByRole('button', { name: '创建' }))

    // mutationFn 会收到 react-query 追加的上下文第二参，所以只钉第一参（提交体）
    await waitFor(() => expect(createApiKeyMock).toHaveBeenCalled())
    expect(createApiKeyMock.mock.calls[0][0]).toEqual({
      name: 'CI 机器人',
      permission: 'readonly',
      knowledge_base_ids: ['kb-1'],
      allowed_tool_names: ['search'],
    })

    // 明文出现在"只此一次"那一块，文案照实说关掉就看不到
    expect(await screen.findByDisplayValue('kylab_sk_plaintext-only-once')).toBeInTheDocument()
    expect(screen.getByText(/关掉就再也看不到了/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '复制' }))
    expect(copyTextMock).toHaveBeenCalledWith('kylab_sk_plaintext-only-once')

    await userEvent.click(screen.getByRole('button', { name: '我已保存' }))
    // 明文从界面上消失，回到列表（创建成功那一刻列表已失效重拉）
    expect(screen.queryByDisplayValue('kylab_sk_plaintext-only-once')).not.toBeInTheDocument()
    await waitFor(() => expect(listApiKeysMock.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('不勾工具范围就发空数组（空 = 不限，与后端口径一致）', async () => {
    createApiKeyMock.mockResolvedValue({
      ...apiKey({ id: 'key-9', name: '全量脚本' }),
      token: 'kylab_sk_all-tools',
    })

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: '新建密钥' }))
    await userEvent.type(screen.getByLabelText('名称'), '全量脚本')
    await userEvent.click(screen.getByRole('button', { name: '创建' }))

    await waitFor(() => expect(createApiKeyMock).toHaveBeenCalled())
    expect(createApiKeyMock.mock.calls[0][0]).toEqual({
      name: '全量脚本',
      permission: 'readonly',
      knowledge_base_ids: [],
      allowed_tool_names: [],
    })
  })

  it('名称为空时「创建」不可点', async () => {
    listApiKeysMock.mockResolvedValue({ items: [] })

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: '新建密钥' }))

    expect(screen.getByRole('button', { name: '创建' })).toBeDisabled()
    expect(createApiKeyMock).not.toHaveBeenCalled()
  })
})

describe('撤销', () => {
  it('二次确认写明后果，确认后才发请求并刷新列表', async () => {
    revokeApiKeyMock.mockResolvedValue(undefined)

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: '撤销「报表脚本」' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText(/撤销立即生效/)).toBeInTheDocument()
    expect(within(dialog).getByText(/401/)).toBeInTheDocument()
    expect(revokeApiKeyMock).not.toHaveBeenCalled()

    await userEvent.click(within(dialog).getByRole('button', { name: '撤销' }))
    await waitFor(() => expect(revokeApiKeyMock).toHaveBeenCalledWith('key-1'))
    await waitFor(() => expect(listApiKeysMock.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('取消确认就不发请求', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: '撤销「报表脚本」' }))

    const dialog = await screen.findByRole('alertdialog')
    await userEvent.click(within(dialog).getByRole('button', { name: '取消' }))

    expect(revokeApiKeyMock).not.toHaveBeenCalled()
    // 列表还在（取消不该把列表也带走）
    expect(await screen.findByText('报表脚本')).toBeInTheDocument()
  })
})

describe('受限态（403）', () => {
  it('成员账号：显示需要管理员权限，不发表单、不发请求', async () => {
    asMember()

    renderPage()

    expect(await screen.findByText('需要管理员权限')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建密钥' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('名称')).not.toBeInTheDocument()
    expect(listApiKeysMock).not.toHaveBeenCalled()
    // 工具清单同理：不是管理员就不为它白打一趟
    expect(getToolCatalogueMock).not.toHaveBeenCalled()
  })

  it('管理员但端点回 403（角色在服务端被降了）：归到同一个态', async () => {
    const cause = new Error('需要管理员权限') as Error & { status?: number }
    cause.status = 403
    listApiKeysMock.mockRejectedValue(cause)

    renderPage()

    expect(await screen.findByText('需要管理员权限')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建密钥' })).not.toBeInTheDocument()
  })
})
