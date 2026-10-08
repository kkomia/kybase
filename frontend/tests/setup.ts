/**
 * 测试基建：jsdom 缺的那几件补上（与旧前端 `tests/setup.ts` 同一份意图），
 * 外加**整份用例共用的"网络纪律"**（2026-10-02 收口那轮加的第二段，见下）。
 */
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, expect, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

/* ============================================================== 网络纪律（全局一份）
 *
 * ## 它治的是什么
 *
 * 2026-10-02 前端全量跑出现**跨用例干扰的抖动**（单跑必绿、全量偶尔红，红的用例还不固定：
 * 一次红在设置弹窗的「知识库连接那一节」，下一次红在「连不上时显示原因」）。根因不在断言，
 * 而在**请求逃到真网络 + 晚到的结论写进复位后的共享状态**：
 *
 * 1. `requestLocal()` / `requestLocal` 那一族的第一次 `fetch` **不在调用点上**——它要先
 *    `resolveLocalBase()`（问壳要基址 + 探一次活），所以"这几行代码发出去的读"要过几拍才真打
 *    出去；
 * 2. 于是**上一条用例挂出去的读，可能在下一条用例里才打 `fetch`**。那时用例自己的替身已经
 *    撤了（`vi.unstubAllGlobals()`），真 `fetch` 回到 `globalThis` 上 → 它真打网络：
 *    这台机器上有没有边车在跑（壳开着时它就在 8765），决定了那条请求成不成、写什么进共享
 *    store（`api/provider` / `api/backup` / `api/sidecar` 这几份都是模块级单份）——
 *    于是"红了哪一条"随机器状态变。
 *
 * ## 三条纪律（与 `tests/unit/layout/local-data-strip.test.tsx` 里那套逐条同形）
 *
 * 1. `beforeEach` 先装一层**默认替身**：`/health` 通、其余一律抛错。各用例自己的
 *    `vi.stubGlobal('fetch', …)` 会盖在它上面；这一层管的是"**忘了 stub 的那条不许静默
 *    走真网络**"（实测：整份用例在这个替身下 73 个文件全绿，说明没有哪一条用例真的
 *    依赖"真网络能通"）；
 * 2. `afterEach` **先把还在飞的链条走完**（`drain()`：让它在**本用例自己的替身**下把
 *    `fetch` 打完），再撤替身、装一层**"网络禁用"记账器**——真 `fetch` 从此不回
 *    `globalThis`，任何漏出来的调用都被记进 `escapes` 并立刻抛错；
 * 3. 每条用例收尾对一次账（`expect(escapes).toEqual([])`）：漏了就是一条**带 URL 的**失败，
 *    而不是一条时红时绿的断言。
 *
 * ## 为什么放在全局一份
 *
 * 与文件末那三件 jsdom 兜底同一个理由（"别再让每个测试文件各带一份兜底副本"）：读那三份
 * 共享 store 的用例文件有二十来个，逐份抄同一段纪律只会让"后来新增的文件忘了抄"成为下一个
 * 抖动源。产品侧另有一道**同形的闸门**（`api/provider.ts` 的 `generation`、`api/sidecar.ts`
 * 的 `probeGeneration`、`api/backup.ts` 先前那一位）：复位之后，之前发出去的结论一律作废
 * ——所以**哪怕**真有晚到的结论，也盖不掉复位之后摆好的状态。
 */

/** 漏出替身的那些请求（按用例 + URL 记下来）。**必须永远是空的**。 */
const escapes: string[] = []

/** 让还在飞的链条走完（它在**这一条用例自己的替身**下把 fetch 打完，于是不会漏到外面）。 */
async function drain(): Promise<void> {
  for (let i = 0; i < 3; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
}

/**
 * 这一层的**默认替身**（`/health` 通、其余一律抛错）。
 *
 * 用**直接赋值**而不是 `vi.stubGlobal`：这样它不会被用例文件里的 `vi.unstubAllGlobals()`
 * 撤掉（那一句在各域文件里会用到——它们要清的是自己的 `__TAURI__` 之类），
 * 而用例自己的 `vi.stubGlobal('fetch', …)` 照样能盖在它上面、撤掉之后回到它。
 */
function defaultFetch(url: string): Response {
  const target = String(url)
  // 探活那一趟（`sidecarAvailable` 的 `/health`）是**每条链的必经之路**：放它过去，
  // 免得"忘了 stub"的表现全变成"边车不可达"（那是另一条错误路径）
  if (target.endsWith('/health')) {
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  throw new Error(`这一份用例没预备这条请求：${target}`)
}

/** "网络禁用"记账器：漏出替身的调用都记在这儿并立刻抛错。 */
function forbiddenFetch(url: string): never {
  escapes.push(`${expect.getState().currentTestName ?? '?'} -> ${String(url)}`)
  throw new TypeError('这一份用例里不许打真网络')
}

beforeEach(() => {
  globalThis.fetch = ((url: string) => defaultFetch(url)) as unknown as typeof fetch
})

afterEach(async () => {
  // 先把还在飞的链条走完（此时这一条用例的替身/默认替身还在），再把真 fetch 换掉
  await drain()
  globalThis.fetch = (async (url: string) => forbiddenFetch(String(url))) as typeof fetch
  cleanup()
  window.localStorage.clear()
  // 漏出来了就是一条**带 URL 的**失败，而不是一条时红时绿的断言
  expect(escapes).toEqual([])
})

// jsdom 没有 ResizeObserver，而所有虚拟化/浮层组件都会用它
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ResizeObserverStub)

// 浮层定位（Radix）要 matchMedia
if (!window.matchMedia) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  )
}

// ------------------------------------------------------------------ 其它 jsdom 缺口
//
// 这三件是各域报告里点名要的（ProseMirror 的聚焦、表格/图表的滚动、Radix 的指针捕获），
// 放在**全局**一份，别再让每个测试文件各带一份兜底副本。

if (!Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () =>
    ({
      length: 0,
      item: () => null,
      [Symbol.iterator]: function* () {},
    }) as unknown as DOMRectList
}
if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = () =>
    ({ x: 0, y: 0, width: 0, height: 0, top: 0, right: 0, bottom: 0, left: 0 }) as DOMRect
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn()
}
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
}
