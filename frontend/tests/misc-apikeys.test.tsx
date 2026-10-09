/**
 * API 密钥管理（`features/misc/settings/ApiKeysDialog` + `AccountMenu` 入口）的用例。
 *
 * 覆盖这五件"丢了就出事"的事：
 * 1. **列表渲染**：名称 / 前缀 / 权限档 / 库范围（空 = 全部，非空显示库名）/ 两个时间；
 * 2. **明文只出现一次**：创建成功后那一态能看到、能复制；点「我已保存」后从界面上消失；
 * 3. **撤销是二次确认**：确认框里写清"立即生效、在用的 401"，确认后才发请求；
 * 4. **403 态**：成员账号不发表单也不发请求；管理员但端点回 403 时归到同一个态；
 * 5. **入口**：`AccountMenu` 里点「API 密钥」能打开弹窗。
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

vi.mock('@/lib/clipboard', () => ({
  copyText: vi.fn(async () => true),
  selectNode: vi.fn(() => true),
}))

import { createApiKey, listApiKeys, revokeApiKey, type ApiKey } from '@/api/apiKeys'
import { ApiKeysDialog } from '@/features/misc/settings/ApiKeysDialog'
import { resetToasts } from '@/features/misc/shared/toast'
import { renderMisc } from '@/features/misc/testing/harness'
import { AccountMenu } from '@/features/layout/AccountMenu'
import { copyText } from '@/lib/clipboard'
import { useSessionStore } from '@/lib/session'

const listApiKeysMock = vi.mocked(listApiKeys)
const createApiKeyMock = vi.mocked(createApiKey)
const revokeApiKeyMock = vi.mocked(revokeApiKey)
const copyTextMock = vi.mocked(copyText)

function apiKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: 'key-1',
    name: '报表脚本',
    permission: 'readonly',
    knowledge_base_ids: [],
    created_at: '2026-09-20T02:00:00Z',
    last_used_at: null,
    prefix: 'kylab_sk_ab12…',
    ...overrides,
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

function openDialog(): void {
  renderMisc(<ApiKeysDialog open onClose={() => {}} />)
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
  asAdmin()
  listApiKeysMock.mockResolvedValue({ items: [apiKey()] })
})

describe('列表', () => {
  it('列出密钥：名称、前缀、权限档、库范围与两个时间都在', async () => {
    listApiKeysMock.mockResolvedValue({
      items: [
        apiKey(),
        apiKey({
          id: 'key-2',
          name: '同步服务',
          permission: 'readwrite',
          knowledge_base_ids: ['kb-1', 'kb-404'],
          last_used_at: '2026-10-08T02:00:00Z',
          prefix: 'kylab_sk_xy98…',
        }),
      ],
    })

    openDialog()

    expect(await screen.findByText('报表脚本')).toBeInTheDocument()
    expect(screen.getByText('kylab_sk_ab12…')).toBeInTheDocument()
    expect(screen.getByText('只读')).toBeInTheDocument()
    expect(screen.getByText('读写')).toBeInTheDocument()
    // 范围空 = 不限（照实写"全部知识库"）；非空按库名列出，认不出的库照实标
    expect(screen.getByText(/全部知识库/)).toBeInTheDocument()
    expect(await screen.findByText(/产品手册、已删除的库/)).toBeInTheDocument()
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

    openDialog()

    expect(await screen.findByText('还没有 API 密钥')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建密钥' })).toBeInTheDocument()
  })
})

describe('新建', () => {
  it('默认只读 + 不限范围；明文只在创建成功后出现一次，「我已保存」后消失', async () => {
    listApiKeysMock.mockResolvedValue({ items: [] })
    createApiKeyMock.mockResolvedValue({
      ...apiKey({ id: 'key-9', name: 'CI 机器人' }),
      token: 'kylab_sk_plaintext-only-once',
    })

    openDialog()
    await userEvent.click(await screen.findByRole('button', { name: '新建密钥' }))

    await userEvent.type(screen.getByLabelText('名称'), 'CI 机器人')
    // 权限档不动（默认只读），范围勾一个库（候选由知识库列表接口来，异步到）
    await userEvent.click(await screen.findByRole('button', { name: /产品手册/ }))
    await userEvent.click(screen.getByRole('button', { name: '创建' }))

    // mutationFn 会收到 react-query 追加的上下文第二参，所以只钉第一参（提交体）
    await waitFor(() => expect(createApiKeyMock).toHaveBeenCalled())
    expect(createApiKeyMock.mock.calls[0][0]).toEqual({
      name: 'CI 机器人',
      permission: 'readonly',
      knowledge_base_ids: ['kb-1'],
    })

    // 明文出现在"只此一次"那一态，文案照实说关掉就看不到
    expect(await screen.findByDisplayValue('kylab_sk_plaintext-only-once')).toBeInTheDocument()
    expect(screen.getByText(/关掉就再也看不到了/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '复制' }))
    expect(copyTextMock).toHaveBeenCalledWith('kylab_sk_plaintext-only-once')

    await userEvent.click(screen.getByRole('button', { name: '我已保存' }))
    // 明文从界面上消失，回到列表（创建成功那一刻列表已失效重拉）
    expect(screen.queryByDisplayValue('kylab_sk_plaintext-only-once')).not.toBeInTheDocument()
    await waitFor(() => expect(listApiKeysMock.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('名称为空时「创建」不可点', async () => {
    listApiKeysMock.mockResolvedValue({ items: [] })

    openDialog()
    await userEvent.click(await screen.findByRole('button', { name: '新建密钥' }))

    expect(screen.getByRole('button', { name: '创建' })).toBeDisabled()
    expect(createApiKeyMock).not.toHaveBeenCalled()
  })
})

describe('撤销', () => {
  it('二次确认写明后果，确认后才发请求并刷新列表', async () => {
    revokeApiKeyMock.mockResolvedValue(undefined)

    openDialog()
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
    openDialog()
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

    openDialog()

    expect(await screen.findByText('需要管理员权限')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建密钥' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('名称')).not.toBeInTheDocument()
    expect(listApiKeysMock).not.toHaveBeenCalled()
  })

  it('管理员但端点回 403（角色在服务端被降了）：归到同一个态', async () => {
    const cause = new Error('需要管理员权限') as Error & { status?: number }
    cause.status = 403
    listApiKeysMock.mockRejectedValue(cause)

    openDialog()

    expect(await screen.findByText('需要管理员权限')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建密钥' })).not.toBeInTheDocument()
  })
})

describe('入口', () => {
  it('AccountMenu 的「API 密钥」打开弹窗', async () => {
    renderMisc(<AccountMenu />)

    await userEvent.click(screen.getByRole('button', { name: '账号：管理员' }))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'API 密钥' }))

    expect(await screen.findByRole('dialog', { name: 'API 密钥' })).toBeInTheDocument()
    // 打开即拉列表
    expect(await screen.findByText('报表脚本')).toBeInTheDocument()
  })
})
