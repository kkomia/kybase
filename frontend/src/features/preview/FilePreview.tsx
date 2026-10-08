/**
 * 按格式选渲染器的文件预览（React 版，v0.26 那张表的对照实现）。
 *
 * 分派规则**不在这个文件里**，在 `./kinds.ts`——那里是唯一一份表，界面与测试都对着它。
 * 这里只做三件事：把规则的结果接到具体组件上、给"取不到内容"一个统一的失败态、
 * 把文本类的内容取回来。
 *
 * 与旧 `frontend/src/components/files/FilePreview.vue` 的对照（逐条）：
 *
 * | 后缀 / kind | 旧实现 | 这里 |
 * | --- | --- | --- |
 * | `md` / `markdown` | `renderPlainMarkdown` | 同一份 `renderPlainMarkdown`（只读那一档） |
 * | `txt` / `log` / `csv` / 各种代码 | `<pre>` | `<pre class="kylab-text">` |
 * | `png` / `jpg` / `gif` / `webp` / `bmp` / `avif` | `<img>` | `<img>` |
 * | `pdf` | `<iframe>` 指向签名链接 | `<iframe>` 指向签名链接（挂之前先探一次，见 `PdfPane`） |
 * | `docx` | `OfficePreview` + docx-preview | `DocxPreview` |
 * | `pptx` | `OfficePreview` + `@vue-office/pptx` | `PptxPreview` |
 * | `xlsx` / `xls` | `OfficePreview` + `@vue-office/excel` | `SpreadsheetPreview`（exceljs） |
 * | 其它 | 一句"下载它" | `PreviewNotSupported` |
 *
 * **两处刻意的差别**（都写在 `kinds.ts` 的头注里）：SVG 仍不进图片那一档
 * （它能在本站 origin 下执行脚本，服务端也不给它 `inline`）；`csv` 的落点取决于
 * 调用方给的是后缀还是后端 kind——两份输入本来就不一样。
 *
 * 一条安全说明：Markdown 走 `features/chat/model/markdown.tsx` 的
 * `renderPlainMarkdown`（"只读"那一档，**按需 import**，见 `PlainMarkdown`），
 * **不开 `rehype-raw`**，所以文档里的 HTML 不会被当标记执行
 * （与旧前端"先整体转义、再白名单还原"同一个结果）。
 */
import { useEffect, useState, type ReactNode } from 'react'

import type { DocumentPreview } from '@/api/documents'

import { DocxPreview } from './DocxPreview'
import { PptxPreview } from './PptxPreview'
import { SpreadsheetPreview } from './SpreadsheetPreview'
import { resolveRenderer } from './kinds'
import {
  PreviewLoading,
  PreviewNote,
  PreviewNotSupported,
  PreviewUnavailable,
  loadFailure,
} from './notes'
import { NO_URL, useContentProbe, useRemoteText } from './usePreviewSource'
import './preview.css'

export interface FilePreviewProps {
  /** 文件名（含扩展名）。分派与报错文案都用它；给 `preview` 时可以省。 */
  name?: string | null
  /**
   * 种类：后端的 `PreviewKind`（`docx` / `excel` / `binary`…）或文件后缀
   * （`md` / `png`…，会话文件区给的就是后缀）。缺省时按 `mime` 与文件名后缀判。
   */
  kind?: string | null
  /** 媒体类型：只在前两者都认不出来时兜底。 */
  mime?: string | null
  /** 后端签发的签名链接。图片/PDF 直接用它，Office 用它取字节。 */
  url?: string | null
  /** 文本类**已经内联**的内容（文档接口的阅读视角直接回 `text`，不必再取一次）。 */
  text?: string | null
  /** 取不到内容时上层给出的原因；没有时用"拿不到预览链接"。 */
  reason?: string | null
  /**
   * 文档接口「阅读视角」的整份返回（`getDocumentPreview`）。
   *
   * 给了它就按它取名字/种类/链接/文本——**文档页不必把四个字段拆开来传**，
   * 而它本来就是"这一页要渲染什么"的完整回答（`kind` / `url` / `text` / `filename`）。
   * 显式传的 `name` / `kind` / `url` / `text` 优先于它（调用方点名要的算数）。
   *
   * 不需要另外传 `documentId`：签名链接就在这份返回里（`url`）。
   */
  preview?: DocumentPreview | null
  /**
   * PDF 的跳页锚点（1-based）。
   *
   * `#page=N` 是浏览器内置阅读器的 PDF Open Parameters，**零依赖**就能从引用直接
   * 落到那一页；`#` 之后是片段，不会发给服务端（旧 `DocumentDrawer.vue` 的做法）。
   */
  page?: number | null
}

/** 空链接的统一说明：区分"后端没给"与"给了但取不到"（后者由各自的 hook 报）。 */
function missingReason(reason?: string | null): string {
  return reason?.trim() ? reason.trim() : NO_URL
}

/**
 * 把只读渲染器**按需**拉进来。
 *
 * 为什么不像最初那样写 `import { renderPlainMarkdown } from '…/markdown'`：
 * 那一份渲染器身后是一整串重依赖（react-markdown + remark-gfm/math + rehype-katex/
 * highlight + KaTeX），**实测 451 kB（gzip 139 kB）**。静态 import 会把这些塞进
 * `FilePreview` 所在的**共享预览 chunk**——而那个 chunk 是"打开任何一份文件预览"
 * 都要加载的：知识库/文档页看一个 pdf、图片、docx 也不例外。实测那份 chunk 因此
 * 从 **41.35 kB（gzip 13.49）涨到 492.46 kB（gzip 152.97）**，全是给 Markdown 付的钱。
 *
 * 动态 import 之后，只有**真的要看 Markdown** 的那一次才付这趟下载（对话页本来就
 * 静态带着这份渲染器，所以那边一行没多）；代价是首帧要等一次模块解析——那段时间
 * 走本文件统一的加载态，不白屏。
 *
 * `setRenderer(() => module.renderPlainMarkdown)` 用函数式更新存函数：
 * 直接 `setRenderer(module.renderPlainMarkdown)` 会被 React 当成"更新函数"调用。
 */
function PlainMarkdown({ text }: { text: string }) {
  const [renderer, setRenderer] = useState<((value: string) => ReactNode) | null>(null)

  useEffect(() => {
    let alive = true
    void import('@/features/chat/model/markdown').then((module) => {
      if (alive) setRenderer(() => module.renderPlainMarkdown)
    })
    return () => {
      alive = false
    }
  }, [])

  if (!renderer) return <PreviewLoading />
  return renderer(text)
}

/**
 * Markdown：**用对话页同一份渲染器的只读档**（`renderPlainMarkdown`），HTML 不解析。
 *
 * 为什么不再自己搭一个 `ReactMarkdown`：这个文件的对照表一直写着 md 走
 * `renderPlainMarkdown`，实现却自成一份——注释与实现不符只是表面症状，真正的问题是
 * **解析口径有了两份**：自己那一份没有公式（`$…$` 原样显示）、没有代码高亮，
 * 危险协议的链接也被 react-markdown 的默认清洗抹成空 `href`（看不见原文）。
 * 合成一份之后，安全规则（HTML 不当标记、非白名单协议退回原文）、GFM 表格、
 * KaTeX 公式、highlight.js 高亮都跟着对话页走，改一处两边同时生效。
 *
 * 只读档**不挂复制 / 下载按钮**——预览是"看"，不是"操作这份内容"，
 * 与那个渲染器自己的注释同一条（`model/markdown.tsx` 的 `PLAIN_COMPONENTS`）。
 * 它给预览的另外两处"文档语义"也一并在那边：标题按原文层级（不夹层）、单换行不换 `<br>`。
 *
 * 外层 `kylab-md` 保留：预览自己的排版（颜色、字号、行高、表格外框）挂在它上面，
 * 渲染器发的 `md-*` 类名是另一套钩子，两边取值同源（都来自 `tokens.css`）。
 *
 * 内容是**取回来的**：文档接口的阅读视角会内联给 `text`，会话文件区只有链接，
 * 两条路都在 `useRemoteText` 里（内联优先，不会再跑一趟网络）。
 */
function MarkdownPane({
  url,
  inline,
  name,
}: {
  url?: string | null
  inline?: string | null
  name: string
}) {
  const { loading, failure, text } = useRemoteText(url, inline)
  if (failure) return <PreviewUnavailable name={name} reason={loadFailure(failure)} />
  if (loading) return <PreviewLoading />
  return (
    <div className="kylab-md">
      <PlainMarkdown text={text} />
    </div>
  )
}

/** 文本与代码：等宽、保留空白（旧 `FilePreview.vue` 的 `.preview-text`）。 */
function TextPane({
  url,
  inline,
  name,
}: {
  url?: string | null
  inline?: string | null
  name: string
}) {
  const { loading, failure, text } = useRemoteText(url, inline)
  if (failure) return <PreviewUnavailable name={name} reason={loadFailure(failure)} />
  if (loading) return <PreviewLoading />
  return <pre className="kylab-text">{text}</pre>
}

/** "要签名链接才能画"的四种（图片 / PDF / Office 三件套）共用的一句失败说明。 */
function MissingUrl({ name, reason }: { name: string; reason?: string | null }) {
  return <PreviewUnavailable name={name} reason={loadFailure(missingReason(reason))} />
}

/**
 * PDF：交给浏览器自带的阅读器（`#page=N` 锚点零依赖就能跳页）。
 *
 * **先探一次再挂载**（`useContentProbe`）：iframe 的失败观测不到，服务端回 500 时
 * 用户看到的是浏览器画的那份错误信封原文——所以**没探出结论之前根本不挂 iframe**，
 * 那份响应体也就没有机会被渲染出来（挂上去再撤换来不及：浏览器已经开始画了）。
 * 探出非 2xx 就换成我们自己的失败态 + 一个「重试」；探不出结论（fetch 自己抛错）
 * 照常挂载——宁可按浏览器的行为来，也不能把能看的 PDF 判成看不了。
 */
function PdfPane({ url, name }: { url: string; name: string }) {
  const probe = useContentProbe(url)
  if (probe.failure) {
    return (
      <PreviewUnavailable
        name={name}
        reason={loadFailure(probe.failure)}
        onRetry={probe.retry}
        retrying={probe.checking}
      />
    )
  }
  // 探测还在飞：这一趟网络往返很短，就说"正在加载原文"（与文本/Office 分支同一句）
  if (probe.checking) return <PreviewLoading />
  return <iframe className="kylab-pdf" src={url} title={name} />
}

export function FilePreview({
  name,
  kind,
  mime,
  url,
  text,
  reason,
  preview,
  page,
}: FilePreviewProps) {
  // `preview`（文档接口的整份返回）是这四个字段的完整来源；显式传的优先
  // ——调用方点名要的算数，没点的从那份返回里补
  const source = {
    name: name ?? preview?.filename ?? '',
    kind: kind ?? preview?.kind ?? null,
    url: url ?? preview?.url ?? null,
    text: text ?? preview?.text ?? null,
  }
  const renderer = resolveRenderer({ name: source.name, kind: source.kind, mime })
  // PDF 跳页锚点：只对 PDF 有意义（`#` 之后是片段，不会发给服务端）
  const frameUrl =
    source.url && page && page > 0 ? `${source.url}#page=${page}` : (source.url ?? '')

  switch (renderer) {
    case 'none':
      return (
        <div className="kylab-preview">
          <PreviewNotSupported name={source.name} />
        </div>
      )

    case 'markdown':
      return (
        <div className="kylab-preview">
          <MarkdownPane url={source.url} inline={source.text} name={source.name} />
        </div>
      )

    case 'text':
      return (
        <div className="kylab-preview">
          <TextPane url={source.url} inline={source.text} name={source.name} />
        </div>
      )

    case 'image':
      return (
        <div className="kylab-preview">
          {source.url ? (
            <img className="kylab-image" src={source.url} alt={source.name} />
          ) : (
            <MissingUrl name={source.name} reason={reason} />
          )}
        </div>
      )

    case 'pdf':
      return (
        <div className="kylab-preview">
          {frameUrl ? (
            // `key` 绑在最终地址上：签名链接会过期，重新签发后必须**重建** iframe，
            // 否则它还在用旧 src；页码变了也要重建，否则已加载的 PDF 不会重新定位
            // （片段变化不会让原生阅读器跳页——旧 `DocumentDrawer.vue` 踩过同一个坑）
            <PdfPane key={frameUrl} url={frameUrl} name={source.name} />
          ) : (
            <MissingUrl name={source.name} reason={reason} />
          )}
        </div>
      )

    case 'docx':
      return (
        <div className="kylab-preview">
          {source.url ? (
            <DocxPreview url={source.url} name={source.name} />
          ) : (
            <MissingUrl name={source.name} reason={reason} />
          )}
        </div>
      )

    case 'pptx':
      return (
        <div className="kylab-preview">
          {source.url ? (
            <PptxPreview url={source.url} name={source.name} />
          ) : (
            <MissingUrl name={source.name} reason={reason} />
          )}
        </div>
      )

    case 'sheet':
      return (
        <div className="kylab-preview">
          {source.url ? (
            <SpreadsheetPreview url={source.url} name={source.name} />
          ) : (
            <MissingUrl name={source.name} reason={reason} />
          )}
        </div>
      )

    default:
      // 分派表是穷举的联合类型：走到这里说明加了新预览器却忘了接上，
      // 与其静默留白，不如说清是哪一种没接上
      return (
        <div className="kylab-preview">
          <PreviewNote>
            「{source.name}」的预览器（{String(renderer)}）还没有接上。
          </PreviewNote>
        </div>
      )
  }
}
