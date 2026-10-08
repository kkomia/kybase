// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import * as TabsPrimitive from '@radix-ui/react-tabs'

import { cn } from '@/lib/utils'

/**
 * 标签页。`default` 变体是"灰底胶囊槽 + 白色当前项"（槽用 `--bg-subtle`、
 * 当前项用 `--bg-surface`，靠**底色差**表示选中，不给阴影）；
 * `line` 变体是"下划线指示条"，指示条颜色用 `--text-primary`（墨色，不是品牌蓝）。
 */
function Tabs({
  className,
  orientation = 'horizontal',
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      orientation={orientation}
      className={cn('group/tabs flex gap-2 data-[orientation=horizontal]:flex-col', className)}
      {...props}
    />
  )
}

/**
 * 轨道高 **32px = `--control-height`**（与按钮、输入框同一档）。
 *
 * 写法上有一个坑必须避开：`group-data-[orientation=horizontal]/tabs:h-8` 这种
 * **带变体的工具类优先级高于调用点上的 `h-9`**（变体选择器是两级类名，`h-9` 是一级），
 * 于是调用点写 `h-9` 会被静默吃掉——曾经真的这样埋过一处（已删的 `SegmentedControl`
 * 上写着 `h-9 p-0.5`，实测轨道一直是 32px，谁也看不出那句没生效）。
 * 带变体的高度只留**竖排那一档**（`h-fit`，它必须靠变体才生效）；横排这个是普通
 * `h-8`，`cn()` 的 tailwind-merge 就能让调用点的 `h-*` 正常覆盖它。
 */
const tabsListVariants = cva(
  'group/tabs-list inline-flex w-fit items-center justify-center rounded-row p-[3px] h-8 group-data-[orientation=vertical]/tabs:h-fit group-data-[orientation=vertical]/tabs:flex-col data-[variant=line]:rounded-none',
  {
    variants: {
      variant: {
        default: 'bg-[var(--bg-subtle)]',
        line: 'gap-1 bg-transparent',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

function TabsList({
  className,
  variant = 'default',
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> & VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        'relative inline-flex h-[calc(100%-1px)] flex-1 items-center justify-center gap-1.5 rounded-control border border-transparent px-3 py-1 text-[length:var(--text-meta-size)] font-medium whitespace-nowrap text-text-secondary transition-[color,background-color,border-color,box-shadow] hover:text-text-primary group-data-[orientation=vertical]/tabs:w-full group-data-[orientation=vertical]/tabs:justify-start disabled:pointer-events-none disabled:bg-[var(--button-disabled-bg)] disabled:text-[var(--button-disabled-text)] [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*="size-"])]:size-4',
        'group-data-[variant=line]/tabs-list:bg-transparent group-data-[variant=line]/tabs-list:data-[state=active]:bg-transparent',
        'data-[state=active]:bg-surface data-[state=active]:text-text-primary',
        'after:absolute after:bg-[var(--text-primary)] after:opacity-0 after:transition-opacity group-data-[orientation=horizontal]/tabs:after:inset-x-0 group-data-[orientation=horizontal]/tabs:after:bottom-[-5px] group-data-[orientation=horizontal]/tabs:after:h-0.5 group-data-[orientation=vertical]/tabs:after:inset-y-0 group-data-[orientation=vertical]/tabs:after:-right-1 group-data-[orientation=vertical]/tabs:after:w-0.5 group-data-[variant=line]/tabs-list:data-[state=active]:after:opacity-100',
        className,
      )}
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn('flex-1', className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }
