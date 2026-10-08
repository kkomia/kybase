/**
 * 环境声明：Vite 能 import 的东西比 TS 默认认识的多（CSS、图片、`?raw`……）。
 * 这里只补我们真会用的那几个，`vite/client` 那份是官方给的，先接着它写。
 */
/// <reference types="vite/client" />

declare module '*.css' {
  const content: string
  export default content
}
