/**
 * 知识库详情页：文档列表（《前端设计规范》§6）——旧 `views/KnowledgeBaseView.vue` 逐条对齐。
 *
 * 形态：文档是"条目型"对象且会很多，用**列表**而不是卡片。
 * 每行 = 类型图标 + 名称 + 状态（文字 + 语义色）+ 时间；大文件可展开子文件树。
 *
 * 上传走"提交后轮询"：后端是异步流水线（架构 §4），界面必须能看见任务在动，
 * 否则用户会以为"点了没反应"。
 *
 * 与旧实现一致的三处关键约定：
 * - **抽屉的状态放 URL**（`?doc=`）：刷新、分享链接、后退都能回到同一个画面；
 * - **翻页不清空勾选**（跨页累计），界面上如实说明"另有 N 篇不在本页"；
 * - **删除前先给人看影响清单**："删除该文档"没人会有感觉，"412 个切块"才会让人停一下。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router'
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Download,
  Folder as FolderIcon,
  Inbox,
  Library,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Share2,
  Trash2,
  Upload,
  X,
} from 'lucide-react'

import {
  batchDocuments,
  cancelDocument,
  deleteDocument,
  downloadDocument,
  getDocumentImpact,
  listDocumentParts,
  listDocuments,
  moveDocument,
  renameDocument,
  reprocessDocument,
  setDocumentDisabled,
  DOCUMENT_PAGE_SIZE,
  type DataSourceKind,
  type DocumentBatchAction,
  type DocumentBatchResult,
  type DocumentListFilter,
  type DocumentPart,
  type DocumentStage,
  type DocumentSummary,
  type ImpactReport,
} from '@/api/documents'
import { createFolder, deleteFolder, listFolders, renameFolder, type Folder } from '@/api/folders'
import {
  docListViewKey,
  getKbCacheDocuments,
  getKbCacheFolders,
  revalidateKbCache,
} from '@/api/kbCache'
import { providerGateApplies } from '@/api/provider'
import { MeterBar, SkeletonRows, StatusTag } from '@/features/knowledge/composites'
import { DocumentDrawer } from '@/features/knowledge/DocumentDrawer'
import { KbSearchPanel } from '@/features/knowledge/KbSearchPanel'
import { KnowledgeBaseSettings } from '@/features/knowledge/KnowledgeBaseSettings'
import { ShareDialog } from '@/features/knowledge/ShareDialog'
import {
  LIVE_FRAME,
  createSnapshotGate,
  docListViewOf,
  revalidateTargetOf,
  snapshotFrame,
  snapshotNote,
  snapshotPayload,
  snapshotRows,
  withoutProgress,
  type SnapshotFrame,
} from '@/features/knowledge/snapshot'
import { messageOf, notify, useKnowledgeBases, usePolling } from '@/features/knowledge/store'
import { useOperatorStore } from '@/lib/operator'
import {
  documentSourceLabel,
  documentStageView,
  FILTER_STAGE_KEYS,
} from '@/features/knowledge/status'
import { UploadDialog } from '@/features/knowledge/UploadDialog'
import { useUploadLimits } from '@/features/knowledge/uploadLimits'
import {
  formatBytes,
  formatCount,
  formatDate,
  formatMillis,
  formatRelativeTime,
} from '@/lib/format'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/alert-dialog'
import { Button } from '@/ui/button'
import { Checkbox } from '@/ui/checkbox'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/ui/dropdown-menu'
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'
import { RadioGroup, RadioGroupItem } from '@/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'

const POLL_INTERVAL_MS = 2000
/** 处于这些阶段的文档还在动，需要轮询刷新。 */
const ACTIVE_STAGES = new Set([
  'uploaded',
  'probing',
  'parsing',
  'parsed',
  'chunking',
  'chunked',
  'embedding',
  'enriching',
])
/** 目录筛选的三种取值：`''` 全部 / `ROOT` 未归档 / 其余是目录 id。 */
const ROOT_FILTER = '__root__'
/** 搜文件名是逐键触发的：不防抖就会每敲一个字发一次请求。 */
const SEARCH_DEBOUNCE_MS = 300
/** 与 CSS 的 `--row-height` 一致：补白行按它算，两处漂了就会算错行数。 */
const ROW_HEIGHT = 44

/**
 * 「全部」那一项的取值。
 *
 * Radix 的 `<Select.Item>` **不接受空串**（空串被它当成"没有值"，触发器会退回占位符），
 * 而这里的值域里 `''` 正是"不带这个筛选条件"。所以界面上用哨兵值，进出都换回 `''`
 * ——送给接口的筛选对象（`stageFilter` / `sourceFilter`）一点没变。
 */
const ALL_STAGES = '__all_stages__'
const ALL_SOURCES = '__all_sources__'

const STAGE_FILTER_OPTIONS = [
  { value: ALL_STAGES, label: '全部状态' },
  ...FILTER_STAGE_KEYS.map((stage) => ({ value: stage, label: documentStageView(stage).label })),
]
/** 只列真实可能出现的来源：webdav 是框架预留、MVP 不实现，摆上去就是点了没反应的死选项。 */
const SOURCE_FILTER_OPTIONS = [
  { value: ALL_SOURCES, label: '全部来源' },
  { value: 'upload', label: documentSourceLabel('upload') },
  { value: 'html', label: documentSourceLabel('html') },
  { value: 'rss', label: documentSourceLabel('rss') },
]

/*
 * 「状态」与「上传者」两列的盒子（表头与数据行共用同一组取值）。
 *
 * 为什么要定宽：这两个盒子是**从左边开始**的列，宽度若随文案走（"失败"比"向量化中"窄、
 * 用户名有长有短），表头就没法同时对齐每一行——只能对齐"最常见的那一行"。定宽之后，
 * 表头与每一行的左边缘落在同一条竖线上（评审 K6：表里原先有三条左基准）。
 *
 * 取值按现有文案的最宽者留一点余量：状态最宽是"向量化中"（66px），上传者最长按 8 个
 * 半角字符算（80px），再长的用省略号，全文在抽屉的「上传者」里。
 */
const STATUS_CELL = { flex: '0 0 72px', display: 'flex', alignItems: 'center' }
const UPLOADER_CELL = {
  flex: '0 0 80px',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}
/** 表头里的「文件」：弹性、可被压缩到省略号——文件名那一列就是这么用的。 */
const HEAD_FILE_CELL = { flex: 1, minWidth: 0 }

/** 列表行的悬浮提示：文件名 + 摘要（v25）。摘要不占列，但对"这篇是啥"很省事。 */
function rowTitle(document: DocumentSummary): string {
  const summary = document.summary?.trim()
  return summary ? `${document.name}\n\n${summary}` : document.name
}

export interface KnowledgeBaseViewProps {
  /** 不传则从路由参数取（`/kb/:kbId`）。 */
  kbId?: string
}

export function KnowledgeBaseView({ kbId: kbIdProp }: KnowledgeBaseViewProps) {
  const params = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const store = useKnowledgeBases()

  const kbId = kbIdProp ?? String(params.kbId ?? '')
  const knowledgeBase = store.byId(kbId)
  /**
   * 名册（G6）由壳负责加载；这里只读它决定**要不要**显示"谁传的"那一列——
   * 名册为空（没配名册、或鉴权开着而没人名）时不摆一列"未记录"。
   */
  const roster = useOperatorStore((state) => state.roster)
  /** 上传约束（M3 阶段 6）：上限与格式提示从握手取，兜底见 `uploadLimits.ts`。 */
  const limits = useUploadLimits()

  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  /** 文档列表这一块是"上次看到的内容"还是刚取回来的（决定页顶那一行小字，§4.3-③）。 */
  const [frame, setFrame] = useState<SnapshotFrame>(LIVE_FRAME)
  /** 「先画一帧」的闸门（§4.3-①②）：文档列表 / 目录树 / 未归档计数三个视图各记各的账。 */
  const snapshotGate = useRef(createSnapshotGate()).current
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [searchDraft, setSearchDraft] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [stageFilter, setStageFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [activeFolder, setActiveFolder] = useState('')
  const [folders, setFolders] = useState<Folder[]>([])
  const [unfiledCount, setUnfiledCount] = useState<number | null>(null)
  const [treeOpen, setTreeOpen] = useState(true)
  const [selected, setSelected] = useState<string[]>([])
  const [expanded, setExpanded] = useState<Record<string, DocumentPart[] | undefined>>({})
  const [batchRunning, setBatchRunning] = useState(false)
  const [fillerRows, setFillerRows] = useState(0)
  const listPanel = useRef<HTMLDivElement | null>(null)

  const [searchOpen, setSearchOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)

  const [folderFormOpen, setFolderFormOpen] = useState(false)
  const [folderEditingId, setFolderEditingId] = useState('')
  const [folderDraft, setFolderDraft] = useState('')
  const [folderSaving, setFolderSaving] = useState(false)
  const [folderDeleteTarget, setFolderDeleteTarget] = useState<Folder | null>(null)
  const [folderDeleting, setFolderDeleting] = useState(false)

  const [moveTarget, setMoveTarget] = useState<DocumentSummary | null>(null)
  const [moveBatchIds, setMoveBatchIds] = useState<string[]>([])
  const [moveChoice, setMoveChoice] = useState('')
  const [moving, setMoving] = useState(false)

  const [renameTarget, setRenameTarget] = useState<DocumentSummary | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [renaming, setRenaming] = useState(false)

  const [deleteTarget, setDeleteTarget] = useState<DocumentSummary | null>(null)
  const [deleteImpact, setDeleteImpact] = useState<ImpactReport | null>(null)
  const [deleting, setDeleting] = useState(false)

  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)
  const [cancelTarget, setCancelTarget] = useState<DocumentSummary | null>(null)
  const [canceling, setCanceling] = useState('')

  /* ------------------------------------------------------------ 抽屉（状态在 URL） */

  const openDocumentId = searchParams.get('doc') ?? ''
  const tabParam = searchParams.get('tab') ?? ''
  const initialTab =
    tabParam === 'read' || tabParam === 'chunks' || tabParam === 'progress' ? tabParam : null
  const pageParam = Number(searchParams.get('page') ?? '') || null

  /** 打开一份文档：**去掉 `page` 与 `tab`**——它们只对"刚才点开的那一份"有意义。 */
  function documentLink(id: string): string {
    const next = new URLSearchParams(searchParams)
    next.delete('page')
    next.delete('tab')
    next.set('doc', id)
    return `${location.pathname}?${next.toString()}`
  }

  function closeDocument(): void {
    const next = new URLSearchParams(searchParams)
    next.delete('doc')
    next.delete('page')
    next.delete('tab')
    const query = next.toString()
    setSearchParams(query, { replace: true })
  }

  /* ------------------------------------------------------------ 先画一帧（M4 阶段 5） */

  const buildFilter = useCallback((): DocumentListFilter => {
    const filter: DocumentListFilter = {}
    if (activeFolder === ROOT_FILTER) filter.root = true
    else if (activeFolder) filter.folderId = activeFolder
    if (searchQuery) filter.q = searchQuery
    if (stageFilter) filter.stage = stageFilter as DocumentStage
    if (sourceFilter) filter.sourceKind = sourceFilter as DataSourceKind
    return filter
  }, [activeFolder, searchQuery, stageFilter, sourceFilter])

  /**
   * 「先画一帧」：把本机留的那份**这一视图**的内容先摆上来（§4.3 的时序规则）。
   *
   * 三条纪律逐条落在下面：
   *
   * - **只在「这个视图还没有实时结果」时画**：闸门的 `claim` 把"画过 / 已有实时结果 /
   *   正在飞"一并算掉——不满足就**连请求都不发**；带搜索或筛选的视图**没有副本**
   *   （决策点 D-D），压根不画（照旧骨架屏）；
   * - **实时结果永远赢**：回来时实时那条线已经落地了就把这一帧丢掉（`stillWanted`）；
   * - **只填第一帧**：画过（或那份内容本来就是空的）之后这个视图就记成"了结"（`settle`），
   *   不再画第二次——换到别的视图是另一个键，那是那个视图自己的第一帧。
   *
   * 失败静默：它是顺手快一点的那一下，页面自己那条实时读才是权威（也是报错的位置）。
   */
  const paintDocuments = useCallback(async () => {
    if (!kbId) return
    const view = docListViewOf(buildFilter(), page, DOCUMENT_PAGE_SIZE)
    if (!view) return
    const viewKey = docListViewKey(kbId, view)
    if (!snapshotGate.claim(viewKey)) return
    try {
      const snapshot = await getKbCacheDocuments(kbId, view)
      if (!snapshot?.available || !snapshotGate.stillWanted(viewKey)) return
      const rows = snapshotRows<DocumentSummary>(snapshot).map(withoutProgress)
      snapshotGate.settle(viewKey)
      // 空的那一份摆不上任何东西：不置帧（不置帧才不会一边摆骨架屏一边说"上次更新于 X"）
      if (rows.length === 0) return
      setDocuments(rows)
      setTotal(snapshotPayload<{ total?: number }>(snapshot)?.total ?? rows.length)
      setFrame(snapshotFrame(snapshot.fetched_at))
    } catch {
      // 见上：静默
    } finally {
      snapshotGate.release(viewKey)
    }
  }, [buildFilter, kbId, page, snapshotGate])

  /** 目录树那一份：与文档列表**同屏**，不做就是"半屏"。 */
  const paintFolders = useCallback(async () => {
    if (!kbId) return
    const viewKey = `folders:${kbId}`
    if (!snapshotGate.claim(viewKey)) return
    try {
      const snapshot = await getKbCacheFolders(kbId)
      if (!snapshot?.available || !snapshotGate.stillWanted(viewKey)) return
      const rows = snapshotRows<Folder>(snapshot)
      snapshotGate.settle(viewKey)
      if (rows.length === 0) return
      setFolders(rows)
    } catch {
      // 静默（与上面同）
    } finally {
      snapshotGate.release(viewKey)
    }
  }, [kbId, snapshotGate])

  /**
   * 「未归档」计数那一份：它就是文档列表的 `root` 视图加 `size:1`
   * （后端那边也是这一次读落成同一个 `doc_list` 的键，不必单开资源）。
   *
   * 这一份画的是**那个数**（0 也是内容：树上要显示"0"），所以它不按行数判空。
   */
  const paintCounts = useCallback(async () => {
    if (!kbId) return
    const viewKey = docListViewKey(kbId, { root: true, page: 1, size: 1 })
    if (!snapshotGate.claim(viewKey)) return
    try {
      const snapshot = await getKbCacheDocuments(kbId, { root: true, page: 1, size: 1 })
      if (!snapshot?.available || !snapshotGate.stillWanted(viewKey)) return
      const total = snapshotPayload<{ total?: number }>(snapshot)?.total
      if (typeof total !== 'number') return
      snapshotGate.settle(viewKey)
      setUnfiledCount(total)
    } catch {
      // 静默（与上面同）
    } finally {
      snapshotGate.release(viewKey)
    }
  }, [kbId, snapshotGate])

  /* ------------------------------------------------------------ 列表加载 */

  const loadDocuments = useCallback(async () => {
    if (!kbId) return
    const filter = buildFilter()
    // 这一读对应哪个视图（带筛选的那一档没有键：它本来就不留副本，D-D）
    const view = docListViewOf(filter, page, DOCUMENT_PAGE_SIZE)
    try {
      const list = await listDocuments(kbId, {
        ...filter,
        limit: DOCUMENT_PAGE_SIZE,
        offset: (page - 1) * DOCUMENT_PAGE_SIZE,
      })
      // 删除到某页空了（比如最后一页只剩 1 篇被删掉）就夹回最后一页再取，
      // 而不是给用户一个空列表——那看起来像"这个库没文档了"
      const pages = Math.max(1, Math.ceil(list.total / DOCUMENT_PAGE_SIZE))
      if (page > pages) {
        setPage(pages)
        setLoading(false)
        return
      }
      // 实时结果落地：这个视图从此不许再被快照盖（§4.3-②），小字那一行也就收了
      if (view) snapshotGate.live(docListViewKey(kbId, view))
      setDocuments(list.items)
      setTotal(list.total)
      setError('')
      setFrame(LIVE_FRAME)
    } catch (cause) {
      // 失败**不清**已经画上的那一帧：内容还在屏幕上，顶上那句话由它说（§4.5）
      setError(messageOf(cause, '文档列表加载失败'))
    } finally {
      setLoading(false)
    }
  }, [buildFilter, kbId, page, snapshotGate])

  const loadFolders = useCallback(async () => {
    if (!kbId) return
    try {
      setFolders((await listFolders(kbId)).items)
      snapshotGate.live(`folders:${kbId}`)
    } catch {
      // 目录读不到不该挡住文档列表：退化成"还没有目录"
      setFolders([])
    }
  }, [kbId, snapshotGate])

  /** 「未归档」计数：树上的数字要准，所以单独查一次根目录范围（`limit: 1` 只要那个 total）。 */
  const loadCounts = useCallback(async () => {
    if (!kbId) return
    try {
      setUnfiledCount((await listDocuments(kbId, { root: true, limit: 1 })).total)
      snapshotGate.live(docListViewKey(kbId, { root: true, page: 1, size: 1 }))
    } catch {
      setUnfiledCount(null)
    }
  }, [kbId, snapshotGate])

  const refreshAll = useCallback(async () => {
    await Promise.all([loadDocuments(), loadFolders(), loadCounts()])
  }, [loadDocuments, loadFolders, loadCounts])

  useEffect(() => {
    // 列表随筛选/页码变；`loadDocuments` 的依赖里带着这几项。
    // `loading` 只在**首次**那一轮让位给骨架屏（之后换筛选不该把整块列表换成骨架）
    if (documents.length === 0) setLoading(true)
    // 先画一帧（与那次实时读并行）：画上了就不摆骨架屏（§4.3-③）
    void paintDocuments()
    void loadDocuments()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadDocuments])

  useEffect(() => {
    void paintFolders()
    void loadFolders()
    void paintCounts()
    void loadCounts()
  }, [loadFolders, loadCounts, paintFolders, paintCounts])

  useEffect(() => {
    // 进页面时把库清单拉回来（标题与设置弹窗的"文档 N 篇"都要它）
    if (store.items.length === 0) void store.load()
    // 只做首屏这一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** 换库时把上一库的现场清掉（同一个组件被复用，路由参数变了不会重新挂载）。 */
  useEffect(() => {
    setDocuments([])
    setActiveFolder('')
    setSearchDraft('')
    setSearchQuery('')
    setStageFilter('')
    setSourceFilter('')
    setSelected([])
    setExpanded({})
    setPage(1)
    // 小字那一行也跟着清：上一库的"上次更新于 X"与新库无关
    setFrame(LIVE_FRAME)
  }, [kbId])

  /** 搜文件名逐键触发，但**防抖**：中文输入还会带上拼音中间态。 */
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = searchDraft.trim()
      if (next === searchQuery) return
      setSearchQuery(next)
      setPage(1)
      setSelected([])
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [searchDraft, searchQuery])

  /* ------------------------------------------------------------ 表格补白 */

  /**
   * 表格的行数**不跟着文件数走**：只有两三个文件时如果只画两三行，下面就是一大片空白，
   * 页面看着像没加载完。补的是**空行**（带分隔线），而不是把面板拉高。
   *
   * **一篇都没有时不补**（v0.14，界面评审第四批）：那一行是「这个知识库里还没有文档」，
   * 它自己就是这块区域的答案；底下再铺一屏装饰行，空态看起来像"列表还没渲染完"。
   * 补白只服务"有内容但填不满一屏"，空列表不属于这一类。
   */
  const syncFillerRows = useCallback(() => {
    if (documents.length === 0) {
      setFillerRows(0)
      return
    }
    const element = listPanel.current
    if (!element) return
    const head = element.querySelector<HTMLElement>('.panel-head')
    const headHeight = head?.offsetHeight ?? 36
    const bottomGap = 56
    const available = window.innerHeight - element.getBoundingClientRect().top - bottomGap
    const dataRows = documents.length
    const fit = Math.ceil((available - headHeight) / ROW_HEIGHT)
    setFillerRows(Math.max(0, Math.min(fit - dataRows, 60)))
  }, [documents.length])

  useEffect(() => {
    syncFillerRows()
    window.addEventListener('resize', syncFillerRows)
    return () => window.removeEventListener('resize', syncFillerRows)
  }, [syncFillerRows])

  /* ------------------------------------------------------------ 轮询与焦点 */

  /** 当前页还有没有在跑的文档。按时间倒序时它们都在第 1 页，所以只看当前页不会漏信号。 */
  const hasActive = documents.some((item) => ACTIVE_STAGES.has(item.stage))
  /** 出题任务不改变文档阶段，`hasActive` 看不到它，必须单列（否则"点了没反应"）。 */
  const questionsPending = documents.some((item) => item.questions_pending)
  // 轮询规则**一个字都没动**（§4.3-⑤）：还是只看"有没有在跑的活儿"
  usePolling(loadDocuments, {
    active: hasActive || questionsPending,
    intervalMs: POLL_INTERVAL_MS,
    immediate: false,
  })

  /**
   * 焦点回到窗口：先让本机确认**当前视图**那一份，再自己拉一次实时（§4.4 的接点）。
   *
   * 一次焦点**只确认一份**（当前文档列表那一键；带了搜索/筛选时它没有键，退而确认库详情
   * ——见 `revalidateTargetOf`）："一个页面三个视图一起炸"正是那一条要避免的。
   * `revalidate` 是**显式动作**（不是每次挂载都发，也不在这里轮询）；后面那次实时读是
   * **可见更新**——用户从别的窗口切回来看到的是刚确认过的内容。
   *
   * 门控：只有本机档才做（`providerGateApplies`）。服务器档的知识库就是它自己，
   * 那一档一个字都不改（与 M3 同一条纪律）。
   */
  useEffect(() => {
    const onFocus = () => {
      if (!providerGateApplies() || !kbId) return
      void revalidateKbCache(
        revalidateTargetOf(kbId, buildFilter(), page, DOCUMENT_PAGE_SIZE),
      ).then(() => refreshAll())
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [buildFilter, kbId, page, refreshAll])

  /* ------------------------------------------------------------ 多选 */

  const selectedCount = selected.length
  const allSelected = documents.length > 0 && documents.every((item) => selected.includes(item.id))
  const pageSelectedCount = documents.filter((item) => selected.includes(item.id)).length
  const someSelected = !allSelected && pageSelectedCount > 0
  const offPageSelected = useMemo(() => {
    const onPage = new Set(documents.map((item) => item.id))
    return selected.filter((id) => !onPage.has(id)).length
  }, [documents, selected])

  function toggleSelect(id: string): void {
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    )
  }

  /**
   * 表头复选框：**只切换当前页**在选中集里的去留。
   * 连点几页的表头 = 跨页全选（当前页已全选时把这一页移出，其他页保留）。
   */
  function toggleSelectAll(): void {
    if (allSelected) {
      const onPage = new Set(documents.map((item) => item.id))
      setSelected((current) => current.filter((id) => !onPage.has(id)))
      return
    }
    const merged = new Set(selected)
    for (const item of documents) merged.add(item.id)
    setSelected([...merged])
  }

  function dropFromSelection(ids: readonly string[]): void {
    if (selected.length === 0) return
    const gone = new Set(ids)
    setSelected((current) => current.filter((id) => !gone.has(id)))
  }

  /* ------------------------------------------------------------ 批量的统一骨架 */

  /**
   * 跑一次批量动作并**按逐条结果报账**。
   *
   * 四个入口（删除/重建、出题、停用/恢复、移动）共用同一套骨架：守卫 → 置忙 → 调接口
   * → 刷新 → 按"全成 / 部分失败"两套口径报账 → 复位。**文案仍由调用方给**：
   * 措辞是产品口径（出题要说"已排队"、停用要说"的检索"），抽象不该把它吞掉。
   *
   * "部分失败"单独一条路：批量里"10 篇删掉 9 篇"是**正常结果**，后端也因此不 reject。
   */
  async function runBatchAction(
    action: DocumentBatchAction,
    options: {
      verb: string
      success?: (result: DocumentBatchResult) => string
      partial?: (result: DocumentBatchResult, firstError: string) => string
      ids?: string[]
      all?: boolean
      refresh?: 'all' | 'list'
      clearOnSuccess?: boolean
      keepFailed?: boolean
    },
  ): Promise<void> {
    const ids = options.ids ?? [...selected]
    if (ids.length === 0 || batchRunning) return
    setBatchRunning(true)
    try {
      const result = await batchDocuments(kbId, action, ids, null, options.all ?? false)
      if (options.refresh === 'list') {
        await loadDocuments()
      } else {
        await refreshAll()
        // 汇总（每库的文档数）也要跟着变：删/移之后侧栏与设置弹窗的数字不能停在旧值
        void store.refreshSummaries()
      }

      if (result.failed === 0) {
        if (options.clearOnSuccess) setSelected([])
        notify.success(options.success?.(result) ?? `已${options.verb} ${result.succeeded} 篇`)
        return
      }

      const firstError = result.items.find((item) => !item.ok)?.error ?? ''
      notify.error(
        options.partial?.(result, firstError) ??
          `${options.verb}：${result.succeeded} 篇成功、${result.failed} 篇失败` +
            (firstError ? `（${firstError}）` : ''),
      )
      if (options.keepFailed) {
        setSelected(result.items.filter((item) => !item.ok).map((item) => item.document_id))
      }
    } catch (cause) {
      notify.error(messageOf(cause, `批量${options.verb}失败`))
    } finally {
      setBatchRunning(false)
    }
  }

  async function runBatch(action: 'delete' | 'reprocess'): Promise<void> {
    setBatchDeleteOpen(false)
    await runBatchAction(action, {
      verb: action === 'delete' ? '删除' : '重新摄入',
      clearOnSuccess: true,
      keepFailed: true,
    })
  }

  /**
   * 批量补生成分段问题（v24）：**非破坏性、也不清空选择**——文档还在、还想再生成一次
   * 都可能。它是异步任务，所以立刻给一句"已完成会自动刷新"的预期。
   */
  async function runBatchQuestions(): Promise<void> {
    await runBatchAction('questions', {
      verb: '生成问题',
      refresh: 'list',
      success: (result) => `已排队为 ${result.succeeded} 篇生成问题，完成后列表会自动刷新`,
      partial: (result, firstError) =>
        `生成问题：${result.succeeded} 篇已排队、${result.failed} 篇未排队` +
        (firstError ? `（${firstError}）` : ''),
    })
  }

  async function runBatchToggleDisabled(action: 'enable' | 'disable'): Promise<void> {
    const verb = action === 'disable' ? '停用' : '恢复'
    await runBatchAction(action, {
      verb,
      success: (result) => `已${verb} ${result.succeeded} 篇的检索`,
    })
  }

  /* ------------------------------------------------------------ 单篇动作 */

  async function reprocess(document: DocumentSummary): Promise<void> {
    try {
      await reprocessDocument(document.id)
      notify.success(`已重新提交「${document.name}」`)
      await loadDocuments()
    } catch (cause) {
      notify.error(messageOf(cause, '重跑失败'))
    }
  }

  async function onToggleDisabled(document: DocumentSummary): Promise<void> {
    try {
      await setDocumentDisabled(document.id, !document.disabled)
      await loadDocuments()
      notify.success(document.disabled ? '已恢复检索' : '已停用检索：文档与内容都保留')
    } catch (cause) {
      notify.error(messageOf(cause, '操作失败'))
    }
  }

  async function onDownload(document: DocumentSummary): Promise<void> {
    try {
      await downloadDocument(document.id)
    } catch (cause) {
      notify.error(messageOf(cause, '下载失败'))
    }
  }

  async function confirmRename(): Promise<void> {
    const target = renameTarget
    if (!target || renaming) return
    const name = renameDraft.trim()
    if (!name) {
      notify.error('文件名不能为空')
      return
    }
    setRenaming(true)
    try {
      await renameDocument(target.id, name)
      setRenameTarget(null)
      await loadDocuments()
      notify.success('已重命名')
    } catch (cause) {
      notify.error(messageOf(cause, '重命名失败'))
    } finally {
      setRenaming(false)
    }
  }

  async function confirmCancelParse(): Promise<void> {
    const target = cancelTarget
    if (!target || canceling) return
    setCanceling(target.id)
    try {
      await cancelDocument(target.id)
      setCancelTarget(null)
      await loadDocuments()
      notify.success('已取消解析')
    } catch (cause) {
      notify.error(messageOf(cause, '取消失败'))
    } finally {
      setCanceling('')
    }
  }

  /** 从行内菜单点删除：先开弹窗（带上目标），再去取影响清单。 */
  function openDelete(document: DocumentSummary): void {
    setDeleteTarget(document)
    setDeleteImpact(null)
    void getDocumentImpact(document.id)
      .then(setDeleteImpact)
      // 拿不到清单不阻断删除：弹窗会停在"正在统计影响…"，用户仍能取消
      .catch(() => setDeleteImpact(null))
  }

  async function confirmDelete(): Promise<void> {
    const target = deleteTarget
    if (!target || deleting) return
    setDeleting(true)
    try {
      await deleteDocument(target.id)
      setDeleteTarget(null)
      // 跨页选择下要**主动**摘掉这一篇：列表刷新只换当前页的内容
      dropFromSelection([target.id])
      notify.success('已删除，原文在回收站保留 7 天')
      await refreshAll()
    } catch (cause) {
      notify.error(messageOf(cause, '删除失败'))
    } finally {
      setDeleting(false)
    }
  }

  async function confirmMove(): Promise<void> {
    if (moving) return
    setMoving(true)
    const folderId = moveChoice || null
    try {
      if (moveBatchIds.length > 0) {
        const result = await batchDocuments(kbId, 'move', [...moveBatchIds], folderId)
        setMoveBatchIds([])
        await refreshAll()
        if (result.failed === 0) {
          setSelected([])
          notify.success(`已移动 ${result.succeeded} 篇`)
        } else {
          const reason = result.items.find((item) => !item.ok)?.error ?? ''
          notify.error(
            `移动：${result.succeeded} 篇成功、${result.failed} 篇失败${reason ? `（${reason}）` : ''}`,
          )
          setSelected(result.items.filter((item) => !item.ok).map((item) => item.document_id))
        }
        return
      }
      const single = moveTarget
      if (!single) return
      await moveDocument(single.id, folderId)
      setMoveTarget(null)
      dropFromSelection([single.id])
      await refreshAll()
      notify.success('已移动')
    } catch (cause) {
      notify.error(messageOf(cause, '移动失败'))
    } finally {
      setMoving(false)
    }
  }

  async function toggleParts(document: DocumentSummary): Promise<void> {
    if (expanded[document.id]) {
      setExpanded((current) => ({ ...current, [document.id]: undefined }))
      return
    }
    try {
      const parts = (await listDocumentParts(document.id)).items
      setExpanded((current) => ({ ...current, [document.id]: parts }))
    } catch (cause) {
      notify.error(messageOf(cause, '子文件加载失败'))
    }
  }

  /* ------------------------------------------------------------ 目录 */

  function startCreateFolder(): void {
    setFolderEditingId('')
    setFolderDraft('')
    setFolderFormOpen(true)
  }

  function startRenameFolder(folder: Folder): void {
    setFolderEditingId(folder.id)
    setFolderDraft(folder.name)
    setFolderFormOpen(true)
  }

  async function submitFolder(): Promise<void> {
    const name = folderDraft.trim()
    if (!name) {
      notify.error('请输入目录名')
      return
    }
    setFolderSaving(true)
    try {
      if (folderEditingId) await renameFolder(folderEditingId, name)
      else await createFolder(kbId, name)
      setFolderFormOpen(false)
      setFolderEditingId('')
      setFolderDraft('')
      await loadFolders()
      notify.success('目录已保存')
    } catch (cause) {
      notify.error(messageOf(cause, '保存失败'))
    } finally {
      setFolderSaving(false)
    }
  }

  async function confirmFolderDelete(): Promise<void> {
    const folder = folderDeleteTarget
    if (!folder || folderDeleting) return
    setFolderDeleting(true)
    try {
      await deleteFolder(folder.id)
      setFolderDeleteTarget(null)
      // 正在看的目录被删了：回到「全部」，否则列表会停在一个不存在的筛选上
      if (activeFolder === folder.id) setActiveFolder('')
      await refreshAll()
      notify.success('目录已删除')
    } catch (cause) {
      // 目录非空时后端会拒绝，消息里带"还有 N 篇"——原样透给用户
      notify.error(messageOf(cause, '删除失败'))
    } finally {
      setFolderDeleting(false)
    }
  }

  /* ------------------------------------------------------------ 展示口径 */

  const hasFilter = Boolean(searchDraft.trim() || stageFilter || sourceFilter)
  /** 空状态文案随筛选范围变——"这个库还没有文档"在目录或搜索里看到会误导。 */
  const emptyTitle = hasFilter
    ? '没有符合条件的文档'
    : activeFolder === ROOT_FILTER
      ? '根目录下还没有文档'
      : activeFolder
        ? '这个目录里还没有文档'
        : '这个知识库里还没有文档'

  /**
   * 这个库有没有目录。**它决定目录那一栏是纵向栏还是列表上方的一行**：没有目录时
   * 「全部文档」与「未归档」筛的是同一批文档，那一栏便只剩一句「还没有目录」。
   */
  const hasFolders = folders.length > 0
  /** 上传目标目录：只有选中了具体目录时才带上（"全部/未归档"都算根目录）。 */
  const uploadFolderId = activeFolder && activeFolder !== ROOT_FILTER ? activeFolder : undefined
  const foldersTotal = folders.reduce((sum, folder) => sum + (folder.document_count ?? 0), 0)
  /** 拿不到未归档数时回 null（界面不显示数字）——显示一个错的数比不显示更糟。 */
  const totalCount = unfiledCount === null ? null : unfiledCount + foldersTotal
  const pageCount = Math.max(1, Math.ceil(total / DOCUMENT_PAGE_SIZE))

  /**
   * 出题列文案：生成中 > 已出题条数 > 未生成 > 还没切块（出不了题）。
   *
   * **单位不写在格子里（单位上提）**：列头就叫「问题」，右边紧挨的「切块」列同样
   * 只有数字——一列写 222 题、一列写裸 74，读起来像两种东西。列头给单位、格子给数，
   * 两个数值列才对齐得上（全库规模那类表也是这个写法）。
   */
  function questionCell(document: DocumentSummary): string {
    if (document.questions_pending) return '生成中…'
    if (document.question_count > 0) return formatCount(document.question_count)
    if (document.chunk_count === 0) return '—'
    return '未生成'
  }

  function questionTitle(document: DocumentSummary): string {
    if (document.questions_pending) return '正在为这份文档的切块生成问题'
    if (document.question_count === 0) {
      return document.chunk_count === 0
        ? '还没有切块，无法出题'
        : '还没有为这份文档生成切块问题（选中后可点「生成问题」）'
    }
    return `${formatCount(document.questioned_chunk_count)} / ${formatCount(document.chunk_count)} 段有问题，共 ${formatCount(document.question_count)} 题`
  }

  function goToPage(next: number): void {
    const clamped = Math.min(Math.max(1, next), pageCount)
    if (clamped === page) return
    setPage(clamped)
  }

  function onKbChanged(action: 'renamed' | 'deleted' | 'sources'): void {
    if (action === 'deleted') {
      void navigate('/knowledge-bases')
      return
    }
    if (action === 'sources') void refreshAll()
  }

  /**
   * 页顶那一行小字（§4.5：在线「上次更新于 X」/ 断连「现在连不上，这是上次看到的内容（X）」）。
   *
   * 文档列表那一帧若是"上次看到的"，先说它的时间；否则看**库本身**那一份（标题与权限位
   * 从库列表来）——只要屏幕上还有一份是上次看到的，就说清楚。断连的判据是**这一页那次
   * 实时读失败**（`error`）：内容还在（快照那一帧没被清），但必须如实说"现在连不上"。
   */
  const snapshotShown: SnapshotFrame = frame.fromSnapshot ? frame : store
  const note = snapshotNote(snapshotShown, error !== '')

  return (
    <div className="page-shell">
      {/* 页头：标题 + **紧贴标题的**设置齿轮。
          旧前端把它放在 `PageHeader` 的 `title-suffix` 槽里，注释写明理由：
          "它是'这个库本身'的入口，不是页面的动作"——放进右侧动作区会被读成后者。
          所以这里也**不给 h1 `flex: 1`**：那会把齿轮推到页面的右缘去
          （对照记录 §3 第 3 条）。齿轮的显示条件与旧版一致：`can_write`。 */}
      <div className="kb-title-row">
        <h1>{knowledgeBase?.name ?? '知识库'}</h1>
        {knowledgeBase?.can_write ? (
          <KnowledgeBaseSettings kb={knowledgeBase} onChanged={onKbChanged} />
        ) : null}
      </div>

      {/* 快照那一帧的时间戳：实时结果一落地就收（§4.3-③） */}
      {note ? <p className="kb-snapshot-line">{note}</p> : null}

      {error ? <p className="kb-error-line">{error}</p> : null}

      {/*
        只读分享那句话**只在权限位确认过的时候说**。快照帧上 `can_write` 一律是假
        （D-B：那一位是按调用者身份算的，本机那份里根本没有它），照旧说"你是只读权限"
        就是把"未确认"说成了"确认过"——写入口因此晚一步出现，但**说法不能提前**。
      */}
      {knowledgeBase && !knowledgeBase.can_write && !store.fromSnapshot ? (
        <p className="kb-readonly-note">
          这是别人分享给你的库，你是只读权限：可以检索与查看，不能上传或删除。
        </p>
      ) : null}

      {/*
        目录树：**贯穿整个内容高度的侧栏**，不是浮在旁边的一个方块。

        但**没有目录时不摆这一栏**（`hasFolders`）：此刻它只说得出一句「还没有目录」，
        却常驻 200px 加一条分隔线，把本来就挤的七列表再压窄一档。这时它摊成列表上方的
        一行（`.kb-tree-flat`）——「目录 / 还没有目录 / 新建目录」三件事仍然都在原位，
        少掉的只是那一整栏空白。目录一旦建出来，纵向栏立刻回来。
      */}
      <div className={hasFolders ? 'kb-body' : 'kb-body kb-body-flat'}>
        {knowledgeBase ? (
          <aside className={hasFolders ? 'kb-tree' : 'kb-tree kb-tree-flat'} aria-label="目录">
            <div className="kb-tree-head">
              <span>目录</span>
              {hasFolders ? null : <span className="kb-tree-empty">还没有目录</span>}
              {/* 「+」原来是一颗 24px 的纯图标按钮：点不准，也读不出它是"新建目录"
                  （同一个「+」在本页还指"新建知识库"）。所以给按钮形态：图标 + 文字。 */}
              {knowledgeBase.can_write ? (
                <Button variant="ghost" size="sm" onClick={startCreateFolder}>
                  <Plus aria-hidden="true" />
                  新建目录
                </Button>
              ) : null}
            </div>

            {/* 没有目录时这一列不渲染：此刻「全部文档」「未归档」筛的是同一批文档
                （数字在下面的分页器上仍有），整列只说得出一句"还没有目录" */}
            {hasFolders ? (
              <ul className="kb-tree-list">
                <li>
                  <div
                    className={['kb-tree-row', activeFolder === '' ? 'kb-tree-row-on' : '']
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <button
                      type="button"
                      className="kb-tree-caret"
                      aria-expanded={treeOpen}
                      aria-label={treeOpen ? '收起目录' : '展开目录'}
                      onClick={() => setTreeOpen((value) => !value)}
                    >
                      {treeOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <button
                      type="button"
                      className="kb-tree-node"
                      aria-current={activeFolder === '' ? 'true' : undefined}
                      onClick={() => setActiveFolder('')}
                    >
                      <Inbox size={14} />
                      <span className="kb-tree-label">全部文档</span>
                      {totalCount !== null ? (
                        <span className="kb-tree-count tabular">{formatCount(totalCount)}</span>
                      ) : null}
                    </button>
                  </div>

                  {treeOpen ? (
                    <ul className="kb-tree-children">
                      <li>
                        <div
                          className={[
                            'kb-tree-row',
                            activeFolder === ROOT_FILTER ? 'kb-tree-row-on' : '',
                          ]
                            .filter(Boolean)
                            .join(' ')}
                        >
                          {/* 占位：让未归档与目录项的文字起点对齐 */}
                          <span className="kb-tree-caret" aria-hidden="true" />
                          <button
                            type="button"
                            className="kb-tree-node"
                            aria-current={activeFolder === ROOT_FILTER ? 'true' : undefined}
                            onClick={() => setActiveFolder(ROOT_FILTER)}
                          >
                            <FolderIcon size={14} />
                            <span className="kb-tree-label">未归档</span>
                            {unfiledCount !== null ? (
                              <span className="kb-tree-count tabular">
                                {formatCount(unfiledCount)}
                              </span>
                            ) : null}
                          </button>
                        </div>
                      </li>

                      {folders.map((folder) => (
                        <li key={folder.id}>
                          <div
                            className={[
                              'kb-tree-row',
                              activeFolder === folder.id ? 'kb-tree-row-on' : '',
                            ]
                              .filter(Boolean)
                              .join(' ')}
                          >
                            <span className="kb-tree-caret" aria-hidden="true" />
                            <button
                              type="button"
                              className="kb-tree-node"
                              aria-current={activeFolder === folder.id ? 'true' : undefined}
                              title={folder.name}
                              onClick={() => setActiveFolder(folder.id)}
                            >
                              <FolderIcon size={14} />
                              <span className="kb-tree-label">{folder.name}</span>
                              <span className="kb-tree-count tabular">
                                {formatCount(folder.document_count)}
                              </span>
                            </button>
                            {knowledgeBase.can_write ? (
                              /* 行菜单：`@/ui/dropdown-menu`（Radix）。与旧 `RowMenu` 的行为差异：
                               菜单走 Portal 渲染到 body（旧实现是行内绝对定位）、打开时焦点进菜单、
                               关掉后还给触发器；菜单项是 `[role=menuitem]` 的 div 不是 `<button>`，
                               选中后菜单自动关闭（旧实现的 `close()` 因此消失）。 */
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <Button
                                    variant="ghost"
                                    size="icon"
                                    aria-label={`${folder.name} 的操作`}
                                    title={`${folder.name} 的操作`}
                                  >
                                    <MoreHorizontal aria-hidden="true" />
                                  </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent align="end">
                                  <DropdownMenuItem onSelect={() => startRenameFolder(folder)}>
                                    <Pencil aria-hidden="true" />
                                    重命名
                                  </DropdownMenuItem>
                                  <DropdownMenuItem
                                    variant="destructive"
                                    onSelect={() => setFolderDeleteTarget(folder)}
                                  >
                                    <Trash2 aria-hidden="true" />
                                    删除目录
                                  </DropdownMenuItem>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            ) : null}
                          </div>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              </ul>
            ) : null}

            {/* 新建/重命名共用一个表单：靠 folderEditingId 区分两种模式 */}
            {folderFormOpen ? (
              <div className="kb-tree-form">
                <Input
                  value={folderDraft}
                  placeholder="目录名，例如：合同"
                  aria-label="目录名"
                  onChange={(event) => setFolderDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void submitFolder()
                  }}
                />
                <div className="kb-tree-form-actions">
                  <Button
                    size="sm"
                    variant="default"
                    disabled={folderSaving}
                    onClick={() => void submitFolder()}
                  >
                    {folderSaving ? '保存中…' : '保存'}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setFolderFormOpen(false)}>
                    取消
                  </Button>
                </div>
              </div>
            ) : null}
          </aside>
        ) : null}

        <section className="kb-doc-area">
          {/*
            工具栏：**筛选是一组、库级动作是一组**。分组不是为了好看——原先三个筛选
            各自是 `.kb-toolbar` 的直接子元素，而 `.kb-filter` 盒子留了 148px 的定宽
            （里面的下拉自己只占 ~116px），于是"输入框→状态"看着 4px、"状态→来源"看着
            33px。归组之后组内一律 `--space-2` 等距，读到的是"这四个是一回事"。

            两个入口的**词也分开**：就地筛列表那一个是「按文件名过滤」（过滤，改的是
            当前列表），开弹层做语义检索那一个是「按内容检索」（检索，另开一个面）。
            两个都叫"搜索"时，用户点哪个都像抽签。
          */}
          <div className="kb-toolbar">
            <div className="kb-filters">
              <div className="kb-search-box">
                <Search className="kb-search-icon" size={16} aria-hidden="true" />
                {/* 输入框的"底 + 无描边 + 圆角 + 给图标让位"写在调用点：收层之后
                    utilities 赢过 knowledge.css，原语自己的 `px-3 / bg-surface / border-border`
                    会把 `.kb-search-box [data-slot='input']` 那几条吃掉（图标压到文字上）。
                    左内边距的算式与 `.kb-search-icon` 的 `left` 同一份：12px + 图标与间隙 24px。 */}
                <Input
                  value={searchDraft}
                  placeholder="按文件名过滤…"
                  aria-label="按文件名过滤"
                  className="rounded-row border-transparent bg-subtle pl-[calc(var(--space-3)+24px)]"
                  onChange={(event) => setSearchDraft(event.target.value)}
                />
              </div>
              <div className="kb-filter">
                <Select
                  value={stageFilter || ALL_STAGES}
                  onValueChange={(value) => {
                    setStageFilter(value === ALL_STAGES ? '' : value)
                    setPage(1)
                    setSelected([])
                  }}
                >
                  <SelectTrigger aria-label="按状态筛选">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {STAGE_FILTER_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="kb-filter">
                <Select
                  value={sourceFilter || ALL_SOURCES}
                  onValueChange={(value) => {
                    setSourceFilter(value === ALL_SOURCES ? '' : value)
                    setPage(1)
                    setSelected([])
                  }}
                >
                  <SelectTrigger aria-label="按来源筛选">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SOURCE_FILTER_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {hasFilter ? (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => {
                    setSearchDraft('')
                    setSearchQuery('')
                    setStageFilter('')
                    setSourceFilter('')
                    setPage(1)
                    setSelected([])
                  }}
                >
                  清除筛选
                </Button>
              ) : null}
            </div>

            <div className="kb-toolbar-actions">
              {/* 与「按文件名过滤」区分开：这一颗开的是检索面板（语义 + BM25），
                  查的是库里的正文，不是当前这一页的文件名 */}
              <Button
                variant="secondary"
                disabled={documents.length === 0}
                onClick={() => setSearchOpen(true)}
              >
                <Search aria-hidden="true" />
                按内容检索
              </Button>
              {/* Wiki 入口只在库形态选了「向量检索 + Wiki」时出现 */}
              {knowledgeBase?.wiki_enabled ? (
                <Button variant="secondary" onClick={() => void navigate(`/kb/${kbId}/wiki`)}>
                  <Library aria-hidden="true" />
                  Wiki
                </Button>
              ) : null}
              {/* 分享入口只对 owner / 管理员出现：can_manage 由后端算 */}
              {knowledgeBase?.can_manage ? (
                <Button variant="secondary" onClick={() => setShareOpen(true)}>
                  <Share2 aria-hidden="true" />
                  分享
                </Button>
              ) : null}
              {/* 只读分享的成员看得到内容，但没有写入口 */}
              {knowledgeBase?.can_write ? (
                <Button variant="secondary" onClick={() => setUploadOpen(true)}>
                  <Upload aria-hidden="true" />
                  上传文档
                </Button>
              ) : null}
            </div>
          </div>

          {loading && documents.length === 0 ? <SkeletonRows variant="list" rows={4} /> : null}

          {!(loading && documents.length === 0) ? (
            <>
              {/* 勾选后浮出的批量动作条（全选在列表内的表头里，不在这里） */}
              {selectedCount > 0 ? (
                <div className="kb-batch">
                  <span className="kb-batch-count">
                    已选 {formatCount(selectedCount)} 篇
                    {/* 跨页选择必须说清楚：否则用户看到"已选 60 篇"而眼前只有 20 行 */}
                    {offPageSelected > 0 ? (
                      <span className="kb-batch-offpage">
                        （另有 {offPageSelected} 篇不在本页）
                      </span>
                    ) : null}
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => {
                      setMoveBatchIds([...selected])
                      // 默认落在"根目录"：批量选中的文档可能来自不同目录，没有共同的当前值
                      setMoveChoice(
                        activeFolder && activeFolder !== ROOT_FILTER ? activeFolder : '',
                      )
                    }}
                  >
                    <FolderIcon aria-hidden="true" />
                    移动到目录
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => void runBatchToggleDisabled('disable')}
                  >
                    停用检索
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => void runBatchToggleDisabled('enable')}
                  >
                    恢复检索
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => void runBatchQuestions()}
                  >
                    <CircleHelp aria-hidden="true" />
                    生成问题
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => void runBatch('reprocess')}
                  >
                    <RefreshCw aria-hidden="true" />
                    重新摄入
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={batchRunning}
                    onClick={() => {
                      if (selectedCount === 0 || batchRunning) return
                      setBatchDeleteOpen(true)
                    }}
                  >
                    <Trash2 aria-hidden="true" />
                    删除
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={batchRunning}
                    onClick={() => setSelected([])}
                  >
                    取消选择
                  </Button>
                </div>
              ) : null}

              <div className="panel" ref={listPanel}>
                {/*
                  表头与数据行用同一套列宽：「文件」让出 36px 给展开箭头（`.kb-head-file`），
                  「状态」「上传者」用与行内同宽的盒子——七列的左边缘才在一条竖线上
                  （评审 K6/K3：原先这两列有内容没表头，左侧还有三条基准）。
                  「上传者」跟着行内那颗一起出现/消失：花名册没加载出来时行内也没有这一格。
                */}
                <div className="panel-head kb-doc-head">
                  {knowledgeBase?.can_write ? (
                    <span className="kb-col-check">
                      {/* @/ui/checkbox（Radix）：半选走 `'indeterminate'`（旧实现写的是原生 DOM 的 `indeterminate`） */}
                      <Checkbox
                        checked={someSelected ? 'indeterminate' : allSelected}
                        aria-label={allSelected ? '取消选择本页' : '全选本页（可逐页累加）'}
                        onCheckedChange={toggleSelectAll}
                      />
                    </span>
                  ) : null}
                  <span className="kb-col-file kb-head-file" aria-hidden="true">
                    <span style={HEAD_FILE_CELL}>文件</span>
                    <span style={STATUS_CELL}>状态</span>
                    {/* 与行内那一格同名同类：窄屏那条 `@media` 藏的是 `.kb-col-uploader`，
                        两边都挂这个类，列头才会跟着一起藏（否则窄屏下两边的列数不一样） */}
                    {roster.length > 0 ? (
                      <span className="kb-col-uploader" style={UPLOADER_CELL}>
                        上传者
                      </span>
                    ) : null}
                  </span>
                  <span className="kb-col-chunks" aria-hidden="true">
                    切块
                  </span>
                  <span className="kb-col-questions" aria-hidden="true">
                    问题
                  </span>
                  <span className="kb-col-size" aria-hidden="true">
                    大小
                  </span>
                  <span className="kb-col-updated" aria-hidden="true">
                    更新时间
                  </span>
                  <span className="kb-col-menu" aria-hidden="true" />
                </div>

                <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
                  {/* 空范围也照常给表头与数据区，只是第一行换成一句说明 */}
                  {documents.length === 0 ? (
                    <li className="kb-doc-group">
                      <div className="kb-doc-row panel-row kb-doc-empty">
                        <span className="kb-doc-empty-text">{emptyTitle}</span>
                        <span className="kb-doc-empty-hint">
                          {limits.formatHint}；单文件上限 {limits.maxMb}MB。
                        </span>
                      </div>
                    </li>
                  ) : null}

                  {documents.map((document) => {
                    const stage = documentStageView(document.stage)
                    return (
                      <li key={document.id} className="kb-doc-group">
                        {/* 抽屉里正开着的那一篇在列表里留一个选中态：抽屉盖住列表时
                            它是"我点的是哪一行"的唯一线索（`--bg-selected`，与左栏
                            当前项同一档底色） */}
                        <div
                          className={[
                            'kb-doc-row',
                            'panel-row',
                            openDocumentId === document.id ? 'kb-doc-row-on' : '',
                          ]
                            .filter(Boolean)
                            .join(' ')}
                        >
                          {knowledgeBase?.can_write ? (
                            <span className="kb-col-check">
                              <Checkbox
                                checked={selected.includes(document.id)}
                                aria-label={`选择 ${document.name}`}
                                onCheckedChange={() => toggleSelect(document.id)}
                              />
                            </span>
                          ) : null}

                          {document.is_split ? (
                            <button
                              type="button"
                              className="kb-col-caret"
                              aria-label={expanded[document.id] ? '收起子文件' : '展开子文件'}
                              aria-expanded={Boolean(expanded[document.id])}
                              onClick={() => void toggleParts(document)}
                            >
                              {expanded[document.id] ? (
                                <ChevronDown size={16} />
                              ) : (
                                <ChevronRight size={16} />
                              )}
                            </button>
                          ) : (
                            <span className="kb-col-caret" aria-hidden="true" />
                          )}

                          <span className="kb-col-file">
                            {/* 点文件名**留在本页**：把文档 id 写进查询参数，右侧滑出详情抽屉 */}
                            <Link
                              className="kb-row-name-link"
                              to={documentLink(document.id)}
                              title={rowTitle(document)}
                              aria-current={openDocumentId === document.id ? 'page' : undefined}
                            >
                              <span className="kb-row-name-text">{document.name}</span>
                            </Link>
                            {/* 停用：不参与检索但一切保留。中性色——它是"被搁置"，不是"出错"。
                                摆在文件名之后而不是状态那一格里：多出来的宽度由文件名让出来
                                （这一列是弹性的），右边的列不会因为这一颗标签整体移位 */}
                            {document.disabled ? <StatusTag label="已停用" tone="neutral" /> : null}
                            {/* 状态列：定宽的盒子，列头与它同宽（见 `STATUS_CELL`） */}
                            <span style={STATUS_CELL}>
                              <StatusTag
                                label={stage.label}
                                tone={stage.tone}
                                running={ACTIVE_STAGES.has(document.stage)}
                                title={document.error ?? undefined}
                              />
                            </span>
                            {/* 谁传的（G6）。没记到时显示"未记录"而不是留空——留空会让人以为是没渲染出来 */}
                            {roster.length > 0 ? (
                              <span
                                className="kb-col-uploader"
                                style={UPLOADER_CELL}
                                title={document.uploaded_by_name || undefined}
                              >
                                {document.uploaded_by_name || '未记录'}
                              </span>
                            ) : null}
                          </span>

                          <span className="kb-col-chunks tabular">
                            {formatCount(document.chunk_count)}
                          </span>
                          <span
                            className={[
                              'kb-col-questions',
                              questionCell(document) === '未生成' ? 'kb-col-questions-muted' : '',
                            ]
                              .filter(Boolean)
                              .join(' ')}
                            title={questionTitle(document)}
                          >
                            {questionCell(document)}
                          </span>
                          <span className="kb-col-size">{formatBytes(document.size_bytes)}</span>
                          {/* 与抽屉里同一个 helper、同一串文本；绝对时间挂 title 供核对 */}
                          <span className="kb-col-updated" title={formatDate(document.updated_at)}>
                            {formatRelativeTime(document.updated_at)}
                          </span>

                          {/* 操作菜单对**所有能看这个库的人**开放：下载是只读动作。
                              与旧 `RowMenu` 的差异同目录菜单：Portal 渲染、焦点进菜单后还给触发器、
                              菜单项是 div[role=menuitem]、选中即自动关闭。 */}
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="kb-col-menu"
                                aria-label={`${document.name} 的操作`}
                                title={`${document.name} 的操作`}
                              >
                                <MoreHorizontal aria-hidden="true" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem onSelect={() => void onDownload(document)}>
                                <Download aria-hidden="true" />
                                下载
                              </DropdownMenuItem>
                              {knowledgeBase?.can_write ? (
                                <>
                                  <DropdownMenuItem
                                    onSelect={() => {
                                      setRenameTarget(document)
                                      setRenameDraft(document.name)
                                    }}
                                  >
                                    <Pencil aria-hidden="true" />
                                    重命名
                                  </DropdownMenuItem>
                                  <DropdownMenuItem
                                    onSelect={() => {
                                      setMoveTarget(document)
                                      // 默认落在它当前所在的位置：多数人是想改到别处
                                      setMoveChoice(document.folder_id ?? '')
                                    }}
                                  >
                                    <FolderIcon aria-hidden="true" />
                                    移动到目录
                                  </DropdownMenuItem>
                                  <DropdownMenuItem onSelect={() => void reprocess(document)}>
                                    <RefreshCw aria-hidden="true" />
                                    重新摄入
                                  </DropdownMenuItem>
                                  {/* 补生成分段问题：只对已索引的文档有意义 */}
                                  {document.stage === 'indexed' ? (
                                    <DropdownMenuItem
                                      onSelect={() =>
                                        void runBatchAction('questions', {
                                          ids: [document.id],
                                          verb: '生成问题',
                                          refresh: 'list',
                                          success: () => '已排队生成问题，完成后列表会自动刷新',
                                          partial: (_result, firstError) =>
                                            firstError || '生成问题失败',
                                        })
                                      }
                                    >
                                      <CircleHelp aria-hidden="true" />
                                      生成问题
                                    </DropdownMenuItem>
                                  ) : null}
                                  <DropdownMenuItem
                                    onSelect={() => void onToggleDisabled(document)}
                                  >
                                    <X aria-hidden="true" />
                                    {document.disabled ? '恢复检索' : '停用检索'}
                                  </DropdownMenuItem>
                                  {/* 取消只在真的还在跑时出现：对已完成的文档摆一个点了报错的按钮没有意义 */}
                                  {ACTIVE_STAGES.has(document.stage) ? (
                                    <DropdownMenuItem
                                      disabled={canceling === document.id}
                                      onSelect={() => setCancelTarget(document)}
                                    >
                                      <X aria-hidden="true" />
                                      {canceling === document.id ? '取消中…' : '取消解析'}
                                    </DropdownMenuItem>
                                  ) : null}
                                  <DropdownMenuItem
                                    variant="destructive"
                                    onSelect={() => openDelete(document)}
                                  >
                                    <Trash2 aria-hidden="true" />
                                    删除
                                  </DropdownMenuItem>
                                </>
                              ) : null}
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>

                        {document.error ? <p className="kb-doc-error">{document.error}</p> : null}

                        {/* 分段进度：**只在还没跑完的文档上出现**（跑完的每段都是满的，纯噪声） */}
                        {document.progress && document.progress.status !== 'done' ? (
                          <div className="kb-doc-progress">
                            <div className="kb-doc-progress-meter">
                              <MeterBar
                                size="sm"
                                segments={progressSegments(document, hasActive || questionsPending)}
                                tone={progressTone(document)}
                                ariaLabel={progressCaption(document)}
                              />
                            </div>
                            <span className="kb-doc-progress-text">
                              {progressCaption(document)}
                            </span>
                            <Link
                              className="kb-doc-progress-more"
                              to={`${documentLink(document.id)}&tab=progress`}
                            >
                              处理明细
                            </Link>
                          </div>
                        ) : null}

                        {expanded[document.id]?.length ? (
                          <ul className="kb-parts">
                            {expanded[document.id]?.map((part) => (
                              <li key={part.id} className="kb-part">
                                <span className="kb-part-name">分片 P{part.part_index + 1}</span>
                                <span>
                                  第 {part.page_start}–{part.page_end} 页
                                </span>
                                <StatusTag
                                  label={documentStageView(part.stage).label}
                                  tone={documentStageView(part.stage).tone}
                                />
                              </li>
                            ))}
                          </ul>
                        ) : null}
                      </li>
                    )
                  })}

                  {/* 补白行：把表格铺满可视区，文件少时不留一大片空白（纯装饰） */}
                  {Array.from({ length: fillerRows }, (_, index) => (
                    <li
                      key={`filler-${index}`}
                      className="kb-doc-group kb-doc-filler"
                      aria-hidden="true"
                    >
                      <div className="kb-doc-row panel-row" />
                    </li>
                  ))}
                </ul>
              </div>

              {/* 总数常显，只有翻页控件在单页时隐藏：总数恰恰是扫列表时想知道的第一个数 */}
              {total > 0 || documents.length > 0 ? (
                <div className="kb-pager">
                  <span>共 {formatCount(total)} 篇</span>
                  {pageCount > 1 ? (
                    <div className="kb-pager-controls">
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={page <= 1}
                        onClick={() => goToPage(page - 1)}
                      >
                        <ChevronLeft aria-hidden="true" />
                        上一页
                      </Button>
                      <span className="kb-pager-page tabular">
                        第 {page} / {pageCount} 页
                      </span>
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={page >= pageCount}
                        onClick={() => goToPage(page + 1)}
                      >
                        下一页
                        <ChevronRight aria-hidden="true" />
                      </Button>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </>
          ) : null}
        </section>
      </div>

      {knowledgeBase ? (
        <KbSearchPanel
          open={searchOpen}
          kbId={kbId}
          kbName={knowledgeBase.name}
          onClose={() => setSearchOpen(false)}
          onOpenDocument={(id) => {
            setSearchOpen(false)
            void navigate(documentLink(id))
          }}
        />
      ) : null}

      {/* 上传：**弹窗而不是"选完就传"**——批量上传的重复/失败必须留在屏幕上让人逐个处理 */}
      <UploadDialog
        open={uploadOpen}
        kbId={kbId}
        folderId={uploadFolderId}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => {
          // 新文档按时间倒序排在第 1 页最前，所以传完先跳回第 1 页
          setPage(1)
          void refreshAll()
        }}
      />

      {/* 分享（v10）：私有是默认，想让别人看到就必须显式授出 */}
      {knowledgeBase ? (
        <ShareDialog
          open={shareOpen}
          kbId={kbId}
          kbName={knowledgeBase.name}
          onClose={() => setShareOpen(false)}
        />
      ) : null}

      {/* 文档详情抽屉：从右侧滑出、盖在列表上，**列表本身一点不动** */}
      {openDocumentId ? (
        <DocumentDrawer
          key={openDocumentId}
          documentId={openDocumentId}
          initialTab={initialTab}
          page={pageParam}
          onClose={closeDocument}
        />
      ) : null}

      {/* 移动到目录：单篇与批量共用（目录清单、选中逻辑、提示文案都一样） */}
      <Dialog
        open={moveTarget !== null || moveBatchIds.length > 0}
        onOpenChange={(next) => {
          if (!next) {
            setMoveTarget(null)
            setMoveBatchIds([])
          }
        }}
      >
        <DialogContent className="flex max-h-[min(88vh,900px)] flex-col gap-0 p-0">
          <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
            <DialogTitle>移动到目录</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <p className="kb-lead">
              {moveBatchIds.length > 0
                ? `把选中的 ${formatCount(moveBatchIds.length)} 篇移到：`
                : `把「${moveTarget?.name ?? ''}」移到：`}
            </p>
            {/*
              目标目录：`@/ui/radio-group`（Radix）。值域里 `''` 是"根目录（未归档）"，
              而 Radix 的单选项允许空串，所以这个哨兵值不需要（与两个筛选下拉不同）。
              每一行是"读得到的选项文字"：14px / 400，与 label 原语默认的 12px / 500 不同，
              字号写在调用点（`.kb-switch` 只留布局，见 knowledge.css）。
            */}
            <RadioGroup
              value={moveChoice}
              onValueChange={setMoveChoice}
              className="flex flex-col gap-1"
            >
              <Label className="kb-switch text-[length:var(--text-meta-size)] font-normal">
                <RadioGroupItem value="" aria-label="根目录（未归档）" />
                <span>根目录（未归档）</span>
              </Label>
              {folders.map((folder) => (
                <Label
                  key={folder.id}
                  className="kb-switch text-[length:var(--text-meta-size)] font-normal"
                >
                  <RadioGroupItem value={folder.id} aria-label={folder.name} />
                  <span>{folder.name}</span>
                </Label>
              ))}
            </RadioGroup>
            {folders.length === 0 ? (
              <p className="text-hint">还没有目录。先关掉这里，用左侧目录树右上角的「+」建一个。</p>
            ) : null}
          </div>
          <DialogFooter className="border-t border-[var(--border-hairline)] px-4 py-3">
            <Button
              variant="outline"
              onClick={() => {
                setMoveTarget(null)
                setMoveBatchIds([])
              }}
            >
              取消
            </Button>
            <Button variant="default" disabled={moving} onClick={() => void confirmMove()}>
              {moving ? '移动中…' : '移动'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 重命名：只改显示名，不重跑解析 */}
      <Dialog
        open={renameTarget !== null}
        onOpenChange={(next) => {
          if (!next) setRenameTarget(null)
        }}
      >
        <DialogContent className="flex max-h-[min(88vh,900px)] flex-col gap-0 p-0">
          <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
            <DialogTitle>重命名文档</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <p className="kb-lead">给「{renameTarget?.name ?? ''}」换一个名字：</p>
            <Input
              value={renameDraft}
              placeholder="文件名"
              aria-label="文件名"
              onChange={(event) => setRenameDraft(event.target.value)}
            />
          </div>
          <DialogFooter className="border-t border-[var(--border-hairline)] px-4 py-3">
            <Button variant="outline" onClick={() => setRenameTarget(null)}>
              取消
            </Button>
            <Button variant="default" disabled={renaming} onClick={() => void confirmRename()}>
              {renaming ? '保存中…' : '保存'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/*
        四个确认弹窗都是 `@/ui/alert-dialog`（Radix）。与旧 `ConfirmDialog` 的**两处差异**：
        点「确定」后弹窗立即关闭（旧实现停在忙碌态直到请求回来，失败仍会弹 toast）；
        Esc 与点遮罩不再关闭（AlertDialog 的设计如此：只能走「取消 / 确定」二选一）。
        删除确认：**先给人看影响清单再动手**。
      */}
      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(next) => {
          if (!next) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除文档</AlertDialogTitle>
            <AlertDialogDescription>
              确定删除「{deleteTarget?.name ?? ''}」？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">
            {deleteImpact?.restorable
              ? '原文进回收站保留 7 天，其间可恢复；切块与向量立即清除，删掉就搜不到。'
              : '此操作不可恢复。'}
          </p>
          {deleteImpact === null ? (
            <p className="text-note">正在统计影响…</p>
          ) : (
            <dl className="kb-impact">
              <div>
                <dt>切块</dt>
                <dd className="tabular">{deleteImpact.chunks}</dd>
              </div>
              <div>
                <dt>占用的空间</dt>
                <dd className="tabular">{formatBytes(deleteImpact.size_bytes)}</dd>
              </div>
              <div>
                <dt>进行中的任务</dt>
                <dd className="tabular">{deleteImpact.running_tasks}</dd>
              </div>
            </dl>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmDelete()}>确定</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 目录删除：非空时后端会拒绝，说明文案由确认弹窗带出 */}
      <AlertDialog
        open={folderDeleteTarget !== null}
        onOpenChange={(next) => {
          if (!next) setFolderDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除目录</AlertDialogTitle>
            <AlertDialogDescription>
              删除目录「{folderDeleteTarget?.name ?? ''}」？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">
            目录本身删除后不可恢复；目录里的文档不受影响（仍留在知识库中）。
          </p>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmFolderDelete()}>确定</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={batchDeleteOpen}
        onOpenChange={(next) => {
          if (!next) setBatchDeleteOpen(false)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除文档</AlertDialogTitle>
            <AlertDialogDescription>
              删除选中的 {formatCount(selectedCount)} 篇文档？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">
            原文会移入回收站保留 7 天；切块与向量立即清除，删除后立刻搜不到。
          </p>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void runBatch('delete')}>确定</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 取消解析：不是删除——已产出的内容保留，之后可以重新摄入 */}
      <AlertDialog
        open={cancelTarget !== null}
        onOpenChange={(next) => {
          if (!next) setCancelTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>取消解析</AlertDialogTitle>
            <AlertDialogDescription>
              取消「{cancelTarget?.name ?? ''}」的解析？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">已解析出的内容会保留，之后可以重新摄入。</p>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmCancelParse()}>
              取消解析
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** 清单是否还没拉过（空数组 = 该拉一次）。 */ /* ------------------------------------------------------------------ 进度口径 */

/** 把列表行上的进度折成进度条的分段（与旧 `useProgress.ts` 同一口径）。 */
function progressSegments(document: DocumentSummary, pulsing: boolean) {
  const progress = document.progress
  if (!progress) return []
  return Array.from({ length: progress.step_total }, (_, index) => {
    const position = index + 1
    if (position < progress.step_index) return { fill: 1, tone: 'accent' as const }
    if (position === progress.step_index) {
      return {
        fill: 1,
        tone: progressTone(document),
        pulsing: pulsing || progress.status === 'running',
      }
    }
    return { fill: 0, tone: 'neutral' as const }
  })
}

function progressTone(document: DocumentSummary) {
  const progress = document.progress
  if (!progress) return 'accent' as const
  if (progress.stalled) return 'danger' as const
  if (progress.status === 'failed') return 'danger' as const
  if (progress.status === 'canceled') return 'neutral' as const
  if (progress.status === 'done') return 'success' as const
  return 'info' as const
}

/** 那一行文字：`第 3/6 步 · 解析内容 · 已用 2 分 14 秒`（失败/停滞各自缀一句）。 */
function progressCaption(document: DocumentSummary): string {
  const progress = document.progress
  if (!progress) return ''
  const head = `第 ${formatCount(progress.step_index)} / ${formatCount(progress.step_total)} 步 · ${progress.step_label}`
  if (progress.status === 'done') return `${head} · 共 ${formatMillis(progress.total_ms)}`
  const elapsed = `已用 ${formatMillis(progress.elapsed_ms)}`
  if (progress.stalled) return `${head} · ${elapsed} · 疑似卡住（没有 worker 在处理）`
  if (progress.status === 'failed') {
    return `${head} · 失败于这一步（已用 ${formatMillis(progress.elapsed_ms)}）`
  }
  if (progress.status === 'canceled') return `${head} · 已取消`
  if (progress.retries > 0) return `${head} · ${elapsed} · 重试 ${progress.retries} 次`
  return `${head} · ${elapsed}`
}
