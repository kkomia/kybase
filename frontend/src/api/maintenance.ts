/**
 * 存储维护接口（对应后端 /api/v1/maintenance/*，v17）。
 *
 * 两个端点都是管理员专属：
 * - 概览告诉你**空间都花在哪**（尤其是"可回收"那一项——删掉的行只留下死元组，
 *   不看这个数字会以为删了没用）；
 * - 整理会丢掉无主的向量分区并跑 `VACUUM (ANALYZE)`：它重写**含死元组的堆表页**
 *   （不是整个库文件，也不是 ``VACUUM FULL``），几 GB 的库仍可能要几十秒，
 *   所以只能由用户显式点击触发，这里不做任何自动轮询或自动重试。
 */

import { request } from './client'
import type { components } from './schema'

/**
 * 存储概览：契约来自后端的 OpenAPI（SQLite 时代的 `freelist` 口径已随 v0.12
 * 换成 PG 的 `pg_database_size` / `pg_stat_user_tables`，这里不再复述字段含义）。
 */
export type StorageOverview = Required<components['schemas']['StorageOverviewOut']>

export function getStorageOverview(): Promise<StorageOverview> {
  return request('/maintenance/storage')
}

/** 丢掉无主向量分区并 VACUUM，返回**整理之后**的概览。 */
export function compactStorage(): Promise<StorageOverview> {
  return request('/maintenance/compact', { method: 'POST' })
}
