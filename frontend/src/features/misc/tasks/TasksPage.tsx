/**
 * 任务中心（界面规则6）——与旧前端 `views/TasksView.vue` 逐条对应。
 *
 * 每行一个任务：图标 + 类型 + 关联文档 + 状态 + **健康判据** + 尝试次数 + 时间。
 * 有任务在跑时自动刷新——否则用户只能不停手动点刷新。
 *
 * **健康列（M7 / T7.4）不是装饰**：`running` 这个状态本身说明不了任何事——
 * 一个跑了 5 秒的和一个卡了两小时的看起来完全一样。后端按租约是否续上算出
 * "可能卡住 / 长时间未执行"，这里负责把它显示出来。
 *
 * 缓存口径与旧 store 一致：**列表整份缓存在 query 里**，筛选/分页都在客户端做
 * （任务量级在几百以内）；轮询期间旧数据继续显示，不闪骨架屏。
 *
 * ## 2026-10 重做的排布
 *
 * ```
 * 任务中心                                  [有任务在跑，自动刷新中] [刷新]
 * 246 个任务 · 12 个失败 · 3 个排队 · 1 个执行中
 * 运行负载                                   ← 仪表卡（管理员才有）
 * [CPU] [内存] [并发槽位] [云端解析额度] [常驻内存]
 * [按知识库▾] [按状态▾] [按健康▾]  □显示已取消  …  66 / 246 项
 * ┌ 类型 │ 任务 │ 状态 │ 健康 │ 尝试次数 │ 更新时间 ┐
 * │ 失败行：淡红底 + 左侧危险色竖条（不靠文字颜色单独表达）│
 * └ 20 行一页，容器定高 8 行，翻页器常显 ┘
 * ```
 *
 * 页头是主角（28px 标题 + 一句现状 + 右侧主按钮），下面的负载卡与列表安静地排开；
 * 原来那层多余的 `.page-shell-body` 已经去掉——`PageShell` 自己就带这一层，
 * 套两层等于页头到内容多空了一倍。
 */
import { TASKS_QUERY_KEY } from '@/features/misc/queryKeys'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, FileText, RefreshCw } from 'lucide-react'
import { useNavigate } from 'react-router'

import { cancelTasks, getTaskLoad, listTasks, type TaskSummary } from '@/api/tasks'
import { formatCount, formatDate } from '@/lib/format'
import { useSessionStore } from '@/lib/session'
import { cn } from '@/lib/utils'

import { useKnowledgeBases } from '../shared/knowledgeBases'
import { isTaskProblem, taskHealthTone, taskKindLabel, taskStateView } from '../shared/status'
import { notifyError, notifySuccess } from '../shared/toast'
import { Button } from '@/ui/button'
import {
  CheckRow,
  ConfirmDialog,
  EmptyState,
  ErrorLine,
  Modal,
  OptionSelect,
  PageShell,
  SkeletonBlock,
  StatusTag,
  type TagTone,
} from '../shared/composites'
import { LoadPanel } from './LoadPanel'

const POLL_INTERVAL_MS = 2000
/** 任务列表原先一次铺满（几百条时滚不到底），与文档列表同一套口径。 */
const PAGE_SIZE = 20

const TASK_LOAD_QUERY_KEY = ['tasks', 'load'] as const

const STATE_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '执行中' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'canceled', label: '已取消' },
]

/**
 * 「按健康筛选」的选项。
 *
 * **不给状态列的词留位置**：健康列回答的是"有没有问题"，而"执行中 / 排队中"是
 * **状态**列的说法——在这份选项里再放一遍，就等于把那两列的同一个词又搬进筛选
 * （第六批：此前这里逐字重复了状态筛选的两档）。合并成「无异常」之后，
 * 剩下两档正是行上会写出来的那两句判定结论。
 *
 * 与行上的对应关系：筛「可能卡住 / 长时间未执行」= 行上写那两个词的行；
 * 筛「无异常」= 行上健康列是中性记号（没有话要说）的行。
 */
const HEALTH_OPTIONS = [
  { value: '', label: '全部健康' },
  { value: 'ok', label: '无异常' },
  { value: 'stalled', label: '可能卡住' },
  { value: 'overdue', label: '长时间未执行' },
]

/**
 * 健康筛选的判定。`ok` 是"还没结束且没有问题"那一档——后端在这一档给
 * `running` 或 `idle` 两种取值（都表示租约正常、没排太久）。
 */
function matchesHealth(task: TaskSummary, filter: string): boolean {
  if (filter === '') return true
  if (filter === 'ok') return task.health === 'running' || task.health === 'idle'
  return task.health === filter
}

export function TasksPage() {
  const navigate = useNavigate()

  const [kb, setKb] = useState('')
  const [state, setState] = useState('')
  const [health, setHealth] = useState('')
  /**
   * 是否把**已取消**的任务也列出来（默认不显示）。
   *
   * 取消是"我不等了"的动作，撤下来的那批会立刻把列表淹掉——实测一次批量撤下
   * 留下 23 行"已取消"。但它们不能永久藏起来（排查时要看得到），
   * 所以给一个显式开关，并在工具栏上如实说明"藏了多少条"。
   */
  const [showCanceled, setShowCanceled] = useState(false)
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<TaskSummary | null>(null)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [canceling, setCanceling] = useState(false)
  /**
   * 列表滚到底了没有（决定底部那层渐隐出不出现）。
   * 初值是 `true`：首帧还没量过，宁可不显示——一层渐隐压在最后一行上，
   * 比"该出现时晚半帧"要误导得多。
   */
  const [listAtEnd, setListAtEnd] = useState(true)
  const listRef = useRef<HTMLDivElement | null>(null)

  /**
   * **这一处刻意不用共享那条判据**：它管的是**「运行负载」**（`/tasks/load`）那一块——
   * 这个端点对非管理员是 403（机器资源与运维参数不给成员看），
   * 明知会被拒还发请求只会让控制台多一串红字。
   */
  const isAdmin = useSessionStore((store) => store.currentUser?.role === 'admin')
  const knowledgeBases = useKnowledgeBases()

  const list = useQuery({
    queryKey: TASKS_QUERY_KEY,
    queryFn: () => listTasks(),
    // 轮询的**开与关**由"有没有任务在跑"决定：空闲时不发请求是有意的
    // （旧 `usePolling` 的 `active`），切换标签页看不见时 react-query 自己会停表，
    // 切回来立刻刷一次——这正是旧版 `visibilitychange` 那段的手工活。
    refetchInterval: (query) => tasksRefetchInterval(query.state.data?.items),
    refetchOnWindowFocus: true,
  })

  const running = hasActive(list.data?.items)

  /**
   * 运行负载（12.115）——**只管理员拉**：这个端点对成员是 403
   * （机器资源与运维参数不给成员看），明知会被拒还发请求只会让控制台多一串红字。
   *
   * 与列表**同一个节拍**（同一个轮询间隔，而不是另起一个计时器）：
   * 两个计时器会让"列表说有 3 个在跑"和面板上"在跑 1"出现在同一帧里对不上，
   * 而这两个数正是要合起来读的（"队列深 + 槽位满"才是结论）。
   */
  const load = useQuery({
    queryKey: TASK_LOAD_QUERY_KEY,
    queryFn: getTaskLoad,
    // 管理员专属（对成员是 403）
    enabled: isAdmin,
    refetchInterval: running ? POLL_INTERVAL_MS : false,
    // 负载读不到不该影响任务列表：它是解释性的附加信息，而列表才是主体
    // （旧版把这里的异常整个吞掉，这里靠"不渲染错误态"表达同一件事）
    retry: false,
  })

  const tasks = useMemo(() => list.data?.items ?? [], [list.data])
  const error = list.isError ? messageOf(list.error, '任务列表加载失败') : ''
  const loading = list.isLoading

  /**
   * 页头那句现状要用的三个数，**全从这一页已有的列表里算**：
   * 不另发请求（也就不会引出一个非管理员拉不到的端点），不编，也不为了页头多算一份缓存。
   *
   * 失败与排队是"要不要现在动手"的判据，所以 0 也照常写出来（"0 个失败"本身就是结论）；
   * 执行中只在真有任务在跑时补一句——没有在跑的时候，那个 0 不提供新信息。
   */
  const counts = useMemo(() => {
    let failed = 0
    let pending = 0
    let runningCount = 0
    for (const task of tasks) {
      if (task.state === 'failed') failed += 1
      else if (task.state === 'pending') pending += 1
      else if (task.state === 'running') runningCount += 1
    }
    return { failed, pending, running: runningCount }
  }, [tasks])

  const subtitle = list.data
    ? `${formatCount(tasks.length)} 个任务 · ${formatCount(counts.failed)} 个失败 · ${formatCount(counts.pending)} 个排队` +
      (counts.running > 0 ? ` · ${formatCount(counts.running)} 个执行中` : '')
    : loading
      ? '正在读取任务…'
      : ''

  /** 工具栏上那条提示：藏了多少条（0 就不显示）。 */
  const hiddenCanceled = showCanceled ? 0 : tasks.filter((task) => task.state === 'canceled').length

  const kbOptions = useMemo(
    () => [
      { value: '', label: '全部知识库' },
      ...knowledgeBases.items.map((item) => ({ value: item.id, label: item.name })),
    ],
    [knowledgeBases.items],
  )

  const hasFilter = kb !== '' || state !== '' || health !== '' || showCanceled

  /** 任务量级在几百以内，筛选在客户端做：不必为它再加一版后端查询参数。 */
  const visibleTasks = useMemo(
    () =>
      tasks.filter(
        (task) =>
          (kb === '' || task.knowledge_base_id === kb) &&
          (state === '' || task.state === state) &&
          (health === '' || matchesHealth(task, health)) &&
          // 默认不看已取消；显式选了「已取消」这个状态时当然要显示
          (showCanceled || state === 'canceled' || task.state !== 'canceled'),
      ),
    [tasks, kb, state, health, showCanceled],
  )

  const pageCount = Math.max(1, Math.ceil(visibleTasks.length / PAGE_SIZE))
  const pagedTasks = visibleTasks.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  /** 筛选一变就回第 1 页：否则会停在"新条件下不存在的那一页"上，看起来像列表空了。 */
  useEffect(() => {
    setPage(1)
  }, [kb, state, health, showCanceled])

  // 页数缩了（任务被撤下）之后当前页可能越界，拉回最后一页
  useEffect(() => {
    setPage((current) => Math.min(current, pageCount))
  }, [pageCount])

  /**
   * 换页之后把列表滚回顶部：停在上一页的滚动位置上，第一行会像是被跳过了。
   * **只挂在 `page` 上**——挂 `visibleTasks` 的话，轮询期间任务一变就把用户从
   * 正在看的那几行上拽回顶部。
   */
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = 0
  }, [page])

  /** 换页或换筛选之后重新量一次列表的滚动位置：内容换了，渐隐该不该出现也跟着变。 */
  useEffect(() => {
    const element = listRef.current
    if (!element) return
    setListAtEnd(scrolledToEnd(element))
  }, [page, visibleTasks])

  /**
   * 撤下任务。
   *
   * **逐条结果**：批量里"30 条撤下 28 条"是正常结果（有的刚好跑完了），
   * 所以按成功/失败分别报，并把第一条失败原因带出来——只报总数等于让人自己找。
   */
  async function runCancel(taskIds?: string[]): Promise<void> {
    if (canceling) return
    setCanceling(true)
    setCancelOpen(false)
    try {
      const result = await cancelTasks(taskIds ? { taskIds } : {})
      setDetail(null)
      await list.refetch()
      if (result.failed === 0) {
        notifySuccess(`已撤下 ${formatCount(result.succeeded)} 个任务`)
        return
      }
      const firstError = result.items.find((item) => !item.ok)?.error
      notifyError(
        `撤下：${formatCount(result.succeeded)} 个成功、${formatCount(result.failed)} 个未撤下` +
          (firstError ? `（${firstError}）` : ''),
      )
    } catch (cause) {
      notifyError(messageOf(cause, '取消任务失败'))
    } finally {
      setCanceling(false)
    }
  }

  const clearFilters = () => {
    setKb('')
    setState('')
    setHealth('')
    setShowCanceled(false)
  }

  const refresh = () => {
    void list.refetch()
    if (isAdmin) void load.refetch()
  }

  return (
    <PageShell
      title="任务中心"
      subtitle={subtitle}
      actions={
        <>
          {running && <StatusTag tone="info" live label="有任务在跑，自动刷新中" />}
          <Button variant="secondary" onClick={refresh}>
            <RefreshCw size={14} />
            刷新
          </Button>
        </>
      }
    >
      <>
        {/*
            运行负载放在最上面：它解释的是"为什么后台慢"，而那正是用户打开这一页时
            的问题——排在列表下方的话，他要先翻过几十行任务才看得到（管理员专属）。
          */}
        {isAdmin && <LoadPanel load={load.data ?? null} live={running} />}

        {error && <ErrorLine>{error}</ErrorLine>}
        {loading && <SkeletonBlock variant="list" rows={5} />}

        {!loading && tasks.length === 0 && (
          <EmptyState
            title="还没有任务"
            hint="上传文档后会在这里看到探测、解析、切分、向量化各步骤的进展。"
          />
        )}

        {!loading && tasks.length > 0 && (
          <>
            <div className="m-toolbar">
              <div className="m-filter-select">
                <OptionSelect
                  value={kb}
                  onValueChange={setKb}
                  options={kbOptions}
                  label="按知识库筛选"
                />
              </div>
              <div className="m-filter-select">
                <OptionSelect
                  value={state}
                  onValueChange={setState}
                  options={STATE_OPTIONS}
                  label="按状态筛选"
                />
              </div>
              <div className="m-filter-select">
                <OptionSelect
                  value={health}
                  onValueChange={setHealth}
                  options={HEALTH_OPTIONS}
                  label="按健康筛选"
                />
              </div>
              {/* 已取消默认不显示：给一个显式开关，并如实说藏了多少条。
                    右边那个数**不再复述"已取消"**——勾选框的标签已经说过了
                    （原来是「显示已取消 已隐藏 23 条已取消」，同一句说了两遍，2026-09-24 改） */}
              <CheckRow checked={showCanceled} onCheckedChange={setShowCanceled}>
                显示已取消
              </CheckRow>
              {/* 失败是这一页唯一"要人现在动手"的状态：给一个一键只看失败的入口，
                  不用先去状态下拉里找。再点一次回全部。 */}
              {counts.failed > 0 && (
                <Button
                  variant={state === 'failed' ? 'default' : 'secondary'}
                  size="sm"
                  aria-pressed={state === 'failed'}
                  onClick={() => setState(state === 'failed' ? '' : 'failed')}
                >
                  只看失败（{formatCount(counts.failed)}）
                </Button>
              )}
              {hiddenCanceled > 0 && (
                <span className="m-toolbar-note">已取消 {formatCount(hiddenCanceled)}</span>
              )}
              {hasFilter && (
                <Button variant="secondary" size="sm" onClick={clearFilters}>
                  清除筛选
                </Button>
              )}
              {/* 唯一能真正"给队列踩刹车"的地方：几十条 pending 堵着时，
                    逐篇取消文档是做不到的（用户反馈） */}
              {counts.pending > 0 && (
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={canceling}
                  onClick={() => setCancelOpen(true)}
                >
                  取消排队中的任务（{formatCount(counts.pending)}）
                </Button>
              )}
              <span className="m-toolbar-count tabular">
                {formatCount(visibleTasks.length)} / {formatCount(tasks.length)} 项
              </span>
            </div>

            <div className="panel">
              {/*
                  表头**逐格对着行内的格子**（评审 T3：原表头 5 格、行内 7 件，且「任务」还带
                  `padding-left: 16px + gap` 去让图标，于是它正好压在每行的类型标签「解析」上，
                  「状态」也比徽章左边缘多出 30px）。
                  现在两边共用同一套列宽：类型列（图标 16 + gap 12 + 标签 56 = 84）、
                  标题列（`flex: 1`，与 `.m-row-main` 同一套负外边距 + 内边距）、
                  状态 84 / 健康 92 / 尝试次数 100 / 更新时间 120——列宽只有一处定义。
                  类型列给了表头「类型」而不是留白：这一列有内容（解析 / 切分 / 向量化），
                  没有表头的话读者只能靠猜它与标题的关系。
                */}
              <div className="panel-head m-list-head min-w-[680px]" aria-hidden="true">
                <span className="m-head-kind">类型</span>
                <span className="m-head-task">任务</span>
                <span className="m-head-col-status">状态</span>
                <span className="m-head-col-health">健康</span>
                <span className="m-head-col-attempts">尝试次数</span>
                <span className="m-row-time">更新时间</span>
              </div>

              {visibleTasks.length === 0 ? (
                <p className="m-filter-empty">
                  没有符合筛选条件的任务。换个条件，或点右上角「清除筛选」。
                </p>
              ) : (
                /*
                    列表有**确定的高度上限**（8 行 ≈ `--row-height * 8`）并自己滚动
                    （评审 T6：此前整页滚，第 13 行被视口从中间切断，翻页器落到折线以下——
                    "还有多少"看不出来）。定高之后：表头留在面板里不动、翻页器回到首屏、
                    "共 N 项 / 第几页"常显；行数不足 8 行时容器按内容收缩，不留一块空白。
                    行内是按钮（可聚焦），键盘 Tab 到没露出来的那几行时浏览器会自己把这一格
                    滚进来，所以这个滚动容器不需要 `tabIndex` 去抢一个焦点位（8）。
                    底部那层渐隐只在**下面确实还有没露出来的行**时出现。
                  */
                <div className="relative min-w-[680px]">
                  <div
                    ref={listRef}
                    onScroll={(event) => setListAtEnd(scrolledToEnd(event.currentTarget))}
                    className="max-h-[calc(var(--row-height)*8)] overflow-y-auto"
                  >
                    <ul className={cn('m-list', page === 1 && 'stagger')}>
                      {pagedTasks.map((task) => (
                        <li key={task.id} className={cn('m-list-item', rowToneClass(task))}>
                          <div className="m-task-row panel-row">
                            <FileText className="m-row-icon" size={16} />
                            <span className="m-row-kind">{taskKindLabel(task.kind)}</span>

                            {/*
                              整行可点是这里最要紧的交互：任务名只有十几个字宽，
                              而"看失败原因"是这一页唯一的深层动作，命中区不该只有一个词那么大。
                              里面是个真 button，所以 Tab 能到、回车能开（8 键盘可达）。
                              */}
                            <button
                              type="button"
                              className="m-row-main"
                              aria-label={`查看任务详情：${taskKindLabel(task.kind)} ${documentName(task)}`}
                              onClick={() => setDetail(task)}
                            >
                              <span className="m-row-name">{documentName(task)}</span>
                              {needsAttention(task) && (
                                <span className="m-row-attention">
                                  {attentionText(task)}
                                  <ChevronRight size={12} />
                                </span>
                              )}
                            </button>

                            {/*
                                状态与健康各自包在**固定列宽**里（`.m-col-status` 84 / `.m-col-health` 92，
                                与表头同宽）：徽章宽度随文案变（「执行中」比「已完成」多一个脉动点），
                                不固定列宽的话后面的列会跟着徽章一起左右挪，表头永远对不上。
                              */}
                            <span className="m-col-status">
                              <StatusTag
                                label={taskStateView(task.state).label}
                                tone={taskStateView(task.state).tone}
                                live={task.state === 'running'}
                              />
                            </span>
                            <span className="m-col-health">{healthCell(task)}</span>
                            <span className="m-row-attempts">{attemptText(task)}</span>
                            <span className="m-row-time">{formatDate(task.updated_at)}</span>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                  {!listAtEnd && (
                    <div
                      aria-hidden="true"
                      className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-[var(--bg-subtle)] to-transparent"
                    />
                  )}
                </div>
              )}
            </div>

            {/* 分页：**总数常显、只藏翻页控件**，与文档列表同一套口径 */}
            {visibleTasks.length > 0 && (
              <div className="m-pager">
                <span className="m-pager-total">共 {formatCount(visibleTasks.length)} 项</span>
                {pageCount > 1 && (
                  <div className="m-pager-controls">
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={page <= 1}
                      onClick={() => setPage((current) => Math.max(1, current - 1))}
                    >
                      <ChevronLeft size={14} />
                      上一页
                    </Button>
                    <span className="m-pager-page tabular">
                      第 {page} / {pageCount} 页
                    </span>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={page >= pageCount}
                      onClick={() => setPage((current) => Math.min(pageCount, current + 1))}
                    >
                      下一页
                      <ChevronRight size={14} />
                    </Button>
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {/*
            任务详情。**为什么必须有个弹窗而不是 title 属性**：失败原因常常是一整段
            （含上游 URL、错误码、JSON 片段），`title` 一移开鼠标就没了，
            也没法选中复制去查——而"拿这段去搜"正是用户下一步要做的事。
          */}
        <Modal
          open={detail !== null}
          title="任务详情"
          onClose={() => setDetail(null)}
          footer={
            detail ? (
              <>
                <Button onClick={() => setDetail(null)}>关闭</Button>
                {canCancel(detail) && (
                  <Button
                    variant="destructive"
                    disabled={canceling}
                    onClick={() => void runCancel([detail.id])}
                  >
                    取消这个任务
                  </Button>
                )}
                {detail.document_id && (
                  <Button
                    onClick={() => {
                      const documentId = detail.document_id
                      setDetail(null)
                      if (documentId) void navigate(`/documents/${documentId}`)
                    }}
                  >
                    打开文档
                  </Button>
                )}
              </>
            ) : null
          }
        >
          {detail && (
            <>
              <dl className="m-detail-grid">
                <div>
                  <dt>任务</dt>
                  <dd>
                    {taskKindLabel(detail.kind)} · {documentName(detail)}
                  </dd>
                </div>
                <div>
                  <dt>状态</dt>
                  <dd>{taskStateView(detail.state).label}</dd>
                </div>
                <div>
                  <dt>健康</dt>
                  {/* 与行内**同一处判定**（`healthVerdict`）：同一个任务在两处不能说两句话。
                        「没有需要注意的」这一档在这里也只是一枚记号——它上面那行的 `dt`
                        就是列头那个词，读者不会把"—"读成缺失。 */}
                  <dd>{healthText(detail)}</dd>
                </div>
                <div>
                  <dt>尝试次数</dt>
                  <dd className="tabular">{attemptText(detail)}</dd>
                </div>
                <div>
                  <dt>创建</dt>
                  <dd>{formatDate(detail.created_at)}</dd>
                </div>
                <div>
                  <dt>更新</dt>
                  <dd>{formatDate(detail.updated_at)}</dd>
                </div>
                {detail.lease_expires_at && (
                  <div>
                    <dt>租约到期</dt>
                    <dd>{formatDate(detail.lease_expires_at)}</dd>
                  </div>
                )}
              </dl>

              {/* 原因单独成块：它是这个弹窗里唯一需要**读**的东西 */}
              {(detail.error || detail.health_detail) && (
                <>
                  <h3 className="m-block-title">
                    {detail.state === 'failed' ? '失败原因' : '健康判据说明'}
                  </h3>
                  <pre className="m-detail-error">{detail.error || detail.health_detail}</pre>
                </>
              )}

              <p className="m-detail-id">
                任务 ID <code>{detail.id}</code>。排查日志时用它去搜。
              </p>
            </>
          )}
        </Modal>

        {/* 批量撤下要二次确认：这是"把几十篇的处理全叫停"，点错了代价不小。
              文案里说清"文档不会被删/不会变状态"，因为那正是用户担心的 */}
        <ConfirmDialog
          open={cancelOpen}
          title="取消排队中的任务"
          lead={`撤下 ${formatCount(counts.pending)} 个还在排队的任务？`}
          note="只是不再处理：文档与已入库内容都保留，之后可重新上传或点「重新摄入」。正在执行的任务不在此列。"
          confirmLabel="撤下"
          busy={canceling}
          onCancel={() => setCancelOpen(false)}
          onConfirm={() => void runCancel()}
        />
      </>
    </PageShell>
  )
}

function hasActive(items: TaskSummary[] | undefined): boolean {
  return (items ?? []).some((task) => task.state === 'pending' || task.state === 'running')
}

/**
 * 滚动容器到底了没有。留 4px 容差：浏览器在小数行高/缩放下报的 `scrollHeight`
 * 常比"最后一行正好露完"多零点几像素，不留容差会让渐隐在到底之后还赖着不走。
 */
function scrolledToEnd(element: HTMLElement): boolean {
  return element.scrollTop + element.clientHeight >= element.scrollHeight - 4
}

/**
 * 轮询间隔：**只在有任务在跑时才开**（空闲时不发请求是有意的，12.116）。
 *
 * 抽成纯函数导出，是让这条口径能被用例钉住——它正是"别把轮询丢了"那件事本身。
 */
export function tasksRefetchInterval(items: TaskSummary[] | undefined): number | false {
  return hasActive(items) ? POLL_INTERVAL_MS : false
}

function documentName(task: TaskSummary): string {
  if (!task.document_id) return '—'
  // 后端解析好的名字；理论上不会为空，为空时退回 id 而不是显示空白
  return task.document_name || task.document_id
}

/**
 * 尝试次数：**成功也照实显示"1 / 5"**，不再换成"一次通过"。
 *
 * 原来成功行写"一次通过"、其余写"第 N / M 次尝试"，同一列出现两种句式，
 * 列头叫「尝试」却读不出它到底是次数还是结论。统一成次数之后这一列只有一个含义。
 *
 * 斜杠两侧带空格：全站的配比都写 `a / b`（列表的「66 / 89 项」、翻页的「第 1 / 4 页」、
 * 负载环里的「0 / 1」、对话页的「1,234 / 32,768 tokens」），一个写法走到底。
 */
function attemptText(task: TaskSummary): string {
  return `${formatCount(task.attempts)} / ${formatCount(task.max_attempts)}`
}

/**
 * 健康这一格 / 这一行该说什么——**行内与详情弹窗共用这一处判定**。
 * 两处各写一遍的话就会出现"行上是中性记号、弹窗里写着已取消"这种自己跟自己不一致的状态。
 *
 * 三种结果：
 * - `problem`：有问题的两档（可能卡住 / 长时间未执行），逐字用后端给的判定结论；
 * - `word`：失败。它是终态里唯一带来新判断的——状态列说"这件事没成"，
 *   这里说"这一轮**已经结束**，不必再等"；
 * - `quiet`：没有新判断可说，不写词（由调用点画成中性记号）。
 */
type HealthVerdict =
  | { kind: 'problem'; label: string; tone: TagTone }
  | { kind: 'word'; label: string }
  | { kind: 'quiet' }

/**
 * 健康列的判定。**两列说的是两件事**：状态列说"这个任务处在哪个状态"（排队中 / 执行中 /
 * 已完成 / 失败 / 已取消），健康列说"**有没有问题**"——后端按租约续没续、排了多久算出来的
 * 判定（执行中 / 排队中 / 可能卡住 / 长时间未执行 / 已结束）。所以：
 *
 * 1. **有问题的两档照字写**：它们正是健康列存在的理由（"执行中"与"可能卡住"看起来一样，
 *    这一列负责分开它们），而且「按健康筛选」里就有那两档——筛了却在行上看不到那个词，
 *    筛选结果就没法核对；
 * 2. **没有问题的档不写词**：在跑的两档（后端的 label 是"执行中" / "排队中"）与已结束的
 *    两档（"已完成" / "已取消"）都退成同一个中性记号。它们的 label 与状态列**逐字相同**，
 *    写出来就是同一行两个徽章说同一件事、一个字的新判断都没有（评审 T2；第一批只收了
 *    succeeded、第三批收了 canceled，这一批把在跑的两档也收掉——它们当时留着是为了让
 *    健康筛选的数在行上看得见，现在筛选那一侧改成了「无异常」）；
 * 3. **`failed` 是终态里唯一的例外**：保留"已失败"（见上面 `word` 那条）。
 *
 * 判定按 `health` / `state` 的取值点名，而不是"看 label 长什么样"：后端换文案时这里的
 * 取舍会重新被看见，而不会跟着一起变。
 */
function healthVerdict(task: TaskSummary): HealthVerdict {
  if (isTaskProblem(task.health)) {
    return { kind: 'problem', label: task.health_label, tone: taskHealthTone(task.health) }
  }
  if (task.state === 'failed') return { kind: 'word', label: task.health_label }
  return { kind: 'quiet' }
}

/**
 * 健康列的单元格。
 *
 * 中性记号不带词，但不能没有名字：列头是 `aria-hidden` 的，读屏器只能靠 `aria-label`
 * 知道这一格是什么。`role="img"` 是让那个名字真的被读出来——`generic` 角色上的
 * `aria-label` 按 ARIA 规范不参与命名。
 */
function healthCell(task: TaskSummary) {
  const verdict = healthVerdict(task)
  if (verdict.kind === 'problem') return <StatusTag label={verdict.label} tone={verdict.tone} />
  if (verdict.kind === 'word') return <span className="m-row-health-done">{verdict.label}</span>
  return (
    <span className="m-row-health-done" role="img" aria-label="健康">
      —
    </span>
  )
}

/** 弹窗那一行要写什么字：与行内同一处判定的文本形态（`quiet` 就是那枚中性记号）。 */
function healthText(task: TaskSummary): string {
  const verdict = healthVerdict(task)
  return verdict.kind === 'quiet' ? '—' : verdict.label
}

/** 行上是否需要招人注意：**失败**与**卡住/逾期**都要，但原因不同，所以文案分开。 */
function needsAttention(task: TaskSummary): boolean {
  return task.state === 'failed' || isTaskProblem(task.health)
}

/**
 * 失败 / 异常行的**非文字标记**。
 *
 * 只在状态列上写"失败"不够：一屏十几行，红字小徽章要逐行扫才找得到，而规范要求
 * "不要只靠文字颜色表达状态"（8）。所以行本身给一个记号：
 *
 * - **失败**：整行淡红底（`--status-danger-soft`）+ 左侧 2px 危险色竖条；
 * - **可能卡住 / 长时间未执行**：只给左侧 2px 警示色竖条——它们的健康徽章已经是语义色，
 *   再铺一层底会把徽章糊进底色里（徽章底也是 `--status-*-soft`，同一个值）。
 *
 * 记号落在 `<li>` 上、**不落在 `.panel-row` 上**：`.panel-row:hover` 是半透明的
 * `--bg-hover`，压在 li 的底色上正好，悬停反馈不会因此消失、列也不会因为左边加 2px
 * 描边而错位（用 inset 阴影画竖条，不占盒宽）。
 */
function rowToneClass(task: TaskSummary): string {
  if (task.state === 'failed') {
    return 'bg-[var(--status-danger-soft)] shadow-[inset_2px_0_0_var(--status-danger)]'
  }
  if (isTaskProblem(task.health)) return 'shadow-[inset_2px_0_0_var(--status-warning)]'
  return ''
}

/**
 * 行尾那句提示。失败与卡住是两回事：失败得看原因（详情弹窗里有）；
 * 卡住/逾期得看运维（重启服务、检查 worker）。合成一句"有问题"会让人不知道找谁。
 */
function attentionText(task: TaskSummary): string {
  if (task.state === 'failed') return '查看失败原因'
  if (task.health === 'stalled') return '可能卡住，查看详情'
  if (task.health === 'overdue') return '长时间未执行，查看详情'
  return ''
}

/** 单个任务能否取消：只有还没结束的两种状态可以（与后端口径一致）。 */
function canCancel(task: TaskSummary | null): boolean {
  return task !== null && (task.state === 'pending' || task.state === 'running')
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
