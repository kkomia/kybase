#!/usr/bin/env sh
# 启动前端开发服务（Vite）
# 用法：sh scripts/dev-frontend.sh
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT/frontend"

[ -d node_modules ] || pnpm install
exec pnpm dev
