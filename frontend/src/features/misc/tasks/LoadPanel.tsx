/**
 * 运行负载面板（任务中心顶部，§12.115）——与旧前端 `components/tasks/LoadPanel.vue` 对应。
 *
 * **要回答的是"后台为什么慢"**，而这个问题在任务列表里找不到答案：列表说的是
 * "每个任务怎么了"，慢的原因却常常与任何单个任务无关——是 CPU 满了、并发槽位只有 1 个、
 * 还是云端额度用尽在降级排队。这一格把那些数摊开。
 *
 * 五格的读数**是同一种控件**（评审 T4：此前三个环形 + 一个环形替身 + 一个纯数字，
 * 其中「并发槽位」那个环里没有数字、轨道还比另两个淡，看起来像没渲染出来）：
 * 一律给环 + 指标名 + 一行原始数值，环里那点位置只放这一格的结论
 * （百分比 / `在跑/槽位` / 本进程占机器内存的比例）。每格的结论都对应一种**可操作**的判断：
 * 系统负载 → 降并发；任务并发 → 抬 KYLAB_WORKER_CONCURRENCY；
 * 进程内存 → 重启/查漏；云端额度 → 等次日额度或换本地解析。
 *
 * **CPU 可能是"—"**：后端按两次采样之差算，第一次问就是没有差值。
 * 这不是缺失，界面上如实显示"—"而不是画一根 0% 的环（那会被读成"机器很空闲"）。
 * 0 值的环**不画弧**（`RingGauge` 里那一条）：`strokeDasharray="0 C"` 配圆头会画出一个圆点，
 * 与"0 / 1000 页"的读数直接矛盾（评审 T5）。
 */
import type { SystemLoad } from '@/api/tasks'
import { formatBytes, formatCount, formatDuration, formatPercent } from '@/lib/format'

import { loadTone, taskKindLabel } from '../shared/status'
import { RingGauge } from '../shared/composites'

/**
 * 环的**可访问名**。`role="img"` 会把环里的 `<text>` 当装饰（读屏器读不到），
 * 所以读数必须进名字里；没有读数时名字只留指标名，不给一个孤零零的"—"。
 *
 * 名字里的读数与环里**逐字相同**（含 `12.3%` 这种小数位与 `0 / 1` 这种斜杠写法）：
 * 读屏器听到的与屏幕上看到的处处一样，配比写法全站一种。
 */
function ringLabel(name: string, reading: string): string {
  return reading === '' || reading === '—' || reading === '…' ? name : `${name} ${reading}`
}

export function LoadPanel({ load, live }: { load: SystemLoad | null; live?: boolean }) {
  const hardware = load?.hardware ?? null
  const queue = load?.queue ?? null
  const quota = load?.quota ?? null
  /** 首屏还没拿到数据：**照常画出骨架结构**，不整块消失（否则下面的内容会被推下去）。 */
  const pendingData = load === null

  const cpuPercent = hardware?.cpu_percent ?? null
  const memoryPercent = hardware?.memory_percent ?? null
  const quotaPercent =
    quota?.configured && quota.daily_quota > 0 ? (quota.pages_used / quota.daily_quota) * 100 : null
  const quotaRatio = quotaPercent === null ? 0 : Math.min(quotaPercent / 100, 1)

  /**
   * 并发槽位：环里的读数是 `在跑 / 槽位`，弧按同一个比值画。
   * 槽位为 0 时给"—"：那时没有分母，写 `0 / 0` 会被读成"一个都没占用"。
   *
   * 斜杠两侧**带空格**，与任务页的「`66 / 89` 项」「第 `1 / 4` 页」、对话页的
   * 「`已用 1,234 / 32,768` tokens」同一种写法（全站一种）。
   */
  const slotsText =
    queue && queue.slots > 0 ? `${formatCount(queue.running)} / ${formatCount(queue.slots)}` : '—'
  const slotsRatio = queue && queue.slots > 0 ? queue.running / queue.slots : 0

  /**
   * 本进程常驻内存占机器内存的比例（0–1）。给这个比例是为了让这一格回答的问题从
   * "这个数是什么"变成"它算不算大"——0.4% 一眼就知道不是瓶颈。
   * 环里放不下字节数（`185.9 MB` 会溢出一圈 48px 的环），所以环放比例、下面那行放两个原始值。
   */
  const processShare =
    hardware && hardware.memory_total_bytes > 0 && hardware.process_rss_bytes !== null
      ? (hardware.process_rss_bytes / hardware.memory_total_bytes) * 100
      : null
  // 一位小数交给 formatPercent：这一档常态小于 10%，取整会把 0.6% 写成 1%
  const processShareText = formatPercent(processShare)

  const cpuTone = loadTone(cpuPercent)
  const memoryTone = loadTone(memoryPercent, 85, 93)
  /** 额度用尽用 warn 而不是 danger：**它不是故障**，是"变慢"的原因。 */
  const quotaTone = quota?.exhausted ? 'warning' : 'accent'
  /** 槽位占用：跑满了才提示——这正是"该抬并发"的信号。 */
  const slotsTone =
    queue && queue.slots > 0 && queue.running >= queue.slots && queue.pending > 0
      ? 'warning'
      : 'accent'
  /** 本进程吃满机器内存同样是要处置的事（重启/查漏），与另两格的阈值口径一致。 */
  const processTone = loadTone(processShare, 85, 93)

  /**
   * 在跑数**超过**上限。真会出现：进程被 kill 时它手上的任务还写着 running，
   * 租约到期才回收——在到期之前库里确实有"比消费者还多"的运行中任务。
   * 要说明白，否则"上限 1 却有 2 个在跑"读起来像面板算错了。
   */
  const oversubscribed = queue !== null && queue.slots > 0 && queue.running > queue.slots

  /** 排队任务按类型分布，按条数降序——"积压全是出题"这个结论要一眼看得出来。 */
  const pendingKinds = Object.entries(queue?.pending_by_kind ?? {})
    .map(([kind, count]) => ({ kind, count, label: taskKindLabel(kind) }))
    .sort((left, right) => right.count - left.count)

  const oldestSeconds = queue?.oldest_pending_seconds ?? null
  const oldestWait = oldestSeconds === null ? null : formatDuration(oldestSeconds)

  /** 有没有需要解释的异常状态（停滞 / 逾期）。没有就把那一行整个省掉。 */
  const problems: string[] = []
  if (queue && queue.stalled > 0) problems.push(`${formatCount(queue.stalled)} 个任务可能卡住`)
  if (queue && queue.overdue > 0)
    problems.push(`${formatCount(queue.overdue)} 个任务长时间未被领取`)

  return (
    <section className="m-load" aria-label="运行负载">
      <header className="m-load-head">
        <h2 className="m-load-title">运行负载</h2>
        {live && <span className="m-load-live">实时刷新中</span>}
      </header>

      <div className="m-gauges">
        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <RingGauge
              ratio={(cpuPercent ?? 0) / 100}
              label={formatPercent(cpuPercent)}
              tone={cpuTone}
              ariaLabel={ringLabel('CPU 使用率', formatPercent(cpuPercent))}
            />
          </span>
          <span className="m-gauge-name">CPU</span>
          <span className="m-gauge-detail tabular">
            {hardware
              ? `${formatCount(hardware.cpu_count)} 核${cpuPercent === null ? ' · 采样中' : ''}`
              : '—'}
          </span>
        </div>

        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <RingGauge
              ratio={(memoryPercent ?? 0) / 100}
              label={formatPercent(memoryPercent)}
              tone={memoryTone}
              ariaLabel={ringLabel('内存使用量', formatPercent(memoryPercent))}
            />
          </span>
          <span className="m-gauge-name">内存</span>
          <span className="m-gauge-detail tabular">
            {hardware
              ? `${formatBytes(hardware.memory_used_bytes)} / ${formatBytes(hardware.memory_total_bytes)}`
              : '—'}
          </span>
        </div>

        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <RingGauge
              ratio={slotsRatio}
              label={slotsText}
              tone={slotsTone}
              ariaLabel={ringLabel('并发槽位占用', slotsText)}
            />
          </span>
          <span className="m-gauge-name">并发槽位</span>
          {/* 环里已经写了「在跑 / 槽位」，这一行就只剩队列深度——同一个数不写两遍 */}
          <span className="m-gauge-detail">
            {queue ? (
              <>
                排队 <strong className="tabular">{formatCount(queue.pending)}</strong> 条
              </>
            ) : (
              '—'
            )}
          </span>
        </div>

        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <RingGauge
              ratio={quotaRatio}
              label={pendingData ? '…' : formatPercent(quotaPercent)}
              tone={quotaTone}
              ariaLabel={ringLabel(
                '云端解析今日页数',
                pendingData ? '…' : formatPercent(quotaPercent),
              )}
            />
          </span>
          <span className="m-gauge-name">云端解析额度</span>
          <span className="m-gauge-detail tabular">
            {pendingData
              ? '读取中…'
              : quota?.configured
                ? `${formatCount(quota.pages_used)} / ${formatCount(quota.daily_quota)} 页`
                : '未配置'}
          </span>
        </div>

        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <RingGauge
              ratio={processShare === null ? 0 : processShare / 100}
              label={processShareText}
              tone={processTone}
              ariaLabel={ringLabel('本进程常驻内存占机器内存', processShareText)}
            />
          </span>
          {/* 一个数就该只有一个数：原先这里挂着 ⓘ 解释"为什么它会涨"（已删） */}
          <span className="m-gauge-name">本进程常驻内存</span>
          {/* 环里是占机器内存的比例，这一行给两个原始值——分母写在明处，比例才读得出大小 */}
          <span className="m-gauge-detail tabular">
            {hardware
              ? `${formatBytes(hardware.process_rss_bytes)} / ${formatBytes(hardware.memory_total_bytes)}`
              : '—'}
          </span>
        </div>
      </div>

      {/* 排队构成与最久的等待：只在真有积压时出现（平时排队是 0，常驻就是噪音） */}
      {queue && queue.pending > 0 && (pendingKinds.length > 0 || oldestWait) && (
        <p className="m-load-note">
          {pendingKinds.length > 0 && (
            <>
              排队的构成：
              {pendingKinds.map((item) => `${item.label} ${formatCount(item.count)}`).join('、')}
            </>
          )}
          {oldestWait && (
            <>
              {pendingKinds.length > 0 && '，'}最久的已等 <strong>{oldestWait}</strong>
            </>
          )}
        </p>
      )}

      {oversubscribed ? (
        <p className="m-load-hint">刚被中断的任务会在约一分钟内回到队列。</p>
      ) : (
        queue &&
        queue.pending > 0 &&
        queue.running >= queue.slots && (
          <p className="m-load-hint">槽位已占满，后面的要等前一个跑完。</p>
        )
      )}
      {quota?.configured && quota.exhausted ? (
        <p className="m-load-hint">
          额度已用尽：解析会明显变慢（<strong>不是失败</strong>）。
        </p>
      ) : (
        !pendingData &&
        quota &&
        !quota.configured && (
          <p className="m-load-hint">未配置云端解析令牌：所有文件都走本地解析，不消耗额度。</p>
        )
      )}

      {problems.length > 0 && <p className="m-load-problem">{problems.join('；')}</p>}
    </section>
  )
}
