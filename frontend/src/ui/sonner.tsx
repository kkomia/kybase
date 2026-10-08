// 来源：shadcn/ui（new-york）@98a1fe6 —— 类名已换成 src/styles/tokens.css 的令牌（映射表见 src/ui/README.md）
import * as React from 'react'
import {
  CircleCheckIcon,
  InfoIcon,
  Loader2Icon,
  OctagonXIcon,
  TriangleAlertIcon,
} from 'lucide-react'
import { Toaster as Sonner, type ToasterProps } from 'sonner'

import { cn } from '@/lib/utils'

/**
 * 全局通知条（sonner 的 `Toaster`）。与上游的三处差别：
 * 1. **不用 `next-themes`**：本仓的主题是 `<html data-theme="light|dark">`
 *    （见 index.html 的首屏脚本），所以这里直接盯这个属性，不引第二个主题库。
 *    默认 `light` 时同步读一次，之后用 `MutationObserver` 跟着切。
 * 2. 位置改 `top-center`：旧前端的通知条就在顶部居中（`--space-3` 处）。
 * 3. 图标换成**语义色**的 lucide（旧前端就是"语义色图标 + 文字"，
 *    sonner 默认是单色，光靠底色读不出是成功还是失败）。
 *
 * 挂载点属于应用壳：`src/app/**` 里放一个 `<Toaster />` 即可，通知就在所有页面之上。
 */
export type ThemeName = 'light' | 'dark' | 'system'

/** 读当前主题：`data-theme` 缺失时按 light（与 index.html 的默认一致）。 */
function readTheme(): ThemeName {
  const value = document.documentElement.dataset['theme']
  return value === 'dark' ? 'dark' : 'light'
}

function useDocumentTheme(): ThemeName {
  const [theme, setTheme] = React.useState<ThemeName>(readTheme)

  React.useEffect(() => {
    const root = document.documentElement
    const sync = () => setTheme(readTheme())
    sync()
    const observer = new MutationObserver(sync)
    observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  return theme
}

const Toaster = ({ className, ...props }: ToasterProps) => {
  const theme = useDocumentTheme()

  return (
    <Sonner
      theme={theme}
      position="top-center"
      // 关闭按钮：旧 `ToastStack.vue` 每条通知都带一个「关闭通知」（可点、可键盘到），
      // sonner 默认不给——不开的话用户只能等它自己消失（默认 4 秒），
      // 长一点的提示（"上传失败：…"）还没读完就没了。
      closeButton
      className={cn('toaster group', className)}
      icons={{
        success: <CircleCheckIcon className="size-4 text-status-success" />,
        info: <InfoIcon className="size-4 text-text-secondary" />,
        warning: <TriangleAlertIcon className="size-4 text-status-warning" />,
        error: <OctagonXIcon className="size-4 text-status-danger" />,
        loading: <Loader2Icon className="size-4 animate-spin text-text-secondary" />,
      }}
      style={
        {
          '--normal-bg': 'var(--bg-overlay)',
          '--normal-text': 'var(--text-primary)',
          '--normal-border': 'var(--border)',
          '--border-radius': 'var(--radius-control)',
        } as React.CSSProperties
      }
      {...props}
    />
  )
}

export { Toaster }
