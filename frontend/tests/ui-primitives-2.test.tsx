/**
 * 第二批 shadcn 原语的冒烟用例（`src/ui/README.md` §5 的 19 个之外的那 10 个）。
 *
 * 与 `tests/ui-primitives.test.tsx` 同一条口径：只测"装得起来、开得开、点得动"，
 * 外加**一条令牌守卫**（字号不许写具名的 `text-meta` / `text-micro`，
 * 见 README §1.1）——视觉取值由映射表兜底，业务行为由各域自己的用例覆盖。
 *
 * jsdom 缺的几件：指针捕获 / 滚动 / 浮层定位的兜底在 `tests/setup.ts` 里；
 * 这里只补一件**本文件特有**的：`Avatar` 的图片加载探测会 `new Image()`，
 * 而 jsdom 永远不会发 `load`——补一个同步"加载成功"的替身，才能测到"图片与兜底"两条路。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { fileURLToPath, URL as NodeURL } from 'node:url'
import { beforeAll, describe, expect, it, vi } from 'vitest'

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/ui/alert-dialog'
import { Avatar, AvatarFallback, AvatarImage } from '@/ui/avatar'
import { Button } from '@/ui/button'
import { Checkbox } from '@/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/collapsible'
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/ui/command'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from '@/ui/context-menu'
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
  DrawerTrigger,
} from '@/ui/drawer'
import { Progress } from '@/ui/progress'
import { RadioGroup, RadioGroupItem } from '@/ui/radio-group'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/ui/resizable'

beforeAll(() => {
  // `Avatar` 的 Image 用 `new window.Image()` 探路（`addEventListener('load')` +
  // `complete` / `naturalWidth` 三重判断），而 jsdom 不会发 load——
  // 这里给一个"赋 src 即加载成功"的替身，好把"图片"这条路径也测到。
  class ImmediateImage {
    complete = false
    naturalWidth = 0
    crossOrigin: string | null = null
    referrerPolicy = ''
    private source = ''
    private listeners = new Map<string, Set<(event: unknown) => void>>()
    get src() {
      return this.source
    }
    set src(next: string) {
      this.source = next
      this.complete = true
      this.naturalWidth = 32
      this.listeners.get('load')?.forEach((listener) => listener({ currentTarget: this }))
    }
    addEventListener(type: string, listener: (event: unknown) => void) {
      const set = this.listeners.get(type) ?? new Set()
      set.add(listener)
      this.listeners.set(type, set)
    }
    removeEventListener(type: string, listener: (event: unknown) => void) {
      this.listeners.get(type)?.delete(listener)
    }
  }
  vi.stubGlobal('Image', ImmediateImage)
})

/** 令牌守卫：src/ui 里任何一处都不许写具名的 text-meta / text-micro / text-body。 */
function expectNoNamedFontSizeClass(className: string) {
  expect(className).not.toMatch(/(?:^|\s)text-(?:meta|micro|body)(?:\s|$)/)
}

describe('ui 原语（第二批）', () => {
  it('AlertDialog：点确认走回调，弹窗关掉', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button>删除目录</Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除目录「合同」？</AlertDialogTitle>
            <AlertDialogDescription>关联 12 篇文档，删除后不可恢复。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={onConfirm}>删除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>,
    )

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '删除目录' }))
    const dialog = await screen.findByRole('alertdialog')
    // 表面与 dialog.tsx 同一口径：我们的弹层底 + 浮层阴影，不是 shadcn 的 bg-background
    expect(dialog).toHaveClass('bg-[var(--bg-overlay)]')
    expect(screen.getByText('删除目录「合同」？')).toHaveClass(
      'text-[length:var(--text-section-size)]',
    )

    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  })

  it('Checkbox：点一下从勾到不勾（受控回调 + data-state）', async () => {
    const user = userEvent.setup()
    const onCheckedChange = vi.fn()
    render(<Checkbox aria-label="记住这台机器" onCheckedChange={onCheckedChange} />)

    const box = screen.getByRole('checkbox', { name: '记住这台机器' })
    expect(box).toHaveAttribute('data-state', 'unchecked')
    expect(box.className).toContain('data-[state=checked]:bg-[var(--accent)]')
    expectNoNamedFontSizeClass(box.className)

    await user.click(box)
    expect(onCheckedChange).toHaveBeenCalledWith(true)
    expect(box).toHaveAttribute('data-state', 'checked')

    await user.click(box)
    expect(onCheckedChange).toHaveBeenLastCalledWith(false)
    expect(box).toHaveAttribute('data-state', 'unchecked')
  })

  it('RadioGroup：选中的那一项成为当前值', async () => {
    const user = userEvent.setup()
    const onValueChange = vi.fn()
    render(
      <RadioGroup defaultValue="k3" onValueChange={onValueChange}>
        <RadioGroupItem value="k3" aria-label="K3" />
        <RadioGroupItem value="k2" aria-label="K2" />
      </RadioGroup>,
    )

    const k3 = screen.getByRole('radio', { name: 'K3' })
    const k2 = screen.getByRole('radio', { name: 'K2' })
    expect(k3).toHaveAttribute('data-state', 'checked')
    expect(k2).toHaveAttribute('data-state', 'unchecked')

    await user.click(k2)
    expect(onValueChange).toHaveBeenCalledWith('k2')
    expect(k2).toHaveAttribute('data-state', 'checked')
    expect(k3).toHaveAttribute('data-state', 'unchecked')
  })

  it('ContextMenu：右键唤出菜单，且能点到项', async () => {
    const user = userEvent.setup()
    const onRename = vi.fn()
    render(
      <ContextMenu>
        <ContextMenuTrigger>会话行</ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuItem onSelect={onRename}>重命名</ContextMenuItem>
          <ContextMenuItem variant="destructive">删除</ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>,
    )

    expect(screen.queryByRole('menu')).not.toBeInTheDocument()

    fireEvent.contextMenu(screen.getByText('会话行'), { clientX: 24, clientY: 24 })
    const menu = await screen.findByRole('menu')
    // 菜单底是 --bg-menu（深色下比画布亮两档），不是 --bg-surface
    expect(menu).toHaveClass('bg-[var(--bg-menu)]')

    await user.click(screen.getByRole('menuitem', { name: '重命名' }))
    expect(onRename).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })

  it('Avatar：图片加载成功后出图、没图可加载时走兜底', async () => {
    render(
      <>
        <Avatar>
          <AvatarImage src="/avatar/xiaoyou.png" alt="小又" />
          <AvatarFallback>小</AvatarFallback>
        </Avatar>
        <Avatar size="sm">
          {/* 没有 src：Radix 立刻判 error，兜底接管 */}
          <AvatarImage alt="游客" />
          <AvatarFallback>游</AvatarFallback>
        </Avatar>
      </>,
    )

    // 加载成功的那一侧：<img> 出现，兜底让位
    const image = await screen.findByRole('img', { name: '小又' })
    expect(image).toHaveAttribute('data-slot', 'avatar-image')
    expect(screen.queryByText('小')).not.toBeInTheDocument()

    // 拿不到图的那一侧：兜底文字在，<img> 不在
    expect(await screen.findByText('游')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: '游客' })).not.toBeInTheDocument()

    // 默认尺寸取 --avatar-size（28px），不是上游写死的 32px
    const root = screen.getByText('游').closest('[data-slot="avatar"]')
    expect(root).toHaveClass('size-[var(--avatar-size)]')
  })

  it('Progress：值既写进 aria-valuenow，也写进指示条的位移', () => {
    render(<Progress value={40} aria-label="索引进度" />)

    const bar = screen.getByRole('progressbar', { name: '索引进度' })
    expect(bar).toHaveAttribute('aria-valuenow', '40')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(bar).toHaveClass('bg-[var(--meter-track)]')

    const indicator = bar.querySelector('[data-slot="progress-indicator"]')
    expect(indicator).toHaveClass('bg-[var(--accent)]')
    expect(indicator).toHaveStyle({ transform: 'translateX(-60%)' })
  })

  it('Collapsible：点触发器展开内容', async () => {
    const user = userEvent.setup()
    render(
      <Collapsible>
        <CollapsibleTrigger asChild>
          <Button>高级设置</Button>
        </CollapsibleTrigger>
        <CollapsibleContent>仅本机可见</CollapsibleContent>
      </Collapsible>,
    )

    expect(screen.queryByText('仅本机可见')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '高级设置' }))
    expect(await screen.findByText('仅本机可见')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '高级设置' }))
    await waitFor(() => expect(screen.queryByText('仅本机可见')).not.toBeInTheDocument())
  })

  it('Command：输入后只剩匹配的项', async () => {
    const user = userEvent.setup()
    render(
      <Command>
        <CommandInput placeholder="搜索命令" />
        <CommandList>
          <CommandEmpty>没有匹配的命令</CommandEmpty>
          <CommandGroup heading="操作">
            <CommandItem>重命名</CommandItem>
            <CommandItem>删除</CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>,
    )

    const item = (text: string) => screen.getByText(text).closest('[cmdk-item]')
    expect(screen.getByText('重命名')).toBeInTheDocument()

    await user.type(screen.getByPlaceholderText('搜索命令'), '删除')

    // 被过滤掉的项 cmdk 直接不渲染（不是加 hidden 藏起来）
    await waitFor(() => expect(screen.queryByText('重命名')).not.toBeInTheDocument())
    expect(item('删除')).toBeInTheDocument()
    expect(item('删除')).toHaveAttribute('data-selected', 'true')

    await user.clear(screen.getByPlaceholderText('搜索命令'))
    await user.type(screen.getByPlaceholderText('搜索命令'), '不存在的命令')
    expect(await screen.findByText('没有匹配的命令')).toBeInTheDocument()
  })

  it('CommandDialog：接的是我们 dialog.tsx 的壳（弹窗能开也能关）', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    render(
      <CommandDialog open onOpenChange={onOpenChange}>
        <CommandInput placeholder="搜索命令" />
        <CommandList>
          <CommandItem>新建笔记</CommandItem>
        </CommandList>
      </CommandDialog>,
    )

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveClass('bg-[var(--bg-overlay)]')
    // 标题是 sr-only 的（只给读屏用），面板自己不能变成第二个弹窗壳
    expect(screen.getByText('命令面板')).toBeInTheDocument()
    expect(screen.getByText('新建笔记')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '关闭' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('Drawer：能打开，也能用 DrawerClose 关掉', async () => {
    const user = userEvent.setup()
    render(
      <Drawer>
        <DrawerTrigger asChild>
          <Button>打开设置</Button>
        </DrawerTrigger>
        <DrawerContent>
          <DrawerHeader>
            <DrawerTitle>外观</DrawerTitle>
            <DrawerDescription>只影响这台机器</DrawerDescription>
          </DrawerHeader>
          <DrawerFooter>
            <DrawerClose asChild>
              <Button>收起</Button>
            </DrawerClose>
          </DrawerFooter>
        </DrawerContent>
      </Drawer>,
    )

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '打开设置' }))
    const drawer = await screen.findByRole('dialog')
    // 表面与 dialog / sheet 同一口径；方向属性由 vaul 给（默认 bottom）
    expect(drawer).toHaveClass('bg-[var(--bg-overlay)]')
    expect(drawer).toHaveAttribute('data-vaul-drawer-direction', 'bottom')

    await user.click(screen.getByRole('button', { name: '收起' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('Resizable：两组面板 + 一条无障碍分隔条', () => {
    render(
      <ResizablePanelGroup orientation="horizontal">
        <ResizablePanel>左栏</ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel>右栏</ResizablePanel>
      </ResizablePanelGroup>,
    )

    expect(document.querySelectorAll('[data-slot="resizable-panel"]')).toHaveLength(2)
    expect(screen.getByText('左栏')).toBeInTheDocument()
    expect(screen.getByText('右栏')).toBeInTheDocument()

    // 无障碍是库给的，不能因为我们换类名而丢：分隔条必须是可聚焦的 separator
    const handle = screen.getByRole('separator')
    expect(handle).toHaveAttribute('data-slot', 'resizable-handle')
    expect(handle).toHaveAttribute('tabindex', '0')
    expect(handle.className).toContain('after:absolute')
  })
})

/* ---------------------------------------------------------------- 基础层护栏
 *
 * 这一批（第三批 A）动的是 `tokens.css` 与 `misc.css` 两个全局文件，两件事都能被
 * **源码级**钉住，而且都踩过坑、值得护栏：
 *
 * 1. **分层**。未分层的规则永远压过 `@layer utilities`（与优先级无关），而这批把
 *    `misc.css` 的 364 条 `.m-*` 收进了 `@layer components`；同时 `tokens.css` 与
 *    `misc.css` 都必须先声明同一句**层级顺序**——层的顺序由"名字第一次出现的位置"决定，
 *    而两者谁先被浏览器看到并不固定（开发环境里 `misc.css` 就先于 `tokens.css`）。
 *    少了任何一半，`components` 会落到 `base` 底下，preflight 的 `*{margin:0;padding:0}`
 *    会把整份 `.m-*` 吃掉（实测：页标题退回 15px、卡片内边距归零）。
 *    jsdom 不应用样式表，这类契约只能在源码上量——与 `notes*.test.tsx` 读 `notes.css`
 *    是同一条路。
 *
 * 2. **搜索框的宽度口径**。`.m-toolbar-search` 在 row 工具栏与块级弹窗（技能市场）复用，
 *    宽度只能写成 `flex-basis`：多一个 `width`，块级那处就会被缩成 240px。
 *    记忆页侧栏那一处（column 容器，另有一条容器规则）随页面下线一起删了（§6.3），
 *    所以这里只剩 base 那一半——顺带钉住 `.m-side-col` 真的不在了。
 */
function styleSource(relative: string): string {
  return readFileSync(fileURLToPath(new NodeURL(relative, import.meta.url)), 'utf8')
}

/** 去掉块注释：中文注释里有括号与 `@layer` 字样，不该参与判断。 */
function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '')
}

/** 某个选择器的声明块正文（源码里第一条同名规则）。 */
function ruleBody(source: string, selector: string): string {
  const found = [...withoutComments(source).matchAll(/([^{}]+)\{([^{}]*)\}/g)].find(
    (rule) => (rule[1] ?? '').trim() === selector,
  )
  expect(found, `没在样式源码里找到 ${selector}`).toBeTruthy()
  return (found?.[2] ?? '').replace(/\s+/g, ' ')
}

describe('基础层：分层与搜索框结构护栏', () => {
  const ORDER = '@layer theme, base, components, utilities;'

  it('misc.css 与 tokens.css 都先声明同一句层级顺序', () => {
    for (const file of ['../src/features/misc/shared/misc.css', '../src/styles/tokens.css']) {
      const head = withoutComments(styleSource(file)).replace(/\s+/g, ' ').trim()
      const at = head.indexOf(ORDER)
      expect(at, `${file} 缺少层级顺序声明`).toBeGreaterThanOrEqual(0)
      // 而且必须在**第一个开层块之前**：晚于它就没有排序作用了
      // （`[^{};]` 里的分号是必要的：少了它，这句顺序声明自己会被当成开层块）
      const firstBlock = head.search(/@layer [^{};]*\{/)
      expect(firstBlock, `${file} 里一句开层块都没找到`).toBeGreaterThanOrEqual(0)
      expect(at, `${file} 的顺序声明必须排在第一个 @layer 块之前`).toBeLessThan(firstBlock)
    }
  })

  it('misc.css 的规则全部在 @layer components 里（层外只剩 @keyframes）', () => {
    const source = withoutComments(styleSource('../src/features/misc/shared/misc.css'))
    // 扫一遍大括号：每一次"深度 0 → 1"都是一条顶层构造，它只能是这两者之一
    const topLevel: string[] = []
    let depth = 0
    for (let i = 0; i < source.length; i += 1) {
      const ch = source[i]
      if (ch === '{') {
        if (depth === 0) {
          const before = source.slice(Math.max(0, i - 40), i)
          topLevel.push(before.split(/[;}]/).pop()?.trim().replace(/\s+/g, ' ') ?? '')
        }
        depth += 1
      } else if (ch === '}') {
        depth -= 1
      }
    }
    expect(depth).toBe(0)
    expect(topLevel).toHaveLength(2)
    expect(topLevel[0]).toContain('@keyframes m-pulse')
    expect(topLevel[1]).toBe('@layer components')
  })

  it('.m-toolbar-search：宽度只在 flex-basis 上，不加 width', () => {
    const source = styleSource('../src/features/misc/shared/misc.css')
    const base = ruleBody(source, '.m-toolbar-search')
    expect(base).toContain('flex: 0 1 240px')
    // 多一个 `width` 就会把块级那处（技能市场弹窗的搜索框）缩成 240px
    expect(base).not.toMatch(/(?:^|[;\s])width:/)
    // 记忆页侧栏那一处随页面下线（§6.3）一起删掉了，`.m-side-col` 的容器规则
    // 不再有使用者；这一条只保 row 工具栏与块级弹窗两处的口径。
    expect(withoutComments(source)).not.toContain('.m-side-col')
  })

  it('两套主题的三级灰/四级灰都指向达标档，而不是 Kimi 原值', () => {
    for (const [file, tert, quat] of [
      ['../src/styles/themes/light.css', '#0000008c', '#00000070'],
      ['../src/styles/themes/dark.css', '#ffffff85', '#ffffff5c'],
    ]) {
      const source = withoutComments(styleSource(file))
      expect(source, `${file} 三级灰没走达标档`).toContain(`--Labels-Tertiary-text: ${tert}`)
      expect(source, `${file} 四级灰没走达标档`).toContain(`--Labels-Quaternary-text: ${quat}`)
      expect(source).toContain('--text-tertiary: var(--Labels-Tertiary-text)')
      expect(source).toContain('--text-quaternary: var(--Labels-Quaternary-text)')
    }
  })
})
