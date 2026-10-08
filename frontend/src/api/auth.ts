/**
 * 账号与登录接口（`/api/v1/auth`，v10 起的主路径）。
 *
 * 两种凭据的关系（后端 `app/api/auth.py` 注释是权威）：
 * - **登录会话**（`kylab_st_` 前缀）：控制台唯一的凭据，用户名+密码换来，7 天滑动续期；
 * - **API Key**：给外部程序，进不了设置页，也不能当管理员。
 *
 * 这里所有调用都带 `authFailure: 'throw'`：**登录失败本身也是 401**，
 * 若走默认的 401 处理会触发"重新登录"信号，用户填错密码会被弹去登录页——
 * 而他本来就在登录页。凭据失效的判定只属于"业务请求"，不属于「认证端点」。
 */

import { request } from './client'

export type UserRole = 'admin' | 'member'

export interface Account {
  id: string
  username: string
  name: string
  role: UserRole
  /**
   * 头像链接（后端签发的**签名 URL**，v0.29）。空 = 没有头像。
   *
   * 它是个会过期的链接：`<img src>` 带不了 Authorization 头，所以"你有权取这张图"
   * 这件事被编码进 URL 本身（与文件预览同一套）。过期了图会 401——
   * 界面那份退回"用名字生成的默认头像"（见 `AppAvatar`）。
   */
  avatar_url: string
}

export interface LoginResult {
  /** 会话令牌，只在这一次响应里出现。 */
  token: string
  user: Account
}

export interface AuthBootstrapStatus {
  /** 还没有任何可登录账号：控制台应进入首次设置向导。 */
  needs_setup: boolean
}

/** 认证状态。**不需要凭据**，前端靠它决定显示登录、首次设置还是直接进入。 */
export function getAuthBootstrapStatus(): Promise<AuthBootstrapStatus> {
  return request('/auth/status', undefined, { authFailure: 'throw' })
}

export function login(username: string, password: string): Promise<LoginResult> {
  return request(
    '/auth/login',
    { method: 'POST', body: JSON.stringify({ username, password }) },
    { authFailure: 'throw' },
  )
}

/** 首次初始化：创建管理员账号，并认领 v10 之前的无主数据。 */
export function setup(username: string, password: string, name?: string): Promise<LoginResult> {
  return request(
    '/auth/setup',
    { method: 'POST', body: JSON.stringify({ username, password, name: name || null }) },
    { authFailure: 'throw' },
  )
}

export function logout(): Promise<void> {
  return request('/auth/logout', { method: 'POST' }, { authFailure: 'throw' })
}

/** 当前登录账号。会话失效时 401——由 `restoreSession` 捕获并清除本地令牌。 */
export function me(): Promise<Account> {
  return request('/auth/me', undefined, { authFailure: 'throw' })
}

export function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<{ revoked_sessions: number }> {
  return request(
    '/auth/password',
    {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    },
    { authFailure: 'throw' },
  )
}

/**
 * 换头像：上传一张图片，返回更新后的账号。
 *
 * **图片在前端先缩到 256px 再传**（见 `AppAvatarDialog`）：服务端不装成像库
 * （这个项目到目前一张图都不处理），它只守"是不是图、有多大"两条。
 */
export function uploadAvatar(file: File): Promise<Account> {
  const body = new FormData()
  body.append('file', file)
  return request<Account>('/auth/avatar', { method: 'POST', body }, { authFailure: 'throw' })
}

/** 去掉头像，回到"用名字生成的那张"。没有头像时也成功。 */
export function clearAvatar(): Promise<Account> {
  return request<Account>('/auth/avatar', { method: 'DELETE' }, { authFailure: 'throw' })
}

/** 口令下限与后端 `services/auth.py` 的 `MIN_PASSWORD_CHARS` 对齐。 */
export const MIN_PASSWORD_CHARS = 8
