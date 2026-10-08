/**
 * 复制到剪贴板（开发计划 §12.205）。
 *
 * 起因是一条实测：在应用内浏览器里点代码块的复制按钮，**四条提示全是
 * "复制失败"**。查下来不是权限、不是 http——`isSecureContext` 是 `true`，
 * `navigator.clipboard.writeText` 也在，真正的报错是：
 *
 *     NotAllowedError: Failed to execute 'writeText' on 'Clipboard':
 *     Document is not focused.
 *
 * 异步剪贴板 API **要求文档处于聚焦状态**。窗口失焦是常态：鼠标在另一块屏幕上、
 * 焦点在侧栏或终端里、从别的应用切回来还没点页面。而用户明明点了按钮，
 * 凭什么复制不了——按钮点得动，就该复制得上。
 *
 * 所以这里分两级，**先干净的后兜底**：
 *
 * 1. `navigator.clipboard.writeText`：安全上下文里的正路，异步、不碰 DOM；
 * 2. 失败（失焦 / 权限策略 / 老内核）退回 `document.execCommand('copy')`：
 *    同一个环境下实测返回 `true`，并且**真的写进了系统剪贴板**（用系统级
 *    「读剪贴板」核对过，不是只看返回值）。
 *
 * 两条都失败时返回 `false`，收场交给调用方——代码块与表格那边会调
 * `selectNode` 把内容替用户选中，他按一下 Ctrl+C 就完事。这比原样甩一句
 * "请手动选中后复制"多说一句"我已经替你选好了"，而那一句正是他刚才白点的原因。
 */

/** 当前是否**已经**选中了某段内容（浏览器不支持时按否处理）。 */
function currentRange(): Range | null {
  const selection = typeof document.getSelection === 'function' ? document.getSelection() : null
  if (!selection || selection.rangeCount === 0) return null
  return selection.getRangeAt(0)
}

/** 把用户原来的选中状态还回去（点了复制不该顺手抹掉他选中的东西）。 */
function restoreRange(range: Range | null): void {
  if (!range) return
  const selection = document.getSelection()
  if (!selection) return
  selection.removeAllRanges()
  selection.addRange(range)
}

/**
 * 兜底那条路：临时塞一个不可见的 `textarea`，选中它，执行 `execCommand`。
 *
 * **不能 `display:none`**：隐藏的元素 `select()` 选不中，`execCommand` 也拿不到内容。
 * 所以是「挂在视口里、1px、透明」——不闪、不挤布局，但浏览器认它是个可选中元素。
 *
 * `readonly` + `setSelectionRange` 是给移动端 Safari 的：它的 `select()`
 * 对可编辑元素会拉起键盘。桌面端这两句无害。
 */
function execCommandCopy(text: string): boolean {
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '0'
  area.style.left = '0'
  area.style.width = '1px'
  area.style.height = '1px'
  area.style.padding = '0'
  area.style.border = 'none'
  area.style.outline = 'none'
  area.style.background = 'transparent'
  area.style.opacity = '0'

  const previous = currentRange()
  document.body.appendChild(area)
  area.select()
  area.setSelectionRange(0, text.length)
  let copied: boolean
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  } finally {
    area.remove()
    restoreRange(previous)
  }
  return copied
}

/**
 * 复制一段文本，返回**是否真的进了剪贴板**。
 *
 * 空字符串直接返回 `false`：代码块取不到文本时（`pre` 还没渲染完）不该
 * 报"复制成功"——那会让用户去别处粘一下才发现是空的。
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 失焦 / 权限策略 / 老内核——都不是"复制不了"的理由，换下一条路
    }
  }
  return execCommandCopy(text)
}

/**
 * 两级都失败时的收场：**把这段内容替用户选中**，他按 Ctrl+C 就拿到了。
 *
 * 选中是能兑现的承诺（内容已经在浏览器自己的选区里，复制是本地操作、
 * 不再经过任何权限），而"请手动选中"是把刚才那件事原样退回给用户。
 */
export function selectNode(node: Element | null): boolean {
  if (!node || typeof document.createRange !== 'function') return false
  const selection = document.getSelection()
  if (!selection) return false
  const range = document.createRange()
  range.selectNodeContents(node)
  // 空节点选出来是个塌缩区间（一个字符都没选中）。那不是"已替你选中"，
  // 别给一句兑现不了的承诺
  if (range.collapsed) return false
  selection.removeAllRanges()
  selection.addRange(range)
  return true
}
