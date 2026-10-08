# kylab 知识库剥离方案 v0.1

> 2026-10-08 · 调研对象：`E:\gitlab\kylab`（只读调查，未做任何修改）
> 目标读者：kylab 项目同事
> 一句话结论：**这个项目已经把"知识库剥离"做完了一半**——提供者协议、握手端点、方法级存储分流、MCP 对外面、官方 Skill 全部存在并经过验证。剩下的工作不是"设计拆分"，而是"把已经画好的接缝落成独立的进程、库、镜像、仓库"。

---

## 1. 背景与目标

kylab 当前形态：Agent（对话工具循环 / 工作区 / 技能 / 外部 MCP / 子 Agent / 沙箱 / 笔记 / 记忆）与知识库（摄入流水线 / 混合检索 / Wiki / 表格副本）同处一个后端进程、一个 PG 库、一个对象存储桶、一个前端壳。

目标：知识库剥离为**独立服务**，Agent 侧通过 **MCP / Skill / 窄 API** 接入；Agent 在没有知识库时完整可用（降级而非失效）。

该目标并非新发明——`docs/设计/架构设计-v0.3.md` §9 M3 的验收原文就是它的定义：「**不配知识库时导航无 KB 页、Agent 全流程不受影响**」。本方案是把这条已验收的逻辑边界，落成物理边界。

---

## 2. 现状：已存在的"半剥离"事实

调研确认的五个既成事实，是剥离方案的地基：

1. **部署档已分**。`KYLAB_DEPLOYMENT` 两个档位（`backend/app/core/config.py`）：`server` 档挂 KB 路由表，`local` 档挂 Agent 路由表（`backend/app/api/v1/router.py:103` / `:187`）。**服务器档今天实质只剩"知识库管理台 + 备份 + 健康"**——会话面端点已从服务器档退役（`router.py:15-57` 记录了该决定），仅剩 `/conversations/export` 与 `/chat/suggested-questions` 两条。
2. **检索已抽象成协议**。`KnowledgeClient` 协议（`backend/app/services/knowledge_client.py:34`），本机档实现 `KnowledgeProviderClient`（`services/knowledge_provider.py:382`）经 `RemoteKnowledgeClient`（`services/remote_clients.py:113`）打 `POST /api/v1/search`。
3. **存储已按域分流**。`RouterMetaStore` 按方法名把 200+ 个 `MetaStore` 方法切成"本机域 / KB 域"两半（`storage/split_impl/router.py:51-63`），KB 域走 `RemoteMetaStore` 打 NAS 窄 API（`split_impl/remote_meta.py`）。
4. **对外通道已为外部 Agent 备好**。`mcp_server/`（stdio + HTTP，强制鉴权）暴露的工具即 KB 工具面；官方 Skill `skills/kylab-knowledge-base/` 已覆盖 MCP 与 REST 两条接入路径。
5. **前端 KB 调用本来就恒打服务器**。`frontend/src/api/sidecar.ts:156-180` 的 `LOCAL_PATHS` 表里**没有任何 KB 路径**；KB 页面统一由 `ProviderRoute` 守卫，provider 非 ready 时导航里不出现 KB 页。

**Agent → KB 的进程内调用，全仓只有一处**：`services/agent_tools.py:1263` → `services.chat.retrieve_sources(...)`。

---

## 3. 归属清单

### 3.1 后端模块归属（`backend/app/`）

**KB 带走**：

| 层 | 内容 |
|---|---|
| api | `knowledge_bases.py` `documents.py` `folders.py` `search.py` `chunks.py` `wiki.py` `tabular.py` `data_sources.py` `shares.py` `tasks.py` `lifecycle.py` `webhooks.py` `maintenance.py` `stats.py` `provider.py` |
| services | `knowledge_base.py` `documents.py` `folder.py` `chunk.py` `chunking.py` `batch.py` `ingest.py` `parser_router.py` `splitting.py` `retrieval/` `embedding/` `summary.py` `timeline.py` `wiki.py` `suggested_questions.py` `kb_prompt.py` `tabular.py` `tabular_sql.py` `office.py` `sources.py` `connectors/` `retrieval_eval.py` `lifecycle.py` `maintenance.py` `share.py` `stats.py` `webhook.py` `observability.py` |
| parsers/ | 全部（插件层，互不引用，零障碍） |
| pipeline/ | `state_machine.py`（摄入状态机） |
| storage | `postgres_impl/`（向量+全文+KB元数据）、`duckdb_impl/`（表格副本）、`split_impl/`（历史使命结束，可留 Agent 侧做降级实现） |
| workers | `queue_worker.py`（摄入消费者） |
| mcp_server/ | 整体带走（它暴露的就是 KB 工具面，`server.py:22` 复用 `services/tools.py`） |

**Agent 留下**：

| 层 | 内容 |
|---|---|
| api | `conversations.py` `chat.py`（Agent 链路部分）`notes.py` `memory.py` `skills.py` `plugins.py` `mcp_servers.py` `sandbox.py` `workspaces.py` `schedules.py` `web.py` `site_icons.py` `local.py` |
| services | `agent.py` `tool_loop.py` `agent_tools.py` `agent_exec.py` `agent_files.py` `subagent.py` `sandbox.py` `isolation.py` `command_policy.py` `commands.py` `approvals.py` `skills*.py` `plugins.py` `mcp_client.py` `workspace.py` `notes.py` `note_ai.py` `artifacts.py` `schedules*.py` `cron.py` `conversation*.py` `session_events.py` `live_turns.py` `resume.py` `modes.py` `prompt.py` `thinking.py` `plan_gate.py` `tool_meta.py` `failures.py` `deck/` `memory*.py` `archive*.py` |
| storage | `sqlite_impl/`（本机库） |
| workers | `local_worker.py` |
| 入口 | `sidecar.py` |

**共享底座（需要决策归属，见 §5.3）**：`core/`（config / exceptions / security / signing / http / logging / page_markers / text_hygiene）、`models/enums.py`、`services/llm.py` + `model_registry.py` + `provider_presets.py` + `runtime_config.py` + `credentials.py` + `secrets.py`、`services/api_key.py` + `auth.py` + `users.py` + `avatars.py`、`services/usage.py` + `idempotency.py` + `system_load.py`、`services/backup_*.py`、`storage/base.py` + `s3_impl/` + `local_impl/` + `repositories.py` + `text.py`。

### 3.2 REST 路由归属

- **KB 面**（服务器档现有路由的绝大部分）：`/knowledge-bases` `/documents` `/folders` `/search` `/chunks` `/data-sources` `…/shares` `/wiki*` `/tabular` `/lifecycle` `/maintenance` `/tasks` `/webhooks` `/stats/dashboard` `/provider/handshake` `/chat/suggested-questions`。
- **Agent 面**（本机档现有路由）：`/conversations/*` `/notes` `/memory` `/settings` `/workspaces` `/scheduled-tasks` `/mcp-servers` `/skills` `/plugins` `/sandbox` `/local/*` 等。
- **共享/待定**：`/auth` `/users` `/api-keys`（账号体系，建议随 KB 走，见 §5.3）；`/backup/*`（备份提供者，与 KB 是**两个提供者**，S3 凭据只在服务端——归属需表态）；`/model-registry` `/model-proxy`。

### 3.3 MCP tools 归属（共 21 个）

- **纯 KB（10）**：`list_knowledge_bases` `create_knowledge_base` `upload_document` `add_data_source` `search` `list_documents` `get_document_status` `delete_document` + 跨域 2 个 `attach_note_to_kb` `ingest_artifact`（入库语义在 KB 侧）。
- **纯 Agent（9）**：`create_note` `list_notes` `recall` `remember` `forget` `web_search` `web_fetch` `export_document` `export_table` `export_deck` `export_file`。

注意：`export_document` 等交付工具的实现在 `services/tools.py` 里，但 **MCP server 会把整张工具表挂出去**——剥离后 KB 的 MCP server 只应暴露 KB 工具，`agent_tools.py:511-524` 的 `_KB_TOOLS` 白名单（10 个工具点名在册）就是现成的切分依据。

### 3.4 数据归属

PG 侧（`storage/postgres_impl/schema.sql` + `schema.py` 增量 v2–v17）：

- **KB 表**：`knowledge_bases` `kb_folders` `kb_shares` `data_sources` `documents` `document_parts` `parse_results` `document_stage_events` `chunks` `images` `chunk_images` `vec_<kb_id>`（动态分区）`wiki_pages` `wiki_page_sources` `trash` `webhooks`。
- **Agent 表**：`conversations` `chat_messages` `session_events` `conversation_artifacts` `notes` `note_tags` `note_folders` `workspaces` `scheduled_tasks` `mcp_servers`。
- **共享表**：`users` `sessions` `api_keys` `app_settings` `model_providers` `model_registry` `tasks`（一张队列装 KB 摄入 + Agent 定时任务两种活）`usage_events` `idempotency_keys`。

**好消息：跨域引用全部是无外键的软引用**（`conversations.kb_ids`、`chat_messages.sources` 快照、`notes.kb_id/doc_id`、`workspaces.kb_ids`、`scheduled_tasks.kb_ids`、`api_keys.knowledge_base_ids` 全是 jsonb/文本）。物理拆库不撞任何外键约束，但要定义"引用的库已不存在"的语义。

其他存储：

- **DuckDB 表格副本**（`<data_dir>/tabular.duckdb`）：纯 KB 域，随 KB 走。注意单文件单写者语义，KB 服务多副本时需另行处理。
- **对象存储**：当前同桶同前缀根，按顶层前缀逻辑隔离——`originals/`（内容寻址）`markdown/` `images/` `.trash/` 归 KB；`conversations/` `avatars/` `images/notes/` 归 Agent。剥离时建议**分桶或按前缀发不同凭据**。
- **文件系统域**：memory / skills / sandbox / plugins **没有任何表**，全在 `<data_dir>` 下，归 Agent，不构成数据库层障碍。

### 3.5 前端归属

- **KB 带走**：`features/knowledge/`（25 文件，含页面/抽屉/检索面板/上传/分享/Wiki/数据源/`store.ts`/`ProviderRoute`）、`features/misc/dashboard/`（数据源是 KB 的 `/stats/dashboard` 与 `/tasks`）、`features/misc/tasks/`。
- **Agent 留下**：`features/chat/` `features/notes/` `features/layout/`（应用壳）`features/misc/{memory,capabilities,settings,workspaces}` `features/backup/`。
- **共享**：`features/preview/`（KB 的 DocumentDrawer 与 Agent 的 chat 面板都用，且 `FilePreview.tsx:110` 反向引 `chat/model/markdown`）、`src/ui/**`、`src/lib/*`、`api/client.ts`、样式 token 层。

---

## 4. 真正的卡点（按卡住程度排序）

调研确认：调用点只有一处，卡人的是**四个"混合体"文件 + 一张混装宽接口**。

| # | 文件 | 问题 |
|---|---|---|
| 1 | `backend/app/core/services.py`（1026 行） | **单一组合根**：`Services` 数据类约 60 字段把 KB 与 Agent 服务混装一张图，`build_services` 里互相注入（`:711` ChatService、`:746` IngestService、`:762` DocumentService、`:785` ArtifactService、`:900` NotesService）。**第一刀必须切这里** |
| 2 | `backend/app/services/chat.py`（1805 行） | `retrieve_sources`（KB 检索本体，`:782-870`）与 `tool_loop`/`run_subagent_text`/`agent_messages`（Agent）同处一类；且 `:25-33` 反向 import Agent 工具循环。KB 侧三处生成式（`summary`/`wiki`/`kb_prompt`）把 `ChatService.ask_raw` 当 LLM 网关 |
| 3 | `backend/app/services/tools.py`（2161 行） | KB 工具与 Agent 交付工具同表；`:1836`/`:2024` 反向 import `services.sandbox`（Agent）；MCP 对外面钉在它上面 |
| 4 | `backend/app/storage/base.py` | `MetaStore` 200+ 抽象方法混装 KB 域与本机域；`RouterMetaStore` 靠 `LOCAL_METHODS` 补集机械切两半 |
| 5 | `backend/app/api/v1/schemas.py`（3407 行） | KB 与 Agent 的请求/响应模型同住一个文件 |
| 6 | `services/knowledge_provider.py` | 已是 KB 客户端，但被五处共用同一实例（ChatService 检索 / `Services.ingest`/`documents` / `stores.meta.kb` reader / `/local/provider` / `sidecar.Clients`） |
| 7 | `services/api_key.py` | `check_access`/`visible_kb_ids` 既是 Agent 工具准入闸，又直接读 KB 表（`:214/264/277/313`） |
| 8 | `services/notes.py` + `services/artifacts.py` | 各自持有**真** `IngestService`/`DocumentService` 引用（`notes.py:104-108`、`artifacts.py:252-254`），`dataclasses.replace` 换不掉——这是 `attach_note_to_kb` 曾 503 的已知机制（`知识库提供者-实施方案-v0.1.md` 发现②） |
| 9 | PG `MIGRATIONS` 元组 | KB 迁移（v2/v4/v8…）与 Agent 迁移（v5/v6/v10/v12/v13…）交错连号，拆服务后两边版本号互相牵制 |
| 10 | `config.py` | 扁平 `Settings`，KB 与 Agent 环境变量混在一处；`data_dir` 派生出所有存储路径，是最大共享耦合点 |

另有一个**结构性盲区**要提醒：`scripts/check_layering.py` 只覆盖 `app.api`/`app.mcp_server`/`app.services`/`app.parsers` 四个前缀——**`app.core`、`app.models`、`app.pipeline`、`app.storage.*` 内部、`services/` 内部互引完全无机械约束**。剥离期间建议在两个新仓库各自补上域间引用检查，否则切割线会被人悄悄踩回去。

---

## 5. 目标架构与关键决策

### 5.1 目标形态

```
┌─────────────────────────┐         ┌──────────────────────────┐
│  kylab（Agent）          │         │  知识库服务（新仓库/新服务） │
│  壳 + 边车(本地后端)      │  窄API  │  FastAPI + PG/pgvector     │
│  会话/笔记/记忆/技能/沙箱  │ ──────► │  + 对象存储 + DuckDB        │
│  工作区/定时任务/外部MCP   │  +MCP   │  摄入/检索/Wiki/表格/账号    │
│  ─ KB 客户端(provider)  │ ◄────── │  MCP server + 官方 Skill    │
└─────────────────────────┘  握手    └──────────────────────────┘
```

三条接入路径（与现状一致，能力等价）：
1. **窄 API**（`KnowledgeProviderClient`，握手 `/api/v1/provider/handshake` + 检索/入库）——Agent 工具循环内嵌 `search`/`ingest` 走这里；
2. **MCP**——KB 服务自带的 `mcp_server` 直接登记为 Agent 的外部 MCP 服务（Agent 侧 `mcp_client.py` 已支持 stdio/streamable-http，且实测连过自家 MCP）；
3. **Skill**——`skills/kylab-knowledge-base/` 本来就是"会被安装到别处"的独立交付物（规范 §6.3），原样带走。

### 5.2 接入方式建议

**Agent 内置工具面继续走窄 API（KnowledgeProviderClient），不要改成走 MCP**。理由：窄 API 已实现握手/能力集/三态降级/工具表门控（`KB_PROVIDER_TOOLS`，state≠ready 摘工具）/KB 元数据缓存（`/local/kb-cache`，M4 的"非 ready 只读降级"模板）一整套，是 Agent 主链路的现成形态；MCP 留给外部 Agent 与进阶用户。两者服务端同源（同一份检索实现），不冲突。

### 5.3 共享底座的归属决策（需要拍板）

| 共享件 | 建议归属 | 理由 |
|---|---|---|
| 账号体系（`users`/`sessions`/`api_keys` + `auth`） | **随 KB** | 库分享、库范围钥匙、管理员都是 KB 语义的资产；Agent 本机档本来就不设门禁（`LOCAL_CALLER` 短路）。注意现有坑：裸 API Key 建的资源 `user_id=None` 走共享桶，独立后若需按用户归属，须让调用方带会话令牌或由 Agent 侧代填 owner |
| LLM/embedding 接入（`llm.py`/`model_registry`/`credentials`/`secrets`） | **两份拷贝，各自演化** | KB 侧生成式（summary/wiki/kb_prompt/suggested_questions）对 `ChatService.ask_raw` 的依赖改为直接调 `llm.py` + `runtime_config`；两边模型注册表本就同表不同行（靠 `capabilities`/`dim` 区分），拆库后自然分家。代价是少量代码重复，换来独立演进 |
| `storage/base.py` MetaStore | **按 `LOCAL_METHODS` 切两个窄接口** | `split_impl/router.py:61-63` 那份方法清单就是现成切分线；KB 侧只留 KB 方法集，Agent 侧只留本机方法集 |
| `core/` 纯契约（exceptions/signing/page_markers/text_hygiene/http） | **抽成共享包或双拷贝** | 量级小、无业务语义；建议先双拷贝，稳定后再考虑是否做成内部包 |
| `tasks` 队列表 | **随 KB；Agent 侧定时任务另起机制** | 队列里 KB 摄入占绝对主体；`local_worker.py` 本就不跑摄入（`:1-45` 逐条解释了为什么），Agent 定时任务在本机 SQLite 已有落点 |
| 备份（`backup_*.py` + `/backup/*` + `kylab-backup` 桶） | **归 Agent/本机侧** | 备份的是本机数据（会话/笔记/设置），`BackupProvider` 本来就是与 KB 平行的第二个提供者 |
| `usage_events` `idempotency_keys` `app_settings` | 各带一份 | 无跨域语义 |

### 5.4 前端处置（推荐形态 A，保留演进为 B 的余地）

- **形态 A（推荐起步）**：同一壳内两套前端。现状已接近该形态——路由/ API 文件 / store 已按域切开，KB 页面统一走 `ProviderRoute`，KB 接口恒打服务器。改动集中在 §3.5 列出的耦合点（对话选库 `ComposerControls.tsx:246`、引用原文展示、`ingest` 弹窗、笔记入库、KB 清单的 4 份缓存收敛为 1 份）。
- **形态 B（可选终态）**：KB 独立前端。`features/knowledge/*` 可整包搬走，但应用壳在 Agent 侧、`preview` 域两边都消费、`ui/lib/styles` 要抽包或复制，登录双档逻辑要表态。工作量比 A 大一个量级。
- **形态 C（不推荐单独使用）**：KB 无独立前端纯靠 MCP。Wiki、库设置、分享、目录、处理时间线、检索分数展示等**没有 MCP 工具对应物**的页面级交互会全部丢失；`DashboardPage`/`TasksPage`/设置里的"知识库连接"一节会失去数据源。

**建议：A 起步**——KB 服务独立部署后，现有前端继续以"壳 + ProviderRoute"形态同时服务两边；是否再拆 B 由后续产品决策决定。

---

## 6. 剥离步骤（建议执行顺序）

> 原则：**先在 kylab 仓内完成逻辑切割并跑绿门禁，再做物理搬迁**。每一步都可独立验收、可回退。

**阶段 0 · 准备（不改行为）**
1. 给 `services/` 内部、`core/`、`storage/` 补域间引用检查（扩 `check_layering.py` 或新增脚本），把 §3.1 的归属表落成机械约束——先让"越界引用"亮红灯，再动手。
2. 收敛前端 KB 清单读取路径（4 份 queryKey → 1 份）。

**阶段 1 · 切组合根与混合体（kylab 仓内）**
3. `core/services.py`：`Services` 拆成 `Services`（Agent）与 `KbServices`（KB）两个数据类与两个构建函数，`main.py`/`sidecar.py` 按档位各取所需。
4. `services/chat.py` 对切：`retrieve_sources` + `SourceRef` + 分段阅读器归 KB（移入 `services/retrieval/` 或新模块）；`tool_loop`/`run_subagent_text`/`agent_messages` 归 Agent。KB 三处生成式改直连 `llm.py`。
5. `services/tools.py` 按 `_KB_TOOLS` 白名单（`agent_tools.py:511-524`）对切；修掉 `:1836`/`:2024` 对 `services.sandbox` 的反向依赖（KB 侧只接收"目标目录"参数，不自己 `sandbox_for`）。
6. `storage/base.py`：`MetaStore` 按 `LOCAL_METHODS` 切成 `KbMetaStore` / `LocalMetaStore` 两个窄接口；`RouterMetaStore`/`RemoteMetaStore` 随之简化（KB 独立后它们的使命结束，降级实现可留 Agent 侧）。
7. `api/v1/schemas.py` 按域切文件；`config.py` 切成 KB 引导配置 / Agent 引导配置 / 共享基础设施三组。
8. 解开 `notes.py:104-108` 与 `artifacts.py:252-254` 对真 `IngestService`/`DocumentService` 的持有——改为面向 `KnowledgeClient` 协议编程（这是已知 503 机制的根，正好一并修掉）。

**阶段 2 · 数据与配置分家**
9. PG `MIGRATIONS` 按域切成两条独立连号序列；KB 表集合导出为 KB 服务的基线 schema（向量分区表 `vec_<kb_id>` 的 `ensure_partition` 逻辑随之带走）。
10. 对象存储：分桶或按前缀发凭据；`data_dir` 派生逻辑各自收敛。
11. `tasks` 队列归属落地；`usage_events`/`app_settings` 各带一份。

**阶段 3 · 物理独立**
12. 新建 KB 服务仓库（候选名 `kybase`）：带走 §3.1 KB 列 + parsers/ + pipeline/ + `postgres_impl` + `duckdb_impl` + `queue_worker` + `mcp_server` + 账号体系 + 共享契约；`deploy/` 出一份独立 compose（PG + MinIO + KB 后端）。
13. kylab 侧删除 KB 服务端实现，保留 `KnowledgeProviderClient`/`KnowledgeClient`/`kb_cache`/`RemoteMetaStore` 降级链路与 `ProviderRoute` 前端门控——**这些一行都不用改，它们本来就是为"KB 在别处"写的**。
14. `skills/kylab-knowledge-base/` 移到 KB 仓库维护（规范 §6.3 的判据本来就这么定）。
15. OpenAPI/前端类型生成链（`gen_api_spec.py`/`gen_api_types.py`/`schema.d.ts`）随域拆分，两边各自生成。

**阶段 4 · 收尾**
16. 文档拆分：KB 服务基线文档 = `架构设计-v0.2.md` §2/§3/§4/§5 + `API-接口规范` + `部署与运行-v0.4.md` §2.4；Agent 侧更新 v0.3 与 M3 口径为"已物理分离"。
17. 版本与发布解耦：KB 服务独立版本号（当前没有，需新立）。

---

## 7. 风险与已登记的坑

1. **鉴权双身份**（M3 §1.4 + R7 已登记）：KB 页面用会话令牌、边车用长期 API Key，两套身份同一台 NAS。剥离后该事实不变，但要在 KB 服务的部署文档里写清。
2. **`user_id=None` 共享桶**：裸 API Key 通道建的资源无归属人。独立 KB 服务若要按用户隔离，需协议上补 owner 传递。
3. **DuckDB 单写者**：KB 服务若要多副本，表格副本层要先行改造。
4. **前端登录门禁**（M3 R13 / M4 §9-5 登记未收）：整台 NAS 不可达时前端仍被 `AuthGate` 拦回登录页。剥离后 Agent 侧应能完全脱离 KB 可用，这条遗留正好顺手收掉。
5. **`mcp_server` 会挂整张工具表**：KB 的 MCP server 剥离后必须只暴露 KB 工具（含参数白名单 `server.py:31`），否则 Agent 工具会从 KB 服务泄漏出去。
6. **文档滞后点**：`skills/kylab-knowledge-base/README.md` 关于"MCP HTTP 模式无鉴权"的表述与现状（强制鉴权）矛盾，迁移时顺手修正。
7. **迁移版本号断裂**：现有部署的 PG 已按交错序列升到 v17。切序列时要给存量库一条"版本映射/冻结点"路径，不能只照顾新装。

---

## 8. 验收标准（剥离完成的定义）

1. KB 服务独立启动：自有 compose（PG + 对象存储 + KB 后端），摄入 → 检索 → Wiki → MCP 全链路绿。
2. kylab（Agent）在**不配 KB 地址**时全功能可用（M3 验收原文的物理版）：导航无 KB 页、对话/笔记/记忆/技能/沙箱/定时任务不受影响。
3. kylab 配上 KB 地址后，`search`/`attach_note_to_kb`/`ingest_file` 三件套经 `KnowledgeProviderClient` 工作，provider 三态降级正确。
4. 外部 Agent 经 KB 服务的 MCP 完成「上传 → 检索」（沿用 M4 验收口径）。
5. 两个仓库各自门禁全绿，且域间引用检查（阶段 0 新增）无红灯。
6. 存量部署有明确的迁移路径文档（PG 数据归 KB、本机 SQLite 归 Agent，本来就是两份库）。

---

## 9. 附：关键证据索引

- 唯一进程内调用点：`backend/app/services/agent_tools.py:1263`
- 检索协议：`backend/app/services/knowledge_client.py:34`；本机实现 `knowledge_provider.py:382`
- 存储分流：`backend/app/storage/split_impl/router.py:51-63`、`remote_meta.py`
- 握手端点：`backend/app/api/v1/provider.py`；规范：`docs/规范/API-接口规范-v0.1.md` §1.12
- 工具白名单：`_KB_TOOLS`（`agent_tools.py:511-524`）、`KB_PROVIDER_TOOLS`（`sidecar.py:539`）
- 部署档路由：`backend/app/api/v1/router.py:103`/`:187`
- 目标态定义：`docs/设计/架构设计-v0.3.md` §3 提供者模型、§9 M3
- 分界线原则：「分界线画在 API，不画在数据库」——`docs/规范/数据归属与同步-v0.1.md` §2（v0.3 沿用至今）
