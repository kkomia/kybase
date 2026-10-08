/**
 * 回答里 `[n]` 那枚**站点徽章**要用的数据：编号 → 网页引用（D11-③）。
 *
 * 用户要的样子（照 Kimi）：行内是一枚**小圆角徽章 = 站点真实 logo + 域名**，
 * 悬停/聚焦出一张卡片：站点 + 域名 / 页面标题 / 一两行摘要 / 可点可复制的 URL。
 *
 * ## 数据从哪来（**不新增后端字段**）
 *
 * 知识库出处的 `ChatSource`（`document_name / heading_path / page / preview`）里
 * **没有 URL、没有站点**——它是一条文档片段，画不出"站点徽章"。而网页引用的
 * 标题 / 网址 / 摘要**本来就在库里**：`web_search` 那一步的返回就是后端渲染好的
 * 编号列表（`services/tools.py::_web_search`）：
 *
 * ```
 * 检索词：agent skills，共 3 条：
 * [1] Anthropic 的官方仓库
 * https://github.com/anthropics/skills
 * 摘要…
 * ```
 *
 * 于是这里只做一件事：把**已经有**的那段文本解析成引用（摘要就是搜索结果自带的那行
 * snippet），站点标识复用 `webSites.ts` 那张表。**不新开接口、不动搜索与抓取链路。**
 *
 * ## 只认这一种形状
 *
 * 只解析 `[n] 标题 / 网址 / 摘要` 的编号列表（`web_search` 的返回）。`web_fetch` 的返回
 * 不是编号列表（它是"【标题】网址 + 正文"），回答里的 `[n]` 也不会指它——所以那不解析，
 * 宁可少一枚徽章，也不猜一个编号对应哪一页。
 */

import { hostOfUrl, isFetchStep, siteOfDomain, urlsIn, type WebSite } from './webSites'

/** 一枚网页引用（回答里那个编号指的东西）。 */
export interface WebCitation {
  /** 回答里的编号（`[3]` → 3）。 */
  index: number
  /** 搜索结果给的页面标题。 */
  title: string
  /** 页面网址（可点、可复制）。 */
  url: string
  /** 网址的域名（徽章上写的那个，如 `github.com`）。 */
  domain: string
  /** 搜索结果给的摘要（取不到就是空串——那就只显示标题与 URL）。 */
  snippet: string
  /** 站点标识（本机表给的 logo/名字；未命中时 `id` 为空，界面退化成通用地球）。 */
  site: WebSite
}

/** 摘要最多留多少字：卡片里只给"一两行"，多的交给 URL 与原文。 */
export const SNIPPET_MAX_CHARS = 160

/** `[3] 标题` 那一行。 */
const HIT_RE = /^\[(\d+)\]\s*(.*)$/

/** 正文节选那一段的抬头：到了这里搜索结果就结束了（后面是顺带抓回来的正文）。 */
const EXCERPT_MARK = '【前'

/** 一步一步从搜索返回里挑出引用（纯函数，输入是那一步的 `result` 文本）。 */
function citationsFromResult(text: string, into: Map<number, WebCitation>): void {
  const lines = text.replace(/\\\//g, '/').split('\n')
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!.trim()
    if (line.startsWith(EXCERPT_MARK)) return
    const matched = HIT_RE.exec(line)
    if (!matched) continue
    const number = Number(matched[1])
    if (!Number.isInteger(number) || into.has(number)) continue
    // 标题 / 网址 / 摘要 三行是**连续**的（后端就是这么渲染的）：先找网址那一行
    let url = ''
    let urlAt = -1
    for (let cursor = index + 1; cursor < Math.min(index + 4, lines.length); cursor += 1) {
      const candidate = lines[cursor]!.trim()
      if (/^https?:\/\//i.test(candidate)) {
        url = candidate
        urlAt = cursor
        break
      }
      if (HIT_RE.test(candidate)) break
    }
    if (!url) continue
    const domain = hostOfUrl(url)
    if (!domain) continue
    const snippet = (lines[urlAt + 1] ?? '').trim().slice(0, SNIPPET_MAX_CHARS)
    into.set(number, {
      index: number,
      title: matched[2]!.trim(),
      url,
      domain,
      snippet,
      site: siteOfDomain(domain),
    })
  }
}

/**
 * 这些步骤里能解析出来的网页引用（编号 → 引用）。
 *
 * 只认联网搜索那两步（`web_search` / `web_fetch`，与站点那一行同一个判据来源），
 * 而且只在返回文本里找**编号列表**。多个搜索步骤时**先出现的编号先占**：
 * 一条回答里的编号是模型按它看到的顺序写的，后来的那次搜索用的是新的序号区间。
 */
export function webCitationsOfSteps(
  steps: readonly { tool?: string; label?: string; result?: string }[],
): Map<number, WebCitation> {
  const found = new Map<number, WebCitation>()
  for (const step of steps) {
    if (!isSearchStep(step)) continue
    citationsFromResult(step.result ?? '', found)
  }
  return found
}

/** 这一步是不是"网上搜了一次"（与 `webSites.ts::isWebStep` 同一份口径，只是排除抓页）。 */
function isSearchStep(step: { tool?: string; label?: string }): boolean {
  if (step.tool) return step.tool === 'web_search'
  return step.label === '联网搜索'
}

/**
 * 编号列表**之后**那一段原文（`【前 N 条的正文开头】…`），没有就给空串。
 *
 * ⚠️ **现在没有任何渲染路径调用它**（2026-09-30 用户批注："搜索网页的不显示 request
 * 和 response，只显示网页列表"）——搜索步展开时只画 `ui/SearchHits.tsx` 那张清单，
 * 清单之外的这段原文**整档不再渲染**（抓页那一档同理，见 `fetchedBodies`）。
 * 撤掉的是**渲染那一侧**（调用点在 `ui/ToolchainFlow.tsx`）：这一段文本本身仍在返回里，
 * 这一支取文函数留着；真要把"不再渲染"落成"不再解析"时，它和 `fetchedBodies` 一起删。
 *
 * 判据就是解析时用的那个抬头（`EXCERPT_MARK`）：解析到它就停，这里从它开始取。
 */
export function searchExcerptTail(result: string): string {
  const at = result.indexOf(EXCERPT_MARK)
  return at < 0 ? '' : result.slice(at).trim()
}

/**
 * 抓页那一档"读到了哪几页"——抓页步展开时画成清单（2026-09-30 用户批注："获取网页
 * 用的是不同于搜索网页的图标""注意看获取网页列表的样式"，Kimi 的 `fetch-urls-item`）。
 * 数据同样**不新增后端字段**：`web_fetch` 的返回就是一页一块的文本
 * （`services/tools.py::_fetch_one`）：
 *
 * ```
 * 【标题】
 * 来源：https://example.com/a
 *
 * 正文…
 * ```
 *
 * 一页读不到时后端把标题写成「这一页没抓成」，照样解析得出来（清单里如实显示）——
 * 那样的行点开就是清单里的这一条（正文那一段不再渲染，见 `fetchedBodies`）。
 * 标题 / 网址解析不出来时**退回入参里的网址**、标题用域名顶（"没有 title 就用域名当标题"）。
 */
export interface FetchedPage {
  title: string
  url: string
  domain: string
  site: WebSite
}

/** `【标题】` 那一行。 */
const PAGE_TITLE_RE = /^【(.+)】$/
/** `来源：https://…` 那一行（后端渲染的抬头第二行）。 */
const PAGE_SOURCE_RE = /^来源：(https?:\/\/\S+)$/i

/** 这几步抓回来的网页（去重、保持先后）。 */
export function fetchedPagesOfSteps(
  steps: readonly { tool?: string; label?: string; args?: string; result?: string }[],
): FetchedPage[] {
  const out: FetchedPage[] = []
  const seen = new Set<string>()
  const push = (title: string, url: string): void => {
    const domain = hostOfUrl(url)
    if (!domain || seen.has(url)) return
    seen.add(url)
    out.push({ title: title.trim() || domain, url, domain, site: siteOfDomain(domain) })
  }
  for (const step of steps) {
    if (!isFetchStep(step)) continue
    const lines = (step.result ?? '').replace(/\\\//g, '/').split('\n')
    for (let index = 0; index < lines.length; index += 1) {
      const title = PAGE_TITLE_RE.exec(lines[index]!.trim())
      if (!title) continue
      // `来源：` 紧跟标题行（后端就是这么渲染的）：往下看两行
      for (let cursor = index + 1; cursor < Math.min(index + 3, lines.length); cursor += 1) {
        const source = PAGE_SOURCE_RE.exec(lines[cursor]!.trim())
        if (!source) continue
        push(title[1]!, source[1]!)
        break
      }
    }
    // 抬头解析不出来（半截、被裁过）就退回入参里的网址——标题用域名顶
    for (const url of urlsIn(step.args ?? '')) push('', url)
  }
  return out
}

/**
 * 抓页返回里**去掉每一页的抬头**（`【标题】` 与 `来源：url` 两行）之后剩下的正文。
 *
 * ⚠️ 与 `searchExcerptTail` 同一档：**现在没有任何渲染路径调用它**。抓页步展开只剩
 * 一张网页清单（2026-09-30 用户批注"搜索网页的不显示 request 和 response，只显示网页列表"，
 * 抓页同档处理），抬头之后的正文**整档不再渲染**；真要把这一档从解析层也撤掉时，
 * 它和 `searchExcerptTail` 一起删。
 */
export function fetchedBodies(text: string): string {
  return text
    .replace(/\\\//g, '/')
    .split('\n')
    .filter((line) => !PAGE_TITLE_RE.test(line.trim()) && !PAGE_SOURCE_RE.test(line.trim()))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
