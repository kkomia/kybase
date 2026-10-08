/**
 * 预览域的出口。**外面只从这里进**：
 *
 * ```tsx
 * import { FilePreview } from '@/features/preview'
 * ```
 *
 * 三个渲染器也一并导出（知识库的「阅读视角」与对话页的文件抽屉都可能想直接挑一个用，
 * 例如"这一页确定是 Word"时不必再走一遍分派），但**分派表只有一个**——
 * 需要"该用哪个"时调 `resolveRenderer`，不要在调用方再写一张表。
 *
 * 用法与取舍见同目录的 `README.md`。
 */
export { FilePreview, type FilePreviewProps } from './FilePreview'
export { DocxPreview, type DocxPreviewProps } from './DocxPreview'
export { PptxPreview, type PptxPreviewProps } from './PptxPreview'
export { SpreadsheetPreview, type SpreadsheetPreviewProps } from './SpreadsheetPreview'
export { resolveRenderer, extensionOf, type PreviewRenderer, type PreviewHint } from './kinds'
export {
  CANNOT_PREVIEW,
  NETWORK_UNREACHABLE,
  failureText,
  loadFailure,
  PreviewNote,
  PreviewNotSupported,
  PreviewUnavailable,
} from './notes'
