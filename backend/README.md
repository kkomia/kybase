# backend

kybase 的后端：Python / FastAPI + MCP Server。入口是 `app/main.py`。

## 依赖

```bash
uv sync --all-extras
```

`--all-extras` 会安装表格副本、Office 解析与 MCP 运行所需的依赖。
只装默认依赖时，部分功能会在 import 或调用时报错。

## 本地运行

需要 PostgreSQL（安装 `vector` 扩展）与可选的 S3 兼容对象存储。

```bash
export KYLAB_DATABASE_URL=postgresql://user:password@localhost:5432/kybase
uv run uvicorn app.main:app --reload
```

引导级变量见 `.env.example`；模型地址、密钥与维度在管理台「模型」页登记。
数据库 schema 与迁移由应用启动时处理，不需要手工执行 SQL。

- REST 文档：`/api/v1/docs`
- OpenAPI：`/api/v1/openapi.json`
- 健康检查：`/api/v1/health`

MCP：

```bash
uv run python -m app.mcp_server.server --transport http --port 8765
uv run python -m app.mcp_server.server --list-tools
```

客户端的完整配置见 [外部接入指南](../docs/外部接入指南-v0.1.md)。

## 测试

```bash
uv run pytest tests -m "not bench and not cloud"   # 需要 KYLAB_TEST_DATABASE_URL
uv run ruff check app/ tests/
python ../scripts/check_layering.py
```

没有测试数据库时，仓储与部分集成测试会被跳过。只跑不依赖 PG 的用例：

```bash
uv run pytest tests -m local
```

## 分层

`api/` 与 `mcp_server/` 只做协议适配；`services/` 放业务逻辑，禁止直接写 SQL；
`storage/` 提供 Repository 抽象与 PostgreSQL / S3 / DuckDB / 本地实现；
`parsers/` 的实现只依赖 `base.py` 的 `ParseResult`，解析器之间互不引用。
`scripts/check_layering.py` 会机械检查这些边界。

## 容器

构建与编排见仓库根 `deploy/docker-compose.yml`。
