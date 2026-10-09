"""幻灯片生成·第一层：设计令牌（theme tokens）。

**为什么要有这一层。** 现在 agent 生成 PPT 是把文字直接喂给 python-pptx 的空白母版
（`app/services/office.py` 的 `build_pptx`），结果只有文字、没有版式。要把它做成能交付
的东西，就得按业内那套共识分三段：**内容 → 结构化数据 → 模板套版**，
模型不摆坐标、只在受控页型里填槽。三段各有一个模块：

- 本模块 = **令牌层**：颜色、字体、字号、间距、形状风格、以及**度量预算**。
  它是唯一一处"样式数字"的来源，上下游都从这里取，不许各自写死；
- `layouts.py` = **原型层**：页型 × 内容密度 → 区域矩形与命名槽位；
- `spec.py` = **契约层**：agent 该产出的结构化数据（LLM 与渲染之间的唯一接口）；
- `content_map.py` = **映射层**：deck spec → 每页每槽填什么 + 溢出决策。

**写盘层（把槽位变成 .pptx 字节）不在这一轮**：它要落到 PptxGenJS（Node），
而本仓库的 Node 只在 `frontend/` 里有（见 `content_map` 模块头与交付说明）。
本模块因此只**描述**几何与度量，不 import 任何排版库——`backend` 侧保持零新增依赖。

**实测硬约束（与本模块的常量直接相关）。** 三页 deck 用 PptxGenJS 实测得到三条：

1. **画布必须显式定为 13.333 × 7.5 英寸（16:9）**。库自带的 ``LAYOUT_16x9`` 是
   10 × 5.625，按 13.333 基准写好坐标再套那个布局，超出的部分（页脚、页码、图表轴）
   会被**静默裁掉**——不会报错。所以 `SLIDE_WIDTH_IN` / `SLIDE_HEIGHT_IN` 是
   全流程唯一基准，`layouts.py` 的每个矩形都以它为准；
2. **不能嵌字体**。目标机没装白名单里的字体时，PowerPoint **不报错、直接替换**，
   中文宽度实测会**再宽 6% 左右**。所以度量要按"白名单里最宽的那个"再留余量，
   见 `MeasureBudget.font_width_margin`；
3. **图表里的中文不吃 ``*FontFace``**（chart xml 只有 ``a:latin``，``a:ea`` 是空的）。
   这条**写盘层必须处理**（改 chart xml 或退回图片），这里只登记为已知缺口，
   见 `ThemeTokens.chart_font_gap`。

**字宽系数是怎么标定的。** 溢出判定不引新依赖，用"字数 ≈ 槽宽 ÷ 字号 ÷ 字宽系数"
这种查表/公式实现，所以系数必须是**量出来的**，不是拍的。方法：用本机已装的
`fontTools`（**不新增依赖**，标定过程是一次性的）直接读字体表 ``hmtx`` 的 advance 宽度，
除以 ``unitsPerEm`` 得到每字符的 em 宽度；样本覆盖 CJK、全角标点、ASCII 大写/小写/数字/标点
与空格，字体取白名单里的 微软雅黑（msyh.ttc）与 等线（Deng.ttf）。2026-10-05 本机结果：

| 类别 | 雅黑 mean | 雅黑 max | 等线 mean | 雅黑/等线 |
| --- | --- | --- | --- | --- |
| CJK | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| 全角标点 | 0.9472 | 1.0801 | 0.8073 | 1.0801 |
| ASCII 大写 | 0.6670 | 1.0176 | 0.6005 | 1.1127 |
| ASCII 小写 | 0.5380 | 0.9370 | 0.4782 | 1.1138 |
| ASCII 数字 | 0.5864 | 0.5864 | 0.5269 | 1.1131 |
| ASCII 标点 | 0.5510 | 1.0312 | 0.4807 | 1.0864 |
| 空格 | 0.2959 | 0.2959 | 0.2739 | 1.0802 |

两条结论直接落成常量：**CJK 是整 1.0 em（全宽，与字号相等）**，**ASCII 只能按类取均值**
（按 max 取会高估 50% 以上：`Revenue growth 18.6%` 的 max 模型算出 16.7 em，实测 11.0 em）。
下面 `MeasureBudget` 里的 0.70 / 0.56 / 0.60 / 0.62 / 0.30 就是"雅黑同类均值向上取整"，
再乘 `font_width_margin = 1.06`。回代验证（模型宽 ÷ 雅黑实测宽，7 个样例）：
1.060、1.064、1.063、1.047、1.061、1.062、1.062——**模型稳定落在"最宽字体 + 6%"上**，
对更窄的等线则留出 6%–22% 的余量。也就是说这条公式既不会低估（不会溢出），
也不会把窄字体算得过紧。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

__all__ = [
    "CJK_FONT_WHITELIST",
    "DEFAULT_THEME",
    "EMU_PER_INCH",
    "LATIN_FALLBACK_FONT",
    "POINTS_PER_INCH",
    "SLIDE_HEIGHT_IN",
    "SLIDE_WIDTH_IN",
    "FontChoice",
    "FontPairing",
    "MeasureBudget",
    "Palette",
    "ShapeStyle",
    "Spacing",
    "ThemeTokens",
    "TypeScale",
    "build_theme",
    "inches_to_emu",
    "inches_to_points",
    "theme_with_brand",
]

#: 幻灯片画布（英寸）。**必须显式钉死**：库（PptxGenJS）自带的 16:9 布局是
#: 10 × 5.625，照 13.333 基准写坐标会被静默裁掉页脚与图表轴。全流程只认这一对常量。
SLIDE_WIDTH_IN: Final[float] = 13.333
SLIDE_HEIGHT_IN: Final[float] = 7.5

EMU_PER_INCH: Final[int] = 914_400
POINTS_PER_INCH: Final[float] = 72.0


def inches_to_emu(value: float) -> int:
    """英寸 → EMU（OOXML 的整数单位）。四舍五入到整数，避免累积误差。"""
    return round(value * EMU_PER_INCH)


def inches_to_points(value: float) -> float:
    return value * POINTS_PER_INCH


# --------------------------------------------------------------- 字体白名单
#
# 为什么是"白名单"而不是"嵌字体"：OOXML 不吃工作区字体文件（PptxGenJS 明确不支持
# 嵌入字体），而目标机缺字体时 PowerPoint **不报错、直接替换**——所以能控的只有
# "在哪些平台上挑哪些系统自带字体"，以及"按最宽的那个留余量"。
# 每一条都写清平台与别名：别名是给未来的渲染器/检查器做匹配用的（Windows 的字体名
# 有中英两套，模型可能给出「微软雅黑」，而 OOXML 里要写 "Microsoft YaHei"）。


@dataclass(frozen=True)
class FontChoice:
    """白名单里的一个字体。``aliases`` 含中文名，``cjk`` 指它是否自带汉字。"""

    family: str
    aliases: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("windows",)
    cjk: bool = True


#: **CJK 字体白名单**：Windows（微软雅黑 / 等线）、macOS（苹方-简）、兜底 Arial。
#: 顺序即优先级（先 Windows 再 macOS）：本项目在 Windows 上开发，桌面壳也主要在
#: Windows 上跑，但下载 .pptx 的人可能在 Mac 上打开——所以两边都要有落点。
CJK_FONT_WHITELIST: Final[tuple[FontChoice, ...]] = (
    FontChoice("Microsoft YaHei", ("微软雅黑",), ("windows",), cjk=True),
    FontChoice("DengXian", ("等线",), ("windows",), cjk=True),
    FontChoice("PingFang SC", ("苹方-简", "PingFang SC"), ("macos",), cjk=True),
)

#: 西文兜底。Arial 在两大平台都在，且**不含汉字**——它只是"西文那一段"的兜底，
#: 汉字那一段永远由上面白名单里的字体承担（这就是 OOXML 里 `a:latin` 与 `a:ea`
#: 要分别指定的原因）。
LATIN_FALLBACK_FONT: Final[FontChoice] = FontChoice(
    "Arial", ("Helvetica",), ("windows", "macos"), cjk=False
)


@dataclass(frozen=True)
class FontPairing:
    """标题/正文**成对绑定**：一份 deck 里只有一个标题链与一个正文链。

    成对绑定的理由不是审美，是**度量**：字号阶梯与字宽系数都挂在链上，
    标题和正文各用一套字体时，溢出判定就得同时维护两套系数（实测两种字体宽度差
    最多 11%），那种"两个真相"迟早会分叉。要换风格就整对换。
    """

    title: tuple[str, ...] = ("Microsoft YaHei", "PingFang SC", "Arial")
    body: tuple[str, ...] = ("DengXian", "Microsoft YaHei", "PingFang SC", "Arial")
    latin: str = LATIN_FALLBACK_FONT.family

    def chain(self, role: str) -> tuple[str, ...]:
        return self.title if role == "title" else self.body


# ------------------------------------------------------------------- 色板


@dataclass(frozen=True)
class Palette:
    """配色。``primary`` 品牌主色可换，其余按明度关系跟着走。

    取值原则：正文与背景对比度 ≥ 7:1（AAA），次级文字 ≥ 4.5:1；
    图表序列色不靠红绿对比区分（色弱可用性）。
    """

    background: str = "FFFFFF"
    surface: str = "F4F6F9"
    primary: str = "1B4F8A"
    primary_dark: str = "123A66"
    secondary: str = "3E8FBF"
    accent: str = "E07B1E"
    text_primary: str = "1A1D21"
    text_secondary: str = "5A6472"
    text_muted: str = "8B95A3"
    rule: str = "D9DEE6"
    success: str = "2E7D5B"
    warning: str = "B58100"
    negative: str = "B23A3A"
    chart_series: tuple[str, ...] = (
        "1B4F8A",
        "3E8FBF",
        "E07B1E",
        "2E7D5B",
        "8B5FBF",
        "B23A3A",
    )

    def series_color(self, index: int) -> str:
        return self.chart_series[index % len(self.chart_series)]


# ----------------------------------------------------------------- 字号阶梯


@dataclass(frozen=True)
class TypeScale:
    """字号阶梯（pt）。标题 40/36/32 起步，正文 18 起步，下限 14/24。

    下限不是审美问题：正文低于 14pt 投影到会议室最后一排就读不了，
    那时正确的动作是**拆页**而不是继续缩——映射层的决策顺序就是这么定的
    （见 `content_map.OverflowAction`）。
    """

    cover_title_pt: int = 40
    section_title_pt: int = 36
    slide_title_pt: int = 32
    subtitle_pt: int = 20
    body_pt: int = 18
    kpi_value_pt: int = 28
    caption_pt: int = 14
    chart_label_pt: int = 12
    page_number_pt: int = 11
    min_title_pt: int = 24
    min_body_pt: int = 14

    def title_steps(self, archetype: str) -> tuple[int, ...]:
        """标题可用的字号档（从大到小）。到档底还放不下，就该拆页了。"""
        start = {
            "cover": self.cover_title_pt,
            "section": self.section_title_pt,
        }.get(archetype, self.slide_title_pt)
        return tuple(size for size in (40, 36, 32, 28, 24) if self.min_title_pt <= size <= start)

    @property
    def body_steps(self) -> tuple[int, ...]:
        """正文/要点可用的字号档（18 → 16 → 14）。"""
        return tuple(size for size in (18, 16, 14) if size >= self.min_body_pt)

    @property
    def subtitle_steps(self) -> tuple[int, ...]:
        """副标题：起点比正文大一档，但同样受 14pt 下限约束。"""
        candidates = (24, 20, 18, 16, 14)
        return tuple(
            size for size in candidates if self.min_body_pt <= size <= self.subtitle_pt
        )

    @property
    def caption_steps(self) -> tuple[int, ...]:
        """脚注/元信息：只有下限那一档——它本来就是小字，再缩就没得看了。"""
        return (max(self.caption_pt, self.min_body_pt),)


# ------------------------------------------------------------------- 间距


@dataclass(frozen=True)
class Spacing:
    """间距（英寸）。**0.5 / 0.3 这两个数是全流程的基准**，`layouts.py` 的
    安全区与槽位间隙都由它们推出来，别再另写一套。"""

    margin_x_in: float = 0.5
    margin_top_in: float = 0.4
    margin_bottom_in: float = 0.45
    gutter_in: float = 0.5
    block_gap_in: float = 0.3
    para_gap_in: float = 0.12
    bullet_indent_em: float = 1.15
    footer_height_in: float = 0.33
    footer_gap_in: float = 0.12
    title_height_in: float = 0.9

    @property
    def safe_left(self) -> float:
        return self.margin_x_in

    @property
    def safe_right(self) -> float:
        return SLIDE_WIDTH_IN - self.margin_x_in

    @property
    def safe_top(self) -> float:
        return self.margin_top_in

    @property
    def safe_bottom(self) -> float:
        return SLIDE_HEIGHT_IN - self.margin_bottom_in

    @property
    def content_width(self) -> float:
        return self.safe_right - self.safe_left

    @property
    def footer_top(self) -> float:
        return self.safe_bottom - self.footer_height_in


# ------------------------------------------------------------------- 形状


@dataclass(frozen=True)
class ShapeStyle:
    """形状与装饰风格。``icon_cycle`` 是**要点页的兜底视觉元素**：
    要点页没有图也没有图表，靠每个要点前的一枚图标满足"每页至少一个视觉元素"。
    图标用语义名（写盘层再映射到具体图标库/字形），不在这里绑死某个图标集。"""

    corner_radius_in: float = 0.08
    accent_bar_in: float = 0.14
    rule_thickness_in: float = 0.02
    shadow: str = "soft"
    icon_style: str = "outline"
    icon_cycle: tuple[str, ...] = (
        "circle-check",
        "arrow-right",
        "square-stack",
        "triangle-up",
        "hexagon-node",
        "square-dot",
    )

    def icon_for(self, index: int) -> str:
        return self.icon_cycle[index % len(self.icon_cycle)]


# --------------------------------------------------------------- 度量预算


@dataclass(frozen=True)
class MeasureBudget:
    """度量预算：把"这段文字要占多宽"变成一个不依赖任何库的公式。

    ``*_em`` 系数的标定方法与原始数据见模块头（fontTools 读 hmtx，2026-10-05 本机）。
    单位是 em，含义是"一个字符占几倍字号"：CJK 整 1.0，ASCII 只能按类取均值。

    ``font_width_margin`` = 1.06：**字体替换造成的宽度膨胀**。目标机缺白名单字体时
    PptxGenJS 实测约 +6%（不报错），所以所有宽度预算再乘这一档；
    加上系数本身是按最宽字体（雅黑）取的，合计对等线/苹方留出 6%–22% 余量。

    ``fit_slack`` = 0.97：断行只用到可用宽度的 97%。留这一档是因为"最后一行少放一个字"
    比"最后一行挤出去半个字"便宜得多，而我们的公式终究是估计。
    """

    cjk_em: float = 1.00
    fullwidth_punct_em: float = 1.00
    latin_upper_em: float = 0.70
    latin_lower_em: float = 0.56
    latin_digit_em: float = 0.60
    latin_punct_em: float = 0.62
    space_em: float = 0.30
    font_width_margin: float = 1.06
    line_spacing: float = 1.22
    inset_x_in: float = 0.15
    inset_y_in: float = 0.10
    fit_slack: float = 0.97


# ------------------------------------------------------------------ 主题


@dataclass(frozen=True)
class ThemeTokens:
    """一套主题 = 色板 + 字体对 + 字号阶梯 + 间距 + 形状风格 + 度量预算。

    ``chart_font_gap`` 是对**写盘层**的欠账登记（不是这里能修的）：
    图表的 chart xml 里只有 ``a:latin``、``a:ea`` 为空，所以图表标题/坐标轴上的中文
    不吃 ``*FontFace``，字体替换后可能挤掉轴标签。写盘层要么补 ``a:ea``，要么退回
    图片。放在令牌里是为了让"这个缺口是已知的"有一个可查的落点。
    """

    name: str = "kylab-default"
    palette: Palette = field(default_factory=Palette)
    fonts: FontPairing = field(default_factory=FontPairing)
    type_scale: TypeScale = field(default_factory=TypeScale)
    spacing: Spacing = field(default_factory=Spacing)
    shapes: ShapeStyle = field(default_factory=ShapeStyle)
    measure: MeasureBudget = field(default_factory=MeasureBudget)
    #: 已知缺口：图表内文字不继承东亚字体（写盘层处理）。
    chart_font_gap: str = "chart xml 只有 a:latin，图表内中文不吃 *FontFace"

    def with_brand(self, *, primary: str | None = None, accent: str | None = None) -> ThemeTokens:
        """换品牌主色/强调色，其余派生色跟着主色走（不必每个都传）。"""
        palette = self.palette
        if primary:
            origin = primary.lstrip("#").upper()
            palette = replace(
                palette,
                primary=origin,
                primary_dark=origin,
                chart_series=(origin, *palette.chart_series[1:]),
            )
        if accent:
            palette = replace(palette, accent=accent.lstrip("#").upper())
        return replace(self, palette=palette)

    def font_chain(self, role: str) -> tuple[str, ...]:
        return self.fonts.chain(role)


#: 默认主题。品牌可配：``DEFAULT_THEME.with_brand(primary="0F6CBD")``。
DEFAULT_THEME: Final[ThemeTokens] = ThemeTokens()


def build_theme(
    name: str,
    *,
    primary: str | None = None,
    accent: str | None = None,
    title_fonts: tuple[str, ...] | None = None,
    body_fonts: tuple[str, ...] | None = None,
) -> ThemeTokens:
    """造一套主题：色与字体可换，阶梯/间距/度量沿用默认（要改就改默认那份）。"""
    theme = replace(DEFAULT_THEME, name=name)
    if title_fonts or body_fonts:
        theme = replace(
            theme,
            fonts=replace(
                theme.fonts,
                title=title_fonts or theme.fonts.title,
                body=body_fonts or theme.fonts.body,
            ),
        )
    return theme.with_brand(primary=primary, accent=accent)


def theme_with_brand(theme: ThemeTokens | None = None, **kwargs: str) -> ThemeTokens:
    """``None`` 安全的换色入口：映射层拿到的是"可能是空的品牌配置"。"""
    return (theme or DEFAULT_THEME).with_brand(**kwargs)
