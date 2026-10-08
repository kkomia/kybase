/**
 * 「推荐问题」设置项（`features/knowledge/SuggestedQuestionsFields.tsx`）——
 * **从旧 Vue 版 `tests/unit/components/SuggestedQuestionsFields.test.ts` 的 4 条搬来的**。
 *
 * 它被建库弹窗与知识库设置**共用**，而且四个值是"受控"的：哪个键名对不上
 * （比如 `modelPk` 写成了 `model_pk`），表现就是"改了没反应"——在两处调用方里都不容易一眼看出来。
 * 这 4 条钉的就是这件事：初值、回传的键名、模型下拉的过滤口径、关掉时的说明。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, useRef } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/modelRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/modelRegistry')>()),
  getRegistry: vi.fn(),
}))

import { getRegistry, type Provider, type RegisteredModel } from '@/api/modelRegistry'
import { renderMisc } from '@/features/misc/testing/harness'
import { SuggestedQuestionsFields } from '@/features/knowledge/SuggestedQuestionsFields'
import { useModelRegistry } from '@/features/knowledge/store'

/** 注册表是页面负责预取的，这个组件自己不拉——测试里补一个只拉一次的探针。 */
function LoadRegistryOnce() {
  const { load } = useModelRegistry()
  const done = useRef(false)
  useEffect(() => {
    if (done.current) return
    done.current = true
    void load()
  }, [load])
  return null
}

const getRegistryMock = vi.mocked(getRegistry)

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: 'p1',
    kind: 'deepseek',
    name: '深度求索',
    base_url: '',
    enabled: true,
    created_at: null,
    updated_at: null,
    api_key_configured: true,
    api_key_hint: '…abcd',
    model_count: 2,
    ...overrides,
  }
}

function model(overrides: Partial<RegisteredModel> = {}): RegisteredModel {
  return {
    id: 'mdl_chat',
    provider_id: 'p1',
    provider_name: '深度求索',
    provider_kind: 'deepseek',
    model_id: 'deepseek-chat',
    label: 'deepseek-chat',
    dim: null,
    capabilities: ['chat'],
    options: {},
    created_at: null,
    updated_at: null,
    bound_slots: [],
    ...overrides,
  }
}

function renderFields(
  value: Partial<{ enabled: boolean; count: number; modelPk: string; prompt: string }> = {},
) {
  const onChange = vi.fn()
  const full = { enabled: true, count: 6, modelPk: '', prompt: '', ...value }
  renderMisc(
    <>
      <LoadRegistryOnce />
      <SuggestedQuestionsFields value={full} onChange={onChange} />
    </>,
  )
  return { onChange }
}

beforeEach(() => {
  vi.clearAllMocks()
  getRegistryMock.mockResolvedValue({
    providers: [provider(), provider({ id: 'p2', name: '停用的家', enabled: false })],
    models: [
      model(),
      model({
        id: 'mdl_embed',
        model_id: 'bge-m3',
        label: 'bge-m3',
        dim: 1024,
        capabilities: ['embedding'],
      }),
      model({
        id: 'mdl_disabled_provider',
        provider_id: 'p2',
        provider_name: '停用的家',
        model_id: 'gpt-x',
        label: 'gpt-x',
      }),
    ],
    slots: [],
    provider_kinds: {},
    capabilities: {},
    provider_presets: [],
  })
})

describe('SuggestedQuestionsFields', () => {
  it('四个控件的初值都来自传入的 value（受控）', async () => {
    renderFields({ enabled: false, count: 3, prompt: '按诊断标准出题' })

    expect(
      await screen.findByRole('checkbox', { name: '为每个切块生成推荐问题' }),
    ).not.toBeChecked()
    expect(screen.getByLabelText('每个切块生成几条问题')).toHaveValue('3')
    expect(screen.getByLabelText('自定义出题提示词')).toHaveValue('按诊断标准出题')
    // modelPk 为空 = 跟随对话模型
    expect(screen.getByRole('combobox', { name: '出题用的模型' })).toHaveTextContent('跟随对话模型')
  })

  it('改开关 / 条数 / 提示词各自回传同一个键名（名字对不上就会"改了没反应"）', async () => {
    const { onChange } = renderFields({ enabled: false })

    await userEvent.click(screen.getByRole('checkbox', { name: '为每个切块生成推荐问题' }))
    expect(onChange).toHaveBeenLastCalledWith({ enabled: true })

    fireEvent.change(screen.getByLabelText('每个切块生成几条问题'), { target: { value: '4' } })
    expect(onChange).toHaveBeenLastCalledWith({ count: 4 })

    fireEvent.change(screen.getByLabelText('自定义出题提示词'), { target: { value: '换个问法' } })
    expect(onChange).toHaveBeenLastCalledWith({ prompt: '换个问法' })
  })

  it('模型下拉只列能对话的模型（停用供应商的也不算），第一项是「跟随对话模型」', async () => {
    renderFields()
    await waitFor(() => expect(getRegistryMock).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('combobox', { name: '出题用的模型' }))
    const options = await screen.findAllByRole('option')

    expect(options.map((item) => item.textContent)).toEqual(['跟随对话模型', 'deepseek-chat'])
    // 向量化模型与"供应商已停用"的模型都不该出现
    expect(screen.queryByRole('option', { name: 'bge-m3' })).toBeNull()
    expect(screen.queryByRole('option', { name: 'gpt-x' })).toBeNull()
  })

  it('选项下面不再挂解释文字：勾/不勾各是什么由控件自己说（2026-09-24 删）', async () => {
    renderFields({ enabled: false })
    // 「入库时为每一段让模型出几个问题…关掉则对话页空状态改用内置的静态示例问题。」
    // 与「每 8 段合并成一次模型调用…」两段都删了：前者是名词解释，后者是实现细节
    expect(screen.queryByText(/改用内置的静态示例问题/)).toBeNull()
    expect(screen.queryByText(/合并成一次模型调用/)).toBeNull()
    // 四个控件都还在（删的是文字，不是功能）
    expect(screen.getByRole('checkbox', { name: '为每个切块生成推荐问题' })).toBeInTheDocument()
    expect(screen.getByRole('slider', { name: '每个切块生成几条问题' })).toBeInTheDocument()
  })
})
