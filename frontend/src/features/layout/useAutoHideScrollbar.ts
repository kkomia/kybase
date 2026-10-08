/**
 * 自动隐藏的滚动条（React 版；与旧前端 `composables/useAutoHideScrollbar.ts` 逐条对应）。
 *
 * 问题不是"有滚动条"，是**它一直在那儿**：侧栏用一小会儿就会长出滚动条，
 * 而那条灰色的竖线会一直贴在边上——它回答的是"你还能往下滚"，
 * 那件事只在鼠标进到这一栏时才需要被回答。
 *
 * 行为照 macOS 的悬浮滚动条：
 *
 * - 默认**不显示**（滑块透明，但**仍然占位**——见下）；
 * - 滚动时出现；鼠标移到滚动条那一条窄带上也出现；停手 1.2 秒自动消失。
 *
 * **为什么不做成真的悬浮（不占位）**：那要自己画一个滑块 + 自己实现拖动、
 * 惯性、命中范围，而"看起来一样"的代价是把浏览器已经做对的事情重做一遍。
 * 这里只切换滑块的颜色（`tokens.css` 的 `.scroll-quiet` 那一组），**条槽宽度一直不变**，
 * 所以内容不会在出现/消失时左右跳一下——那种跳动比一条静止的滚动条更烦人。
 *
 * **为什么不纯 CSS**：`:hover` 能让它出现，但做不到"显示一段时间之后自动消失"。
 */
import { useEffect, type RefObject } from 'react'

/** 停手之后多久消失。macOS 大约 1 秒；这里略长一点，让"刚滚完想再滚"不至于闪两下。 */
export const SCROLLBAR_LINGER_MS = 1200

/**
 * 滚动条那一条窄带有多宽。
 *
 * 只在**布局宽度量不出条槽**时用它兜底：Windows 上的 Chromium 用的是**悬浮滚动条**
 * （`offsetWidth === clientWidth`，条槽一点都不占位），此时"用布局算条槽"永远是 0，
 * 鼠标移过去根本不亮。两种情况都由这一条覆盖：
 *
 * - 经典滚动条：`rect.width - clientWidth` 就是真实条槽，取它与 12 的较大者；
 * - 悬浮滚动条：量出来接近 0，退回 CSS 里定的那 12px。
 */
export const SCROLLBAR_ZONE_PX = 12

/**
 * 让一个滚动容器按"用时出现、停手消失"显示滚动条。
 *
 * `ref` 必须指向**真正滚动的那一层**（`overflow-y: auto` 的那个），不是它的外壳：
 * 外壳的 `clientWidth` 与滚动条无关，命中判定会算错。
 *
 * `enabled` 是给"这一层今天才被渲染出来"的情形用的：侧栏折叠时那一层整段不在 DOM 里，
 * 展开之后 ref 才拿到元素。**把它放进依赖**，展开的那一刻才会真的挂上监听
 * （旧 Vue 版靠 `watch(target)` 做同一件事）。
 */
export function useAutoHideScrollbar(ref: RefObject<HTMLElement | null>, enabled = true): void {
  useEffect(() => {
    if (!enabled) return
    const element = ref.current
    if (!element) return

    let timer: ReturnType<typeof setTimeout> | null = null

    const clear = (): void => {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    }

    /** 亮一下，并在 `SCROLLBAR_LINGER_MS` 之后自己灭掉。 */
    const flash = (): void => {
      element.classList.add('is-scroll-visible')
      clear()
      timer = setTimeout(() => {
        element.classList.remove('is-scroll-visible')
        timer = null
      }, SCROLLBAR_LINGER_MS)
    }

    /**
     * 鼠标是不是落在**滚动条那一条窄带**上。
     *
     * 不滚动的内容不该因为鼠标划过右边缘就亮一下——那时根本没有滚动条可抓，
     * 所以先判"这一栏现在滚不滚得动"。
     */
    const onPointerMove = (event: PointerEvent): void => {
      if (element.scrollHeight <= element.clientHeight) return
      const rect = element.getBoundingClientRect()
      const gutter = Math.max(rect.width - element.clientWidth, SCROLLBAR_ZONE_PX)
      if (event.clientX >= rect.right - gutter) flash()
    }

    // 滚动那条回调只改一个 class，不该拖慢滚动
    element.addEventListener('scroll', flash, { passive: true })
    element.addEventListener('pointermove', onPointerMove, { passive: true })
    return () => {
      element.removeEventListener('scroll', flash)
      element.removeEventListener('pointermove', onPointerMove)
      clear()
      element.classList.remove('is-scroll-visible')
    }
  }, [ref, enabled])
}
