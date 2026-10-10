/**
 * 「客户端配置片段」那一块：按客户端分档，每档给**可整段复制**的片段或字段。
 *
 * 四档照用户截图里那几种接法分（键固定，顺序固定）：
 *
 * | 档 | 给什么 |
 * | --- | --- |
 * | Codex | TOML：凭据走环境变量（推荐）与直接写进配置两种写法 + 写变量的两条命令 |
 * | Cursor | JSON `mcpServers`（`url` + `headers`） |
 * | 通用表单 | 别的客户端是**一格一格填**的，故给字段 + 值，不给片段 |
 * | 环境变量 | `Authorization` 头从环境变量取（客户端自己展开 `${VAR}`） |
 *
 * 「通用表单」这一档是给**表单式客户端**的（用户截图里那个「新建 MCP 服务器」对话框
 * 就是它）：它们没有可贴的配置文件。JSON 那一档只列 Cursor 这个**有把握**的形状，
 * 别家客户端各自的键名与必填项没有实机核过，不往上摆。
 *
 * 分档用 Radix 的 `Tabs`（`@/ui/tabs`）：没选中的那一档**不挂载**，一屏只摊开一份片段。
 *
 * 复制统一走 `lib/clipboard` 那条两级路（先异步剪贴板、失败退 `execCommand`），
 * 成败各推一条通知——按钮点了不吭声，用户只会再点一次。
 */
import { useState } from 'react'

import { copyText } from '@/lib/clipboard'
import { Button } from '@/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/ui/tabs'

import { notifyError, notifySuccess } from '../shared/toast'
import {
  codexSnippets,
  envSnippets,
  GENERIC_WHERE,
  genericFields,
  jsonSnippets,
  type Snippet,
} from './snippets'

const CLIENTS = [
  { key: 'codex', label: 'Codex' },
  { key: 'json', label: 'Cursor' },
  { key: 'generic', label: '通用表单' },
  { key: 'env', label: '环境变量' },
] as const

type ClientKey = (typeof CLIENTS)[number]['key']

async function copy(value: string): Promise<void> {
  if (await copyText(value)) {
    notifySuccess('已复制')
    return
  }
  notifyError('复制失败，请手动选中复制')
}

/** 一段片段：标题行（标签 + 复制）、位置、整段可复制的代码块。 */
function SnippetBlock({ snippet }: { snippet: Snippet }) {
  return (
    <div className="m-snippet">
      <div className="m-snippet-head">
        <span className="m-snippet-title">{snippet.label}</span>
        <Button
          size="sm"
          variant="outline"
          aria-label={`复制${snippet.label}`}
          onClick={() => void copy(snippet.text)}
        >
          复制
        </Button>
      </div>
      {snippet.where && <span className="m-snippet-where">{snippet.where}</span>}
      <pre className="m-code-block">{snippet.text}</pre>
    </div>
  )
}

export function ClientSnippets({ endpoint, token }: { endpoint: string; token: string }) {
  const [client, setClient] = useState<ClientKey>('codex')

  return (
    <Tabs value={client} onValueChange={(next) => setClient(next as ClientKey)}>
      <TabsList aria-label="客户端">
        {CLIENTS.map((item) => (
          <TabsTrigger key={item.key} value={item.key}>
            {item.label}
          </TabsTrigger>
        ))}
      </TabsList>

      <TabsContent value="codex" className="m-client-tab">
        {codexSnippets(endpoint, token).map((snippet) => (
          <SnippetBlock key={snippet.label} snippet={snippet} />
        ))}
      </TabsContent>

      <TabsContent value="json" className="m-client-tab">
        {jsonSnippets(endpoint, token).map((snippet) => (
          <SnippetBlock key={snippet.label} snippet={snippet} />
        ))}
      </TabsContent>

      <TabsContent value="generic" className="m-client-tab">
        <span className="m-snippet-where">{GENERIC_WHERE}</span>
        <div className="panel">
          <ul className="m-list">
            {genericFields(endpoint, token).map((field) => (
              <li key={field.label} className="m-list-item">
                <div className="m-list-row">
                  <div className="m-row-lines">
                    <span className="m-row-label">{field.label}</span>
                    <span className="m-row-value">{field.value}</span>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    aria-label={`复制${field.label}`}
                    onClick={() => void copy(field.value)}
                  >
                    复制
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </TabsContent>

      <TabsContent value="env" className="m-client-tab">
        {envSnippets(endpoint, token).map((snippet) => (
          <SnippetBlock key={snippet.label} snippet={snippet} />
        ))}
      </TabsContent>
    </Tabs>
  )
}
