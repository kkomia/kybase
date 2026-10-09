/**
 * 知识库内的检索面板（《前端设计规范》§6；旧 `components/search/KbSearchPanel.vue` 对齐）。
 *
 * 检索本来就是"在某个库里查东西"，单独占一个全局页面没有道理：用户得先去检索台、
 * 再从下拉里挑库，而那一刻他其实已经站在那个库里了。所以检索收进知识库详情页，
 * 用弹窗打开——检索是**动作**，不是常驻视图。
 *
 * 面板里刻意把**通道与分数摊开**（向量 / BM25、各自排名与原始分）：它同时承担
 * "这个库检索质量如何"的验证责任。服务端没配嵌入模型时向量通道会被跳过或退化成
 * 开发用哈希兜底（只有词面重叠、没有语义），此时必须明说"向量召回不可信"。
 *
 * **判定排在命中上面**（v0.55 起后端给了 `summary` 与 `fit`）：这个服务的产品口径是
 * "判定在前、原文在后"，控制台里也得是这个顺序——`fit="none"` 表示"这个库答不了
 * 这个问题"，它不是检索失败。
 *
 * **逐条那一格读的是余弦**（`similarity`），不是融合分：融合分只反映名次
 * （`1/(k+rank)` 量级），把它当"相似度"显示是把名次当成了像不像。只有词面通道捞上来的
 * 命中没有余弦，那一格就写「词面命中」——它们正是 `min_score` 管不到的那一批
 * （后端把条数记在 `decision.unscored_kept` 里，判定行照实报出来）。
 *
 * **一页不是全部**（v0.63 起可翻页）：`next_offset` 非空时判定行写「还有更深的候选」。
 * 面板自己不翻页——它只把"手里这份是不是全部"说清楚。
 *
 * **那一格数字发成两个**：`keep`（这一页几条）与 `top_k`（这次能翻到的池子深浅）都发
 * 用户填的那个数。只发 `top_k` 的话，一页仍按后端**建议**剪（常常比用户要的少），
 * 于是"填 8 却回 4"——用户要的是"最多 N 条"，那个上限得自己说出来。
 */
import { useEffect, useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'

import { search, type SearchFit, type SearchResponse } from '@/api/search'
import { EmptyState, SkeletonRows } from '@/features/knowledge/composites'
import { notify } from '@/features/knowledge/store'
import { failureText } from '@/features/preview'
import { formatAge, formatCount, formatLatency, formatScore } from '@/lib/format'
import { Button } from '@/ui/button'
import { Checkbox } from '@/ui/checkbox'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/ui/dialog'
import { Input } from '@/ui/input'
import { Label } from '@/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select'
import { Textarea } from '@/ui/textarea'

const MODE_OPTIONS = [
  { value: 'hybrid', label: '混合（向量 + BM25）' },
  { value: 'vector', label: '仅向量' },
  { value: 'fulltext', label: '仅 BM25' },
]

function modeLabel(value: string): string {
  if (value === 'hybrid') return '混合检索'
  if (value === 'vector') return '仅向量'
  if (value === 'fulltext') return '仅 BM25'
  return value
}

function channelLabel(channel: string): string {
  return channel === 'vector' ? '向量' : channel === 'fulltext' ? 'BM25' : channel
}

/**
 * `fit` 的中文读数（后端的四种判定，见 `services/retrieval/distribution.py`）。
 *
 * `none` = 整批压在基线之下、或窗口里一条都没有：**这份资料答不了这个问题**
 * ——它与"检索出错"是两件事，用户不该把它读成故障（判定条那一档警示底就为它）。
 */
function fitLabel(fit: SearchFit | undefined): string {
  if (fit === 'strong') return '很对得上'
  if (fit === 'weak') return '只对上一部分'
  if (fit === 'none') return '资料里没有'
  if (fit === 'unknown') return '契合判不了'
  return ''
}

interface Turn {
  query: string
  response: SearchResponse | null
  error: string
  /** 发起时间：会话历史里要能看出"这是刚才那次"还是"十分钟前那次"。 */
  at: number
}

interface KbSearchPanelProps {
  open: boolean
  kbId: string
  kbName: string
  onClose: () => void
  /** 点命中里的文件名：宿主负责跳到文档（默认走 `/documents/:id` 跳板）。 */
  onOpenDocument?: (documentId: string) => void
}

export function KbSearchPanel({ open, kbId, kbName, onClose, onOpenDocument }: KbSearchPanelProps) {
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<'hybrid' | 'vector' | 'fulltext'>('hybrid')
  /** 数字输入留成字符串交给原生 input，提交时再转——避免与输入框类型打架。 */
  const [topK, setTopK] = useState('8')
  const [candidateK, setCandidateK] = useState('40')
  const [rerank, setRerank] = useState(false)
  const [turns, setTurns] = useState<Turn[]>([])
  const [searching, setSearching] = useState(false)
  const [activeTurn, setActiveTurn] = useState(-1)
  /** 会话历史里的"多久之前"需要一个会走的时钟。 */
  const [now, setNow] = useState(() => Date.now())

  // 时钟只在面板打开时走（关掉就停表）
  useEffect(() => {
    if (!open) return undefined
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 10_000)
    return () => window.clearInterval(timer)
  }, [open])

  const latest = turns[activeTurn] ?? null
  /** 命中过多时正文收成三行摘要：一屏放不下两条结果就没法相互比较。 */
  const showFullText = (latest?.response?.hits.length ?? 0) <= 8
  const embeddingIsDevelopment = latest?.response?.embedding_is_development === true
  const embeddingMissing = latest?.response?.embedding_configured === false
  /** 这一批命中的判定：`distribution.fit` 说的是"资料对不对得上"，不是"检索成没成"。 */
  const fit = latest?.response?.distribution?.fit
  /** 收敛砍掉的条数（与"元数据过滤掉"不是同一件事：那一刀切在检索之前）。 */
  const dropped = latest?.response?.decision?.dropped ?? 0
  /**
   * 这一页里**只有词面通道**捞上来的条数：它们没有余弦（`similarity` 为 `null`），
   * `min_score` 那一刀对它们不成立——所以高阈值下回来一批这样的命中不是坏了，是那一刀
   * 管不到它们。数出来的判据与逐条那枚标是同一个（`similarity === null`）。
   */
  const wordOnly = latest?.response?.hits.filter((hit) => hit.similarity === null).length ?? 0
  /** 其中真绕过下限的条数：后端数的（`decision.unscored_kept`），不是这里推的。 */
  const unscoredKept = latest?.response?.decision?.unscored_kept ?? 0
  /** 下一页的起点；`null` = 这一页就是全部（翻到底了）。 */
  const nextOffset = latest?.response?.next_offset ?? null

  async function runSearch(raw?: string): Promise<void> {
    const text = (raw ?? query).trim()
    if (!text) {
      notify.warning('请输入检索内容')
      return
    }
    setSearching(true)
    const turn: Turn = { query: text, response: null, error: '', at: Date.now() }
    setTurns((current) => [...current, turn])
    setActiveTurn(turns.length)
    try {
      // 一格数字 → 两个参数：`keep` 是这一页几条、`top_k` 是这次能翻到的池子深浅。
      // 两个都发同一个数（`top_k` 因此永远不会小于 `keep`）——只发 `top_k` 的话一页
      // 仍按后端建议剪，用户要 8 条只会回来那一档建议（常常 4 条）。
      // 空着 / 填不出数按 8 走；负数与小数先夹到 ≥1 的整数，别让它变成一次 422。
      const wanted = Math.max(1, Math.trunc(Number(topK)) || 8)
      const response = await search({
        query: text,
        kb_ids: [kbId],
        mode,
        keep: wanted,
        top_k: wanted,
        candidate_k: Number(candidateK) || 40,
        rerank,
      })
      setTurns((current) => current.map((item) => (item === turn ? { ...item, response } : item)))
    } catch (cause) {
      const error = failureText(cause, '检索失败')
      setTurns((current) => current.map((item) => (item === turn ? { ...item, error } : item)))
      notify.error(error)
    } finally {
      setSearching(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      {/* 宽档：`sm:max-w-[980px]`（旧 `Modal size="wide"` 的 980px） */}
      <DialogContent className="flex max-h-[min(88vh,900px)] flex-col gap-0 p-0 sm:max-w-[980px]">
        <DialogHeader className="border-b border-[var(--border-hairline)] px-4 py-3 pr-10">
          <DialogTitle>在「{kbName}」中检索</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="kb-search-panel">
            <div className="kb-search-side">
              <label className="field">
                <span className="field-label">检索内容</span>
                <Textarea
                  id="kb-search-input"
                  rows={3}
                  value={query}
                  placeholder="输入问题或关键词，回车检索"
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault()
                      void runSearch()
                    }
                  }}
                />
              </label>

              <div className="kb-search-row" style={{ marginBottom: 'var(--space-4)' }}>
                <label className="field" style={{ flex: 1 }}>
                  <span className="field-label">模式</span>
                  <Select value={mode} onValueChange={(value) => setMode(value as typeof mode)}>
                    <SelectTrigger aria-label="检索模式">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MODE_OPTIONS.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </label>
                <label className="field kb-narrow">
                  <span className="field-label">返回上限</span>
                  <Input
                    type="number"
                    min={1}
                    value={topK}
                    aria-label="返回上限"
                    onChange={(event) => setTopK(event.target.value)}
                  />
                </label>
              </div>

              <details className="kb-advanced">
                <summary>高级选项</summary>
                <div className="kb-search-row">
                  <label className="field kb-narrow">
                    <span className="field-label">候选池</span>
                    <Input
                      type="number"
                      value={candidateK}
                      aria-label="候选池"
                      onChange={(event) => setCandidateK(event.target.value)}
                    />
                  </label>
                  {/* 14px / 400（同 `.kb-switch`：布局在 CSS，字号/字重在调用点） */}
                  <Label className="kb-switch text-[length:var(--text-meta-size)] font-normal">
                    <Checkbox
                      checked={rerank}
                      aria-label="启用 rerank（失败自动退回 RRF 顺序）"
                      onCheckedChange={(checked) => setRerank(checked === true)}
                    />
                    <span>启用 rerank（失败自动退回 RRF 顺序）</span>
                  </Label>
                </div>
              </details>

              <Button variant="default" disabled={searching} onClick={() => void runSearch()}>
                <Search aria-hidden="true" />
                {searching ? '检索中…' : '检索'}
              </Button>

              {turns.length > 0 ? (
                <div>
                  <p className="kb-history">本次会话</p>
                  <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
                    {turns.map((turn, index) => (
                      <li key={index}>
                        <button
                          type="button"
                          className={[
                            'kb-history-item',
                            index === activeTurn ? 'kb-history-item-on' : '',
                          ]
                            .filter(Boolean)
                            .join(' ')}
                          onClick={() => setActiveTurn(index)}
                        >
                          <span className="kb-history-query">{turn.query}</span>
                          <span className="text-micro tabular">
                            {turn.response ? `${formatCount(turn.response.hits.length)} 条` : '—'}
                          </span>
                          <span className="text-micro tabular">{formatAge(turn.at, now)}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>

            <div className="kb-search-hits">
              {searching ? <SkeletonRows variant="text" rows={6} /> : null}

              {!searching && !latest ? (
                <EmptyState title="还没有检索记录" hint="在左侧输入内容开始检索。" />
              ) : null}

              {!searching && latest?.error ? (
                /*
                  失败态：一句人话 + 一个重试按钮。重试**按那一轮的词原样再发一次**
                  （不是把现在的输入框内容发出去——用户可能已经改了框里的字，
                  而失败的这一条历史仍然指着原来那个词）。toast 照旧：它管"这次没成"，
                  结果区这行管"那一条记录为什么是空的"，两处各说各的。
                */
                <div className="kb-failure">
                  <p className="kb-error-line">检索失败：{latest.error}</p>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={searching}
                    onClick={() => void runSearch(latest.query)}
                  >
                    <RefreshCw aria-hidden="true" />
                    重试
                  </Button>
                </div>
              ) : null}

              {!searching && latest?.response ? (
                <>
                  {/* 判定在前（v0.55）：后端那一行 `summary` 照原样放在命中列表**上面**，
                      连同 `fit` 的标。`fit="none"` 时整条换一档底色——那是"这个库答不了
                      这个问题"的信号，不是"检索失败"（失败态在上面那一段里）。 */}
                  {latest.response.summary ? (
                    <p className={fit === 'none' ? 'kb-verdict kb-verdict-none' : 'kb-verdict'}>
                      {fit ? <span className="kb-verdict-mark">{fitLabel(fit)}</span> : null}
                      {latest.response.summary}
                    </p>
                  ) : null}

                  <div className="kb-hit-summary">
                    <span style={{ color: 'var(--text-primary)' }}>
                      {formatCount(latest.response.hits.length)} 条命中
                      <span className="sep">·</span>
                      {modeLabel(latest.response.mode)}
                    </span>
                    {latest.response.reranked ? <span>已 rerank</span> : null}
                    {dropped > 0 ? <span>收敛掉 {formatCount(dropped)} 条</span> : null}
                    {/* 词面命中没有余弦可比：高阈值下它们照样回来，这一格把原因说出来
                        （不然那一批 `similarity` 为空的行看起来像个坏掉的分数）。 */}
                    {wordOnly > 0 ? <span>词面命中 {formatCount(wordOnly)} 条</span> : null}
                    {unscoredKept > 0 ? <span>未过下限 {formatCount(unscoredKept)} 条</span> : null}
                    {/* 这一页只是一趟更深检索里的一段：还有就照实说（面板不翻页，只报事实）。 */}
                    {nextOffset !== null ? <span>还有更深的候选</span> : null}
                    {latest.response.filtered_out > 0 ? (
                      <span>元数据过滤掉 {latest.response.filtered_out} 条</span>
                    ) : null}
                  </div>

                  {embeddingMissing ? (
                    <p className="kb-dev-warning">
                      <strong>本次只做了关键词检索：</strong>
                      服务端未选定嵌入模型，向量通道已跳过。请到「设置 →
                      向量化」选定默认嵌入模型后重试。
                    </p>
                  ) : null}
                  {!embeddingMissing && embeddingIsDevelopment ? (
                    <p className="kb-dev-warning">
                      <strong>向量召回不代表真实效果：</strong>
                      当前走的是开发用兜底（只反映词面重叠）。BM25
                      的结果是可信的，向量分数仅供链路自测。
                    </p>
                  ) : null}

                  {latest.response.stats.length > 0 ? (
                    <ul className="kb-hit-channels">
                      {latest.response.stats.map((stat) => (
                        <li key={stat.channel}>
                          {channelLabel(stat.channel)} · 候选 {formatCount(stat.count)} ·{' '}
                          {formatLatency(stat.elapsed_ms)}
                        </li>
                      ))}
                    </ul>
                  ) : null}

                  {latest.response.hits.length === 0 ? (
                    <EmptyState
                      title="没有命中"
                      hint="换关键词、放宽模式到混合检索，或确认文档已经处理到「已索引」。"
                    />
                  ) : (
                    <ol className="kb-hit-list">
                      {latest.response.hits.map((hit) => (
                        <li key={hit.chunk_id} className="kb-hit">
                          <div className="kb-hit-summary" style={{ marginBottom: 0 }}>
                            <button
                              type="button"
                              className="kb-hit-title"
                              onClick={() => onOpenDocument?.(hit.document_id)}
                            >
                              {hit.document_name ?? hit.document_id}
                            </button>
                            {hit.heading_path ? (
                              <span className="kb-hit-meta">{hit.heading_path}</span>
                            ) : null}
                            {hit.page !== null ? (
                              <span className="kb-hit-meta">第 {hit.page} 页</span>
                            ) : null}
                            <span className="kb-hit-score" style={{ marginLeft: 'auto' }}>
                              {hit.similarity === null
                                ? '词面命中'
                                : `相似度 ${formatScore(hit.similarity)}`}
                            </span>
                          </div>
                          <p
                            className="kb-hit-text"
                            style={
                              showFullText
                                ? undefined
                                : {
                                    display: '-webkit-box',
                                    WebkitLineClamp: 3,
                                    WebkitBoxOrient: 'vertical',
                                    overflow: 'hidden',
                                  }
                            }
                          >
                            {hit.text}
                          </p>
                          <ul className="kb-hit-channels">
                            {hit.channels.map((channel) => (
                              <li key={channel}>
                                {channelLabel(channel)} · 第 {hit.ranks[channel] ?? '—'} 位 ·{' '}
                                {formatScore(hit.raw_scores[channel])}
                              </li>
                            ))}
                            {hit.image_ids.length > 0 ? (
                              <li>{formatCount(hit.image_ids.length)} 张图</li>
                            ) : null}
                          </ul>
                        </li>
                      ))}
                    </ol>
                  )}
                </>
              ) : null}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
