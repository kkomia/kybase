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
| 凭据 | 管理台**侧栏「API 密钥」页 → 新建**，`kylab_sk_…` 前缀；测试用**只读档**就够 |
| 工具面 | 纯知识库工具（见下表），没有 Agent 侧工具 |
| 网络 | **只在局域网/可信网络开放**，别暴露公网 |

工具面（`tools/list` 实读回来就是下表这些；**别比对记忆里的个数**，它随版本增长）：

| 工具 | 干什么 |
|---|---|
| `list_knowledge_bases` | 列库（检索前先调它拿库 id） |
| `search` | 混合检索，**返回带出处的原文片段**（不是生成的答案）。结果的**第一行是判定**（`summary` / `fit`），先读它再读片段 |
| `list_documents` / `get_document_status` | 文档列表 / 处理进度 |
| `get_document_content` | 取一份文档的全文：`format="markdown"`（默认）回解析后的正文，`format="original"` 回原文件 base64；正文超 20 万字符只回前一段并标 `truncated`，原文件超 32 MB 直接拒（改用 REST 下载） |
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
2. TUI 里 `/mcp` —— kybase 下应列出上表那些工具（**以列出来的为准，别比对个数**）；
3. 真问一句，例如：

   > 用 kybase 查一下「眼轴怎么监测」，把出处文档名给我

   预期行为：先 `list_knowledge_bases` 再 `search`，**先读返回第一行的 `summary`（判定）**，
   再据片段回答、每条结论带文档名 / 页码。

## 只放开一部分工具（**在服务端收窄**，推荐）

**不要用客户端配置来"禁用工具"**（Codex 的 `disabled_tools` 之类）：那是"客户端愿意遵守"
的约定，不构成边界——换一个客户端、或直接 `curl` 一下，那些工具照旧可调。真正的边界在
**服务端**：一把 API Key 可以限定它能调哪些 MCP 工具，范围外的工具**服务端会拒**。

建法（管理台**侧栏「API 密钥」页 → 新建 → 勾「工具范围」**，或直接发一张钥匙）：

```bash
curl -sS -X POST 'http://192.168.31.18:8081/api/v1/api-keys' \
  -H "Authorization: Bearer kylab_st_<管理员会话令牌>" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Codex-只读检索","permission":"readonly",
       "knowledge_base_ids":[],
       "allowed_tool_names":["search","list_knowledge_bases"]}'
# → {"token":"kylab_sk_…","allowed_tool_names":["search","list_knowledge_bases"],…}
```

- `allowed_tool_names` **留空 = 不限工具**（与 `knowledge_base_ids` 同一口径）——
  所以在这次改动之前发的 Key 行为一字未变。
- 限了范围的 Key，MCP 的 `tools/list` **只看得到它允许的那几个**：客户端不会摆出一堆
  调不动的工具。真去调范围外的，服务端在 `call_tool` 那一刻回 403 并**列出可用的工具名**。
- 写一个不存在的工具名会在**发钥匙时**被拒（`422`），不会静默存下一把"看起来授权了"的钥匙。
- **改范围要撤销后重发**：这族端点只有列表 / 新建 / 撤销三个，没有"改一把已有 Key"。
- 工具名以 `tools/list`（或管理台侧栏「工具」页 / `GET /api/v1/tools`）列出来的为准，别凭记忆写。

> 想先看全量清单：管理台侧栏「工具」页渲染的就是 `GET /api/v1/tools`——工具（标签 / 摘要 /
> 是否写操作 / 参数）+ 随附技能 + 配好的 MCP 地址。只读档凭据即可读。

## 读 `search` 的结果：**先读判定，再读片段**

`search` 的返回**第一行就是判定**，键序是
`summary` → `distribution` → `decision` → `returned` → `filtered_out` → `query` → `hits`
（`fit=none` 时末尾多一条 `note`）。这不是排版偏好——判定排在原文后面时，
读的人（模型或你）会先被一堆片段带走。

- `summary`：一句话中文判定（候选多少、收敛后留了几条、契合程度如何），**照原样转述**；
- `fit`：`strong` 很对得上 / `weak` 只对上一部分 / **`none` 这份资料答不了**
  （这时如实说"资料里没有"，**别拿这几条片段编答案**）/ `unknown` 没标定过、判不了；
- `decision` + `distribution.suggested`：命中太多时的收敛旋钮。
  直接按建议走，或自己传 `keep`（1–20）/ `min_score`（绝对余弦 0–1）/ `per_doc`（1–10）
  **再查一次**——这三个参数已进白名单，能经 MCP 生效。

一句话：**当 KB 会淹你的时候，读判定、然后收敛，而不是把 12 条片段全塞进上下文。**

## 排障

| 现象 | 多半是什么 | 怎么办 |
|---|---|---|
| `codex mcp list` 显示 auth 失败 / 工具调用全 401 | 凭据没带上：环境变量名拼错、终端没重开、或 Key 已撤销 | 回管理台确认 Key 还在；`echo $KYBASE_MCP_KEY`（Windows: `echo %KYBASE_MCP_KEY%`） |
| 403 越权 | Key 绑了库范围，查的库不在范围内；或者这把 Key 限了**工具范围**，你要调的工具不在其中 | 换不限范围的 Key，或把目标库 / 目标工具加进范围（改范围要**撤销后重发**，见上文「只放开一部分工具」） |
| 连接超时 / 拒绝 | 不在局域网，或 NAS 上 `kybase-app-mcp` 容器没在跑 | `docker ps \| grep kybase-app-mcp`（NAS 上）；本机先 `curl http://192.168.31.18:8765/mcp` 应回 400/401 而不是拒连 |
| `search` 回了一堆片段，不知道有没有用 | **没读结果的第一行**：`summary` / `fit` 就是"这批命中能不能用"的判定 | 先读 `summary` 与 `fit`；`none` 如实说资料里没有，别拿片段编答案；`unknown` 表示这个库的嵌入模型没标定过，判不了 |
| `search` 传了 `keep`/`min_score`/`per_doc` 没效果 | 这三个参数 2026-10-09 起已进白名单；若仍无效，是 `_PARAMS` 与 schema 又漂了（门禁 `test_parameter_whitelists_match_the_schemas` 会红） | 先读 `summary` 看判定；要收敛就按 `distribution.suggested` 传这三个再查一次，或用 `top_k` 控制条数 |
| 想关写操作 / 只放开一部分工具 | **在服务端收窄**，不要靠客户端配置：建 Key 时勾「工具范围」 | 只读档 Key 天然没有写权限；要更细就建一把工具范围的 Key——只勾 `search`、`list_knowledge_bases`、`list_documents`、`get_document_status`、`get_document_content` 这类只读工具，它连 `create_knowledge_base` / `upload_document` / `add_data_source` / `delete_document` 都调不动 |

## 使用口径（给模型也给人）

- 检索结果是**原文片段**，回答时每条结论指回文档名（有页码给页码）；片段覆盖不了就直说库里没有。
- **先看结果的判定行**：`fit=none` 时"库里没有"是这批片段的结论，不是检索失败；
  片段太多就收敛（见上文「读 `search` 的结果」）再读。
- 文档处理是异步的：刚 `upload_document` 完搜不到是正常的，用 `get_document_status` 看进度，
  `searchable` 为真才进检索面。
