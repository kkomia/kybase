/**
 * PowerPoint（.pptx）预览：`pptx-preview` 把每一页画成一个 DOM 舞台。
 *
 * 上游 API（v1.0.7 的 `dist/index.d.ts`）：
 *
 * ```ts
 * init(dom: HTMLElement, options: { width?: number; height?: number; mode?: 'list' | 'slide' }): PPTXPreviewer
 * previewer.preview(file: ArrayBuffer): Promise<unknown>
 * previewer.destroy(): void
 * ```
 *
 * 四件从上游实现（`dist/pptx-preview.es.js`）里读出来、文档里没写的事：
 *
 * 1. **`init` 会往容器里 `append` 一个 `.pptx-preview-wrapper`**，不是替换容器内容；
 *    同一个容器 init 两次会叠两层。所以重渲染必须自己先清空（本组件的做法）。
 * 2. **缩放是 init 时按 `width` 算死的**：`scale = viewPort.width / pptx.width`，
 *    `height` 只用来给 wrapper 定高 + `overflow-y: auto`。因此窗口/抽屉变宽了
 *    **必须重建预览器**，否则画布一直停在旧宽度（上游没有 resize 处理）。
 *    这也是本组件监听 `ResizeObserver` 的原因。
 * 3. **`preview` 只拒绝（reject），不抛同步错**：解析失败（不是 pptx、空文件、
 *    加密包……）会走到 catch，文案在那里给。
 * 4. **`destroy()` 只解事件总线，不清 DOM**（上游实现里就两行 `wt/bt('destroy')`），
 *    所以"松手"这件事由我们负责：清空容器。
 * 5. **舞台宽度要量容器的父节点，不能量容器自己**（2026-10-05 疯狂闪屏的根因）。
 *    容器是**滚动容器**（`.kylab-office`：`overflow: auto; max-height: 70vh`），11 页叠起来
 *    必然出竖直滚动条 ⇒ 它的 `clientWidth` 少了约 15px。而重渲染（第 1 条）第一件事是
 *    `host.innerHTML = ''` ⇒ 容器里空了 ⇒ 撑不到 70vh ⇒ 滚动条消失 ⇒ `clientWidth` 涨回
 *    约 15px ⇒ `ResizeObserver` 触发 ⇒ `setWidth` ⇒ effect 再跑 ⇒ 又清空、又重建……
 *    **判据的输入被它自己触发的那次渲染改掉了**，于是无限"清空 → 重建"，屏幕上就是闪屏。
 *    改成观察容器的**父节点**（调用点上那层就是 `FilePreview` 里的 `.kylab-preview`：
 *    `display: flex; flex-direction: column; width: 100%` 的普通盒子，**不是滚动容器**），
 *    它的宽度只随抽屉变，渲染改不动它，这条回路就断了；抽屉真变宽时父节点跟着变，
 *    RO 照旧触发，"变宽了要重建"一个字没丢。
 *    另外**要减掉容器自己的横向 padding 与 border**：`clientWidth` 含 padding，
 *    不减的话注进去的 wrapper 比容器的内容盒还宽，容器下面会一直挂着一条横向滚动条。
 *    两者从 `getComputedStyle(host)` 取（jsdom 里读出来是空串，按 0 收，见 `px`）。
 *
 * `mode` 取 `'list'`：抽屉里读一份 PPT，最常见的是"往下翻着看完整份"，
 * 而不是一页一页点。`'slide'` 会渲染上游自带的那对圆形翻页按钮（`#666666` 硬编码，
 * 不进主题），不适合我们的壳。
 */
import { useEffect, useRef, useState } from 'react'

import { CANNOT_PREVIEW, PreviewLoading, PreviewUnavailable, loadFailure, reasonOf } from './notes'
import { useRemoteBuffer } from './usePreviewSource'
import './preview.css'

export interface PptxPreviewProps {
  /** 后端签发的原件链接（相对路径）。 */
  url?: string | null
  /** 文件名：只用于报错文案。 */
  name?: string
}

/**
 * 舞台量不到宽度时用的兜底：`init` 拿到 `width: 0` 会算出 0 倍缩放，整份 PPT 塌成一条线。
 * jsdom 里没有布局，量到的一定是 0，所以这个兜底也是测试能跑起来的前提。
 */
const FALLBACK_WIDTH = 960

/** 计算样式里的长度（`padding-left` 这类）：jsdom 读出来是空串，非有限数一律当 0。 */
function px(value: string): number {
  const parsed = Number.parseFloat(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function PptxPreview({ url, name }: PptxPreviewProps) {
  const { loading, failure, buffer } = useRemoteBuffer(url)
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState(0)
  const [renderFailure, setRenderFailure] = useState('')

  // 宽度：量一次 + 之后跟着父节点变。只有宽度变了才重渲染（见文件头注第 2 条）。
  // 量的是父节点，不是容器自己：容器是滚动容器，它的 clientWidth 会被下面那次"清空再画"
  // 改掉，量它就是自己咬自己的尾巴——那就是闪屏（见文件头注第 5 条）。
  useEffect(() => {
    const host = hostRef.current
    const frame = host?.parentElement
    if (!host || !frame) return
    const measure = () => {
      const style = getComputedStyle(host)
      // clientWidth 含 padding、不含 border：这两样要减掉，wrapper 才落在容器的内容盒里
      const chrome =
        px(style.paddingLeft) +
        px(style.paddingRight) +
        px(style.borderLeftWidth) +
        px(style.borderRightWidth)
      setWidth(frame.clientWidth - chrome)
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(frame)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const host = hostRef.current
    if (!buffer || !host) return
    const stageWidth = width > 0 ? width : FALLBACK_WIDTH
    let alive = true
    let previewer: { destroy: () => void } | null = null
    setRenderFailure('')
    // 上游是 append：先清掉上一次的画布，否则 resize 一次就多一整套幻灯片
    host.innerHTML = ''
    void (async () => {
      try {
        const { init } = await import('pptx-preview')
        if (!alive) return
        const instance = init(host, { width: stageWidth, mode: 'list' })
        previewer = instance
        await instance.preview(buffer)
      } catch (cause) {
        if (alive) setRenderFailure(reasonOf(cause, CANNOT_PREVIEW))
      }
    })()
    return () => {
      alive = false
      previewer?.destroy()
      // destroy 不清 DOM（见文件头注第 4 条），大文档的画布得自己松手
      host.innerHTML = ''
    }
  }, [buffer, width])

  // 容器始终挂载（同 `DocxPreview`：卸载再挂回来会让宽度观察器指向旧节点），
  // 只是还没画完 / 画不出来时藏起来
  const veiled = Boolean(failure || renderFailure || loading || !buffer)
  return (
    <>
      {failure ? <PreviewUnavailable name={name} reason={loadFailure(failure)} /> : null}
      {!failure && renderFailure ? (
        <PreviewUnavailable name={name} reason={loadFailure(renderFailure)} />
      ) : null}
      {!failure && !renderFailure && (loading || !buffer) ? <PreviewLoading /> : null}
      <div
        ref={hostRef}
        hidden={veiled}
        className="kylab-office kylab-pptx-pane"
        data-testid="pptx-host"
      />
    </>
  )
}
