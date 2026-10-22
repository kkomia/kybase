/**
 * 概览 = 驾驶舱（界面规则6）——与旧前端 `views/DashboardView.vue` 逐条对应。
 *
 * 回答"这个知识库系统现在什么状态、最近在不在动"，而不是"有哪些库"——
 * 库的清单有自己的页面（侧栏「所有知识库」）。
 *
 * 三条自我约束：
 * 1. **只画有数据支撑的图**：调用量走 `/stats/usage`；宁可少一块图，也不放一个编出来的数字；
 * 2. **颜色只来自主题 Token**：图表与页面共用一套灰阶 + 语义色，不另立一套"图表配色"；
 * 3. 数字先给**结论**（一条仪表带），再给**分布**（节奏 / 构成 / 用量 / 规模）。
 *
 * ## 2026-10 重做的排布
 *
 * ```
 * 概览                      ← 28px 标题 + 一句现状
 * ┌──┬──┬──┬──┬──┐          ← 仪表带：一格一个数，中间用发丝线切开（不是五张卡）
 * │9 │230│28,598│…│
 * └──┴──┴──┴──┴──┘
 * 活跃度
 * 近 365 天入库节奏（热力图）
 * 趋势（分段控件切指标）
 * 构成
 * 阶段分布 │ 文件类型
 * 模型用量
 * 知识库规模（清单表）
 * ```
 *
 * **仪表带是一张卡、五格**：旧版是五张各自描边的卡，摆在一起既占地方又碎。
 * 现在它们共用一层白面，格与格之间只用 1px 发丝线分开——读起来是"一块仪表"，
 * 而不是"五块拼图"。数字到达时走一次 520ms 的滚动（`useCountUp`）。
 *
 * **图表按需加载**：`EChart` 把整个 ECharts 拉进来（数百 KB），所以这里与热力图
 * 都用 `React.lazy`。
 */
import { DASHBOARD_WINDOW_DAYS, USAGE_WINDOW_DAYS } from '@/features/misc/queryKeys'
import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import type { ActivityPoint } from '@/api/stats'
import { getDashboard, getUsage } from '@/api/stats'
import { Button } from '@/ui/button'
import { formatBytes, formatCount, formatRelativeTime } from '@/lib/format'
import { useCountUp } from '@/lib/useCountUp'
import { cn } from '@/lib/utils'

import { documentStageView } from '../shared/status'
import { EmptyState, ErrorLine, PageShell, SkeletonBlock, StatusTag } from '../shared/composites'
import { ActivityHeatmap, HeatLegend } from './ActivityHeatmap'

const EChart = lazy(() => import('./EChart').then((module) => ({ default: module.EChart })))

/**
 * 格子边长上限：宽度不够时按列数自动缩小，够宽就吃满一行的宽度。
 * 365 天是 53 列，上限定死太小会让网格只画到卡片的三分之二。
 */
const MAX_CELL = 20

/** 趋势图 x 轴最多留几个日期标签（太多会糊成一条）。 */
const MAX_TREND_LABELS = 7

const METRIC_LABELS = {
  documents: '入库文档',
  chunks: '新增切块',
  tasks: '流水线任务',
  hits: '检索命中',
} as const

type Metric = keyof typeof METRIC_LABELS

/** 仪表带的一格。`value` 是原始数（要滚动），`format` 负责加千分位 / 字节单位。 */
type FigureSlot = {
  label: string
  value: number
  format: (value: number) => string
  note?: string
  noteTone?: 'danger' | 'warning' | 'neutral'
  ready: boolean
}

/**
 * 按天趋势的聚合口径（纯函数，导出供用例钉住）。
 *
 * 观察窗口太长时按周聚合，否则折线全是锯齿——365 天一天一个点的话，
 * 一屏里塞 365 个锯齿，读不出"最近是不是更忙了"。
 */
export function checkableTrend(
  points: ActivityPoint[],
  metric: Metric,
): { labels: string[]; values: number[] } {
  const bucket = points.length > 60 ? 7 : 1
  const labels: string[] = []
  const values: number[] = []
  for (let index = 0; index < points.length; index += bucket) {
    const slice = points.slice(index, index + bucket)
    labels.push(slice[0].day.slice(5))
    values.push(slice.reduce((sum, point) => sum + point[metric], 0))
  }
  return { labels, values }
}

/** x 轴日期标签的抽稀步长（ECharts 的 `axisLabel.interval`，0 = 全标）。 */
export function trendLabelInterval(count: number): number {
  return Math.max(0, Math.ceil(count / MAX_TREND_LABELS) - 1)
}

/** 仪表带的一格：数字到达时滚一次。 */
function FigureCell({ slot }: { slot: FigureSlot }) {
  const animated = useCountUp(slot.ready ? slot.value : 0)
  return (
    <li className="m-figure">
      <span className="m-figure-label">{slot.label}</span>
      <span className={cn('m-figure-value tabular', !slot.ready && 'm-figure-pending')}>
        {slot.ready ? slot.format(animated) : '—'}
      </span>
      {slot.note ? (
        <span
          className={cn(
            'm-figure-note',
            slot.noteTone === 'danger' && 'text-status-danger',
            slot.noteTone === 'warning' && 'text-status-warning',
          )}
        >
          {slot.note}
        </span>
      ) : null}
    </li>
  )
}

export function DashboardPage() {
  const windowDays = DASHBOARD_WINDOW_DAYS
  const usageDays = USAGE_WINDOW_DAYS
  const [metric, setMetric] = useState<Metric>('documents')

  const dashboard = useQuery({
    queryKey: ['stats', 'dashboard', windowDays],
    queryFn: () => getDashboard(windowDays),
  })
  /** 模型用量：与驾驶舱并行取；取不到只是这一块空着。 */
  const usage = useQuery({
    queryKey: ['stats', 'usage', usageDays],
    queryFn: () => getUsage(usageDays),
    retry: false,
  })

  const data = dashboard.data ?? null
  /** 用量还没回来：数字显示占位符。**不能显示 0**——0 的含义是"确实没有调用"。 */
  const usagePending = usage.data === undefined && usage.isLoading
  const summary = data
  const hasAnyDocument = (data?.total_documents ?? 0) > 0

  /** 页头那句现状：三个数，只报确定的东西。 */
  const subtitle = data
    ? `${formatCount(data.total_knowledge_bases)} 个知识库 · ${formatCount(data.total_documents)} 篇文档 · ${formatCount(data.total_chunks)} 个切块`
    : '正在读取运行数据…'

  /**
   * 仪表带：每格一个"当前值 + 一句它是什么"。
   *
   * **数据没到也先把五格画出来**（值为占位符）：页面的骨架立刻可见，
   * 不用盯着一块空白等最慢的请求。
   *
   * **注解只在"这一格自己说不清"时才给**：只留**口径**（失败 / 待索引）与
   * **加载态**——"相互隔离的检索范围""向量化的最小单位""全部已索引"这类
   * 同义复述全部删掉（卡片上已经有标签与大数字）。
   */
  const figureSlots: FigureSlot[] = (() => {
    if (!data) {
      return [
        { label: '知识库', value: 0, format: formatCount, ready: false },
        { label: '文档与索引', value: 0, format: formatCount, note: '正在统计…', ready: false },
        { label: '切块', value: 0, format: formatCount, ready: false },
        { label: '原文体积', value: 0, format: formatBytes, ready: false },
        {
          label: `近 ${windowDays} 天入库`,
          value: 0,
          format: formatCount,
          note: '正在统计…',
          ready: false,
        },
      ]
    }
    const indexNote =
      data.total_documents === 0
        ? '还没有文档'
        : data.failed_documents > 0
          ? `${formatCount(data.failed_documents)} 篇失败`
          : data.indexed_documents < data.total_documents
            ? `${formatCount(data.total_documents - data.indexed_documents)} 篇待索引`
            : ''
    return [
      {
        label: '知识库',
        value: data.total_knowledge_bases,
        format: formatCount,
        ready: true,
      },
      {
        label: '文档与索引',
        value: data.total_documents,
        format: formatCount,
        note: indexNote,
        noteTone: data.failed_documents > 0 ? 'danger' : 'neutral',
        ready: true,
      },
      { label: '切块', value: data.total_chunks, format: formatCount, ready: true },
      { label: '原文体积', value: data.storage_bytes, format: formatBytes, ready: true },
      {
        label: `近 ${windowDays} 天入库`,
        value: data.recent_documents,
        format: formatCount,
        ready: true,
      },
    ]
  })()

  /** 按天趋势：观察窗口太长时按周聚合。 */
  const trendOption = (() => {
    const points: ActivityPoint[] = data?.activity ?? []
    const { labels, values } = checkableTrend(points, metric)
    return {
      animationDuration: 620,
      animationEasing: 'cubicOut',
      grid: { left: 44, right: 16, top: 18, bottom: 24 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: labels,
        boundaryGap: false,
        axisLabel: { interval: trendLabelInterval(labels.length) },
      },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          type: 'line',
          smooth: 0.35,
          symbol: 'none',
          data: values,
          lineStyle: { width: 2, color: 'var(--accent)' },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: 'var(--accent)' },
                { offset: 1, color: 'transparent' },
              ],
            },
            opacity: 0.18,
          },
        },
      ],
    }
  })()

  /** 文件类型分布：横向条形，类型名放得下且不用旋转标签。 */
  const suffixOption = (() => {
    const entries = Object.entries(data?.by_suffix ?? {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8)
    return {
      animationDuration: 620,
      animationEasing: 'cubicOut',
      grid: { left: 76, right: 40, top: 8, bottom: 8 },
      tooltip: { trigger: 'item' },
      xAxis: { type: 'value', minInterval: 1 },
      yAxis: {
        type: 'category',
        data: entries.map(([suffix]) => suffix),
        splitLine: { show: false },
        inverse: true,
      },
      series: [
        {
          type: 'bar',
          data: entries.map(([, count]) => count),
          barWidth: 10,
          itemStyle: { color: 'var(--chart-2)', borderRadius: 5 },
          label: { show: true, position: 'right', color: 'var(--text-secondary)', fontSize: 11 },
        },
      ],
    }
  })()

  /** 阶段分布用排行条呈现（理由见 JSX 里那一段注释）。 */
  const stageEntries = Object.entries(data?.by_stage ?? {}).filter(([, count]) => count > 0)

  /** 按用途画个相对的条形——一眼看出"力气花在哪一类调用上"。 */
  const barWidth = (calls: number): number => {
    const top = Math.max(...(usage.data?.by_kind ?? []).map((item) => item.calls), 1)
    return Math.max(3, Math.round((calls / top) * 100))
  }

  return (
    <PageShell title="概览" subtitle={subtitle}>
      <>
        {dashboard.isError && <ErrorLine>{messageOf(dashboard.error)}</ErrorLine>}

        {/* 一、结论：一条仪表带 */}
        <ul className="m-figures stagger">
          {figureSlots.map((slot) => (
            <FigureCell key={slot.label} slot={slot} />
          ))}
        </ul>

        {/* 二、节奏：热力图 + 趋势 */}
        <h2 className="m-group-title">活跃度</h2>
        <div className="panel m-card-block">
          <div className="m-block-head">
            <span className="m-block-title">
              近 {summary?.window_days ?? windowDays} 天入库节奏
            </span>
            <span className="m-heat-legend">
              <HeatLegend />
            </span>
          </div>
          {summary ? (
            /* 窄屏兜底：53 列热力图压进一个窄容器会变成一片噪点，所以给到 720px 的
               最小宽度让卡片内部横向滚动——格子保持可读，信息不丢。900 宽下侧栏
               自动折成 72px，容器约 780px，正好不用滚。 */
            <div className="overflow-x-auto">
              <div className="min-w-[720px]">
                <ActivityHeatmap activity={summary.activity} maxCell={MAX_CELL} />
              </div>
            </div>
          ) : (
            <SkeletonBlock variant="card" rows={1} />
          )}
        </div>

        {summary && (
          <div className="panel m-card-block">
            <div className="m-block-head">
              {/* 时间窗写在块标题上：折线大部分是空的这件事需要一个"这是多长的窗口"来解释 */}
              <span className="m-block-title">趋势 · 近 {summary.window_days} 天</span>
              <div className="m-metric-switch" role="group" aria-label="趋势指标">
                {(Object.keys(METRIC_LABELS) as Metric[]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    className={metric === key ? 'm-metric-tab m-metric-tab-on' : 'm-metric-tab'}
                    aria-pressed={metric === key}
                    onClick={() => setMetric(key)}
                  >
                    {METRIC_LABELS[key]}
                  </button>
                ))}
              </div>
            </div>
            <Suspense fallback={<SkeletonBlock variant="card" rows={1} />}>
              <EChart option={trendOption} height={210} />
            </Suspense>
          </div>
        )}

        {/* 三、构成 */}
        {summary && (
          <>
            <h2 className="m-group-title">构成</h2>
            <div className="m-chart-grid">
              {/*
                阶段分布用**排行条**而不是柱状图：这份数据天然是"几个阶段、各有几篇"，
                而实际使用里绝大多数时候只有"已索引"一类——柱状图会退化成一根孤零零的
                柱子加一片空白。排行条在"只有一类"和"五类都有"两种形态下都读得通。
              */}
              <div className="panel m-card-block flex flex-col">
                <div className="m-block-head">
                  <span className="m-block-title">流水线阶段分布</span>
                </div>
                <div className="flex flex-1 flex-col justify-center">
                  <StageBreakdown stages={stageEntries} />
                </div>
              </div>
              <div className="panel m-card-block">
                <div className="m-block-head">
                  <span className="m-block-title">文件类型</span>
                </div>
                <Suspense fallback={<SkeletonBlock variant="card" rows={1} />}>
                  <EChart option={suffixOption} height={200} />
                </Suspense>
              </div>
            </div>
          </>
        )}

        {/* 四、模型用量。**刻意不算钱**：单价随供应商/版本/缓存/折扣不断变，
            内置价目表必然过期，而过期的价钱比不给更糟。 */}
        <h2 className="m-group-title">模型用量</h2>
        <div className="panel m-card-block">
          <div className="m-block-head">
            <span className="m-block-title">近 {usageDays} 天</span>
          </div>

          <dl className="m-usage-figures">
            {[
              { label: '调用次数', value: usage.data?.total.calls },
              { label: '输入 token', value: usage.data?.total.prompt_tokens },
              { label: '输出 token', value: usage.data?.total.completion_tokens },
              { label: '处理条数', value: usage.data?.total.items },
            ].map((item) => (
              <div key={item.label} className="m-usage-figure">
                <dt>{item.label}</dt>
                <dd className={usagePending ? 'tabular m-figure-pending' : 'tabular'}>
                  {usagePending ? '—' : formatCount(item.value ?? 0)}
                </dd>
              </div>
            ))}
          </dl>

          {usagePending ? (
            <p className="m-usage-empty">正在统计…</p>
          ) : !usage.data || usage.data.total.calls === 0 ? (
            <p className="m-usage-empty">还没有用量记录。提问或上传文档之后这里会有数据。</p>
          ) : (
            <ul className="m-usage-list">
              {usage.data.by_kind.map((item) => (
                <li key={item.kind} className="m-usage-row">
                  <span className="m-usage-name">{item.label}</span>
                  <span className="m-usage-bar">
                    <span
                      className="m-usage-bar-fill"
                      style={{ width: `${barWidth(item.calls)}%` }}
                    />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 五、各库规模 */}
        {summary ? (
          <>
            <h2 className="m-group-title">知识库规模</h2>
            {!hasAnyDocument ? (
              <EmptyState
                title="还没有文档"
                hint="到「所有知识库」建一个库并上传文档，这里会出现规模与活跃度统计。"
              >
                <Button asChild>
                  <Link to="/knowledge-bases">去知识库</Link>
                </Button>
              </EmptyState>
            ) : (
              <div className="panel">
                <div className="panel-head m-list-head" aria-hidden="true">
                  <span className="m-col-name">知识库</span>
                  <span className="m-col-num">文档</span>
                  <span className="m-col-num">切块</span>
                  <span className="m-col-time">最近活动</span>
                </div>
                <ul className="m-list">
                  {summary.knowledge_bases.map((kb) => (
                    <li key={kb.id} className="m-list-item">
                      <Link className="m-kb-link" to={`/kb/${kb.id}`}>
                        <span className="m-col-name">
                          <span className="m-kb-name">{kb.name}</span>
                          <span className="m-kb-model ml-[var(--space-2)]">
                            {kb.embedding_model_id}
                          </span>
                        </span>
                        <span className="m-col-num tabular">{formatCount(kb.documents)}</span>
                        <span className="m-col-num tabular">{formatCount(kb.chunks)}</span>
                        <span className="m-col-time">
                          {kb.last_activity ? (
                            <span className="tabular">{formatRelativeTime(kb.last_activity)}</span>
                          ) : (
                            <StatusTag tone="neutral" label="还没有文档" />
                          )}
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        ) : (
          <SkeletonBlock variant="list" rows={3} />
        )}
      </>
    </PageShell>
  )
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '驾驶舱数据加载失败'
}

/**
 * 阶段分布排行条：一行一个阶段 = 名称 + 占比条 + 数量 + 百分比。
 *
 * 为什么不用柱状图：这份数据天然是"几个阶段、各有几篇"，而实际使用里绝大多数时候
 * 只有"已索引"一类——柱状图会退化成一根孤零零的柱子加一片空白。排行条在
 * "只有一类"和"五类都有"两种形态下都读得通，而且带得出百分比。
 */
function StageBreakdown({ stages }: { stages: Array<[string, number]> }) {
  const total = stages.reduce((sum, [, count]) => sum + count, 0)
  if (total === 0) return <p className="m-usage-empty">还没有文档。</p>
  return (
    <ul className="mt-[var(--space-2)] flex flex-col gap-[var(--space-3)]">
      {stages.map(([stage, count]) => {
        const view = documentStageView(stage)
        const color =
          stage === 'failed'
            ? 'var(--status-danger)'
            : stage === 'indexed' || stage === 'enriched'
              ? 'var(--status-success)'
              : 'var(--text-quaternary)'
        return (
          <li key={stage} className="flex items-center gap-[var(--space-3)]">
            <span className="w-16 shrink-0 text-[length:var(--text-micro-size)] text-text-secondary">
              {view.label}
            </span>
            <span className="h-[6px] flex-1 overflow-hidden rounded-pill bg-[var(--meter-track)]">
              <span
                className="block h-full rounded-pill"
                style={{
                  width: `${Math.max(3, Math.round((count / total) * 100))}%`,
                  background: color,
                }}
              />
            </span>
            <span className="w-14 shrink-0 text-right text-[length:var(--text-meta-size)] tabular text-text-primary">
              {formatCount(count)}
            </span>
            <span className="w-10 shrink-0 text-right text-[length:var(--text-micro-size)] tabular text-text-tertiary">
              {Math.round((count / total) * 100)}%
            </span>
          </li>
        )
      })}
    </ul>
  )
}
