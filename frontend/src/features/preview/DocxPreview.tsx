/**
 * Word（.docx）预览：`docx-preview` 把文档画成 HTML。
 *
 * 上游用法（v0.4.1 的 `renderAsync`，见 README 的 API 段）：
 *
 * ```ts
 * renderAsync(data, bodyContainer, styleContainer?, userOptions?): Promise<WordDocument>
 * ```
 *
 * 三件与集成有关、且**上游文档里写在注释里**的事：
 *
 * 1. **它是命令式的**：给它一个容器，它把 DOM 与一份 `<style>` 写进去。
 *    两个容器它都会先 `innerHTML = ""`，所以不必自己清（但卸载时我们要清，
 *    见下）；`styleContainer` 缺省就是 `bodyContainer`，于是样式**只在我们的面板里生效**，
 *    不会漏到全站。
 * 2. **字节自己取**（同旧 `OfficePreview.vue` 的取舍）：签名链接是相对路径、不带鉴权头，
 *    我们 fetch 成 `ArrayBuffer` 再递过去，"取不到"就有统一的报错位。
 * 3. **样式要跟令牌走**：库自带 `.docx-wrapper { background: gray }` 与白纸 + 阴影，
 *    覆盖写在 `preview.css` 的 docx 段（那里解释了为什么靠特异性而不是顺序）。
 */
import { useEffect, useRef, useState } from 'react'

import { CANNOT_PREVIEW, PreviewLoading, PreviewUnavailable, loadFailure, reasonOf } from './notes'
import { useRemoteBuffer } from './usePreviewSource'
import './preview.css'

export interface DocxPreviewProps {
  /** 后端签发的原件链接（相对路径）。 */
  url?: string | null
  /** 文件名：只用于报错文案。 */
  name?: string
}

export function DocxPreview({ url, name }: DocxPreviewProps) {
  const { loading, failure, buffer } = useRemoteBuffer(url)
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [renderFailure, setRenderFailure] = useState('')

  useEffect(() => {
    const host = hostRef.current
    if (!buffer || !host) return
    // 换了文件 / 改了 options 就重画：docx-preview 自己会清空容器，但**先清一次并不多余**
    // ——渲染中途抛错时它已经写进去的半截 DOM 会留在那儿。
    host.innerHTML = ''
    setRenderFailure('')
    let alive = true
    void (async () => {
      try {
        const { renderAsync } = await import('docx-preview')
        if (!alive) return
        await renderAsync(buffer, host, undefined, {
          // 类名前缀换成我们的：注入的样式（以及 `preview.css` 里的覆盖）都按它选
          className: 'kylab-docx',
          // 包一层 `.kylab-docx-wrapper`：一页一页的版式、页与页之间的间距靠它
          inWrapper: true,
        })
      } catch (cause) {
        if (alive) setRenderFailure(reasonOf(cause, CANNOT_PREVIEW))
      }
    })()
    return () => {
      alive = false
      // 大文档的 DOM 很大：换文件或离页时主动松手，别等 GC 猜
      host.innerHTML = ''
    }
  }, [buffer])

  // 容器**始终挂载**（渲染是命令式的，容器要在 effect 里拿得到），
  // 只是还没画完 / 画不出来时藏起来：否则用户会先看到一条加载提示，下面再挂一个空框。
  // **不卸载它**是有意的——卸载再挂回来是一个新节点，命令式库与观察器都会指向旧的那个。
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
        className="kylab-office kylab-docx-pane"
        data-testid="docx-host"
      />
    </>
  )
}
