/**
 * 表格（.xlsx）预览：`exceljs` 读文件，我们自己画一张虚拟化的网格。
 *
 * 上游用法（v4.4.0 的 `lib/xlsx/xlsx.js`，类型在 `index.d.ts`）：
 *
 * ```ts
 * const workbook = new Workbook()
 * await workbook.xlsx.load(arrayBuffer)   // 只读 OOXML（.xlsx/.xlsm）
 * ```
 *
 * 为什么不是"把 xlsx 交给某个现成的 React 表格组件"：
 *
 * - 旧前端用的 `@vue-office/excel` 是 Vue 专属，且它底下那套 `x-data-spreadsheet`
 *   把整张表当编辑器（带公式栏、单元格编辑）——预览页要的是**只读地看**；
 * - 新前端已装 `exceljs` + `@tanstack/react-virtual`，两者拼起来就是"读 + 画"，
 *   不引入第二套 CSS 体系（那套样式还会跟我们的主题打架）。
 *
 * 四件上游的实事（都影响这个组件的形状）：
 *
 * 1. **`load` 只认 OOXML**：老式 `.xls`（OLE2 二进制）会解析失败。
 *    分派表仍把它归到这一档（与旧表一致），失败时给一句专门的说明
 *    ——见 `LEGACY_XLS_NOTE`。
 * 2. **`cell.text` 是"显示用"的字符串**：日期、百分比、货币这些数字格式它已经按
 *    文件里的 `numFmt` 格式好了（`cell.value` 是原始值）。界面直接用 `text`，
 *    只有公式要额外看一眼 `cell.value`。
 * 3. **exceljs 不求值**：公式格只有文件里**缓存的结果**（`.text`）。
 *    结果拿不到时把公式本身显示出来（`=SUM(...)`），比显示空白诚实。
 * 4. **`worksheet.findRow` / `row.findCell` 不创建行/格**：虚拟化滚动时要按索引
 *    取格子，用 `getRow` 会把"滚过去"变成"在内存里造出来"（百万行的表就是灾难）。
 *
 * 表头固定 + 虚拟化：行、列各一个 `useVirtualizer`，共用同一个滚动容器。
 * 表头与行号用 `position: sticky`（它们在流里，粘得住；绝对定位的元素粘不住），
 * 所以网格模板是"行号 + 左占位 + 可见列 + 右占位"，上下留白用整行的占位格撑开。
 */
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Cell, Color, Workbook, Worksheet } from 'exceljs'
import { Fragment, useEffect, useId, useRef, useState } from 'react'

import {
  CANNOT_PREVIEW,
  PreviewLoading,
  PreviewNote,
  PreviewUnavailable,
  loadFailure,
  reasonOf,
} from './notes'
import { useRemoteBuffer } from './usePreviewSource'
import './preview.css'

export interface SpreadsheetPreviewProps {
  /** 后端签发的原件链接（相对路径）。 */
  url?: string | null
  /** 文件名：只用于报错文案。 */
  name?: string
}

/** 一次画到多少行为止。**是截断，不是分页**：说清"看到的不是全部"比悄悄截断好。 */
const MAX_ROWS = 10_000
/** 列的上限（Excel 自己也就 16384 列，超过这个数的表在抽屉里没有阅读价值）。 */
const MAX_COLS = 256
/** 结构常量：行号列宽、表头高（与 `preview.css` 里的 24px 同一处取值）。 */
const ROW_HEADER_WIDTH = 48
const HEADER_HEIGHT = 24
const DEFAULT_ROW_HEIGHT = 22
const DEFAULT_COL_WIDTH = 96
const MIN_COL_WIDTH = 56
const MAX_COL_WIDTH = 360

/** exceljs 的入口是 CJS（`module.exports = { Workbook, ... }`），两种形态下都得从属性上取。 */
type ExcelJsModule = { Workbook?: typeof Workbook; default?: { Workbook?: typeof Workbook } }

/** 老式 `.xls` 专门的一句：不是"文件坏了"，而是**这个格式读不了**。 */
const LEGACY_XLS_NOTE = '老式 .xls（二进制格式）解析不了，只有 .xlsx / .xlsm 能在这里预览'

interface SheetCellView {
  text: string
  formula?: string
  bold?: boolean
  italic?: boolean
  underline?: boolean
  /** 字体颜色（文档里指定的颜色是行内样式，跟主题无关）。 */
  color?: string
  /** 单元格底色。 */
  fill?: string
  align?: 'left' | 'center' | 'right'
}

interface SheetMeta {
  rowCount: number
  colCount: number
  /** 每一列的像素宽（下标 0 = A 列）。 */
  widths: number[]
  /** 被上限截断了：界面下方要说明。 */
  truncated: boolean
}

function argbToCss(color: Partial<Color> | undefined): string | undefined {
  const argb = color?.argb
  if (!argb) return undefined // theme/indexed 色的解析要读主题表，这里不承诺
  const hex = argb.length === 8 ? argb.slice(2) : argb
  return /^[0-9a-f]{6}$/i.test(hex) ? `#${hex.toLowerCase()}` : undefined
}

function columnLetter(columnNumber: number): string {
  let rest = columnNumber
  let label = ''
  while (rest > 0) {
    const remainder = (rest - 1) % 26
    label = String.fromCharCode(65 + remainder) + label
    rest = Math.floor((rest - 1) / 26)
  }
  return label
}

/** 列宽：Excel 的宽度单位是"字符数"，按 7px/字符 + 5px 换算，并卡在可读区间里。 */
function columnPixels(worksheet: Worksheet, columnNumber: number): number {
  const width = worksheet.getColumn(columnNumber)?.width
  if (!width || width <= 0) return DEFAULT_COL_WIDTH
  return Math.min(MAX_COL_WIDTH, Math.max(MIN_COL_WIDTH, Math.round(width * 7 + 5)))
}

/** 行高：Excel 存的是磅（pt），屏幕上按 96dpi 换算成像素。 */
function rowPixels(row: { height?: number } | undefined): number {
  const height = row?.height
  if (!height || height <= 0) return DEFAULT_ROW_HEIGHT
  return Math.max(16, Math.round((height * 4) / 3))
}

function horizontal(align: SheetCellView['align']): string {
  if (align === 'center') return 'center'
  if (align === 'right') return 'flex-end'
  return 'flex-start'
}

/**
 * 读一个格子。返回 `null` = 这个格子没有可显示的东西（空值、且没有样式值得画）。
 *
 * **不创建行/格**（见文件头注第 4 条）：`findRow` / `findCell` 找不到就返回 `undefined`。
 */
function readCell(
  worksheet: Worksheet,
  rowNumber: number,
  columnNumber: number,
): SheetCellView | null {
  const cell: Cell | undefined = worksheet.findRow(rowNumber)?.findCell(columnNumber)
  if (!cell) return null
  // 合并区里被盖住的格子留空：只画左上角那个格子的值（不做跨行跨列，见 README 的限制）
  if (cell.isMerged && cell.master && cell.master.address !== cell.address) return null

  const value = cell.value as unknown
  let formula: string | undefined
  if (value && typeof value === 'object') {
    if ('formula' in value && typeof (value as { formula?: unknown }).formula === 'string') {
      formula = (value as { formula: string }).formula
    } else if ('sharedFormula' in value) {
      const shared = (value as { sharedFormula?: unknown }).sharedFormula
      if (typeof shared === 'string') formula = shared
    }
  }

  const view: SheetCellView = {
    text: cell.text ?? '',
    formula,
    bold: cell.font?.bold || undefined,
    italic: cell.font?.italic || undefined,
    underline: cell.font?.underline ? true : undefined,
    color: argbToCss(cell.font?.color),
    fill: cell.fill && cell.fill.type === 'pattern' ? argbToCss(cell.fill.fgColor) : undefined,
    align:
      cell.alignment?.horizontal === 'center' || cell.alignment?.horizontal === 'right'
        ? (cell.alignment.horizontal as 'center' | 'right')
        : undefined,
  }
  // 公式没有缓存结果时，把公式本身当文本显示（"=SUM(A1:A3)"），别给一个空白格
  if (!view.text && formula) view.text = `=${formula}`
  const empty =
    !view.text && !view.fill && !view.bold && !view.italic && !view.underline && !view.align
  return empty ? null : view
}

function sheetMeta(worksheet: Worksheet): SheetMeta {
  const dimensions = worksheet.dimensions
  const lastRow = Math.max(dimensions?.bottom ?? 0, worksheet.rowCount || 0)
  const lastColumn = Math.max(dimensions?.right ?? 0, worksheet.columnCount || 0)
  const rowCount = Math.min(lastRow, MAX_ROWS)
  const colCount = Math.min(lastColumn, MAX_COLS)
  const widths: number[] = []
  for (let index = 1; index <= colCount; index += 1) widths.push(columnPixels(worksheet, index))
  return { rowCount, colCount, widths, truncated: lastRow > rowCount || lastColumn > colCount }
}

/** 一张工作表的网格。抽成子组件是为了**切 sheet 时把虚拟化状态一并重置**（见 `key`）。 */
function SheetGrid({ worksheet, meta }: { worksheet: Worksheet; meta: SheetMeta }) {
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const rowVirtualizer = useVirtualizer({
    count: meta.rowCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => rowPixels(worksheet.findRow(index + 1)),
    overscan: 6,
    initialRect: { width: 900, height: 480 },
  })
  const columnVirtualizer = useVirtualizer({
    horizontal: true,
    count: meta.colCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => meta.widths[index] ?? DEFAULT_COL_WIDTH,
    overscan: 2,
    initialRect: { width: 900, height: 480 },
  })

  const rows = rowVirtualizer.getVirtualItems()
  const columns = columnVirtualizer.getVirtualItems()
  const totalHeight = rowVirtualizer.getTotalSize()
  const totalWidth = columnVirtualizer.getTotalSize()
  const firstRow = rows[0]
  const lastRow = rows[rows.length - 1]
  const firstColumn = columns[0]
  const lastColumn = columns[columns.length - 1]
  /** 上下左右的留白：用整格占位撑开滚动高度，可见部分仍然是流里的正常元素。 */
  const gapTop = firstRow ? firstRow.start : 0
  const gapBottom = lastRow ? Math.max(0, totalHeight - lastRow.end) : 0
  const gapLeft = firstColumn ? firstColumn.start : 0
  const gapRight = lastColumn ? Math.max(0, totalWidth - lastColumn.end) : 0

  const template = [
    `${ROW_HEADER_WIDTH}px`,
    `${gapLeft}px`,
    ...columns.map((column) => `${column.size}px`),
    `${gapRight}px`,
  ].join(' ')

  return (
    <div className="kylab-sheet-viewport" ref={scrollRef} data-testid="sheet-viewport">
      <div className="kylab-sheet-grid" style={{ gridTemplateColumns: template }}>
        <div className="kylab-sheet-corner" style={{ height: HEADER_HEIGHT }} aria-hidden />
        <div className="kylab-sheet-gap" style={{ width: gapLeft }} />
        {columns.map((column) => (
          <div
            key={column.key}
            className="kylab-sheet-colhead"
            style={{ height: HEADER_HEIGHT }}
            aria-hidden
          >
            {columnLetter(column.index + 1)}
          </div>
        ))}
        <div className="kylab-sheet-gap" style={{ width: gapRight }} />
        <div className="kylab-sheet-spacer" style={{ height: gapTop }} aria-hidden />

        {rows.map((row) => {
          const rowNumber = row.index + 1
          return (
            <Fragment key={row.key}>
              <div className="kylab-sheet-rowhead" style={{ height: row.size }} aria-hidden>
                {rowNumber}
              </div>
              <div className="kylab-sheet-gap" style={{ width: gapLeft }} />
              {columns.map((column) => {
                const cell = readCell(worksheet, rowNumber, column.index + 1)
                return (
                  <div
                    key={column.key}
                    className="kylab-sheet-cell"
                    style={{
                      height: row.size,
                      justifyContent: horizontal(cell?.align),
                      background: cell?.fill,
                      color: cell?.color,
                      fontWeight: cell?.bold ? 600 : undefined,
                      fontStyle: cell?.italic ? 'italic' : undefined,
                      textDecoration: cell?.underline ? 'underline' : undefined,
                    }}
                    title={cell?.text}
                  >
                    {cell?.formula ? (
                      <span className="kylab-sheet-fx" title={`=${cell.formula}`}>
                        fx
                      </span>
                    ) : null}
                    <span className="kylab-sheet-fx-label">{cell?.text}</span>
                  </div>
                )
              })}
              <div className="kylab-sheet-gap" style={{ width: gapRight }} />
            </Fragment>
          )
        })}

        <div className="kylab-sheet-spacer" style={{ height: gapBottom }} aria-hidden />
      </div>
    </div>
  )
}

export function SpreadsheetPreview({ url, name }: SpreadsheetPreviewProps) {
  const { loading, failure, buffer } = useRemoteBuffer(url)
  const [workbook, setWorkbook] = useState<Workbook | null>(null)
  const [parseFailure, setParseFailure] = useState('')
  const [active, setActive] = useState(0)
  const baseId = useId()

  useEffect(() => {
    if (!buffer) return
    let alive = true
    setWorkbook(null)
    setParseFailure('')
    void (async () => {
      try {
        const module = (await import('exceljs')) as unknown as ExcelJsModule
        // 命名导入在 UMD/CJS 混用时行为随打包器变（dev 与 build 不一致最难查），
        // 所以这里显式从属性上取（见文件头注的 exceljs 入口说明）
        const WorkbookClass = module.Workbook ?? module.default?.Workbook
        if (!WorkbookClass) throw new Error('exceljs 没有导出 Workbook')
        const book = new WorkbookClass()
        await book.xlsx.load(buffer)
        if (!alive) return
        setWorkbook(book)
        setActive(0)
      } catch (cause) {
        if (alive) setParseFailure(reasonOf(cause, CANNOT_PREVIEW))
      }
    })()
    return () => {
      alive = false
    }
  }, [buffer])

  if (failure) return <PreviewUnavailable name={name} reason={loadFailure(failure)} />
  if (parseFailure) {
    // 老式 .xls 单独一句：它不是"文件坏了"，而是这个格式读不了（见文件头注第 1 条）
    const legacy = name?.toLowerCase().endsWith('.xls')
    return (
      <PreviewUnavailable
        name={name}
        reason={legacy ? `${LEGACY_XLS_NOTE}（${parseFailure}）` : loadFailure(parseFailure)}
      />
    )
  }
  if (loading || !buffer || !workbook) return <PreviewLoading />

  const sheets = workbook.worksheets ?? []
  if (sheets.length === 0) return <PreviewNote>这个工作簿里没有工作表。</PreviewNote>
  const index = Math.min(active, sheets.length - 1)
  const sheet = sheets[index]
  const meta = sheetMeta(sheet)

  return (
    <div className="kylab-sheet">
      <div className="kylab-sheet-tabs" role="tablist" aria-label="工作表">
        {sheets.map((item, itemIndex) => (
          <button
            key={item.id}
            id={`${baseId}-tab-${itemIndex}`}
            type="button"
            role="tab"
            className="kylab-sheet-tab"
            aria-selected={itemIndex === index}
            aria-controls={`${baseId}-panel`}
            onClick={() => setActive(itemIndex)}
          >
            {item.name}
          </button>
        ))}
      </div>
      {/* `key` 绑工作表 id：换一张表就是换一个组件实例，滚动位置与虚拟化的
          测量缓存跟着一起重置——否则切过去会停在上一张表的滚动位置 */}
      <div id={`${baseId}-panel`} role="tabpanel" aria-label={sheet.name}>
        {meta.rowCount === 0 || meta.colCount === 0 ? (
          <PreviewNote>这张工作表是空的。</PreviewNote>
        ) : (
          <SheetGrid key={sheet.id} worksheet={sheet} meta={meta} />
        )}
      </div>
      {meta.truncated ? (
        <p className="kylab-sheet-note">
          这张表比这里能画的大：只显示前 {MAX_ROWS} 行、前 {MAX_COLS} 列。下载原文能看全。
        </p>
      ) : null}
    </div>
  )
}
