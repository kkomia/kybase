/**
 * API 密钥（`/api-keys`）——它原来挂在 `AccountMenu` 上是个弹窗，2026-10-09 搬成一页。
 *
 * 一页比一个弹窗合适的地方：这里的动作是**反复发生的日常管理**（建一把给脚本、
 * 看谁在用、撤掉过期的），弹窗每次打开都从零重来、也进不了书签；而它的近亲
 * （任务中心 / 工具）都已经是页。弹窗留下来的只有两处：**新建**与**撤销确认**。
 *
 * 四条刻意的取舍：
 *
 * 1. **入口对成员不收起，进来看到的是 403 态**：这一族端点只认管理员会话
 *    （后端 `require_admin`），成员点了只会白打一趟必被拒的请求——但把入口整个藏掉，
 *    成员连"有这个东西、要找管理员开"都无从知道。所以侧栏项照摆，页面按
 *    `currentUser.role` 直接显示受限态（不发请求）；服务端回 403 的（角色在服务端
 *    被降了）也归到同一个态。
 * 2. **明文只在创建成功那一刻出现一次**：`issued` 那一块不持久化、不回看——
 *    点「我已保存」或离开这一页，那份明文就从界面状态里丢掉（后端也只存 hash）。
 *    文案照实写"关掉就再也看不到"，不给人"下次还能回来看"的误会。
 * 3. **撤销要二次确认**，确认里写清后果（立即生效、在用的地方 401）：
 *    它与分享弹窗的「收回」不同——收回可以再授一次，撤销的钥匙救不回来。
 * 4. **工具范围的候选项来自 `GET /tools`**（`TOOLS_QUERY_KEY`），不在这页另抄一份清单。
 *    这一格与库范围、权限档是三条**并列**的收窄轴，三个都是"不选 = 不限"；
 *    读不到工具清单时这一格退化成空清单（照实说读不到），不拦这一页——
 *    列表与创建本身都不依赖它。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import {
  createApiKey,
  listApiKeys,
  revokeApiKey,
  type ApiKey,
  type ApiKeyIssued,
  type ApiKeyPermission,
} from '@/api/apiKeys'
import { getToolCatalogue } from '@/api/tools'
import { copyText } from '@/lib/clipboard'
import { formatDate, formatRelativeTime } from '@/lib/format'
import { useSessionStore } from '@/lib/session'
import { Badge } from '@/ui/badge'
import { Button } from '@/ui/button'
import { Input } from '@/ui/input'

import { TOOLS_QUERY_KEY } from '../queryKeys'
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
  PageShell,
  SkeletonBlock,
} from '../shared/composites'
import { notifyError, notifySuccess } from '../shared/toast'

/** 本页自用的查询键（不与别页共用，所以不进 `queryKeys.ts`）。 */
const API_KEYS_QUERY_KEY = ['api-keys', 'list'] as const

const PERMISSION_OPTIONS: readonly { value: ApiKeyPermission; label: string }[] = [
  { value: 'readonly', label: '只读' },
  { value: 'readwrite', label: '读写' },
]

interface Draft {
  name: string
  permission: ApiKeyPermission
  kbIds: string[]
  toolNames: string[]
}

/** 新建草稿的初值：权限档取**最保守的只读**（与后端默认一致），两条范围空 = 不限。 */
const EMPTY_DRAFT: Draft = { name: '', permission: 'readonly', kbIds: [], toolNames: [] }

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

/** 端点回 403 的判据（`api/client.ts` 给错误标过 `status`）。 */
function isForbidden(cause: unknown): boolean {
  return cause instanceof Error && (cause as { status?: number }).status === 403
}

/**
 * 权限档那一枚徽章。
 *
 * **两档都用中性色**（2026-10 评审之后改的）：一列"读写"如果全是琥珀色，
 * 整页读起来像一排告警，而"读写"是这把钥匙的**能力档**、不是异常状态。
 * 区分靠**形状**：读写是实心浅底、只读是描边——一眼能分开，又不吵。
 * （语义色只留给"出问题了"的状态。）
 */
function PermissionBadge({ permission }: { permission: ApiKeyPermission }) {
  if (permission === 'readwrite') {
    return <Badge variant="secondary">读写</Badge>
  }
  return <Badge variant="outline">只读</Badge>
}

export function ApiKeysPage() {
  const queryClient = useQueryClient()
  /** 这条端点只挂在服务器档且只认管理员：判据与 `TasksPage` 负载面板同一处口径。 */
  const isAdmin = useSessionStore((store) => store.currentUser?.role === 'admin')
  // 候选项只在"能用得上"时才拉：成员看到的是受限态，拉回来只是白打两趟
  const knowledgeBases = useKnowledgeBases(isAdmin)

  const [createOpen, setCreateOpen] = useState(false)
  const [draft, setDraft] = useState<Draft>({ ...EMPTY_DRAFT })
  /** 创建成功的那份回执（含明文）。非空 = 显示"明文只此一次"的那一块。 */
  const [issued, setIssued] = useState<ApiKeyIssued | null>(null)
  const [removing, setRemoving] = useState<ApiKey | null>(null)

  const list = useQuery({
    queryKey: API_KEYS_QUERY_KEY,
    queryFn: listApiKeys,
    // 非管理员不发这一趟（必被拒）
    enabled: isAdmin,
  })

  const tools = useQuery({
    queryKey: TOOLS_QUERY_KEY,
    queryFn: getToolCatalogue,
    enabled: isAdmin,
    // 工具清单只是候选与标签：读不到时不该把这一页变成错误态
    retry: false,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY })

  const create = useMutation({
    mutationFn: createApiKey,
    onSuccess: async (result) => {
      setIssued(result)
      setCreateOpen(false)
      setDraft({ ...EMPTY_DRAFT })
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

  function openCreate(): void {
    // 每次打开都从空草稿开始，不带上一次的残值
    setDraft({ ...EMPTY_DRAFT })
    setCreateOpen(true)
  }

  async function copyIssued(): Promise<void> {
    if (!issued) return
    if (await copyText(issued.token)) notifySuccess('已复制')
    else notifyError('复制失败，请手动选中复制')
  }

  function submitCreate(): void {
    const name = draft.name.trim()
    if (!name || create.isPending) return
    create.mutate({
      name,
      permission: draft.permission,
      knowledge_base_ids: draft.kbIds,
      allowed_tool_names: draft.toolNames,
    })
  }

  /** 库范围那一格的文字：空 = 不限；非空按库名列出（库已被删的照实标出来）。 */
  function kbScopeText(key: ApiKey): string {
    if (key.knowledge_base_ids.length === 0) return '全部知识库'
    // 清单还在路上时照实空着——抢跑会把"还没查到"读成"已删除的库"
    if (knowledgeBases.isLoading) return '…'
    return key.knowledge_base_ids
      .map((id) => knowledgeBases.items.find((kb) => kb.id === id)?.name ?? '已删除的库')
      .join('、')
  }

  /**
   * 工具范围那一格的文字：空 = 全部；非空按**标签**列出，清单里查不到就退回工具名
   * （工具可能刚被摘掉——照实把名字写出来，比写"未知"有用）。
   */
  function toolScopeText(key: ApiKey): string {
    if (key.allowed_tool_names.length === 0) return '全部'
    if (tools.isLoading) return '…'
    return key.allowed_tool_names
      .map((name) => tools.data?.items.find((tool) => tool.name === name)?.label ?? name)
      .join('、')
  }

  const items = list.data?.items ?? []
  /** 受限态：自己不是管理员，或管理员但服务端仍回 403（角色在服务端被降了）。 */
  const forbidden = !isAdmin || (list.isError && isForbidden(list.error))
  const loadFailed = list.isError && !forbidden

  const readonlyCount = items.filter((key) => key.permission === 'readonly').length
  /**
   * 页头那一行：只说从这份清单算得出来的两件事（一共几把、其中几把只读）。
   * 受限态与读不到时**不补话**——下面那块已经把那句话完整说过一遍，
   * 同一句「需要管理员权限」印两次，读起来像两处毛病。
   */
  const subtitle =
    forbidden || loadFailed
      ? undefined
      : list.isLoading
        ? '正在读取密钥清单…'
        : items.length > 0
          ? `共 ${items.length} 把 · ${readonlyCount} 把只读`
          : undefined

  const kbOptions = knowledgeBases.items.map((kb) => ({ value: kb.id, label: kb.name }))
  const toolOptions = (tools.data?.items ?? []).map((tool) => ({
    value: tool.name,
    label: `${tool.label}（${tool.name}）`,
  }))

  return (
    <PageShell
      title="API 密钥"
      subtitle={subtitle}
      actions={
        forbidden ? undefined : (
          <>
            <Button variant="secondary" onClick={() => void list.refetch()}>
              <RefreshCw size={14} />
              刷新
            </Button>
            <Button onClick={openCreate}>新建密钥</Button>
          </>
        )
      }
    >
      {forbidden ? (
        <EmptyState title="需要管理员权限" hint="API 密钥只有管理员能管理，请找管理员开通。" />
      ) : (
        <div className="flex flex-col gap-6">
          {/* 明文只此一次：创建成功后就地摊在列表上方，点「我已保存」才收起
            （做成弹窗的话，Esc 一按这块内容就没了，而它是这次操作唯一要留存的东西） */}
          {issued && (
            <section className="surface-card anim-rise p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <p className="section-label">新密钥</p>
                <Button variant="ghost" size="sm" onClick={() => setIssued(null)}>
                  我已保存
                </Button>
              </div>
              <Notice tone="warn">明文只显示这一次：现在复制并保存好，关掉就再也看不到了。</Notice>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  readOnly
                  value={issued.token}
                  aria-label="新密钥明文"
                  className="min-w-[240px] flex-1 font-mono text-[12.5px]"
                  onFocus={(event) => event.target.select()}
                />
                <Button onClick={() => void copyIssued()}>复制</Button>
              </div>
              <p className="mt-2 text-[length:var(--text-micro-size)] text-text-tertiary">
                「{issued.name}」已创建，已进列表。
              </p>
            </section>
          )}

          {list.isLoading && <SkeletonBlock variant="list" rows={3} />}
          {loadFailed && <ErrorLine>{messageOf(list.error, '读不到 API 密钥')}</ErrorLine>}
          {!list.isLoading && !loadFailed && items.length === 0 && (
            <EmptyState title="还没有 API 密钥" hint="新建一把，给脚本或外部集成用。" />
          )}

          {/* 清单是一张 `.panel`：行与行之间只有一条发丝线，没有一行一张卡。
              每行四段：哪把钥匙（名称 + 权限档 + 前缀）→ 两条收窄轴 → 两个时间 →
              撤销。窄屏时按段折行（撤销靠右），不裁内容。 */}
          {items.length > 0 && (
            <ul className="panel m-0 list-none p-0">
              {items.map((key) => (
                <li
                  key={key.id}
                  className="panel-row flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
                >
                  <div className="min-w-0 flex-1 basis-[200px]">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[length:var(--text-body-size)] font-medium text-text-primary">
                        {key.name}
                      </span>
                      <PermissionBadge permission={key.permission} />
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[12.5px] leading-[1.5] text-text-tertiary">
                      {key.prefix}
                    </p>
                  </div>

                  {/* 两条收窄轴：空 = 不限（照实写"全部"），不选库时也明说 */}
                  <div className="flex min-w-[160px] flex-[1.1] flex-col gap-0.5">
                    <span className="text-[length:var(--text-meta-size)] leading-[1.45] text-text-secondary">
                      范围 {kbScopeText(key)}
                    </span>
                    <span className="text-[length:var(--text-meta-size)] leading-[1.45] text-text-tertiary">
                      工具 {toolScopeText(key)}
                    </span>
                  </div>

                  {/* 两个时间：创建是绝对时间，最近使用是相对口径（没用过就是 —
                      ）。这一格宽度按最长串留够，折行时不会被挤断 */}
                  <div className="tabular flex w-[170px] shrink-0 flex-col gap-0.5 whitespace-nowrap text-[length:var(--text-meta-size)] leading-[1.45] text-text-tertiary">
                    <span>创建于 {formatDate(key.created_at)}</span>
                    <span>最近使用 {formatRelativeTime(key.last_used_at)}</span>
                  </div>

                  {/* 危险动作：静止时是**中性灰字**，悬停才转 danger 色 + 浅红底。
                      一列 9 个红色「撤销」会把整页读成一条告警带——危险动作要能被找到，
                      但不该在扫视时一直喊。`ml-auto`：窄屏折行时它留在右端。 */}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="ml-auto text-text-secondary hover:bg-[var(--danger-soft)] hover:text-status-danger"
                    aria-label={`撤销「${key.name}」`}
                    onClick={() => setRemoving(key)}
                  >
                    撤销
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <Modal
        open={createOpen}
        title="新建密钥"
        onClose={() => setCreateOpen(false)}
        size="md"
        footer={
          <>
            <Button onClick={() => setCreateOpen(false)}>取消</Button>
            <Button disabled={draft.name.trim() === '' || create.isPending} onClick={submitCreate}>
              {create.isPending ? '创建中…' : '创建'}
            </Button>
          </>
        }
      >
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
          <div className="field">
            <span className="field-label">工具范围</span>
            <MultiSelect
              options={toolOptions}
              value={draft.toolNames}
              onChange={(toolNames) => setDraft((cur) => ({ ...cur, toolNames }))}
              emptyText="读不到工具清单，这一格没法勾。"
              label="工具范围"
            />
            <span className="text-hint">不选就是不限定工具（全部工具都能调）。</span>
          </div>
        </div>
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
    </PageShell>
  )
}
