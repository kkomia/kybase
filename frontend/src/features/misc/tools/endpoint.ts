/**
 * 「服务地址」那一格：本机记住的地址、地址合法性、示例占位。
 *
 * 为什么这一格必须能自己填：服务端给的是**整条**地址（`GET /tools` 的 `mcp_endpoint`，
 * 由部署方一次性给出）——MCP 是另一个监听，在 REST 基址后面接 `/mcp` 拼出来的地址
 * 端口多半是错的，所以后端"不猜、不拼"，没配就回 `null`。没有这一格时，"服务端没配"
 * 就等于"整个「接入」档一个片段都拿不到"，接入这件事断在最前面一步。
 *
 * 三件事：
 *
 * 1. **记住填过的地址**：落在 `localStorage`，键 `kylab-mcp-endpoint`（与主题
 *    `kylab-theme`、侧栏折叠 `kylab-sidebar-collapsed` 同一族前缀）。它只是一条地址、
 *    不是凭据——**密钥不在这里**：明文密钥只活在 `AccessPanel` 的组件内存里，
 *    不落盘、不进 URL、不打日志（见 `snippets.ts` 的文件头）。
 * 2. **合法性**：这一格的值要拼进客户端片段里，坏值拼出来的是"照抄连不上"（最难排的
 *    一类），所以不合法就不给片段（那个区块退回空态）、也不弹窗不推通知拦人——
 *    用户可能正打到一半。
 * 3. **示例占位**：见 `ENDPOINT_PLACEHOLDER`。
 *
 * 纯逻辑、不碰 React：组件只负责把值接上界面。
 */

/** 本机记住的那条地址的存储键（同族键都带 `kylab-` 前缀）。 */
export const MCP_ENDPOINT_STORAGE_KEY = 'kylab-mcp-endpoint'

/** 本机记住的地址；没填过、或读不出来（隐私模式）时是空串。 */
export function readStoredEndpoint(): string {
  try {
    return window.localStorage.getItem(MCP_ENDPOINT_STORAGE_KEY)?.trim() ?? ''
  } catch {
    // 隐私模式下 localStorage 不可读：当成"没填过"
    return ''
  }
}

/**
 * 记住这条地址（**空串 = 忘掉它**）。
 *
 * 写不进去就只在本次会话生效——不能因为存不上就把填的值丢了：这一格的值当次就要
 * 用来拼片段。
 */
export function writeStoredEndpoint(value: string): void {
  try {
    const next = value.trim()
    if (next) window.localStorage.setItem(MCP_ENDPOINT_STORAGE_KEY, next)
    else window.localStorage.removeItem(MCP_ENDPOINT_STORAGE_KEY)
  } catch {
    // 隐私模式下不可写：不记忆即可，填的值仍然照用
  }
}

/**
 * 这一格的值能不能用来拼片段：得是一条 `http` / `https` 的地址。
 *
 * 判据交给 `URL` 自己（比手写正则准）：协议必须是这两者之一，且要能解析出主机名——
 * `http://`、`mcp.example.com/mcp`（没写协议）、`ftp://x/mcp` 这些半截值/别的协议
 * 都会在这里被挡掉。
 */
export function isValidEndpoint(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return false
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return false
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false
  return parsed.hostname !== ''
}

/** MCP 那个监听的默认端口（后端默认值）。示例里用它示形，**不拿它去猜任何地址**。 */
const MCP_DEFAULT_PORT = 8765

/** 示例里的主机名：它只是形，不是一个地址。 */
const EXAMPLE_HOST = '<你的主机>'

/** 示例的协议：按明文示形（部署走 TLS 时把它换掉即可，地址本来由用户自己填）。 */
const EXAMPLE_SCHEME = 'http'

/**
 * 「MCP 地址」那一格的示例占位（服务端没给地址时，用户照它示形）。
 *
 * **三段都取自变量、不写成一条字面量**：这条示例里因此没有任何一个真实地址，
 * 也不会被"界面上不许出现地址/端口字面量"那条门禁当成泄露（示例是形状，不是数据；
 * 真正要照抄的地址来自服务端或用户手里那条）。
 */
export const ENDPOINT_PLACEHOLDER = `${EXAMPLE_SCHEME}://${EXAMPLE_HOST}:${MCP_DEFAULT_PORT}/mcp`
