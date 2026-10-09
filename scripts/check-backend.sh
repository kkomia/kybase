#!/usr/bin/env sh
# 后端门禁（**只改后端时跑这一个**）：ruff + 分层纪律 + emoji + API 文档同步 + pytest
#
# 用法：sh scripts/check-backend.sh
# 范围纪律见 scripts/check-frontend.sh 的说明与开发计划 §12.24。
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
fail=0

pick_python() {
    for candidate in "${PYTHON:-}" python3 python py; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

PY=$(pick_python || true)
[ -n "$PY" ] || { echo "未找到可用的 Python 解释器" >&2; exit 2; }

VENV_PY="$ROOT/backend/.venv/Scripts/python.exe"
[ -x "$VENV_PY" ] || VENV_PY="$ROOT/backend/.venv/bin/python"

step() {
    label=$1
    shift
    echo "==> $label"
    if ! "$@"; then
        echo "!! $label 失败"
        fail=$((fail + 1))
    fi
}

step "ruff" sh -c "cd '$ROOT/backend' && uv run ruff check app/ tests/"
step "emoji 扫描（后端）" "$PY" "$ROOT/scripts/scan_emoji.py" "$ROOT/backend/app"
step "结构性规范（分层 / 测试位置 / 界面文案 / 版本号）" "$PY" "$ROOT/scripts/check_layering.py" "$ROOT"
if [ -x "$VENV_PY" ]; then
    step "同步 API 接口规范" "$VENV_PY" "$ROOT/scripts/gen_api_spec.py"
    # 前端类型与后端 schema 的契约核对：改后端的人最该跑它——schema 动了而前端
    # 那座城市没跟上时，这里会直接红（见 scripts/gen_api_types.py 的说明）
    step "核对 API 类型（前端 ← OpenAPI）" "$VENV_PY" "$ROOT/scripts/gen_api_types.py" --check
else
    echo "==> 同步 API 接口规范（跳过：找不到 venv 解释器）"
    echo "==> 核对 API 类型（跳过：找不到 venv 解释器）"
fi
if [ -z "${KYLAB_TEST_DATABASE_URL:-}" ]; then
    echo "!! 未设置 KYLAB_TEST_DATABASE_URL"
    echo "   服务器档的存储是 PostgreSQL（M2 起本机档另有 SQLite，见下），没有它"
    echo "   仓储测试与所有走 create_app 的集成测试都会整体跳过，"
    echo "   门禁会变成'绿得没有意义'。请指向一个带 pgvector 的库："
    echo "   KYLAB_TEST_DATABASE_URL=postgresql://用户:口令@主机:5432/postgres \\"
    echo "     sh scripts/check-backend.sh"
    echo "   只想跑测试（允许从 backend/.env 借库作维护连接）：sh scripts/test-backend.sh"
    echo "   只想跑**本机档**那一半（不需要 PG）：cd backend && pytest tests -q -m local"
    fail=$((fail + 1))
else
    step "后端测试" sh -c "cd '$ROOT/backend' && '$VENV_PY' -m pytest tests -m 'not bench and not cloud' -q"
fi

if [ "$fail" -ne 0 ]; then
    echo "后端门禁未通过（$fail 项）"
    exit 1
fi
echo "后端门禁全绿"
