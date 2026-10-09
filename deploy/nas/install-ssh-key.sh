#!/usr/bin/env sh
# **把本机这把部署密钥装到 NAS 上**——只做一次，这一次要输密码；装好之后部署不再问密码。
#
# 用法：sh deploy/nas/install-ssh-key.sh [主机，默认 192.168.31.18] [SSH 用户，默认 yumao]
#      NAS_KEY=别的密钥 sh deploy/nas/install-ssh-key.sh
#
# 它做三件事，每件都打印出来（不静默）：
#   1. 打印公钥那一行（你要手工装的话，直接复制这一行）；
#   2. 用一条 ssh 把这行**幂等地**追加到 NAS 的 ~/.ssh/authorized_keys
#      （已经在了就不重复追加；目录与文件的权限按 sshd 的要求 700/600 设好）；
#   3. 立刻用这把密钥做一次 BatchMode 验证（`BatchMode=yes` = **绝不问密码**，
#      所以"验证通过"这句话是真的，不会是靠密码过的）。
#
# 为什么不用 `ssh-copy-id`：Git Bash 里不保证有它；而且它不会幂等去重，
# 反复跑会把同一把公钥追加成一堆重复行。
set -eu

HOST="${1:-192.168.31.18}"
NAS_USER="${2:-${NAS_USER:-yumao}}"
KEY="${NAS_KEY:-$HOME/.ssh/kylab-nas}"
PUB="$KEY.pub"
TARGET="$NAS_USER@$HOST"

[ -f "$KEY" ] || { echo "!! 私钥不在：$KEY（先用 ssh-keygen 生成，或 NAS_KEY= 指一把现成的）"; exit 2; }
[ -f "$PUB" ] || { echo "!! 公钥不在：$PUB"; exit 2; }

echo "== 1/3 公钥（要手工装的话就复制这一行）"
PUBKEY=$(cat "$PUB")
echo "   $PUBKEY"

echo
echo "== 2/3 装到 $TARGET 的 ~/.ssh/authorized_keys（这一步会问一次密码）"
# 幂等：先在远端 grep 一次，只有不在才追加。
# 公钥整行由本地展开进远端命令的**单引号**里（公钥里不会有单引号）——
# 这样远端 sh 不会再解释它，base64 里的 `+` `/` `=` 原样落盘。
ssh "$TARGET" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && { grep -qxF '$PUBKEY' ~/.ssh/authorized_keys || printf '%s\n' '$PUBKEY' >> ~/.ssh/authorized_keys; } && echo '   已装好（或本来就在）'"

echo
echo "== 3/3 用密钥验证（BatchMode：不会问密码，所以过了就是真过了）"
ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$TARGET" 'echo "   免密码登录 OK：$(hostname)"'

echo
echo "完成。之后直接跑：sh deploy/nas/deploy-from-windows.sh（不再问密码）"
