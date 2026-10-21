/**
 * 「接入」档的正文——照着它就能把客户端接上，五段：
 *
 * 1. **服务地址**：`GET /tools` 给的 `mcp_endpoint` 就是这一格的默认值，**但这一格
 *    可以自己填**——后端没配（`null`）时就用本机上次填过的那条（见 `endpoint.ts`），
 *    于是无论服务端配没配，这一档都能产出可复制的片段。服务端给了值时默认用它、
 *    可以就地改，也留着「用服务端的值」复位。不拼地址：默认值只来自服务端或用户自己；
 * 2. **鉴权**：这一族端点每个请求都要 `Authorization: Bearer <密钥>`，无匿名放行。
 *    管理员可以**就地建一把接入用的密钥**（默认只读档——接入读检索就够了），明文就地
 *    一次性展示；要改范围或撤销，去「API 密钥」页。不是管理员就不给新建入口（新建端点
 *    只认管理员会话），但地址与片段照给——别人给他一把密钥，他照样接得上；
 * 3. **客户端配置片段**：见 `ClientSnippets`；
 * 4. **三步验证**、5. **排障**：每步写清"成功长什么样"，失败对现象先看哪一处。
 *
 * 两处刻意的取舍：
 *
 * - **密钥明文只活在这一次渲染里**：`issued` 不持久化、不回看（后端也只存 hash），
 *   所以文案照实写"离开这张卡片就看不到了"；组件卸载（切档、离开页面）即丢。
 *   建完顺手失效一次 `['api-keys']` 前缀的查询：「API 密钥」页那份列表缓存着，
 *   不失效的话刚建的钥匙在那页 30 秒内看不见；
 * - **地址不合法（含空）时整块配置片段退成空态**：地址是这些片段的唯一变量，坏值拼出来
 *   的是"照抄连不上"，所以字段级校验就够，不弹窗不推通知（用户可能正打到一半）；
 *   空态那两句说的是"填一次地址就能拿到片段"，而不是"服务端没配，所以没有"。
 *
 * 版式：服务地址与鉴权是两张**信息卡**（`.surface-card`，左栏 120px 标签 + 右栏内容），
 * 片段是深一档的面，验证与排障回到画布上的行/表——这一段里没有卡片套卡片。
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'

import { createApiKey, type ApiKeyIssued } from '@/api/apiKeys'
import { copyText } from '@/lib/clipboard'
import { useSessionStore } from '@/lib/session'
import { Button } from '@/ui/button'
import { Input } from '@/ui/input'

import { EmptyState, Notice } from '../shared/composites'
import { notifyError, notifySuccess } from '../shared/toast'
import { ClientSnippets } from './ClientSnippets'
import {
  ENDPOINT_PLACEHOLDER,
  isValidEndpoint,
  readStoredEndpoint,
  writeStoredEndpoint,
} from './endpoint'
import { TOKEN_PLACEHOLDER } from './snippets'

/** 新建密钥时的名字：带日期，一眼看得出这批钥匙是给接入用的。 */
function freshKeyName(): string {
  return `接入密钥 ${new Date().toISOString().slice(0, 10)}`
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

/** 信息卡里的一行：左栏 120px 标签 + 右栏内容。窄屏退成一列，标签在上。 */
const INFO_ROW = 'grid grid-cols-1 gap-x-5 gap-y-1.5 sm:grid-cols-[120px_minmax(0,1fr)]'

/** 信息卡里的标签格：13.5px、二级墨色。 */
const INFO_LABEL = 'text-[length:var(--text-meta-size)] leading-6 text-text-secondary'

/** 正文一档的说明文字：13.5px、二级墨色、行高按 UI 文案走。 */
const BODY_TEXT = 'text-[length:var(--text-meta-size)] leading-[var(--line-ui)] text-text-secondary'

/** 三步验证：往客户端里贴 → 重连 → 真用一次。每步都写"成功长什么样"。 */
const STEPS: readonly { what: string; expect: string }[] = [
  { what: '把片段贴进客户端', expect: '客户端里多出 kybase 这一项。' },
  { what: '重启客户端或重连', expect: '工具列表里能看到 kybase 的工具。' },
  { what: '让它列一次库，或问一句', expect: '列出你的库名，回答里带文档名与出处。' },
]

/** 排障：现象 → 先看哪一处（四条按真实踩过的顺序排）。 */
const TROUBLE: readonly { spot: string; hint: string }[] = [
  {
    spot: '调用被拒 / 401',
    hint: '密钥没带或已撤销：确认 Authorization: Bearer 后面就是那把明文。',
  },
  { spot: '连不上 / 超时', hint: '地址能不能直连；这个服务只在局域网开放。' },
  { spot: '工具比文档少', hint: '重连一次；工具面指纹变了就是契约更新过。' },
  { spot: '403', hint: '库范围或工具范围挡住了，去 API 密钥页核对。' },
]

export function AccessPanel({ endpoint: served }: { endpoint: string | null }) {
  const isAdmin = useSessionStore((store) => store.currentUser?.role === 'admin')
  const queryClient = useQueryClient()
  const [issued, setIssued] = useState<ApiKeyIssued | null>(null)
  /**
   * 这一格现在的值。**取值优先级：服务端给的 → 本机记住的 → 空**（见 `endpoint.ts`）。
   * 一旦就地改过，值就跟着这一格走；本机记住的那条只是服务端没给时的底。
   */
  const [value, setValue] = useState(() => served || readStoredEndpoint())

  const create = useMutation({
    mutationFn: () => createApiKey({ name: freshKeyName(), permission: 'readonly' }),
    onSuccess: async (result) => {
      setIssued(result)
      // 前缀失效：键是「API 密钥」页自己的（那边写死同一串），这里按前缀点一次
      await queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '新建密钥失败')),
  })

  /** 片段里填哪个密钥：建出来了就用明文，没建就用占位（用户自己填）。 */
  const token = issued?.token ?? TOKEN_PLACEHOLDER

  /** 能用来拼片段的地址：不合法（含空）时是空串，那一块就退回空态。 */
  const endpoint = isValidEndpoint(value) ? value.trim() : ''

  /** 这一格与服务端给的那条不一致？（一致就没必要给「用服务端的值」） */
  const overridden = Boolean(served) && value.trim() !== served?.trim()

  /** 填一次就记住（地址不是密钥，见 `endpoint.ts`）；打到一半的值也记，下次接着填。 */
  function edit(next: string): void {
    setValue(next)
    writeStoredEndpoint(next)
  }

  /** 复位到服务端给的那条，并忘掉本机那条覆盖值。 */
  function useServed(): void {
    setValue(served ?? '')
    writeStoredEndpoint('')
  }

  /** 复制一格的值（地址 / 明文密钥都走这一条，成败各推一条通知）。 */
  async function copyValue(text: string): Promise<void> {
    if (await copyText(text)) {
      notifySuccess('已复制')
      return
    }
    notifyError('复制失败，请手动选中复制')
  }

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-3">
        <h2 className="section-label">服务地址</h2>
        <div className="surface-card p-5">
          <div className={INFO_ROW}>
            <span className={INFO_LABEL}>MCP 地址</span>
            <div className="flex min-w-0 flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  className="w-auto min-w-0 flex-1 basis-[160px] font-mono text-[length:var(--text-meta-size)] break-all"
                  value={value}
                  aria-label="MCP 地址"
                  placeholder={ENDPOINT_PLACEHOLDER}
                  spellCheck={false}
                  aria-invalid={value.trim() !== '' && !endpoint ? true : undefined}
                  onChange={(event) => edit(event.target.value)}
                />
                {(endpoint || overridden) && (
                  <div className="flex shrink-0 items-center gap-2">
                    {endpoint && (
                      <Button
                        size="sm"
                        variant="secondary"
                        aria-label="复制服务地址"
                        onClick={() => void copyValue(endpoint)}
                      >
                        复制
                      </Button>
                    )}
                    {overridden && (
                      <Button size="sm" variant="secondary" onClick={useServed}>
                        用服务端的值
                      </Button>
                    )}
                  </div>
                )}
              </div>
              {!served && value.trim() === '' && (
                <p className="text-hint">服务端没给地址，你自己填一次。</p>
              )}
              {value.trim() !== '' && !endpoint && (
                <p className="text-[length:var(--text-meta-size)] text-status-danger">
                  填完整地址：以 http 或 https 开头。
                </p>
              )}
            </div>
          </div>
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="section-label">鉴权</h2>
        <div className="surface-card px-5">
          <div className={`${INFO_ROW} py-4`}>
            <span className={INFO_LABEL}>请求头</span>
            <code className="min-w-0 break-all font-mono text-[length:var(--text-meta-size)] leading-6 text-text-primary">
              Authorization: Bearer &lt;密钥&gt;
            </code>
          </div>

          <div className={`${INFO_ROW} border-t border-[var(--border-hairline)] py-4`}>
            <span className={INFO_LABEL}>密钥</span>
            <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
              <span className={BODY_TEXT}>
                在 <Link to="/api-keys">API 密钥</Link> 页可以管范围与撤销。
              </span>
              {isAdmin && (
                <Button disabled={create.isPending} onClick={() => create.mutate()}>
                  {create.isPending ? '生成中…' : '新建接入用密钥'}
                </Button>
              )}
            </div>
          </div>

          {issued && (
            <div className="border-t border-[var(--border-hairline)] py-4">
              <Notice tone="warn">明文只显示这一次，离开这张卡片就看不到了。</Notice>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  readOnly
                  value={issued.token}
                  aria-label="接入密钥明文"
                  className="w-auto min-w-0 flex-1 basis-[200px] font-mono text-[length:var(--text-meta-size)] break-all"
                  onFocus={(event) => event.target.select()}
                />
                <div className="flex shrink-0 items-center gap-2">
                  <Button aria-label="复制明文密钥" onClick={() => void copyValue(issued.token)}>
                    复制
                  </Button>
                  <Button variant="secondary" onClick={() => setIssued(null)}>
                    我已保存
                  </Button>
                </div>
              </div>
              <p className="mt-3 text-hint">「{issued.name}」已创建。</p>
            </div>
          )}
        </div>

        {!isAdmin && <p className="text-hint">新建密钥要管理员，找管理员要一把再贴进来。</p>}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="section-label">客户端配置片段</h2>
        {endpoint ? (
          <ClientSnippets endpoint={endpoint} token={token} />
        ) : (
          <EmptyState title="填一次地址就能拿到片段" hint="地址填在上面「服务地址」那一格。" />
        )}
      </section>

      {endpoint && (
        <>
          <section className="flex flex-col gap-3">
            <h2 className="section-label">三步验证</h2>
            <ol className="flex flex-col gap-3">
              {STEPS.map((step, index) => (
                <li key={step.what} className="flex items-start gap-3">
                  <span
                    className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-pill bg-subtle text-[length:var(--text-micro-size)] text-text-secondary tabular"
                    aria-hidden="true"
                  >
                    {index + 1}
                  </span>
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <span className="text-[length:var(--text-meta-size)] leading-6 text-text-primary">
                      {step.what}
                    </span>
                    <span className="text-[length:var(--text-micro-size)] leading-[var(--line-ui)] text-text-secondary">
                      成功：{step.expect}
                    </span>
                  </div>
                </li>
              ))}
            </ol>
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="section-label">排障</h2>
            <div className="panel">
              <table className="m-trouble">
                <thead>
                  <tr className="bg-[var(--bg-subtle)]">
                    <th>现象</th>
                    <th>先看这里</th>
                  </tr>
                </thead>
                <tbody>
                  {TROUBLE.map((row) => (
                    <tr key={row.spot}>
                      <td>{row.spot}</td>
                      <td>{row.hint}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
