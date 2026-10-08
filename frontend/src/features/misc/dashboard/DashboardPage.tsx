/**
 * 概览 = 驾驶舱（《前端设计规范》§6）——与旧前端 `views/DashboardView.vue` 逐条对应。
 *
 * 回答"这个知识库系统现在什么状态、最近在不在动"，而不是"有哪些库"——
 * 库的清单有自己的页面（侧栏「知识库」）。
 *
 * 三条自我约束：
 * 1. **只画有数据支撑的图**：调用量走 `/stats/usage`；宁可少一块图，也不放一个编出来的数字；
 * 2. **颜色只来自主题 Token**：图表与页面共用一套灰阶 + 语义色，不另立一套"图表配色"；
 * 3. 数字先给**结论**（几个大数），再给**分布**（点状图 / 柱状图 / 趋势）。
 *
 * **图表按需加载**：`EChart` 把整个 ECharts 拉进来（数百 KB），所以这里与热力图
 * 都用 `React.lazy`——静态 import 会让驾驶舱首屏必须先下完这个包；异步化之后
 * 数字卡片与骨架先画出来，图表包到了再补上。
 *
 * ## 知识库那几块按提供者状态分流（本机档的判据）
 *
 * `/stats/dashboard` 数的是**知识库**的文档与任务，而本机档里知识库在**提供者**那台
 * （`backend/app/api/v1/router.py` 的"明确不挂"那一段），这条端点**不挂本机档**——
 * 没接上还硬打它，屏幕上只会是一行 `Not Found` 加一片空白。所以五个大数、活跃度、
 * 构成、知识库规模那几块只在 `kbReady` 时画：还没问出结论时画一个占位（按缺席渲染，
 * 与 `features/knowledge/ProviderRoute.tsx` 同一条口径），问出结论且没接上时换成一块
 * 空态（原因与下一步用后端给的那句 `reason`，这里不另写一句把它盖掉）。
 *
 * **模型用量那一节不受影响**：它走 `/stats/usage`，那是本机挂了的
 * （`local.stats_reads`）——本机的数据不该因为知识库在别处而藏起来。
 */
import { DASHBOARD_WINDOW_DAYS, USAGE_WINDOW_DAYS } from '@/features/misc/queryKeys'
import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { useLocalBackend } from '@/api/local'
import { useKnowledgeProviderStatus } from '@/api/provider'
import type { ActivityPoint } from '@/api/stats'
import { getDashboard, getUsage } from '@/api/stats'
import { Button } from '@/ui/button'
import { formatBytes, formatCount, formatRelativeTime } from '@/lib/format'

import { documentStageView } from '../shared/status'
import { EmptyState, ErrorLine, PageShell, SkeletonBlock, StatusTag } from '../shared/composites'
import { ActivityHeatmap, HeatLegend } from './ActivityHeatmap'

const EChart = lazy(() => import('./EChart').then((module) => ({ default: module.EChart })))

/**
 * 格子边长上限：宽度不够时按列数自动缩小，够宽就吃满一行的宽度。
 *
 * 这个上限**必须跟着卡片宽度走**：365 天是 53 列，上限定死在 14px 时网格只画到
 * 卡片的三分之二，右边空出一大片（界面评审 D4：约 360px 空白）。20px 之下
 * 53 列刚好铺满一行，卡片也不再是一块"半截图 + 半截空"。
 */
const MAX_CELL = 20

/** 趋势图 x 轴最多留几个日期标签（27 个标签会糊成一条，界面评审 D6）。 */
const MAX_TREND_LABELS = 7

const METRIC_LABELS = {
  documents: '入库文档',
  chunks: '新增切块',
  tasks: '流水线任务',
} as const

type Metric = keyof typeof METRIC_LABELS

/**
 * 顶部大数的一个槽位。`note` 是**可缺省**的：没有口径要交代时那一行不出现
 * （见 `figureSlots` 的注释——`全部已索引` / `全部在窗口内` 那一批解释小字已删）。
 */
type FigureSlot = {
  label: string
  value: string
  note?: string
  ready: boolean
  wide: boolean
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

/**
 * x 轴日期标签的抽稀步长（ECharts 的 `axisLabel.interval`，0 = 全标）。
 *
 * 抽的是**标签**不是数据：折线仍按每个点画，只是不再给每一列配一行日期——
 * 27 个 "09-25" 挨在一起会糊成一条灰带，反而读不出任何一天（界面评审 D6）。
 */
export function trendLabelInterval(count: number): number {
  return Math.max(0, Math.ceil(count / MAX_TREND_LABELS) - 1)
}

export function DashboardPage() {
  const windowDays = DASHBOARD_WINDOW_DAYS
  const usageDays = USAGE_WINDOW_DAYS
  const [metric, setMetric] = useState<Metric>('documents')

  /**
   * 这一档的知识库在不在（见文件头那一段）。
   *
   * - 有本机后端（本机档）→ 知识库在**提供者**那台，要握手 `ready` 才算接上；
   * - 没有本机后端（服务器档）→ 知识库就是它自己，`/local/status` 一有结论就算接上。
   *
   * `kbPending` 是"还没问出结论"：那时不画数据块，也不提前判它"没接上"（不猜）。
   */
  const local = useLocalBackend()
  const provider = useKnowledgeProviderStatus()
  const kbReady = local.present ? provider.ready : local.settled
  const kbPending = local.present ? !provider.settled : !local.settled

  const dashboard = useQuery({
    queryKey: ['stats', 'dashboard', windowDays],
    queryFn: () => getDashboard(windowDays),
    // 没接上就**一条都不发**：这条端点不挂本机档，打过去只会是 404
    enabled: kbReady,
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

  /**
   * 顶部大数：每格一个"当前值 + 一句它是什么"。
   *
   * **数据没到也先把六个槽位画出来**（值为占位符、注解用静态说明）：
   * 这是流式渲染的一半——页面的骨架立刻可见，用户不用盯着一块空白等最慢的请求。
   *
   * **注解只在"这一格自己说不清"时才给**（2026-09-24 用户："ai 味道太重，
   * 有很多 ai 味道很重的解释小字"）：`相互隔离的检索范围` / `向量化的最小单位` /
   * `不含向量与索引` / `全部已索引` / `全部在窗口内` 这一批全部删掉了——卡片上已经有
   * 标签（`切块`）与大数字，再加一句同义复述就是"这一页是什么"式的小字。
   * 留下的两类都有判据：**口径**（失败/待索引这类状态读数）与**加载态**（`正在统计…`）。
   * 「近 N 天入库」原来还有一句"N 篇更早入库"：它等于「文档与索引」的总数减这一格的
   * 数（两个数就在同一行的相邻卡片上），补不出来新判断力，也一并删。
   */
  const figureSlots = ((): FigureSlot[] => {
    if (!data) {
      return [
        { label: '知识库', value: '—', ready: false, wide: false },
        { label: '文档与索引', value: '—', note: '正在统计…', ready: false, wide: true },
        { label: '切块', value: '—', ready: false, wide: false },
        { label: '原文体积', value: '—', ready: false, wide: false },
        {
          label: `近 ${windowDays} 天入库`,
          value: '—',
          note: '正在统计…',
          ready: false,
          wide: false,
        },
      ]
    }
    /**
     * 文档与索引合成一张卡（界面评审 D8）。
     *
     * 此前"文档 60 / 已索引 60 篇 / 占全部 60 篇"把同一件事在首屏最强的三个位置说三遍，
     * 而第二张卡的主数字与第三张卡的注解其实都是**同一个数**。合成一张之后：
     * 主数字只留文档总数，注解只说索引的**状态**——失败和待索引都报"还差多少"，
     * 数字只在真的不等于总数时才出现，于是它既不是重复、也能一眼看出有没有活没干完。
     *
     * **全都索完之后不再留一句"全部已索引"**：那是"没有异常"的复述，主数字与
     * 状态都正常时这一格无话可说，就什么都不说（2026-09-24 删解释小字那一批）。
     */
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
        value: formatCount(data.total_knowledge_bases),
        ready: true,
        wide: false,
      },
      {
        label: '文档与索引',
        value: formatCount(data.total_documents),
        note: indexNote,
        ready: true,
        wide: true,
      },
      {
        label: '切块',
        value: formatCount(data.total_chunks),
        ready: true,
        wide: false,
      },
      {
        label: '原文体积',
        value: formatBytes(data.storage_bytes),
        ready: true,
        wide: false,
      },
      {
        label: `近 ${windowDays} 天入库`,
        // 注释必须与**主数字同义**：任务在跑的信息属于任务中心，不该蹭这张卡
        value: formatCount(data.recent_documents),
        ready: true,
        wide: false,
      },
    ]
  })()

  /** 按天趋势：观察窗口太长时按周聚合，否则折线全是锯齿。 */
  const trendOption = (() => {
    const points: ActivityPoint[] = data?.activity ?? []
    const { labels, values } = checkableTrend(points, metric)
    return {
      animationDuration: 300,
      grid: { left: 44, right: 12, top: 16, bottom: 24 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: labels,
        boundaryGap: false,
        // 抽稀只作用于标签：数据点一个不少，折线形状不变
        axisLabel: { interval: trendLabelInterval(labels.length) },
      },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          type: 'line',
          smooth: true,
          symbol: 'none',
          data: values,
          lineStyle: { width: 2, color: 'var(--accent)' },
          areaStyle: { color: 'var(--accent)', opacity: 0.1 },
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
      animationDuration: 300,
      // 右侧那 40px 是给条形末端的数值标签留的：标签画在绘图区**外面**，
      // 留 24px 时它顶到卡片边框只剩 2–6px，右侧看起来像溢出了（界面评审 D10）
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
          barWidth: 12,
          // 文件类型是"第一组数据"，用副色：主色留给趋势与活跃度
          itemStyle: { color: 'var(--accent-2)', borderRadius: 3 },
          label: { show: true, position: 'right', color: 'var(--text-secondary)', fontSize: 11 },
        },
      ],
    }
  })()

  /** 阶段分布：让"卡在哪一步"一眼可见，而不是只给一个完成率。 */
  const stageOption = (() => {
    const entries = Object.entries(data?.by_stage ?? {}).filter(([, count]) => count > 0)
    return {
      animationDuration: 300,
      tooltip: { trigger: 'item' },
      xAxis: {
        type: 'category',
        data: entries.map(([stage]) => documentStageView(stage).label),
        axisLabel: { interval: 0, fontSize: 10 },
      },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          type: 'bar',
          data: entries.map(([stage, count]) => ({
            value: count,
            itemStyle: {
              // 阶段分布里"失败"必须是红色；"已索引"用成功色；其余中间态用中性灰，
              // 避免整张图变成一根彩虹柱
              color:
                stage === 'failed'
                  ? 'var(--status-danger)'
                  : stage === 'indexed' || stage === 'enriched'
                    ? 'var(--status-success)'
                    : 'var(--text-tertiary)',
              borderRadius: [3, 3, 0, 0],
            },
          })),
          barMaxWidth: 36,
        },
      ],
    }
  })()

  /** 按用途画个相对的条形——一眼看出"钱花在哪一类调用上"。 */
  const barWidth = (calls: number): number => {
    const top = Math.max(...(usage.data?.by_kind ?? []).map((item) => item.calls), 1)
    return Math.max(2, Math.round((calls / top) * 100))
  }

  return (
    <PageShell title="概览">
      {/*
        知识库那几块的三态（判据见组件上面那一小段）：
        还没问出结论 → 一个占位；问出来且没接上 → 一块空态（原因在后端那句 `reason` 里）；
        接上了 → 与原来**一字不差**地画全套（含错误路径那条 `ErrorLine`）。
      */}
      {kbPending ? (
        <p data-testid="dashboard-kb-pending" className="text-text-tertiary">
          正在确认知识库连接…
        </p>
      ) : !kbReady ? (
        <EmptyState
          title="知识库还没接上"
          hint={provider.reason || provider.error || '这台机器还没接上知识库'}
        />
      ) : (
        <>
          {dashboard.isError && <ErrorLine>{messageOf(dashboard.error)}</ErrorLine>}

          {/* 一、结论：六个大数 */}
          <ul className="m-figures">
            {figureSlots.map((item) => (
              <li key={item.label} className={item.wide ? 'm-figure m-figure-wide' : 'm-figure'}>
                <span className="m-figure-label">{item.label}</span>
                <span
                  className={
                    item.ready
                      ? 'm-figure-value tabular'
                      : 'm-figure-value tabular m-figure-pending'
                  }
                >
                  {item.value}
                </span>
                {/* 注解是**可选**的：没有异常、没有口径要交代时就不摆这一行
                （`全部已索引` / `全部在窗口内` 那一批解释小字已删） */}
                {item.note ? <span className="m-figure-note">{item.note}</span> : null}
              </li>
            ))}
          </ul>

          {/* 二、节奏：点状图 + 趋势 */}
          <h2 className="m-group-title">活跃度</h2>
          <div className="panel m-card-block">
            <div className="m-block-head">
              <span className="m-block-title">
                近 {summary?.window_days ?? windowDays} 天入库节奏
              </span>
              {/* 图例只留**色阶样本**：样本本身已经把"深=多"说清楚了，再加一句
              "颜色越深表示当天入库越多"就是替图形说话（2026-09-24 删解释小字）。
              方块与图上的分档同源（HEAT_STEPS）。这里不再带 `m-block-hint`：
              那个类只管文字的字号与颜色，而这一格已经没有字了。 */}
              <span className="m-heat-legend">
                <HeatLegend />
              </span>
            </div>
            {summary ? (
              <ActivityHeatmap activity={summary.activity} maxCell={MAX_CELL} />
            ) : (
              <SkeletonBlock variant="card" rows={1} />
            )}
          </div>

          {summary && (
            <div className="panel m-card-block">
              <div className="m-block-head">
                <span className="m-block-title">趋势</span>
                <div className="m-metric-switch">
                  {(Object.keys(METRIC_LABELS) as Metric[]).map((key) => (
                    <button
                      key={key}
                      type="button"
                      className={metric === key ? 'm-metric-tab m-metric-tab-on' : 'm-metric-tab'}
                      onClick={() => setMetric(key)}
                    >
                      {METRIC_LABELS[key]}
                    </button>
                  ))}
                </div>
              </div>
              <Suspense fallback={<SkeletonBlock variant="card" rows={1} />}>
                <EChart option={trendOption} height={200} />
              </Suspense>
            </div>
          )}

          {/* 三、构成 */}
          {summary && (
            <>
              <h2 className="m-group-title">构成</h2>
              <div className="m-chart-grid">
                <div className="panel m-card-block">
                  <div className="m-block-head">
                    <span className="m-block-title">流水线阶段分布</span>
                  </div>
                  <Suspense fallback={<SkeletonBlock variant="card" rows={1} />}>
                    <EChart option={stageOption} height={200} />
                  </Suspense>
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
        </>
      )}

      {/* 三、模型用量。**刻意不算钱**：单价随供应商/版本/缓存/折扣不断变，
          内置价目表必然过期，而过期的价钱比不给更糟——用户会照着它做决定。
          **这一节与知识库接没接上无关**：用量是本机的数据（`/stats/usage`） */}
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
                {/* 这一行原先还挂着一串数（`428 次 · 共 4,646 条` / `输入 … 输出 …`）：
                    条只负责"相对量"，绝对数在上方那四个大数里已经给过一遍（2026-09-24 删） */}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 四、各库规模（与上面那几块同一档：没接上时整块不画） */}
      {kbReady &&
        (summary ? (
          <>
            <h2 className="m-group-title">知识库规模</h2>
            {!hasAnyDocument ? (
              <EmptyState
                title="还没有文档"
                hint="到「知识库」页建一个库并上传文档，这里会出现规模与活跃度统计。"
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
                          {/* 模型 id 与库名之间**必须有缝**：`.m-col-name` 是纯 flex:1 容器，
                            两个 span 当行内元素排会贴在一起（`城市建成环境研究现状BAAI/bge-m3`
                            ——2026-09-24 用户截图圈的就是这处排版缺陷）。缝写在调用点：
                            工具类压 `@layer components` 里的 `.m-kb-model`，不会打架 */}
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
          // 驾驶舱数据还没到：这一块先给骨架，标题已经在上面了
          <SkeletonBlock variant="list" rows={3} />
        ))}
    </PageShell>
  )
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '驾驶舱数据加载失败'
}
