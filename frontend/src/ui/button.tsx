// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { Slot } from '@radix-ui/react-slot'

import { cn } from '@/lib/utils'

/**
 * 按钮。四档高度（36 / 32 / 28 / 40），与输入框、下拉共用 `--control-height` 那一档。
 *
 * ## 2026-10 重做的三处变化
 *
 * 1. **主按钮是品牌蓝**（旧版是墨色实心）：这一版里"动作"与"当前"是蓝的，
 *    墨色留给字。蓝底白字实测 5.6:1；
 * 2. **按下缩 2%**（`active:scale-[0.98]`）：120ms 的一下，手指与鼠标都能感到"接住了"；
 * 3. **主按钮带一层内高光 + 极浅的落影**：它是一块"按下去的实体"，
 *    而不是一块被填了颜色的矩形。
 *
 * 焦点环只画自己那一层（`data-focus-ring="self"` 让 tokens.css 的全局墨环跳过本元素）。
 */
const buttonVariants = cva(
  'inline-flex shrink-0 items-center justify-center gap-1.5 rounded-[var(--radius-control)] text-[length:var(--text-meta-size)] font-medium whitespace-nowrap transition-[color,background-color,border-color,box-shadow,transform] duration-[var(--motion-fast)] ease-out outline-none select-none active:scale-[0.98] focus-visible:ring-[3px] focus-visible:ring-[var(--accent-selected)] disabled:pointer-events-none disabled:cursor-not-allowed disabled:border disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] disabled:text-[var(--button-disabled-text)] disabled:shadow-none disabled:active:scale-100 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*="size-"])]:size-4',
  {
    variants: {
      variant: {
        default:
          'bg-[var(--button-primary-bg)] text-[var(--button-primary-text)] shadow-[0_1px_2px_rgb(31_26_15/14%),inset_0_1px_0_rgb(255_255_255/14%)] hover:bg-[var(--button-primary-bg-hover)]',
        secondary:
          'border border-[var(--button-secondary-border)] bg-surface text-text-primary shadow-[0_1px_1px_rgb(31_26_15/4%)] hover:border-[var(--border-strong)] hover:bg-[var(--bg-subtle)]',
        outline:
          'border border-[var(--border)] bg-transparent text-text-primary hover:border-[var(--border-strong)] hover:bg-[var(--bg-hover)]',
        ghost:
          'border border-transparent text-text-secondary hover:bg-[var(--bg-hover)] hover:text-text-primary',
        destructive: 'border border-transparent text-status-danger hover:bg-[var(--danger-soft)]',
        link: 'text-accent-text underline-offset-4 hover:underline',
      },
      size: {
        default: 'h-9 px-3.5 py-1 has-[>svg]:px-3',
        xs: 'h-6 gap-1 rounded-[var(--radius-control)] px-2 text-[length:var(--text-micro-size)] has-[>svg]:px-1.5 [&_svg:not([class*="size-"])]:size-3',
        sm: 'h-8 gap-1.5 px-3',
        lg: 'h-10 px-5',
        icon: 'size-9',
        'icon-xs': 'size-6 [&_svg:not([class*="size-"])]:size-3',
        'icon-sm': 'size-8',
        'icon-lg': 'size-10',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
)

function Button({
  className,
  variant = 'default',
  size = 'default',
  asChild = false,
  ...props
}: React.ComponentProps<'button'> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot : 'button'

  return (
    <Comp
      data-slot="button"
      data-focus-ring="self"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  )
}

export { Button, buttonVariants }
