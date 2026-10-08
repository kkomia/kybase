# `src/ui/**` —— shadcn/ui 原语（已换成我们的设计令牌）

## 0. 来源与版本

| 项       | 值                                                                                                                                                                                 |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 上游     | [`shadcn-ui/ui`](https://github.com/shadcn-ui/ui) 的 **new-york** 风格                                                                                                             |
| commit   | **`98a1fe6`**（`98a1fe67b439324ddc857f47fbdce056600a4329`，2026-09-21）                                                                                                            |
| 源码路径 | `apps/v4/registry/new-york-v4/ui/*.tsx`                                                                                                                                            |
| 拉取方式 | 本机 **`git clone` 连不上 github.com**（443 超时），改用 `raw.githubusercontent.com` 按 commit 取文件（`.cache/upstream/fetch/ny-v4/`）；`api.github.com` 用于定 commit 号与目录树 |
| 许可     | MIT（上游仓库根 `LICENSE`），我们只改类名与 import                                                                                                                                 |

每个文件第一行都写着 `来源：shadcn/ui（new-york）@98a1fe6`。要升级时**照这个流程重新拉一份、重新映射**，
不要凭记忆改：上游这一版把目录从 `apps/www/registry/new-york/ui/` 挪到了 `apps/v4/registry/new-york-v4/ui/`，
import 也从 `@radix-ui/react-*` 换成了统一的 `radix-ui` 包——凭记忆写必然对不上。

## 1. 这一层的不变量

1. **取值只有一处**：所有颜色/圆角/字号/间距都来自 `src/styles/tokens.css` 与
   `src/styles/themes/{light,dark}.css`（经 `src/index.css` 的 `@theme inline` 变成类名）。
   **这里不新增任何一套颜色**，也没有一个硬编码色值（`text-white`、`bg-black/50` 这类全部换掉了）。
2. **`dark:` 变体一律删除**。上游靠 `dark:` 做深色适配，而 `dark:` 在 Tailwind v4 里默认是
   `prefers-color-scheme: dark`——我们的主题是 `<html data-theme="dark">` 驱动的。
   两套机制并存会出现"系统是深色、用户选了浅色"时样式错乱。令牌自己会随主题变，**不需要 `dark:`**。
3. **焦点环不写类名**。上游那套 `focus-visible:ring-[3px] ring-ring/50 ring-offset-background`
   全部删掉：`tokens.css` 里那条全局 `:focus-visible`（2px 墨色 + 2px offset）是**唯一**的焦点环，
   而且它是无 `@layer` 的普通 CSS，优先级高于 Tailwind 的 utilities——
   写在组件里也压不过它，留着只会是死代码。
   （因此上游每个组件里的 `outline-none` / `outline-hidden` 也一并删了：
   它们会**关掉**那条全局焦点环，方向正好相反。）
4. **禁用态用实色，不用 `opacity`**。上游的 `disabled:opacity-50` 换成
   `--button-disabled-bg/-text/-border` 三个令牌：半透明会把文字与底色一起推向对方，
   而"禁用态写着什么"恰恰最该读得清（旧前端的同一条取舍）。
5. `cn()` 只有一份：`src/lib/utils.ts`（`clsx` + `tailwind-merge`），
   并且**扩了一处 font-size 组**，原因见该文件注释——不扩的话
   `cn('text-meta text-text-secondary')` 会把 `text-meta` 静默吃掉。
6. import 路径：`@/lib/utils`（上游是 `cn`）、`@/ui/button`（上游是相对 registry 路径）。
7. `data-slot="..."` 属性全部保留（上游用它做组合选择器，也是我们测试/排查的抓手）。
8. **文件顶部没有 `"use client"`**：那是 Next.js 的 RSC 指令，在 Vite 里会被当作
   未知指令报警告，Vite 环境用不到，已删。
9. **字号一律写带 `length:` 提示的任意值形式**（`text-[` + `length:` + `var(--text-meta-size)`
   这样拼，三档对应 `--text-{meta,micro,body}-size`），不写具名的 `text-meta` /
   `text-micro` / `text-body`。原因是踩过的坑，见 §1.1。

### 1.1 两个必须知道的坑（都实测过）

**(1) `text-meta` / `text-micro` 这两个具名类不能用。**

`tokens.css` 里有两条遗留的**辅助类**（旧 Vue 前端直接写在模板上的那种）：

```css
.text-meta {
  font-size: var(--text-meta-size);
  color: var(--text-tertiary);
}
.text-micro {
  font-size: var(--text-micro-size);
  color: var(--text-tertiary);
}
```

它们**没有 `@layer`**，而 Tailwind 的 utilities 在 `@layer utilities` 里。按 CSS 的层叠规则，
**无层的规则优先于带层的规则**——所以只要元素上写了 `class="text-meta"`，
它的 `color: var(--text-tertiary)` 就会**压过任何 Tailwind 颜色类**
（构建产物里能同时搜到两条 `.text-meta` 规则：一条在层外，一条在 `@layer utilities` 里）。

后果很隐蔽：主按钮的白字、徽章的蓝字、危险项的红色会**全部变成三级灰**。
所以组件里一律写 `text-[length:var(--text-meta-size)]`——`length:` 提示既让它只生成
`font-size`，也让 tailwind-merge 按字号归类（见 `src/lib/utils.ts`）。
`tests/ui-primitives.test.tsx` 里有一条守卫断言盯着这件事。

**(2) `tailwind-merge` 会猜错我们的字号类名。** 见 `src/lib/utils.ts` 的长注释：
没把三个名字注册进 font-size 组之前，`cn('text-meta text-text-secondary')`
会**把 `text-meta` 静默丢掉**。

## 2. 令牌映射表

### 2.1 颜色

| shadcn 的类                                                         | 我们的类                                                              | 令牌 / 依据                                                                                    |
| ------------------------------------------------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `bg-background`                                                     | `bg-canvas`                                                           | `--bg-canvas`（`Bg-GroundPC`，页面底）                                                         |
| `bg-card` / `text-card-foreground`                                  | `bg-surface` / `text-text-primary`                                    | `--bg-surface`（`Bg-Primary`，抬起来的那层）                                                   |
| `bg-popover` / `text-popover-foreground`（**菜单/下拉/选择/浮层**） | `bg-[var(--bg-menu)]` / `text-text-primary`                           | `--bg-menu` = `Bg-Tertiary`：**深色下比画布亮两档**，不能复用 `--bg-surface`（那个比画布还暗） |
| `bg-popover`（**弹窗**）                                            | `bg-[var(--bg-overlay)]`                                              | `--bg-overlay` = `modal-bg`（带蓝调的弹层底）                                                  |
| `bg-primary` / `text-primary-foreground`                            | `bg-[var(--button-primary-bg)]` / `text-[var(--button-primary-text)]` | **墨色实心**，不是品牌蓝                                                                       |
| `bg-secondary` / `text-secondary-foreground`                        | `bg-[var(--bg-subtle)]` / `text-text-primary`                         | `--bg-subtle` = `Bg-Secondary`                                                                 |
| `bg-muted`、`bg-muted/50`（行悬停）                                 | `bg-[var(--bg-subtle)]`、`hover:bg-[var(--bg-hover)]`                 | `--bg-hover` = `Fills-F1`（alpha 填充，叠在任何底上都对）                                      |
| `data-[state=selected]:bg-muted`                                    | `bg-[var(--bg-selected)]`                                             | `--bg-selected` = `Fills-F2`                                                                   |
| `text-muted-foreground`                                             | `text-text-tertiary`（说明句用 `text-text-secondary`）                | 三级灰只有 3.35:1，tokens.css 对它有用途约束                                                   |
| `placeholder:text-muted-foreground`                                 | `placeholder:text-text-quaternary`                                    | 四级灰给占位符（旧前端 `.field::placeholder` 同款）                                            |
| `bg-accent` / `text-accent-foreground`（悬停、键盘高亮）            | `bg-[var(--bg-hover)]` / `text-text-primary`                          | 不用品牌蓝底                                                                                   |
| `border-input`、`bg-input`                                          | `border-border`、`bg-[var(--meter-track)]`（开关空槽）                | `--border` = `Separators-S1`                                                                   |
| `border-border`、`bg-border`                                        | `border-border`、`bg-border`                                          | `--color-border` 已注册，直接用                                                                |
| `bg-border`（滚动条滑块）                                           | `bg-[var(--border-strong)]`                                           | 滑块比描边深一档                                                                               |
| `bg-black/50`（遮罩）                                               | `bg-[var(--overlay-scrim)]`                                           | `--overlay-scrim` = `MaskBg-Base`                                                              |
| `text-destructive` / `bg-destructive`                               | `text-status-danger` / `bg-[var(--status-danger-soft)]`               | 语义色只有 `--status-*` 与 `-soft` 两档                                                        |
| `ring-destructive`（错误框）                                        | `aria-invalid:border-status-danger`                                   | 不另开红环                                                                                     |
| `selection:bg-primary`                                              | `selection:bg-[var(--accent-selected)]`                               | 选中文字底（= `Others-TextSelected`）                                                          |
| `text-foreground`                                                   | `text-text-primary`                                                   |                                                                                                |
| `bg-foreground`（徽章底色 / 开关滑块）                              | `bg-[var(--badge-bg)]` / `bg-surface`、`bg-[var(--Always-White)]`     | 恒定色只用于"压在有色底上的白"                                                                 |
| `bg-primary/20`（**进度条空槽**）                                   | `bg-[var(--meter-track)]`                                             | `--meter-track` 的注释就是"进度条空槽"（`Fills-F3`）                                           |
| `bg-primary`（**进度条填充 / 勾选与单选的选中态**）                 | `bg-[var(--accent)]` + `text-[var(--Always-White)]`                   | 小面积强调用品牌蓝；勾/点在蓝底上用恒定白（与开关滑块同一档）                                  |
| `border-input`（**复选框 / 单选框未选中**）                         | `border-border`                                                       | 与输入框同一档描边                                                                             |
| `ring-2 ring-background`（**成组头像 / 状态点外那圈"隔断"**）       | `border-2 border-canvas`                                              | 本仓 `ring-*` 只有全局焦点环一处，这里要的是描边不是环，取值仍是 `--bg-canvas`                 |
| `bg-border`（**拖拽把手 / 滚动条滑块**）                            | `bg-[var(--border-strong)]`                                           | 滑块/把手比描边深一档                                                                          |
| `shadow-sm/md/lg`                                                   | `shadow-[var(--shadow-popover)]`，静态面**不用阴影**                  | `--shadow-popover` 是唯一的浮层阴影                                                            |

### 2.2 尺寸与间距

| 项                         | 取值                                                                                                     | 依据                                                                                                                 |
| -------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| 控件高度（按钮/输入/下拉） | `h-8`（32px）；`sm` 档 `h-7`、`lg` `h-9`、`xs` `h-6`；图标按钮 `size-8`                                  | `--control-height: 32px`（"输入框 / 下拉 / 按钮唯一的高度口径"）                                                     |
| 多行输入                   | `min-h-16` + `resize-y`                                                                                  | 与旧前端 `rows=3` 的实际高度一致                                                                                     |
| 表头                       | `h-9`（36px）+ `bg-[var(--bg-subtle)]`                                                                   | `.panel-head`                                                                                                        |
| 菜单项                     | `min-h-9`（36px）+ `py-1.5`                                                                              | `--menu-item-height`（两行内容仍能长）                                                                               |
| 菜单内边距                 | `p-2`（8px），分隔线 `-mx-2`                                                                             | `--menu-pad`                                                                                                         |
| 弹窗内边距                 | `p-4`（16px），宽度 `sm:max-w-[480px]`                                                                   | 旧 `AppModal` 的 `md` 档                                                                                             |
| 标题字号                   | `text-[length:var(--text-section-size)]` + `tracking-[-0.005em]`                                         | 与 `tokens.css` 的 `h2` / `.modal-title` 同级                                                                        |
| 正文字号                   | `text-[length:var(--text-meta-size)]`（14px）、`...-micro-size`（12px）、`...-body-size`（15px，输入类） | 三个具名类是坑，见 §1.1；取值仍来自 `@theme inline` 注册的那三档                                                     |
| 间距                       | 全部用 Tailwind 的数值刻度（`p-4`/`gap-2`/`px-3`…）                                                      | Tailwind 默认的 4px 基数与 `--space-*` **逐值相等**（`p-1.5`=6px、`p-2.5`=10px…），所以数值刻度就是 `--space-*` 阶梯 |
| 滚动条槽                   | `w-3`（12px）                                                                                            | `::-webkit-scrollbar { width: 12px }`                                                                                |

### 2.3 圆角

| shadcn                                              | 我们的类                          | 令牌                                   |
| --------------------------------------------------- | --------------------------------- | -------------------------------------- |
| `rounded-sm`、`rounded-md`（控件、菜单项）          | `rounded-control`                 | `--radius-control` 10px                |
| 胶囊槽（segmented 控件）                            | `rounded-row`                     | `--radius-row` 12px                    |
| 卡片                                                | `rounded-[var(--radius-panel)]`   | `--radius-panel` 16px                  |
| 浮层（菜单/下拉/选择/弹窗/抽屉/气泡之外的 popover） | `rounded-[var(--radius-overlay)]` | `--radius-overlay` 20px                |
| `rounded-full`（开关/滑块/头像）                    | `rounded-pill`                    | `--radius-pill`                        |
| `rounded-[4px]`（复选框、拖拽把手里的小方块）       | `rounded-[var(--radius-badge)]`   | `--radius-badge` 就是 4px 方角         |
| 徽章                                                | `rounded-[var(--radius-badge)]`   | `--radius-badge` **4px 方角**（见 §4） |

> `index.css` 里注册的 `--radius-card` 指向一个**不存在**的 `--radius-card` 令牌，
> 所以 `rounded-card` 是空类；卡片请用 `rounded-[var(--radius-panel)]`。详见 §7。

### 2.4 动效

上游的 `animate-in / animate-out / fade-in-0 / zoom-in-95 / slide-in-from-*` 全部**原样保留**，
但它们依赖 `tw-animate-css`（shadcn 的 Tailwind v4 动画插件），**目前项目里没有这个包**——
所以现在这些类是空跑（不影响布局，只是没有进出场动画）。主控加上
`tw-animate-css` 并在 `src/index.css` 里 `@import 'tw-animate-css';` 之后，动画会自动生效，
**不需要再动 ui/ 里的任何文件**。过渡时长/曲线用我们的 `--motion-*` 语义（`transition-colors`、
`duration-200`）。

## 3. 组合示例（旧 `AppModal` 的观感怎么拼出来）

```tsx
<Dialog>
  <DialogTrigger asChild>
    <Button>删除</Button>
  </DialogTrigger>
  <DialogContent className="flex max-h-[80vh] flex-col gap-0 p-0">
    <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3">
      <DialogTitle>删除目录「合同」？</DialogTitle>
    </DialogHeader>
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <p>关联 12 篇文档，删除后不可恢复。</p>
    </div>
    <DialogFooter className="border-t border-[var(--border-hairline)] px-4 py-3">
      <Button variant="secondary">取消</Button>
      <Button variant="destructive">删除</Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

`min-h-0` 那一句是关键：flex 子项默认 `min-height: auto`，不写它内容会把弹窗撑高、
`overflow-y: auto` 永远不生效（旧 `AppModal` 踩过同一个坑）。

## 4. 有意偏离上游的地方（全部有理由，改回去之前先读理由）

| 偏离                                                           | 理由                                                                                                                                                                                                                                                                                             |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Button` 默认高度 36 → **32px**                                | 本仓"按钮与输入框同高"的硬口径（`--control-height`）                                                                                                                                                                                                                                             |
| `Button variant="destructive"` 是**红字 + 浅红底**，不是红实心 | 沿用旧前端 `AppButton variant="danger"`；Kimi 的纪律是"动作靠墨色，红只做小面积"，且白字压红在深色主题下只有 3.0:1                                                                                                                                                                               |
| `Badge` 默认圆角 999px → **4px 方角**（另留 `shape="pill"`）   | `tokens.css` 对 `--radius-badge` 有明确注解："Kimi 的 badge 一律 4px 方角，胶囊是最像生成式设计的一种做法"。方角仍给卡片里的 `chip`（旧 `.chip` 本来就是方角）；**状态标签**那条走 `shape="pill"`——旧 `StatusTag.vue` 的 `.status` 明确是胶囊（22px 高 + `--radius-pill`），两者本来就是两种东西 |
| `Tooltip` 浅底 + 描边（上游是反色气泡），并**去掉小三角**      | 与旧前端 `InfoTip` 一致；带描边的气泡接实心三角会留接缝                                                                                                                                                                                                                                          |
| `Popover`/菜单**保留 1px 描边**                                | Kimi 深色靠底色差分，浅色下 `Bg-Tertiary`(#fff) 与画布(#fbfaf9) 几乎同色，必须靠描边                                                                                                                                                                                                             |
| `Card` 去掉 `shadow-sm`                                        | 规范：静态内容不用阴影                                                                                                                                                                                                                                                                           |
| `Skeleton` 底色 `bg-accent` → `bg-[var(--bg-hover)]`           | 与旧前端 `SkeletonBlock` 同款                                                                                                                                                                                                                                                                    |
| `Tabs` 指示条/当前项用**墨色与底色差**，不用品牌蓝             | 同上，强调只在小面积上用品牌色                                                                                                                                                                                                                                                                   |
| `ScrollArea` 只是"要和浮层一起滚"时才用                        | 全局原生细滚动条（tokens.css 那一大段）已经覆盖常规场景                                                                                                                                                                                                                                          |
| `Sheet` 的关闭按钮 28×28 + `--bg-hover`                        | 与旧 `AppModal` 的关闭按钮同款                                                                                                                                                                                                                                                                   |
| `Toaster` 位置 `top-center`、图标用语义色、主题读 `data-theme` | 旧前端通知条在顶部居中；不引 `next-themes`                                                                                                                                                                                                                                                       |
| `Avatar` 默认尺寸 32 → **28px**（`size-[var(--avatar-size)]`） | tokens.css 那一行注释就是照 Kimi 的 `.not-login-icon` 实测写的（28×28 圆形）                                                                                                                                                                                                                     |
| `Avatar` 兜底文字用 `--text-secondary`，不是三级灰             | 首字母/缩写是**要被读到的内容**，而三级灰的用途约束是"只给可略过的元信息"（见 `themes/light.css` 文件头）                                                                                                                                                                                        |
| `Checkbox` 选中态是**品牌蓝**（`--accent`）不是墨色实心        | 墨色实心在本仓只有一个意思："主按钮"；勾选框做成墨块会被读成一个按钮                                                                                                                                                                                                                             |
| `AlertDialog` 内边距 `p-6` → `p-4`、宽度 480px                 | 本仓的弹窗只有一套取值（`dialog.tsx` 的 `md` 档），`size="sm"` 仍保留 `max-w-xs`                                                                                                                                                                                                                 |
| `Progress` 把 `value` 也传给 `Root`                            | 上游只拿它算 `translateX`，于是 `role="progressbar"` 上没有 `aria-valuenow` / `aria-valuemax`——读屏读不出进度                                                                                                                                                                                    |
| `ContextMenu` / `Command` 的菜单取值**对齐 `DropdownMenu`**    | 上游这三份的 `p-1` / `p-2`、`shadow-md` / `lg`、字号各写各的；本仓菜单只有一套取值                                                                                                                                                                                                               |
| `ResizableHandle` 删掉 `focus-visible:ring-1 ...`              | 与其它组件同一条：焦点环只有 `tokens.css` 那条全局 `:focus-visible`（`role="separator"` / `tabIndex` 由库给，未丢）                                                                                                                                                                              |
| 字号写带 `length:` 提示的任意值形式而不是具名类                | 具名类与 `tokens.css` 的遗留辅助类同名且会被它们的 `color` 压过，见 §1.1                                                                                                                                                                                                                         |

## 5. 已 vendor（29 个）

第一批（19 个，不动 `package.json`）：

`button` `input` `textarea` `label` `badge` `card` `skeleton` `separator` `table` `switch`
`scroll-area` `tabs` `tooltip` `popover` `dropdown-menu` `select` `dialog` `sheet` `sonner`

第二批（10 个，依赖到位后落地）——**每个原语用的包**都写在这里，换包时别只改一处：

| 文件           | 包                             | 备注                                                                        |
| -------------- | ------------------------------ | --------------------------------------------------------------------------- |
| `alert-dialog` | `@radix-ui/react-alert-dialog` | 复用 `@/ui/button`；表面/圆角/内边距与 `dialog.tsx` 同一口径                |
| `checkbox`     | `@radix-ui/react-checkbox`     | 选中态 `--accent` + `--Always-White`；4px 圆角取 `--radius-badge`           |
| `radio-group`  | `@radix-ui/react-radio-group`  | 同上一条；圆点是 `rounded-pill` 槽 + `fill-current` 的 8px 点               |
| `context-menu` | `@radix-ui/react-context-menu` | 取值逐条对齐 `dropdown-menu`（见 §4）                                       |
| `avatar`       | `@radix-ui/react-avatar`       | 默认尺寸 `--avatar-size`（28px）；隔断圈用 `border-canvas` 替 `ring-*`      |
| `progress`     | `@radix-ui/react-progress`     | 空槽 `--meter-track`、填充 `--accent`；`value` 同时给 `Root`（a11y）        |
| `collapsible`  | `@radix-ui/react-collapsible`  | 上游这一版纯透传、一个类名都没有                                            |
| `command`      | `cmdk`                         | `CommandDialog` 走 `@/ui/dialog` 的壳，不另写弹窗；搜索图标是 lucide        |
| `drawer`       | `vaul`                         | 带手势拖拽；桌面优先的场景仍推荐 `sheet`（同一个 `@radix-ui/react-dialog`） |
| `resizable`    | `react-resizable-panels`       | v4 的 `Group` / `Panel` / `Separator`；`role="separator"` + `tabIndex` 保留 |

`tw-animate-css` 已经在 `package.json` 且 `src/index.css` 里 `@import` 了，
所以 §2.4 那套 `animate-in` / `slide-in-from-*` 现在**真的在跑**，不是空类。

## 6. 上游源码缓存

`.cache/upstream/fetch/ny-v4/` 里按同一个 commit（`98a1fe6`）存着 35 份 `ui/*.tsx`。
本次新落的 10 个文件都按同一流程重新从
`raw.githubusercontent.com/shadcn-ui/ui/98a1fe67b439324ddc857f47fbdce056600a4329/apps/v4/registry/new-york-v4/ui/<name>.tsx`
拉过一遍并与缓存逐字节比对（十个文件全部一致），再用 §2 的映射表改造。

还没 vendor 的（上游有、我们暂时用不上）：`button-group` `empty` `input-group` `item` `kbd`
`spinner`——它们都不是缺依赖，而是"目前没有调用方"。

## 7. 给主控的三条建议（都不在本次所有权内，故只报告）

1. `src/index.css` 的 `@theme inline` 里有三个**指向不存在的令牌**的映射，生成的类名是空类：
   `--color-surface-raised: var(--bg-surface-raised)`、`--color-elevated: var(--bg-elevated)`、
   `--radius-card: var(--radius-card)`——`tokens.css` 里没有 `--bg-surface-raised` /
   `--bg-elevated` / `--radius-card`（对应的是 `--bg-overlay`? / `--radius-panel`）。
   要么补上令牌，要么把这行删掉，否则 `bg-surface-raised` 会静默失效。
2. 建议补几条注册，省掉组件里的 `[...]` 写法：
   `--color-menu: var(--bg-menu)`、`--color-subtle: var(--bg-subtle)`、`--color-hover: var(--bg-hover)`、
   `--color-overlay: var(--bg-overlay)`、`--color-scrim: var(--overlay-scrim)`、
   `--radius-overlay: var(--radius-overlay)`、`--radius-panel: var(--radius-panel)`、
   `--radius-badge: var(--radius-badge)`、`--color-danger-soft: var(--danger-soft)`、
   `--color-status-info: var(--status-info)`。
   补了之后 `bg-[var(--bg-menu)]` 可以写成 `bg-menu`，语义一样但短得多。
3. **`tokens.css` 与新注册的字号类名撞车了（建议改一处）**：`tokens.css` 的
   `.text-meta` / `.text-micro`（旧 Vue 前端当辅助类用）与 `@theme inline` 注册出来的
   `text-meta` / `text-micro` **同名**，而前者是无 `@layer` 的，会连 `color` 一起赢过 Tailwind。
   彻底的办法二选一（都能让 `text-meta` 这类短名字重新可用）：
   把 `tokens.css` 里那两条辅助类删掉（新前端没人用它），或者把主题键改成
   `--text-size-meta`（类名变成 `text-size-meta`，不再撞车）。
   在那之前，`src/ui/**` 一律用带 `length:` 提示的任意值写法，见 §1.1。
