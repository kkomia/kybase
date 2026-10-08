/**
 * 知识库分享弹窗（v10「私有 + 可分享」；旧 `ShareDialog.vue` 的行为逐条对齐）。
 *
 * 三个刻意的取舍：
 * 1. **按登录名授出，不做成员下拉**：`/users` 是控制台级端点，成员调不通——
 *    而分享恰恰是成员最常用的动作。让 owner 敲对方的登录名，是唯一既不提权、
 *    又不泄露名册全貌的方式。
 * 2. **档位在行内改**：把"只读 / 可写"做成每行一个下拉，而不是"删了再授"——
 *    调整权限不该产生一次权限真空。
 * 3. **收回不做二次确认**：它是可逆的（再授一次即可），而确认弹窗会挡住
 *    下面的列表，让"我到底给了谁"看不清。
 */
import { useCallback, useEffect, useState } from 'react'

import { grantShare, listShares, revokeShare, type Share, type SharePermission } from '@/api/shares'
import { messageOf, notify } from '@/features/knowledge/store'
import { Button } from '@/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/ui/dialog'
import { Input } from '@/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'

const PERMISSION_OPTIONS: { value: SharePermission; label: string }[] = [
  { value: 'read', label: '只读' },
  { value: 'write', label: '可写' },
]

interface ShareDialogProps {
  open: boolean
  kbId: string
  kbName: string
  onClose: () => void
}

export function ShareDialog({ open, kbId, kbName, onClose }: ShareDialogProps) {
  const [shares, setShares] = useState<Share[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [usernameDraft, setUsernameDraft] = useState('')
  const [permissionDraft, setPermissionDraft] = useState<SharePermission>('read')
  const [granting, setGranting] = useState(false)
  const [grantError, setGrantError] = useState('')
  /** 正在改档位 / 收回的 user_id：行内按钮据此显示忙碌态。 */
  const [busyId, setBusyId] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setShares((await listShares(kbId)).items)
      setLoadError('')
    } catch (cause) {
      setLoadError(messageOf(cause, '分享列表加载失败'))
    } finally {
      setLoading(false)
    }
  }, [kbId])

  // 每次打开都重新拉：别人可能刚被授出或收回，缓存一份过期的名单没有意义。
  // 顺带把上一次的草稿清掉（关掉再打开看到上次没提交的名字会让人以为已经分享了）。
  useEffect(() => {
    if (!open) return
    setUsernameDraft('')
    setPermissionDraft('read')
    setGrantError('')
    void load()
  }, [open, load])

  async function submitGrant(): Promise<void> {
    const username = usernameDraft.trim()
    if (!username) {
      setGrantError('请填写对方的登录名')
      return
    }
    setGranting(true)
    setGrantError('')
    try {
      const share = await grantShare(kbId, username, permissionDraft)
      setUsernameDraft('')
      notify.success(`已分享给 ${share.name}（${share.username}）`)
      await load()
    } catch (cause) {
      setGrantError(messageOf(cause, '分享失败'))
    } finally {
      setGranting(false)
    }
  }

  async function changePermission(share: Share, permission: string): Promise<void> {
    if (permission === share.permission) return
    setBusyId(share.user_id)
    try {
      await grantShare(kbId, share.username, permission as SharePermission)
      await load()
    } catch (cause) {
      notify.error(messageOf(cause, '调整档位失败'))
      await load()
    } finally {
      setBusyId('')
    }
  }

  async function revoke(share: Share): Promise<void> {
    setBusyId(share.user_id)
    try {
      await revokeShare(kbId, share.user_id)
      notify.success(`已收回「${share.name}」的访问`)
      await load()
    } catch (cause) {
      notify.error(messageOf(cause, '收回失败'))
    } finally {
      setBusyId('')
    }
  }

  return (
    /** 弹窗壳：`@/ui/dialog`（README §3 的组合）。标题栏/底部各带一条分隔线、内容区自己滚。 */
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent className="flex max-h-[min(88vh,900px)] flex-col gap-0 p-0">
        <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
          <DialogTitle>分享知识库</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <p className="kb-lead">把「{kbName}」分享给其他成员。对方登录后就能在列表里看到它。</p>

          <div className="kb-grant">
            <Input
              className="kb-grant-input"
              value={usernameDraft}
              placeholder="对方的登录名"
              aria-label="对方的登录名"
              disabled={granting}
              onChange={(event) => setUsernameDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submitGrant()
              }}
            />
            <Select
              value={permissionDraft}
              onValueChange={(value) => setPermissionDraft(value as SharePermission)}
              disabled={granting}
            >
              <SelectTrigger className="kb-grant-permission" aria-label="访问档位">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PERMISSION_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="default" disabled={granting} onClick={() => void submitGrant()}>
              {granting ? '分享中…' : '分享'}
            </Button>
          </div>
          {grantError ? (
            <p className="kb-share-error" role="alert">
              {grantError}
            </p>
          ) : null}
          <p className="text-hint">
            只读 = 可检索、可对话；可写 = 还能上传与删除。被分享者不能把库再转授给别人。
          </p>

          <h3 className="kb-share-title">已分享给</h3>
          {loading ? <p className="text-hint">正在加载…</p> : null}
          {!loading && loadError ? <p className="kb-share-error">{loadError}</p> : null}
          {!loading && !loadError && shares.length === 0 ? (
            <p className="text-hint">
              还没有分享给任何人。这个库目前只有你自己（和管理员）能看到。
            </p>
          ) : null}
          {!loading && shares.length > 0 ? (
            <ul className="kb-share-list">
              {shares.map((share) => (
                <li key={share.user_id} className="kb-share-row">
                  <span className="kb-share-person">
                    <span className="kb-share-name">{share.name}</span>
                    <span className="kb-share-username">{share.username}</span>
                  </span>
                  <div style={{ width: 104 }}>
                    <Select
                      value={share.permission}
                      disabled={busyId === share.user_id}
                      onValueChange={(value) => void changePermission(share, value)}
                    >
                      <SelectTrigger aria-label={`调整 ${share.name} 的访问档位`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {PERMISSION_OPTIONS.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <Button
                    variant="destructive"
                    size="sm"
                    disabled={busyId === share.user_id}
                    onClick={() => void revoke(share)}
                  >
                    收回
                  </Button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}
