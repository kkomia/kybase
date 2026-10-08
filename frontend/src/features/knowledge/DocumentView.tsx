/**
 * `/documents/:id` 只是**跳板**——详情现在是文档列表页上滑出的抽屉
 * （旧 `views/DocumentView.vue` 逐条对齐）。
 *
 * 为什么保留这条路径：引用、旧书签、聊天里的"查看原文"都指向它，改掉就会断链。
 * 所以这里只做一件事：查出这份文档属于哪个知识库，然后换到 `/kb/{kbId}?doc={id}`
 * ——列表页见到 `doc` 参数就把抽屉滑出来。
 *
 * 代价是一次重定向加一次取文档的请求；换来的是"详情只有一种呈现方式"，
 * 不必同时维护"独立页"与"抽屉"两套。
 */
import { useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router'

import { getDocument } from '@/api/documents'
import { messageOf } from '@/features/knowledge/store'

export interface DocumentViewProps {
  /** 不传则从路由参数取（`/documents/:documentId`）。 */
  documentId?: string
}

export function DocumentView({ documentId: documentIdProp }: DocumentViewProps) {
  const params = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [error, setError] = useState('')

  const documentId = documentIdProp ?? String(params.documentId ?? '')

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const document = await getDocument(documentId)
        // `page` 是引用带进来的页码，换到列表页时要一起带过去（PDF 查看器靠它跳页）
        const query = new URLSearchParams({ doc: documentId })
        const page = searchParams.get('page')
        if (page) query.set('page', page)
        if (!cancelled) {
          await navigate(`/kb/${document.knowledge_base_id}?${query.toString()}`, { replace: true })
        }
      } catch (cause) {
        if (!cancelled) setError(messageOf(cause, '打不开这份文档'))
      }
    })()
    return () => {
      cancelled = true
    }
    // 只在挂载时跳一次（`searchParams` 的变化不该触发第二次跳转）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId])

  return (
    <div className="page-shell">
      <p className="text-meta">{error || '正在打开文档…'}</p>
    </div>
  )
}
