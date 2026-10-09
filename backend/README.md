# backend

KYLAB 的后端（Python / FastAPI），**一份业务层、两个入口**：

| 入口 | 是什么 | 什么时候用 |
| --- | --- | --- |
| `app/sidecar.py` | **本机档**：对话与工具循环跑在本机，数据落本机 SQLite | 产品的正规形态，改后端默认改这一条 |
| `app/main.py` | **服务器档**：过渡期形态，只对外保留备份与健康探针 | 只服务既有的旧部署，逐步退役 |

两个入口共用同一份 `services/`（循环、工具、工作区、沙箱、笔记、记忆、技能都是同一批代码），
差别只在组合根怎么装配（`core/services.py`）；工程约束见《[项目工程规范 v0.6](../docs/规范/项目工程规范-v0.6.md)》。

装依赖用 `uv sync --all-extras`——**`--all-extras` 不是可选的**：`app/core/storage.py` 会无条件
构造 DuckDB 表格副本，少了 `tabular` 这个 extra，`import app.main` 直接 `ModuleNotFoundError`。
`parsers`（pypdf / pymupdf / python-docx / openpyxl / pandas）是文档解析主链路要用的，
惰性导入、缺了只是该功能不可用，但运行产品就该装上；`mcp` 只在跑 MCP Server 那个入口时需要。

**本机档**（推荐）：`sh ../scripts/dev-sidecar.sh`——与桌面壳同一个数据目录
（`%APPDATA%\com.kylab.desktop\kylab.db`）；直接 `uv run --all-extras python -m app.sidecar`
则落 `~/.kylab/data/kylab.db`。只监听本机（默认 `127.0.0.1:8765`），`--workspace` / `--data-dir`
可改落点，系统目录一律拒绝。

**服务器档**（改 NAS 侧那份旧单体时才用）：要一个带 pgvector 的 PostgreSQL，
`KYLAB_DATABASE_URL` 必填，表结构由启动时的迁移自动补齐；`uvicorn app.main:app --reload`
之后看 `/api/v1/docs`，存活探针是 `/api/v1/health`。

测试与检查：`uv run pytest tests -m "not bench and not cloud"`（服务器档的用例要
`KYLAB_TEST_DATABASE_URL`，没配会整体跳过——静默变绿比跳过更糟；只要本机档那一半用 `-m local`）、
`uv run ruff check app/ tests/`、`python ../scripts/scan_emoji.py app/`、
`python ../scripts/check_layering.py`。

分层：`api/`（协议适配）→ `services/`（业务，禁止直接写 SQL）→ `storage/`（Repository 抽象 +
`sqlite_impl` 本机 / `postgres_impl` 服务器档），另有 `pipeline/`、`parsers/`（互不引用）、
`workers/`、`mcp_server/`、`models/`、`core/`。三条铁律由 `scripts/check_layering.py` 自动核查：

1. `api/`、`mcp_server/` 禁止写业务逻辑，一律转发 `services/`；
2. `services/` 禁止直接写 SQL，存储访问只能经 `storage/` 的 Repository 接口；
3. `parsers/` 各实现只依赖 `base.py` 的 `ParseResult`，实现之间禁止互相 import。
