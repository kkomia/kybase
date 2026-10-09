#!/usr/bin/env sh
# 启动后端开发服务（FastAPI + 热重载）
# 用法：sh scripts/dev-backend.sh
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT/backend"

PORT=8000

# **端口先探一次**（v0.37）：Windows 上 SO_REUSEADDR 会让第二个实例**绑得上**同一个端口，
# 于是"再启动一次"不会报错，而是变成两个后端同时在跑——连接落到哪一个由系统挑，
# 表现为"我明明改了后端，界面却在报 Method Not Allowed / 找不到路由"（实测踩到，
# 见《开发计划》§12.218）。所以这里先看端口，占了就明确拒绝启动并告诉用户怎么查。
port_in_use() {
    if command -v netstat >/dev/null 2>&1; then
        netstat -an 2>/dev/null | grep -q "[:.]$PORT .*LISTEN"
    elif command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | grep -q ":$PORT "
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
    else
        # 三样都没有（极少见）：跳过这道检查，让 uvicorn 自己去报
        return 1
    fi
}

if port_in_use; then
    echo "端口 $PORT 已经被占用——**先停掉那个进程再启动**，不要开第二个。" >&2
    echo "  查是谁：netstat -ano | grep :$PORT   （Windows 记下 PID 后 taskkill /PID <pid> /T /F）" >&2
    echo "  两个后端同时在跑时，请求落到哪一个由系统挑：界面会报「找不到路由」这类看着像代码坏了的现象。" >&2
    exit 1
fi

# **--all-extras 不是可选的**：裸 `uv sync` 会把 extras（duckdb / 解析器 / office…）卸掉，
# 而后端启动时就 import duckdb。README、Dockerfile 与 CI 用的都是 --all-extras，这里对齐
uv sync --all-extras
exec uv run uvicorn app.main:app --reload --host 127.0.0.1 --port "$PORT"
