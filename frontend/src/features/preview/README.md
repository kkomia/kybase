# 预览（docx / pptx / xlsx / 图片 / PDF / 文本）

知识库的「阅读视角」与对话页的文件抽屉共用这一套。入口只有一个：

```tsx
import { FilePreview } from '@/features/preview'
```

**文档页**（`DocumentDrawer` 的「原文版式 / 解析文本」）把后端那份返回整个递进来即可：

```tsx
;<FilePreview preview={preview} page={citedPage} />
```

`preview` 就是 `getDocumentPreview()` 的返回（`kind` / `url` / `text` / `filename` 都在里面），
**不需要再传 `documentId`**——签名链接已经在 `preview.url` 上，组件只认链接、不认文档 id。
`page` 只对 PDF 有意义：会拼成 `#page=N`（原生阅读器的 PDF Open Parameters），
从引用点进来直接落到那一页；`#` 之后是片段，不会发给服务端。

**会话文件区**（`ConversationFile` 只有 key 与后缀）先自己换链接，再按后缀传：

```tsx
const { url } = await getFileUrl(conversationId, file.key, 'inline')
;<FilePreview name={file.name} kind={file.kind} url={url} />
```

`kind` 传后端给的 `PreviewKind`（文档接口）或文件后缀（会话文件区）；
显式传的 `name` / `kind` / `url` / `text` **优先于** `preview` 里的同名字段。
**字节由预览自己 fetch**：链接是相对路径、不带鉴权头，让三个库各自去猜怎么取
不如集中取一次，"取不到"也就有统一的报错位（旧 `OfficePreview.vue` 同一条）。

## 哪种文件走哪条路

分派表在 `kinds.ts` 的 `resolveRenderer()`——**纯函数，界面与测试都对着它**。
认的顺序：后端 `kind` → `kind` 本身当后缀 → `mime` → 文件名后缀。

| 输入（后缀 / 后端 kind）                                | 走哪条路                 | 用什么                                                                                                  |
| ------------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------- |
| `md` / `markdown`                                       | Markdown                 | `features/chat/model/markdown.tsx` 的 **`renderPlainMarkdown`**（只读档，**按需 import**；HTML 不解析） |
| `txt` `log` `csv` `tsv` `json` `yaml` `py` `ts` `sql` … | 纯文本 `<pre>`           | 浏览器自带                                                                                              |
| `png` `jpg` `jpeg` `gif` `webp` `bmp` `avif` / `image`  | `<img>`                  | 浏览器自带                                                                                              |
| `pdf` / `pdf`                                           | `<iframe>`               | 浏览器内置 PDF 阅读器（不引 pdf.js）                                                                    |
| `docx` / `docx`                                         | `DocxPreview`            | `docx-preview` 0.4.1                                                                                    |
| `pptx` / `pptx`                                         | `PptxPreview`            | `pptx-preview` 1.0.7                                                                                    |
| `xlsx` `xls` / `excel`                                  | `SpreadsheetPreview`     | `exceljs` 4.4.0 + `@tanstack/react-virtual` 3                                                           |
| 其它 / `binary`                                         | **一句"不能在这里预览"** | 不假装能预览                                                                                            |

`csv` 在两条路上落点不同，这是刻意的：会话文件区给的是后缀（`kind: "csv"`）→ 纯文本；
文档接口给的是后端算的 kind（`backend/app/services/documents.py` 把 `.csv` 归到
`markdown`，因为它有解析产物）→ Markdown。两份输入不一样，结果不一样。

## 什么条件下会预览失败

失败态一律显示**原因**，不吞异常；原因是这几类，文案都从 `notes.tsx` 走：

| 条件                            | 显示                                                           |
| ------------------------------- | -------------------------------------------------------------- |
| 没拿到签名链接（`url` 为空）    | `预览失败（拿不到预览链接）`                                   |
| 取字节失败（过期、403、断网）   | `预览失败（HTTP 404）` 等，取的是 `fetch` 的真实原因           |
| 解析失败（文件坏了 / 格式不对） | `预览失败（<库抛出的原因>）`；没有原因时 `这类文件无法预览`    |
| 分派结果是"不支持"              | `「x.bin」这个格式不能在这里预览` + 下载建议（**不是**失败态） |

两处专门说明：

- **老式 `.xls`**（OLE2 二进制）：分派表仍把它归到表格那一档（与旧前端一致），
  但 `exceljs` 只读 OOXML，所以它会落到失败态，文案换成
  `老式 .xls（二进制格式）解析不了，只有 .xlsx / .xlsm 能在这里预览`。
- **后端说 `binary`**（老式 `.doc`、压缩包、没配签名密钥时的兜底）：直接走"不能预览"，
  不再试——那是服务端已经判过一轮的结论。

## 已知限制（都在代码里留了注释）

| 项                            | 事实                                                                                                                                                                                                                                      | 影响                                                                                                                                                                                           |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| docx 的分页                   | docx-preview 只在**手动分页符**处断页，不做实时重排（上游 README 的 "Breaks" 一节）                                                                                                                                                       | 长文档的页边界可能与 Word 里不同                                                                                                                                                               |
| docx 的 TOC 字段              | 上游明说 field 还不支持                                                                                                                                                                                                                   | 目录显示为静态文字                                                                                                                                                                             |
| docx 的渲染                   | 页背景/描边走我们的令牌（覆盖了库自带的灰底白纸），正文默认色跟主题；**文档自己带的颜色是行内样式，仍然照原样**                                                                                                                           | 深色主题下是深色纸面，不再是刺眼的白                                                                                                                                                           |
| pptx 的自适应                 | `pptx-preview` 的缩放是 `init` 时按宽度算死的，且没有 resize 处理                                                                                                                                                                         | 组件监听 `ResizeObserver` 重建预览器；拖动窗口时会重画一次                                                                                                                                     |
| pptx 的字体/动画              | 库里没有嵌入字体，动画与切换效果不还原                                                                                                                                                                                                    | 字体回退到系统字体，观感与 PowerPoint 有差                                                                                                                                                     |
| pptx 的翻页                   | 固定 `mode: 'list'`（全部页面竖排、滚动看）。`slide` 模式上游自带一对硬编码 `#666666` 的圆形按钮                                                                                                                                          | 抽屉里读完整份 PPT 更顺；需要一页一页翻时再开 `slide`                                                                                                                                          |
| xlsx 的公式                   | `exceljs` **不求值**，只有文件里缓存的结果                                                                                                                                                                                                | 公式格显示缓存值，左上角标 `fx`（悬停看公式）；没有缓存值时直接显示 `=SUM(...)`                                                                                                                |
| xlsx 的合并单元格             | 只显示左上角的值，不做跨行跨列（虚拟化按网格独立定位）                                                                                                                                                                                    | 合并区看起来像空了一片                                                                                                                                                                         |
| xlsx 的规模                   | 单表最多画 10,000 行 / 256 列，超出会**截断并在表下说明**                                                                                                                                                                                 | 超大表要看全得下载                                                                                                                                                                             |
| xlsx 的样式                   | 只还原字体（粗斜体、下划线、颜色）、底色、水平对齐与列宽/行高                                                                                                                                                                             | 边框、条件格式、批注、图片不还原                                                                                                                                                               |
| xlsx 的工作表                 | 隐藏的工作表也列在标签里（不区分显示状态）                                                                                                                                                                                                | 与 Excel 的默认视图略有差别                                                                                                                                                                    |
| 图片                          | `svg` **不预览**（能在本站 origin 下执行脚本；服务端也不给它 `inline`）                                                                                                                                                                   | 与后端 `INLINE_SAFE_KINDS` 两处口径一致                                                                                                                                                        |
| PDF                           | 用浏览器内置阅读器，样式与能力跟着浏览器走                                                                                                                                                                                                | 零体积、零维护的代价                                                                                                                                                                           |
| 打包体积                      | 三个引擎都是**动态 import**；Markdown 那一档的渲染器（451 KB / gzip 140 KB）同样按需（见下面「体积」）                                                                                                                                    | 首屏不受影响                                                                                                                                                                                   |
| md 预览与对话共用 `md-*` 类名 | 预览的 Markdown 走的是对话那份只读渲染器，类名（`md-p` / `md-ul` / `md-pre`…）与对话页相同——`chat.css` 的规则因此也会命中预览；`preview.css` 已把**列表符号与条目间距、段落软换行、表格分隔线、标题字重、代码块底色与配色**这几处显式对齐 | 两条路（有没有 `chat.css`：对话页 vs 知识库/文档页）实测 23 个字段逐字段一致（`.shots/hygiene-md-code-contrast.json`，亮/暗各一组）。**代价是耦合**：改 `chat.css` 的 `.md-*` 时要连预览一起量 |

## 样式

`preview.css` 里所有颜色/间距/圆角都引 `tokens.css` 与 `themes/*.css` 的变量，
类名统一 `kylab-` 前缀。三层对手，覆盖处都写了原因：

- **层外的第三方样式**（`docx-preview` 注入到容器的 `<style>`、`main.tsx` 引进来的
  `highlight.js` 主题）：层外压过一切层，**选择器写得更具体也救不了**，所以覆盖它们的
  规则也只能留在层外（逐条带 `@unlayered` 标记）；
- `docx-preview` 的默认样式（灰底 + 白纸 + 阴影）：换成走令牌的纸面（深色主题下不再是
  一块白）；
- `pptx-preview` 把底色写成了**行内样式**（`background: #000`），只有 `!important`
  盖得住——本文件里唯一一处。

**代码高亮的配色也在这一层外面**：`github.css` 那份是**浅色**主题，深色主题下代码正文
（`.hljs` 的 `#24292e` 压在 `--bg-subtle` 上）实测只有 **1.12:1**，等于看不见。
`preview.css` 末尾那段层外规则把底色与前景交回令牌、并给深色主题换一份令牌配色
（按 `.md-pre` 选择，对话页回答里的代码块同样命中）。亮/暗实测：正文 16.2 / 11.9，
注释 5.6 / 6.0，关键字 4.2 / 4.9，字符串 12.1 / 7.1，数字 5.8 / 5.4——
口径是 WCAG 2.x 相对亮度（前景带 alpha 先与代码块底色合成），明细见
`.shots/hygiene-md-code-contrast.json`。

## 依赖

不需要新增。用到的都已在新前端的 `package.json` 里：

`docx-preview@0.4.1`、`pptx-preview@1.0.7`、`exceljs@4.4.0`、
`@tanstack/react-virtual@3.14.13`、`lucide-react`。

Markdown 那一档**不在这里直接依赖 `react-markdown`**：它走
`features/chat/model/markdown.tsx` 的只读档（`renderPlainMarkdown`），
那串依赖（`react-markdown@10` + `remark-gfm@4` + `remark-math` + `rehype-katex` +
`rehype-highlight` + `katex`）跟着**对话页那份实现**走，本域只按需 `import()` 它。

没有引 `@vue-office/*`（Vue 专属）、也没有引第二套表格组件
（`x-data-spreadsheet` / `react-spreadsheet`）：exceljs + 虚拟化已经够画一张只读的表，
再引一套等于多一份样式体系要跟主题对齐。

**体积**（`pnpm exec vite build` 实测，gzip 后/原始；一轮构造产物，日期见仓库记录）：

| chunk                            | 内容                                                           | gzip                 |
| -------------------------------- | -------------------------------------------------------------- | -------------------- |
| 入口（`FilePreview` + 各域骨架） | 我们的代码（**不含** Markdown 渲染器）                         | 13.6 KB（41.7 KB）   |
| Markdown 渲染器（**按需**）      | `features/chat/model/markdown.tsx` + react-markdown + KaTeX 等 | 139.9 KB（451.2 KB） |
| `docx-preview`                   | docx 引擎                                                      | 20.6 KB（75.5 KB）   |
| `jszip`                          | docx 用（pptx 自带一份自己的）                                 | 28.5 KB（96.0 KB）   |
| `pptx-preview`                   | pptx 引擎 + 它自带的 jszip + **echarts**（图表页要它）         | 387.6 KB（1.2 MB）   |
| `exceljs`                        | 表格引擎（浏览器版）                                           | 256.4 KB（929.6 KB） |

看 Word 不会下载 Excel 的引擎——各块独立，只有真的预览那一类文件时才取。

**Markdown 渲染器为什么必须按需**：它身后是 451 KB（gzip 140 KB）。曾经直接
`import { renderPlainMarkdown }`，那份代码就被塞进 `FilePreview` 所在的共享 chunk——
而那个 chunk 是**打开任何一份预览**都要加载的（pdf / 图片 / docx 也一样），
实测它从 41.4 KB（gzip 13.5）涨到 492.5 KB（gzip 153.0）；改成动态 import 之后
回到 41.7 KB（gzip 13.6），只有真的渲染 Markdown 才付那 451 KB。
对话页一直是静态带着这份渲染器，所以那边一分没省、也一分没多。

## 测试

`frontend-react/tests/preview.test.tsx`：分派表（后缀 / 后端 kind / mime）、
失败态（取不到 blob 时显示原因）、**md 预览的只读档**（类名来自渲染器、标题按原文层级、
单换行不换 `<br>`、不挂复制/下载按钮、危险协议退回原文、外链带 `target`/`rel`、
HTML 不当标记）、docx 与 pptx 的"容器挂载后调用上游 API"
（把 `docx-preview` / `pptx-preview` / `exceljs` 都 `vi.mock` 掉，断言调用参数）。

Markdown 那一档的渲染器是**按需 `import()`** 的，所以那组用例都从"等渲染器到位"开始
（`await screen.findBy…` / `waitFor`）——这一点也是契约的一部分。
