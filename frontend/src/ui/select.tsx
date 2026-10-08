// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { CheckIcon, ChevronDownIcon, ChevronUpIcon } from 'lucide-react'
import * as SelectPrimitive from '@radix-ui/react-select'

import { cn } from '@/lib/utils'

/**
 * 下拉选择。触发器与 `Input` **同高同款**（`h-8` / `--control-height`，
 * 描边 `--border`）——旧前端曾在这里出过"同一行里高差 11px"的问题，
 * 所以这四处取值不再各写各的。
 *
 * 聚焦（2026-09-24）：触发器是个 `<button>`，键盘走到它时**只出 `--ring` 那一层环**
 * （3px，画在描边外侧）。改前是 `focus:border-[var(--text-primary)]` 加全局墨色
 * `:focus-visible`——1px 墨边 + 2px 空隙 + 2px 墨环，用户点名的"黑线加粗"。
 * 两点有意与 shadcn 上游不同（理由同 tokens.css「焦点环」）：
 *   - 不像上游那样把 `border-ring` 也加上：本仓控件描边是 hairline，染成环色会与
 *     3px 环连成一条 4px 粗环——又回到"加粗"那条抱怨上；环单独画在外面已满足 3:1；
 *   - 环不半透明（上游 `ring-ring/50` 对白底约 1.7:1）。 */
function Select({ ...props }: React.ComponentProps<typeof SelectPrimitive.Root>) {
  return <SelectPrimitive.Root data-slot="select" {...props} />
}

function SelectGroup({ ...props }: React.ComponentProps<typeof SelectPrimitive.Group>) {
  return <SelectPrimitive.Group data-slot="select-group" {...props} />
}

function SelectValue({ ...props }: React.ComponentProps<typeof SelectPrimitive.Value>) {
  return <SelectPrimitive.Value data-slot="select-value" {...props} />
}

function SelectTrigger({
  className,
  size = 'default',
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Trigger> & { size?: 'sm' | 'default' }) {
  return (
    <SelectPrimitive.Trigger
      data-slot="select-trigger"
      data-focus-ring="self"
      data-size={size}
      className={cn(
        'flex w-fit items-center justify-between gap-2 rounded-control border border-border bg-surface px-3 py-1 text-[length:var(--text-body-size)] whitespace-nowrap transition-[color,box-shadow,border-color] outline-none hover:border-[var(--text-quaternary)] focus-visible:ring-[3px] focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] disabled:text-[var(--button-disabled-text)] aria-invalid:border-status-danger data-[placeholder]:text-text-quaternary data-[size=default]:h-8 data-[size=sm]:h-7 *:data-[slot=select-value]:line-clamp-1 *:data-[slot=select-value]:flex *:data-[slot=select-value]:items-center *:data-[slot=select-value]:gap-2 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*="size-"])]:size-4 [&_svg:not([class*="text-"])]:text-text-tertiary',
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDownIcon className="size-4 opacity-60" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  )
}

function SelectContent({
  className,
  children,
  position = 'item-aligned',
  align = 'center',
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        data-slot="select-content"
        className={cn(
          'relative z-50 max-h-(--radix-select-content-available-height) min-w-[8rem] origin-(--radix-select-content-transform-origin) overflow-x-hidden overflow-y-auto rounded-[var(--radius-overlay)] border border-border bg-[var(--bg-menu)] text-text-primary shadow-[var(--shadow-popover)] data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95',
          position === 'popper' &&
            'data-[side=bottom]:translate-y-1 data-[side=left]:-translate-x-1 data-[side=right]:translate-x-1 data-[side=top]:-translate-y-1',
          className,
        )}
        position={position}
        align={align}
        {...props}
      >
        <SelectScrollUpButton />
        <SelectPrimitive.Viewport
          className={cn(
            'p-2',
            position === 'popper' &&
              'h-[var(--radix-select-trigger-height)] w-full min-w-[var(--radix-select-trigger-width)] scroll-my-1',
          )}
        >
          {children}
        </SelectPrimitive.Viewport>
        <SelectScrollDownButton />
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  )
}

function SelectLabel({ className, ...props }: React.ComponentProps<typeof SelectPrimitive.Label>) {
  return (
    <SelectPrimitive.Label
      data-slot="select-label"
      className={cn(
        'px-2 py-1.5 text-[length:var(--text-micro-size)] text-text-tertiary',
        className,
      )}
      {...props}
    />
  )
}

function SelectItem({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      data-slot="select-item"
      className={cn(
        'relative flex min-h-9 w-full cursor-default items-center gap-2 rounded-control py-1.5 pr-8 pl-2 text-[length:var(--text-meta-size)] transition-colors select-none focus:bg-[var(--bg-hover)] focus:text-text-primary data-[disabled]:pointer-events-none data-[disabled]:text-[var(--button-disabled-text)] [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*="size-"])]:size-4 [&_svg:not([class*="text-"])]:text-text-tertiary *:[span]:last:flex *:[span]:last:items-center *:[span]:last:gap-2',
        className,
      )}
      {...props}
    >
      <span
        data-slot="select-item-indicator"
        className="absolute right-2 flex size-3.5 items-center justify-center"
      >
        <SelectPrimitive.ItemIndicator>
          <CheckIcon className="size-4" />
        </SelectPrimitive.ItemIndicator>
      </span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  )
}

function SelectSeparator({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Separator>) {
  return (
    <SelectPrimitive.Separator
      data-slot="select-separator"
      className={cn('pointer-events-none -mx-2 my-1 h-px bg-border', className)}
      {...props}
    />
  )
}

function SelectScrollUpButton({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.ScrollUpButton>) {
  return (
    <SelectPrimitive.ScrollUpButton
      data-slot="select-scroll-up-button"
      className={cn('flex cursor-default items-center justify-center py-1', className)}
      {...props}
    >
      <ChevronUpIcon className="size-4" />
    </SelectPrimitive.ScrollUpButton>
  )
}

function SelectScrollDownButton({
  className,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.ScrollDownButton>) {
  return (
    <SelectPrimitive.ScrollDownButton
      data-slot="select-scroll-down-button"
      className={cn('flex cursor-default items-center justify-center py-1', className)}
      {...props}
    >
      <ChevronDownIcon className="size-4" />
    </SelectPrimitive.ScrollDownButton>
  )
}

export {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectScrollDownButton,
  SelectScrollUpButton,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
}
