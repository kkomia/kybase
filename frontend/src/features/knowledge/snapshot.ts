/**
 * 「先画一帧」的形状与文案（M4 阶段 5）——**界面文案的唯一出口**。
 *
 * ## 它管三件事
 *
 * 1. **页顶那一行小字只有两句话**（方案 §4.5）：在线「上次更新于 X」、断连
 *    「现在连不上，这是上次看到的内容（X）」。两页（列表 / 详情）都从这里取——
 *    各拼一句的话，两个页面的说法迟早会漂开；
 * 2. **快照行只读**（§4.3-④ / 决策点 D-B）：权限位一律置假，写/管理入口
 *    **晚一步出现**，实时那一条读一落地就亮。这就是「不许装成实时」的具体形态；
 * 3. **快照行没有进度**（§1.1）：冻结的进度条是最糟的假象，进度只从实时读来。
 *
 * ## 时序规则五条（§4.3）怎么落在代码里
 *
 * | 规则 | 落点 |
 * | --- | --- |
 * | ① 只在「这个视图还没有实时结果」时画 | `createSnapshotGate().claim()`：画过/已有实时结果 → `false`，调用方**连请求都不发** |
 * | ② 实时结果永远赢 | `markLive()` + `stillWanted()`：实时结果一落地，在飞的那一帧作废 |
 * | ③ 画快照期间不显示骨架屏 | 调用方按「内存里有内容没有」判（`documents.length > 0` / `items.length > 0`） |
 * | ④ 快照面只读 | `READONLY_MARK` / `withoutPermissions()` / `withoutProgress()` |
 * | ⑤ 轮询规则不变 | 不在这里：轮询那两条（`usePolling` / 提供者 30s）一个字都没动 |
 *
 * ## 「缓存」二字不许上界面（U2 是硬门禁）
 *
 * 用户要知道的是「这是上次看到的内容、什么时候看到的」，不是这份东西在本机怎么放。
 * 所以文案一律从本文件出去，且**不许**出现实现语汇（`scripts/check_layering.py` 的 U2）。
 */
import type { DocumentListFilter } from '@/api/documents'
import type { DocListView, KbCacheRevalidate, KbCacheSnapshot } from '@/api/kbCache'
import { formatRelativeTime } from '@/lib/format'

/* ------------------------------------------------------------------ 这一帧从哪来 */

/**
 * 屏幕上这一份内容的**来历**：是实时读回来的，还是「上次看到的那一份」。
 *
 * 两个字段就是界面那一行小字要的全部输入（§5：两个时间戳不许混——
 * 「上次确认连接」由提供者状态那条线自己说，这里是「上次看到的内容」）。
 */
export interface SnapshotFrame {
  /** 这一帧是快照画的（实时结果一落地就置假）。 */
  fromSnapshot: boolean
  /** 那份内容是什么时候看到的（`fetched_at`；实时帧为空串）。 */
  snapshotAt: string
}

/** 实时读画的帧（`fromSnapshot` 的那些字段在它这里全是「没有」）。 */
export const LIVE_FRAME: SnapshotFrame = { fromSnapshot: false, snapshotAt: '' }

/** 一份快照的 `fetched_at` → 这一帧的来历。 */
export function snapshotFrame(fetchedAt: string | null | undefined): SnapshotFrame {
  return { fromSnapshot: true, snapshotAt: fetchedAt ?? '' }
}

/**
 * 页顶那一行小字（**两句话，唯一一处**）。
 *
 * `offline = true` 用在「实时读刚刚失败」那一档：内容还在屏幕上（快照那帧没被清），
 * 所以要如实说清「现在连不上」，而不是让用户以为这是刚取回来的。
 * 不是快照帧时**返回空串**（实时内容不需要这一行）。
 */
export function snapshotNote(frame: SnapshotFrame, offline: boolean): string {
  if (!frame.fromSnapshot) return ''
  const at = formatRelativeTime(frame.snapshotAt)
  return offline ? `现在连不上，这是上次看到的内容（${at}）` : `上次更新于 ${at}`
}

/**
 * 非 ready（连不上 / 还没配）**但本机留着上次看到的内容**时，路由守卫在页顶上说的那一句
 * （M4 阶段 6，决策点 D-A）。
 *
 * 与上面那句是兄弟而不是同一句：那一句长在**页面里**（内容的时间戳），这一句长在
 * **页面外面**（守卫），所以要多说两件事——**为什么现在只能看**（原因原话）与
 * **这一份是什么时候看到的**。两句都从本文件出，界面上就不会出现两种说法。
 *
 * **"只能看"说的是"本机留的那一份"，不是"这一页"**：页面自己那条实时读压根不经过本机后端
 * （它用会话身份直连 NAS），所以"关掉提供者但 NAS 好好的"这一档里，页面照旧能读能写
 * ——那时页顶这句话对"那一份"仍然成立，页面上看到的却已经是实时内容（写/管理入口照旧由
 * 实时那一份的权限位说了算）。把话说成"这一页只能看"会在那一档里说谎。
 *
 * `reason` 为空时兜底那句与 toast（`providerBlockedMessage`）同一口径：**不许静默**。
 */
export function offlineSnapshotNote(reason: string, fetchedAt: string): string {
  const at = formatRelativeTime(fetchedAt)
  return (
    `现在连不上知识库提供者：${reason || '本机后端没给出原因'}；` +
    `下面是本机留的那一份（${at}），那一份只能看，不能改。`
  )
}

/* ------------------------------------------------------------------ 快照帧只读 */

/**
 * 快照行的**只读标记**（§4.3-④ / 决策点 D-B）：权限位一律置假。
 *
 * 后端进快照前就把 `can_write` / `can_manage` 剥掉了（那是**按调用者身份**算的，
 * 而快照是边车钥匙看到的那个身份）。落到界面上这两个位置就是「未确认」——
 * 写与管理入口因此晚一步出现：宁可晚一步，也不给一个点下去 403 的入口。
 */
export const READONLY_MARK = { can_write: false, can_manage: false } as const

/** 一行快照 → 界面那一行（权限位按只读标记补齐）。 */
export function withoutPermissions<T extends object>(row: T): T & typeof READONLY_MARK {
  return { ...row, ...READONLY_MARK }
}

/** 一行快照 → 界面那一行（**进度一律抹掉**：冻结的进度条是最糟的假象）。 */
export function withoutProgress<T extends object>(row: T): T & { progress: null } {
  return { ...row, progress: null }
}

/** 快照里的行（列表型资源）。**没有副本 → 空表**（调用方据此照旧骨架屏）。 */
export function snapshotRows<T>(snapshot: KbCacheSnapshot | null): T[] {
  if (!snapshot?.available || !Array.isArray(snapshot.items)) return []
  return snapshot.items as T[]
}

/** 快照那份内容的**原样**（`items` 之外的量，比如 `total`，从这里读）。 */
export function snapshotPayload<T>(snapshot: KbCacheSnapshot | null): T | null {
  if (!snapshot?.available || !snapshot.payload) return null
  return snapshot.payload as T
}

/* ------------------------------------------------------------------ 「先画一帧」的闸门 */

/**
 * 一个视图的「先画」闸门（时序规则①②的机械形态）。
 *
 * 按**视图键**记账：视图 = 「这一屏现在说的是哪一份内容」（文档列表的键就是
 * `docListViewKey()`，目录树/计数/库列表各有一个自己的）。这样「先画」与「实时」
 * 谁先到都说得清，不靠一个全局布尔去猜。
 */
export interface SnapshotGate {
  /** 认领这一次先画：已画过 / 已有实时结果 / 正在飞 → `false`（就此打住，连请求都不发）。 */
  claim(view: string): boolean
  /**
   * 这一次**了结**了：从此这个视图不再画第二次。
   *
   * 两种情形都算：①真的画上了；②那份内容**本来就是空的**（本机留着的是一份空清单）——
   * 空的那一份摆不上任何东西，所以不写内存也不置帧（不然会一边摆骨架屏一边说
   * "这是上次看到的内容"），但"这个视图要不要先画一帧"这个问题已经有答案了。
   */
  settle(view: string): void
  /** 这一次没画上（取不到）：把认领还回去，下一次还有机会。 */
  release(view: string): void
  /** 实时结果落地：这个视图**从此不许再被快照盖**（§4.3-②「实时结果永远赢」）。 */
  live(view: string): void
  /** 在飞的那一帧**还作数吗**——等待期间实时结果落地了就作废。 */
  stillWanted(view: string): boolean
}

export function createSnapshotGate(): SnapshotGate {
  const inFlight = new Set<string>()
  /** 这个视图的内存已经有内容了：画过、或实时到了（「内存已有 → 跳过」那条判据）。 */
  const settled = new Set<string>()
  /** 其中「实时」来的那些：快照画的帧不许盖它们。 */
  const live = new Set<string>()
  return {
    claim(view) {
      if (settled.has(view) || inFlight.has(view)) return false
      inFlight.add(view)
      return true
    },
    settle(view) {
      inFlight.delete(view)
      settled.add(view)
    },
    release(view) {
      inFlight.delete(view)
    },
    live(view) {
      inFlight.delete(view)
      settled.add(view)
      live.add(view)
    },
    stillWanted(view) {
      return !live.has(view)
    },
  }
}

/* ------------------------------------------------------------------ 视图与再确认的目标 */

/**
 * 实时读那一条的筛选对象 → 快照面的**规范视图**（`null` = 这一档没有副本）。
 *
 * `q` / `stage` / `source_kind` 任一给了就**换不出快照键**（决策点 D-D：搜索结果是
 * 「这一问的答案」，过期即误导）。这时**一个请求都不发**（调用方看 `null` 就照旧骨架屏）
 * ——本机后端那一侧对这三个参数也是如实回「这个筛选条件下的内容不留副本」，
 * 但那是一句给人看的说明，不值得为它每次敲键盘都打一趟。
 *
 * `folder` 与 `root` 的互斥**照 `api/documents.ts::listDocuments` 的写法**：
 * 接口那边是 `folder_id` 优先（`if / else if`），这里给同一个口径。
 */
export function docListViewOf(
  filter: DocumentListFilter,
  page: number,
  size: number,
): DocListView | null {
  if (filter.q?.trim() || filter.stage || filter.sourceKind) return null
  const view: DocListView = { page, size }
  if (filter.folderId) view.folder = filter.folderId
  else if (filter.root) view.root = true
  return view
}

/**
 * 焦点回到窗口时该确认的**那一键**（§4.4 的接点）。
 *
 * 当前视图是规范视图 → 确认文档列表那一份（这一页的主线内容，也是下次冷启动
 * 最想快点看到的那一份）；带了搜索/筛选 → 它没有键，退而确认**库详情**
 * （这一页自己的那个对象）。**一次焦点只确认一份**：「一个页面三个视图一起炸」
 * 正是 §4.4 防风暴那条点名要避免的。
 */
export function revalidateTargetOf(
  kbId: string,
  filter: DocumentListFilter,
  page: number,
  size: number,
): KbCacheRevalidate {
  const view = docListViewOf(filter, page, size)
  if (!view) return { resource: 'kb_detail', kb_id: kbId }
  const target: KbCacheRevalidate = { resource: 'doc_list', kb_id: kbId, page, size }
  if (view.folder) target.folder = view.folder
  else if (view.root) target.root = true
  return target
}
