/**
 * 登录页（旧 `views/LoginView.vue`）与 404 的用例。
 *
 * 三条与后端约定直接相关的口径：
 * 1. `/auth/status` 的 `needs_setup` 决定这一页是**首次设置**还是登录（不开放注册）；
 * 2. 密码下限与后端 `MIN_PASSWORD_CHARS` 对齐，两次不一致要**在本地就拦住**；
 * 3. 登录成功后令牌立刻写进 `lib/session`（下一次请求就带上，不必刷新页面）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({
  getAuthBootstrapStatus: vi.fn(),
  login: vi.fn(),
  setup: vi.fn(),
  logout: vi.fn(),
  me: vi.fn(),
  changePassword: vi.fn(),
  MIN_PASSWORD_CHARS: 8,
}))

import { getAuthBootstrapStatus, login, me, setup } from '@/api/auth'
import { NotFoundPage } from '@/features/misc/auth/NotFoundPage'
import { LoginPage } from '@/features/misc/auth/LoginPage'
import { renderMisc } from '@/features/misc/testing/harness'
import { resetToasts } from '@/features/misc/shared/toast'
import { SESSION_TOKEN_STORAGE_KEY, useSessionStore } from '@/lib/session'

const statusMock = vi.mocked(getAuthBootstrapStatus)
const loginMock = vi.mocked(login)
const setupMock = vi.mocked(setup)
const meMock = vi.mocked(me)

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
  statusMock.mockResolvedValue({ needs_setup: false })
  // 带上凭据进这一页时会验一次 `/auth/me`（失败即清本地令牌）
  meMock.mockResolvedValue({
    id: 'u1',
    username: 'admin',
    name: '管理员',
    role: 'admin',
    avatar_url: '',
  })
})

describe('登录页', () => {
  it('已有账号时是登录态：只有用户名 + 密码两个字段', async () => {
    renderMisc(<LoginPage />)

    expect(await screen.findByRole('heading', { name: '登录' })).toBeInTheDocument()
    expect(screen.getByLabelText('用户名')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
    expect(screen.queryByLabelText('显示名')).not.toBeInTheDocument()
  })

  it('品牌位是字标（与侧栏/欢迎态同一个），标题下没有解释性小字', async () => {
    renderMisc(<LoginPage />)

    // 字标用仓库里那个 `Logo`：可读名就是它自带的产品名
    expect(await screen.findByRole('img', { name: 'KYLAB 知识库' })).toBeInTheDocument()
    // 规范 §5.1：标题下那行"用管理员为你开通的账号登录。"已删
    expect(screen.queryByText(/用管理员为你开通的账号登录/)).not.toBeInTheDocument()
  })

  it('needs_setup 时变成首次设置：补显示名与确认密码，并在本地拦住两次不一致', async () => {
    statusMock.mockResolvedValue({ needs_setup: true })

    renderMisc(<LoginPage />)
    expect(await screen.findByRole('heading', { name: '创建管理员账号' })).toBeInTheDocument()

    const { user } = { user: userEvent.setup() }
    await user.type(screen.getByLabelText(/用户名/), 'admin')
    await user.type(screen.getByLabelText('密码'), 'longenough1')
    await user.type(screen.getByLabelText('确认密码'), 'longenough2')
    await user.click(screen.getByRole('button', { name: '创建并进入' }))

    expect(await screen.findByText('两次输入的密码不一致')).toBeInTheDocument()
    expect(setupMock).not.toHaveBeenCalled()

    await user.clear(screen.getByLabelText('确认密码'))
    await user.type(screen.getByLabelText('确认密码'), 'longenough1')
    setupMock.mockResolvedValue({
      token: 'kylab_st_abc',
      user: { id: 'u1', username: 'admin', name: '管理员', role: 'admin', avatar_url: '' },
    })
    await user.click(screen.getByRole('button', { name: '创建并进入' }))

    await waitFor(() => expect(setupMock).toHaveBeenCalledWith('admin', 'longenough1', undefined))
  })

  it('登录失败把后端那句话原样显示，且不写会话令牌', async () => {
    loginMock.mockRejectedValueOnce(new Error('用户名或密码不正确'))

    renderMisc(<LoginPage />)
    await userEvent.type(await screen.findByLabelText('用户名'), 'admin')
    await userEvent.type(screen.getByLabelText('密码'), 'whatever1')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('用户名或密码不正确')).toBeInTheDocument()
    expect(window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY)).toBeNull()
    expect(useSessionStore.getState().token).toBe('')
  })

  it('登录失败时才说明"账号要管理员开通"（原来那行常驻说明搬到了这里）', async () => {
    loginMock.mockRejectedValueOnce(new Error('用户名或密码不正确'))

    renderMisc(<LoginPage />)
    // 还没提交：这句话不该在页面上
    expect(await screen.findByRole('heading', { name: '登录' })).toBeInTheDocument()
    expect(screen.queryByText(/账号由管理员在设置里开通/)).not.toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('用户名'), 'admin')
    await userEvent.type(screen.getByLabelText('密码'), 'whatever1')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText(/账号由管理员在设置里开通/)).toBeInTheDocument()
  })

  it('首次设置（needs_setup）不背登录那套提示：导语仍在，错误里不出现"管理员开通"', async () => {
    statusMock.mockResolvedValue({ needs_setup: true })

    renderMisc(<LoginPage />)
    expect(await screen.findByRole('heading', { name: '创建管理员账号' })).toBeInTheDocument()
    expect(screen.getByText(/第一次使用：创建管理员账号即可进入/)).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText(/用户名/), 'admin')
    await userEvent.click(screen.getByRole('button', { name: '创建并进入' }))

    expect(await screen.findByText('请填写密码')).toBeInTheDocument()
    expect(screen.queryByText(/账号由管理员在设置里开通/)).not.toBeInTheDocument()
  })

  it('登录成功后令牌与账号立刻落进 session（下一次请求就带上新凭据）', async () => {
    loginMock.mockResolvedValue({
      token: 'kylab_st_xyz',
      user: { id: 'u1', username: 'admin', name: '管理员', role: 'admin', avatar_url: '' },
    })

    renderMisc(<LoginPage />, { route: '/login?redirect=/tasks' })
    await userEvent.type(await screen.findByLabelText('用户名'), 'admin')
    await userEvent.type(screen.getByLabelText('密码'), 'longenough1')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => expect(useSessionStore.getState().token).toBe('kylab_st_xyz'))
    expect(useSessionStore.getState().currentUser?.username).toBe('admin')
    expect(window.localStorage.getItem(SESSION_TOKEN_STORAGE_KEY)).toBe('kylab_st_xyz')
  })
})

describe('404', () => {
  it('一句标题 + 两个出口，不再有第二行同义标题', () => {
    renderMisc(<NotFoundPage />)

    expect(screen.getByRole('heading', { name: '页面不存在' })).toBeInTheDocument()
    // 原来那句空态标题「没有找到这个地址」与页标题说同一件事：合并之后不再出现
    expect(screen.queryByText('没有找到这个地址')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '回到概览' })).toHaveAttribute('href', '/')
    // 第二条出路：从哪儿点错的就退回哪儿
    expect(screen.getByRole('button', { name: '返回上一页' })).toBeInTheDocument()
  })

  it('「返回上一页」真的退回上一个地址', async () => {
    render(
      <MemoryRouter initialEntries={['/', '/no-such-page']} initialIndex={1}>
        <Routes>
          <Route path="/" element={<div>概览页</div>} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await userEvent.click(screen.getByRole('button', { name: '返回上一页' }))
    expect(await screen.findByText('概览页')).toBeInTheDocument()
  })
})
