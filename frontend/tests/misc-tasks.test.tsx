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
import { beforeEach, describe, expect, it, vi } from 'vitest'

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

import { cancelTasks, getTaskLoad, listTasks, type SystemLoad, type TaskSummary } from '@/api/tasks'
import { renderMisc } from '@/features/misc/testing/harness'
import { resetToasts } from '@/features/misc/shared/toast'
import { TasksPage, tasksRefetchInterval } from '@/features/misc/tasks/TasksPage'
import { useSessionStore } from '@/lib/session'

const listTasksMock = vi.mocked(listTasks)
const getTaskLoadMock = vi.mocked(getTaskLoad)
const cancelTasksMock = vi.mocked(cancelTasks)

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

/** 某个带名字的图形里的元素（条 / 格子都靠它取）。 */
function glyphOf(panel: HTMLElement, name: string): HTMLElement {
  return within(panel).getByRole('img', { name })
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  // 真网络一律挡住：漏出去的请求会被记成一条带 URL 的失败
  globalThis.fetch = (async (url: string) => {
    throw new TypeError(`这一份用例不发真请求：${String(url)}`)
  }) as unknown as typeof fetch
  useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
  listTasksMock.mockResolvedValue({ items: [task()] })
  getTaskLoadMock.mockRejectedValue(new Error('403'))
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

  it('运行负载：五格各有各的形，每格的读数都进得了名字（评审 T4 的下一版）', async () => {
    asAdmin()
    getTaskLoadMock.mockResolvedValue(systemLoad())

    renderMisc(<TasksPage />)
    const panel = await screen.findByLabelText('运行负载')
    // 面板先按骨架画出来，数据是异步到的：等一个"只有拿到数据才会有"的读数
    expect(await within(panel).findByRole('img', { name: 'CPU 使用率 12%' })).toBeInTheDocument()

    // 五格一个带名字的图形：`role="img"` 会把图形里的数字当装饰，读数必须写进名字里
    expect(within(panel).getAllByRole('img')).toHaveLength(5)

    // 1/5 CPU：唯一一个环——"满没满"最省地方的形
    expect(panel.querySelectorAll('svg')).toHaveLength(1)
    expect(ringStrokes(panel, 'CPU 使用率 12%')).toBe(2)

    // 2/5 内存：水平条 + 右端读数（不是环，也没有第二根弧）
    const memory = glyphOf(panel, '内存使用量 50%')
    expect(memory.querySelector('.m-bar-fill')).toHaveStyle({ width: '50%' })
    expect(memory.querySelector('.m-bar-reading')?.textContent).toBe('50%')

    // 3/5 并发槽位：大号数字 + 一格一个槽位（默认档 slots=1、running=0）
    const slots = glyphOf(panel, '并发槽位占用 0 / 1')
    expect(slots.querySelector('.m-slots-figure')?.textContent).toBe('0 / 1')
    expect(slots.querySelectorAll('.m-slot')).toHaveLength(1)
    expect(slots.querySelectorAll('.m-slot-on')).toHaveLength(0)

    // 4/5 云端解析额度：12 格的分格进度条（0 页 → 一格都不亮）
    const quota = glyphOf(panel, '云端解析今日页数 0%')
    expect(quota.querySelectorAll('.m-quota-cell')).toHaveLength(12)
    expect(quota.querySelectorAll('.m-quota-cell-on')).toHaveLength(0)

    // 5/5 本进程常驻内存：纯数字卡，一格刻度都不画
    const rss = glyphOf(panel, '本进程常驻内存占机器内存 6.3%')
    expect(rss).toHaveTextContent('6.3%')
    expect(rss.querySelectorAll('.m-bar, .m-slot, .m-quota-cell, circle')).toHaveLength(0)

    // 上面写了「在跑 / 槽位」，下面那行就只剩队列深度：同一个数不写两遍
    const details = [...panel.querySelectorAll('.m-gauge-detail')].map((cell) => cell.textContent)
    expect(details[2]).toBe('排队 2 条')
    // 本进程那格的原始值：分母写在明处，比例才读得出大小
    expect(details[4]).toBe('1.0 GB / 16.0 GB')
  })

  it('0 值只留空槽：环不画弧、条不画填充、格子不点亮（评审 T5 的口径覆盖三种形）', async () => {
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

    // CPU 首次采样没有差值：只有轨道，环里写"—"，名字里就不带读数了
    expect(ringStrokes(panel, 'CPU 使用率')).toBe(1)
    // 0 / 1000 页：一格都不点亮（`strokeDasharray="0 C"` 配圆头会画成一个圆点，
    // 0 宽度的填充条同样会留下一小截——两种形都只留空槽）
    const quota = glyphOf(panel, '云端解析今日页数 0%')
    expect(quota.querySelectorAll('.m-quota-cell-on')).toHaveLength(0)
    // 有进度的照旧画出来：内存 50% 的条占一半
    expect(glyphOf(panel, '内存使用量 50%').querySelector('.m-bar-fill')).toHaveStyle({
      width: '50%',
    })
    // 本进程那格拿不到 RSS：一样是"—"，名字里也不带读数
    expect(glyphOf(panel, '本进程常驻内存占机器内存')).toHaveTextContent('—')
  })
})
