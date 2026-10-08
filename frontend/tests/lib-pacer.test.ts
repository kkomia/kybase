/**
 * 显示节拍器（`lib/pacer.ts`）——流式节奏的用例。
 *
 * 这一份是**从旧前端 `tests/unit/composables/displayPacer.test.ts` 逐条搬来的**
 * （实现是同一份代码，只换了 import 路径）：迁移期新前端的用例里漏了它，
 * 而这正是用户最能感知的一处——"字是流出来的，还是转圈完突然整段出现"。
 *
 * 手搓假时钟（注入 `now` / `setTimeoutFn` / `clearTimeoutFn`）：要断言"某一拍只吐了多少字"，
 * 自己推进调度器最直观，比 `vi.useFakeTimers` 更好控。
 */
import { describe, expect, it } from 'vitest'

import { createDisplayPacer } from '@/lib/pacer'

function makeClock() {
  let clock = 0
  let nextId = 1
  const timers: { id: number; at: number; run: () => void }[] = []

  return {
    now: () => clock,
    setTimeoutFn: (run: () => void, ms: number) => {
      const id = nextId++
      timers.push({ id, at: clock + ms, run })
      return id as unknown as ReturnType<typeof setTimeout>
    },
    clearTimeoutFn: (id: ReturnType<typeof setTimeout>) => {
      const index = timers.findIndex((timer) => timer.id === (id as unknown as number))
      if (index >= 0) timers.splice(index, 1)
    },
    advance(ms: number) {
      const end = clock + ms
      for (;;) {
        timers.sort((a, b) => a.at - b.at)
        const next = timers[0]
        if (!next || next.at > end) break
        timers.shift()
        clock = next.at
        next.run()
      }
      clock = end
    },
    /** 只推进时钟、不跑定时器——模拟后台标签页里定时器被降频/暂停。 */
    jump(ms: number) {
      clock += ms
      for (const timer of timers) timer.at += ms
    },
  }
}

/** 测试用的固定节奏：100/1000 字每秒、1 秒排空、来源 100ms 一条、心跳 10ms。 */
function pacerOn(clock: ReturnType<typeof makeClock>) {
  const state = { text: '', sources: [] as number[][], drained: 0 }
  const pacer = createDisplayPacer<number>(
    {
      onText: (chunk) => {
        state.text += chunk
      },
      onSources: (items) => state.sources.push(items),
      onDrained: () => {
        state.drained += 1
      },
    },
    {
      now: clock.now,
      setTimeoutFn: clock.setTimeoutFn,
      clearTimeoutFn: clock.clearTimeoutFn,
      minCps: 100,
      maxCps: 1000,
      catchUpSeconds: 1,
      sourceIntervalMs: 100,
      tickMs: 10,
    },
  )
  return { pacer, state }
}

describe('pacer（显示节拍器）', () => {
  it('一次涌进几千字也不会瞬现，但终会吐完', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('x'.repeat(2000))
    pacer.finish()

    clock.advance(50)
    expect(state.text.length).toBeGreaterThan(0)
    // 上限约束：50ms 内最多吐出 maxCps 的一半左右，绝不可能整篇出现
    expect(state.text.length).toBeLessThan(200)

    clock.advance(5000)
    expect(state.text).toHaveLength(2000)
    expect(state.drained).toBe(1)
  })

  it('只有零星增量时按起步速度走，不空转也不拖沓', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('abcde')

    clock.advance(60)
    expect(state.text).toBe('abcde')
    // 正文吐完了但输入没结束：不算交付
    expect(state.drained).toBe(0)

    pacer.finish()
    expect(state.drained).toBe(1)
  })

  it('收到的是分片时逐段显示，顺序与内容都不变', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('甲')
    clock.advance(30)
    pacer.pushText('乙')
    pacer.pushText('丙')
    pacer.finish()

    clock.advance(1000)
    expect(state.text).toBe('甲乙丙')
    expect(state.drained).toBe(1)
  })

  it('finish 带回后端的拼装全文时以它为准，不重复也不丢字', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('旧的')
    pacer.finish('新的全文')

    clock.advance(5000)
    expect(state.text).toBe('新的全文')
    expect(state.drained).toBe(1)
  })

  it('来源按固定间隔逐条亮出，且每次给的是累计前缀', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.setSources([1, 2, 3])

    clock.advance(10)
    expect(state.sources.at(-1)).toEqual([1])

    clock.advance(110)
    expect(state.sources.at(-1)).toEqual([1, 2])

    clock.advance(200)
    expect(state.sources.at(-1)).toEqual([1, 2, 3])
  })

  it('flush 把收到的内容立刻全部显示，用于报错/叫停', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('x'.repeat(500))
    clock.advance(20)
    const before = state.text.length
    pacer.flush()

    expect(state.text).toHaveLength(500)
    expect(state.text.length).toBeGreaterThan(before)
    // flush 不等于交付：done 由调用方决定怎么处理
    expect(state.drained).toBe(0)
  })

  it('stop 丢弃未显示内容并停表', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('x'.repeat(500))
    clock.advance(20)
    const before = state.text.length
    pacer.stop()
    clock.advance(5000)

    expect(state.text).toHaveLength(before)
    expect(state.drained).toBe(0)
  })

  it('后台标签页降频后靠每拍上限追平，不会把积压一次性倾泻', () => {
    const clock = makeClock()
    const { pacer, state } = pacerOn(clock)
    pacer.pushText('x'.repeat(5000))
    // 时钟一下走了 10 秒但一拍都没跑（标签页在后台），下一拍最多 maxCps * 0.25 字
    clock.jump(10000)
    clock.advance(10)
    expect(state.text.length).toBeGreaterThan(200)
    expect(state.text.length).toBeLessThanOrEqual(260)
    expect(pacer.pending).toBeGreaterThan(4000)
  })
})
