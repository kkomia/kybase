# KYBASE

kylab 的**知识库服务**：从单体仓库 [kylab](https://gitee.com/kkomia/kylab) 里剥离出来的独立后端。
它持有摄入流水线、混合检索（向量 + BM25）、Wiki、表格副本与账号体系，
对外经**窄 API / MCP / 官方 Skill** 三条等价路径服务 Agent 侧；
Agent 侧在没有它的时候完整可用（降级而非失效）。

> 当前状态：**已在 NAS 提供正式服务**（2026-10-09 切换完成）。
> 三容器 `kybase-app`（backend + mcp + frontend）全 healthy，镜像是 `kybase-backend:0.1.0`
> 与 `kybase-frontend:0.1.0`；三个接入点：管理台 `http://192.168.31.18:8081/`、
> REST `http://192.168.31.18:8081/api/v1`、MCP `http://192.168.31.18:8765/mcp`。
> 数据**零迁移**（连的还是旧 kylab-app 那套 PG 库 `kylab` + MinIO 桶 `kylab`），
> 旧 `kylab-app` 已注销、镜像与 compose 留着作回滚。
> 这一份是自 kylab `b0685e9` 搬入的整树：`backend/`（app + tests + pyproject/uv.lock/Dockerfile）、
> 根级 `scripts/` 门禁脚本族、`deploy/`（NAS 应用层 `deploy/nas/docker-compose.yml` + 干净机器四件套），
> 加上**知识库管理台前端**（`frontend/`，形态 B：独立 WebUI，不进桌面壳）与官方 Skill。
> **瘦身已完成**（2026-10-09 本轮）：Agent 侧与本机档代码整体拆除，`backend/` 今天只剩知识库本体，
> MCP 对外面只剩纯 KB 工具（清单以 `services/tools.py` 的 `TOOL_NAMES` 为准，**别记个数**）。
> 剩下的是**分家**：环境变量前缀仍是 `KYLAB_`、
> PG 迁移序列仍是交错那一串（见交接文档 §3.2）。
> 接手先读[交接文档](docs/交接文档-v0.1.md)，施工顺序见
> [剥离方案](kylab知识库剥离方案-v0.1.md) §6。

## 从哪来

| | |
|---|---|
| 源仓库 | `E:\gitlab\kylab`——**只读**，本仓库的改动不得碰它一个字节；`backend/` 里的知识库实现已经属于本仓（kylab 侧已从 HEAD 删除） |
| 图纸 | `kylab知识库剥离方案-v0.1.md`（本仓库根目录） |
| 性质 | 一份**调研结论**，不是计划草案 |

这不是"重新设计一次拆分"：接缝在 kylab 里已经存在并验证过——`KnowledgeClient` 提供者协议、
`/provider/handshake` 握手端点、`RouterMetaStore` 方法级存储分流、`mcp_server` 对外面、
官方 Skill——剩下的活是把已画好的**逻辑边界落成物理边界**（独立进程、库、镜像、仓库）。

## 文档

| 文档 | 给谁 |
|---|---|
| [部署指南](docs/部署指南-v0.1.md) | 想自己部署一套知识库服务的运维/工程师（含「已上线形态 / 历史回滚形态 / 目标形态」对照） |
| [外部接入指南](docs/外部接入指南-v0.1.md) | 外部系统/外部 Agent 的接入方（MCP / REST / Skill / 窄 API 四条路径） |
| [交接文档](docs/交接文档-v0.1.md) | 接手施工的人 |
| [剥离方案](kylab知识库剥离方案-v0.1.md) | 需要「为什么这么切」的人 |

## 这个服务负责什么

- **摄入**：上传 → 逐文件探测 → 解析 → 切块 → 向量化 → 索引，异步流水线（租约/退避/断点续跑），
  云端与本地解析按文件动态路由；所有格式统一转 Markdown，向量化只针对 Markdown。
- **检索**：向量 + BM25 双通道 RRF 融合，可选 rerank，元数据过滤；**返回带出处的原文片段**，
  不是生成的回答——这是它对外最硬的一条语义。
  **每次检索都先给一份判定**：`summary` 一句话说清候选多少、收敛后留了几条，
  `distribution` 给出完整分布与 `fit`（`strong` / `weak` / `none` / `unknown`：这份资料
  到底对不对得上），`decision` 记这次按什么收敛的；原文排在它们之后。
  命中太多时用 `keep` / `min_score` / `per_doc` 收敛（MCP 与 REST 同名同义），
  而不是把十几条片段全塞进上下文。
  **要"把某个主题看全"就翻页**：`keep` 是一页几条、`top_k`（上限 50）是一次能翻到的总量，
  按 `offset` / `next_offset` 一路翻到 `null`——判定在每一页上逐字相同。
  每条命中还标出它是哪条通道捞上来的（`basis` / `lexical_score`），
  `min_score` 只切向量通道、绕过它的条数如实记在 `decision.unscored_kept` 里。
- **出处诚实**：页码只在文档自己的分页标记与页数自洽时才给，否则 `page` 是 `null`
  （`.md` 来源本来就没有页）——报一个翻不到原文的页码比不报更糟。
  失败也如实报：领域失败（没权限、文档不存在、参数给错）是 `isError: true` 的工具结果，
  正文是我们那句中文。
- **统计**：概览驾驶舱（文档 / 切块 / 任务 / **检索命中**趋势）、用量、以及按知识库的
  检索命中事件（哪座库在涨、哪座最近答不上来）。
- **组织**：知识库 / 目录 / 文档 / 分块 / 分享、Wiki 页、数据源连接器、表格副本（DuckDB）。
- **生成式**：摘要、Wiki 生成、检索建议问、知识库提示词（直连 `llm.py`，不经 Agent 的对话服务）。
- **账号与准入**：`users`/`sessions`/`api_keys` 与 `auth`——库分享、库范围钥匙、管理员都是 KB 语义的资产。
- **对外三条路径**：窄 API（`KnowledgeProviderClient`，握手 + 检索/入库，Agent 主链路走这条）、
  MCP（`mcp_server`，stdio + Streamable HTTP，**只暴露 KB 工具**，
  并给出**工具面指纹**——`serverInfo.version` 的 `+mcp.<指纹>` / `--list-tools` 最后一行 /
  `GET /api/v1/tools`，客户端据此判断契约有没有变过）、
  官方 Skill（`skills/kylab-knowledge-base/`，给不会说 MCP 的运行环境）。

Agent 侧的东西（对话/笔记/记忆/技能/沙箱/工作区/定时任务/外部 MCP/备份）**不归这里**。

## 目录

```
backend/app/
  api/          KB 面 REST（knowledge-bases / documents / folders / search / chunks /
                wiki / tabular / data-sources / shares / tasks / lifecycle / webhooks /
                maintenance / stats / provider / tools / api-keys）
  services/     摄入、切块、检索、嵌入、Wiki、表格、连接器、分享等 KB 业务
  storage/      postgres_impl（向量 + 全文 + KB 元数据）、duckdb_impl（表格副本）
  parsers/      插件式解析器层（互不引用）
  pipeline/     摄入状态机
  workers/      队列消费者（摄入）
  mcp_server/   对外 MCP（stdio + Streamable HTTP，强制鉴权）
  core/         配置、异常、安全/签名、HTTP、日志、文本卫生等纯契约
backend/tests/  测试（KB 服务的门禁落点）——unit/（与 app/ 镜像同构）+ integration/ + bench/
backend/        pyproject.toml / uv.lock / Dockerfile / .env.example / README.md
scripts/        门禁脚本族（check-backend / check-frontend / check_layering / scan_emoji / ci / …）
skills/         官方 Skill（顶层交付物：会被安装到别处，不得 import app/）
frontend/       知识库管理台（Vite + React + TS + pnpm）——独立 WebUI，见下；自带 Dockerfile + nginx.conf
deploy/         compose：`deploy/docker-compose.yml`（干净机器一键起四件套，未实测）
                与 `deploy/nas/docker-compose.yml`（NAS 应用层：backend + mcp + frontend，
                **已上线**，共用存储层的同库同桶）
docs/           本服务的文档：部署指南 / 外部接入指南 / 交接文档
```

`backend/app/` 下的是**实现**（不是占位）。测试分两档：全量要一个带 pgvector 的 PostgreSQL
（`KYLAB_TEST_DATABASE_URL`，测试会自己建一个临时库、跑完删掉；`check-backend` 缺它直接判红，
因为「全跳过」的绿没有意义）；**没有 PG 的机器**跑 `pytest tests -m local`——进程内 / 文件系统
那一半（2026-10-20 实测 76 passed / 1 skipped）。搬迁时登记的「测试树没跟上 `KbServices` 拆分」
那批红，已在 2026-10-09 的两轮瘦身提交里跟着测试树一起收口（NAS 测试库上全量真跑）。

### frontend/（知识库管理台）

老板 2026-10-08 拍板：知识库前端走**独立 WebUI**（形态 B），kylab 前端以后不再有知识库页面群。
这一份就是从 kylab `frontend/` 按 **import 闭包**搬出来的知识库页面群 + 登录 + 壳（只留
「所有知识库 / 概览 / 任务中心 / 接入 / API 密钥」这一组导航，后两项是 2026-10-09 补的
对外能力面；「接入」原名「工具」，2026-10-10 改名、路由仍是 `/tools`），
agent 语义的入口一个都没进来。

| 项 | 值 |
|---|---|
| 技术栈 | Vite 8 + React 19 + TS 5.9 + Tailwind 4 + pnpm 11（与 kylab 前端同版本） |
| 开发端口 | **5175**（5173 是 kylab 前端的，两个要能同时跑） |
| `/api` 反代 | 默认 `http://192.168.31.18:8081`（NAS 上正在跑的知识库服务）；改档用 `KYBASE_API_TARGET=... pnpm dev` |
| 起法 | `cd frontend && pnpm install && pnpm dev` |
| 门禁 | `pnpm exec tsc --noEmit` / `pnpm exec eslint src` / `pnpm build` / `pnpm test` |

**线上跑的就是这一份**（`kybase-frontend:0.1.0`，NAS 8081）；它现在属于本仓库自己的代码，
与 kylab 那份不再互相同步——两条路径从此各走各的。

## 怎么参与

1. **先读两份**：本 README → [交接文档](docs/交接文档-v0.1.md)；需要"为什么这么切"时再翻
   [剥离方案](kylab知识库剥离方案-v0.1.md)。
2. **改之前先看归属**：`backend/` 今天只剩知识库本体——Agent 侧与本机档代码已在 2026-10-09 的
   瘦身里整体拆除（交接文档 §3.2、§3.5）；新代码只落在知识库这一域，别把 Agent 语义的东西长回来。
3. **门禁**：按 kylab 的双平台脚本口径跑（`sh scripts/check-backend.sh`，
   Windows 用同名 `.ps1`；前端 `sh scripts/check-frontend.sh`）。
   **域间引用检查（`scripts/check_domains.py`）已删除**：它守的是"KB 域 ↔ Agent 域不许互引"
   那张归属表（剥离方案 §3.1），而本仓只剩 KB 域、名单里的 Agent 侧模块
   （`app.services.chat` / `skill*` / `sandbox` / `app.api.v1.local` / `app.storage.sqlite_impl` …）
   整族已删，跑起来只是围着不存在的模块报旧账，还会往本仓没有的 `docs/计划与记录/` 写报告。
   分层引用方向由 `check_layering.py` 的 L1–L6 全量守着（api / services / storage / parsers）。
4. **提交**：`<类型>: <简述>`，类型限 `feat/fix/docs/test/refactor/chore`（与 kylab 同一套）。
   `origin`（Gitee）已经在推进，`master` 与 `origin/master` 同步在 `1b88fc0`；
   NAS 上的部署检出从同一个仓库 `git pull` 升级。推送仍要有人明确要求。
5. **NAS 上不许乱动**：`kylab-postgres` / `kylab-minio` 那套**存储层一个字节都不动**
   （两代应用共用），应用层今天是 `kybase-app`（交接文档 §3.5、§7 第 1 条）。
