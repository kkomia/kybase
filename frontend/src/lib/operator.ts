/**
 * 当前使用者（React 版；对应旧前端 `composables/useOperator.ts`）。
 *
 * **是本地偏好，不是登录态**：局域网内几个人共用一台机器，浏览器不区分人，
 * "我是谁"由用户自己声明。它不参与鉴权——伪造一个名字不会获得任何权限，
 * 只会让归属记错（契约见后端 `services/users.py`）。
 *
 * **存 id 不存名字**：请求头只能是 ASCII，而名字可能是中文（实测抛 UnicodeEncodeError）。
 */
import { create } from 'zustand'

import { listUsers, type RosterUser } from '@/api/users'
import { useSessionStore } from '@/lib/session'

const STORAGE_KEY = 'kylab-operator-id'

function readStored(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

interface OperatorState {
  operatorId: string
  roster: RosterUser[]
}

export const useOperatorStore = create<OperatorState>(() => ({
  operatorId: typeof window === 'undefined' ? '' : readStored(),
  roster: [],
}))

export function setOperator(id: string): void {
  useOperatorStore.setState({ operatorId: id })
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // 存不上就只在本次会话生效
  }
}

/** 当前使用者对象（不在名册里时为 undefined）。 */
export function currentOperator(): RosterUser | undefined {
  const { operatorId, roster } = useOperatorStore.getState()
  return roster.find((item) => item.id === operatorId)
}

/**
 * 拉名册。失败不抛：名册是可选的，拿不到就退化成"不记归属"，
 * 不该因此让整个界面报错（比如鉴权开着而没填令牌时）。
 */
export async function loadRoster(): Promise<void> {
  try {
    const roster = (await listUsers()).items
    useOperatorStore.setState({ roster })
    const { operatorId } = useOperatorStore.getState()
    if (operatorId && !roster.some((item) => item.id === operatorId)) setOperator('')
  } catch {
    useOperatorStore.setState({ roster: [] })
  }
}

/** 供 `client.ts` 组装请求头用：**每个请求都带**。 */
export function operatorHeaders(): Record<string, string> {
  // 归属 = 当前账号（用户批注：不在系统里切换使用者）：谁传的与谁登录着永远一致。
  // 会话恢复完成前回退到本地声明。它只是归属标注，不参与鉴权。
  const id = useSessionStore.getState().currentUser?.id ?? useOperatorStore.getState().operatorId
  return id ? { 'X-Kylab-Operator': id } : {}
}
