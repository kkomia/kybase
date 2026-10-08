// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { Slot } from '@radix-ui/react-slot'

import { cn } from '@/lib/utils'

/**
 * 小徽章。形状默认取 tokens.css 里的 `--radius-badge`（**4px 方角，不是胶囊**）：
 * Kimi 的 badge 一律 `4px + padding 1px 4px + 12px 字`。
 *
 * **`shape="pill"` 是给本仓留的口子**：旧前端的**状态标签**（`StatusTag.vue` 的 `.status`）
 * 是明确的胶囊——`height: 22px; padding: 0 var(--space-2); border-radius: var(--radius-pill)`。
 * 所以那不是"把徽章做成胶囊"（那件事按上面的注解就是该避免的），而是**另一种东西**：
 * 状态标签与卡片里的小徽章（旧 `.chip`，本来就是 4px 方角）是两类，形状本来就不同。
 * 加一个形状变体而不是全局改默认值，就是为了不把"方角徽章"这个既定取值一起改掉。
 *
 * 默认色是"蓝字 + 蓝的 10% 底"（`--badge-bg` / `--badge-text`）。
 */
const badgeVariants = cva(
  'inline-flex w-fit shrink-0 items-center justify-center gap-1 overflow-hidden border border-transparent px-1 py-0.5 text-[length:var(--text-micro-size)] font-medium whitespace-nowrap transition-[color,box-shadow] [&>svg]:pointer-events-none [&>svg]:size-3',
  {
    variants: {
      variant: {
        default: 'bg-[var(--badge-bg)] text-[var(--badge-text)]',
        secondary: 'bg-[var(--bg-subtle)] text-text-secondary',
        destructive: 'bg-[var(--status-danger-soft)] text-status-danger',
        success: 'bg-[var(--status-success-soft)] text-status-success',
        warning: 'bg-[var(--status-warning-soft)] text-status-warning',
        outline: 'border-border text-text-primary',
        ghost: 'text-text-secondary [a&]:hover:bg-[var(--bg-hover)]',
        link: 'text-accent-text underline-offset-4 [a&]:hover:underline',
      },
      shape: {
        square: 'rounded-[var(--radius-badge)]',
        /** 状态标签那一档：22px 高、左右 8px——与旧 `.status` 同一套取值。 */
        pill: 'h-[22px] rounded-[var(--radius-pill)] px-[var(--space-2)] py-0',
      },
    },
    defaultVariants: { variant: 'default', shape: 'square' },
  },
)

function Badge({
  className,
  variant = 'default',
  shape = 'square',
  asChild = false,
  ...props
}: React.ComponentProps<'span'> & VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'span'

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      data-shape={shape}
      className={cn(badgeVariants({ variant, shape }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }
