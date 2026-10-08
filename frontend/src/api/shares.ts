/**
 * 知识库分享接口（`/api/v1/knowledge-bases/{kb_id}/shares`，v10）。
 *
 * 授权对象按 **username**：成员没有权限列全量名册（`/users` 是控制台级），
 * 让对方敲登录名是唯一不泄露名册全貌的方式。
 *
 * 谁能管：库的 owner 或管理员。被分享者（哪怕 write 档）不能再外授——
 * 端点会给 403，界面应据 `KnowledgeBase.can_manage` 决定是否显示入口。
 */

import { request } from './client'

export type SharePermission = 'read' | 'write'

export interface Share {
  user_id: string
  username: string
  name: string
  permission: SharePermission
  created_at: string | null
}

export function listShares(kbId: string): Promise<{ items: Share[] }> {
  return request(`/knowledge-bases/${kbId}/shares`)
}

/** 授出或调整档位。同一个 username 再授一次 = 改档位，不产生第二条。 */
export function grantShare(
  kbId: string,
  username: string,
  permission: SharePermission,
): Promise<Share> {
  return request(`/knowledge-bases/${kbId}/shares`, {
    method: 'PUT',
    body: JSON.stringify({ username, permission }),
  })
}

export function revokeShare(kbId: string, userId: string): Promise<void> {
  return request(`/knowledge-bases/${kbId}/shares/${userId}`, { method: 'DELETE' })
}
