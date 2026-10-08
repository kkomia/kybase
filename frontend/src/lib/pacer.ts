/**
 * 显示节拍器：把「可能一口气到齐」的流式内容，按一个受控的速度送到界面。
 *
 * 为什么需要它：SSE 到得整齐不代表人看得见过程。向量检索是一次 API 调用，
 * 结果整批返回；有些对话模型的端点也快得离谱，几十个 delta 挤在同一毫秒里。
 * 直接把 delta 往 DOM 上贴，用户看到的就是「转圈 → 突然整段文字」——
 * 既没有"它在检索、它在写"的过程感，也无法在长回答里提前读前半段。
 *
 * 做法：内容照收不误（不丢字），但显示速度由这里统一控制——
 *
 * - 速度随积压自适应，再被上下限夹住：`rate = clamp(积压 / catchUpSeconds, min, max)`。
 *   只有零星增量时按 minCps 走（自然的手打节奏）；一次涌进来几千字则提速到 maxCps
 *   （再快也不瞬现）。积压变小时速度平滑回落，不会"啪"地停住。
 * - 收尾时若只剩几个字，直接吐完：否则按指数回落的那条尾巴永远逼近 0，会拖很久。
 * - 来源（检索结果）单独排队，按固定间隔逐条亮出——它们是"先到的一批"，
 *   整批出现同样没有过程感。
 *
 * 计时用 `setTimeout`（默认 24ms ≈ 40fps）而不是 requestAnimationFrame：
 * 后台标签页里 rAF 会被完全暂停，用户切回来时内容还停在半路；定时器只被降频，
 * 配合每帧的 dt 上限，切回来会稳稳追上而不是一次性倾泻。
 *
 * 本模块不依赖 Vue，可单独测：注入 `now` 与定时器即可用假时钟驱动。
 */

export interface PacerHandlers<T> {
  /** 正文增量：调用方拿到的永远是"这一拍该显示的字"。 */
  onText?: (chunk: string) => void
  /** 来源前缀：随亮出进度反复回调，每次给的是**累计**到当前的前几条。 */
  onSources?: (items: T[]) => void
  /** 正文与来源都已吐完（且不再有输入）时触发一次。 */
  onDrained?: () => void
}

export interface DisplayPacer<T> {
  /** 追加一段收到的正文。 */
  pushText: (text: string) => void
  /** 登记本轮的来源，之后按间隔逐条亮出。 */
  setSources: (items: T[]) => void
  /**
   * 告知输入已结束。可带最终全文（后端 done 里的拼装结果，以它为准）；
   * 排空后会触发 onDrained。
   */
  finish: (finalText?: string) => void
  /** 把还没显示的内容立刻全部显示（用于报错/叫停，别让收到的字白收）。 */
  flush: () => void
  /** 丢弃尚未显示的内容并停表（组件销毁等场合）。 */
  stop: () => void
  /** 还剩多少字没显示。 */
  readonly pending: number
}

export interface DisplayPacerOptions {
  /** 起步速度（字/秒）。 */
  minCps?: number
  /** 速度上限（字/秒）——再快也不会一瞬间全糊上去。 */
  maxCps?: number
  /** 排空目标时长（秒）：积压按它反推速度。 */
  catchUpSeconds?: number
  /** 相邻两条来源之间的间隔（毫秒）。 */
  sourceIntervalMs?: number
  /** 心跳间隔（毫秒）。 */
  tickMs?: number
  /** 注入时钟/定时器，测试用。 */
  now?: () => number
  setTimeoutFn?: (handler: () => void, ms: number) => ReturnType<typeof setTimeout>
  clearTimeoutFn?: (id: ReturnType<typeof setTimeout>) => void
}

/** 自然的手打节奏。太慢会显得卡，40 字/秒接近"看得清又在动"。 */
export const PACER_MIN_CPS = 40
/**
 * 显示速度天花板。选 600 而不是更大的数：2000 字的回答约 3.3 秒放完，
 * 既明显是"流"出来的，又不至于让用户等得比模型还久。
 */
export const PACER_MAX_CPS = 600
/** 积压排空的目标时长；越小越接近瞬现。 */
export const PACER_CATCH_UP_SECONDS = 0.9
/** 来源逐条亮出的节奏。 */
export const PACER_SOURCE_INTERVAL_MS = 110
/** 心跳间隔，约 40fps。 */
export const PACER_TICK_MS = 24
/** 结尾只剩这么几个字时直接吐完，避免指数尾巴拖不完。 */
const SNAP_CHARS = 10

export function createDisplayPacer<T>(
  handlers: PacerHandlers<T>,
  options: DisplayPacerOptions = {},
): DisplayPacer<T> {
  const minCps = options.minCps ?? PACER_MIN_CPS
  const maxCps = options.maxCps ?? PACER_MAX_CPS
  const catchUpSeconds = options.catchUpSeconds ?? PACER_CATCH_UP_SECONDS
  const sourceIntervalMs = options.sourceIntervalMs ?? PACER_SOURCE_INTERVAL_MS
  const tickMs = options.tickMs ?? PACER_TICK_MS
  const now = options.now ?? Date.now
  const setTimer = options.setTimeoutFn ?? ((handler, ms) => setTimeout(handler, ms))
  const clearTimer = options.clearTimeoutFn ?? ((id) => clearTimeout(id))

  /** 要显示的全文（finish 后以后端 done 的拼装结果为准）。 */
  let target = ''
  /** 已经送出的字数。 */
  let emitted = 0
  /** 攒下来的"可显示额度"（字），不足 1 就留到下一拍。 */
  let budget = 0
  let sources: T[] = []
  let sourceEmitted = 0
  /** 来源队列的计时器，初值给一个间隔让第一条在本拍就亮。 */
  let sourceAccum = sourceIntervalMs
  let finished = false
  let drained = false
  let running = false
  let lastTick = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  function emitText(count: number): void {
    if (count <= 0) return
    const chunk = target.slice(emitted, emitted + count)
    emitted += count
    if (chunk) handlers.onText?.(chunk)
  }

  function stopTimer(): void {
    if (timer !== null) {
      clearTimer(timer)
      timer = null
    }
  }

  function fireDrained(): void {
    if (drained) return
    drained = true
    running = false
    stopTimer()
    handlers.onDrained?.()
  }

  function schedule(): void {
    if (timer === null && !drained) timer = setTimer(tick, tickMs)
  }

  function queue(): void {
    if (running || drained) return
    running = true
    lastTick = now()
    schedule()
  }

  function tick(): void {
    timer = null
    const at = now()
    // 后台标签页里定时器会被降频：把每拍的时间上限压到 250ms，
    // 用户切回来时是"快放一会儿"追平，而不是一帧倾泻整篇。
    const elapsed = Math.max(0, Math.min(at - lastTick, 250)) / 1000
    lastTick = at

    const textPending = target.length - emitted
    if (textPending > 0) {
      const rate = Math.min(maxCps, Math.max(minCps, textPending / catchUpSeconds))
      budget = Math.min(budget + rate * elapsed, maxCps * 0.25)
      let count = Math.floor(budget)
      if (count > 0) {
        count = Math.min(count, textPending)
        budget -= count
        emitText(count)
      }
      if (finished && target.length - emitted <= SNAP_CHARS) {
        emitText(target.length - emitted)
        budget = 0
      }
    }

    if (sourceEmitted < sources.length) {
      sourceAccum += elapsed * 1000
      let grew = false
      while (sourceEmitted < sources.length && sourceAccum >= sourceIntervalMs) {
        sourceAccum -= sourceIntervalMs
        sourceEmitted += 1
        grew = true
      }
      if (grew) handlers.onSources?.(sources.slice(0, sourceEmitted))
    }

    const textDone = emitted >= target.length
    const sourcesDone = sourceEmitted >= sources.length
    if (finished && textDone && sourcesDone) {
      fireDrained()
      return
    }
    if (textDone && sourcesDone) {
      // 暂时没内容可吐（等下一段增量），停表别空转
      running = false
      return
    }
    schedule()
  }

  return {
    pushText(text) {
      if (!text) return
      target += text
      queue()
    },
    setSources(items) {
      if (items.length === 0) {
        handlers.onSources?.([])
        return
      }
      sources = items
      sourceEmitted = 0
      sourceAccum = sourceIntervalMs
      queue()
    },
    finish(finalText) {
      if (finalText !== undefined) {
        target = finalText
        if (emitted > target.length) emitted = target.length
      }
      finished = true
      const textDone = emitted >= target.length
      const sourcesDone = sourceEmitted >= sources.length
      if (textDone && sourcesDone) {
        fireDrained()
        return
      }
      queue()
    },
    flush() {
      running = false
      stopTimer()
      if (emitted < target.length) emitText(target.length - emitted)
      if (sourceEmitted < sources.length) {
        sourceEmitted = sources.length
        handlers.onSources?.(sources.slice())
      }
      budget = 0
    },
    stop() {
      running = false
      stopTimer()
    },
    get pending() {
      return target.length - emitted
    },
  }
}
