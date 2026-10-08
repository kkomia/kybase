// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import { GripVerticalIcon } from 'lucide-react'
import * as ResizablePrimitive from 'react-resizable-panels'

import { cn } from '@/lib/utils'

/**
 * 可拖拽分栏（`react-resizable-panels` v4）。笔记两栏、预览左右对照会用到。
 *
 * **无障碍原样保留**：分隔条由库自己渲染成 `role="separator"` + `tabIndex=0` +
 * `aria-valuenow/valuemin/valuemax`（方向键能拖动宽度），`Group` 用 `orientation`
 * 设置 `flex-direction`。所以这里的 className **只加视觉，不加语义**——
 * 上游那条 `focus-visible:ring-1 ring-ring ring-offset-1` 删掉了：
 * 本仓的焦点环只有 tokens.css 里那一条全局 `:focus-visible`（2px 墨色 + 2px offset），
 * 而且它是无 `@layer` 的，写在组件里也压不过它。
 *
 * 取值：分隔条 `bg-border`（`--border` = `Separators-S1`）、把手里的小方块
 * `rounded-[var(--radius-badge)]`（4px，与徽章同一档 4px 方角）。
 * 分隔条的"命中区"由库给（`after:` 那条 4px 的透明扩展），不用我们自己加 padding。
 */
function ResizablePanelGroup({ className, ...props }: ResizablePrimitive.GroupProps) {
  return (
    <ResizablePrimitive.Group
      data-slot="resizable-panel-group"
      className={cn('flex h-full w-full aria-[orientation=vertical]:flex-col', className)}
      {...props}
    />
  )
}

function ResizablePanel({ ...props }: ResizablePrimitive.PanelProps) {
  return <ResizablePrimitive.Panel data-slot="resizable-panel" {...props} />
}

function ResizableHandle({
  withHandle,
  className,
  ...props
}: ResizablePrimitive.SeparatorProps & {
  withHandle?: boolean
}) {
  return (
    <ResizablePrimitive.Separator
      data-slot="resizable-handle"
      className={cn(
        'relative flex w-px items-center justify-center bg-border after:absolute after:inset-y-0 after:left-1/2 after:w-1 after:-translate-x-1/2 aria-[orientation=horizontal]:h-px aria-[orientation=horizontal]:w-full aria-[orientation=horizontal]:after:left-0 aria-[orientation=horizontal]:after:h-1 aria-[orientation=horizontal]:after:w-full aria-[orientation=horizontal]:after:translate-x-0 aria-[orientation=horizontal]:after:-translate-y-1/2 [&[aria-orientation=horizontal]>div]:rotate-90',
        className,
      )}
      {...props}
    >
      {withHandle && (
        <div className="z-10 flex h-4 w-3 items-center justify-center rounded-[var(--radius-badge)] border border-border bg-border">
          <GripVerticalIcon className="size-2.5" />
        </div>
      )}
    </ResizablePrimitive.Separator>
  )
}

export { ResizableHandle, ResizablePanel, ResizablePanelGroup }
