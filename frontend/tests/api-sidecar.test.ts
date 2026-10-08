/**
 * 对话轮次的基址分派（P4 第 2 片）—— 三组断言，对应派单的三条验收。
 *
 * **2026-10-05 改过一轮**（NAS 网页端退役）：这一族端点服务器档不再挂
 * （`/chat/stream` 与 `/chat/approvals/{id}` 两档都没有了），所以"边车打不到就回退
 * 服务器"那几条断言换成了"**打不到就显式失败，而且一次都不打服务器**"——
 * 见下面那几条的说明（`TurnUnavailableError`）。它要防的正是那种最坏的样子：
 * 请求发到一个谁也不服务的 URL 上，界面上只多一条 404。
 *
 * **同一天紧接着又改了一处**：`VITE_SIDECAR_TURNS` 那个开关退役（它原先决定"打边车还是
 * 回服务器"，而回退那条路已经没了 ⇒ 没有可切换的两端）。所以"开关本身"那一组断言删掉了，
 * 留下的是"**落点只有一个、打不到就抛**"这件事的四条（默认 / 探不到 / 探过了 / 还没探）。
 *
 * 原则与其它 api-*.test.ts 一致：**不打真网络**（`fetch` 全程被替身接管）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { API_BASE } from '@/api/client'
import {
  DEFAULT_SIDECAR_BASE,
  SIDECAR_STREAM_PATH,
  TurnUnavailableError,
  baseForPath,
  isSidecarPath,
  resetSidecarProbe,
  resolveApprovalTarget,
  resolveTurnTarget,
  sidecarAvailable,
  sidecarBase,
  SIDECAR_APPROVAL_PATH,
  sidecarStatus,
  toSidecarTurnBody,
} from '@/api/sidecar'

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

/** 打出去的**全部** URL（用来钉"一条都没落到服务器那条链上"）。 */
function urlsOf(mock: unknown): string[] {
  return vi.mocked(mock as typeof fetch).mock.calls.map((call) => String(call[0]))
}

describe('边车分派：判定只有一处', () => {
  beforeEach(() => {
    resetSidecarProbe()
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('① 对话轮次归边车：基址与 URL 都指到边车', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    const target = await resolveTurnTarget()

    expect(target.base).toBe(DEFAULT_SIDECAR_BASE)
    expect(target.url).toBe(`${DEFAULT_SIDECAR_BASE}${SIDECAR_STREAM_PATH}`)
    // 探活打的是边车自己的 /health（不是服务器）
    expect(String(fetchMock.mock.calls[0][0])).toBe(`${DEFAULT_SIDECAR_BASE}/health`)
  })

  it('② 对话轮次那条链之外的，一律不归它（多一个都不行）', () => {
    // 归这条链的**只有**对话轮次这一个
    expect(isSidecarPath('/chat/stream')).toBe(true)
    expect(isSidecarPath('/chat/stream?x=1')).toBe(true)

    // 下面这些**一条都不许**归"对话轮次那条链"：要么是别的接口，
    // 要么归**本机权威面**（M2 阶段 4 的 `LOCAL_PATHS` —— 那是另一套判定，
    // 判据在 `tests/unit/api/sidecar.test.ts`：这几条如今直连**边车那台**的本机库）
    for (const path of [
      '/chat', // 非流式那条：响应形状不同，这一片不做
      '/chat/turns/conv_1/live', // 重连补发：边车没有会话事件日志
      '/chat/approvals/appr_1',
      '/conversations', // 本机权威面（会话在本机库里）
      '/auth/me',
      '/knowledge-bases',
      '/memory', // 本机权威面（记忆本体本来就在 data_dir/memory）
      '/settings', // 本机权威面（运行期配置表在本机库）
    ]) {
      expect(isSidecarPath(path), path).toBe(false)
      expect(baseForPath(path), path).toBe(API_BASE)
    }

    // 两个基址确实不是一个东西（否则这条用例什么也没证明）
    expect(baseForPath('/chat/stream')).toBe(sidecarBase())
    expect(sidecarBase()).not.toBe(API_BASE)
  })

  it('③ 边车不可用时**显式失败**，而且一次都不打服务器（回退已清掉）', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'))
    vi.stubGlobal('fetch', fetchMock)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    // 原先这里回退服务器 `/chat/stream`，而那条链 2026-10-05 起两档都没有了
    // （`backend/app/api/v1/router.py` 的模块头）：打过去只多一条 404，
    // 用户看到的是"对话坏了"，看不出真正的原因是"本机那个对话后端没跑起来"。
    const error = await resolveTurnTarget().catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(TurnUnavailableError)
    // 失败原因是**探测给的那句原话**（含 ECONNREFUSED），而且说清"没有别的落点"
    expect((error as Error).message).toContain('ECONNREFUSED')
    expect((error as Error).message).toContain('没有别的落点')
    // 状态位照样说得清（不许静默）：available=false + 一条 warn
    expect(sidecarStatus().available).toBe(false)
    expect(sidecarStatus().reason).toContain('边车不可达')
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('[sidecar]')
    // **一条都没落到服务器那条链上**：这条链上只该有探活那一次（`/health`）
    expect(urlsOf(fetchMock)).toEqual([`${DEFAULT_SIDECAR_BASE}/health`])
    expect(urlsOf(fetchMock).some((url) => url.includes('/chat/stream'))).toBe(false)

    // 探测结果**有缓存**：一秒内不再打了（否则每轮对话都先等一次超时）
    const calls = vi.mocked(fetchMock).mock.calls.length
    await sidecarAvailable()
    expect(vi.mocked(fetchMock).mock.calls.length).toBe(calls)
  })

  it('/health 返回非 2xx 也算不可用（并且原因如实带上状态码）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('nope', { status: 503 })))
    vi.spyOn(console, 'warn').mockImplementation(() => {})

    const error = await resolveTurnTarget().catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(TurnUnavailableError)
    expect((error as Error).message).toContain('503')
  })

  it('请求体只挑边车认识的字段：conversation_id 要带，mode/permission 不带', () => {
    // 多余的字段（mode / permission）用一个变量传进去 ——
    // 字面量直接传会触发 TS 的"多余属性"检查（那是故意的：这函数只挑它认识的字段）
    const serverPayload = {
      query: '读一下 hello.txt',
      kb_ids: ['kb_1'],
      conversation_id: 'conv_1',
      mode: 'plan',
      permission: 'workspace',
    }
    const body = toSidecarTurnBody(serverPayload)

    // `query` → `message` 的字段名适配就在这一处（服务器叫 query、边车叫 message）；
    // `conversation_id` **必须带上**：边车靠它写回服务器——不带这一轮刷新就没了（2026-09-30 实测的丢数据）。
    expect(body).toEqual({
      message: '读一下 hello.txt',
      kb_ids: ['kb_1'],
      conversation_id: 'conv_1',
    })
    // mode / permission 边车没有对应语义，仍然不塞（多发只会让人以为它们生效了）
    expect(body).not.toHaveProperty('mode')
    expect(body).not.toHaveProperty('permission')
    // 没有 kb_ids / conversation_id 时不发空值（边车按"没选知识库 / 不写回"处理）
    expect(toSidecarTurnBody({ query: '在吗' })).toEqual({ message: '在吗' })
  })

  it('历史随体带上，且只带最近 20 条（不带就是失忆的一轮）', () => {
    const history = Array.from({ length: 25 }, (_, index) => ({
      role: (index % 2 === 0 ? 'user' : 'assistant') as 'user' | 'assistant',
      content: `第 ${index} 条`,
    }))

    const body = toSidecarTurnBody({ query: '接着上面说', history })
    const carried = body.history as { role: string; content: string }[]

    expect(carried).toHaveLength(20)
    // 保留的是**最近的** 20 条（第 5 条到第 24 条），不是最旧的
    expect(carried[0].content).toBe('第 5 条')
    expect(carried[19].content).toBe('第 24 条')
    // 空内容与非法角色都剔掉（边车那一侧还会再兜一层）
    const mixed = toSidecarTurnBody({
      query: '在吗',
      history: [
        { role: 'user', content: '   ' },
        { role: 'assistant', content: '在的' },
      ] as never,
    })
    expect(mixed.history).toEqual([{ role: 'assistant', content: '在的' }])
  })
})

/**
 * 落点只有一个（本机边车）：**打得到就走、打不到就抛**，没有"换个地方跑"这一说。
 *
 * 这里原先还有一组"开关（默认开 / 显式关）"的断言，随 `VITE_SIDECAR_TURNS`
 * 一起退役（见 `api/sidecar.ts` 的文件头）——所以"显式关"那两条删掉了，
 * 剩下的三条量的都是**同一个落点**的三个状态：走边车 / 探不到 / 还没探过。
 */
describe('边车轮次的落点：只有边车，打不到就抛', () => {
  beforeEach(() => {
    resetSidecarProbe()
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('① 默认（不设任何变量）就走边车，状态位说清打的是哪个基址', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    const target = await resolveTurnTarget()

    expect(target.base).toBe(DEFAULT_SIDECAR_BASE)
    expect(target.url).toBe(`${DEFAULT_SIDECAR_BASE}${SIDECAR_STREAM_PATH}`)
    // 走边车也要**说得清**（不是只有一个布尔值）：状态位带上实际基址
    const status = sidecarStatus()
    expect(status.available).toBe(true)
    expect(status.reason).toContain('走边车')
    expect(status.reason).toContain(DEFAULT_SIDECAR_BASE)
  })

  it('② 边车没起来 → 显式失败（状态位 + warn），一条请求都不额外发', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'))
    vi.stubGlobal('fetch', fetchMock)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    await expect(resolveTurnTarget()).rejects.toBeInstanceOf(TurnUnavailableError)

    const status = sidecarStatus()
    expect(status.available).toBe(false)
    expect(status.reason).toContain('ECONNREFUSED')
    expect(status.reason).toContain('跑不了')
    expect(warn).toHaveBeenCalledTimes(1)
    // 探活那一次之外，一条请求都不该发（尤其**没有**打到服务器 /chat/stream 上）
    expect(urlsOf(fetchMock)).toEqual([`${DEFAULT_SIDECAR_BASE}/health`])
  })

  it('③ 还没探过边车时不猜好坏：available=null，但仍然说清默认会先试边车', () => {
    const status = sidecarStatus()

    expect(status.available).toBeNull()
    expect(status.reason).toContain('还没探过')
    expect(status.reason).toContain(DEFAULT_SIDECAR_BASE)
    // reason **不许空着**：三种状态都要能据它判断"这一轮会怎样"
    expect(status.reason.length).toBeGreaterThan(0)
  })
})

describe('审批决定的选址（与轮次同一套，交接文档点名的缺口）', () => {
  beforeEach(() => {
    resetSidecarProbe()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('边车可用 → 决定打到**边车那台**的 /turn/approvals/{id}', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    const target = await resolveApprovalTarget('appr_1')

    expect(target.url).toBe(`${DEFAULT_SIDECAR_BASE}${SIDECAR_APPROVAL_PATH}/appr_1`)
  })

  it('边车不可用 → 抛（不再回退服务器 /chat/approvals/{id}）', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'))
    vi.stubGlobal('fetch', fetchMock)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const error = await resolveApprovalTarget('appr_2').catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(TurnUnavailableError)
    expect((error as Error).message).toContain('ECONNREFUSED')
    expect(warn).toHaveBeenCalledTimes(1)
    expect(urlsOf(fetchMock).some((url) => url.includes('/chat/approvals'))).toBe(false)
  })
})
