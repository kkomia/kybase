// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { Slot } from '@radix-ui/react-slot'

import { cn } from '@/lib/utils'

/**
 * 小徽章 / 状态标签。默认**胶囊形**（`--radius-badge` = 999px）：
 *
 * 2026-10 重做把默认形状从"4px 方角"换成胶囊——这一版里徽章承载的是**状态短语**
 * （"已完成""解析中""只读"），胶囊读起来像一个状态，方角读起来像一枚被裁过的标签。
 * `shape="square"` 留给"种类 / 计数"这类名词性标记（旧调用方按需自选）。
 *
 * 色相只用语义色的 10% 低饱和底 + 达标字色，两套主题同一套取值逻辑。
 */
const badgeVariants = cva(
  'inline-flex w-fit shrink-0 items-center justify-center gap-1 overflow-hidden border border-transparent px-2 py-0.5 text-[length:var(--text-micro-size)] leading-[18px] font-medium whitespace-nowrap transition-[color,background-color] duration-[var(--motion-fast)] [&>svg]:pointer-events-none [&>svg]:size-3',
  {
    variants: {
      variant: {
        default: 'bg-[var(--badge-bg)] text-[var(--badge-text)]',
        secondary: 'bg-[var(--bg-subtle)] text-text-secondary',
        destructive: 'bg-[var(--status-danger-soft)] text-status-danger',
        success: 'bg-[var(--status-success-soft)] text-status-success',
        warning: 'bg-[var(--status-warning-soft)] text-status-warning',
        outline: 'border-[var(--border)] text-text-secondary',
        ghost: 'text-text-secondary [a&]:hover:bg-[var(--bg-hover)]',
        link: 'text-accent-text underline-offset-4 [a&]:hover:underline',
      },
      shape: {
        square: 'rounded-[6px]',
        pill: 'rounded-[var(--radius-badge)]',
      },
      /** 带一个小圆点（状态标签用）：点用 `currentColor`，跟着语义色走。 */
      dot: {
        true: '',
      },
    },
    defaultVariants: { variant: 'default', shape: 'pill' },
  },
)

function Badge({
  className,
  variant = 'default',
  shape = 'pill',
  asChild = false,
  children,
  ...props
}: React.ComponentProps<'span'> &
  VariantProps<typeof badgeVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot : 'span'

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      data-shape={shape}
      className={cn(badgeVariants({ variant, shape }), className)}
      {...props}
    >
      {children}
    </Comp>
  )
}

export { Badge, badgeVariants }
