/**
 * 「按页」视角：选一段页范围，看这些页的**解析文字**与**原件页图**
 * （`GET /documents/{id}/pages`）。
 *
 * 为什么单独画这一块：整份原件预览走 `@/features/preview` 的 `<iframe>`（PDF 那一档），
 * 它由浏览器自带的阅读器渲染，**没有"按页取一张图"的能力**；而"第 3 页解析出来是这段字"
 * 要能一眼对上原页，就得让文字与那一页的图并排出现。接口给的是**相对路径的签名链接**，
 * 直接塞进 `<img src>` 就是那一页的图（与 `DocumentPreview.url` 同一种东西）。
 *
 * 三条与接口对齐的口径（都写在 `schema.d.ts` 的字段说明里，这里只是按它渲染）：
 *
 * 1. `page_text_available=false` 说的是**这份文档的解析产物里没有页码信息**（例如 Markdown
 *    来源），不是"文档是空的"——所以文字档只给一句空态，不铺一叠空页；
 * 2. `image_url` 为 null 的页（超出总页数、或原件本来就不能按页出图）给一句空态，
 *    **不挂破图**；
 * 3. `pages` 的长度恒等于请求的页数，所以按返回的顺序铺格子就行，不在这里补齐。
 *
 * 单次跨度上限由后端定（`PAGE_RANGE_MAX`）：输入在这里就夹住，别让人白等一趟 422。
 */
import { useEffect, useId, useState } from 'react'

import {
  PAGE_RANGE_MAX,
  getDocumentPages,
  type DocumentPages as DocumentPagesResult,
} from '@/api/documents'
import { failureText, loadFailure, PreviewUnavailable } from '@/features/preview'
import { SkeletonRows } from '@/features/knowledge/composites'
import { formatCount } from '@/lib/format'
import { Button } from '@/ui/button'
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'

/** 这一档看什么：原件页图 / 解析文字。 */
type PageMode = 'image' | 'text'

const MODE_TABS: { key: PageMode; label: string }[] = [
  { key: 'image', label: '图片' },
  { key: 'text', label: '文字' },
]

export interface DocumentPagesProps {
  documentId: string
  /** 文件名：失败态要用它说清"哪一份没取到"。 */
  name: string
  /** 文档总页数（探测阶段写入）。`null` = 探测没给出页数，结束页只能默认 1。 */
  pageCount: number | null
  /** 原件能不能按页出图（`pdf` / `image`）。默认档与「图片 / 文字」的选择都看它。 */
  canPageImages: boolean
}

/** 输入框里的整数：空串 / 乱填一律回落到 `fallback`（不发一次注定被拒的请求）。 */
function integerOr(value: string, fallback: number): number {
  const parsed = Number.parseInt(value.trim(), 10)
  return Number.isFinite(parsed) ? parsed : fallback
}

export function DocumentPages({ documentId, name, pageCount, canPageImages }: DocumentPagesProps) {
  const fromId = useId()
  const toId = useId()
  /**
   * 结束页的默认值：总页数已知时就把这一份看到底（未知时只能 1），并夹在单次上限之内
   * ——"打开就看见整份"是这个视角最常见的用法，而超上限的请求一定会被后端拒掉。
   */
  const initialTo = Math.min(Math.max(pageCount ?? 1, 1), PAGE_RANGE_MAX)
  const [mode, setMode] = useState<PageMode>(canPageImages ? 'image' : 'text')
  const [fromDraft, setFromDraft] = useState('1')
  const [toDraft, setToDraft] = useState(String(initialTo))
  /** 真正发出去的那一段（输入框改了不算数，点「查看」才生效）。 */
  const [range, setRange] = useState({ from: 1, to: initialTo })
  const [result, setResult] = useState<DocumentPagesResult | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  /** 失败后重试：这个数一变就重新取一次同一段（与 `range` 无关）。 */
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    // 换范围/换档时先清空：旧的页还挂在新范围下面会被读成"这一段就是这些内容"
    setResult(null)
    getDocumentPages(documentId, {
      from_page: range.from,
      to_page: range.to,
      images: mode === 'image',
    })
      .then((body) => {
        if (alive) setResult(body)
      })
      .catch((cause) => {
        if (!alive) return
        setError(failureText(cause, '加载失败'))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [documentId, range, mode, attempt])

  /** 把输入框里的两个数收成一段合法范围：起 >= 1、止 >= 起、跨度 <= 上限。 */
  function submit(): void {
    const from = Math.max(integerOr(fromDraft, 1), 1)
    const to = Math.min(Math.max(integerOr(toDraft, from), from), from + PAGE_RANGE_MAX - 1)
    // 夹过之后写回输入框：显示的与请求的必须是同一段
    setFromDraft(String(from))
    setToDraft(String(to))
    setRange({ from, to })
  }

  const pages = result?.pages ?? []
  /** 这份文档的解析产物里没有页码信息——文字档没有内容可铺。 */
  const textMissing = result !== null && !result.page_text_available

  return (
    <div className="kb-pages">
      <form
        className="kb-pages-bar"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <div className="kb-pages-range">
          <Label htmlFor={fromId}>起</Label>
          <Input
            id={fromId}
            className="kb-pages-input"
            inputMode="numeric"
            value={fromDraft}
            onChange={(event) => setFromDraft(event.target.value)}
          />
          <Label htmlFor={toId}>止</Label>
          <Input
            id={toId}
            className="kb-pages-input"
            inputMode="numeric"
            value={toDraft}
            onChange={(event) => setToDraft(event.target.value)}
          />
          <Button type="submit" variant="outline" size="sm" disabled={loading}>
            查看
          </Button>
          <span className="text-hint">单次最多 {PAGE_RANGE_MAX} 页</span>
        </div>

        {canPageImages ? (
          <div className="kb-source-switch" role="tablist" aria-label="按页内容">
            {MODE_TABS.map((option) => (
              <button
                key={option.key}
                type="button"
                role="tab"
                aria-selected={mode === option.key}
                className={['kb-source-tab', mode === option.key ? 'kb-source-tab-on' : '']
                  .filter(Boolean)
                  .join(' ')}
                onClick={() => setMode(option.key)}
              >
                {option.label}
              </button>
            ))}
          </div>
        ) : null}
      </form>

      {loading ? (
        // 与抽屉其它加载态同一套骨架（`@/ui/skeleton` 的两种预设之一）
        <SkeletonRows variant="card" rows={2} />
      ) : null}

      {!loading && error ? (
        // 与阅读视角同一个失败态：为什么 + 出路（重试就地重取这一段，输入框里的值不动）
        <PreviewUnavailable
          name={name}
          reason={loadFailure(error)}
          onRetry={() => setAttempt((current) => current + 1)}
        />
      ) : null}

      {!loading && !error && textMissing && mode === 'text' ? (
        <p className="kb-empty-hint">这份文档没有页码信息。</p>
      ) : null}
      {!loading && !error && textMissing && mode === 'text' && canPageImages ? (
        <p className="kb-empty-hint">切到「图片」档可以看原件。</p>
      ) : null}

      {!loading && !error && result && result.total_pages !== null ? (
        <p className="kb-pages-total">文档共 {formatCount(result.total_pages)} 页</p>
      ) : null}
      {!loading && !error && result?.truncated ? (
        <p className="kb-pages-total">超出的部分没有内容。</p>
      ) : null}

      {!loading && !error && (mode === 'image' || !textMissing) ? (
        <ol className="kb-page-list">
          {pages.map((page) => (
            <li key={page.page} className="kb-page">
              <p className="kb-page-head">第 {page.page} 页</p>
              {mode === 'image' ? (
                page.image_url ? (
                  // 一次最多 50 页，`loading="lazy"` 让视口外的页图不参与首屏
                  <img
                    className="kb-page-image"
                    src={page.image_url}
                    alt={`第 ${page.page} 页`}
                    loading="lazy"
                    decoding="async"
                  />
                ) : (
                  <p className="kb-page-empty">这一页没有原图。</p>
                )
              ) : page.text ? (
                <pre className="kb-page-text">{page.text}</pre>
              ) : (
                <p className="kb-page-empty">这一页没有解析文字。</p>
              )}
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  )
}
