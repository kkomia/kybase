/**
 * 剪贴板（`lib/clipboard.ts`）——两级兜底那套契约的用例。
 *
 * 这个文件的头注释记着一条真实事故：应用内浏览器里点代码块的复制按钮，
 * **四条提示全是"复制失败"**，根因是异步剪贴板要求文档聚焦。所以这里的几条用例
 * 钉的正是"按钮点得动就该复制得上"：
 *
 * 1. 空字符串不发"复制成功"；
 * 2. 异步剪贴板能走就走，不走 DOM；
 * 3. 它被拒（失焦 / 权限策略 / 老内核）→ **退回 execCommand 兜底**；
 * 4. 两级都失败 → `false`，且**不留临时 textarea**（异常路径也一样）；
 * 5. 没有异步剪贴板的老内核 → 直接用兜底那条。
 *
 * jsdom 没实现 `document.execCommand` / `navigator.clipboard` / `textarea.select`，
 * 这三件在本文件里各替一份（与 `tests/setup.ts` 补 ResizeObserver 同一类做法）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { copyText, selectNode } from '@/lib/clipboard'

const writeText = vi.fn()
const execCommand = vi.fn()

function setClipboard(value: unknown): void {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true, writable: true })
}

beforeEach(() => {
  writeText.mockReset()
  execCommand.mockReset()
  setClipboard({ writeText })
  Object.defineProperty(document, 'execCommand', {
    value: execCommand,
    configurable: true,
    writable: true,
  })
  // jsdom 没有这两个方法（元素本身有，方法不存在）
  Object.defineProperty(HTMLTextAreaElement.prototype, 'select', {
    value: vi.fn(),
    configurable: true,
    writable: true,
  })
  Object.defineProperty(HTMLTextAreaElement.prototype, 'setSelectionRange', {
    value: vi.fn(),
    configurable: true,
    writable: true,
  })
})

afterEach(() => {
  // 替身要收回去：同一次运行里别的用例文件共用这份 document / navigator
  Reflect.deleteProperty(document, 'execCommand')
  setClipboard(undefined)
})

describe('copyText', () => {
  it('空字符串直接返回 false：不报"复制成功"让用户去别处才发现是空的', async () => {
    expect(await copyText('')).toBe(false)
    expect(writeText).not.toHaveBeenCalled()
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('异步剪贴板可用时走它，不碰 DOM', async () => {
    writeText.mockResolvedValue(undefined)
    expect(await copyText('hello')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('hello')
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('失焦被拒时退回 execCommand —— 按钮点得动就该复制得上', async () => {
    writeText.mockRejectedValue(new DOMException('Document is not focused.', 'NotAllowedError'))
    execCommand.mockReturnValue(true)
    expect(await copyText('hello')).toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
  })

  it('两级都失败返回 false，且不留临时 textarea', async () => {
    writeText.mockRejectedValue(new Error('nope'))
    execCommand.mockReturnValue(false)
    expect(await copyText('hello')).toBe(false)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('execCommand 抛异常也返回 false，同样不留 textarea（异常不许漏给调用方）', async () => {
    writeText.mockRejectedValue(new Error('nope'))
    execCommand.mockImplementation(() => {
      throw new Error('boom')
    })
    expect(await copyText('hello')).toBe(false)
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('老内核没有异步剪贴板时，直接用兜底那条', async () => {
    setClipboard(undefined)
    execCommand.mockReturnValue(true)
    expect(await copyText('hello')).toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
  })
})

describe('selectNode（两级都失败后的收场）', () => {
  it('空节点选出来是塌缩区间 → false，不给兑现不了的"已替你选中"', () => {
    expect(selectNode(document.createElement('div'))).toBe(false)
  })

  it('有内容时真的选中它', () => {
    const node = document.createElement('div')
    node.textContent = 'hello'
    document.body.appendChild(node)
    expect(selectNode(node)).toBe(true)
    expect(document.getSelection()?.toString()).toBe('hello')
    node.remove()
  })
})
