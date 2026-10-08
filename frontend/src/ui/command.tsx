// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { SearchIcon } from 'lucide-react'
import { Command as CommandPrimitive } from 'cmdk'

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/ui/dialog'
import { cn } from '@/lib/utils'

/**
 * 命令面板（`cmdk`）。搜索框、列表、项都走菜单那一组令牌：
 * 表面 `--bg-menu`、项当前态 `--bg-hover`、描边 `--border` / `--border-hairline`、
 * 项高下限 `--menu-item-height`（`min-h-9`）、字号走带 `length:` 提示的任意值形式。
 *
 * `CommandDialog` **不重新实现弹窗壳**：它就是我们 `dialog.tsx` 的 `Dialog` +
 * `DialogContent`（`p-0` 让命令面板自己管内边距），标题/说明用 `sr-only` 藏起来
 * 只给读屏用——上游也是这么接的。
 *
 * 与上游的差别只有类名：`bg-popover` → `--bg-menu`、`text-muted-foreground` → `text-text-tertiary`
 * （说明性小字）、`outline-hidden` / `focus-visible:ring-*` 删除（焦点环只有全局那一处）。
 */
function Command({ className, ...props }: React.ComponentProps<typeof CommandPrimitive>) {
  return (
    <CommandPrimitive
      data-slot="command"
      className={cn(
        'flex h-full w-full flex-col overflow-hidden rounded-control bg-[var(--bg-menu)] text-text-primary',
        className,
      )}
      {...props}
    />
  )
}

function CommandDialog({
  title = '命令面板',
  description = '搜索要执行的命令……',
  children,
  className,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof Dialog> & {
  title?: string
  description?: string
  className?: string
  showCloseButton?: boolean
}) {
  return (
    <Dialog {...props}>
      <DialogHeader className="sr-only">
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <DialogContent
        className={cn('overflow-hidden p-0', className)}
        showCloseButton={showCloseButton}
      >
        <Command className="**:data-[slot=command-input-wrapper]:h-12 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-text-tertiary [&_[cmdk-group]]:px-2 [&_[cmdk-group]:not([hidden])_~[cmdk-group]]:pt-0 [&_[cmdk-input-wrapper]_svg]:size-5 [&_[cmdk-input]]:h-12 [&_[cmdk-item]]:px-2 [&_[cmdk-item]]:py-3 [&_[cmdk-item]_svg]:size-5">
          {children}
        </Command>
      </DialogContent>
    </Dialog>
  )
}

function CommandInput({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Input>) {
  return (
    <div
      data-slot="command-input-wrapper"
      className="flex h-9 items-center gap-2 border-b border-[var(--border-hairline)] px-3"
    >
      <SearchIcon className="size-4 shrink-0 text-text-tertiary" />
      <CommandPrimitive.Input
        data-slot="command-input"
        className={cn(
          'flex h-10 w-full rounded-control bg-transparent py-3 text-[length:var(--text-meta-size)] placeholder:text-text-quaternary disabled:cursor-not-allowed disabled:text-[var(--button-disabled-text)]',
          className,
        )}
        {...props}
      />
    </div>
  )
}

function CommandList({ className, ...props }: React.ComponentProps<typeof CommandPrimitive.List>) {
  return (
    <CommandPrimitive.List
      data-slot="command-list"
      className={cn('max-h-[300px] scroll-py-1 overflow-x-hidden overflow-y-auto', className)}
      {...props}
    />
  )
}

function CommandEmpty({ ...props }: React.ComponentProps<typeof CommandPrimitive.Empty>) {
  return (
    <CommandPrimitive.Empty
      data-slot="command-empty"
      className="py-6 text-center text-[length:var(--text-meta-size)] text-text-tertiary"
      {...props}
    />
  )
}

function CommandGroup({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Group>) {
  return (
    <CommandPrimitive.Group
      data-slot="command-group"
      className={cn(
        'overflow-hidden p-1 text-text-primary [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[length:var(--text-micro-size)] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-text-tertiary',
        className,
      )}
      {...props}
    />
  )
}

function CommandSeparator({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Separator>) {
  return (
    <CommandPrimitive.Separator
      data-slot="command-separator"
      className={cn('-mx-1 h-px bg-border', className)}
      {...props}
    />
  )
}

function CommandItem({ className, ...props }: React.ComponentProps<typeof CommandPrimitive.Item>) {
  return (
    <CommandPrimitive.Item
      data-slot="command-item"
      className={cn(
        'relative flex min-h-9 cursor-default items-center gap-2 rounded-control px-2 py-1.5 text-[length:var(--text-meta-size)] transition-colors select-none data-[disabled=true]:pointer-events-none data-[disabled=true]:text-[var(--button-disabled-text)] data-[selected=true]:bg-[var(--bg-hover)] data-[selected=true]:text-text-primary [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*="size-"])]:size-4 [&_svg:not([class*="text-"])]:text-text-tertiary',
        className,
      )}
      {...props}
    />
  )
}

function CommandShortcut({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      data-slot="command-shortcut"
      className={cn(
        'ml-auto text-[length:var(--text-micro-size)] tracking-widest text-text-tertiary',
        className,
      )}
      {...props}
    />
  )
}

export {
  Command,
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandShortcut,
  CommandSeparator,
}
