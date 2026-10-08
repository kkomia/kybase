/**
 * 类名合并：`clsx`（条件拼接）+ `tailwind-merge`（后写的同类覆盖先写的）。
 * 与 shadcn/ui 官方同一份实现（`cn(...inputs) => twMerge(clsx(inputs))`），
 * 只多了一处**必需的**扩展，见下。
 *
 * ---------------------------------------------------------------- 为什么要扩一处
 * `tailwind-merge` 靠"猜"来归类类名：`text-` 开头的它要么当字号（text-sm）、
 * 要么当颜色（text-red-500）。我们的字阶类名 `text-micro` / `text-meta` / `text-body`
 * 不在 Tailwind 内置字号表里，于是被猜成**颜色**——只要同一个元素上还写了文字色
 * （`text-text-secondary` 这种），两者就被判为同一组冲突，**字号被静默丢掉**：
 *
 *     twMerge('text-meta text-text-secondary')  →  'text-text-secondary'   // text-meta 没了
 *
 * 全局都靠 `cn()` 拼类名，所以这不是"某个组件的小毛病"，而是"所有字号都不生效"。
 * 用官方给的 `extendTailwindMerge` 把三个名字注册进 font-size 组即可（实测：
 * 注册后两者共存，真正的同类冲突仍然照常覆盖）。
 *
 * 顺带一条约定：写**任意值**字号时必须带上 `length:` 类型提示
 * （例如 `text-[length:var(--text-section-size)]`）。省掉它会同样被当成颜色类，
 * 于是又回到上面那个"字号被吞掉"的问题——提示写与不写，只差两个字符。
 *
 * 注意这里扩展的 `text-meta` / `text-micro` 两个名字**同时还有一个层叠陷阱**：
 * `tokens.css` 里有同名的遗留辅助类（无 `@layer`，会压过 Tailwind 的 utilities、
 * 连 `color` 一起改掉）。`src/ui/**` 因此一律走带 `length:` 提示的任意值写法，
 * 详见 `src/ui/README.md` §1.1。
 */
import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      // `@theme inline` 里注册的三个字阶（index.css）；新增字号时这里要一起加
      'font-size': [{ text: ['micro', 'meta', 'body'] }],
    },
  },
})

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
