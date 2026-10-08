/**
 * 本域对 `@/ui/*` 原语的**组合件**（composites）——**这不是第二套原语**。
 *
 * 按钮 / 纯图标按钮 / 输入框 / 文本框 / 下拉 / 弹窗 / 确认弹窗 / 行菜单**都不在这里**：
 * 它们已经逐处换成 `@/ui/button`、`@/ui/input`、`@/ui/textarea`、`@/ui/select`、
 * `@/ui/dialog`、`@/ui/alert-dialog`、`@/ui/dropdown-menu`（见各页面的 import）。
 * 这里只留五件 `@/ui/*` **没有一一对应物**的组合，每件都写清"为什么是拼出来而不是换掉"：
 *
 * | 导出 | 由什么拼出 | 为什么不直接用 `@/ui` |
 * |------|-----------|----------------------|
 * | `StatusTag` | `@/ui/badge` + 圆点 | Badge 没有"圆点 + 进行中呼吸"这一档，而"状态 = 文字 + 色块 + 圆点"是本域的既定形态 |
 * | `SkeletonRows` | `@/ui/skeleton` × N | Skeleton 只给**一块**；"列表 / 卡片 / 正文"三种预设是页面排版（`src/ui/README.md` §5 明说预设不进原语） |
 * | `EmptyState` | 纯排版（`kb-empty-*`） | 上游 `empty` 没有 vendor（`src/ui/README.md` §6），它也不是控件 |
 * | `MeterBar` | 纯排版（`kb-meter-*`） | **分段**进度条：`@/ui/progress` 是单值条，表达不了"共 6 段、第 3 段是红的 / 在呼吸" |
 * | `InfoTip` | `@/ui/tooltip` 三件套 | 要自带的 `TooltipProvider`（应用壳里没有，而本域不改壳）；气泡取值见 `@/ui/tooltip` |
 *
 * 这里**没有**自己的颜色、圆角、字号：色与形全部来自 `@/ui/*` 或 `kb-*` 里那些
 * 已经引用 tokens.css 变量的排版类。
 *
 * 本文件同时是本域样式（`knowledge.css`）的入口——每个页面都从这里取至少一件东西
 * （状态标签 / 骨架 / 空态 / 说明气泡），所以 CSS 只在这一处 import 就够了。
 */
import type { ReactNode } from 'react'
import { CircleHelp } from 'lucide-react'

import { Badge } from '@/ui/badge'
import { Skeleton } from '@/ui/skeleton'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/ui/tooltip'

import './knowledge.css'

/* ---------------------------------------------------------------- 状态标签 */

export type StatusTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info'

/**
 * 语义色 → Badge 变体。逐条与旧 `.kb-tag-*` 的颜色对上（都取同一批 `--status-*` 令牌）：
 * neutral = `bg-subtle` + 三级灰、info = 蓝（`--badge-bg` / `--badge-text`）、
 * success / warning / danger = 各自的 `--status-*-soft` 底 + 达标字色。
 *
 * **形状取胶囊**（`shape="pill"`）：旧 `StatusTag.vue` 的 `.status` 明确写着
 * `height: 22px; padding: 0 var(--space-2); border-radius: var(--radius-pill)`——
 * 状态标签本来就不是"徽章"（`@/ui/badge` 的默认形状 4px 方角留给卡片里的 `chip`）。
 * 这里换的是**形状**，颜色仍走 Badge 的变体（同一个 `@/ui/badge`，一处取值）。
 */
const TONE_VARIANT: Record<
  StatusTone,
  'default' | 'secondary' | 'success' | 'warning' | 'destructive'
> = {
  neutral: 'secondary',
  info: 'default',
  success: 'success',
  warning: 'warning',
  danger: 'destructive',
}

interface StatusTagProps {
  label: string
  tone?: StatusTone
  /** 还在动：不给底色，改用呼吸的圆点（与旧 `.kb-tag-running` 同一形态）。 */
  running?: boolean
  title?: string
}

export function StatusTag({ label, tone = 'neutral', running = false, title }: StatusTagProps) {
  return (
    <Badge shape="pill" variant={running ? 'ghost' : TONE_VARIANT[tone]} title={title}>
      <span
        className={['size-1.5 shrink-0 rounded-pill bg-current', running && 'kb-dot-running']
          .filter(Boolean)
          .join(' ')}
        aria-hidden="true"
      />
      {label}
    </Badge>
  )
}

/* ---------------------------------------------------------------- 骨架屏 */

/** 骨架屏的三种预设（旧 `Skeleton`）：高度不同，底色与呼吸交给 `@/ui/skeleton`。 */
export function SkeletonRows({
  variant,
  rows,
}: {
  variant: 'card' | 'list' | 'text'
  rows: number
}) {
  const itemClass =
    variant === 'card' ? 'kb-skeleton-card' : variant === 'text' ? 'kb-skeleton-text' : ''
  return (
    <div className="kb-skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton
          key={index}
          className={['kb-skeleton-row', itemClass].filter(Boolean).join(' ')}
        />
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- 空态 */

export function EmptyState({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children?: ReactNode
}) {
  return (
    <div className="kb-empty">
      <p className="kb-empty-title">{title}</p>
      {hint ? <p className="kb-empty-hint">{hint}</p> : null}
      {children}
    </div>
  )
}

/* ---------------------------------------------------------------- 分段进度条 */

export type MeterTone = 'accent' | 'info' | 'success' | 'warning' | 'danger' | 'neutral'

export interface MeterSegment {
  fill: number
  tone?: MeterTone
  pulsing?: boolean
}

interface MeterBarProps {
  segments: MeterSegment[]
  tone?: MeterTone
  size?: 'sm' | 'md'
  valueLabel?: string
  ariaLabel?: string
}

/**
 * 分段进度条：每段一种色，空槽也画出来（"共 6 段、走到第 3 段"本身就是信息）。
 *
 * 保留本域实现而不是 `@/ui/progress`：后者是一条单值条，"哪一段停住了、那一段是什么状态"
 * 表达不出来；而"停在解析那一步且是红的"正是这份进度条要回答的问题。
 * 取值仍是同一批令牌（空槽 `--meter-track`、填充 `--status-*` / `--accent`）。
 */
export function MeterBar({
  segments,
  tone = 'accent',
  size = 'md',
  valueLabel,
  ariaLabel,
}: MeterBarProps) {
  const ratio = segments.length
    ? segments.reduce((sum, segment) => sum + Math.max(0, Math.min(segment.fill, 1)), 0) /
      segments.length
    : 0
  return (
    <div
      className={['kb-meter', size === 'sm' ? 'kb-meter-sm' : 'kb-meter-md'].join(' ')}
      role="progressbar"
      aria-label={ariaLabel}
      aria-valuenow={Math.round(ratio * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuetext={valueLabel}
    >
      {segments.map((segment, index) => (
        <span
          key={index}
          className={[
            'kb-segment',
            `kb-segment-${segment.tone ?? tone}`,
            segment.pulsing ? 'kb-segment-pulsing' : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          <span
            className="kb-segment-fill"
            style={{ width: `${Math.min(Math.max(segment.fill, 0), 1) * 100}%` }}
          />
        </span>
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- 说明气泡 */

/**
 * 标题旁的「?」：悬停/聚焦才显示的解释性文字（《前端设计规范》§5.1 的保留项）。
 *
 * 从原生 `title`（旧实现，靠浏览器气泡）换成 `@/ui/tooltip`：**气泡现在是 DOM**，
 * 读屏与测试都拿得到（原生 `title` 只对鼠标有效）。触发器仍是那个带 `aria-label`
 * 的小按钮，`kb-infotip` 只管它的形状。
 *
 * 每个 InfoTip 自带一个 `TooltipProvider`：`@/ui/tooltip` 的 Provider 是**必需**的
 * （Radix 在没有 Provider 时会抛错），而应用壳（`src/app/**`）不归本域改。
 */
export function InfoTip({ text }: { text: string }) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" className="kb-infotip" aria-label={text}>
            <CircleHelp className="size-3.5" aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent>{text}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
