# NAS 部署（应用层）

这台 NAS（`192.168.31.18`）上的**存储层与应用层是两层，各自独立管理**。
kybase 只占应用层——存储层是 kylab 时代留下的、**两个应用共用**的那一套：

| 层 | 内容 | 位置 | 谁管 |
| --- | --- | --- | --- |
| 存储层 | PostgreSQL 17 + pgvector（54321）、MinIO（9000/9001） | `/vol1/1000/docker/kylab/`（PG 数据）、`/vol1/1000/docker/1panel/.../minio/`（对象） | **不是本仓**：前者一份单服务 compose，后者 1Panel。**一个都不许动** |
| 应用层 | 后端（FastAPI）+ MCP + 前端（nginx） | `/vol1/1000/docker/kybase/` 下的 `app/`（compose + `.env`）、`src/`（源码）、`data/`（运行期数据） | **本目录这份 compose** |

两层都挂在同一个 docker 网络 `1panel-network` 上，应用层按容器名
（`kylab-postgres` / `kylab-minio`）连过去——**不改存储层的任何配置，也不占宿主机的
54321/9000 端口**。容器名带 `kylab` 是那套存储自己的名字，与今天用哪个仓库无关，别改。

## 为什么要与旧 kylab-app 共用同一套库与桶（**这是有意的**）

`kybase-app` 连的就是旧 `kylab-app` 那套：同一个库 `kylab`、同一个桶 `kylab`、
同一对凭据。这么做的唯一目的是**数据零迁移**——切过去就直接看到同一份知识库，
没有导出/导入这一步（也就没有那一步能出的错）。代价与注意事项：

- **切换期间只该有一边在跑**。两边同时开着就是两个实例看同一份数据；任务队列靠租约
  认领、不会重复处理，但没必要。停旧的那份：`docker compose -p kylab-app down`
  （在旧的 `/vol1/1000/docker/kylab/app` 目录里跑）。
- **`data/` 目录必须各用各的**：里面躺着 DuckDB 表格副本（`tabular.duckdb`），
  而 **DuckDB 是单写者**——文件被另一个进程打开着，第二个进程连读都可能拿不到锁。
  所以 kybase 用 `/vol1/1000/docker/kybase/data`，不复用旧那个。
  切换时把旧的表格副本拷过来（**先停掉旧应用**，别让它在写）：
  `cp -a /vol1/1000/docker/kylab/data/tabular.duckdb /vol1/1000/docker/kybase/data/`。
  不拷也能起来，只是历史导入的表格要重新导入一遍。
- 想让 kybase 用**自己一套**库与桶也可以：先把新库/新桶建好，再改 `app/.env` 里的
  `KYLAB_PG_DB` / `KYLAB_S3_BUCKET`（compose 已经把这两个读成变量）。

## 目录布局（服务器上）

```
/vol1/1000/docker/kylab/          # 存储层（**别动**）
├── docker-compose.yml            #   postgres（原有的）
├── pgdata/
└── data/                         #   旧应用的数据目录（切换后可以留着备份）

/vol1/1000/docker/kybase/         # 应用层（本仓）
├── src/                          # 源码（git archive 解开，不含 .venv/node_modules）
├── app/
│   ├── docker-compose.yml        # 应用层（就是 deploy/nas/docker-compose.yml）
│   └── .env                      # 凭据：PG 口令 + MinIO 密钥（**不入库**，见下）
└── data/                         # 后端 /data：DuckDB 表格副本、日志、回退原件（持久化）
```

## 起 / 停 / 升级

```bash
cd /vol1/1000/docker/kybase/app
docker compose up -d --build        # 构建并起（首次约几分钟）
docker compose logs -f backend      # 看后端日志
docker compose ps                   # 状态（backend 应显示 healthy）
docker compose down                 # 停掉（**不动存储层**）
```

三个服务：`backend`（FastAPI，不发布宿主机端口）、`mcp`（同一镜像，8765 对外）、
`frontend`（nginx，8081 对外）。**MCP 那条通道强制鉴权**：每个请求要带
`Authorization: Bearer kylab_sk_…`（「设置 → API 密钥」里建），别把 8765 暴露到公网。

构建很慢（`uv sync` / `pnpm install`）时不要挂在 SSH 会话上：把构建放到脱离会话的后台跑，
日志落文件，另开会话 tail 即可（`setsid nohup docker compose build … > build.log 2>&1 &`）。

升级：把新源码替换 `src/`（见下"干净重解"），再 `docker compose up -d --build`。

### 后端也变了的时候（这一节是踩出来的）

`update-frontend.sh` 与 `deploy-from-windows.sh` **只重建前端**——它们是"快速换管理台"
那条路留下的工具。**`backend/` 或 `mcp/` 相关的东西改了，必须整棵重建**，否则会得到一个
**页面是新的、后端还是老的**部署：页面 200、没有任何报错，但新接口 404 或行为照旧，
这是最难查的一类假成功。

```bash
cd /vol1/1000/docker/kybase/app && docker compose build && docker compose up -d
```

**解包覆盖会留下旧文件**：NAS 的 `src/` 是 `git archive` 解开的**累积**目录，
`tar --overwrite` **只覆盖、不删**。旧版本留下的文件会被一起构建进去（`pnpm build` 的
tsc 会把整个 `frontend/` 检查一遍，多出来的旧文件能直接把构建弄红）。所以换代码时
**干净重解**：

```bash
# 在 NAS 上：把旧 src 改名备份（同卷 rename，秒级、可回滚），再解一份新的
cd /vol1/1000/docker/kybase && mv src src.bak-$(date +%Y%m%d-%H%M%S) && mkdir src
# 然后从本机再 archive 一次（就是 deploy-from-windows.sh 的 ① 那一步）
```

`src/` 里**没有任何"只存在于 NAS"的东西**（无 `.env`、无 `.git`、无 `*.local`；凭据住在
`app/.env`，数据住在 `data/`，都不在 `src/` 下），所以改名备份是安全的；确认部署无误后
`rm -rf src.bak-*` 清掉即可。

**部署完的自证**（比"页面 200"强得多：那次 200 完，后端却还是老的）：

```bash
cd /vol1/1000/docker/kybase/app
docker compose exec -T backend python -c "from app.services.runtime_config import DEFAULTS; print(sorted(k for k in DEFAULTS if k.startswith('retrieval.') or k == 'chat.permission'))"
docker compose exec -T backend python -c "from app.services import modes; print(list(modes.MODES), list(modes.PERMISSIONS))"
# 对外 MCP 面正好是知识库那 10 个（Agent 工具一个都不该在）：
docker compose exec -T backend python -c "from app.mcp_server.server import MCP_TOOL_NAMES; print(len(MCP_TOOL_NAMES), sorted(MCP_TOOL_NAMES))"
```

## 从这台 Windows 直接部署

**两选一**：

- **双击** `deploy/nas/deploy-from-windows.cmd`（找 Git Bash、切到仓库根、跑完停住别关窗）；
- 或在仓库目录里用 Git Bash 跑：

```bash
sh deploy/nas/deploy-from-windows.sh                            # 默认 master、192.168.31.18、用户 yumao
sh deploy/nas/deploy-from-windows.sh master 192.168.31.18 yumao # 三个都可以显式给
NAS_USER=别的账号 sh deploy/nas/deploy-from-windows.sh          # 环境变量也行
```

**它只换前端**（推源码 → 在 NAS 上只重建 frontend → 核对 200 与挂载点）。后端变了照上面那节整棵重建。

**SSH 用户必须写 `yumao`，不能省**：这台 Windows 的登录用户是「小又」，`ssh <主机>`
默认就用它——NAS 上没有这个账号，会直接 Permission denied，白输一次密码。

**免密码部署（装一次，之后都不问密码）**：本机有一把**专用密钥** `~/.ssh/kylab-nas`
（kylab 时代生成，ed25519、**无口令**、只用于这台 NAS 的部署）。**名字里的 `kylab` 是历史，
别改名**——改了就得在 NAS 上重装一次公钥。装一次：

```bash
sh deploy/nas/install-ssh-key.sh     # 会问一次 NAS 密码；之后部署不再问
```

它先把公钥那一行打印出来（也可以复制到 NAS 的界面里手工粘贴），再用一条 ssh
**幂等**地追加到 `~/.ssh/authorized_keys`（已在就不重复），最后用 `BatchMode=yes`
——**这一步绝不会问密码**——验证这把钥匙真的能进。要换一把就 `NAS_KEY=别的密钥`。

**先看一眼它要干什么**（不碰网络）：`sh deploy/nas/deploy-from-windows.sh --dry-run`
——打印分支与两条自证结果、归档大小、**将要执行的那一条 ssh 完整命令**，
以及远端那一步会做的事（判源码形态 → 只重建 frontend → `up -d` → 核对 200 与 `#root`）。

它把下面那两条 ssh 合成**一次连接**（所以只问一次密码），并在传之前先自证
"这次要传的确实是本仓的管理台"（`frontend/src/app/routes.ts` 在不在）——传一份别的过去
再构建，最后只会得到一个别的界面，而那种失败最难看出来（页面 200、没有报错）。

<details>
<summary>展开：手工的两条命令（脚本做的就是这两步）</summary>


```bash
# ① 把 master 的源码推到 NAS 的 src/（archive 覆盖；NAS 上那份本来就不是 git 检出）
git archive --format=tar master | ssh yumao@192.168.31.18 "mkdir -p /vol1/1000/docker/kybase/src && tar -x -C /vol1/1000/docker/kybase/src --overwrite"

# ② 在 NAS 上跑升级脚本（只重建前端 + 核对 #root）
ssh yumao@192.168.31.18 "cd /vol1/1000/docker/kybase/app && sh /vol1/1000/docker/kybase/src/deploy/nas/update-frontend.sh master"
```

两条都会在**你这边的终端**里问 NAS 的密码（OpenSSH 的交互式认证，密码不经脚本、也不落地）。
源码用 archive 覆盖是刻意的：NAS 的 `src/` 从来不是 git 检出，"git pull" 那条路在那边不存在。

</details>

**NAS 上一条命令的版本**（已经登录在 NAS 上时）：
`sh /vol1/1000/docker/kybase/src/deploy/nas/update-frontend.sh master`

它先看源码形态：`src/frontend/src/app/routes.ts` 在 → 是本仓的管理台，继续；
不在 → **不构建**，直接打印两条换源码的路子（本机 archive 过去 / 只覆盖 `frontend/` 这一棵）
并退出码 2 —— 免得白构建一次却发现界面不是我们的。

源码 OK 之后还会先过两道守卫，再做事：
- `$APP/docker-compose.yml` 不在 → 提示路径不对并退出码 2（免得在一台机器上瞎构建）；
- `docker info` 不通（最常见的是 `/var/run/docker.sock` 权限）→ **直接把该敲的那一行打出来**
  （`sudo sh <脚本> master`）并退出码 3，而不是丢一个 `permission denied` 让人猜。

然后是三件事：**只重建 frontend**（后端不重建）→ `up -d frontend` →
核对首页 200 **并认一次 `#root`**（镜像没真换掉时页面会安静地是旧版）。

> 构建走的是 **npmmirror**（`deploy/nas/docker-compose.yml` 给前端构建传了
> `NPM_REGISTRY`，与后端那份 `UV_INDEX_URL` 同一个理由：这台机器到境外带宽极差）。
> 换机器部署时不用改 Dockerfile——它默认仍是官方源。

镜像里的构建要点（排查用）：前端镜像用 **pnpm**（`corepack enable` + `packageManager:
pnpm@11.21.0` 钉版本），`.dockerignore` 排掉了 `node_modules`——**别把它删掉**：pnpm 装的是
平台相关依赖（esbuild 的 win32 二进制等），带进 Linux 镜像会以"本机能建、镜像里报平台错"收场。
`docker compose logs -f frontend` 看 nginx 日志。

构建完可以就地验两件事：

```bash
# 1) 镜像里真的带了仓库自带的技能（走 KYLAB_SKILLS_DIR=/app/skills，见 backend/Dockerfile）
docker compose exec -T backend python -c "from pathlib import Path; from app.services.skills import SkillService; print(sorted(r.name for r in SkillService(Path('/data')).list()))"
#    期望里有 'kylab-knowledge-base'——本仓只带这一份官方技能

# 2) 记忆/人设模板已经在数据目录里铺好（启动时幂等补的，落在挂载卷上）
ls -l /vol1/1000/docker/kybase/data/memory
#    期望：AGENTS.md / MEMORY.md / PROFILE.md / SOUL.md
```

界面：`http://192.168.31.18:8081`（首次打开会让你创建管理员账号）。
接口文档：`http://192.168.31.18:8081/api/v1/docs`；健康检查：`/api/v1/health`（不鉴权）。

## 部署后核对与回滚

**部署后核对 5 条**（`deploy-from-windows.sh` 只做了第 1、2 条的机器可判部分）：

**有脚本了**——在本机跑：

```bash
sh deploy/nas/verify-deployed-frontend.sh              # 默认 192.168.31.18:8081
sh deploy/nas/verify-deployed-frontend.sh 别的地址 8081
```

它判五件：首页 200 / 挂载点是 `id="root"` / `/api/v1/health` 是 ok /
index.html 引用的每个 `/assets/...` 都 200（防"dist 传了一半"）/**线上入口文件名与本机
`frontend/dist` 里那份一致**（证明"线上跑的就是本机验过的那份"；本机没构建过就跳过这条）。
任一条不过退出码 1 并指出是哪条。人眼那三条（登录 / 翻文档 / 搜一次看出处）它替不了。

| # | 怎么核 | 期望 |
| --- | --- | --- |
| 1 | `curl -s http://127.0.0.1:8081/ \| grep -o 'id="root"'` | 打到 `id="root"`（本仓管理台） |
| 2 | `curl -s http://127.0.0.1:8081/api/v1/health` | `{"status":"ok",…}` |
| 3 | 浏览器打开 8081，**登录一次** | 能进（凭据是库里的账号） |
| 4 | 打开一个知识库、翻一页文档、开一条笔记 | 列表/分页/抽屉/编辑器都在；控制台无红色报错 |
| 5 | **搜一次** | 结果带出处徽标、可点开看原文片段 |

**回滚**（应用层，两分钟）：把 `src/` 换回上一个可用版本，再重建对应服务。

```bash
# 在服务器上：先备份当前 src，再把上一次备份换回来，然后重建
cd /vol1/1000/docker/kybase
mv src src.bad-$(date +%Y%m%d-%H%M%S) && mv src.bak-<最近那个> src
cd app && docker compose build && docker compose up -d
curl -s http://127.0.0.1:8081/ | grep -o 'id="root"'
```

同一条路也能从本机做（`git archive` 某个提交推过去即可）。

## `.env`（在服务器上，不进仓库）

`app/.env` 只有四项，值与本机 `/vol1/1000/docker/kylab/docker-compose.yml` 里的一致
（共用同一套存储，见文件头）：

```ini
KYLAB_PG_PASSWORD=<与存储层 compose 里的一致>
KYLAB_S3_ACCESS_KEY=<MinIO 的 access key>
KYLAB_S3_SECRET_KEY=<MinIO 的 secret key>
KYLAB_S3_BUCKET=kylab
```

**为什么不把这些写进仓库**：仓库里那两份 compose 用的是占位默认值，真实凭据只活在这台
机器的 `.env` 里。`.env` 不要提交。

## 备份桶

备份提供者的快照落在**另一个桶**里（默认 `kylab-backup`，与知识库那个 `kylab` 分开：
一个是索引的原件、一个是用户会按恢复点整份删的整机快照，生命周期完全不同）。
那个桶**不在这份 compose 里建**——MinIO 是 1Panel 装的那个容器（`kylab-minio`）。
**桶名沿用 `kylab-backup` 不改**：里面已经有旧应用的历史快照，改名只会让它们变成孤儿。

**`mc` 从哪跑**（实测的三条事实）：这台 NAS 的**宿主上没有 `mc`**，本地也没有
`minio/mc` 镜像；**但 `kylab-minio` 容器里自带**（`/usr/bin/mc`）。所以最短的一条路是
**进那个容器跑**——凭据取容器自己的环境变量，不经过你的 shell 历史，也不用拉任何镜像：

```bash
# 在 NAS 上：一行（幂等，重复跑不报错；只建桶，不碰任何已有对象）
docker exec kylab-minio sh -c \
  'mc alias set kybase http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
   mc mb --ignore-existing kybase/kylab-backup'
# 核对：应该列出 kylab 与 kylab-backup 两个桶
docker exec kylab-minio sh -c 'mc ls kybase'
```

想换个桶名就**两处一起改**：`app/.env` 里加 `KYLAB_BACKUP_BUCKET=你的桶名`
（compose 已经把它透传进后端），再把上面那条 `mc mb` 的最后一段换成同一个名字。

**没有建桶也能先把应用起起来**：后端"**启动不校验桶**"，缺桶时备份握手仍然 200，
只是如实报 `capabilities.snapshot.available=false` 加一句可执行的下一步；
第一次真的上传时它还会顺手补建（要有 `s3:CreateBucket` 权限）。所以建桶是一步
**有意识的部署动作**，不是启动的前提。

## 持久化

| 数据 | 落在哪 | 备份方式 |
| --- | --- | --- |
| 元数据 / 向量 / 全文 | `kylab-postgres` 的 `/vol1/1000/docker/kylab/pgdata` | **`pg_dump`**，不要直接拷运行中的 data 目录 |
| 原件 / Markdown / 图片 | `kylab-minio` 的 `/vol1/1000/docker/1panel/data/1panel/apps/minio/minio/data` | 拷目录即可（对象是只写不改的） |
| 备份快照 | 同上——同一个 MinIO 数据目录里的另一个桶 | 同上 |
| 表格副本（DuckDB）、日志 | `/vol1/1000/docker/kybase/data`（bind mount） | 拷目录，或整卷备份 |

容器重建、`docker compose down` 都不会动这几处。**注意前两行是存储层**：它们由 kylab 时代
那两份配置管着，kybase 只是使用者——要备份/迁移存储层，别在这个目录里找配置文件。
