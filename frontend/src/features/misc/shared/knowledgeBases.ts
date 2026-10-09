/**
 * 知识库清单（misc 域自用的只读缓存）。
 *
 * 定时任务、工作区两个页面都需要"有哪些库"来喂选择器。旧前端读的是
 * `useKnowledgeBaseStore`（一个全局 Pinia store）；新前端里知识库域归别人所有，
 * 而 `src/api/**` 是只读可用的——所以这里用 react-query 起一份**只读缓存**
 * （同一个 queryKey 全站共用：谁先拉谁填，别处直接命中）。
 *
 * 失败不抛给调用方：这两个页面里"库列表"只是选择器的候选，
 * 它拿不到不该让整页报错，退化成"没有可选项"即可。
 *
 * `enabled`（默认 `true`，既有调用方行为不变）：挂在**常驻外壳**里的弹窗
 * （`ApiKeysDialog` 在 `AccountMenu` 上，每页都挂着）要传"弹窗开着才算"——
 * 不然一个多数人永远不打开的弹窗会在每次进应用时白拉一趟清单。
 */
import { useQuery } from '@tanstack/react-query'

import { listKnowledgeBases, type KnowledgeBase } from '@/api/knowledgeBases'

export const KNOWLEDGE_BASES_QUERY_KEY = ['knowledge-bases', 'list'] as const

export interface KnowledgeBaseList {
  items: KnowledgeBase[]
  isLoading: boolean
  refetch: () => void
}

export function useKnowledgeBases(enabled = true): KnowledgeBaseList {
  const query = useQuery({
    queryKey: KNOWLEDGE_BASES_QUERY_KEY,
    queryFn: async () => (await listKnowledgeBases()).items,
    enabled,
  })
  return {
    items: query.data ?? [],
    isLoading: query.isLoading,
    refetch: () => void query.refetch(),
  }
}
