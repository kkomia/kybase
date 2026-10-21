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
 *
 * 版式：一段片段 = 一小行标题与复制按钮（`justify-between`）+ 一块深一档的等宽面。
 * 片段之间只用间距分层，不把每段包成卡片；「通用表单」那几格是一张信息卡里的行。
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

/** 「通用表单」里的一行：左栏 120px 字段名 + 右栏值（与接入档的信息卡同一口径）。 */
const FIELD_ROW = 'grid grid-cols-1 gap-x-5 gap-y-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]'

/** 字段名那一格：13.5px、二级墨色。 */
const FIELD_LABEL = 'text-[length:var(--text-meta-size)] leading-6 text-text-secondary'

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
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[length:var(--text-meta-size)] font-medium text-text-primary">
          {snippet.label}
        </span>
        <Button
          size="sm"
          variant="secondary"
          className="shrink-0"
          aria-label={`复制${snippet.label}`}
          onClick={() => void copy(snippet.text)}
        >
          复制
        </Button>
      </div>
      {snippet.where && (
        <span className="break-all font-mono text-[length:var(--text-micro-size)] text-text-tertiary">
          {snippet.where}
        </span>
      )}
      {/*
        深一档的面 + 等宽 12.5px / 行高 1.7：它是"照抄"的主角，所以比正文更沉一点。
        `m-code-block` 保留（整段横向滚动、不折行；测试按 `pre.m-code-block` 取整段原文）。
      */}
      <pre className="m-code-block rounded-[var(--radius-row)] bg-[var(--bg-subtle)] text-[12.5px] leading-[1.7]">
        {snippet.text}
      </pre>
    </div>
  )
}

export function ClientSnippets({ endpoint, token }: { endpoint: string; token: string }) {
  const [client, setClient] = useState<ClientKey>('codex')

  return (
    <Tabs value={client} onValueChange={(next) => setClient(next as ClientKey)} className="gap-4">
      <TabsList aria-label="客户端" className="max-w-full max-sm:h-auto max-sm:flex-wrap">
        {CLIENTS.map((item) => (
          <TabsTrigger key={item.key} value={item.key}>
            {item.label}
          </TabsTrigger>
        ))}
      </TabsList>

      <TabsContent value="codex" className="flex flex-col gap-5">
        {codexSnippets(endpoint, token).map((snippet) => (
          <SnippetBlock key={snippet.label} snippet={snippet} />
        ))}
      </TabsContent>

      <TabsContent value="json" className="flex flex-col gap-5">
        {jsonSnippets(endpoint, token).map((snippet) => (
          <SnippetBlock key={snippet.label} snippet={snippet} />
        ))}
      </TabsContent>

      <TabsContent value="generic" className="flex flex-col gap-3">
        <span className="text-hint">{GENERIC_WHERE}</span>
        <div className="surface-card px-5">
          {genericFields(endpoint, token).map((field, index) => (
            <div
              key={field.label}
              className={
                index === 0 ? FIELD_ROW : `${FIELD_ROW} border-t border-[var(--border-hairline)]`
              }
            >
              <span className={FIELD_LABEL}>{field.label}</span>
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                <span className="min-w-0 break-all font-mono text-[length:var(--text-meta-size)] leading-6 text-text-primary">
                  {field.value}
                </span>
                <Button
                  size="sm"
                  variant="secondary"
                  className="shrink-0"
                  aria-label={`复制${field.label}`}
                  onClick={() => void copy(field.value)}
                >
                  复制
                </Button>
              </div>
            </div>
          ))}
        </div>
      </TabsContent>

      <TabsContent value="env" className="flex flex-col gap-5">
        {envSnippets(endpoint, token).map((snippet) => (
          <SnippetBlock key={snippet.label} snippet={snippet} />
        ))}
      </TabsContent>
    </Tabs>
  )
}
