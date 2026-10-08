/**
 * 对话回答的富文本渲染（《前端设计规范》§4「文档优先」）。
 *
 * 源文件：`frontend/src/composables/useMarkdown.ts`（580 行）。**换实现、留规则**：
 * 自研的"先切块再渲染"那一套（`splitBlocks` / `inline` / 两层缓存）换成了
 * **react-markdown + remark-gfm + rehype-katex/highlight**，而旧实现里那些
 * "为什么这么写"的边界一条条搬了过来，落在下面这些 plugin 与组件里：
 *
 * | 旧实现 | 现在 |
 * | --- | --- |
 * | `escapeHtml` 先转义、只还原自己认出来的标记 | react-markdown **默认不认 HTML**：`<img …>` 这类原样当成文本转义输出（`skipHtml` 保持默认，进不了元素）；文本节点由 React 自己转义 |
 * | `inline()` 的 `代码` / `**加粗**` / `[文字](url)` | remark 的 CommonMark + `remark-gfm`（表格 / 删除线 / 任务列表） |
 * | `SAFE_LINK` 只放行 http(s)/mailto | `rehypeUnwrapLinks`：**非白名单的链接退回原文**（`javascript:` 变回字面量，可读不可点） |
 * | `BARE_URL` 裸网址成链（含"裁断的网址不链"） | `rehypeBareUrls`：**同一套正则、同一条尾巴规则**（见那里） |
 * | `linkify` 前先 `keep()` 摘走代码段与已生成的 `<a>` | 现在是**结构性跳过**：hast 里"在 `code`/`pre`/`a` 里"就天然不在扫描范围 |
 * | `codeBlockHtml` / `tableBlockHtml` 两段式容器 + 复制按钮 | `MarkdownPre` / `MarkdownTable` 两个组件（同样的 class 与 `data-*`，事件委托照样能用） |
 * | `decorateCitations` 把 `[N]` 换成徽标（跳过代码） | `rehypeCitations`（同一套正则与"整组对不上就整组不换"） |
 * | `HTML_CACHE` / `BLOCK_CACHE` 两层缓存 | 只留文本级（React 元素按 `(text, sources, plain)` 缓存）。**块级那层没有插点了**：解析在库里面，见 `putCapped` 上面那段 |
 * | `splitInlineLatex` 切公式、`rehype-katex` 排 | **`remark-math` + `rehype-katex`**（标准口径，差异见 `model/README.md`） |
 *
 * ## 出口：一个组件 + 三个函数（README 里写着怎么选）
 *
 * - `<Answer text sources onOpenSource … />`：**推荐给对话页**。自带点击/键盘处理，
 *   不用再在容器上做 `[data-cite-index]` 的事件委托（旧的接法也仍然能用）；
 * - `renderAnswerMarkdown(text, actions?)` / `renderAnswerWithCitations(text, sources, actions?)`
 *   / `renderPlainMarkdown(text)`：**返回 ReactNode**（旧实现返回 HTML 字符串），
 *   名称与旧实现一致，方便逐条对照；`data-*` 也照旧，页面可以继续用委托。
 *
 * 两个使用注意写在 README：KaTeX 的样式表要自己引；`renderPlainMarkdown` 是**只读那一档**，
 * 它与富文本档有三处不同（见 `PLAIN_COMPONENTS` 与 `renderMarkdown` 的插件表）：
 * 不挂复制 / 下载按钮；**标题保留原文层级**（不夹到 2–4 级）；**单换行不换成 `<br>`**。
 * 后两条是"对话里那份是模型的回复、预览里那份是一份文档"这条区别落下来的。
 */

import { ChevronDown, ChevronUp } from 'lucide-react'
import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type JSX,
  type ReactNode,
} from 'react'
import ReactMarkdown, { type ExtraProps, type Options } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import styleToObject from 'style-to-object'

import type { WebCitation } from './sourceCitations'
import { hostOfUrl } from './webSites'

/**
 * 只允许这两种协议：`javascript:` 之类的链接点了就是执行代码，必须挡掉。
 *
 * 与旧实现逐字一致（含"相对链接也退回原文"这条副作用——见 `rehypeUnwrapLinks`）。
 */
const SAFE_LINK = /^(https?:\/\/|mailto:)/i

/**
 * 正文里的**裸链接**（v0.26）。**正则逐字照搬**。
 *
 * 模型经常直接把网址写在正文里（"来源：https://…"、搜索结果那一段），
 * 而 Markdown 的 `[文字](链接)` 只有它主动写出来才有——那种情况下用户只能手抄。
 *
 * 只认 `http(s)://` 与 `www.` 开头：**不猜邮箱、不猜裸域名**。
 * 猜错一个（把 `README.md` 认成网址）比漏掉几个糟得多，而漏掉的那些还能手抄。
 *
 * 两条边界都是踩出来的：
 * - **前面只要不是字母数字**（`[^\w/@]`）就算开头。原先写的是"前面必须是空白或左括号"，
 *   于是"来源：https://…"里的网址根本不匹配——那个全角冒号不在白名单里，
 *   而这恰恰是模型最常见的写法；
 * - **网址体里不许出现中文标点**。"……详见 https://a.com。另外……"这种句子里，
 *   句号后面紧跟的是下一个句子，不排除的话整个"https://a.com。另外"会被当成一个网址。
 *
 * GFM 自己也有一套 autolink（会把邮箱、中文句号一起吞进链接），所以
 * `rehypeUnwrapLinks` 先把**解析器猜出来的**链接退回原文，再由这条正则重新成链——
 * 于是"哪种算网址"只有这一处口径。
 */
const BARE_URL = /(^|[^\w/@])((?:https?:\/\/|www\.)[^\s<>()（）「」『』【】"'。，、；：！？…]+)/g

/**
 * 网址**末尾不该跟着的字符**：句号、逗号、中文标点、右括号。
 *
 * 必须剥掉：模型写的是"…详见 https://example.com/a。"，那个句号属于句子。
 * 括号一律剥（`(a)` 里那个本该属于网址）——中文语境里右括号跟着网址几乎总是句法括号，
 * 剥错了只是让链接短一点，不剥则会让链接点开 404。
 */
const URL_TAIL = /[.,;:!?，。、；：！？）)】」』"']+$/

/* ------------------------------------------------------------------ hast 最小形状 */

/**
 * hast 的最小形状（只写这个文件用到的）。
 *
 * 不 import `hast` 的类型：它不是本包的依赖（pnpm 的 node_modules 里只有直接依赖），
 * 而这一层要的形状就这么点。React 那边（`react-markdown`）的 `Components` 收的
 * 也是同一批对象，只是类型声明来自它自己的依赖树。
 */
interface HastText {
  type: 'text'
  value: string
}

interface HastElement {
  type: 'element'
  tagName: string
  properties: Record<string, unknown>
  children: HastNode[]
}

/** 其余节点（注释 / doctype / raw）。`type` 写死成这几个，好让 `=== 'text'` 收窄。 */
interface HastOther {
  type: 'raw' | 'comment' | 'doctype'
  value?: string
  children?: HastNode[]
}

interface HastRoot {
  type: 'root'
  children: HastNode[]
}

type HastNode = HastText | HastElement | HastOther | HastRoot

/** 插件拿到的第二个参数只需要 `value`（原始 Markdown 源码）。 */
interface SourceFile {
  value?: unknown
}

function isElement(node: HastNode): node is HastElement {
  return node.type === 'element'
}

/** 子节点（文本节点没有子节点；写成函数是为了让联合类型收窄在一处）。 */
function childrenOf(node: HastNode): HastNode[] {
  return node.type === 'text' ? [] : (node.children ?? [])
}

/** 节点下所有文本拼起来（等价于 DOM 的 `textContent`）。 */
function textOf(node: HastNode): string {
  if (node.type === 'text') return node.value
  return childrenOf(node).map(textOf).join('')
}

/** 深度优先遍历（自己写十几行，不为它引一个包）。`parents` 是祖先链，含自己。 */
function walk(
  node: HastNode,
  visit: (node: HastNode, parents: HastNode[]) => void,
  parents: HastNode[] = [],
): void {
  const chain = [...parents, node]
  visit(node, chain)
  for (const child of childrenOf(node)) walk(child, visit, chain)
}

/** 祖先链里有没有这些标签（`code`/`pre`/`a`/数学）——文本级的几条规则都要它。 */
const SKIP_TAGS = new Set(['code', 'pre', 'a', 'script', 'style', 'math'])

function inSkippedTag(parents: HastNode[]): boolean {
  for (const parent of parents) {
    if (isElement(parent) && SKIP_TAGS.has(parent.tagName)) return true
    if (isElement(parent) && classNameOf(parent).includes('math')) return true
  }
  return false
}

function classNameOf(node: HastElement): string[] {
  const value = node.properties.className
  return Array.isArray(value) ? value.map((item) => String(item)) : []
}

/** 造一个元素（省得每处写一遍三个字段）。 */
function element(
  tagName: string,
  properties: Record<string, unknown>,
  children: HastNode[],
): HastElement {
  return { type: 'element', tagName, properties, children }
}

function text(value: string): HastText {
  return { type: 'text', value }
}

/** 把一串节点接回父节点的某个位置（用来替换文本节点）。 */
/** 换掉某个子节点（文本节点没有 `children`，所以赋值要走这里收窄）。 */
function setChildren(node: HastNode, children: HastNode[]): void {
  ;(node as { children?: HastNode[] }).children = children
}

function replaceChild(parent: HastNode, index: number, nodes: HastNode[]): HastNode[] {
  const children = childrenOf(parent)
  return [...children.slice(0, index), ...nodes, ...children.slice(index + 1)]
}

/* ------------------------------------------------------------------ rehype 插件 */

/**
 * 把**解析器猜出来的链接**与**不在白名单里的链接**退回原文（旧 `inline()` 的两条规则）。
 *
 * 两件事合成一个插件：
 *
 * 1. GFM 的 autolink literal 会把 `www.x.com`、邮箱、甚至
 *    `https://a.com。另外`（中文句号照吞）变成链接——旧实现只认
 *    `http(s)://` 与 `www.` 开头、且网址体里不许有中文标点。所以这里把
 *    **不是手写 Markdown 链接**的那些（源码里不以 `[` 开头的）统统退回文本，
 *    交给 `rehypeBareUrls` 用旧正则重新成链；
 * 2. 手写的 `[文字](链接)` 只放行 http(s)/mailto：`javascript:` 会被 markdown
 *    解析阶段就把 `href` 洗成空串，这里据"源码原文"把它整条退回字面量
 *    （`[点我](javascript:alert(1))`），与旧实现"原样留着（可读、不可点）"一致。
 *
 * 靠 `position` 取源码原文：hast 的节点带着它在源文里的偏移量。
 */
function rehypeUnwrapLinks() {
  return (tree: HastRoot, file: SourceFile): void => {
    const source = typeof file?.value === 'string' ? file.value : ''
    // 从**根**开始扫：根不是 element，入口不能先按 element 收窄
    const scan = (node: HastNode): void => {
      const children = childrenOf(node)
      for (let index = 0; index < children.length; index += 1) {
        const child = children[index]
        if (!isElement(child)) continue
        if (child.tagName === 'a') {
          const href = String(child.properties.href ?? '')
          const raw = rawOf(child, source)
          // 手写的 `[文字](链接)`：白名单外的一律退回原文
          const handWritten = raw?.startsWith('[') === true
          if (href && SAFE_LINK.test(href) && (!raw || handWritten)) continue
          setChildren(node, replaceChild(node, index, [text(raw ?? textOf(child))]))
          continue
        }
        scan(child)
      }
    }
    scan(tree)
  }
}

/** 元素在源码里的原文（拿不到 `position` 时返回 null）。 */
function rawOf(node: HastElement, source: string): string | null {
  const position = (
    node as unknown as { position?: { start?: { offset?: number }; end?: { offset?: number } } }
  ).position
  const start = position?.start?.offset
  const end = position?.end?.offset
  if (!source || typeof start !== 'number' || typeof end !== 'number') return null
  return source.slice(start, end)
}

/**
 * 裸网址 → `<a class="md-link">`（旧 `linkify`，规则逐条照搬）。
 *
 * 三条边界：
 *
 * - **被裁断的网址不做链接**：紧跟一个省略号就说明它只剩半截
 *   （工具结果那一行由后端裁到 120 字）。链过去是个不存在的地址，
 *   而用户会以为是自己网络的问题——那比"不能点"糟得多。
 * - **末尾的句法标点剥掉**（`URL_TAIL`），剥下来的那些字仍在正文里；
 * - `www.` 开头的补上 `https://`：不带协议的 href 会被当成站内相对路径。
 *
 * 旧实现要先把代码段与已生成的 `<a>` 摘出来（`keep` + 占位符），因为它是扫全文字符串；
 * 这里**结构性地跳过**：`code`/`pre`/`a`（以及 KaTeX 生成的节点）里的文本根本不看。
 */
/**
 * 把某一层里的**文本节点**逐个过一遍变换（返回 null = 不动它）。
 *
 * 为什么先收成新数组、最后一次性写回：几个插件都是"一个文本节点换成好几个"
 * （裸链接、引用徽标、公式、软换行），边遍历边改数组会让**下标错位**
 * （改完一个节点，后面那些还在按旧下标取），表现是丢字——踩过。
 * 递归在**变换之前**做完，所以每一层只写自己那一次。
 */
function mapTextChildren(
  node: HastNode,
  parents: HastNode[],
  transform: (value: string, chain: HastNode[]) => HastNode[] | null,
): void {
  const children = childrenOf(node)
  const next: HastNode[] = []
  let changed = false
  for (const child of children) {
    if (child.type === 'text') {
      const chain = [...parents, child]
      const pieces = inSkippedTag(chain) ? null : transform(child.value, chain)
      if (pieces) {
        changed = true
        next.push(...pieces)
        continue
      }
      next.push(child)
      continue
    }
    if (isElement(child)) mapTextChildren(child, [...parents, child], transform)
    next.push(child)
  }
  if (changed) setChildren(node, next)
}

function rehypeBareUrls() {
  return (tree: HastRoot): void => {
    mapTextChildren(tree, [], (value) => linkifyText(value))
  }
}

/** 把一个文本节点按裸网址切开；没有可链的就返回 null（表示"别动它"）。 */
function linkifyText(value: string): HastNode[] | null {
  BARE_URL.lastIndex = 0
  const out: HastNode[] = []
  let cursor = 0
  let touched = false
  for (let match = BARE_URL.exec(value); match; match = BARE_URL.exec(value)) {
    // **被裁断的网址不做链接**：紧跟一个省略号就说明它只剩半截
    if (value[match.index + match[0].length] === '…') continue
    const prefix = match[1]
    const url = match[2]
    const trimmed = url.replace(URL_TAIL, '')
    const tail = url.slice(trimmed.length)
    // `www.` 开头的补上协议：不带协议的 href 会被当成站内相对路径
    const href = trimmed.startsWith('www.') ? `https://${trimmed}` : trimmed
    touched = true
    if (match.index > cursor) out.push(text(value.slice(cursor, match.index)))
    out.push(text(prefix))
    // `rel` 与 `target` 与 Markdown 那条规则**完全一致**——两处不能有两种开法
    out.push(
      element(
        'a',
        {
          className: ['md-link'],
          href,
          target: '_blank',
          rel: 'noopener noreferrer',
        },
        [text(trimmed)],
      ),
    )
    if (tail) out.push(text(tail))
    cursor = match.index + match[0].length
  }
  if (!touched) return null
  if (cursor < value.length) out.push(text(value.slice(cursor)))
  return out
}

/**
 * 段落里的软换行 → `<br />`（旧实现："段落内的换行折成 br，而不是各起一段"）。
 *
 * 块级切分交给 remark，只有这一条排版规则要自己接：Markdown 的软换行渲染成一个
 * 空格，而**模型的回答**里换行就是换行（口语、行短，模型写的换行是有意断行）。
 *
 * **只挂在富文本那一档**：文件预览渲染的是一份文档，文档里的单换行就是 CommonMark
 * 说的那个空格（按 `\n` 断行会把原文的段落折行当成作者的手动断行，排版被改写）。
 * 差异由调用方决定，见 `renderMarkdown` 的插件表。
 *
 * **只管 `p` 里的文本**：两处坑都踩过——
 * 块与块之间（`</p>\n<p>`、列表项之间、`blockquote` 里包着的那一层）也有换行，
 * 那是排版分隔，换成 `<br>` 会凭空多出一倍空行。
 * 引用块里的多行由它内部那个 `p` 接住（`> 第一行\n> 第二行` → `blockquote > p`）。
 */
function rehypeSoftBreaks() {
  return (tree: HastRoot): void => {
    mapTextChildren(tree, [], (value, chain) => {
      const parent = chain.at(-2)
      if (!parent || !isElement(parent) || parent.tagName !== 'p') return null
      if (!value.includes('\n')) return null
      const pieces: HastNode[] = []
      value.split('\n').forEach((line, order) => {
        if (order > 0) pieces.push(element('br', {}, []))
        if (line) pieces.push(text(line))
      })
      return pieces
    })
  }
}

/** 块级标签：只有夹在它们之间的空白才属于"排版换行"（见 `rehypeTrimBlocks`）。 */
const BLOCK_TAGS = new Set([
  'p',
  'div',
  'section',
  'blockquote',
  'ul',
  'ol',
  'li',
  'pre',
  'table',
  'thead',
  'tbody',
  'tr',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
])

/** 可能夹着"排版换行"的那几层容器（`p` / `h?` 里的空白是内容，不能动）。 */
const TRIM_CONTAINERS = new Set(['root', 'div', 'section', 'blockquote', 'ul', 'ol', 'li'])

/**
 * 丢掉**块与块之间**那个只含换行的文本节点。
 *
 * remark 在块级元素之间插一个 `"
"` 文本节点（`</p>
<p>`）。旧的自研渲染器是
 * 直接 `join('')`，没有这种节点；React 把它当普通文本渲染出来，DOM 里就多出一堆
 * 空白节点——视觉上无害（块之间本来就是隔开的），但**"同内容同输出"这条对不上**，
 * 而且 `white-space: pre-wrap` 的容器里会真的多出一行。
 *
 * 只管**容器**里的空白（见 `TRIM_CONTAINERS`）：`p` 里的换行是内容，
 * 由 `rehypeSoftBreaks` 换成 `<br>`。
 */
function rehypeTrimBlocks() {
  return (tree: HastRoot): void => {
    const scan = (node: HastNode): void => {
      const children = childrenOf(node)
      const keep = children.filter((child, index) => {
        if (child.type !== 'text' || child.value.trim() !== '') return true
        const sibling = (list: HastNode[]): boolean => {
          const found = list.find((item) => !(item.type === 'text' && item.value?.trim() === ''))
          return found === undefined || (isElement(found) && BLOCK_TAGS.has(found.tagName))
        }
        const before = sibling(children.slice(0, index).reverse())
        const after = sibling(children.slice(index + 1))
        return !(before && after)
      })
      if (keep.length !== children.length) setChildren(node, keep)
      for (const child of keep) {
        if (!isElement(child)) continue
        if (child.tagName === 'code' || child.tagName === 'pre') continue
        scan(child)
      }
    }
    const walkContainers = (node: HastNode): void => {
      const tag = isElement(node) ? node.tagName : 'root'
      if (TRIM_CONTAINERS.has(tag)) scan(node)
      for (const child of childrenOf(node)) {
        if (isElement(child) && !TRIM_CONTAINERS.has(child.tagName)) continue
        walkContainers(child)
      }
    }
    walkContainers(tree)
  }
}

/* ------------------------------------------------------------------ 公式 */

/*
 * `$…$` / `$$…$$` 走**标准那一路**（`remark-math` + `rehype-katex`，在 `renderMarkdown`
 * 的 plugins 里挂上），这里没有自写的 rehype 插件了——原先那个
 * `rehypeInlineMath`（拿 `latex.ts` 的 `splitInlineLatex` 把文本节点切成 `span.math`）
 * 是"标准件还没装上"时的替身。现在公式在**解析期**（mdast）就被认走，走的是
 * GitHub / remark 生态同一套口径：
 *
 * - `$…$`（行内）与 `$$ … $$`（整式：围栏自成一行才是 display）都由 micromark 认；
 * - 配对个数必须相等（`$$` 配 `$$`），公式体里不出现落单的 `$`、不跨空行；
 * - 公式体是**原文**（`\%` 这类转义不会被 CommonMark 提前吃掉，交给 KaTeX 自己解）。
 *
 * **与旧口径的差异逐条写在 `model/README.md`**（最要紧的一条：标准口径下
 * `$5 到 $10` 这种"钱"也会被当成公式——它不判"这像不像公式"，只认 `$` 配对）。
 */

/* ------------------------------------------------------------------ 行内引用 */

/** 引用徽标要用的那一小撮字段（`ChatSource` 的子集，避免这里依赖 api 层）。 */
export interface CitationSource {
  index: number
  document_name: string
  heading_path?: string | null
  page?: number | null
}

/** `[1]`、`[1,2]`、`[1，2]`——模型这几种写法都见过。 */
const CITE_RE = /\[(\d+(?:\s*[,，]\s*\d+)*)\]/g

/**
 * 回答里的 `[1] [2]` 标号**渲染成可点击的徽标**（参考 WeKnora / Perplexity 的做法）。
 *
 * 旧实现是在渲染出来的 HTML 上再替换一遍（所以要按 `<pre>` / `<code>` 切开、
 * 跳过代码段）；这里改成在文本节点上做，代码段天然不在扫描范围里——
 * **同一条规则（代码里的 `[1]` 是代码，不是引用），少一层切分**。
 *
 * 只替换**确实存在对应出处**的编号：模型偶尔会写 `[7]` 而检索只给了 6 条，
 * 那种天上掉下来的编号必须原样留着——做成一个点了没反应的徽标比不替换更糟。
 * 组里有一个对不上就整组不换（`[3, 9]` 换一半会把原意读歪）。
 *
 * `options.fallback` 是**给"对不上的编号"的一句说明**（v0.28，第二批评审 A6）。给了它
 * 之后，对不上的编号会渲染成一枚**不可点**的虚线标记，`title` 就是那句话；没给则照旧
 * 原样留着。两条边界：**不伪造出处**（联网结果没有文档名/页码，做不成出处），
 * **不把没有对应实体的编号做成可点**（点了没反应的徽标比不替换更糟）。
 */
export interface CiteFallback {
  /** 悬停说明，例如「联网搜索结果，见过程面板」。 */
  title: string
  /**
   * **编号 → 网页引用**（D11-③，可选）。给了之后，对得上的编号不再画成
   * "有说明的非链接"，而是交给界面画成**站点徽章**（真实 logo + 域名 + 悬停卡片）；
   * 卡片长什么样由调用方给（见 `MarkdownActions.renderWebCitation`）——
   * 这一层不认识任何界面组件，只认识"哪几个编号是网页引用"。
   *
   * 数据从哪来：联网那一步的返回文本（`model/sourceCitations.ts` 解析），
   * **不新增后端字段**。
   */
  citations?: ReadonlyMap<number, WebCitation>
}

function rehypeCitations(options: { sources: readonly CitationSource[]; fallback?: CiteFallback }) {
  const known = new Map(options.sources.map((source) => [source.index, source]))
  const web = options.fallback?.citations
  return (tree: HastRoot): void => {
    mapTextChildren(tree, [], (value) => citationPieces(value, known, options.fallback, web))
  }
}

function citationPieces(
  value: string,
  known: Map<number, CitationSource>,
  fallback?: CiteFallback,
  web?: ReadonlyMap<number, WebCitation>,
): HastNode[] | null {
  CITE_RE.lastIndex = 0
  if (!CITE_RE.test(value)) return null
  CITE_RE.lastIndex = 0
  return value
    .split(/(\[\d+(?:\s*[,，]\s*\d+)*\])/)
    .filter((part) => part !== '')
    .flatMap((part): HastNode[] => {
      const matched = /^\[(\d+(?:\s*[,，]\s*\d+)*)\]$/.exec(part)
      if (!matched) return [text(part)]
      const numbers = matched[1]
        .split(/[,，]/)
        .map((item) => Number(item.trim()))
        .filter((item) => Number.isInteger(item))
      if (numbers.length === 0) return [text(part)]
      /** 这个编号有出处吗（知识库那条，或者联网那条）。 */
      const has = (item: number): boolean => known.has(item) || Boolean(web?.has(item))
      // 没有兜底说明时：组里有一个对不上就整组不换（`[3, 9]` 换一半会把原意读歪）
      if (!fallback && numbers.some((item) => !has(item))) return [text(part)]
      // 走到这里：要么每个编号都有出处，要么有兜底说明接住对不上的那些
      return numbers.map((item) => {
        if (known.has(item)) return citationChip(known.get(item)!)
        const citation = web?.get(item)
        if (citation) return siteCitationChip(citation)
        return plainCitationChip(item, fallback!)
      })
    })
}

/**
 * **网页引用**那一枚（D11-③）：先把"编号 + 域名"落成标记，界面那一层再换成
 * `SourceBadge`（真实 logo + 悬停卡片）。
 *
 * 为什么不让这一层直接画徽章：`model/` 不认识界面组件（分层纪律），
 * 而"画成什么样"正是界面的事。所以这里给的是**数据 + 一个可认的类名**，
 * 界面通过 `MarkdownActions.renderWebCitation` 接管。
 * 落到文本里的那一份是域名（没有界面接管时，读者看到的也是"这条来自 github.com"，
 * 而不是一个光秃秃的 `[1]`）。
 */
function siteCitationChip(citation: WebCitation): HastElement {
  return element(
    'a',
    {
      className: ['md-cite', 'md-cite-site'],
      'data-cite-index': String(citation.index),
      // 界面那一层据此认出"这是网页引用那一枚"，改成真实 logo + 卡片
      'data-cite-site-chip': '1',
      title: citation.title ? `${citation.title}（${citation.domain}）` : citation.domain,
    },
    [text(citation.domain)],
  )
}

/**
 * 常见文档扩展名。
 *
 * 徽标里"这份文件叫什么"比"它是什么格式"更该先被看到，`…专家共识（2024年.pdf`
 * 里的 `.pdf` 只是尾巴；而且它占的那 4 个字符正好是最先被省略号吃掉的位置。
 */
const DOC_EXTENSION = /\.(pdf|docx?|xlsx?|pptx?|md|markdown|txt|csv|json|html?)$/i

/** 徽标上显示的文档短名：压平空白 + 去掉目录前缀与扩展名（空名兜底成"文档"）。 */
export function shortDocumentName(name: string): string {
  const trimmed = name.trim().replace(/\s+/g, ' ')
  if (!trimmed) return '文档'
  // **目录前缀也要去掉**：整目录上传的文档名带一层批次目录
  // （`markdown_20260908-…_110files/共识.md`），留着它，徽标里最先被看到的就是
  // 那串无意义的批次号——而窄徽标恰恰只显示开头几个字。
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  const base = cut >= 0 ? trimmed.slice(cut + 1) : trimmed
  return base.replace(DOC_EXTENSION, '') || base
}

/**
 * 徽标上的 `title` 给鼠标悬停看"这一条是哪份文件的哪一段"。
 *
 * 徽标**显示文档名而不是序号**：读者要的是"这句依据来自哪份资料"，序号只有回去
 * 数出处列表才有意义。名字太长由内层 span 省略（样式见对话页的 `.md-cite-name`），
 * 完整名字与位置仍在 `title` 里，悬停可见。
 */
function citationChip(source: CitationSource): HastElement {
  const where: string[] = []
  if (source.heading_path) where.push(source.heading_path)
  if (source.page != null) where.push(`第 ${source.page} 页`)
  const title = where.length
    ? `${source.document_name} · ${where.join(' › ')}`
    : source.document_name
  return element(
    'a',
    {
      className: ['md-cite'],
      // 组件那一层据此认出"这是徽标不是链接"，并把它重新发成 `data-cite-index` 属性
      // （页面的 `[data-cite-index]` 事件委托因此照旧可用）
      'data-cite-index': String(source.index),
      role: 'button',
      tabIndex: 0,
      title,
    },
    [
      element('span', { className: ['md-cite-name'] }, [
        text(shortDocumentName(source.document_name)),
      ]),
    ],
  )
}

/**
 * **对不上出处的编号**（v0.28）：一枚不可点的标记 + 一句说明。
 *
 * 为什么不做成链接：它背后没有实体（联网搜索的结果不是"出处"——没有文档名、没有页码、
 * 也不在检索结果里），点开只能是空动作。做成 `<span>` 而不是 `<a role="button">`，
 * 键盘与读屏都不会把它当成控件；`title` 里那句"见过程面板"才是它给出的**下一步**
 * （过程面板里确实列着那一次的搜索返回，编号就在里面）。
 */
function plainCitationChip(index: number, fallback: CiteFallback): HastElement {
  return element(
    'span',
    {
      className: ['md-cite', 'md-cite-plain'],
      title: fallback.title,
    },
    [text(`[${index}]`)],
  )
}

/**
 * 代码块的**原文**（旧实现是复制时从 DOM 取 `pre.textContent`，并特意只取 `pre`——
 * 语言名在头部带里，不该被带进剪贴板）。
 *
 * 为什么要在渲染前存一份：`rehype-highlight` 会给代码补一个收尾换行
 * （highlight.js 的 `finalize`），而复制要的是**原文**。所以这个插件跑在
 * 高亮之前，把原文挂在节点的 `data` 上（`data` 不进 DOM，只给组件读）。
 * 用 `textContent` 而不是 `innerText`：要的是原文，不是渲染结果（受 `display` 影响、
 * 按排版归一空白）——旧实现里那句注释说的就是这件事。
 */
const CODE_TEXT_KEY = 'mdCodeText'

function rehypeCodeText() {
  return (tree: HastRoot): void => {
    walk(tree, (node) => {
      if (!isElement(node) || node.tagName !== 'pre') return
      const code = childrenOf(node).find(
        (child): child is HastElement => isElement(child) && child.tagName === 'code',
      )
      if (!code) return
      // 围栏里那段代码的**原文**（CommonMark 会把收尾换行也算进来，这里按旧实现的
      // "逐行 join" 口径去掉一个：`body.join('\n')` 本来就不会多出这一行）
      const raw = textOf(code).replace(/\n$/, '')
      trimCodeTail(code, raw)
      ;(code as unknown as { data?: Record<string, unknown> }).data = {
        ...((code as unknown as { data?: Record<string, unknown> }).data ?? {}),
        [CODE_TEXT_KEY]: raw,
      }
    })
  }
}

/** 把代码节点末尾多出来的那一行换行去掉（文本不一致时不动）。 */
function trimCodeTail(code: HastElement, raw: string): void {
  const children = childrenOf(code)
  const last = children.at(-1)
  if (!last || last.type !== 'text') return
  if (textOf(code) === raw) return
  if (last.value.endsWith('\n')) last.value = last.value.slice(0, -1)
}

/**
 * 高亮之后再收一次尾（`rehype-highlight` / highlight.js 的 `finalize` 也会补一个）。
 *
 * `<pre>` 里那个换行会渲染成末尾多出的一整行空白，而旧实现（自研解析器按行 `join`）
 * 没有这一行。判据始终是**存下来的原文**（见 `rehypeCodeText`）：
 * 文本已经等于原文就什么都不做。
 */
function rehypeCodeTail() {
  return (tree: HastRoot): void => {
    walk(tree, (node) => {
      if (!isElement(node) || node.tagName !== 'code') return
      const data = (node as unknown as { data?: Record<string, unknown> }).data
      const raw = data?.[CODE_TEXT_KEY]
      if (typeof raw !== 'string') return
      trimCodeTail(node, raw)
    })
  }
}

/* ------------------------------------------------------------------ 缓存 */

/**
 * 文本级缓存：**同一个 `(文本, 出处, plain)` 只解析一次**。
 *
 * 旧实现有两层（文本级 + 块级），块级那层是给"流式时每拍把已累积的全文重新解析一遍"
 * 准备的。现在**没有那一层了**：解析在 `react-markdown` 里面，我们没有"逐块解析"
 * 的插点（要就得自己接管 unified 流水线，那是另一件事）。旧注释里那份实测数字
 * 也说明它不该被当成热点优化——形状是 O(n²)，**绝对量是毫秒级**，分散在几百帧里。
 *
 * 键都是**内容本身**，所以同内容必然同输出：缓存只允许更快，不允许改变结果。
 * 缓存的 React 元素**不含任何回调闭包**（点击走 Context，见 `ActionsContext`），
 * 所以跨调用方复用是安全的。
 */
const ELEMENT_CACHE = new Map<string, ReactNode>()
/** 只防"聊一整天"把内存撑大，不是性能旋钮（淘汰策略见 `putCapped`）。 */
const ELEMENT_CACHE_LIMIT = 300

/**
 * 超过上限时淘汰**最旧的一条**，而不是清空整张表。
 *
 * 原先写的是"满了清空"。它的代价不在内存，在**下一次渲染**：清空之后
 * 紧接着的那次重渲染要把整条会话的所有消息重新解析一遍，表现成一个尖峰而不是
 * 平摊的开销。淘汰一条没有这个悬崖，代价只是命中率略低。
 * （实测差距在毫秒级、长会话那一组测不出来——这是一次正确性之外的"做得更像样"。）
 */
function putCapped(
  cache: Map<string, ReactNode>,
  limit: number,
  key: string,
  value: ReactNode,
): void {
  if (!cache.has(key) && cache.size >= limit) {
    // Map 的迭代顺序就是插入顺序：第一个即最旧的
    const oldest = cache.keys().next()
    if (!oldest.done) cache.delete(oldest.value)
  }
  cache.set(key, value)
}

/** 出处那一撮字段拼成缓存键的一部分（旧实现同款：序号 + 文件名 + 页码）。 */
function sourcesSignature(sources: readonly CitationSource[]): string {
  return sources
    .map((source) => `${source.index}\u0001${source.document_name}\u0001${source.page ?? ''}`)
    .join('\u0002')
}

/**
 * **网页引用也进缓存键**（D11-③）。
 *
 * 不加它的后果实测过：同一段正文先在"有联网引用"那一轮渲染成站点徽章，缓存住；
 * 另一轮同样文字但没有引用时命中同一棵树，于是**没有引用的那一轮也画出了 github.com**
 * （用例 `没给 citations（老调用方）` 就是这么红的）。文案要进键（上面那句），
 * 引用当然也要。
 */
function citationsSignature(citations?: ReadonlyMap<number, WebCitation>): string {
  if (!citations || citations.size === 0) return ''
  return [...citations.values()]
    .map((citation) => `${citation.index}\u0001${citation.url}\u0001${citation.domain}`)
    .join('\u0002')
}

/* ------------------------------------------------------------------ 组件 */

/**
 * 页面能挂上来的动作。
 *
 * **走 Context 而不是塞进 `components`**：`components` 必须是稳定引用，缓存才能命中；
 * 而回调是每次渲染新建的闭包。放 Context 里之后，缓存的那棵元素树与回调无关，
 * 两个目的同时成立（React 的惯用解法）。
 */
export interface MarkdownActions {
  /** 点了行内引用徽标（`[1]`）。旧实现是页面上的 `[data-cite-index]` 事件委托。 */
  onOpenSource?: (index: number) => void
  /**
   * **网页引用那一枚怎么画**（D11-③，可选）。给了它、并且这个编号在
   * `citations` 里，界面就可以把它渲染成站点徽章（真实 logo + 域名 + 悬停卡片）；
   * 不给就退回"有说明的非链接"。
   *
   * 为什么由调用方传渲染函数：`model/` 不认识界面组件（分层纪律），
   * 而"徽章长什么样"是界面的事。
   */
  renderWebCitation?: (citation: WebCitation, onOpen: (index: number) => void) => ReactNode
  /**
   * **正文里的普通外链怎么画**（2026-10-01 批四，可选）。
   *
   * 给了它，`href` 是外链（`SAFE_LINK` 那张判据）**且解析得出域名**的那些链接就交给它
   * 渲染——界面给的是同一副**来源胶囊**（`ui/SourceCard.tsx` 的 `LinkBadge`，悬停/聚焦
   * 出同一张卡片）。两条边界：
   *
   * - **`mailto:` 不走它**：那是"发信"不是"这一页"，胶囊上没域名可写（判据就是
   *   `hostOfUrl`，与徽章取域名同一份）；
   * - **不给就照旧 `a.md-link`**：文件预览、知识库那一族调用方零影响。
   *
   * 为什么普通外链也上胶囊（用户 2026-10-01 原话）："这个来源怎么回事。我之前不让
   * 做成按钮 hover 会变色的那种吗""包括 hover 上按钮的变色和 hover 出来的卡片样式。
   * 一模一样照抄"。改前这些链接是一行蓝字（`a.md-link`），与 `[n]` 那枚胶囊并列时
   * 是两种"来源"两种样子。裸网址（`rehypeBareUrls` 生成的 `a`）也流过 `MarkdownAnchor`，
   * 所以**同一条路**——两种写法不会有两个样子。
   */
  renderWebLink?: (url: string) => ReactNode
  /** 编号 → 网页引用（与 `CiteFallback.citations` 同一份，界面渲染徽章时要用）。 */
  citations?: ReadonlyMap<number, WebCitation>
  /**
   * 复制代码块。给的是**代码原文**（不含语言名——旧实现从 `.md-code` 里只取 `pre`，
   * 同一个口径）。不给回调时按钮照旧渲染（带 `data-copy-code`），页面可以用事件委托接。
   */
  onCopyCode?: (code: string) => void
  /** 复制表格：给的是**制表符分隔**的正文（粘进 Excel / 飞书会被拆成单元格）。 */
  onCopyTable?: (tsv: string) => void
  /** 下载表格：给的是表头与各行（BOM、CSV 转义、文件名归页面那一层）。 */
  onDownloadTable?: (table: MarkdownTable) => void
}

/** 一张表格的内容（复制 / 下载要用的那两份）。 */
export interface MarkdownTable {
  header: string[]
  rows: string[][]
}

const ActionsContext = createContext<MarkdownActions>({})

/**
 * 代码块与表格右上角那两个按钮的图标。
 *
 * 路径与仓库图标集里的 `copy` / `download` 同一份（免得"回答里的复制按钮"与
 * "消息上的复制按钮"画得不一样）。旧实现把 SVG 写成字符串（`v-html` 出来的节点里
 * 模板组件用不上），这里是 React 元素，不需要那层绕法。
 */
function CopyIcon() {
  return createElement(
    'svg',
    { viewBox: '0 0 24 24', width: 20, height: 20, fill: 'currentColor', 'aria-hidden': 'true' },
    createElement('path', {
      d: 'M7 6V3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1h-3v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1zm2 0h8a1 1 0 0 1 1 1v9h2V4H9zM5 8v10h10V8z',
    }),
  )
}

function DownloadIcon() {
  return createElement(
    'svg',
    { viewBox: '0 0 24 24', width: 20, height: 20, fill: 'currentColor', 'aria-hidden': 'true' },
    createElement('path', {
      d: 'M13 3v10.586l3.293-3.293 1.414 1.414L12 17.414 6.293 11.707 7.707 10.293 11 13.586V3zM5 19h14v2H5z',
    }),
  )
}

/** 组件从 react-markdown 拿到的那点东西（只写用到的）。 */
type NodeProps = ExtraProps
type PropsOf<Tag extends keyof JSX.IntrinsicElements> = JSX.IntrinsicElements[Tag] & NodeProps

type HastNodeProp = HastNode | undefined

function hastOf<Tag extends keyof JSX.IntrinsicElements>(
  props: PropsOf<Tag>,
): (HastElement & { data?: Record<string, unknown> }) | undefined {
  const node = props.node as unknown as HastNodeProp
  return node && isElement(node) ? (node as HastElement) : undefined
}

/** 标题：**保留原始层级，夹到 2–4 级**（旧实现同一条）。 */
const HEADING_CLASS: Record<string, number> = { h1: 2, h2: 3, h3: 4, h4: 4, h5: 4, h6: 4 }

function heading(level: number) {
  const tag = `h${level}` as 'h2' | 'h3' | 'h4'
  return function MarkdownHeading({ children }: PropsOf<'h2'>) {
    // 同时给 h 标签与类名：h 标签让浏览器/辅助技术知道层级，类名让样式能一致地管
    return createElement(tag, { className: `md-h md-h${level}` }, children as ReactNode)
  }
}

function MarkdownParagraph({ children }: PropsOf<'p'>) {
  return createElement('p', { className: 'md-p' }, children as ReactNode)
}

function MarkdownList({ children, ordered }: PropsOf<'ul'> & { ordered?: boolean }) {
  return createElement(
    ordered ? 'ol' : 'ul',
    { className: ordered ? 'md-ol' : 'md-ul' },
    children as ReactNode,
  )
}

function MarkdownBlockquote({ children }: PropsOf<'blockquote'>) {
  return createElement('blockquote', { className: 'md-quote' }, children as ReactNode)
}

function MarkdownHr() {
  return createElement('hr', { className: 'md-hr' })
}

/**
 * 链接与引用徽标。
 *
 * 全在这一层收口，因为两者都要"知道自己在哪"：徽标要能点（`onOpenSource`），
 * 外链要 `target`/`rel`（与裸链接那条规则**完全一致**——两处不能有两种开法）。
 *
 * 外链现在还有第二个出口（2026-10-01 批四）：界面注入了 `renderWebLink` 就交给它，
 * 画成与 `[n]` 同一副**来源胶囊**（`ui/SourceCard.tsx` 的 `LinkBadge`）；
 * 没注入（或这条链接没有域名可言）仍旧是 `a.md-link`。
 *
 * 手写的 `[文字](https://…)` 与裸网址**都到得了这里**：后者由 `rehypeBareUrls` 生成
 * 同样形状的 `a`（`className: ['md-link']`），所以两种写法只有一条路。
 */
function MarkdownAnchor(props: PropsOf<'a'>) {
  const actions = useContext(ActionsContext)
  const node = hastOf(props)
  const citeIndex = node?.properties['data-cite-index']
  if (citeIndex !== undefined) {
    const index = Number(citeIndex)
    // **只有真给了 `onOpenSource` 才挂处理函数**：没给的时候这一块要保持"纯标记"，
    // 好让页面沿用旧接法（在容器上监听 `[data-cite-index]`）时一个字节都不受影响
    const open = actions.onOpenSource
    // 网页引用那一枚（D11-③）：界面给了渲染函数就交给它（真实 logo + 域名 + 卡片）
    const citation = actions.citations?.get(index)
    if (node?.properties['data-cite-site-chip'] !== undefined && citation) {
      if (actions.renderWebCitation) {
        return actions.renderWebCitation(citation, open ?? (() => undefined))
      }
    }
    return createElement(
      'a',
      {
        className: 'md-cite',
        // 徽标本身没有 href（点了是"就地滑出抽屉"，不是跳走）；
        // `data-cite-index` 照旧给出去，页面沿用老的委托接法也行
        'data-cite-index': String(index),
        role: 'button',
        tabIndex: 0,
        title: node?.properties.title === undefined ? undefined : String(node.properties.title),
        onClick: open
          ? (event: { preventDefault: () => void; stopPropagation: () => void }) => {
              // 有回调就把这一下吃掉：页面若同时挂了委托，别处理两遍
              event.preventDefault()
              event.stopPropagation()
              open(index)
            }
          : undefined,
        onKeyDown: open
          ? (event: { key: string; preventDefault: () => void; stopPropagation: () => void }) => {
              // 键盘与鼠标走同一条路：徽标是 `role="button"`，Enter / 空格都得能用
              if (event.key !== 'Enter' && event.key !== ' ') return
              event.preventDefault()
              event.stopPropagation()
              open(index)
            }
          : undefined,
      },
      props.children as ReactNode,
    )
  }
  const href = node?.properties.href === undefined ? undefined : String(node.properties.href)
  /*
    普通外链这一档（2026-10-01 批四）。三个条件缺一不可：

    1. `SAFE_LINK`：与安全那一条**同一份判据**，不另写正则（`javascript:` 到不了这里——
       `rehypeUnwrapLinks` 已经把它整条退回字面量）；
    2. `hostOfUrl(href)` **解析得出域名**：胶囊上写的就是域名，`mailto:` 没有域名，
       硬上就是一枚空胶囊（与徽章取域名同一份判据）；
    3. `renderWebLink` 注入：没给就退回下面那条 `a.md-link`——文件预览（`plain`
       那一档根本不带 actions）与知识库等旧调用方一个字节都不受影响。
  */
  if (href && SAFE_LINK.test(href) && hostOfUrl(href) && actions.renderWebLink) {
    return actions.renderWebLink(href)
  }
  return createElement(
    'a',
    {
      className: 'md-link',
      href,
      target: '_blank',
      rel: 'noopener noreferrer',
    },
    props.children as ReactNode,
  )
}

/**
 * 代码块的两段式容器（旧 `codeBlockHtml`）。
 *
 * 头部带里的按钮由页面用**事件委托**接（`[data-copy-code]`），或者由 `Answer` 的
 * `onCopyCode` 接——两条路都在，`data-*` 一直在。头部**不参与复制**：
 * 复制的是代码原文，语言名不会被带进剪贴板。
 */
function MarkdownPre(props: PropsOf<'pre'>) {
  const actions = useContext(ActionsContext)
  const node = hastOf(props)
  const preRef = useRef<HTMLPreElement | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  const code = childrenOf(node ?? { type: 'root', children: [] }).find(
    (child): child is HastElement => isElement(child) && child.tagName === 'code',
  )
  const lang = (code ? classNameOf(code) : [])
    .map((name) => (/^language-(.+)$/.exec(name) ?? [])[1])
    .find((name) => Boolean(name))
  const codeText = String(
    (code as unknown as { data?: Record<string, unknown> })?.data?.[CODE_TEXT_KEY] ?? '',
  )
  /**
   * 这一块代码是不是**超过了限高**（D04，2026-09-28 走查）。
   *
   * 只量**不受限高影响**的那部分：`scrollHeight` 是内容自然高度，`maxHeight` 压的只是
   * 显示框，所以"超了"和"限高"互不干扰，可以反复量、量出来的判据始终是同一个。
   * **只有真的超出才给展开按钮**——每一块都挂一个永远用不上的按钮，比没有更烦人。
   */
  const measureOverflow = useCallback((element: HTMLElement | null): void => {
    if (!element) return
    setOverflowing(element.scrollHeight > CODE_MAX_HEIGHT_PX + 1)
  }, [])

  /**
   * 内容或**排版**一变就重新量。
   *
   * 为什么用 `ResizeObserver` 而不是"再挂几个依赖"：让这一块变高的，除了代码文本，
   * 还有字阶（根节点上的 `--font-scale`）与窗口宽度——它们改的是**折行**，不是
   * `codeText`，只依赖 `[codeText]` 就永远不重量（症状：调完字号，长代码块的展开键
   * 不出现、短代码块反倒挂着按钮，用户读不全代码）。而"字号/宽度变了"没有一条能订阅的
   * 回调：`--font-scale` 是写在 `<html>` 的 style 上的，改它不派发任何事件，
   * `matchMedia` 也不知道中栏容器被侧栏挤窄了多少。`ResizeObserver` 看的正是
   * **观察对象自己的盒子**——字号与宽度最终都会落到 `.md-pre` 的尺寸上，所以它一种机制
   * 就把两条路都盖住，且比"再叠一个 window resize 监听 / 轮询计算样式"更准也更省
   * （不变化时不回调）。
   *
   * 依赖里的 `codeText` 不是用来判断尺寸的，而是**换内容就换一个观察对象**：
   * 断开旧元素、重新观察新的那个，顺带立刻量一次（`ResizeObserver` 的首次回调
   * 只保证"观察之后"，不等它也能出结果）。卸载时同一段清理把观察断开。
   */
  useEffect(() => {
    const element = preRef.current
    measureOverflow(element)
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => measureOverflow(element))
    observer.observe(element)
    return () => observer.disconnect()
  }, [codeText, measureOverflow])
  return createElement(
    'div',
    { className: 'md-code' },
    createElement(
      'div',
      { className: 'md-code-head' },
      // 语言名与复制按钮都在**头部带**上（v0.25，照 Kimi 的对话页）：
      // 原先语言名是绝对定位在右上角的，代码一长就从它底下穿过去，
      // 像两样东西叠在一起；而且整块没有复制入口。
      lang ? createElement('span', { className: 'md-code-lang' }, lang) : createElement('span'),
      // 展开 / 收起（D04）：长代码原先一路把整屏撑开（`.md-pre` 只有 `overflow-x`），
      // 后面的话都被顶到几屏之外；现在先限高，想全看再展开。
      overflowing
        ? createElement(
            'button',
            {
              type: 'button',
              className: 'md-icon-btn',
              'data-toggle-code': true,
              'aria-expanded': expanded,
              'aria-label': expanded ? '收起代码' : '展开代码',
              title: expanded ? '收起' : '展开',
              onClick: (event: { stopPropagation: () => void }) => {
                event.stopPropagation()
                setExpanded((value) => !value)
              },
            },
            createElement(expanded ? ChevronUp : ChevronDown, { size: 13 }),
          )
        : null,
      createElement(
        'button',
        {
          type: 'button',
          className: 'md-icon-btn',
          'data-copy-code': true,
          'aria-label': '复制代码',
          title: '复制',
          onClick: actions.onCopyCode
            ? (event: { stopPropagation: () => void }) => {
                event.stopPropagation()
                actions.onCopyCode?.(codeText)
              }
            : undefined,
        },
        createElement(CopyIcon),
      ),
    ),
    createElement(
      'pre',
      {
        className: 'md-pre',
        ref: preRef,
        // 限高走**内联样式**而不是 CSS：那个数就是上面量出来的判据（`CODE_MAX_HEIGHT_PX`），
        // 写在两个地方迟早对不上。**只在"真的超了、而且没展开"时给**——
        // 短块不该带这个属性（HTML 保持干净，展开时同样清掉，交给页面滚动）。
        style: overflowing && !expanded ? { maxHeight: CODE_MAX_HEIGHT_PX } : undefined,
      },
      props.children as ReactNode,
    ),
  )
}

/**
 * 代码块的限高（px，D04）。
 *
 * 400 这个数取的是"大约 20 行等宽代码 + 内边距"：再高就开始把整屏占满（走查实测
 * 长代码块把后面的正文顶到几屏之外），再低则常见的一个函数都看不完。
 */
const CODE_MAX_HEIGHT_PX = 400

/** 表格内容（复制 / 下载要的那两份，从 hast 上取，不碰 DOM）。 */
function tableOf(node: HastElement | undefined): MarkdownTable {
  const table: MarkdownTable = { header: [], rows: [] }
  if (!node) return table
  walk(node, (child) => {
    if (!isElement(child)) return
    if (child.tagName === 'th') table.header.push(cellText(child))
    if (child.tagName === 'td') {
      const row = table.rows.at(-1)
      if (row) row.push(cellText(child))
    }
    if (child.tagName === 'tr') table.rows.push([])
  })
  // 表头那一行走的是 `<th>`，它也会被 `tr` 建一行空数组——去掉它
  if (table.rows.length && table.rows[0].length === 0) table.rows.shift()
  return table
}

function cellText(cell: HastElement): string {
  return textOf(cell).replace(/\s+/g, ' ').trim()
}

/** 表格进剪贴板用**制表符分隔**而不是 CSV：粘进 Excel / 飞书表格时会被直接拆成单元格。 */
function tableToTsv(table: MarkdownTable): string {
  return [table.header, ...table.rows].map((row) => row.join('\t')).join('\n')
}

/**
 * 表格：带复制与下载的块（旧 `tableBlockHtml`）。
 *
 * 外层套一个可横向滚动的容器：宽表格在窄列里必须能滚，否则会把整页撑破。
 */
function MarkdownTableBlock(props: PropsOf<'table'>) {
  const actions = useContext(ActionsContext)
  const node = hastOf(props)
  const data = useMemo(() => tableOf(node), [node])
  return createElement(
    'div',
    { className: 'md-table-block' },
    createElement(
      'div',
      { className: 'md-code-head' },
      createElement('span', { className: 'md-code-lang' }, '表格'),
      createElement(
        'button',
        {
          type: 'button',
          className: 'md-icon-btn',
          'data-copy-table': true,
          'aria-label': '复制表格',
          title: '复制',
          onClick: actions.onCopyTable
            ? (event: { stopPropagation: () => void }) => {
                event.stopPropagation()
                actions.onCopyTable?.(tableToTsv(data))
              }
            : undefined,
        },
        createElement(CopyIcon),
      ),
      createElement(
        'button',
        {
          type: 'button',
          className: 'md-icon-btn',
          'data-download-table': true,
          'aria-label': '下载表格',
          title: '下载 CSV',
          onClick: actions.onDownloadTable
            ? (event: { stopPropagation: () => void }) => {
                event.stopPropagation()
                actions.onDownloadTable?.(data)
              }
            : undefined,
        },
        createElement(DownloadIcon),
      ),
    ),
    createElement(
      'div',
      { className: 'md-table-wrap' },
      createElement('table', { className: 'md-table' }, props.children as ReactNode),
    ),
  )
}

/** 带按钮的那一套（对话页）。 */
const COMPONENTS = {
  p: MarkdownParagraph,
  h1: heading(HEADING_CLASS.h1),
  h2: heading(HEADING_CLASS.h2),
  h3: heading(HEADING_CLASS.h3),
  h4: heading(HEADING_CLASS.h4),
  h5: heading(HEADING_CLASS.h5),
  h6: heading(HEADING_CLASS.h6),
  ul: MarkdownList,
  ol: (props: PropsOf<'ol'>) => MarkdownList({ ...props, ordered: true }),
  blockquote: MarkdownBlockquote,
  hr: MarkdownHr,
  a: MarkdownAnchor,
  pre: MarkdownPre,
  table: MarkdownTableBlock,
}

/**
 * 只读那一档的标题：**保留原文层级**（`#` 就是 `<h1>`，不夹到 2–4 级）。
 *
 * 对话页把层级夹到 2–4 级（见 `HEADING_CLASS`）是因为那一份是**模型的回复**：
 * 口语、行短，`#` 出现在回答里多半只是个"小节"，h1 太吵。
 * 而 `renderPlainMarkdown` 渲染的是**一份文档**：`#` 就是这篇文档的最外层标题，
 * 降一档会让层级与原文对不上；落到预览的样式上还会从 `.kylab-md h1` 的 page-title
 * 字号掉到 section 字号（换渲染器时实测到的回退，见 `preview/FilePreview.tsx` 的头注）。
 *
 * 类名照旧给 `md-h md-hN`：样式钩子与对话页一致，变的只是 h 标签的层级。
 */
function plainHeading(level: 1 | 2 | 3 | 4 | 5 | 6) {
  const tag = `h${level}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
  return function PlainHeading({ children }: PropsOf<'h1'>) {
    return createElement(tag, { className: `md-h md-h${level}` }, children as ReactNode)
  }
}

/**
 * ``plain``：**只读**的那一套（文件预览用）。
 *
 * 对话里的答案要能复制代码、下载表格，那两个按钮的点击由页面接住；
 * 而文件预览里没有那条委托（也不该有——预览是"看"，不是"操作这份内容"）。
 * 所以同一套解析、两种收尾：``plain`` 只出内容，不出按钮。
 *
 * 与富文本档的另外两处差别也在这一档里：标题**不夹层**（见 `plainHeading`）、
 * 段落里的单换行**不换成 `<br>`**（见 `renderMarkdown` 的插件表）——预览里那份是文档。
 */
const PLAIN_COMPONENTS = {
  ...COMPONENTS,
  h1: plainHeading(1),
  h2: plainHeading(2),
  h3: plainHeading(3),
  h4: plainHeading(4),
  h5: plainHeading(5),
  h6: plainHeading(6),
  pre: ({ children }: PropsOf<'pre'>) =>
    createElement('pre', { className: 'md-pre' }, children as ReactNode),
  table: ({ children }: PropsOf<'table'>) =>
    createElement('table', { className: 'md-table' }, children as ReactNode),
}

/** 渲染参数（这一层只认"要不要徽标、要不要按钮、是不是文档那一档"）。 */
interface RenderOptions {
  sources?: readonly CitationSource[]
  plain?: boolean
  /** 对不上的编号怎么画（给了才画成"有说明的非链接"）。 */
  fallback?: CiteFallback
}

function renderMarkdown(text: string, options: RenderOptions = {}): ReactNode {
  if (!text) return null
  const sources = options.sources ?? []
  const plain = options.plain === true
  const fallback = options.fallback
  // 说明文案进缓存键：同一段正文在两轮里（一轮有联网、一轮没有）输出不同
  const key = `${text}\u0000${sourcesSignature(sources)}\u0000${plain ? 'plain' : 'rich'}\u0000${
    fallback?.title ?? ''
  }\u0000${citationsSignature(fallback?.citations)}`
  const cached = ELEMENT_CACHE.get(key)
  if (cached !== undefined) return cached

  const rehypePlugins: NonNullable<Options['rehypePlugins']> = [
    rehypeUnwrapLinks,
    rehypeCodeText,
    // 软换行只在**富文本那一档**：模型回复的换行是有意断行；
    // 文档预览里那是一个空格（CommonMark 口径），见 `rehypeSoftBreaks` 的说明
    ...(plain ? [] : [rehypeSoftBreaks]),
    rehypeTrimBlocks,
    rehypeBareUrls,
  ]
  // 出处那一步要参数，所以单独推：只在**有出处、或有兜底说明**时才挂
  // （两样都没有就不再扫一遍文本——扫描是逐文本节点的，没必要白跑）
  if (sources.length || fallback) {
    const citations: [
      typeof rehypeCitations,
      { sources: readonly CitationSource[]; fallback?: CiteFallback },
    ] = [rehypeCitations, { sources, fallback }]
    rehypePlugins.push(citations)
  }
  rehypePlugins.push(
    rehypeHighlight,
    // 高亮之后：把它补的那个收尾换行撤掉（复制用的是之前存下的原文，见 `rehypeCodeText`）
    rehypeCodeTail,
    // KaTeX **放最后**：它把公式换成一大片 span，后面的文本级规则就不该再进那片了
    // （几个扫描函数都跳过 `math`，顺序上再兜一层）。
    // `strict: 'ignore'` 与 `latex.ts` 的 `renderLatexToHtml` 同口径：语料里
    // `$5 到 $10` 这类"被当成公式的钱"一定会碰上，警告刷满控制台没有意义；
    // 真排不出来时 KaTeX 自己会把原文画成 `katex-error`（可读、不假装渲染成功）
    rehypeMathSpacing,
    [rehypeKatex, { strict: 'ignore' }],
    // **必须排在 KaTeX 之后**：它刚生成的那一大片 span 才是有 style 的那些；
    // 顺序反了就没有可转的东西（见下面那个插件自己的说明）
    rehypeStyleObjects,
  )
  const rendered = createElement(ReactMarkdown, {
    children: text,
    // GFM 负责表格 / 删除线 / 任务列表这几样"旧实现不认、但语料里真有"的语法；
    // 它的 autolink 会被 `rehypeUnwrapLinks` 退回去，见那里的说明。
    // `remarkMath` 认 `$…$` / `$$…$$`；紧随其后的 `remarkMathSpacing` 把不合
    // GitHub 口径的（`$` 与内容之间有空格）退回普通文本——见那个插件的注释。
    // 完整口径与差异记在 `model/README.md`。
    remarkPlugins: [remarkGfm, remarkMath],
    rehypePlugins,
    components: (plain ? PLAIN_COMPONENTS : COMPONENTS) as Options['components'],
  })

  putCapped(ELEMENT_CACHE, ELEMENT_CACHE_LIMIT, key, rendered)
  return rendered
}

/**
 * 行内公式的**边界**：只认 GitHub 口径的那种写法——`$` 与内容之间**不留空格**。
 *
 * 为什么要有这一层：`remark-math` 默认只要求"配对"，于是"价格区间"这类文本也被排成公式
 * （`$5 到 $10` 的收尾 `$` 前是空格，照排）。而 **GitHub 的实现要求 `$` 紧挨内容**
 * （`$x$` 排、`$ x $` 与 `$5 到 $10` 都不排）——这是主流口径，用一条边界规则说得清楚。
 *
 * **为什么在 hast 这一步做**：先试过在 mdast 上（`inlineMath` 节点）改，但转换那一步
 * 拿到的仍是没改过的那棵树（实测：插件跑完树里 0 个 `inlineMath`，产物里照样有 KaTeX）。
 * 这里直接换掉 `remark-math` 生成的那个 `span.math-inline`：rehype-katex 认的是
 * **这个类的元素**，它跑到时元素已经变成文本，自然不排。`$$…$$` 的 display 公式不受影响。
 */
function rehypeMathSpacing() {
  return (tree: HastRoot): void => {
    walk(tree, (node, parents) => {
      if (!isElement(node)) return
      const classes = classNameOf(node)
      // `remark-math` 的产物：**`<code class="language-math math-inline">`**
      // （实测——不是 `span.math-inline`，这一点没有文档，写错就永远不命中）。
      // 两种形状都认，免得上游哪天改回 span。
      const isInlineMath =
        (node.tagName === 'code' && classes.includes('language-math')) ||
        (node.tagName === 'span' && classes.includes('math-inline'))
      if (!isInlineMath || classes.includes('math-display')) return
      // 元素里装的是**公式原文**（不含两侧 `$`，尾随空格保留）；兜底再剥一次 `$`
      const inner = textOf(node).replace(/^\$+/, '').replace(/\$+$/, '')
      // GitHub 口径：`$` 与内容之间不留空格——留了就不是公式（`$5 到 $` / `$ x $`）
      if (!/^\s/.test(inner) && !/\s$/.test(inner)) return
      // 退回普通文本：把 `$` 补回去，原文照旧，一个字符都不改
      const parent = parents[parents.length - 2]
      if (!parent) return
      const siblings = childrenOf(parent)
      const index = siblings.indexOf(node)
      if (index >= 0) siblings[index] = text(`$${inner}$`)
    })
  }
}

/**
 * 把 `properties.style` 从**字符串**换成对象（D01，2026-09-28 走查）。
 *
 * ## 为什么必须换
 *
 * `hast-util-to-jsx-runtime` 处理 style 的那一行是
 * `typeof value === 'object' ? value : parseStyle(state, String(value))`，
 * 而 `parseStyle` 里调的是 `styleToJs(value, {reactCompat: true})` —— 但
 * `style-to-js@1.0.0` 是纯 CJS 且只 `exports.default = StyleToJS`：
 * ESM 那边 `import styleToJs from 'style-to-js'` 拿到的是**命名空间对象**，
 * 一调就 `TypeError`，紧接着被 `ignoreInvalidStyle: true` **静默吞成 `{}`**。
 * 后果是 KaTeX 的内联 style **全丢**：实测 `.katex-html` 里 19 个节点 0 个 style、
 * `.vlist` 计算高度 0、指数掉到下一行、`∫₀¹x²dx=⅓` 散成一串碎片。
 *
 * ## 为什么修在这里（而不是打包器那侧）
 *
 * 试过 `optimizeDeps.needsInterop: ['style-to-js']`：**没用**——`style-to-js` 被**内联**进了
 * react-markdown 的预打包产物（`.vite/deps/react-markdown.js` 里那段 `style-to-js@1.0.0`
 * 就是它），单独的互操作设置够不到它。而"先把 style 转成对象"走的是**另一支**：
 * 对象原样透传，与 bundler 怎么判 CJS/ESM 无关——dev 与生产构建都稳。
 *
 * ## 转法照抄上游（不引它的包）
 *
 * `style-to-js` 的实现本身就三步：`style-to-object` 解串 + `camelCase` 属性名
 * （它那个 `reactCompat` 参数在 1.0.0 里**根本没被用**，函数签名只有 `style`）。
 * 所以这里用同一个解析器 `style-to-object`（已是依赖）+ 一份与它 `cjs/utilities.js`
 * **逐条对齐**的 `camelCase`，语义与"它没坏的时候"完全一致。
 *
 * 顺序上**必须排在 `rehypeKatex` 之后**：那段 HTML 是它刚生成的。
 */
function rehypeStyleObjects() {
  return (tree: HastRoot): void => {
    walk(tree, (node) => {
      if (!isElement(node)) return
      const raw = node.properties.style
      if (typeof raw !== 'string') return
      const style: Record<string, string> = {}
      styleToObject(raw, (property, value) => {
        // 与 `style-to-js` 同一条：属性或值缺一个就丢这一条
        if (property && value) style[camelCaseStyle(property)] = value
      })
      node.properties.style = style
    })
  }
}

/**
 * CSS 属性名 → React 的驼峰名。**与 `style-to-js/cjs/utilities.js` 的 `camelCase` 逐条对齐**：
 * 没有连字符的原样返回、`--x` 自定义属性原样返回（React 也认这种）、
 * 厂商前缀 `-webkit-` 只把前缀后的第一个连字符去掉（`-webkit-line-clamp` → `WebkitLineClamp` 那种口径）。
 */
function camelCaseStyle(property: string): string {
  if (!property || !property.includes('-') || /^--[a-zA-Z0-9-]+$/.test(property)) return property
  return property
    .toLowerCase()
    .replace(/^-(webkit|moz|ms|o)-/, (_, prefix: string) => `${prefix}-`)
    .replace(/-([a-z])/g, (_, character: string) => character.toUpperCase())
}

/** 把动作挂进 Context（只有真有回调时才包一层 Provider）。 */ function withActions(
  content: ReactNode,
  actions: MarkdownActions | undefined,
): ReactNode {
  if (!actions) return content
  return createElement(ActionsContext.Provider, { value: actions }, content)
}

/**
 * 把一段 Markdown 渲染成**只读**的结果（文件预览用）。
 *
 * 与 `renderAnswerMarkdown` 同一套解析与安全管线，三处按"文档"而不是"回复"来：
 *
 * 1. **不挂复制 / 下载按钮**——那些按钮的点击由对话页接住，而预览里没有那条委托，
 *    按钮会变成"点了没反应"的假控件（见 `PLAIN_COMPONENTS`）；
 * 2. **标题不夹层**：`#` 出 `<h1>`，按原文层级（见 `plainHeading`）；
 * 3. **单换行不换 `<br>`**：文档里的软换行就是 CommonMark 说的那个空格。
 *
 * 安全那几条一个字不差：HTML 不解析、非白名单协议退回原文、KaTeX 与代码高亮都照旧。
 * 不走缓存：它用在文件预览里，一次只渲染一份，没有"每个 tick 重算历史"那种模式。
 */
export function renderPlainMarkdown(text: string): ReactNode {
  if (!text) return null
  return renderMarkdown(text, { plain: true })
}

export function renderAnswerMarkdown(text: string, actions?: MarkdownActions): ReactNode {
  return withActions(renderMarkdown(text), actions)
}

/**
 * 把 `[N]` 换成可点的引用徽标后再渲染（`sources` 为空时退回普通渲染）。
 *
 * 徽标的点击有两条路（二选一，别同时用）：`actions.onOpenSource`，或者页面在容器上
 * 监听 `[data-cite-index]`（旧实现的接法，`data-*` 一直保留）。
 */
export function renderAnswerWithCitations(
  text: string,
  sources: readonly CitationSource[],
  actions?: MarkdownActions,
): ReactNode {
  if (!text || sources.length === 0) return renderAnswerMarkdown(text, actions)
  return withActions(renderMarkdown(text, { sources }), actions)
}

export interface AnswerProps extends MarkdownActions {
  text: string
  /** 出处：给了就渲染 `[N]` 徽标。 */
  sources?: readonly CitationSource[]
  /**
   * 对不上的编号怎么画（v0.28）。给了它就渲染成**有说明的非链接**，
   * 没给就照旧原样留在正文里——调用方只在**真的知道那些编号从哪来**时才给
   * （对话页：这一轮确实跑过联网搜索，过程面板里列着那几次的返回）。
   */
  citeFallback?: CiteFallback
  /** 只读（文件预览）：不挂复制 / 下载按钮。 */
  plain?: boolean
  /** 挂在最外层容器上的类名（对话页用它接自己的排版）。给了才包一层 `div`。 */
  className?: string
}

/**
 * `<Answer>`：**推荐的用法**（对话页直接摆这一个）。
 *
 * 与三个函数是同一套东西，只是把动作接得更顺（不用自己包 Provider）。
 * `className` 给了才包一层 `div`——没给就是 Fragment，免得在调用方自己的
 * 容器（旧的 `.reply-text`）里凭空多一层，把 `> p` 这种选择器隔断。
 */
export function Answer({
  text,
  sources = [],
  citeFallback,
  plain = false,
  className,
  onOpenSource,
  renderWebCitation,
  renderWebLink,
  onCopyCode,
  onCopyTable,
  onDownloadTable,
}: AnswerProps): ReactNode {
  // 回调每次都可能是新的闭包，这里按**回调本身**记忆，免得 Provider 的 value 每次渲染都换
  const citations = citeFallback?.citations
  const actions = useMemo<MarkdownActions>(
    () => ({
      onOpenSource,
      renderWebCitation,
      renderWebLink,
      citations,
      onCopyCode,
      onCopyTable,
      onDownloadTable,
    }),
    [
      onOpenSource,
      renderWebCitation,
      renderWebLink,
      citations,
      onCopyCode,
      onCopyTable,
      onDownloadTable,
    ],
  )
  const content = renderMarkdown(text, { sources, plain, fallback: citeFallback })
  if (!content) return null
  const filled = createElement(ActionsContext.Provider, { value: actions }, content)
  if (!className) return filled
  return createElement('div', { className }, filled)
}

/**
 * `Answer` 的别名：**给同时开工的界面那一层固定一个名字**。
 *
 * 界面（`features/chat/ui/AnswerText.tsx`）用命名空间导入 + 运行时取键来取这个组件，
 * 候选名里有 `AnswerMarkdown`——这里显式导出它，省得那边在"叫什么"上猜。
 */
export const AnswerMarkdown = Answer
