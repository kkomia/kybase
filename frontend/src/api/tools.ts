/**
 * 对外工具面（对应后端 /api/v1/tools）。
 *
 * 这一条只读端点把"这个服务对外提供了什么"一次给全：工具清单（名字 / 标签 / 摘要 /
 * 要不要写权限 / 参数）、MCP 接入地址、随服务一起发出去的那份技能。
 *
 * 两个调用点共用它：`ToolsPage` 据此渲染整张表，`ApiKeysPage` 的「工具范围」据此列候选项。
 * **候选项只能来自这里**——界面上另抄一份工具清单，它与真实工具面必然相漂，
 * 而漂出来的形态是"勾得出来的工具，调用时被拒"。
 *
 * 工具由后端按登记顺序给出（`items` 的顺序就是 MCP 客户端看到的顺序），
 * 这一层不排序、不筛选、**不写死任何一个工具名**。
 */

import { request } from './client'
import type { components } from './schema'

/** 一个参数：`summary` 是 JSON Schema 里的说明，没写就是空串。 */
export type ToolParam = Required<components['schemas']['ToolParamOut']>

/** 一个工具：`requires_write` 为真表示写档下才可调。 */
export type Tool = Required<components['schemas']['ToolOut']>

/** 那份技能：只报身份与路径，不报内容。 */
export type Skill = Required<components['schemas']['SkillOut']>

/**
 * 工具目录。
 *
 * `mcp_endpoint` 是 `string | null`：**没配部署地址时后端回 `null`**，界面照实说没配，
 * 不拼一个"看起来能用"的地址（拼出来的端口多半是错的）。
 *
 * `surface_fingerprint` 是**整张工具面的摘要**（12 位十六进制，后端
 * `services.tools.surface_fingerprint()`）：任何一个工具的名字 / 说明 / 入参 / 出参变了
 * 它就变，客户端据此判断"对外契约有没有变"（MCP 侧同一个值跟在 `serverInfo.version`
 * 后面）。界面照原样显示、让接入方读得到、复制得走——**不解释它由什么算出来**。
 *
 * `items` / `skill` 两处再收一次 `Required`：`Required<…>` 只剥**顶层**的可选，
 * 而 `ToolOut.params` 与 `SkillOut.transports` 都是带默认值的可选字段——
 * 不放宽的话，页面上每一处读它们都要先防一次 `undefined`，而契约上后端一定会给。
 */
export type ToolCatalog = Omit<
  Required<components['schemas']['ToolCatalogOut']>,
  'items' | 'skill'
> & {
  items: Tool[]
  skill: Skill
}

export function getToolCatalogue(): Promise<ToolCatalog> {
  return request('/tools')
}
