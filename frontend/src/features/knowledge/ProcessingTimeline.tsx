/**
 * 处理明细（§12.115；旧 `ProcessingTimeline.vue` 的行为逐条对齐）。
 *
 * 三块内容，对应三个问题：
 *
 * | 块 | 回答的问题 |
 * |----|-----------|
 * | 顶部一行结论 | 共几步、现在第几步、一共花了多久、是死是活 |
 * | 分段进度条 | 一眼看形状（走到哪、在哪停的） |
 * | 环节清单 | 每一步各花多久、重试过几次、失败的原因是什么 |
 *
 * **不给百分比**：环节耗时不均（解析几分钟、切分几秒），百分比只能编出一个对不上的数字；
 * "第 3/6 步 + 每步实际耗时"每一项都能和事实对上。
 */
import { useCallback, useEffect, useState } from 'react'

import { getDocumentTimeline, type DocumentTimeline } from '@/api/documents'
import {
  MeterBar,
  SkeletonRows,
  StatusTag,
  type MeterTone,
  type StatusTone,
} from '@/features/knowledge/composites'
import { messageOf, usePolling } from '@/features/knowledge/store'
import { formatCount, formatMillis } from '@/lib/format'

/** 与列表同一个节拍：两处数字对得上比"更实时"重要。 */
const POLL_INTERVAL_MS = 2000

const STEP_TONE: Record<string, StatusTone> = {
  done: 'success',
  running: 'info',
  failed: 'danger',
  canceled: 'neutral',
  pending: 'neutral',
}

const STEP_LABEL: Record<string, string> = {
  done: '已完成',
  running: '进行中',
  failed: '失败',
  canceled: '已取消',
  pending: '未开始',
}

interface ProcessingTimelineProps {
  documentId: string
  /** 这一页签是否正被看着。**没被看着就别轮询**——抽屉里还有阅读/切块两个页签。 */
  active: boolean
}

export function ProcessingTimeline({ documentId, active }: ProcessingTimelineProps) {
  const [timeline, setTimeline] = useState<DocumentTimeline | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      setTimeline(await getDocumentTimeline(documentId))
      setError('')
    } catch (cause) {
      setError(messageOf(cause, '处理进度加载失败'))
    } finally {
      setLoading(false)
    }
  }, [documentId])

  // 切到这个页签时才第一次加载；换文档时先清空（否则会显示上一份的进度）
  useEffect(() => {
    setTimeline(null)
    setError('')
    if (!active) {
      setLoading(false)
      return
    }
    setLoading(true)
    void load()
  }, [active, documentId, load])

  const running = timeline?.status === 'running'
  // 正在跑**而且**这一页签被看着时才轮询（另外两个页签的读者不关心这些数）
  usePolling(load, {
    active: active && running,
    intervalMs: POLL_INTERVAL_MS,
    immediate: false,
  })

  if (error) return <p className="kb-error-line">{error}</p>
  if (loading) return <SkeletonRows variant="list" rows={4} />
  if (!timeline) return <p className="kb-timeline-note">这份文档还没有处理记录。</p>

  const segments = Array.from({ length: timeline.step_total }, (_, index) => {
    const position = index + 1
    if (position < timeline.current_index) {
      // 走过的那几步：填满。失败的文档走到过的那几步仍然是"走过"，不改色——
      // 只有**停下的那一步**才是红的，不然整条都是红的，看不出炸在哪
      return { fill: 1, tone: 'accent' as MeterTone }
    }
    if (position === timeline.current_index) {
      // 当前这一步填满：它是"正在进行"，不是"完成了 30%"——环节内部我们并不知道进度
      return {
        fill: 1,
        tone: (timeline.stalled
          ? 'danger'
          : timeline.status === 'failed'
            ? 'danger'
            : timeline.status === 'canceled'
              ? 'neutral'
              : 'info') as MeterTone,
        pulsing: Boolean(running),
      }
    }
    return { fill: 0, tone: 'neutral' as MeterTone }
  })

  const statusLabel = timeline.stalled
    ? '疑似卡住'
    : {
        done: '已完成',
        running: '进行中',
        failed: '失败',
        canceled: '已取消',
      }[timeline.status]

  const failure =
    timeline.status === 'failed' || timeline.status === 'canceled'
      ? (timeline.steps.find((step) => step.error)?.error ?? '')
      : ''

  return (
    <div className="kb-timeline">
      <div className="kb-timeline-summary">
        <p style={{ margin: 0 }}>
          共 {formatCount(timeline.step_total)} 个环节 · 当前第{' '}
          {formatCount(timeline.current_index)} 个 · 总耗时 {formatMillis(timeline.total_ms)}
        </p>
        <StatusTag
          label={statusLabel}
          tone={running ? 'info' : (STEP_TONE[timeline.status] ?? 'neutral')}
          running={running}
        />
      </div>

      <MeterBar
        segments={segments}
        valueLabel={`${timeline.current_index} / ${timeline.step_total}`}
        ariaLabel="处理进度"
      />

      {/* 停滞要说清"该做什么"：只说"卡住"等于把问题丢回给用户。
          不再说心跳/续约是怎么算的（实现），只留"它会自己重跑"与那条手动出路 */}
      {timeline.stalled ? (
        <p className="kb-timeline-stall">
          这一步停住了：进程可能重启过，它会自动重跑，也可以在列表里对这篇文档点「重新摄入」。
        </p>
      ) : null}

      <h3 className="kb-timeline-heading">各环节耗时</h3>
      <ol className="kb-step-list">
        {timeline.steps.map((step, index) => (
          <li
            key={step.key}
            className={['kb-step', step.status === 'pending' ? 'kb-step-pending' : '']
              .filter(Boolean)
              .join(' ')}
          >
            <span className="kb-step-order tabular">{index + 1}</span>
            <span className="kb-step-label">{step.label}</span>
            {/* 重试要露头：反复重试的文档与卡住的文档处置不同 */}
            {step.visits > 1 ? <span className="kb-step-visits">进入 {step.visits} 次</span> : null}
            <span className="kb-step-duration tabular">
              {step.status === 'pending' ? '—' : formatMillis(step.duration_ms)}
            </span>
            {step.status !== 'pending' ? (
              <span className="kb-step-status">
                <StatusTag
                  label={STEP_LABEL[step.status] ?? step.status}
                  tone={STEP_TONE[step.status] ?? 'neutral'}
                  running={step.status === 'running'}
                />
              </span>
            ) : (
              <span className="kb-step-status">未开始</span>
            )}
          </li>
        ))}
      </ol>

      {failure ? (
        <>
          <h3 className="kb-timeline-heading">
            {timeline.status === 'failed' ? '失败原因' : '取消说明'}
          </h3>
          {/* 可选中的原文：用户选它就是想去搜、去贴给别人看 */}
          <pre className="kb-timeline-failure">{failure}</pre>
        </>
      ) : null}
    </div>
  )
}
