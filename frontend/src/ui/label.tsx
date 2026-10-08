// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as LabelPrimitive from '@radix-ui/react-label'

import { cn } from '@/lib/utils'

/**
 * 表单标签。取值照 tokens.css 里那条"全局唯一口径"（`.field-label`）：
 * `--text-micro-size` + 500 + `--text-secondary`。
 *
 * 禁用态与上游不同：用 `--button-disabled-text` 的**实色**而不是 `opacity-50`
 * （同一个表单里的按钮与输入框都是实色禁用，只有标签半透明就不齐了）。
 */
function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn(
        'flex items-center gap-2 text-[length:var(--text-micro-size)] leading-none font-medium text-text-secondary select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:text-[var(--button-disabled-text)] peer-disabled:cursor-not-allowed peer-disabled:text-[var(--button-disabled-text)]',
        className,
      )}
      {...props}
    />
  )
}

export { Label }
