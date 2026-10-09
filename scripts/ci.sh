#!/usr/bin/env sh
# CI 门禁（本地复现 CI 用）：规范检查 + 后端测试 + 前端测试
#
# 用法：sh scripts/ci.sh
#
# 跨平台注意：解释器名不写死（Windows 没有 python3），见 lint.sh 中的探测逻辑。
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
fail=0

step() {
    label=$1
    shift
    echo "==> $label"
    if ! "$@"; then
        echo "!! $label 失败"
        fail=$((fail + 1))
    fi
}

step "规范检查" sh "$ROOT/scripts/lint.sh"

step "后端测试" sh -c "cd '$ROOT/backend' && uv run pytest tests/ -m 'not bench and not cloud' --cov=app --cov-report=term-missing"

if [ -f "$ROOT/frontend/package.json" ]; then
    step "前端测试" pnpm --dir "$ROOT/frontend" test
    step "前端生产构建" pnpm --dir "$ROOT/frontend" build
fi

if [ "$fail" -ne 0 ]; then
    echo "CI 门禁未通过（$fail 项）"
    exit 1
fi
echo "CI 门禁全绿"
