/**
 * 使用者名册与账号管理接口（`/api/v1/users`）。
 *
 * 两层含义共用一张表（v10）：
 * - **纯名册条目**（`username` 为空）：只回答"这份文档是谁传的"，不能登录；
 * - **账号**（`username` 非空）：可登录，有角色与禁用状态。
 *
 * 名册读取要 `require_read`，写与账号管理是 **控制台级**（`require_console`）——
 * 与 API Key 管理同一档待遇，普通成员够不着。
 */

import { request } from './client'

export type UserRole = 'admin' | 'member'

export interface RosterUser {
  id: string
  name: string
  note: string
  created_at: string | null
  document_count: number
  /** 头像链接（签名 URL，v0.29）。空 = 用名字生成的默认头像。 */
  avatar_url: string
  username: string | null
  role: UserRole
  disabled: boolean
}

export interface Roster {
  items: RosterUser[]
  /** 操作者应当放在哪个请求头里。由后端给出，免得两边各写一份会漂。 */
  header: string
}

export interface AccountCreate {
  /** 显示名；必填（后端 min_length=1）。 */
  name: string
  note?: string
  /** 带 username 即开通账号，此时 password 必填。 */
  username?: string
  password?: string
  role?: UserRole
}

export function listUsers(): Promise<Roster> {
  return request('/users')
}

export function createUser(payload: AccountCreate): Promise<RosterUser> {
  return request('/users', { method: 'POST', body: JSON.stringify(payload) })
}

/** 管理员重置某人密码（吊销其全部会话）。 */
export function resetUserPassword(userId: string, password: string): Promise<void> {
  return request(`/users/${userId}/password`, {
    method: 'PUT',
    body: JSON.stringify({ password }),
  })
}

/** 禁用 / 启用账号（禁用即吊销全部会话）。 */
export function setUserDisabled(userId: string, disabled: boolean): Promise<RosterUser> {
  return request(`/users/${userId}/disabled`, {
    method: 'PUT',
    body: JSON.stringify({ disabled }),
  })
}

export function deleteUser(userId: string): Promise<void> {
  return request(`/users/${userId}`, { method: 'DELETE' })
}
