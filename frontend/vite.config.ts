import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * 知识库管理台（kybase/frontend）的构建与开发配置。
 *
 * **端口 5175**：这一份额外的前端与 kylab 的前端（5173）**同时在一台机器上跑**，
 * 所以不能共用一个端口；`strictPort` 照旧为真——端口被占就**直接失败**，
 * 静默换到别的端口会留下"我改了界面却没变"的错觉（上游同款理由）。
 *
 * `@` 别名、`/api` 反代到后端、`host: '0.0.0.0'` 三条与上游逐字一致，
 * 所以两套前端的开发形态一样，只有"反代打到哪台后端"由环境变量区分。
 */

/**
 * `/api` 反代的目标（`KYBASE_API_TARGET`，**默认是 NAS 上那台知识库服务**）。
 *
 * 1. 默认 `http://192.168.31.18:8081` = NAS 上正在跑的知识库服务端
 *    （`/api/v1/health` 可探活）。开发形态就是"本机起界面、后端在那台"：
 *    浏览器里没有壳，等价的做法就是这个反代，再在登录页对那台服务登一次，
 *    于是知识库 / 概览 / 任务中心那几页都有真后端可打。
 * 2. 将来本仓库的后端（`backend/app/**`）独立跑起来之后，把它指向本机，例如
 *    `KYBASE_API_TARGET=http://127.0.0.1:8000 pnpm dev`。
 */
const apiTarget = process.env.KYBASE_API_TARGET || 'http://192.168.31.18:8081'

//: 反代的匹配串**带尾斜杠**，与线上那份 nginx 的 `location /api/` 同一口径。
//:
//: 不带斜杠的 `'/api'` 会把**任何以 `/api` 开头的路径**都吃掉，包括界面上的路由
//: `/api-keys`（2026-10-09 新增）——那条路由就被打到后端去、拿回一份 HTML 而不是
//: 本仓的界面，表现是"这一页整片空白"。线上 nginx 有那道斜杠所以没这毛病，
//: 于是"线上好好的、本机 5175 打开是空白"这种最难排的形态就出来了：
//: 开发与线上必须用同一套匹配规则。
const apiProxy = { '/api/': apiTarget }

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': new URL('./src', import.meta.url).pathname } },
  server: {
    host: '0.0.0.0',
    port: 5175,
    strictPort: true,
    proxy: apiProxy,
  },
  build: { outDir: 'dist', sourcemap: false },
  // `vite preview` 是**生产构建的验收入口**：这一份最终要由知识库服务端（或 nginx）
  // 发出去，这里给它同一套 `/api/` 反代，就能在本机用真实后端验一遍"构建产物 + 反代"
  // 这条链（`pnpm build && pnpm preview`，端口 4175 避开 dev 的 5175，也避开 kylab 的 4174）。
  preview: {
    port: 4175,
    strictPort: true,
    proxy: apiProxy,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    setupFiles: ['tests/setup.ts'],
    css: false,
  },
})
