/**
 * 运行负载面板（任务中心顶部，§12.115）——与旧前端 `components/tasks/LoadPanel.vue` 对应。
 *
 * **要回答的是"后台为什么慢"**，而这个问题在任务列表里找不到答案：列表说的是
 * "每个任务怎么了"，慢的原因却常常与任何单个任务无关——是 CPU 满了、并发槽位只有 1 个、
 * 还是云端额度用尽在降级排队。这一格把那些数摊开。
 *
 * **五格各有各的形**（评审 T4 那一版是"五格同一种控件"，五个环排成一列）：一律画环
 * 既看不出哪一格最要紧，也把"百分比""槽位""余量"这些性质不同的东西画成了同一种刻度。
 * 现在每一格的形都对着它真正要回答的问题：
 *
 * - CPU —— 细环：单个百分比，环是最省地方、又一眼看得出"满没满"的形；
 * - 内存 —— 水平条形 + 右端读数：条给体量感，右边那个数给刻度（`已用 / 总量` 在下一行）；
 * - 并发槽位 —— 大号数字 + 槽位方块：槽位是个位数（`KYLAB_WORKER_CONCURRENCY`），
 *   一格一个槽位可以**数**出还剩几个位置，而环上那条弧只能心算；
 * - 云端解析额度 —— 分格进度条：这一格回答的是"还剩多少"（用尽就降级排队），
 *   12 格比一根连续的条更容易扫出余量；
 * - 本进程常驻内存 —— 纯数字卡：这个比例常态在 10% 以下，画成条永远是一条几乎空着的槽，
 *   图形不携带信息，数字才携带。
 *
 * 形态不同，**读数与文案逐字不变**（"20 核"、"5.3 GB / 15.2 GB"、"排队 0 条"），
 * 且每一格仍然只有一个**带名字的图形**：`role="img"` 会把图形里的文字当装饰，
 * 所以读数必须写进图形的可访问名里（见 `gaugeLabel`），读屏器听到的与屏幕上看到的
 * 处处一样。每格的结论都对应一种**可操作**的判断：系统负载 → 降并发；
 * 任务并发 → 抬 KYLAB_WORKER_CONCURRENCY；进程内存 → 重启/查漏；
 * 云端额度 → 等次日额度或换本地解析。
 *
 * **CPU 可能是"—"**：后端按两次采样之差算，第一次问就是没有差值。
 * 这不是缺失，界面上如实显示"—"而不是画一根 0% 的环（那会被读成"机器很空闲"）。
 * 0 值**不画进度**——环、条、格子三处同一口径：`strokeDasharray="0 C"` 配圆头会画出一个
 * 圆点，0 宽度的填充也会留下一小段，与"0 / 1000 页"的读数直接矛盾（评审 T5）。
 */
import type { SystemLoad } from '@/api/tasks'
import { formatBytes, formatCount, formatDuration, formatPercent } from '@/lib/format'

import { loadTone, taskKindLabel } from '../shared/status'
import { RingGauge } from '../shared/composites'

/** 三档语义色，环 / 条 / 格子共用（与 `RingGauge` 的 `tone` 同一套取值）。 */
type GaugeTone = 'accent' | 'warning' | 'danger'

/** 色值只在主题里：这里一个色值都不写，深浅两套主题由令牌自己跟。 */
const TONE_FILL: Record<GaugeTone, string> = {
  accent: 'var(--accent)',
  warning: 'var(--status-warning)',
  danger: 'var(--status-danger)',
}

/**
 * 纯数字那格的文字色：**正常时不换色**。20px 的品牌蓝数字看着像链接/可点，
 * 而"没什么问题"本来就该是正文色；只有要处置的两档才上语义色。
 */
const TONE_TEXT: Record<GaugeTone, string> = { ...TONE_FILL, accent: 'var(--text-primary)' }

/** 额度那格分几格：12 档的"电池"读法——扫得出余量，又不至于细到读不清。 */
const QUOTA_SEGMENTS = 12

/**
 * 图形的**可访问名**。`role="img"` 会把图形里的 `<text>` / 数字当装饰（读屏器读不到），
 * 所以读数必须进名字里；没有读数时名字只留指标名，不给一个孤零零的"—"。
 *
 * 名字里的读数与屏幕上**逐字相同**（含 `12.3%` 这种小数位与 `0 / 1` 这种斜杠写法）：
 * 读屏器听到的与看到的处处一样，配比写法全站一种。
 */
function gaugeLabel(name: string, reading: string): string {
  return reading === '' || reading === '—' || reading === '…' ? name : `${name} ${reading}`
}

/**
 * 内存：水平条形 + 右端读数。
 *
 * 条与数是同一个量的两种说法，所以并排放在一行里：条给"还剩多少"的体量，数给刻度。
 * 0 值不画填充（与环同一口径），否则"0%"会被读成"有一点点"。
 */
function BarMeter({
  ratio,
  reading,
  tone,
  ariaLabel,
}: {
  /** 0–1。 */
  ratio: number
  reading: string
  tone: GaugeTone
  ariaLabel: string
}) {
  const clamped = Math.min(Math.max(ratio, 0), 1)
  return (
    <span className="m-bar" role="img" aria-label={ariaLabel}>
      <span className="m-bar-track">
        {clamped > 0 && (
          <span
            className="m-bar-fill"
            style={{ width: `${clamped * 100}%`, background: TONE_FILL[tone] }}
          />
        )}
      </span>
      <span className="m-bar-reading tabular">{reading}</span>
    </span>
  )
}

/**
 * 并发槽位：大号数字 + 一格一个槽位。
 *
 * 亮格按**在跑数**数，并封顶在槽位总数上——在跑数确实可能一时超过上限（进程被 kill 时
 * 它手上的任务还写着 running，见下面的 `oversubscribed`）：那时"全亮"才是实话，
 * 位置全占着，多出来的还没被回收。
 */
function SlotCells({
  running,
  slots,
  reading,
  tone,
  ariaLabel,
}: {
  running: number
  slots: number
  reading: string
  tone: GaugeTone
  ariaLabel: string
}) {
  // 槽位为 0 时一格都不画：没有分母就不该有格子（读数是"—"）
  const cells = slots > 0 ? Array.from({ length: slots }, (_, index) => index < running) : []
  return (
    <span className="m-slots" role="img" aria-label={ariaLabel}>
      <span className="m-slots-figure tabular">{reading}</span>
      <span className="m-slots-row">
        {cells.map((filled, index) => (
          <span
            key={index}
            className={filled ? 'm-slot m-slot-on' : 'm-slot'}
            style={filled ? { background: TONE_FILL[tone] } : undefined}
          />
        ))}
      </span>
    </span>
  )
}

/**
 * 云端解析额度：分格进度条 + 百分比读数。
 *
 * 刻度化是刻意的：这一格回答的是"还剩多少"（额度用尽就降级排队），12 格比一根连续的条
 * 更容易一眼扫出余量。**0 页不点亮任何一格**；只要用了一点就至少点亮一格——
 * 否则"用了 1 页"与"用了 500 页"在刻度上分不开，等于把"有"画成"没有"（T5 的反面）。
 */
function QuotaMeter({
  reading,
  lit,
  tone,
  ariaLabel,
}: {
  reading: string
  /** 点亮几格（0–`QUOTA_SEGMENTS`）。 */
  lit: number
  tone: GaugeTone
  ariaLabel: string
}) {
  return (
    <span className="m-quota" role="img" aria-label={ariaLabel}>
      <span className="m-quota-reading tabular">{reading}</span>
      <span className="m-quota-row">
        {Array.from({ length: QUOTA_SEGMENTS }, (_, index) => (
          <span
            key={index}
            className={index < lit ? 'm-quota-cell m-quota-cell-on' : 'm-quota-cell'}
            style={index < lit ? { background: TONE_FILL[tone] } : undefined}
          />
        ))}
      </span>
    </span>
  )
}

/**
 * 本进程常驻内存：纯数字卡。
 *
 * 这一格的比例常态在 10% 以下，画成条永远是一条几乎空着的槽——图形不携带信息，
 * 数字才携带（`0.6%` 与 `60%` 是两件事）。只给一个数，外面框一层浅底小卡：
 * 大字号数字单落在 48px 的格子里会像没画完。
 */
function NumberCard({
  reading,
  tone,
  ariaLabel,
}: {
  reading: string
  tone: GaugeTone
  ariaLabel: string
}) {
  return (
    <span
      className="m-number-card tabular"
      role="img"
      aria-label={ariaLabel}
      style={{ color: TONE_TEXT[tone] }}
    >
      {reading}
    </span>
  )
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
  /**
   * 额度条点亮几格。刻度化的取整口径写在 `QuotaMeter` 上：0 页不点亮，用了一点至少点亮一格。
   */
  const quotaLit =
    quotaPercent === null || quotaPercent <= 0
      ? 0
      : Math.max(1, Math.round((Math.min(quotaPercent, 100) / 100) * QUOTA_SEGMENTS))

  /**
   * 并发槽位：读数是 `在跑 / 槽位`，方块按同一个数点亮。
   * 槽位为 0 时给"—"：那时没有分母，写 `0 / 0` 会被读成"一个都没占用"。
   *
   * 斜杠两侧**带空格**，与任务页的「`66 / 89` 项」「第 `1 / 4` 页」、对话页的
   * 「`已用 1,234 / 32,768` tokens」同一种写法（全站一种）。
   */
  const slotsText =
    queue && queue.slots > 0 ? `${formatCount(queue.running)} / ${formatCount(queue.slots)}` : '—'

  /**
   * 本进程常驻内存占机器内存的比例（0–1）。给这个比例是为了让这一格回答的问题从
   * "这个数是什么"变成"它算不算大"——0.4% 一眼就知道不是瓶颈。
   * 20px 的卡里放不下字节数（`185.9 MB` 会顶满一整个格子），所以卡放比例、下面那行放两个原始值。
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
        {/* CPU：整块面板里唯一的环——单个百分比最省地方的形 */}
        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <RingGauge
              ratio={(cpuPercent ?? 0) / 100}
              label={formatPercent(cpuPercent)}
              tone={cpuTone}
              ariaLabel={gaugeLabel('CPU 使用率', formatPercent(cpuPercent))}
            />
          </span>
          <span className="m-gauge-name">CPU</span>
          <span className="m-gauge-detail tabular">
            {hardware
              ? `${formatCount(hardware.cpu_count)} 核${cpuPercent === null ? ' · 采样中' : ''}`
              : '—'}
          </span>
        </div>

        {/* 内存：水平条形 + 右端的百分数 */}
        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <BarMeter
              ratio={(memoryPercent ?? 0) / 100}
              reading={formatPercent(memoryPercent)}
              tone={memoryTone}
              ariaLabel={gaugeLabel('内存使用量', formatPercent(memoryPercent))}
            />
          </span>
          <span className="m-gauge-name">内存</span>
          <span className="m-gauge-detail tabular">
            {hardware
              ? `${formatBytes(hardware.memory_used_bytes)} / ${formatBytes(hardware.memory_total_bytes)}`
              : '—'}
          </span>
        </div>

        {/* 并发槽位：大号数字 + 槽位方块 */}
        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <SlotCells
              running={queue?.running ?? 0}
              slots={queue?.slots ?? 0}
              reading={slotsText}
              tone={slotsTone}
              ariaLabel={gaugeLabel('并发槽位占用', slotsText)}
            />
          </span>
          <span className="m-gauge-name">并发槽位</span>
          {/* 上面已经写了「在跑 / 槽位」，这一行就只剩队列深度——同一个数不写两遍 */}
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

        {/* 云端解析额度：分格进度条 + 百分数 */}
        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <QuotaMeter
              reading={pendingData ? '…' : formatPercent(quotaPercent)}
              lit={quotaLit}
              tone={quotaTone}
              ariaLabel={gaugeLabel(
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

        {/* 本进程常驻内存：纯数字卡 */}
        <div className="m-gauge">
          <span className="m-gauge-glyph">
            <NumberCard
              reading={processShareText}
              tone={processTone}
              ariaLabel={gaugeLabel('本进程常驻内存占机器内存', processShareText)}
            />
          </span>
          {/* 一个数就该只有一个数：原先这里挂着 ⓘ 解释"为什么它会涨"（已删） */}
          <span className="m-gauge-name">本进程常驻内存</span>
          {/* 卡里是占机器内存的比例，这一行给两个原始值——分母写在明处，比例才读得出大小 */}
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
