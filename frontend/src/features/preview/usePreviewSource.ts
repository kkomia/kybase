/**
 * 把签名链接变成字节（或文本）。
 *
 * **字节由我们自己取**（与旧 `OfficePreview.vue` 同一条）：签名链接是相对路径、
 * 不带鉴权头，`fetch` 拿到 `ArrayBuffer` 再交给渲染器——比让三个库各自去猜怎么取
 * 更可控，也让"取不到"有统一的报错位（三处各写一遍 fetch，就会有三套报错文案）。
 *
 * 两个钩子分开，因为**要的东西不一样**：
 *
 * - `useRemoteBuffer`：Office 三件套要 `ArrayBuffer`（docx-preview / pptx-preview /
 *   exceljs 三家都收字节，不收 Blob 也不收 base64）；
 * - `useRemoteText`：Markdown 与纯文本要字符串。**已经内联给过来的就不要再取一次**
 *   （文档接口的阅读视角直接回 `text`，那是它已经解码好的）。
 *
 * 三条共同的纪律：
 *
 * 1. **换 URL 就重来**：链接会过期（后端默认十分钟），重新签发后必须重新取；
 * 2. **卸载即中止**：`AbortController` 收掉在飞的请求，避免把结果写进已卸载的组件；
 * 3. **失败留下原因**：`HTTP 404` 与"网络不可达"都是用户要看到的区别。
 */
import { useEffect, useState } from 'react'

import { reasonOf } from './notes'

export interface RemoteBuffer {
  loading: boolean
  /** 空串 = 没出错。 */
  failure: string
  buffer: ArrayBuffer | null
}

export interface RemoteText {
  loading: boolean
  failure: string
  text: string
}

export interface ContentProbe {
  /** 上一次探测的结论：**非空 = 服务端明确回了个非 2xx**。空串 = 没结论，照常渲染。 */
  failure: string
  /** 这一轮探测还在飞（重试期间界面据此把按钮置灰）。 */
  checking: boolean
  /** 再探一次（签名链接会过期、服务端也会缓过来）。 */
  retry: () => void
}

/** 取不到链接时的那句话（后端没给 url / 上层还没拿到签名）。 */
export const NO_URL = '拿不到预览链接'

async function readBuffer(url: string, signal: AbortSignal): Promise<ArrayBuffer> {
  const response = await fetch(url, { signal })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.arrayBuffer()
}

/** Office 三件套的字节。`url` 为空即失败（不留一个永远转圈的框）。 */
export function useRemoteBuffer(url: string | null | undefined): RemoteBuffer {
  const [state, setState] = useState<RemoteBuffer>({ loading: true, failure: '', buffer: null })

  useEffect(() => {
    if (!url) {
      setState({ loading: false, failure: NO_URL, buffer: null })
      return
    }
    const controller = new AbortController()
    let alive = true
    // 换链接时先回到加载态：旧的字节留在屏幕上会让人以为"换了一份文件却没反应"
    setState({ loading: true, failure: '', buffer: null })
    void (async () => {
      try {
        const buffer = await readBuffer(url, controller.signal)
        if (alive) setState({ loading: false, failure: '', buffer })
      } catch (cause) {
        if (!alive) return // 主动中止不算失败
        setState({ loading: false, failure: reasonOf(cause, '读取失败'), buffer: null })
      }
    })()
    return () => {
      alive = false
      controller.abort()
    }
  }, [url])

  return state
}

/**
 * 文本类的内容。**`inline` 优先**：后端已经把解码好的文本给了，
 * 再去 fetch 一遍是同一次会话里多跑一趟网络，还会因为多余的一次失败把好好的内容变成报错。
 */
export function useRemoteText(url: string | null | undefined, inline?: string | null): RemoteText {
  const provided = inline ?? null
  const [state, setState] = useState<RemoteText>(() =>
    provided === null
      ? { loading: true, failure: '', text: '' }
      : { loading: false, failure: '', text: provided },
  )

  useEffect(() => {
    if (provided !== null) {
      setState({ loading: false, failure: '', text: provided })
      return
    }
    if (!url) {
      setState({ loading: false, failure: NO_URL, text: '' })
      return
    }
    const controller = new AbortController()
    let alive = true
    setState({ loading: true, failure: '', text: '' })
    void (async () => {
      try {
        const response = await fetch(url, { signal: controller.signal })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const text = await response.text()
        if (alive) setState({ loading: false, failure: '', text })
      } catch (cause) {
        if (!alive) return
        setState({ loading: false, failure: reasonOf(cause, '读取失败'), text: '' })
      }
    })()
    return () => {
      alive = false
      controller.abort()
    }
  }, [url, provided])

  return state
}

/**
 * 探一条签名链接到底通不通。**只给 `<iframe>` 这一条路用**（PDF）。
 *
 * 为什么要探：iframe 里加载的响应体是浏览器自己画的——服务端回 500 时，
 * 用户看到的就是那份错误信封原文（`{"code":"internal_error",…}`，还带浏览器的
 * JSON 查看器）。`onError` 指望不上：**HTTP 错误状态对 iframe 来说也是一次成功的加载**，
 * 它不触发任何事件。所以先问一次，问出非 2xx 就改画我们自己的失败态
 * （调用方**等这一问有结论再挂 iframe**：挂上去再撤换，浏览器已经开始画那份 JSON 了）。
 *
 * 三条纪律：
 *
 * 1. **只报"服务端明确说了不 OK"**。`fetch` 自己抛错（CORS、环境里没有可比对的 fetch）、
 *    被中止——都算**没结论**，调用方照常挂载：探测的职责是"别把服务端的错误信封画给用户"，
 *    不是替浏览器判断这份文件打不开（判错了就是"明明能看却说看不了"）；
 * 2. **`Range: bytes=0-0`**：只问第一个字节。后端这个端点 `Accept-Ranges: bytes`，
 *    问一声就够；真不支持 Range 时它开始回整包，这里主动 `abort()` 掐掉——
 *    "探一下"不该把 200MB 的 PDF 拉下来；
 * 3. **换链接就重探**（签名链接十分钟就过期，重新签发后那条 URL 是新的）。
 */
export function useContentProbe(url: string | null | undefined): ContentProbe {
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState<{ checking: boolean; failure: string }>({
    checking: Boolean(url),
    failure: '',
  })

  useEffect(() => {
    if (!url) {
      setState({ checking: false, failure: '' })
      return
    }
    const controller = new AbortController()
    let alive = true
    // 重试期间**留着上次的结论**：按钮在转、说明还在，不是先白一下再出现
    setState((current) => ({ checking: true, failure: current.failure }))
    void (async () => {
      let status = 0
      try {
        const response = await fetch(url, {
          headers: { Range: 'bytes=0-0' },
          signal: controller.signal,
        })
        status = response.status
        controller.abort() // 2xx 就到结论了，别陪着把响应体读完
      } catch {
        // 服务端状态码已经拿到（上面那次 abort）／网络层自己失败：两种都在 `status` 里区分
      }
      if (!alive) return
      setState({ checking: false, failure: status >= 400 ? `HTTP ${status}` : '' })
    })()
    return () => {
      alive = false
      controller.abort()
    }
  }, [url, attempt])

  return { ...state, retry: () => setAttempt((count) => count + 1) }
}
