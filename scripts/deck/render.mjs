/**
 * 幻灯片写盘层：`DeckPlan.to_dict()` 的 JSON → `.pptx`。
 *
 * **这一层不实现版式。** 画什么、画在哪儿、多大字号、几行，全部在 Python 侧
 * （`app/services/deck/` 的令牌 / 原型 / 契约 / 映射四层）算完，本脚本只做一件事：
 * 把"已经定好的矩形与文字"落成 OOXML。所以每个坐标都来自计划里的 `rect_in`，
 * 没有一个是自己推的——**两处真相迟早分叉，所以只有一处**。
 * 写盘层自己决定的只有四样：颜色（令牌里的哪个色号）、页码（真页码字段）、
 * 图表 xml 的字体补丁、以及清掉库留下的悬空内容类型声明——前两样都在令牌里，
 * 后两样是"库做不到 / 库做错了"的补账，见结论 3 与 `pruneContentTypeOverrides`。
 *
 * 用法（开发机上用系统 Node 跑；给 sidecar 补 Node 运行时另立项，见交付说明）：
 *
 *     node scripts/deck/render.mjs --plan plan.json --out deck.pptx
 *     node scripts/deck/render.mjs --plan - --out deck.pptx < plan.json   # 走 stdin
 *     node scripts/deck/render.mjs --plan plan.json                      # 默认写 <plan>.pptx
 *
 * 退出码（Python 桥靠它把失败翻成人话，不指望 stderr 的措辞去匹配）：
 *
 * | 码 | 含义 | stderr |
 * | --- | --- | --- |
 * | 0 | 成功 | 无（stdout 末行是 `KYLAB_DECK_OK {…}` 一行 JSON 摘要） |
 * | 2 | 计划不合法（缺画布 / 槽位坐标不是数 / 不认识的 kind） | 一句话，指明第几页哪个槽 |
 * | 3 | 写盘失败（路径不可写、zip 后处理失败） | 一句话 |
 * | 4 | 缺依赖（`node_modules` 没装） | 一句话，说清在哪个目录 `pnpm install` |
 *
 * 四条实测硬约束（来自 `.tmp/pptx-spike` 那次 3 页 deck 实测，照做别改）：
 *
 * 1. **画布必须显式定尺寸**。库自带的 `LAYOUT_16x9` 是 10×5.625 英寸，照 13.333 基准
 *    写坐标会被**静默裁掉**页脚、页码、图表分类轴——不报错。所以这里从计划的
 *    `canvas` 读宽高，先 `defineLayout` 再 `pptx.layout = …`。
 * 2. **不能嵌字体**（PptxGenJS 没有 `embedFont`）。目标机缺字体时 Office/WPS
 *    **不报错、直接替换**（实测中文宽 +6%）。可控的只有写哪个字体名：取令牌字体链的
 *    第一个（`FontPairing.chain(role)[0]`），并**同时写 `a:latin` / `a:ea` / `a:cs` 三条**
 *    ——传 `fontFace` 时库会把三条都写上，只写一条的话中文那一段拿不到指定字体。
 *    （链上后面的名字是"客户端的替换表"，OOXML 里没有位置写它，写盘层也不用管。）
 * 3. **图表里的中文不吃 `*FontFace`**：chart xml 里只有 `<a:latin>`，`<a:ea>` 是空的
 *    （spike 那份 3 页 deck：a:latin 8 处 / a:ea 0 处；本轮这份 5 页 deck：6 / 0）。
 *    所以写完之后**解包把 `a:ea`/`a:cs` 补齐**（`patchChartFonts`），
 *    保持图表原生、不退化成图片。
 * 4. **行高要用 `spcPts` 而不是 `spcPct`**。映射层的容量模型里一行的高度是
 *    `字号 × measure.line_spacing`（18pt → 22pt）；而 `spcPct`（库的
 *    `lineSpacingMultiple`）是**相对"单倍行距"**的百分比，单倍行距本身约等于字号的
 *    1.2–1.4 倍——照模型写 1.22 会渲染成 ~1.5 倍，把计划算好的行数顶出去。
 *    因此一律传 `lineSpacing: 字号 × line_spacing`（pt），与容量模型同一口径。
 *
 * 另外两条与令牌对齐的落点：文字框内边距用 `measure.inset_x/y_in`（映射层算可用宽度
 * 时扣的是同一对数字），项目符号的悬挂缩进用 `spacing.bullet_indent_em`（库默认 27pt
 * 是另一套数，差 0.2 em 也是两处真相）。
 */

import { mkdir, readFile, writeFile, stat } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

// ---------------------------------------------------------------- 依赖（缺了要说人话）

const EXIT_OK = 0;
const EXIT_PLAN = 2;
const EXIT_WRITE = 3;
const EXIT_DEPS = 4;

const INSTALL_HINT =
  '缺少 Node 依赖 pptxgenjs / jszip / image-size：请在 scripts/deck 目录下执行 `pnpm install`' +
  '（开发机上装一次即可；node_modules 不入库，锁文件入库）';

let PptxGenJS;
let JSZip;
let imageSize;
try {
  ({ default: PptxGenJS } = await import('pptxgenjs'));
  ({ default: JSZip } = await import('jszip'));
  ({ imageSize } = await import('image-size'));
} catch (error) {
  fail(EXIT_DEPS, `${INSTALL_HINT}。原始错误：${error.message}`);
}

/** 打印一句人话并退出。所有失败路径都走这里，**不留裸栈给调用方**。 */
function fail(code, message) {
  process.stderr.write(`${message}\n`);
  process.exit(code);
}

// ---------------------------------------------------------------- 参数

function parseArgs(argv) {
  const args = { plan: null, out: null, baseDir: null };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === '--plan') args.plan = argv[++index];
    else if (token === '--out') args.out = argv[++index];
    else if (token === '--base-dir') args.baseDir = argv[++index];
    else if (token === '--help' || token === '-h') {
      process.stdout.write(
        '用法：node render.mjs --plan <plan.json|-> [--out deck.pptx] [--base-dir 目录]\n',
      );
      process.exit(EXIT_OK);
    } else {
      fail(EXIT_PLAN, `不认识的参数「${token}」：可用的是 --plan / --out / --base-dir`);
    }
  }
  if (!args.plan) fail(EXIT_PLAN, '缺 --plan：要么给计划 JSON 的路径，要么给 -（从 stdin 读）');
  return args;
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

// ---------------------------------------------------------------- 计划校验（人话）

const SLOT_KINDS = new Set(['text', 'list', 'chart', 'image', 'kpi', 'icons', 'decoration']);

class PlanError extends Error {}

function requireNumber(value, label) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new PlanError(`${label} 要是一个数字（当前是 ${JSON.stringify(value)}）`);
  }
  return value;
}

function checkRect(rect, label) {
  if (!rect || typeof rect !== 'object') throw new PlanError(`${label}缺 rect_in（英寸矩形）`);
  for (const key of ['x', 'y', 'w', 'h']) requireNumber(rect[key], `${label}的 rect_in.${key}`);
  if (rect.w <= 0 || rect.h <= 0) {
    throw new PlanError(`${label}的矩形宽高必须是正数（w=${rect.w} h=${rect.h}）`);
  }
}

function checkPlan(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) {
    throw new PlanError('计划 JSON 的顶层要是一个对象（DeckPlan.to_dict() 的输出）');
  }
  const canvas = plan.canvas;
  if (!canvas || typeof canvas !== 'object') {
    throw new PlanError(
      '计划里缺 canvas：画布必须显式给尺寸（库默认 16:9 是 10×5.625，' +
        '照 13.333 写坐标会被静默裁掉页脚与图表轴）',
    );
  }
  requireNumber(canvas.width_in, 'canvas.width_in');
  requireNumber(canvas.height_in, 'canvas.height_in');
  if (!plan.theme || typeof plan.theme !== 'object') {
    throw new PlanError('计划里缺 theme（设计令牌）');
  }
  for (const key of ['palette', 'fonts', 'type_scale', 'spacing', 'measure']) {
    if (!plan.theme[key] || typeof plan.theme[key] !== 'object') {
      throw new PlanError(`计划里的 theme 缺 ${key}：令牌是写盘层唯一的样式数字来源`);
    }
  }
  if (!Array.isArray(plan.pages) || plan.pages.length === 0) {
    throw new PlanError('计划里没 pages，或者 pages 是空的：一份没有页的 deck 没有可写的东西');
  }
  plan.pages.forEach((page, pageIndex) => {
    const where = `第 ${page?.index ?? pageIndex + 1} 页`;
    if (!page || typeof page !== 'object') throw new PlanError(`${where}不是对象`);
    if (!page.layout) throw new PlanError(`${where}缺 layout（版式键，如 bullets/heavy）`);
    if (!Array.isArray(page.slots) || page.slots.length === 0) {
      throw new PlanError(`${where}没有任何槽位（slots）：这一页会是一片空白`);
    }
    page.slots.forEach((slot) => {
      const label = `${where}的「${slot?.slot ?? '?'}」槽`;
      if (!SLOT_KINDS.has(slot?.kind)) {
        throw new PlanError(
          `${label}的 kind 不认识：收到 ${JSON.stringify(slot?.kind)}，` +
            `可用的是 ${[...SLOT_KINDS].join(' / ')}`,
        );
      }
      checkRect(slot.rect_in, label);
      if (slot.kind === 'chart') {
        const chart = slot.payload;
        if (!chart || !Array.isArray(chart.categories) || !Array.isArray(chart.series)) {
          throw new PlanError(`${label}的图表载荷要含 categories 与 series 两个数组`);
        }
        chart.series.forEach((series, seriesIndex) => {
          if (!Array.isArray(series?.values)) {
            throw new PlanError(`${label}的第 ${seriesIndex + 1} 个系列没有 values 数组`);
          }
          if (series.values.length !== chart.categories.length) {
            throw new PlanError(
              `${label}的系列「${series.name ?? seriesIndex + 1}」有 ${series.values.length} 个数值，` +
                `但类别有 ${chart.categories.length} 个：两者必须一一对应`,
            );
          }
        });
      }
    });
  });
  return plan;
}

// ---------------------------------------------------------------- 计划 → 画布上的形状

/** 绘制上下文：令牌 + 从计划读出来的常量，只算一次。 */
function context(plan) {
  const theme = plan.theme;
  return {
    theme,
    palette: theme.palette,
    measure: theme.measure,
    spacing: theme.spacing,
    typeScale: theme.type_scale,
    widthIn: plan.canvas.width_in,
    heightIn: plan.canvas.height_in,
    // 字体链只取第一个名字：OOXML 的一个 a:latin 只能写一个字体名。
    fonts: {
      title: (theme.fonts.title ?? [])[0] ?? 'Microsoft YaHei',
      body: (theme.fonts.body ?? [])[0] ?? 'Microsoft YaHei',
    },
    lang: 'zh-CN',
  };
}

/** 深底页型：封面与章节页用主色深底，其余白底。底色是令牌决定的，不是库默认。 */
const DARK_ARCHETYPES = new Set(['cover', 'section']);

/** 页码 / 页脚带的高（英寸），与 `Spacing.footer_height_in` 同源。 */
function footerRect(ctx, page) {
  const slot = page.slots.find((item) => item.slot === 'footer');
  if (slot) return slot.rect_in;
  const height = ctx.spacing.footer_height_in ?? 0.33;
  return {
    x: ctx.spacing.margin_x_in ?? 0.5,
    y: ctx.heightIn - (ctx.spacing.margin_bottom_in ?? 0.45) - height,
    w: ctx.widthIn - 2 * (ctx.spacing.margin_x_in ?? 0.5),
    h: height,
  };
}

/**
 * 槽位矩形的并集——heavy 密度那张"内容底板"的框。
 *
 * 用并集而不是自己按令牌推一个内容区：并集**天然落在计划自己给的几何里**，
 * 不可能压到标题、页脚或出安全区。写盘层凭空算一块底板，就等于在版式层之外
 * 又开了一处几何真相。
 */
function unionRect(rects) {
  const left = Math.min(...rects.map((rect) => rect.x));
  const top = Math.min(...rects.map((rect) => rect.y));
  const right = Math.max(...rects.map((rect) => rect.x + rect.w));
  const bottom = Math.max(...rects.map((rect) => rect.y + rect.h));
  return { x: left, y: top, w: right - left, h: bottom - top };
}

function hex(value) {
  return String(value ?? '')
    .replace(/^#/, '')
    .toUpperCase();
}

/** 颜色映射表（令牌 → OOXML 色号）。深底页要另配一套，否则正文与底色同色。 */
function textColor(ctx, page, fontRole) {
  const p = ctx.palette;
  if (DARK_ARCHETYPES.has(page.archetype)) {
    if (fontRole === 'kpi' || fontRole === 'display') return hex(p.secondary);
    return fontRole === 'caption' || fontRole === 'subtitle' ? hex(p.rule) : hex(p.background);
  }
  switch (fontRole) {
    case 'display':
      return hex(p.primary);
    case 'subtitle':
      return hex(p.text_secondary);
    case 'caption':
      return hex(p.text_muted);
    case 'kpi':
      return hex(p.primary);
    default:
      return hex(p.text_primary);
  }
}

/** 文字链：与 `FontPairing.chain(role)` 同一规则——只有 title 走标题链。 */
function fontFor(ctx, fontRole) {
  return fontRole === 'title' ? ctx.fonts.title : ctx.fonts.body;
}

/** 一行的高度（pt）：与映射层容量模型同一口径，见文件头结论 4。 */
function lineSpacingPt(ctx, fontPt) {
  return Math.round(fontPt * (ctx.measure.line_spacing ?? 1.22) * 100) / 100;
}

/** 段间距（pt），与 `Spacing.para_gap_in` 同源。 */
function paraSpaceAfterPt(ctx) {
  return Math.round((ctx.spacing.para_gap_in ?? 0.12) * 72 * 100) / 100;
}

function textOptions(ctx, slot, page) {
  const fontPt = slot.font_size_pt ?? ctx.typeScale.body_pt;
  const insetX = (ctx.measure.inset_x_in ?? 0.15) * 72;
  const insetY = (ctx.measure.inset_y_in ?? 0.1) * 72;
  return {
    x: slot.rect_in.x,
    y: slot.rect_in.y,
    w: slot.rect_in.w,
    h: slot.rect_in.h,
    fontFace: fontFor(ctx, slot.font_role),
    fontSize: fontPt,
    color: textColor(ctx, page, slot.font_role),
    lang: ctx.lang,
    align: slot.align ?? 'left',
    valign: slot.role === 'title' ? 'middle' : 'top',
    lineSpacing: lineSpacingPt(ctx, fontPt),
    margin: [insetY, insetX, insetY, insetX],
    wrap: true,
    // 溢出已经在映射层决定完了（缩字号 → 两栏 → 拆页）。OOXML 的 autofit
    // 只在"编辑之后"才生效，靠它等于把溢出丢给运气。
    fit: 'none',
  };
}

function writeText(slide, ctx, page, slot, { bullets = false } = {}) {
  const payload = slot.payload;
  const items = Array.isArray(payload) ? payload : [payload];
  const last = items.length - 1;
  // 悬挂缩进要自己钉死：库默认的项目符号缩进是 27pt（0.375"），
  // 而映射层按令牌的 bullet_indent_em（1.15 em）算续行宽度。按字号换算，两边同一个数。
  const bulletIndentPt = (ctx.spacing.bullet_indent_em ?? 1.15) * (slot.font_size_pt ?? 18);
  const runs = items.map((item, index) => {
    // breakLine / paraSpaceAfter **只加在段与段之间**：映射层的容量模型扣的是
    // (n-1) 个段间距，最后一段后面再补一个就多占一格。
    const options = { breakLine: index < last };
    if (bullets) {
      options.bullet = { characterCode: '25CF', indent: bulletIndentPt };
      options.indentLevel = 0;
      if (index < last) options.paraSpaceAfter = paraSpaceAfterPt(ctx);
    }
    return { text: String(item ?? ''), options };
  });
  slide.addText(runs, textOptions(ctx, slot, page));
}

function writeKpi(slide, ctx, page, slot) {
  const kpi = slot.payload ?? {};
  const valuePt = ctx.typeScale.kpi_value_pt ?? slot.font_size_pt ?? 28;
  const captionPt = ctx.typeScale.caption_pt ?? 14;
  const runs = [
    {
      text: String(kpi.value ?? ''),
      options: {
        fontSize: valuePt,
        bold: true,
        color: textColor(ctx, page, 'kpi'),
        breakLine: true,
      },
    },
    {
      text: String(kpi.label ?? ''),
      options: {
        fontSize: captionPt,
        color: textColor(ctx, page, 'caption'),
        breakLine: Boolean(kpi.delta),
      },
    },
  ];
  if (kpi.delta) {
    runs.push({
      text: String(kpi.delta),
      options: { fontSize: captionPt, color: hex(ctx.palette.success) },
    });
  }
  slide.addText(runs, { ...textOptions(ctx, slot, page), valign: 'middle' });
}

/** 图标循环里的语义名 → 一个基础形状（写盘层不绑图标库，见 `ShapeStyle.icon_cycle`）。 */
function iconShape(ctx, name) {
  const ShapeType = ctx.shape;
  if (name.startsWith('hexagon')) return ShapeType.hexagon;
  if (name.startsWith('circle')) return ShapeType.ellipse;
  if (name.startsWith('arrow')) return ShapeType.rightArrow;
  if (name.startsWith('triangle')) return ShapeType.triangle;
  return ShapeType.diamond;
}

/**
 * 要点页左边那一列图标：一枚对一个要点，纵向按**正文的行距**排。
 *
 * 不按槽位高度等分：图标列的高度是整块内容区（5 英寸），而 5 条要点只占顶上两英寸——
 * 等分的结果是图标散成一根虚线，跟右边的文字对不上。按行距排则正好落在每一行上
 * （两栏版式也成立：第 i 条要点在左右两栏是同一高度）。
 *
 * 近似之处：每个要点按**一行**算。某条要点折成两行时，它之后的图标会逐渐偏下
 * ——映射层给了整栏的总行数，没给每条的行数，这里不去猜（猜就是再实现一遍断行）。
 */
function writeIcons(slide, ctx, page, slot) {
  const names = Array.isArray(slot.payload) ? slot.payload : [];
  if (names.length === 0) return;
  const list = page.slots.find((item) => item.kind === 'list' && item.font_size_pt);
  const rect = slot.rect_in;
  const fontPt = list?.font_size_pt ?? ctx.typeScale.body_pt;
  const rowH = (lineSpacingPt(ctx, fontPt) + paraSpaceAfterPt(ctx)) / 72;
  const top = rect.y + (ctx.measure.inset_y_in ?? 0.1);
  const offset = list ? Math.max(0, list.rect_in.y - rect.y) : 0;
  let pitch = rowH;
  if (top + offset + pitch * names.length > rect.y + rect.h) {
    pitch = Math.max(0.1, (rect.h - offset) / names.length);
  }
  const size = Math.max(0.08, Math.min(rect.w, pitch) * 0.55);
  names.forEach((name, index) => {
    slide.addShape(iconShape(ctx, String(name)), {
      x: rect.x + (rect.w - size) / 2,
      y: top + offset + pitch * index + (rowH - size) / 2,
      w: size,
      h: size,
      fill: { color: hex(index % 2 === 0 ? ctx.palette.primary : ctx.palette.secondary) },
      line: { color: hex(ctx.palette.background), width: 0.75 },
      altText: `要点图标：${name}`,
    });
  });
}

/**
 * `ChartKind`（契约层的六个词）→ PptxGenJS 的图表类型 + 柱条方向。
 *
 * 枚举从 `pptx` **实例**上取：这个库的 ESM 构建不导出静态枚举
 * （`PptxGenJS.ChartType` 是 undefined），挂在实例上（`pptx.ChartType`）。
 */
function chartTypeFor(ctx, kind) {
  const CT = ctx.chartType;
  const map = {
    column: [CT.bar, 'col'],
    bar: [CT.bar, 'bar'],
    line: [CT.line, null],
    area: [CT.area, null],
    pie: [CT.pie, null],
    doughnut: [CT.doughnut, null],
  };
  return map[kind] ?? map.column;
}

function writeChart(slide, ctx, slot) {
  const chart = slot.payload ?? {};
  const [type, barDir] = chartTypeFor(ctx, chart.kind);
  const pieLike = chart.kind === 'pie' || chart.kind === 'doughnut';
  const font = ctx.fonts.body;
  const labelPt = ctx.typeScale.chart_label_pt ?? 12;
  const captionPt = ctx.typeScale.caption_pt ?? 12;
  const data = chart.series.map((series) => ({
    name: String(series.name ?? ''),
    labels: chart.categories.map((label) => String(label)),
    values: series.values.map((value) => Number(value)),
  }));
  const options = {
    x: slot.rect_in.x,
    y: slot.rect_in.y,
    w: slot.rect_in.w,
    h: slot.rect_in.h,
    ...(barDir ? { barDir, barGapWidthPct: 60 } : {}),
    chartColors: (ctx.palette.chart_series ?? [ctx.palette.primary]).map((value) =>
      hex(value).substring(0, 6),
    ),
    // 图表不画标题：计划里没有"图表标题"这个载荷，页标题已经承担了这件事。
    showTitle: false,
    showLegend: true,
    legendPos: 'b',
    legendFontFace: font,
    legendFontSize: captionPt,
    legendColor: hex(ctx.palette.text_secondary),
    chartArea: {
      fill: { color: hex(ctx.palette.background) },
      border: { pt: 1, color: hex(ctx.palette.rule) },
    },
    plotArea: { fill: { color: hex(ctx.palette.background) } },
  };
  if (pieLike) {
    Object.assign(options, {
      showPercent: true,
      dataLabelFontFace: font,
      dataLabelFontSize: labelPt,
      dataLabelColor: hex(ctx.palette.background),
    });
  } else {
    Object.assign(options, {
      showValue: true,
      dataLabelFontFace: font,
      dataLabelFontSize: labelPt,
      dataLabelColor: hex(ctx.palette.text_primary),
      catAxisLabelFontFace: font,
      catAxisLabelFontSize: labelPt,
      catAxisLabelColor: hex(ctx.palette.text_primary),
      valAxisLabelFontFace: font,
      valAxisLabelFontSize: captionPt,
      valAxisLabelColor: hex(ctx.palette.text_secondary),
      ...(chart.unit
        ? {
            valAxisTitle: String(chart.unit),
            valAxisTitleFontFace: font,
            valAxisTitleFontSize: captionPt,
            valAxisTitleColor: hex(ctx.palette.text_secondary),
          }
        : {}),
    });
  }
  slide.addChart(type, data, options);
}

/**
 * 原图的像素尺寸；读不出来（不认识的格式）返回 null。
 */
function measureImage(bytes) {
  try {
    const size = imageSize(bytes);
    return size && size.width > 0 && size.height > 0 ? size : null;
  } catch {
    return null;
  }
}

/**
 * 按 contain 规则把原图放进槽位：等比缩放 + 居中，**矩形自己算**（理由见 `writeImage`）。
 * 尺寸未知时返回 null，由调用方退回"整块槽位"。
 */
function containRect(box, size) {
  if (!size) return null;
  const scale = Math.min(box.w / size.width, box.h / size.height);
  const w = size.width * scale;
  const h = size.height * scale;
  return { x: box.x + (box.w - w) / 2, y: box.y + (box.h - h) / 2, w, h };
}

/**
 * 图片槽：本地文件真插图；现生 / 检索意图这里做不到，画一个说明框。
 *
 * **等比缩放要自己算，不能交给库的 `sizing`。** 实测（3 页 spike 之后的这一轮）：
 * 传 `sizing: {type:'contain', w, h}` 时，库对**本地 path 图片**根本量不出原图尺寸
 * （它的注释里写着 "FIXME: Measure actual image when no intWidth/intHeight params passed"），
 * 于是 `a:srcRect` 算成 0、`a:ext` 就是槽位的整块矩形——图片被**拉变形**塞进框里，
 * 而且不报错。上一轮没发现是因为 spike 那两张图的宽高比正好等于它的框。
 * 所以这里用 `image-size` 读原图像素，按 contain 规则算出居中后的矩形再交给库。
 *
 * **不静默丢图**：丢掉的后果是"图片数吻合"这条结构检查永远为真——
 * 检查恒真等于没有检查。所以做不到的那一格留一个带 alt 的虚线框，并把原因回传。
 */
async function writeImage(slide, ctx, page, slot, baseDir) {
  const image = slot.payload ?? {};
  const rect = slot.rect_in;
  const alt = String(image.alt ?? '图片');
  let warning = null;
  if (image.kind === 'local' && image.path) {
    const resolved = path.isAbsolute(image.path)
      ? image.path
      : path.resolve(baseDir, image.path);
    try {
      const info = await stat(resolved);
      if (info.isFile()) {
        const bytes = await readFile(resolved);
        const fitted = containRect(rect, measureImage(bytes));
        if (!fitted) {
          warning = `读不出的图片格式（${path.basename(resolved)}）：按槽位整块放，可能被拉变形`;
        }
        slide.addImage({ path: resolved, ...(fitted ?? rect), altText: alt });
        return { media: 1, warning };
      }
      warning = `图片槽的本地文件不存在：${resolved}（这一格画成说明框）`;
    } catch {
      warning = `图片槽的本地文件读不到：${resolved}（这一格画成说明框）`;
    }
  } else {
    warning = `图片来路是「${image.kind}」，写盘层不会现生/检索图片：${alt}（这一格画成说明框）`;
  }
  slide.addShape(ctx.shape.rect, {
    x: rect.x,
    y: rect.y,
    w: rect.w,
    h: rect.h,
    fill: { color: hex(ctx.palette.surface) },
    line: { color: hex(ctx.palette.rule), width: 1, dashType: 'dash' },
    altText: alt,
  });
  slide.addText(`图片位：${alt}`, {
    x: rect.x,
    y: rect.y + rect.h / 2 - 0.25,
    w: rect.w,
    h: 0.5,
    fontFace: ctx.fonts.body,
    fontSize: ctx.typeScale.caption_pt ?? 12,
    color: textColor(ctx, page, 'caption'),
    align: 'center',
    valign: 'middle',
    lang: ctx.lang,
    margin: 0,
  });
  return { media: 0, warning };
}

// ---------------------------------------------------------------- 母版（页型 × 密度）

/**
 * 每个"页型-密度"一把母版：底色 + （heavy 的）内容底板 + 页码字段。
 *
 * 母版这一层的价值是**密度档看得见**：12 个版式在计划里本来就是 12 套矩形
 * （bullets-light 一栏、bullets-heavy 两栏……）。heavy 多一层 `surface` 色的内容底板，
 * 画在**母版**里 = 一定在内容之下，不会压住任何文字，也不改变任何槽位几何。
 *
 * 强调条（`accent` 槽）**不在这里**：它是计划里一个有坐标的槽位（封面上是左边那条竖线、
 * 章节页是右下角那条短线），照它自己的 `rect_in` 画在页面上才对。
 */
function defineMasters(pptx, plan, ctx) {
  const byLayout = new Map();
  for (const page of plan.pages) {
    if (!byLayout.has(page.layout)) byLayout.set(page.layout, page);
  }
  const masters = new Map();
  for (const [layoutKey, page] of byLayout) {
    const dark = DARK_ARCHETYPES.has(page.archetype);
    const objects = [];
    if (!dark && page.density === 'heavy') {
      const content = page.slots
        .filter((slot) => slot.role !== 'decoration')
        .map((slot) => slot.rect_in);
      if (content.length > 0) {
        const box = unionRect(content);
        objects.push({
          rect: {
            x: box.x,
            y: box.y,
            w: box.w,
            h: box.h,
            fill: { color: hex(ctx.palette.surface) },
            line: { color: hex(ctx.palette.rule), width: 0.5 },
          },
        });
      }
    }
    const footer = footerRect(ctx, page);
    const name = `KYLAB_${layoutKey.toUpperCase().replace(/[^A-Z0-9]+/g, '_')}`;
    pptx.defineSlideMaster({
      title: name,
      background: { color: hex(dark ? ctx.palette.primary_dark : ctx.palette.background) },
      objects,
      // 页码用**真页码字段**（`a:fld type="slidenum"`），不是死文字：
      // 用户删一页、调一次页序，号码自己跟着变。
      slideNumber: {
        x: footer.x + Math.max(0, footer.w - 0.7),
        y: footer.y,
        w: 0.7,
        h: footer.h,
        fontFace: ctx.fonts.body,
        fontSize: ctx.typeScale.page_number_pt ?? 11,
        color: hex(dark ? ctx.palette.rule : ctx.palette.text_secondary),
        align: 'right',
        margin: 0,
      },
    });
    masters.set(layoutKey, name);
  }
  return masters;
}

// ---------------------------------------------------------------- 写页

async function buildDeck(plan, baseDir) {
  const ctx = context(plan);
  const pptx = new PptxGenJS();
  // 形状 / 图表枚举从这个**实例**上取：库的 ESM 构建不导出静态枚举。
  ctx.shape = pptx.ShapeType;
  ctx.chartType = pptx.ChartType;
  // 结论 1：显式画布。库的 'LAYOUT_16x9' 是 10×5.625，照 13.333 写坐标会被静默裁掉。
  pptx.defineLayout({ name: 'KYLAB_CANVAS', width: ctx.widthIn, height: ctx.heightIn });
  pptx.layout = 'KYLAB_CANVAS';
  pptx.author = 'Kylab';
  pptx.company = 'Kylab';
  pptx.title = plan.deck?.title ?? '';
  pptx.subject = 'Kylab deck plan';
  // 主题字体：让没被显式指定字体的那一小部分（图表继承、表格默认）也落到白名单上。
  pptx.theme = { headFontFace: ctx.fonts.title, bodyFontFace: ctx.fonts.body, lang: ctx.lang };

  const masters = defineMasters(pptx, plan, ctx);
  const warnings = [];
  let images = 0;
  let charts = 0;

  for (const page of plan.pages) {
    const slide = pptx.addSlide({ masterName: masters.get(page.layout) });
    if (page.speaker_notes) slide.addNotes(String(page.speaker_notes));
    for (const slot of page.slots) {
      switch (slot.kind) {
        case 'decoration': {
          const payload = slot.payload;
          if (payload && typeof payload === 'object' && 'page_number' in payload) {
            // 页码正常走母版的 slideNumber 字段。只有计划里的编号与页序不一致时
            // 才画成死文字——那说明计划自己就不自洽，按计划的数字画，并且说出来。
            if (Number(payload.page_number) !== Number(page.index)) {
              warnings.push(
                `第 ${page.index} 页的页脚写的是第 ${payload.page_number} 页：按计划的数字画成死文字`,
              );
              slide.addText(String(payload.page_number), {
                ...textOptions(ctx, { ...slot, font_role: 'caption' }, page),
                align: 'right',
              });
            }
          } else if (payload === 'accent-bar') {
            // 强调条照它自己的 rect 画（封面是左边那条竖线、章节页是右下角那条短线），
            // 颜色按底色深浅取令牌里的一个：深底用 accent，浅底用 primary。
            const ink = DARK_ARCHETYPES.has(page.archetype)
              ? ctx.palette.accent
              : ctx.palette.primary;
            slide.addShape(ctx.shape.rect, {
              x: slot.rect_in.x,
              y: slot.rect_in.y,
              w: slot.rect_in.w,
              h: slot.rect_in.h,
              fill: { color: hex(ink) },
              line: { color: hex(ink), width: 0 },
              altText: '强调条',
            });
          }
          break;
        }
        case 'text':
          writeText(slide, ctx, page, slot);
          break;
        case 'list':
          writeText(slide, ctx, page, slot, { bullets: true });
          break;
        case 'kpi':
          writeKpi(slide, ctx, page, slot);
          break;
        case 'icons':
          writeIcons(slide, ctx, page, slot);
          break;
        case 'chart':
          writeChart(slide, ctx, slot);
          charts += 1;
          break;
        case 'image': {
          const result = await writeImage(slide, ctx, page, slot, baseDir);
          images += result.media;
          if (result.warning) warnings.push(result.warning);
          break;
        }
        default:
          throw new PlanError(`不认识的槽位 kind「${slot.kind}」`);
      }
    }
    for (const note of page.warnings ?? []) warnings.push(`第 ${page.index} 页：${note}`);
  }
  for (const note of plan.warnings ?? []) warnings.push(`整份 deck：${note}`);
  return { pptx, warnings, images, charts };
}

// ---------------------------------------------------------------- 后处理：两处补丁

/**
 * 后处理总入口：**先补图表字体，再清悬空声明**，最后打回一个 zip。
 *
 * 两处都在写完之后做、都只碰该碰的那几个部件，其余部件一个字节不动。
 * 之所以合在一处：都要把包拆开再打回去，拆两次不如拆一次。
 */
async function postProcess(buffer) {
  const zip = await JSZip.loadAsync(buffer);
  const fonts = await patchChartFonts(zip);
  const contentTypes = await pruneContentTypeOverrides(zip);
  const repacked = await zip.generateAsync({
    type: 'nodebuffer',
    compression: 'DEFLATE',
    compressionOptions: { level: 6 },
  });
  return { buffer: repacked, fonts, pruned: contentTypes.pruned };
}

/**
 * 把 `ppt/charts/chart*.xml` 里的 `a:ea` / `a:cs` 补齐（结论 3）。
 *
 * 只做一件事：**清掉原有的 `a:ea`/`a:cs`，再按同一条 `a:latin` 补回来**
 * （`<a:latin typeface="X"/>` → `<a:latin .../><a:ea .../><a:cs .../>`）。
 *
 * 为什么是"清掉再补"而不是"只在缺的时候插"：库写出来的 chart xml 里既有
 * `<a:ea typeface=""/>` 这种空值，也有少数位置（图例的 txPr）写了非空的 `a:cs`
 * 却没有 `a:ea`——直接插会得到**两个 `<a:cs>`**。而 OOXML 的
 * `CT_TextCharacterProperties` 里 `latin` / `ea` / `cs` 各只能出现一次，
 * 多出来的那一个就是 schema 违规，PowerPoint 打开时可能弹"需要修复"。
 * 图表的字体 API 本来也只能给一个字体名（没有单独的东亚字体入口），
 * 所以"按 latin 重写 ea/cs"不丢信息，只是把三处对齐。
 *
 * 返回补齐前后的条数，让调用方（Python 桥 / 测试 / 产品负责人）能自己核对，
 * 而不是只信一句"补过了"。
 */
async function patchChartFonts(zip) {
  const names = Object.keys(zip.files)
    .filter((name) => /^ppt\/charts\/chart\d+\.xml$/.test(name))
    .sort();
  const stats = [];
  for (const name of names) {
    const original = await zip.file(name).async('string');
    let xml = original
      .replace(/<a:(?:ea|cs)\b[^>]*\/>/g, '')
      .replace(/<a:(?:ea|cs)\b[^>]*><\/a:(?:ea|cs)>/g, '');
    xml = xml.replace(
      /<a:latin\b([^>]*?)\/>/g,
      (match, attrs) => `<a:latin${attrs}/><a:ea${attrs}/><a:cs${attrs}/>`,
    );
    if (xml !== original) await zip.file(name, xml);
    stats.push({
      part: name,
      latin: (original.match(/<a:latin\b/g) ?? []).length,
      ea_before: (original.match(/<a:ea\b/g) ?? []).length,
      ea_after: (xml.match(/<a:ea\b/g) ?? []).length,
      cs_after: (xml.match(/<a:cs\b/g) ?? []).length,
      patched: xml !== original,
    });
  }
  return { stats, parts: names.length };
}

/**
 * 删掉 `[Content_Types].xml` 里**指向不存在部件**的 `<Override>`。
 *
 * 起因是一次实测抓到的库缺陷：PptxGenJS 每调一次 `defineSlideMaster` 就往
 * `[Content_Types].xml` 写一条母版声明，但它**只落一个母版部件**
 * （5 次定义 → 声明 slideMaster1..5，包里只有 slideMaster1）。于是包里挂着 4 条
 * 悬空声明——`[Content_Types].xml` 声明了不存在的 part，属于包结构不合规，
 * 打开时可能弹"需要修复"。上一轮 spike 的 3 页 deck 也有同样两条（当时只记录未处理）。
 *
 * 为什么在后处理里删而不是"少调几次 defineSlideMaster"：母版是按"页型-密度"分的
 * （12 个版式各一套装饰），少了就没有版式差异。悬空声明是库的账，写盘层在这里平掉。
 */
async function pruneContentTypeOverrides(zip) {
  const part = '[Content_Types].xml';
  const file = zip.file(part);
  if (!file) return { pruned: [] };
  const existing = new Set(
    Object.entries(zip.files)
      .filter(([, entry]) => !entry.dir)
      .map(([name]) => name),
  );
  const xml = await file.async('string');
  const pruned = [];
  const fixed = xml.replace(/<Override\b[^>]*\/>/g, (tag) => {
    const matched = /PartName="([^"]+)"/.exec(tag);
    if (!matched) return tag;
    const name = matched[1].replace(/^\//, '');
    if (existing.has(name)) return tag;
    pruned.push(name);
    return '';
  });
  if (pruned.length > 0) await zip.file(part, fixed);
  return { pruned };
}

// ---------------------------------------------------------------- 主流程

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const fromStdin = args.plan === '-';
  const raw = fromStdin ? await readStdin() : await readFile(args.plan, 'utf8');
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    fail(EXIT_PLAN, `计划不是合法 JSON：${error.message}`);
  }

  let plan;
  try {
    plan = checkPlan(parsed);
  } catch (error) {
    if (error instanceof PlanError) fail(EXIT_PLAN, `计划不合法：${error.message}`);
    throw error;
  }

  const baseDir = args.baseDir
    ? path.resolve(args.baseDir)
    : fromStdin
      ? process.cwd()
      : path.dirname(path.resolve(args.plan));
  const outPath = path.resolve(
    args.out ??
      (fromStdin ? 'deck.pptx' : `${path.basename(args.plan, path.extname(args.plan))}.pptx`),
  );

  const { pptx, warnings, images, charts } = await buildDeck(plan, baseDir);

  let written;
  try {
    written = await pptx.write({ outputType: 'nodebuffer' });
  } catch (error) {
    fail(EXIT_WRITE, `生成 pptx 字节失败：${error.message}`);
  }

  let packed;
  try {
    packed = await postProcess(Buffer.isBuffer(written) ? written : Buffer.from(written));
  } catch (error) {
    fail(EXIT_WRITE, `包后处理失败（没写出文件，原文件未被改动）：${error.message}`);
  }

  try {
    await mkdir(path.dirname(outPath), { recursive: true });
    await writeFile(outPath, packed.buffer);
  } catch (error) {
    fail(EXIT_WRITE, `写盘失败 ${outPath}：${error.message}`);
  }

  for (const warning of warnings) process.stderr.write(`[渲染警告] ${warning}\n`);
  process.stdout.write(
    `KYLAB_DECK_OK ${JSON.stringify({
      out: outPath,
      pages: plan.pages.length,
      layouts: plan.pages.map((page) => page.layout),
      charts,
      chart_parts: packed.fonts.parts,
      chart_font_patch: packed.fonts.stats,
      pruned_overrides: packed.pruned,
      media_added: images,
      warnings,
    })}\n`,
  );
  process.exit(EXIT_OK);
}

await main();
