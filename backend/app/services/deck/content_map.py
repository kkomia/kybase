"""幻灯片生成·第三层：内容映射（deck spec → 每页每槽填什么 + 溢出决策）。

**这一层是"内容与展示之间的那道翻译"**：左边是结构化数据（`spec.DeckSpec`），
右边是槽位（`layouts`），中间要回答两个问题——

1. **每页每槽填什么**：标题进标题槽、要点进正文或两栏、图表进 ``data``、
   一句结论进 ``quote``、图片进 ``image``、指标卡进 ``kpi_n``。给了的载荷**必须
   落进某个槽**（落不进去就换密度，见 :func:`pick_density`，绝不静默丢弃）。
2. **溢出怎么办**：文字比槽多的时候，**必须由这一层做决定**。
   不能指望 OOXML 的 autofit：Google Slides API 里它只读，PptxGenJS 的 ``fit``
   只在"编辑过之后"才生效——把溢出交给它等于把版式交给运气。

## 溢出的判据与下限（阶梯**就是这一段**）

阶梯：**缩字号到下限 → 拆页 → 两栏**，每一步都有明确判据：

| 步 | 判据 | 结果 |
| --- | --- | --- |
| 1 保留 | 按声明字号算出的总行数 ≤ 槽容量 | ``keep`` |
| 2 缩字号 | 降到某一档（18→16→14）后装得下 | ``shrink``，记录落到的字号 |
| 3 拆页 | 条目**彼此独立**（要点就是这种）且按最小字号切成的每一页都装得下 | ``split``，切成 N 页 |
| 4 两栏 | 版式有两栏槽位，且把条目（或一条长文本按句切开）分到两栏后装得下 | ``columns`` |
| 5 装不下 | 以上都不成立 | ``warn``：**不截断、不静默丢字**，把这件事报到计划里 |

第 3 步与第 4 步的先后是刻意的：**"少一页"不是目标，"看得清"才是**。所以先试拆页
（拆开各页都保持正常字号），再试两栏（同一页里字号可能已经压到下限）。
而"这一页该用单栏还是双栏（light/heavy）"这个问题**在密度选择时就答完了**
（:func:`pick_density`）——内容多的页从一开始就选 heavy 双栏，不是等到溢出才补救。

下限之所以是 14pt：投影到会议室最后一排，正文低于 14pt 就读不了。**到下限还装不下，
正确动作是拆页或换页型，不是继续缩**。

## 度量：为什么不用库

判据里最核心的量是"这段文字要占多宽"。算它有三种做法：装 fontTools 量真实字体（新依赖）、
调渲染器试排（要把写盘层接上才知道）、或者用**按类别的字宽系数**估。
这里选第三种：**零依赖、可解释、可标定**。系数的来源与标定过程写在
`theme_tokens` 的模块头（fontTools 读 ``hmtx``，样本覆盖 CJK/全角标点/ASCII 各类，
回代误差稳定在 +5%–6%，正好落在"最宽字体 + 6%"这条预算上）。

于是每行可放字数这条公式是**具名的、可复查的**：

    每行可放字数 ≈ 槽宽(in) × 72 × 断行余量 ÷ (字号pt × 中文字宽系数 × 替换余量)

即 :func:`chars_per_line`。中文按 1.0 em（全宽），中英混排按每类字符的系数加权
（:func:`measure_width_em`），所以"混排比纯中文放得下更多字"这件事是自然结果，
不是另写一条规则。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Final

from app.services.deck.layouts import (
    ARCHETYPE_LABELS,
    Archetype,
    Density,
    Layout,
    Rect,
    Slot,
    SlotRole,
    get_layout,
)
from app.services.deck.spec import (
    DeckSpec,
    ImageIntent,
    Kpi,
    SlideSpec,
    load_deck_spec,
)
from app.services.deck.theme_tokens import (
    DEFAULT_THEME,
    POINTS_PER_INCH,
    SLIDE_HEIGHT_IN,
    SLIDE_WIDTH_IN,
    ThemeTokens,
)

__all__ = [
    "ACTION_LABELS",
    "LIGHT_BULLETS_MAX",
    "OVERFLOW_CASES",
    "BlockPage",
    "BlockPlan",
    "DeckPlan",
    "OverflowAction",
    "OverflowCase",
    "PagePlan",
    "SlotDecision",
    "SlotFill",
    "chars_per_line",
    "fit_block",
    "glyph_em",
    "map_deck",
    "measure_width_em",
    "pick_density",
    "run_overflow_cases",
    "text_width_em",
    "wrap_lines",
]

#: 密度选择的阈值（内容量超过它就上 heavy 双栏）。三个数字都是**可调的一处**：
#: 它们只影响"什么时候开始用密版"，不影响任何几何。
LIGHT_BULLETS_MAX: Final[int] = 4
HEAVY_TEXT_CHARS: Final[int] = 220
HEAVY_CHART_SERIES: Final[int] = 3
HEAVY_CHART_CATEGORIES: Final[int] = 8

#: 类别过多时的提醒阈值（轴标签会挤在一起——这是图表的常见丑法）。
CHART_CATEGORY_HINT: Final[int] = 12
PIE_CATEGORY_HINT: Final[int] = 7

#: 全角但不在 CJK 码位区里的那些字符（破折号、省略号、弯引号、间隔号）。
#: 它们在东亚字体里同样是整全宽，量宽度时不能按西文算。
_FULLWIDTH_EXTRA: Final[frozenset[str]] = frozenset("—–…“”‘’·")

#: 少数"同样占满一格"的符号（参考标记 U+203B、小节号 U+00A7）按**码位**列出来，
#: 不写成字符字面量：仓库的源码 emoji 扫描（`scripts/scan_emoji.py`）会把这几个符号
#: 当成 emoji 拦下来，而它们在度量上确实占整全宽。按码位写既过了扫描，也保住了分类。
_FULLWIDTH_EXTRA_CODES: Final[frozenset[int]] = frozenset({0x203B, 0x00A7})

#: 断行时允许在此字符**之后**断开的字符（西文在空格后断，汉字逐字可断）。
_ASCII_BREAK_AFTER: Final[frozenset[str]] = frozenset(" -/")


class OverflowAction(StrEnum):
    """溢出决策的结果。``warn`` 不是"失败了"，是"这一页按现有内容摆不下，请人工定夺"。"""

    KEEP = "keep"
    SHRINK = "shrink"
    SPLIT = "split"
    COLUMNS = "columns"
    WARN = "warn"


ACTION_LABELS: Final[dict[OverflowAction, str]] = {
    OverflowAction.KEEP: "保留",
    OverflowAction.SHRINK: "缩字号",
    OverflowAction.SPLIT: "拆页",
    OverflowAction.COLUMNS: "两栏",
    OverflowAction.WARN: "装不下",
}


# ------------------------------------------------------------------ 度量


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # 基本区
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0xF900 <= code <= 0xFAFF  # 兼容汉字
        or 0x3040 <= code <= 0x30FF  # 平假名/片假名
        or 0xAC00 <= code <= 0xD7AF  # 谚文
    )


def glyph_em(char: str, theme: ThemeTokens | None = None) -> float:
    """一个字符占几倍字号（em）。分类规则与标定样本一一对应，
    见 `theme_tokens` 模块头那张表。"""
    measure = (theme or DEFAULT_THEME).measure
    code = ord(char)
    if char == " ":
        return measure.space_em
    if char == "\u3000":  # 全角空格
        return measure.fullwidth_punct_em
    if _is_cjk(char):
        return measure.cjk_em
    if (
        0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
        or char in _FULLWIDTH_EXTRA
        or code in _FULLWIDTH_EXTRA_CODES
    ):
        return measure.fullwidth_punct_em
    if char.isupper():
        return measure.latin_upper_em
    if char.islower():
        return measure.latin_lower_em
    if char.isdigit():
        return measure.latin_digit_em
    return measure.latin_punct_em


def text_width_em(text: str, theme: ThemeTokens | None = None) -> float:
    """文字的**原始** em 宽度（不含字体替换余量）。"""
    token = theme or DEFAULT_THEME
    return sum(glyph_em(char, token) for char in text)


def measure_width_em(text: str, theme: ThemeTokens | None = None) -> float:
    """**预算**宽度（em）：原始宽度 × 替换余量。

    为什么要乘这一下：目标机缺白名单字体时 PowerPoint 不报错、直接替换，
    中文实测再宽约 6%。乘在这里，后面所有容量判断就都自动带了这档余量——
    比在每个调用点各写一次 `* 1.06` 可靠。
    """
    token = theme or DEFAULT_THEME
    return text_width_em(text, token) * token.measure.font_width_margin


def chars_per_line(
    width_in: float,
    font_pt: float,
    theme: ThemeTokens | None = None,
    *,
    char_em: float | None = None,
) -> float:
    """槽宽与字号能放多少个字——**具名公式**，溢出判据的入口。

    ``每行可放字数 ≈ 槽宽(in) × 72 × 断行余量 ÷ (字号pt × 字宽系数 × 替换余量)``

    ``char_em`` 默认取中文字宽系数（1.0 em，即"一个汉字 = 一个字号宽"）。
    混排的精确算法在 :func:`wrap_lines`（逐字符加权），这个函数给的是**人话估算**：
    用来解释判据、估页数、给报告看。
    """
    token = theme or DEFAULT_THEME
    measure = token.measure
    ratio = measure.cjk_em if char_em is None else char_em
    usable_pt = width_in * POINTS_PER_INCH * measure.fit_slack
    return usable_pt / (font_pt * ratio * measure.font_width_margin)


def _usable_em(slot: Slot, font_pt: float, theme: ThemeTokens, *, indent_em: float = 0.0) -> float:
    """槽内可用宽度的**预算 em**（已含 inset、断行余量、字体替换余量）。

    统一在这里收敛，是为了让 `wrap_lines` 只管"排",不用关心盒模型。
    """
    measure = theme.measure
    text_w_in = max(0.0, slot.rect.w - 2 * measure.inset_x_in)
    usable_pt = text_w_in * POINTS_PER_INCH * measure.fit_slack
    budget = usable_pt / (font_pt * measure.font_width_margin)
    return max(0.2, budget - indent_em)


def _indent_em(slot: Slot, theme: ThemeTokens) -> float:
    """悬挂缩进**只对列表槽生效**。

    列表（要点）左边有项目符号，续行要对齐到文字起始处，所以要扣掉一个符号的宽度。
    标题与正文是单段文字，没有这个前缀——给它们扣会白丢 1.15 em，
    实测把一个 31 字的标题从"两行"算成"三行"，于是误报"装不下"。
    """
    return theme.spacing.bullet_indent_em if slot.role is SlotRole.LIST else 0.0


def _breakable_after(char: str) -> bool:
    return _is_cjk(char) or char in _ASCII_BREAK_AFTER


def _take_line(text: str, budget_em: float, theme: ThemeTokens) -> tuple[str, str]:
    """取一行：贪心累加字符宽度，尽量在"可断点"上断，断不了就硬断（至少留一个字符）。"""
    width = 0.0
    limit = 0
    last_break = 0
    last_break_width = 0.0
    for index, char in enumerate(text):
        char_width = glyph_em(char, theme)
        if width + char_width > budget_em and limit > 0:
            break
        width += char_width
        limit = index + 1
        if _breakable_after(char):
            last_break = limit
            last_break_width = width
    if limit <= 0:  # 一个字符都放不下（槽太窄或字号太大）：硬留一个，避免死循环
        return text[:1], text[1:]

    cut = limit
    if limit < len(text) and last_break > 0 and last_break_width >= 0.6 * budget_em:
        cut = last_break  # 在可断点上断，别把西文单词劈开
    line = text[:cut].rstrip()
    rest = text[cut:]
    return (line, rest) if line else (text[:cut], rest)


def wrap_lines(
    text: str,
    max_em: float,
    theme: ThemeTokens | None = None,
    *,
    indent_em: float = 0.0,
) -> list[str]:
    """按**预算宽度**（em）断行，返回每一行的文字。

    ``indent_em`` 是悬挂缩进：要点第二行起要缩进到文字起始处（项目符号占的宽度），
    所以续行少这么多宽度。``\\n`` 是硬换行。

    这是**估算**，不是排版引擎：它保证"行数不会少算"，不保证与渲染器逐字一致
    ——这正是 `fit_slack`（只用到 97% 宽度）留出来要吸收的差。
    """
    token = theme or DEFAULT_THEME
    lines: list[str] = []
    for paragraph in text.split("\n"):
        remaining = paragraph.strip()
        if not remaining:
            lines.append("")
            continue
        first = True
        while remaining:
            budget = max(0.2, max_em - (0.0 if first else indent_em))
            line, remaining = _take_line(remaining, budget, token)
            lines.append(line)
            remaining = remaining.lstrip()
            first = False
    return lines


def _line_height_in(font_pt: float, theme: ThemeTokens) -> float:
    return font_pt * theme.measure.line_spacing / POINTS_PER_INCH


def _capacity_lines(slot: Slot, font_pt: float, theme: ThemeTokens, item_count: int) -> int:
    """槽在某个字号下能放几行：取"几何容量"与"版式硬上限"里的小者。

    段落间距要先从可用高度里扣掉（少扣了就会多算一行，而那正好是溢出）。
    """
    measure = theme.measure
    usable_h = max(0.0, slot.rect.h - 2 * measure.inset_y_in)
    gaps = max(0, item_count - 1) * theme.spacing.para_gap_in
    line_h = _line_height_in(font_pt, theme)
    if usable_h - gaps < line_h:
        return 0
    return max(0, min(slot.max_lines, math.floor((usable_h - gaps) / line_h)))


def _wrap_item(item: str, slot: Slot, font_pt: float, theme: ThemeTokens) -> list[str]:
    """一条文字在某个槽里的行（含悬挂缩进规则）。所有行数统计都走这里，
    免得"哪里该缩进"这件事有一处漏了就前后不一致。"""
    indent = _indent_em(slot, theme)
    budget = _usable_em(slot, font_pt, theme, indent_em=indent)
    return wrap_lines(item, budget, theme, indent_em=indent)


def _count_lines(
    items: Sequence[str], slots: Sequence[Slot], font_pt: float, theme: ThemeTokens
) -> list[int]:
    """每个槽（栏）里要放的行数。

    单栏时全部条目都进那一栏；双栏时按条目顺序对半分（前一半进左栏）。
    """
    if not slots:
        return []
    if len(slots) == 1:
        return [sum(len(_wrap_item(item, slots[0], font_pt, theme)) for item in items)]
    half = math.ceil(len(items) / 2)
    groups: tuple[Sequence[str], ...] = (items[:half], items[half:])
    return [
        sum(len(_wrap_item(item, slot, font_pt, theme)) for item in group)
        for slot, group in zip(slots, groups, strict=True)
    ]


# -------------------------------------------------------------- 决策产物


@dataclass(frozen=True)
class SlotDecision:
    """一次溢出决策的记录。**它要能回答"为什么最后是这个字号/为什么拆了两页"**。"""

    slot: str
    action: OverflowAction
    font_size_pt: int
    lines: tuple[int, ...]
    capacity: tuple[int, ...]
    detail: str = ""

    @property
    def overflow(self) -> bool:
        return self.action is OverflowAction.WARN

    def __str__(self) -> str:
        lines = "/".join(str(item) for item in self.lines)
        capacity = "/".join(str(item) for item in self.capacity)
        return (
            f"{self.slot}: {ACTION_LABELS[self.action]} → {self.font_size_pt}pt "
            f"{lines}行/容量{capacity}" + (f"（{self.detail}）" if self.detail else "")
        )


@dataclass(frozen=True)
class BlockPage:
    """一页里的这一段内容：每栏各装哪些条目、各占几行。"""

    columns: tuple[tuple[str, ...], ...]
    lines: tuple[int, ...]


@dataclass(frozen=True)
class BlockPlan:
    """一段文字内容的落地方式：决策 + 分页 + 每页每栏的条目。"""

    action: OverflowAction
    font_size_pt: int
    pages: tuple[BlockPage, ...]
    capacity: tuple[int, ...] = ()
    detail: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class SlotFill:
    """一个槽位最终的填充。``payload`` 就是写盘层要画的东西（文字/列表/图表/图片/指标卡）。"""

    slot: str
    kind: str
    payload: Any = None
    font_size_pt: int | None = None
    lines: int = 0
    action: OverflowAction = OverflowAction.KEEP

    def summary(self) -> str:
        size = f"{self.font_size_pt}pt " if self.font_size_pt else ""
        lines = f"{self.lines}行 " if self.lines else ""
        return f"{self.slot:<8}{size}{lines}{self.kind}"


@dataclass(frozen=True)
class PagePlan:
    """一页的完整方案：版式、每个槽填什么、做过哪些溢出决策。"""

    index: int
    archetype: Archetype
    density: Density
    title: str
    fills: tuple[SlotFill, ...]
    decisions: tuple[SlotDecision, ...]
    warnings: tuple[str, ...] = ()
    speaker_notes: str = ""
    continuation: bool = False

    @property
    def layout(self) -> Layout:
        return get_layout(self.archetype, self.density)

    @property
    def key(self) -> str:
        return f"{self.archetype.value}/{self.density.value}"

    @property
    def visual_elements(self) -> tuple[str, ...]:
        """本页的非文字元素（图表/图片/图标/指标卡）。原型装饰（强调条/序号/页脚）
        不算在这里——它由写盘层保证绘制，见 `layouts.Layout.guaranteed_visual`。"""
        visual_kinds = ("chart", "image", "icons", "kpi")
        return tuple(fill.slot for fill in self.fills if fill.kind in visual_kinds)

    def fill(self, slot: str) -> SlotFill | None:
        for candidate in self.fills:
            if candidate.slot == slot:
                return candidate
        return None

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class DeckPlan:
    """整份 deck 的方案。``to_dict()`` 就是**写盘层的输入**（JSON 可直传）。"""

    title: str
    theme: ThemeTokens
    pages: tuple[PagePlan, ...]
    warnings: tuple[str, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_dict(self) -> dict[str, Any]:
        """写盘层要的全部东西：画布 + 令牌 + 每页每槽的矩形与内容。

        几何在这里**已经算好**（英寸与 EMU 两套都给），所以 Node 侧不需要再实现
        一遍版式——它只负责画。这也是"内容与展示两段式"的落点：Python 决定画什么、
        画在哪，写盘层只决定怎么落成 OOXML。
        """
        return {
            "deck": {"title": self.title, "page_count": self.page_count},
            "canvas": {
                "width_in": SLIDE_WIDTH_IN,
                "height_in": SLIDE_HEIGHT_IN,
                "note": (
                    "必须显式设置画布尺寸：库默认 16:9 是 10×5.625，"
                    "按 13.333 写坐标会被静默裁掉页脚与图表轴"
                ),
            },
            "theme": _theme_to_dict(self.theme),
            "known_gaps": [
                self.theme.chart_font_gap,
                "不支持嵌入字体：目标机缺字体时被替换，宽度按最宽字体 + 6% 预留",
                (
                    "OOXML autofit 不可靠（Google API 只读、PptxGenJS 仅在编辑后生效）："
                    "溢出已在映射层决定"
                ),
            ],
            "warnings": list(self.warnings),
            "pages": [_page_to_dict(page) for page in self.pages],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)

    def summary(self, *, with_slots: bool = True) -> str:
        """给人看的一行行报告（验收与排查都读它）。"""
        lines = [f"《{self.title}》 {self.page_count} 页 · 主题 {self.theme.name}"]
        if self.warnings:
            lines.extend(f"  ! {note}" for note in self.warnings)
        for page in self.pages:
            mark = "（续）" if page.continuation else ""
            lines.append(f"  第 {page.index:>2} 页  {page.key:<14}{page.title}{mark}")
            if with_slots:
                for fill in page.fills:
                    lines.append(f"        {fill.summary()}")
            for decision in page.decisions:
                if decision.action is not OverflowAction.KEEP:
                    lines.append(f"        -> {decision}")
            for warning in page.warnings:
                lines.append(f"        ! {warning}")
        return "\n".join(lines)


def _theme_to_dict(theme: ThemeTokens) -> dict[str, Any]:
    payload = {
        "name": theme.name,
        "palette": asdict(theme.palette),
        "fonts": {
            "title": list(theme.fonts.title),
            "body": list(theme.fonts.body),
            "latin": theme.fonts.latin,
        },
        "type_scale": asdict(theme.type_scale),
        "spacing": asdict(theme.spacing),
        "shapes": asdict(theme.shapes),
        "measure": asdict(theme.measure),
    }
    return payload


def _fill_to_dict(fill: SlotFill) -> dict[str, Any]:
    return {
        "slot": fill.slot,
        "kind": fill.kind,
        "font_size_pt": fill.font_size_pt,
        "lines": fill.lines,
        "action": fill.action.value,
        "payload": _payload_to_dict(fill.payload),
    }


def _payload_to_dict(payload: Any) -> Any:
    if payload is None or isinstance(payload, (str, int, float, bool)):
        return payload
    if isinstance(payload, (list, tuple)):
        return [_payload_to_dict(item) for item in payload]
    if isinstance(payload, (ImageIntent, Kpi)):
        return payload.model_dump(mode="json")
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return str(payload)


def _page_to_dict(page: PagePlan) -> dict[str, Any]:
    layout = page.layout
    return {
        "index": page.index,
        "archetype": page.archetype.value,
        "archetype_label": ARCHETYPE_LABELS[page.archetype],
        "density": page.density.value,
        "layout": page.key,
        "title": page.title,
        "continuation": page.continuation,
        "speaker_notes": page.speaker_notes,
        "visual_elements": list(page.visual_elements),
        "decorations": [slot.name for slot in layout.slots if slot.role.value == "decoration"],
        "warnings": list(page.warnings),
        "slots": [
            {
                **_fill_to_dict(fill),
                "rect_in": layout.slot(fill.slot).rect.as_dict(),
                "rect_emu": layout.slot(fill.slot).rect.to_emu(),
                "role": layout.slot(fill.slot).role.value,
                "font_role": layout.slot(fill.slot).font_role,
                "align": layout.slot(fill.slot).align,
            }
            for fill in page.fills
        ],
    }


# ------------------------------------------------------------ 溢出决策


def fit_block(
    items: Sequence[str],
    slots: Sequence[Slot],
    theme: ThemeTokens | None = None,
    *,
    steps: Sequence[int] | None = None,
) -> BlockPlan:
    """一段文字内容（若干条目 + 它可用的槽位）→ 落地方式。

    ``slots`` 是**这一块能用的文字槽**：一个就是单栏，两个就是双栏（heavy 档）。
    阶梯见模块头：缩字号 → 拆页 → 两栏 → 装不下就报出来。
    """
    token = theme or DEFAULT_THEME
    size_steps = tuple(steps) if steps else token.type_scale.body_steps
    usable = tuple(slots)
    if not usable or not items:
        return BlockPlan(
            action=OverflowAction.KEEP,
            font_size_pt=size_steps[0],
            pages=(BlockPage(columns=tuple(() for _ in usable), lines=tuple(0 for _ in usable)),),
        )

    # 1) 保留 / 2) 缩字号
    for position, size in enumerate(size_steps):
        if not _fits(items, usable, size, token):
            continue
        action = OverflowAction.KEEP if position == 0 else OverflowAction.SHRINK
        return BlockPlan(
            action=action,
            font_size_pt=size,
            pages=(
                BlockPage(
                    columns=_distribute(items, len(usable)),
                    lines=_count_lines(items, usable, size, token),
                ),
            ),
            capacity=_capacities(usable, size, token, len(items)),
            detail="" if action is OverflowAction.KEEP else f"从 {size_steps[0]}pt 降下来",
        )

    min_size = size_steps[-1]

    # 3) 拆页：条目彼此独立时切成多页，每页仍按**最小字号**（不再往下缩）
    if len(items) >= 2:
        split = _split_pages(items, usable, min_size, token)
        if split is not None and len(split) > 1:
            return BlockPlan(
                action=OverflowAction.SPLIT,
                font_size_pt=min_size,
                pages=split,
                capacity=_capacities(
                    usable, min_size, token, _items_per_column(len(items), len(usable))
                ),
                detail=f"{len(items)} 条按最小字号 {min_size}pt 切到 {len(split)} 页",
            )

    # 4) 两栏：版式有两栏槽位时，把条目（或一条长文本按句切开）分到两栏
    if len(usable) == 2:
        columns = _two_columns(items, min_size, (usable[0], usable[1]), token)
        if columns is not None:
            lines = tuple(
                _block_lines(part, slot, min_size, token)
                for part, slot in zip(columns, usable, strict=True)
            )
            capacity = _capacities(usable, min_size, token, max(len(part) for part in columns) or 1)
            if all(line <= cap for line, cap in zip(lines, capacity, strict=True)):
                return BlockPlan(
                    action=OverflowAction.COLUMNS,
                    font_size_pt=min_size,
                    pages=(BlockPage(columns=columns, lines=lines),),
                    capacity=capacity,
                    detail=f"按最小字号 {min_size}pt 分两栏",
                )

    # 5) 装不下：如实报出来（不截断、不丢字）
    lines = _count_lines(items, usable, min_size, token)
    capacity = _capacities(usable, min_size, token, len(items))
    return BlockPlan(
        action=OverflowAction.WARN,
        font_size_pt=min_size,
        pages=(BlockPage(columns=_distribute(items, len(usable)), lines=lines),),
        capacity=capacity,
        detail=(
            f"压到下限 {min_size}pt 仍要 {'/'.join(str(item) for item in lines)} 行，"
            f"容量 {'/'.join(str(item) for item in capacity)} 行：请缩短内容或换页型"
        ),
    )


def _items_per_column(item_count: int, column_count: int) -> int:
    if column_count <= 1:
        return item_count
    return max(1, math.ceil(item_count / column_count))


def _capacities(
    slots: Sequence[Slot], font_pt: float, theme: ThemeTokens, item_count: int
) -> tuple[int, ...]:
    per_column = _items_per_column(item_count, len(slots))
    return tuple(_capacity_lines(slot, font_pt, theme, per_column) for slot in slots)


def _fits(items: Sequence[str], slots: Sequence[Slot], font_pt: float, theme: ThemeTokens) -> bool:
    """这批条目在这个字号下装不装得下（按版式提供的栏数一起算）。"""
    lines = _count_lines(items, slots, font_pt, theme)
    capacity = _capacities(slots, font_pt, theme, len(items))
    return all(line <= cap for line, cap in zip(lines, capacity, strict=True))


def _distribute(items: Sequence[str], column_count: int) -> tuple[tuple[str, ...], ...]:
    """把条目分到栏里（单栏就是全部）。"""
    if column_count <= 1:
        return (tuple(items),)
    half = math.ceil(len(items) / 2)
    return (tuple(items[:half]), tuple(items[half:]))


def _block_lines(items: Sequence[str], slot: Slot, font_pt: float, theme: ThemeTokens) -> int:
    """一栏里这些条目一共占几行（`_count_lines` 的单槽版本）。"""
    return sum(len(_wrap_item(item, slot, font_pt, theme)) for item in items)


def _split_pages(
    items: Sequence[str], slots: Sequence[Slot], font_pt: float, theme: ThemeTokens
) -> tuple[BlockPage, ...] | None:
    """把条目贪心切成多页，每页都按**版式提供的栏数**装（切完仍分栏）。

    只要有**任何一条单独**都装不下（一条占满一页还超），就返回 ``None``
    ——那时拆页不解决问题，该走两栏或人工定夺，而不是切出一页塞一个字。
    """
    pages: list[tuple[str, ...]] = []
    current: list[str] = []
    for item in items:
        if not current:
            if not _fits([item], slots, font_pt, theme):
                return None
            current = [item]
            continue
        if _fits([*current, item], slots, font_pt, theme):
            current = [*current, item]
            continue
        pages.append(tuple(current))
        if not _fits([item], slots, font_pt, theme):
            return None
        current = [item]
    if current:
        pages.append(tuple(current))
    return tuple(
        BlockPage(
            columns=_distribute(page, len(slots)),
            lines=_count_lines(page, slots, font_pt, theme),
        )
        for page in pages
    )


def _two_columns(
    items: Sequence[str], font_pt: float, slots: tuple[Slot, Slot], theme: ThemeTokens
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """两栏的两种来路：条目对半分；只有一条时按句切开（长句点正好适合分两栏）。"""
    if len(items) >= 2:
        return _distribute(items, 2)
    if len(items) == 1:
        parts = _split_sentences(items[0])
        if len(parts) >= 2:
            half = math.ceil(len(parts) / 2)
            left = "".join(parts[:half])
            right = "".join(parts[half:])
            if left and right:
                return (left,), (right,)
    return None


#: 句末标点：按句切分是"一条长文本分两栏"的切点（切在句末，语义不会断）。
_SENTENCE_END: Final[tuple[str, ...]] = ("。", "！", "？", "；", ".", "!", "?", ";")


def _split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in _SENTENCE_END:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return parts


# ---------------------------------------------------------------- 密度


def pick_density(slide: SlideSpec) -> Density:
    """按**内容密度**挑 light 还是 heavy。三条规则，按优先级：

    1. **载荷优先**：给了要点/图片/指标卡/图表/结语，而 light 档没有对应的槽位
       （按**角色**判定） → 必须 heavy。否则那些东西会被静默丢掉——
       那是这类接口最典型的失败：模型给了，没人看。实测踩过一次：
       章节页给了三条要点，而 light 档没有列表槽，那三条要点就这么消失了。
    2. **量级判据**：要点条数、正文长度、图表系列/类别数超阈值 → heavy。
    3. 其余 → light（疏版更好读，不要默认选密版）。
    """
    light = get_layout(slide.archetype, Density.LIGHT)
    heavy = get_layout(slide.archetype, Density.HEAVY)

    needed_roles = {
        role
        for role, wanted in (
            (SlotRole.LIST, bool(slide.bullets or slide.body)),
            (SlotRole.CHART, slide.chart is not None),
            (SlotRole.IMAGE, slide.image is not None),
            (SlotRole.KPI, bool(slide.kpis)),
        )
        if wanted
    }
    # 结语/结论不是"角色"而是具体槽位名（它可以是正文也可以是引语），单独判
    needed_names = {"quote"} if (slide.quote and slide.chart is None) else set()

    def supports(layout: Layout) -> bool:
        roles = {slot.role for slot in layout.slots}
        return needed_roles <= roles and needed_names <= set(layout.names)

    if (needed_roles or needed_names) and not supports(light) and supports(heavy):
        return Density.HEAVY

    body_text = "".join(slide.bullets) + slide.body
    if len(slide.bullets) > LIGHT_BULLETS_MAX:
        return Density.HEAVY
    if len(body_text) > HEAVY_TEXT_CHARS:
        return Density.HEAVY
    if slide.chart is not None and (
        len(slide.chart.series) >= HEAVY_CHART_SERIES
        or len(slide.chart.categories) > HEAVY_CHART_CATEGORIES
    ):
        return Density.HEAVY
    if slide.image is not None and body_text:
        return Density.HEAVY
    if slide.kpis:
        return Density.HEAVY
    return Density.LIGHT


# ------------------------------------------------------------------ 映射


def _decoration_fills(layout: Layout, *, page_number: int) -> list[SlotFill]:
    """装饰槽的填充：页脚（页码）+ 强调条。

    强调条是"原型保证的视觉元素"（`Layout.guaranteed_visual`），所以它**必须每页都填**
    ——漏一个，那一页在定义上就变成了"没有视觉元素"。
    大序号（``number``）不在这里：它是**文字**槽（要按字号量宽度），走 `_map_slide` 的
    文字填充。
    """
    fills: list[SlotFill] = []
    for slot in layout.slots:
        if slot.role.value != "decoration":
            continue
        if slot.name == "footer":
            fills.append(
                SlotFill(slot="footer", kind="decoration", payload={"page_number": page_number})
            )
        else:
            fills.append(SlotFill(slot=slot.name, kind="decoration", payload="accent-bar"))
    return fills


def _text_fill(
    slot: Slot,
    items: Sequence[str],
    theme: ThemeTokens,
    *,
    steps: Sequence[int] | None = None,
    columns: Sequence[Slot] | None = None,
) -> tuple[list[SlotFill], list[SlotDecision], list[str]]:
    """一段文字 → 槽填充 + 决策 + 警告。多页由调用方处理（这里只看一页）。"""
    targets = tuple(columns) if columns else (slot,)
    plan = fit_block(items, targets, theme, steps=steps)
    page = plan.pages[0]
    fills: list[SlotFill] = []
    for target, content, line_count in zip(targets, page.columns, page.lines, strict=True):
        if not content:
            continue
        fills.append(
            SlotFill(
                slot=target.name,
                kind="list" if len(content) > 1 else "text",
                payload=tuple(content) if len(content) > 1 else content[0],
                font_size_pt=plan.font_size_pt,
                lines=line_count,
                action=plan.action,
            )
        )
    decision = SlotDecision(
        slot=fills[0].slot if len(fills) == 1 else "+".join(f.slot for f in fills),
        action=plan.action,
        font_size_pt=plan.font_size_pt,
        lines=page.lines,
        capacity=plan.capacity,
        detail=plan.detail,
    )
    warnings: list[str] = []
    if plan.action is OverflowAction.WARN:
        warnings.append(f"「{decision.slot}」{plan.detail}")
    return fills, [decision], warnings


def _chart_fills(
    slide: SlideSpec, layout: Layout, theme: ThemeTokens
) -> tuple[list[SlotFill], list[str]]:
    chart = slide.chart
    if chart is None:
        return [], []
    warnings: list[str] = []
    limit = PIE_CATEGORY_HINT if chart.kind.value in ("pie", "doughnut") else CHART_CATEGORY_HINT
    if len(chart.categories) > limit:
        warnings.append(
            f"图表有 {len(chart.categories)} 个类别（{chart.kind.value} 建议不超过 {limit} 个）："
            f"轴标签/图例会挤在一起，考虑合并类别或改用条形图"
        )
    if "data" not in layout.names:
        warnings.append("这个版式没有图表槽：图表被放过了，请换数据页（data）")
        return [], warnings
    fills = [SlotFill(slot="data", kind="chart", payload=chart)]
    takeaway = chart.takeaway or slide.quote
    if takeaway and "quote" in layout.names:
        quote_slot = layout.slot("quote")
        steps = theme.type_scale.body_steps
        quote_fills, _, quote_warnings = _text_fill(quote_slot, [takeaway], theme, steps=steps)
        fills.extend(quote_fills)
        warnings.extend(quote_warnings)
    return fills, warnings


def _kpi_fills(slide: SlideSpec, layout: Layout) -> list[SlotFill]:
    fills: list[SlotFill] = []
    for index, kpi in enumerate(slide.kpis[:3], start=1):
        name = f"kpi_{index}"
        if name in layout.names:
            fills.append(SlotFill(slot=name, kind="kpi", payload=kpi))
    return fills


def _icon_fills(slide: SlideSpec, layout: Layout, theme: ThemeTokens) -> list[SlotFill]:
    """要点页的视觉元素：每个要点前面一枚图标（沿主题的图标循环取）。"""
    if "icons" not in layout.names or not slide.bullets:
        return []
    icons = (
        tuple(slide.icons)
        if slide.icons
        else tuple(theme.shapes.icon_for(index) for index in range(len(slide.bullets)))
    )
    return [SlotFill(slot="icons", kind="icons", payload=icons, lines=len(slide.bullets))]


def _map_slide(
    slide: SlideSpec,
    theme: ThemeTokens,
    *,
    start_index: int,
    section_ordinal: int,
) -> list[PagePlan]:
    density = slide.density if slide.density is not Density.AUTO else pick_density(slide)
    layout = get_layout(slide.archetype, density)

    items: tuple[str, ...] = ()
    if slide.bullets:
        items = tuple(slide.bullets)
    elif slide.body:
        items = (slide.body,)
    decisions: list[SlotDecision] = []
    warnings: list[str] = []
    fills: list[SlotFill] = []

    # 标题 / 副标题 / 元信息：都是单条目，各自一格（标题槽高放得下两行，所以换行不算溢出）
    title_fills, title_decisions, title_warnings = _text_fill(
        layout.slot(layout.title_slot),
        [slide.title],
        theme,
        steps=theme.type_scale.title_steps(slide.archetype.value),
    )
    fills.extend(title_fills)
    decisions.extend(title_decisions)
    warnings.extend(title_warnings)

    if slide.subtitle and "subtitle" in layout.names:
        extra, extra_decisions, extra_warnings = _text_fill(
            layout.slot("subtitle"), [slide.subtitle], theme, steps=theme.type_scale.subtitle_steps
        )
        fills.extend(extra)
        decisions.extend(extra_decisions)
        warnings.extend(extra_warnings)

    if slide.meta and "meta" in layout.names:
        extra, extra_decisions, extra_warnings = _text_fill(
            layout.slot("meta"), [slide.meta], theme, steps=theme.type_scale.caption_steps
        )
        fills.extend(extra)
        decisions.extend(extra_decisions)
        warnings.extend(extra_warnings)

    # 章节页的大序号：它是文字，但内容是"这一章排第几"，从标题推不出来
    if "number" in layout.names:
        number = f"{section_ordinal:02d}" if section_ordinal else ""
        number_slot = layout.slot("number")
        fills.append(
            SlotFill(
                slot="number",
                kind="text",
                payload=number,
                font_size_pt=theme.type_scale.cover_title_pt,
                lines=1 if number else 0,
            )
        )
        decisions.append(
            SlotDecision(
                slot="number",
                action=OverflowAction.KEEP,
                font_size_pt=theme.type_scale.cover_title_pt,
                lines=(1,),
                capacity=(number_slot.max_lines,),
                detail="章节序号",
            )
        )

    # 主体内容（要点 / 正文）：一条决策定到底，多出来的页由 chunks 摊开
    body_plan: BlockPlan | None = None
    body_slots: tuple[Slot, ...] = ()
    if items:
        body_slots = tuple(
            layout.slot(name) for name in (layout.columns or ("body",)) if name in layout.names
        )
        if body_slots:
            body_plan = fit_block(items, body_slots, theme)
            decisions.append(
                SlotDecision(
                    slot="+".join(slot.name for slot in body_slots),
                    action=body_plan.action,
                    font_size_pt=body_plan.font_size_pt,
                    lines=body_plan.pages[0].lines,
                    capacity=body_plan.capacity,
                    detail=body_plan.detail,
                )
            )
            if body_plan.action is OverflowAction.WARN:
                warnings.append(f"要点区装不下：{body_plan.detail}")
            if body_plan.page_count > 1:
                warnings.append(
                    f"要点过多，已按最小字号拆成 {body_plan.page_count} 页（标题会带「（续）」）"
                )

    chart_fills, chart_warnings = _chart_fills(slide, layout, theme)
    fills.extend(chart_fills)
    warnings.extend(chart_warnings)

    if slide.image is not None:
        if "image" in layout.names:
            fills.append(SlotFill(slot="image", kind="image", payload=slide.image))
        else:
            warnings.append("这个版式没有图片槽：图片被放过了，请换图文页（split）或加密度到 heavy")

    fills.extend(_kpi_fills(slide, layout))
    fills.extend(_icon_fills(slide, layout, theme))

    chunks = body_plan.pages if body_plan is not None else (BlockPage(columns=(), lines=()),)
    body_size = body_plan.font_size_pt if body_plan is not None else theme.type_scale.body_pt
    body_action = body_plan.action if body_plan is not None else OverflowAction.KEEP

    pages: list[PagePlan] = []
    for page_offset, chunk in enumerate(chunks):
        page_fills = list(fills)
        for index, content in enumerate(chunk.columns):
            if not content or index >= len(body_slots):
                continue
            target = body_slots[index]
            page_fills.append(
                SlotFill(
                    slot=target.name,
                    kind="list" if len(content) > 1 else "text",
                    payload=tuple(content) if len(content) > 1 else content[0],
                    font_size_pt=body_size,
                    lines=chunk.lines[index] if index < len(chunk.lines) else 0,
                    action=body_action,
                )
            )
        page_fills.extend(
            _decoration_fills(layout, page_number=start_index + page_offset + 1)
        )
        pages.append(
            PagePlan(
                index=start_index + page_offset + 1,
                archetype=slide.archetype,
                density=density,
                title=slide.title if page_offset == 0 else f"{slide.title}（续）",
                fills=tuple(page_fills),
                decisions=tuple(decisions),
                warnings=tuple(warnings),
                speaker_notes=slide.notes,
                continuation=page_offset > 0,
            )
        )
    return pages


def map_deck(
    spec: DeckSpec | Mapping[str, Any] | str,
    *,
    theme: ThemeTokens | None = None,
) -> DeckPlan:
    """deck spec → 计划。合不合法由 `spec.load_deck_spec` 负责（这里只管落地）。"""
    deck = spec if isinstance(spec, DeckSpec) else load_deck_spec(spec)
    base = theme or DEFAULT_THEME
    if deck.brand is not None:
        base = base.with_brand(
            primary=deck.brand.primary or None, accent=deck.brand.accent or None
        )

    warnings = list(deck.structure_notes())
    pages: list[PagePlan] = []
    section_ordinal = 0
    for slide in deck.slides:
        if slide.archetype is Archetype.SECTION:
            section_ordinal += 1
        pages.extend(
            _map_slide(
                slide,
                base,
                start_index=len(pages),
                section_ordinal=section_ordinal,
            )
        )

    for page in pages:
        if not page.visual_elements and page.archetype not in (
            Archetype.COVER,
            Archetype.SECTION,
            Archetype.CLOSING,
        ):
            warnings.append(
                f"第 {page.index} 页（{page.key}）没有任何视觉元素："
                f"请补图标/图表/图片，或换页型"
            )
    return DeckPlan(title=deck.title, theme=base, pages=tuple(pages), warnings=tuple(warnings))


# ------------------------------------------------------ 溢出判据的对照用例
#
# 下面这张表是**判据的可执行说明**：它同时被 `python -m app.services.deck.content_map`
# 打印（给人看"什么情况会得到什么决策"）和单测断言（钉住判据不被改坏）。
# 造这张表的动机很直接：溢出阈值的三个量（字号、字数、槽宽）互相耦合，
# 光看代码说不清"多少字会缩、多少字会拆"，而这张表把三种结果都摆出来了。


@dataclass(frozen=True)
class OverflowCase:
    """一个判据用例：若干条目放进一个指定尺寸的槽（或双栏槽）。

    ``role`` 要跟着真实版式来：标题槽是 TITLE（没有项目符号，不扣悬挂缩进），
    要点槽是 LIST（扣）。**这一格写错，判据就会被验错**——实测过一次：
    标题用例按 LIST 算，一个 31 字的标题被算成三行，于是"缩到 24pt 能放下"
    变成了"装不下"。
    """

    label: str
    items: tuple[str, ...]
    width_in: float
    height_in: float
    columns: int = 1
    max_lines: int = 6
    steps: tuple[int, ...] = (18, 16, 14)
    role: SlotRole = SlotRole.LIST
    expect: OverflowAction = OverflowAction.KEEP


_SHORT_BULLETS: Final[tuple[str, ...]] = (
    "营收同比增长 18%",
    "毛利率回升到 42%",
    "新客获取成本下降 9%",
)
_LONG_BULLETS: Final[tuple[str, ...]] = (
    "华东区经销渠道的复购率提升了 12 个百分点，主要来自会员体系改版与门店导购话术的统一培训，"
    "其中会员权益的感知度提升贡献了大约三分之二",
    "华南区因为雨季与门店改造叠加，客流量同比下滑 7%，但客单价提升 11%，"
    "两边抵消之后整体持平，这一条需要在下一季度重点观察",
)
_MIXED_BULLETS: Final[tuple[str, ...]] = (
    "Q3 revenue 同比 +18.6%（合并口径），EBITDA margin 站上 21%",
    "North 区 offline 渠道拖累最大，约 -4.2pp，其中 store traffic 是主要变量",
)
_LONG_TITLE: Final[str] = "本季度华东区经销渠道复购率提升情况的复盘与下一阶段重点工作安排"
_LONG_TITLE_OVER: Final[str] = (
    "本季度华东区经销渠道复购率提升情况的复盘、归因分析、门店执行差异与下一阶段重点工作安排及资源需求"
)
_GREEDY_BULLETS: Final[tuple[str, ...]] = tuple(
    f"第 {index} 项结论：指标正常，无异常" for index in range(1, 13)
)
_ONE_LONG_ITEM: Final[str] = (
    "这一版的核心结论是渠道结构正在发生变化。线下门店的贡献占比从 62% 降到 51%，"
    "而线上自营与私域合计从 21% 涨到 34%。变化主要发生在二三线城市的中端客群。"
    "如果这个趋势延续，明年的一号位动作应当是重配导购编制与门店面积，"
    "并把会员权益从折扣驱动改成服务驱动。"
    "同时要注意线上获客成本也在抬头，从 118 元涨到 143 元，"
    "其中信息流投放占了七成。"
    "线下门店的坪效虽然回升，但坪效的回升主要来自客流回升而不是转化率提升，"
    "这两者的可持续性并不一样：客流随季节波动，转化率才是能力。"
    "所以下一季度的观察重点应当放在转化率与复购间隔这两条线上。"
)

#: 对照用例（槽宽取真实版式的尺寸：满宽 11.583、半幅 5.9165、双栏 5.5415、封面标题 6.4）。
OVERFLOW_CASES: Final[tuple[OverflowCase, ...]] = (
    OverflowCase(
        label="短要点 · 中文 · 满宽槽",
        items=_SHORT_BULLETS,
        width_in=11.583,
        height_in=5.0,
        expect=OverflowAction.KEEP,
    ),
    OverflowCase(
        label="长要点 · 中文 · 满宽槽",
        items=_LONG_BULLETS,
        width_in=11.583,
        height_in=5.0,
        expect=OverflowAction.KEEP,
    ),
    OverflowCase(
        label="中英混排 · 满宽槽（西文较窄，同样内容行数更少）",
        items=_MIXED_BULLETS,
        width_in=11.583,
        height_in=5.0,
        expect=OverflowAction.KEEP,
    ),
    OverflowCase(
        label="短要点 · 中文 · 半幅槽（图文页左栏）",
        items=_SHORT_BULLETS,
        width_in=5.9165,
        height_in=5.0,
        expect=OverflowAction.KEEP,
    ),
    OverflowCase(
        label="长要点 · 中文 · 半幅槽（要缩字号）",
        items=_LONG_BULLETS,
        width_in=5.9165,
        height_in=5.0,
        expect=OverflowAction.SHRINK,
    ),
    OverflowCase(
        label="要点很多（12 条）· 满宽槽（要拆页）",
        items=_GREEDY_BULLETS,
        width_in=11.583,
        height_in=5.0,
        max_lines=6,
        expect=OverflowAction.SPLIT,
    ),
    OverflowCase(
        label="单个超长要点 · 双栏（按句切两栏）",
        items=(_ONE_LONG_ITEM,),
        width_in=5.5415,
        height_in=5.0,
        columns=2,
        max_lines=7,
        expect=OverflowAction.COLUMNS,
    ),
    OverflowCase(
        label="超长标题（封面标题槽 6.4in，缩到下限并换行）",
        items=(_LONG_TITLE,),
        width_in=6.4,
        height_in=1.8,
        max_lines=2,
        steps=(40, 36, 32, 28, 24),
        role=SlotRole.TITLE,
        expect=OverflowAction.SHRINK,
    ),
    OverflowCase(
        label="过长的标题（缩到 24pt 仍超两行 → 报出来）",
        items=(_LONG_TITLE_OVER,),
        width_in=6.4,
        height_in=1.8,
        max_lines=2,
        steps=(40, 36, 32, 28, 24),
        role=SlotRole.TITLE,
        expect=OverflowAction.WARN,
    ),
    OverflowCase(
        label="要点略多（5 条）· 双栏",
        items=tuple(f"第 {i} 条要点：情况正常" for i in range(1, 6)),
        width_in=5.5415,
        height_in=5.0,
        columns=2,
        max_lines=7,
        expect=OverflowAction.KEEP,
    ),
)


def run_overflow_cases(theme: ThemeTokens | None = None) -> list[tuple[OverflowCase, BlockPlan]]:
    """跑一遍对照表（`__main__` 与单测共用）。

    用的槽是**造出来的**（只给宽高与行数上限），不是从版式里取的：
    这张表要回答的是判据本身，所以它应当能独立于某个具体版式被读懂与改动。
    """
    token = theme or DEFAULT_THEME
    results: list[tuple[OverflowCase, BlockPlan]] = []
    for case in OVERFLOW_CASES:
        slots = tuple(
            Slot(
                name=f"col_{index + 1}",
                rect=Rect(0.5, 1.6, case.width_in, case.height_in),
                role=case.role,
                max_lines=case.max_lines,
            )
            for index in range(case.columns)
        )
        results.append((case, fit_block(case.items, slots, token, steps=case.steps)))
    return results


def _main() -> int:  # pragma: no cover - 开发期手工核对入口
    """``python -m app.services.deck.content_map``：打印溢出判据对照表。"""
    print("溢出判据对照表（槽宽/字号/字数 → 决策）")
    print(
        f"  公式：每行可放字数 ≈ 槽宽(in) × 72 × {DEFAULT_THEME.measure.fit_slack} ÷ "
        f"(字号pt × 中文字宽系数 × {DEFAULT_THEME.measure.font_width_margin})"
    )
    for width, label in ((11.583, "满宽"), (5.9165, "半幅"), (5.5415, "双栏")):
        print(f"  {label} {width}in 在 18pt 下约 {chars_per_line(width, 18):.1f} 字/行")
    print()
    header = (
        f"{'用例':<44}{'条目':>4}{'槽宽':>7}{'栏':>3}{'决策':>7}{'字号':>5}{'行数':>7}{'页数':>5}"
    )
    print(header)
    print("-" * len(header))
    for case, plan in run_overflow_cases():
        total = sum(sum(page.lines) for page in plan.pages)
        print(
            f"{case.label:<44}{len(case.items):>4}{case.width_in:>7.3f}{case.columns:>3}"
            f"{ACTION_LABELS[plan.action]:>7}{plan.font_size_pt:>5}{total:>7}{plan.page_count:>5}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
