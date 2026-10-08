// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { CheckIcon } from 'lucide-react'
import * as CheckboxPrimitive from '@radix-ui/react-checkbox'

import { cn } from '@/lib/utils'

/**
 * 复选框。与上游的三处偏离（都写进 README §4）：
 * 1. **选中态是品牌蓝**（`--accent`），不是上游那套 `bg-primary` 的墨色实心——
 *    墨色实心在本仓只有一个意思："主按钮"；勾选框变成墨块会被读成一个按钮。
 *    勾选标记用恒定白（`--Always-White`，两套主题同值），和开关滑块压在蓝底上时同一档。
 * 2. 禁用态用 `--button-disabled-*` 三件套，不用 `opacity-50`。
 * 3. `rounded-[4px]` → `rounded-[var(--radius-badge)]`（同为 4px 方角，与徽章一档）。
 *
 * 上游的 `shadow-xs` 删掉（本仓静态内容不用阴影）；`dark:`、`focus-visible:ring-*`、
 * `outline-none` 一并删（见 README §1）。
 */
function Checkbox({ className, ...props }: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        'peer size-4 shrink-0 rounded-[var(--radius-badge)] border border-[var(--border-strong)] transition-[color,background-color,border-color] disabled:cursor-not-allowed disabled:border-[var(--button-disabled-border)] disabled:bg-[var(--button-disabled-bg)] aria-invalid:border-status-danger data-[state=checked]:border-[var(--accent)] data-[state=checked]:bg-[var(--accent)] data-[state=checked]:text-[var(--Always-White)]',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="grid place-content-center text-current"
      >
        <CheckIcon className="size-3.5" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
