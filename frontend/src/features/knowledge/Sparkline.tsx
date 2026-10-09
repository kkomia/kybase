/**
 * 迷你趋势图（手写 SVG，不引图表库）。
 *
 * 三件事说清楚：
 *
 * 1. **高度固定**：图形高度是常量，数据少的库与数据多的库画出来一样高——
 *    卡片网格里"一行高一行矮"是最扎眼的毛病，而"这个库最近没进东西"本身
 *    就是这张图要说的话（一根压在底部的平线），不该靠把图缩小来表达；
 * 2. **宽度让 CSS 给，坐标不重算**：`viewBox` 横轴一天一列，
 *    `preserveAspectRatio="none"` 把它拉伸到卡片宽，`vector-effect:
 *    non-scaling-stroke` 把线宽拉回设备像素——否则拉伸之后线会跟着变粗变细；
 * 3. **纵轴按这一天里自己那条线的峰值归一**：卡片上的读数是"近 90 天入库 N 篇" /
 *    "近 90 天检索命中 N 条"，峰值只决定形状。要跨库比大小看的是那个数，不是线的高低。
 *
 * 颜色全部在 `knowledge.css` 的 `.kb-spark*` 里引用主题变量（深浅两套主题同一句话）；
 * 卡片上第二根线（检索命中）靠 `.kb-trend-hits` 换一档副色，**不是**在这里传色值。
 */

/** 画布高（结构常量，与列宽同类，不走 `--space-*` 阶梯；CSS 那边给的是同一个数）。 */
const HEIGHT = 28

/** 上下各留一点，峰顶与 0 线都不会被裁掉半个线宽。 */
const PAD_Y = 3

/** 坐标只留两位小数：`viewBox` 里再精确也画不出来，path 短一点更好读。 */
function round(value: number): number {
  return Math.round(value * 100) / 100
}

export function Sparkline({
  series,
  className,
}: {
  series: readonly number[]
  className?: string
}) {
  const columns = Math.max(series.length, 1)
  // 全是 0 时用 1 当分母：曲线落在最下面那条 0 线上，而不是除出 NaN
  const peak = Math.max(...series, 0) || 1
  const inner = HEIGHT - PAD_Y * 2

  const line = series
    .map((value, index) => {
      const x = round(index + 0.5)
      const y = round(HEIGHT - PAD_Y - (inner * value) / peak)
      return `${index === 0 ? 'M' : 'L'}${x} ${y}`
    })
    .join(' ')

  // 面积是线本身 + 两端垂到底边的封口：一整块的节奏比一根细线好读
  const area = line ? `${line} L${round(columns - 0.5)} ${HEIGHT} L0.5 ${HEIGHT} Z` : ''

  const classes = ['kb-spark', series.some((value) => value > 0) ? '' : 'kb-spark-empty', className]
    .filter(Boolean)
    .join(' ')

  return (
    // 图形本身不带文字：同一件事已经由旁边那个读数（「近 90 天入库 N 篇」/「近 90 天
    // 检索命中 N 条」）说清楚了，屏读用户在卡片链路上听到的就是那句（重复念一遍是噪音）
    <svg
      className={classes}
      viewBox={`0 0 ${columns} ${HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      {area ? <path className="kb-spark-area" d={area} /> : null}
      {line ? <path className="kb-spark-line" d={line} /> : null}
    </svg>
  )
}
