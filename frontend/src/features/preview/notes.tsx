/**
 * 预览的两种"说不出口"：还在取、以及取不到 / 画不出来。
 *
 * 三件小事刻意放在一个文件里，因为它们总是一起出现，而且**文案必须一致**：
 * 同一份文件从对话页打开和从知识库打开，看到的失败说明应当一字不差。
 *
 * 三条口径：
 *
 * 1. **失败要说原因，不吞异常**。能拿到后端给的原因（签名链接 403 / 404、
 *    服务端返回的说明）就显示它；只有"确实没有原因"时才退到
 *    `CANNOT_PREVIEW`（"这类文件无法预览"）——一句"加载失败"会把
 *    "网络断了"和"这个格式本来就不支持"混成同一件事。
 * 2. **失败不是白屏**：说清"为什么不能"与"该怎么办"（用下载按钮拿本机程序开），
 *    比给一个空框诚实（旧 `FilePreview.vue` 同一条）。
 * 3. **失败要有出路**：这一次没取到（网络抖、服务端 500）的重试按钮由调用方点名要
 *    （`onRetry`）；下载不在这里放——抽屉工具条上本来就有「下载原文」
 *    （规范：主操作不重复在说明里再放一次）。
 */
import { FileText, RefreshCw } from 'lucide-react'
import type { ReactNode } from 'react'

import { Button } from '@/ui/button'

/** 没有更具体的原因时用的那句话。 */
export const CANNOT_PREVIEW = '这类文件无法预览'

/** 拿不到内容（链接没给 / fetch 失败 / 解析抛错）时的统一开头。 */
export function loadFailure(reason: string): string {
  return `预览失败（${reason}）`
}

/** 把任意抛出物变成一句能显示的话。 */
export function reasonOf(cause: unknown, fallback = CANNOT_PREVIEW): string {
  if (cause instanceof Error && cause.message) return cause.message
  if (typeof cause === 'string' && cause.trim()) return cause
  return fallback
}

/**
 * 浏览器自己抛的那几个英文串 → 一句人话。
 *
 * `fetch` 连不上时抛的是 `Failed to fetch` / `Load failed` 这类**写给调用方看的**
 * 英文，直接端给用户就是"检索失败：Failed to fetch"。只翻这一类：其余原因原样显示
 * ——后端写给用户的中文（"服务内部错误"）比任何二次改写都准，原因只有一处真相。
 *
 * 词表与 `features/chat/model/turns.ts` 的 `failureText`（第四批评审 B①）**同一份**：
 * 那个函数住在对话域里，知识库这边跨域 import 会把整套对话模型拖进这一页，
 * 所以两处各留一份，用词统一（"网络没连上"是那句的前半句，这里不重复它后半句的
 * 括注——外面的句子已经说清是哪一步失败了）。改词表要两边一起改。
 */
const NETWORK_FAILURES = [
  'failed to fetch',
  'load failed',
  'networkerror',
  'network request failed',
  'fetch failed',
  'network error',
  'err_network',
  'err_internet_disconnected',
]

/** 网络层失败的那句话（请求压根没出去，重试多半就好——与服务端出错不是一回事）。 */
export const NETWORK_UNREACHABLE = '网络没连上'

export function failureText(cause: unknown, fallback: string): string {
  const raw = reasonOf(cause, fallback).trim() || fallback
  const lower = raw.toLowerCase()
  return NETWORK_FAILURES.some((mark) => lower.includes(mark)) ? NETWORK_UNREACHABLE : raw
}

/** 一行说明（加载中 / 失败）。 */
export function PreviewNote({
  children,
  tone = 'muted',
}: {
  children: ReactNode
  tone?: 'muted' | 'bad'
}) {
  return (
    <p className="kylab-note" data-tone={tone}>
      {children}
    </p>
  )
}

/** 「正在取原文」——三个 Office 预览与文本分支共用同一句话。 */
export function PreviewLoading({ label = '正在加载原文' }: { label?: string }) {
  return <PreviewNote>{label}…</PreviewNote>
}

/**
 * 「这份看不了」。`reason` 是**完整的一句话**（`loadFailure(...)` 或 `CANNOT_PREVIEW`），
 * 文件名与"该怎么办"在这里补上，调用方不必各写一遍。
 *
 * `onRetry` **给了才画重试按钮**：失败是"这一次没取到"（网络抖动、服务端 500）时重试有意义，
 * "后端压根没给链接"时重试只是让人白点一次——所以由调用方点名要。
 * 下载不在这个组件里：抽屉的工具条上本来就有「下载原文」（规范 §"空态不重复放主操作按钮"）。
 */
export function PreviewUnavailable({
  name,
  reason,
  onRetry,
  retrying = false,
}: {
  name?: string
  reason: string
  onRetry?: () => void
  retrying?: boolean
}) {
  const prefix = name ? `「${name}」` : ''
  const advice =
    reason === CANNOT_PREVIEW ? '下载它，用本机的程序打开。' : '可以用下载按钮，用本机程序打开它。'
  return (
    <div className="kylab-failure">
      <PreviewNote tone="bad">
        {prefix}
        {reason}。{advice}
      </PreviewNote>
      {onRetry ? (
        <Button variant="outline" size="sm" disabled={retrying} onClick={onRetry}>
          <RefreshCw aria-hidden="true" />
          {retrying ? '重试中…' : '重试'}
        </Button>
      ) : null}
    </div>
  )
}

/**
 * 「这个格式不能在这里预览」（不是失败，是**本来就不打算渲染**）。
 *
 * 与 `PreviewUnavailable` 分开：那个说的是"试过但没成"，这个说的是"没打算试"。
 * 用户看到的东西一样（一句说明 + 下载建议），但代码上分得清，
 * 将来加"为什么不能"的解释时不用去猜是哪一种。
 */
export function PreviewNotSupported({ name }: { name?: string }) {
  return (
    <div className="kylab-none">
      <FileText aria-hidden className="kylab-none-icon" size={28} />
      <p className="kylab-none-title">
        {name ? `「${name}」这个格式不能在这里预览` : CANNOT_PREVIEW}
      </p>
      <p className="kylab-none-note">下载它，用本机的程序打开。</p>
    </div>
  )
}
