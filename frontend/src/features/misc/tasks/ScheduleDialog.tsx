/**
 * 新建 / 改一条定时任务（与旧前端 `components/tasks/ScheduleDialog.vue` 对应）。
 *
 * **时间这块刻意不做 cron 编辑器**：那是一个要自己维护语法高亮与校验的控件，
 * 而真实用法里九成是"每天几点""每周几几点"。所以给**四个常见档 + 自定义**：
 * 前四档由"时间"选择器拼出 cron（拼法只有一份，就是 `composedCron`），
 * 自定义那一档直接把表达式交给后端校验。
 *
 * 「一次性」用 `datetime-local`：它给的字符串**不带时区**，后端按服务器本地时间解释。
 */
import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import {
  createScheduledTask,
  updateScheduledTask,
  type ScheduledTask,
  type ScheduledTaskPayload,
} from '@/api/schedules'
import { useKnowledgeBases } from '@/features/misc/shared/knowledgeBases'

import { MultiSelect } from '../shared/MultiSelect'
import { notifyError, notifySuccess } from '../shared/toast'
import { Button } from '@/ui/button'
import { Input } from '@/ui/input'
import { Textarea } from '@/ui/textarea'
import { Field, Modal, OptionSelect } from '../shared/composites'

const MODES = [
  { value: 'daily', label: '每天' },
  { value: 'weekdays', label: '每个工作日' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'custom', label: '自定义表达式' },
]

const WEEKDAYS = [
  { value: '1', label: '周一' },
  { value: '2', label: '周二' },
  { value: '3', label: '周三' },
  { value: '4', label: '周四' },
  { value: '5', label: '周五' },
  { value: '6', label: '周六' },
  { value: '0', label: '周日' },
]

const KIND_OPTIONS = [
  { value: 'cron', label: '重复' },
  { value: 'once', label: '只跑一次' },
]

interface Draft {
  name: string
  prompt: string
  kind: 'cron' | 'once'
  mode: string
  time: string
  weekday: string
  dayOfMonth: string
  customCron: string
  runAt: string
  kbIds: string[]
}

const EMPTY: Draft = {
  name: '',
  prompt: '',
  kind: 'cron',
  mode: 'daily',
  time: '09:00',
  weekday: '1',
  dayOfMonth: '1',
  customCron: '0 9 * * *',
  runAt: '',
  kbIds: [],
}

/** 把一条既有表达式认回某一档（认不出就是自定义）。 */
export function guessMode(cron: string): string {
  const parts = (cron || '').split(' ')
  if (parts.length !== 5) return 'custom'
  const [, , day, month, week] = parts
  if (day === '*' && month === '*' && week === '1-5') return 'weekdays'
  if (day === '*' && month === '*' && /^\d$/.test(week)) return 'weekly'
  if (/^\d+$/.test(day) && month === '*' && week === '*') return 'monthly'
  if (day === '*' && month === '*' && week === '*') return 'daily'
  return 'custom'
}

/** 四个档拼出来的表达式（**只有这一处拼**，别处不再拼第二份）。 */
export function composeCron(draft: Draft): string {
  if (draft.mode === 'custom') return draft.customCron.trim()
  const [hour, minute] = (draft.time || '09:00').split(':')
  const m = String(Number(minute ?? 0))
  const h = String(Number(hour ?? 9))
  if (draft.mode === 'weekdays') return `${m} ${h} * * 1-5`
  if (draft.mode === 'weekly') return `${m} ${h} * * ${draft.weekday}`
  if (draft.mode === 'monthly') return `${m} ${h} ${Number(draft.dayOfMonth) || 1} * *`
  return `${m} ${h} * * *`
}

/** 带时区的 ISO → `datetime-local` 要的本地钟点串。 */
function toLocalInput(value: string): string {
  const date = new Date(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 打开时按目标装载（新建则清空）。**每一次打开都重置**，免得带上一条的残留。 */
export function draftOf(target: ScheduledTask | null): Draft {
  if (!target) return { ...EMPTY }
  const next: Draft = {
    ...EMPTY,
    name: target.name,
    prompt: target.prompt,
    kind: target.kind,
    customCron: target.cron || EMPTY.customCron,
    mode: target.kind === 'cron' ? guessMode(target.cron) : 'daily',
    runAt: target.run_at ? toLocalInput(target.run_at) : '',
    kbIds: [...target.kb_ids],
  }
  const [minute, hour] = (target.cron || '').split(' ')
  if (/^\d+$/.test(hour ?? '') && /^\d+$/.test(minute ?? '')) {
    next.time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
  }
  const parts = (target.cron || '').split(' ')
  if (parts[4] && /^\d$/.test(parts[4])) next.weekday = parts[4]
  if (parts[2] && /^\d+$/.test(parts[2])) next.dayOfMonth = parts[2]
  return next
}

/** 预览那一行：说清"什么时候跑"（拼法与人话一一对应）。 */
export function previewText(draft: Draft): string {
  if (draft.kind === 'once') {
    return draft.runAt ? `只跑一次：${draft.runAt.replace('T', ' ')}` : '还没选时间'
  }
  const at = draft.time
  if (draft.mode === 'custom') return `按表达式：${composeCron(draft)}`
  if (draft.mode === 'weekdays') return `每个工作日 ${at}`
  if (draft.mode === 'weekly') {
    const label = WEEKDAYS.find((item) => item.value === draft.weekday)?.label ?? '周一'
    return `每${label} ${at}`
  }
  if (draft.mode === 'monthly') return `每月 ${Number(draft.dayOfMonth) || 1} 号 ${at}`
  return `每天 ${at}`
}

export function ScheduleDialog({
  open,
  target,
  timezone,
  onClose,
  onSaved,
}: {
  open: boolean
  target: ScheduledTask | null
  timezone: string
  onClose: () => void
  onSaved: () => void
}) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(target))
  const knowledgeBases = useKnowledgeBases()

  // 打开时重置：每一次打开都从当前目标重新装载，不带上一次的残值
  useEffect(() => {
    if (open) setDraft(draftOf(target))
  }, [open, target])

  const save = useMutation({
    mutationFn: async (payload: ScheduledTaskPayload) =>
      target ? updateScheduledTask(target.id, payload) : createScheduledTask(payload),
    onSuccess: () => {
      notifySuccess(target ? '已保存' : '已挂上定时任务')
      onSaved()
      onClose()
    },
    // 后端那句报错是照着"怎么改对"写的（哪个字段、错在哪），直接显示它
    onError: (error: unknown) => notifyError(messageOf(error, '没保存成功')),
  })

  const patch = (next: Partial<Draft>) => setDraft((current) => ({ ...current, ...next }))
  const ready = draft.name.trim() !== '' && draft.prompt.trim() !== ''

  const submit = () => {
    const payload: ScheduledTaskPayload = {
      name: draft.name.trim(),
      prompt: draft.prompt.trim(),
      kind: draft.kind,
      kb_ids: draft.kbIds,
    }
    if (draft.kind === 'once') payload.run_at = draft.runAt || null
    else payload.cron = composeCron(draft)
    save.mutate(payload)
  }

  const kbOptions = knowledgeBases.items.map((item) => ({ value: item.id, label: item.name }))

  return (
    <Modal
      open={open}
      title={target ? '改定时任务' : '新建定时任务'}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button disabled={!ready || save.isPending} onClick={submit}>
            {save.isPending ? '保存中…' : '保存'}
          </Button>
        </>
      }
    >
      <div className="m-form">
        <Field label="名字" htmlFor="schedule-name">
          <Input
            id="schedule-name"
            value={draft.name}
            onChange={(event) => patch({ name: event.target.value })}
            placeholder="每日早报"
          />
        </Field>

        <Field label="到点要做什么" htmlFor="schedule-prompt" hint="写具体一点，结果更有用。">
          <Textarea
            id="schedule-prompt"
            rows={3}
            value={draft.prompt}
            onChange={(event) => patch({ prompt: event.target.value })}
            placeholder="把昨天的构建日志汇总成三条结论"
          />
        </Field>

        <div className="field">
          <span className="field-label">什么时候跑</span>
          <div className="m-form-actions">
            <div className="m-filter-select">
              <OptionSelect
                value={draft.kind}
                onValueChange={(value) => patch({ kind: value as Draft['kind'] })}
                options={KIND_OPTIONS}
                label="重复还是一次"
              />
            </div>
            {draft.kind === 'cron' ? (
              <>
                <div className="m-filter-select">
                  <OptionSelect
                    value={draft.mode}
                    onValueChange={(mode) => patch({ mode })}
                    options={MODES}
                    label="周期"
                  />
                </div>
                {draft.mode !== 'custom' && (
                  <Input
                    type="time"
                    style={{ width: '120px' }}
                    aria-label="时间"
                    value={draft.time}
                    onChange={(event) => patch({ time: event.target.value })}
                  />
                )}
                {draft.mode === 'weekly' && (
                  <div className="m-filter-select">
                    <OptionSelect
                      value={draft.weekday}
                      onValueChange={(weekday) => patch({ weekday })}
                      options={WEEKDAYS}
                      label="周几"
                    />
                  </div>
                )}
                {draft.mode === 'monthly' && (
                  <Input
                    type="number"
                    min={1}
                    max={31}
                    style={{ width: '72px' }}
                    aria-label="几号"
                    value={draft.dayOfMonth}
                    onChange={(event) => patch({ dayOfMonth: event.target.value })}
                  />
                )}
              </>
            ) : (
              <Input
                type="datetime-local"
                aria-label="时间"
                value={draft.runAt}
                onChange={(event) => patch({ runAt: event.target.value })}
              />
            )}
          </div>
          {draft.kind === 'cron' && draft.mode === 'custom' && (
            <Input
              value={draft.customCron}
              onChange={(event) => patch({ customCron: event.target.value })}
              placeholder="0 9 * * *"
              aria-label="自定义 cron 表达式"
            />
          )}
          <span className="text-hint">
            {previewText(draft)} · 按服务器时区（{timezone || '未知'}）计算
          </span>
        </div>

        <div className="field">
          <span className="field-label">到点查哪些知识库</span>
          <MultiSelect
            options={kbOptions}
            value={draft.kbIds}
            onChange={(kbIds) => patch({ kbIds })}
            emptyText="还没有知识库可查。"
            label="到点查哪些知识库"
          />
          <span className="text-hint">不选就是不查知识库。</span>
        </div>
      </div>
    </Modal>
  )
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
