"""幻灯片生成·契约层：**deck spec 是 LLM 与渲染之间的唯一接口**。

模型不再产出坐标、字号、颜色——它产出这份结构化数据；版式、字体、颜色、溢出
全部由下面三层（令牌 / 原型 / 映射）决定。**接口只有一份，而且是可校验的**：
校验不通过就带着人话错误回去让模型重写，而不是让它把一份半成品塞进渲染器。

三条设计决定：

1. **字段集来自原型层，不是另发明一套。** "这一页能不能放图表"这种问题不在这里
   拍脑袋：`_payload_capability` 直接遍历 `layouts.LAYOUTS` 取该页型两个密度档的
   槽位并集。原型层加了槽位，契约自动跟着能收对应载荷——**两处真相迟早分叉，
   所以只有一处**。
2. **报错必须是模型能照着改的话。** pydantic 默认会说
   ``Input should be 'cover', 'section', ...``——对模型来说这等于没说。所以枚举、
   非空、长度这类判断全部走自定义校验器，消息写成"第 2 页「季度营收」的标题不能为空：
   这一页至少要有一句话说明它在讲什么"。:func:`describe_errors` 负责把 pydantic 的
   结构错误（缺字段、类型不对）也翻成人话，并定位到具体第几页。
3. **"每页至少一个视觉元素"是硬约束，不是风格建议。** 纯标题 + 要点的页在投影上
   等于没有信息层次。这条约束在这里挡住（见 `SlideSpec._check_payload`），
   而不是等渲染出来再说。

**写盘层未接**：本模块不 import 任何排版库，也不 import `office.py`
（那条是现役路径，等写盘层接上再换）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.services.deck.layouts import (
    ARCHETYPE_LABELS,
    LAYOUTS,
    Archetype,
    Density,
    SlotRole,
)
from app.services.deck.theme_tokens import DEFAULT_THEME

__all__ = [
    "MAX_BULLETS_PER_SLIDE",
    "MAX_SLIDES",
    "MAX_TITLE_CHARS",
    "BrandTokens",
    "ChartKind",
    "ChartSeries",
    "ChartSpec",
    "DeckSpec",
    "DeckSpecError",
    "ImageIntent",
    "ImageIntentKind",
    "Kpi",
    "SlideSpec",
    "contract_hint",
    "describe_errors",
    "load_deck_spec",
]

#: 与 `app/services/office.py` 的上限保持一致：一页一页写出来的是沟通材料，不是归档材料。
MAX_SLIDES: Final[int] = 60
#: 一页最多几条要点。超了不是拒绝，是**说清楚该拆页**（拆页决策在映射层）。
MAX_BULLETS_PER_SLIDE: Final[int] = 12
#: 标题长度上限（字符）。标题是路牌不是句子：超过这个长度的内容属于要点或正文。
MAX_TITLE_CHARS: Final[int] = 60


class ChartKind(StrEnum):
    """原生图表类型。

    取值与两处对齐：``column/bar/line/area/pie`` 与 `office.CHART_TYPES` 同词，
    ``doughnut`` 是写盘层（PptxGenJS）原生支持、而 openpyxl 没有的那一个。
    **图表原生优先**：能用原生图表就不贴图片——图片里的数字不能被改、不能被读屏。
    """

    COLUMN = "column"
    BAR = "bar"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    DOUGHNUT = "doughnut"


class ImageIntentKind(StrEnum):
    """图片的来路。三种都是**意图**，不是文件：写盘层按它去取/生图。"""

    GENERATE = "generate"
    LOCAL = "local"
    SEARCH = "search"


class _Strict(BaseModel):
    """所有契约模型的共同设置：多给字段即报错。

    默认的 pydantic 行为是**忽略**多余字段——那意味着模型写错一个字段名
    （``bullet`` / ``points`` / ``chartData``）时它自己不知道，渲染出来少一块内容也没人知道。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ImageIntent(_Strict):
    """图片意图：``alt`` 必填（人会读它，读屏软件也读它）。"""

    kind: ImageIntentKind = ImageIntentKind.GENERATE
    prompt: str = ""
    path: str = ""
    alt: str

    @field_validator("kind", mode="before")
    @classmethod
    def _check_kind(cls, value: Any) -> Any:
        if isinstance(value, ImageIntentKind):
            return value
        allowed = {item.value for item in ImageIntentKind}
        if isinstance(value, str) and value in allowed:
            return value
        raise ValueError(
            f"未知的图片来路「{value}」：可用的是 generate（现生一张）、"
            f"local（用工作区里的文件）、search（检索一张）"
        )

    @field_validator("alt")
    @classmethod
    def _check_alt(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "图片要有一句 alt 说明它在讲什么：这话会出现在读屏软件里，"
                "也是给写盘层的图注"
            )
        return value

    @model_validator(mode="after")
    def _check_source(self) -> ImageIntent:
        if self.kind is ImageIntentKind.GENERATE and not self.prompt:
            raise ValueError("现生图（generate）必须给 prompt：一句话说清这张图画什么")
        if self.kind is ImageIntentKind.LOCAL and not self.path:
            raise ValueError("用工作区文件（local）必须给 path：指向那个图片文件")
        return self


class Kpi(_Strict):
    """指标卡：一个数字 + 它的含义。数据页 heavy 档用（最多三个）。"""

    label: str
    value: str
    delta: str = ""

    @field_validator("label", "value")
    @classmethod
    def _check_text(cls, value: str) -> str:
        if not value:
            raise ValueError("指标卡要有 label（这是什么）与 value（多少）")
        return value


class ChartSeries(_Strict):
    name: str
    values: list[float] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not value:
            raise ValueError("数据系列要有名字：图例上写的就是它")
        return value

    @field_validator("values")
    @classmethod
    def _check_values(cls, value: list[float]) -> list[float]:
        for item in value:
            if item != item or item in (float("inf"), float("-inf")):  # NaN / ±Inf
                raise ValueError("数据系列里有非法数值（NaN/无穷）：图表画不出来，请给真实数字")
        return value


class ChartSpec(_Strict):
    """图表：类型 + 类别 + 至少一个系列 + **一句结论**。

    "一句结论"是必给的：图不自己说话——同一张折线图，有人读成"在涨"，
    有人读成"涨幅在收敛"。结论由作者写，不由读者猜。
    """

    kind: ChartKind = ChartKind.COLUMN
    categories: list[str] = Field(min_length=1)
    series: list[ChartSeries] = Field(min_length=1)
    takeaway: str = ""
    unit: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _check_kind(cls, value: Any) -> Any:
        if isinstance(value, ChartKind):
            return value
        allowed = ", ".join(item.value for item in ChartKind)
        if isinstance(value, str) and value in {item.value for item in ChartKind}:
            return value
        raise ValueError(f"未知的图表类型「{value}」：可用的是 {allowed}")

    @model_validator(mode="after")
    def _check_shape(self) -> ChartSpec:
        expected = len(self.categories)
        for item in self.series:
            if len(item.values) != expected:
                raise ValueError(
                    f"系列「{item.name}」有 {len(item.values)} 个数值，但类别有 {expected} 个："
                    f"两者必须一一对应（缺的补 0，不要少给）"
                )
        if self.kind in (ChartKind.PIE, ChartKind.DOUGHNUT) and len(self.series) > 1:
            raise ValueError(
                f"{self.kind.value} 饼/环图只能有一个系列：多系列请改用 column 或 bar"
            )
        if not self.takeaway:
            raise ValueError("图表要给一句结论（takeaway）：这一页想让读者记住的那一句话")
        return self


def _payload_capability(archetype: Archetype) -> frozenset[str]:
    """某页型**能收哪些载荷**——直接取自原型层的槽位并集（两个密度档合起来看）。

    载荷名 ↔ 槽位角色的对应关系写在这里，是"契约跟着版式走"的那一处粘合：
    页型有 ``data`` 槽就能收 ``chart``，有 ``image`` 槽就能收 ``image``，
    以此类推。版式改了这里不用改（除非新增角色）。

    **并集是有代价的**：``image`` 槽只在部分密度档里存在（封面只有 heavy 有图），
    所以"能收"不等于"任何密度都能收"——选了 light 而页上没图槽，图会被静默丢掉。
    因此映射层有一条硬规矩：**给了载荷就按载荷能落地的密度走**（见
    `content_map.pick_density`），而不是反过来把载荷扔掉。
    """
    roles: set[SlotRole] = set()
    names: set[str] = set()
    for (layout_archetype, _density), layout in LAYOUTS.items():
        if layout_archetype is not archetype:
            continue
        for slot in layout.slots:
            roles.add(slot.role)
            names.add(slot.name)

    capability: set[str] = set()
    if SlotRole.CHART in roles:
        capability.add("chart")
    if SlotRole.IMAGE in roles:
        capability.add("image")
    if SlotRole.KPI in roles:
        capability.add("kpis")
    if SlotRole.LIST in roles:
        capability.add("bullets")
    if SlotRole.TEXT in roles and "subtitle" in names:
        capability.add("subtitle")
    if "meta" in names:
        capability.add("meta")
    if "quote" in names:
        capability.add("quote")
    return frozenset(capability)


#: 载荷的中文名（错误话术里用）。
_PAYLOAD_LABELS: Final[dict[str, str]] = {
    "chart": "图表",
    "image": "图片",
    "kpis": "指标卡",
    "bullets": "要点",
    "subtitle": "副标题",
    "meta": "元信息",
    "quote": "结语",
}

#: 载荷"本该放在哪种页型"（只说第一个，够模型改对就行）。
_PAYLOAD_HOME: Final[dict[str, Archetype]] = {
    "chart": Archetype.DATA,
    "kpis": Archetype.DATA,
    "image": Archetype.SPLIT,
    "quote": Archetype.DATA,
}


def _archetype_choices() -> str:
    return "、".join(f"{item.value}（{ARCHETYPE_LABELS[item]}）" for item in Archetype)


def _density_before(value: Any) -> Any:
    """``density`` 的取值校验（页与 deck 两级共用，避免两处各写一套话术）。"""
    if isinstance(value, Density):
        return value
    if isinstance(value, str) and value in {item.value for item in Density}:
        return value
    raise ValueError("未知密度：可用的是 auto（按内容自动挑）、light（疏）、heavy（密）")


class SlideSpec(_Strict):
    """一页幻灯片。**页型决定它能收什么**，收不了的东西在这里就被挡住。"""

    archetype: Archetype
    title: str
    subtitle: str = ""
    body: str = ""
    bullets: list[str] = Field(default_factory=list)
    icons: list[str] = Field(default_factory=list)
    chart: ChartSpec | None = None
    image: ImageIntent | None = None
    quote: str = ""
    kpis: list[Kpi] = Field(default_factory=list)
    meta: str = ""
    notes: str = ""
    density: Density = Density.AUTO

    @field_validator("archetype", mode="before")
    @classmethod
    def _check_archetype(cls, value: Any) -> Any:
        if isinstance(value, Archetype):
            return value
        if isinstance(value, str) and value in {item.value for item in Archetype}:
            return value
        raise ValueError(f"未知页型「{value}」：可用的是 {_archetype_choices()}")

    @field_validator("density", mode="before")
    @classmethod
    def _check_density(cls, value: Any) -> Any:
        return _density_before(value)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        if not value:
            raise ValueError("标题不能为空：这一页至少要有一句话说明它在讲什么")
        if len(value) > MAX_TITLE_CHARS:
            raise ValueError(
                f"标题有 {len(value)} 字，超过上限 {MAX_TITLE_CHARS} 字："
                f"标题是路牌不是句子，长内容请放进 body 或 bullets"
            )
        return value

    @field_validator("bullets")
    @classmethod
    def _check_bullets(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for index, item in enumerate(value, start=1):
            text = item.strip()
            if not text:
                raise ValueError(f"第 {index} 条要点是空的：空要点在页面上就是一个孤零零的项目符号")
            cleaned.append(text)
        if len(cleaned) > MAX_BULLETS_PER_SLIDE:
            raise ValueError(
                f"一页给了 {len(cleaned)} 条要点，上限 {MAX_BULLETS_PER_SLIDE} 条："
                f"请拆成两页（映射层也会拆，但那是兜底）"
            )
        return cleaned

    @field_validator("chart", mode="before")
    @classmethod
    def _check_chart_given(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict) and not value:
            raise ValueError(
                "图表是空对象：要么给 kind/categories/series/takeaway，要么整个不给"
            )
        if not isinstance(value, dict):
            raise ValueError(
                f"图表要写成一个对象（含 kind/categories/series/takeaway），"
                f"现在给的是 {type(value).__name__}「{value}」"
            )
        return value

    @model_validator(mode="after")
    def _check_payload(self) -> SlideSpec:
        allowed = _payload_capability(self.archetype)
        provided = {
            "chart": self.chart is not None,
            "image": self.image is not None,
            "kpis": bool(self.kpis),
            "bullets": bool(self.bullets) or bool(self.body),
            "subtitle": bool(self.subtitle),
            "meta": bool(self.meta),
            "quote": bool(self.quote),
        }
        label = f"{ARCHETYPE_LABELS[self.archetype]}「{self.title}」"

        for field_name, has in provided.items():
            if has and field_name not in allowed:
                home = _PAYLOAD_HOME.get(field_name)
                hint = (
                    f"，那属于{ARCHETYPE_LABELS[home]}（{home.value}）"
                    if home is not None
                    else ""
                )
                raise ValueError(
                    f"{label}收不了{_PAYLOAD_LABELS.get(field_name, field_name)}{hint}"
                    f"：这个页型的槽位里没有它的位置"
                )

        if self.kpis and len(self.kpis) > 3:
            raise ValueError(f"{label}给了 {len(self.kpis)} 个指标卡，最多 3 个")

        if self.chart is not None and self.quote and self.chart.takeaway:
            raise ValueError(
                f"{label}同时给了图表的 takeaway 和 quote：留一个就够"
                f"（页面上只有一处结论位置）"
            )

        if not self._has_visual_element():
            raise ValueError(
                f"{label}没有任何视觉元素：**每页至少一个非文字元素**"
                f"（图标 / 图表 / 图片 / 指标卡）——要点页靠每条要点前的图标，"
                f"数据页靠原生图表，图文页靠图；封面、章节页、收尾页由原型的"
                f"装饰形状（强调条 / 大序号）保证。请补内容，或换一个装得下的页型。"
            )
        return self

    def _has_visual_element(self) -> bool:
        """视觉元素判定：内容给的（图表/图片/指标卡/要点图标）或原型保证的装饰。"""
        if self.chart is not None or self.image is not None or self.kpis:
            return True
        if self.bullets or self.icons:
            return True
        return any(
            layout.guaranteed_visual
            for (archetype, _), layout in LAYOUTS.items()
            if archetype is self.archetype
        )


def _has_image_slot(archetype: Archetype) -> bool:
    """该页型有没有放图的位置。

    注意：``image`` 槽**只存在于部分密度的变体里**（封面只有 heavy 档有图）。
    所以"给了图"会**逼着映射层选 heavy**——这条约束在 `content_map.pick_density`
    里落实（否则图会被静默丢掉，那是这一类接口最常见的失败：给了东西但没人看它）。
    本函数供 :func:`contract_hint` 说明"这一页能不能给图"。
    """
    return "image" in _payload_capability(archetype)


class BrandTokens(_Strict):
    """品牌色（可空）。只收 6 位十六进制——颜色名（"深蓝"）没法落到 OOXML。"""

    primary: str = ""
    accent: str = ""

    @field_validator("primary", "accent")
    @classmethod
    def _check_hex(cls, value: str) -> str:
        if not value:
            return ""
        text = value.lstrip("#").upper()
        if len(text) != 6 or any(ch not in "0123456789ABCDEF" for ch in text):
            raise ValueError(
                f"品牌色「{value}」不是 6 位十六进制：请写成像 1B4F8A 这样"
                f"（不要写 # 前缀，也不要写颜色名）"
            )
        return text


class DeckSpec(_Strict):
    """一份 deck。``slides`` 至少一页、至多 `MAX_SLIDES` 页。"""

    title: str
    subtitle: str = ""
    author: str = ""
    org: str = ""
    brand: BrandTokens | None = None
    default_density: Density = Density.AUTO
    slides: list[SlideSpec] = Field(min_length=1, max_length=MAX_SLIDES)

    @field_validator("title")
    @classmethod
    def _check_deck_title(cls, value: str) -> str:
        if not value:
            raise ValueError("这份 deck 要有标题")
        return value

    @field_validator("default_density", mode="before")
    @classmethod
    def _check_default_density(cls, value: Any) -> Any:
        return _density_before(value)

    def structure_notes(self) -> list[str]:
        """**不致命但该说的话**：不常见的结构在这里提醒，不在这里拒绝。"""
        notes: list[str] = []
        if self.slides[0].archetype is not Archetype.COVER:
            notes.append("第一页不是封面：多数场合第一页应当是 cover，除非是插入到别处的单页")
        if self.slides[-1].archetype is not Archetype.CLOSING:
            notes.append("最后一页不是收尾页：留一页 closing 收住，比让 deck 戛然而止好读")
        if sum(1 for s in self.slides if s.archetype is Archetype.DATA) == 0:
            notes.append("整份 deck 没有数据页：如果这一版有数字要讲，考虑补一页 data")
        return notes


class DeckSpecError(ValueError):
    """deck spec 不合格。``message`` 是给人（也给模型）看的人话，可直接回给上层重写。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _where(location: tuple[Any, ...], payload: Mapping[str, Any]) -> str:
    """把 pydantic 的 ``loc`` 翻成"第几页哪一处"。"""
    if not location or location[0] != "slides" or len(location) < 2:
        return "整份 deck"
    index = location[1]
    where = f"第 {index + 1} 页"
    slides = payload.get("slides")
    if isinstance(slides, list) and 0 <= index < len(slides):
        item = slides[index]
        if isinstance(item, Mapping):
            title = str(item.get("title") or "").strip()
            if title:
                where += f"「{title[:20]}」"
    if location[2:]:
        where += "的「" + ".".join(_field_label(str(part)) for part in location[2:]) + "」"
    return where


#: 路径上的字段名 → 中文（报错定位用）。模型看到的路径应该是中文，
#: 否则"slides.2.chart.series.values"这种定位它得自己翻译一遍。
_FIELD_LABELS: Final[dict[str, str]] = {
    "archetype": "页型",
    "title": "标题",
    "subtitle": "副标题",
    "body": "正文",
    "bullets": "要点",
    "icons": "图标",
    "chart": "图表",
    "chart.kind": "图表类型",
    "chart.categories": "图表类别",
    "chart.series": "数据系列",
    "chart.takeaway": "图表结论",
    "image": "图片",
    "image.kind": "图片来路",
    "image.alt": "图片说明",
    "image.prompt": "图片描述",
    "image.path": "图片路径",
    "quote": "结语",
    "kpis": "指标卡",
    "meta": "元信息",
    "notes": "讲者备注",
    "density": "密度",
    "series": "数据系列",
    "values": "数值",
    "categories": "类别",
    "kind": "类型",
    "alt": "图片说明",
    "prompt": "图片描述",
    "path": "路径",
    "name": "名称",
    "label": "指标名",
    "value": "指标值",
    "takeaway": "结论",
    "slides": "页面",
    "brand": "品牌色",
    "primary": "主色",
    "accent": "强调色",
}


def _field_label(part: str) -> str:
    if part in _FIELD_LABELS:
        return _FIELD_LABELS[part]
    if part.isdigit():
        return f"第 {int(part) + 1} 项"
    return _PAYLOAD_LABELS.get(part, part)


def _human(error: Mapping[str, Any]) -> str:
    """单条 pydantic 错误 → 中文。

    ``exc.errors()`` 里装消息的键是 ``msg``（不是 ``message``）；自定义校验器
    抛的 ``ValueError`` 会被它前缀成 ``Value error, …``，那个前缀要剥掉——
    模型看到的是我们写的那句话，不是 pydantic 的包装。
    """
    message = str(error.get("msg", "")).strip()
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    kind = error.get("type", "")
    field_name = _field_label(str(error.get("loc", ("",))[-1]))
    if kind == "missing":
        return f"缺了必填的「{field_name}」"
    if kind == "string_too_short":
        return f"「{field_name}」是空的"
    if kind in ("list_type", "dict_type", "string_type", "float_parsing", "int_parsing"):
        return f"「{field_name}」的类型不对（{message}）"
    if kind == "extra_forbidden":
        return f"多了不认识的字段「{field_name}」：契约里没有它，请对照结构说明改名或删掉"
    return f"「{field_name}」不合法：{message}"


def describe_errors(error: ValidationError, payload: Mapping[str, Any] | None = None) -> str:
    """把 pydantic 的报错整段翻成"第几页哪里错了、怎么改"。"""
    data = payload or {}
    lines = [
        f"- {_where(tuple(item.get('loc', ())), data)}：{_human(item)}"
        for item in error.errors()
    ]
    head = f"这份 deck 有 {len(lines)} 处要改："
    return "\n".join([head, *lines])


def load_deck_spec(payload: Mapping[str, Any] | str) -> DeckSpec:
    """校验入口。合规则返回 `DeckSpec`，否则抛 `DeckSpecError`（人话）。"""
    if isinstance(payload, str):
        try:
            data: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DeckSpecError(
                f"这不是合法的 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from None
    else:
        data = payload
    if not isinstance(data, Mapping):
        raise DeckSpecError("deck 的顶层要是一个对象（带 title 与 slides 的那种）")
    try:
        return DeckSpec.model_validate(data)
    except ValidationError as exc:
        raise DeckSpecError(describe_errors(exc, data)) from None


def contract_hint() -> str:
    """给模型的**结构说明**（将来做工具 schema / system prompt 时用它）。

    它是从原型层现算的，不是手抄的：每个页型能收什么载荷、必给什么，
    一问 `_payload_capability` 就知道。手抄一份的结果是版式加了槽而说明没跟着改，
    模型于是永远填不满那几个槽。

    文字型页面的措辞要短（一句话一条），因为它会被塞进 system prompt 或工具描述里，
    **每多加一句都在挤模型的注意力**。
    """
    lines = [
        "产出 slides 数组，每页一个对象。字段：archetype（页型）、title（必给，≤"
        f"{MAX_TITLE_CHARS} 字）、density（auto/light/heavy，默认 auto）、以及该页型能收的载荷。",
        "**每页必须至少有一个非文字元素**（图标 / 图表 / 图片 / 指标卡）。",
        "",
        "页型与载荷：",
    ]
    for archetype in Archetype:
        capability = _payload_capability(archetype)
        light = LAYOUTS[(archetype, Density.LIGHT)]
        receives = "、".join(_PAYLOAD_LABELS.get(name, name) for name in sorted(capability))
        lines.append(
            f"- {archetype.value}（{ARCHETYPE_LABELS[archetype]}）："
            f"{receives or '只有标题与副标题'}"
        )
        if archetype is Archetype.DATA:
            lines.append("    必给 chart：kind/categories/series（值要与类别一一对应）/takeaway")
        if archetype is Archetype.SPLIT:
            lines.append("    必给 image：kind/alt（generate 时还要 prompt）")
        if light.guaranteed_visual:
            lines.append("    视觉元素由原型装饰保证（强调条 / 大序号），不必额外给图")
        if not light.guaranteed_visual and not _has_image_slot(archetype):
            lines.append("    这一页没有放图的位置")
    lines.extend(
        [
            "",
            "文字槽有容量上限：超了由映射层按 缩字号 → 拆页 → 两栏 处理；"
            f"一页要点上限 {MAX_BULLETS_PER_SLIDE} 条。",
            f"主题：默认 {DEFAULT_THEME.name}（品牌色可换，字体走白名单，不嵌字体）。",
        ]
    )
    return "\n".join(lines)
