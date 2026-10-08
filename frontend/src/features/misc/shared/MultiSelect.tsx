/**
 * 多选（勾选式），用于"到点查哪些知识库"这一格。
 *
 * 旧前端这里是个下拉浮层（`AppMultiSelect`）。这里换成**平铺的勾选行**：候选通常
 * 只有几个（自己建的库），浮层要自己补定位、键盘、点外关闭三件事，而平铺出来
 * 每一项都是原生 checkbox——键盘可达、Tab 顺序正确、"已选几个"一眼可见。
 * 库多的时候它会自动换行，不会把弹窗撑高。
 */
import type { SelectOption } from './composites'

export function MultiSelect({
  options,
  value,
  onChange,
  emptyText,
  label,
}: {
  options: readonly SelectOption[]
  value: readonly string[]
  onChange: (next: string[]) => void
  emptyText: string
  label: string
}) {
  if (options.length === 0) return <p className="text-micro">{emptyText}</p>

  const toggle = (key: string) => {
    onChange(value.includes(key) ? value.filter((item) => item !== key) : [...value, key])
  }

  return (
    <ul className="m-kb-picks" aria-label={label}>
      {options.map((option) => {
        const on = value.includes(option.value)
        return (
          <li key={option.value}>
            <button
              type="button"
              className={on ? 'm-kb-pick m-kb-pick-on' : 'm-kb-pick'}
              aria-pressed={on}
              onClick={() => toggle(option.value)}
            >
              <span>{option.label}</span>
              {on && <span className="m-kb-pick-mark">已选</span>}
            </button>
          </li>
        )
      })}
    </ul>
  )
}
