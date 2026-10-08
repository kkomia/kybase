/**
 * 切分参数的表单校验（知识库设置与新建弹窗共用，旧 `composables/useChunking.ts` 的同名口径）。
 *
 * 草稿是**字符串**：输入框被清空（`''`）与填了 `0` 必须分得开，所以校验在字符串上做，
 * 不先 `Number('')` 变成 0 再判断。文案只在一处，两处各写一份迟早会出现
 * "这边说 128–2048、那边说 1–8000"。
 */
import { CHUNK_SIZE_MAX, CHUNK_SIZE_MIN, chunkOverlapMax } from '@/api/knowledgeBases'

/** 解析成整数；空串或非数字给 `null`（与 0 区分）。 */
export function parseIntOrNull(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isInteger(value) ? value : null
}

/** 返回中文错误文案；空串 = 通过。 */
export function chunkingErrorOf(sizeText: string, overlapText: string): string {
  const size = parseIntOrNull(sizeText)
  if (size === null || size < CHUNK_SIZE_MIN || size > CHUNK_SIZE_MAX) {
    return `块长需要在 ${CHUNK_SIZE_MIN}–${CHUNK_SIZE_MAX} 之间`
  }
  const max = chunkOverlapMax(size)
  const overlap = parseIntOrNull(overlapText)
  if (overlap === null || overlap < 0 || overlap > max) {
    return `块重叠需要在 0–${max} 之间（不超过块长的一半）`
  }
  return ''
}

/** 滑杆的通用读法：空草稿按默认值显示（草稿仍是字符串，见上）。 */
export function numberOr(text: string, fallback: number): number {
  return parseIntOrNull(text) ?? fallback
}
