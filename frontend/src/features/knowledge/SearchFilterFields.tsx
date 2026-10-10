/**
 * 检索面板「过滤条件」区的内容物：文档多选、页码范围、章节标题、入库时间。
 *
 * 为什么单列一个文件：这一块有两种边界各自要交代——**口径**（什么算填了一格、
 * 填错了怎么办、摘要那一行写什么）与**版式**（一个可折叠区里的若干行）。
 * 面板本身还要照顾判定条、命中列表与会话历史，混在一起两件事都读不清。
 *
 * 后端契约（`MetadataFilterIn`，字段说明随 `./schema.d.ts` 一起生成）里三条要点落在这一层：
 *
 * 1. **条件在候选生成之前生效**（下推到 SQL）。于是"这次命中少"是过滤的直接后果，
 *    不是相关度变差——命中摘要那边因此不再报"过滤掉几条"（那个数现在恒为 0）。
 * 2. **页码与标题是排除性条件**：`chunk.page` / `heading_path` 为空的块（`.md`、
 *    Office 来源）会被整体排除。用户按页码筛一个纯 Markdown 库会一条都拿不到，
 *    所以那两格下面各有一句**字段级提示**——它是"这一格该怎么填"，不是对界面的解释。
 * 3. **日期只到"天"**：下界取当天零点、上界取当天最后一刻（后端说"含边界"）。
 *    直接发 `2025-01-01` 这种日期串的话，上界落在当天零点，"起止填同一天"会一条都查不到，
 *    而那种边界错误从界面上看不出来。
 */
import { Checkbox } from '@/ui/checkbox'
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'

import type { SearchFilters } from '@/api/search'
import { parseIntOrNull } from '@/features/knowledge/chunking'

/** 文档多选的候选项：只要 id 与显示名两件东西。 */
export interface DocumentOption {
  id: string
  name: string
}

/** 过滤区的草稿。数字与日期都留成字符串交给原生输入，提交时再解析。 */
export interface SearchFilterDraft {
  documentIds: string[]
  pageFrom: string
  pageTo: string
  heading: string
  createdAfter: string
  createdBefore: string
}

/** 一件条件都没有的草稿（面板首次打开、换库时的初值）。 */
export const EMPTY_SEARCH_FILTER_DRAFT: SearchFilterDraft = {
  documentIds: [],
  pageFrom: '',
  pageTo: '',
  heading: '',
  createdAfter: '',
  createdBefore: '',
}

/**
 * 文档多选一次取多少篇。
 *
 * 后端一页上限 200，这里就取满——"只查某本书"要在那一堆文档里勾出来，
 * 20 篇一页意味着还得自己翻页找。整个库超过 200 篇时只列前 200（够用，且不再往下翻）。
 */
export const DOCUMENT_OPTIONS_LIMIT = 200

/** 页码那两格的校验；返回中文提示，空串 = 通过（空串 = 这一头不限）。 */
export function pageRangeError(pageFrom: string, pageTo: string): string {
  const from = parseIntOrNull(pageFrom)
  const to = parseIntOrNull(pageTo)
  if (pageFrom.trim() !== '' && (from === null || from < 1)) return '起始页要填 1 或更大的整数'
  if (pageTo.trim() !== '' && (to === null || to < 1)) return '结束页要填 1 或更大的整数'
  if (from !== null && to !== null && to < from) return '结束页不能小于起始页'
  return ''
}

/** 入库时间那两格的校验（日期就是 `YYYY-MM-DD`，字符串比较即时间先后）。 */
export function dateRangeError(createdAfter: string, createdBefore: string): string {
  if (createdAfter && createdBefore && createdBefore < createdAfter) {
    return '结束日期不能早于起始日期'
  }
  return ''
}

/**
 * 草稿 → 请求里的 `filters`。
 *
 * 一件条件都没有时返回 `undefined`：**不筛与筛了个空是两件事**，别用一个空对象去表达前者。
 * 页码只认 ≥1 的整数（后端那一项是 `ge=1`，本地拦下过就不会换来一次 422）。
 */
export function buildSearchFilters(draft: SearchFilterDraft): SearchFilters | undefined {
  const filters: SearchFilters = {}

  if (draft.documentIds.length > 0) filters.document_ids = [...draft.documentIds]

  const from = parseIntOrNull(draft.pageFrom)
  if (from !== null && from >= 1) filters.page_from = from
  const to = parseIntOrNull(draft.pageTo)
  if (to !== null && to >= 1) filters.page_to = to

  const heading = draft.heading.trim()
  if (heading) filters.heading_contains = heading

  if (draft.createdAfter) filters.created_after = `${draft.createdAfter}T00:00:00`
  if (draft.createdBefore) filters.created_before = `${draft.createdBefore}T23:59:59.999999`

  return Object.keys(filters).length > 0 ? filters : undefined
}

/**
 * 已生效条件的摘要（每项一条，标签短到能并排看）。
 *
 * 文档名要照着勾选时的清单翻出来：只报"1 篇"的话，收起过滤区之后就认不出查的是哪本书。
 * 名字翻不到（清单还没回来、或它已不在候选里）时退回条数，不空着。
 */
export function filterSummaryOf(
  draft: SearchFilterDraft,
  documents: readonly DocumentOption[],
): string[] {
  const chips: string[] = []

  if (draft.documentIds.length === 1) {
    const name = documents.find((item) => item.id === draft.documentIds[0])?.name
    chips.push(name ? `文档：${name}` : '文档：1 篇')
  } else if (draft.documentIds.length > 1) {
    chips.push(`文档：${draft.documentIds.length} 篇`)
  }

  const from = parseIntOrNull(draft.pageFrom)
  const to = parseIntOrNull(draft.pageTo)
  const hasFrom = from !== null && from >= 1
  const hasTo = to !== null && to >= 1
  if (hasFrom && hasTo) chips.push(`第 ${from}–${to} 页`)
  else if (hasFrom) chips.push(`第 ${from} 页起`)
  else if (hasTo) chips.push(`到第 ${to} 页`)

  const heading = draft.heading.trim()
  if (heading) chips.push(`标题含「${heading}」`)

  if (draft.createdAfter) chips.push(`${draft.createdAfter} 起`)
  if (draft.createdBefore) chips.push(`至 ${draft.createdBefore}`)

  return chips
}

interface SearchFilterFieldsProps {
  draft: SearchFilterDraft
  onChange: (patch: Partial<SearchFilterDraft>) => void
  /** 当前库的文档（多选的候选）。 */
  documents: readonly DocumentOption[]
  documentsLoading: boolean
  /** 文档清单没读回来的原因；空串 = 读到了。 */
  documentsError: string
  /** 页码与日期各自的字段级提示，空串 = 通过。 */
  pageError: string
  dateError: string
}

export function SearchFilterFields({
  draft,
  onChange,
  documents,
  documentsLoading,
  documentsError,
  pageError,
  dateError,
}: SearchFilterFieldsProps) {
  function toggleDocument(id: string): void {
    onChange({
      documentIds: draft.documentIds.includes(id)
        ? draft.documentIds.filter((item) => item !== id)
        : [...draft.documentIds, id],
    })
  }

  return (
    <div className="kb-filter-body">
      <div className="field">
        <span className="field-label">文档</span>
        {documentsLoading ? <p className="text-micro">正在读取文档…</p> : null}
        {!documentsLoading && documentsError ? (
          <p className="kb-filter-error">{documentsError}</p>
        ) : null}
        {!documentsLoading && !documentsError && documents.length === 0 ? (
          <p className="text-micro">这个库里还没有文档</p>
        ) : null}
        {documents.length > 0 ? (
          <>
            <div className="kb-filter-picked">
              <span>
                {draft.documentIds.length > 0
                  ? `已选 ${draft.documentIds.length} 篇`
                  : '未选文档 = 查全库'}
              </span>
              {draft.documentIds.length > 0 ? (
                <button
                  type="button"
                  className="kb-filter-clear"
                  onClick={() => onChange({ documentIds: [] })}
                >
                  清空
                </button>
              ) : null}
            </div>
            <ul className="kb-filter-docs">
              {documents.map((document) => (
                <li key={document.id}>
                  <Label className="w-full min-w-0 cursor-pointer text-[length:var(--text-meta-size)] font-normal">
                    <Checkbox
                      checked={draft.documentIds.includes(document.id)}
                      aria-label={document.name}
                      onCheckedChange={() => toggleDocument(document.id)}
                    />
                    <span className="min-w-0 flex-1 truncate" title={document.name}>
                      {document.name}
                    </span>
                  </Label>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>

      <div className="field">
        <span className="field-label">页码范围</span>
        <div className="kb-filter-pair">
          <Input
            type="number"
            min={1}
            value={draft.pageFrom}
            placeholder="起"
            aria-label="起始页"
            aria-invalid={pageError ? true : undefined}
            onChange={(event) => onChange({ pageFrom: event.target.value })}
          />
          <Input
            type="number"
            min={1}
            value={draft.pageTo}
            placeholder="止"
            aria-label="结束页"
            aria-invalid={pageError ? true : undefined}
            onChange={(event) => onChange({ pageTo: event.target.value })}
          />
        </div>
        {pageError ? (
          <p className="kb-filter-error">{pageError}</p>
        ) : (
          <p className="text-hint">没有页码的块会被排除（Markdown、Office 来源常见）</p>
        )}
      </div>

      <div className="field">
        <span className="field-label">章节标题包含</span>
        <Input
          value={draft.heading}
          placeholder="如：第3章"
          aria-label="章节标题包含"
          onChange={(event) => onChange({ heading: event.target.value })}
        />
        <p className="text-hint">标题为空的块会被排除</p>
      </div>

      <div className="field">
        <span className="field-label">入库时间</span>
        <div className="kb-filter-pair">
          <Input
            type="date"
            value={draft.createdAfter}
            aria-label="入库时间起"
            aria-invalid={dateError ? true : undefined}
            onChange={(event) => onChange({ createdAfter: event.target.value })}
          />
          <Input
            type="date"
            value={draft.createdBefore}
            aria-label="入库时间止"
            aria-invalid={dateError ? true : undefined}
            onChange={(event) => onChange({ createdBefore: event.target.value })}
          />
        </div>
        {dateError ? <p className="kb-filter-error">{dateError}</p> : null}
      </div>
    </div>
  )
}
