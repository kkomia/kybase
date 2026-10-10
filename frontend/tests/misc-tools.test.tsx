/**
 * 接入页（`features/misc/tools/ToolsPage`）的用例。
 *
 * 这一页 2026-10-10 从「工具」改成「接入」（路由没动），分两档：默认的「接入」是主体
 * （服务地址 / 鉴权 / 客户端配置片段 / 三步验证 / 排障），「工具」档是原来那一块工具面。
 * 覆盖这几件"丢了就接不上、或者接了也不知道对不对"的事：
 *
 * 1. **两档各有什么**：默认落在「接入」上；「工具」档要切过去才渲染（Radix 不挂载未选中的档）；
 * 2. **服务地址那一格**：服务端给的整条地址就是它的默认值；**服务端没给（`null`）时它空着
 *    但可填**——填了之后下面所有片段立刻用这个值、本机记住它（重新挂载还在）、服务端给了
 *    值时本机那条不生效且有「用服务端的值」复位。不合法（缺协议等）就不给片段、只就地提示；
 * 3. **片段填的是真数据**：每段片段里的地址就是这一格当前的值，不是写死的例子；
 * 4. **建了密钥就地换明文**：没建时片段里是占位，建完就是明文，且明文只此一次
 *    （**落盘的只有地址那一条，明文一个字节都不进 localStorage**）；
 * 5. **非管理员不给新建入口**（新建端点只认管理员），但地址与片段照给；
 * 6. **复制成败两条路**：成功推「已复制」，失败推「复制失败」那一句；
 * 7. **工具档一件没少**：指纹可复制、技能与工具清单照旧、链到 API 密钥页。
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/tools', () => ({ getToolCatalogue: vi.fn() }))

vi.mock('@/api/apiKeys', () => ({ createApiKey: vi.fn() }))

vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn(async () => true) }))

import { createApiKey, type ApiKey } from '@/api/apiKeys'
import { getToolCatalogue, type ToolCatalog } from '@/api/tools'
import { resetToasts } from '@/features/misc/shared/toast'
import { renderMisc } from '@/features/misc/testing/harness'
import { ToolsPage } from '@/features/misc/tools/ToolsPage'
import { copyText } from '@/lib/clipboard'
import { useSessionStore } from '@/lib/session'

const getToolCatalogueMock = vi.mocked(getToolCatalogue)
const createApiKeyMock = vi.mocked(createApiKey)
const copyTextMock = vi.mocked(copyText)

/** 工具面的指纹：12 位十六进制，工具面一变它就变（后端 `surface_fingerprint()`）。 */
const FINGERPRINT = '7d10b9d1586e'

/** 这一档拿到的服务地址（服务端给值时所有片段都该填这一个）。 */
const ENDPOINT = 'http://nas:8000/mcp'

/** 服务端没给地址时，用户自己填的那一条（用例里的第二条例，与上面的端口本就无关）。 */
const TYPED = 'http://nas:8765/mcp'

/** 本机记住地址的那个键（与 `tools/endpoint.ts` 同一个，用例按它核对"记住了没"）。 */
const ENDPOINT_STORAGE_KEY = 'kylab-mcp-endpoint'

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
    mcp_endpoint: ENDPOINT,
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

function apiKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: 'key-1',
    name: '接入密钥 2026-10-10',
    permission: 'readonly',
    knowledge_base_ids: [],
    allowed_tool_names: [],
    created_at: '2026-10-10T02:00:00Z',
    last_used_at: null,
    prefix: 'kylab_sk_ab12…',
    ...overrides,
  }
}

function asAdmin(): void {
  useSessionStore.setState({
    token: 'st',
    currentUser: { id: 'u1', username: 'admin', name: '管理员', role: 'admin', avatar_url: '' },
  })
}

function asMember(): void {
  useSessionStore.setState({
    token: 'st',
    currentUser: { id: 'u2', username: 'bob', name: '小博', role: 'member', avatar_url: '' },
  })
}

/** 页面上所有代码块的文本：片段是整段的，断言必须落到整段上（不是某一行）。 */
function snippetTexts(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('pre.m-code-block')).map(
    (node) => node.textContent ?? '',
  )
}

/** 「MCP 地址」那一格。 */
async function addressField(): Promise<HTMLElement> {
  return screen.findByRole('textbox', { name: 'MCP 地址' })
}

/** 片段里有没有出现这条地址（三个片段形状各一处，取任一处命中即可）。 */
function snippetsUse(container: HTMLElement, endpoint: string): boolean {
  return snippetTexts(container).some((text) => text.includes(endpoint))
}

/** localStorage 里现存的那些值（用来核对"只落了地址、没落明文密钥"）。 */
function storedValues(): string[] {
  return Array.from({ length: window.localStorage.length }, (_, index) =>
    window.localStorage.getItem(window.localStorage.key(index) ?? ''),
  ).filter((value): value is string => value !== null)
}

/** 切到「工具」档（默认档是「接入」）。 */
async function openToolsTab(): Promise<void> {
  await userEvent.click(await screen.findByRole('tab', { name: '工具' }))
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  useSessionStore.setState({ token: '', currentUser: null, authStatus: null, reloginCount: 0 })
  asAdmin()
  getToolCatalogueMock.mockResolvedValue(toolCatalog())
  copyTextMock.mockResolvedValue(true)
  createApiKeyMock.mockResolvedValue({
    ...apiKey(),
    token: 'kylab_sk_plaintext-only-once',
  })
})

describe('分档', () => {
  it('默认落在「接入」档：地址、鉴权、片段、验证、排障都在，工具清单不在', async () => {
    renderMisc(<ToolsPage />)

    expect(await screen.findByRole('tab', { name: '接入' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '工具' })).toBeInTheDocument()
    expect(screen.getByText('服务地址')).toBeInTheDocument()
    expect(screen.getByText('鉴权')).toBeInTheDocument()
    expect(screen.getByText('客户端配置片段')).toBeInTheDocument()
    expect(screen.getByText('三步验证')).toBeInTheDocument()
    expect(screen.getByText('排障')).toBeInTheDocument()
    // 工具清单在另一档，没选中的档**不挂载**
    expect(screen.queryByText('对外工具')).not.toBeInTheDocument()
  })
})

describe('服务地址与鉴权', () => {
  it('地址来自 GET /tools，就是那一格的默认值，可复制', async () => {
    renderMisc(<ToolsPage />)

    expect(await screen.findByDisplayValue(ENDPOINT)).toBeInTheDocument()
    // 请求头写的是 Bearer（不是别的鉴权方式）
    expect(screen.getByText('Authorization: Bearer <密钥>')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '复制服务地址' }))
    expect(copyTextMock).toHaveBeenCalledWith(ENDPOINT)
  })

  it('服务端没给地址：这一格空着、给一次填的机会，片段退回空态（不摆带假地址的模板）', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ mcp_endpoint: null }))

    const { container } = renderMisc(<ToolsPage />)

    const field = await addressField()
    expect(field).toHaveValue('')
    expect(field).toHaveAttribute('placeholder', 'http://<你的主机>:8765/mcp')
    expect(screen.getByText('服务端没给地址，你自己填一次。')).toBeInTheDocument()
    // 空态说的是"填一次就有片段"，不是"服务端没配所以没有"
    expect(screen.getByText('填一次地址就能拿到片段')).toBeInTheDocument()
    expect(screen.getByText('地址填在上面「服务地址」那一格。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '复制服务地址' })).not.toBeInTheDocument()
    expect(snippetTexts(container)).toHaveLength(0)
  })

  it('给一条去 API 密钥页的路（范围与撤销在那儿管）', async () => {
    renderMisc(<ToolsPage />)

    const link = await screen.findByRole('link', { name: 'API 密钥' })
    expect(link).toHaveAttribute('href', '/api-keys')
  })
})

describe('自己填一次服务地址', () => {
  it('服务端没给时：填进去，下面所有片段立刻用这个值', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ mcp_endpoint: null }))

    const { container } = renderMisc(<ToolsPage />)
    const field = await addressField()

    await userEvent.type(field, TYPED)

    expect(field).toHaveValue(TYPED)
    // 填上之后：提示与空态都让位给片段
    expect(screen.queryByText('服务端没给地址，你自己填一次。')).not.toBeInTheDocument()
    expect(screen.queryByText('填一次地址就能拿到片段')).not.toBeInTheDocument()
    expect(snippetsUse(container, `url = "${TYPED}"`)).toBe(true)

    // 别的档也跟着换（未选中的档不挂载，所以逐档切过去看）
    await userEvent.click(screen.getByRole('tab', { name: 'Cursor' }))
    expect(snippetsUse(container, `"url": "${TYPED}"`)).toBe(true)

    // 「通用表单」那一档的「服务器 URL」同样是这个值
    await userEvent.click(screen.getByRole('tab', { name: '通用表单' }))
    expect(screen.getByText(TYPED)).toBeInTheDocument()
  })

  it('填过的值被记住：重新挂载后仍在，片段也照它给', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ mcp_endpoint: null }))

    const first = renderMisc(<ToolsPage />)
    await userEvent.type(await addressField(), TYPED)
    expect(window.localStorage.getItem(ENDPOINT_STORAGE_KEY)).toBe(TYPED)
    first.unmount()

    const second = renderMisc(<ToolsPage />)

    expect(await screen.findByDisplayValue(TYPED)).toBeInTheDocument()
    expect(snippetsUse(second.container, `url = "${TYPED}"`)).toBe(true)
  })

  it('服务端给了值：默认用它，可以就地改，也可以复位回去', async () => {
    const { container } = renderMisc(<ToolsPage />)

    const field = await screen.findByDisplayValue(ENDPOINT)
    // 与服务端一致时不留复位键（没东西可复位）
    expect(screen.queryByRole('button', { name: '用服务端的值' })).not.toBeInTheDocument()

    await userEvent.clear(field)
    await userEvent.type(field, TYPED)
    expect(snippetsUse(container, `url = "${TYPED}"`)).toBe(true)

    await userEvent.click(screen.getByRole('button', { name: '用服务端的值' }))
    expect(field).toHaveValue(ENDPOINT)
    expect(snippetsUse(container, `url = "${ENDPOINT}"`)).toBe(true)
    expect(screen.queryByRole('button', { name: '用服务端的值' })).not.toBeInTheDocument()
    // 复位顺手忘掉本机那条覆盖值：下次打开这一格还是服务端给的
    expect(window.localStorage.getItem(ENDPOINT_STORAGE_KEY)).toBeNull()
  })

  it('服务端给了值就优先用它（本机记住的那条只是服务端没给时的底）', async () => {
    window.localStorage.setItem(ENDPOINT_STORAGE_KEY, TYPED)

    renderMisc(<ToolsPage />)

    expect(await screen.findByDisplayValue(ENDPOINT)).toBeInTheDocument()
    expect(screen.queryByDisplayValue(TYPED)).not.toBeInTheDocument()
  })
})

describe('地址不合法', () => {
  it('缺协议之类的值：就地提示 + 不给片段，不弹窗、不推通知', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ mcp_endpoint: null }))

    const { container } = renderMisc(<ToolsPage />)
    const field = await addressField()

    await userEvent.type(field, 'nas:8765/mcp')

    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('填完整地址：以 http 或 https 开头。')).toBeInTheDocument()
    expect(screen.getByText('填一次地址就能拿到片段')).toBeInTheDocument()
    expect(snippetTexts(container)).toHaveLength(0)
    expect(screen.queryByRole('button', { name: '复制服务地址' })).not.toBeInTheDocument()
    expect(screen.queryByText('已复制')).not.toBeInTheDocument()
    expect(screen.queryByText('复制失败，请手动选中复制')).not.toBeInTheDocument()

    // 改成一条完整的地址：错误行消失，片段立刻回来
    await userEvent.clear(field)
    await userEvent.type(field, TYPED)
    expect(screen.queryByText('填完整地址：以 http 或 https 开头。')).not.toBeInTheDocument()
    expect(snippetsUse(container, `url = "${TYPED}"`)).toBe(true)
  })

  it('服务端给了值时改坏了：同样不给片段，复位键可以回到能用的那条', async () => {
    const { container } = renderMisc(<ToolsPage />)

    const field = await screen.findByDisplayValue(ENDPOINT)
    await userEvent.clear(field)
    await userEvent.type(field, 'ftp://nas/mcp')

    expect(snippetTexts(container)).toHaveLength(0)
    expect(screen.getByText('填一次地址就能拿到片段')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '用服务端的值' }))
    expect(snippetsUse(container, `url = "${ENDPOINT}"`)).toBe(true)
  })
})

describe('新建接入用密钥', () => {
  it('一键建一把只读档的密钥：明文就地出现一次，片段里的占位换成明文', async () => {
    const { container } = renderMisc(<ToolsPage />)

    await screen.findByDisplayValue(ENDPOINT)
    // 还没建密钥：片段里是占位
    expect(snippetTexts(container).some((text) => text.includes('kylab_sk_<你的 Key>'))).toBe(true)

    await userEvent.click(screen.getByRole('button', { name: '新建接入用密钥' }))

    await waitFor(() => expect(createApiKeyMock).toHaveBeenCalled())
    const payload = createApiKeyMock.mock.calls[0][0]
    expect(payload.permission).toBe('readonly')
    expect(payload.name).toMatch(/^接入密钥 \d{4}-\d{2}-\d{2}$/)

    // 明文只此一次：就地摊出来、能复制、文案照实说离开就没了
    expect(await screen.findByDisplayValue('kylab_sk_plaintext-only-once')).toBeInTheDocument()
    expect(screen.getByText(/离开这张卡片就看不到了/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '复制明文密钥' }))
    expect(copyTextMock).toHaveBeenCalledWith('kylab_sk_plaintext-only-once')

    // 片段跟着换成明文，占位一处不剩
    const texts = snippetTexts(container)
    expect(texts.some((text) => text.includes('Bearer kylab_sk_plaintext-only-once'))).toBe(true)
    expect(texts.some((text) => text.includes('kylab_sk_<你的 Key>'))).toBe(false)

    // 落盘的只有地址：明文一个字节都没进 localStorage
    expect(window.localStorage.getItem(ENDPOINT_STORAGE_KEY)).toBeNull()
    expect(storedValues().some((value) => value.includes('kylab_sk_'))).toBe(false)

    // 「我已保存」收起明文（界面上不再有它）
    await userEvent.click(screen.getByRole('button', { name: '我已保存' }))
    expect(screen.queryByDisplayValue('kylab_sk_plaintext-only-once')).not.toBeInTheDocument()
  })

  it('成员账号：给地址与片段，但不给新建入口（新建端点只认管理员）', async () => {
    asMember()

    const { container } = renderMisc(<ToolsPage />)

    expect(await screen.findByDisplayValue(ENDPOINT)).toBeInTheDocument()
    expect(screen.getByText(/找管理员要一把/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建接入用密钥' })).not.toBeInTheDocument()
    // 地址与片段照给：别人给他一把密钥，他照样接得上
    expect(snippetTexts(container).length).toBeGreaterThan(0)
    expect(createApiKeyMock).not.toHaveBeenCalled()
  })
})

describe('客户端配置片段', () => {
  it('Codex 档：两段 TOML 都填真地址，另给写环境变量的两条命令', async () => {
    const { container } = renderMisc(<ToolsPage />)

    await screen.findByDisplayValue(ENDPOINT)
    const texts = snippetTexts(container)

    expect(texts.some((text) => text.includes(`url = "${ENDPOINT}"`))).toBe(true)
    expect(texts.some((text) => text.includes('bearer_token_env_var = "KYBASE_MCP_KEY"'))).toBe(
      true,
    )
    expect(texts.some((text) => text.includes('http_headers = { "Authorization"'))).toBe(true)
    expect(texts.some((text) => text.includes('setx KYBASE_MCP_KEY'))).toBe(true)
    // 每段片段各有各的复制按钮（一屏好几段，按钮不能都叫「复制」）
    expect(screen.getByRole('button', { name: '复制凭据走环境变量' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '复制凭据直接写进配置' })).toBeInTheDocument()
  })

  it('Cursor 档：JSON mcpServers，地址与 Authorization 头都在', async () => {
    const { container } = renderMisc(<ToolsPage />)

    await userEvent.click(await screen.findByRole('tab', { name: 'Cursor' }))
    const texts = snippetTexts(container)

    expect(texts.some((text) => text.includes('"mcpServers"'))).toBe(true)
    expect(texts.some((text) => text.includes(`"url": "${ENDPOINT}"`))).toBe(true)
    expect(texts.some((text) => text.includes('"Authorization": "Bearer '))).toBe(true)
    expect(screen.getByText('~/.cursor/mcp.json')).toBeInTheDocument()
  })

  it('通用表单档：逐字段给「客户端标识符 / 显示名称 / 传输方式 / URL / 环境变量 / Headers」，每格可复制', async () => {
    renderMisc(<ToolsPage />)

    await userEvent.click(await screen.findByRole('tab', { name: '通用表单' }))

    expect(screen.getByText('客户端标识符')).toBeInTheDocument()
    expect(screen.getByText('kybase')).toBeInTheDocument()
    expect(screen.getByText('KYLAB 知识库')).toBeInTheDocument()
    expect(screen.getByText('Streamable HTTP')).toBeInTheDocument()
    // 地址在这一档是「服务器 URL」那一格的值（上面那条在输入框里，不是文字）
    expect(screen.getByText(ENDPOINT)).toBeInTheDocument()
    expect(screen.getByText('Bearer token env var')).toBeInTheDocument()
    expect(screen.getByText('KYBASE_MCP_KEY')).toBeInTheDocument()
    expect(screen.getByText('Authorization: Bearer kylab_sk_<你的 Key>')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '复制服务器 URL' }))
    expect(copyTextMock).toHaveBeenCalledWith(ENDPOINT)
  })

  it('环境变量档：Authorization 头从环境变量取（配置里不落明文）', async () => {
    const { container } = renderMisc(<ToolsPage />)

    await userEvent.click(await screen.findByRole('tab', { name: '环境变量' }))
    const texts = snippetTexts(container)

    expect(texts.some((text) => text.includes('Bearer ${KYBASE_MCP_KEY}'))).toBe(true)
    // 这一档的片段里一个密钥都不该出现（明文只走环境变量）
    expect(texts.some((text) => text.includes('Bearer kylab_sk_'))).toBe(false)
  })
})

describe('复制', () => {
  it('成功推「已复制」', async () => {
    copyTextMock.mockResolvedValue(true)

    renderMisc(<ToolsPage />)
    await screen.findByDisplayValue(ENDPOINT)
    await userEvent.click(screen.getByRole('button', { name: '复制服务地址' }))

    expect(await screen.findByText('已复制')).toBeInTheDocument()
  })

  it('失败推「复制失败」那一句（别让人以为复制上了）', async () => {
    copyTextMock.mockResolvedValue(false)

    renderMisc(<ToolsPage />)
    await screen.findByDisplayValue(ENDPOINT)
    await userEvent.click(screen.getByRole('button', { name: '复制服务地址' }))

    expect(await screen.findByText('复制失败，请手动选中复制')).toBeInTheDocument()
    expect(screen.queryByText('已复制')).not.toBeInTheDocument()
  })
})

describe('三步验证与排障', () => {
  it('三步各写一句"成功长什么样"，排障按现象给去处', async () => {
    renderMisc(<ToolsPage />)

    expect(await screen.findByText('把片段贴进客户端')).toBeInTheDocument()
    expect(screen.getByText('重启客户端或重连')).toBeInTheDocument()
    expect(screen.getByText('让它列一次库，或问一句')).toBeInTheDocument()
    expect(screen.getByText(/成功：列出你的库名/)).toBeInTheDocument()

    expect(screen.getByText('调用被拒 / 401')).toBeInTheDocument()
    expect(screen.getByText('工具比文档少')).toBeInTheDocument()
    expect(screen.getByText(/库范围或工具范围挡住了/)).toBeInTheDocument()
  })
})

describe('工具档', () => {
  it('工具面：指纹可复制 + 那份技能（名字 / 两条通路 / 路径 / 摘要）', async () => {
    renderMisc(<ToolsPage />)
    await openToolsTab()

    expect(screen.getByText(FINGERPRINT)).toBeInTheDocument()
    expect(screen.getByText('工具面一变此值就变。')).toBeInTheDocument()
    expect(screen.getByText('kylab-knowledge-base')).toBeInTheDocument()
    // 通路上界面是大写那一档（后端给的是 mcp / rest）
    expect(screen.getByText('MCP')).toBeInTheDocument()
    expect(screen.getByText('REST')).toBeInTheDocument()
    expect(screen.getByText('skills/kylab-knowledge-base')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '复制工具面指纹' }))
    expect(copyTextMock).toHaveBeenCalledWith(FINGERPRINT)
  })

  it('一行一个工具：标签 + 技术标识 + 摘要 + 权限分两档 + 无参数照实写', async () => {
    renderMisc(<ToolsPage />)
    await openToolsTab()

    expect(screen.getByText('检索')).toBeInTheDocument()
    expect(screen.getByText('取原文')).toBeInTheDocument()
    expect(screen.getByText('删除文档')).toBeInTheDocument()
    expect(screen.getByText('search')).toBeInTheDocument()
    expect(screen.getByText('get_document_content')).toBeInTheDocument()
    expect(screen.getByText('按库检索原文段落')).toBeInTheDocument()
    expect(screen.getAllByText('只读').length).toBe(2)
    expect(screen.getByText('需要读写')).toBeInTheDocument()
    expect(screen.getByText('无参数')).toBeInTheDocument()
  })

  it('参数列名字与必填标记；参数说明只在非空时挂 title', async () => {
    renderMisc(<ToolsPage />)
    await openToolsTab()

    expect(screen.getByText('query')).toBeInTheDocument()
    expect(screen.getByText('knowledge_base_ids')).toBeInTheDocument()
    expect(screen.getByText('document_id')).toBeInTheDocument()
    expect(screen.getAllByText('必填').length).toBe(2)
    expect(screen.getByText('query')).toHaveAttribute('title', '要检索的问题')
    expect(screen.getByText('knowledge_base_ids')).not.toHaveAttribute('title')
  })

  it('空目录给空态', async () => {
    getToolCatalogueMock.mockResolvedValue(toolCatalog({ items: [] }))

    renderMisc(<ToolsPage />)
    await openToolsTab()

    expect(screen.getByText('还没有工具')).toBeInTheDocument()
  })

  it('按工具收窄范围那一句仍在，链到 API 密钥页', async () => {
    renderMisc(<ToolsPage />)
    await openToolsTab()

    const link = within(screen.getByText(/一把密钥可以只开其中一部分工具/)).getByRole('link', {
      name: 'API 密钥',
    })
    expect(link).toHaveAttribute('href', '/api-keys')
  })
})

describe('读不到目录', () => {
  it('给错误行（不是空白页）', async () => {
    getToolCatalogueMock.mockRejectedValue(new Error('读不到工具清单'))

    renderMisc(<ToolsPage />)

    expect(await screen.findByText('读不到工具清单')).toBeInTheDocument()
  })
})
