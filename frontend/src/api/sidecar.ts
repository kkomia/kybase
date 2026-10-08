/**
 * 对话轮次的**基址分派**（P4 第 2 片）：一轮对话在**本机边车**上跑。
 *
 * ## 2026-10-05：服务器那条链退役了，这一片的"回退"随之清掉
 *
 * `/chat/stream` 与 `/chat/approvals/{id}` 原先在服务器档也挂着一份（前端打不到边车时
 * 就落过去）。NAS 网页端退役之后这一族**两个档里都不再对外**
 * （`backend/app/api/v1/router.py` 的模块头写着那一步）——于是"回退服务器"成了一条
 * **死路**：那个 URL 谁都答不上来，打过去只会在界面上多一条 404，而用户看到的是
 * "对话坏了"，看不出真正的原因是"这台机器上的对话后端没跑起来"。
 *
 * 所以现在只有**一个落点**：本机边车。打不到就**显式失败**（`TurnUnavailableError`，
 * 由 `resolveTurnTarget` / `resolveApprovalTarget` 抛，界面如实显示那句话）——
 * 与"本机权威面"那条纪律（`localDownReason`：打不到就报错、绝不换源）终于一致了。
 *
 * ## 紧接着的一步：`VITE_SIDECAR_TURNS` 那个变量也退役了（2026-10-05）
 *
 * 它原先决定"这一轮打边车、还是回服务器那条链"，而上面那一步已经把"回服务器"清掉了
 * ⇒ 它**没有可切换的两端**，留着只会让人以为还有退路。所以变量本身、以及围着它的
 * 那一整套（`sidecarTurnsEnabled` / `sidecarTurnsEnabledFrom` / `explicitOffReason` /
 * 用例用的 `setSidecarTurnsForTest`）一并删掉。
 *
 * "对话跑不了"如今只剩**一个**原因：**边车没起来**（`probe.reason`）——
 * 那句话照旧由 `TurnUnavailableError` 带出去，文案一个字没少（见 `resolveTurnTarget`）。
 * **`VITE_LOCAL_DATA` 不受影响**：那是另一件事（这份数据归谁），见本文件后半段。
 *
 * ## 已完成（P4-2b，2026-09-29）：一轮对话落回本机会话库那条链
 *
 * 原先这里是 TODO：`api/v1/conversations.py` **没有**任何"追加/写入一轮"的端点，
 * 而当年会话是**服务器权威** —— 边车跑完这一轮若没人写回，用户刷新一下这轮就没了
 * （数据丢失，比"慢一点"严重得多），所以那时这条接线被一个开关挡着（默认关）。
 * 后来那两件事都入库并验通了：`POST /api/v1/chat/turns/record` 与边车跑完写回
 * （真机端到端物证 `.shots/e2e-turn.json`，`recorded: true`），开关随之翻成默认开。
 * （那一轮之后边车的会话**直接写本机库**，写回服务器那一环已删——见 `app/sidecar.py`；
 * 而那个开关本身在上面那一步里退役了。）
 *
 * ## 为什么分派落在"接口类别"这一层
 *
 * 已定的分界是 **API 层**（不是数据库）：**对话轮次 → 本地边车**，**账号 / 知识库 /
 * 记忆 / 会话列表 / 设置 → 本机权威面或服务器**（按域名判）。判定只有这一处，
 * 其余模块不许自己拼基址。
 *
 * ## 只有对话轮次归边车（多一个都要有理由）
 *
 * | 归边车的路径 | 理由 |
 * | --- | --- |
 * | `POST /chat/stream` → 边车 `POST /turn/stream` | 一轮对话：模型调用 + 工具在本机执行 + SSE |
 *
 * **明确不归边车的**：
 * - `POST /chat`（非流式）：边车 `/turn` 的响应是 `answer/steps/notes`，与原来那套不同，
 *   这一片不换解析口径（而且这条端点两个档里都没有了：`router.py` 的模块头那张表里
 *   逐条写着哪一族退到哪里）。
 * - `/chat/turns/{id}/live`（重连补发）：**边车没有会话事件日志**；
 * - `/conversations/*`、`/settings`、`/memory` 这些：它们归**本机权威面**（`LOCAL_PATHS`）。
 *
 * ## 没落点就显式失败，不许静默
 *
 * 三种状态都要看得出来：**走边车**（`sidecarStatus().reason` 说清打的是哪个基址）/
 * **边车没起来**（`probe.reason`）/ **还没探过**（`available: null`，不猜好坏）。
 * 「走边车」时正常返回落点；后两种**都没有回退可走**——
 * `resolveTurnTarget()` / `resolveApprovalTarget()`（同一套）抛 `TurnUnavailableError`，
 * 同时各留一条 `console.warn`（探不到是异常，不是主动选择），
 * 抛出去那句话就是界面上显示的那句。「还没探过」不必单独一支：`resolveTurnTarget`
 * 自己会先探一次（`sidecarAvailable()`）。
 *
 * ## M2 阶段 4：这个模块现在管**两件事**（别混）
 *
 * | 管什么 | 谁读它 | 本机那台不可用时 |
 * | --- | --- | --- |
 * | **一轮对话在哪台机器上跑**（`/chat/stream`，上面那几段） | `resolveTurnTarget` / `resolveApprovalTarget` | **显式失败**（这条链只在边车上有，见上） |
 * | **这份数据的主人是谁**（`LOCAL_PATHS` 那张前缀表） | `requestLocal`（`client.ts`）与顶栏那条状态 | **如实报错、不回退**（会话数据在本机，换源 = 让用户以为会话丢了） |
 *
 * 两者**都**打边车那台机器，但**开关不同**（轮次那条链**没有**开关了——它的落点只有一个，
 * 见文件头；本机权威面还有 `VITE_LOCAL_DATA` 这条逃生门）、
 * 判定不同（`isSidecarPath` / `isLocalPath`）、措辞也不同（"这一轮跑不了" / "数据没换源"）——
 * 合并成一个概念，其中一条纪律必然写错，所以刻意分成两段。
 * 上面那张"明确不归边车"的表说的是**对话轮次那条链**：`/conversations`、`/settings`
 * 这些如今仍然不在 `isSidecarPath` 里，但它们归**本机权威面**（`LOCAL_PATHS`）。
 *
 * 两条基址的来路也各说一句：轮次那条链原先读**构建期常量**，本机权威面拿的是
 * **壳里问到的真实端口** ——M2 起两边统一走 `sidecarBase()`（壳里优先）。
 */

import { API_BASE } from './client'

/** 边车默认地址（可被构建期 `VITE_SIDECAR_URL` 覆盖）。 */
export const DEFAULT_SIDECAR_BASE = 'http://127.0.0.1:8765'

/**
 * 这一片**唯一**归边车的接口类别：对话轮次的流式端点。
 *
 * 它现在**只在边车上有**（服务器档那一份 2026-10-05 随 NAS 网页端退役，
 * 见文件头）：所以这个常量只剩"这一族叫什么"的意思，`sidecarStatus().reason`
 * 拿它说清打的是哪个基址。
 */
export const SIDECAR_TURN_PATH = '/chat/stream'

/** 边车那一侧对应的路径（`remote_clients.py` 打的就是这个）。 */
export const SIDECAR_STREAM_PATH = '/turn/stream'

/**
 * 边车那台的**审批决定**端点（`app/sidecar.py` 的 `POST /turn/approvals/{id}`）。
 * 挂 `SIDECAR_STREAM_PATH` 的兄弟位置，是因为两者服务的是同一条轮次：流停在边车的
 * `wait` 上，决定就必须送回**那台**。（原先它有个服务器的镜像
 * `/chat/approvals/{id}`，那一份随本轮退役一起没有了。）
 */
export const SIDECAR_APPROVAL_PATH = '/turn/approvals'

/** 探测结果的缓存时长：太短会每轮都探、太长会让"边车刚起来"要等。 */
export const PROBE_TTL_MS = 5_000

/**
 * 历史条数的前端上限（与边车 `MAX_HISTORY_MESSAGES` 同一口径）。
 *
 * 为什么要带历史：边车那一轮就按请求体里这份历史跑（它不自己去翻会话库），
 * 不带历史就是**失忆的一轮**（用户立刻感觉到"它忘了上文"，界面还看不出来）。
 * 上限是**提示词预算**：别把整个会话灌进去。
 */
export const MAX_HISTORY_MESSAGES = 20

/**
 * 本机权威面的**前缀表**（M2 §4.2 那张白名单，逐条照抄）。
 *
 * 一个路径归不归本机**只在这里判**（`isLocalPath`）：`client.ts` 的 `requestLocal`
 * 与顶栏那条状态都读它，其余模块不许自己拼基址 ✓。按**路径段**匹配
 * （`/notes` 认 `/notes/folders/x`，但不认 `/notes-old` ✗），所以多加一个域＝往这张表
 * 里加一行，而不是去各模块里改字符串 ✓。
 *
 * ⚠️ 与方案原文的一处**实差别**：方案那张表写的是 `/schedules`，实际端点前缀是
 * `/scheduled-tasks`（`api/v1/router.py` 里 `schedules.router` 的 prefix ✓，
 * 界面这边 `api/schedules.ts` 也逐条用它 ✓）。照方案写会让**定时任务继续绕道 NAS** ✗，
 * 而 NAS 上那份库里根本没有本机的这些行 ✓ —— 写实的那一个。
 *
 * 两条只读端点（`/chat/context-usage` / `/conversations/{id}/events`）也在表里：
 * 本机档在 `local.chat_reads` 上**薄重声明**了它们（只读本机数据）✓。
 * `/chat/commands`（2026-10-05 加）同一条理由：命令目录与技能就在这台机器上，
 * 而桌面真正执行那一轮的是**边车** —— 目录与执行同源，`local_router` 那一侧
 * 同一轮挂了它。
 *
 * ## 2026-10-05 补的五条（技能 / 插件包 / 沙箱 / 站点图标 / 用量）
 *
 * 本机档那张白名单早就挂了这五族的端点（`local_router`：`skills` / `plugins` /
 * `sandbox` / `site_icons` / `local.stats_reads`），这张表却一直没跟上 —— 于是
 * 任何走 `requestLocal` 的调用点都会**先吃一条"不在 LOCAL_PATHS 里"的警告**，
 * 再照常落到本机（`resolveLocalBase` 对表外路径只警告不拦）。补上是让这张表
 * 重新等于"本机档到底服务什么"。
 *
 * ⚠️ 第二处**实差别**（与上面 `/scheduled-tasks` 那条同一类，也是"写实的那一个"）：
 * 用量那一条写的是 `/stats/usage` 而**不是** `/stats`（施工单原文写的是 `/stats`）。
 * 理由是同一条——本机档只**薄重声明**了 `/stats/usage` 这一条（`local.stats_reads`），
 * 而 `/stats/dashboard` 数的是知识库的文档与任务、**不挂本机档**（见
 * `api/v1/router.py` 里 `local_router` 那段说明）。表里写 `/stats` 会把
 * `/stats/dashboard` 一并卷成"本机权威面" —— 那样概览页在壳里会 404，
 * 而它本来就是服务器那份统计的正确入口。
 */
export const LOCAL_PATHS = [
  '/conversations',
  '/notes',
  '/settings',
  '/model-registry',
  '/workspaces',
  '/scheduled-tasks',
  '/mcp-servers',
  '/memory',
  '/chat/context-usage',
  // 命令目录：命令与技能在本机，执行那一轮也在本机（边车）——目录与执行同源
  '/chat/commands',
  // 2026-10-05 补的五族（上一条注释里写了为什么要补、以及 `/stats/usage` 的边界）
  '/skills',
  '/plugins',
  '/sandbox',
  '/site-icons',
  '/stats/usage',
  // 网页那一族（`/web/page` 抓正文、`/web/embed-check` 探嵌入）：代抓的是**这台机器**，
  // SSRF 闸（内网 / 本机地址一律拒）也在本机 —— 绕道 NAS 等于把"本机代取"这件事说反了
  // （那台上既不认这些地址、也不会替这台机器守那道闸），所以它归本机权威面。
  '/web',
  // `/local/status`（本机档状态）与阶段 5 的 `/local/import*`
  '/local',
] as const

function env(): Record<string, string | undefined> {
  return (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env ?? {}
}

/**
 * 边车基址：去尾斜杠（拼路径时统一由这里保证只有一层斜杠）。
 *
 * **两处来源、一处判定**：壳里问到的真实端口优先 ✓（`ensureLocalBase` 问的
 * `sidecar_info`，见 M2 那一段），问不到才退回构建期那份 `VITE_SIDECAR_URL` / 常量 ✓
 * （浏览器开发形态 ✓）。
 * 为什么壳里那份优先：端口 8765 被占时边车会顺延到 8766-8769
 * （`desktop/src-tauri/src/sidecar.rs` 的 `PORT_RANGE`）✗，而构建期常量永远是 8765 ✗
 * ——原先这只影响"这一轮走哪条链"，M2 之后本机权威面**也**在边车那台上，
 * 打错端口就是"本机后端明明在跑、会话却看不见" ✓。
 */
export function sidecarBase(): string {
  return shellBase ?? buildtimeBase()
}

/** 构建期那份基址（浏览器开发形态的落点；见 `sidecarBase` 的说明）。 */
function buildtimeBase(): string {
  return (env().VITE_SIDECAR_URL || DEFAULT_SIDECAR_BASE).replace(/\/+$/, '')
}

/**
 * "显式关"的字面量判定（`VITE_LOCAL_DATA` 那一处用，见 `localDataEnabledFrom`）。
 *
 * 为什么单独抽成纯函数（而不是把 `env()` 读在里头）：判据是这一片最容易写错的一位 ✓
 * ——`1/true/yes` 那套是"默认关"时代的写法 ✗（只认真值、其余全关 ✗），
 * 现在要求的是**反过来**：`0` / `false` / `no` / `off`（大小写不敏感 ✓、前后空白忽略 ✓）
 * 才关 ✓，**别的值（含不设、含空）一律算开着** ✓。抽出来之后用例能逐个字面量钉住它 ✓，
 * 不必去动构建期的环境变量 ✗（`vi.stubEnv` 改不到本模块读的那一份，见 `setLocalDataForTest` ✓）。
 */
function explicitlyOff(raw: string | undefined): boolean {
  const value = (raw ?? '').trim().toLowerCase()
  return value === '0' || value === 'false' || value === 'no' || value === 'off'
}

/** 用例用：强制本机数据面的开关值（`undefined` = 不覆盖，按 env 判）。 */
let localOverride: boolean | undefined

/**
 * 用例用：强制开关值（传 `undefined` 恢复按 env 判）。
 *
 * 为什么不靠 `vi.stubEnv`：它只改**测试文件自己**那份 `import.meta.env`，
 * 改不到本模块读的那一份（实测：开了 stub 仍然读到空值）。给一个显式的窄接口更老实，
 * 也让人一眼看出"这是给用例开的门"。
 */
export function setLocalDataForTest(value: boolean | undefined): void {
  localOverride = value
}

/** 本机数据面开关的字面量判据（见 `explicitlyOff`）。 */
export function localDataEnabledFrom(raw: string | undefined): boolean {
  return !explicitlyOff(raw)
}

/**
 * 本机权威面这个开关（**默认开** ✓）。
 *
 * 不设 `VITE_LOCAL_DATA` → 会话 / 笔记 / 设置 这些直连本机边车 ✓；
 * `VITE_LOCAL_DATA=0` → 全部回服务器 ✓（**排障/对比用的逃生门** ✓：
 * 想看看"同一批数据走服务器那条链长什么样"、或者怀疑本机库有问题时，改一个环境变量即可 ✓）。
 * 被显式关掉时**绝不静默** ✗：顶栏那条状态会一直写着"服务器"与原因 ✓（见 `localStatus`）。
 *
 * **与对话轮次那条链互不牵连** ✓：轮次那边只有一个落点（边车）、**没有**开关
 * （原先那个 `VITE_SIDECAR_TURNS` 已随回退一起退役，见文件头）；
 * 这一条管的是"这份数据归谁"，与"这一轮在哪台机器上跑"是两件事 ✓。
 */
export function localDataEnabled(): boolean {
  if (localOverride !== undefined) return localOverride
  return localDataEnabledFrom(env().VITE_LOCAL_DATA)
}

/** 这个路径归**对话轮次那条链**吗？（判定只有这一处） */
export function isSidecarPath(path: string): boolean {
  const clean = path.split('?')[0].replace(/\/+$/, '')
  return clean === SIDECAR_TURN_PATH
}

/**
 * 这个路径归**本机权威面**吗？（判定只有这一处，表在 `LOCAL_PATHS`）
 *
 * 与 `isSidecarPath` 是两件事：本条说的是"这份数据的主人在本机" ✓，
 * 那条说的是"这一轮对话在哪台机器上跑" ✓（见文件头那张表）。
 */
export function isLocalPath(path: string): boolean {
  const clean = path.split('?')[0].replace(/\/+$/, '')
  // 按**路径段**比：`/notes` 要认 `/notes/xxx`，但 `'/notes-old'` 不是笔记域的路径 ✗
  return LOCAL_PATHS.some((prefix) => clean === prefix || clean.startsWith(`${prefix}/`))
}

/**
 * 这个路径该打的基址。
 *
 * **对话轮次那条链不再看开关**（2026-10-05）：它只有一个落点（边车），而"开关关掉"
 * 那种情形在 `resolveTurnTarget` 里是一条显式的失败——这里再回 `API_BASE` 就与
 * 那段话自相矛盾了（那个 URL 两档都不存在）。
 */
export function baseForPath(path: string): string {
  return isSidecarPath(path) ? sidecarBase() : API_BASE
}

/**
 * 这一轮要打的那个落点——**只有一个**（本机边车），所以不再有"走哪条链"那个判别位。
 *
 * 拿不到它就是一条显式的失败（`resolveTurnTarget` / `resolveApprovalTarget` 抛
 * `TurnUnavailableError`）：服务器那条链已经退役，没有"换个地方跑"这一说。
 */
export interface TurnTarget {
  /** 实际要打的基址（壳里问到的真实端口，8765 被占会顺延）。 */
  base: string
  /** 实际要打的完整 URL。 */
  url: string
}

interface ProbeState {
  available: boolean | null
  reason: string
  at: number
}

const probe: ProbeState = { available: null, reason: '', at: 0 }

/**
 * **探活结论被复位过几次**（`resetSidecarProbe()` 每次 +1）——与 `api/backup.ts` 的
 * `generation`、`api/provider.ts` 的那一位**同形、同一条理由**（那两处的注释写得更细）。
 *
 * 这条链上有一个**跨越复位的空档**：`sidecarAvailable()` 的 `fetch` 不在调用点上
 * （要先 `ensureLocalBase()` 问壳），所以"那一刻发出去的探活"可能**在复位之后**才回来；
 * 那份结论（`probe.available/reason/at`）一旦写进去，就会把复位之后重新探的那份盖掉
 * ——表现是"复位之后顶栏又闪回上一次的结论"，用例里则是跨用例干扰
 * （2026-10-02 门禁抓到的抖动就是这一族；实测：复位后放行旧的探活，`localStatus()`
 * 会被写成"走本机"，而它本该是"还没探过"）。
 *
 * 复位在**生产代码里没有调用点**（`resetSidecarProbe` 是用例出口），所以这一位不影响
 * 真机行为；它把"复位"这件事的语义补完整：**复位之后，之前发出去的一律不算数**。
 */
let probeGeneration = 0

/**
 * 给界面读的状态位（这一轮打哪儿不是静默的）。
 *
 * `reason` **三种状态都说得清**（原先那第四种"被显式关掉"随 `VITE_SIDECAR_TURNS`
 * 一起退役了，见文件头）：
 *
 * 1. **走边车** —— 说打的是哪个基址；
 * 2. **边车没起来** —— 带上探测给的原因；
 * 3. **还没探过**（`available: null`）—— 不猜边车是好是坏，只说"还没探过、
 *    默认会先试边车"。后两种都是**打不了**（`resolveTurnTarget` 抛
 *    `TurnUnavailableError`）：服务器那条链已退役，没有回退可报（见文件头）。
 */
export function sidecarStatus(): {
  base: string
  available: boolean | null
  reason: string
} {
  const base = sidecarBase()
  if (probe.available === true) {
    return {
      base,
      available: true,
      reason: `走边车：对话轮次打 ${base}${SIDECAR_STREAM_PATH}`,
    }
  }
  if (probe.available === false) {
    return {
      base,
      available: false,
      reason: `${probe.reason || '边车不可用'}；这一轮跑不了（服务器那条链已退役，没有别的落点）`,
    }
  }
  return {
    base,
    available: null,
    reason: `还没探过边车（默认先试 ${base}；服务器那条链已退役，探不到就打不了这一轮）`,
  }
}

/**
 * 用例用：清掉探测缓存（不然几条断言会互相影响）。
 *
 * M2 起它还要清**本机权威面**那份状态（壳里问到的基址、问壳的结论、那条 info 打过没有）——
 * 那些也是模块级缓存，用例之间必须互不影响 ✓。
 */
export function resetSidecarProbe(): void {
  // 复位也**作废所有在飞的探活**（见 `probeGeneration`）：复位之后回来的结论不许写进来
  probeGeneration += 1
  probe.available = null
  probe.reason = ''
  probe.at = 0
  shellBase = null
  shellLookup = 'unasked'
  shellReason = ''
  basePromise = null
  offLogged = false
}

/**
 * 边车活着吗（问壳拿基址 + `GET /health`，5 秒超时）。
 *
 * 用 `AbortSignal.timeout` 而不是让它慢慢连：不可达时 TCP 连接可能挂很久，
 * 而这里要的是**快速回退**（用户点一下对话，不该先等 3 秒超时）。
 *
 * M2 起它做两件事：① 先 `ensureLocalBase()` —— 基址要问壳（端口顺延到 8766 时，
 * 构建期常量是错的 ✗，见文件头）；② 壳里**明确说"没有边车"**（或问壳这一步就失败了）时
 * **不探** ✗ —— 那种情况下 8765 上可能坐着**别人的**进程，探到 200 只会把
 * "本机后端没起来"说成"好着呢" ✓。浏览器形态（没有壳）照旧去探那份常量基址 ✓
 * （开发时有边车就用它，没有就走服务器）。
 *
 * 结论是**本模块里唯一一份**探测状态 ✓：轮次那条链（"打不到就换个地方跑"）与
 * 本机权威面（"打不到就如实报错"）读的是同一次测量 ✓，两种处置 ✓。
 */
export async function sidecarAvailable(options: { force?: boolean } = {}): Promise<boolean> {
  await ensureLocalBase()
  // **这一探是什么时候发起的**：结论回来时对一下，复位之后就不许写（见 `probeGeneration`）
  const gen = probeGeneration
  if (shellLookup === 'missing' || shellLookup === 'failed') {
    if (gen !== probeGeneration) return false
    probe.available = false
    probe.reason = shellReason
    probe.at = Date.now()
    return false
  }
  const now = Date.now()
  if (!options.force && probe.available !== null && now - probe.at < PROBE_TTL_MS) {
    return probe.available
  }
  const base = sidecarBase()
  // 这一探**这次测出来的结论**（要不要写进缓存另说：复位之后就不写，见 `probeGeneration`）
  let available: boolean
  try {
    const response = await fetch(`${base}/health`, { signal: AbortSignal.timeout(5_000) })
    available = response.ok
    if (gen === probeGeneration) {
      probe.available = available
      probe.reason = available ? '' : `边车 /health 返回 HTTP ${response.status}`
    }
  } catch (error) {
    available = false
    if (gen === probeGeneration) {
      probe.available = false
      probe.reason = `边车不可达（${base}）：${error instanceof Error ? error.message : String(error)}`
    }
  }
  if (gen === probeGeneration) probe.at = Date.now()
  return available
}

/**
 * 对话轮次**没有落点**时抛的那个错（边车没跑起来——原先还有"被显式关掉"那一种，
 * 随 `VITE_SIDECAR_TURNS` 一起没有了，见文件头）。
 *
 * 单独一个类而不是一句 `Error`（与 `LocalUnavailableError` 同一条理由）：调用方要能区分
 * "这台机器上的对话后端没跑起来"与"后端回了一个错"——前者要劝用户重启壳/看日志，
 * 后者是那条请求本身的问题。界面上这就是那条错误提示的全部内容
 * （`liveTurn.begin` 的 `failWith` / `ApprovalBar` 的 `notifyError`）。
 *
 * 它**不是回退**：原先打不到边车时这一轮会落到服务器 `/chat/stream`，
 * 而那条链 2026-10-05 起两档都没有了（见文件头）——所以这里如实失败，不换源。
 */
export class TurnUnavailableError extends Error {
  constructor(detail: string) {
    super(
      `对话跑不起来：${detail}；` +
        `这一轮只能在本机后端（边车）上跑，而服务器那条链已退役——没有别的落点`,
    )
    this.name = 'TurnUnavailableError'
  }
}

/**
 * 这一轮对话该打哪里（异步：先问壳拿基址、再探边车）。
 *
 * **只有一个落点**：边车活着就给 `{base, url}`（打 `/turn/stream`），否则**抛**
 * `TurnUnavailableError`（同时 `console.warn` 一条）——"打不到"的原因就是探测给的那句
 * （`probe.reason`）。原先这里回退服务器 `/chat/stream`，那条端点 2026-10-05 起
 * 两档都没有了（见文件头），围着它的那个开关也一并退役了。
 */
export async function resolveTurnTarget(): Promise<TurnTarget> {
  // 基址**先问壳**再取（`sidecarAvailable` 里也会问一次，那一步是幂等的缓存）：
  // 端口顺延到 8766-8769 时，下面这个 `base` 必须是壳里那个真实端口
  await ensureLocalBase()
  const base = sidecarBase()
  if (await sidecarAvailable()) {
    return { base, url: `${base}${SIDECAR_STREAM_PATH}` }
  }
  const reason = probe.reason || '边车不可用'
  console.warn(`[sidecar] ${reason}；不回退服务器（/chat/stream 两档都没有了）`)
  throw new TurnUnavailableError(reason)
}

/**
 * 审批决定该打到哪边——**与 `resolveTurnTarget` 同一套选址**（只有边车一个落点）。
 *
 * 为什么要这一条（交接文档点名的缺口）：流在边车那台停在 `wait` 上等这一下，
 * 而 `decideApproval` 原先写死 `/chat/approvals/{id}` 打服务器——服务器根本不知道
 * 这个 id（404），那一轮只能等超时按拒绝走。那个服务器镜像端点如今也退役了，
 * 所以这里同样只剩边车：打不到就抛（界面上如实说"这条确认送不到"）。
 */
export async function resolveApprovalTarget(approvalId: string): Promise<TurnTarget> {
  const encoded = encodeURIComponent(approvalId)
  await ensureLocalBase()
  const base = sidecarBase()
  if (await sidecarAvailable()) {
    return { base, url: `${base}${SIDECAR_APPROVAL_PATH}/${encoded}` }
  }
  const reason = probe.reason || '边车不可用'
  console.warn(`[sidecar] ${reason}；这条确认没有别处可送（/chat/approvals 两档都没有了）`)
  throw new TurnUnavailableError(reason)
}

/**
 * 前端那份请求体（`ChatPayload`）里**边车会用到的那几个字段**。
 *
 * 名字里的 "Server" 是那会儿的叫法（这条适配原先写在"服务器请求体 → 边车请求体"之间），
 * 服务器那条链 2026-10-05 退役之后它只剩一个输入：界面那一份 `ChatPayload`。
 *
 * 刻意**不加索引签名** `[key: string]: unknown`：加了之后 `ChatPayload` 反而不满足它
 * （TS 要求源类型也有索引签名）→ 调用点会报"missing index signature"。
 * 用"子集 + 结构化类型"就是对的：`ChatPayload` 有 `query` 就够了，多余字段本来就要被丢掉。
 */
export interface ServerTurnPayload {
  /** 用户这一句（界面这一侧叫 `query`，边车那边叫 `message` —— 适配就在这一处做）。 */
  query: string
  kb_ids?: string[]
  history?: { role: 'user' | 'assistant'; content: string }[]
  /**
   * 这一轮归属的会话。**必须带**（2026-09-30 修的真 bug）：边车拿它把这一轮写进
   * **本机会话库**；不带 = 这一轮**不入库**——刷新就没了。
   */
  conversation_id?: string
}

/**
 * 界面的请求体 → 边车请求体（`sidecar.TurnIn`：`message` / `kb_ids` / `history` / `conversation_id`）。
 *
 * 只挑边车认识的字段：其余（`mode`、`permission`、附件……）边车这一侧没有对应语义，
 * 硬塞过去只会被忽略（或更糟：让人以为它们生效了）。
 * **`conversation_id` 不属于"其余"那一类**：边车的 `TurnIn` 有它，落库那一环全靠它——
 * 之前漏在门外，实测表现是每一轮都写着"未带会话 id，本轮未写回"（真丢数据）。
 * `workspace` 由边车自己按启动参数定 —— 前端不指定，避免"浏览器决定本机路径"这种危险默认。
 *
 * **历史带上**（最近 `MAX_HISTORY_MESSAGES` 条）：边车按这一份跑那一轮（它不自己去翻会话库），
 * 不带就是失忆的一轮。
 */
export function toSidecarTurnBody(payload: ServerTurnPayload): Record<string, unknown> {
  // `query` → `message`：**字段名的适配只在这一处**（界面叫 query、边车叫 message）
  const body: Record<string, unknown> = { message: payload.query }
  if (payload.kb_ids?.length) body.kb_ids = payload.kb_ids
  // 会话 id 照传：边车用它把这一轮写进本机库（不带就静默丢这一轮，见上面的说明）
  if (payload.conversation_id) body.conversation_id = payload.conversation_id
  const history = (payload.history ?? [])
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .filter((item) => typeof item.content === 'string' && item.content.trim().length > 0)
    .slice(-MAX_HISTORY_MESSAGES)
  if (history.length) body.history = history
  return body
}

/* ============================================================== 本机权威面（M2 阶段 4）
 *
 * 这一段管的是**"这份数据的主人是谁"**（见文件头那张表），与上面几段"这一轮对话在哪台
 * 机器上跑"是两件事。会话 / 笔记 / 设置 / 模型注册 / 工作区 / 定时任务 / MCP / 记忆
 * 从 M2 起落**本机**（边车进程里的 `local_router`，库在 `%APPDATA%\com.kylab.desktop\kylab.db`），
 * 所以界面打它们要**直连边车** ✓，不再绕道 NAS ✓。
 *
 * ## 三条出路（`resolveLocalBase`，也就是两条回退纪律的落点）
 *
 * | 情形 | 打哪 | 可见性 |
 * | --- | --- | --- |
 * | 本机后端在（`sidecar_info` 拿到基址 + `/health` 通） | 边车 `http://127.0.0.1:<port>/api/v1<path>` | 顶栏写「本机」+ 基址 |
 * | **壳里**本机后端没起来 | **抛错，绝不换源** | 顶栏写「本机后端未启动」+ 原因；错误文案就是方案那句原话 |
 * | `VITE_LOCAL_DATA=0` 显式关 | 服务器 | 顶栏写「服务器」+ 是哪个变量关的（**不许静默**） |
 *
 * "壳里"这三个字是关键：**本机后端是桌面壳拉起来的那个进程**（`sidecar.rs`）——
 * 壳不在（浏览器里开发、NAS 上那份网页端）的时候，"本机档"这一档根本不成立，
 * 那里读的就是服务器上的数据（顶栏照样写清楚，不是静默换源 ✗）。
 * 而壳在、边车却没起来时**必须如实报错** ✗✗：会话数据在本机，
 * 静默换到服务器会让用户看到 NAS 上**旧的那一份**，表现得就像"我的会话不见了" ✓
 * ——这也是方案把"边车不可达就回退服务器"这条**只**留给检索/模型的原因 ✓。
 * （2026-10-05 补一句：那一族留给检索/模型的回退也**没了**——`/chat/stream` 两档都不再对外，
 * 所以对话那条链如今同样"打不到就如实报错"，见文件头那一段。）
 *
 * ## 为什么基址要问壳（而不是继续读构建期常量）
 *
 * 边车端口 8765 被占时会顺延到 8766-8769（`desktop/src-tauri/src/sidecar.rs` 的
 * `PORT_RANGE`），而构建期常量永远是 8765 —— 原先这只让"一轮对话打错地方"
 * （还有回退兜着），M2 之后本机权威面**也**在边车那台上，打错端口就是
 * "本机后端明明在跑、会话却看不见" ✗。`sidecar_info` 命令（`main.rs`）就是为这件事
 * 注册的：拿它的 `base`，拿不到才退回常量（浏览器开发形态 ✓）。
 */

/** 壳那边 `invoke` 的形状（只声明我们用到的那一点）。 */
type TauriInvoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>

/** 壳里 `sidecar_info` 的回答（`desktop/src-tauri/src/sidecar.rs::Info`）。 */
interface ShellSidecarInfo {
  port?: number
  /** `http://127.0.0.1:<port>`。 */
  base?: string
  on_default_port?: boolean
}

/** 问壳要基址的结论（`unasked` = 还没问过）。 */
type ShellLookup = 'unasked' | 'ready' | 'missing' | 'failed' | 'no-shell'

/** 壳里问到的**真实**边车基址；`null` = 没问到（原因在 `shellReason` 里）。 */
let shellBase: string | null = null

let shellLookup: ShellLookup = 'unasked'

/** 问壳那一趟的结果（拿不到基址时的原因，会被拼进 `reason` 与错误文案里）。 */
let shellReason = ''

/** 问壳这件事只做一次（`ensureLocalBase` 的缓存）。 */
let basePromise: Promise<string> | null = null

/** "被显式关掉"那条 `console.info` 只打一次（每个请求都打会把控制台刷满）。 */
let offLogged = false

/**
 * 壳那边的 IPC（`invoke`）；**不是桌面壳**时返回 `null`。
 *
 * Tauri v2 在 `withGlobalTauri: true` 下把 `window.__TAURI__` 挂出来
 * （`desktop/src-tauri/tauri.conf.json` 里就是开着的），没开那个开关时
 * `window.__TAURI_INTERNALS__` 也一直在（核心桥，两个版次都有）。
 * 两个都读、都读不到就老实说"这不是壳" ✓ —— 浏览器开发形态与 NAS 网页端走的就是这一支 ✓。
 */
function tauriInvoke(): TauriInvoke | null {
  const host = globalThis as unknown as {
    __TAURI__?: { core?: { invoke?: TauriInvoke } }
    __TAURI_INTERNALS__?: { invoke?: TauriInvoke }
  }
  return host.__TAURI__?.core?.invoke ?? host.__TAURI_INTERNALS__?.invoke ?? null
}

/** 这一份界面跑在桌面壳里吗？（能调壳的命令 = 壳在） */
export function inDesktopShell(): boolean {
  return tauriInvoke() !== null
}

/**
 * 拿**真实**基址：壳里问一次 `sidecar_info`，问不到退回构建期那份常量。
 *
 * **从不抛** ✓：拿不到基址是常态（浏览器开发形态），说清原因、退回常量就是了 ✓。
 * "这地址对不对"与"它答不答应"是两件事：后者归 `localAvailable()` ✓。
 * 幂等且只问一次（`force` 可以再问一遍，给顶栏那颗「重试」用）。
 */
export function ensureLocalBase(options: { force?: boolean } = {}): Promise<string> {
  if (options.force) basePromise = null
  basePromise ??= askShellForBase()
  return basePromise
}

/** 问壳那一趟本身（`ensureLocalBase` 的实现；名字避开对外的 `resolveLocalBase`）。 */
async function askShellForBase(): Promise<string> {
  const invoke = tauriInvoke()
  if (!invoke) {
    shellLookup = 'no-shell'
    shellBase = null
    shellReason = ''
    return buildtimeBase()
  }
  try {
    const info = (await invoke('sidecar_info')) as ShellSidecarInfo | null
    if (info?.base) {
      shellBase = info.base.replace(/\/+$/, '')
      shellLookup = 'ready'
      shellReason = ''
      return shellBase
    }
    // 壳在跑，但它说"没有边车"：那是**本机档真的没有后端**（不是探不通）
    shellBase = null
    shellLookup = 'missing'
    shellReason = '桌面壳里没有正在运行的边车（sidecar_info 返回空）'
  } catch (error) {
    shellBase = null
    shellLookup = 'failed'
    shellReason = `问壳要边车地址失败：${error instanceof Error ? error.message : String(error)}`
  }
  return buildtimeBase()
}

/**
 * 边车（本机后端）没起来时那句话——**方案 §4.3 的原话，别改写**。
 *
 * 与 `TurnUnavailableError` 那句（对话跑不起来）是**两套口径**：这一句说的是"数据在
 * 本机、没有换源"——混着说会让用户以为会话在服务器上还有一份新的（其实那上面是旧的一份）。
 * 前者是"换个地方跑"，而**对话那条链已经没有"别的地方"了**（见文件头），所以它如今也
 * 不再是回退，而是如实失败。
 */
export function localDownReason(detail: string): string {
  return `本机后端未启动：${detail || '边车没有应答'}；会话数据在本机，未回退服务器`
}

/**
 * 被显式关掉时把用户写的那个值带出来（**本机面**那一处；对话那条链没有开关了，
 * 所以这份措辞只服务 `VITE_LOCAL_DATA`，见文件头）。
 */
function explicitLocalOffReason(): string {
  const raw = env().VITE_LOCAL_DATA
  const shown = raw === undefined || raw.trim() === '' ? '' : `=${raw.trim()}`
  return (
    `本机数据被显式关掉（VITE_LOCAL_DATA${shown}）` +
    `——会话 / 笔记 / 设置 这些走服务器 ${API_BASE}` +
    '（排障用的逃生门；删掉这个变量就回到本机）'
  )
}

/** 浏览器形态下那句话（**不是回退**，是"这一档不成立"）。 */
function browserShapeReason(): string {
  return (
    '浏览器里没有本机后端（它由桌面壳拉起）' +
    `：这一份界面读的是服务器上的数据 ${API_BASE}；桌面壳里读的才是本机库`
  )
}

/** 本机后端活着，这句是给人看的落点。 */
function localReadyReason(): string {
  return `走本机：会话 / 笔记 / 设置 这些打 ${sidecarBase()}${API_BASE}`
}

/** 还没探过（**不猜**好坏，与 `sidecarStatus` 同一条口径）。 */
function localUnprobedReason(): string {
  return `还没探过本机后端（默认打 ${sidecarBase()}${API_BASE}）`
}

/**
 * 本机权威面现在的状态（顶栏那条状态条读它，三态都要说得清）。
 *
 * - `kind: 'local'` ✓ 本机后端在（`available` 为 `null` = 还没探过）；
 * - `kind: 'server'` ✓ 走服务器：**被显式关掉**、或**浏览器形态里根本没有本机后端**；
 * - `kind: 'unavailable'` ✗ 壳里本机后端没起来（这时本机权威面会**如实报错**，不换源）。
 */
export interface LocalDataStatus {
  kind: 'local' | 'server' | 'unavailable'
  enabled: boolean
  available: boolean | null
  base: string
  /** 给人看的一句话（**三态都必须非空** ✓，顶栏就显示它）。 */
  reason: string
}

export function localStatus(): LocalDataStatus {
  const base = sidecarBase()
  if (!localDataEnabled()) {
    return {
      kind: 'server',
      enabled: false,
      available: null,
      base,
      reason: explicitLocalOffReason(),
    }
  }
  if (probe.available === false) {
    const shellSaidNo = shellLookup === 'missing' || shellLookup === 'failed'
    if (shellSaidNo) {
      return {
        kind: 'unavailable',
        enabled: true,
        available: false,
        base,
        reason: localDownReason(shellReason || probe.reason),
      }
    }
    if (shellLookup === 'no-shell') {
      return { kind: 'server', enabled: true, available: false, base, reason: browserShapeReason() }
    }
    return {
      kind: 'unavailable',
      enabled: true,
      available: false,
      base,
      reason: localDownReason(probe.reason),
    }
  }
  if (probe.available === true) {
    return { kind: 'local', enabled: true, available: true, base, reason: localReadyReason() }
  }
  return { kind: 'local', enabled: true, available: null, base, reason: localUnprobedReason() }
}

/**
 * 本机后端活着吗（问壳拿基址 + `GET /health`，结论按 `PROBE_TTL_MS` 缓存）。
 *
 * 与 `sidecarAvailable()` **是同一次测量** ✓：轮次那条链拿它决定"这一轮换个地方跑" ✓，
 * 本机权威面拿它决定"如实报错还是照常走" ✓ —— 两个名字点的是同一份状态，
 * 因为"边车在不在"只有一个答案 ✓（各探一次的话，界面上会出现
 * "对话说在、会话说不在一台"这种自相矛盾的样子 ✗）。
 */
export async function localAvailable(options: { force?: boolean } = {}): Promise<boolean> {
  return sidecarAvailable(options)
}

/**
 * 本机权威面这一条请求该打哪个**基址前缀**（含 `/api/v1`；完整 URL = 它 + `path`）。
 *
 * 异步是因为要先确认本机后端在不在。三条出路见上面那张表；
 * **不做"全局把 `request()` 的基址换掉"** ✗ —— 那会让几十个服务器调用点一起变，
 * 而其中一半（账号 / 知识库 / 技能……）的数据本来就在服务器上 ✓。
 *
 * 为什么回的是**基址**而不是拼好的 URL：`client.ts` 里还有一条 multipart 的路
 * （`uploadLocal`，笔记配图）要走同一套选址 —— 拼 URL 那一步各拼各的，
 * 判定仍然只有这一处 ✓。
 */
export async function resolveLocalBase(path: string): Promise<string> {
  if (!isLocalPath(path)) {
    // 不在前缀表里的路径不该走这里（调用点写错了）。**只警告不拦**：
    // 运行时把一条能用的请求变成异常，比多打一行日志更糟
    console.warn(
      `[sidecar] requestLocal('${path}') 不在 LOCAL_PATHS 里：本机权威面的判定只认那张前缀表`,
    )
  }
  if (!localDataEnabled()) {
    // **显式关不是失败** ✓，但也绝不静默 ✓（顶栏那条状态一直在；这里再留一条 info 给排障）
    if (!offLogged) {
      offLogged = true
      console.info(`[sidecar] ${explicitLocalOffReason()}`)
    }
    return API_BASE
  }
  // `localAvailable()` 内部会先把基址问清楚（`ensureLocalBase`），所以 `shellLookup`
  // 到这里一定已经有结论了 —— 下面那两支都靠它
  if (await localAvailable()) {
    return `${sidecarBase()}${API_BASE}`
  }
  if (shellLookup === 'no-shell') {
    // 浏览器里没有"本机后端"这个概念（它是桌面壳拉起来的进程）——数据本来就在服务器上，
    // 这不是回退，是"这一档不成立" ✓。顶栏照样把这件事写出来 ✓（不静默）。
    return API_BASE
  }
  // 壳里、边车却没起来：**如实报错，绝不换源** ✗✗（见这一段开头那张表）
  throw new LocalUnavailableError(shellReason || probe.reason)
}

/**
 * 本机后端没起来时抛的那个错（`client.ts::requestLocal` 只抛它）。
 *
 * 单独一个类是为了让调用方**能区分**"本机后端没起来"与"后端回了一个错"
 * （前者要劝用户重启壳/看日志，后者是那条请求本身的问题）。
 */
export class LocalUnavailableError extends Error {
  constructor(detail: string) {
    super(localDownReason(detail))
    this.name = 'LocalUnavailableError'
  }
}

/**
 * 把**本机后端回的相对链接**贴到本机基址上（签名 URL 那几条：`<img>` / `<a>` /
 * `<iframe>` 都带不了鉴权头，只能靠链接本身带着授权）。
 *
 * 为什么必须有这一步：边车回的是相对地址（`/api/v1/...`）—— 在桌面壳里它会落到
 * **`app://localhost`** 上，而壳把 `/api/**` 转发给 **NAS** ✗，可这份文件在本机
 * （会话产物与笔记配图都在本机对象存储里）→ 表现是"图裂了 / 下载 404"。
 *
 * 不进本机档（显式关 / 浏览器里）时**原样返回** ✓：那两种形态里相对地址本来就落在
 * 同源上（网页端由 nginx、壳里由 `/api/**` 转发到 NAS），硬贴一个基址反而坏事。
 * 边车那一刻不在（探不通）时也原样返回：那时本机面整个不可用，顶栏已经写着原因了。
 */
export async function localizeUrl(url: string): Promise<string> {
  // 已经是绝对 URL（http/https/data/blob……）就一个字都不改
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return url
  if (!localDataEnabled()) return url
  if (!(await localAvailable())) return url
  return `${sidecarBase()}${url}`
}
