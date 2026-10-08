# KYBASE

kylab 的**知识库服务**：从单体仓库 [kylab](https://gitee.com/kkomia/kylab) 里剥离出来的独立后端。
它持有摄入流水线、混合检索（向量 + BM25）、Wiki、表格副本与账号体系，
对外经**窄 API / MCP / 官方 Skill** 三条等价路径服务 Agent 侧；
Agent 侧在没有它的时候完整可用（降级而非失效）。

> 当前状态：**剥离进行中**（2026-10-08 起）。本仓库此刻只有目录骨架与官方 Skill，
> 服务端实现仍在 kylab 仓内跑着（NAS 上的单体**照跑不动**）。
> 接手先读[交接文档](docs/交接文档-v0.1.md)，施工顺序见
> [剥离方案](kylab知识库剥离方案-v0.1.md) §6。

## 从哪来

| | |
|---|---|
| 源仓库 | `E:\gitlab\kylab`——剥离完成前，**同一文件以 kylab 为准**，本仓库不回写 |
| 图纸 | `kylab知识库剥离方案-v0.1.md`（本仓库根目录） |
| 性质 | 一份**调研结论**，不是计划草案 |

这不是"重新设计一次拆分"：接缝在 kylab 里已经存在并验证过——`KnowledgeClient` 提供者协议、
`/provider/handshake` 握手端点、`RouterMetaStore` 方法级存储分流、`mcp_server` 对外面、
官方 Skill——剩下的活是把已画好的**逻辑边界落成物理边界**（独立进程、库、镜像、仓库）。

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
skills/         官方 Skill（顶层交付物：会被安装到别处，不得 import app/）
deploy/         独立 compose（PG + 对象存储 + KB 后端），待填
docs/           本服务的文档；交接文档在此
tests/          测试（KB 服务的门禁落点），待填
```

骨架里的 `__init__.py` 是**占位**：目录先立住、边界先看得见，实现随方案 §6 各阶段搬入。

## 怎么参与

1. **先读两份**：本 README → [交接文档](docs/交接文档-v0.1.md)；需要"为什么这么切"时再翻
   [剥离方案](kylab知识库剥离方案-v0.1.md)。
2. **挪代码前先在 kylab 仓内切割**：方案 §6 的原则是"先完成逻辑切割并跑绿门禁，再做物理搬迁"，
   每一步都可独立验收、可回退。**不要**直接往本仓库贴实现。
3. **门禁**：本仓库的实现进来后照 kylab 的双平台脚本口径跑
   （`sh scripts/check-backend.sh`，Windows 用同名 `.ps1`），并补上域间引用检查——
   kylab 现有的 `scripts/check_layering.py` **不覆盖** `app.core`/`app.models`/`app.pipeline`/
   `storage.*` 内部与 `services/` 内部互引，不补的话切割线会被悄悄踩回去（方案 §4 末）。
4. **提交**：`<类型>: <简述>`，类型限 `feat/fix/docs/test/refactor/chore`（与 kylab 同一套）。
   未经明确要求不 push——远端 `origin` 已配置但至今没有推送。
5. **别改源仓库之外的假设**：NAS 上的单体在切割完成前保持门禁全绿、部署不动。
