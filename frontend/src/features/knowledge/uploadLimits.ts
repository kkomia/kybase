/**
 * 上传约束（前端侧的一份）。
 *
 * **为什么要有这个文件**：这些数字与清单以前散在三处——上传弹窗里的提示、空状态里的
 * 说明、以及各自写死的 `200 * 1024 * 1024`。改一处漏一处的后果不是崩溃，而是
 * **界面上两句话互相矛盾**（一边说上限 200MB、一边说 100MB），用户只能靠试。
 *
 * ## M3 阶段 6：上限与格式**改从握手取**（R8）
 *
 * 数字与清单的**唯一作者是提供者**（后端 `documents.py` 的 `MAX_UPLOAD_BYTES`
 * 与 `provider.py` 机械导出的 `INGEST_EXTENSIONS`）。前端原先硬编码一份，等于第二份
 * 会漂的白名单——提供者那边改了（比如上限调到 500MB、加收 `.epub`），界面还在说旧话，
 * 而用户看到的是"明明写着支持，传上去却被拒"。
 *
 * 所以这里改成**握手优先、常量兜底**：
 *
 * - 握手在（本机档 `ready`，`capabilities.ingest` 拿到）→ 一切以它为准；
 * - 握手不在（服务器档、或还没探到）→ 用下面那几个与后端**逐字对齐**的常量兜底
 *   （服务器档没有"能力集"可问：那一档的界面与后端是同一份代码，常量就是那份契约；
 *   而没探到时用兜底不会把能传的文件拦下来，只是提示略微保守）。
 *
 * 读法统一走 `useUploadLimits()`（订阅提供者状态，握手一变提示就跟着变），
 * 而不是在组件里各读各的 `capabilities` —— 那样"上限写在哪"又会散开。
 */
import { useKnowledgeProviderStatus, type ProviderCapabilities } from '@/api/provider'

/**
 * 单文件上限的**兜底值**，与后端 `app/api/v1/documents.py` 的 `MAX_UPLOAD_BYTES` 对齐。
 *
 * 它现在只在"握手问不到"时兜底（服务器档 / 还没探到）；本机档的真值来自
 * `capabilities.ingest.max_bytes`。**对话附件的上限是另一件事**
 * （`api/v1/conversations.py` 的常量，走的是本机/服务器那条链，不经过提供者），
 * `chat/runtime/ChatProvider.tsx` 引的正是这个兜底常量。
 */
export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024

/** 给文案用的整数 MB（兜底那一份的），避免每个地方各写一个 200。 */
export const MAX_UPLOAD_MB = Math.round(MAX_UPLOAD_BYTES / (1024 * 1024))

/**
 * 一次最多几个文件。与 MaxKB 的"每次最多 50 个"同档——
 * 这是业界已经在用的量级，不必另发明一个；再多也只会让清单长到没人看得完。
 * **握手的能力集里没有这一项**（它是前端的交互约束，后端不限）：不参与 R8 那条消费。
 */
export const MAX_UPLOAD_FILES = 50

/**
 * 格式提示的**兜底文案**（握手问不到时用）。
 *
 * 不逐条罗列扩展名：用户认的是"Word 能不能传"，而不是".docx 在不在白名单里"。
 * **也不写"扫描件走 OCR 渠道"这类解析链路的说明**：用户要决定的是"这个文件能不能传"。
 */
export const UPLOAD_FORMAT_HINT = '支持 PDF、Word、PPT、Excel、Markdown、纯文本、CSV 与图片'

/**
 * 扩展名 → 用户认得的那种说法（**只在句子里用一次**）。
 *
 * 不认识的扩展名原样大写呈现（`epub` → `EPUB`）：宁可多写一个缩写，
 * 也不要因为它不在表里就不提——那会变成"明明支持却不说"。
 */
const EXTENSION_LABELS: Record<string, string> = {
  pdf: 'PDF',
  doc: 'Word',
  docx: 'Word',
  ppt: 'PPT',
  pptx: 'PPT',
  xls: 'Excel',
  xlsx: 'Excel',
  csv: 'CSV',
  md: 'Markdown',
  markdown: 'Markdown',
  txt: '纯文本',
  html: '网页',
  htm: '网页',
  png: '图片',
  jpg: '图片',
  jpeg: '图片',
  webp: '图片',
  gif: '图片',
  bmp: '图片',
}

/** 这一档的上传约束（组件里读它，别自己拆 `capabilities`）。 */
export interface UploadLimits {
  maxBytes: number
  maxMb: number
  /** 收哪些扩展名（小写、无点）；握手问不到时为空数组。 */
  extensions: string[]
  /** 一句话的格式提示（含"与"的那种中文并列）。 */
  formatHint: string
  maxFiles: number
}

/**
 * 把能力集翻成一句给用户看的格式提示（纯函数：能逐字钉）。
 *
 * 同一族（Word 的 `.doc`/`.docx`、图片那几种）**只提一次**：列成
 * "Word、Word、图片、图片"是机器话，不是人话。
 */
export function formatHintOf(extensions: string[]): string {
  if (extensions.length === 0) return UPLOAD_FORMAT_HINT
  const labels: string[] = []
  for (const raw of extensions) {
    const label = EXTENSION_LABELS[raw.toLowerCase()] ?? raw.toUpperCase()
    if (!labels.includes(label)) labels.push(label)
  }
  if (labels.length === 1) return `支持 ${labels[0]}`
  return `支持 ${labels.slice(0, -1).join('、')} 与 ${labels[labels.length - 1]}`
}

/** 能力集 → 约束（纯函数；缺项走兜底，见文件头"握手优先、常量兜底"）。 */
export function limitsOf(capabilities: ProviderCapabilities | null): UploadLimits {
  const ingest = capabilities?.ingest
  const maxBytes = ingest?.max_bytes && ingest.max_bytes > 0 ? ingest.max_bytes : MAX_UPLOAD_BYTES
  return {
    maxBytes,
    maxMb: Math.round(maxBytes / (1024 * 1024)),
    extensions: ingest?.extensions ?? [],
    formatHint: formatHintOf(ingest?.extensions ?? []),
    maxFiles: MAX_UPLOAD_FILES,
  }
}

/**
 * 这一档的上传约束（订阅提供者状态：握手一变，界面上的提示当场跟着变）。
 *
 * 服务器档（浏览器 / NAS 网页端）没有能力集可问，读到的就是兜底那一份
 * ——与后端同一份取值，不是"少了一个来源"。
 */
export function useUploadLimits(): UploadLimits {
  return limitsOf(useKnowledgeProviderStatus().capabilities)
}
