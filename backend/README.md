# backend

KYLAB 知识库服务的后端（Python / FastAPI + MCP Server），**只有一个入口**：`app/main.py`。

v0.1.0 瘦身之后这里只有知识库服务本体——原先那个"本机档"（桌面壳的边车）与它带来的
会话 / 笔记 / 记忆 / 技能 / 插件 / 沙箱 / 工作区 / 定时任务 / 备份 / 导出交付一整族
都拆掉了，连同它们的存储形态（SQLite / 双档机制 / `sqlite_impl` / `split_impl`）。
留下来的四块：**KB 族 api + services + parsers + pipeline + queue_worker + mcp_server +
PG/DuckDB/对象存储 + 账号鉴权 + 模型注册**。

装依赖用 `uv sync --all-extras`——**`--all-extras` 不是可选的**：`app/core/storage.py`
会无条件构造 DuckDB 表格副本，少了 `tabular` 这个 extra，`import app.main` 直接
`ModuleNotFoundError`。`parsers`（pypdf / pymupdf / python-docx / openpyxl / pandas）
是文档解析主链路要用的，惰性导入、缺了只是该功能不可用，但运行产品就该装上；
`office` 与 `mcp` 分别只在跑导出与 MCP Server 时需要。

## 跑起来

要一个带 pgvector 的 PostgreSQL，`KYLAB_DATABASE_URL` **必填**（表结构由启动时的迁移
自动补齐）；对象存储留空则原件落 `data_dir/{originals,markdown,images}`。引导级配置的
完整清单与说明见 `.env.example`（模型地址 / 密钥不在这里——那是「设置 → 模型注册」的事）。

```sh
uv run uvicorn app.main:app --reload        # 开发（scripts/dev-backend.sh 会先探端口）
```

- 交互文档 `/api/v1/docs`，OpenAPI 事实源 `/api/v1/openapi.json`，存活探针 `/api/v1/health`；
- MCP 面：`uv run python -m app.mcp_server.server --transport http --port 8765`
  （`--list-tools` 只打印工具清单）；客户端接入见 `../docs/MCP接入-Codex-v0.1.md`；
- 容器形态见 `Dockerfile` 与 `../deploy/docker-compose.yml`。

## 测试与检查

```sh
uv run pytest tests -m "not bench and not cloud"     # 需要 KYLAB_TEST_DATABASE_URL
uv run ruff check app/ tests/
python ../scripts/scan_emoji.py app/
python ../scripts/check_layering.py
```

没有 `KYLAB_TEST_DATABASE_URL` 时仓储测试与所有走 `create_app` 的集成测试会**整体跳过**
——静默变绿比跳过更糟，所以 `scripts/check-backend.sh` 会把它判成红。只想跑不需要 PG 的
那一半：`uv run pytest tests -m local`。

## 分层

`api/`（协议适配，含 `api/v1/` 与 `mcp_server/`）→ `services/`（业务，禁止直接写 SQL）→
`storage/`（Repository 抽象 + `postgres_impl` / `s3_impl` / `local_impl` / `duckdb_impl`），
另有 `pipeline/`、`parsers/`（互不引用）、`workers/`、`models/`、`core/`。
铁律由 `scripts/check_layering.py` 机械核查（L1–L6）：

1. `api/`、`mcp_server/` 禁止写业务逻辑，一律转发 `services/`；
2. `services/` 禁止直接写 SQL，存储访问只能经 `app.storage.base` 的 Repository 接口；
3. `parsers/` 各实现只依赖 `base.py` 的 `ParseResult`，实现之间禁止互相 import，
   且不得反向依赖 `services/` / `api/`。
