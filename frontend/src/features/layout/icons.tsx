/**
 * 侧栏这一片**自绘**的两个图标——不是 Remix 那套（其余图标见 `@remixicon/react`）。
 *
 * 旧前端（Vue）在 `components/icons/` 下也把这两枚单独放：它们的来源由用户指定，
 * 不属于 Remix Icon 集合，所以不能拿 `Ri*` 顶替。这里**照搬旧 SVG 的几何**，
 * 一个数都不改（只把 Vue 的模板写法翻成 JSX）。
 *
 * | 本文件 | 旧文件 | 用在哪 |
 * | --- | --- | --- |
 * | `IconChatNew` | `components/icons/IconChatNew.vue` | 侧栏最上面那颗「新建会话」 |
 * | `IconSidebar` | `components/icons/IconSidebar.vue` | 品牌行右侧的「收缩/展开侧栏」 |
 *
 * 两枚都遵守同一套口径（与旧 `IconBase.vue` 一致）：**24×24 网格、颜色由 `currentColor`
 * 决定、`aria-hidden`**——图标是装饰，名字由外层按钮的 `aria-label` 给。
 */
import type { SVGProps } from 'react'

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'width' | 'height'> {
  /** 边长（px）。默认 16——与旧 `IconBase` 的 `size` 默认值相同。 */
  size?: number
}

/**
 * 「新建会话」：圆角矩形对话气泡 + 左下角小尾巴 + 气泡里一支时钟指针。
 *
 * 照搬旧 `IconChatNew.vue`（含它那份"为什么不用 remixicon"的记录）：
 * `remixicon chat-history-line` 与 `tdesign chat-bubble-history` 都是**圆**气泡，
 * `mdi message-text-clock-outline` 里面还塞了文字线——都对不上参考图，
 * 所以这组几何是照参考图描的。三个口径与旧文件一致：
 *
 * 1. **直接按 24×24 网格布点**（不需要 `IconSidebar` 那样的 `transform` 缩放）；
 * 2. `fill="none"` + `stroke="currentColor"`：深浅主题都由 `color` 决定；
 * 3. **线宽 1.8**：16px 下约 1.2px，与旁边 Remix 那套的观感相当。
 */
export function IconChatNew({ size = 16, ...props }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <g fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
        <path d="M4.3 4.1h15.4a1.6 1.6 0 0 1 1.6 1.6v10.2a1.6 1.6 0 0 1-1.6 1.6h-9.9l-4.9 4.3v-4.3h-.6a1.6 1.6 0 0 1-1.6-1.6V5.7a1.6 1.6 0 0 1 1.6-1.6z" />
        <path d="M12 7.8v4.1l2.7 2.7" />
      </g>
    </svg>
  )
}

/**
 * 「收缩 / 展开侧栏」：一块面板，左侧那一条是侧栏。
 *
 * 照搬旧 `IconSidebar.vue`（用户提供的 iconfont 原稿），它那份文件记了三个适配：
 *
 * 1. **1024×1024 网格**：`IconBase` 统一 24×24，所以保留原路径、用
 *    `transform="scale(0.0234375)"` 缩进 24 网格（1024 × 0.0234375 = 24）；
 * 2. **原稿硬编码的 `fill="#666666"` 改成 `currentColor`**——写死的灰在深色主题下
 *    会看不见（由下面 `fill="currentColor"` 统一保证）；
 * 3. 去掉 iconfont 生成器的 `t` / `p-id` / `class` 属性，只留几何。
 */
export function IconSidebar({ size = 16, ...props }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path
        transform="scale(0.0234375)"
        d="M810.666667 85.333333a128 128 0 0 1 128 128v597.333334a128 128 0 0 1-128 128H213.333333a128 128 0 0 1-128-128V213.333333a128 128 0 0 1 128-128h597.333334zM341.333333 170.666667H213.333333l-5.802666 0.426666a42.538667 42.538667 0 0 0-36.48 36.437334L170.666667 213.333333v597.333334l0.426666 5.802666a42.538667 42.538667 0 0 0 36.437334 36.48L213.333333 853.333333h128V170.666667z m469.333334 0h-384v682.666666h384l5.802666-0.426666a42.538667 42.538667 0 0 0 36.48-36.437334L853.333333 810.666667V213.333333l-0.426666-5.802666A42.538667 42.538667 0 0 0 810.666667 170.666667z"
      />
    </svg>
  )
}
