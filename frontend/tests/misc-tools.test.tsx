/**
 * 工具页（`features/misc/tools/ToolsPage`）的用例。
 *
 * 覆盖五件"丢了就误导人"的事：
 *
 * 1. **一条工具都不写死**：页面上出现哪些行、标签与参数长什么样，全由 `GET /tools` 决定
 *    （用例摆一份三条工具的目录，页面就该出三行）；
 * 2. **MCP 地址没配时照实说没配**，不是留白、也不是拼一个地址；
 * 3. **工具面指纹**：整串显示、一键复制——接入方拿它跟自己对的那一版核；
 * 4. **权限与参数两列**：权限按 `requires_write` 分档；参数列名字 + 必填标记，
 *    参数说明只在非空时挂 `title`；
 * 5. **与 API 密钥的关系**：那一句链到 `/api-keys`（按工具收窄范围的地方）。
 */
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/tools', () => ({ getToolCatalogue: vi.fn() }))

vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn(async () => true) }))

import { getToolCatalogue, type ToolCatalog } from '@/api/tools'
import { ToolsPage } from '@/features/misc/tools/ToolsPage'
import { renderMisc } from '@/features/misc/testing/harness'
import { copyText } from '@/lib/clipboard'

const getToolCatalogueMock = vi.mocked(getToolCatalogue)
const copyTextMock = vi.mocked(copyText)

/** 工具面的指纹：12 位十六进制，工具面一变它就变（后端 `surface_fingerprint()`）。 */
const FINGERPRINT = '7d10b9d1586e'

function toolCatalog(overrides: Partial<ToolCatalog> = {}): ToolCatalog {
  return {
    items: [
      {
        name: 'search',
        label: '检索',
        summary: '按库检索原文段落',
        requires_write: false,
        params: [
          { name: 'query', required: true, summary: '要检索的问题' },
          { name: 'knowledge_base_ids', required: false, summary: '' },
        ],
      },
      {
        name: 'get_document_content',
        label: '取原文',
        summary: '取一篇文档的正文',
        requires_write: false,
        params: [],
      },
      {
        name: 'delete_document',
        label: '删除文档',
        summary: '删掉一篇文档',
        requires_write: true,
        params: [{ name: 'document_id', required: true, summary: '' }],
      },
    ],
    mcp_endpoint: 'http://nas:8000/mcp',
    surface_fingerprint: FINGERPRINT,
    skill: {
      name: 'kylab-knowledge-base',
      summary: '在你的知识库里检索原文段落',
      path: 'skills/kylab-knowledge-base',
      transports: ['mcp', 'rest'],
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  getToolCatalogueMock.mockResolvedValue(toolCatalog())
})

describe('接入方式', () => {
  it('MCP 地址与那份技能（名字 / 两条通路 / 路径 / 摘要）都在', async () => {
    renderMisc(<ToolsPage />)

    expect(await screen.findByText('http://nas:8000/mcp')).toBeInTheDocument()
    expect(screen.getByText('kylab-knowledge-base')).toBeInTheDocument()
    // 通路上界面是大写那一档（后端给的是 mcp / rest）
    expect(screen.getByText('MCP')).toBeInTheDocument()
    expect(screen.getByText('REST')).toBeInTheDocument()
    expect(screen.getByText('skills/kylab-knowledge-base')).toBeInTheDocument()
  })

  it('没配 MCP 地址时照实说没配', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ mcp_endpoint: null }))

    renderMisc(<ToolsPage />)

    expect(await screen.findByText('这一档没有配 MCP 地址。')).toBeInTheDocument()
  })

  it('工具面指纹：整串显示 + 一键复制（接入方拿它对契约那一版）', async () => {
    const user = userEvent.setup()
    renderMisc(<ToolsPage />)

    expect(await screen.findByText(FINGERPRINT)).toBeInTheDocument()
    // 那一行说明它什么时候会变（短标签，不是对页面的解释）
    expect(screen.getByText('工具面一变此值就变。')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '复制工具面指纹' }))

    expect(copyTextMock).toHaveBeenCalledWith(FINGERPRINT)
  })
})

describe('对外工具', () => {
  it('一行一个工具：标签 + 技术标识 + 摘要 + 权限分两档 + 无参数照实写', async () => {
    renderMisc(<ToolsPage />)

    // 三把工具都出来了（数量由目录决定，页面不写死）
    expect(await screen.findByText('检索')).toBeInTheDocument()
    expect(screen.getByText('取原文')).toBeInTheDocument()
    expect(screen.getByText('删除文档')).toBeInTheDocument()
    // 技术标识跟在标签旁边（同一个工具格里）
    expect(screen.getByText('search')).toBeInTheDocument()
    expect(screen.getByText('get_document_content')).toBeInTheDocument()
    // 摘要
    expect(screen.getByText('按库检索原文段落')).toBeInTheDocument()
    // 权限两档：两把只读、一把需要读写
    expect(screen.getAllByText('只读').length).toBe(2)
    expect(screen.getByText('需要读写')).toBeInTheDocument()
    // 没有参数的工具那一格照实写「无参数」
    expect(screen.getByText('无参数')).toBeInTheDocument()
  })

  it('参数列名字与必填标记；参数说明只在非空时挂 title', async () => {
    renderMisc(<ToolsPage />)

    expect(await screen.findByText('query')).toBeInTheDocument()
    expect(screen.getByText('knowledge_base_ids')).toBeInTheDocument()
    expect(screen.getByText('document_id')).toBeInTheDocument()
    // 两个必填参数（query / document_id）都带标记
    expect(screen.getAllByText('必填').length).toBe(2)
    // 非空说明进了 title（鼠标移上去能读到），空说明不挂
    expect(screen.getByText('query')).toHaveAttribute('title', '要检索的问题')
    expect(screen.getByText('knowledge_base_ids')).not.toHaveAttribute('title')
  })

  it('空目录给空态', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ items: [] }))

    renderMisc(<ToolsPage />)

    expect(await screen.findByText('还没有工具')).toBeInTheDocument()
  })

  it('读不到目录给错误行（不是空白页）', async () => {
    getToolCatalogueMock.mockRejectedValue(new Error('读不到工具清单'))

    renderMisc(<ToolsPage />)

    expect(await screen.findByText('读不到工具清单')).toBeInTheDocument()
  })
})

describe('与 API 密钥的关系', () => {
  it('给一条去 API 密钥页的路（按工具收窄范围在那儿勾）', async () => {
    renderMisc(<ToolsPage />)

    const link = await screen.findByRole('link', { name: 'API 密钥' })
    expect(link).toHaveAttribute('href', '/api-keys')
  })
})
