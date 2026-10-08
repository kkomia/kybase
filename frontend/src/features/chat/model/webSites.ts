/**
 * 联网那几步"查了哪些站点"的**本机站点表 + 域名解析**（纯函数，§12.334 第二节）。
 *
 * 用户的原话："网页搜索 一定要把 网页的 logo 给显示出来。这样用户大致可以知道
 * 在查询哪些常见的网页。" 所以这一步要给出来的信息是**站点**（github.com、
 * arxiv.org、知乎…），而不是"又跑了一次工具"。
 *
 * 三条做法上的取舍，都写在这里，界面只负责画：
 *
 * 1. **不用 `<img src="https://<域名>/favicon.ico">`**。那会让**浏览器**在渲染这一行时
 *    向被查的那个站点发一次真实请求：用户的 IP、UA、访问时刻全都交出去了，而他只是
 *    问了一句话。这与本产品"本地优先"的姿态直接相冲（本仓连检索都跑在本地）。
 *    本机站点表 + 域名文字一样满足"看得出在查哪些站点"，且**零外部请求、结果确定、可单测**；
 * 2. **域名从这一步自己已有的数据里取**（`TraceStep.args` / `TraceStep.result`），
 *    **不新增后端字段**：过程面板本来就把入参与返回存下来了。
 *    `web_fetch` 的入参里就是网址（`{"url": "…"}` / `{"urls": […]}`）；
 *    `web_search` 的入参只有检索词，**它查了哪些站要看返回**——后端把结果渲染成
 *    "标题 / 网址 / 摘要"的编号文本（见 `services/tools.py::_web_search`），
 *    网址就在里面。两处都扫，是因为这两个工具各自把网址放在不同的地方；
 * 3. **未命中的站点不猜**：名字就是域名本身（`· arxiv.example`），界面退化成一枚通用地球。
 *    硬编一个"常见站点表"只为了认得出那几个常客，认不出来就如实显示域名。
 *
 * 另外，**"哪些步算联网、哪一步是抓页"这两个判据也只有这一份**（`isWebStep` /
 * `isFetchStep`）：图标那一档、行尾站点那一档、清单与解析那几档全都 import 它们，
 * 不各自写词表（`web_search` 与 `web_fetch` 后端同归 `kind=search`，只能靠工具名分）。
 *
 * 这一层不认识 React，也不发任何请求：输入是步骤里的字符串，输出是站点表。
 */

/** 联网的两个工具名（后端 `tool_meta` 里它们的 kind 都是 `search`）。 */
const WEB_TOOLS: ReadonlySet<string> = new Set(['web_search', 'web_fetch'])

/**
 * 老快照的兜底：v0.26 之前落库的步骤**没有工具名**，只有当时的中文标签
 * （见 `services/tool_loop.TOOL_LABELS`）。那批数据正躺在用户手上的会话里。
 */
const WEB_LABELS: ReadonlySet<string> = new Set(['联网搜索', '抓取网页'])

/** 抓页的那一个工具名与那一个标签（`web_fetch`；两条表从上面那对里各取一半）。 */
const FETCH_TOOLS: ReadonlySet<string> = new Set(['web_fetch'])
const FETCH_LABELS: ReadonlySet<string> = new Set(['抓取网页'])

/**
 * 这一步是"上网"吗。
 *
 * 判据**只看工具名**（老快照退回中文标签）——不看 kind：kind 里
 * `web_search` / `web_fetch` / `search`（知识库检索）/ `search_files`（在文件里搜）
 * 全是 `search`，靠它分不出"去外面翻"与"在本地翻"。
 *
 * 两个调用方（图标那一枚地球、站点那一行）都走这一个判据，不各写一遍。
 */
export function isWebStep(step: { tool?: string; label?: string }): boolean {
  if (step.tool) return WEB_TOOLS.has(step.tool)
  return step.label !== undefined && WEB_LABELS.has(step.label)
}

/**
 * 这一步是"抓了一页网页"吗——**`isWebStep` 里只留 `web_fetch` 那一半**，
 * 规则与它逐字相同（工具名优先、老快照退标签）。
 *
 * 为什么要单拎出来、又为什么搁在这一层：2026-09-30 用户批注
 * "获取网页用的是不同于搜索网页的图标"、"搜索网页的不显示 request 和 response，
 * 只显示网页列表"。于是**图标那一档**（`ui/stepIcons.tsx` 的窗口那枚）、**清单那一档**
 * （`ui/SearchHits.tsx` 的抓页清单）与**解析那一档**（`model/sourceCitations.ts`）
 * 都要问同一个问题；三处各写一份词表迟早漂掉，所以词表与判据都在这里，
 * 别处一律 import 这一份。
 */
export function isFetchStep(step: { tool?: string; label?: string }): boolean {
  if (step.tool) return FETCH_TOOLS.has(step.tool)
  return step.label !== undefined && FETCH_LABELS.has(step.label)
}

/** 一个站点在界面上的样子。 */
export interface WebSite {
  /** 站点标识（本机表里的键，如 `github`）；**不在表里就是空串**（界面据此退化）。 */
  id: string
  /** 域名（小写、去 `www.`）。 */
  domain: string
  /** 给用户看的名字：表里没有就用域名本身。 */
  name: string
  /** 牌子上的那一个字母/字；表里没有就是空串（界面画通用地球）。 */
  badge: string
}

/**
 * **本机已知站点表**（§12.334：github.com / arxiv.org / wikipedia.org / stackoverflow.com /
 * developer.mozilla.org / 常见中文站点…）。
 *
 * 收录口径：搜索引擎里高频出现、且名字一眼认得出的站点。**没有品牌色**——
 * 一枚字母牌 + 名字就够认，而硬编一列品牌色会绕开 `tokens.css` 那套设计令牌
 * （本仓的硬要求：颜色只从令牌取）。所以牌子走中性灰，靠字母/字与名字区分。
 *
 * 匹配是**后缀式**的（`en.wikipedia.org` → `wikipedia.org`、`mp.weixin.qq.com` →
 * `weixin.qq.com`），所以一个站点只需一条；但键越短覆盖越宽（`qq.com` 会把
 * `news.qq.com` 也算进来），因此只收"整个域名都归它"的那些。
 */
const KNOWN_SITES: Readonly<Record<string, { id: string; name: string; badge: string }>> = {
  // 代码 / 学术 / 百科：查技术问题时出现得最多的那几个
  'github.com': { id: 'github', name: 'GitHub', badge: 'G' },
  'gitlab.com': { id: 'gitlab', name: 'GitLab', badge: 'G' },
  'gitee.com': { id: 'gitee', name: 'Gitee', badge: 'G' },
  'stackoverflow.com': { id: 'stackoverflow', name: 'Stack Overflow', badge: 'S' },
  'developer.mozilla.org': { id: 'mdn', name: 'MDN', badge: 'M' },
  'npmjs.com': { id: 'npm', name: 'npm', badge: 'n' },
  'python.org': { id: 'python', name: 'Python', badge: 'P' },
  'nodejs.org': { id: 'nodejs', name: 'Node.js', badge: 'N' },
  'react.dev': { id: 'react', name: 'React', badge: 'R' },
  'arxiv.org': { id: 'arxiv', name: 'arXiv', badge: 'a' },
  'wikipedia.org': { id: 'wikipedia', name: '维基百科', badge: 'W' },
  'nature.com': { id: 'nature', name: 'Nature', badge: 'N' },
  'sciencedirect.com': { id: 'sciencedirect', name: 'ScienceDirect', badge: 'S' },
  'ieee.org': { id: 'ieee', name: 'IEEE', badge: 'I' },
  'acm.org': { id: 'acm', name: 'ACM', badge: 'A' },
  'springer.com': { id: 'springer', name: 'Springer', badge: 'S' },
  'jstor.org': { id: 'jstor', name: 'JSTOR', badge: 'J' },
  // AI / 厂商官方
  'openai.com': { id: 'openai', name: 'OpenAI', badge: 'O' },
  'anthropic.com': { id: 'anthropic', name: 'Anthropic', badge: 'A' },
  'huggingface.co': { id: 'huggingface', name: 'Hugging Face', badge: 'H' },
  // 常见中文站点
  'zhihu.com': { id: 'zhihu', name: '知乎', badge: '知' },
  'baidu.com': { id: 'baidu', name: '百度', badge: '百' },
  'juejin.cn': { id: 'juejin', name: '掘金', badge: '掘' },
  'csdn.net': { id: 'csdn', name: 'CSDN', badge: 'C' },
  'cnblogs.com': { id: 'cnblogs', name: '博客园', badge: '博' },
  'segmentfault.com': { id: 'segmentfault', name: '思否', badge: '思' },
  'infoq.cn': { id: 'infoq', name: 'InfoQ', badge: 'I' },
  'sspai.com': { id: 'sspai', name: '少数派', badge: '少' },
  '36kr.com': { id: '36kr', name: '36氪', badge: '氪' },
  'bilibili.com': { id: 'bilibili', name: '哔哩哔哩', badge: 'B' },
  'weixin.qq.com': { id: 'weixin', name: '微信公众号', badge: '微' },
  'qq.com': { id: 'qq', name: '腾讯网', badge: '腾' },
  '163.com': { id: 'netease', name: '网易', badge: '网' },
  'sina.com.cn': { id: 'sina', name: '新浪', badge: '新' },
  'sohu.com': { id: 'sohu', name: '搜狐', badge: '搜' },
  'ifeng.com': { id: 'ifeng', name: '凤凰网', badge: '凤' },
  'people.com.cn': { id: 'people', name: '人民网', badge: '人' },
  'xinhuanet.com': { id: 'xinhua', name: '新华网', badge: '华' },
  'thepaper.cn': { id: 'thepaper', name: '澎湃新闻', badge: '澎' },
  'caixin.com': { id: 'caixin', name: '财新', badge: '财' },
  // 英文社区 / 媒体
  'medium.com': { id: 'medium', name: 'Medium', badge: 'M' },
  'reddit.com': { id: 'reddit', name: 'Reddit', badge: 'r' },
  'ycombinator.com': { id: 'hackernews', name: 'Hacker News', badge: 'Y' },
}

/**
 * 表的键**按长度倒序**：子域匹配要取最具体的那一条
 * （`developer.mozilla.org` 比 `mozilla.org` 具体——将来两条都在表里时不能认错）。
 */
const SITE_KEYS: readonly string[] = Object.keys(KNOWN_SITES).sort((a, b) => b.length - a.length)

/**
 * 表里的域名（D11-② 起对外）。
 *
 * **它不再决定"准不准抓图标"**：后端那张同源的 `ALLOWED_DOMAINS` 白名单已经撤了
 * （2026-10-01：表外域名一律不发请求的后果，是真实结果里绝大多数站点只剩一枚字母圆，
 * 用户："这个为啥抓不到真实的图标呢，你放个字母标在这儿没意义啊"）。这张表现在只剩一件事：
 * **认得出名字与字牌**——认得出的用字牌（知乎的「知」），认不出的画一枚通用地球
 * （兜底那一档在 `ui/SearchHits.tsx` 的 `HitLogo` 与 `ui/WebSiteList.tsx` 的
 * `CompactLogo`；带站点名的 `SiteLogo` 已随批四那一次删除一并撤掉）。
 */
export const KNOWN_DOMAINS: readonly string[] = Object.freeze(Object.keys(KNOWN_SITES))

/** 一个合法主机名：至少两节、只含字母数字与连字符（单标签的 `localhost` 没有站点可言）。 */
const HOST_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/

/**
 * 把一段"主机名"收拾成可比对的域名：去 `user:pass@` / 端口 / 前导 `www.`，
 * 截掉粘在网址后面的中文句读，小写。
 *
 * 认不出合法域名就给空串（`localhost`、内网名、纯数字地址都没有"站点"可言）。
 */
function normalizeHost(authority: string): string {
  const host = (authority.split('@').pop() ?? '')
    .split(':')[0]!
    .toLowerCase()
    // 中文标点常直接粘在网址后面（"…见 https://example.com。"）：从第一个非法字符起截断
    .replace(/[^a-z0-9.-].*$/, '')
    .replace(/^www\./, '')
    .replace(/^\.+|\.+$/g, '')
    .replace(/\.{2,}/g, '.')
  return HOST_RE.test(host) ? host : ''
}

/** 网址 → 域名；认不出来（或不是 http/https）就给空串。 */
export function hostOfUrl(url: string): string {
  const match = /^https?:\/\/([^/?#]+)/i.exec(url.trim())
  return match ? normalizeHost(match[1]!) : ''
}

/**
 * 一段文本里的所有 http(s) 网址（按出现先后，**不做去重**）。
 *
 * 为什么要把 `\/` 还原成 `/`：有些模型把 JSON 里的斜杠转义着发出来
 * （`{"url": "https:\/\/example.com"}`），不还原的话整条网址都认不出来。
 */
export function urlsIn(text: string): string[] {
  return text.replace(/\\\//g, '/').match(/https?:\/\/[^\s"'`<>()[\]]+/gi) ?? []
}

/** 域名 → 站点；不在本机表里就如实退回域名（`id` 与 `badge` 都是空串）。 */
export function siteOfDomain(domain: string): WebSite {
  const host = normalizeHost(domain)
  for (const key of SITE_KEYS) {
    if (host === key || host.endsWith(`.${key}`)) {
      const site = KNOWN_SITES[key]!
      return { id: site.id, domain: host, name: site.name, badge: site.badge }
    }
  }
  return { id: '', domain: host, name: host, badge: '' }
}

/**
 * 一行上最多画几个站点（与出处那 3 条同一口径）。
 *
 * 一次联网搜索动辄 8 条结果、7 个不同站点，全铺出来这一行比结论还长；
 * 多出来的收成 `+N`，要看细节本来就能点开这一步的「返回」。
 */
export const MAX_WEB_SITES = 3

/** 一行（一步，或并成一组的那几步）查过的站点。 */
export interface WebSites {
  /** 按出现先后去重之后的前几个站点。 */
  sites: WebSite[]
  /** 还有几个站点没画出来（`+N`）。 */
  more: number
}

/**
 * 这几步查了哪些站点（去重、保序、截断）。
 *
 * **不是 web 工具就一律不认**（哪怕它的返回里全是网址：`search_files` 在文件里搜、
 * 读文件读到的正文里也有链接，那些都不是"它在查哪些网页"）。
 */
export function webSitesOfSteps(
  steps: readonly {
    tool?: string
    label?: string
    args?: string
    result?: string
  }[],
): WebSites {
  const seen = new Set<string>()
  const sites: WebSite[] = []
  let more = 0
  for (const step of steps) {
    if (!isWebStep(step)) continue
    // 入参在前、返回在后：`web_fetch` 的网址在入参里（那是"它要查的"），
    // `web_search` 的网址在返回里（那是"它查到的"）
    for (const url of [...urlsIn(step.args ?? ''), ...urlsIn(step.result ?? '')]) {
      const host = hostOfUrl(url)
      if (!host || seen.has(host)) continue
      seen.add(host)
      if (sites.length < MAX_WEB_SITES) sites.push(siteOfDomain(host))
      else more += 1
    }
  }
  return { sites, more }
}
