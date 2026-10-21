/**
 * 产品标识 —— **环行星 + 小写 "kybase"**。
 *
 * 两种形态：
 *
 * - `mark`：只有那颗行星（环 + 外圆 + 环上的小点）。折叠侧栏、顶栏这类只有一格里塞得下的位置用它；
 * - `wordmark`：行星 + 文字字标。文字按当前字号**排出来**（系统字体、600、负字距），
 *   而不是画成矢量路径：`kybase` 需要 6 个字形的路径，手绘那套是给旧名 "kylab" 量的，
 *   改字重/字号时路径不会跟着变。文字排版在两个主题、任意缩放下都稳。
 *
 * 行星的几何数值**逐个照抄**旧实现（那是量出来的）：
 *
 * 1. **跟文字颜色**：描边都是 `currentColor`——浅色主题近黑、深色近白。
 *    固定的位图在深色主题下就是一块脏东西；
 * 2. **小尺寸要光学校正**：原设计的笔画相对标高极细（外圆 5.5/299），
 *    等比缩到 22px 只剩 0.4px，渲染出来是一片淡灰。所以每根线有一个
 *    **渲染像素下限**（外圆与环上小圆 1.15px、环 0.8px）。
 */
const MARK_VIEWBOX = '0 0 370 299'

/** 行星标自己的画布高度，光学校正用它把"渲染像素"换回"用户单位"。 */
const MARK_HEIGHT = 299

/** 字标相对行星高度：0.86 是让文字的重心与行星外圆对齐的那个比例。 */
const WORDMARK_SCALE = 0.86

function PlanetMark({
  size,
  label,
  className,
}: {
  size: number
  /** 给了才当图片读（装饰性用法传空串）。 */
  label?: string
  className?: string
}) {
  /** 光学校正：尺寸小的时候返回按下限反推的笔宽，尺寸大时回到原设计比例。 */
  const stroke = (base: number, minPixels: number): number =>
    Math.max(base, (minPixels * MARK_HEIGHT) / size)

  return (
    <svg
      width={Math.round((size * 370) / 299)}
      height={size}
      viewBox={MARK_VIEWBOX}
      fill="none"
      stroke="currentColor"
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : true}
      focusable="false"
      className={className}
    >
      <circle cx="182.5" cy="148.5" r="145.75" strokeWidth={stroke(5.5, 1.15)} />
      <ellipse
        cx="185.5"
        cy="155.5"
        rx="192"
        ry="44"
        transform="rotate(-18.5 185.5 155.5)"
        strokeWidth={stroke(2.2, 0.8)}
      />
      <circle cx="318.5" cy="147" r="13.75" strokeWidth={stroke(5.5, 1.15)} />
    </svg>
  )
}

export function Logo({
  size = 20,
  variant = 'wordmark',
  label = 'KYBASE 知识库',
}: {
  size?: number
  variant?: 'wordmark' | 'mark'
  label?: string
}) {
  if (variant === 'mark') {
    return <PlanetMark size={size} label={label || undefined} />
  }

  const text = Math.round(size * WORDMARK_SCALE)
  return (
    <span
      className="inline-flex items-center gap-[0.32em] text-current"
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
    >
      <PlanetMark size={size} />
      <span
        aria-hidden={label ? true : undefined}
        className="font-semibold lowercase whitespace-nowrap"
        style={{
          fontSize: `${text}px`,
          letterSpacing: '-0.035em',
          lineHeight: 1,
          // 让文字基线在视觉上贴着行星的水平中线（"kybase" 没有升部外的装饰）
          paddingBottom: `${Math.max(1, Math.round(size * 0.06))}px`,
        }}
      >
        kybase
      </span>
    </span>
  )
}
