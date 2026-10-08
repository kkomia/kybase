/**
 * 预览分派表：**哪种文件走哪条路**，只此一处。
 *
 * 一张表决定一切——**按后缀 / 后端给的 kind 路由**，每个只归一类，不做"猜内容"。
 * 与旧 `frontend/src/components/files/FilePreview.vue` 的那张表逐条对齐（v0.26），
 * 差别只有一处，写在下面第 3 条。
 *
 * | 输入 | 怎么渲染 | 为什么 |
 * | --- | --- | --- |
 * | `md` / `markdown` | 自己的 Markdown 渲染器 | 与对话里的答案同一套规则，不另引一个库 |
 * | `txt` / `log` / `csv` / 各种代码 | `<pre>` 等宽 | 这些就该原样看 |
 * | `png` / `jpg` / `gif` / `webp` / `bmp` / `avif` | `<img>` | 浏览器本来就会 |
 * | `pdf` | `<iframe>` 指向签名链接 | 浏览器内置阅读器就是它，自建一层要自己管分页/缩放/文本层 |
 * | `docx` | `DocxPreview`（docx-preview） | 浏览器不会原生显示 |
 * | `pptx` | `PptxPreview`（pptx-preview） | 同上 |
 * | `xlsx` / `xls` | `SpreadsheetPreview`（exceljs） | 同上 |
 * | 其它（含 `binary`） | 一句"不能在这里预览" | **不假装能预览** |
 *
 * 三条取舍写在明处：
 *
 * 1. **SVG 不在图片那一档**：它能带 `<script>`，内联在本站 origin 下就是存储型 XSS。
 *    服务端按后缀白名单强制 `attachment`（见 `backend/app/api/v1/conversations.py` 的
 *    `INLINE_SAFE_KINDS`，那里也不含 `svg`），所以这里也把它归到"下载看"——
 *    **两处口径必须一致**，否则就是"界面画了一个框，里面永远加载失败"。
 * 2. **PDF 不再引 pdf.js**（同旧前端的取舍）：浏览器的内置阅读器就是它，
 *    自建一层要自己管分页、缩放、文本层与那 1MB 的 worker。
 * 3. **`csv` 在两条路上口径不同，且是刻意的**：会话文件区给的是**后缀**
 *    （`kind: "csv"`），按旧表走纯文本 `<pre>`；而文档接口给的是**后端算出来的**
 *    `kind`（`backend/app/services/documents.py` 把 `.csv` 归到 `markdown`，因为它有
 *    解析产物），于是走 Markdown 分支。两份输入不一样，结果不一样，不是漏了一条。
 *
 * 认 kind 的顺序也是刻意的：**后端给的 kind 优先**（它是服务端按文件名后缀算的、
 * 比客户端声明的 mime 可靠），然后是 kind 本身是不是一个已知后缀，
 * 再是 mime，最后才从文件名后缀兜底。
 */
import type { PreviewKind } from '@/api/documents'

/** 一种预览器。`none` = 不假装能预览（给"下载看"的提示）。 */
export type PreviewRenderer =
  'markdown' | 'text' | 'image' | 'pdf' | 'docx' | 'pptx' | 'sheet' | 'none'

export interface PreviewHint {
  /** 文件名（含扩展名）。分派与报错文案都用它。 */
  name?: string | null
  /**
   * 种类：既可能是后端的 `PreviewKind`（`docx` / `excel` / `binary`…），
   * 也可能是文件后缀（`md` / `png` / `docx`…，会话文件区给的就是后缀）。
   */
  kind?: string | null
  /** 媒体类型。**只在前两者都认不出来时兜底**：上传方声明的 mime 常常是空的或错的。 */
  mime?: string | null
}

/** 按后缀认 Markdown。 */
const MARKDOWN_EXTENSIONS = new Set(['md', 'markdown'])

/**
 * 按后缀认图片。**不含 `svg`**：见文件头注第 1 条。
 *
 * 与 `backend/app/services/documents.py` 的 `_IMAGE_SUFFIXES` 相比少了 `svg`，
 * 那是刻意的——后端那份是"这是不是图片"（用于归类），这份是"能不能内联渲染"。
 */
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'avif'])

/**
 * 表格：`xlsx` 走 exceljs。
 *
 * `xls` 也在这一档（与旧表一致：旧前端把它交给 `@vue-office/excel`），
 * 但 exceljs 只读 OOXML——二进制 `.xls` 会解析失败并落到失败态，
 * 文案由 `SpreadsheetPreview` 单独给（见那里的 `LEGACY_XLS_NOTE`）。
 */
const SHEET_EXTENSIONS = new Set(['xlsx', 'xls'])

/** Office：旧表逐条对齐（`docx` / `pptx` / `xlsx` / `xls`），没有 `doc` / `ppt`。 */
const OFFICE_EXTENSIONS: Readonly<Record<string, PreviewRenderer>> = {
  docx: 'docx',
  pptx: 'pptx',
  xlsx: 'sheet',
  xls: 'sheet',
}

/** 当纯文本看的：代码、配置、日志、数据。够用就好，不是一张穷举表（旧表原样搬来）。 */
const TEXT_EXTENSIONS = new Set([
  'txt',
  'log',
  'csv',
  'tsv',
  'json',
  'jsonl',
  'yaml',
  'yml',
  'toml',
  'ini',
  'conf',
  'env',
  'py',
  'ts',
  'tsx',
  'js',
  'jsx',
  'vue',
  'sh',
  'bash',
  'ps1',
  'bat',
  'sql',
  'go',
  'rs',
  'java',
  'kt',
  'c',
  'h',
  'cpp',
  'hpp',
  'cs',
  'rb',
  'php',
  'swift',
  'scala',
  'lua',
  'r',
  'html',
  'htm',
  'xml',
  'css',
  'scss',
  'less',
  'diff',
  'patch',
  'gitignore',
  'dockerfile',
])

/**
 * 后端 `DocumentPreview.kind` 的取值（见 `backend/app/api/v1/documents.py` 的
 * `PreviewOut`）。
 *
 * 这些**不是后缀**，只能从这里认：`excel` 是表格、`image` 是一整类图片、
 * `binary` 是"服务端也不知道怎么画"（老式 `.doc`、压缩包、没有签名密钥时的兜底）。
 * `binary` 显式映射成 `none`：它是后端说"别试了"，不是"没听说过后缀"。
 */
const BACKEND_KINDS: Readonly<Record<PreviewKind, PreviewRenderer>> = {
  markdown: 'markdown',
  pdf: 'pdf',
  image: 'image',
  docx: 'docx',
  pptx: 'pptx',
  excel: 'sheet',
  binary: 'none',
}

/**
 * 媒体类型兜底。**有序**：先具体、后概括（`image/svg` 必须排在 `image/` 之前）。
 *
 * 只有在后缀与 kind 都认不出来时才走到这里——所以不必穷举，
 * 只需要覆盖"后缀被改坏 / 缺失、而 mime 还对"的那几种常见情况。
 */
const MIME_RENDERERS: ReadonlyArray<readonly [RegExp, PreviewRenderer]> = [
  [/^image\/svg/, 'none'],
  [/^image\//, 'image'],
  [/^application\/pdf\b/, 'pdf'],
  [/wordprocessingml\.document/, 'docx'],
  [/presentationml\.presentation/, 'pptx'],
  [/spreadsheetml\.sheet/, 'sheet'],
  [/^application\/(vnd\.)?ms-excel\b/, 'sheet'],
  [/^text\/(markdown|x-markdown)\b/, 'markdown'],
  [/^text\//, 'text'],
  [
    /^(application|text)\/(json|.*\+json|xml|.*\+xml|javascript|x-javascript|yaml|x-yaml|toml|csv)\b/,
    'text',
  ],
]

/** 去掉点号、大小写、mime 参数与路径前缀，只留下能对着表查的记号。 */
function normalise(token: string | null | undefined): string {
  if (!token) return ''
  const value = token.trim().toLowerCase().split(';')[0].trim()
  const tail = value.includes('/') ? value.slice(value.lastIndexOf('/') + 1) : value
  return tail.startsWith('.') ? tail.slice(1) : tail
}

/** 从文件名取后缀（小写、不带点）。`.env` 这类"只有前缀名"的文件不算有后缀。 */
export function extensionOf(name: string | null | undefined): string {
  if (!name) return ''
  const clean = name.trim().toLowerCase().split(/[?#]/)[0]
  const dot = clean.lastIndexOf('.')
  return dot > 0 ? clean.slice(dot + 1) : ''
}

/** 按后缀选渲染器；认不出来返回 `undefined`（= 让调用方接着找别的线索）。 */
function rendererForExtension(extension: string): PreviewRenderer | undefined {
  if (!extension) return undefined
  if (MARKDOWN_EXTENSIONS.has(extension)) return 'markdown'
  if (IMAGE_EXTENSIONS.has(extension)) return 'image'
  if (SHEET_EXTENSIONS.has(extension)) return 'sheet'
  if (TEXT_EXTENSIONS.has(extension)) return 'text'
  if (extension === 'pdf') return 'pdf'
  return OFFICE_EXTENSIONS[extension]
}

/** 按媒体类型选渲染器；`none` = 认得出、但**明确不渲染**（如 SVG）。 */
function rendererForMime(mime: string): PreviewRenderer | undefined {
  if (!mime.includes('/')) return undefined
  return MIME_RENDERERS.find(([pattern]) => pattern.test(mime))?.[1]
}

/**
 * 这份文件该用哪个预览器。**纯函数**：界面与测试都对着它，分派规则只有这一份。
 */
export function resolveRenderer(hint: PreviewHint): PreviewRenderer {
  const kind = hint.kind?.trim().toLowerCase() ?? ''
  if (kind) {
    // 1. 后端的 kind（`excel` / `image` / `binary` 这类不是后缀，只能在这里认）
    const backend = BACKEND_KINDS[kind as PreviewKind]
    if (backend) return backend
    // 2. kind 本身是后缀，或干脆是一条 mime
    const byKind = kind.includes('/')
      ? rendererForMime(kind)
      : rendererForExtension(normalise(kind))
    if (byKind) return byKind
  }
  // 3. mime 兜底（后缀被改坏、或调用方只拿得到 mime）
  const byMime = rendererForMime(hint.mime?.trim().toLowerCase() ?? '')
  if (byMime) return byMime
  // 4. 最后才从文件名后缀认
  return rendererForExtension(extensionOf(hint.name)) ?? 'none'
}
