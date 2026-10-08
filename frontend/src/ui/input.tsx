// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * 单行输入。高度就是 `--control-height`（32px），与按钮、下拉同一个口径；
 * 文字用 `text-[length:var(--text-body-size)]`（15px，与旧前端 `.field { font: inherit }` 一致，不是上游的 16px）。
 * 悬停仍转一档（`--text-quaternary`）。
 *
 * **聚焦：只画一层环**（2026-09-24 用户点名"很多输入框或者下拉框的选中，黑线加粗过于逆天"）。
 * 此前这里是 `focus:border-[var(--text-primary)]`，再叠上 tokens.css 那条全局墨色
 * `:focus-visible`，实测是"内圈 1px 墨边 + 2px 空隙 + 外圈 2px 墨环"的 5px 双黑环。
 * 现在照 **Radix Themes 文本输入**的口径办：
 *   - 用 `:focus` 而不是 `:focus-visible`——文本输入点进来就要看得见
 *     （Chrome 里点击文本输入同样命中 `:focus-visible`，所以此前**点击**也是双环）；
 *   - 环用 box-shadow 的 `ring-inset` 画在描边内侧（等于 Radix 的 `outline-offset: -1px`），
 *     不再有"描边 + 外环"两层；
 *   - 描边不再随聚焦变色（那正是双环的来源）；
 *   - 环色 `--ring`（3.23:1，实测与取值理由见 tokens.css「焦点环」）、宽 2px。
 * `outline-none` 与 `data-focus-ring="self"` 管同一件事：后者让 tokens.css 那条全局
 * `:focus-visible` 跳过本元素——双环从"谁压过谁"变成"根本不会同时出现"。
 */
function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type={type}
      data-slot="input"
      data-focus-ring="self"
      className={cn(
        'h-8 w-full min-w-0 rounded-control border border-border bg-surface px-3 py-1 text-[length:var(--text-body-size)] text-text-primary transition-[color,box-shadow,border-color] outline-none selection:bg-[var(--accent-selected)] selection:text-text-primary placeholder:text-text-quaternary hover:border-[var(--text-quaternary)] focus:ring-[2px] focus:ring-inset focus:ring-[var(--ring)] file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-meta file:font-medium file:text-text-primary disabled:cursor-not-allowed disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] disabled:text-[var(--button-disabled-text)] aria-invalid:border-status-danger',
        className,
      )}
      {...props}
    />
  )
}

export { Input }
