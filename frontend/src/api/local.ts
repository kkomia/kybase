/**
 * 本机档的**导入账**（`GET /api/v1/local/status`，M2 阶段 6 接线）。
 *
 * ## 为什么类型是**手写**的（不读 `schema.d.ts`）
 *
 * `/local/*` 这一族端点**不进服务器档的 OpenAPI**：它们是"只在本机档成立"的东西
 * （`local_router` 挂在边车上，而 `gen_api_types.py` 生成的是那一份服务器档的契约，
 * 生成物里没有它们）。阶段 5 已经把这件事登记过，这里照那条登记办——
 * **手写 + 写清出处**，别再promote成"没人维护的第二份契约"：
 * 字段名逐条对着 `backend/app/api/v1/local.py::LocalStatusOut` 抄，
 * 改了后端就回来改这里（`tests/unit/api/local.test.ts` 钉住要用的那三项）。
 *
 * ## 只用得着那三项里的两笔账（其余留给排障）
 *
 * 界面要的是**如实可见**（阶段 6 的施工口径）：
 *
 * | 字段 | 界面上的话 |
 * | --- | --- |
 * | `imports` | 最近几批导入：各是 `done` / `failed` / `rolled_back`、新建了多少条 |
 * | `unfinished_imports` | 有几笔导入**没跑完**（`planned`/`running`）：重跑同一来源即可续上 |
 * | `unimported_file_references` | 最近一批里**没随导入过来**的文件引用数（产物 + 消息附件） |
 *
 * 其余字段（`data_dir` / `database` / `database_bytes`…）也一并声明：
 * 状态条把库路径写进 `title`，排障第一眼要看的正是它。
 *
 * ## 阶段 7 补上两条（恢复向导要用）
 *
 * 导入的**进度**与**回滚**这两个端点 M2 就有（`ImportBatchOut`），只是一直没有人从界面
 * 调用：备份页的恢复向导正是"用快照当来源的一次导入"，所以进度读
 * `GET /local/import/{batch_id}`、回滚走 `POST /local/import/{batch_id}/rollback`——
 * 与 CLI 在另一个进程里开的批次查的是**同一份台账**。
 */

import { useEffect, useState } from 'react'

import { requestLocal } from './client'
import { inDesktopShell, localDataEnabled } from './sidecar'

/** 一个导入批次的摘要（`LocalStatusOut.imports[]`，字段与后端同形）。 */
export interface LocalImportBatch {
  batch_id: string
  /** `planned` / `running` / `done` / `failed` / `rolled_back`。 */
  state: string
  source: string
  /** 逐型计数：`created` / `skipped` / `messages` / `file_references`…（形状由后端定）。 */
  counts: Record<string, number | unknown>
  error: string
  updated_at: string | null
}

/** `/local/status` 的响应（本机档运行态，只读）。 */
export interface LocalStatus {
  deployment: string
  data_dir: string
  database: string
  database_exists: boolean
  database_bytes: number
  database_wal_bytes: number
  server_url: string | null
  imports: LocalImportBatch[]
  unfinished_imports: number
  unimported_file_references: number
  note: string
}

/**
 * 读一次本机档状态。
 *
 * 走 `requestLocal`（不是裸 `fetch`）：基址判定只有一处（`sidecar.ts`），
 * 而且**本机后端没起来时它抛 `LocalUnavailableError`** —— 那条纪律在这儿同样成立：
 * 会话数据的账在本机，读不到就该如实报，不许静默换源。
 *
 * 响应过一道**形状检查**（`imports` 必须是数组）：类型是手写的（没有生成物替我们守契约），
 * 而这条链上有真实的版本错配可能（界面比边车新）。不检查的代价是把"版本对不上"变成
 * 界面上一行 `Cannot read properties of undefined`——那是崩溃，不是交代。
 * 检查失败就抛，调用方（顶栏那条状态条）会把它写成"本机状态读不到：…"。
 */
export async function getLocalStatus(): Promise<LocalStatus> {
  const payload = await requestLocal<LocalStatus>('/local/status')
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.imports)) {
    throw new Error('本机状态响应不认识（没有 imports 这一项：是不是本机后端比界面老？）')
  }
  return payload
}

/** 批次状态的人话名字（认不出来就原样返回：后端加了新状态时界面不该崩）。 */
const BATCH_STATES: Record<string, string> = {
  planned: '已计划',
  running: '进行中',
  done: '已完成',
  failed: '失败',
  rolled_back: '已回滚',
}

export function batchStateLabel(state: string): string {
  return BATCH_STATES[state] ?? state
}

/**
 * 一段给人看摘要（`imports` / `unfinished_imports` / `unimported_file_references` 三笔账）。
 *
 * 抽成纯函数是为了能逐字钉住（`tests/unit/api/local.test.ts`）：
 * 这三句话是"数据在哪"的全部交代，写在组件里就只能靠渲染快照去验了。
 * **空库 / 没导过**时也要说得清（"还没导过"不是"导了 0 条"）。
 */
export function importAccountsText(status: LocalStatus): string {
  const parts: string[] = []
  const latest = status.imports[0]
  if (latest) {
    const created = Number(latest.counts.created ?? 0)
    const skipped = Number(latest.counts.skipped ?? 0)
    parts.push(
      `导入 ${status.imports.length} 批，最近一批${batchStateLabel(latest.state)}` +
        `（新建 ${created} / 跳过 ${skipped}）`,
    )
  } else {
    parts.push('还没导过旧会话')
  }
  parts.push(
    status.unfinished_imports > 0
      ? `${status.unfinished_imports} 批没跑完（重跑同一来源即可续上）`
      : '没有没跑完的批次',
  )
  parts.push(
    status.unimported_file_references > 0
      ? `${status.unimported_file_references} 个文件引用没随导入（文件还在来源服务器上）`
      : '没有未随导入的文件引用',
  )
  return parts.join(' · ')
}

/* ------------------------------------------------------------------ 进度与回滚（恢复向导用） */

/**
 * 一个批次的进度（`ImportBatchOut`）。
 *
 * 形状对着后端那个响应模型抄：`counts` 是**逐次型**的形状（后端那份 `counts_json` 原样），
 * 恢复那一趟会把 `counts["restore"]` 一段并进来——取那一段的唯一入口是
 * `api/backup.ts::restoreCountsOf`（它知道那一小段的字段）。
 */
export interface ImportBatch {
  batch_id: string
  /** `planned` / `running` / `done` / `failed` / `rolled_back`。 */
  state: string
  source: string
  dry_run: boolean
  counts: Record<string, unknown>
  error: string
  updated_at: string | null
}

/** 响应过一道形状检查：类型是手写的，而这条链上有真实的版本错配可能。 */
function looksLikeBatch(payload: unknown): payload is ImportBatch {
  if (!payload || typeof payload !== 'object') return false
  const record = payload as Partial<ImportBatch>
  return typeof record.state === 'string' && !!record.counts && typeof record.counts === 'object'
}

/**
 * 查一个批次到哪一步了（恢复向导轮询它）。
 *
 * **批不存在时后端 404**：那句话的意思是"这个 id 不认识"，与"还没开始"是两件事——
 * 所以这里不吞，由调用方如实说出来。
 */
export async function getImportBatch(batchId: string): Promise<ImportBatch> {
  const payload = await requestLocal<ImportBatch>(`/local/import/${encodeURIComponent(batchId)}`)
  if (!looksLikeBatch(payload)) {
    throw new Error('导入进度不认识（没有 state / counts：是不是本机后端比界面老？）')
  }
  return payload
}

/**
 * 回滚一个批次（同步返回结论）。
 *
 * 两条规则按台账办：**我们新建的**没再动过就删、**替换过的**用导入前的快照恢复，
 * 而**在本机改过的一律保留**并如实报数——`counts` 里逐条说着哪几条没动、为什么。
 */
export async function rollbackImportBatch(batchId: string): Promise<ImportBatch> {
  const payload = await requestLocal<ImportBatch>(
    `/local/import/${encodeURIComponent(batchId)}/rollback`,
    { method: 'POST' },
  )
  if (!looksLikeBatch(payload)) {
    throw new Error('回滚结论不认识（没有 state / counts：是不是本机后端比界面老？）')
  }
  return payload
}

/* ------------------------------------------------ 这一档有没有本机后端（显隐的唯一判据） */

/**
 * 浏览器里那一份（**没有壳**）从 2026-10-05 起只剩知识库管理台。
 *
 * NAS 上跑的网页端与桌面端共用这一套前端，而浏览器里没有 Tauri 壳 → 会话面那几页
 * 读的是 NAS 的 PG（`sidecar.ts::resolveLocalBase` 在"无壳"那一支直接回 `API_BASE`）。
 * 产品判定：**那一个网页端退役**，服务器档从那一轮起也不再挂会话面那几族端点
 * （`backend/app/api/v1/router.py`）。于是"这一份有没有本机后端"不只是一个数据选址问题，
 * 它直接决定**哪几页存在**：有本机后端 = 完整产品；没有 = 知识库 + 备份 + 健康。
 *
 * ## 判据用已有的信号：本机后端答不答 `/local/status`
 *
 * 与 `lib/sessionActions.ts::localOnlyDeployment` **同一个判据、同一个端点**
 * （那条是登录守卫用的，见它的说明）：`/local/status` 只在 `local_router` 上，
 * 服务器档里**根本不存在**这条路径。所以这里不新发明模式探测——
 * 端口、环境变量、`navigator.onLine` 都不该用来判这件事。
 *
 * 与那两个"本机档才显隐"的既有函数（`provider.ts::providerGateApplies` /
 * `backup.ts::backupGateApplies`）同形：先看显式关掉的逃生门，再看"壳在不在"，
 * 最后看"那个本机端点答没答"。
 */

/** `/local/status` 那一趟的结论。 */
export type LocalAnswer =
  /** 答上来了，而且是本机档。 */
  | 'local'
  /** **答了"这一档没有它"**：404（服务器档），或答上来却说自己不是本机档。 */
  | 'absent'
  /** 还没问出结论：没探过 / 探不通 / 形状不认识。**不按缺席处理**（见下）。 */
  | null

let answer: LocalAnswer = null
/** 有结论了吗（`answer` 为 `null` 时它是 `false`）——界面据此区分"还不知道"与"知道没有"。 */
let settled = false

const listeners = new Set<() => void>()
/** 探这一趟只做一次（并发调用共享同一次）。 */
let inflight: Promise<void> | null = null

/** 状态变了就广播（与 `api/provider.ts` 那套同一个写法）。 */
function publish(): void {
  for (const listener of listeners) listener()
}

/**
 * 探一次（**单飞 + 只此一次**）。
 *
 * **壳里与显式关掉时一次都不探**：那两种情形下这个判据是同步的（`localBackendView`
 * 里那两条前置分支），打一趟注定不改结论的请求只是白费一次往返——而这里是
 * "本机后端在不在"的唯一一条探测，别让它出现在启动路径上。
 *
 * 三档的处置在下面两个回调里：**404 是明确答案**（"这一档没有这条端点"），
 * 于是它落 `absent`；而网络不通 / 超时 / 形状不认识落"不知道"——**不落 `absent`**。
 * 理由：`absent` 会让界面把会话面那几页**摘掉**，而"问不出来"与"确认没有"是两件事。
 * 装机形态的显隐不该被一次网络抖动改掉（与 `provider.ts` 里"一次读失败不改状态"同一条）。
 */
export function probeLocalBackend(): Promise<void> {
  if (inDesktopShell() || !localDataEnabled()) return Promise.resolve()
  inflight ??= getLocalStatus().then(
    (status) => {
      answer = status.deployment === 'local' ? 'local' : 'absent'
      settled = true
      publish()
    },
    (error: unknown) => {
      settled = true
      // 404 = 这一档**没有**这条端点（服务器档：浏览器 / NAS 网页端）；
      // 其余（连不上 / 超时 / 形状不认识）**保留"不知道"**，见上面那一句。
      if ((error as { status?: number } | undefined)?.status === 404) answer = 'absent'
      publish()
    },
  )
  return inflight
}

/** 给界面读的那一份（`useLocalBackend` 的返回值）。 */
export interface LocalBackendView {
  /** 这一份界面**有没有**本机后端（会话面那几页在不在只看它）。 */
  present: boolean
  /** 问出结论了吗（`false` = 还在探，界面按"不拆"处理，见 `localBackendView`）。 */
  settled: boolean
}

/**
 * 同步结论（**任何地方都能读**：路由表、侧栏、账号菜单）。
 *
 * 三条分支：
 *
 * 1. 本机数据面被显式关掉（`VITE_LOCAL_DATA=0`，排障用的逃生门）→ **没有**：
 *    那一档界面读的就是服务器上的数据，与"本机后端"无关
 *    （与 `providerGateApplies` / `backupGateApplies` 的头两条逐字同形）；
 * 2. **壳里恒真有**（`inDesktopShell()`）：本机后端由壳拉起，那是"本机档"成立的地方。
 *    这一条必须是**同步**的——它是主产品形态，不能在首屏先按"没有"渲染一帧再纠正；
 * 3. 其余（浏览器里的这一份）看那一趟探测的结论：`local` → 有；`absent` → 没有；
 *    **还没结论 → 有**（宁可按完整产品渲染：一次没答上来的探测不该把界面拆掉，
 *    而"确认没有"那一支是 404，很快就有结论）。
 */
export function localBackendView(): LocalBackendView {
  if (!localDataEnabled()) return { present: false, settled: true }
  if (inDesktopShell()) return { present: true, settled: true }
  return { present: answer !== 'absent', settled }
}

/** 这一个（同步版）：只问"会话面那几页在不在"。 */
export function localBackendPresent(): boolean {
  return localBackendView().present
}

/**
 * 订阅它（侧栏、路由表读的是同一份结论）。
 *
 * 首次被问到才探（不在启动时挡路）——与 `useKnowledgeProviderStatus` 同一条：
 * 已经有结论时那一次 `probeLocalBackend()` 是空操作。
 */
export function useLocalBackend(): LocalBackendView {
  const [, bump] = useState(0)

  useEffect(() => {
    const listener = (): void => bump((value) => value + 1)
    listeners.add(listener)
    // **挂上订阅之后再对一次表**：结论可能在"这次渲染"与"挂上订阅"之间就到了
    // （与 `api/provider.ts` 里那一句同一条理由，那里写着实测过的缺口）
    bump((value) => value + 1)
    // 壳里不必探（判据同步）——那一跳在 `probeLocalBackend` 里面
    void probeLocalBackend()
    return () => {
      listeners.delete(listener)
    }
  }, [])

  return localBackendView()
}

/* ------------------------------------------------------------------ 用例用的窄接口 */

/**
 * 用例用：把结论**直接摆好**（不经过网络），或复位成"还没探过"。
 *
 * 与 `setProviderStatusForTest` 同一条纪律：模块级单份状态必须由调用方复位，
 * 否则前一条用例的结论会串到下一条（真机上表现为"服务器档的形状测试时有时无"）。
 */
export function setLocalBackendForTest(next: LocalAnswer): void {
  answer = next
  settled = true
  inflight = null
  publish()
}

/** 用例用：复位成"还没探过"（`undefined` = 让下一次 `probeLocalBackend` 真的去探）。 */
export function resetLocalBackendForTest(): void {
  answer = null
  settled = false
  inflight = null
  listeners.clear()
}
