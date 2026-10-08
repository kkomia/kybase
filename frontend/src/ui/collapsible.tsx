// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import * as CollapsiblePrimitive from '@radix-ui/react-collapsible'

/**
 * 可折叠区块。上游这一版**一个类名都没有**（纯透传），所以这里也没有 `cn` 与令牌要用：
 * 高度、动画、缩进都由调用方给，`data-slot` 照旧保留。
 *
 * 折叠动画（上游文档里那套 `CollapsibleContent` + 关键帧）需要调用方自己加；
 * 本仓的动效只有 `--motion-fast` / `--motion-slow` 两档（tokens.css），
 * 要做"展开时滑下来"就用 `transition-[height]` 配这两档，别现拍时长。
 */
function Collapsible({ ...props }: React.ComponentProps<typeof CollapsiblePrimitive.Root>) {
  return <CollapsiblePrimitive.Root data-slot="collapsible" {...props} />
}

function CollapsibleTrigger({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleTrigger>) {
  return <CollapsiblePrimitive.CollapsibleTrigger data-slot="collapsible-trigger" {...props} />
}

function CollapsibleContent({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleContent>) {
  return <CollapsiblePrimitive.CollapsibleContent data-slot="collapsible-content" {...props} />
}

export { Collapsible, CollapsibleTrigger, CollapsibleContent }
