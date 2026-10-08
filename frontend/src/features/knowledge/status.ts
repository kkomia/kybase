/**
 * 文档状态 → 文案与语义色（《前端设计规范》§6/§7；旧 `components/ui/status.ts` 的同名映射）。
 *
 * 放在本域而不是各页面各写一份：流水线阶段（架构 §4）一旦增删，只有这一处需要改，
 * 也保证列表、抽屉、筛选下拉三处口径一致（否则"失败"在一处叫"失败"、另一处叫"异常"）。
 *
 * 文字永远在，颜色只是加速识别的辅助（§8 必须项）。
 */
import type { StatusTone } from '@/features/knowledge/composites'

export interface StatusView {
  label: string
  tone: StatusTone
}

const DOCUMENT_STAGES: Record<string, StatusView> = {
  uploaded: { label: '待处理', tone: 'neutral' },
  probing: { label: '探测中', tone: 'info' },
  parsing: { label: '解析中', tone: 'info' },
  parsed: { label: '已解析', tone: 'info' },
  chunking: { label: '切分中', tone: 'info' },
  chunked: { label: '已切分', tone: 'info' },
  embedding: { label: '向量化中', tone: 'info' },
  indexed: { label: '已索引', tone: 'success' },
  enriching: { label: '增强中', tone: 'info' },
  enriched: { label: '已增强', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  // 取消不是失败：中性色 + 明确说"已取消"，别让它冒充红色故障
  canceled: { label: '已取消', tone: 'neutral' },
}

/** 未知识别值兜底成中性文案：后端新增阶段时界面不会空白或崩掉。 */
export function documentStageView(stage: string): StatusView {
  return DOCUMENT_STAGES[stage] ?? { label: stage, tone: 'neutral' }
}

/** 筛选下拉里的状态顺序（与旧前端逐条一致，顺序本身也是信息）。 */
export const FILTER_STAGE_KEYS = [
  'uploaded',
  'probing',
  'parsing',
  'parsed',
  'chunking',
  'chunked',
  'embedding',
  'indexed',
  'enriching',
  'enriched',
  'failed',
] as const

/**
 * 文档**来源**（`DocumentOut.source_kind`）→ 中文文案。
 *
 * 与上面的状态映射同一个理由：列表的筛选下拉与抽屉的「来源」读的是同一份取值，
 * 两处各写一份就会出现"一处叫本地上传、另一处把 `upload` 直接摆出来"。
 *
 * 注意它**不是上传者**：来源说的是"这份文档怎么进来的"，上传者是"谁传的"
 * （`uploaded_by_name`，列表里那一列）——`DocumentOut` 上两个不同的字段。
 */
const SOURCE_KINDS: Record<string, string> = {
  upload: '本地上传',
  html: '网页',
  rss: 'RSS 订阅',
  webdav: 'WebDAV',
}

/** 未知识别值兜底成原值：后端新增来源时界面不会空白（同 `documentStageView`）。 */
export function documentSourceLabel(kind: string): string {
  return SOURCE_KINDS[kind] ?? kind
}
