/**
 * 知识库设置（齿轮按钮 + 弹窗）——旧 `components/knowledge/KnowledgeBaseMenu.vue` 的行为对齐。
 *
 * **左侧分组导航 + 右侧内容 + 底部统一保存**（参考 WeKnora）。为什么不是一列到底：
 * 库级配置会越加越多（名称/简介/库信息/数据源/删除…），堆成一列时用户得从头滚到尾才能
 * 确认"这里都有些什么"，而多数设置是**互不相干**的（改简介的人不关心数据源）。
 *
 * 保存收敛到右下**一个**按钮：原先名称与简介各有一个"保存"，两个并列的按钮会让人以为
 * 必须分别点一遍。现在改完任意一项点一次「保存并关闭」，而且**只把真正变了的字段发出去**。
 *
 * 抽成组件而不是在列表（卡片 + 行两种形态）与详情页各写一遍：
 * "删除前先看影响清单"这套交互最不该复制三份。
 */
import { useState, type ComponentType } from 'react'
import { useNavigate } from 'react-router'
import {
  ChevronRight,
  Database,
  Inbox,
  Library,
  Pencil,
  RefreshCw,
  Settings as SettingsIcon,
  Sparkles,
  Trash2,
} from 'lucide-react'

import { batchDocuments } from '@/api/documents'
import {
  CHUNK_DEFAULT_SIZE,
  CHUNK_OVERLAP_MARKS,
  CHUNK_SIZE_MARKS,
  CHUNK_SIZE_MAX,
  CHUNK_SIZE_MIN,
  chunkOverlapMax,
  generateKBPrompt,
  getKnowledgeBaseImpact,
  type KBPromptDraft,
  type KnowledgeBase,
} from '@/api/knowledgeBases'
import { SUGGESTED_COUNT_DEFAULT } from '@/api/knowledgeBases'
import type { ImpactReport } from '@/api/documents'
import { chunkingErrorOf, numberOr, parseIntOrNull } from '@/features/knowledge/chunking'
import { RangeField } from '@/features/knowledge/RangeField'
import { SourcePanel } from '@/features/knowledge/SourcePanel'
import {
  SuggestedQuestionsFields,
  type SuggestedQuestionsValue,
} from '@/features/knowledge/SuggestedQuestionsFields'
import { messageOf, notify, useKnowledgeBases } from '@/features/knowledge/store'
import { copyText } from '@/lib/clipboard'
import { formatBytes, formatCount } from '@/lib/format'
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
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'
import { Textarea } from '@/ui/textarea'

/** 简介上限。与后端 `KB_DESCRIPTION_MAX_CHARS` 对齐，超了后端也会拒。 */
const DESCRIPTION_MAX = 200
/** 库级提示词的字数上限。与后端 `SYSTEM_PROMPT_MAX_CHARS` 对齐。 */
const PROMPT_MAX = 4000

type SectionKey = 'basic' | 'prompt' | 'chunking' | 'wiki' | 'info' | 'sources' | 'danger'

/** 底部有「取消 / 保存并关闭」的分区：只有会改数据的那些。 */
const SAVE_SECTIONS: SectionKey[] = ['basic', 'prompt', 'chunking', 'wiki']

interface SectionItem {
  key: SectionKey
  label: string
  icon: ComponentType<{ size?: number }>
}

/**
 * 左侧导航的分组。**分组不是装饰**：它回答"这些设置属于哪一类"，
 * 用户按"我要改什么"去找，而不是按"第几项"去找。
 */
const GROUPS: { label: string; items: SectionItem[] }[] = [
  {
    label: '基础',
    items: [
      { key: 'basic', label: '基本信息', icon: Pencil },
      { key: 'prompt', label: '回答要求', icon: Sparkles },
      { key: 'info', label: '库信息', icon: Database },
    ],
  },
  {
    label: '数据',
    items: [
      { key: 'chunking', label: '切块策略', icon: SettingsIcon },
      { key: 'wiki', label: 'Wiki', icon: Library },
      { key: 'sources', label: '数据源', icon: Inbox },
    ],
  },
  {
    label: '危险操作',
    items: [{ key: 'danger', label: '删除知识库', icon: Trash2 }],
  },
]

export interface KnowledgeBaseSettingsProps {
  kb: KnowledgeBase
  className?: string
  /** 库级动作的回声：改名（宿主刷新标题）、删除（回列表）、数据源拉取（刷新文档列表）。 */
  onChanged?: (action: 'renamed' | 'deleted' | 'sources') => void
}

export function KnowledgeBaseSettings({ kb, className, onChanged }: KnowledgeBaseSettingsProps) {
  const store = useKnowledgeBases()
  const navigate = useNavigate()

  const [open, setOpen] = useState(false)
  const [section, setSection] = useState<SectionKey>('basic')
  const [nameDraft, setNameDraft] = useState('')
  const [descriptionDraft, setDescriptionDraft] = useState('')
  const [chunkSizeDraft, setChunkSizeDraft] = useState('')
  const [chunkOverlapDraft, setChunkOverlapDraft] = useState('')
  const [saving, setSaving] = useState(false)
  /** 库级提示词（v0.19）：回答这个库的问题时的额外要求。 */
  const [kbPrompt, setKbPrompt] = useState('')
  const [promptDraft, setPromptDraft] = useState<KBPromptDraft | null>(null)
  const [generating, setGenerating] = useState(false)
  const [suggested, setSuggested] = useState<SuggestedQuestionsValue>({
    enabled: true,
    count: SUGGESTED_COUNT_DEFAULT,
    modelPk: '',
    prompt: '',
  })
  const [wikiEnabled, setWikiEnabled] = useState(false)
  /**
   * 刚保存过切分参数、但已有文档还是旧切块：面板上那排「重新摄入全部文档」会转成警示色。
   *
   * 它现在**只负责高亮**：说明文字已删（2026-09-24），"已有文档怎么办"由那颗按钮自己回答。
   */
  const [chunkingStale, setChunkingStale] = useState(false)
  const [reingestOpen, setReingestOpen] = useState(false)
  const [reingesting, setReingesting] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [impact, setImpact] = useState<ImpactReport | null>(null)
  const [deleting, setDeleting] = useState(false)

  const documentCount = store.summaries[kb.id]?.count ?? null

  const chunkOverlapCap = chunkOverlapMax(numberOr(chunkSizeDraft, kb.chunk_size))
  const chunkingError = chunkingErrorOf(chunkSizeDraft, chunkOverlapDraft)
  const chunkingDirty =
    parseIntOrNull(chunkSizeDraft) !== kb.chunk_size ||
    parseIntOrNull(chunkOverlapDraft) !== kb.chunk_overlap
  // `?? ''` 不是防御性摆设：后端比前端旧时字段缺失会写成 undefined，脏检查会一直是 true
  const promptDirty = kbPrompt.trim() !== (kb.system_prompt ?? '')
  const suggestedDirty =
    suggested.enabled !== kb.suggested_enabled ||
    suggested.count !== kb.suggested_count ||
    suggested.modelPk !== (kb.suggested_model_pk ?? '') ||
    suggested.prompt.trim() !== kb.suggested_prompt
  const wikiDirty = wikiEnabled !== kb.wiki_enabled
  const dirty =
    nameDraft.trim() !== kb.name ||
    descriptionDraft.trim() !== kb.description ||
    chunkingDirty ||
    promptDirty ||
    suggestedDirty ||
    wikiDirty

  /** 把提示词草稿拉回"库里存的那份"，并丢掉上一次生成留下的溯源（留着会与框里的内容对不上）。 */
  function resetPrompt(): void {
    setKbPrompt(kb.system_prompt ?? '')
    setPromptDraft(null)
  }

  function resetSuggested(): void {
    setSuggested({
      enabled: kb.suggested_enabled,
      count: kb.suggested_count,
      modelPk: kb.suggested_model_pk ?? '',
      prompt: kb.suggested_prompt,
    })
  }

  function openSettings(): void {
    setSection('basic')
    setNameDraft(kb.name)
    setDescriptionDraft(kb.description)
    setChunkSizeDraft(String(kb.chunk_size))
    setChunkOverlapDraft(String(kb.chunk_overlap))
    resetPrompt()
    resetSuggested()
    setWikiEnabled(kb.wiki_enabled)
    setChunkingStale(false)
    setOpen(true)
  }

  /** 取消：丢掉草稿。不丢的话下次打开会看到上次没存的半截内容。 */
  function cancel(): void {
    setNameDraft(kb.name)
    setDescriptionDraft(kb.description)
    setChunkSizeDraft(String(kb.chunk_size))
    setChunkOverlapDraft(String(kb.chunk_overlap))
    resetPrompt()
    resetSuggested()
    setWikiEnabled(kb.wiki_enabled)
    setChunkingStale(false)
    setOpen(false)
  }

  async function save(): Promise<void> {
    const name = nameDraft.trim()
    if (!name) {
      notify.error('知识库名称不能为空')
      return
    }
    // 参数不合法时**跳到那一栏再说原因**：一个"就是不让你点"的灰按钮除了让人反复试，
    // 什么信息都没给
    if (chunkingError) {
      setSection('chunking')
      notify.error(chunkingError)
      return
    }

    const patch: Parameters<typeof store.update>[1] = {}
    if (name !== kb.name) patch.name = name
    const description = descriptionDraft.trim()
    if (description !== kb.description) patch.description = description
    const size = Number(chunkSizeDraft)
    const overlap = Number(chunkOverlapDraft)
    if (size !== kb.chunk_size) patch.chunk_size = size
    if (overlap !== kb.chunk_overlap) patch.chunk_overlap = overlap
    const chunkingChanged = patch.chunk_size !== undefined || patch.chunk_overlap !== undefined
    const suggestedChanged = suggestedDirty
    if (suggestedChanged) {
      // 四个值一起提交（后端也是一次写四个）。空串表示"跟随对话模型"/"用内置提示词"
      patch.suggested_enabled = suggested.enabled
      patch.suggested_count = suggested.count
      patch.suggested_model_pk = suggested.modelPk
      patch.suggested_prompt = suggested.prompt.trim()
    }
    if (promptDirty) patch.system_prompt = kbPrompt.trim()
    if (wikiDirty) patch.wiki_enabled = wikiEnabled

    // 没改就直接关：发一次空 PATCH 除了浪费一个来回没有任何意义
    if (Object.keys(patch).length === 0) {
      setOpen(false)
      return
    }

    setSaving(true)
    try {
      await store.update(kb.id, patch)
      // 改名会让列表/页面标题跟着变，得让宿主知道
      if (patch.name) onChanged?.('renamed')
      if (suggestedChanged)
        setSuggested((current) => ({ ...current, prompt: current.prompt.trim() }))
      if (chunkingChanged || suggestedChanged) {
        // **不关弹窗**：这两件事都发生在**摄入那一步**，已有文档不会跟着变。
        // 让用户停在这一栏，重新摄入的按钮就在眼前
        setChunkingStale(true)
        setSection('chunking')
        setChunkSizeDraft(String(kb.chunk_size))
        setChunkOverlapDraft(String(kb.chunk_overlap))
        notify.success(chunkingChanged ? '切分参数已保存' : '切块出题设置已保存')
      } else {
        notify.success('已保存')
        setOpen(false)
      }
    } catch (cause) {
      notify.error(messageOf(cause, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  /**
   * 按文档摘要生成一版提示词。生成结果**直接填进编辑框**（而不是只读展示）：
   * 模型写出来的东西是要过目的草稿，不是可以直接生效的配置。
   */
  async function generatePrompt(): Promise<void> {
    setGenerating(true)
    try {
      // 用这个库配的出题模型；没配就跟随默认对话模型（后端逐级回退）
      const draft = await generateKBPrompt(kb.id, kb.suggested_model_pk)
      setPromptDraft(draft)
      setKbPrompt(draft.prompt)
    } catch (cause) {
      notify.error(messageOf(cause, '生成失败'))
    } finally {
      setGenerating(false)
    }
  }

  /**
   * 整库重新摄入：由服务端解析全集（`all = true`），这里只负责把代价说清
   * ——一次云端解析要花钱、要时间，所以放在确认弹窗后面。
   */
  async function confirmReingest(): Promise<void> {
    if (reingesting) return
    setReingesting(true)
    try {
      const result = await batchDocuments(kb.id, 'reprocess', [], null, true)
      setReingestOpen(false)
      setChunkingStale(false)
      if (result.failed > 0) {
        notify.error(`已排队 ${result.succeeded} 篇，${result.failed} 篇没能入队`)
      } else {
        notify.success(`已把 ${result.succeeded} 篇文档排入重新摄入队列`)
      }
      onChanged?.('sources')
    } catch (cause) {
      notify.error(messageOf(cause, '重新摄入失败'))
    } finally {
      setReingesting(false)
    }
  }

  async function copyId(): Promise<void> {
    if (await copyText(kb.id)) {
      notify.success('知识库 ID 已复制')
      return
    }
    notify.error('复制失败，请手动选中复制')
  }

  /** 删除是**不可恢复**的（不进回收站），所以必须先把"会失去什么"摆出来。 */
  async function openDelete(): Promise<void> {
    setImpact(null)
    setDeleteOpen(true)
    try {
      setImpact(await getKnowledgeBaseImpact(kb.id))
    } catch {
      // 拿不到清单不阻断：弹窗会停在"正在统计"，用户仍能取消
      setImpact(null)
    }
  }

  async function confirmDelete(): Promise<void> {
    if (deleting) return
    setDeleting(true)
    try {
      await store.remove(kb.id)
      setDeleteOpen(false)
      setOpen(false)
      notify.success(`已删除知识库「${kb.name}」`)
      onChanged?.('deleted')
    } catch (cause) {
      notify.error(messageOf(cause, '删除失败'))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <span className={className}>
      <Button
        variant="ghost"
        size="icon"
        aria-label={`${kb.name} 的设置`}
        title="知识库设置"
        onClick={openSettings}
      >
        <SettingsIcon aria-hidden="true" />
      </Button>

      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) setOpen(false)
        }}
      >
        {/* 宽档 + 整高：旧 `Modal size="wide" height="full"`（980px / min(88vh,900px)） */}
        <DialogContent
          className="flex h-[min(88vh,900px)] max-h-[min(88vh,900px)] flex-col gap-0 p-0 sm:max-w-[980px]"
          onKeyDown={(event) => {
            // 旧 `Modal onEnter`：「回车 = 保存」这一条只挂在会改数据的分区上
            if (event.key === 'Enter' && SAVE_SECTIONS.includes(section)) {
              if (event.target instanceof HTMLTextAreaElement) return
              void save()
            }
          }}
        >
          <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
            <DialogTitle>知识库设置</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <div className="kb-settings-layout">
              <nav className="kb-settings-nav" aria-label="设置分组">
                {GROUPS.map((group) => (
                  <div key={group.label}>
                    <p className="kb-nav-group">{group.label}</p>
                    {group.items.map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        aria-current={section === item.key ? 'true' : undefined}
                        className={[
                          'kb-nav-item',
                          section === item.key ? 'kb-nav-item-on' : '',
                          item.key === 'danger' ? 'kb-nav-item-danger' : '',
                        ]
                          .filter(Boolean)
                          .join(' ')}
                        onClick={() => setSection(item.key)}
                      >
                        <item.icon size={15} />
                        <span>{item.label}</span>
                      </button>
                    ))}
                  </div>
                ))}
              </nav>

              <div className="kb-pane">
                {section === 'basic' ? (
                  <>
                    <h3 className="kb-pane-title">基本信息</h3>
                    <div className="kb-field">
                      <span className="field-label">知识库 ID</span>
                      <div className="kb-field-inline">
                        <code className="kb-id">{kb.id}</code>
                        <Button
                          size="sm"
                          variant="outline"
                          aria-label="复制知识库 ID"
                          onClick={() => void copyId()}
                        >
                          复制
                        </Button>
                      </div>
                    </div>
                    <label className="kb-field">
                      <span className="field-label">知识库名称</span>
                      <Input
                        id="kb-setting-name"
                        value={nameDraft}
                        placeholder="知识库名称"
                        onChange={(event) => setNameDraft(event.target.value)}
                      />
                    </label>
                    <label className="kb-field">
                      <span className="field-label">知识库描述</span>
                      <Textarea
                        id="kb-setting-description"
                        rows={4}
                        maxLength={DESCRIPTION_MAX}
                        value={descriptionDraft}
                        placeholder="例如：产品说明书与常见问题，面向客服与售前"
                        onChange={(event) => setDescriptionDraft(event.target.value)}
                      />
                      <p className="text-hint" style={{ textAlign: 'right' }}>
                        {formatCount(descriptionDraft.trim().length)} /{' '}
                        {formatCount(DESCRIPTION_MAX)}
                      </p>
                    </label>
                  </>
                ) : null}

                {section === 'info' ? (
                  <>
                    <h3 className="kb-pane-title">库信息</h3>
                    <dl className="kb-info-list">
                      <div>
                        <dt>文档</dt>
                        <dd>{documentCount === null ? '—' : `${formatCount(documentCount)} 篇`}</dd>
                      </div>
                      <div>
                        <dt>嵌入模型</dt>
                        <dd>{kb.embedding_model_id || '—'}</dd>
                      </div>
                      <div>
                        <dt>向量维度</dt>
                        <dd>{kb.embedding_dim || '—'}</dd>
                      </div>
                      <div>
                        <dt>切分</dt>
                        <dd>
                          块长 {kb.chunk_size} / 重叠 {kb.chunk_overlap}
                        </dd>
                      </div>
                    </dl>
                  </>
                ) : null}

                {section === 'prompt' ? (
                  <>
                    <h3 className="kb-pane-title kb-pane-title-standalone">回答要求</h3>
                    <div className="kb-field-inline" style={{ marginBottom: 'var(--space-3)' }}>
                      <Button
                        variant="outline"
                        disabled={generating}
                        onClick={() => void generatePrompt()}
                      >
                        <Sparkles aria-hidden="true" />
                        {generating ? '正在按摘要生成…' : '按文档摘要生成'}
                      </Button>
                    </div>
                    <Textarea
                      id="kb-setting-prompt"
                      rows={12}
                      value={kbPrompt}
                      placeholder="留空 = 只用内置提示词"
                      aria-label="库级提示词"
                      onChange={(event) => setKbPrompt(event.target.value)}
                    />
                    <p className="text-hint" style={{ textAlign: 'right' }}>
                      {formatCount(kbPrompt.trim().length)} / {formatCount(PROMPT_MAX)} 字
                    </p>

                    {/* 溯源：这一段是"不捏造"里**可验证**的那一半（只展示依据了哪些摘要） */}
                    {promptDraft ? (
                      <div className="kb-blocked-note">
                        <p style={{ margin: 0 }}>
                          这次生成依据了 {formatCount(promptDraft.sources.length)} 篇摘要：
                        </p>
                        <ul style={{ margin: 'var(--space-2) 0 0', paddingLeft: 'var(--space-5)' }}>
                          {promptDraft.sources.map((item) => (
                            <li key={item.document_id} className="text-micro">
                              {item.name}
                            </li>
                          ))}
                        </ul>
                        {promptDraft.filename_style_citations.length ? (
                          <p className="kb-chunking-error">
                            这段提示词里还在要求把文件名写进正文（
                            {promptDraft.filename_style_citations.join('、')}
                            ）。它会和系统提示词的编号式引用打架——建议改成 `[1]` `[2]` 再保存。
                          </p>
                        ) : (
                          <p className="text-hint">
                            引用口径是编号式（`[1]` `[2]`，与检索结果对应），正文里不写文件名。
                          </p>
                        )}
                      </div>
                    ) : null}
                  </>
                ) : null}

                {section === 'chunking' ? (
                  <>
                    <h3 className="kb-pane-title kb-pane-title-standalone">切块与出题</h3>

                    <div className="kb-field">
                      <label className="field-label" htmlFor="kb-chunk-size">
                        块长（字符）
                      </label>
                      <RangeField
                        id="kb-chunk-size"
                        value={numberOr(chunkSizeDraft, CHUNK_DEFAULT_SIZE)}
                        onChange={(value) => {
                          setChunkSizeDraft(String(value))
                          // 块长调小以后原重叠可能超过新上限，就地压回（滑块画不出超上限的值）
                          const max = chunkOverlapMax(value)
                          if (numberOr(chunkOverlapDraft, 0) > max)
                            setChunkOverlapDraft(String(max))
                        }}
                        min={CHUNK_SIZE_MIN}
                        max={CHUNK_SIZE_MAX}
                        marks={CHUNK_SIZE_MARKS}
                        snapToMarks
                        editableValue
                        ariaLabel="块长（字符）"
                        valueLabel="块长（字符）"
                      />
                    </div>

                    <div className="kb-field">
                      <label className="field-label" htmlFor="kb-chunk-overlap">
                        块重叠（字符）
                      </label>
                      <RangeField
                        id="kb-chunk-overlap"
                        value={numberOr(chunkOverlapDraft, 0)}
                        onChange={(value) => setChunkOverlapDraft(String(value))}
                        min={0}
                        max={chunkOverlapCap}
                        marks={CHUNK_OVERLAP_MARKS}
                        snapToMarks
                        editableValue
                        ariaLabel="块重叠（字符）"
                        valueLabel="块重叠（字符）"
                      />
                      {/* 这一行留着：上限是个**跟着块长变的数**，提示里写不死 */}
                      <p className="text-hint">上限 {chunkOverlapCap}（块长的一半）。</p>
                    </div>

                    {chunkingError ? (
                      <p className="kb-pane-error" role="alert">
                        {chunkingError}
                      </p>
                    ) : null}

                    {/* 那两句"改动只对之后上传的文档生效"已删（2026-09-24 用户反馈）：
                        控件自身把话说完了——「重新摄入全部文档」这颗按钮就在同一行，
                        它就是"已有文档怎么办"的答案。 */}
                    <div
                      className={['kb-callout', chunkingStale ? 'kb-callout-strong' : '']
                        .filter(Boolean)
                        .join(' ')}
                    >
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={reingesting || documentCount === 0}
                        onClick={() => setReingestOpen(true)}
                      >
                        <RefreshCw aria-hidden="true" />
                        重新摄入全部文档
                      </Button>
                    </div>

                    <div className="kb-pane-divider" role="separator" aria-hidden="true" />
                    <SuggestedQuestionsFields
                      value={suggested}
                      onChange={(patch) => setSuggested((current) => ({ ...current, ...patch }))}
                    />
                  </>
                ) : null}

                {section === 'wiki' ? (
                  <>
                    <h3 className="kb-pane-title kb-pane-title-standalone">Wiki</h3>
                    <div className="kb-field">
                      {/* 14px / 400（同 `.kb-switch`：布局在 CSS，字号/字重在调用点） */}
                      <Label className="kb-switch text-[length:var(--text-meta-size)] font-normal">
                        <Checkbox
                          checked={wikiEnabled}
                          aria-label="为这个知识库开启 Wiki"
                          onCheckedChange={(checked) => setWikiEnabled(checked === true)}
                        />
                        <span>为这个知识库开启 Wiki</span>
                      </Label>
                    </div>
                    <div className="kb-field-inline">
                      <Button
                        variant="outline"
                        disabled={!kb.wiki_enabled}
                        onClick={() => void navigate(`/kb/${kb.id}/wiki`)}
                      >
                        <ChevronRight aria-hidden="true" />
                        打开 Wiki 页面
                      </Button>
                      {!kb.wiki_enabled ? (
                        <span className="text-hint">勾上开关并保存后，入口就会出现。</span>
                      ) : null}
                    </div>
                  </>
                ) : null}

                {section === 'sources' ? (
                  <SourcePanel
                    kbId={kb.id}
                    canWrite={kb.can_write}
                    onChanged={() => onChanged?.('sources')}
                  />
                ) : null}

                {section === 'danger' ? (
                  <>
                    <h3 className="kb-pane-title" style={{ color: 'var(--status-danger)' }}>
                      删除知识库
                    </h3>
                    <p className="text-note">
                      整个知识库连同其中的文档、切块与向量都会被删除，<strong>不会进回收站</strong>
                      ，无法恢复。
                    </p>
                    <Button variant="destructive" onClick={() => void openDelete()}>
                      删除知识库
                    </Button>
                  </>
                ) : null}
              </div>
            </div>
          </div>
          <DialogFooter className="border-t border-[var(--border-hairline)] px-4 py-3">
            {SAVE_SECTIONS.includes(section) ? (
              <>
                <Button variant="outline" onClick={cancel}>
                  取消
                </Button>
                <Button variant="default" disabled={saving || !dirty} onClick={() => void save()}>
                  {saving ? '保存中…' : '保存并关闭'}
                </Button>
              </>
            ) : (
              <Button variant="outline" onClick={() => setOpen(false)}>
                关闭
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/*
        两个确认弹窗都是 `@/ui/alert-dialog`（Radix）。与旧 `ConfirmDialog` 的**两处差异**：
        点「确定」后弹窗立即关闭（旧实现停在忙碌态直到请求回来，失败仍会弹 toast）；
        Esc 与点遮罩不再关闭（AlertDialog 的设计如此：只能走「取消 / 确定」二选一）。
      */}
      <AlertDialog
        open={reingestOpen}
        onOpenChange={(next) => {
          if (!next) setReingestOpen(false)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>重新摄入全部文档</AlertDialogTitle>
            <AlertDialogDescription>
              把「{kb.name}」里的文档全部重新解析、切块与向量化？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">
            这会消耗云端解析额度并占用一段时间；期间知识库照常可检索（旧的切块会保留到新切块写入）。正在处理中的文档会被跳过。
          </p>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmReingest()}>
              开始重新摄入
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={deleteOpen}
        onOpenChange={(next) => {
          if (!next) setDeleteOpen(false)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除知识库</AlertDialogTitle>
            <AlertDialogDescription>确定删除知识库「{kb.name}」？</AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">不可恢复。库里的文档、切块与向量都会删除，不进回收站。</p>
          {impact === null ? (
            <p className="text-hint">正在统计影响…</p>
          ) : (
            <dl className="kb-impact">
              <div>
                <dt>文档</dt>
                <dd className="tabular">{impact.documents}</dd>
              </div>
              <div>
                <dt>切块</dt>
                <dd className="tabular">{impact.chunks}</dd>
              </div>
              <div>
                <dt>占用的空间</dt>
                <dd className="tabular">{formatBytes(impact.size_bytes)}</dd>
              </div>
            </dl>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmDelete()}>删除知识库</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </span>
  )
}
