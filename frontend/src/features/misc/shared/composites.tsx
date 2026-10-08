/**
 * misc 域的**组合件**层（composites）——**这不是第二套原语**。
 *
 * 原 `shared/ui.tsx` 是本域临时自写的一层：按钮的悬停/禁用、输入框的描边、弹窗的
 * Esc 与遮罩关闭、焦点退还、菜单的键盘导航……那些**都是 `src/ui/**` 已经做好的事**，
 * 本域再写一遍只会出现两套观感。现在：
 *
 * 1. 有对应原语的**一律直接用 `@/ui/*`**，调用点不再经过这里：
 *    `Button`→`@/ui/button`、`IconButton`→`Button size="icon-sm"`、
 *    `TextInput`→`Input`、`TextArea`→`Textarea`、`Chip`→`Badge`、
 *    `RowMenu`/`MenuItem`→`@/ui/dropdown-menu`。
 * 2. 只剩本域**特有的组合**留在这个文件里：要么没有对应原语（页面外壳、字段行、空态、
 *    通知条、筛选胶囊、环形仪表），要么是"一个组合"（弹窗的三段式、`options` 数组式的
 *    下拉、带文字的勾选行、带语义色的状态标签、骨架预设、分段控件）。内部**一律由
 *    `@/ui/*` 承担行为**——这里没有一行自写的 Esc/遮罩/焦点/键盘逻辑。
 *
 * 命名口径：与原语同名的都换了更准的名字，免得读代码时以为本域还养着一套原语
 * （下拉叫 `OptionSelect`、勾选行叫 `CheckRow`；`Avatar` 保留名字，但它只是
 * `@/ui/avatar` 的组合）。
 *
 * 样式来自两处，都是令牌：`tokens.css` 的语义类（`.page-shell` / `.field` /
 * `.field-label` / `.text-hint` …）与 `misc.css` 里本域自己的结构类
 * （`.m-page-head` / `.m-empty` / `.m-notice` / `.m-filter` …）。
 */
import type { ReactNode } from 'react'

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/alert-dialog'
import { Avatar as AvatarRoot, AvatarFallback, AvatarImage } from '@/ui/avatar'
import { Badge } from '@/ui/badge'
import { Checkbox as CheckboxPrimitive } from '@/ui/checkbox'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/ui/dialog'
import { Label } from '@/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'
import { Skeleton } from '@/ui/skeleton'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/ui/tooltip'
import { cn } from '@/lib/utils'

import './misc.css'

// ------------------------------------------------------------------ 页面外壳

export function PageShell({
  title,
  actions,
  children,
}: {
  /** 空串 = 不画页标题（能力页刻意不要它，左侧菜单已经写着「能力」）。 */
  title: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="page-shell">
      {(title || actions) && (
        <header className="m-page-head">
          {title ? <h1 className="m-page-title">{title}</h1> : <span />}
          {actions && <div className="m-page-actions">{actions}</div>}
        </header>
      )}
      <div className="page-shell-body">{children}</div>
    </div>
  )
}

// ------------------------------------------------------------------ 表单组合

/**
 * 字段行：标签 + 控件 + 说明。
 *
 * 标签用 `@/ui/label`（它的取值就是 tokens.css 的 `.field-label`），于是"点标签聚焦
 * 控件"由 Radix 给；`hint` / `optional` / `tip` 是本域的排版口径，不是原语的能力。
 */
export function Field({
  label,
  hint,
  optional,
  tip,
  htmlFor,
  children,
}: {
  label: string
  hint?: ReactNode
  optional?: boolean
  tip?: ReactNode
  htmlFor?: string
  children: ReactNode
}) {
  return (
    <div className="field">
      {/* `tip`（InfoTip 是个按钮）**不能**放进 `<label>` 里：label 会把它当成"被标注的
          控件"，读屏与 `getByLabelText` 都会把这个问号算成一次命中。 */}
      <span className="flex items-center gap-1">
        <Label htmlFor={htmlFor}>
          <span>
            {label}
            {optional && <span className="field-optional">（可选）</span>}
          </span>
        </Label>
        {tip}
      </span>
      {children}
      {hint && <span className="text-hint">{hint}</span>}
    </div>
  )
}

export interface SelectOption {
  value: string
  label: string
}

/** Radix 的 `SelectItem` 不接受空串，而这一域用空串表示"不限 / 未指定"。 */
const EMPTY_OPTION = '__misc-empty-option__'

/**
 * 下拉：`@/ui/select` 的组合（浮层、键盘、typeahead 全是 Radix 的）。
 *
 * 只多两件事，都是本域的调用口径：
 * 1. 传 `options` 数组而不是 children —— 本域 27 处都是"一组固定选项里挑一个"；
 * 2. **空串是"不限 / 未指定"**（`{ value: '', label: '全部状态' }`），进出各转一次
 *    哨兵值，对调用方仍然只有空串这一种写法。
 */
export function OptionSelect({
  value,
  onValueChange,
  options,
  label,
  disabled,
  id,
  className,
}: {
  value: string
  onValueChange: (next: string) => void
  options: readonly SelectOption[]
  label?: string
  disabled?: boolean
  id?: string
  className?: string
}) {
  return (
    <Select
      value={value === '' ? EMPTY_OPTION : value}
      onValueChange={(next) => onValueChange(next === EMPTY_OPTION ? '' : next)}
    >
      <SelectTrigger
        id={id}
        aria-label={label}
        disabled={disabled}
        className={cn('w-full', className)}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value === '' ? EMPTY_OPTION : option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * 勾选行：`@/ui/checkbox` 的组合——勾选框本体（品牌蓝的选中态、实色禁用态、勾形图标、
 * 键盘）是原语，这里只管"框 + 文字"这一行的排版与 `label` 关联。
 *
 * 回调收窄成 `boolean`：Radix 给的是 `boolean | 'indeterminate'`，而本域没有三态勾选。
 */
export function CheckRow({
  checked,
  onCheckedChange,
  children,
}: {
  checked: boolean
  onCheckedChange: (next: boolean) => void
  children: ReactNode
}) {
  return (
    <label className="m-check">
      <CheckboxPrimitive
        checked={checked}
        onCheckedChange={(next) => onCheckedChange(next === true)}
      />
      <span>{children}</span>
    </label>
  )
}

// ------------------------------------------------------------------ 筛选

/**
 * 筛选胶囊（带计数）：保留的**样式壳**。
 *
 * 与分段控件的区别是它**不是**"当前视图"而是"一排可横着滚的过滤器"，选中态是
 * `--bg-selected` + 深描边、形状是胶囊——`@/ui/tabs` 与 `@/ui/badge` 都没有这一形态
 * （Badge 是 4px 方角、Tabs 是面板槽），所以只留样式；语义仍是 `role="tablist"`。
 *
 * 分段控件本身**不再在这里包一层**：三个用它的页面（任务 / 记忆 / 能力）直接用
 * `@/ui/tabs`。原来那个 `SegmentedControl` 包装是绕开未分层 CSS 那件事的产物
 * （它垫 `h-9 p-0.5` / `h-8` 去仿旧的 `.m-tabs`，而那个类早就不存在了），
 * 收层之后最后的调用点也退回了原语，包装于是一并删掉——留着只会有两种形状。
 */
export function FilterChips<T extends string>({
  items,
  value,
  onChange,
  ariaLabel,
}: {
  items: readonly { key: T; label: string; count: number }[]
  value: T
  onChange: (next: T) => void
  ariaLabel: string
}) {
  return (
    <div className="m-filters" role="tablist" aria-label={ariaLabel}>
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="tab"
          aria-selected={value === item.key}
          className={value === item.key ? 'm-filter m-filter-on' : 'm-filter'}
          onClick={() => onChange(item.key)}
        >
          {item.label}
          <span className="tabular">{item.count}</span>
        </button>
      ))}
    </div>
  )
}

// ------------------------------------------------------------------ 标签

export type TagTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'

/** 语义色 → Badge 变体（色值只在 Badge 的 cva 里，这里不写一个色值）。 */
const TAG_VARIANTS = {
  neutral: 'secondary',
  info: 'default',
  success: 'success',
  warning: 'warning',
  danger: 'destructive',
} as const

/**
 * 状态标签：`@/ui/badge` 的组合，只负责把**语义色**映射到徽章变体上。
 * `live` 的脉动圆点仍然带着文字（状态要"图标 + 文字"双编码，颜色只是加速识别）。
 *
 * **形状取胶囊**（`shape="pill"`）：旧 `StatusTag.vue` 的 `.status` 是
 * `height: 22px; padding: 0 var(--space-2); border-radius: var(--radius-pill)`。
 * Badge 默认那个 4px 方角是给卡片里的 `chip` 的（旧 `.chip` 也是方角），
 * 两者本来就是两种东西——所以形状做成变体，不动默认值（见对照记录 §3 第 4 条）。
 */
export function StatusTag({
  label,
  tone = 'neutral',
  live,
  title,
}: {
  label: string
  tone?: TagTone
  /** 在跑的状态：加一个脉动圆点（文字仍在）。 */
  live?: boolean
  title?: string
}) {
  return (
    <Badge shape="pill" variant={TAG_VARIANTS[tone]} title={title}>
      {live && <span className="m-dot m-dot-live" aria-hidden="true" />}
      {label}
    </Badge>
  )
}

// ------------------------------------------------------------------ 骨架 / 空态

/**
 * 骨架屏预设：`@/ui/skeleton` 的组合（"一整块灰"是原语，这里只排几行、多高、多宽——
 * 预设属于页面排版，不该塞进原语里）。
 */
export function SkeletonBlock({
  rows = 3,
  variant = 'list',
}: {
  rows?: number
  variant?: 'list' | 'text' | 'card'
}) {
  if (variant === 'card') {
    return <Skeleton className="h-24 rounded-[var(--radius-panel)]" aria-hidden="true" />
  }
  return (
    <div className="flex flex-col gap-2" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton
          key={index}
          className="h-4"
          style={{ width: variant === 'text' ? `${92 - index * 6}%` : '100%' }}
        />
      ))}
    </div>
  )
}

export function EmptyState({
  title,
  hint,
  center,
  children,
}: {
  title: string
  hint?: string
  center?: boolean
  children?: ReactNode
}) {
  return (
    <div className={center ? 'm-empty m-empty-center' : 'm-empty'}>
      <p className="m-empty-title">{title}</p>
      {hint && <p className="m-empty-hint">{hint}</p>}
      {children}
    </div>
  )
}

export function ErrorLine({ children }: { children: ReactNode }) {
  return <p className="m-error-line">{children}</p>
}

export function Notice({
  tone = 'neutral',
  icon,
  children,
}: {
  tone?: 'neutral' | 'error' | 'warn' | 'ok'
  icon?: ReactNode
  children: ReactNode
}) {
  const classes = ['m-notice']
  if (tone !== 'neutral') classes.push(`m-notice-${tone}`)
  return (
    <div className={classes.join(' ')}>
      {icon}
      <span>{children}</span>
    </div>
  )
}

// ------------------------------------------------------------------ 提示（InfoTip）

/**
 * 行内的一个问号提示：`@/ui/tooltip` 的组合。
 *
 * 旧实现用原生 `title`（理由是"读一次就够的说明不值得引浮层"）。既然气泡已经是现成的
 * 原语，就换成它：仍然用 `aria-label` 把整句话交给读屏，视觉上也仍是"移上去就出现"
 * （`delayDuration={0}`，与 `@/ui/tooltip` 的取值口径一致）。`Provider` 就地嵌套一层，
 * 于是这个组合件不依赖应用壳挂没挂全局的 Provider。
 */
export function InfoTip({ text, label = '说明' }: { text: string; label?: string }) {
  return (
    <TooltipProvider delayDuration={0}>
      <Tooltip>
        <TooltipTrigger asChild>
          {/* 无障碍名给**短名**：旧版就是「说明」。把整句长文当按钮名，
              读屏会一口气念完一段——长文属于气泡里的内容（`TooltipContent`）。 */}
          <button type="button" className="m-infotip" aria-label={label}>
            <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true">
              <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
              <path d="M8 4.6v.1" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              <path d="M8 7v4.4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </button>
        </TooltipTrigger>
        <TooltipContent>{text}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

// ------------------------------------------------------------------ 弹窗

export interface ModalProps {
  open: boolean
  title: string
  onClose: () => void
  size?: 'sm' | 'md' | 'wide'
  height?: 'auto' | 'tall' | 'full'
  footer?: ReactNode
  children: ReactNode
}

const MODAL_SIZES = {
  sm: 'sm:max-w-[520px]',
  md: 'sm:max-w-[620px]',
  wide: 'sm:max-w-[960px]',
} as const

/**
 * 弹窗：`@/ui/dialog` 的组合（`src/ui/README.md` §3 的那套三段式）。
 *
 * **行为一件都不在这里**：Esc 关闭、点遮罩关闭、打开时锁滚动、关闭后把焦点还给打开它的
 * 那个元素、`aria-modal` 与标题关联——全部由 Radix 的 Dialog 给（旧实现自己补了其中几件，
 * 还漏了滚动锁）。这里只把标题栏 / 可滚内容区 / 底部按本域口径拼出来，并把 `size` /
 * `height` 两档映射成宽度与最大高度。
 *
 * `min-h-0` 那一条是关键：flex 子项默认 `min-height: auto`，不写它内容会把弹窗撑高、
 * `overflow-y: auto` 永远不生效。
 */
export function Modal({ open, title, onClose, size = 'sm', height, footer, children }: ModalProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent
        className={cn(
          'flex flex-col gap-0 p-0',
          height === 'tall' || height === 'full' ? 'max-h-[92vh]' : 'max-h-[84vh]',
          MODAL_SIZES[size],
        )}
      >
        <DialogHeader className="flex-row items-center justify-between gap-3 border-b border-[var(--border-hairline)] px-5 py-4 pr-12 text-left">
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-4">
          {children}
        </div>
        {footer && (
          <DialogFooter className="flex-row items-center justify-end gap-2 border-t border-[var(--border-hairline)] px-5 py-3">
            {footer}
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

/**
 * 确认弹窗：`@/ui/alert-dialog` 的组合。
 *
 * 与 `Modal` 的差别是 Radix 的语义本身：`AlertDialog` **不吃 Esc、点遮罩也不关**
 * （必须"取消 / 确认"二选一），也没有右上角的关闭按钮——这正是确认框该有的行为。
 * `busy` 时确认键禁用（禁用态由 `@/ui/button` 的实色令牌给）。
 */
export function ConfirmDialog({
  open,
  title,
  lead,
  note,
  confirmLabel = '确认',
  busyLabel = '处理中…',
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  lead: string
  note?: string
  confirmLabel?: string
  busyLabel?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel()
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{lead}</AlertDialogDescription>
          {note && <p className="text-hint">{note}</p>}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel}>取消</AlertDialogCancel>
          <AlertDialogAction variant="destructive" disabled={busy} onClick={onConfirm}>
            {busy ? busyLabel : confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

// ------------------------------------------------------------------ 头像 / 仪表

/**
 * 头像：`@/ui/avatar` 的组合（"图片加载失败就换兜底"由 Radix 的加载探测给）。
 *
 * 本域多出来的只有一条：**同一个人永远同一个底色**（按字符码求和取模定的色相），
 * 所以底色与字号仍写在内联样式上；尺寸是调用方要的像素值（设置页的头像预览要 96px）。
 */
export function Avatar({ name, url, size = 28 }: { name: string; url?: string; size?: number }) {
  const initial = Array.from(name.trim())[0] ?? '?'
  const hue = Array.from(name).reduce((sum, char) => sum + char.codePointAt(0)!, 0) % 360
  return (
    <AvatarRoot
      style={{ width: `${size}px`, height: `${size}px` }}
      aria-hidden="true"
      className="relative flex shrink-0 overflow-hidden rounded-pill select-none"
    >
      {url && <AvatarImage src={url} alt="" className="object-cover" />}
      <AvatarFallback
        className="text-text-secondary"
        style={{ background: `hsl(${hue} 24% 88%)`, fontSize: `${Math.round(size * 0.42)}px` }}
      >
        {initial}
      </AvatarFallback>
    </AvatarRoot>
  )
}

export function RingGauge({
  ratio,
  label,
  tone = 'accent',
  ariaLabel,
}: {
  /** 0–1。 */
  ratio: number
  label: string
  tone?: 'accent' | 'warning' | 'danger'
  ariaLabel: string
}) {
  const size = 48
  const stroke = 5
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const clamped = Math.min(Math.max(ratio, 0), 1)
  const color = `var(--${tone === 'accent' ? 'accent' : tone === 'warning' ? 'status-warning' : 'status-danger'})`
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={ariaLabel}
    >
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="var(--meter-track)"
        strokeWidth={stroke}
      />
      {/*
        **0 值不画弧**：`strokeDasharray="0 C"` 配圆头（`strokeLinecap="round"`）不是"什么都没有"，
        而是一个实心圆点——评审 T5 实测「0 / 1000 页」的环上就挂着这么一段蓝弧，
        图形与读数直接矛盾（CPU 首次采样拿不到差值时同理）。
        没有进度就是没有进度：0 只留轨道。
      */}
      {clamped > 0 && (
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${circumference * clamped} ${circumference}`}
          // 从 12 点方向顺时针画：起点转到正上方，进度才是"从满往里缺"的读法
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      )}
      <text
        x="50%"
        y="53%"
        textAnchor="middle"
        dominantBaseline="middle"
        fontSize="11"
        fill="var(--text-secondary)"
      >
        {label}
      </text>
    </svg>
  )
}
