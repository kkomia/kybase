# GitHub Pages：把 site/ 发布成静态站

展示页是纯静态的，没有构建步骤；推送 `site/` 的改动即可触发部署。

- 本地预览：在仓库根目录跑 `python -m http.server 4180`，打开 <http://localhost:4180/site/>。
  （直接双击 `site/index.html` 也能看，但 `file://` 下 `fetch` 与动画会受限。）
- 线上地址：<https://kkomia.github.io/kybase/>
- 首次启用：仓库 Settings → Pages → Source 选 **GitHub Actions**，或由维护者调用
  `POST /repos/kkomia/kybase/pages`（`build_type=workflow`）。
- 站点是项目页，挂载在 `/kybase/` 下，所以 `index.html` 里所有资源路径都用**相对路径**
  （`./assets/...`），不要改成 `/assets/...`。
- 资源：`vendor/` 下是 GSAP 3.13.0（ScrollTrigger 同版本），按固定版本随仓库提交，
  不依赖 CDN；字体用系统字体，不加载外部字体。
