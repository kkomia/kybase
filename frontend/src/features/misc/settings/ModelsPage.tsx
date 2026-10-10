/**
 * 模型（`/models`，侧栏在「工具」与「API 密钥」之间）——两节：
 *
 * 1. **用途**（本页新写）：把三个槽位各绑一个模型。"出题 / 摘要用哪个大模型""检索用哪个
 *    向量模型"这两件事就在这一节定——它们在后端**共用 `chat` 一个槽位**（见
 *    `backend/app/services/model_registry.py` 的 `SLOTS` 与 `app/services/llm.py` 的
 *    `chat_for`），所以 `chat` 那一行的说明必须点出「出题与摘要」：不点，用户会去别处
 *    找那两格，找不到。
 * 2. **供应商与模型**（`ModelRegistryPanel`）：登记地址、凭据、模型清单。
 *
 * 两节读的是同一份注册表（共用 `REGISTRY_QUERY_KEY`），所以一次请求画全页。
 *
 * ## 写入口对成员全收起
 *
 * 这一族的写端点（绑槽 / 测槽 / 建供应商 / 登记模型 / 删除）全要管理员（后端
 * `require_admin`）。成员进这一页**看得到现状**（三个槽位各绑了什么、有哪些供应商与
 * 模型），但写按钮一个都不渲染——照 `ApiKeysPage` 的口径：与其让人点出一串 403，
 * 不如让"能不能动手"一眼看得出来。读（`GET /model-registry`）成员照旧打得上。
 *
 * ## 槽位那一行的三格
 *
 * 槽位名 → 当下绑的是谁（未绑就明说「未选定」，不留空）→ 换绑 / 测试。
 * 下拉候选只列**该槽位能力匹配、且供应商没停用**的模型：停用供应商的模型绑上去
 * 只会得到一句 422，不该出现在候选里。当下已绑的那个模型即使落在候选之外也照旧列出
 * （供应商被停用、能力对不上），否则这一格是空白而旁边那枚标签说"已选定"，自相矛盾。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { bindSlot, getRegistry, testSlot, type Slot } from '@/api/modelRegistry'
import { useSessionStore } from '@/lib/session'
import { Button } from '@/ui/button'

import {
  OptionSelect,
  PageShell,
  type SelectOption,
  SkeletonBlock,
  StatusTag,
} from '../shared/composites'
import { notifyError, notifySuccess } from '../shared/toast'
import { ModelRegistryPanel, REGISTRY_QUERY_KEY } from './ModelRegistryPanel'

/**
 * 槽位那一行右边的一句话（短标签，不是句子）。
 *
 * `chat` 那一条是这一页的**关键一句**：出题与摘要都落在它上面（用户要的就是"设置生成
 * 问题和摘要的大语言模型"，而这两件事在后端没有各自的槽位）。
 */
const SLOT_NOTES: Record<string, string> = {
  chat: '出题与摘要也走这一格',
  embedding: '建库与检索都用它',
}

/** 一次测槽的结果，就地摊在那一行下面。 */
interface SlotProbe {
  ok: boolean
  detail: string
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

export function ModelsPage() {
  const queryClient = useQueryClient()
  /** 写端点只认管理员：判据与 `ApiKeysPage` / `TasksPage` 同一处口径。 */
  const isAdmin = useSessionStore((store) => store.currentUser?.role === 'admin')

  const registry = useQuery({ queryKey: REGISTRY_QUERY_KEY, queryFn: getRegistry })

  /** 槽位键 → 最近一次测槽的结论（不持久化：换绑之后就过期了）。 */
  const [probes, setProbes] = useState<Record<string, SlotProbe>>({})

  const invalidate = () => queryClient.invalidateQueries({ queryKey: REGISTRY_QUERY_KEY })

  const bind = useMutation({
    mutationFn: (input: { slot: Slot; modelPk: string | null }) =>
      bindSlot(input.slot.slot, input.modelPk),
    onSuccess: async (_result, input) => {
      await invalidate()
      notifySuccess(
        input.modelPk ? `已绑定「${input.slot.label}」` : `「${input.slot.label}」已解除绑定`,
      )
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '绑定失败')),
  })

  const probe = useMutation({
    mutationFn: (slot: Slot) => testSlot(slot.slot),
    onSuccess: (result, slot) => {
      const probe: SlotProbe = {
        ok: result.ok,
        detail: result.detail || (result.ok ? '连接正常' : '测试未通过'),
      }
      setProbes((current) => ({ ...current, [slot.slot]: probe }))
      if (result.ok) notifySuccess(probe.detail)
      else notifyError(probe.detail)
    },
    onError: (cause: unknown, slot) => {
      const detail = messageOf(cause, '测试失败')
      setProbes((current) => ({ ...current, [slot.slot]: { ok: false, detail } }))
      notifyError(detail)
    },
  })

  const providers = registry.data?.providers ?? []
  const models = registry.data?.models ?? []
  const slots = registry.data?.slots ?? []

  /** 停用供应商的模型不进候选（绑上去只会得到一句 422）。 */
  const liveProviders = new Set(providers.filter((item) => item.enabled).map((item) => item.id))

  function optionsFor(slot: Slot): SelectOption[] {
    const candidates = models
      .filter(
        (model) =>
          liveProviders.has(model.provider_id) && model.capabilities.includes(slot.capability),
      )
      .map((model) => ({
        value: model.id,
        label: `${model.label || model.model_id} · ${model.provider_name}`,
      }))
    if (slot.bound_model_pk && !candidates.some((item) => item.value === slot.bound_model_pk)) {
      candidates.unshift({ value: slot.bound_model_pk, label: slot.bound_model_label })
    }
    // 空值 = 不指定（解绑）：换供应商时得有一条退回去的路
    return [{ value: '', label: '不指定' }, ...candidates]
  }

  const refresh = (
    <Button variant="secondary" onClick={() => void registry.refetch()}>
      <RefreshCw size={14} />
      刷新
    </Button>
  )

  return (
    <PageShell title="模型" actions={refresh}>
      {/* 正文那一层与 `TasksPage` **同一结构**（页面自己写一层 `page-shell-body`）：
          这样本页正文与标题行之间的那一档间距，与任务中心那一页逐像素一致。 */}
      <div className="page-shell-body">
        {/* 读不到注册表时这一节整块不画：下面那一块报的是同一份数据，
            同一句「读不到」印两次只会让人以为是两处毛病 */}
        {!registry.isError && (
          <section className="m-block">
            <h3 className="m-block-title">用途</h3>
            {registry.isLoading && <SkeletonBlock variant="text" rows={3} />}
            {slots.map((slot) => {
              const result = probes[slot.slot]
              const testing = probe.isPending && probe.variables?.slot === slot.slot
              return (
                <div key={slot.slot} className="m-slot-field">
                  <div className="m-slot-head">
                    <span className="m-slot-label">{slot.label}</span>
                    {SLOT_NOTES[slot.slot] && (
                      <span className="text-hint">{SLOT_NOTES[slot.slot]}</span>
                    )}
                    {slot.bound_model_pk ? (
                      <StatusTag
                        tone="success"
                        label={
                          slot.provider_name
                            ? `${slot.bound_model_label} · ${slot.provider_name}`
                            : slot.bound_model_label
                        }
                      />
                    ) : (
                      <StatusTag tone="warning" label="未选定" />
                    )}
                  </div>

                  {isAdmin && (
                    <div className="m-form-actions">
                      <OptionSelect
                        className="max-w-[360px]"
                        value={slot.bound_model_pk ?? ''}
                        options={optionsFor(slot)}
                        label={`${slot.label}用哪个模型`}
                        disabled={bind.isPending}
                        onValueChange={(next) =>
                          bind.mutate({ slot, modelPk: next === '' ? null : next })
                        }
                      />
                      <Button
                        variant="secondary"
                        size="sm"
                        aria-label={`测试「${slot.label}」`}
                        disabled={testing || !slot.bound_model_pk}
                        onClick={() => probe.mutate(slot)}
                      >
                        {testing ? '测试中…' : '测试'}
                      </Button>
                    </div>
                  )}

                  {result && (
                    <p
                      className={result.ok ? 'm-test-result m-test-ok' : 'm-test-result m-test-bad'}
                    >
                      {result.detail}
                    </p>
                  )}
                </div>
              )
            })}
          </section>
        )}

        <ModelRegistryPanel canWrite={isAdmin} />
      </div>
    </PageShell>
  )
}
