/**
 * 活跃度点状图（GitHub 贡献图那种）——与旧前端 `components/charts/ActivityHeatmap.vue` 对应。
 *
 * 三个刻意的做法：
 * 1. **每一格是正方形**：用热力图 + 底色的格间边框实现，而不是靠圆角小圆点。
 *    方块之间留一道细缝、整体接近密排，才能读成"一片节奏"而不是"一堆散点"；
 * 2. **空值也有底色**：没有活动的日子画一格低透明度的浅色块（见 `HEAT_EMPTY`），
 *    这样整块图形是连续的，数据缺口和"零活动"能分开；
 * 3. **季度刻度放底部**：x 轴按周分列，标签只打在每个季度首月的那一列上——
 *    放在顶部会和色块挤在一起，而按"月"标会被跨月的周吞掉几个（见下面的注释）。
 *
 * **ECharts 在这个文件里也是异步加载的**（`lazy`）：热力图是驾驶舱最下面的一块，
 * 静态引 EChart 会把几百 KB 拽进驾驶舱的静态依赖图，让别处的按需拆分全部失效。
 */
import { lazy, Suspense, useMemo } from 'react'

import type { ActivityPoint } from '@/api/stats'

import { SkeletonBlock } from '../shared/composites'

const EChart = lazy(() => import('./EChart').then((module) => ({ default: module.EChart })))

/**
 * 纵轴：**周一在最上**。
 *
 * 顺序不用再翻一次——`type: 'category'` 的纵轴是**从下往上**排的（第 0 项在底部），
 * 所以配一个 `inverse` 让第 0 项（周一）落在最上；只改数据顺序的话，
 * 屏幕上还是老样子（日→六→…→一，界面评审 D5）。
 */
const WEEKDAY_LABELS = ['一', '二', '三', '四', '五', '六', '日']

/** 只标季度首月：4 个标签，且带年份。 */
const QUARTER_FIRST_MONTHS = new Set([0, 3, 6, 9])

/**
 * 空格子（当天没有入库）的颜色：**低透明度 token**，不是实色。
 *
 * `--heat-0` 是实色，浅色下（#eeeff1 压在白卡上）刚好，深色下（#232326 压在 #121212 上）
 * 就几乎与卡底同色——整块热力图在深色里读作一片空黑（界面评审 D2）。
 * 换成"把文字色按比例混进卡底"，同一句表达式在两套主题里都成立：
 * 浅色得到浅灰格，深色得到比卡底亮一档的格子，都不写死色值。
 */
export const HEAT_EMPTY = 'color-mix(in srgb, var(--text-primary) 16%, var(--bg-surface))'

/** 四档色阶：图例方块与图上的 `visualMap` **共用这一份**，两处颜色不许各写一遍。 */
export const HEAT_STEPS = [HEAT_EMPTY, 'var(--heat-1)', 'var(--heat-2)', 'var(--heat-3)'] as const

/**
 * 图例的色阶方块（浅 → 深）。
 *
 * 此前图例只有一句"颜色越深表示当天入库越多"，**没有任何样本**——用户不知道
 * "深"是哪一档、更不知道深色下网格在哪（界面评审 D2）。
 */
export function HeatLegend() {
  return (
    <span className="m-heat-swatches" aria-hidden="true">
      {HEAT_STEPS.map((color) => (
        <span key={color} className="m-heat-swatch" style={{ background: color }} />
      ))}
    </span>
  )
}

/** 第一天的星期几决定它落在纵轴第几格，所以列数要把它算进去。 */
export function leadingBlanksOf(activity: ActivityPoint[]): number {
  if (activity.length === 0) return 0
  const first = new Date(activity[0].day)
  return (first.getDay() + 6) % 7
}

export function weekCountOf(activity: ActivityPoint[]): number {
  if (activity.length === 0) return 0
  return Math.ceil((activity.length + leadingBlanksOf(activity)) / 7)
}

export function ActivityHeatmap({
  activity,
  maxCell = 20,
}: {
  activity: ActivityPoint[]
  maxCell?: number
}) {
  const hasData = activity.some((point) => point.documents > 0)
  const weekCount = weekCountOf(activity)

  const option = useMemo(() => {
    // 第一天的星期几决定它在纵轴上的位置：让每一列恰好是一周
    const leadingBlanks = leadingBlanksOf(activity)
    const data: [number, number, number][] = []
    activity.forEach((point, index) => {
      const slot = index + leadingBlanks
      data.push([Math.floor(slot / 7), slot % 7, point.documents])
    })
    const weeks = Math.ceil((activity.length + leadingBlanks) / 7)

    /**
     * x 轴刻度：**按季度，带年份**，一列只有一个标签。
     *
     * 此前标的是"每月第一周"：一列只能挂一个标签，于是"月首落在周中"的月份必然被吞掉，
     * 27 个月里只剩 6 个、间隔还不匀（界面评审 D5：跳过 12/3/5/7/9/10 月）。
     * 季度标签的间隔天然均匀（约 13 列），不可能互相挤掉；带上年份之后
     * 跨年的窗口（本窗口就跨 2025/2026）也读得出来。
     */
    const labelByWeek = new Map<number, string>()
    activity.forEach((point, index) => {
      const day = new Date(point.day)
      const previous = index > 0 ? new Date(activity[index - 1].day) : null
      // **判据是"这一天是这个月在本窗口里的第一天"**，不是"日期 ≤ 7"：
      // 一个月的前 7 天会横跨两个周列（比如 10/1 在周三、10/6 在下一列），
      // 按"日期 ≤ 7"会给出两个挨着的标签，读起来是一团重影（实测过）
      const firstOfMonth = !previous || previous.getMonth() !== day.getMonth()
      if (!firstOfMonth || !QUARTER_FIRST_MONTHS.has(day.getMonth())) return
      const week = Math.floor((index + leadingBlanks) / 7)
      if (!labelByWeek.has(week)) {
        labelByWeek.set(week, `${day.getFullYear()} Q${Math.floor(day.getMonth() / 3) + 1}`)
      }
    })

    return {
      animationDuration: 300,
      // 只留左侧星期标签的位置；绘图区宽度由 squareCells 反推锁死
      grid: { left: 28 },
      tooltip: {
        formatter: (params: { value: [number, number, number] }) => {
          const index = params.value[0] * 7 - leadingBlanks + params.value[1]
          const point = activity[index]
          if (!point) return ''
          return `${point.day}<br/>入库文档 ${point.documents} 篇`
        },
      },
      xAxis: {
        type: 'category',
        data: Array.from({ length: weeks }, (_, index) => String(index)),
        axisLabel: {
          show: true,
          formatter: (value: string) => labelByWeek.get(Number(value)) ?? '',
          // **必须写 0**：默认的 'auto' 会自己再抽一次稀，把已经排好的季度标签也吃掉
          // （实测 4 个标签只剩 2 个）。这里绝大多数列返回空串，本来就不会挤。
          interval: 0,
          fontSize: 11,
          margin: 10,
        },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'category',
        data: WEEKDAY_LABELS,
        // 纵轴默认从下往上排，不加这一条"周一"就落在最下面（见 WEEKDAY_LABELS 的注释）
        inverse: true,
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontSize: 10 },
      },
      visualMap: {
        show: false,
        type: 'piecewise',
        // 离散分档而不是连续渐变：1 篇和 2 篇必须**看得出区别**。
        // 四档对应"零 / 少 / 中 / 多"，颜色与图例方块同源（HEAT_STEPS）
        pieces: [
          { min: 3, color: HEAT_STEPS[3] },
          { min: 2, max: 2, color: HEAT_STEPS[2] },
          { min: 1, max: 1, color: HEAT_STEPS[1] },
          { value: 0, color: HEAT_STEPS[0] },
        ],
        outOfRange: { color: HEAT_STEPS[0] },
      },
      series: [
        {
          type: 'heatmap',
          data,
          // 缝隙由 EChart 统一用底色画（透明边框挡不住相邻格子）
          itemStyle: { borderRadius: 3 },
          emphasis: { itemStyle: { borderColor: 'var(--text-secondary)' } },
        },
      ],
    }
  }, [activity])

  if (!hasData) {
    return <p className="m-empty-note">窗口内还没有入库记录。上传文档后这里会显示活跃度。</p>
  }

  return (
    <Suspense fallback={<SkeletonBlock variant="list" rows={3} />}>
      <EChart
        option={option}
        squareCells={{ columns: weekCount, rows: 7, maxSize: maxCell, minSize: 9, gap: 3 }}
      />
    </Suspense>
  )
}
