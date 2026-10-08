/**
 * 404 页：地址拼错时给一个明确出口，而不是空白。
 * ——与旧前端 `views/NotFoundView.vue` 对应。
 *
 * 三处照界面评审改的（E1/E2）：
 *
 * 1. **标题只有一句**：原来是页标题「页面不存在」+ 空态标题「没有找到这个地址」，
 *    两行说同一件事、中间还空着 60px。现在只留页标题那一句（`<h1>` 仍在，
 *    页面标题只此一处——原来那句空态标题是 `<p>`，标题层级也是错的）；
 * 2. **两个出口**：「回到概览」之外补「返回上一页」。走到 404 多半是"从上一页点错了
 *    一个链接"，能原路退回去比重新找路便宜；
 * 3. **内容块垂直居中**：原来贴在左上角（右侧与下方整片空着），看起来像渲染失败，
 *    而不像一个做过的页面。
 *
 * 两枚按钮都是 `@/ui/button`：主操作是默认实心档（与全站主操作同一形态），
 * 「返回上一页」是描边档——形状一致，只分主次。
 */
import { Link, useNavigate } from 'react-router'

import { Button } from '@/ui/button'

export function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-6 px-[var(--page-gutter)] py-[var(--space-12)] text-center">
      <div className="flex flex-col items-center gap-3">
        <h1 className="m-page-title">页面不存在</h1>
        <p className="m-empty-hint">链接可能已失效，或者知识库/文档已经被删除。</p>
      </div>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {/* 主按钮样式的链接：`Button asChild` 把类名给到 `<a>` 本身，
            而不是在 `<a>` 里再套一个按钮（那样是无障碍上的嵌套交互元素） */}
        <Button asChild>
          <Link to="/">回到概览</Link>
        </Button>
        <Button type="button" variant="outline" onClick={() => void navigate(-1)}>
          返回上一页
        </Button>
      </div>
    </div>
  )
}
