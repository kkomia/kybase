// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { Drawer as DrawerPrimitive } from 'vaul'

import { cn } from '@/lib/utils'

/**
 * 抽屉（`vaul`）。与 `sheet.tsx` 的区别只有交互：这一版带**手势拖拽关闭**与
 * 底部把手，卖点在移动端；桌面上的抽屉仍推荐用 `sheet`（它不需要拖拽，
 * 也没有 vaul 那套 `window.visualViewport` 的键盘避让逻辑）。
 *
 * 取值与 `sheet` / `dialog` 同一套：遮罩 `--overlay-scrim`、表面 `--bg-overlay`、
 * 贴在窗口边的那条边保留 1px `--border-hairline`（贴在窗口边上时它是"这是两块"的唯一线索）、
 * 上下方向的圆角取 `--radius-overlay`（20px，与其它浮层一致）。
 * 把手的颜色取 `--border-strong`——它和滚动条滑块是同一个角色（比描边深一档的实心条）。
 *
 * 上游的 `bg-black/50` → `bg-[var(--overlay-scrim)]`，`bg-background` → `bg-[var(--bg-overlay)]`，
 * `bg-muted`（把手）→ `bg-[var(--border-strong)]`，`rounded-full` → `rounded-pill`；
 * `dark:` / `outline-none` / `focus-visible:ring-*` 全部没有（上游这一版也没有）。
 *
 * 无障碍：`vaul` 底下就是 `@radix-ui/react-dialog`，`role="dialog"` /
 * `aria-modal` / 焦点陷阱 / Esc 关闭都由它给；所以 `DrawerTitle` 不能省。
 */
function Drawer({ ...props }: React.ComponentProps<typeof DrawerPrimitive.Root>) {
  return <DrawerPrimitive.Root data-slot="drawer" {...props} />
}

function DrawerTrigger({ ...props }: React.ComponentProps<typeof DrawerPrimitive.Trigger>) {
  return <DrawerPrimitive.Trigger data-slot="drawer-trigger" {...props} />
}

function DrawerPortal({ ...props }: React.ComponentProps<typeof DrawerPrimitive.Portal>) {
  return <DrawerPrimitive.Portal data-slot="drawer-portal" {...props} />
}

function DrawerClose({ ...props }: React.ComponentProps<typeof DrawerPrimitive.Close>) {
  return <DrawerPrimitive.Close data-slot="drawer-close" {...props} />
}

function DrawerOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DrawerPrimitive.Overlay>) {
  return (
    <DrawerPrimitive.Overlay
      data-slot="drawer-overlay"
      className={cn(
        'fixed inset-0 z-50 bg-[var(--overlay-scrim)] data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0',
        className,
      )}
      {...props}
    />
  )
}

function DrawerContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DrawerPrimitive.Content>) {
  return (
    <DrawerPortal data-slot="drawer-portal">
      <DrawerOverlay />
      <DrawerPrimitive.Content
        data-slot="drawer-content"
        className={cn(
          'group/drawer-content fixed z-50 flex h-auto flex-col bg-[var(--bg-overlay)]',
          'data-[vaul-drawer-direction=top]:inset-x-0 data-[vaul-drawer-direction=top]:top-0 data-[vaul-drawer-direction=top]:mb-24 data-[vaul-drawer-direction=top]:max-h-[80vh] data-[vaul-drawer-direction=top]:rounded-b-[var(--radius-overlay)] data-[vaul-drawer-direction=top]:border-b data-[vaul-drawer-direction=top]:border-[var(--border-hairline)]',
          'data-[vaul-drawer-direction=bottom]:inset-x-0 data-[vaul-drawer-direction=bottom]:bottom-0 data-[vaul-drawer-direction=bottom]:mt-24 data-[vaul-drawer-direction=bottom]:max-h-[80vh] data-[vaul-drawer-direction=bottom]:rounded-t-[var(--radius-overlay)] data-[vaul-drawer-direction=bottom]:border-t data-[vaul-drawer-direction=bottom]:border-[var(--border-hairline)]',
          'data-[vaul-drawer-direction=right]:inset-y-0 data-[vaul-drawer-direction=right]:right-0 data-[vaul-drawer-direction=right]:w-3/4 data-[vaul-drawer-direction=right]:border-l data-[vaul-drawer-direction=right]:border-[var(--border-hairline)] data-[vaul-drawer-direction=right]:sm:max-w-sm',
          'data-[vaul-drawer-direction=left]:inset-y-0 data-[vaul-drawer-direction=left]:left-0 data-[vaul-drawer-direction=left]:w-3/4 data-[vaul-drawer-direction=left]:border-r data-[vaul-drawer-direction=left]:border-[var(--border-hairline)] data-[vaul-drawer-direction=left]:sm:max-w-sm',
          className,
        )}
        {...props}
      >
        <div className="mx-auto mt-4 hidden h-2 w-[100px] shrink-0 rounded-pill bg-[var(--border-strong)] group-data-[vaul-drawer-direction=bottom]/drawer-content:block" />
        {children}
      </DrawerPrimitive.Content>
    </DrawerPortal>
  )
}

function DrawerHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="drawer-header"
      className={cn(
        'flex flex-col gap-0.5 p-4 group-data-[vaul-drawer-direction=bottom]/drawer-content:text-center group-data-[vaul-drawer-direction=top]/drawer-content:text-center md:gap-1.5 md:text-left',
        className,
      )}
      {...props}
    />
  )
}

function DrawerFooter({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="drawer-footer"
      className={cn('mt-auto flex flex-col gap-2 p-4', className)}
      {...props}
    />
  )
}

function DrawerTitle({ className, ...props }: React.ComponentProps<typeof DrawerPrimitive.Title>) {
  return (
    <DrawerPrimitive.Title
      data-slot="drawer-title"
      className={cn(
        'text-[length:var(--text-section-size)] font-semibold tracking-[-0.005em] text-text-primary',
        className,
      )}
      {...props}
    />
  )
}

function DrawerDescription({
  className,
  ...props
}: React.ComponentProps<typeof DrawerPrimitive.Description>) {
  return (
    <DrawerPrimitive.Description
      data-slot="drawer-description"
      className={cn('text-[length:var(--text-meta-size)] text-text-secondary', className)}
      {...props}
    />
  )
}

export {
  Drawer,
  DrawerPortal,
  DrawerOverlay,
  DrawerTrigger,
  DrawerClose,
  DrawerContent,
  DrawerHeader,
  DrawerFooter,
  DrawerTitle,
  DrawerDescription,
}
