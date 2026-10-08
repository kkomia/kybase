/**
 * 数据源接口（M6 / T6.1–T6.3）。
 *
 * 一个数据源 = "定期去看某处有没有新内容"。当前支持 RSS 订阅与单页 HTML。
 */

import { request } from './client'
import type { components } from './schema'

/** 界面支持的形式。后端将来加种类时，见下面 `kind` 的处理。 */
export type SourceKind = 'rss' | 'html'

type DataSourceOut = Required<components['schemas']['DataSourceOut']>

/**
 * 数据源：契约来自后端的 OpenAPI。
 *
 * `kind` **显式放宽成 `string`**：后端加一种数据源（比如 WebDAV）时，
 * 老前端不该因为"读到一个没见过的 kind"而崩——它只需要显示一个名字。
 * 收窄成 `SourceKind` 会让新种类在上线那天变成类型错误，而那时前端还没发版。
 */
export type DataSource = Omit<DataSourceOut, 'kind'> & { kind: SourceKind | string }

export interface SyncResult {
  task_id: string | null
  source_id: string
  fetched: number
  created: number
  duplicates: number
  /** 服务端回了 304：源没有任何变化。 */
  not_modified: boolean
  errors: string[]
}

export function listDataSources(kbId: string): Promise<{ items: DataSource[] }> {
  return request(`/knowledge-bases/${kbId}/data-sources`)
}

export function createDataSource(
  kbId: string,
  payload: { kind: SourceKind; name: string; url: string; max_items?: number | null },
): Promise<DataSource> {
  return request(`/knowledge-bases/${kbId}/data-sources`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function setDataSourceEnabled(id: string, enabled: boolean): Promise<DataSource> {
  return request(`/data-sources/${id}/enabled?enabled=${enabled}`, { method: 'PATCH' })
}

export function deleteDataSource(id: string): Promise<void> {
  return request(`/data-sources/${id}`, { method: 'DELETE' })
}

/**
 * 拉取一次。
 *
 * `wait=true` 会同步跑完并返回统计（界面用这个——用户点了"立即拉取"
 * 就该立刻看到"抓到了几条"，而不是一个任务号）。
 * 后台定时任务走入队那条路。
 */
export function syncDataSource(id: string, wait = true): Promise<SyncResult> {
  return request(`/data-sources/${id}/sync?wait=${wait}`, { method: 'POST' })
}
