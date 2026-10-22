# GitHub Pages：把 site/ 发布成静态站

展示页是纯静态的，没有构建步骤；推送 `site/` 的改动即可触发部署。

- 本地预览：在仓库根目录跑 `python -m http.server 4180`，打开 <http://localhost:4180/site/>。
  （直接双击 `site/index.html` 也能看，但 `file://` 下动画与复制会受限。）
  调试参数：`?motion=off` 看静态/减少动态版本。
- 线上地址：<https://kkomia.github.io/kybase/>
- 首次启用：仓库 Settings → Pages → Source 选 **GitHub Actions**，或调用
  `POST /repos/kkomia/kybase/pages`（`build_type=workflow`）。
- 站点是项目页，挂载在 `/kybase/` 下，所以 `index.html` 里所有资源路径都用**相对路径**
  （`./assets/...`），不要改成 `/assets/...`。
- 资源：
  - `vendor/` 下是 GSAP 3.13.0（ScrollTrigger 同版本），按固定版本随仓库提交，不依赖 CDN。
  - `assets/fonts/sans-sc-var.woff2` 是 Noto Sans SC 可变字体按页面实际字符裁出的子集；
    `plex-mono-*.woff2` 是 IBM Plex Mono 400/500 的 ASCII 子集。两者均为 SIL OFL 1.1，
    许可证随文件放在 `assets/fonts/OFL-*.txt`。
- 页面结构：`index.html`（语义结构 + 示例数据）、`styles.css`（深色工作台与纸面证据两套表面）、
  `app.js`（工具列表、返回字段说明、双格式与调参演示、滚动动效）。
