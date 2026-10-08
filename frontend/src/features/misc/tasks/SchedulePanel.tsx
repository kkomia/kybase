/**
 * 定时任务这一块（v0.33）——与旧前端 `components/tasks/SchedulePanel.vue` 对应。
 *
 * 三条刻意的设计（与旧版一致）：
 * 1. **时区必须显示**：cron 按服务器时区解释（后端回 `timezone`），不写出来
 *    "每天 9 点"是哪个 9 点就只能靠猜——而猜错的代价是它在你睡觉时跑；
 * 2. **上一轮的结果要能一眼看出**：`ok` / `degraded`（跑完了但没跑完）/ `failed`
 *    三种状态分开显示，第三种带原因；
 * 3. **「立即跑一次」是主要的确认手段**：挂完一条任务，最想确认的是"它会跑成什么样"。
 */
import { SCHEDULES_QUERY_KEY } from '@/features/misc/queryKeys'

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router'

import {
  deleteScheduledTask,
  listScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
  type ScheduledTask,
} from '@/api/schedules'
import { formatCount, formatDate } from '@/lib/format'

import { notifyError, notifySuccess } from '../shared/toast'
import { Button } from '@/ui/button'
import {
  ConfirmDialog,
  EmptyState,
  ErrorLine,
  SkeletonBlock,
  StatusTag,
  type TagTone,
} from '../shared/composites'
import { ScheduleDialog } from './ScheduleDialog'

/** 上一次运行的结论：三种状态分开说（"跑完了但没跑完"既不是成功也不是失败）。 */
function lastRunTone(status: string): TagTone {
  if (status === 'ok') return 'success'
  if (status === 'degraded') return 'warning'
  if (status === 'failed') return 'danger'
  return 'neutral'
}

function lastRunText(item: ScheduledTask): string {
  if (!item.last_run_at) return '还没跑过'
  const when = formatDate(item.last_run_at)
  if (item.last_status === 'ok') return `上次跑成了（${when}）`
  if (item.last_status === 'degraded') return `上次没跑完（${when}）`
  if (item.last_status === 'failed') return `上次失败（${when}）`
  return `上次运行：${when}`
}

export function nextRunText(item: ScheduledTask): string {
  if (!item.enabled) return '已停用'
  if (!item.next_run_at) return '不会再跑'
  return `下次 ${formatDate(item.next_run_at)}`
}

export function SchedulePanel() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<ScheduledTask | null>(null)
  const [removing, setRemoving] = useState<ScheduledTask | null>(null)
  const [busyId, setBusyId] = useState('')

  const list = useQuery({ queryKey: SCHEDULES_QUERY_KEY, queryFn: listScheduledTasks })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: SCHEDULES_QUERY_KEY })

  /**
   * 跑一次只是入队：立刻返回，真正的问答在 worker 里跑（结果落进那条会话）。
   *
   * 刻意**不失效列表**：这一次点击不改变任何调度字段（`next_run_at` 不动、
   * `run_count` 要等 worker 真的跑完才涨），刷新一次只会让按钮闪一下。
   */
  const runNow = useMutation({
    mutationFn: (item: ScheduledTask) => runScheduledTaskNow(item.id),
    onMutate: (item) => setBusyId(item.id),
    onSuccess: () => notifySuccess('已经排上队，跑完的结果会落在这条任务的会话里'),
    onError: (error: unknown) => notifyError(messageOf(error, '没能排上队')),
    onSettled: () => setBusyId(''),
  })

  const toggle = useMutation({
    mutationFn: (item: ScheduledTask) => updateScheduledTask(item.id, { enabled: !item.enabled }),
    onMutate: (item) => setBusyId(item.id),
    onError: (error: unknown) => notifyError(messageOf(error, '没能改')),
    onSettled: async () => {
      setBusyId('')
      await invalidate()
    },
  })

  const remove = useMutation({
    mutationFn: (item: ScheduledTask) => deleteScheduledTask(item.id),
    onSuccess: () => {
      setRemoving(null)
      notifySuccess('已删除（它跑出来的会话还在）')
    },
    onError: (error: unknown) => notifyError(messageOf(error, '删不掉')),
    onSettled: invalidate,
  })

  const items = list.data?.items ?? []
  const timezone = list.data?.timezone ?? ''
  const loading = list.isLoading
  const empty = !loading && items.length === 0

  return (
    <div className="m-block">
      <div className="m-block-head">
        {/* 只说时区口径（cron 语义要对得上），不说"到点会自己跑"这种机制 */}
        <p className="m-muted">时间按服务器时区（{timezone || '未知'}）计算</p>
        <div className="m-page-actions">
          <Button size="sm" onClick={() => void list.refetch()}>
            <RefreshCw size={14} />
            刷新
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setEditing(null)
              setDialogOpen(true)
            }}
          >
            <Plus size={14} />
            新建
          </Button>
        </div>
      </div>

      {list.isError && <ErrorLine>{messageOf(list.error, '读不到定时任务')}</ErrorLine>}
      {loading && <SkeletonBlock variant="list" rows={3} />}

      {empty && (
        /* 空态给一条能照抄的例子（这一格该填什么），不解释挂上之后怎么跑 */
        <EmptyState
          title="还没有定时任务"
          hint="比如「每天 9 点把昨天的构建日志汇总成三条结论」。"
        />
      )}

      {!loading && items.length > 0 && (
        <ul className="m-cards">
          {items.map((item) => (
            <li
              key={item.id}
              className={item.enabled ? 'm-schedule-row' : 'm-schedule-row m-schedule-row-off'}
            >
              <span className="m-schedule-icon">
                <Clock size={15} />
              </span>
              <div className="m-schedule-body">
                <div className="m-schedule-line">
                  <span className="m-schedule-name">{item.name}</span>
                  <StatusTag tone={lastRunTone(item.last_status)} label={lastRunText(item)} />
                  {item.run_count > 0 && (
                    <span className="tabular text-micro">
                      跑过 {formatCount(item.run_count)} 次
                    </span>
                  )}
                </div>
                <p className="m-schedule-prompt">{item.prompt}</p>
                <div className="m-schedule-meta">
                  <span>{item.schedule_text}</span>
                  <span className="sep">·</span>
                  <span>{nextRunText(item)}</span>
                  {item.last_status === 'failed' && item.last_error && (
                    <>
                      <span className="sep">·</span>
                      <span className="m-schedule-err">{item.last_error}</span>
                    </>
                  )}
                </div>
              </div>
              <div className="m-schedule-actions">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busyId === item.id || runNow.isPending}
                  onClick={() => runNow.mutate(item)}
                >
                  立即跑一次
                </Button>
                {item.conversation_id && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void navigate(`/chat/${item.conversation_id}`)}
                  >
                    看结果
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busyId === item.id}
                  onClick={() => toggle.mutate(item)}
                >
                  {item.enabled ? '停用' : '启用'}
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`改动「${item.name}」`}
                  title={`改动「${item.name}」`}
                  onClick={() => {
                    setEditing(item)
                    setDialogOpen(true)
                  }}
                >
                  <Pencil size={14} />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`删除「${item.name}」`}
                  title={`删除「${item.name}」`}
                  onClick={() => setRemoving(item)}
                >
                  <Trash2 size={14} />
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <ScheduleDialog
        open={dialogOpen}
        target={editing}
        timezone={timezone}
        onClose={() => setDialogOpen(false)}
        onSaved={invalidate}
      />

      <ConfirmDialog
        open={removing !== null}
        title="删除这条定时任务？"
        lead={`「${removing?.name ?? ''}」不会再跑了。`}
        note="它已经跑出来的会话不会被删。"
        confirmLabel="删除"
        busy={remove.isPending}
        busyLabel="删除中…"
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          if (removing) remove.mutate(removing)
        }}
      />
    </div>
  )
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
