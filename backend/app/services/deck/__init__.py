"""幻灯片生成的四层（令牌 / 原型 / 契约 / 映射）+ 写盘通路（Node 渲染 + 结构校验）。

**写盘层**：把槽位变成 .pptx 字节用的是 PptxGenJS（Node），脚本在
`scripts/deck/render.mjs`；Python 侧只有两个门面——
:func:`render_deck`（计划 → 产物）与 :func:`verify_pptx`（产物 → 结构证据）。
后端本身零新增依赖：没有 Node 时渲染降级成一句人话（:func:`node_requirement`），
而结构校验是纯 Python，永远可用。

分层（依赖方向单向，禁止反向）：

    theme_tokens ← layouts ← spec ← content_map ← render ← verify

- `theme_tokens`：色板 / 字体白名单 / 字号阶梯 / 间距 / 度量预算（唯一的样式数字来源）；
- `layouts`：6 个原型 × light/heavy = 12 个版式，区域矩形 + 命名槽位 + 机械核对；
- `spec`：agent 该产出的结构化数据，LLM 与渲染之间的唯一接口（pydantic 校验，人话报错）；
- `content_map`：deck spec → 每页每槽填什么 + 溢出决策（缩字号 → 拆页 → 两栏）；
- `render`：`DeckPlan.to_dict()` → `.pptx`，只调 `node scripts/deck/render.mjs`，不碰 OOXML 细节；
- `verify`：解包产物做结构断言（原生图表 / 真 `<a:t>` / 没栅格化 / 字体三元组 / 版式序列）。
"""

from __future__ import annotations

from app.services.deck.content_map import (
    ACTION_LABELS,
    DeckPlan,
    OverflowAction,
    PagePlan,
    SlotDecision,
    SlotFill,
    chars_per_line,
    fit_block,
    map_deck,
    measure_width_em,
    pick_density,
    run_overflow_cases,
    text_width_em,
    wrap_lines,
)
from app.services.deck.layouts import (
    ARCHETYPE_LABELS,
    ARCHETYPES,
    LAYOUTS,
    Archetype,
    Density,
    Layout,
    Rect,
    Slot,
    SlotRole,
    get_layout,
    iter_layouts,
    validate_layouts,
)
from app.services.deck.render import (
    RENDER_SCRIPT_REL,
    DeckRenderError,
    RenderResult,
    find_node,
    node_requirement,
    plan_json,
    render_deck,
    render_script,
)
from app.services.deck.spec import (
    MAX_BULLETS_PER_SLIDE,
    MAX_SLIDES,
    ChartKind,
    ChartSeries,
    ChartSpec,
    DeckSpec,
    DeckSpecError,
    ImageIntent,
    Kpi,
    SlideSpec,
    contract_hint,
    load_deck_spec,
)
from app.services.deck.theme_tokens import (
    DEFAULT_THEME,
    SLIDE_HEIGHT_IN,
    SLIDE_WIDTH_IN,
    ThemeTokens,
    build_theme,
)
from app.services.deck.verify import (
    Check,
    DeckCheck,
    verify_pptx,
)

__all__ = [
    "ACTION_LABELS",
    "ARCHETYPES",
    "ARCHETYPE_LABELS",
    "DEFAULT_THEME",
    "LAYOUTS",
    "MAX_BULLETS_PER_SLIDE",
    "MAX_SLIDES",
    "RENDER_SCRIPT_REL",
    "SLIDE_HEIGHT_IN",
    "SLIDE_WIDTH_IN",
    "Archetype",
    "ChartKind",
    "ChartSeries",
    "ChartSpec",
    "Check",
    "DeckCheck",
    "DeckPlan",
    "DeckRenderError",
    "DeckSpec",
    "DeckSpecError",
    "Density",
    "ImageIntent",
    "Kpi",
    "Layout",
    "OverflowAction",
    "PagePlan",
    "Rect",
    "RenderResult",
    "SlideSpec",
    "Slot",
    "SlotDecision",
    "SlotFill",
    "SlotRole",
    "ThemeTokens",
    "build_theme",
    "chars_per_line",
    "contract_hint",
    "find_node",
    "fit_block",
    "get_layout",
    "iter_layouts",
    "load_deck_spec",
    "map_deck",
    "measure_width_em",
    "node_requirement",
    "pick_density",
    "plan_json",
    "render_deck",
    "render_script",
    "run_overflow_cases",
    "text_width_em",
    "validate_layouts",
    "verify_pptx",
    "wrap_lines",
]
