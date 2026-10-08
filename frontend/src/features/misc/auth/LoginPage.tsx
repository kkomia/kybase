/**
 * 登录 / 首次初始化（v10 账号体系的前端入口）
 * ——与旧前端 `views/LoginView.vue` 逐条对应。
 *
 * 一个页面两种模式，由 `/auth/status` 的 `needs_setup` 决定：
 * - **首次设置**：还没有任何可登录账号 → 创建管理员（用户名 + 显示名 + 密码 + 确认）；
 * - **登录**：已有账号 → 用户名 + 密码。
 *
 * 为什么不做成注册页：本产品面向个人与家庭自托管，**不开放注册**——
 * 首个用户即管理员，其余账号由管理员在设置里开通。
 *
 * 会话写入走 `lib/session`（`setSessionToken` + `currentUser`），与其它页面读的是同一份，
 * 所以登录成功后不需要刷新页面，下一次请求就带上了新凭据。
 *
 * 视觉上刻意只留一条窄列：登录页没有可"扫视比较"的内容，
 * 界面越像一份表越好——品牌名、标题、两个字段、一个按钮。
 *
 * **品牌位改用字标**（界面评审 L3/L4：此前三处三种标识——登录页那个"箭头插进方括号"
 * 的图标（语义接近"退出"）、侧栏的环行星、欢迎态的「kylab」字标）。现在统一到仓库里
 * **已有的**字标组件（`chat/ui/Logo` 的 `wordmark`，与欢迎态同一个），
 * 登录页只放它，不再有第二个图形标。
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router'

import { getAuthBootstrapStatus, login, me, setup, MIN_PASSWORD_CHARS } from '@/api/auth'
import { clearSessionToken, isUnauthorized, setSessionToken, useSessionStore } from '@/lib/session'

import { Button } from '@/ui/button'
import { Input } from '@/ui/input'
import { Logo } from '@/features/chat/ui/Logo'
import { Field } from '../shared/composites'

/** 登录后回到用户原本想去的页面（守卫在 query 里带了 redirect）。 */
function redirectTarget(raw: string | null): string {
  return raw && raw.startsWith('/') ? raw : '/'
}

export function LoginPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const currentUser = useSessionStore((store) => store.currentUser)
  const token = useSessionStore((store) => store.token)

  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [localError, setLocalError] = useState('')

  const status = useQuery({
    queryKey: ['auth', 'bootstrap'],
    queryFn: getAuthBootstrapStatus,
    retry: false,
  })
  const isSetup = status.data?.needs_setup === true

  const target = redirectTarget(searchParams.get('redirect'))

  /**
   * 已持有凭据时不再要求登录；守卫通常已经拦下了，这里是页内二次确认。
   *
   * 验一次 `/auth/me`（失败即清本地令牌）而不是直接放行：令牌可能已过期或被吊销，
   * 直接跳走会让用户看到一串 401。
   */
  useEffect(() => {
    if (isSetup || !token) return
    let cancelled = false
    void (async () => {
      try {
        const account = await me()
        if (cancelled) return
        // 拿不到账号就当令牌失效：**不能把 undefined 写进 store**，
        // 那会让 `isAdmin` 与页头账号一起变成空，而界面还以为登录着。
        // 这是**服务器明确说"没有这个账号"**（200 但没内容），所以在这里直接清。
        if (!account?.id) {
          clearSessionToken()
          return
        }
        useSessionStore.setState({ currentUser: account })
        await navigate(target, { replace: true })
      } catch (error) {
        if (cancelled) return
        // **只有 401 才清令牌**（D07，2026-09-28 走查）：网络不通 / 超时 / 5xx
        // 都不是"你的登录过期了"——那时候把令牌删掉，网好了还得重新输密码。
        // 判据是共享的那一条（`error.status`，见 `lib/session.ts::isUnauthorized`）。
        if (isUnauthorized(error)) clearSessionToken()
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSetup, token])

  const submit = useMutation({
    mutationFn: async () => {
      const name = username.trim()
      if (!name) throw new Error('请填写用户名')
      if (!password) throw new Error('请填写密码')
      if (isSetup) {
        if (password.length < MIN_PASSWORD_CHARS) {
          throw new Error(`密码至少 ${MIN_PASSWORD_CHARS} 个字符`)
        }
        if (password !== confirmPassword) throw new Error('两次输入的密码不一致')
        return setup(name, password, displayName.trim() || undefined)
      }
      return login(name, password)
    },
    onSuccess: async (result) => {
      // 会话令牌只在这一次响应里出现：立刻落本地，丢了只能重新登录
      setSessionToken(result.token)
      useSessionStore.setState({ currentUser: result.user })
      await queryClient.invalidateQueries()
      // 登录之后引导状态已经变了（needs_setup 不再为真），让守卫读到新的一份
      await status.refetch()
      await navigate(target, { replace: true })
    },
    onError: (error: unknown) =>
      setLocalError(error instanceof Error ? error.message : '操作失败，请重试'),
  })

  return (
    <div className="m-login">
      <div className="m-login-panel">
        {/* 字标跟正文色（`Logo` 里全是 `currentColor`），不另给品牌色——
            它在三处（登录页 / 侧栏 / 欢迎态）必须是同一个东西 */}
        <div className="flex items-center text-text-primary">
          <Logo variant="wordmark" size={34} />
        </div>

        <h1 className="m-login-title">{isSetup ? '创建管理员账号' : '登录'}</h1>
        {/* 登录态**没有**标题下那行说明（规范 §5.1 删的就是这一类"这一页是什么"的小字）：
            "账号要管理员开通"是失败时才需要知道的事，所以它并进下面的错误提示里。 */}
        {/*
          首态那一句**保留**（它是"第一次使用该点哪儿"的落点，不是解释登录页是什么），
          但后半句"已有的知识库会归到这个账号名下"删掉——那是在讲账号建好之后会发生什么，
          2026-09-24 按用户要求去掉解释性的那半句。
          第一句保持逐字不变：`tests/misc-auth.test.tsx` 断的就是它（那条用例不在本泳道，
          不该由这里改）。
        */}
        {isSetup && <p className="m-login-hint">第一次使用：创建管理员账号即可进入。</p>}

        <form
          className="m-form"
          onSubmit={(event) => {
            event.preventDefault()
            setLocalError('')
            submit.mutate()
          }}
        >
          <Field label="用户名" htmlFor="login-username">
            <Input
              id="login-username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder={isSetup ? '由你决定，例如 admin' : ''}
              autoComplete="username"
              disabled={submit.isPending}
            />
          </Field>

          {isSetup && (
            <Field label="显示名" optional htmlFor="login-name">
              <Input
                id="login-name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="界面上显示的名字，留空则用用户名"
                disabled={submit.isPending}
              />
            </Field>
          )}

          <Field label="密码" htmlFor="login-password">
            <Input
              id="login-password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={isSetup ? `至少 ${MIN_PASSWORD_CHARS} 个字符` : ''}
              autoComplete="current-password"
              disabled={submit.isPending}
            />
          </Field>

          {isSetup && (
            <Field label="确认密码" htmlFor="login-confirm">
              <Input
                id="login-confirm"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
                disabled={submit.isPending}
              />
            </Field>
          )}

          {localError && (
            <p className="m-login-error" role="alert">
              {localError}
              {/* 本产品不开放注册：**登录失败时**才把"账号从哪来"说清楚
                  （原来是标题下的一行常驻说明，规范 §5.1 那一类）。 */}
              {!isSetup && (
                <span className="mt-1 block text-text-secondary">
                  账号由管理员在设置里开通，请联系管理员。
                </span>
              )}
            </p>
          )}

          <Button className="w-full" type="submit" disabled={submit.isPending}>
            {submit.isPending ? '请稍候…' : isSetup ? '创建并进入' : '登录'}
          </Button>
        </form>

        {currentUser && !isSetup && <p className="m-login-hint">当前已登录：{currentUser.name}</p>}
      </div>
    </div>
  )
}
