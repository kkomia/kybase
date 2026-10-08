/**
 * 数值滑杆（旧 `components/ui/RangeField.vue` 的同名实现，《前端设计规范》§7.4）。
 *
 * 用在"有推荐值、但精确到个位没意义"的参数上（块长 / 块重叠）：滑杆把**范围**与
 * **常用值落在哪**直接画出来，右侧还有常驻读数。
 *
 * 六条约定照搬旧实现，其中三条是踩过坑的，必须在 React 里同样成立：
 *
 * 1. **刻度点 = 常用值**：越界的自动丢掉（重叠的上限跟着块长变，块长 128 时
 *    传进来的 256 必须消失，否则点会画到轨道外面）。
 * 2. **默认值那个点画成强调色**；点只铺不点（`pointer-events: none`），**绝不能挡住拖动**。
 * 3. **吸附只认指针拖动**（`snapToMarks`）：键盘**不吸**。站在 1024 上按方向键得到 1025，
 *    一吸就被拽回去，用户再也走不出这个刻度。数字框同样不吸——手打 1000 是明确意图。
 * 4. 读数可编辑（`editableValue`）时，**输入过程允许暂时非法**（打「1024」时先出现「1」），
 *    所以框里存的是自己的草稿字符串；**失焦 / 回车才提交**，能解析就按 `[min, max]`
 *    夹一下，解析不出来就回显当前值（等于这次输入没发生）。
 * 5. 拖滑杆来的变化**不回写正在打字的草稿**，否则会把用户手里的字冲掉。
 * 6. 磁力半径是量程的 3%（约 ±12px）：手能感觉到，又不至于把刻度之间的值整段吃掉。
 */
import { useEffect, useRef, useState } from 'react'

export interface RangeMark {
  value: number
  primary?: boolean
}

interface RangeFieldProps {
  value: number
  onChange: (value: number) => void
  min: number
  max: number
  step?: number
  id?: string
  marks?: readonly RangeMark[]
  ariaLabel?: string
  /** 拖动时靠近刻度就吸附过去。键盘与数字框不受影响（见顶部第 3 条）。 */
  snapToMarks?: boolean
  /** 右侧读数改成可输入的数字框。 */
  editableValue?: boolean
  /** 数字框的名字：外层 `<label for>` 指的是滑杆，管不到这个框。 */
  valueLabel?: string
}

/** 吸附的磁力半径：量程的比例。 */
const SNAP_RATIO = 0.03

export function RangeField({
  value,
  onChange,
  min,
  max,
  step = 1,
  id,
  marks = [],
  ariaLabel,
  snapToMarks = false,
  editableValue = false,
  valueLabel,
}: RangeFieldProps) {
  /** 指针正按在滑杆上：只有这时才吸附。 */
  const [dragging, setDragging] = useState(false)
  /** 数字框里的原始文本（见顶部第 4 条）。 */
  const [draft, setDraft] = useState(String(value))
  /** 正在这个框里打字：此时从外面（拖滑杆）来的变化不回写草稿。 */
  const editing = useRef(false)

  useEffect(() => {
    if (!editing.current) setDraft(String(value))
  }, [value])

  const items = marks
    .filter((mark) => mark.value >= min && mark.value <= max)
    .map((mark) => {
      const span = max - min
      const ratio = span <= 0 ? 0 : (mark.value - min) / span
      return {
        ...mark,
        // 与 `--kb-range-thumb` 同一个口径：滑块中心走 [半滑块, 宽度 − 半滑块]
        left: `calc((100% - var(--kb-range-thumb)) * ${ratio} + var(--kb-range-thumb) / 2)`,
      }
    })

  /** 靠近某个刻度就返回那个刻度，否则原样返回。 */
  function snapped(raw: number): number {
    if (!snapToMarks || !dragging) return raw
    const radius = (max - min) * SNAP_RATIO
    let nearest = raw
    let distance = Number.POSITIVE_INFINITY
    for (const item of items) {
      const gap = Math.abs(item.value - raw)
      if (gap < distance) {
        distance = gap
        nearest = item.value
      }
    }
    return distance <= radius ? nearest : raw
  }

  /** 失焦 / 回车提交：能解析就夹到 `[min, max]`，解析不出来回显当前值。 */
  function commit(): void {
    editing.current = false
    const text = draft.trim()
    const parsed = text === '' ? Number.NaN : Number(text)
    if (!Number.isFinite(parsed)) {
      setDraft(String(value))
      return
    }
    const next = Math.min(max, Math.max(min, Math.round(parsed)))
    onChange(next)
    setDraft(String(next))
  }

  return (
    <div className="kb-range">
      <div className={['kb-range-col', items.length > 0 ? 'kb-range-col-marked' : ''].join(' ')}>
        <div className="kb-range-rail">
          <input
            id={id}
            className="kb-range-input"
            type="range"
            min={min}
            max={max}
            step={step}
            value={value}
            aria-label={ariaLabel}
            onChange={(event) => onChange(snapped(Number(event.target.value)))}
            onPointerDown={() => setDragging(true)}
            onPointerUp={() => setDragging(false)}
            onPointerCancel={() => setDragging(false)}
            onBlur={() => setDragging(false)}
          />
          {items.map((mark) => (
            <span
              key={mark.value}
              className={['kb-range-mark', mark.primary ? 'kb-range-mark-primary' : '']
                .filter(Boolean)
                .join(' ')}
              style={{ left: mark.left }}
              aria-hidden="true"
            />
          ))}
        </div>
        {items.map((mark) => (
          <span
            key={`label-${mark.value}`}
            className={['kb-range-mark-label', mark.primary ? 'kb-range-mark-label-primary' : '']
              .filter(Boolean)
              .join(' ')}
            style={{ left: mark.left }}
            aria-hidden="true"
          >
            {mark.value}
          </span>
        ))}
      </div>

      {editableValue ? (
        <input
          id={id ? `${id}-value` : undefined}
          className="kb-range-number tabular"
          type="number"
          min={min}
          max={max}
          step={step}
          value={draft}
          aria-label={valueLabel}
          onChange={(event) => setDraft(event.target.value)}
          onFocus={() => {
            editing.current = true
          }}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              // **不能让这个回车继续冒泡**：外层弹窗用「回车 = 保存」的便利，
              // 而 React 的 setState 是批处理的——`commit()` 刚写下的数字要到本轮
              // 事件处理结束才落到父组件的 state 上，冒泡上去的 save() 读到的仍是旧值
              // （表现为"回车一下，弹窗关了、数字没保存"）。旧 Vue 实现靠 ref 的同步读
              // 恰好绕过了这一点，React 里没有等价物，所以这里明确分工：
              // 数字框里的回车只提交这个数字，保存由底部的「保存并关闭」负责。
              event.stopPropagation()
              commit()
            }
          }}
        />
      ) : (
        <output className="kb-range-value tabular">{value}</output>
      )}
    </div>
  )
}
