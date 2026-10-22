/**
 * 知识库（卡片）——旧 `views/KnowledgeBasesView.vue` 的行为逐条对齐。
 *
 * 为什么这里用卡片、而文档清单用行：知识库是**容器型对象且数量少**，用户的动作是
 * "挑一个进去"（扫视比较），不是"在长列表里找某一条"。卡片给每个库一个独立边界，
 * 正好承载"名称 + 规模 + 概述 + 近 90 天两条节奏（入库 / 检索命中）+ 入口"这一小簇
 * 信息。条目多起来时（>12）切回行——这条判定规则原本就写在规范 5.1。
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { Plus } from 'lucide-react'

import {
  CHUNK_DEFAULT_OVERLAP,
  CHUNK_DEFAULT_SIZE,
  CHUNK_OVERLAP_MARKS,
  CHUNK_SIZE_MARKS,
  CHUNK_SIZE_MAX,
  CHUNK_SIZE_MIN,
  chunkOverlapMax,
  SUGGESTED_COUNT_DEFAULT,
} from '@/api/knowledgeBases'
import { EmptyState, InfoTip, SkeletonRows } from '@/features/knowledge/composites'
import { chunkingErrorOf, numberOr, parseIntOrNull } from '@/features/knowledge/chunking'
import { TREND_MAX_ROWS, useIngestTrends, type IngestTrend } from '@/features/knowledge/ingestTrend'
import { KnowledgeBaseSettings } from '@/features/knowledge/KnowledgeBaseSettings'
import { RangeField } from '@/features/knowledge/RangeField'
import {
  useSearchTrends,
  searchTrendTitleOf,
  type SearchTrend,
} from '@/features/knowledge/searchTrend'
import { Sparkline } from '@/features/knowledge/Sparkline'
import {
  SuggestedQuestionsFields,
  type SuggestedQuestionsValue,
} from '@/features/knowledge/SuggestedQuestionsFields'
import { messageOf, notify, useKnowledgeBases, useModelRegistry } from '@/features/knowledge/store'
import { Notice, PageShell } from '@/features/misc/shared/composites'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { Button } from '@/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/ui/dialog'
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'
import { RadioGroup, RadioGroupItem } from '@/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'

/** 规范 5.1 的阈值：超过 12 个容器就用行而不是卡片，避免格子被挤窄。 */
const CARD_LIMIT = 12

/** 行形态下传给趋势钩子的空清单（同一份引用，免得每帧造一个新数组）。 */
const EMPTY_KB_IDS: string[] = []

/** 库形态（v24）：`vector` = 仅向量检索（默认），`wiki` = 额外开启 Wiki。 */
type KbForm = 'vector' | 'wiki'

const SQ_DEFAULTS: SuggestedQuestionsValue = {
  enabled: false,
  count: SUGGESTED_COUNT_DEFAULT,
  modelPk: '',
  prompt: '',
}

/**
 * 「跟随默认」那一项的取值（`''` = 用默认嵌入模型）。
 *
 * Radix 的 `<Select.Item>` **不接受空串**，所以界面上用哨兵值，送出/取回都换回 `''`
 * ——对外的契约定（`embedding_model_pk` 为空即默认）一点没变。
 */
const EMBEDDING_DEFAULT = '__default__'

/**
 * 卡片底部那条活动线的时间窗：**30 天**。
 *
 * 原先卡片上摆两根 90 天的线（入库 / 检索命中），读数四行、颜色两种，扫一眼读不完。
 * 现在只有一条"这个库在被用"的线：两种事件都是活动，按天合并，单色（`--accent`）。
 */
const ACTIVITY_DAYS = 30

/**
 * 近 30 天的活动曲线：入库篇数 + 检索命中条数，**按天相加**。
 *
 * 两串趋势的口径都是"末位是今天、长度恒为天数"（见 `ingestTrend.ts` / `searchTrend.ts`），
 * 所以从末尾对齐就够；还没读回来的那一串按 0 算（图上不补假数据，只是短一截）。
 */
function activitySeriesOf(
  ingest: IngestTrend | undefined,
  search: SearchTrend | undefined,
): number[] {
  const ing = ingest?.status === 'ready' ? ingest.series.slice(-ACTIVITY_DAYS) : []
  const hit = search?.status === 'ready' ? search.series.slice(-ACTIVITY_DAYS) : []
  const length = Math.max(ing.length, hit.length)
  return Array.from(
    { length },
    (_, index) => (ing[ing.length - length + index] ?? 0) + (hit[hit.length - length + index] ?? 0),
  )
}

/** 窗口内的合计（读数用；读不到时给 `null`，界面写「—」）。 */
function windowTotalOf(trend: { status: string; series: number[] } | undefined): number | null {
  if (trend?.status !== 'ready') return null
  return trend.series.slice(-ACTIVITY_DAYS).reduce((sum, value) => sum + value, 0)
}

export function KnowledgeBasesView() {
  const store = useKnowledgeBases()
  const registry = useModelRegistry()

  const [createOpen, setCreateOpen] = useState(false)
  const [draftName, setDraftName] = useState('')
  const [draftModel, setDraftModel] = useState('')
  const [draftForm, setDraftForm] = useState<KbForm>('vector')
  const [chunkSizeDraft, setChunkSizeDraft] = useState(String(CHUNK_DEFAULT_SIZE))
  const [chunkOverlapDraft, setChunkOverlapDraft] = useState(String(CHUNK_DEFAULT_OVERLAP))
  const [suggested, setSuggested] = useState<SuggestedQuestionsValue>(SQ_DEFAULTS)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    // 计数随列表一起回来，不必再单独拉一轮汇总。
    void store.load()
    // 提前把注册表拉回来：这样"没有可用嵌入模型"能在点开弹窗**之前**就显示在页头上
    void registry.load()
    // 只做首屏这两次加载
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const embeddingModels = registry.embeddingModels
  const defaultModel = embeddingModels.find((model) => model.id === registry.defaultEmbeddingPk)
  const defaultLabel = defaultModel
    ? `${defaultModel.label || defaultModel.model_id}${defaultModel.dim ? ` · ${defaultModel.dim} 维` : ''}`
    : ''

  const embeddingOptions = [
    ...(defaultModel ? [{ value: EMBEDDING_DEFAULT, label: `默认（${defaultLabel}）` }] : []),
    ...embeddingModels.map((model) => ({
      value: model.id,
      label: `${model.label || model.model_id}${model.dim ? ` · ${model.dim} 维` : ''}`,
    })),
  ]

  /** 没有任何可用的嵌入模型：建库入口整体挡掉。**加载完成前不算"没有"**——那是闪一下的误报。 */
  const noEmbeddingModel = registry.modelOptionsLoaded && embeddingModels.length === 0
  const createBlocked = noEmbeddingModel
  /** 有模型但没选默认：必须明确挑一个（没有"不指定"这条路了）。 */
  const mustPickModel = !defaultModel && embeddingModels.length > 0

  const chunkOverlapCap = chunkOverlapMax(numberOr(chunkSizeDraft, CHUNK_DEFAULT_SIZE))
  const chunkError = chunkingErrorOf(chunkSizeDraft, chunkOverlapDraft)
  const canCreate =
    draftName.trim().length > 0 && (Boolean(draftModel) || !mustPickModel) && chunkError === ''

  const hasItems = store.items.length > 0
  const useCards = store.items.length <= CARD_LIMIT

  /**
   * 页头那行说明：**这一页现在是什么状态**（几个库、多少篇文档），不是"本页用于……"。
   * 数字来自列表接口一次带回的汇总，不额外发请求。
   */
  const documentTotal = store.items.reduce(
    (sum, kb) => sum + (store.summaries[kb.id]?.count ?? 0),
    0,
  )
  const subtitle = hasItems
    ? `${formatCount(store.items.length)} 个知识库 · ${formatCount(documentTotal)} 篇文档`
    : store.loading
      ? '正在加载…'
      : '现在还没有任何知识库'

  /**
   * 卡片上那两根趋势图的数据：**入库节奏**（`ingestTrend.ts`，逐库取文档时间）
   * 与**检索命中**（`searchTrend.ts`，一趟请求覆盖全部库，v0.61 起才有真数据）。
   *
   * 行形态（> 12 个库）两个都传空数组：那时趋势图不画，也就不该白拉十几次文档列表，
   * 更不该多打一趟命中趋势。
   */
  const trends = useIngestTrends(useCards ? store.items.map((kb) => kb.id) : EMPTY_KB_IDS)
  const searchTrends = useSearchTrends(useCards ? store.items.map((kb) => kb.id) : EMPTY_KB_IDS)

  function openCreate(): void {
    setCreateOpen(true)
    setDraftModel('')
    // 每次打开都回到默认值：上一次改过的切块参数留着，下一座库会莫名其妙继承它
    setChunkSizeDraft(String(CHUNK_DEFAULT_SIZE))
    setChunkOverlapDraft(String(CHUNK_DEFAULT_OVERLAP))
    setSuggested(SQ_DEFAULTS)
    setDraftForm('vector')
    void registry.load().then(() => {
      // 没有默认模型时预选第一个：让"能选就选"的路径最短，而不是让用户先撞一次校验
      if (mustPickModel && embeddingModels[0]) setDraftModel(embeddingModels[0].id)
    })
  }

  async function submitCreate(): Promise<void> {
    const name = draftName.trim()
    if (!name) {
      notify.error('知识库名称不能为空')
      return
    }
    if (mustPickModel && !draftModel) {
      notify.error('请先选择一个嵌入模型')
      return
    }
    setCreating(true)
    try {
      const size = parseIntOrNull(chunkSizeDraft)
      const overlap = parseIntOrNull(chunkOverlapDraft)
      const created = await store.create({
        name,
        embedding_model_pk: draftModel || undefined,
        // 与默认值相同时**不发**：让"服务端默认"成为唯一的默认，而不是前端也钉一份数字
        ...(size !== null && size !== CHUNK_DEFAULT_SIZE ? { chunk_size: size } : {}),
        ...(overlap !== null && overlap !== CHUNK_DEFAULT_OVERLAP
          ? { chunk_overlap: overlap }
          : {}),
        // 出题开关的默认是**关**，所以只在勾上时发 true（口径与上面两个一致）
        ...(suggested.enabled ? { suggested_enabled: true } : {}),
        ...(suggested.count !== SUGGESTED_COUNT_DEFAULT
          ? { suggested_count: suggested.count }
          : {}),
        ...(suggested.modelPk ? { suggested_model_pk: suggested.modelPk } : {}),
        ...(suggested.prompt.trim() ? { suggested_prompt: suggested.prompt.trim() } : {}),
        // 库形态：默认「仅向量检索」是后端默认值，所以只在选 Wiki 时发 true
        ...(draftForm === 'wiki' ? { wiki_enabled: true } : {}),
      })
      notify.success(`已创建知识库「${created.name}」`)
      setCreateOpen(false)
      setDraftName('')
      setDraftModel('')
    } catch (cause) {
      notify.error(messageOf(cause, '创建失败'))
    } finally {
      setCreating(false)
    }
  }

  function initial(name: string): string {
    return name.trim().slice(0, 1).toUpperCase()
  }

  return (
    <PageShell
      title="知识库"
      subtitle={subtitle}
      actions={
        <Button variant="default" disabled={createBlocked} onClick={openCreate}>
          <Plus aria-hidden="true" />
          新建知识库
        </Button>
      }
    >
      {/* 建不了库时**页面级**就说清原因：等用户填完名字再报错，白填一遍。 */}
      {noEmbeddingModel ? (
        <Notice tone="warn">
          还没有可用的嵌入模型，暂时无法新建知识库。请到「设置 → 模型注册」添加供应商并登记
          向量化模型，再到「设置 → 向量化」把它选为默认。
        </Notice>
      ) : null}

      {store.error ? <Notice tone="error">{store.error}</Notice> : null}

      {store.loading && !hasItems ? <SkeletonRows variant="card" rows={4} layout="grid" /> : null}

      {/* 空态只说"现在没有"，不解释知识库是什么 */}
      {!store.loading && !hasItems ? <EmptyState title="还没有知识库" /> : null}

      {/*
        卡片网格：容器型对象、条目少，用卡片承载"挑一个进去"这个动作。
        一张卡只有三行：**名字**（+ 设置）、一句元信息、一条 30 天活动线——
        原先这里有四行读数与两根不同颜色的线，扫一眼读不完。
      */}
      {hasItems && useCards ? (
        <ul className="kb-cards stagger">
          {store.items.map((kb) => {
            const ingest = trends[kb.id]
            const search = searchTrends[kb.id]
            const docCount = store.summaries[kb.id]?.count ?? null
            const updatedAt = store.summaries[kb.id]?.updatedAt ?? null
            const ingestTotal = windowTotalOf(ingest)
            const hitTotal = windowTotalOf(search)
            const truncatedNote =
              ingest?.status === 'ready' && ingest.truncated
                ? `；入库只统计了最近 ${TREND_MAX_ROWS} 篇文档，更早那几天可能没算全`
                : ''
            /**
             * 读数里的入库数：**被截断时带 `≥`**（只统计了最近 N 篇，真实值只多不少）。
             * 只用 title 提示不够——`≥` 是"这个数不是精确值"的可见信号，不该只活在悬停里。
             */
            const ingestLabel =
              ingestTotal === null
                ? '—'
                : `${ingest?.status === 'ready' && ingest.truncated ? '≥' : ''}${formatCount(ingestTotal)}`
            const activityTitle =
              `近 ${ACTIVITY_DAYS} 天：入库 ${formatCount(ingestTotal)} 篇，` +
              `检索命中 ${formatCount(hitTotal)} 次` +
              // 收敛砍掉一部分时才补一句（"候选 45 条，返回 4 条"）：不砍就没有第二句话可说
              (searchTrendTitleOf(search) ? `；${searchTrendTitleOf(search)}` : '') +
              truncatedNote
            return (
              <li key={kb.id} className="kb-card-item">
                <Link className="kb-card surface-card surface-card-interactive" to={`/kb/${kb.id}`}>
                  {/* 第一行：**这个名字**（17px 600）。嵌入模型串由供应商与库形态决定，
                      同一批库里多半是同一个串（四张卡四个 BAAI/bge-m3）——逐库看它去
                      「知识库设置 → 基本」，卡片上不放。 */}
                  <span className="kb-card-name">{kb.name}</span>

                  {/* 第二行：一句话元信息（13.5px 二级墨色）。两个数、一句话，可扫读。 */}
                  <span className="kb-card-meta">
                    <span className="tabular">{formatCount(docCount)}</span>
                    {' 篇文档 · 最近更新 '}
                    {formatRelativeTime(updatedAt)}
                  </span>

                  {/* 没写简介就整行不渲染：「暂无简介」是替一个空字段占的一行字 */}
                  {kb.description ? <span className="kb-description">{kb.description}</span> : null}

                  {/* 底部：一条**合并的 30 天活动线**（单色 `--accent`、40px、无坐标轴）。
                      入库是"这个库在长"，检索命中是"这个库在被用"，都是活动，合成一条。 */}
                  <span className="kb-card-activity">
                    <span className="kb-activity-head">
                      <span className="kb-activity-label">近 {ACTIVITY_DAYS} 天活动</span>
                      {/* 读数只在这两条趋势真的读回来（或确认读失败）之后才写：
                          加载中摆一个「—」会让人以为"这个库没有活动" */}
                      {ingestTotal !== null || hitTotal !== null ? (
                        <span className="kb-activity-total tabular" title={activityTitle}>
                          入库 {ingestLabel} 篇 · 检索 {formatCount(hitTotal)} 次
                        </span>
                      ) : null}
                    </span>
                    <Sparkline series={activitySeriesOf(ingest, search)} height={40} />
                  </span>
                </Link>
                {/* 管理入口在**第一行右端**（与库名同一行）：写权限才有（与后端"删库属于写"一致）。 */}
                {kb.can_write ? (
                  <KnowledgeBaseSettings kb={kb} className="kb-card-settings" />
                ) : null}
              </li>
            )
          })}
        </ul>
      ) : null}

      {/* 超过阈值切回列表：格子被挤窄之后，"挑一个"反而更慢 */}
      {hasItems && !useCards ? (
        <div className="panel">
          <div className="panel-head kb-row" aria-hidden="true">
            <span className="kb-row-name" style={{ paddingLeft: 'var(--space-4)' }}>
              知识库
            </span>
            <span className="kb-col-num">文档</span>
            <span className="kb-col-time">最近更新</span>
            <span className="kb-col-menu" />
          </div>
          <ul className="kb-rows">
            {store.items.map((kb) => (
              <li key={kb.id} className="kb-row panel-row">
                <Link className="kb-row-link" to={`/kb/${kb.id}`}>
                  <span className="kb-row-name">
                    <span className="kb-mark kb-mark-sm" aria-hidden="true">
                      {initial(kb.name)}
                    </span>
                    <span className="kb-row-text">
                      <span className="kb-row-top">
                        <span className="kb-name">{kb.name}</span>
                        <span className="kb-model">{kb.embedding_model_id}</span>
                      </span>
                      {kb.description ? (
                        <span className="kb-description-one-line">{kb.description}</span>
                      ) : null}
                    </span>
                  </span>
                  <span className="kb-col-num tabular">{store.summaries[kb.id]?.count ?? '—'}</span>
                  <span className="kb-col-time tabular">
                    {formatRelativeTime(store.summaries[kb.id]?.updatedAt ?? null)}
                  </span>
                </Link>
                {kb.can_write ? <KnowledgeBaseSettings kb={kb} /> : null}
                {kb.can_write ? null : <span className="kb-col-menu" />}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <Dialog
        open={createOpen}
        onOpenChange={(next) => {
          if (!next) setCreateOpen(false)
        }}
      >
        <DialogContent
          className="flex max-h-[min(88vh,900px)] flex-col gap-0 p-0"
          onKeyDown={(event) => {
            // 旧 `Modal onEnter`：「随手回车就创建」这一条只在这个弹窗上给。
            // 多行输入里的回车不提交（文本框自己要用它换行）
            if (event.key === 'Enter') {
              if (event.target instanceof HTMLTextAreaElement) return
              void submitCreate()
            }
          }}
        >
          <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
            <DialogTitle>新建知识库</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <div
              className="kb-create-form"
              style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}
            >
              <label className="field">
                <span className="field-label">名称</span>
                <Input
                  id="kb-name"
                  value={draftName}
                  placeholder="例如：产品手册"
                  onChange={(event) => setDraftName(event.target.value)}
                />
              </label>

              <div className="field">
                <span className="field-label">
                  嵌入模型
                  <InfoTip text="建库时定下，之后不能换。" />
                </span>
                {createBlocked ? (
                  <p className="text-note">
                    还没有可用的嵌入模型，无法建库。请先到「设置 →
                    模型注册」添加供应商并登记向量化模型，再到「设置 → 向量化」把它选为默认。
                  </p>
                ) : (
                  <Select
                    value={draftModel || EMBEDDING_DEFAULT}
                    onValueChange={(value) =>
                      setDraftModel(value === EMBEDDING_DEFAULT ? '' : value)
                    }
                  >
                    <SelectTrigger id="kb-embedding" aria-label="嵌入模型">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {embeddingOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>

              {/* 库形态：建库时就要定的第二件大事——它决定"这个库有没有 Wiki" */}
              <fieldset className="field" style={{ border: 0, margin: 0, padding: 0 }}>
                <legend className="field-label">库形态</legend>
                <RadioGroup
                  className="kb-form-options"
                  value={draftForm}
                  onValueChange={(value) => setDraftForm(value as KbForm)}
                >
                  <Label
                    className={['kb-form-option', draftForm === 'vector' ? 'kb-form-option-on' : '']
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <RadioGroupItem value="vector" aria-label="仅向量检索" className="mt-0.5" />
                    <span className="kb-form-option-text">
                      <span className="kb-form-option-title">仅向量检索</span>
                      <span className="kb-form-option-desc">按片段检索原文作答（默认）</span>
                    </span>
                  </Label>
                  <Label
                    className={['kb-form-option', draftForm === 'wiki' ? 'kb-form-option-on' : '']
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <RadioGroupItem value="wiki" aria-label="向量检索 + Wiki" className="mt-0.5" />
                    <span className="kb-form-option-text">
                      <span className="kb-form-option-title">向量检索 + Wiki</span>
                      <span className="kb-form-option-desc">额外整理出一套带出处的百科式页面</span>
                    </span>
                  </Label>
                </RadioGroup>
              </fieldset>

              {/* 切分参数收在折叠区：多数人用默认值就好，但**要用的时候必须找得到** */}
              <details className="kb-advanced">
                <summary>
                  切块与出题（可选，默认 {CHUNK_DEFAULT_SIZE} / {CHUNK_DEFAULT_OVERLAP} · 不出题）
                </summary>
                <div className="kb-advanced-grid">
                  <div className="field">
                    <span className="field-label">块长</span>
                    <RangeField
                      id="kb-chunk-size"
                      value={numberOr(chunkSizeDraft, CHUNK_DEFAULT_SIZE)}
                      onChange={(value) => {
                        setChunkSizeDraft(String(value))
                        // 块长调小后原重叠可能超上限，就地压回——否则滑块停在 max、读数还是旧值
                        const max = chunkOverlapMax(value)
                        if (numberOr(chunkOverlapDraft, 0) > max) setChunkOverlapDraft(String(max))
                      }}
                      min={CHUNK_SIZE_MIN}
                      max={CHUNK_SIZE_MAX}
                      marks={CHUNK_SIZE_MARKS}
                      ariaLabel="块长"
                    />
                  </div>
                  <div className="field">
                    <span className="field-label">块重叠</span>
                    <RangeField
                      id="kb-chunk-overlap"
                      value={numberOr(chunkOverlapDraft, 0)}
                      onChange={(value) => setChunkOverlapDraft(String(value))}
                      min={0}
                      max={chunkOverlapCap}
                      marks={CHUNK_OVERLAP_MARKS}
                      ariaLabel="块重叠"
                    />
                  </div>
                </div>
                {chunkError ? (
                  <p className="kb-chunking-error" role="alert">
                    {chunkError}
                  </p>
                ) : (
                  <p className="text-hint">建库后可在「知识库设置 → 切块策略」调整。</p>
                )}

                {/* 分段出题放在**同一个折叠区**里：它跟的是分段，不是"对话页的展示" */}
                <div className="kb-advanced-divider" role="separator" aria-hidden="true" />
                <SuggestedQuestionsFields
                  value={suggested}
                  onChange={(patch) => setSuggested((current) => ({ ...current, ...patch }))}
                />
              </details>
            </div>
          </div>
          <DialogFooter className="border-t border-[var(--border-hairline)] px-4 py-3">
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button
              variant="default"
              disabled={creating || createBlocked || !canCreate}
              onClick={() => void submitCreate()}
            >
              {creating ? '创建中…' : '创建'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageShell>
  )
}
