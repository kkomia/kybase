#!/usr/bin/env sh
# 前端门禁（**只改前端时跑这一个**）：eslint + prettier + tsc + vitest + 生产构建 + emoji
#
# 用法：sh scripts/check-frontend.sh
#
# 为什么要有它：全量门禁（ci.sh）会跑后端 1100+ 用例（约 3 分钟）。
# 只改了前端美术却去跑后端，是在为"没动过的代码"付时间——门禁的范围应该等于
# 改动的范围（开发计划 §12.24）。
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

step() {
    label=$1
    shift
    echo "==> $label"
    if ! "$@"; then
        echo "!! $label 失败"
        fail=$((fail + 1))
    fi
}

step "eslint + prettier" pnpm --dir "$ROOT/frontend" lint
step "类型检查（tsc）" pnpm --dir "$ROOT/frontend" typecheck
step "前端单测（vitest）" pnpm --dir "$ROOT/frontend" test
step "生产构建" pnpm --dir "$ROOT/frontend" build
step "emoji 扫描（前端）" "$PY" "$ROOT/scripts/scan_emoji.py" "$ROOT/frontend/src"
# 结构性规范里有一条是**前端规则**（U1：界面文案），所以这个门禁也要跑它——
# 只改前端时跳过它，那条规则就等于没有
step "结构性规范（分层 / 测试位置 / 界面文案）" "$PY" "$ROOT/scripts/check_layering.py" "$ROOT"

if [ "$fail" -ne 0 ]; then
    echo "前端门禁未通过（$fail 项）"
    exit 1
fi
echo "前端门禁全绿"
