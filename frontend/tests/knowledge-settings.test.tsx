/**
 * 知识库设置弹窗（`KnowledgeBaseSettings`）的用例。
 *
 * 对应旧用例 `frontend/tests/unit/components/KnowledgeBaseMenu.test.ts` 的逻辑型条目：
 * 只发真正变了的字段、切块滑杆的吸附与数字框联动、保存切分参数后不关弹窗、
 * 「重新摄入全部文档」走 `all = true`、删除前先取影响清单。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeBaseSettings } from '@/features/knowledge'
import { resetKnowledgeBaseCache, resetRegistryCache } from '@/features/knowledge/store'
import type { KnowledgeBase } from '@/api/knowledgeBases'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn().mockResolvedValue(true) }))

vi.mock('@/api/knowledgeBases', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/knowledgeBases')>()),
  listKnowledgeBases: vi.fn(),
  updateKnowledgeBase: vi.fn(),
  deleteKnowledgeBase: vi.fn(),
  getKnowledgeBaseImpact: vi.fn(),
  generateKBPrompt: vi.fn(),
  createKnowledgeBase: vi.fn(),
}))

vi.mock('@/api/documents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/documents')>()),
  batchDocuments: vi.fn(),
}))

vi.mock('@/api/dataSources', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/dataSources')>()),
  listDataSources: vi.fn().mockResolvedValue({ items: [] }),
  createDataSource: vi.fn(),
  setDataSourceEnabled: vi.fn(),
  deleteDataSource: vi.fn(),
  syncDataSource: vi.fn(),
}))

vi.mock('@/api/modelRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/modelRegistry')>()),
  getRegistry: vi.fn().mockResolvedValue({
    providers: [],
    models: [],
    slots: [],
    provider_kinds: {},
    capabilities: {},
    provider_presets: [],
  }),
}))

import {
  deleteKnowledgeBase,
  generateKBPrompt,
  getKnowledgeBaseImpact,
  listKnowledgeBases,
  updateKnowledgeBase,
} from '@/api/knowledgeBases'
import { batchDocuments } from '@/api/documents'
import { copyText } from '@/lib/clipboard'

const listKbMock = vi.mocked(listKnowledgeBases)
const updateMock = vi.mocked(updateKnowledgeBase)
const deleteMock = vi.mocked(deleteKnowledgeBase)
const impactMock = vi.mocked(getKnowledgeBaseImpact)
const generateMock = vi.mocked(generateKBPrompt)
const batchMock = vi.mocked(batchDocuments)
const copyMock = vi.mocked(copyText)
const successToast = vi.mocked(toast.success)
const errorToast = vi.mocked(toast.error)

function makeKB(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: 'kb-1',
    name: '产品手册',
    description: '说明书与常见问题',
    embedding_model_id: 'bge-m3',
    embedding_dim: 1024,
    chunk_strategy: 'recursive',
    chunk_size: 512,
    chunk_overlap: 64,
    suggested_enabled: false,
    suggested_count: 3,
    suggested_model_pk: null,
    suggested_prompt: '',
    system_prompt: '',
    wiki_enabled: false,
    created_at: null,
    can_manage: true,
    can_write: true,
    document_count: 5,
    last_activity: null,
    ...overrides,
  }
}

type ChangedFn = (action: 'renamed' | 'deleted' | 'sources') => void

let onChanged: ReturnType<typeof vi.fn<ChangedFn>>

function renderSettings(kb: KnowledgeBase = makeKB()) {
  onChanged = vi.fn<ChangedFn>()
  return render(
    <MemoryRouter>
      <KnowledgeBaseSettings kb={kb} onChanged={onChanged} />
    </MemoryRouter>,
  )
}

async function openSettings(kb: KnowledgeBase = makeKB()) {
  const user = userEvent.setup()
  renderSettings(kb)
  await user.click(screen.getByRole('button', { name: `${kb.name} 的设置` }))
  return user
}

beforeEach(() => {
  vi.clearAllMocks()
  resetKnowledgeBaseCache()
  resetRegistryCache()
  listKbMock.mockResolvedValue({ items: [makeKB()] })
  updateMock.mockImplementation(async (kbId, patch) => ({ ...makeKB(), id: kbId, ...patch }))
})

afterEach(() => {
  resetKnowledgeBaseCache()
  resetRegistryCache()
})

describe('设置弹窗的分区', () => {
  it('左侧按分组列出可设置项，默认停在基本信息，底部有统一保存', async () => {
    await openSettings()

    const nav = screen.getByRole('navigation', { name: '设置分组' })
    for (const label of [
      '基本信息',
      '回答要求',
      '库信息',
      '切块策略',
      'Wiki',
      '数据源',
      '删除知识库',
    ]) {
      expect(within(nav).getByRole('button', { name: label })).toBeInTheDocument()
    }
    expect(screen.getByLabelText('知识库名称')).toHaveValue('产品手册')
    expect(screen.getByRole('button', { name: '保存并关闭' })).toBeDisabled()
  })

  it('库信息只读呈现建库时冻结的参数，且底部没有保存按钮', async () => {
    await openSettings()

    await userEvent.setup().click(screen.getByRole('button', { name: '库信息' }))
    expect(screen.getByText('块长 512 / 重叠 64')).toBeInTheDocument()
    expect(screen.getByText('bge-m3')).toBeInTheDocument()
    expect(screen.getByText('1024')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存并关闭' })).not.toBeInTheDocument()
    // 弹窗壳换成 `@/ui/dialog` 之后右上角多了一颗同名的关闭按钮（`sr-only` 的「关闭」），
    // 所以按底部的那个位置（`data-slot="dialog-footer"`）取
    const dialog = screen.getByRole('dialog', { name: '知识库设置' })
    const footer = dialog.querySelector('[data-slot="dialog-footer"]') as HTMLElement
    expect(within(footer).getByRole('button', { name: '关闭' })).toBeInTheDocument()
  })

  it('只发真正变了的字段：改名称不发别的，没改动时保存按钮禁用', async () => {
    const user = await openSettings()

    await user.clear(screen.getByLabelText('知识库名称'))
    await user.type(screen.getByLabelText('知识库名称'), '新名字')
    const save = screen.getByRole('button', { name: '保存并关闭' })
    await waitFor(() => expect(save).toBeEnabled())
    await user.click(save)

    await waitFor(() => expect(updateMock).toHaveBeenCalledWith('kb-1', { name: '新名字' }))
    expect(onChanged).toHaveBeenCalledWith('renamed')
    expect(successToast).toHaveBeenCalledWith('已保存')
  })

  it('复制知识库 ID 走剪贴板那条路', async () => {
    const user = await openSettings()

    await user.click(screen.getByRole('button', { name: '复制知识库 ID' }))
    await waitFor(() => expect(copyMock).toHaveBeenCalledWith('kb-1'))
    expect(successToast).toHaveBeenCalledWith('知识库 ID 已复制')
  })
})

describe('切块参数的滑杆与数字框', () => {
  async function openChunking(kb = makeKB()) {
    const user = await openSettings(kb)
    await user.click(screen.getByRole('button', { name: '切块策略' }))
    return user
  }

  it('预填当前值：滑杆范围固定在 128–2048，默认值处有刻度点', async () => {
    await openChunking()

    const slider = screen.getByRole('slider', { name: '块长（字符）' })
    expect(slider).toHaveAttribute('min', '128')
    expect(slider).toHaveAttribute('max', '2048')
    expect(screen.getByRole('spinbutton', { name: '块长（字符）' })).toHaveValue(512)
    // 刻度数值（常用值）都画出来了（256 在重叠那条轨道上也有一份）
    for (const mark of ['256', '512', '1024', '2048']) {
      expect(screen.getAllByText(mark).length).toBeGreaterThan(0)
    }
  })

  it('拖动吸附到最近的常用档（1000 → 1024），键盘路径不吸附', async () => {
    await openChunking()
    const slider = screen.getByRole('slider', { name: '块长（字符）' })

    // 指针拖动：靠近刻度就吸过去
    fireEvent.pointerDown(slider)
    fireEvent.change(slider, { target: { value: '1000' } })
    expect(screen.getByRole('spinbutton', { name: '块长（字符）' })).toHaveValue(1024)

    // 指针松开之后（键盘/箭头键那一路）不再吸附：1000 就是 1000
    fireEvent.pointerUp(slider)
    fireEvent.change(slider, { target: { value: '1000' } })
    expect(screen.getByRole('spinbutton', { name: '块长（字符）' })).toHaveValue(1000)
  })

  it('块长调小以后重叠自动压回新上限（不超过块长的一半）', async () => {
    await openChunking(makeKB({ chunk_overlap: 256 }))
    const overlap = screen.getByRole('spinbutton', { name: '块重叠（字符）' })
    expect(overlap).toHaveValue(256)

    fireEvent.pointerDown(screen.getByRole('slider', { name: '块长（字符）' }))
    fireEvent.change(screen.getByRole('slider', { name: '块长（字符）' }), {
      target: { value: '256' },
    })

    expect(screen.getByRole('spinbutton', { name: '块长（字符）' })).toHaveValue(256)
    expect(overlap).toHaveValue(128)
    expect(screen.getByText('上限 128（块长的一半）。')).toBeInTheDocument()
  })

  it('数字框可以直接输入：回车生效，超范围夹回，清空等于这次输入没发生', async () => {
    await openChunking()
    const size = () => screen.getByRole('spinbutton', { name: '块长（字符）' })

    // 手打 1000：吸附不替手打的数做主；回车只提交数字，不会顺手把弹窗关掉
    fireEvent.change(size(), { target: { value: '1000' } })
    fireEvent.keyDown(size(), { key: 'Enter' })
    expect(size()).toHaveValue(1000)
    expect(screen.getByRole('dialog', { name: '知识库设置' })).toBeInTheDocument()

    // 超出上限：失焦时夹回 2048
    fireEvent.change(size(), { target: { value: '9999' } })
    fireEvent.blur(size())
    expect(size()).toHaveValue(2048)

    // 清空：解析不出来就回显当前值，等于这次输入没发生
    fireEvent.change(size(), { target: { value: '' } })
    fireEvent.blur(size())
    expect(size()).toHaveValue(2048)
  })

  it('保存切分参数后**不关弹窗**，停在切块栏并提示需要重新摄入', async () => {
    const user = await openChunking()

    fireEvent.pointerDown(screen.getByRole('slider', { name: '块长（字符）' }))
    fireEvent.change(screen.getByRole('slider', { name: '块长（字符）' }), {
      target: { value: '1024' },
    })
    await user.click(screen.getByRole('button', { name: '保存并关闭' }))

    await waitFor(() => expect(updateMock).toHaveBeenCalledWith('kb-1', { chunk_size: 1024 }))
    expect(successToast).toHaveBeenCalledWith('切分参数已保存')
    // 弹窗还在（存完停在这一栏）。
    // **那两句"改动只对之后上传的文档生效"已删**（2026-09-24 用户反馈）：
    // 「重新摄入全部文档」那颗按钮就是"已有文档怎么办"的答案，它必须还在，
    // 而且这一栏保存过之后它要换成加重态（`kb-callout-strong`）——
    // 用例外观只剩这一处提示了，所以这里改为断言它。
    expect(screen.getByRole('dialog', { name: '知识库设置' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重新摄入全部文档/ })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /重新摄入全部文档/ }).closest('.kb-callout'),
    ).toHaveClass('kb-callout-strong')
  })

  it('「重新摄入全部文档」走 all=true，由服务端解析全集', async () => {
    batchMock.mockResolvedValue({
      action: 'reprocess',
      succeeded: 5,
      failed: 0,
      items: [],
    })
    const user = await openChunking()

    await user.click(screen.getByRole('button', { name: /重新摄入全部文档/ }))
    expect(await screen.findByText(/把「产品手册」里的文档全部重新解析/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始重新摄入' }))

    await waitFor(() => expect(batchMock).toHaveBeenCalledWith('kb-1', 'reprocess', [], null, true))
    expect(successToast).toHaveBeenCalledWith('已把 5 篇文档排入重新摄入队列')
    expect(onChanged).toHaveBeenCalledWith('sources')
  })
})

describe('分段出题与回答要求', () => {
  it('出题四项的初值来自这个库，没动过就不发这一组字段', async () => {
    const user = await openSettings(
      makeKB({
        suggested_enabled: true,
        suggested_count: 4,
        suggested_model_pk: 'm-1',
        suggested_prompt: '每行一个问题',
      }),
    )
    await user.click(screen.getByRole('button', { name: '切块策略' }))

    expect(screen.getByLabelText('为每个切块生成推荐问题')).toBeChecked()
    expect(screen.getByLabelText('自定义出题提示词')).toHaveValue('每行一个问题')

    // 只改条数：那一组四个值一起提交（后端也是一次写四个）
    fireEvent.change(screen.getByRole('slider', { name: '每个切块生成几条问题' }), {
      target: { value: '5' },
    })
    const save = screen.getByRole('button', { name: '保存并关闭' })
    await waitFor(() => expect(save).toBeEnabled())
    await user.click(save)

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1))
    const patch = updateMock.mock.calls[0][1]
    expect(patch.suggested_enabled).toBe(true)
    expect(patch.suggested_count).toBe(5)
    expect(patch.suggested_model_pk).toBe('m-1')
    expect(patch.suggested_prompt).toBe('每行一个问题')
    expect('chunk_size' in patch).toBe(false)
  })

  it('按摘要生成：草稿填进编辑框，并把依据了哪几篇列出来；写文件名的口径会被点名', async () => {
    generateMock.mockResolvedValue({
      prompt: '请按编号式引用作答',
      sources: [{ document_id: 'doc-1', name: '说明书.pdf', summary: '讲了安装' }],
      filename_style_citations: ['[来源: 文件名]'],
    })
    const user = await openSettings()

    await user.click(screen.getByRole('button', { name: '回答要求' }))
    await user.click(screen.getByRole('button', { name: /按文档摘要生成/ }))

    await waitFor(() => expect(generateMock).toHaveBeenCalledWith('kb-1', null))
    expect(await screen.findByLabelText('库级提示词')).toHaveValue('请按编号式引用作答')
    expect(screen.getByText('这次生成依据了 1 篇摘要：')).toBeInTheDocument()
    expect(screen.getByText('说明书.pdf')).toBeInTheDocument()
    expect(screen.getByText(/还在要求把文件名写进正文/)).toBeInTheDocument()
  })

  it('关掉再打开：上一次生成的溯源不会留着（否则框与溯源对不上）', async () => {
    generateMock.mockResolvedValue({
      prompt: '草稿',
      sources: [{ document_id: 'doc-1', name: '说明书.pdf', summary: '' }],
      filename_style_citations: [],
    })
    const user = await openSettings()

    await user.click(screen.getByRole('button', { name: '回答要求' }))
    await user.click(screen.getByRole('button', { name: /按文档摘要生成/ }))
    expect(await screen.findByText(/这次生成依据了/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: '产品手册 的设置' }))
    await user.click(screen.getByRole('button', { name: '回答要求' }))

    expect(screen.queryByText(/这次生成依据了/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('库级提示词')).toHaveValue('')
  })
})

describe('删除知识库', () => {
  it('删除前先取影响清单（不可恢复的库要把"会失去什么"摆出来）', async () => {
    impactMock.mockResolvedValue({
      kind: 'knowledge_base',
      id: 'kb-1',
      name: '产品手册',
      documents: 5,
      chunks: 412,
      parts: 3,
      size_bytes: 2048,
      running_tasks: 0,
      document_names: [],
      restorable: false,
    })
    deleteMock.mockResolvedValue({
      kind: 'knowledge_base',
      id: 'kb-1',
      name: '产品手册',
      documents: 5,
      chunks: 412,
      parts: 3,
      size_bytes: 2048,
      running_tasks: 0,
      document_names: [],
      restorable: false,
    })
    const user = await openSettings()

    // 左侧导航里也有一项叫「删除知识库」，所以按面板范围取那颗入口
    await user.click(screen.getByRole('button', { name: '删除知识库' }))
    const pane = document.querySelector('.kb-pane') as HTMLElement
    await user.click(within(pane).getByRole('button', { name: '删除知识库' }))

    expect(await screen.findByText('412')).toBeInTheDocument()
    expect(screen.getByText('2.0 KB')).toBeInTheDocument()
    expect(impactMock).toHaveBeenCalledWith('kb-1')

    // 弹窗里那一颗确认按钮（同名按钮有两颗：面板里的入口 + 弹窗里的确认）
    // 确认弹窗是 `@/ui/alert-dialog`（Radix）：role 是 alertdialog，不是 dialog
    const dialog = screen.getByRole('alertdialog', { name: '删除知识库' })
    await user.click(within(dialog).getAllByRole('button', { name: /删除知识库/ })[0])

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith('kb-1'))
    expect(onChanged).toHaveBeenCalledWith('deleted')
    expect(successToast).toHaveBeenCalledWith('已删除知识库「产品手册」')
  })

  it('名称清空时给一句中文原因，而不是静默失败', async () => {
    const user = await openSettings()

    await user.clear(screen.getByLabelText('知识库名称'))
    await user.click(screen.getByRole('button', { name: '保存并关闭' }))

    expect(errorToast).toHaveBeenCalledWith('知识库名称不能为空')
    expect(updateMock).not.toHaveBeenCalled()
  })
})
