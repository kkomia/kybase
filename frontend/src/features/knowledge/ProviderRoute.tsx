/**
 * 知识库页面的**路由守卫**（M3 阶段 6；M4 阶段 6 加了 D-A 那条分支）。
 *
 * ## 它挡什么
 *
 * 提供者不可用（没配 / 连不上 / 凭据错 / 协议版本不认识）时，四条知识库路由
 * （`/knowledge-bases`、`/kb/:kbId`、`/kb/:kbId/wiki`、`/documents/:documentId`）
 * **重定向到 `/chat` 并给一条 toast（含原因）**——不静默、不白屏、不留一个死页面
 * （方案 §3.4「断连后消失」那一段）。
 *
 * 侧栏那一组显隐与这里是**同一个判定源**（`api/provider.ts`），所以"导航里没了"
 * 与"直接敲地址进不去"永远一致：不会出现"菜单藏了、书签还能进"这种半截状态。
 *
 * ## 五条分支（顺序不能换）
 *
 * 1. **不是本机档** → 放行。浏览器 / NAS 网页端那一档知识库就是它自己
 *    （`gate = false`），守卫在这儿**一个字都不该管**——管了会把好端端的产品挡掉；
 * 2. `ready` → 放行；
 * 3. **本机档但还没结论** → 先等一下（一行"正在确认…"）。这是首屏那一次握手的窗口：
 *    直接跳走会把一条正常深链接冤枉成"不可用"，而"未知按缺席渲染"管的是**导航**
 *    （不闪一个点进去报错的入口），不是把深链接弹回去；
 * 4. 有结论且不是 `ready`，**但本机留着上次看到的内容** → **放行**（M4 阶段 6 /
 *    决策点 D-A）：页顶上一条说明（含原因与那句"上次看到的是什么时候的"），内容照旧
 *    只读——"离线快照"要看得见，就得让人进得去这扇门；
 * 5. 有结论且不是 `ready`，本机也没有那份内容 → 记一条 toast + 重定向。
 *
 * ## 第 4 条为什么是"先问本机再决定"（而不是同步判）
 *
 * "本机有没有那份内容"的答案在**本机后端**（`/local/kb-cache/knowledge-bases` 那一族的
 * `available` 字段）。所以这一档多了一次**本机回环**的读（个位数毫秒），在它回来之前
 * 摆"正在看本机留的那一份…"——不先重定向再纠正（那会闪一下对话页），也不猜（猜错就是
 * 一条点下去白屏的深链接）。服务器档那几档压根走不到这里（第 1 条先放行了）。
 *
 * 判据是**那份内容在不在**（`available`），不是"这一档有没有本机后端"：本机后端没起来时
 * 它同样是 `available:false`（这一族的所有失败都折成"没有"），于是照旧走第 5 条。
 *
 * ## 只读是谁在管（这里只说不做）
 *
 * 守卫**不改页面**：页面自己那条实时读失败时，屏幕上留下的就是快照画的那一帧，
 * 而快照帧的权限位在本机那一层就被剥掉了（D-B / `snapshot.ts::withoutPermissions`）——
 * 写与管理入口因此"晚一步出现"，这正是"不许装成实时"的具体形态。守卫要做的是**放行 +
 * 在页顶上把话说清**（原因 + 那份内容的时间戳）。
 *
 * toast 只发一次（`ref` 挡板）：状态每次广播都会重渲染这个组件，
 * 而"连不上"这件事说一遍就够，刷屏反而把原因淹掉。重定向用 `replace`：
 * 目的地址不该在历史里留下这条进不去的知识库路由。
 */
import { useEffect, useRef, useState } from 'react'
import { Navigate } from 'react-router'

import { getKbCacheKnowledgeBases } from '@/api/kbCache'
import { useKnowledgeProviderStatus } from '@/api/provider'

import { offlineSnapshotNote } from './snapshot'
import { notify } from './store'

/**
 * 那条 toast 的原话（抽成纯函数：文案里**必须有原因**，用例逐字钉它）。
 *
 * 两句分开是有意的：`unconfigured`（还没配）与 `unavailable`（配了但连不上）
 * 的下一个动作完全不同——前者要填地址，后者要查网络/钥匙。后端给的原因句子里
 * 已经带了下一步，这里只补两件事：**本机也没留上次看到的内容**（所以这次真的看不到了），
 * 以及你被送回哪儿了、本机后端还在不在。
 */
export function providerBlockedMessage(state: string, reason: string): string {
  const lead = state === 'unconfigured' ? '还没接上知识库提供者' : '知识库提供者连不上'
  return (
    `${lead}：${reason || '本机后端没给出原因'}；` +
    '本机没留上次看到的内容，已回到对话页（本机后端仍在运行）'
  )
}

/** 本机留的那一份的结论：`null` = 还没问出结论（问的是本机后端，个位数毫秒）。 */
interface KeptSnapshot {
  /** 本机有没有那份内容。 */
  available: boolean
  /** 那份内容是什么时候看到的（要说给用户听）。 */
  fetchedAt: string
}

export function ProviderRoute({ children }: { children: React.ReactNode }) {
  const provider = useKnowledgeProviderStatus()
  const blocked = provider.gate && provider.settled && !provider.ready
  const [kept, setKept] = useState<KeptSnapshot | null>(null)
  const notified = useRef(false)

  /**
   * 不可用时先问一句"本机有没有上次看到的那一份"（第 4/5 条分支的判据）。
   *
   * `alive` 是必须的：状态每次广播都会重渲染，而这一读是异步的——组件已经不在这一档了
   * 还往状态里写，就会把"这一档有内容"这句话留给下一次进页面。
   */
  useEffect(() => {
    if (!blocked) {
      setKept(null)
      return undefined
    }
    let alive = true
    void getKbCacheKnowledgeBases().then((snapshot) => {
      if (!alive) return
      setKept({ available: snapshot?.available === true, fetchedAt: snapshot?.fetched_at ?? '' })
    })
    return () => {
      alive = false
    }
  }, [blocked])

  useEffect(() => {
    // 只在**已经问出结论**且结论是"本机也没有"时说这一句：问的答案还没回来时
    // 就报"已回到对话页"是把一次本机回环说成了故障
    if (!blocked || kept === null || kept.available || notified.current) return
    notified.current = true
    notify.warning(providerBlockedMessage(provider.state, provider.reason || provider.error))
  }, [blocked, kept, provider.state, provider.reason, provider.error])

  // 服务器档（浏览器 / NAS 网页端）：这一档知识库就是它自己，守卫不管
  if (!provider.gate) return <>{children}</>
  if (provider.ready) return <>{children}</>

  if (!provider.settled) {
    return (
      <div className="page-shell">
        <p data-testid="provider-pending" className="text-text-tertiary">
          正在确认知识库连接…
        </p>
      </div>
    )
  }

  // 还没问出"本机有没有那份内容"：等一下（本机回环，不先重定向再纠正）
  if (kept === null) {
    return (
      <div className="page-shell">
        <p data-testid="provider-offline-checking" className="text-text-tertiary">
          正在看本机留的那一份…
        </p>
      </div>
    )
  }

  // D-A：本机留着上次看到的内容 → **放行**（只读由快照帧的权限位管，见文件头那段）
  if (kept.available) {
    return (
      <>
        {/* 页顶上那一条说明：它长在页面**外面**，所以自己带一份与 `.page-shell` 相同的
            左右内边距，让这行字与页面内容对齐（守卫不做页面布局，只把话说在页顶） */}
        <p
          data-testid="provider-offline-note"
          className="px-[var(--page-gutter)] pt-[var(--page-pad-top)] text-[length:var(--text-meta-size)] text-text-secondary"
        >
          {offlineSnapshotNote(provider.reason || provider.error, kept.fetchedAt)}
        </p>
        {children}
      </>
    )
  }

  return <Navigate to="/chat" replace />
}
