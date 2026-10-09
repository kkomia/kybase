#!/usr/bin/env sh
# 代码规范检查（提交前跑一遍）：ruff + emoji 扫描 + 分层纪律 + eslint/prettier
#
# 用法：sh scripts/lint.sh
#
# 跨平台注意：解释器名不能写死。Linux/macOS 是 python3，Windows 只有 python，
# 且 Windows 应用商店的 python3 别名会在 PATH 上"存在但不可执行"，因此逐个探测能否真正运行。
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

if ! PY=$(pick_python); then
    echo "未找到可用的 Python 解释器（可用 PYTHON 环境变量指定）" >&2
    exit 2
fi

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
# 同步《API 接口规范》的端点清单：它是从真实 OpenAPI 生成的，
# 跑这一步之后文档里的清单必然与代码一致（T4.9）
# 这一步要 import app（进而 import duckdb），所以**必须用 venv 的解释器**：
# lint.sh 顶部的 $PY 是通用探测（Windows 上常落到系统 python），
# 那个环境里没有项目依赖。找不到 venv 时跳过而不是报失败——
# 生成文档不该成为门禁里最脆的一环，而 pytest 那条已经会核对文档一致性。
VENV_PY="$ROOT/backend/.venv/Scripts/python.exe"
[ -x "$VENV_PY" ] || VENV_PY="$ROOT/backend/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
    step "同步 API 接口规范" "$VENV_PY" "$ROOT/scripts/gen_api_spec.py"
    # 前端类型与后端 schema 的契约核对（T4.9 的姊妹项）：**不一致就红**，
    # 不像上面那条把文档改掉——类型参与编译，静默重写等于把"契约变了"藏起来。
    step "核对 API 类型（前端 ← OpenAPI）" "$VENV_PY" "$ROOT/scripts/gen_api_types.py" --check
else
    echo "==> 同步 API 接口规范（跳过：找不到 venv 解释器）"
    echo "==> 核对 API 类型（跳过：找不到 venv 解释器）"
fi

if [ -f "$ROOT/frontend/package.json" ]; then
    step "eslint + prettier" pnpm --dir "$ROOT/frontend" lint
    step "类型检查（tsc）" pnpm --dir "$ROOT/frontend" typecheck
    step "emoji 扫描（前端）" "$PY" "$ROOT/scripts/scan_emoji.py" "$ROOT/frontend/src"
fi

# Shell 脚本语法自检（与 CI 的「门禁脚本自检」job 同一件事）：
# **按 git 跟踪的文件扫，不手写清单**——手写清单会漏，`deploy/nas/` 那两个脚本
# （其中 `deploy-from-windows.sh` 是用户照着跑的那条路）就这么漏过一轮。
# 只在 git 检出里做；不在检出里（比如解开的源码包）就跳过，别让门禁凭空变脆。
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "==> Shell 脚本语法（sh -n，git 跟踪的全部 .sh）"
    # 子 shell 里 cd，别把 ROOT 之后要用的相对路径搅了；管道取"最后一条命令"的退出码，
    # 所以循环里 exit 1 能让整条失败
    if (cd "$ROOT" && git ls-files '*.sh' | while IFS= read -r f; do
            echo "   sh -n $f"
            sh -n "$f" || { echo "!! $f 语法不过"; exit 1; }
        done); then
        :
    else
        echo "!! Shell 脚本语法自检失败"
        fail=$((fail + 1))
    fi
else
    echo "==> Shell 脚本语法（跳过：不在 git 检出里）"
fi

if [ "$fail" -ne 0 ]; then
    echo "共 $fail 项检查失败"
    exit 1
fi
echo "全部检查通过"
