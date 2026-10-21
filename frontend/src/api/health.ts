/**
 * 服务探活（`GET /api/v1/health`）。
 *
 * 界面上只有一个消费方：顶栏那颗"在线"的绿点。单独一页 API 文件而不是塞进
 * `client.ts`，是因为它**不参与任何业务读写**——只是一次心跳。
 */
import { request } from './client'

export interface HealthOut {
  status: string
  app?: string
  version?: string
  api_version?: string
}

export function getHealth(): Promise<HealthOut> {
  return request<HealthOut>('/health')
}
