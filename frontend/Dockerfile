# 前端镜像（React 19 + Vite + pnpm）——kybase 知识库管理台。
#
# 构建上下文是 frontend/ 目录：package.json / pnpm-lock.yaml / pnpm-workspace.yaml
# / src 都在这儿，构建自足。
#
# 走 pnpm 的理由：依赖树按 pnpm 解析（`pnpm-workspace.yaml` 给 esbuild 放行安装脚本，
# 缺了它 vite 起不来）。
FROM node:22-slim AS builder

# npm 源可参数化，默认官方源；网络受限时可通过构建参数换镜像。
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

# 某些挂载文件系统会把源文件报成 000，COPY 进镜像后 nginx 读不了会返回 403
# （页面能开、图标是坏的）。与后端 Dockerfile 的 `chmod -R a+rX /app` 同一理由。
RUN chmod -R a+rX /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
