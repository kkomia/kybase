# kybase MCP 接入 Codex v0.1

> 2026-10-09 · 读者：要把 kybase 知识库接进 Codex（CLI / IDE 插件 / ChatGPT 桌面端）的人。
> 服务端事实已实机核对（NAS 在线）；Codex 配置语法按
> [Codex 官方 MCP 文档](https://developers.openai.com/codex/mcp)。
> 通用版接入说明（REST / Skill / 窄 API）在《外部接入指南 v0.1》，本页只管 MCP + Codex 这一条路。

## 服务端事实（已上线）

| 项 | 值 |
|---|---|
| MCP 端点（Streamable HTTP） | `http://192.168.31.18:8765/mcp` |
| 鉴权 | **每个请求都要** `Authorization: Bearer <API Key>`，无匿名放行 |
| 凭据 | 管理台「设置 → API 密钥 → 新建」，`kylab_sk_…` 前缀；测试用**只读档**就够 |
| 工具面 | 8 个纯知识库工具（见下表），没有 Agent 侧工具 |
| 网络 | **只在局域网/可信网络开放**，别暴露公网 |

工具面（`tools/list` 实读回来就是这 8 个）：

| 工具 | 干什么 |
|---|---|
| `list_knowledge_bases` | 列库（检索前先调它拿库 id） |
| `search` | 混合检索，**返回带出处的原文片段**（不是生成的答案） |
| `list_documents` / `get_document_status` | 文档列表 / 处理进度 |
| `create_knowledge_base` / `upload_document` / `add_data_source` | 建库 / 上传 / 登记数据源（**写操作，需要读写档 Key**） |
| `delete_document` | 删文档（原文进回收站 7 天，**向量立即清除**） |

## Codex 配置

编辑 `~/.codex/config.toml`（CLI 与 IDE 插件、桌面端共用这份配置）：

**推荐写法**（凭据放环境变量，不进配置文件）：

```toml
[mcp_servers.kybase]
url = "http://192.168.31.18:8765/mcp"
bearer_token_env_var = "KYBASE_MCP_KEY"
```

然后在 shell 里设 `KYBASE_MCP_KEY=kylab_sk_<你的 Key>`（Windows 用「系统环境变量」或
`setx KYBASE_MCP_KEY ...`，设完重开终端）。

**图省事写法**（凭据直接写进配置文件——注意这个文件别提交到任何仓库）：

```toml
[mcp_servers.kybase]
url = "http://192.168.31.18:8765/mcp"
http_headers = { "Authorization" = "Bearer kylab_sk_<你的 Key>" }
```

也可以用 CLI 注册（效果相同）：`codex mcp add kybase --url http://192.168.31.18:8765/mcp`，
再补上 bearer 环境变量。

## 验证

1. `codex mcp list` —— 应看到 `kybase` 且不是 auth 失败态；
2. TUI 里 `/mcp` —— kybase 下应列出 8 个工具；
3. 真问一句，例如：

   > 用 kybase 查一下「眼轴怎么监测」，把出处文档名给我

   预期行为：先 `list_knowledge_bases` 再 `search`，回答里的每条结论带文档名/页码。

## 排障

| 现象 | 多半是什么 | 怎么办 |
|---|---|---|
| `codex mcp list` 显示 auth 失败 / 工具调用全 401 | 凭据没带上：环境变量名拼错、终端没重开、或 Key 已撤销 | 回管理台确认 Key 还在；`echo $KYBASE_MCP_KEY`（Windows: `echo %KYBASE_MCP_KEY%`） |
| 403 越权 | Key 绑了库范围，查的库不在范围内 | 换不限范围的 Key，或把目标库加进范围 |
| 连接超时 / 拒绝 | 不在局域网，或 NAS 上 `kybase-app-mcp` 容器没在跑 | `docker ps \| grep kybase-app-mcp`（NAS 上）；本机先 `curl http://192.168.31.18:8765/mcp` 应回 400/401 而不是拒连 |
| `search` 传了 `keep`/`min_score`/`per_doc` 没效果 | 已知缺口：这三个参数在 MCP 通道被参数白名单丢掉（《外部接入指南》§3.5） | 用 `top_k` 控制条数 |
| 想关写操作 | 只读档 Key 天然没有写权限；也可以在 config.toml 里加 `disabled_tools = ["create_knowledge_base","upload_document","add_data_source","delete_document"]` | — |

## 使用口径（给模型也给人）

- 检索结果是**原文片段**，回答时每条结论指回文档名（有页码给页码）；片段覆盖不了就直说库里没有。
- 文档处理是异步的：刚 `upload_document` 完搜不到是正常的，用 `get_document_status` 看进度，
  `searchable` 为真才进检索面。
