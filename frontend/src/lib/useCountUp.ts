/**
 * 数字滚动（0 → 目标值，缓出 520ms）。
 *
 * 只用在**概览页那几个大数**上：数据到达时让数字自己走到位，是"这一页在动"
 * 最轻的一种表达——没有位移、没有颜色变化，只有一个数在长。
 *
 * 尊重 `prefers-reduced-motion`：那种情况下直接给终值（不是给 0 再跳）。
 */
import { useEffect, useRef, useState } from 'react'

export function useCountUp(target: number, duration = 520): number {
  const [value, setValue] = useState(0)
  const valueRef = useRef(0)

  useEffect(() => {
    valueRef.current = value
  }, [value])

  useEffect(() => {
    /**
     * 用例环境（vitest）里**直接给终值**：数字滚动依赖 `requestAnimationFrame`，
     * 而用例断的是"最终显示的是 21,606"——让它等一帧帧动画既慢又脆。
     */
    const animate = import.meta.env.MODE !== 'test'
    const reduce =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
    if (!animate || reduce || !Number.isFinite(target)) {
      setValue(target)
      return
    }
    const from = valueRef.current
    if (from === target) return
    const start = performance.now()
    let frame = 0
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration)
      // 缓出三次：前段快、后段收，数字停下时不"急刹"
      const eased = 1 - Math.pow(1 - progress, 3)
      setValue(from + (target - from) * eased)
      if (progress < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [target, duration])

  return value
}
