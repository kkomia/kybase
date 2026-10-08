/**
 * Markdown 渲染（Wiki 正文、文档「阅读」视角、命中引文共用一份）。
 *
 * 与旧前端 `composables/useMarkdown.ts` 的**规则**对齐，实现换成 react-markdown + remark-gfm
 * （迁移计划 §2：实现换库、规则作为对照）：
 *
 * - 整段先当作纯文本处理，只有解析器认识的标记会被还原，所以正文里带 HTML 也不会被注入；
 * - 编号式引用 `[n]` 变成**可点的徽标**（与出处列表一一对应），`n` 不在出处表里时原样留着
 *   ——给一个点了没反应的死徽标比留着 `[12]` 更糟；
 * - 站内双链 `[[标题]]` 只在**标题对得上**时建链（同理：写错字的双链原样留着更好读）；
 * - 代码块 / 行内代码里的 `[1]` 与 `[[x]]` **不替换**——那里的方括号是代码，不是引用。
 *
 * 实现手法：先把要变成控件的片段换成哨兵（`%%cite:1%%` / `%%wiki:标题%%`），
 * 再在段落级渲染里把字符串子节点按哨兵切开换成按钮。比自定义 remark 插件短得多，
 * 也避免在 AST 层跟别人的数据结构打交道。
 */
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

export interface Citation {
  index: number
  /** 徽标上显示的文件名（旧口径：过长的再被 CSS 截断）。 */
  document_name: string
}

interface MarkdownProps {
  text: string
  /** 出处表：正文里的 `[n]` 按它变成可点徽标。 */
  citations?: readonly Citation[]
  onCite?: (index: number) => void
  /** 站内双链：标题 → 页面 id。只有对得上的标题才建链。 */
  wikiLinks?: { [title: string]: string }
  onWikiPage?: (pageId: string) => void
}

const CITE_RE = /%%cite:(\d+)%%/g
const WIKI_RE = /%%wiki:([^%]*)%%/g

/** 代码区（围栏块与行内代码）：这些片段一律不动。 */
const CODE_RE = /```[\s\S]*?```|`[^`]*`|~~~[\s\S]*?~~~/g

function escapeRe(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 把正文里的 `[n]` 与 `[[标题]]` 换成哨兵。
 *
 * 逐段（代码区之外）处理：`CODE_RE` 切出来的代码片段原样拼回去。
 */
export function decorate(
  text: string,
  citations: readonly Citation[] = [],
  wikiLinks: { [title: string]: string } = {},
): string {
  const indexes = new Set(citations.map((item) => item.index))
  const titles = Object.keys(wikiLinks)

  const decorateSegment = (segment: string): string => {
    let result = segment
    if (indexes.size > 0 && result.includes('[')) {
      result = result.replace(/\[(\d+)\]/g, (match, raw: string) =>
        indexes.has(Number(raw)) ? `%%cite:${raw}%%` : match,
      )
    }
    for (const title of titles) {
      if (!result.includes('[[')) break
      result = result.replace(
        new RegExp(`\\[\\[\\s*${escapeRe(title)}\\s*\\]\\]`, 'g'),
        `%%wiki:${title}%%`,
      )
    }
    return result
  }

  let out = ''
  let cursor = 0
  CODE_RE.lastIndex = 0
  for (let match = CODE_RE.exec(text); match; match = CODE_RE.exec(text)) {
    out += decorateSegment(text.slice(cursor, match.index)) + match[0]
    cursor = match.index + match[0].length
  }
  return out + decorateSegment(text.slice(cursor))
}

/** 段落级子节点 → 带控件的一串节点。 */
function renderInline(
  children: React.ReactNode,
  citations: readonly Citation[],
  onCite: ((index: number) => void) | undefined,
  wikiLinks: { [title: string]: string },
  onWikiPage: ((pageId: string) => void) | undefined,
): React.ReactNode {
  const byIndex = new Map(citations.map((item) => [item.index, item]))
  const renderString = (text: string, keyPrefix: string): React.ReactNode[] => {
    const parts: React.ReactNode[] = []
    const regex = new RegExp(`${CITE_RE.source}|${WIKI_RE.source}`, 'g')
    let cursor = 0
    let match = regex.exec(text)
    while (match) {
      if (match.index > cursor) parts.push(text.slice(cursor, match.index))
      const citeIndex = match[1] !== undefined ? Number(match[1]) : null
      const title = match[2]
      const key = `${keyPrefix}-${match.index}`
      if (citeIndex !== null && byIndex.has(citeIndex)) {
        const citation = byIndex.get(citeIndex)!
        parts.push(
          <button
            key={key}
            type="button"
            className="kb-cite"
            data-cite-index={citeIndex}
            title={`${citation.document_name}（第 ${citeIndex} 条出处）`}
            onClick={() => onCite?.(citeIndex)}
          >
            <span className="kb-cite-name">{citation.document_name}</span>
          </button>,
        )
      } else if (title !== undefined && wikiLinks[title]) {
        const pageId = wikiLinks[title]
        parts.push(
          <button
            key={key}
            type="button"
            className="kb-wikilink"
            data-wiki-page={pageId}
            onClick={() => onWikiPage?.(pageId)}
          >
            {title}
          </button>,
        )
      } else {
        parts.push(match[0])
      }
      cursor = match.index + match[0].length
      match = regex.exec(text)
    }
    if (cursor < text.length) parts.push(text.slice(cursor))
    return parts
  }

  const walk = (node: React.ReactNode, keyPrefix: string): React.ReactNode => {
    if (typeof node === 'string') return renderString(node, keyPrefix)
    if (Array.isArray(node)) return node.map((item, index) => walk(item, `${keyPrefix}.${index}`))
    return node
  }
  return walk(children, 'i')
}

export function Markdown({
  text,
  citations = [],
  onCite,
  wikiLinks = {},
  onWikiPage,
}: MarkdownProps) {
  const source = decorate(text, citations, wikiLinks)

  /** 段落级块：子节点里可能有哨兵，统一过一遍 `renderInline`。 */
  const inline = (children: React.ReactNode): React.ReactNode =>
    renderInline(children, citations, onCite, wikiLinks, onWikiPage)

  const components: Components = {
    p: ({ children }) => <p className="kb-md-p">{inline(children)}</p>,
    li: ({ children }) => <li className="kb-md-li">{inline(children)}</li>,
    td: ({ children }) => <td className="kb-md-td">{inline(children)}</td>,
    th: ({ children }) => <th className="kb-md-th">{inline(children)}</th>,
    h1: ({ children }) => <h2 className="kb-md-h">{inline(children)}</h2>,
    h2: ({ children }) => <h2 className="kb-md-h">{children}</h2>,
    h3: ({ children }) => <h3 className="kb-md-h">{children}</h3>,
    h4: ({ children }) => <h4 className="kb-md-h">{children}</h4>,
    ul: ({ children }) => <ul className="kb-md-list">{children}</ul>,
    ol: ({ children }) => <ol className="kb-md-list">{children}</ol>,
    blockquote: ({ children }) => <blockquote className="kb-md-quote">{children}</blockquote>,
    pre: ({ children }) => <pre className="kb-md-pre">{children}</pre>,
    hr: () => <hr className="kb-md-hr" />,
    a: ({ href, children }) => (
      <a className="kb-md-link" href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    ),
    table: ({ children }) => (
      <div className="kb-md-table-wrap">
        <table className="kb-md-table">{children}</table>
      </div>
    ),
  }

  return (
    <div className="kb-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  )
}
