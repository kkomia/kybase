/**
 * 本机权威面（M2 阶段 4）：前缀表、真实基址、以及**不回退的纪律**。
 *
 * 这一份钉的是"会话数据的主人是谁"那一半（`LOCAL_PATHS` + `requestLocal`）。
 * 上面那份 `tests/api-sidecar.test.ts` 钉的是"一轮对话在哪台机器上跑"那一半
 * （`/chat/stream` 的选址）——两者**不是一回事**：对话那条链 2026-10-05 起
 * **只有边车一个落点**（打不到就抛，服务器那一份退役了），而本机权威面是
 * "打不到就如实报错、绝不换源"。两边的处置现在一致了，判据仍然是两套。
 *
 * 三条纪律各有一组用例（方案 §4.3）：
 *
 * 1. **前缀判定只有一处**（`LOCAL_PATHS`）：服务器面（账号 / 知识库 / 技能 / 对话流）
 *    一条都不许被卷进来；
 * 2. **壳里本机后端没起来 → 如实报错、不回退**：错误文案就是方案那句原话，
 *    而且**一次都不许**打服务器那条链（换源会让用户以为会话丢了）；
 * 3. **显式关不许静默**：`VITE_LOCAL_DATA=0` 时走服务器，顶栏那条状态要说得清
 *    是哪个变量关的。
 *
 * 另有两组是"这两件事为什么不会出现在别的形态里"：壳里问到的**真实端口**
 * （8765 被占会顺延，构建期常量是错的），与**浏览器形态**（本机后端由桌面壳提供，
 * 那一档不成立 → 走服务器，但顶栏照样说清楚，不静默）。
 *
 * 全程不打真网络：`fetch` 与壳的 IPC 都是替身。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { API_BASE, requestLocal } from '@/api/client'
import {
  DEFAULT_SIDECAR_BASE,
  LOCAL_PATHS,
  ensureLocalBase,
  inDesktopShell,
  isLocalPath,
  localAvailable,
  localDataEnabled,
  localDataEnabledFrom,
  localizeUrl,
  localStatus,
  resetSidecarProbe,
  resolveTurnTarget,
  setLocalDataForTest,
  sidecarBase,
  sidecarStatus,
} from '@/api/sidecar'

/** 壳的 IPC 替身：只有 `sidecar_info` 一个命令，回答由用例给。 */
function stubShell(info: { port?: number; base?: string; on_default_port?: boolean } | null): void {
  vi.stubGlobal('__TAURI__', {
    core: {
      invoke: vi.fn(async (cmd: string) => {
        if (cmd !== 'sidecar_info') throw new Error(`用例没料到的命令：${cmd}`)
        return info
      }),
    },
  })
}

/** 边车活着的回答（`/health` 200）。 */
function okJson(): Response {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

/** 一次请求的地址（断言"打的是哪台"用）。 */
function urlOf(index: number): string {
  return String(vi.mocked(fetch).mock.calls[index]?.[0] ?? '')
}

beforeEach(() => {
  resetSidecarProbe()
  vi.restoreAllMocks()
  setLocalDataForTest(undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
  setLocalDataForTest(undefined)
  resetSidecarProbe()
})

describe('① 前缀表：判定只有一处', () => {
  it('表里那些前缀本身、它们的子路径、带查询串/尾斜杠的都归本机', () => {
    for (const prefix of LOCAL_PATHS) {
      expect(isLocalPath(prefix), prefix).toBe(true)
      expect(isLocalPath(`${prefix}/x`), prefix).toBe(true)
      expect(isLocalPath(`${prefix}/x/y?k=v`), prefix).toBe(true)
      expect(isLocalPath(`${prefix}/`), prefix).toBe(true)
    }
    // 表里必须有前端**真在用的**那些域（少一项＝那一域还在绕道 NAS，
    // 而本机库里那批数据根本不会出现在响应里）
    expect(LOCAL_PATHS).toEqual([
      '/conversations',
      '/notes',
      '/settings',
      '/model-registry',
      '/workspaces',
      // 方案原文写的是 `/schedules`，实际端点前缀是 `/scheduled-tasks`
      // （`api/v1/router.py` 里 `schedules.router` 的 prefix）——写实的那一个
      '/scheduled-tasks',
      '/mcp-servers',
      '/memory',
      '/chat/context-usage',
      // 命令目录（2026-10-05）：命令与技能在本机、执行那一轮也在本机（边车）
      '/chat/commands',
      // 2026-10-05 补的五条：本机档早就挂了这五族端点，表一直没跟上
      // （`skills` / `plugins` / `sandbox` / `site_icons` / `stats_reads`）
      '/skills',
      '/plugins',
      '/sandbox',
      '/site-icons',
      // 用量那一条是**写实的**：本机档只薄重声明了 `/stats/usage`，
      // 而 `/stats/dashboard` 数的是知识库的文档与任务（不挂本机档）
      '/stats/usage',
      // 网页那两条（`/web/page` 抓正文、`/web/embed-check` 探嵌入）：代抓与 SSRF 闸
      // 都在这台机器上，绕道 NAS 等于把"本机代取"这件事说反了（见 sidecar.ts 那一段）
      '/web',
      '/local',
    ])
  })

  it('按路径段比：`/conversation`、`/notes-old`、`/localStorage` 都不是本机权威面', () => {
    for (const path of [
      '/conversation',
      '/notes-old',
      '/settingsX',
      '/localStorage',
      '/workspace',
      '/memories',
      '/skill',
      '/stats/usage-all',
    ]) {
      expect(isLocalPath(path), path).toBe(false)
    }
  })

  it('服务器面一条都不许被卷进来（数据的主人在 NAS）', () => {
    for (const path of [
      '/auth/status', // 账号
      '/knowledge-bases',
      '/documents',
      '/search',
      '/chunks',
      '/folders',
      '/wiki',
      // `/stats` 这一族**只**把那一条薄重声明（`/stats/usage`）算本机权威面：
      // 概览那条数的是知识库的文档与任务，权威在 NAS
      '/stats',
      '/stats/dashboard',
      '/tasks',
      '/tabular',
      '/data-sources',
      '/chat/stream', // 对话轮次那条链（另一套选址，见 api-sidecar.test.ts）
      '/chat/suggested-questions',
      '/chat/turns/c1/live',
    ]) {
      expect(isLocalPath(path), path).toBe(false)
    }
  })
})

describe('② 壳里本机后端没起来：如实报错，**不换源**', () => {
  it('壳说"没有边车"（sidecar_info 空）→ 抛方案那句原话，且一次都不打服务器', async () => {
    stubShell(null)
    const fetchMock = vi.fn().mockResolvedValue(okJson())
    vi.stubGlobal('fetch', fetchMock)

    // 先问一次（界面启动时那条状态条就是这么做的），结论要落在"不可用"上
    expect(await localAvailable()).toBe(false)
    const status = localStatus()
    expect(status.kind).toBe('unavailable')
    expect(status.reason).toContain('本机后端未启动')
    expect(status.reason).toContain('未回退服务器')

    await expect(requestLocal('/conversations')).rejects.toThrow(
      '本机后端未启动：桌面壳里没有正在运行的边车（sidecar_info 返回空）；会话数据在本机，未回退服务器',
    )
    // **最要紧的一条**：连 `/health` 都没探（壳已经说没有了，8765 上可能是别人的进程），
    // 更没有任何一条请求落到服务器那条链上
    expect(fetchMock).not.toHaveBeenCalled()
    expect(localDataEnabled()).toBe(true)
  })

  it('壳给了端口但边车不答应（/health 连不上）→ 同样如实报错，探的只有那个端口', async () => {
    stubShell({ port: 8766, base: 'http://127.0.0.1:8766' })
    const fetchMock = vi.fn().mockRejectedValue(new Error('ECONNREFUSED'))
    vi.stubGlobal('fetch', fetchMock)

    await expect(requestLocal('/notes')).rejects.toThrow('本机后端未启动')
    await expect(requestLocal('/notes')).rejects.toThrow('会话数据在本机，未回退服务器')

    // 探活打的是**壳给的那个端口**，而且只有探活（没有一条业务请求打到服务器）
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(urlOf(0)).toBe('http://127.0.0.1:8766/health')
    expect(localStatus().kind).toBe('unavailable')
    expect(localStatus().reason).toContain('边车不可达')
  })

  it('后端回了错（有状态码）不算"没起来"：原样抛，别把排障方向带跑偏', async () => {
    stubShell({ port: 8765, base: DEFAULT_SIDECAR_BASE })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        String(url).endsWith('/health')
          ? okJson()
          : new Response(JSON.stringify({ code: 'not_found', message: '会话不存在' }), {
              status: 404,
            }),
      ),
    )

    await expect(requestLocal('/conversations/conv_x')).rejects.toThrow('会话不存在')
  })
})

describe('③ 显式关（VITE_LOCAL_DATA=0）：走服务器，而且**不静默**', () => {
  it('状态条说得清是哪个变量关的；请求打服务器；**不去探边车**', async () => {
    setLocalDataForTest(false)
    // 每次调用都给一份**新的** Response（同一份对象的正文只能读一次）
    const fetchMock = vi.fn(async () => okJson())
    vi.stubGlobal('fetch', fetchMock)
    const info = vi.spyOn(console, 'info').mockImplementation(() => {})

    const status = localStatus()
    expect(status.kind).toBe('server')
    expect(status.enabled).toBe(false)
    expect(status.reason).toContain('VITE_LOCAL_DATA')
    expect(status.reason).toContain(API_BASE)

    await requestLocal('/settings', { method: 'PATCH', body: '{}' })

    expect(urlOf(0)).toBe(`${API_BASE}/settings`)
    // 关掉之后**一次探测都不该有**（探了也没用，白打一次请求）
    expect(fetchMock).toHaveBeenCalledTimes(1)
    // 那条 info 只打一次（每个请求都打会把控制台刷满）
    await requestLocal('/notes')
    expect(info).toHaveBeenCalledTimes(1)
    expect(String(info.mock.calls[0][0])).toContain('VITE_LOCAL_DATA')
  })

  it('字面量判据与对话轮次同一套：只有 0/false/no/off 关', () => {
    for (const raw of [undefined, '', '   ', '1', 'true', 'yes', 'on']) {
      expect(localDataEnabledFrom(raw), String(raw)).toBe(true)
    }
    for (const raw of ['0', 'false', 'FALSE', 'no', 'Off', ' 0 ']) {
      expect(localDataEnabledFrom(raw), String(raw)).toBe(false)
    }
  })
})

describe('④ 壳里问到的**真实基址**（8765 被占会顺延）', () => {
  it('sidecar_info 给的端口才是打的那个（两侧共用同一个基址）', async () => {
    stubShell({ port: 8767, base: 'http://127.0.0.1:8767', on_default_port: false })
    const fetchMock = vi.fn().mockResolvedValue(okJson())
    vi.stubGlobal('fetch', fetchMock)

    expect(inDesktopShell()).toBe(true)
    await ensureLocalBase()
    expect(sidecarBase()).toBe('http://127.0.0.1:8767')

    await requestLocal('/notes')
    // 顺序：先探活（那个真实端口），再打业务接口
    expect(urlOf(0)).toBe('http://127.0.0.1:8767/health')
    expect(urlOf(1)).toBe(`http://127.0.0.1:8767${API_BASE}/notes`)

    // 对话轮次那条链读的是**同一个**基址（原先它读构建期常量＝永远 8765 ✗）
    const target = await resolveTurnTarget()
    expect(target.base).toBe('http://127.0.0.1:8767')
    expect(target.url).toBe('http://127.0.0.1:8767/turn/stream')
  })

  it('拿不到基址就退回构建期那份常量（浏览器开发形态），状态照样是"本机"', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson())
    vi.stubGlobal('fetch', fetchMock)

    expect(inDesktopShell()).toBe(false)
    expect(await ensureLocalBase()).toBe(DEFAULT_SIDECAR_BASE)
    expect(sidecarBase()).toBe(DEFAULT_SIDECAR_BASE)

    await requestLocal('/conversations?limit=1')
    expect(urlOf(1)).toBe(`${DEFAULT_SIDECAR_BASE}${API_BASE}/conversations?limit=1`)
    expect(localStatus().kind).toBe('local')
    expect(sidecarStatus().available).toBe(true)
  })
})

describe('⑤ 浏览器里（没有桌面壳）：这一档不成立 → 走服务器，但不静默', () => {
  it('探不到本机后端时不抛错、走服务器，顶栏照样说清"浏览器里没有本机后端"', async () => {
    // 只有探活那条不通（本机后端不在），业务请求照常给答复（服务器那条链是活的）
    const fetchMock = vi.fn(async (url: string) => {
      if (String(url).endsWith('/health')) throw new Error('ECONNREFUSED')
      return okJson()
    })
    vi.stubGlobal('fetch', fetchMock)

    expect(await localAvailable()).toBe(false)
    const status = localStatus()
    expect(status.kind).toBe('server')
    expect(status.reason).toContain('浏览器')

    // NAS 上那份网页端就是这么活的（方案 §9："网页端继续可用"）——
    // 服务器就是那边的权威，这不是"把本机数据换源"
    await requestLocal('/conversations')
    expect(urlOf(1)).toBe(`${API_BASE}/conversations`)
  })
})

describe('⑥ 调用点写错了只警告不拦（运行时把能用的请求变成异常更糟）', () => {
  it('不在 LOCAL_PATHS 里的路径照样发出去，但留一条 warn', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson())
    vi.stubGlobal('fetch', fetchMock)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    // `/stats/dashboard` 是一个**真实存在**的表外路径：数的是知识库的文档与任务，
    // 权威在 NAS（表里那条 `/stats/usage` 与它按路径段并不互相覆盖）
    await requestLocal('/stats/dashboard')

    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('LOCAL_PATHS')
    expect(urlOf(1)).toBe(`${DEFAULT_SIDECAR_BASE}${API_BASE}/stats/dashboard`)
  })
})

describe('⑦ 本机后端回的**相对**链接要贴到本机基址上（签名 URL 那几条）', () => {
  it('本机档：相对地址 → 边车那台的绝对地址（否则预览/下载会打到 NAS 上）', async () => {
    stubShell({ port: 8766, base: 'http://127.0.0.1:8766' })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => okJson()),
    )

    const signed = '/api/v1/conversations/conv_1/files/content?key=a%2Fb.png&signature=s'

    expect(await localizeUrl(signed)).toBe(`http://127.0.0.1:8766${signed}`)
  })

  it('已经是绝对地址、或不在本机档：一个字都不改', async () => {
    const fetchMock = vi.fn(async () => okJson())
    vi.stubGlobal('fetch', fetchMock)

    // 服务器回的绝对地址与 data URL 都不是"本机后端那条相对链接"
    expect(await localizeUrl('https://nas.example/api/v1/x')).toBe('https://nas.example/api/v1/x')
    expect(await localizeUrl('data:image/png;base64,AAA')).toBe('data:image/png;base64,AAA')

    // 显式关（逃生门）：相对地址本来就落在同源上（壳转发 / nginx），不许硬贴基址
    setLocalDataForTest(false)
    const relative = '/api/v1/notes/n1/images/a.png?signature=s'
    expect(await localizeUrl(relative)).toBe(relative)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

/* ------------------- 复位之后，晚到的探活结论不许写进来（`probeGeneration`） ------------------- */

/**
 * 与 `api/backup.ts` 的 `generation` / `api/provider.ts` 的那一位同形、同一条理由：
 * `sidecarAvailable()` 的 `fetch` 不在调用点上（要先 `ensureLocalBase()` 问壳），所以
 * "那一刻发出去的探活"可能**在复位之后**才回来。那份结论一旦写进 `probe`
 * （`available/reason/at`），就会把复位之后重新探的那份盖掉——真机上是"顶栏又闪回上一次
 * 的结论"，用例里是跨用例干扰（2026-10-02 那次全量抖动就是这一族）。
 *
 * `resetSidecarProbe()` 在生产代码里没有调用点（它就是用例出口），所以这一位只把
 * "复位"的语义补完整：**复位之后，之前发出去的一律不算数**。
 */
describe('复位之后，晚到的探活结论不许写进来', () => {
  it('复位之后放行旧探活：`localStatus()` 仍是"还没探过"，不是被它写成"走本机"', async () => {
    stubShell({ port: 8765, base: 'http://127.0.0.1:8765' })
    let open = false
    let openGate: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      openGate = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        if (!open) await gate
        return okJson()
      }),
    )

    const pending = localAvailable()
    await new Promise((resolve) => setTimeout(resolve, 0))

    resetSidecarProbe() // 下一条用例的常态
    open = true
    openGate()
    await pending
    await new Promise((resolve) => setTimeout(resolve, 0))

    // 复位之后回到"还没探过"（`available: null`），而不是被那条旧探活写成 true
    expect(localStatus().available).toBeNull()
    expect(localStatus().kind).toBe('local')
  })
})
