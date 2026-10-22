#!/usr/bin/env sh
# 后端门禁（**只改后端时跑这一个**）：ruff + 分层纪律 + emoji + API 类型核对 + pytest
#
# 用法：sh scripts/check-backend.sh
# 范围纪律见 scripts/check-frontend.sh 的说明与开发记录 12.24。
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
# **没有域间引用检查**（原先那一步跑 scripts/check_domains.py）：它守的是"KB 域 ↔ Agent 域
# 不许互引"那张归属表（设计 3.1），而本仓只剩 KB 域——名单里的 调用方模块
# （app.services.chat / skill* / sandbox / app.api.v1.local / app.storage.sqlite_impl …）
# 整族已删，跑起来只是围着不存在的模块报旧账，还会往 docs/计划与记录/ 写一份本仓没有的
# 报告目录。分层引用方向本身由上面那条 check_layering.py 全量守着（L1–L6 覆盖
# api / services / storage / parsers），所以连一个退化的域检查都不留：重复的门禁
# 只会多一处要跟着仓库形态改的地方。
if [ -x "$VENV_PY" ]; then
    # **不生成接口规则**（原先那一步跑 scripts/gen_api_spec.py）：那个脚本的正文
    # 是 kylab 时代的（整段讲技能分类 / 精选），落点 docs/规范/ 本仓也没有，跑一次就是
    # FileNotFoundError。接口事实的权威处是运行中的 /api/v1/openapi.json；真要维护一份
    # kybase 版规范，先改那个脚本的正文与落点再把它接回来。
    #
    # 前端类型与后端 schema 的契约核对：改后端的人最该跑它——schema 动了而前端
    # 那座城市没跟上时，这里会直接红（见 scripts/gen_api_types.py 的说明）
    step "核对 API 类型（前端 ← OpenAPI）" "$VENV_PY" "$ROOT/scripts/gen_api_types.py" --check
else
    echo "==> 核对 API 类型（跳过：找不到 venv 解释器）"
fi
if [ -z "${KYLAB_TEST_DATABASE_URL:-}" ]; then
    echo "!! 未设置 KYLAB_TEST_DATABASE_URL"
    echo "   本服务的存储只有 PostgreSQL，没有它仓储测试与所有走 create_app 的集成测试"
    echo "   都会整体跳过，门禁会变成'绿得没有意义'。请指向一个带 pgvector 的库："
    echo "   KYLAB_TEST_DATABASE_URL=postgresql://用户:口令@主机:5432/postgres \\"
    echo "     sh scripts/check-backend.sh"
    echo "   只想跑测试（允许从 backend/.env 借库作维护连接）：sh scripts/test-backend.sh"
    echo "   只想跑不需要 PG 的那一半（进程内 / 文件系统，marker local）："
    echo "     cd backend && pytest tests -q -m local"
    fail=$((fail + 1))
else
    step "后端测试" sh -c "cd '$ROOT/backend' && '$VENV_PY' -m pytest tests -m 'not bench and not cloud' -q"
fi

if [ "$fail" -ne 0 ]; then
    echo "后端门禁未通过（$fail 项）"
    exit 1
fi
echo "后端门禁全绿"
