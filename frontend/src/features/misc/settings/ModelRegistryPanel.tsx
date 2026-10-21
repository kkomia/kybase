/**
 * 模型注册：供应商 → 模型清单（调研报告 G1 / v0.8 归属整理）
 * ——与旧前端 `components/settings/ModelRegistryPanel.vue` 逐条对应。
 *
 * **这一块只做一件事：把模型登记进来。** 地址、凭据、模型名、维度都在这儿填。
 * "哪个用途用哪个模型"不在这儿绑——它由同一页上面那一节（`ModelsPage` 的「用途」）
 * 负责。**注册入口必须唯一**：同一件事（这家供应商的这把钥匙）能在两个地方写，
 * 就必然会漂。
 *
 * 模型行上仍会显示「用于嵌入」这类标记，但那是**只读的状态**：
 * 让用户删模型之前知道会影响什么。
 *
 * `canWrite`（本仓新增，默认 `true`）：这一族的写端点只认管理员，成员进来只看现状
 * ——传 `false` 时那几处写入口（表单与行内菜单）不渲染，现状照旧看得见。
 *
 * 密钥纪律：只显示掩码。**改名字时不回传掩码**——那会把密钥写成掩码。
 */
import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { EllipsisVertical, Plus, RefreshCw } from 'lucide-react'

import {
  createProvider,
  deleteModel,
  deleteProvider,
  getRegistry,
  listAvailableModels,
  registerModel,
  testProvider,
  updateModel,
  updateProvider,
  type AvailableModel,
  type Provider,
  type RegisteredModel,
} from '@/api/modelRegistry'

import { formatCount } from '@/lib/format'

import { notifyError, notifySuccess } from '../shared/toast'
import { Badge } from '@/ui/badge'
import { Button } from '@/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/ui/dropdown-menu'
import { Input } from '@/ui/input'
import {
  CheckRow,
  ConfirmDialog,
  EmptyState,
  Field,
  OptionSelect,
  SkeletonBlock,
} from '../shared/composites'
import { mergeModelOptions, presetModelMeta } from './providerPresets'

export const REGISTRY_QUERY_KEY = ['model-registry'] as const

interface ModelDraft {
  model_id: string
  label: string
  dim: string
  capabilities: string[]
}

const EMPTY_MODEL: ModelDraft = { model_id: '', label: '', dim: '', capabilities: [] }

type DeleteTarget =
  { kind: 'provider'; provider: Provider } | { kind: 'model'; model: RegisteredModel }

/**
 * 供应商那一枚状态徽章。
 *
 * **一枚就够**：同一个人在卡头读的是"这家能不能用"——停用了、缺密钥、正常，
 * 三选一。两枚并排（"已启用" + "未配置密钥"）会同时说"能用"和"不能用"。
 */
function providerBadge(provider: Provider): {
  label: string
  variant: 'success' | 'warning' | 'secondary'
} {
  if (!provider.enabled) return { label: '已停用', variant: 'secondary' }
  if (!provider.api_key_configured) return { label: '未配置密钥', variant: 'warning' }
  return { label: '已启用', variant: 'success' }
}

/**
 * 卡片里的行内表单（编辑供应商 / 登记模型 / 编辑模型）。
 *
 * **它不是第二张卡**：契约说"不要在卡片里再套卡片"。所以这里不套 `.m-form-card`
 * （那个有四面描边 + 圆角 + 浅底，放进白卡里就是一张小卡），而是一条**通栏的浅色带**
 * ——顶上一条发丝线与上面的行分开，左右直接顶到卡片边缘。读起来是"这一块展开成了
 * 表单"，而不是"卡里又开了一张卡"。
 */
const INLINE_FORM = 'flex flex-col gap-3 border-t border-border-hairline bg-subtle px-4 py-3'

/**
 * `canWrite = false`：只读态（成员账号）——写端点全要管理员，与其让人点出一串 403，
 * 不如把"添加供应商"与两条行内菜单收起，清单本身照旧看得见。
 */
export function ModelRegistryPanel({ canWrite = true }: { canWrite?: boolean }) {
  const queryClient = useQueryClient()

  const registry = useQuery({ queryKey: REGISTRY_QUERY_KEY, queryFn: getRegistry })

  const [busy, setBusy] = useState('')
  const [addingProvider, setAddingProvider] = useState(false)
  const [selectedPresetId, setSelectedPresetId] = useState('')
  const [providerDraft, setProviderDraft] = useState({
    kind: 'llm',
    name: '',
    base_url: '',
    api_key: '',
  })
  const [editingProvider, setEditingProvider] = useState('')
  const [providerEdit, setProviderEdit] = useState({ name: '', base_url: '', api_key: '' })
  const [addingModelFor, setAddingModelFor] = useState('')
  const [modelDraft, setModelDraft] = useState<ModelDraft>(EMPTY_MODEL)
  const [availableModels, setAvailableModels] = useState<AvailableModel[]>([])
  const [loadingAvailable, setLoadingAvailable] = useState(false)
  const [availableError, setAvailableError] = useState('')
  /** 候选属于哪个供应商——避免上一个供应商的列表串进下一个的表单。 */
  const availableForRef = useRef('')
  const [editingModel, setEditingModel] = useState('')
  const [modelEdit, setModelEdit] = useState<ModelDraft>(EMPTY_MODEL)
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null)

  const providers = registry.data?.providers ?? []
  const kinds = registry.data?.provider_kinds ?? {}
  const capabilities = registry.data?.capabilities ?? {}
  const slots = registry.data?.slots ?? []
  const presets = registry.data?.provider_presets ?? []

  const invalidate = () => queryClient.invalidateQueries({ queryKey: REGISTRY_QUERY_KEY })

  const presetOptions = [
    // 自定义 = 自己填一个地址
    { value: '', label: '自定义' },
    ...presets.map((preset) => ({ value: preset.id, label: preset.label })),
  ]
  const selectedPreset = presets.find((preset) => preset.id === selectedPresetId) ?? null

  function onPresetPick(id: string): void {
    setSelectedPresetId(id)
    const preset = presets.find((item) => item.id === id)
    if (!preset) {
      // 回到自定义：清空由预设填进来的名称与地址，避免半截状态
      setProviderDraft((current) => ({ ...current, name: '', base_url: '' }))
      return
    }
    setProviderDraft((current) => ({
      ...current,
      kind: preset.kind,
      name: preset.label,
      base_url: preset.base_url,
    }))
  }

  const kindOptions = Object.entries(kinds).map(([value, label]) => ({ value, label }))

  const modelsOf = (providerId: string): RegisteredModel[] =>
    (registry.data?.models ?? []).filter((item) => item.provider_id === providerId)

  /**
   * 用途键 → 中文名。**只用于模型行上的「用于嵌入」这种标记**：绑定动作在
   * 「对话模型」面板里做（嵌入槽位现在没有界面入口），但"这个模型正被谁用着"
   * 要在这里看得见——删它之前得知道会影响什么。
   */
  const slotLabel = (key: string): string => slots.find((item) => item.slot === key)?.label ?? key

  const availableOptions = availableModels.map((item) => ({
    value: item.model_id,
    label: item.owned_by ? `${item.model_id} · ${item.owned_by}` : item.model_id,
  }))

  const createProviderMutation = useMutation({
    mutationFn: () => createProvider(providerDraft),
    onSuccess: async () => {
      setProviderDraft({ kind: 'llm', name: '', base_url: '', api_key: '' })
      setSelectedPresetId('')
      setAddingProvider(false)
      await invalidate()
      notifySuccess('供应商已添加')
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '添加失败')),
    onMutate: () => setBusy('provider:create'),
    onSettled: () => setBusy(''),
  })

  const updateProviderMutation = useMutation({
    mutationFn: (provider: Provider) =>
      updateProvider(provider.id, {
        name: providerEdit.name,
        base_url: providerEdit.base_url,
        // 只有用户真填了才带上 api_key；留空表示保持原值
        ...(providerEdit.api_key ? { api_key: providerEdit.api_key } : {}),
      }),
    onSuccess: async () => {
      setEditingProvider('')
      await invalidate()
      notifySuccess('供应商已更新')
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '更新失败')),
  })

  /** 探活供应商：登记时最会填错的就是地址与凭据。 */
  const testProviderMutation = useMutation({
    mutationFn: (provider: Provider) => testProvider(provider.id),
    onMutate: (provider) => setBusy(`test:${provider.id}`),
    onSuccess: (result) => notifySuccess(result.detail || '连接正常'),
    onError: (cause: unknown) => notifyError(messageOf(cause, '测试失败')),
    onSettled: () => setBusy(''),
  })

  const toggleProviderMutation = useMutation({
    mutationFn: (provider: Provider) => updateProvider(provider.id, { enabled: !provider.enabled }),
    onMutate: (provider) => setBusy(`provider:${provider.id}`),
    onError: (cause: unknown) => notifyError(messageOf(cause, '操作失败')),
    onSettled: async () => {
      setBusy('')
      await invalidate()
    },
  })

  const registerModelMutation = useMutation({
    mutationFn: (providerId: string) =>
      registerModel({
        provider_id: providerId,
        model_id: modelDraft.model_id.trim(),
        label: modelDraft.label,
        dim: modelDraft.dim ? Number(modelDraft.dim) : null,
        capabilities: modelDraft.capabilities,
      }),
    onSuccess: async () => {
      setAddingModelFor('')
      setAvailableModels([])
      setAvailableError('')
      await invalidate()
      notifySuccess('模型已登记')
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '登记失败')),
    onMutate: () => setBusy('model:create'),
    onSettled: () => setBusy(''),
  })

  const updateModelMutation = useMutation({
    mutationFn: (model: RegisteredModel) =>
      updateModel(model.id, {
        model_id: modelEdit.model_id,
        label: modelEdit.label,
        dim: modelEdit.dim ? Number(modelEdit.dim) : null,
        capabilities: modelEdit.capabilities,
      }),
    onSuccess: async () => {
      setEditingModel('')
      await invalidate()
      notifySuccess('模型已更新')
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '更新失败')),
  })

  const confirmDelete = useMutation({
    mutationFn: (target: DeleteTarget) =>
      target.kind === 'provider'
        ? deleteProvider(target.provider.id)
        : deleteModel(target.model.id),
    onSuccess: async (_result, target) => {
      setDeleteTarget(null)
      await invalidate()
      notifySuccess(target.kind === 'provider' ? '供应商已删除' : '模型已删除')
    },
    onError: (cause: unknown) => notifyError(messageOf(cause, '删除失败')),
  })

  /**
   * 拉取该供应商上游可用的模型，喂给「添加模型」的候选框。
   *
   * **失败不弹错误通知**：手写模型 ID 这条出路一直在，探测失败只该是一句就地提示，
   * 不该打断"我要手填"这个动作。
   */
  async function loadAvailable(provider: Provider): Promise<void> {
    availableForRef.current = provider.id
    setLoadingAvailable(true)
    setAvailableError('')
    setAvailableModels([])
    try {
      const result = await listAvailableModels(provider.id)
      if (availableForRef.current !== provider.id) return // 用户已经切走，别写回旧列表
      setAvailableModels(result.models)
    } catch (cause) {
      if (availableForRef.current !== provider.id) return
      setAvailableError(cause instanceof Error ? cause.message : '拉取失败')
    } finally {
      if (availableForRef.current === provider.id) setLoadingAvailable(false)
    }
  }

  function startAddModel(provider: Provider): void {
    setAddingModelFor(provider.id)
    setModelDraft(EMPTY_MODEL)
    void loadAvailable(provider)
  }

  /**
   * 从预设建议里选中一条模型时，把能力与维度也带上。
   *
   * **只有预设里的模型才有这待遇**：手写或上游探测到的 ID 没有元数据可填，
   * 强行猜能力等于替用户做决定（一个 embedding 模型被标成 chat，建库时才发现）。
   */
  const setModelId = (value: string): void => {
    const provider = providers.find((item) => item.id === addingModelFor)
    if (provider) {
      const match = presetModelMeta(presets, provider, value)
      if (match) {
        setModelDraft((current) => ({
          ...current,
          model_id: value,
          capabilities: [...match.capabilities],
          dim: match.dim !== null ? String(match.dim) : current.dim,
          label: current.label || match.label,
        }))
        return
      }
    }
    setModelDraft((current) => ({ ...current, model_id: value }))
  }

  /** 勾选/取消一个能力标记。 */
  const toggleCapability = (
    setter: (updater: (current: ModelDraft) => ModelDraft) => void,
    name: string,
  ) => {
    setter((current) => ({
      ...current,
      capabilities: current.capabilities.includes(name)
        ? current.capabilities.filter((item) => item !== name)
        : [...current.capabilities, name],
    }))
  }

  if (registry.isLoading) return <SkeletonBlock variant="text" rows={4} />

  if (registry.isError) {
    return <EmptyState title="读不到模型注册表" hint={messageOf(registry.error, '请稍后重试')} />
  }

  const deleteLead = !deleteTarget
    ? ''
    : deleteTarget.kind === 'provider'
      ? `删除供应商「${deleteTarget.provider.name}」？`
      : `删除模型「${deleteTarget.model.label || deleteTarget.model.model_id}」？`
  const deleteNote = !deleteTarget
    ? ''
    : deleteTarget.kind === 'provider'
      ? `它下面的 ${formatCount(deleteTarget.provider.model_count)} 个模型会被一并删除，引用这些模型的用途会自动解绑。`
      : deleteTarget.model.bound_slots.length > 0
        ? `它正被 ${formatCount(deleteTarget.model.bound_slots.length)} 个用途使用，删除后会自动解绑。`
        : ''

  return (
    <section className="flex flex-col gap-4">
      {/* 这一块的标题：页标题已经是「模型」，这里只说这一块管什么 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-[length:var(--text-section-size)] font-semibold tracking-[-0.012em] text-text-primary">
          供应商与模型
        </h2>
        {canWrite && (
          /* 表单打开时这一颗退成次要按钮：一次只留一个主动作（表单右下角的「添加」） */
          <Button
            variant={addingProvider ? 'secondary' : 'default'}
            onClick={() => setAddingProvider((value) => !value)}
          >
            {addingProvider ? (
              '取消'
            ) : (
              <>
                <Plus size={14} />
                添加供应商
              </>
            )}
          </Button>
        )}
      </div>

      {addingProvider && (
        <div className="m-form-card max-w-[760px]">
          <div className="m-form-grid">
            {/* 预设：一键填好名称与接口地址（各家地址不一样，抄地址是纯摩擦） */}
            <div className="m-field-wide">
              <Field label="预设">
                <OptionSelect
                  value={selectedPresetId}
                  onValueChange={onPresetPick}
                  options={presetOptions}
                  label="供应商预设"
                />
              </Field>
            </div>
            {selectedPreset?.hint && <p className="m-preset-hint">{selectedPreset.hint}</p>}
            <Field label="类别">
              <OptionSelect
                value={providerDraft.kind}
                onValueChange={(kind) => setProviderDraft((current) => ({ ...current, kind }))}
                options={kindOptions}
                label="供应商类别"
              />
            </Field>
            <Field label="名称" htmlFor="provider-name">
              <Input
                id="provider-name"
                value={providerDraft.name}
                onChange={(event) =>
                  setProviderDraft((current) => ({ ...current, name: event.target.value }))
                }
                placeholder="例如：深度求索"
              />
            </Field>
            <div className="m-field-wide">
              <Field label="接口地址" htmlFor="provider-url">
                <Input
                  id="provider-url"
                  value={providerDraft.base_url}
                  onChange={(event) =>
                    setProviderDraft((current) => ({ ...current, base_url: event.target.value }))
                  }
                  placeholder="https://api.deepseek.com"
                />
              </Field>
            </div>
            <div className="m-field-wide">
              <Field label="API Key" htmlFor="provider-key">
                <Input
                  id="provider-key"
                  type="password"
                  value={providerDraft.api_key}
                  onChange={(event) =>
                    setProviderDraft((current) => ({ ...current, api_key: event.target.value }))
                  }
                  placeholder="sk-…"
                />
              </Field>
            </div>
          </div>
          <div className="m-form-actions justify-end">
            <Button
              disabled={busy === 'provider:create' || !providerDraft.name.trim()}
              onClick={() => createProviderMutation.mutate()}
            >
              {busy === 'provider:create' ? '添加中…' : '添加'}
            </Button>
          </div>
        </div>
      )}

      {providers.length === 0 && !addingProvider && (
        <p className="text-[length:var(--text-meta-size)] text-text-secondary">
          还没有供应商。先添加一家，再登记模型。
        </p>
      )}

      {providers.length > 0 && (
        <div className="stagger grid items-start gap-4 xl:grid-cols-2">
          {providers.map((provider) => {
            const state = providerBadge(provider)
            return (
              <article key={provider.id} className="surface-card flex flex-col overflow-hidden">
                {/* 卡头一行：供应商名 + 一枚状态徽章；右边是"哪把钥匙"与行内菜单。
                    操作全收进「⋯」——五个按钮平铺会把卡片变成按钮墙 */}
                <header className="flex items-center gap-3 px-4 py-3">
                  <h3 className="min-w-0 truncate">{provider.name}</h3>
                  <Badge variant={state.variant}>{state.label}</Badge>
                  <div className="ml-auto flex shrink-0 items-center gap-1.5">
                    {provider.api_key_configured && (
                      <span className="tabular font-mono text-[length:var(--text-micro-size)] text-text-tertiary">
                        {provider.api_key_hint}
                      </span>
                    )}
                    {canWrite && (
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`${provider.name} 的操作`}
                            title={`${provider.name} 的操作`}
                          >
                            <EllipsisVertical />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="start">
                          <DropdownMenuItem
                            disabled={
                              !provider.api_key_configured || busy === `test:${provider.id}`
                            }
                            onSelect={() => testProviderMutation.mutate(provider)}
                          >
                            <RefreshCw size={14} />
                            测试连接
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            onSelect={() => {
                              setEditingProvider(provider.id)
                              // **密钥栏留空**：留空 = 不改。把掩码填进去会被当成新密钥
                              setProviderEdit({
                                name: provider.name,
                                base_url: provider.base_url,
                                api_key: '',
                              })
                            }}
                          >
                            编辑
                          </DropdownMenuItem>
                          <DropdownMenuItem onSelect={() => startAddModel(provider)}>
                            添加模型
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            onSelect={() => toggleProviderMutation.mutate(provider)}
                          >
                            {provider.enabled ? '停用' : '启用'}
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            variant="destructive"
                            onSelect={() => setDeleteTarget({ kind: 'provider', provider })}
                          >
                            删除
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                  </div>
                </header>

                {editingProvider === provider.id && (
                  <div className={INLINE_FORM}>
                    <div className="m-form-grid">
                      <Field label="名称" htmlFor={`provider-edit-name-${provider.id}`}>
                        <Input
                          id={`provider-edit-name-${provider.id}`}
                          value={providerEdit.name}
                          onChange={(event) =>
                            setProviderEdit((current) => ({
                              ...current,
                              name: event.target.value,
                            }))
                          }
                        />
                      </Field>
                      <div className="m-field-wide">
                        <Field label="接口地址" htmlFor={`provider-edit-url-${provider.id}`}>
                          <Input
                            id={`provider-edit-url-${provider.id}`}
                            value={providerEdit.base_url}
                            onChange={(event) =>
                              setProviderEdit((current) => ({
                                ...current,
                                base_url: event.target.value,
                              }))
                            }
                          />
                        </Field>
                      </div>
                      <div className="m-field-wide">
                        <Field label="API Key" htmlFor={`provider-edit-key-${provider.id}`}>
                          <Input
                            id={`provider-edit-key-${provider.id}`}
                            type="password"
                            value={providerEdit.api_key}
                            onChange={(event) =>
                              setProviderEdit((current) => ({
                                ...current,
                                api_key: event.target.value,
                              }))
                            }
                            placeholder="留空表示不修改"
                          />
                        </Field>
                      </div>
                    </div>
                    <div className="m-form-actions justify-end">
                      <Button variant="secondary" onClick={() => setEditingProvider('')}>
                        取消
                      </Button>
                      <Button
                        disabled={busy === `provider:${provider.id}`}
                        onClick={() => updateProviderMutation.mutate(provider)}
                      >
                        保存
                      </Button>
                    </div>
                  </div>
                )}

                {addingModelFor === provider.id && (
                  <div className={INLINE_FORM}>
                    <div className="m-form-grid">
                      <Field label="模型 ID" htmlFor={`model-id-${provider.id}`}>
                        {/* 候选来自上游探测（**不落库**）+ 预设建议；手写这条路一直在。
                      用原生 `datalist`：可搜索、可手写、键盘可达，且没有额外的浮层要维护 */}
                        <Input
                          id={`model-id-${provider.id}`}
                          list={`model-options-${provider.id}`}
                          value={modelDraft.model_id}
                          onChange={(event) => setModelId(event.target.value)}
                          placeholder="选择或直接输入，如 BAAI/bge-m3"
                        />
                        <datalist id={`model-options-${provider.id}`}>
                          {mergeModelOptions(availableOptions, presets, provider).map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </datalist>
                      </Field>
                      <Field label="显示名" optional htmlFor={`model-label-${provider.id}`}>
                        <Input
                          id={`model-label-${provider.id}`}
                          value={modelDraft.label}
                          onChange={(event) =>
                            setModelDraft((current) => ({
                              ...current,
                              label: event.target.value,
                            }))
                          }
                          placeholder="对话主力"
                        />
                      </Field>
                      <Field label="向量维度" optional htmlFor={`model-dim-${provider.id}`}>
                        <Input
                          id={`model-dim-${provider.id}`}
                          value={modelDraft.dim}
                          onChange={(event) =>
                            setModelDraft((current) => ({ ...current, dim: event.target.value }))
                          }
                          placeholder="仅嵌入模型需要"
                        />
                      </Field>
                      <div className="m-field-wide field">
                        <span className="field-label">能力</span>
                        <div className="m-cap-row">
                          {Object.entries(capabilities).map(([key, label]) => (
                            <CheckRow
                              key={key}
                              checked={modelDraft.capabilities.includes(key)}
                              onCheckedChange={() => toggleCapability(setModelDraft, key)}
                            >
                              {label}
                            </CheckRow>
                          ))}
                        </div>
                      </div>
                    </div>
                    {/* 候选来自上游探测：说清"有没有的选"，失败/为空都指明手写这条出路 */}
                    <p className="m-source-note">
                      {loadingAvailable
                        ? '正在从供应商拉取候选模型…'
                        : availableError
                          ? `拉取候选失败：${availableError}。可直接输入模型 ID。`
                          : availableModels.length > 0
                            ? `已拉取到 ${formatCount(availableModels.length)} 个候选，可搜索选择，也可直接输入。`
                            : '供应商没有返回模型列表，手动填写模型 ID。'}
                      {!loadingAvailable && (
                        <button
                          type="button"
                          className="m-source-refresh"
                          onClick={() => void loadAvailable(provider)}
                        >
                          重新拉取
                        </button>
                      )}
                    </p>
                    <div className="m-form-actions justify-end">
                      <Button variant="secondary" onClick={() => setAddingModelFor('')}>
                        取消
                      </Button>
                      <Button
                        disabled={busy === 'model:create' || !modelDraft.model_id.trim()}
                        onClick={() => registerModelMutation.mutate(provider.id)}
                      >
                        登记
                      </Button>
                    </div>
                  </div>
                )}

                {modelsOf(provider.id).length > 0 ? (
                  <ul className="m-0 list-none divide-y divide-border-hairline border-t border-border-hairline p-0">
                    {modelsOf(provider.id).map((model) => (
                      <li key={model.id}>
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5">
                          {/* 名字旁边只留"另一份信息"：维度不重复任何东西 */}
                          <div className="flex min-w-0 flex-1 items-baseline gap-2">
                            <span className="truncate text-[length:var(--text-meta-size)] text-text-primary">
                              {model.label || model.model_id}
                            </span>
                            {model.dim !== null && (
                              <span className="tabular shrink-0 text-[length:var(--text-micro-size)] text-text-tertiary">
                                {model.dim} 维
                              </span>
                            )}
                          </div>
                          {/* 这个模型的角色（删它之前要知道会影响什么）；没有用途就明写
                              「未指定」，不留空位——一列里几行有标记、几行空白，空白会被
                              读成"没渲染出来" */}
                          <div className="flex shrink-0 items-center gap-1.5">
                            {model.bound_slots.length > 0 ? (
                              model.bound_slots.map((slot) => (
                                <Badge key={slot} variant="secondary">
                                  用于{slotLabel(slot)}
                                </Badge>
                              ))
                            ) : (
                              <span className="text-[length:var(--text-micro-size)] text-text-tertiary">
                                未指定
                              </span>
                            )}
                          </div>
                          {canWrite && (
                            <DropdownMenu>
                              <DropdownMenuTrigger asChild>
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`${model.label || model.model_id} 的操作`}
                                  title={`${model.label || model.model_id} 的操作`}
                                >
                                  <EllipsisVertical />
                                </Button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="start">
                                <DropdownMenuItem
                                  onSelect={() => {
                                    setEditingModel(model.id)
                                    setModelEdit({
                                      model_id: model.model_id,
                                      label: model.label,
                                      dim: model.dim === null ? '' : String(model.dim),
                                      capabilities: [...model.capabilities],
                                    })
                                  }}
                                >
                                  编辑
                                </DropdownMenuItem>
                                <DropdownMenuItem
                                  variant="destructive"
                                  onSelect={() => setDeleteTarget({ kind: 'model', model })}
                                >
                                  删除
                                </DropdownMenuItem>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          )}
                        </div>

                        {editingModel === model.id && (
                          <div className={INLINE_FORM}>
                            <div className="m-form-grid">
                              <Field label="模型 ID" htmlFor={`model-edit-id-${model.id}`}>
                                <Input
                                  id={`model-edit-id-${model.id}`}
                                  value={modelEdit.model_id}
                                  onChange={(event) =>
                                    setModelEdit((current) => ({
                                      ...current,
                                      model_id: event.target.value,
                                    }))
                                  }
                                />
                              </Field>
                              <Field label="显示名" htmlFor={`model-edit-label-${model.id}`}>
                                <Input
                                  id={`model-edit-label-${model.id}`}
                                  value={modelEdit.label}
                                  onChange={(event) =>
                                    setModelEdit((current) => ({
                                      ...current,
                                      label: event.target.value,
                                    }))
                                  }
                                />
                              </Field>
                              <Field label="向量维度" htmlFor={`model-edit-dim-${model.id}`}>
                                <Input
                                  id={`model-edit-dim-${model.id}`}
                                  value={modelEdit.dim}
                                  onChange={(event) =>
                                    setModelEdit((current) => ({
                                      ...current,
                                      dim: event.target.value,
                                    }))
                                  }
                                />
                              </Field>
                              <div className="m-field-wide field">
                                <span className="field-label">能力</span>
                                <div className="m-cap-row">
                                  {Object.entries(capabilities).map(([key, label]) => (
                                    <CheckRow
                                      key={key}
                                      checked={modelEdit.capabilities.includes(key)}
                                      onCheckedChange={() => toggleCapability(setModelEdit, key)}
                                    >
                                      {label}
                                    </CheckRow>
                                  ))}
                                </div>
                              </div>
                            </div>
                            <div className="m-form-actions justify-end">
                              <Button variant="secondary" onClick={() => setEditingModel('')}>
                                取消
                              </Button>
                              <Button
                                disabled={busy === `model:${model.id}`}
                                onClick={() => updateModelMutation.mutate(model)}
                              >
                                保存
                              </Button>
                            </div>
                          </div>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  addingModelFor !== provider.id && (
                    <p className="border-t border-border-hairline px-4 py-3 text-[length:var(--text-micro-size)] text-text-tertiary">
                      还没有登记模型。
                    </p>
                  )
                )}
              </article>
            )
          })}
        </div>
      )}

      {/* 删除供应商 / 删除模型：两者的后果不同，由上面两个 computed 按目标拼出来 */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除确认"
        lead={deleteLead}
        note={deleteNote || undefined}
        confirmLabel="删除"
        busy={confirmDelete.isPending}
        busyLabel="删除中…"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && confirmDelete.mutate(deleteTarget)}
      />
    </section>
  )
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
