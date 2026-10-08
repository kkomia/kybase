// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as AvatarPrimitive from '@radix-ui/react-avatar'

import { cn } from '@/lib/utils'

/**
 * 头像。默认尺寸取 **`--avatar-size`（28px）**——tokens.css 里这一条就是照 Kimi 的
 * `.not-login-icon` 实测写的，所以"默认头像多大"只在这一处。`sm` / `lg` 保持上游的
 * 24 / 40px（这两个不在令牌阶梯上，是尺寸档不是颜色档，用数值刻度即可）。
 *
 * 圆形一律 `rounded-pill`；兜底底色 `--bg-subtle`、文字 `--text-secondary`——
 * 上游用的是 `text-muted-foreground`（三级灰），但首字母/缩写是**要被读到的内容**，
 * 而 tokens.css 对三级灰的约束是"只给可略过的元信息"，所以这里用二级灰。
 *
 * 上游的 `ring-2 ring-background`（状态点/成组头像外面那一圈与底色同色的"隔断"）
 * 换成 `border-2 border-canvas`：本仓的 `ring-*` 只有全局焦点环一处，
 * 这里要的是描边而不是环，换成 border 语义更准，取值仍是同一个 `--bg-canvas`。
 */
function Avatar({
  className,
  size = 'default',
  ...props
}: React.ComponentProps<typeof AvatarPrimitive.Root> & {
  size?: 'default' | 'sm' | 'lg'
}) {
  return (
    <AvatarPrimitive.Root
      data-slot="avatar"
      data-size={size}
      className={cn(
        'group/avatar relative flex size-[var(--avatar-size)] shrink-0 overflow-hidden rounded-pill select-none data-[size=lg]:size-10 data-[size=sm]:size-6',
        className,
      )}
      {...props}
    />
  )
}

function AvatarImage({ className, ...props }: React.ComponentProps<typeof AvatarPrimitive.Image>) {
  return (
    <AvatarPrimitive.Image
      data-slot="avatar-image"
      className={cn('aspect-square size-full', className)}
      {...props}
    />
  )
}

function AvatarFallback({
  className,
  ...props
}: React.ComponentProps<typeof AvatarPrimitive.Fallback>) {
  return (
    <AvatarPrimitive.Fallback
      data-slot="avatar-fallback"
      className={cn(
        'flex size-full items-center justify-center rounded-pill bg-[var(--bg-subtle)] text-[length:var(--text-meta-size)] text-text-secondary group-data-[size=sm]/avatar:text-[length:var(--text-micro-size)]',
        className,
      )}
      {...props}
    />
  )
}

function AvatarBadge({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      data-slot="avatar-badge"
      className={cn(
        'absolute right-0 bottom-0 z-10 inline-flex items-center justify-center rounded-pill border-2 border-canvas bg-[var(--button-primary-bg)] text-[var(--button-primary-text)] select-none',
        'group-data-[size=sm]/avatar:size-2 group-data-[size=sm]/avatar:[&>svg]:hidden',
        'group-data-[size=default]/avatar:size-2.5 group-data-[size=default]/avatar:[&>svg]:size-2',
        'group-data-[size=lg]/avatar:size-3 group-data-[size=lg]/avatar:[&>svg]:size-2',
        className,
      )}
      {...props}
    />
  )
}

function AvatarGroup({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="avatar-group"
      className={cn(
        'group/avatar-group flex -space-x-2 *:data-[slot=avatar]:border-2 *:data-[slot=avatar]:border-canvas',
        className,
      )}
      {...props}
    />
  )
}

function AvatarGroupCount({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="avatar-group-count"
      className={cn(
        'relative flex size-[var(--avatar-size)] shrink-0 items-center justify-center rounded-pill border-2 border-canvas bg-[var(--bg-subtle)] text-[length:var(--text-meta-size)] text-text-secondary group-has-data-[size=lg]/avatar-group:size-10 group-has-data-[size=sm]/avatar-group:size-6 [&>svg]:size-4 group-has-data-[size=lg]/avatar-group:[&>svg]:size-5 group-has-data-[size=sm]/avatar-group:[&>svg]:size-3',
        className,
      )}
      {...props}
    />
  )
}

export { Avatar, AvatarImage, AvatarFallback, AvatarBadge, AvatarGroup, AvatarGroupCount }
