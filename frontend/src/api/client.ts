/**
 * 后端接口封装（与 /api/v1 对齐，工程规范 §4.1）。
 * 组件不直接发请求，一律经本目录。
 */

import { operatorHeaders } from '@/lib/operator'
import { clearSessionToken, requestRelogin, sessionToken } from '@/lib/session'

import { LocalUnavailableError, resolveLocalBase } from './sidecar'

export const API_BASE = '/api/v1'

export interface ApiErrorBody {
  code: string
  message: string
}

/**
 * 401 的处理策略：
 * - `redirect`（默认）：业务请求凭据失效 → 清会话令牌并请求重新登录。
 *   **但收到 401 不等于"登录过期"**（2026-10-02 修的真 bug）：先核一次会话
 *   （`checkSession()`），只有"凭据真的失效"才登出，见 `unwrap` 那一段；
 * - `throw`：认证端点自身的 401（密码错、初始化已关闭、`/auth/me` 探测）。
 *   这些不该触发"重新登录"——用户本来就在登录页，触发只会造成跳转循环；
 *   **也不核会话**（要核的就是它自己，核了只会多打一趟）。
 */
export interface RequestOptions {
  authFailure?: 'redirect' | 'throw'
}

/**
 * 带上登录凭据。
 *
 * **每个请求都现取，而不是在模块加载时读一次**：用户刚登录拿到会话，
 * 下一次请求就该生效，不能要求刷新页面。没有令牌时不加这个头——
 * 此时请求注定 401，由 `unwrap` 统一送回登录页。
 *
 * 导出给 `api/chat.ts` 用：对话走 SSE，响应体是持续打开的字节流，
 * 没法套 `request()`（它假定"响应是 JSON"），只能自己 fetch。
 * 那种"自己发请求"的地方必须记得带上它——漏过一次，
 * 表现是对话页永远回「缺少凭据」，而其它页面全都正常。
 */
export function authHeaders(): Record<string, string> {
  const token = sessionToken()
  // 操作者归属（G6）随**每个**请求带：它要出现在所有写操作上（上传、建库、删块……），
  // 逐个接口加字段既啰嗦又容易漏。值必须是 id——HTTP 头只能是 ASCII，
  // 而使用者名字可能是中文（实测会抛 UnicodeEncodeError）
  const headers: Record<string, string> = { ...operatorHeaders() }
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

/**
 * 凭据失效的统一处置：清掉本地令牌并请求重新登录。
 *
 * 返回给用户看的文案——**不要**把后端原文（"请在请求头带上 Authorization: Bearer …"）
 * 甩给用户，那是写给调用方看的。
 */
export function handleUnauthorized(): string {
  clearSessionToken()
  requestRelogin()
  return '登录已过期，请重新登录'
}

/* ------------------------------------------------------------------ 401 到底是哪一件事 */

/**
 * 会话探活那一条。**唯一一处**：它是"凭据还有效吗"的权威问法
 * （与 `sessionActions.restoreSession()` 问的是同一条，那边走 `api/auth.ts::me()`）。
 *
 * 为什么打在**服务器**而不是"失败的那条请求打的基址"上：凭据只有一种——服务器签发的
 * 登录会话（`lib/session.ts` 的文件头写着这件事），它的权威就在服务器。而本机档（边车）
 * 按后端自己的设计**根本不看 `Authorization`**（`api/auth.py::current_caller`：本机档
 * 短路成"本机主人"，没有账号体系），所以"边车回了个 401"永远不是"你的会话失效了"
 * ——它只可能是那条接口自己的问题（例如这台机器没配下载签名密钥）。
 */
const SESSION_PROBE_PATH = '/auth/me'

/**
 * 探活的结论。
 *
 * `unknown` 是**必须存在**的一档：网络不通 / 超时 / 5xx 时问不出结论，而这一档的处置
 * 与"有效"一致——**不登出**（D07，2026-09-28 走查："网络抖动不该把人强制登出，
 * 而且本地令牌被删了才是最难补救的后果"，与 `app/App.tsx` 里 `restored === 'expired'`
 * 那条判据同一句话）。
 */
export type SessionVerdict = 'valid' | 'invalid' | 'unknown'

/** 正在飞的那一次探活（**单飞**：一次 401 风暴里十几个请求只核一次会话）。 */
let sessionProbe: Promise<SessionVerdict> | null = null

/**
 * 核一次会话：**这一条 401 是"凭据失效"还是"那个接口自己的问题"**。
 *
 * 并发进来的调用共享同一次探活（`sessionProbe` 那个单飞），所以"N 个请求同时 401"
 * 只会多打**一趟** `/auth/me`。结论落地之后就把单飞清掉：下一次 401 是**新的一次判断**
 * （会话可能真的在这几秒里过期了），该重核就重核。
 *
 * 手上根本没有令牌时**直接回 `invalid`、连问都不问**：那种情况下这个 401 的意思就是
 * "你没登录"（与原来那条路一字不差），再打一趟 `/auth/me` 只会白费一次往返。
 */
export function checkSession(): Promise<SessionVerdict> {
  sessionProbe ??= probeSession().finally(() => {
    sessionProbe = null
  })
  return sessionProbe
}

/** 真去问那一趟（`checkSession` 的实现；`fetch` 直接打，**不经过 `unwrap`**：免得绕回 401 处理里）。 */
async function probeSession(): Promise<SessionVerdict> {
  if (!sessionToken()) return 'invalid'
  try {
    const response = await fetch(`${API_BASE}${SESSION_PROBE_PATH}`, { headers: authHeaders() })
    if (response.ok) return 'valid'
    // 401 = 这把令牌服务器不认了（过期 / 改密 / 被踢）→ 这才是"登录已过期"
    if (response.status === 401) return 'invalid'
    // 403 之类的相反是"凭据有效、但没有权限"；5xx 是对方自己出错。
    // 两种都**不是**"你的登录过期了"，归到 unknown（不登出）。
    return 'unknown'
  } catch {
    // 连不上（`fetch` 直接抛 TypeError / 超时）：问不出结论 → 不登出
    return 'unknown'
  }
}

/**
 * 用例用：把"正在飞的那一次探活"清掉。
 *
 * 单飞本身会在落地时自己清（见 `checkSession`），所以只在"替身让那次探活永远不回答"
 * 这类用例里才需要它——留着一条挂死的单飞会串到下一个用例里去。
 */
export function resetSessionProbeForTest(): void {
  sessionProbe = null
}

/** 把响应翻成结果或抛出带后端文案的错误（错误信封见后端 core/exceptions.py）。 */
async function unwrap<T>(response: Response, options: RequestOptions): Promise<T> {
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const body = (await response.json()) as ApiErrorBody
      if (body?.message) detail = body.message
    } catch {
      // 非 JSON 错误体：保留默认文案
    }
    // **收到 401 不等于"登录过期"**（2026-10-02 修的真 bug）：服务端自己没配下载签名密钥
    // 时也回 401（`{"code":"unauthorized","message":"尚未配置下载签名密钥…"}`），而这里原先
    // 把**任何** 401 都当"凭据失效" → 用户点一下产物「预览」就被弹到登录页，而他根本没掉线。
    //
    // 现在先核一次会话（单飞，见 `checkSession`），**只有"凭据真的失效"才登出**：
    // - `invalid`（`/auth/me` 也 401）→ 照旧清令牌 + 落登录页（这条路一个字没改）；
    // - `valid`（会话还在）→ **这一条 401 是那个接口自己的问题**：令牌留着、不跳登录，
    //   把**后端那句话**原样抛给调用方（预览里就地显示"预览失败（尚未配置下载签名密钥…）"）；
    // - `unknown`（网络不通 / 5xx）→ 同样不登出，抛出原错误。
    //
    // 401 照旧在错误对象上标一个记号（`status`）：既有的判据（`lib/session.ts::isUnauthorized`、
    // 登录页那条探活）都认它。用 Error 的自定义属性而不是新异常类，是为了让所有既有 catch 继续工作。
    if (response.status === 401 && options.authFailure !== 'throw') {
      if ((await checkSession()) === 'invalid') {
        // v0.11 起只有一种凭据（登录会话）：凭据真失效时就只有一条恢复路径——重新登录。
        detail = handleUnauthorized()
      }
    }
    const error = new Error(detail) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** 带统一错误处理的请求（JSON 为默认，但**不覆盖 multipart**）。 */
export async function request<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  return requestUrl<T>(`${API_BASE}${path}`, init, options)
}

/**
 * 与 `request` 同一条链，但吃**绝对 URL**（`request` 那个 API_BASE 前缀在这里由调用方给）。
 *
 * 为什么要有它：对话轮次与审批决定在边车模式下要打到**边车那台**（`http://127.0.0.1:8765`）
 * ——那是另一个源，`request('/path')` 够不着。**只加这一条缝**：错误信封、401 处理、
 * multipart 的 Content-Type 判定全都与 `request` 共用，两处不再各写一套。
 */
export async function requestUrl<T>(
  url: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  // **`FormData` 的 Content-Type 必须由浏览器自己写**：它要带 `boundary`，
  // 而手写一个 `application/json` 会让后端解析不出任何字段——实测的表现是
  // `422 {"message": "file: Field required"}`，看着像"请求里没带文件"，
  // 其实是头不对（浏览器**不会**覆盖作者显式设置的那个头）。
  // 所有上传（文档、会话文件、技能、头像）都走这条路，所以判断放在这一处。
  const multipart = typeof FormData !== 'undefined' && init?.body instanceof FormData
  return unwrap<T>(
    await fetch(url, {
      ...init,
      headers: {
        ...(multipart ? {} : { 'Content-Type': 'application/json' }),
        ...authHeaders(),
        ...(init?.headers ?? {}),
      },
    }),
    options,
  )
}

/**
 * **本机权威面**的请求（M2 阶段 4）：会话 / 笔记 / 设置 / 模型注册 / 工作区 / 定时任务 /
 * MCP / 记忆 这几块数据的**主人在本机**（库在 `%APPDATA%\com.kylab.desktop\kylab.db`），
 * 所以它们直连本机边车，不再绕道 NAS。
 *
 * 与 `request()` 的分工只有一条：`request()` 恒打服务器（`API_BASE`），
 * 本函数打哪台由 `sidecar.ts::resolveLocalBase` 判（前缀表在那边，判定只有那一处）。
 *
 * **不做"全局把 `request()` 的基址换掉"** ✗：那会让几十个服务器调用点一起变，
 * 而其中一半（账号 / 知识库 / 技能 / 文档……）的数据本来就在服务器上 ✓。
 */
export async function requestLocal<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  const base = await resolveLocalBase(path)
  try {
    return await requestUrl<T>(`${base}${path}`, init, options)
  } catch (error) {
    asLocalFailure(error)
  }
}

/**
 * 连接层的失败也要如实报 ✓ ——边车在那一次请求之前倒了的话，`fetch` 抛的是 `TypeError`
 * （不是 HTTP 状态码），照原样抛出去用户看到的是浏览器黑话（"Failed to fetch"），
 * 而这正是"本机后端未启动"那条纪律要盖住的情形 ✓。
 *
 * 后端**回了状态码**的错照旧原样抛 ✗（含 401）：那是这条请求自己的问题，
 * 说成"后端没起来"会把排障方向带跑偏。
 */
function asLocalFailure(error: unknown): never {
  if (error instanceof TypeError) throw new LocalUnavailableError(error.message)
  throw error
}

/**
 * 上传文件。
 *
 * 单独一个函数而不是复用 ``request``：上传必须让浏览器自己带
 * ``multipart/form-data; boundary=...``，手写 Content-Type 会把 boundary 弄丢，
 * 后端直接解析失败。
 *
 * `base` 由调用方给（`upload` = 服务器、`uploadLocal` = 本机权威面）：
 * **一处实现两条出路**，免得本机那份再抄一遍 multipart 的规矩。
 */
async function uploadTo<T>(base: string, path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  return unwrap<T>(
    await fetch(`${base}${path}`, {
      method: 'POST',
      body: form,
      headers: authHeaders(),
    }),
    {},
  )
}

/** 上传到**服务器**（文档 / 技能 / 头像这些本来就归服务器的）。 */
export async function upload<T>(path: string, file: File): Promise<T> {
  return uploadTo<T>(API_BASE, path, file)
}

/**
 * 上传到**本机权威面**（笔记配图：笔记本身在本机库里，图也得落在本机的对象存储上）。
 *
 * 与 `requestLocal` 同一套选址（含两条回退纪律），只是不能复用 `requestUrl`
 * ——multipart 的 Content-Type 必须让浏览器自己写（见 `uploadTo`）。
 */
export async function uploadLocal<T>(path: string, file: File): Promise<T> {
  const base = await resolveLocalBase(path)
  try {
    return await uploadTo<T>(base, path, file)
  } catch (error) {
    asLocalFailure(error)
  }
}
