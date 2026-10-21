/**
 * 接入（`/tools`）——外面怎么接进来，以及这个服务对外提供了什么。
 *
 * 2026-10-10 由「工具」页改成「接入」页（**路由不变**，改的是侧栏标签、页标题与正文）：
 * 用户反馈"配置接入那个地方做的不够好，用户看了还是不知道怎么接"。原来这一页把 MCP
 * 地址与工具清单一前一后摊着，"密钥"只在末尾一句话里出现过——而把客户端接上其实是
 * 三件事：**拿地址 → 拿密钥 → 贴进客户端**。所以现在分两档：
 *
 * | 档 | 内容 |
 * | --- | --- |
 * | 接入 | 服务地址 / 鉴权（管理员可就地建一把接入用密钥）/ 客户端配置片段 / 三步验证 / 排障 |
 * | 工具 | 工具面指纹 + 随附技能 + 对外工具清单（原来那一块，位置挪到这里） |
 *
 * 「接入」是默认档：进来第一眼要看到的是"照这个接"，不是"我有哪些工具"。
 *
 * 两档的数据仍来自**同一条只读端点**（`GET /tools`）：
 *
 * 1. **接入**用它的 `mcp_endpoint`（后端没配就回 `null`，那一格就让用户自己填一次
 *    ——服务端不猜不拼，界面也不该因此变成死胡同）；
 * 2. **工具**用它给全的工具面——一行一个工具，标签（技术标识跟在旁边）、摘要、权限、
 *    参数。顺序就是后端登记顺序（与 MCP 客户端看到的顺序一致），所以这一档**不排序**；
 * 3. **工具面指纹**也跟着工具面走（工具名 / 说明 / 入参 / 出参变了它就变），故它留在
 *    「工具」档，整串可复制。
 *
 * 权限那一列照 `requires_write` 分两档（**需要读写 / 只读**）：它回答的是"这把工具
 * 在只读档下能不能调"，与工具自身的用途无关——所以不并进摘要里，单独成列。
 *
 * 参数只列**名字 + 必填标记**，参数的说明挂了 `title`（没写说明就不挂）：
 * 参数多的工具（检索）列成一行已经很长，把说明摊开会把这一列撑成一段正文。
 *
 * 版式上这一页只有两层：页头（标题 + 一句现状 + 刷新）是主角；下面每一档里，
 * 信息用 `.surface-card`、清单用 `.panel`，两者都不再套第二层卡片。
 */
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { Link } from 'react-router'

import { getToolCatalogue } from '@/api/tools'
import { copyText } from '@/lib/clipboard'
import { Badge } from '@/ui/badge'
import { Button } from '@/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/ui/tabs'

import { TOOLS_QUERY_KEY } from '../queryKeys'
import { EmptyState, ErrorLine, PageShell, SkeletonBlock } from '../shared/composites'
import { notifyError, notifySuccess } from '../shared/toast'
import { AccessPanel } from './AccessPanel'

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

/** 信息卡里的一行：左栏 120px 标签 + 右栏内容（与「接入」档的信息卡同一口径）。 */
const INFO_ROW = 'grid grid-cols-1 gap-x-5 gap-y-1.5 sm:grid-cols-[120px_minmax(0,1fr)]'

/** 信息卡里的标签格：13.5px、二级墨色——它是"这一行是什么"，不是值。 */
const INFO_LABEL = 'text-[length:var(--text-meta-size)] leading-6 text-text-secondary'

export function ToolsPage() {
  const catalogue = useQuery({ queryKey: TOOLS_QUERY_KEY, queryFn: getToolCatalogue })

  const data = catalogue.data
  const tools = data?.items ?? []

  /**
   * 页头那行现状：**只写算得出来的部分**——地址是不是服务端给的、有几个对外工具。
   * 密钥数量要另打一次 `/api-keys`，这一页不为了凑一句副标题多发一个请求。
   */
  const subtitle = catalogue.isLoading
    ? '正在读取工具面…'
    : catalogue.isError
      ? '工具面读取失败，刷新重试'
      : data
        ? `${data.mcp_endpoint ? 'MCP 地址已配置' : '服务端未给 MCP 地址'} · ${tools.length} 个对外工具`
        : '正在读取工具面…'

  /** 指纹要能整串复制走：接入方拿它跟自己对的那一版核。 */
  async function copyFingerprint(): Promise<void> {
    if (!data) return
    if (await copyText(data.surface_fingerprint)) {
      notifySuccess('工具面指纹已复制')
      return
    }
    notifyError('复制失败，请手动选中复制')
  }

  return (
    <PageShell
      title="接入"
      subtitle={subtitle}
      actions={
        <Button variant="secondary" onClick={() => void catalogue.refetch()}>
          <RefreshCw size={14} />
          刷新
        </Button>
      }
    >
      {/* 读不到时页头已经说了状态，这里只留一行具体原因；读到了才展开两档。 */}
      <div className="flex flex-col gap-6">
        {catalogue.isError && <ErrorLine>{messageOf(catalogue.error, '读不到工具清单')}</ErrorLine>}
        {catalogue.isLoading && <SkeletonBlock variant="list" rows={4} />}

        {data && (
          <Tabs defaultValue="access">
            <TabsList aria-label="接入页分档">
              <TabsTrigger value="access">接入</TabsTrigger>
              <TabsTrigger value="tools">工具</TabsTrigger>
            </TabsList>

            {/* 每一档自己竖排：档内的几段之间隔一档 32px，段内再紧凑一点 */}
            <TabsContent value="access" className="mt-4 flex flex-col gap-8">
              <AccessPanel endpoint={data.mcp_endpoint} />
            </TabsContent>

            <TabsContent value="tools" className="mt-4 flex flex-col gap-8">
              <section className="flex flex-col gap-3">
                <h2 className="section-label">工具面</h2>
                <div className="surface-card px-5">
                  <div className={`${INFO_ROW} py-4`}>
                    <span className={INFO_LABEL}>工具面指纹</span>
                    <div className="flex min-w-0 items-start justify-between gap-3">
                      <code className="min-w-0 break-all font-mono text-[length:var(--text-meta-size)] leading-6 text-text-primary">
                        {data.surface_fingerprint}
                      </code>
                      <Button
                        size="sm"
                        variant="secondary"
                        className="shrink-0"
                        aria-label="复制工具面指纹"
                        onClick={() => void copyFingerprint()}
                      >
                        复制
                      </Button>
                    </div>
                  </div>

                  <div className={`${INFO_ROW} border-t border-[var(--border-hairline)] py-4`}>
                    <span className={INFO_LABEL}>随附技能</span>
                    <div className="flex min-w-0 flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-[length:var(--text-meta-size)] leading-6 text-text-primary">
                          {data.skill.name}
                        </span>
                        {data.skill.transports.map((transport) => (
                          <Badge key={transport} variant="secondary">
                            {transport.toUpperCase()}
                          </Badge>
                        ))}
                      </div>
                      <span className="text-[length:var(--text-meta-size)] leading-6 text-text-secondary">
                        {data.skill.summary}
                      </span>
                      <code className="break-all font-mono text-[length:var(--text-micro-size)] text-text-tertiary">
                        {data.skill.path}
                      </code>
                    </div>
                  </div>
                </div>
                <p className="text-hint">工具面一变此值就变。</p>
              </section>

              <section className="flex flex-col gap-3">
                <div className="flex items-baseline justify-between gap-3">
                  <h2 className="section-label">对外工具</h2>
                  <span className="tabular text-[length:var(--text-micro-size)] text-text-tertiary">
                    {tools.length} 个
                  </span>
                </div>
                {tools.length === 0 ? (
                  <EmptyState title="还没有工具" hint="这个服务还没有对外工具。" />
                ) : (
                  <div className="panel">
                    {/* 表头与行**引用同一套列宽类**（`.m-tool-col-*`）：两处各写一份就会错位 */}
                    <div className="panel-head m-list-head min-w-[720px]" aria-hidden="true">
                      <span className="m-tool-col-name">工具</span>
                      <span className="m-tool-col-perm">权限</span>
                      <span className="m-tool-col-params">参数</span>
                    </div>
                    <ul className="m-list stagger">
                      {tools.map((tool) => (
                        <li key={tool.name} className="panel-row m-tool-row min-w-[720px]">
                          <span className="m-tool-col-name">
                            <span className="m-row-label">
                              {tool.label}
                              <span className="m-tool-id">{tool.name}</span>
                            </span>
                            <span className="m-row-value">{tool.summary}</span>
                          </span>
                          <span className="m-tool-col-perm">
                            <Badge variant={tool.requires_write ? 'warning' : 'secondary'}>
                              {tool.requires_write ? '需要读写' : '只读'}
                            </Badge>
                          </span>
                          <span className="m-tool-col-params">
                            {tool.params.length === 0 ? (
                              <span className="m-row-value">无参数</span>
                            ) : (
                              tool.params.map((param) => (
                                <span
                                  key={param.name}
                                  className="m-tool-param"
                                  title={param.summary || undefined}
                                >
                                  {param.name}
                                  {param.required && <Badge variant="warning">必填</Badge>}
                                </span>
                              ))
                            )}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <p className="text-[length:var(--text-meta-size)] leading-[var(--line-ui)] text-text-secondary">
                  一把密钥可以只开其中一部分工具，在 <Link to="/api-keys">API 密钥</Link> 里勾选。
                </p>
              </section>
            </TabsContent>
          </Tabs>
        )}
      </div>
    </PageShell>
  )
}
