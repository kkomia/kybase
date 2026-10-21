/**
 * 模型（`/models`，侧栏在「工具」与「API 密钥」之间）——两节：
 *
 * 1. **用途**：把几个槽位各绑一个模型。"出题 / 摘要用哪个大模型""检索用哪个
 *    向量模型"这两件事就在这一节定——它们在后端**共用 `chat` 一个槽位**（见
 *    `backend/app/services/model_registry.py` 的 `SLOTS` 与 `app/services/llm.py` 的
 *    `chat_for`），所以 `chat` 那一格的说明必须点出「出题与摘要」：不点，用户会去别处
 *    找那两格，找不到。
 * 2. **供应商与模型**（`ModelRegistryPanel`）：登记地址、凭据、模型清单。
 *
 * 两节读的是同一份注册表（共用 `REGISTRY_QUERY_KEY`），所以一次请求画全页。
 *
 * ## 版式（2026-10 重做）
 *
 * 页头一句话说清现状（几格用途绑好了、几家供应商、几个模型——全从注册表算出来，
 * 数据没到就不编数字）；下面「用途」是两列小卡：label 12px 三级墨色 → 当前模型
 * 15px 500 → 一行说明。**没绑的那一格用虚线框写「未选定」**，不用警告色：
 * "还没选"是一种状态，不是错误。
 *
 * 再往下是供应商卡片（`.surface-card`）：供应商名 + 一枚状态徽章一行，模型是卡里的
 * **行**（行间走发丝线，不是一行一张卡）。换绑、测槽、增删都还在原处，只是从"每一行
 * 一条横向表单"改成"每一格一张安静的卡"——页面里只有页头与主按钮是响的。
 *
 * ## 写入口对成员全收起
 *
 * 这一族的写端点（绑槽 / 测槽 / 建供应商 / 登记模型 / 删除）全要管理员（后端
 * `require_admin`）。成员进这一页**看得到现状**（几个槽位各绑了什么、有哪些供应商与
 * 模型），但写按钮一个都不渲染——照 `ApiKeysPage` 的口径：与其让人点出一串 403，
 * 不如让"能不能动手"一眼看得出来。读（`GET /model-registry`）成员照旧打得上。
 *
 * ## 槽位卡的三层
 *
 * 槽位名 → 当下绑的是谁（未绑就明说「未选定」，不留空）→ 换绑 / 测试。
 * 下拉候选只列**该槽位能力匹配、且供应商没停用**的模型：停用供应商的模型绑上去
 * 只会得到一句 422，不该出现在候选里。当下已绑的那个模型即使落在候选之外也照旧列出
 * （供应商被停用、能力对不上），否则那一格是空白而旁边那枚标签说"已选定"，自相矛盾。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { bindSlot, getRegistry, testSlot, type Slot } from '@/api/modelRegistry'
import { formatCount } from '@/lib/format'
import { useSessionStore } from '@/lib/session'
import { Button } from '@/ui/button'

import { OptionSelect, PageShell, type SelectOption, SkeletonBlock } from '../shared/composites'
import { notifyError, notifySuccess } from '../shared/toast'
import { ModelRegistryPanel, REGISTRY_QUERY_KEY } from './ModelRegistryPanel'

/**
 * 槽位卡上那一行说明（短标签，不是句子）。
 *
 * `chat` 那一条是这一页的**关键一句**：出题与摘要都落在它上面（用户要的就是"设置生成
 * 问题和摘要的大语言模型"，而这两件事在后端没有各自的槽位）。`rerank` 那一格是可选的，
 * 也照样说清它管什么——三张卡都有一行说明，缺一张就会被读成"这一格还没做完"。
 */
const SLOT_NOTES: Record<string, string> = {
  chat: '出题与摘要也走这一格',
  embedding: '建库与检索都用它',
  rerank: '检索结果按相关度再排一次',
}

/** 一次测槽的结果，就地摊在那一格下面。 */
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

  /** 已绑的槽位数（不是 `configured`：配置回退到环境变量时那一格其实没绑）。 */
  const boundSlots = slots.filter((slot) => Boolean(slot.bound_model_pk)).length
  const pausedProviders = providers.filter((provider) => !provider.enabled).length

  /**
   * 页头那一行：只说从注册表算得出来的事。数据在路上时说"正在读"，读不到时**不补话**
   * ——同一个「读不到」由下面那块的错误态说一次就够，印两遍会让人以为是两处毛病。
   */
  const subtitle = registry.isLoading
    ? '正在读取模型注册表…'
    : registry.data
      ? [
          slots.length > 0 ? `${boundSlots}/${slots.length} 用途已绑定` : null,
          `${formatCount(providers.length)} 家供应商`,
          `${formatCount(models.length)} 个模型`,
          pausedProviders > 0 ? `${formatCount(pausedProviders)} 家已停用` : null,
        ]
          .filter(Boolean)
          .join(' · ')
      : undefined

  const refresh = (
    <Button variant="secondary" onClick={() => void registry.refetch()}>
      <RefreshCw size={14} />
      刷新
    </Button>
  )

  return (
    <PageShell title="模型" subtitle={subtitle} actions={refresh}>
      {/* 两节之间一档大留白（40px）：页头、用途、供应商是三层，不是一张长表 */}
      <div className="flex flex-col gap-10">
        {/* 读不到注册表时这一节整块不画：下面那一块报的是同一份数据，
            同一句「读不到」印两次只会让人以为是两处毛病 */}
        {!registry.isError && (
          <section className="flex flex-col gap-3">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h2 className="text-[length:var(--text-section-size)] font-semibold tracking-[-0.012em] text-text-primary">
                用途
              </h2>
              {isAdmin && (
                <p className="text-[length:var(--text-micro-size)] text-text-tertiary">
                  选中即生效
                </p>
              )}
            </div>

            {registry.isLoading ? (
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <SkeletonBlock variant="card" />
                <SkeletonBlock variant="card" />
              </div>
            ) : (
              <div className="stagger grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {slots.map((slot) => {
                  const result = probes[slot.slot]
                  const testing = probe.isPending && probe.variables?.slot === slot.slot
                  const note = SLOT_NOTES[slot.slot]
                  return (
                    <article key={slot.slot} className="surface-card flex flex-col gap-3 p-4">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-[length:var(--text-micro-size)] font-medium tracking-[0.02em] text-text-tertiary">
                          {slot.label}
                        </p>
                        {isAdmin && (
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label={`测试「${slot.label}」`}
                            disabled={testing || !slot.bound_model_pk}
                            onClick={() => probe.mutate(slot)}
                          >
                            {testing ? '测试中…' : '测试'}
                          </Button>
                        )}
                      </div>

                      {/*
                        值只写一次：管理员那张卡上**下拉本身就是当前值**，
                        再在上面重复一行同名模型只是把卡片撑高（评审指出"同名重复"）。
                        非管理员没有下拉，所以要用这一行把它读出来。
                      */}
                      {slot.bound_model_pk ? (
                        isAdmin ? null : (
                          <p className="truncate text-[length:var(--text-body-size)] font-medium text-text-primary">
                            {slot.bound_model_label}
                            {slot.provider_name ? ` · ${slot.provider_name}` : ''}
                          </p>
                        )
                      ) : (
                        /* 未绑的那一格：虚线 + 三级墨色。**不借警告色**——
                           "还没选"不是错误，而这一页真正的错误在右上角的通知里。
                           写「未选定」而不是「未绑定」：`tests/misc-registry.test.tsx`
                           按这三个字点数（恰好两格），换字要连同用例一起改。 */
                        <p className="rounded-[var(--radius-input)] border border-dashed border-[var(--border-strong)] px-3 py-1.5 text-[length:var(--text-meta-size)] text-text-tertiary">
                          未选定
                        </p>
                      )}

                      {note && (
                        <p className="text-[length:var(--text-micro-size)] leading-[1.65] text-text-tertiary">
                          {note}
                        </p>
                      )}

                      {isAdmin && (
                        <div className="mt-auto">
                          <OptionSelect
                            value={slot.bound_model_pk ?? ''}
                            options={optionsFor(slot)}
                            label={`${slot.label}用哪个模型`}
                            disabled={bind.isPending}
                            onValueChange={(next) =>
                              bind.mutate({ slot, modelPk: next === '' ? null : next })
                            }
                          />
                        </div>
                      )}

                      {result && (
                        <p
                          className={
                            result.ok ? 'm-test-result m-test-ok' : 'm-test-result m-test-bad'
                          }
                        >
                          {result.detail}
                        </p>
                      )}
                    </article>
                  )
                })}
              </div>
            )}
          </section>
        )}

        <ModelRegistryPanel canWrite={isAdmin} />
      </div>
    </PageShell>
  )
}
