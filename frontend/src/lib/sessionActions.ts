/**
 * 登录动作（React 版；对应旧前端 `composables/useSession.ts`）。
 *
 * 状态在 `lib/session.ts`，这里只做"发请求 + 落状态"的编排。前三条边界照搬旧实现，
 * 第四条是本轮按"本机权威"补的（R13）：
 *
 * 1. `ensureAuthStatus` **失败不抛**——后端没起时抛出去会让路由守卫变成死循环；
 *    拿不到状态按"不拦"处理，让界面照常渲染，由各请求自己报网络错误；
 * 2. 登录 / 初始化的令牌**只此一次**，立刻落本地；丢了只能重新登录；
 * 3. `logout` 即使服务端调用失败也清本地——用户点了退出，就不该还留着令牌；
 * 4. **本机档不要求远端登录**（R13，2026-10-04）：远端不可达、而这一档只连本机后端时，
 *    守卫照旧放行——本机那份数据是本机权威，见 `localOnlyDeployment`。
 */
import {
  changePassword as apiChangePassword,
  clearAvatar as apiClearAvatar,
  getAuthBootstrapStatus,
  login as apiLogin,
  logout as apiLogout,
  me as apiMe,
  setup as apiSetup,
  uploadAvatar as apiUploadAvatar,
  type AuthBootstrapStatus,
  type LoginResult,
} from '@/api/auth'
import { getLocalStatus } from '@/api/local'
import { clearSessionToken, isUnauthorized, setSessionToken, useSessionStore } from '@/lib/session'

/** 在途的状态请求：并发调用共享同一次，避免启动时连打三四个 `/auth/status`。 */
let statusPromise: Promise<AuthBootstrapStatus | null> | null = null

export async function ensureAuthStatus(force = false): Promise<AuthBootstrapStatus | null> {
  const cached = useSessionStore.getState().authStatus
  if (!force && cached) return cached
  if (!force && statusPromise) return statusPromise
  statusPromise = getAuthBootstrapStatus()
    .then((status) => {
      useSessionStore.setState({ authStatus: status })
      return status
    })
    .catch(() => null)
    .finally(() => {
      statusPromise = null
    })
  return statusPromise
}

export async function login(username: string, password: string): Promise<LoginResult> {
  const result = await apiLogin(username, password)
  setSessionToken(result.token)
  useSessionStore.setState({ currentUser: result.user })
  await ensureAuthStatus(true)
  return result
}

export async function setup(
  username: string,
  password: string,
  name?: string,
): Promise<LoginResult> {
  const result = await apiSetup(username, password, name)
  setSessionToken(result.token)
  useSessionStore.setState({ currentUser: result.user })
  await ensureAuthStatus(true)
  return result
}

export async function logout(): Promise<void> {
  try {
    if (useSessionStore.getState().token) await apiLogout()
  } catch {
    // 会话可能已过期或被吊销：服务端报错不影响"本地退出"这件事
  }
  clearSessionToken()
  await ensureAuthStatus(true)
}

/**
 * 恢复身份的结果——**三态，不是一个布尔**（D07，2026-09-28 走查）。
 *
 * 原先这里是 `Promise<boolean>`：`catch` 一律 `clearSessionToken()` + `false`，
 * 于是**网络抖动 / 超时 / 后端 5xx** 与"你的登录真过期了"被当成同一件事：
 * 用户被强制登出，而且**本地令牌被删掉**（网好了还得重新输密码）。
 * 实测报告 D07 记的就是这一条，`client.ts` 其实**早就给错误标了 `status`** ✗ 没人看。
 *
 * 三态之后调用方才能各做各的：
 * - `ok`：身份拿到了；
 * - `expired`：401，凭据真失效 → 清令牌、去登录页；
 * - `unreachable`：没连上/服务端出错 → **令牌留着**，别把用户踢出去。
 */
export type SessionRestore = 'ok' | 'expired' | 'unreachable'

export async function restoreSession(): Promise<SessionRestore> {
  if (!useSessionStore.getState().token) return 'expired'
  try {
    useSessionStore.setState({ currentUser: await apiMe() })
    return 'ok'
  } catch (error) {
    // **只有 401 才算过期**（见 `lib/session.ts::isUnauthorized` 的说明）
    if (!isUnauthorized(error)) return 'unreachable'
    clearSessionToken()
    return 'expired'
  }
}

/**
 * 这一档**是不是只连本机后端**（本机档：本机那份数据就是权威，远端登录不是它的前提）。
 *
 * ## 它回答的问题（R13）
 *
 * M3 / M4 / M5 三次验收记录与《交接说明》都记着同一条：**远端（NAS）不可达时前端被
 * 登录守卫拦回登录页，本机那份进不了界面**。而这台机器上的会话 / 笔记 / 设置 / 记忆
 * 本来就是权威 —— 本机档里**账号体系整个不参与**：本机后端的 `current_caller` 直接
 * 短路成"本机主人"，`/auth/*` 那一族**根本没有挂在**本机档的路由表上。所以"远端连不上"
 * 不该等于"界面进不去"（与"本机权威"直接冲突）。
 *
 * ## 判据用**已有的信号**：本机后端答不答 `/local/status`
 *
 * `GET /local/status` 是**本机档专属**的一条（那一族只挂在本机档的路由表上，服务器档里
 * 根本不存在），所以"它答得上来"本身就等于"这一档有一个本机后端在跑"，而它回的
 * `deployment` 再明确说一次档位（`local` / `server`）。**不在这里发明模式探测**：
 * 端口、环境变量、`navigator.onLine` 都不该用来判这件事。
 *
 * 拿不到结论（本机后端没起来 / 端点不存在 / 形状不认识）时回 `false`：方向是**保守**的
 * ——那时照原行为去登录页，宁可多要一次登录，也不把服务器档错当成本机档放行。
 */
export async function localOnlyDeployment(): Promise<boolean> {
  try {
    return (await getLocalStatus()).deployment === 'local'
  } catch {
    return false
  }
}

export function changeOwnPassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ revoked_sessions: number }> {
  return apiChangePassword(currentPassword, newPassword)
}

export async function setAvatar(file: File): Promise<void> {
  const user = await apiUploadAvatar(file)
  useSessionStore.setState({ currentUser: user })
}

export async function removeAvatar(): Promise<void> {
  const user = await apiClearAvatar()
  useSessionStore.setState({ currentUser: user })
}
