/**
 * 模型注册器接口（`/api/v1/model-registry`，调研报告 G1）。
 *
 * 三层：**供应商 → 模型目录 → 按用途绑定**。
 *
 * 密钥纪律与设置页一致：列表里只回 `api_key_configured` 与 `api_key_hint`（掩码），
 * **永远不回密钥本身**。所以更新供应商时**不要回传 hint**——那会把掩码写成真实密钥。
 * 改名字就只传名字，改密钥才传 `api_key`。
 */

import { request } from './client'

export interface Provider {
  id: string
  kind: string
  name: string
  base_url: string
  enabled: boolean
  created_at: string | null
  updated_at: string | null
  api_key_configured: boolean
  /** 掩码后的尾巴，只用于显示"是哪一把"。 */
  api_key_hint: string
  model_count: number
}

export interface RegisteredModel {
  id: string
  provider_id: string
  provider_name: string
  provider_kind: string
  model_id: string
  label: string
  dim: number | null
  capabilities: string[]
  options: Record<string, unknown>
  created_at: string | null
  updated_at: string | null
  bound_slots: string[]
}

export interface Slot {
  slot: string
  label: string
  capability: string
  bound_model_pk: string | null
  bound_model_label: string
  provider_name: string
  configured: boolean
  /** 当前生效的来源：v0.8 起只有 `registry`（绑定过）或 `none`（未绑定）。 */
  source: 'registry' | 'none'
}

/** 预设里的一条模型建议。 */
export interface PresetModel {
  model_id: string
  label: string
  capabilities: string[]
  dim: number | null
}

/** 常见供应商预设：一键填好名称 / 类别 / 地址与几条常见模型。 */
export interface ProviderPreset {
  id: string
  label: string
  kind: string
  base_url: string
  hint: string
  models: PresetModel[]
}

export interface Registry {
  providers: Provider[]
  models: RegisteredModel[]
  slots: Slot[]
  provider_kinds: Record<string, string>
  capabilities: Record<string, string>
  /** 只用于"添加供应商"填表单，不落库。 */
  provider_presets: ProviderPreset[]
}

export function getRegistry(): Promise<Registry> {
  return request('/model-registry')
}

export interface ProviderInput {
  kind: string
  name: string
  base_url?: string
  api_key?: string
  enabled?: boolean
}

export function createProvider(payload: ProviderInput): Promise<Provider> {
  return request('/model-registry/providers', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * 改供应商。
 *
 * `api_key` **不传**表示保持原值，传 `''` 表示清空。界面只在用户真去改密钥时
 * 才带上这个字段——把列表里的掩码回传会把密钥写成掩码（后端能挡住，
 * 但前端不该制造这种请求）。
 */
export function updateProvider(
  providerId: string,
  payload: Partial<ProviderInput>,
): Promise<Provider> {
  return request(`/model-registry/providers/${providerId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteProvider(providerId: string): Promise<void> {
  return request(`/model-registry/providers/${providerId}`, { method: 'DELETE' })
}

export interface ModelInput {
  provider_id: string
  model_id: string
  label?: string
  dim?: number | null
  capabilities?: string[]
  options?: Record<string, unknown>
}

export function registerModel(payload: ModelInput): Promise<RegisteredModel> {
  return request('/model-registry/models', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateModel(
  modelPk: string,
  payload: Partial<Omit<ModelInput, 'provider_id'>>,
): Promise<RegisteredModel> {
  return request(`/model-registry/models/${modelPk}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteModel(modelPk: string): Promise<void> {
  return request(`/model-registry/models/${modelPk}`, { method: 'DELETE' })
}

/** 绑定用途到模型；`modelPk` 传 `null` 解绑（回退到设置页那套字段）。 */
export function bindSlot(slot: string, modelPk: string | null): Promise<Slot> {
  return request(`/model-registry/slots/${slot}`, {
    method: 'PUT',
    body: JSON.stringify({ model_pk: modelPk }),
  })
}

export function testSlot(slot: string): Promise<{ ok: boolean; detail: string }> {
  return request(`/model-registry/slots/${slot}/test`, { method: 'POST' })
}

/**
 * 探活一家供应商：后端请求一次 `GET {base_url}/models`，**不计费**，
 * 只验"地址对不对、凭据有没有效"——注册环节最会填错的两件事。
 */
export function testProvider(providerId: string): Promise<{ ok: boolean; detail: string }> {
  return request(`/model-registry/providers/${providerId}/test`, { method: 'POST' })
}

export interface AvailableModel {
  model_id: string
  owned_by: string
}

export interface AvailableModels {
  models: AvailableModel[]
  count: number
}

/**
 * 拉取该供应商上游可用的模型列表（**不落库**）。
 *
 * 用于「添加模型」时给下拉框喂候选：上游动不动几十上百个模型，全登记进来只是噪声，
 * "选哪一个"才是用户的决定。探测不到时返回空列表而非报错——手写输入这条出路一直在。
 */
export function listAvailableModels(providerId: string): Promise<AvailableModels> {
  return request(`/model-registry/providers/${providerId}/available-models`)
}
