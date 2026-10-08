/**
 * 产品标识（环行星 + 小写 "kylab"）——旧 `components/icons/IconLogo.vue` 的 React 版，
 * 几何数值**逐个照抄**（那是量出来的：对得像不像记在开发计划 §12.209）。
 *
 * 两处要点，搬过来时一字未改：
 *
 * 1. **跟文字颜色**：全描边都是 `currentColor`——浅色主题近黑、深色近白。
 *    固定的位图在深色主题下就是一块脏东西；
 * 2. **小尺寸要光学校正**：原设计的笔画相对标高极细（外圆 5.5/299），
 *    等比缩到 22px 只剩 0.4px，渲染出来是一片淡灰。所以每根线有一个
 *    **渲染像素下限**（外圆与环上小圆 1.15px、环 0.8px）。
 */
const VARIANTS = {
  wordmark: { viewBox: '0 0 1128 303', ratio: 1128 / 303 },
  mark: { viewBox: '0 0 370 299', ratio: 370 / 299 },
} as const

/** 行星标自己的画布高度，光学校正用它把"渲染像素"换回"用户单位"。 */
const MARK_HEIGHT = 299
/** 组合里字标相对行星的落点（原图像素，量出来的）。 */
const WORDMARK_AT = { x: 437, y: 28 }
/** 组合里行星的落点：它在自己的画布里从 x=1 起笔，所以往左挪 1。 */
const MARK_AT = { x: -1, y: 0 }

export function Logo({
  size = 20,
  variant = 'wordmark',
  label = 'KYLAB 知识库',
}: {
  size?: number
  variant?: 'wordmark' | 'mark'
  label?: string
}) {
  const box = VARIANTS[variant]
  /** 光学校正：尺寸小的时候返回按下限反推的笔宽，尺寸大时回到原设计比例。 */
  const stroke = (base: number, minPixels: number): number =>
    Math.max(base, (minPixels * MARK_HEIGHT) / size)

  return (
    <svg
      width={Math.round(size * box.ratio)}
      height={size}
      viewBox={box.viewBox}
      fill="none"
      stroke="currentColor"
      role="img"
      aria-label={label}
      focusable="false"
    >
      <g transform={variant === 'wordmark' ? `translate(${MARK_AT.x} ${MARK_AT.y})` : undefined}>
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
      </g>

      {variant === 'wordmark' ? (
        <g
          transform={`translate(${WORDMARK_AT.x} ${WORDMARK_AT.y})`}
          strokeWidth={21.5}
          strokeLinecap="butt"
        >
          <path d="M11.5 1V212" />
          <path d="M24 157L107 70" />
          <path d="M47 131L116 214" />
          <path d="M144.8 67L205 210.4" />
          <path d="M268.7 67L177.3 273" />
          <path d="M315.5 1V212" />
          <circle cx="430" cy="140.5" r="63.5" />
          <path d="M492 67V213" />
          <path d="M553 1V212" />
          <circle cx="615.5" cy="140.5" r="63.5" />
        </g>
      ) : null}
    </svg>
  )
}
