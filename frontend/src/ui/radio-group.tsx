// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { CircleIcon } from 'lucide-react'
import * as RadioGroupPrimitive from '@radix-ui/react-radio-group'

import { cn } from '@/lib/utils'

/**
 * 单选组。选中色与复选框同一档：**品牌蓝** `--accent`（小面积强调），
 * 未选中描边 `--border`，禁用态 `--button-disabled-*` 实色（不用 opacity）。
 * 圆点是 16px 的 `rounded-pill` 圆槽 + 8px 实心点，与上游一致。
 */
function RadioGroup({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Root>) {
  return (
    <RadioGroupPrimitive.Root
      data-slot="radio-group"
      className={cn('grid gap-3', className)}
      {...props}
    />
  )
}

function RadioGroupItem({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Item>) {
  return (
    <RadioGroupPrimitive.Item
      data-slot="radio-group-item"
      className={cn(
        'aspect-square size-4 shrink-0 rounded-pill border border-[var(--border-strong)] text-[var(--accent)] transition-[color,background-color,border-color] disabled:cursor-not-allowed disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] aria-invalid:border-status-danger data-[state=checked]:border-[var(--accent)]',
        className,
      )}
      {...props}
    >
      <RadioGroupPrimitive.Indicator
        data-slot="radio-group-indicator"
        className="relative flex items-center justify-center"
      >
        <CircleIcon className="absolute top-1/2 left-1/2 size-2 -translate-x-1/2 -translate-y-1/2 fill-current" />
      </RadioGroupPrimitive.Indicator>
    </RadioGroupPrimitive.Item>
  )
}

export { RadioGroup, RadioGroupItem }
