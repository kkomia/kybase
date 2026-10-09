# 后端镜像（M7 / T7.1）。
#
# 两阶段构建：构建期需要编译工具链（部分依赖带原生扩展），运行期不需要——
# 把它们留在运行镜像里会让攻击面与体积都白白变大。
#
# **构建上下文是源码树的根，不是 backend/**（v0.1.1）：镜像里要装仓库自带的
# ``skills/``（见下面那条 COPY），而 COPY 只能从上下文里取文件。所以两份
# compose 里 backend.build 的 context 都指到"包含 backend/ 的那一层"
# （开发机上就是仓库根，NAS 上是解开的 ``src/``），dockerfile 指到
# ``backend/Dockerfile``；仓库根那份 ``.dockerignore`` 挡住
# .venv / node_modules / .git 这三大块（不挡的话每次构建白传几百 MB）。
# 没有用 compose 的 ``additional_contexts``：那要 BuildKit 加较新的 compose，
# 而部署机（NAS）上的版本我没法在本地验证；普通 context 到处都能用。
FROM python:3.12-slim AS builder

# uv 装依赖比 pip 快一个数量级，也是本项目开发期用的工具，口径一致
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# **索引源可覆盖**（v0.41）：默认仍是官方 PyPI（行为与以前逐字一致），
# 但国内机器上从 files.pythonhosted.org 拉 wheel 可能慢到不可用——
# 实测某次构建 `uv sync` 跑满 700 秒仍卡在下载里。构建时给
# `--build-arg UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple` 即可换源
# （清华源在并发下载时会对 cli 返 403，实测；别照抄它）。
# 注意它只影响**构建期**的依赖下载，与运行期无关。
ARG UV_INDEX_URL=https://pypi.org/simple
ENV UV_INDEX_URL=${UV_INDEX_URL}

# 先只拷依赖清单再装：源码改动不会让依赖层失效，重复构建能命中缓存
#
# **`--all-extras` 是必需的，不是可选的**：`app/core/storage.py` 无条件构造 DuckDB
# 表格副本，少了 `tabular` 这个 extra，容器连 `import app.main` 都过不去（启动即崩）；
# 解析层（pypdf/pymupdf/python-docx/openpyxl）是上传→入库的主链路，镜像里也必须有。
# **README.md 也必须拷进来**：`pyproject.toml` 里写着 `readme = "README.md"`，
# 而 hatchling 在"装项目自身"那一步要读它——没有它整步崩在
# `OSError: Readme file does not exist: README.md`（2026-09-21 在 NAS 上首次实测到）。
#
# 路径带 ``backend/`` 前缀是因为上下文是仓库根（见文件头）。
COPY backend/pyproject.toml backend/uv.lock* backend/README.md ./
RUN uv sync --no-install-project --no-dev --all-extras

COPY backend/app ./app
RUN uv sync --no-dev --all-extras


FROM python:3.12-slim AS runtime

# **不设 PYTHONUNBUFFERED 之外的优化**：日志要实时出来，
# 否则容器里排查问题只能看到缓冲后的残片
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# 非 root 运行。容器里以 root 跑一个 Web 服务没有任何好处，
# 而一旦被拿下就是宿主级权限
RUN useradd --create-home --uid 10001 kylab

WORKDIR /app
COPY --from=builder --chown=kylab:kylab /app /app

# 仓库自带的技能（``skills/kylab-*`` 五个）放进镜像：没有它，容器里的
# 「能力 → 技能」一个预装技能都看不到（§12.224 第 9 条）。
#
# **落点是 /app/skills，不是"按代码位置推出来"的 /skills**：原先的发现逻辑只按
# ``Path(__file__).resolve().parents[3] / "skills"`` 找，容器里代码在
# /app/app/services，往上四层是 `/`——那里没有 skills。现在技能目录优先读
# KYLAB_SKILLS_DIR（下面那条 ENV 钉死它），推断那条只留给开发布局。
# 顺带：放在 /app 下才吃得到下面那条 ``chmod -R a+rX /app``——COPY 会把源文件的
# mode 原样带进来，而这台 NAS 的 /vol1 把文件报成 000（§12.223 第 4 条），
# 落在 chmod 之后就没有人再给它补可读位了。所以**这两行的顺序不能换**。
COPY --chown=kylab:kylab skills ./skills
ENV KYLAB_SKILLS_DIR=/app/skills

# **代码在镜像里必须对任何 uid 可读**（2026-09-21 NAS 上实测踩到）：
# COPY 会把源文件的 mode 原样带进来，而某些文件系统（这台 NAS 的 /vol1 就是）
# 把文件报成 000——构建阶段以 root 跑所以看不出来，一旦运行期换了 uid
# （`user:` 覆盖成宿主用户），读源码就是 `PermissionError: [Errno 13]`。
# 这条与"以哪个 uid 运行"解耦：镜像内部的可读性不该依赖源文件系统。
RUN chmod -R a+rX /app

# 数据目录（SQLite + 向量分区 + 对象存储）挂在这里，见 compose 的卷
RUN mkdir -p /data && chown kylab:kylab /data
VOLUME ["/data"]

USER kylab
EXPOSE 8000

# 健康检查打 /api/v1/health：它不鉴权（探针不该带凭据），
# 且会真的探一下存储是否可用
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
