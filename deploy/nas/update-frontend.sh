#!/usr/bin/env sh
# NAS 上更新前端（kybase 管理台）。
#
# 用法（在 NAS 上）：sh /vol1/1000/docker/kybase/src/deploy/nas/update-frontend.sh [分支]
#   默认分支 master。这个脚本**只重建 frontend 一个服务**；后端变了要走整棵重建
#   （见 deploy/nas/README.md 的「后端也变了的时候」）。
#
# 两处**这台机器特有的现实**（都踩过或核过，不是通用假设）：
#
# 1. **`src/` 是 `git archive` 解开的，不含 `.git`**（见 deploy/nas/README.md 的目录布局）
#    ——所以"先 git pull 再构建"在 NAS 上会直接失败。这里的做法是：**只看
#    `src/frontend` 是不是 kybase 的管理台**（判据是页面清单 `src/app/routes.ts`），
#    是就继续构建；顺带发现 `.git` 在才做 fetch/pull（那是加分项，不是前提）。
# 2. 这台机器到境外带宽极差（kylab 时代实测：`uv sync` 700 秒仍在下载）——所以
#    compose 里给前端构建传了 `NPM_REGISTRY=registry.npmmirror.com`（见 deploy/nas/docker-compose.yml）。
set -eu

BRANCH="${1:-master}"
SRC="/vol1/1000/docker/kybase/src"
APP="/vol1/1000/docker/kybase/app"
WEB="$SRC/frontend"

echo "== 0/4 源码形态"
if [ -f "$WEB/src/app/routes.ts" ] && [ -f "$WEB/package.json" ]; then
  echo "   src/frontend 是 kybase 的管理台（有 src/app/routes.ts）"
else
  echo "!! src/frontend 里没有 src/app/routes.ts（或没有 package.json）——这不是本仓的管理台。"
  echo "   先把源码换成 kybase 的源码树，两选一（在**别的机器**上做第 1 条最省事）："
  echo "     a) 仓库克隆处：git archive --format=tar $BRANCH | ssh yumao@<NAS> 'tar -x -C $SRC --overwrite'"
  echo "     b) 只搬前端：把 kybase 的 frontend/ 整目录覆盖到 $WEB（构建只需要这一棵）"
  echo "   注意 \`tar --overwrite\` **只覆盖、不删**：换代码时照 README 那节做「干净重解」，"
  echo "   否则上一版留下的文件会被一起构建进去"
  exit 2
fi

if [ ! -f "$APP/docker-compose.yml" ]; then
  echo "!! 找不到应用层的 compose（$APP/docker-compose.yml）——路径与这台机器对不上，先确认部署位置"
  exit 2
fi

if [ -d "$SRC/.git" ]; then
  echo "== 1/4 源码是 git 检出：切 $BRANCH 并拉取"
  cd "$SRC"
  git fetch --all --prune || echo "   （fetch 失败，继续用当前源码构建）"
  git checkout "$BRANCH" || echo "   （checkout 失败，继续用当前源码构建）"
  git pull --ff-only || echo "   （pull 失败，继续用当前源码构建）"
  git --no-pager log --oneline -1 || true
else
  echo "== 1/4 源码不是 git 检出（archive 解开的）：跳过拉取，直接用当前源码构建"
fi

echo "== 2/4 只重建前端镜像（后端不重建）"
cd "$APP"
# docker 权限自查：这台 NAS 上 docker 从普通用户就能用（README 里的命令都没有 sudo），
# 但换一台机器/换一个用户时最常见的坑就是 "permission denied ... docker.sock"——
# 与其让人对着这个报错猜，不如当场给出该敲的那一行。
if ! docker info >/dev/null 2>&1; then
  echo "!! 当前用户用不了 docker（多半是 /var/run/docker.sock 权限）。用 sudo 重跑这条就行："
  echo "     sudo sh $0 $BRANCH"
  exit 3
fi
docker compose build frontend

echo "== 3/4 重启前端容器"
docker compose up -d frontend

# 等首页真的能出：**最多 45 秒**（按真实时间算，不是按轮次——Windows 上被拒的连接
# 一次要 ~3.5 秒，按轮次算会把 45 轮拖成两三分钟）。容器刚被替换的那几百毫秒里 curl
# 会被拒，那不是"部署失败"——只 curl 一次的话，会让人以为没成、白白再跑一遍（这台机器的盘还慢）。
start=$(date +%s)
code=000
while [ "$(( $(date +%s) - start ))" -lt 45 ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8081/ || true)
  [ "$code" = "200" ] && break
  sleep 1
done
elapsed=$(( $(date +%s) - start ))

echo "== 4/4 核对"
echo "   首页 HTTP：$code（等了 ${elapsed} 秒）"
[ "$code" = "200" ] || { echo "!! 首页不是 200；看 docker compose logs -f frontend"; exit 1; }
if curl -s http://127.0.0.1:8081/ | grep -q 'id="root"'; then
  echo "   已确认是 kybase 管理台（挂载点 #root）"
else
  echo "!! 页面里没有 #root —— 确认源码确实换了、镜像确实重建了"
  exit 1
fi
echo "完成。"
