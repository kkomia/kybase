// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { XIcon } from 'lucide-react'
import * as DialogPrimitive from '@radix-ui/react-dialog'

import { Button } from '@/ui/button'
import { cn } from '@/lib/utils'

/**
 * 模态弹窗。与旧前端 `AppModal` 对齐的取值：
 * 遮罩 `--overlay-scrim`、表面 `--bg-overlay`（Kimi 那个带蓝调的弹层底）、
 * 圆角 `--radius-overlay`（20px）、内边距 `--space-4`（16px）、宽度 480px（旧 `md` 档）。
 * 关闭按钮 28×28（`--hit-target` 之上），悬停 `--bg-hover` + 文字转 `--text-primary`。
 *
 * 旧 `AppModal` 的"标题栏/底部各带一条分隔线、内容区自己滚"由调用方组合出来：
 *
 *   <DialogContent className="flex max-h-[80vh] flex-col gap-0 p-0">
 *     <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3">…
 *     <div className="min-h-0 flex-1 overflow-y-auto p-4">…
 *     <DialogFooter className="border-t border-[var(--border-hairline)] px-4 py-3">…
 *
 * 弹窗比 `<dialog>` 好的一点：Radix 用的是 portal 而不是浏览器 top-layer，
 * 所以 Toast（sonner）这类 `z-50` 的全局浮层不会被弹窗盖住。
 */
function Dialog({ ...props }: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />
}

function DialogTrigger({ ...props }: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />
}

function DialogPortal({ ...props }: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />
}

function DialogClose({ ...props }: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        'fixed inset-0 z-50 bg-[var(--overlay-scrim)] data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0',
        className,
      )}
      {...props}
    />
  )
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & { showCloseButton?: boolean }) {
  return (
    <DialogPortal data-slot="dialog-portal">
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          'fixed top-[50%] left-[50%] z-50 grid w-full max-w-[calc(100%-2rem)] translate-x-[-50%] translate-y-[-50%] gap-4 rounded-[var(--radius-overlay)] border border-[var(--border-hairline)] bg-[var(--bg-overlay)] p-4 shadow-[var(--shadow-popover)] duration-200 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 sm:max-w-[480px]',
          className,
        )}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close
            data-slot="dialog-close"
            className="absolute top-3 right-3 inline-flex size-7 items-center justify-center rounded-control text-text-secondary transition-colors hover:bg-[var(--bg-hover)] hover:text-text-primary disabled:pointer-events-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4"
          >
            <XIcon />
            <span className="sr-only">关闭</span>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
}

function DialogHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="dialog-header"
      className={cn('flex flex-col gap-2 text-center sm:text-left', className)}
      {...props}
    />
  )
}

function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<'div'> & { showCloseButton?: boolean }) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">关闭</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  )
}

function DialogTitle({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn(
        'text-[length:var(--text-section-size)] leading-none font-semibold tracking-[-0.005em] text-text-primary',
        className,
      )}
      {...props}
    />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn('text-[length:var(--text-meta-size)] text-text-secondary', className)}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
}
