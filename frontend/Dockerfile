# 前端镜像（React 19 + Vite + pnpm）——kybase 知识库管理台。
#
# `deploy/nas/docker-compose.yml` 的 frontend 服务用它，构建上下文是
# **本目录**（`context: ../src/frontend`，NAS 上就是 …/kybase/src/frontend）：
# package.json / pnpm-lock.yaml / pnpm-workspace.yaml / src 都在这儿，构建自足。
#
# 走 pnpm 的理由：本目录的依赖树按 pnpm 解析（`pnpm-workspace.yaml` 里给 esbuild
# 放行安装脚本，缺了它 vite 起不来）。
FROM node:22-slim AS builder

# npm 源**可参数化**，默认官方源（行为不变）。
# 为什么需要它：NAS 那台机器到境外的带宽极差（kylab 侧实测——后端 `uv sync` 从
# pythonhosted 拉 wheel 跑到 700 秒仍在下载，最后给 compose 加了
# `UV_INDEX_URL` 指阿里云）。前端同构：`deploy/nas/docker-compose.yml` 把
# `NPM_REGISTRY` 指到 registry.npmmirror.com。
# `COREPACK_NPM_REGISTRY` 也要一起给：corepack 下载 pnpm 本身走的是它，
# 不给的话"依赖装得挺快、卡在装 pnpm"会让人以为镜像是坏的。
ARG NPM_REGISTRY=https://registry.npmjs.org
ENV COREPACK_NPM_REGISTRY=$NPM_REGISTRY

WORKDIR /app

# pnpm 由 corepack 提供（版本跟着 package.json 的 packageManager 走：pnpm@11.21.0）
RUN corepack enable

# 先装依赖再拷源码：源码改动不该让依赖层失效
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --registry="$NPM_REGISTRY" || pnpm install --registry="$NPM_REGISTRY"

COPY . .
# `pnpm build` = `tsc -b && vite build`，产物在 /app/dist
RUN pnpm build


FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

# NAS 的 /vol1 文件系统会把个别文件报成 000（源文件 `favicon.svg` 就是），
# COPY 原样带进镜像后 nginx 读不了、请求返回 403（页面能开、标签页图标是坏的）。
# 与后端 Dockerfile 的 `chmod -R a+rX /app` 同一个理由，作用在静态产物根上。
RUN chmod -R a+rX /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
