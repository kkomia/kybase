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
  StatusTag,
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
    <div className="m-block">
      {/* 独立页里的区块标题：页标题已经是「模型」，这里只说这一块管什么 */}
      <h3 className="m-section-title">供应商与模型</h3>
      <div className="m-block-head">
        <h3 className="m-block-title">供应商</h3>
        {canWrite && (
          <Button onClick={() => setAddingProvider((value) => !value)}>
            <Plus size={14} />
            {addingProvider ? '取消' : '添加供应商'}
          </Button>
        )}
      </div>

      {addingProvider && (
        <div className="m-form-card">
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
          <div className="m-form-actions">
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
        <p className="m-muted">还没有供应商。先添加一家，再登记模型。</p>
      )}

      {providers.map((provider) => (
        <div key={provider.id} className="m-provider">
          {/* 卡内三段式：标题 → 元信息 → 次要操作。
              操作全部收进右上角「⋯」——五个按钮平铺会把卡片变成按钮墙 */}
          <div className="m-provider-head">
            <div className="m-provider-title">
              <span className="m-provider-name">{provider.name}</span>
              {!provider.enabled && <StatusTag tone="warning" label="已停用" />}
            </div>
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
                    disabled={!provider.api_key_configured || busy === `test:${provider.id}`}
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
                  <DropdownMenuItem onSelect={() => toggleProviderMutation.mutate(provider)}>
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

          {/*
            供应商卡上**只留"哪把钥匙"**（v0.53）。原先这一行还有「供应商类型」与
            「N 个模型」，下面另起一行印 base_url——三样都不重复任何东西，但也没有
            区分度：这一列里所有供应商都是同一类，模型数量下面那份清单里数得出来，
            而 base_url 是实现细节（要看要改去「编辑」）。用户圈的就是这一块。
            掩码密钥留着：同一个供应商配了几把钥匙时，它是唯一能分辨的那一条。
          */}
          <p className="m-provider-meta">
            <span className="tabular">
              {provider.api_key_configured ? provider.api_key_hint : '未配密钥'}
            </span>
          </p>

          {editingProvider === provider.id && (
            <div className="m-form-card">
              <div className="m-form-grid">
                <Field label="名称" htmlFor={`provider-edit-name-${provider.id}`}>
                  <Input
                    id={`provider-edit-name-${provider.id}`}
                    value={providerEdit.name}
                    onChange={(event) =>
                      setProviderEdit((current) => ({ ...current, name: event.target.value }))
                    }
                  />
                </Field>
                <div className="m-field-wide">
                  <Field label="接口地址" htmlFor={`provider-edit-url-${provider.id}`}>
                    <Input
                      id={`provider-edit-url-${provider.id}`}
                      value={providerEdit.base_url}
                      onChange={(event) =>
                        setProviderEdit((current) => ({ ...current, base_url: event.target.value }))
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
                        setProviderEdit((current) => ({ ...current, api_key: event.target.value }))
                      }
                      placeholder="留空表示不修改"
                    />
                  </Field>
                </div>
              </div>
              <div className="m-form-actions">
                <Button onClick={() => setEditingProvider('')}>取消</Button>
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
            <div className="m-form-card">
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
                      setModelDraft((current) => ({ ...current, label: event.target.value }))
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
              <div className="m-form-actions">
                <Button onClick={() => setAddingModelFor('')}>取消</Button>
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
            <ul className="m-model-list">
              {modelsOf(provider.id).map((model) => (
                <li key={model.id} className="m-model-row">
                  <div className="m-model-title">
                    <span className="m-model-name">{model.label || model.model_id}</span>
                    {/*
                      名字旁边**只留"另一份信息"**（v0.53，用户圈着这一串说"同一个东西
                      说两遍"）：模型 id 与用途原先也印在这里，而两样都已经在别处说过一遍
                      ——名字本身就是 id（或它的短形式），用途是右边那枚胶囊。最刺眼的是
                      `Qwen/Qwen3.5-4B` 那种没有单独显示名的模型：标题与副标题一字不差。
                      维度留着：它不重复任何东西。
                    */}
                    {model.dim && (
                      <span className="m-model-meta">
                        <span className="tabular">{model.dim} 维</span>
                      </span>
                    )}
                  </div>
                  {/*
                    正被哪些用途用着：一眼能看出"删了会影响什么"。
                    两处口径：
                    1. 这一格说的是**这个模型的角色**，不是"一切正常"——原先借 success
                       的绿胶囊当角色色，于是同一张卡里绿色同时表示"已连接""已选定"
                       与"正被某用途使用"，三种意思一件颜色；
                    2. 没有任何用途的模型**显式写「未指定」**，不留空位：一列里几行有
                       标记、几行空白，空白会被读成"没渲染出来"。
                  */}
                  {model.bound_slots.length > 0
                    ? model.bound_slots.map((slot) => (
                        <StatusTag key={slot} tone="neutral" label={`用于${slotLabel(slot)}`} />
                      ))
                    : [<StatusTag key="unbound" tone="neutral" label="未指定" />]}
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

                  {editingModel === model.id && (
                    <div className="m-form-card m-model-edit">
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
                              setModelEdit((current) => ({ ...current, label: event.target.value }))
                            }
                          />
                        </Field>
                        <Field label="向量维度" htmlFor={`model-edit-dim-${model.id}`}>
                          <Input
                            id={`model-edit-dim-${model.id}`}
                            value={modelEdit.dim}
                            onChange={(event) =>
                              setModelEdit((current) => ({ ...current, dim: event.target.value }))
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
                      <div className="m-form-actions">
                        <Button onClick={() => setEditingModel('')}>取消</Button>
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
            addingModelFor !== provider.id && <p className="m-muted">还没有登记模型。</p>
          )}
        </div>
      ))}

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
    </div>
  )
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
