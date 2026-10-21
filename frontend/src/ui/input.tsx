// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * 单行输入。高度 `--control-height`（36px），圆角 `--radius-input`（12px），白面。
 *
 * 聚焦只画**一层**品牌蓝环（3px、18% 透明度），描边同时转蓝：
 * 输入框是"正在这里写字"的地方，它是这一版里少数几处允许出现蓝的控件之一。
 * `data-focus-ring="self"` 让 tokens.css 那条全局墨环跳过本元素——
 * 环只出现一次，从"谁压过谁"变成"根本不会同时出现"。
 */
function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type={type}
      data-slot="input"
      data-focus-ring="self"
      className={cn(
        'h-9 w-full min-w-0 rounded-[var(--radius-input)] border border-[var(--border)] bg-surface px-3 py-1 text-[length:var(--text-body-size)] text-text-primary shadow-[0_1px_1px_rgb(31_26_15/3%)] transition-[color,box-shadow,border-color,background-color] duration-[var(--motion-fast)] outline-none selection:bg-[var(--accent-selected)] selection:text-text-primary placeholder:text-text-quaternary hover:border-[var(--border-strong)] focus:border-[var(--accent)] focus:ring-[3px] focus:ring-[var(--accent-selected)] file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-meta file:font-medium file:text-text-primary disabled:cursor-not-allowed disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] disabled:text-[var(--button-disabled-text)] aria-invalid:border-status-danger',
        className,
      )}
      {...props}
    />
  )
}

export { Input }
