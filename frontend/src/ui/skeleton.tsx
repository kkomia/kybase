// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * 骨架屏的**一块**。底色换成 `--bg-hover`（旧前端 `SkeletonBlock` 就是它），
 * 圆角走 `--radius-control`。组合用法（列表行/卡片）在旧前端是三种预设，
 * 这里只留原语：预设属于页面自己的排版，不该塞进原语里。
 */
function Skeleton({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="skeleton"
      className={cn('animate-pulse rounded-control bg-[var(--bg-hover)]', className)}
      {...props}
    />
  )
}

export { Skeleton }
