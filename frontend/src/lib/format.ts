/**
 * 展示层格式化（《前端设计规范 v0.3》§4）。
 *
 * 前端不引 dayjs 之类的库：知识库界面的时间只需要"刚刚 / 3 分钟前 / 具体日期"三档，
 * 自己写比多一个依赖划算。
 */

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** 相对时间：7 天内说"多久之前"，更久直接给日期。 */
export function formatRelativeTime(value: string | null | undefined): string {
  if (!value) return '—'
  const moment = new Date(value)
  if (Number.isNaN(moment.getTime())) return '—'

  const diff = Date.now() - moment.getTime()
  if (diff < 0) return formatDate(moment)
  if (diff < MINUTE) return '刚刚'
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)} 天前`
  return formatDate(moment)
}

/** 绝对时间：`2026-09-10 18:07`。 */
export function formatDate(value: string | Date | null | undefined): string {
  if (!value) return '—'
  const moment = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(moment.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())} ` +
    `${pad(moment.getHours())}:${pad(moment.getMinutes())}`
  )
}

/**
 * 文件体积：1024 进制，最多一位小数。
 *
 * 进位按**取整后的读数**判（不是按原值）：`1048575 B` 的原始换算是 `1023.999 KB`，
 * 一位小数会写成一个 `1024.0 KB`，而列表里紧挨着的另一行是 `1.0 MB`——两个数其实
 * 一样大，却像差了一个量级。所以先算到一位小数，够 1024 就抬一档。
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let index = 0
  while (index < units.length - 1 && Number(value.toFixed(1)) >= 1024) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(1)} ${units[index]}`
}

/**
 * 百分比：**10% 以上取整，10% 以下留一位小数**。
 *
 * 两头的理由不一样：
 * - 大数取一位小数是噪音：`37.4%` 与 `37%` 对"要不要处置"没有任何区别，而读数越长越难扫；
 * - 小数**必须**留一位：内存占用常态在 1% 以下，取整会把 `0.6%` 写成 `1%`（差近一倍），
 *   把 `0.04%` 写成 `0%`——那是在说"没有"，而它有。
 * 一位小数仍会被抹平时写 `<0.1%`，不写 `0.0%`。
 * 末尾的 `.0` 一律收掉（`2.0%` → `2%`），与 10% 以上那一档写法一致。
 */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs >= 10) return `${Math.round(value)}%`
  if (abs === 0) return '0%'
  const tenth = value.toFixed(1)
  if (Number(tenth) === 0) return `${value > 0 ? '<' : '>-'}0.1%`
  return `${tenth.endsWith('.0') ? tenth.slice(0, -2) : tenth}%`
}

/**
 * 毫秒级耗时：`12.3 ms`，固定毫秒、一位小数。
 *
 * **不复用 `formatMillis`**：那条走的是"秒级 + 只给两级"的口径（秒/分/小时），
 * 拿它写检索耗时会把 `12.3 ms`、`340.8 ms` 全变成"不到 1 秒"——三个数量级的差别
 * 被抹成一句话，而"这一步慢不慢"正是这个数唯一要回答的问题。
 * 单位也固定不换算：毫秒档里换到秒会让 12.3 ms 变成"0.0 秒"。
 */
export function formatLatency(millis: number | null | undefined): string {
  if (millis === null || millis === undefined || !Number.isFinite(millis)) return '—'
  return `${millis.toFixed(1)} ms`
}

/** 相似度分数：融合分数是相对值（后端按 top 归一化），统一三位小数。 */
/**
 * 千分位计数。
 *
 * 用量动辄六位数（几十万 token），不分位根本读不出量级——
 * 而"这个月是不是烧太快了"正是看这组数字时要回答的问题。
 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  // `+ 0` 收掉 -0：`Math.round(-0.4)` 是 -0，直接分位会写出一个读不通的「-0」
  return (Math.round(value) + 0).toLocaleString('zh-CN')
}

export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return '—'
  return score.toFixed(3)
}

/**
 * 会话内的短时间：几十秒前的那次检索要能显示"秒"，
 * 否则几次查询全是"刚刚"，分不出先后。分钟级以上的展示交给 formatRelativeTime。
 */
export function formatAge(at: number, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - at) / 1000))
  if (seconds < 60) return `${seconds} 秒前`
  return `${Math.floor(seconds / 60)} 分钟前`
}

/**
 * 时长：`2 分 14 秒` / `1 小时 3 分` / `不到 1 秒`。
 *
 * **只给两级**（最大单位 + 下一级）：再多的位数没人读，"1 小时 3 分 12 秒"里的后两位
 * 从来不影响判断。而进度条那一栏要同时看**多个**时长（总耗时 + 各环节耗时），
 * 每个都写全三位会立刻挤成一团。
 *
 * 入参是**秒**。后端时间线给的是毫秒，转换只经 `formatMillis` 一次，
 * 免得各处各写一个 `/1000`。
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const total = Math.max(0, Math.round(seconds))
  if (total < 1) return '不到 1 秒'
  if (total < 60) return `${total} 秒`
  if (total < 3600) {
    const minutes = Math.floor(total / 60)
    const rest = total % 60
    return rest === 0 ? `${minutes} 分` : `${minutes} 分 ${rest} 秒`
  }
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  return minutes === 0 ? `${hours} 小时` : `${hours} 小时 ${minutes} 分`
}

/** 毫秒版，给时间线字段用。 */
export function formatMillis(millis: number | null | undefined): string {
  if (millis === null || millis === undefined) return '—'
  return formatDuration(millis / 1000)
}

/** 每个知识库的文档数与最近更新时间。 */
export interface DocStats {
  count: number
  updatedAt: string | null
}
/**
 * 把文档行按知识库聚合成两列数字（文档数、最近更新）。
 *
 * 规则只写一份：概览页与侧栏都要这两列，两处各写一遍口径就会漂。
 * `updated_at` 是不可解析的字符串时不会抛错——比较只是字符串序，
 * 后端给的是 ISO 时间，字符串序即时间序。
 */
export function summarizeDocuments(
  documents: readonly { knowledge_base_id: string; updated_at: string | null }[],
): Record<string, DocStats> {
  const result: Record<string, DocStats> = {}
  for (const row of documents) {
    const entry = (result[row.knowledge_base_id] ??= { count: 0, updatedAt: null })
    entry.count += 1
    if (row.updated_at && (!entry.updatedAt || row.updated_at > entry.updatedAt)) {
      entry.updatedAt = row.updated_at
    }
  }
  return result
}
