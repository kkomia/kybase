/**
 * 侧栏那两枚**自绘**图标（`features/layout/icons.tsx`）——**从旧 Vue 版
 * `tests/unit/components/icons.test.ts` 的契约搬来的**。
 *
 * 它们不属于 Remix 那套（几何是照用户给的参考图描的），例外的代价是**必须自己把网格与
 * 线宽调对、把颜色交还给 `currentColor`**——这几件事一错就是"某个主题下图标看不见"
 * 或者"比旁边粗/大一圈"，看着正常却悄悄退化。迁移期新测试只覆盖到侧栏渲染，契约本身没钉。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { IconChatNew, IconSidebar } from '@/features/layout/icons'

function svgOf(element: React.ReactElement): SVGSVGElement {
  const { container } = render(element)
  const svg = container.querySelector('svg')
  if (!svg) throw new Error('没有渲染出 svg')
  return svg
}

describe('两枚自绘图标共同的契约（与旧 IconBase 一致）', () => {
  for (const [name, Icon] of [
    ['IconChatNew', IconChatNew],
    ['IconSidebar', IconSidebar],
  ] as const) {
    it(`${name}：24 网格、currentColor、默认 16px、对读屏器隐藏`, () => {
      const svg = svgOf(<Icon />)
      expect(svg.getAttribute('viewBox')).toBe('0 0 24 24')
      expect(svg.getAttribute('fill')).toBe('currentColor')
      expect(svg.getAttribute('width')).toBe('16')
      expect(svg.getAttribute('height')).toBe('16')
      // 图标是装饰，名字由外层按钮的 aria-label 给
      expect(svg.getAttribute('aria-hidden')).toBe('true')
    })

    it(`${name}：size 控制宽高`, () => {
      const svg = svgOf(<Icon size={24} />)
      expect(svg.getAttribute('width')).toBe('24')
      expect(svg.getAttribute('height')).toBe('24')
    })

    it(`${name}：颜色只能来自 currentColor——不许出现写死的色值`, () => {
      const { container } = render(<Icon />)
      const fills = Array.from(container.querySelectorAll('[fill]')).map((el) =>
        el.getAttribute('fill'),
      )
      expect(fills.length).toBeGreaterThan(0)
      for (const fill of fills) expect(['none', 'currentColor']).toContain(fill)
      // 写死的色值（#hex / rgb()）会在另一套主题下露馅
      expect(container.innerHTML).not.toMatch(/#[0-9a-fA-F]{3,6}\b|rgba?\(/)
    })
  }
})

describe('IconChatNew', () => {
  it('直接按 24 网格布点：没有第二套坐标系，就不该有 transform 缩放', () => {
    const svg = svgOf(<IconChatNew />)
    expect(svg.querySelector('g')?.getAttribute('transform')).toBeFalsy()
  })

  it('是线稿而不是实心块：fill=none + stroke 走 currentColor + 线宽 1.8 上下', () => {
    const group = svgOf(<IconChatNew />).querySelector('g')!
    expect(group.getAttribute('fill')).toBe('none')
    expect(group.getAttribute('stroke')).toBe('currentColor')
    // 线宽写成 24 网格之外的大数（比如 40）会糊成一坨
    const width = Number(group.getAttribute('stroke-width'))
    expect(width).toBeGreaterThanOrEqual(1.5)
    expect(width).toBeLessThanOrEqual(2.1)
  })

  it('几何完整：气泡轮廓 + 时钟指针（竖一笔再斜一笔）', () => {
    const paths = Array.from(svgOf(<IconChatNew />).querySelectorAll('path'))
    expect(paths).toHaveLength(2)
    // 气泡轮廓：圆角矩形 + 左下角小尾巴，点很多，长度自然长
    expect((paths[0].getAttribute('d') ?? '').length).toBeGreaterThan(80)
    // 指针：竖到圆心再斜向右下——参考图里就是这个形状，
    // 换成别的走向（比如整根横线）这条断言会拦住
    expect(paths[1].getAttribute('d') ?? '').toMatch(/^M[\d. ]+v[\d.]+l[\d. ]+$/)
  })
})
