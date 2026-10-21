# vendor

| 文件 | 版本 | 来源 | 许可 |
|---|---|---|---|
| `gsap.min.js` | 3.13.0 | <https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/gsap.min.js> | GreenSock Standard "No Charge" License（个人与商业项目可免费使用） |
| `ScrollTrigger.min.js` | 3.13.0 | <https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/ScrollTrigger.min.js> | 同上 |

按固定版本随仓库提交，页面不依赖 CDN。升级时两个文件一起换，并在 `app.js` 里回归
滚动叙事与 `prefers-reduced-motion` 的降级表现。
