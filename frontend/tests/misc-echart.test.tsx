/**
 * 图表基座（`features/misc/dashboard/EChart.tsx`）的三条行为——都是组件头注释里
 * "踩过才知道"的那几条，也是《前端设计规范 v0.14》§8 记过的同一件事
 * （旧前端靠 `watch(theme, render)` 做到"切主题热力图重新取色"）：
 *
 * 1. **主题切换要重画**：CSS 变量的值在渲染时读，不重建就还是旧颜色；
 * 2. **`var(--x)` 要解析成实色**：canvas 渲染器不认 CSS 变量，传进去那一块直接不画；
 * 3. **`color-mix(...)` 同理**要解析成实色。
 *
 * echarts 本身在这里被替掉（jsdom 没有 canvas，真 init 会直接把用例打挂）；
 * 换掉的是"画布"，不是被测的这块逻辑。
 */
import { act, render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { initMock, setOptionMock, disposeMock } = vi.hoisted(() => {
  const setOption = vi.fn()
  const resize = vi.fn()
  const dispose = vi.fn()
  return {
    initMock: vi.fn(() => ({ setOption, resize, dispose })),
    setOptionMock: setOption,
    disposeMock: dispose,
  }
})

vi.mock('echarts/core', () => ({ init: initMock, use: vi.fn() }))
vi.mock('echarts/charts', () => ({ BarChart: {}, HeatmapChart: {}, LineChart: {} }))
vi.mock('echarts/components', () => ({
  GridComponent: {},
  LegendComponent: {},
  TooltipComponent: {},
  VisualMapComponent: {},
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

import { EChart } from '@/features/misc/dashboard/EChart'
import { setTheme } from '@/features/misc/settings/useTheme'

/** setOption 最后一次收到的 option。 */
function lastOption(): Record<string, unknown> {
  const call = setOptionMock.mock.calls.at(-1)
  return (call?.[0] ?? {}) as Record<string, unknown>
}

beforeEach(() => {
  setOptionMock.mockClear()
  initMock.mockClear()
  setTheme('system')
})

describe('EChart 图表基座', () => {
  it('切主题会再画一次（否则图表停在旧色）', () => {
    render(<EChart option={{ series: [{ type: 'line', data: [1, 2] }] }} />)
    expect(initMock).toHaveBeenCalledTimes(1)
    const before = setOptionMock.mock.calls.length
    expect(before).toBeGreaterThan(0)

    act(() => setTheme('dark'))

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(setOptionMock.mock.calls.length).toBeGreaterThan(before)
  })

  it('var(--x) 解析成实色（canvas 不认 CSS 变量）', () => {
    document.documentElement.style.setProperty('--text-primary', '#123456')
    render(<EChart option={{ color: 'var(--text-primary)' }} />)
    expect(lastOption().color).toBe('#123456')
    document.documentElement.style.removeProperty('--text-primary')
  })

  it('color-mix(...) 解析成混合后的实色', () => {
    document.documentElement.style.setProperty('--mix-a', '#000000')
    document.documentElement.style.setProperty('--mix-b', '#ffffff')
    render(<EChart option={{ color: 'color-mix(in srgb, var(--mix-a) 50%, var(--mix-b))' }} />)
    // 黑 50% + 白 50% → 127.5 → 四舍五入 128（0x80）
    expect(lastOption().color).toBe('#808080')
    document.documentElement.style.removeProperty('--mix-a')
    document.documentElement.style.removeProperty('--mix-b')
  })

  it('卸载时销毁实例（切页不留 canvas 与观察者）', () => {
    const { unmount } = render(<EChart option={{ series: [{ type: 'line', data: [1] }] }} />)
    unmount()
    expect(disposeMock).toHaveBeenCalledTimes(1)
  })
})
