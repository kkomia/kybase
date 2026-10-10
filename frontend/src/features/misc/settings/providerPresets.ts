/**
 * 供应商预设的纯逻辑（与框架无关，便于单测）——从旧前端 `components/settings/providerPresets.ts` 搬来。
 *
 * 预设解决的是"接口地址各家不一样"：百炼要 `/compatible-mode/v1`、Gemini 要
 * `/v1beta/openai/`、自建的千奇百怪。抄地址是纯摩擦，而且抄错了报错在很后面。
 * 这里只做三件事：规范化后比较、按地址把供应商和预设对上、把"上游探测到的模型"
 * 与"预设建议的模型"合并去重。
 *
 * 预设本身来自后端（`registry.provider_presets`），这里不内置任何地址清单。
 */
import type { PresetModel, Provider, ProviderPreset } from '@/api/modelRegistry'

/** 尾部斜杠归一化：`https://x/v1/` 与 `https://x/v1` 是同一个地址。 */
export function normalizeUrl(url: string): string {
  return url.trim().replace(/\/+$/, '')
}

/**
 * 这个供应商对应哪个预设。
 *
 * 按**地址**匹配而不是按名称：用户可能把"深度求索"改成"我的 DeepSeek"，
 * 地址才是那件真正决定"发到哪去"的事。改过地址就匹配不上——也就不给建议，
 * 好过拿一个不对的预设列表去误导。
 */
export function presetForProvider(
  presets: ProviderPreset[],
  provider: Pick<Provider, 'base_url'>,
): ProviderPreset | null {
  if (!provider.base_url) return null
  const target = normalizeUrl(provider.base_url)
  return (
    presets.find((preset) => preset.base_url && normalizeUrl(preset.base_url) === target) ?? null
  )
}

export function suggestedModels(
  presets: ProviderPreset[],
  provider: Pick<Provider, 'base_url'>,
): PresetModel[] {
  return presetForProvider(presets, provider)?.models ?? []
}

/**
 * 「添加模型」下拉的候选：上游探测到的 + 预设建议（去重）。
 *
 * 探测是**权威**（反映上游此刻真有什么），预设是**兜底**（有的端点不返回模型列表）。
 * 预设项打 `· 预设` 标记，让用户知道这条不是探测回来的。
 */
export function mergeModelOptions(
  probeOptions: { value: string; label: string }[],
  presets: ProviderPreset[],
  provider: Pick<Provider, 'base_url'>,
): { value: string; label: string }[] {
  const known = new Set(probeOptions.map((item) => item.value))
  const suggested = suggestedModels(presets, provider)
    .filter((item) => !known.has(item.model_id))
    .map((item) => ({ value: item.model_id, label: `${item.label || item.model_id} · 预设` }))
  return [...probeOptions, ...suggested]
}

/**
 * 选中预设建议里的某条模型时，它自带的元数据（能力、维度）。
 *
 * **只对预设项返回**：手写或探测到的 ID 没有元数据，猜能力等于替用户做决定
 * （把 embedding 标成 chat，要到建库时才发现）。
 */
export function presetModelMeta(
  presets: ProviderPreset[],
  provider: Pick<Provider, 'base_url'>,
  modelId: string,
): PresetModel | null {
  const value = modelId.trim()
  if (!value) return null
  return suggestedModels(presets, provider).find((item) => item.model_id === value) ?? null
}
