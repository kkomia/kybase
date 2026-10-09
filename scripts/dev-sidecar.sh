#!/usr/bin/env sh
# 启动本机边车（桌面壳拉起的那个后端：对话 + 本机数据）
# 用法：sh scripts/dev-sidecar.sh
#
# **它是什么**：本机档那个后端，与桌面壳拉起的是**同一个入口**（`python -m app.sidecar`，
# 参数口径见 desktop/src-tauri/src/sidecar.rs::arguments）。
#
# **为什么在浏览器里迭代 web 端要用它**：对话那一轮（`POST /turn/stream`，以及审批、会话、
# 笔记、设置、记忆、工作区这些本机权威面 `/api/v1/local/*`）都挂在它身上；而 `app.main:app`
# （scripts/dev-backend.sh 那一份）**不挂 `/turn/*`** —— 只起那一份时对话一律报
# 「这一轮没跑起来：网络没连上（这条请求没有发出去）」（2026-10-05 实测真因）。
#
# **数据落在哪**：`--data-dir` 默认就是壳的真实数据目录（Tauri `app_data_dir()` 口径：
# Windows `%APPDATA%\com.kylab.desktop`、macOS `~/Library/Application Support/com.kylab.desktop`、
# Linux `~/.local/share/com.kylab.desktop`），工作区取 `<数据目录>/workspace` —— 与壳一致，
# 于是同一个库、同一批会话、同一份设置。要一份隔离开的数据（不碰壳里的真实数据）就设
# `KYLAB_DATA_DIR`。
#
# **它现在与壳同一个口径（地址与钥匙）**：启动时读壳的 `config.json`（`server` + `api_key`）
# → 给边车传 `--server {server}/api/v1` / `--token {api_key}`，于是这一档的知识库提供者
# （`GET /api/v1/local/provider`）与备份上传都拿得到地址与凭据 —— 与壳拉起它时传的
# 是同两项（`desktop/src-tauri/src/main.rs:434` 起那个口径）。钥匙串优先、回落
# `config.json`：`api_key` 被壳收进系统钥匙串的那些机器（M5 阶段 6B 之后是常态），
# 从 `kylab:nas_token:<地址>` 读同一把（壳的 `Shell::api_key_for` 就是这条链）。
# 两个环境变量可显式覆盖：`KYLAB_SERVER` / `KYLAB_TOKEN`（排障、连别的 NAS 用，
# **改了地址就一并给钥匙** —— 钥匙串是按地址归档的）。
# **密钥纪律**：钥匙只在变量与 argv 里活着 —— 不 echo、不进日志、不进任何提示
# （argv 同机器可见，见 `desktop/src-tauri/src/sidecar.rs` 的头三条纪律）。
#
# **与 dev-backend.sh 的关系**：两个进程可并存，同一份 SQLite 库（定时任务的认领是一次
# CAS，只会跑一遍，见 backend/app/sidecar.py::_lifespan）。`/api/**` 那条"服务器面"的请求
# 在浏览器里走 Vite 反代，**默认**仍打到 :8000（那是"服务器档"的开发后端，自己带知识库）；
# 要接 NAS 就设 `KYLAB_API_TARGET=http://192.168.31.18:8081` —— 那正是桌面壳的形态（壳把
# `/api/**` 转发到 NAS、`Authorization` 用页面自己的），出处与理由见 frontend/vite.config.ts
# 的那段注释。不设它时那些请求按各页自己的降级显示。
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)

PORT=${KYLAB_SIDECAR_PORT:-8765}

# 数据目录按上面三档取（与 Tauri 的 app_data_dir() 同一个口径）
if [ -n "${KYLAB_DATA_DIR:-}" ]; then
    DATA_DIR=$KYLAB_DATA_DIR
else
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*)
            # %APPDATA% 是 Windows 形式（C:\...）：换成 C:/... 这种两边都认的写法
            DATA_DIR=$(printf '%s' "${APPDATA:-$HOME/AppData/Roaming}" | tr '\\' '/')/com.kylab.desktop
            ;;
        Darwin)
            DATA_DIR="$HOME/Library/Application Support/com.kylab.desktop"
            ;;
        *)
            DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/com.kylab.desktop"
            ;;
    esac
fi

# 目录不存在就建（与壳一致：工作区在数据目录下面）
mkdir -p "$DATA_DIR/workspace"
# 统一成绝对路径：相对路径按**你调用脚本时所在的位置**解析（随后会 cd 到 backend/）
DATA_DIR=$(cd "$DATA_DIR" && pwd)
WORKSPACE=$DATA_DIR/workspace

# **端口先探一次**（照 dev-backend.sh）：Windows 上 SO_REUSEADDR 会让第二个实例**绑得上**
# 同一个端口，于是"再启动一次"不会报错，而是变成两个边车同时在跑——连接落到哪一个由系统挑，
# 表现为"我明明改了后端，界面却还是旧行为"。所以这里先看端口，占了就明确拒绝启动。
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
    echo "  8765 上往往是桌面壳拉起的边车：调试时可以停掉它，或用 KYLAB_SIDECAR_PORT 换一个端口。" >&2
    exit 1
fi

cd "$ROOT/backend"

# ---------------------------------------------------------------- 地址与钥匙
# **与壳同一个口径**：壳起边车时传的就是 `--server {server}/api/v1` + `--token {壳里那把钥匙}`
# （desktop/src-tauri/src/main.rs:434 起）。三个来源按下面的顺序取，**明文不进任何输出**：
#   地址：KYLAB_SERVER → 壳的 config.json 的 server
#   钥匙：KYLAB_TOKEN → 系统钥匙串 kylab:nas_token:<地址> → config.json 的 api_key
# （钥匙串排在明文前面是照壳的 `Shell::api_key_for`：钥匙串优先、回落 config.json ——
#  M5 阶段 6B 把明文收进钥匙串之后，config.json 里那一栏只是"老配置还读得动"。）
CONFIG=$DATA_DIR/config.json

config_value() {
    # $1 = config.json 里的键名；文件没有 / JSON 读不动 → 什么都不打印
    [ -f "$CONFIG" ] || return 0
    uv run --all-extras python -c 'import json,sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
value = data.get(sys.argv[2])
print(value.strip() if isinstance(value, str) else "")' "$CONFIG" "$1" 2>/dev/null || true
}

keychain_value() {
    # $1 = 地址；成功打印钥匙，没配 / 钥匙串不可用 → 什么都不打印。
    # 失败时那个 CLI 会往 **stdout** 打一句人话，所以这里按退出码决定要不要它。
    if ! _key=$(uv run --all-extras python -m app.services.credentials nas-token \
            --data-dir "$DATA_DIR" --origin "$1" --show 2>/dev/null); then
        _key=""
    fi
    printf '%s' "$_key"
}

SERVER=${KYLAB_SERVER:-}
SERVER_SOURCE="KYLAB_SERVER"
if [ -z "$SERVER" ]; then
    SERVER=$(config_value server)
    SERVER_SOURCE="壳的 config.json"
fi

TOKEN=${KYLAB_TOKEN:-}
TOKEN_SOURCE="KYLAB_TOKEN"
if [ -z "$TOKEN" ] && [ -n "$SERVER" ]; then
    TOKEN=$(keychain_value "$SERVER")
    TOKEN_SOURCE="系统钥匙串"
fi
# **没有地址就一把钥匙都不给**：不给 `--server` 时边车会用自己那档默认地址
# （`KYLAB_SERVER_URL` / :8000），把 NAS 的钥匙送到那儿去是"钥匙送错门"
if [ -z "$TOKEN" ] && [ -n "$SERVER" ]; then
    TOKEN=$(config_value api_key)
    TOKEN_SOURCE="壳的 config.json"
fi

# `--server` 要的是**含 /api/v1** 的基址（知识库提供者拿它拼 `/provider/handshake`）；
# 末尾斜杠先去掉，已经是 /api/v1 结尾的就不再拼一次
SERVER=$(printf '%s' "$SERVER" | sed 's:/*$::')
case "$SERVER" in
    */api/v1) ;;
    "") ;;
    *) SERVER=$SERVER/api/v1 ;;
esac

if [ -n "$SERVER" ]; then
    if [ -n "$TOKEN" ]; then
        echo "边车：接上 $SERVER（地址来自 $SERVER_SOURCE，钥匙来自 $TOKEN_SOURCE，不回显）" >&2
    else
        echo "边车：接上 $SERVER（地址来自 $SERVER_SOURCE），但没有钥匙——知识库会如实回「凭据缺失」；" >&2
        echo "      在壳里对那台 NAS 登一次，或给 KYLAB_TOKEN。" >&2
    fi
else
    echo "边车：没找到壳的 config.json（或里面没有 server）—— 这一档不带 NAS 地址与钥匙（知识库与备份上传按各页自己的降级显示）" >&2
fi

# **--all-extras 不是可选的**：裸 `uv sync` 会把 extras（duckdb / 解析器 / office 这些）卸掉，
# 而边车启动时就 import duckdb（backend/Dockerfile 与 CI 用的都是 --all-extras，同一条理由）；
# `uv run` 自己会同步环境，所以不需要单独再 sync 一次。
set --
if [ -n "$SERVER" ]; then set -- "$@" --server "$SERVER"; fi
if [ -n "$TOKEN" ]; then set -- "$@" --token "$TOKEN"; fi
exec uv run --all-extras python -m app.sidecar --port "$PORT" --workspace "$WORKSPACE" --data-dir "$DATA_DIR" "$@"
