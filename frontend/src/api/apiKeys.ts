/**
 * API Key 管理接口（对应后端 /api/v1/api-keys）。
 * 字段与后端 `api/v1/schemas.py` 一一对应，改后端时同步改这里。
 *
 * 这一族端点**只认管理员会话**（后端 `require_admin`，明文钥匙自己打不开它）；
 * 明文只在创建响应里出现一次（`ApiKeyIssued.token`），列表里只有展示用的 `prefix`。
 */

import { request } from './client'
import type { components } from './schema'

/** 权限档：`readonly` 只读 / `readwrite` 读写（后端 `ApiKeyPermission`）。 */
export type ApiKeyPermission = components['schemas']['ApiKeyPermission']

/** 列表项：契约来自后端的 OpenAPI（`ApiKeyOut`，绝不回显 hash 与明文）。 */
export type ApiKey = Required<components['schemas']['ApiKeyOut']>

/** 创建响应：比列表项多一个 `token`——明文**只在这一次响应里**出现。 */
export type ApiKeyIssued = Required<components['schemas']['ApiKeyIssuedOut']>

export interface ApiKeyCreate {
  name: string
  /** 缺省 `readonly`（与后端默认一致：默认值取最保守的那一档）。 */
  permission?: ApiKeyPermission
  /** 空 = 不限定范围（全部知识库都可用这把钥匙）。 */
  knowledge_base_ids?: string[]
}

export function listApiKeys(): Promise<{ items: ApiKey[] }> {
  return request('/api-keys')
}

export function createApiKey(payload: ApiKeyCreate): Promise<ApiKeyIssued> {
  return request('/api-keys', { method: 'POST', body: JSON.stringify(payload) })
}

/** 撤销立即生效：还在用这把钥匙的请求当场开始 401。 */
export function revokeApiKey(keyId: string): Promise<void> {
  return request(`/api-keys/${keyId}`, { method: 'DELETE' })
}
