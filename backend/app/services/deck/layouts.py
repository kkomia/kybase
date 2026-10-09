"""幻灯片生成·第二层：版式原型（layout archetypes）。

**这一层的职责只有一句话：决定每页长什么样，而且是"内容密度的函数"。**

模型的活是"把内容说清楚"，不是"把框摆在哪儿"。所以页型（archetype）是一等公民，
每个页型有两档密度变体（``light`` / ``heavy``），几何在这里写死，模型只能往命名槽位里
填东西。业内的共同做法就是这两条：**页型 ≤ 6–8 种**、**每个页型按内容密度给变体**
（light：标题 + 留白；heavy：多栏或更密的图）。

**六个原型**（与《大厂共识》对齐）：

| 原型 | 讲什么 | light | heavy |
| --- | --- | --- | --- |
| ``cover`` | 封面 | 强调条 + 标题/副标题/元信息 | 左文右图 |
| ``section`` | 章节页 | 大序号 + 章标题 | 序号 + 标题 + 本章要点 |
| ``bullets`` | 要点页 | 图标列 + 单栏要点 | 图标列 + 双栏要点 |
| ``split`` | 图文对半 | 左文右图各半 | 左文 + 结语 + 右图 |
| ``data`` | 数据页 | 图表 + 右侧一句结论 | 整幅图表 + 3 个 KPI + 结论条 |
| ``closing`` | 收尾 | 标题 + 副标题 + 短强调条 | 左文 + 联系方式 + 二维码图 |

**几何基准是 13.333 × 7.5 英寸（16:9）**，且全部由 `theme_tokens.Spacing` 推导：
安全区 = 画布扣掉四边留白（0.5 / 0.4 / 0.5 / 0.45），页脚带 = 安全区底部 0.33 高，
标题带 = 安全区顶部 0.9 高（放得下两行 32pt 标题）。**没有一个坐标是拍的**——
它们要么是推导值，要么是一处命名常量。

**"槽位互不重叠、不越界、不出安全区"是机械核对的，不是靠眼看**：
:func:`validate_layouts` 遍历 6 × 2 = 12 个原型的全部槽位，逐对算面积交集、
逐块比对画布与安全区，违规就返回人话描述。它的入口有三个（同一份实现）：
单测（`tests/unit/services/deck/test_layouts.py`）、
``python -m app.services.deck.layouts``、以及 CI 里的 pytest。
理由是这类错误**肉眼最容易漏**（差 0.1 英寸在代码里看不出来，在投影上就是文字压线），
而它恰好是纯几何、可以完全机械判定的事。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.services.deck.theme_tokens import (
    DEFAULT_THEME,
    SLIDE_HEIGHT_IN,
    SLIDE_WIDTH_IN,
    Spacing,
    ThemeTokens,
    inches_to_emu,
)

__all__ = [
    "ARCHETYPES",
    "ARCHETYPE_LABELS",
    "DENSITIES",
    "LAYOUTS",
    "Archetype",
    "Density",
    "Layout",
    "Rect",
    "Slot",
    "SlotRole",
    "get_layout",
    "iter_layouts",
    "layout_rows",
    "validate_layouts",
]

#: 几何比对容差（平方英寸）。1e-6 in² 约 6.5e-4 mm²——远小于任何真实排版误差，
#: 用来吸收浮点求和噪声（0.1 + 0.2 那种），不会掩盖"真的压住了一点点"。
_AREA_EPS: Final[float] = 1e-6
#: 坐标保留 3 位小数（≈0.025 mm），EMU 换算前先收敛，避免 12.332999… 这类噪声。
_PRECISION: Final[int] = 3


class Archetype(StrEnum):
    """页型枚举。**它是契约的一部分**：agent 只能从这六个里选。"""

    COVER = "cover"
    SECTION = "section"
    BULLETS = "bullets"
    SPLIT = "split"
    DATA = "data"
    CLOSING = "closing"


class Density(StrEnum):
    """内容密度变体。``AUTO`` 只出现在 spec 里，映射层会把它落成 light 或 heavy。"""

    LIGHT = "light"
    HEAVY = "heavy"
    AUTO = "auto"


class SlotRole(StrEnum):
    """槽位装什么。角色决定填法（文字要量宽度，图表/图片不要）与"算不算视觉元素"。"""

    TITLE = "title"
    TEXT = "text"
    LIST = "list"
    ICONS = "icons"
    CHART = "chart"
    IMAGE = "image"
    KPI = "kpi"
    DECORATION = "decoration"


#: 语言标签（给模型的错误话术与给人看的报告都用它）。
ARCHETYPE_LABELS: Final[dict[Archetype, str]] = {
    Archetype.COVER: "封面",
    Archetype.SECTION: "章节页",
    Archetype.BULLETS: "要点页",
    Archetype.SPLIT: "图文页",
    Archetype.DATA: "数据页",
    Archetype.CLOSING: "收尾页",
}

#: 会被算作"视觉元素"的槽位角色（页面不许是纯文字，见 `spec.py` 的校验）。
VISUAL_ROLES: Final[frozenset[SlotRole]] = frozenset(
    {SlotRole.ICONS, SlotRole.CHART, SlotRole.IMAGE, SlotRole.KPI}
)


@dataclass(frozen=True)
class Rect:
    """一个矩形（英寸，原点在左上角）。坐标在构造时即收敛到 3 位小数。"""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            object.__setattr__(self, name, round(float(getattr(self, name)), _PRECISION))

    @property
    def x2(self) -> float:
        return round(self.x + self.w, _PRECISION)

    @property
    def y2(self) -> float:
        return round(self.y + self.h, _PRECISION)

    @property
    def area(self) -> float:
        return self.w * self.h

    def intersection_area(self, other: Rect) -> float:
        """面积交集。正交矩形不重叠时结果为 0（含容差）。"""
        overlap_w = min(self.x2, other.x2) - max(self.x, other.x)
        overlap_h = min(self.y2, other.y2) - max(self.y, other.y)
        if overlap_w <= 0 or overlap_h <= 0:
            return 0.0
        return overlap_w * overlap_h

    def overlaps(self, other: Rect) -> bool:
        return self.intersection_area(other) > _AREA_EPS

    def contains(self, other: Rect) -> bool:
        return (
            other.x >= self.x - _AREA_EPS
            and other.y >= self.y - _AREA_EPS
            and other.x2 <= self.x2 + _AREA_EPS
            and other.y2 <= self.y2 + _AREA_EPS
        )

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def to_emu(self) -> dict[str, int]:
        """写盘层要的整数 EMU 坐标（dev 用；PptxGenJS 侧也吃英寸，两边都留着方便核对）。"""
        return {k: inches_to_emu(v) for k, v in self.as_dict().items()}

    def __str__(self) -> str:
        return f"({self.x:.3f},{self.y:.3f} {self.w:.3f}×{self.h:.3f})"


@dataclass(frozen=True)
class Slot:
    """一个命名槽位。

    ``font_role`` 只在文字槽上有值（写盘层据此取字体链与行距）；
    ``max_lines`` 是**硬上限**——文本超过它就由映射层做溢出决策（缩字号/两栏/拆页），
    而不是让写盘层去开 autofit：OOXML 的 autofit 只在"编辑过之后"才生效，
    靠它等于把溢出丢给运气（Google Slides API 里它甚至是只读的）。
    """

    name: str
    rect: Rect
    role: SlotRole
    font_role: str = "body"
    max_lines: int = 1
    align: str = "left"
    vertical: str = "top"
    required: bool = False
    note: str = ""

    @property
    def is_text(self) -> bool:
        return self.role in (SlotRole.TITLE, SlotRole.TEXT, SlotRole.LIST)

    @property
    def is_visual(self) -> bool:
        return self.role in VISUAL_ROLES


@dataclass(frozen=True)
class Layout:
    """一个原型的一个密度档：一组槽位 + 几个写盘层要用的索引。"""

    archetype: Archetype
    density: Density
    slots: tuple[Slot, ...]
    title_slot: str = "title"
    footer_slot: str = "footer"
    columns: tuple[str, ...] = ()
    accent_slot: str | None = None
    #: True = 这个原型**必定**绘制一个非文字装饰形状（强调条/大序号/页码带），
    #: 所以"每页至少一个视觉元素"由原型本身保证；False 的页型必须由内容提供
    #: 图标/图表/图片（见 `spec.py` 的校验与 `content_map` 的 icons 自动补齐）。
    guaranteed_visual: bool = False

    def slot(self, name: str) -> Slot:
        for candidate in self.slots:
            if candidate.name == name:
                return candidate
        raise KeyError(f"原型 {self.archetype.value}/{self.density.value} 没有槽位「{name}」")

    def slots_with_role(self, *roles: SlotRole) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if s.role in roles)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.slots)

    @property
    def key(self) -> str:
        return f"{self.archetype.value}/{self.density.value}"


# --------------------------------------------------------------- 几何推导
#
# 下面所有矩形都从 Spacing 推出来。改间距常量 = 改整版几何，这是刻意的：
# 让"版式"只有一个真相，而不是 12 个原型各写一套数字。

_SP: Final[Spacing] = DEFAULT_THEME.spacing
_CONTENT_W: Final[float] = _SP.content_width
_SAFE: Final[Rect] = Rect(
    _SP.safe_left, _SP.safe_top, _CONTENT_W, _SP.safe_bottom - _SP.safe_top
)
_TITLE: Final[Rect] = Rect(_SP.safe_left, _SP.safe_top, _CONTENT_W, _SP.title_height_in)
_FOOTER: Final[Rect] = Rect(
    _SP.safe_left, _SP.footer_top, _CONTENT_W, _SP.footer_height_in
)
_BODY_TOP: Final[float] = round(_TITLE.y2 + _SP.block_gap_in, _PRECISION)
_BODY_BOTTOM: Final[float] = round(_SP.footer_top - _SP.footer_gap_in, _PRECISION)
_BODY_H: Final[float] = round(_BODY_BOTTOM - _BODY_TOP, _PRECISION)

#: 图标列的宽度与它到正文的间隙（要点页用；图标是那类页面的视觉元素）。
_ICON_W: Final[float] = 0.55
_ICON_GAP: Final[float] = 0.2
_ICON_COL: Final[Rect] = Rect(_SP.safe_left, _BODY_TOP, _ICON_W, _BODY_H)
_TEXT_X: Final[float] = round(_ICON_COL.x2 + _ICON_GAP, _PRECISION)
_TEXT_W: Final[float] = round(_SP.safe_right - _TEXT_X, _PRECISION)


def _floor3(value: float) -> float:
    """向下取到 3 位小数。

    **除不尽的宽度必须向下取**：半幅/栏宽算出来是 5.9165，四舍五入成 5.917 之后，
    第二栏的右边界会变成 12.834——**顶出安全区 0.001 英寸**（实测被
    `validate_layouts` 抓出来）。向下取的代价只是间隙宽 0.0005 英寸，肉眼看不见；
    向上取的代价是版式越界，而越界在投影上就是文字贴边。
    """
    return int(value * 1000) / 1000


#: 双栏（要点页 heavy / 图文页）的列宽。
_HALF_W: Final[float] = _floor3((_CONTENT_W - _SP.gutter_in) / 2)
_COL_W: Final[float] = _floor3((_TEXT_W - _SP.gutter_in) / 2)

#: 数据页：图表区宽度与 heavy 档图表高度、KPI 行高度。
_CHART_W: Final[float] = 8.0
_CHART_H: Final[float] = 3.5
#: heavy 档图表矮一点，把高度让给 KPI 行与底部结论条。
_CHART_H_HEAVY: Final[float] = 3.4
_KPI_H: Final[float] = 0.7
_KPI_W: Final[float] = _floor3((_CONTENT_W - 2 * _SP.block_gap_in) / 3)
#: 底部结论条的高度：**0.55 不是随便定的**——它减掉上下 inset（各 0.1）之后要放得下
#: 一行 18pt（行高 18 × 1.22 / 72 = 0.305 英寸）。原先按 0.5 给，可用高度只剩 0.30，
#: 差 0.005 英寸放不下，于是整条结论被映射层缩到 16pt（实测发现）。
_QUOTE_BAR_H: Final[float] = 0.55
_QUOTE_BAR_TOP: Final[float] = round(_BODY_BOTTOM - _QUOTE_BAR_H, _PRECISION)

#: 封面/收尾这类"整页构图"用的少量固定量（仍以安全区/页脚带为界）。
_COVER_ACCENT_H: Final[float] = round(_BODY_BOTTOM - _SP.safe_top, _PRECISION)
_IMG_W: Final[float] = 4.833
_QR_W: Final[float] = 3.5


def _slot(
    name: str,
    rect: Rect,
    role: SlotRole,
    *,
    font_role: str = "body",
    max_lines: int = 1,
    align: str = "left",
    vertical: str = "top",
    required: bool = False,
    note: str = "",
) -> Slot:
    return Slot(
        name=name,
        rect=rect,
        role=role,
        font_role=font_role,
        max_lines=max_lines,
        align=align,
        vertical=vertical,
        required=required,
        note=note,
    )


def _footer_slot() -> Slot:
    return _slot("footer", _FOOTER, SlotRole.DECORATION, note="页码/品牌条，写盘层必绘")


def _title_slot(max_lines: int = 2, align: str = "left") -> Slot:
    return _slot(
        "title",
        _TITLE,
        SlotRole.TITLE,
        font_role="title",
        max_lines=max_lines,
        align=align,
        required=True,
        note="标题带，放得下两行 32pt",
    )


def _cover() -> dict[Density, Layout]:
    accent = Rect(
        _SP.safe_left, _SP.safe_top, DEFAULT_THEME.shapes.accent_bar_in, _COVER_ACCENT_H
    )
    text_x = round(accent.x2 + 0.5, _PRECISION)
    light = Layout(
        archetype=Archetype.COVER,
        density=Density.LIGHT,
        accent_slot="accent",
        guaranteed_visual=True,
        slots=(
            _slot("accent", accent, SlotRole.DECORATION, note="左侧强调条，视觉锚点"),
            _slot(
                "title",
                Rect(text_x, 2.6, 7.6, 1.5),
                SlotRole.TITLE,
                font_role="title",
                max_lines=2,
                required=True,
            ),
            _slot(
                "subtitle",
                Rect(text_x, 4.25, 7.6, 0.75),
                SlotRole.TEXT,
                font_role="subtitle",
                max_lines=2,
            ),
            _slot(
                "meta",
                Rect(text_x, 5.25, 7.6, 0.5),
                SlotRole.TEXT,
                font_role="caption",
                max_lines=2,
            ),
            _footer_slot(),
        ),
    )
    heavy = Layout(
        archetype=Archetype.COVER,
        density=Density.HEAVY,
        accent_slot="accent",
        guaranteed_visual=True,
        slots=(
            _slot("accent", accent, SlotRole.DECORATION, note="左侧强调条，视觉锚点"),
            _slot(
                "title",
                Rect(text_x, 1.8, 6.4, 1.8),
                SlotRole.TITLE,
                font_role="title",
                max_lines=2,
                required=True,
            ),
            _slot(
                "subtitle",
                Rect(text_x, 3.75, 6.4, 0.75),
                SlotRole.TEXT,
                font_role="subtitle",
                max_lines=2,
            ),
            _slot(
                "meta",
                Rect(text_x, 4.75, 6.4, 0.6),
                SlotRole.TEXT,
                font_role="caption",
                max_lines=3,
            ),
            _slot(
                "image",
                Rect(_SP.safe_right - _IMG_W, 1.05, _IMG_W, round(_BODY_BOTTOM - 1.05, _PRECISION)),
                SlotRole.IMAGE,
                required=True,
                note="封面主视觉",
            ),
            _footer_slot(),
        ),
    )
    return {Density.LIGHT: light, Density.HEAVY: heavy}


def _section() -> dict[Density, Layout]:
    light = Layout(
        archetype=Archetype.SECTION,
        density=Density.LIGHT,
        accent_slot="accent",
        guaranteed_visual=True,
        slots=(
            _slot(
                "number",
                Rect(_SP.safe_left, 2.0, 3.0, 2.2),
                SlotRole.TEXT,
                font_role="display",
                max_lines=1,
                note="章节大序号，视觉锚点",
            ),
            _slot(
                "title",
                Rect(_SP.safe_left, 4.4, 9.0, 1.4),
                SlotRole.TITLE,
                font_role="title",
                max_lines=2,
                required=True,
            ),
            _slot(
                "accent",
                Rect(_SP.safe_left, 6.0, 4.0, DEFAULT_THEME.shapes.accent_bar_in),
                SlotRole.DECORATION,
            ),
            _footer_slot(),
        ),
    )
    heavy = Layout(
        archetype=Archetype.SECTION,
        density=Density.HEAVY,
        accent_slot="accent",
        guaranteed_visual=True,
        slots=(
            _slot(
                "number",
                Rect(_SP.safe_left, _BODY_TOP, 3.4, 2.0),
                SlotRole.TEXT,
                font_role="display",
                max_lines=1,
            ),
            _slot(
                "title",
                Rect(_SP.safe_left, 3.8, 8.0, 1.2),
                SlotRole.TITLE,
                font_role="title",
                max_lines=2,
                required=True,
            ),
            _slot(
                "body",
                Rect(_SP.safe_left, 5.2, 11.0, 1.4),
                SlotRole.LIST,
                font_role="body",
                max_lines=3,
                note="本章要点预告，装不下就换双栏（heavy）",
            ),
            _slot(
                "accent",
                Rect(
                    _SP.safe_right - DEFAULT_THEME.shapes.accent_bar_in,
                    _BODY_TOP,
                    DEFAULT_THEME.shapes.accent_bar_in,
                    _BODY_H,
                ),
                SlotRole.DECORATION,
                note="右侧强调条",
            ),
            _footer_slot(),
        ),
    )
    return {Density.LIGHT: light, Density.HEAVY: heavy}


def _bullets() -> dict[Density, Layout]:
    icons = _slot(
        "icons", _ICON_COL, SlotRole.ICONS, note="每个要点一枚图标，本页的视觉元素"
    )
    light = Layout(
        archetype=Archetype.BULLETS,
        density=Density.LIGHT,
        slots=(
            _title_slot(),
            icons,
            _slot(
                "body",
                Rect(_TEXT_X, _BODY_TOP, _TEXT_W, _BODY_H),
                SlotRole.LIST,
                font_role="body",
                max_lines=6,
                required=True,
            ),
            _footer_slot(),
        ),
    )
    heavy = Layout(
        archetype=Archetype.BULLETS,
        density=Density.HEAVY,
        columns=("col_1", "col_2"),
        slots=(
            _title_slot(),
            icons,
            _slot(
                "col_1",
                Rect(_TEXT_X, _BODY_TOP, _COL_W, _BODY_H),
                SlotRole.LIST,
                font_role="body",
                max_lines=7,
                required=True,
            ),
            _slot(
                "col_2",
                Rect(
                    round(_TEXT_X + _COL_W + _SP.gutter_in, _PRECISION),
                    _BODY_TOP,
                    _COL_W,
                    _BODY_H,
                ),
                SlotRole.LIST,
                font_role="body",
                max_lines=7,
                required=True,
            ),
            _footer_slot(),
        ),
    )
    return {Density.LIGHT: light, Density.HEAVY: heavy}


def _split() -> dict[Density, Layout]:
    left = Rect(_SP.safe_left, _BODY_TOP, _HALF_W, _BODY_H)
    right_x = round(_SP.safe_left + _HALF_W + _SP.gutter_in, _PRECISION)
    image = Rect(right_x, _BODY_TOP, _HALF_W, _BODY_H)
    light = Layout(
        archetype=Archetype.SPLIT,
        density=Density.LIGHT,
        slots=(
            _title_slot(),
            _slot("body", left, SlotRole.LIST, font_role="body", max_lines=7, required=True),
            _slot("image", image, SlotRole.IMAGE, required=True, note="右半幅图，本页的视觉元素"),
            _footer_slot(),
        ),
    )
    body_h = 3.6
    quote_y = round(_BODY_TOP + body_h + _SP.block_gap_in, _PRECISION)
    heavy = Layout(
        archetype=Archetype.SPLIT,
        density=Density.HEAVY,
        slots=(
            _title_slot(),
            _slot(
                "body",
                Rect(_SP.safe_left, _BODY_TOP, _HALF_W, body_h),
                SlotRole.LIST,
                font_role="body",
                max_lines=5,
                required=True,
            ),
            _slot(
                "quote",
                Rect(
                    _SP.safe_left,
                    quote_y,
                    _HALF_W,
                    round(_BODY_BOTTOM - quote_y, _PRECISION),
                ),
                SlotRole.TEXT,
                font_role="body",
                max_lines=3,
                note="图下结论/图注",
            ),
            _slot("image", image, SlotRole.IMAGE, required=True),
            _footer_slot(),
        ),
    )
    return {Density.LIGHT: light, Density.HEAVY: heavy}


def _data() -> dict[Density, Layout]:
    quote_x = round(_SP.safe_left + _CHART_W + _SP.block_gap_in, _PRECISION)
    light = Layout(
        archetype=Archetype.DATA,
        density=Density.LIGHT,
        slots=(
            _title_slot(),
            _slot(
                "data",
                Rect(_SP.safe_left, _BODY_TOP, _CHART_W, _BODY_H),
                SlotRole.CHART,
                required=True,
                note="原生图表（图表优先，图片只做兜底）",
            ),
            _slot(
                "quote",
                Rect(quote_x, _BODY_TOP, round(_SP.safe_right - quote_x, _PRECISION), _BODY_H),
                SlotRole.TEXT,
                font_role="body",
                max_lines=8,
                required=True,
                note="一句结论：图不自己说话",
            ),
            _footer_slot(),
        ),
    )
    kpi_y = round(_BODY_TOP + _CHART_H_HEAVY + _SP.block_gap_in, _PRECISION)
    kpi_slots = tuple(
        _slot(
            f"kpi_{index + 1}",
            Rect(
                round(_SP.safe_left + index * (_KPI_W + _SP.block_gap_in), _PRECISION),
                kpi_y,
                _KPI_W,
                _KPI_H,
            ),
            SlotRole.KPI,
            font_role="kpi",
            max_lines=2,
        )
        for index in range(3)
    )
    heavy = Layout(
        archetype=Archetype.DATA,
        density=Density.HEAVY,
        slots=(
            _title_slot(),
            _slot(
                "data",
                Rect(_SP.safe_left, _BODY_TOP, _CONTENT_W, _CHART_H_HEAVY),
                SlotRole.CHART,
                required=True,
            ),
            *kpi_slots,
            _slot(
                "quote",
                Rect(_SP.safe_left, _QUOTE_BAR_TOP, _CONTENT_W, _QUOTE_BAR_H),
                SlotRole.TEXT,
                font_role="body",
                max_lines=2,
                required=True,
                note="底部结论条",
            ),
            _footer_slot(),
        ),
    )
    return {Density.LIGHT: light, Density.HEAVY: heavy}


def _closing() -> dict[Density, Layout]:
    light = Layout(
        archetype=Archetype.CLOSING,
        density=Density.LIGHT,
        accent_slot="accent",
        guaranteed_visual=True,
        slots=(
            _slot(
                "title",
                Rect(_SP.safe_left, 2.6, _CONTENT_W, 1.2),
                SlotRole.TITLE,
                font_role="title",
                max_lines=2,
                required=True,
            ),
            _slot(
                "subtitle",
                Rect(_SP.safe_left, 4.0, _CONTENT_W, 0.8),
                SlotRole.TEXT,
                font_role="subtitle",
                max_lines=2,
            ),
            _slot(
                "accent",
                Rect(
                    round(_SP.safe_left + (_CONTENT_W - 2.0) / 2, _PRECISION),
                    5.2,
                    2.0,
                    DEFAULT_THEME.shapes.accent_bar_in,
                ),
                SlotRole.DECORATION,
            ),
            _footer_slot(),
        ),
    )
    heavy = Layout(
        archetype=Archetype.CLOSING,
        density=Density.HEAVY,
        accent_slot="accent",
        guaranteed_visual=True,
        slots=(
            _slot(
                "title",
                Rect(_SP.safe_left, 1.9, 7.4, 1.2),
                SlotRole.TITLE,
                font_role="title",
                max_lines=2,
                required=True,
            ),
            _slot(
                "subtitle",
                Rect(_SP.safe_left, 3.3, 7.4, 0.9),
                SlotRole.TEXT,
                font_role="subtitle",
                max_lines=2,
            ),
            _slot(
                "meta",
                Rect(_SP.safe_left, 4.5, 7.4, 1.6),
                SlotRole.TEXT,
                font_role="caption",
                max_lines=5,
                note="联系方式/下一步",
            ),
            _slot(
                "image",
                Rect(round(_SP.safe_right - _QR_W - 0.5, _PRECISION), 2.4, _QR_W, _QR_W),
                SlotRole.IMAGE,
                note="二维码/品牌图",
            ),
            _slot(
                "accent",
                Rect(
                    _SP.safe_left,
                    round(_BODY_BOTTOM - 0.2, _PRECISION),
                    4.0,
                    DEFAULT_THEME.shapes.accent_bar_in,
                ),
                SlotRole.DECORATION,
            ),
            _footer_slot(),
        ),
    )
    return {Density.LIGHT: light, Density.HEAVY: heavy}


#: 6 个原型 × 2 档密度 = 12 个版式。**这是唯一的权威表**，别在别处再写一份。
LAYOUTS: Final[dict[tuple[Archetype, Density], Layout]] = {
    **{(Archetype.COVER, k): v for k, v in _cover().items()},
    **{(Archetype.SECTION, k): v for k, v in _section().items()},
    **{(Archetype.BULLETS, k): v for k, v in _bullets().items()},
    **{(Archetype.SPLIT, k): v for k, v in _split().items()},
    **{(Archetype.DATA, k): v for k, v in _data().items()},
    **{(Archetype.CLOSING, k): v for k, v in _closing().items()},
}

ARCHETYPES: Final[tuple[Archetype, ...]] = tuple(Archetype)
DENSITIES: Final[tuple[Density, ...]] = (Density.LIGHT, Density.HEAVY)


def get_layout(archetype: Archetype, density: Density) -> Layout:
    """取版式。``AUTO`` 在这里不允许——密度必须由映射层先落定。"""
    if density is Density.AUTO:
        raise ValueError("密度 AUTO 要先由 content_map 落成 light/heavy，版式层不接受 AUTO")
    try:
        return LAYOUTS[(archetype, density)]
    except KeyError as exc:  # pragma: no cover - 枚举封闭，构造表时就该覆盖全
        raise KeyError(f"没有原型 {archetype.value}/{density.value}") from exc


def iter_layouts() -> Iterator[Layout]:
    """按 原型 → 密度 的固定顺序遍历 12 个版式（报告与核对都用这个顺序）。"""
    for archetype in ARCHETYPES:
        for density in DENSITIES:
            yield LAYOUTS[(archetype, density)]


def safe_rect(spacing: Spacing | None = None) -> Rect:
    """安全区（当前间距下的）。给测试与报告用，避免它们各算一遍。"""
    space = spacing or _SP
    return Rect(
        space.safe_left,
        space.safe_top,
        space.safe_right - space.safe_left,
        space.safe_bottom - space.safe_top,
    )


def canvas_rect() -> Rect:
    return Rect(0.0, 0.0, SLIDE_WIDTH_IN, SLIDE_HEIGHT_IN)


def validate_layouts(theme: ThemeTokens | None = None) -> list[str]:
    """机械核对 12 个版式：**面积交集为空、不越界、不出安全区**。

    返回人话描述的违规清单（空列表 = 通过）。这里是纯几何判定，
    所以它可以做得很严，而且**不必靠谁记得**——单测与 CI 都会调它。

    只核对默认主题下的几何：换主题只换色与字体，间距若改了会同时改变安全区，
    那时用 ``theme`` 参数传入并核对（见 `test_layouts.py`）。
    """
    violations: list[str] = []
    canvas = canvas_rect()
    safe = safe_rect(theme.spacing if theme else None)

    for layout in iter_layouts():
        seen: dict[str, Slot] = {}
        for slot in layout.slots:
            if slot.name in seen:
                violations.append(f"[{layout.key}] 槽位名重复：{slot.name}")
            seen[slot.name] = slot

            if slot.rect.w <= 0 or slot.rect.h <= 0:
                violations.append(f"[{layout.key}] 槽位「{slot.name}」尺寸非正：{slot.rect}")
            if not canvas.contains(slot.rect):
                violations.append(
                    f"[{layout.key}] 槽位「{slot.name}」{slot.rect} 越出画布 "
                    f"{SLIDE_WIDTH_IN}×{SLIDE_HEIGHT_IN}"
                )
            if not safe.contains(slot.rect):
                violations.append(
                    f"[{layout.key}] 槽位「{slot.name}」{slot.rect} 越出安全区 {safe}"
                )
            if slot.is_text and not slot.font_role:
                violations.append(f"[{layout.key}] 文字槽「{slot.name}」没有 font_role")

        names = layout.names
        if layout.title_slot not in names:
            violations.append(f"[{layout.key}] 缺少标题槽「{layout.title_slot}」")
        if layout.footer_slot not in names:
            violations.append(f"[{layout.key}] 缺少页脚槽「{layout.footer_slot}」")
        for column in layout.columns:
            if column not in names:
                violations.append(f"[{layout.key}] 声明的分栏「{column}」没有对应槽位")

        for i, left in enumerate(layout.slots):
            for right in layout.slots[i + 1 :]:
                area = left.rect.intersection_area(right.rect)
                if area > _AREA_EPS:
                    violations.append(
                        f"[{layout.key}] 槽位重叠：「{left.name}」{left.rect} × "
                        f"「{right.name}」{right.rect}，交集面积 {area:.4f} in²"
                    )
    return violations


def layout_rows() -> list[str]:
    """每个版式一行摘要（槽位数、列数、视觉元素数）——给报告与 `__main__` 用。"""
    rows: list[str] = []
    for layout in iter_layouts():
        visuals = [s.name for s in layout.slots if s.is_visual]
        rows.append(
            f"{layout.key:<16} 槽位 {len(layout.slots):>2} 个  "
            f"分栏 {len(layout.columns)}  视觉元素槽 {'、'.join(visuals) or '（原型装饰保证）'}"
        )
    return rows


def _main() -> int:  # pragma: no cover - 开发期手工核对入口
    """``python -m app.services.deck.layouts``：打印 12 个版式与机械核对结果。"""
    print(f"画布 {SLIDE_WIDTH_IN} × {SLIDE_HEIGHT_IN} 英寸；安全区 {safe_rect()}")
    for row in layout_rows():
        print("  " + row)
    print("\n槽位几何：")
    for layout in iter_layouts():
        print(f"  {layout.key}")
        for slot in layout.slots:
            print(f"    {slot.name:<10}{slot.rect}  {slot.role.value:<11}lines<={slot.max_lines}")
    problems = validate_layouts()
    if problems:
        print(f"\n不通过：{len(problems)} 条")
        for problem in problems:
            print("  - " + problem)
        return 1
    print("\n通过：12 个版式的全部槽位互不重叠、不越界、不出安全区")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
