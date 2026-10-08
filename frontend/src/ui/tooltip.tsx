// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'

import { cn } from '@/lib/utils'

/** 延迟取 0：旧前端 `InfoTip` 是"移上去就出现"，中间那 700ms 的等待只会让人以为没反应。 */
function TooltipProvider({
  delayDuration = 0,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  )
}

function Tooltip({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />
}

function TooltipTrigger({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />
}

/**
 * 气泡本体。**两处与上游不同**（都记在 src/ui/README.md）：
 * 1. 上游是"反色气泡"（`bg-foreground` + `text-background`），我们是**浅底 + 描边**，
 *    与旧前端 `InfoTip` 一致（浅色下反色气泡在暖白界面上过于抢眼）；
 * 2. **移除了小三角**（`TooltipPrimitive.Arrow`）：带 1px 描边的气泡上再接一个实心三角
 *    会留下一道接缝，而旧前端的提示气泡本来就没有三角。需要时自己传一个 Arrow 即可。
 */
function TooltipContent({
  className,
  sideOffset = 6,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          'z-50 w-fit origin-(--radix-tooltip-content-transform-origin) rounded-control border border-border bg-surface px-3 py-2 text-[length:var(--text-micro-size)] text-text-primary text-balance shadow-[var(--shadow-popover)] animate-in fade-in-0 zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95',
          className,
        )}
        {...props}
      >
        {children}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
