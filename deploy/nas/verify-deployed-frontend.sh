#!/usr/bin/env sh
# **部署后核对（机器能判的那几条）**——README 里那张 5 条清单的第 1、2 条全自动，
# 外加一条"线上跑的就是本机验过的那份"。
#
# 用法：sh deploy/nas/verify-deployed-frontend.sh [主机，默认 192.168.31.18] [端口，默认 8081]
#
# 判据（逐条打印，任何一条不过就退出 1）：
#   1. 首页 HTTP 200；
#   2. HTML 里的挂载点是 `id="root"`（本仓管理台）而不是 `id="app"`（别的应用）；
#   3. `/api/v1/health` 返回 `"status":"ok"`；
#   4. index.html 里引用的每个 `/assets/...` 都 200（防"dist 传了一半"）；
#   5. **产物同一性**：本机 `frontend/dist/index.html` 里那个带 hash 的入口文件名，
#      在线上 index.html 里也出现——证明"线上跑的就是本机验过的那一份"。
#      （本机没有 `frontend/dist` 时这条跳过并说明，不算失败。）
#
# 人眼那三条（登录 / 翻一页文档 / 搜一次看出处）它替不了——
# 那三条在 `deploy/nas/README.md` 的「部署后核对与回滚」里。
set -eu

HOST="${1:-192.168.31.18}"
PORT="${2:-8081}"
BASE="http://$HOST:$PORT"
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LOCAL_INDEX="$ROOT/frontend/dist/index.html"

fail=0
ok() { echo "  OK   $1"; }
bad() { echo "  !!   $1"; fail=$((fail + 1)); }

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

echo "== 1/5 首页"
code=$(curl -s -o "$TMP" -w '%{http_code}' --max-time 15 "$BASE/" || true)
[ "$code" = "200" ] && ok "首页 HTTP 200" || bad "首页 HTTP $code（看 NAS 上 docker compose logs -f frontend）"

echo "== 2/5 挂载点"
if grep -q 'id="root"' "$TMP"; then
  ok 'HTML 里有 id="root"（kybase 管理台）'
elif grep -q 'id="app"' "$TMP"; then
  bad 'HTML 里是 id="app" —— 这不是本仓的管理台（端口上跑着别的应用？）'
else
  bad 'HTML 里既没有 id="root" 也没有 id="app" —— 这个页面不是我们的应用？'
fi

echo "== 3/5 后端健康"
health=$(curl -s --max-time 15 "$BASE/api/v1/health" || true)
case "$health" in
  *'"status":"ok"'*) ok "health：$health" ;;
  *) bad "health 不是 ok：$(printf '%s' "$health" | head -c 120)" ;;
esac

echo "== 4/5 静态资源"
# 从线上 index.html 里抠出 /assets/... 的 js/css，逐个看是不是 200
assets=$(grep -o '/assets/[A-Za-z0-9._-]*\.\(js\|css\)' "$TMP" | sort -u || true)
count=0
for a in $assets; do
  count=$((count + 1))
  acode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$a" || true)
  [ "$acode" = "200" ] || bad "资源 $a → HTTP $acode"
done
[ "$count" -gt 0 ] && ok "$count 个 /assets 资源全 200" || bad "index.html 里没找到 /assets 资源（不太对劲）"

echo "== 5/5 产物同一性（线上 vs 本机构建）"
if [ -f "$LOCAL_INDEX" ]; then
  entry=$(grep -o '/assets/index-[A-Za-z0-9._-]*\.js' "$LOCAL_INDEX" | head -1 || true)
  if [ -z "$entry" ]; then
    bad "本机 dist/index.html 里没找到入口文件名（先 pnpm build）"
  elif grep -qF "$entry" "$TMP"; then
    ok "线上入口就是本机那份：$entry"
  else
    bad "线上入口与本机不一致（本机 $entry）——线上不是这份构建，或构建产物变了"
  fi
else
  echo "  --   跳过：本机没有 frontend/dist（跑一次 pnpm --dir frontend build 就能比）"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "机器能判的四条全过。剩下三条人眼：登录一次 / 翻一页文档 / 搜一次看出处与分块。"
else
  echo "有 $fail 条不过。"
  exit 1
fi
