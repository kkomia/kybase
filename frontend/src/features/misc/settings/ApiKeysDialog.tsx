/**
 * API 密钥管理（挂在 `AccountMenu` 的「API 密钥」一项上）。
 *
 * 三条刻意的取舍：
 *
 * 1. **入口对成员不收起，打开看到的是 403 态**：这一族端点只认管理员会话
 *    （后端 `require_admin`），成员点了只会白打一趟必被拒的请求——但把入口整个藏掉，
 *    成员连"有这个东西、要找管理员开"都无从知道。所以菜单项照摆，弹窗里按
 *    `currentUser.role` 直接显示受限态（不发请求）；服务端回 403 的（角色在服务端
 *    被降了）也归到同一个态。
 * 2. **明文只在创建成功那一刻出现一次**：`issued` 视图不持久化、不回看——
 *    点「我已保存」或关掉弹窗，那份明文就从界面状态里丢掉（后端也只存 hash）。
 *    文案照实写"关掉就再也看不到"，不给人"下次还能回来看"的误会。
 * 3. **撤销要二次确认**，确认里写清后果（立即生效、在用的地方 401）：
 *    它与分享弹窗的「收回」不同——收回可以再授一次，撤销的钥匙救不回来。
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createApiKey,
  listApiKeys,
  revokeApiKey,
  type ApiKey,
  type ApiKeyIssued,
  type ApiKeyPermission,
} from '@/api/apiKeys'
import { copyText } from '@/lib/clipboard'
import { formatDate, formatRelativeTime } from '@/lib/format'
import { useSessionStore } from '@/lib/session'
import { Button } from '@/ui/button'
import { Input } from '@/ui/input'

import { useKnowledgeBases } from '../shared/knowledgeBases'
import { MultiSelect } from '../shared/MultiSelect'
import {
  ConfirmDialog,
  EmptyState,
  ErrorLine,
  Field,
  Modal,
  Notice,
  OptionSelect,
  SkeletonBlock,
  StatusTag,
} from '../shared/composites'
import { notifyError, notifySuccess } from '../shared/toast'

/** 本弹窗自用的查询键（不与别页共用，所以不进 `queryKeys.ts`）。 */
const API_KEYS_QUERY_KEY = ['api-keys', 'list'] as const

const PERMISSION_OPTIONS: readonly { value: ApiKeyPermission; label: string }[] = [
  { value: 'readonly', label: '只读' },
  { value: 'readwrite', label: '读写' },
]

interface Draft {
  name: string
  permission: ApiKeyPermission
  kbIds: string[]
}

/** 新建草稿的初值：权限档取**最保守的只读**（与后端默认一致），范围空 = 不限。 */
const EMPTY_DRAFT: Draft = { name: '', permission: 'readonly', kbIds: [] }

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

/** 端点回 403 的判据（`api/client.ts` 给错误标过 `status`）。 */
function isForbidden(cause: unknown): boolean {
  return cause instanceof Error && (cause as { status?: number }).status === 403
}

export function ApiKeysDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  /** 这条端点只挂在服务器档且只认管理员：判据与 `TasksPage` 负载面板同一处口径。 */
  const isAdmin = useSessionStore((store) => store.currentUser?.role === 'admin')
  // 本弹窗常驻在 AccountMenu 上（每页都挂着）：库清单只在"开着且能用"时才拉
  const knowledgeBases = useKnowledgeBases(open && isAdmin)

  const [view, setView] = useState<'list' | 'create'>('list')
  const [draft, setDraft] = useState<Draft>({ ...EMPTY_DRAFT })
  /** 创建成功的那份回执（含明文）。非空 = 显示"明文只此一次"的那一态。 */
  const [issued, setIssued] = useState<ApiKeyIssued | null>(null)
  const [removing, setRemoving] = useState<ApiKey | null>(null)

  const list = useQuery({
    queryKey: API_KEYS_QUERY_KEY,
    queryFn: listApiKeys,
    // 非管理员不发这一趟（必被拒）；`open` 收进 enabled：每次打开都重新拉一份，
    // 缓存一份"上次看到的名单"没有意义（别人可能刚建过或撤销过）。
    enabled: open && isAdmin,
  })

  /** 每一次打开都从"列表 + 空草稿"重来，不带上一次的残值（尤其不能带上一把明文）。 */
  useEffect(() => {
    if (!open) return
    setView('list')
    setDraft({ ...EMPTY_DRAFT })
    setIssued(null)
    setRemoving(null)
  }, [open])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY })

  const create = useMutation({
    mutationFn: createApiKey,
    onSuccess: async (result) => {
      setIssued(result)
      setView('list')
      await invalidate()
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '创建失败')),
  })

  const revoke = useMutation({
    mutationFn: (key: ApiKey) => revokeApiKey(key.id),
    onSuccess: async (_result, key) => {
      setRemoving(null)
      notifySuccess(`已撤销「${key.name}」`)
      await invalidate()
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '撤销失败')),
  })

  async function copyIssued(): Promise<void> {
    if (!issued) return
    if (await copyText(issued.token)) notifySuccess('已复制')
    else notifyError('复制失败，请手动选中复制')
  }

  function submitCreate(): void {
    const name = draft.name.trim()
    if (!name || create.isPending) return
    create.mutate({ name, permission: draft.permission, knowledge_base_ids: draft.kbIds })
  }

  /** 库范围那一格的文字：空 = 不限；非空按库名列出（库已被删的照实标出来）。 */
  function scopeText(key: ApiKey): string {
    if (key.knowledge_base_ids.length === 0) return '全部知识库'
    // 清单还在路上时照实空着——抢跑会把"还没查到"读成"已删除的库"
    if (knowledgeBases.isLoading) return '…'
    return key.knowledge_base_ids
      .map((id) => knowledgeBases.items.find((kb) => kb.id === id)?.name ?? '已删除的库')
      .join('、')
  }

  const items = list.data?.items ?? []
  /** 受限态：自己不是管理员，或管理员但服务端仍回 403（角色在服务端被降了）。 */
  const forbidden = !isAdmin || (list.isError && isForbidden(list.error))
  const loadFailed = list.isError && !forbidden

  const kbOptions = knowledgeBases.items.map((kb) => ({ value: kb.id, label: kb.name }))

  return (
    <>
      <Modal
        open={open}
        title="API 密钥"
        onClose={onClose}
        size="md"
        footer={
          issued ? (
            <Button onClick={() => setIssued(null)}>我已保存</Button>
          ) : forbidden ? undefined : view === 'create' ? (
            <>
              <Button onClick={() => setView('list')}>取消</Button>
              <Button
                disabled={draft.name.trim() === '' || create.isPending}
                onClick={submitCreate}
              >
                {create.isPending ? '创建中…' : '创建'}
              </Button>
            </>
          ) : (
            <Button onClick={() => setView('create')}>新建密钥</Button>
          )
        }
      >
        {issued ? (
          <>
            <Notice tone="warn">明文只显示这一次：现在复制并保存好，关掉就再也看不到了。</Notice>
            <div className="flex items-center gap-2">
              <Input
                readOnly
                value={issued.token}
                aria-label="新密钥明文"
                onFocus={(event) => event.target.select()}
              />
              <Button onClick={() => void copyIssued()}>复制</Button>
            </div>
            <p className="m-muted">「{issued.name}」已创建，已进列表。</p>
          </>
        ) : forbidden ? (
          <EmptyState title="需要管理员权限" hint="API 密钥只有管理员能管理，请找管理员开通。" />
        ) : view === 'create' ? (
          <div className="m-form">
            <Field label="名称" htmlFor="apikey-name">
              <Input
                id="apikey-name"
                value={draft.name}
                placeholder="比如：报表脚本"
                onChange={(event) => setDraft((cur) => ({ ...cur, name: event.target.value }))}
              />
            </Field>
            <Field label="权限" hint="只读 = 只能检索与对话；读写 = 还能上传与删除。">
              <OptionSelect
                value={draft.permission}
                options={PERMISSION_OPTIONS}
                label="权限"
                onValueChange={(value) =>
                  setDraft((cur) => ({ ...cur, permission: value as ApiKeyPermission }))
                }
              />
            </Field>
            <div className="field">
              <span className="field-label">库范围</span>
              <MultiSelect
                options={kbOptions}
                value={draft.kbIds}
                onChange={(kbIds) => setDraft((cur) => ({ ...cur, kbIds }))}
                emptyText="还没有知识库可选。"
                label="库范围"
              />
              <span className="text-hint">不选就是不限定范围（全部知识库都能用）。</span>
            </div>
          </div>
        ) : (
          <>
            {list.isLoading && <SkeletonBlock variant="list" rows={3} />}
            {loadFailed && <ErrorLine>{messageOf(list.error, '读不到 API 密钥')}</ErrorLine>}
            {!list.isLoading && !loadFailed && items.length === 0 && (
              <EmptyState title="还没有 API 密钥" hint="新建一把，给脚本或外部集成用。" />
            )}
            {items.length > 0 && (
              <ul className="m-list">
                {items.map((key) => (
                  <li key={key.id} className="m-row">
                    <div className="m-row-main">
                      <span className="m-row-label">
                        {key.name}
                        <StatusTag
                          tone={key.permission === 'readwrite' ? 'warning' : 'neutral'}
                          label={key.permission === 'readwrite' ? '读写' : '只读'}
                        />
                      </span>
                      <span className="m-row-value">{key.prefix}</span>
                      <span className="m-row-value">
                        {scopeText(key)} · 创建于 {formatDate(key.created_at)} · 最近使用{' '}
                        {formatRelativeTime(key.last_used_at)}
                      </span>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`撤销「${key.name}」`}
                      onClick={() => setRemoving(key)}
                    >
                      撤销
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Modal>

      <ConfirmDialog
        open={removing !== null}
        title="撤销这把密钥？"
        lead={`「${removing?.name ?? ''}」撤销立即生效。`}
        note="还在用它的地方都会 401，要换一把新的才能接着用。"
        confirmLabel="撤销"
        busy={revoke.isPending}
        busyLabel="撤销中…"
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          if (removing) revoke.mutate(removing)
        }}
      />
    </>
  )
}
