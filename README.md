# kybase

**面向 AI Agent 的知识库服务。** 上传文档，解析成可检索的索引；检索返回带页码与
标题路径的原文片段，而不是生成好的回答。

MIT · 自托管 · MCP / REST / Skill 三种接入方式 · PostgreSQL + pgvector · Docker

---

## 它是什么

kybase 负责三件事：

1. 把 PDF、Word、图片与文本解析成 Markdown，按页切块并建立索引；
2. 建库、管理文档、维护账号与 API Key；
3. 对外提供带出处的检索：命中带原文片段、页码、标题路径、相似度与命中的检索通道。

它不生成回答，也不做对话、记忆、笔记或沙箱。这些属于 Agent 侧。

## 特性

- **检索先给判定**：`summary` 说明候选与收敛结果，`distribution` 给出分布与
  `fit`（`strong` / `weak` / `none` / `unknown`），`decision` 记录这次使用的参数。
- **原文可核对**：命中包含原文片段、页码、标题路径与 `chunk_id`；PDF 与图片类文档
  可按页取回原图。
- **参数可调整**：支持 `keep` / `min_score` / `per_doc` 收敛，`offset` / `next_offset`
  翻页，`filters` 先筛后排。
- **异步摄入**：上传、探测、解析、切块、向量化、索引在后台完成；支持续跑与重新解析。
- **双格式存储**：原件与 Markdown 解析产物都保留，可分别读取。
- **解析引擎可选**：本地直提、MinerU、PaddleOCR 按文件动态选择；
  嵌入服务可接 OpenAI 兼容接口（例如硅基流动的 BGE-M3）。

## 快速开始

需要 Docker 24+ 与 Docker Compose v2。

```bash
git clone https://github.com/kkomia/kybase.git
cd kybase
docker compose -f deploy/docker-compose.yml up -d --build
```

启动后：

| 入口 | 地址 |
|---|---|
| 管理台 | http://localhost:8080 |
| REST | http://localhost:8080/api/v1 |
| MCP | http://localhost:8765/mcp |
| 健康检查 | http://localhost:8080/api/v1/health |

首次打开管理台会进入管理员创建向导。之后需要：

1. 在「模型」页登记嵌入模型；
2. 在「API 密钥」页创建 Key；
3. 把 MCP 地址与 Key 填入 Agent 客户端。

完整步骤见 [部署指南](docs/部署指南-v0.1.md)。

## 一次检索

```http
POST /api/v1/search
Authorization: Bearer <API Key>
Content-Type: application/json

{
  "query": "嵌入模型怎么标定？",
  "kb_ids": ["kb_xxx"],
  "keep": 5,
  "min_score": 0.531
}
```

```json
{
  "summary": "候选 38 条、其中 5 条高于基线 0.531，收敛后留 4 条。",
  "distribution": {
    "fit": "strong",
    "count": 38,
    "above_baseline": 5,
    "suggested": { "keep": 4, "min_score": 0.531, "per_doc": null }
  },
  "decision": {
    "keep": 4,
    "min_score": 0.531,
    "per_doc": null,
    "decided_by": "suggested",
    "fit": "strong",
    "dropped": 33,
    "unscored_kept": 0
  },
  "hits": [
    {
      "document_id": "doc_xxx",
      "document_name": "部署手册.pdf",
      "text": "……原文片段……",
      "page": 7,
      "heading_path": "五、嵌入模型与维度",
      "score": 0.0328,
      "similarity": 0.712,
      "basis": "vector"
    }
  ],
  "next_offset": 4
}
```

> 示例数据。`score` 是融合分，只反映名次；`similarity` 是余弦相似度。
> 字段与行为以 [外部接入指南](docs/外部接入指南-v0.1.md) 和 OpenAPI 为准。

## 接入方式

| 方式 | 入口 | 适合 |
|---|---|---|
| MCP | `http://<host>:8765/mcp` | 支持 MCP 的 Agent 客户端 |
| REST | `http://<host>:8080/api/v1` | 任何能发 HTTP 的程序 |
| Skill | `skills/` | 只有 shell 与 HTTP 的环境 |

MCP 工具清单以运行时 `tools/list` 为准；`GET /api/v1/tools` 提供同一份目录与
`surface_fingerprint`。所有业务请求都需要 API Key，没有匿名访问。

## 仓库结构

```
backend/app/
  api/          REST 接口
  services/     摄入、切块、检索、嵌入、解析路由、模型注册
  storage/      PostgreSQL（向量 + 全文 + 元数据）、DuckDB 表格副本、对象存储
  parsers/      本地与云端解析器
  pipeline/     摄入状态机
  workers/      队列消费者
  mcp_server/   MCP 服务（stdio + Streamable HTTP）
frontend/       管理台（Vite + React + TypeScript + Tailwind）
skills/         官方 Skill 包
deploy/         完整栈 compose
docs/           部署与接入文档
site/           GitHub Pages 展示页（纯静态）
```

## 本地开发

后端：

```bash
cd backend
uv sync --all-extras
export KYLAB_DATABASE_URL=postgresql://user:password@localhost:5432/kybase
uv run uvicorn app.main:app --reload
```

前端：

```bash
cd frontend
pnpm install
pnpm dev
```

测试与门禁：

```bash
sh scripts/check-backend.sh
sh scripts/check-frontend.sh
```

Windows 使用同名 `.ps1` 脚本。后端测试需要 `KYLAB_TEST_DATABASE_URL`；
没有数据库时，相关用例会被跳过。

## 文档

- [部署指南](docs/部署指南-v0.1.md)：Docker 部署、配置、初始化、升级、备份与排障
- [外部接入指南](docs/外部接入指南-v0.1.md)：MCP、REST、Skill、检索字段与错误
- [界面设计](docs/界面设计-v0.2.md)：管理台的视觉与交互约定

## 状态

- 版本 `0.1.0`，镜像 `kybase-backend:0.1.0` / `kybase-frontend:0.1.0`。
- 表格副本（DuckDB）为单文件单写者，多副本部署前需要改造。
- 环境变量前缀与 API Key 前缀暂时保留 `KYLAB_` / `kylab_`，用于兼容早期版本。
- 完整栈 compose 建议先在测试环境做一次端到端验证，再用于生产。

## 参与

欢迎提 issue 与 PR。提交信息使用 `<类型>: <简述>`，类型限
`feat` / `fix` / `docs` / `test` / `refactor` / `chore`。

## 许可

[MIT](LICENSE) © 2026 kkomia
