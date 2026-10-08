/**
 * 数据源订阅面板（M6 / T6.1–T6.3；旧 `SourcePanel.vue` 的行为逐条对齐）。
 *
 * 允许给一个知识库挂上 RSS 订阅或某个网页，之后定时/手动把内容抓进来。
 *
 * 两处刻意的界面决定：
 * 1. **"登记"与"拉取"分开**：刚登记完不会立刻有文档。合成一个动作会让用户以为点完就有了，
 *    然后在文档列表里找不到东西。
 * 2. **拉取结果显示"取回 / 新入库 / 重复"三个数**：看到"取回 20、新入库 0"时该立刻明白
 *    "这个源没更新"，而不是以为抓取失败了。
 */
import { useCallback, useEffect, useState } from 'react'
import { Plus, RefreshCw, Trash2 } from 'lucide-react'

import {
  createDataSource,
  deleteDataSource,
  listDataSources,
  setDataSourceEnabled,
  syncDataSource,
  type DataSource,
  type SourceKind,
} from '@/api/dataSources'
import { EmptyState, StatusTag } from '@/features/knowledge/composites'
import { messageOf, notify } from '@/features/knowledge/store'
import { formatCount, formatRelativeTime } from '@/lib/format'
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
import { Input } from '@/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'

const KIND_OPTIONS: { value: SourceKind; label: string }[] = [
  { value: 'rss', label: 'RSS / Atom 订阅' },
  { value: 'html', label: '单个网页' },
]

const KIND_LABELS: Record<string, string> = { rss: 'RSS 订阅', html: '网页' }

interface SourcePanelProps {
  kbId: string
  canWrite: boolean
  /** 拉取成功后通知宿主刷新文档列表（新文档要出现在列表上）。 */
  onChanged: () => void
}

export function SourcePanel({ kbId, canWrite, onChanged }: SourcePanelProps) {
  const [sources, setSources] = useState<DataSource[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<{ kind: SourceKind; name: string; url: string }>({
    kind: 'rss',
    name: '',
    url: '',
  })
  const [deleteTarget, setDeleteTarget] = useState<DataSource | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setSources((await listDataSources(kbId)).items)
    } catch (cause) {
      notify.error(messageOf(cause, '数据源加载失败'))
    } finally {
      setLoading(false)
    }
  }, [kbId])

  useEffect(() => {
    void load()
  }, [load])

  async function submit(): Promise<void> {
    if (!draft.url.trim()) {
      notify.error('请填地址')
      return
    }
    setBusy('create')
    try {
      await createDataSource(kbId, {
        kind: draft.kind,
        name: draft.name.trim(),
        url: draft.url.trim(),
      })
      setDraft({ kind: 'rss', name: '', url: '' })
      setAdding(false)
      await load()
      notify.success('已登记。点「立即拉取」把内容抓进来')
    } catch (cause) {
      notify.error(messageOf(cause, '登记失败'))
    } finally {
      setBusy('')
    }
  }

  async function sync(source: DataSource): Promise<void> {
    setBusy(`sync:${source.id}`)
    try {
      const result = await syncDataSource(source.id, true)
      await load()
      onChanged()
      if (result.not_modified) {
        notify.success('这个源没有更新')
      } else if (result.created > 0) {
        notify.success(
          `取回 ${result.fetched} 条，新入库 ${result.created} 条` +
            (result.duplicates ? `，跳过重复 ${result.duplicates} 条` : ''),
        )
      } else {
        // 取回了一些但都是重复的——要说清是"没有新内容"而不是失败
        notify.success(`取回 ${result.fetched} 条，都是已有内容（无新增）`)
      }
      if (result.errors.length) {
        notify.error(`${formatCount(result.errors.length)} 条入库失败：${result.errors[0]}`)
      }
    } catch (cause) {
      notify.error(messageOf(cause, '拉取失败'))
    } finally {
      setBusy('')
    }
  }

  async function toggle(source: DataSource): Promise<void> {
    setBusy(`toggle:${source.id}`)
    try {
      await setDataSourceEnabled(source.id, !source.enabled)
      await load()
    } catch (cause) {
      notify.error(messageOf(cause, '操作失败'))
    } finally {
      setBusy('')
    }
  }

  async function confirmRemove(): Promise<void> {
    const source = deleteTarget
    if (!source) return
    try {
      await deleteDataSource(source.id)
      setDeleteTarget(null)
      await load()
      notify.success('数据源已删除，已抓取的文档保留')
    } catch (cause) {
      notify.error(messageOf(cause, '删除失败'))
    }
  }

  return (
    <div>
      <div className="kb-toolbar-actions" style={{ marginBottom: 'var(--space-3)' }}>
        {canWrite ? (
          <Button variant="outline" onClick={() => setAdding((value) => !value)}>
            <Plus aria-hidden="true" />
            {adding ? '取消' : '添加数据源'}
          </Button>
        ) : null}
      </div>

      {/* 只读分享：说明为什么没有操作入口，而不是让按钮点了才报 403 */}
      {!canWrite ? (
        <p className="text-hint">只读分享：你可以查看这里的数据源，但不能添加或修改。</p>
      ) : null}

      {adding ? (
        <div className="kb-source-form">
          <div className="kb-form-row">
            <label className="field">
              <span className="field-label">类型</span>
              <Select
                value={draft.kind}
                onValueChange={(value) => setDraft({ ...draft, kind: value as SourceKind })}
              >
                <SelectTrigger aria-label="数据源类型">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {KIND_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <label className="field">
              <span className="field-label">名称（可选）</span>
              <Input
                value={draft.name}
                placeholder="例如：科技爱好者周刊"
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              />
            </label>
          </div>
          <label className="field">
            <span className="field-label">地址</span>
            <Input
              value={draft.url}
              placeholder={
                draft.kind === 'rss'
                  ? 'https://example.com/feed.xml'
                  : 'https://example.com/some-page'
              }
              onChange={(event) => setDraft({ ...draft, url: event.target.value })}
            />
          </label>
          <Button variant="default" disabled={busy === 'create'} onClick={() => void submit()}>
            {busy === 'create' ? '登记中…' : '登记'}
          </Button>
        </div>
      ) : null}

      {loading ? <p className="text-hint">正在加载数据源…</p> : null}
      {!loading && sources.length === 0 && !adding ? (
        <EmptyState
          title="还没有数据源"
          hint="点「添加数据源」订阅一个 RSS 源，或盯住一个网页让它自动更新。"
        />
      ) : null}

      {!loading && sources.length > 0 ? (
        <ul className="kb-source-list">
          {sources.map((source) => (
            <li key={source.id} className="kb-source-item">
              <span className="kb-source-main">
                <span>{source.name}</span>
                <StatusTag label={KIND_LABELS[source.kind] ?? source.kind} />
                {!source.enabled ? <StatusTag label="已停用" tone="warning" /> : null}
              </span>
              <span className="kb-source-url">{source.url}</span>
              <span className="text-micro">
                {source.last_pulled_at ? formatRelativeTime(source.last_pulled_at) : '从未拉取'}
              </span>
              {canWrite ? (
                <span className="kb-source-actions">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy === `sync:${source.id}`}
                    onClick={() => void sync(source)}
                  >
                    <RefreshCw aria-hidden="true" />
                    {busy === `sync:${source.id}` ? '拉取中…' : '立即拉取'}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => void toggle(source)}>
                    {source.enabled ? '停用' : '启用'}
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="outline"
                    aria-label={`删除 ${source.name}`}
                    onClick={() => setDeleteTarget(source)}
                  >
                    <Trash2 aria-hidden="true" />
                  </Button>
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {/*
        删除确认：`@/ui/alert-dialog`（Radix）。与旧 `ConfirmDialog` 的**两处行为差异**：
        1. 点「确定」后弹窗立即关闭（旧实现停在忙碌态直到请求回来）——失败仍会弹 toast，
           所以不会静默失败；
        2. Esc 与点遮罩不再关闭（AlertDialog 的设计如此：关闭只能走「取消 / 确定」二选一）。
      */}
      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(next) => {
          if (!next) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除数据源</AlertDialogTitle>
            <AlertDialogDescription>
              删除数据源「{deleteTarget?.name ?? ''}」？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="kb-modal-note">已抓进来的文档会保留。</p>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmRemove()}>确定</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
