/**
 * 上传文档弹窗（旧 `UploadDialog.vue` 的行为逐条对齐）。
 *
 * **为什么是弹窗而不是"选完就传"**：批量上传的重复与失败必须留在屏幕上让人逐个处理，
 * toast 做不到这件事（20 个文件连成一片，"哪几个没成功"根本看不清）。中间这一步
 * 也让人能反悔：选错了可以逐个移除，而不是等它传完再删。
 *
 * **刻意不做的事**：不提供切块参数——切块策略是**知识库级属性**（建库时定），
 * 放在这里会让人以为可以逐文件不同，那会造成同一库里切法不一致、检索质量无从解释。
 *
 * 两个入口（文件 / 文件夹）：`选择文件` 本身就是多选；`选择文件夹` 用 `webkitdirectory`，
 * 它必须独占一个 input。拖放统一走一个处理函数，拖文件夹也收（`dataTransfer.files`
 * 对目录是空的，要靠 `webkitGetAsEntry` 递归展开）。
 */
import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'

import { uploadDocument } from '@/api/documents'
import { messageOf } from '@/features/knowledge/store'
import { MAX_UPLOAD_FILES, useUploadLimits } from '@/features/knowledge/uploadLimits'
import { formatBytes, formatCount } from '@/lib/format'
import { Button } from '@/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/ui/dialog'

/**
 * `rejected` 与 `failed` 分开，是因为**下一步动作不同**：
 * - `rejected`：本地就不收（超过单文件上限）。原样重试毫无意义，得换个文件。
 * - `failed`：发出去了、后端拒了。可能是一次网络抖动，值得重试。
 */
type UploadStatus = 'pending' | 'uploading' | 'done' | 'duplicate' | 'failed' | 'rejected'

interface Candidate {
  file: File
  /** 相对路径（文件夹上传时）：不同子目录里的同名文件要靠它区分。 */
  name: string
}

interface Item extends Candidate {
  status: UploadStatus
  message: string
}

const STATUS_LABEL: Record<UploadStatus, string> = {
  pending: '待上传',
  uploading: '上传中…',
  done: '已提交',
  duplicate: '重复，已跳过',
  rejected: '未接收',
  failed: '失败',
}

/* ---------------------------------------------------------- 拖放条目 API */

/**
 * 拖放条目 API 的结构化声明（旧实现同一手法）。
 *
 * **不复用 lib.dom 的 `FileSystemEntry` 系列名字**：它们在类型层存在但各浏览器的
 * 支持面不同，而这里只用到四个字段，自己声明反而更清楚要依赖什么。
 */
interface DroppedDirReader {
  readEntries(callback: (entries: DroppedEntry[]) => void): void
}

interface DroppedEntry {
  isFile: boolean
  isDirectory: boolean
  fullPath: string
  file(onSuccess: (file: File) => void, onError?: () => void): void
  createReader(): DroppedDirReader
}

/** `readEntries` 一次最多回 100 条，必须反复读到空——只读一次会静默漏掉大目录里的文件。 */
function readEntries(reader: DroppedDirReader): Promise<DroppedEntry[]> {
  return new Promise((resolve) => reader.readEntries((entries) => resolve(entries)))
}

async function walkEntry(entry: DroppedEntry, out: Candidate[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File | null>((resolve) =>
      entry.file(
        (value) => resolve(value),
        () => resolve(null),
      ),
    )
    if (file) out.push({ file, name: entry.fullPath.replace(/^\//, '') || file.name })
    return
  }
  if (entry.isDirectory) {
    const reader = entry.createReader()
    for (;;) {
      const batch = await readEntries(reader)
      if (batch.length === 0) break
      for (const child of batch) await walkEntry(child, out)
    }
  }
}

interface UploadDialogProps {
  open: boolean
  kbId: string
  /** 上传目标目录：只有选中了具体目录时才带上。 */
  folderId?: string
  onClose: () => void
  /** 传完之后通知宿主刷新（弹窗自己负责逐文件结果）。 */
  onUploaded: () => void
}

export function UploadDialog({ open, kbId, folderId, onClose, onUploaded }: UploadDialogProps) {
  const [items, setItems] = useState<Item[]>([])
  const [uploading, setUploading] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [notice, setNotice] = useState('')
  /**
   * 上限与格式提示**从握手取**（M3 阶段 6，R8）：提供者那边改了上限/格式，
   * 这里的提示与本地拒收的判据当场跟着变，不会出现"写着支持却被拒"。
   */
  const limits = useUploadLimits()
  const fileInput = useRef<HTMLInputElement | null>(null)
  const folderInput = useRef<HTMLInputElement | null>(null)

  // 关掉就清空：Esc、点遮罩、点关闭按钮都能关，只处理一条路径会让下次打开看到
  // 一堆陈旧的文件，分不清哪些是这次的。
  useEffect(() => {
    if (!open) {
      setItems([])
      setUploading(false)
      setNotice('')
      setDragActive(false)
    }
  }, [open])

  // `webkitdirectory` 与 `directory` 不是标准属性，React 的类型里没有，
  // 所以挂载后直接写到元素上（比在 JSX 里塞未知属性再祈祷类型系统放行更明确）。
  useEffect(() => {
    const element = folderInput.current
    if (!element) return
    element.setAttribute('webkitdirectory', '')
    element.setAttribute('directory', '')
  }, [open])

  const pendingCount = items.filter((item) => item.status === 'pending').length
  const doneCount = items.filter(
    (item) => item.status === 'done' || item.status === 'duplicate',
  ).length
  const failedCount = items.filter((item) => item.status === 'failed').length
  const rejectedCount = items.filter((item) => item.status === 'rejected').length
  const totalBytes = items
    .filter((item) => item.status === 'pending')
    .reduce((sum, item) => sum + item.file.size, 0)
  const hasResult = items.some((item) => item.status !== 'pending')
  const canSubmit = pendingCount > 0 && !uploading

  function candidatesFrom(list: FileList | null): Candidate[] {
    return Array.from(list ?? []).map((file) => ({
      file,
      name: file.webkitRelativePath || file.name,
    }))
  }

  function addFiles(candidates: Candidate[]): void {
    // 去重要覆盖**同一批里自己重**：`Set` 随加入一起长，不是先算完再筛——
    // 否则同一次拖进来的两个同名同大小文件会双双进清单（看着像界面出了错）
    const existing = new Set(items.map((item) => `${item.name}:${item.file.size}`))
    const fresh: Candidate[] = []
    const dupInBatch: string[] = []
    for (const candidate of candidates) {
      const key = `${candidate.name}:${candidate.file.size}`
      if (existing.has(key)) {
        dupInBatch.push(candidate.name)
        continue
      }
      existing.add(key)
      fresh.push(candidate)
    }

    // 超出一次能处理的量：只取前 N 个，并**明说被砍掉了几个**（静默丢弃最坏）
    const overflow = fresh.length > MAX_UPLOAD_FILES
    const taken = overflow ? fresh.slice(0, MAX_UPLOAD_FILES) : fresh
    const prepared: Item[] = taken.map((candidate) =>
      candidate.file.size > limits.maxBytes
        ? {
            ...candidate,
            // 超限在本地就判定：等后端读完 300MB 再回 413，白等的那几十秒用 file.size 就能省掉
            status: 'rejected' as UploadStatus,
            message: `超过 ${limits.maxMb}MB 上限，请先压缩或切分`,
          }
        : { ...candidate, status: 'pending' as UploadStatus, message: '' },
    )

    setItems((current) => [...current, ...prepared])
    if (overflow) {
      setNotice(
        `一次最多 ${MAX_UPLOAD_FILES} 个文件，后面的 ${fresh.length - MAX_UPLOAD_FILES} 个没有加入清单`,
      )
    } else if (dupInBatch.length) {
      setNotice(`「${[...new Set(dupInBatch)].join('、')}」重复选择，只加入清单一次`)
    }
  }

  function onPicked(event: React.ChangeEvent<HTMLInputElement>): void {
    const target = event.target
    addFiles(candidatesFrom(target.files))
    // 清空 value 才能连续选同一个文件
    target.value = ''
  }

  async function collectDropped(transfer: DataTransfer): Promise<Candidate[]> {
    const entries = Array.from(transfer.items ?? [])
      .map((item) => (typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null))
      .filter((entry): entry is NonNullable<typeof entry> => entry !== null)
      .map((entry) => entry as unknown as DroppedEntry)
    if (entries.length === 0) return candidatesFrom(transfer.files)
    const collected: Candidate[] = []
    for (const entry of entries) await walkEntry(entry, collected)
    return collected
  }

  async function onDrop(event: React.DragEvent<HTMLDivElement>): Promise<void> {
    event.preventDefault()
    setDragActive(false)
    const transfer = event.dataTransfer
    if (!transfer) return
    try {
      addFiles(await collectDropped(transfer))
    } catch {
      // 条目 API 出问题时退回平铺列表：至少把文件收下（文件夹会丢，但不至于整个拖放失灵）
      addFiles(candidatesFrom(transfer.files))
    }
  }

  /** 一行的稳定标识：同一份文件只进一次清单，所以「名字 + 大小」是唯一的。 */
  const keyOf = (item: Candidate): string => `${item.name}:${item.file.size}`

  function patchItem(key: string, patch: Partial<Item>): void {
    setItems((current) => current.map((row) => (keyOf(row) === key ? { ...row, ...patch } : row)))
  }

  function removeAt(index: number): void {
    setItems((current) => current.filter((_, position) => position !== index))
  }

  /** 值得重试的只有 `failed`：`rejected` 重置了还是会被同一把尺子拦下。 */
  function retryFailed(): void {
    setItems((current) =>
      current.map((item) =>
        item.status === 'failed' ? { ...item, status: 'pending', message: '' } : item,
      ),
    )
  }

  async function submit(): Promise<void> {
    if (uploading || pendingCount === 0) return
    setUploading(true)
    // 串行而不是并发：后端摄入是 CPU 密集的（切块 + 向量化），并发只会让每个都变慢，
    // 还会让"卡在第几个"无从判断
    for (const item of items) {
      if (item.status !== 'pending') continue
      const key = keyOf(item)
      patchItem(key, { status: 'uploading' })
      try {
        const accepted = await uploadDocument(kbId, item.file, folderId)
        patchItem(
          key,
          accepted.is_duplicate
            ? { status: 'duplicate', message: '内容与库中已有文档相同，未重复入库' }
            : { status: 'done', message: '已提交，正在后台处理' },
        )
      } catch (cause) {
        patchItem(key, { status: 'failed', message: messageOf(cause, '上传失败') })
      }
    }
    setUploading(false)
    onUploaded()
  }

  const summary =
    items.length === 0
      ? '还没有选择文件'
      : `共 ${formatCount(items.length)} 个${
          pendingCount ? ` · 待上传 ${formatCount(pendingCount)}（${formatBytes(totalBytes)}）` : ''
        }${doneCount ? ` · 已处理 ${formatCount(doneCount)}` : ''}${failedCount ? ` · 失败 ${formatCount(failedCount)}` : ''}${
          rejectedCount ? ` · 未接收 ${formatCount(rejectedCount)}` : ''
        }`

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      {/* 宽档：`sm:max-w-[980px]`（旧 `Modal size="wide"` 的 980px） */}
      <DialogContent className="flex max-h-[min(88vh,900px)] flex-col gap-0 p-0 sm:max-w-[980px]">
        <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
          <DialogTitle>上传文档</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div
            className={[
              'kb-dropzone',
              dragActive ? 'kb-dropzone-active' : '',
              items.length > 0 ? 'kb-dropzone-compact' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            onDragOver={(event) => {
              event.preventDefault()
              setDragActive(true)
            }}
            onDragLeave={(event) => {
              event.preventDefault()
              setDragActive(false)
            }}
            onDrop={(event) => void onDrop(event)}
          >
            <span className="kb-dropzone-actions">
              <Button variant="outline" onClick={() => fileInput.current?.click()}>
                选择文件
              </Button>
              <Button variant="outline" onClick={() => folderInput.current?.click()}>
                选择文件夹
              </Button>
            </span>
            <span className="kb-dropzone-hint">也可以把文件或整个文件夹拖到这里</span>
          </div>

          <input
            ref={fileInput}
            className="kb-visually-hidden"
            type="file"
            multiple
            tabIndex={-1}
            aria-label="选择文件"
            onChange={onPicked}
          />
          <input
            ref={folderInput}
            className="kb-visually-hidden"
            type="file"
            multiple
            tabIndex={-1}
            aria-label="选择文件夹"
            onChange={onPicked}
          />

          <p className="text-hint">
            {limits.formatHint}。单个文件不超过 {limits.maxMb}MB，一次最多 {limits.maxFiles} 个。
          </p>

          {notice ? <p className="text-note">{notice}</p> : null}

          {items.length > 0 ? (
            <ul className="kb-file-list">
              {items.map((item, index) => (
                <li key={keyOf(item)} className="kb-file-row">
                  <span className="kb-file-name" title={item.name}>
                    {item.name}
                  </span>
                  <span className="kb-file-size tabular">{formatBytes(item.file.size)}</span>
                  <span className={`kb-file-status-${item.status}`}>
                    {item.status === 'uploading' ? '上传中…' : STATUS_LABEL[item.status]}
                  </span>
                  <span className="kb-file-message">{item.message}</span>
                  <span>
                    {item.status === 'pending' ? (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`移除 ${item.name}`}
                        onClick={() => removeAt(index)}
                      >
                        <X aria-hidden="true" />
                      </Button>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <DialogFooter className="flex-wrap items-center border-t border-[var(--border-hairline)] px-4 py-3">
          <span className="kb-foot-note">{summary}</span>
          {failedCount ? (
            <Button variant="outline" onClick={retryFailed}>
              重试失败项（{formatCount(failedCount)}）
            </Button>
          ) : null}
          {hasResult ? (
            <Button
              variant="outline"
              onClick={() => {
                setItems([])
                setNotice('')
              }}
            >
              清空清单
            </Button>
          ) : null}
          <Button variant="outline" onClick={onClose}>
            关闭
          </Button>
          <Button variant="default" disabled={!canSubmit} onClick={() => void submit()}>
            {uploading
              ? '上传中…'
              : `开始上传${pendingCount ? `（${formatCount(pendingCount)}）` : ''}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
