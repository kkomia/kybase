/**
 * 状态 → 文案与语义色的映射（逐条照搬旧前端 `components/ui/status.ts`）。
 *
 * 放在一处而不是各页面各写一份：流水线阶段一旦增删，只有这里需要改，
 * 也保证任务中心、文档页、驾驶舱的口径一致。
 *
 * **文字永远在**，颜色只是加速识别的辅助（规范 §8）。
 */
import type { TagTone } from './composites'

export interface StatusView {
  label: string
  tone: TagTone
}

const TASK_STATES: Record<string, StatusView> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '执行中', tone: 'info' },
  succeeded: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  canceled: { label: '已取消', tone: 'neutral' },
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

const TASK_KINDS: Record<string, string> = {
  probe: '探测',
  parse: '解析',
  chunk: '切分',
  embed: '向量化',
  questions: '出题',
  // 定时任务跑出来的也是队列任务，会出现在任务列表里——漏了它那一行会显示英文
  scheduled: '定时任务',
  fetch_source: '拉取数据源',
  wiki: '生成 Wiki',
  memory: '记忆沉淀',
}

/**
 * 健康判据 → 语义色。
 *
 * **只有"有问题"的两档会画成标签**（见 `TasksPage.healthVerdict`）：`running` / `idle` /
 * `done` 三档在行上与弹窗里都退成一个中性记号——后端给它们的 label（执行中 / 排队中 /
 * 已完成 / 已取消）与状态列逐字相同，复述而已。所以这里只留下会用到颜色的两档：
 * 给一张查不到用处的表，只会让人以为还有别的用武之地。
 */
const TASK_HEALTH_TONES: Record<string, TagTone> = {
  stalled: 'danger',
  overdue: 'warning',
}

export function taskHealthTone(health: string): TagTone {
  return TASK_HEALTH_TONES[health] ?? 'neutral'
}

/** 只有这两种是**需要用户做点什么**的；用来决定要不要在行上给一个提示。 */
export function isTaskProblem(health: string): boolean {
  return health === 'stalled' || health === 'overdue'
}

/** 未知识别值兜底成中性文案：后端新增阶段时界面不会空白或崩掉。 */
export function taskStateView(state: string): StatusView {
  return TASK_STATES[state] ?? { label: state, tone: 'neutral' }
}

export function documentStageView(stage: string): StatusView {
  return DOCUMENT_STAGES[stage] ?? { label: stage, tone: 'neutral' }
}

export function taskKindLabel(kind: string): string {
  return TASK_KINDS[kind] ?? kind
}

/**
 * 占用率的语义色：**只有真的高才变色**（阈值 80/90，不是 50）。
 *
 * 摄入是 CPU 密集型任务，跑到 60~70% 完全正常；一过半就变黄会让人天天看到黄条，
 * 然后就再也不看它了。
 */
export function loadTone(
  percent: number | null,
  warning = 80,
  danger = 90,
): 'accent' | 'warning' | 'danger' {
  if (percent === null) return 'accent'
  if (percent >= danger) return 'danger'
  if (percent >= warning) return 'warning'
  return 'accent'
}
