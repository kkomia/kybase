// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as ScrollAreaPrimitive from '@radix-ui/react-scroll-area'

import { cn } from '@/lib/utils'

/**
 * 滚动容器。Radix 的滚动条是**自己画的**（不走原生 `::-webkit-scrollbar`），
 * 所以取值要手动与 tokens.css 对齐：条槽 12px（`--webkit-scrollbar { width: 12px }`）、
 * 滑块 `--border-strong`、圆角胶囊、条槽透明。
 *
 * 只是需要"滚动 + 细滚动条"时**不必用它**：全局那条原生滚动条已经够用且更省事
 * （tokens.css 里那一大段就是干这个的）。它真正不可替代的场景只有一个——
 * **要和浮层一起滚动**（如菜单里的长列表），那时才需要 Radix 这层包装。
 */
function ScrollArea({
  className,
  children,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.Root>) {
  return (
    <ScrollAreaPrimitive.Root
      data-slot="scroll-area"
      className={cn('relative', className)}
      {...props}
    >
      <ScrollAreaPrimitive.Viewport
        data-slot="scroll-area-viewport"
        className="size-full rounded-[inherit] transition-[color,box-shadow]"
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollBar />
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  )
}

function ScrollBar({
  className,
  orientation = 'vertical',
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      data-slot="scroll-area-scrollbar"
      orientation={orientation}
      className={cn(
        'flex touch-none transition-colors select-none',
        orientation === 'vertical' && 'h-full w-3 border-l border-l-transparent',
        orientation === 'horizontal' && 'h-3 flex-col border-t border-t-transparent',
        className,
      )}
      {...props}
    >
      <ScrollAreaPrimitive.ScrollAreaThumb
        data-slot="scroll-area-thumb"
        className="relative flex-1 rounded-pill bg-[var(--border-strong)]"
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
