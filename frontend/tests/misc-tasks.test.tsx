/**
 * 任务中心（旧 `views/TasksView.vue`）的用例。
 *
 * 覆盖三件最容易在迁移里丢掉的事：
 * 1. **健康列不是装饰**——失败/卡住要有可读的文字出口，而不是只靠一个颜色；
 * 2. **缓存口径**：轮询失败时列表**保留上一次的数据**（旧 store 的 SWR 语义），
 *    只有错误提示出现；
 * 3. **批量撤下的逐条结果**：部分成功要分开报，并把第一条失败原因带出来。
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/tasks', () => ({
  listTasks: vi.fn(),
  getTaskLoad: vi.fn(),
  cancelTasks: vi.fn(),
}))

vi.mock('@/api/knowledgeBases', () => ({
  listKnowledgeBases: vi.fn(async () => ({
    items: [{ id: 'kb-1', name: '产品手册' }],
  })),
}))

vi.mock('@/api/schedules', () => ({
  listScheduledTasks: vi.fn(),
  createScheduledTask: vi.fn(),
  updateScheduledTask: vi.fn(),
  deleteScheduledTask: vi.fn(),
  runScheduledTaskNow: vi.fn(),
}))

// 「有没有本机后端」那条判据（`api/local.ts`）在本文件里**逐档摆答案**：
// 任务中心的「定时任务」分段只看它（`useLocalBackend`）。探那一趟不出网络——
// 默认替身会把 `/local/status` 记成"漏出替身的请求"，而且那不是这一份要验的东西。
vi.mock('@/api/local', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/local')>()),
  getLocalStatus: vi.fn(),
}))

import {
  listScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
  type ScheduledTask,
} from '@/api/schedules'
import { getLocalStatus, resetLocalBackendForTest, setLocalBackendForTest } from '@/api/local'
import { resetProviderStore, setProviderStatusForTest, type ProviderStatus } from '@/api/provider'
import { cancelTasks, getTaskLoad, listTasks, type SystemLoad, type TaskSummary } from '@/api/tasks'
import { renderMisc } from '@/features/misc/testing/harness'
import { resetToasts } from '@/features/misc/shared/toast'
import { TasksPage, tasksRefetchInterval } from '@/features/misc/tasks/TasksPage'
import { useSessionStore } from '@/lib/session'

/** 提供者握手成功的结论（这一页的「流水线任务」那一半按 `provider.ready` 分流）。 */
const KB_READY: ProviderStatus = { state: 'ready', available: true }

const listTasksMock = vi.mocked(listTasks)
const getTaskLoadMock = vi.mocked(getTaskLoad)
const cancelTasksMock = vi.mocked(cancelTasks)
const listSchedulesMock = vi.mocked(listScheduledTasks)
const runScheduleNowMock = vi.mocked(runScheduledTaskNow)
const updateScheduleMock = vi.mocked(updateScheduledTask)
const localStatusMock = vi.mocked(getLocalStatus)

function schedule(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: 'sch-1',
    name: '每日早报',
    prompt: '把昨天的构建日志汇总成三条结论',
    kind: 'cron',
    cron: '0 9 * * *',
    run_at: null,
    next_run_at: '2026-09-24T01:00:00Z',
    enabled: true,
    kb_ids: [],
    conversation_id: 'conv-1',
    last_run_at: '2026-09-23T01:00:00Z',
    last_status: 'ok',
    last_error: '',
    run_count: 3,
    schedule_text: '每天 09:00',
    created_at: null,
    updated_at: null,
    ...overrides,
  }
}

function task(overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    id: 'task-1',
    kind: 'parse',
    state: 'running',
    document_id: 'doc-1',
    knowledge_base_id: 'kb-1',
    document_name: '手册.pdf',
    attempts: 1,
    max_attempts: 5,
    error: null,
    next_run_at: null,
    lease_expires_at: null,
    created_at: '2026-09-23T10:00:00Z',
    updated_at: '2026-09-23T10:02:00Z',
    health: 'running',
    health_label: '执行中',
    health_detail: '正在解析第 3 页',
    ...overrides,
  }
}

function systemLoad(overrides: Partial<SystemLoad> = {}): SystemLoad {
  return {
    hardware: {
      cpu_percent: 12,
      cpu_count: 8,
      memory_used_bytes: 8 * 1024 ** 3,
      memory_total_bytes: 16 * 1024 ** 3,
      memory_percent: 50,
      process_rss_bytes: 1024 ** 3,
    },
    queue: {
      running: 0,
      pending: 2,
      slots: 1,
      pending_by_kind: {},
      oldest_pending_seconds: null,
      stalled: 0,
      overdue: 0,
    },
    quota: {
      parser_name: 'mineru',
      configured: true,
      pages_used: 0,
      calls: 0,
      daily_quota: 1000,
      remaining: 1000,
      exhausted: false,
    },
    sampled_at: '2026-09-23T10:00:00Z',
    ...overrides,
  }
}

/** 进管理员视角：运行负载面板是管理员专属（端点在成员那里是 403）。 */
function asAdmin() {
  useSessionStore.setState({
    token: 'st',
    currentUser: { id: 'u1', username: 'admin', name: '管理员', role: 'admin', avatar_url: '' },
  })
}

/** 某个环上画了几笔：只有轨道（1）还是有进度弧（2）。 */
function ringStrokes(panel: HTMLElement, name: string): number {
  return within(panel).getByRole('img', { name }).querySelectorAll('circle').length
}

/**
 * 页签当前态的抓手。
 *
 * 上一轮这里钉的是那对**垫 span**（`data-slot="segment-current"` / `segment-label"`）：
 * 当时 `tokens.css` 的元素重置没进 `@layer`，压掉了原语自带的
 * `data-[state=active]:bg-surface` / `px-3` / `font-medium`，只能把底与字手写进 span。
 * 根因修掉之后垫层退回原语，于是这里改钉**原语自己的钩子**：
 *
 * - 两个触发的类名**逐字相同**（差别只在 Radix 给的 `data-state`）——当前态不再靠
 *   "给当前项多加几个类"实现，谁再手写一套就会在这里挂掉；
 * - 白底、字色、框内距都在原语的类里（评审 T1 的病正是这几条被吃掉）。
 *
 * jsdom 不算样式（`vite.config.ts` 里 `test.css: false`），"当前项真的白底"由真浏览器
 * 取证：`.shots/batch2/11-tasks.png`，以及第六批那份探针里逐页签读到的计算样式
 * （当前档 `background-color: rgb(255, 255, 255)` + 墨色 500 字重 + `padding-left: 12px`，
 * 另一档透明 + 次级灰；`.shots/batch6/probe-misc/after-summary.json` 的 `tabs` 一节）。
 * 这里守的是"别再手写一遍"。
 */
function tabShape(trigger: HTMLElement) {
  const cls = trigger.className
  return {
    activeStyles:
      cls.includes('data-[state=active]:bg-surface') &&
      cls.includes('data-[state=active]:text-text-primary'),
    padded: cls.includes('px-3'),
    // 垫层已退回原语：触发按钮里不再有那个绝对定位的 span
    patched: trigger.querySelector('[data-slot="segment-current"]') !== null,
  }
}

/**
 * 这一份用例的网络记账器（断言"那一条没发"时看它）。
 *
 * 用**直接赋值**而不是 `vi.stubGlobal`：`tests/setup.ts` 那层默认替身靠同一种写法，
 * 而 `vi.unstubAllGlobals()` 会把那份文件装上的 jsdom 兜底一起撤掉
 * （`tests/local-backend-gate.test.tsx` 里记着那条教训）。
 */
const fetchedUrls: string[] = []

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  fetchedUrls.length = 0
  globalThis.fetch = (async (url: string) => {
    fetchedUrls.push(String(url))
    throw new TypeError('这一份用例不发真请求')
  }) as unknown as typeof fetch
  useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
  listTasksMock.mockResolvedValue({ items: [task()] })
  getTaskLoadMock.mockRejectedValue(new Error('403'))
  listSchedulesMock.mockResolvedValue({ items: [schedule()], timezone: 'CST UTC+08:00' })
  // 判据的默认档：**有本机后端**（桌面壳那一份）——这一份的大部分用例验的是那一档的形状。
  // 要摆"没有本机后端"的那两条用例自己再摆一次（`setLocalBackendForTest('absent')`）
  localStatusMock.mockRejectedValue(new Error('这一份用例不探本机后端'))
  setLocalBackendForTest('local')
  // 知识库那一条同理：默认摆成**握手成功**（要摆"没接上"的用例自己再摆一次）
  setProviderStatusForTest(KB_READY)
})

afterEach(() => {
  // 模块级单份状态（`api/local.ts` 与 `api/provider.ts` 各有一份）必须由调用方复位，
  // 否则会串到下一条用例
  resetLocalBackendForTest()
  resetProviderStore()
})

describe('任务中心', () => {
  it('列出任务并显示健康判据与尝试次数（失败行给出可读出口）', async () => {
    listTasksMock.mockResolvedValue({
      items: [
        task(),
        task({
          id: 'task-2',
          kind: 'embed',
          state: 'failed',
          document_name: '年报.docx',
          attempts: 3,
          error: 'upstream 502: {"detail":"bad gateway"}',
          health: 'stalled',
          health_label: '可能卡住',
          health_detail: '租约已过期',
        }),
      ],
    })

    renderMisc(<TasksPage />)

    expect(
      await screen.findByRole('button', { name: /查看任务详情：解析 手册.pdf/ }),
    ).toBeInTheDocument()
    // 健康列的文字是后端给的判定结论（前端不再翻译一遍）。
    // 按列表范围查：筛选下拉里也有一个同名的选项，全局查会撞上两处
    expect(within(screen.getByRole('list')).getByText('可能卡住')).toBeInTheDocument()
    // 尝试次数统一成"次数"口径：成功也照实显示 1 / 5，不换成"一次通过"
    expect(screen.getByText('3 / 5')).toBeInTheDocument()
    // 失败行必须给一个**文字**出口（红点在黑白截图里就不见了）
    // 行尾给一句**可操作**的提示：失败与卡住要找的人不同，所以分开说
    expect(screen.getByText('查看失败原因')).toBeInTheDocument()
  })

  it('终态行的「健康」只在有新判断时写词：成功/已取消是不带词的记号，失败仍是「已失败」（评审 T2 / 第三批 A②）', async () => {
    listTasksMock.mockResolvedValue({
      items: [
        task({
          id: 't-ok',
          state: 'succeeded',
          document_name: '成功的.pdf',
          health: 'done',
          health_label: '已完成',
        }),
        task({
          id: 't-bad',
          state: 'failed',
          document_name: '失败的.pdf',
          health: 'done',
          health_label: '已失败',
        }),
        task({
          id: 't-canceled',
          state: 'canceled',
          document_name: '取消的.pdf',
          health: 'done',
          health_label: '已取消',
        }),
      ],
    })

    renderMisc(<TasksPage />)
    await screen.findByText('成功的.pdf')
    // 已取消默认不显示，点开才看得到那一行（失败/取消同属 done 档，要一起核）
    await userEvent.click(screen.getByRole('checkbox', { name: '显示已取消' }))

    // 按列表范围查：筛选下拉的兜底原生 select 里也有同名的选项
    const rows = screen.getByRole('list')
    // 两列说的不是一件事，但**同词的行不能出现两遍**：
    // 已完成 / 已取消 各只留状态列徽章那一个（健康列复述的那份已经收掉）
    expect(within(rows).getAllByText('已完成')).toHaveLength(1)
    expect(within(rows).getAllByText('已取消')).toHaveLength(1)
    // 失败行两列分别是「失败」与「已失败」——这里说的不是同一件事，所以两个字都在
    expect(within(rows).getByText('已失败')).toBeInTheDocument()
    // 不写词的那两格仍有名字：列头是 aria-hidden 的，读屏器只能靠这个标记
    expect(within(rows).getAllByLabelText('健康')).toHaveLength(2)
    // 健康列三格的内容（这一档的弱文字单元格就是 `.m-row-health-done`）：
    // 成功与已取消都退成同一个中性记号，只有失败那格逐字写后端标签
    const healthCells = [...rows.querySelectorAll('.m-row-health-done')].map(
      (cell) => cell.textContent,
    )
    expect(healthCells).toEqual(['—', '已失败', '—'])
  })

  it('在跑的行也不说同一个词：健康列只回答"有没有问题"（第六批）', async () => {
    listTasksMock.mockResolvedValue({
      items: [
        task({
          id: 't-queued',
          state: 'pending',
          document_name: '排队中.pdf',
          health: 'idle',
          health_label: '排队中',
        }),
        task({ id: 't-run', document_name: '执行中.pdf' }),
        task({
          id: 't-stuck',
          document_name: '卡住.pdf',
          health: 'stalled',
          health_label: '可能卡住',
        }),
      ],
    })

    renderMisc(<TasksPage />)
    await screen.findByText('排队中.pdf')
    const rows = screen.getByRole('list')

    /** 逐行读两列的文本——这一页要守的就是"同一行里两列不说同一个词"。 */
    const columns = () =>
      [...rows.querySelectorAll('li.m-list-item')].map((row) => ({
        name: row.querySelector('.m-row-name')?.textContent ?? '',
        status: row.querySelector('.m-col-status')?.textContent ?? '',
        health: row.querySelector('.m-col-health')?.textContent ?? '',
      }))

    expect(columns()).toEqual([
      // 在跑的两档：健康列不再复述状态列那个词，退成中性记号
      { name: '排队中.pdf', status: '排队中', health: '—' },
      { name: '执行中.pdf', status: '执行中', health: '—' },
      // 该出声的仍出声：有问题的档逐字写后端给的判定结论（也正是这一列存在的理由）
      { name: '卡住.pdf', status: '执行中', health: '可能卡住' },
    ])
    // 记号仍有名字：列头是 aria-hidden 的，读屏器只能靠它
    expect(within(rows).getAllByLabelText('健康')).toHaveLength(2)
    expect(within(rows).getAllByText('可能卡住')).toHaveLength(1)
  })

  it('详情弹窗的「健康」与「状态」也不说同一个词（与行内同一处判定，第六批）', async () => {
    listTasksMock.mockResolvedValue({
      items: [
        task({
          id: 't-canceled',
          state: 'canceled',
          document_name: '取消的.pdf',
          health: 'done',
          health_label: '已取消',
        }),
        task({
          id: 't-failed',
          state: 'failed',
          document_name: '失败的.pdf',
          health: 'done',
          health_label: '已失败',
        }),
      ],
    })

    renderMisc(<TasksPage />)
    await screen.findByText('失败的.pdf')
    await userEvent.click(screen.getByRole('checkbox', { name: '显示已取消' }))

    /** 弹窗里 `dt → dd` 的对照表（比按文本查稳：两列本来就可能是同一个词）。 */
    const fields = (dialog: HTMLElement): Record<string, string> =>
      Object.fromEntries(
        [...dialog.querySelectorAll('.m-detail-grid > div')].map((wrap) => [
          wrap.querySelector('dt')?.textContent ?? '',
          wrap.querySelector('dd')?.textContent ?? '',
        ]),
      )

    await userEvent.click(
      await screen.findByRole('button', { name: /查看任务详情：解析 取消的\.pdf/ }),
    )
    const canceled = fields(await screen.findByRole('dialog'))
    expect(canceled['状态']).toBe('已取消')
    // 与行内同一句：已经结束、没有问题，就不复述状态列那个词
    expect(canceled['健康']).toBe('—')
    // 关掉再开另一个（弹窗里有两颗叫「关闭」的按钮，用 Esc 不留歧义）
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await userEvent.click(
      await screen.findByRole('button', { name: /查看任务详情：解析 失败的\.pdf/ }),
    )
    const failed = fields(await screen.findByRole('dialog'))
    // 失败是终态里唯一带来新判断的：状态列说"没成"，这里说"这一轮已经结束"
    expect(failed['状态']).toBe('失败')
    expect(failed['健康']).toBe('已失败')
  })

  it('「按健康筛选」与健康列同一套词：无异常 / 可能卡住 / 长时间未执行（第六批）', async () => {
    listTasksMock.mockResolvedValue({
      items: [
        task({
          id: 't-queued',
          state: 'pending',
          document_name: '排队中.pdf',
          health: 'idle',
          health_label: '排队中',
        }),
        task({ id: 't-run', document_name: '执行中.pdf' }),
        task({
          id: 't-overdue',
          state: 'pending',
          document_name: '逾期.pdf',
          health: 'overdue',
          health_label: '长时间未执行',
        }),
      ],
    })

    renderMisc(<TasksPage />)
    await screen.findByText('排队中.pdf')

    // 选项不再把状态筛选那两个词搬过来（搬过来就又变成"两列同词"）
    await userEvent.click(screen.getByRole('combobox', { name: '按健康筛选' }))
    expect(await screen.findByRole('option', { name: '无异常' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '排队中' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '执行中' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('option', { name: '无异常' }))

    // 「无异常」= 还没结束且没有问题（后端的 running / idle 两档）：逾期那条被筛掉
    await waitFor(() => expect(screen.queryByText('逾期.pdf')).not.toBeInTheDocument())
    expect(screen.getByText('排队中.pdf')).toBeInTheDocument()
    expect(screen.getByText('执行中.pdf')).toBeInTheDocument()
  })

  it('页签就是原语本身：当前态由 @/ui/tabs 自带的类画出来，不再垫 span（评审 T1）', async () => {
    renderMisc(<TasksPage />)
    const pipeline = await screen.findByRole('tab', { name: '流水线任务' })
    const schedules = screen.getByRole('tab', { name: '定时任务' })

    // 语义态仍由 Radix 给（T1 只缺视觉，这一条不许改坏）
    expect(pipeline).toHaveAttribute('aria-selected', 'true')
    expect(schedules).toHaveAttribute('aria-selected', 'false')
    // 两个触发**同一个形状**：类名逐字相同，当前态由 `data-state` + 原语的类决定
    expect(pipeline.className).toBe(schedules.className)
    expect(tabShape(pipeline)).toEqual({ activeStyles: true, padded: true, patched: false })
    expect(tabShape(schedules)).toEqual({ activeStyles: true, padded: true, patched: false })

    // 切过去之后当前态跟着走（同一个钩子，不另写一套）
    await userEvent.click(schedules)
    expect(schedules).toHaveAttribute('aria-selected', 'true')
    expect(pipeline).toHaveAttribute('aria-selected', 'false')
    expect(tabShape(schedules).patched).toBe(false)
  })

  it('点开详情用 pre 显示失败原文（可选中复制，而不是 title 属性）', async () => {
    listTasksMock.mockResolvedValue({
      items: [
        task({
          state: 'failed',
          error: 'upstream 502: {"detail":"bad gateway"}',
          health: 'done',
          health_label: '已结束',
          health_detail: '任务已结束',
        }),
      ],
    })

    renderMisc(<TasksPage />)
    await userEvent.click(await screen.findByRole('button', { name: /查看任务详情/ }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('失败原因')).toBeInTheDocument()
    expect(within(dialog).getByText(/"detail":"bad gateway"/)).toBeInTheDocument()
    expect(within(dialog).getByText('task-1')).toBeInTheDocument()
  })

  it('轮询只在有任务在跑时开着（空闲时不发请求是有意的）', () => {
    expect(tasksRefetchInterval([task({ state: 'running' })])).toBe(2000)
    expect(tasksRefetchInterval([task({ state: 'pending' })])).toBe(2000)
    expect(tasksRefetchInterval([task({ state: 'succeeded' })])).toBe(false)
    expect(tasksRefetchInterval([])).toBe(false)
  })

  it('刷新失败时保留上一次的列表（缓存口径与旧 store 的 SWR 一致）', async () => {
    const { client } = renderMisc(<TasksPage />)
    expect(await screen.findByText('手册.pdf')).toBeInTheDocument()

    listTasksMock.mockRejectedValueOnce(new Error('后端不可达'))
    await client.refetchQueries({ queryKey: ['tasks', 'list'] })

    expect(await screen.findByText('后端不可达')).toBeInTheDocument()
    // 旧数据**不消失**：只有错误提示出现（这是旧 store 刻意保留的口径）
    expect(screen.getByText('手册.pdf')).toBeInTheDocument()
  })

  it('批量撤下排队任务：确认后按成功/失败分别报，并带出第一条失败原因', async () => {
    listTasksMock.mockResolvedValue({
      items: [task({ id: 'p1', state: 'pending', health: 'idle', health_label: '排队中' })],
    })
    cancelTasksMock.mockResolvedValue({
      succeeded: 1,
      failed: 1,
      items: [
        { task_id: 'p1', ok: true, error: null },
        { task_id: 'p2', ok: false, error: '任务已经开始执行' },
      ],
    })

    renderMisc(<TasksPage />)
    await userEvent.click(await screen.findByRole('button', { name: /取消排队中的任务（1）/ }))

    const dialog = await screen.findByRole('alertdialog')
    // @/ui/alert-dialog：确认框不是 dialog 而是 alertdialog（Esc / 点遮罩都不关）
    expect(dialog).toHaveAttribute('data-slot', 'alert-dialog-content')
    await userEvent.click(within(dialog).getByRole('button', { name: '撤下' }))

    await waitFor(() => expect(cancelTasksMock).toHaveBeenCalledWith({}))
    expect(
      await screen.findByText(/撤下：1 个成功、1 个未撤下（任务已经开始执行）/),
    ).toBeInTheDocument()
  })

  it('管理员才看得到运行负载面板（端点在成员那里是 403）', async () => {
    getTaskLoadMock.mockResolvedValue(
      systemLoad({
        hardware: {
          cpu_percent: null,
          cpu_count: 8,
          memory_used_bytes: 1024,
          memory_total_bytes: 2048,
          memory_percent: 50,
          process_rss_bytes: null,
        },
        queue: {
          running: 0,
          pending: 0,
          slots: 1,
          pending_by_kind: {},
          oldest_pending_seconds: null,
          stalled: 0,
          overdue: 0,
        },
        quota: {
          parser_name: 'mineru',
          configured: false,
          pages_used: 0,
          calls: 0,
          daily_quota: 0,
          remaining: 0,
          exhausted: false,
        },
      }),
    )

    const { unmount } = renderMisc(<TasksPage />)
    await screen.findByText('手册.pdf')
    expect(screen.queryByLabelText('运行负载')).not.toBeInTheDocument()
    unmount()

    asAdmin()
    renderMisc(<TasksPage />)

    expect(await screen.findByLabelText('运行负载')).toBeInTheDocument()
    // CPU 的 null 显示"—"而不是 0%（后端首次采样没有差值可算）
    expect(await screen.findByText('8 核 · 采样中')).toBeInTheDocument()
  })

  it('运行负载：五个读数是同一种控件，环里的读数进得了名字（评审 T4）', async () => {
    asAdmin()
    getTaskLoadMock.mockResolvedValue(systemLoad())

    renderMisc(<TasksPage />)
    const panel = await screen.findByLabelText('运行负载')
    // 面板先按骨架画出来，数据是异步到的：等一个"只有拿到数据才会有"的读数
    expect(await within(panel).findByRole('img', { name: 'CPU 使用率 12%' })).toBeInTheDocument()

    // 五个读数一种控件：都是环（此前 3 个环 + 一个空环替身 + 一个纯数字）
    expect(within(panel).getAllByRole('img')).toHaveLength(5)
    // `role="img"` 会把环里的 `<text>` 当装饰，读数必须写进名字里才算数
    expect(within(panel).getByRole('img', { name: '并发槽位占用 0 / 1' })).toBeInTheDocument()
    expect(within(panel).getByRole('img', { name: '云端解析今日页数 0%' })).toBeInTheDocument()
    expect(
      within(panel).getByRole('img', { name: '本进程常驻内存占机器内存 6.3%' }),
    ).toBeInTheDocument()

    // 环里写了「在跑 / 槽位」，下面那行就只剩队列深度：同一个数不写两遍
    const details = [...panel.querySelectorAll('.m-gauge-detail')].map((cell) => cell.textContent)
    expect(details[2]).toBe('排队 2 条')
    // 本进程那格的原始值：分母写在明处，比例才读得出大小
    expect(details[4]).toBe('1.0 GB / 16.0 GB')
  })

  it('0 值的环只留轨道，不画弧（评审 T5：0 / 1000 页 却有一段实心蓝弧）', async () => {
    asAdmin()
    getTaskLoadMock.mockResolvedValue(
      systemLoad({
        hardware: {
          cpu_percent: null,
          cpu_count: 8,
          memory_used_bytes: 8 * 1024 ** 3,
          memory_total_bytes: 16 * 1024 ** 3,
          memory_percent: 50,
          process_rss_bytes: null,
        },
      }),
    )

    renderMisc(<TasksPage />)
    const panel = await screen.findByLabelText('运行负载')
    expect(await within(panel).findByRole('img', { name: '内存使用量 50%' })).toBeInTheDocument()

    // 0 / 1000 页：一段弧都没有（`strokeDasharray="0 C"` 配圆头会画成一个圆点）
    expect(ringStrokes(panel, '云端解析今日页数 0%')).toBe(1)
    // CPU 首次采样没有差值：一样只有轨道，环里写"—"，名字里就不带读数了
    expect(ringStrokes(panel, 'CPU 使用率')).toBe(1)
    // 有进度的仍是"轨道 + 弧"两笔（内存 50%）
    expect(ringStrokes(panel, '内存使用量 50%')).toBe(2)
  })
})

describe('定时任务分段', () => {
  it('显示服务器时区、上一轮结论与下次时间（"下次几点"只能靠它，猜错的代价是它在你睡觉时跑）', async () => {
    renderMisc(<TasksPage />)
    // 分段控件已经是 @/ui/tabs（Radix）：role=tab 由库给，键盘左右键也能切
    const tab = await screen.findByRole('tab', { name: '定时任务' })
    expect(tab).toHaveAttribute('data-slot', 'tabs-trigger')
    await userEvent.click(tab)

    expect(await screen.findByText('每日早报')).toBeInTheDocument()
    expect(screen.getByText(/时间按服务器时区（CST UTC\+08:00）计算/)).toBeInTheDocument()
    expect(screen.getByText('每天 09:00')).toBeInTheDocument()
    expect(screen.getByText(/上次跑成了（/)).toBeInTheDocument()
    expect(screen.getByText(/下次 2026-09-24/)).toBeInTheDocument()
    expect(screen.getByText('跑过 3 次')).toBeInTheDocument()
  })

  it('「立即跑一次」只入队（不动调度字段），「停用」改的是 enabled', async () => {
    runScheduleNowMock.mockResolvedValue({ task_id: 't1', detail: '已入队' })
    listSchedulesMock
      .mockResolvedValueOnce({ items: [schedule()], timezone: 'CST UTC+08:00' })
      .mockResolvedValue({ items: [schedule({ enabled: false })], timezone: 'CST UTC+08:00' })
    updateScheduleMock.mockResolvedValue(schedule({ enabled: false }))

    renderMisc(<TasksPage />)
    await userEvent.click(await screen.findByRole('tab', { name: '定时任务' }))

    await userEvent.click(await screen.findByRole('button', { name: '立即跑一次' }))
    await waitFor(() => expect(runScheduleNowMock).toHaveBeenCalledWith('sch-1'))
    // 只是入队：调度字段一个字都没改
    expect(updateScheduleMock).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: '停用' }))
    await waitFor(() =>
      expect(updateScheduleMock).toHaveBeenCalledWith('sch-1', { enabled: false }),
    )
  })
})

/*
 * 「定时任务」那一章的显隐（2026-10-05，NAS 网页端退役）。
 *
 * 那一族端点（`/scheduled-tasks`）只挂在本机档上，服务器档没有它——所以判据不是
 * "这一页在不在"（任务中心两档都在），而是"这一页**里面**有没有这一段"。
 * 判据与侧栏 / 路由表是同一个（`api/local.ts`），这里按两档各摆一次答案。
 */
describe('定时任务分段的显隐（按"有没有本机后端"）', () => {
  it('没有本机后端：那一段不在，留下的是知识库那一半', async () => {
    setLocalBackendForTest('absent')
    renderMisc(<TasksPage />)

    // 列表照样渲染（这一页在这一档仍然在，只是少了定时任务那一段）
    expect(
      await screen.findByRole('button', { name: /查看任务详情：解析 手册\.pdf/ }),
    ).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '流水线任务' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '定时任务' })).not.toBeInTheDocument()
    // 那一段自己那条读也一次都不发（它只由 SchedulePanel 发，而面板没渲染）
    expect(listSchedulesMock).not.toHaveBeenCalled()
  })

  it('有本机后端：两个分段都在（桌面壳那一档一个字没改）', async () => {
    setLocalBackendForTest('local')
    renderMisc(<TasksPage />)

    expect(await screen.findByRole('tab', { name: '定时任务' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '流水线任务' })).toBeInTheDocument()
  })
})

/*
 * 「流水线任务」那一半的知识库分流（见 `TasksPage.tsx` 的文件头）。
 *
 * `/tasks` 与 `/tasks/load` 数的都是知识库那边的家当（本机档**不挂**它们，
 * `backend/app/api/v1/router.py` 的"明确不挂"那一段），没接上时发过去只会是一行 `Not Found`。
 * 判据与概览页同一套（`kbReady` / `kbPending`，两边各写一处、口径相同）。**「定时任务」
 * 那一段不受它管**：那一族的数据在本机，显隐仍旧只看"有没有本机后端"——第一条用例专门核这一点。
 */
describe('流水线任务：知识库接没接上的分流', () => {
  const KB_UNCONFIGURED: ProviderStatus = {
    state: 'unconfigured',
    available: false,
    reason: '还没配知识库提供者的地址（到「设置 → 知识库连接」里填一下，或问管理员要）',
  }

  it('本机档 + 提供者没接上：两个查询都不发、内容区换成空态，「定时任务」那一段照旧在', async () => {
    // 管理员视角：这样"负载也不发"就只可能是 kbReady 这一条判据挡下来的（而不是非管理员）
    asAdmin()
    setProviderStatusForTest(KB_UNCONFIGURED)
    renderMisc(<TasksPage />)

    expect(await screen.findByText('知识库还没接上')).toBeInTheDocument()
    expect(screen.getByText(KB_UNCONFIGURED.reason as string)).toBeInTheDocument()
    expect(listTasksMock).not.toHaveBeenCalled()
    expect(getTaskLoadMock).not.toHaveBeenCalled()
    expect(fetchedUrls.filter((url) => url.includes('/api/v1/tasks'))).toEqual([])
    // 列表那一块一件都不画（也没有"加载失败"那条错误行）
    expect(screen.queryByText('还没有任务')).toBeNull()
    expect(document.querySelector('.m-error-line')).toBeNull()
    // 「定时任务」那一段的判据**一个字没改**：有本机后端 ⇒ 还在
    expect(screen.getByRole('tab', { name: '定时任务' })).toBeInTheDocument()
  })

  it('本机档 + 提供者 ready：列表照旧（那一条被请求到）', async () => {
    renderMisc(<TasksPage />)

    expect(
      await screen.findByRole('button', { name: /查看任务详情：解析 手册\.pdf/ }),
    ).toBeInTheDocument()
    expect(listTasksMock).toHaveBeenCalledTimes(1)
  })

  it('还没问出提供者结论：只画占位，不画列表、也不提前发请求（不猜）', async () => {
    setProviderStatusForTest(null)
    renderMisc(<TasksPage />)

    expect(screen.getByTestId('tasks-kb-pending')).toBeInTheDocument()
    expect(screen.queryByText('还没有任务')).toBeNull()
    // 还没结论 ≠ 没接上：空态那句不许提前出现
    expect(screen.queryByText('知识库还没接上')).toBeNull()
    expect(listTasksMock).not.toHaveBeenCalled()
  })
})
