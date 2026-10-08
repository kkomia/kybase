// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as SwitchPrimitive from '@radix-ui/react-switch'

import { cn } from '@/lib/utils'

/**
 * 开关。色只用两处：**打开**是品牌蓝（`--accent`，小面积强调，与"未读点"同一档），
 * **关**是 `--meter-track`（`Fills-F3`，与进度条空槽同一档灰）。
 * 滑块分两色：打开时压在蓝底上用恒定白（`--Always-White`，两套主题同值），
 * 关闭时用 `--bg-surface`——**不靠 opacity**，与本仓"禁用/状态用实色"的口径一致。
 */
function Switch({
  className,
  size = 'default',
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & {
  size?: 'sm' | 'default'
}) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      data-size={size}
      className={cn(
        'peer group/switch inline-flex shrink-0 items-center rounded-pill border border-transparent transition-[color,background-color,box-shadow] disabled:cursor-not-allowed disabled:bg-[var(--button-disabled-bg)] data-[size=default]:h-[1.15rem] data-[size=default]:w-8 data-[size=sm]:h-3.5 data-[size=sm]:w-6 data-[state=checked]:bg-[var(--accent)] data-[state=unchecked]:bg-[var(--meter-track)]',
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          'pointer-events-none block rounded-pill transition-transform group-data-[size=default]/switch:size-4 group-data-[size=sm]/switch:size-3 data-[state=checked]:translate-x-[calc(100%-2px)] data-[state=checked]:bg-[var(--Always-White)] data-[state=unchecked]:translate-x-0 data-[state=unchecked]:bg-surface',
        )}
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
