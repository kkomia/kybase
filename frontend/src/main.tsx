/**
 * 新前端（React）的入口。
 *
 * 与旧前端一样：**首屏之前不请求任何东西**（主题与字号在 `index.html` 里已经定好），
 * 挂载后由 `App` 里的 Provider 决定要拉什么（会话恢复、名册、注册表……）。
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from '@/app/App'
import '@/styles/tokens.css'
// 公式与代码高亮的样式：由**应用层**引（渲染层只管产出 class 名，见 chat/model/README.md）
import 'katex/dist/katex.min.css'
import 'highlight.js/styles/github.css'
import '@/styles/themes/light.css'
import '@/styles/themes/dark.css'
import './index.css'

const root = document.getElementById('root')
if (!root) throw new Error('缺少 #root 挂载点（index.html 被改过？）')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
