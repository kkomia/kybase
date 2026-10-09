/**
 * 上传约束（前端侧的一份）。
 *
 * **为什么要有这个文件**：这些数字与清单以前散在三处——上传弹窗里的提示、空状态里的
 * 说明、以及各自写死的 `200 * 1024 * 1024`。改一处漏一处的后果不是崩溃，而是
 * **界面上两句话互相矛盾**（一边说上限 200MB、一边说 100MB），用户只能靠试。
 *
 * ## 取值的唯一作者是后端
 *
 * `MAX_UPLOAD_BYTES` 与后端 `app/api/v1/documents.py` 的 `MAX_UPLOAD_BYTES` 逐字对齐，
 * `UPLOAD_FORMAT_HINT` 写的就是后端收的那几族格式。界面与后端是同一份代码的两个面
 * （v0.29 起只有服务器一套），后端改了这两个值，这里要跟着改——改不到，用户就会看到
 * "明明写着支持，传上去却被拒"。
 *
 * 读法统一走 `useUploadLimits()`，而不是在组件里各读各的常量——那样"上限写在哪"又会散开。
 */

/**
 * 单文件上限，与后端 `app/api/v1/documents.py` 的 `MAX_UPLOAD_BYTES` 对齐。
 *
 * 组件里**不要**直接引用它：走 `useUploadLimits()`，免得"上限写在哪"又散开。
 */
export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024

/** 给文案用的整数 MB，避免每个地方各写一个 200。 */
export const MAX_UPLOAD_MB = Math.round(MAX_UPLOAD_BYTES / (1024 * 1024))

/**
 * 一次最多几个文件。与 MaxKB 的"每次最多 50 个"同档——
 * 这是业界已经在用的量级，不必另发明一个；再多也只会让清单长到没人看得完。
 * 它是前端的交互约束（后端不限）。
 */
export const MAX_UPLOAD_FILES = 50

/**
 * 格式提示。
 *
 * 不逐条罗列扩展名：用户认的是"Word 能不能传"，而不是".docx 在不在白名单里"。
 * **也不写"扫描件走 OCR 渠道"这类解析链路的说明**：用户要决定的是"这个文件能不能传"。
 */
export const UPLOAD_FORMAT_HINT = '支持 PDF、Word、PPT、Excel、Markdown、纯文本、CSV 与图片'

/** 这一档的上传约束（组件里读它，别自己拆常量）。 */
export interface UploadLimits {
  maxBytes: number
  maxMb: number
  /** 一句话的格式提示（含"与"的那种中文并列）。 */
  formatHint: string
  maxFiles: number
}

const UPLOAD_LIMITS: UploadLimits = {
  maxBytes: MAX_UPLOAD_BYTES,
  maxMb: MAX_UPLOAD_MB,
  formatHint: UPLOAD_FORMAT_HINT,
  maxFiles: MAX_UPLOAD_FILES,
}

/**
 * 这一档的上传约束。
 *
 * v0.29 起只有服务器一套：上限与格式就是后端那几个常量，没有"握手"可问，
 * 所以这里直接给那一份（不再订阅任何外部状态）。
 */
export function useUploadLimits(): UploadLimits {
  return UPLOAD_LIMITS
}
