/**
 * 「这一份界面上，当前这个人是不是管理员」——从 kylab 前端的同一份照搬。
 *
 * ## 本仓库（kybase）里它暂时没有调用方，判据退化成一条
 *
 * 上游那份要写两档，是因为本机档**没有账号体系**（`/auth/*` 一族不挂在本机后端的路由表上，
 * `currentUser` 恒为 null），而"本机主人"在后端那条记录里就是管理员——按"有没有登录"判，
 * 会把那一档的管理员入口一并藏掉。**本仓库恒为服务器档**（服务端就是知识库本身，
 * 账号体系也在它上面），所以这里 `local.present` **恒为假**，函数等价于
 * `user?.role === 'admin'`。
 *
 * 留着它的理由：① 它是 `lib/` 里的一份纯判据，删掉省不了什么；
 * ② 唯一需要"这一页的管理员入口在不在"的地方（`TasksPage` 的「运行负载」）
 * **刻意不用它**——那一条端点只挂在服务器档，判据写在那个调用点上（见那里的注释）。
 * 将来要接账号/角色那一套界面时，这条判据还在原处。
 *
 * ## 为什么不能只按账号判（用户在上游报过两次的 bug）
 *
 * 本机档**没有账号体系**：`/auth/*` 一族根本不挂在边车的 `local_router` 上
 * （`backend/app/api/v1/router.py`），`App.tsx::AuthGate` 的 R13 分支在"远端问不出来
 * + 没有凭据 + `localOnlyDeployment()`"时直接放行，于是这一档
 * `useSessionStore(s => s.currentUser)` **恒为 null**。
 * 而后端起给这一档那个唯一调用主体的名字是「本机主人」，它的记录里
 * `is_admin=True` / `role=UserRole.ADMIN`（`backend/app/services/api_key.py::LOCAL_CALLER`）
 * ——**本机主人就是这台机器的管理员**。
 *
 * 所以按"有没有登录"判，会把这一档的管理员入口**一并藏掉**（用户 2026-10-05 报了两次：
 * 侧栏左下角「设置」不见了——`AccountMenu` 那一条已按同一口径修过；
 * 输入框那一排的「权限」胶囊不见了）。
 *
 * ## 判据（每条都在这里说清）
 *
 * 1. **有账号** → 按账号的角色（`role === 'admin'`）；
 * 2. **没有账号** → 按**这一份界面有没有本机后端**（`api/local.ts::useLocalBackend`，
 *    那个判据只有它一处）：有（本机档）就是管理员；没有（服务器档且没有账号）就不是。
 *    会话还没恢复完时 `currentUser` 也是 null，而那时 `local.present` 在服务器档里为假，
 *    所以落"不是"——与原先那条"还没恢复完按不是处理"同一条；
 * 3. **本机后端在不在**（`api/local.ts::localBackendView`）在壳里是**同步**的（恒真），所以
 *    那一边不会先按"不是"渲染一帧再纠正；浏览器那一份要探一次 `/local/status` 才有结论，
 *    结论没回来之前判据按"有"走（`localBackendView` 的既有口径：一次没答上来的探测不该把
 *    界面拆掉）——那一档的入口等结论到了才收起来。**本仓库里这一条恒落"不是"**
 *    （`/local/status` 属于本机档，知识库服务端没有它）。
 *
 * ## 边界：只管"那条链在本机服务"的入口
 *
 * `/users`（用户管理，上游住在 `SettingsModal` 的「用户」那一节）、`/tasks/load`（知识库那边的
 * 运行负载，`TasksPage` 的负载面板）这类**只挂在服务器档**的端点不适用本判据
 * （`local_router` 那张白名单里没有 `users.router` / `tasks.router`）：本机档按本机档判成
 * 管理员，等于摆一个点进去读不到、白打一趟必被拒请求的入口。那两处照旧按"有没有账号体系"判
 * （`currentUser?.role === 'admin'`），各自的调用点写着理由。
 *
 * 与上游 `AccountMenu::settingsAvailable` 的关系：那一处是 `local.present && 本判据`
 * （服务器档整个不摆「设置」）——**本仓库里那一项整个没有搬过来**（见 `AccountMenu` 文件头），
 * 所以这条关系在这里只作为出处记着。
 */
import { useLocalBackend } from '@/api/local'
import { useSessionStore } from '@/lib/session'

export function useIsAdmin(): boolean {
  const user = useSessionStore((state) => state.currentUser)
  const local = useLocalBackend()
  return user ? user.role === 'admin' : local.present
}
