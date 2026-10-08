/**
 * 定时任务接口（对应后端 `/api/v1/scheduled-tasks`，v0.33）。
 *
 * 定时任务是**"到点替我做事"**：每天早晨把昨天的日志汇总、每周一把周报底稿准备好。
 * 每次运行的结果落进一条跟它同名的会话里——所以"上周它都跑了什么"就是翻会话记录，
 * 这里只回调度本身的状态（下次什么时候跑、上次跑成没跑成）。
 *
 * 时间口径：cron 按**服务器时区**解释（后端把当前时区放在 `timezone` 里回给界面），
 * 而 `next_run_at` 是带时区的时刻——前端直接 `new Date(...)` 显示即可。
 */

// 定时任务的产物是**会话**（会话在本机，M2 §4.2）→ 全部走 `requestLocal`。
import { requestLocal } from './client'

export interface ScheduledTask {
  id: string
  name: string
  /** 到点要问它的那句话（每次运行的用户消息）。 */
  prompt: string
  kind: 'cron' | 'once'
  /** 5 字段表达式（分 时 日 月 周），`kind: 'cron'` 时有效。 */
  cron: string
  run_at: string | null
  next_run_at: string | null
  enabled: boolean
  kb_ids: string[]
  conversation_id: string | null
  /** 结果落在哪条会话里（没跑过时是 null）。 */
  last_run_at: string | null
  /** `ok` / `degraded`（跑完了但没跑完）/ `failed` / 空串（还没跑过）。 */
  last_status: string
  last_error: string
  run_count: number
  /** 服务端生成的人话（"每天 09:00"）——界面不自己解析 cron。 */
  schedule_text: string
  created_at: string | null
  updated_at: string | null
}

export interface ScheduledTaskList {
  items: ScheduledTask[]
  /** 服务器时区（如 `CST UTC+08:00`）：cron 按它解释，界面必须显示出来。 */
  timezone: string
}

export interface ScheduledTaskPayload {
  name: string
  prompt: string
  kind?: 'cron' | 'once'
  cron?: string
  /** ISO 8601；不带时区的按服务器本地时间解释。 */
  run_at?: string | null
  kb_ids?: string[]
}

export function listScheduledTasks(): Promise<ScheduledTaskList> {
  return requestLocal<ScheduledTaskList>('/scheduled-tasks')
}

export function createScheduledTask(payload: ScheduledTaskPayload): Promise<ScheduledTask> {
  return requestLocal<ScheduledTask>('/scheduled-tasks', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateScheduledTask(
  id: string,
  payload: Partial<ScheduledTaskPayload> & { enabled?: boolean },
): Promise<ScheduledTask> {
  return requestLocal<ScheduledTask>(`/scheduled-tasks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

/** 删一条。**已经跑出来的会话不删**（那是用户问过的内容）。 */
export function deleteScheduledTask(id: string): Promise<void> {
  return requestLocal<void>(`/scheduled-tasks/${id}`, { method: 'DELETE' })
}

/** 立即跑一次：入队一次运行，**不动下次时间**。 */
export function runScheduledTaskNow(id: string): Promise<{ task_id: string; detail: string }> {
  return requestLocal<{ task_id: string; detail: string }>(`/scheduled-tasks/${id}/run`, {
    method: 'POST',
  })
}
