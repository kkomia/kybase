/**
 * 通知（**收敛到 sonner**：Misc 域原先自带的那个模块级通知栈已删除）。
 *
 * 为什么改：迁移计划 §2 的总要求是"能复用就复用、不要重复造轮子"。Misc 域当初
 * 自己写了一个（理由是"应用壳还没挂 Toast 容器，本域要能独立跑"），现在壳已经挂上
 * `@/ui/sonner` 的 `<Toaster/>`，两套通知会在同一个产品里出现两种长得不一样的提示——
 * 那比"少一个轮子"更贵。所以这里只留一层**薄封装**：函数名与旧实现逐个对应
 * （`notifyInfo` / `notifySuccess` / `notifyWarning` / `notifyError` / `toast.*`），
 * 页面一行都不用改。
 *
 * `ToastStack` 保留成 `<Toaster/>` 的别名：它原先是"页面自己挂的容器"，
 * 现在挂的就是全局那一个——测试夹具（`testing/harness.tsx`）继续渲染它即可，
 * 与真实应用的行为一致（壳里也是挂这个）。
 */
import { toast as sonnerToast } from 'sonner'

import { Toaster } from '@/ui/sonner'

export type ToastTone = 'info' | 'success' | 'warning' | 'danger'

export interface ToastItem {
  id: number
  tone: ToastTone
  message: string
}

/** 最近一次通知的自增编号（sonner 的 toast id 是字符串，这里给个数字口径的壳）。 */
let nextId = 1

export function notifyInfo(message: string): number {
  sonnerToast.info(message)
  return nextId++
}

export function notifySuccess(message: string): number {
  sonnerToast.success(message)
  return nextId++
}

export function notifyWarning(message: string): number {
  sonnerToast.warning(message)
  return nextId++
}

export function notifyError(message: string): number {
  sonnerToast.error(message)
  return nextId++
}

/** 旧版 `toast` 对象的形状（调用点写法不用变）。 */
export const toast = {
  notify: notifyInfo,
  notifySuccess,
  notifyWarning,
  notifyError,
}

/** 供测试清空（sonner 自己有 dismiss 全部的口径）。 */
export function resetToasts(): void {
  sonnerToast.dismiss()
}

export function dismissToast(): void {
  sonnerToast.dismiss()
}

/** 全局通知容器的别名（见文件头）。 */
export const ToastStack = Toaster
