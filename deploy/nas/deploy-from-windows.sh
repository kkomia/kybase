#!/usr/bin/env sh
# **从这台 Windows 直接部署到 NAS**（在 Git Bash 里跑；密码只在你的终端里输，不落任何地方）。
#
# **这条路是旧的 archive 推源码，常规升级不要用它。** NAS 的 `src/` 现在是
# `git@gitee.com:kkomia/kybase.git` 的 git 检出（只读部署公钥 `~/.ssh/kybase-gitee-deploy`，
# Gitee 侧名叫 `kybase-nas-deploy`），标准流程是「本仓 push → NAS `cd src && git pull`
# → `cd ../app && docker compose up -d --build`」（见 deploy/nas/README.md 的「升级」与
# 「NAS 上的 git 检出」）。这个脚本保留给两种情形：① 要钉到某个具体提交（回滚）；
# ② NAS 拉不到 Gitee。**它对 git 检出是"外人插手"**：`tar --overwrite` 覆盖工作区会让它
# 偏离 HEAD，之后 NAS 上 `git pull --ff-only` 会拒绝执行——用完记得
# `cd /vol1/1000/docker/kybase/src && git checkout -- .`（或 `git reset --hard <提交>`）。
#
# 它做的是"只换前端"那条路：把 **kybase 的源码树**整份推到 NAS 的 `src/`，
# 再在那边只重建 frontend 一个服务。**后端变了不要用这个脚本**——它不重建后端，
# 会得到"页面是新的、后端还是老的"（见 deploy/nas/README.md 那一节）。
#
# 用法：sh deploy/nas/deploy-from-windows.sh [分支，默认 master] [主机，默认 192.168.31.18] [SSH 用户，默认 yumao]
#      sh deploy/nas/deploy-from-windows.sh --dry-run   # 只打印"将要做什么"，不碰网络
#      NAS_USER=别的账号 sh deploy/nas/deploy-from-windows.sh    # 环境变量也行
#
# **为什么要显式写 SSH 用户 yumao**：这台 Windows 的登录用户是「小又」，`ssh <主机>`
# 不带用户时用的就是它——NAS 上没有这个账号，必然是 Permission denied，白输一次密码。
# yumao 是 NAS 上的登录用户（也是部署记录里用的那个；compose 注释里容器以 uid 1000 跑，
# 也就是它）。
#
# **免密码那条路（推荐）**：本机有一把专用密钥 `~/.ssh/kylab-nas`（**名字里的 kylab 是
# 历史——它指的是 kylab 时代建的那把 NAS 钥匙，与今天用哪个仓库无关，别改名**：改了就
# 得在 NAS 上重装一次公钥）。装法见 `deploy/nas/README.md` 的「免密码部署」一节——
# 用户在 NAS 上贴一次公钥之后，这个脚本**不再问密码**。密钥不在就退回原来的密码方式；
# 用 `NAS_KEY=别的密钥路径` 可以换一把。
#
# 它把 README 里那两条命令合成**一次 ssh**（所以只问一次密码），并且在传之前先自证
# "这次要传的确实是 kybase 的管理台源码"——传一份别的过去再构建，最后只会得到一个
# 不是我们的界面，而那种失败最难看出来（页面 200、没有报错）。
set -eu

DRY=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY=1
  shift
fi
BRANCH="${1:-master}"
HOST="${2:-192.168.31.18}"
NAS_USER="${3:-${NAS_USER:-yumao}}"
NAS_KEY="${NAS_KEY:-$HOME/.ssh/kylab-nas}"
TARGET="$NAS_USER@$HOST"
SRC="/vol1/1000/docker/kybase/src"
APP="/vol1/1000/docker/kybase/app"

# 有专用密钥就用它，并且 `IdentitiesOnly=yes`——否则 ssh 会先把自己知道的其它钥匙
# 挨个试一遍，**还会在都没中之后回头问你密码**（那样"免密码"就成了空话）。
# 密钥没装好/不存在时留空，退回原来的"问一次密码"。
if [ -f "$NAS_KEY" ]; then
  SSH_OPTS="-i $NAS_KEY -o IdentitiesOnly=yes"
  SSH_HOW="密钥 $(basename "$NAS_KEY")"
else
  SSH_OPTS=""
  SSH_HOW="密码（没找到 $NAS_KEY）"
fi
# shellcheck disable=SC2086  # 上面这个变量就是要按词拆开传给 ssh

git rev-parse --show-toplevel >/dev/null 2>&1 \
  || { echo "!! 这里不是 git 仓库——请在仓库目录里跑（或双击 deploy/nas/deploy-from-windows.cmd）"; exit 2; }

echo "== 0/3 本机自证：分支与"要传的东西""
current=$(git rev-parse --abbrev-ref HEAD)
[ "$current" = "$BRANCH" ] || { echo "!! 当前在 $current 分支，不是 $BRANCH（先 git switch $BRANCH）"; exit 2; }
# 两条自证各挡一类错：前一条说明"这是 kybase 的管理台"（页面清单在那个文件里，
# 见 docs/部署指南 §8.1），后一条说明"远端那份应用层 compose 会跟着源码一起更新"。
git cat-file -e "$BRANCH:frontend/src/app/routes.ts" 2>/dev/null \
  || { echo "!! $BRANCH 的 frontend/ 里没有 src/app/routes.ts —— 这不是 kybase 的管理台，传过去只会得到别的界面"; exit 2; }
git cat-file -e "$BRANCH:deploy/nas/docker-compose.yml" 2>/dev/null \
  || { echo "!! $BRANCH 里没有 deploy/nas/docker-compose.yml —— 远端那份 compose 得跟着源码一起更新（见 README 的目录布局）"; exit 2; }
echo "   分支 $BRANCH ✓；归档里含 frontend/src/app/routes.ts 与 deploy/nas/docker-compose.yml ✓"

REMOTE="mkdir -p $SRC && tar -x -C $SRC --overwrite && cd $APP && sh $SRC/deploy/nas/update-frontend.sh $BRANCH"
if [ "$DRY" = "1" ]; then
  echo "== dry-run（不碰网络）=="
  echo "   归档：git archive --format=tar $BRANCH（约 $(git archive --format=tar "$BRANCH" | wc -c | tr -d ' ') 字节）"
  echo "   将以 $TARGET 登录（$SSH_HOW）"
  echo "   将要执行："
  echo "     git archive --format=tar $BRANCH | ssh ${SSH_OPTS:+$SSH_OPTS }$TARGET \"$REMOTE\""
  echo "   远端那一步（update-frontend.sh）会：判源码形态 → 只重建 frontend → up -d → 核对首页 200 与 #root"
  echo "   本机最后会 curl http://$HOST:8081/ 并认挂载点。去掉 --dry-run 就真跑。"
  echo "   提醒：这是**旧 archive 路**（会覆盖 git 检出的工作区）。常规升级更省事的是："
  echo "     cd $SRC && git pull && cd $APP && docker compose up -d --build"
  exit 0
fi

echo "== 1/3 推源码 + 在 NAS 上重建前端（$TARGET，$SSH_HOW）"
echo "   （旧 archive 路：覆盖 $SRC 的工作区。常规升级请在 NAS 上 git pull，见脚本头）"
# 一条 ssh 里做完：解包 → 跑升级脚本（脚本自己还会核对源码形态、docker 权限）
# shellcheck disable=SC2086  # $SSH_OPTS 要按词拆开
git archive --format=tar "$BRANCH" | ssh $SSH_OPTS "$TARGET" \
  "mkdir -p $SRC && tar -x -C $SRC --overwrite && cd $APP && sh $SRC/deploy/nas/update-frontend.sh $BRANCH"

echo "== 2/3 从本机核对首页"
# 远端脚本已经等过一次（最多 45 秒），这里再等 30 秒是给**网络/反代**留的余量：
# 只 curl 一次的话，刚 `up -d` 完的那一两秒会被判成"部署失败"，那是假失败。
# 按**真实时间**算（Windows 上被拒的连接一次要 ~3.5 秒，按轮次算就不准了）。
start=$(date +%s)
code=000
while [ "$(( $(date +%s) - start ))" -lt 30 ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$HOST:8081/" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
elapsed=$(( $(date +%s) - start ))
echo "   http://$HOST:8081/ → HTTP $code（等了 ${elapsed} 秒）"
[ "$code" = "200" ] || { echo "!! 不是 200：看 NAS 上 docker compose logs -f frontend"; exit 1; }

echo "== 3/3 认一下跑的是哪一版（本仓管理台的挂载点是 #root）"
if curl -s "http://$HOST:8081/" | grep -q 'id="root"'; then
  echo "   已确认是 kybase 管理台（#root）—— 部署完成。"
else
  echo "!! 页面里没有 #root：可能镜像没重建成功，或 src/frontend 不是本仓的管理台（看上一步 NAS 的输出）"
  exit 1
fi
