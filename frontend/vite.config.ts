import { availableParallelism } from 'node:os'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * 知识库管理台（kybase/frontend）的构建与开发配置。
 *
 * 开发端口 5175，`strictPort` 为真：端口被占直接失败，避免静默换端口后
 * “改了界面却没变”。
 *
 * `@` 别名、`/api` 反代与 `host: '0.0.0.0'` 是本地开发的固定约定；
 * 后端地址由 `KYBASE_API_TARGET` 指定。
 */

/**
 * `/api` 反代目标（`KYBASE_API_TARGET`，默认本机后端）。
 *
 * 本机起后端时无需设置；连到其他机器时：
 *   `KYBASE_API_TARGET=http://<host>:8080 pnpm dev`
 */
const apiTarget = process.env.KYBASE_API_TARGET || 'http://127.0.0.1:8000'

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
  // `vite preview` 用于验收生产构建：`pnpm build && pnpm preview`。
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
    // 限制并发，避免 jsdom 环境过多互相抢 CPU，导致用例随机超时。
    maxWorkers: Math.max(2, Math.min(8, availableParallelism() - 1)),
  },
})
