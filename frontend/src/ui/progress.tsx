// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as ProgressPrimitive from '@radix-ui/react-progress'

import { cn } from '@/lib/utils'

/**
 * 进度条。两个色都取自 tokens.css：
 * 空槽 `--meter-track`（`Fills-F3`，注释里写明"进度条空槽"就是这一条），
 * 填充 `--accent`（品牌蓝只做小面积强调，进度条正是小面积）。
 *
 * 上游的 `bg-primary/20` 那种半透明底不能用——浅色下它几乎看不见，
 * 而"共 6 段、走到第 3 段"这条信息不能只靠文字撑（tokens.css 对这一档有同样的注）。
 * `rounded-full` → `rounded-pill`；指示条 100% 宽 + `translateX` 位移，与上游同款。
 *
 * 一处**有意**偏离上游：`value` 也传给 `Root`。上游把 `value` 只用于算 `translateX`，
 * 于是 `role="progressbar"` 上既没有 `aria-valuenow` 也没有 `aria-valuemax`——
 * 读屏拿到的是一个"不知道走到哪"的进度条。Radix 自己会补这两个属性，只是要拿到 `value`。
 */
function Progress({
  className,
  value,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root>) {
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      value={value}
      className={cn(
        'relative h-2 w-full overflow-hidden rounded-pill bg-[var(--meter-track)]',
        className,
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className="h-full w-full flex-1 bg-[var(--accent)] transition-transform"
        style={{ transform: `translateX(-${100 - (value || 0)}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

export { Progress }
