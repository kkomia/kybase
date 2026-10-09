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
> 还没做的是**瘦身与分家**：树里仍带着 Agent 侧代码、环境变量前缀仍是 `KYLAB_`、
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
- **组织**：知识库 / 目录 / 文档 / 分块 / 分享、Wiki 页、数据源连接器、表格副本（DuckDB）。
- **生成式**：摘要、Wiki 生成、检索建议问、知识库提示词（直连 `llm.py`，不经 Agent 的对话服务）。
- **账号与准入**：`users`/`sessions`/`api_keys` 与 `auth`——库分享、库范围钥匙、管理员都是 KB 语义的资产。
- **对外三条路径**：窄 API（`KnowledgeProviderClient`，握手 + 检索/入库，Agent 主链路走这条）、
  MCP（`mcp_server`，stdio + Streamable HTTP，**只暴露 KB 工具**）、
  官方 Skill（`skills/kylab-knowledge-base/`，给不会说 MCP 的运行环境）。

Agent 侧的东西（对话/笔记/记忆/技能/沙箱/工作区/定时任务/外部 MCP/备份）**不归这里**。

## 目录

```
backend/app/
  api/          KB 面 REST（knowledge-bases / documents / folders / search / chunks /
                wiki / tabular / data-sources / shares / tasks / lifecycle / webhooks /
                maintenance / stats / provider）
  services/     摄入、切块、检索、嵌入、Wiki、表格、连接器、分享、备份外的 KB 业务
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

`backend/app/` 下的是**实现**（不是占位），`pytest tests` 能跑起来——**但还没全绿**：
搬进来的是 `b0685e9` 那一刻的测试树，其中一批用例还没跟上 `KbServices` 拆分（kylab 在
删除链里随后才改的），另一批要读仍留在 kylab 的《API 接口规范》等文档与另外 4 个技能。

### frontend/（知识库管理台）

老板 2026-10-08 拍板：知识库前端走**独立 WebUI**（形态 B），kylab 前端以后不再有知识库页面群。
这一份就是从 kylab `frontend/` 按 **import 闭包**搬出来的知识库页面群 + 登录 + 壳（只留
「所有知识库 / 概览 / 任务中心」这一组导航），agent 语义的入口一个都没进来。

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
2. **改之前先看归属**：`backend/` 里仍带着 Agent 侧代码（搬入时不瘦身），
   **不要顺手改那些文件**——它们归 Agent 侧，瘦身时会被删（交接文档 §3.2）；
   新代码只落在知识库这一域。
3. **门禁**：按 kylab 的双平台脚本口径跑（`sh scripts/check-backend.sh`，
   Windows 用同名 `.ps1`；前端 `sh scripts/check-frontend.sh`）。
   **域间引用检查还差最后一步**：`scripts/check_domains.py`（覆盖
   `app.core`/`app.models`/`app.pipeline`/`storage.*` 内部与 `services/` 内部互引，
   即 `check_layering.py` 的结构性盲区）已经在仓库里，但**还没接进 `check-backend.sh`**
   ——它自己写明"本阶段只报告不拦门"（交接文档遗留 #2）。
4. **提交**：`<类型>: <简述>`，类型限 `feat/fix/docs/test/refactor/chore`（与 kylab 同一套）。
   `origin`（Gitee）已经在推进，`master` 与 `origin/master` 同步在 `1b88fc0`；
   NAS 上的部署检出从同一个仓库 `git pull` 升级。推送仍要有人明确要求。
5. **NAS 上不许乱动**：`kylab-postgres` / `kylab-minio` 那套**存储层一个字节都不动**
   （两代应用共用），应用层今天是 `kybase-app`（交接文档 §3.5、§7 第 1 条）。
