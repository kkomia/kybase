// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * 多行输入。多行天然高于控件口径，所以这里只给下限（`min-h-16` = 64px，与旧前端
 * `rows=3` 的实际高度一致）；`field-sizing-content` 让它跟着内容长。
 *
 * 聚焦与 `Input` **同一口径**（两者在同一个表单里常常上下相邻，不能一个样一个样）：
 * 只画一层贴内沿的 `--ring` 环、描边不随聚焦变色、用 `:focus` 而不是 `:focus-visible`、
 * 并带 `data-focus-ring="self"` 让全局那条墨色 `:focus-visible` 跳过它。
 * 完整理由写在 `input.tsx` 的文件注释里（含改前的 5px 双黑环实测）。
 */
function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      data-slot="textarea"
      data-focus-ring="self"
      className={cn(
        'flex field-sizing-content min-h-16 w-full resize-y rounded-control border border-border bg-surface px-3 py-2 text-[length:var(--text-body-size)] text-text-primary transition-[color,box-shadow,border-color] outline-none placeholder:text-text-quaternary hover:border-[var(--text-quaternary)] focus:ring-[2px] focus:ring-inset focus:ring-[var(--ring)] disabled:cursor-not-allowed disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] disabled:text-[var(--button-disabled-text)] aria-invalid:border-status-danger',
        className,
      )}
      {...props}
    />
  )
}

export { Textarea }
