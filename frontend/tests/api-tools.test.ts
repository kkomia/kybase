/**
 * 对外工具面（`api/tools.ts`）——**真发请求、真解析响应**的那条链路。
 *
 * 其它用例（`misc-tools` / `misc-apikeys`）都把这层 mock 掉了，所以这条路径只有这里钉得住：
 * 地址拼错（少一段 `/tools`、多了个斜杠）在组件用例里永远是绿的，到了真机上才发现是 404。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getToolCatalogue } from '@/api/tools'

/** 一条最小但**契约完整**的目录（字段与后端 `ToolCatalogOut` 一一对应）。 */
const CATALOG = {
  items: [
    {
      name: 'search',
      label: '检索',
      summary: '按库检索原文段落',
      requires_write: false,
      params: [{ name: 'query', required: true, summary: '' }],
    },
  ],
  mcp_endpoint: 'http://nas:8000/mcp',
  skill: {
    name: 'kylab-knowledge-base',
    summary: '在你的知识库里检索原文段落',
    path: 'skills/kylab-knowledge-base',
    transports: ['mcp', 'rest'],
  },
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('getToolCatalogue', () => {
  it('走 GET /tools，并把目录原样交回（含没配 MCP 地址的那一档）', async () => {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url)
        return jsonResponse(CATALOG)
      }),
    )

    const catalog = await getToolCatalogue()

    expect(calls).toEqual(['/api/v1/tools'])
    expect(catalog.items).toHaveLength(1)
    expect(catalog.items[0].name).toBe('search')
    expect(catalog.items[0].params[0].required).toBe(true)
    expect(catalog.skill.transports).toEqual(['mcp', 'rest'])
  })

  it('mcp_endpoint 为 null 时照实交回 null（不替它编一个地址）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ ...CATALOG, mcp_endpoint: null })),
    )

    const catalog = await getToolCatalogue()

    expect(catalog.mcp_endpoint).toBeNull()
  })
})
