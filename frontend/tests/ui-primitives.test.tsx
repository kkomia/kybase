/**
 * shadcn 原语的冒烟用例：五个组件各渲染一次，断言**能打开、能点到项**。
 *
 * 只测"装得起来、开得开、点得动"这一层——视觉由 `src/ui/README.md` 的映射表兜底，
 * 业务行为由各域自己的用例覆盖。
 *
 * jsdom 缺的几件在这里就地补（不动 `tests/setup.ts`，那是主控的文件）：
 * Radix 的菜单/选择在指针交互里会调 `scrollIntoView` 与指针捕获 API。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeAll, describe, expect, it, vi } from 'vitest'

import { Badge } from '@/ui/badge'
import { Button } from '@/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/ui/dropdown-menu'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/ui/tooltip'

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

describe('ui 原语', () => {
  it('Button：渲染出来、点得动', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<Button onClick={onClick}>新建对话</Button>)

    const button = screen.getByRole('button', { name: '新建对话' })
    expect(button).toHaveAttribute('data-slot', 'button')

    // 令牌守卫（两条都是踩过的坑，别删）：
    // 1. 字号必须走带 length 提示的任意值写法——具名的 text-meta / text-micro
    //    与 tokens.css 里的遗留辅助类同名，而那个类是无 @layer 的，会连文字色一起改掉；
    // 2. 主按钮是"墨色实心"的我们的令牌，不是 shadcn 默认那套 oklch 主题。
    expect(button.className).toContain('var(--text-meta-size)')
    expect(button.className).not.toMatch(/(?:^|\s)text-(?:meta|micro|body)(?:\s|$)/)
    expect(button.className).toContain('var(--button-primary-bg)')

    await user.click(button)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('Dialog：能打开，也能用关闭按钮关掉', async () => {
    const user = userEvent.setup()
    render(
      <Dialog>
        <DialogTrigger asChild>
          <Button>删除目录</Button>
        </DialogTrigger>
        <DialogContent>
          <DialogTitle>删除目录「合同」？</DialogTitle>
          <DialogDescription>关联 12 篇文档，删除后不可恢复。</DialogDescription>
        </DialogContent>
      </Dialog>,
    )

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '删除目录' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    // 弹层表面用我们的 `--bg-overlay`（Kimi 那个带蓝调的弹层底），不是 shadcn 的 `bg-background`
    expect(dialog).toHaveClass('bg-[var(--bg-overlay)]')
    expect(screen.getByText('删除目录「合同」？')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '关闭' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('DropdownMenu：能打开，也能点到项', async () => {
    const user = userEvent.setup()
    const onRename = vi.fn()
    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button aria-label="更多操作">…</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem onSelect={onRename}>重命名</DropdownMenuItem>
          <DropdownMenuItem variant="destructive">删除</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    )

    expect(screen.queryByRole('menu')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '更多操作' }))
    expect(await screen.findByRole('menu')).toBeInTheDocument()

    await user.click(screen.getByRole('menuitem', { name: '重命名' }))
    expect(onRename).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })

  it('Select：能打开，点到的项成为当前值', async () => {
    const user = userEvent.setup()
    render(
      <Select>
        <SelectTrigger aria-label="模型">
          <SelectValue placeholder="选择模型" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="k3">K3</SelectItem>
          <SelectItem value="k2">K2</SelectItem>
        </SelectContent>
      </Select>,
    )

    const trigger = screen.getByRole('combobox', { name: '模型' })
    expect(trigger).toHaveTextContent('选择模型')

    await user.click(trigger)
    await user.click(await screen.findByRole('option', { name: 'K3' }))

    await waitFor(() => expect(trigger).toHaveTextContent('K3'))
  })

  it('Badge：默认是 4px 方角，`shape="pill"` 那一档才是胶囊（状态标签用）', () => {
    // 两档形状都有明确出处，别合并成一档（对照记录 §3 第 4 条）：
    // - 方角 = tokens.css 对 `--radius-badge` 的注解（Kimi 的 badge 一律 4px 方角），
    //   卡片里那些 `chip`（技能来源 / 传输方式 / 版本号）用它；
    // - 胶囊 = 旧 `StatusTag.vue` 的 `.status`（`height: 22px` + `--radius-pill`），
    //   状态标签用它。
    const { unmount } = render(<Badge>内置</Badge>)
    const square = screen.getByText('内置')
    expect(square).toHaveAttribute('data-shape', 'square')
    expect(square.className).toContain('var(--radius-badge)')
    expect(square.className).not.toContain('var(--radius-pill)')
    unmount()

    render(<Badge shape="pill">已索引</Badge>)
    const pill = screen.getByText('已索引')
    expect(pill).toHaveAttribute('data-shape', 'pill')
    expect(pill.className).toContain('var(--radius-pill)')
    // 高度也照旧 `.status`：22px（不是 Badge 默认的 py-0.5 撑出来的那个高度）
    expect(pill.className).toContain('h-[22px]')
  })

  it('Tooltip：悬停触发器后气泡出现', async () => {
    const user = userEvent.setup()
    render(
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button aria-label="说明">?</Button>
          </TooltipTrigger>
          <TooltipContent>把这一段存进知识库</TooltipContent>
        </Tooltip>
      </TooltipProvider>,
    )

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    await user.hover(screen.getByRole('button', { name: '说明' }))
    expect(await screen.findByRole('tooltip')).toHaveTextContent('把这一段存进知识库')
  })
})
