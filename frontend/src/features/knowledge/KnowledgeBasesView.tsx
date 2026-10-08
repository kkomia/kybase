/**
 * 知识库（卡片）——旧 `views/KnowledgeBasesView.vue` 的行为逐条对齐。
 *
 * 为什么这里用卡片、而文档清单用行：知识库是**容器型对象且数量少**，用户的动作是
 * "挑一个进去"（扫视比较），不是"在长列表里找某一条"。卡片给每个库一个独立边界，
 * 正好承载"名称 + 规模 + 模型 + 入口"这一小簇信息。条目多起来时（>12）切回行
 * ——这条判定规则原本就写在规范 §5.1。
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
import { revalidateKbCache } from '@/api/kbCache'
import { useKnowledgeProviderStatus, providerGateApplies } from '@/api/provider'
import { EmptyState, InfoTip, SkeletonRows } from '@/features/knowledge/composites'
import { chunkingErrorOf, numberOr, parseIntOrNull } from '@/features/knowledge/chunking'
import { KnowledgeBaseSettings } from '@/features/knowledge/KnowledgeBaseSettings'
import { RangeField } from '@/features/knowledge/RangeField'
import { snapshotNote } from '@/features/knowledge/snapshot'
import {
  SuggestedQuestionsFields,
  type SuggestedQuestionsValue,
} from '@/features/knowledge/SuggestedQuestionsFields'
import { messageOf, notify, useKnowledgeBases, useModelRegistry } from '@/features/knowledge/store'
import { formatCount, formatRelativeTime } from '@/lib/format'
import { Button } from '@/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/ui/dialog'
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'
import { RadioGroup, RadioGroupItem } from '@/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'

/** 规范 §5.1 的阈值：超过 12 个容器就用行而不是卡片，避免格子被挤窄。 */
const CARD_LIMIT = 12

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

export function KnowledgeBasesView() {
  const store = useKnowledgeBases()
  const registry = useModelRegistry()
  /** 提供者的能力集（M3 阶段 6）：建库按 `capabilities.embedding.configured` 决定可用。 */
  const provider = useKnowledgeProviderStatus()
  /** 取出来单放：它是模块级那个函数，引用稳定——焦点那个 effect 不必每次渲染重挂监听。 */
  const refreshSummaries = store.refreshSummaries

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
    // **`store.load()` 里先画一帧**（M4 阶段 5）：本机留的那份库列表与这次实时读
    // 并行发起，内存里已经有清单时**连请求都不发**（判定在 store 那个闸门里）
    void store.load()
    // 提前把注册表拉回来：这样"没有可用嵌入模型"能在点开弹窗**之前**就显示在页头上
    void registry.load()
    // 只做首屏这两次加载
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /**
   * 焦点回到窗口：先让本机确认一份，再自己拉一次实时（§4.4 的接点）。
   *
   * 两件事分工不同：`revalidate` 是**显式动作**（不是每次挂载都发，也不在这里轮询），
   * 去的是本机后端，它顺手把本机那一份更新掉（下次冷启动更快）；后面那次实时读是
   * **可见更新**——用户从别的窗口切回来，看到的是刚确认过的内容。
   *
   * 门控：只有本机档才做（`providerGateApplies`）。服务器档的知识库就是它自己，
   * 那一档一个字都不改（与 M3 同一条纪律）。同一份的单飞与最短间隔在本机后端那边。
   */
  useEffect(() => {
    const onFocus = () => {
      if (!providerGateApplies()) return
      void revalidateKbCache({ resource: 'kb_list' }).then(() => refreshSummaries())
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [refreshSummaries])

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
  /**
   * 提供者那一侧的嵌入能力（M3 阶段 6）：`configured=false` 时**建库入口也挡掉**。
   *
   * 与上面那条不是同一件事：注册表说的是"这台机器上登记了哪些模型"，
   * 而 `capabilities.embedding.configured` 说的是"**提供者现在真的配好了向量化**"
   * （服务器档没这一位时为 `undefined`，那就只按注册表判，不凭空拦人）。
   * 两条任一为真都挡：能建库但检不出向量，用户拿到的是一个查不到东西的库。
   */
  const providerEmbeddingOff = provider.capabilities?.embedding?.configured === false
  const createBlocked = noEmbeddingModel || providerEmbeddingOff
  /** 有模型但没选默认：必须明确挑一个（没有"不指定"这条路了）。 */
  const mustPickModel = !defaultModel && embeddingModels.length > 0

  const chunkOverlapCap = chunkOverlapMax(numberOr(chunkSizeDraft, CHUNK_DEFAULT_SIZE))
  const chunkError = chunkingErrorOf(chunkSizeDraft, chunkOverlapDraft)
  const canCreate =
    draftName.trim().length > 0 && (Boolean(draftModel) || !mustPickModel) && chunkError === ''

  const hasItems = store.items.length > 0
  const useCards = store.items.length <= CARD_LIMIT

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

  /**
   * 页顶那一行小字（M4 阶段 5 / §4.5）。
   *
   * 断连的判据是**这一次实时读失败**（`store.error`）：内容还在屏幕上（是上次看到的那份），
   * 所以要说清"现在连不上"，而不是让用户以为这是刚取回来的。
   */
  const note = snapshotNote(store, store.error !== '')

  return (
    <div className="page-shell">
      <div className="kb-head-actions">
        <h1 style={{ flex: 1 }}>知识库</h1>
        <Button variant="default" disabled={createBlocked} onClick={openCreate}>
          <Plus aria-hidden="true" />
          新建知识库
        </Button>
      </div>

      {/* 建不了库时**页面级**就说清原因：等用户填完名字再报错，白填一遍。
          两条原因分开说——**下一步动作不一样**（一条去登记模型，一条去配向量化）。 */}
      {providerEmbeddingOff ? (
        <p className="kb-blocked-note">
          提供者那侧的向量化还没配好（`capabilities.embedding.configured` 为假），暂时无法新建
          知识库。请在 NAS 上把嵌入模型配好，再回来。
        </p>
      ) : noEmbeddingModel ? (
        <p className="kb-blocked-note">
          还没有可用的嵌入模型，暂时无法新建知识库。请到「设置 → 模型注册」添加供应商并登记
          向量化模型，再到「设置 → 向量化」把它选为默认。
        </p>
      ) : null}

      {/* 快照那一帧的时间戳（在线"上次更新于 X" / 断连"现在连不上…"）：实时结果一落地就收 */}
      {note ? <p className="kb-snapshot-line">{note}</p> : null}

      {store.error ? <p className="kb-error-line">{store.error}</p> : null}

      {store.loading && !hasItems ? <SkeletonRows variant="card" rows={4} /> : null}

      {/* 空态只说"现在没有"，不解释知识库是什么 */}
      {!store.loading && !hasItems ? <EmptyState title="还没有知识库" /> : null}

      {/* 卡片网格：容器型对象、条目少，用卡片承载"挑一个进去"这个动作 */}
      {hasItems && useCards ? (
        <ul className="kb-cards">
          {store.items.map((kb) => (
            <li key={kb.id} className="kb-card-item">
              <Link className="kb-card" to={`/kb/${kb.id}`}>
                {/* 标题行只留**这个名字**：嵌入模型串原先也挂在这里，而它由供应商与
                    库形态决定，同一批库里多半是同一个串（四张卡四个一样的 BAAI/bge-m3）。
                    逐库看它该去详情/设置（「知识库设置 → 基本」里就有），卡片上不放。 */}
                <span className="kb-card-head">
                  <span className="kb-mark" aria-hidden="true">
                    {initial(kb.name)}
                  </span>
                  <span className="kb-card-title">
                    <span className="kb-name">{kb.name}</span>
                  </span>
                </span>

                {/* 卡片主体只用两样东西说清一个库：**一个数字 + 一段概述** */}
                <span className="kb-card-body">
                  <span className="kb-doc-count">
                    <span className="kb-doc-value tabular">
                      {formatCount(store.summaries[kb.id]?.count ?? null)}
                    </span>
                    <span className="kb-doc-unit">篇文档</span>
                  </span>
                  {/* 没写简介就整行不渲染：「暂无简介」是替一个空字段占的一行字 */}
                  {kb.description ? <span className="kb-description">{kb.description}</span> : null}
                </span>

                <span className="kb-card-foot">
                  最近更新 {formatRelativeTime(store.summaries[kb.id]?.updatedAt ?? null)}
                </span>
              </Link>
              {/* 管理入口收在**卡片右下角**（与脚注同一行）：写权限才有（与后端"删库属于写"一致）。
                  原先钉在右上角——那是整张卡最显要的右手位，还压着标题那一行。 */}
              {kb.can_write ? <KnowledgeBaseSettings kb={kb} className="kb-menu-corner" /> : null}
            </li>
          ))}
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
                    {providerEmbeddingOff
                      ? '提供者那侧的向量化还没配好，无法建库。请先在 NAS 上把嵌入模型配好。'
                      : '还没有可用的嵌入模型，无法建库。请先到「设置 → 模型注册」添加供应商并登记向量化模型，再到「设置 → 向量化」把它选为默认。'}
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
    </div>
  )
}
