/**
 * 工具（`/tools`）——这个服务对外提供了什么。
 *
 * 一条只读端点（`GET /tools`）把三样东西一起给全，这一页照着渲染，**一个工具名都不写死**：
 *
 * 1. **接入方式**：MCP 地址与那份随服务一起发出去的技能（名字 / 路径 / 两条通路）。
 *    MCP 地址没配时后端回 `null`——这里照实说没配，不拼一个"看起来能用"的地址；
 * 2. **对外工具**：一行一个工具，标签（技术标识跟在旁边）、摘要、权限、参数。
 *    顺序就是后端登记顺序（与 MCP 客户端看到的顺序一致），所以这一页**不排序**；
 * 3. **按工具收窄**：一把 API 密钥可以只开其中一部分工具，那一页负责勾选，这里给一条去路。
 *
 * 权限那一列照 `requires_write` 分两档（**需要读写 / 只读**）：它回答的是"这把工具
 * 在只读档下能不能调"，与工具自身的用途无关——所以不并进摘要里，单独成列。
 *
 * 参数只列**名字 + 必填标记**，参数的说明挂了 `title`（没写说明就不挂）：
 * 参数多的工具（检索）列成一行已经很长，把说明摊开会把这一列撑成一段正文。
 */
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { Link } from 'react-router'

import { getToolCatalogue } from '@/api/tools'
import { Button } from '@/ui/button'

import { TOOLS_QUERY_KEY } from '../queryKeys'
import { EmptyState, ErrorLine, PageShell, SkeletonBlock, StatusTag } from '../shared/composites'

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback
}

export function ToolsPage() {
  const catalogue = useQuery({ queryKey: TOOLS_QUERY_KEY, queryFn: getToolCatalogue })

  const data = catalogue.data
  const tools = data?.items ?? []

  return (
    <PageShell
      title="工具"
      actions={
        <Button variant="secondary" onClick={() => void catalogue.refetch()}>
          <RefreshCw size={14} />
          刷新
        </Button>
      }
    >
      {/* 正文那一层与 `TasksPage` **同一结构**（页面自己写一层 `page-shell-body`），
          间距与任务中心那一页一致。 */}
      <div className="page-shell-body">
        {catalogue.isError && <ErrorLine>{messageOf(catalogue.error, '读不到工具清单')}</ErrorLine>}
        {catalogue.isLoading && <SkeletonBlock variant="list" rows={4} />}

        {data && (
          <>
            <section className="m-block">
              <h2 className="m-block-title">接入方式</h2>
              <div className="panel">
                <ul className="m-list">
                  <li className="m-list-item">
                    <div className="m-list-row">
                      <div className="m-row-lines">
                        <span className="m-row-label">MCP 地址</span>
                        {data.mcp_endpoint ? (
                          <span className="m-row-value">{data.mcp_endpoint}</span>
                        ) : (
                          <span className="m-muted">这一档没有配 MCP 地址。</span>
                        )}
                      </div>
                    </div>
                  </li>
                  <li className="m-list-item">
                    <div className="m-list-row">
                      <div className="m-row-lines">
                        <span className="m-row-label">
                          {data.skill.name}
                          {data.skill.transports.map((transport) => (
                            <StatusTag key={transport} label={transport.toUpperCase()} />
                          ))}
                        </span>
                        <span className="m-row-value">{data.skill.summary}</span>
                        <span className="m-row-value">{data.skill.path}</span>
                      </div>
                    </div>
                  </li>
                </ul>
              </div>
            </section>

            <section className="m-block m-section-gap">
              <h2 className="m-block-title">对外工具</h2>
              {tools.length === 0 ? (
                <EmptyState title="还没有工具" hint="这个服务还没有对外工具。" />
              ) : (
                <div className="panel">
                  {/* 表头与行**引用同一套列宽类**（`.m-tool-col-*`）：两处各写一份就会错位 */}
                  <div className="panel-head m-list-head min-w-[560px]" aria-hidden="true">
                    <span className="m-tool-col-name">工具</span>
                    <span className="m-tool-col-perm">权限</span>
                    <span className="m-tool-col-params">参数</span>
                  </div>
                  <ul className="m-list">
                    {tools.map((tool) => (
                      <li key={tool.name} className="m-list-item">
                        <div className="m-tool-row panel-row min-w-[560px]">
                          <span className="m-tool-col-name">
                            <span className="m-row-label">
                              {tool.label}
                              <span className="m-tool-id">{tool.name}</span>
                            </span>
                            <span className="m-row-value">{tool.summary}</span>
                          </span>
                          <span className="m-tool-col-perm">
                            <StatusTag
                              tone={tool.requires_write ? 'warning' : 'neutral'}
                              label={tool.requires_write ? '需要读写' : '只读'}
                            />
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
                                  {param.required && (
                                    <span className="m-tool-param-required">必填</span>
                                  )}
                                </span>
                              ))
                            )}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="m-muted">
                一把密钥可以只开其中一部分工具，在 <Link to="/api-keys">API 密钥</Link> 里勾选。
              </p>
            </section>
          </>
        )}
      </div>
    </PageShell>
  )
}
