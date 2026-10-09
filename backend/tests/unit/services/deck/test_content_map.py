"""内容映射与溢出决策（幻灯片生成·映射层）。

镜像同构：``app/services/deck/content_map.py`` → 本文件。

这里钉三件事：

1. **判据对照表**（`OVERFLOW_CASES`）：每种"字号 × 字数 × 槽宽"组合该得到哪个决策
   （保留 / 缩字号 / 拆页 / 两栏 / 装不下）。这张表既是 `python -m app.services.deck.content_map`
   打印给人看的东西，也是这里的断言对象——**判据改了，测试必须跟着改**，
   不许悄悄漂移；
2. **度量公式**：中文字宽系数 1.0 em、替换余量 1.06。公式是估的，但估的幅度要有据可查
   （标定过程见 `theme_tokens` 模块头），所以这里回代几个样例，钉住量级；
3. **映射不丢东西**：给了的载荷必须落进某个槽；密度不足时**升档而不是扔掉**
   （实测踩过：章节页的三条要点在 light 档没有列表槽，被静默丢了）。
"""

from __future__ import annotations

import json

import pytest

from app.services.deck.content_map import (
    ACTION_LABELS,
    OVERFLOW_CASES,
    OverflowAction,
    PagePlan,
    chars_per_line,
    fit_block,
    map_deck,
    measure_width_em,
    pick_density,
    run_overflow_cases,
    text_width_em,
    wrap_lines,
)
from app.services.deck.layouts import Archetype, Density, get_layout
from app.services.deck.spec import SlideSpec, load_deck_spec
from app.services.deck.theme_tokens import DEFAULT_THEME

pytestmark = pytest.mark.local

SAMPLE_SPEC: dict = {
    "title": "2026 年 Q1 经营复盘",
    "author": "经营分析组",
    "brand": {"primary": "0F6CBD"},
    "slides": [
        {
            "archetype": "cover",
            "title": "2026 年 Q1 经营复盘",
            "subtitle": "渠道结构与复购质量",
            "meta": "经营分析组 · 2026-04-08",
        },
        {
            "archetype": "section",
            "title": "一、整体表现",
            "bullets": ["收入与利润", "渠道结构", "风险项"],
        },
        {
            "archetype": "bullets",
            "title": "三件已经确认的事",
            "bullets": ["营收同比增长 18%", "毛利率回升到 42%", "新客获取成本下降 9%"],
        },
        {
            "archetype": "data",
            "title": "收入结构在变",
            "chart": {
                "kind": "column",
                "categories": ["线下", "自营", "私域", "分销"],
                "series": [
                    {"name": "Q3 占比", "values": [62, 12, 9, 17]},
                    {"name": "Q4 占比", "values": [51, 18, 16, 15]},
                ],
                "takeaway": "线下让出的份额，全部被自营与私域接住了",
            },
            "kpis": [
                {"label": "自营占比", "value": "18%", "delta": "+6pp"},
                {"label": "复购率", "value": "34%", "delta": "+12pp"},
                {"label": "获客成本", "value": "143 元", "delta": "+25 元"},
            ],
        },
        {
            "archetype": "split",
            "title": "门店执行差异",
            "bullets": [
                "华东区导购话术统一后转化率提升 4pp",
                "华南区受雨季与改造叠加影响，客流下滑 7%",
            ],
            "image": {
                "kind": "generate",
                "prompt": "两家门店客流对比的示意图",
                "alt": "华东与华南门店客流对比",
            },
        },
        {
            "archetype": "closing",
            "title": "下一步：把转化率做成能力",
            "subtitle": "Q2 重点动作见附页",
            "meta": "经营分析组 · jingying@example.com",
        },
    ],
}

#: 12 条要点：在满宽单栏里装不下（要拆页），这是"拆页"决策的入口
ONLY_BULLETS: tuple[str, ...] = tuple(
    f"第 {index} 项结论：指标正常，环比无异常，无需额外动作" for index in range(1, 13)
)

MANY_BULLETS: dict = {
    "title": "要点很多的 deck",
    "slides": [
        {"archetype": "bullets", "title": "二、逐项结论", "bullets": list(ONLY_BULLETS)}
    ],
}


# ----------------------------------------------------------------- 判据对照表


def test_overflow_cases_match_documented_expectations() -> None:
    """**核心断言**：对照表里每种情况的决策与预期一致。"""
    mismatches = [
        (case.label, ACTION_LABELS[case.expect], ACTION_LABELS[plan.action])
        for case, plan in run_overflow_cases()
        if plan.action is not case.expect
    ]
    assert mismatches == []


def test_overflow_cases_cover_all_five_outcomes() -> None:
    """对照表要覆盖全部五种结果，否则它证明不了阶梯是完整的。"""
    covered = {case.expect for case in OVERFLOW_CASES}
    assert covered == set(OverflowAction)
    labels = [case.label for case in OVERFLOW_CASES]
    assert any("中文" in label for label in labels)
    assert any("中英混排" in label for label in labels)
    assert any("标题" in label for label in labels)
    assert any("双栏" in label for label in labels)


def test_case_font_sizes_stay_inside_their_steps() -> None:
    for case, plan in run_overflow_cases():
        assert plan.font_size_pt in case.steps
        assert plan.page_count >= 1
        assert plan.pages[0].columns


def test_shrink_uses_the_floor_not_below_it() -> None:
    """缩字号要落到"下限之内最大的那一档"，不许一路缩到看不见。"""
    for case, plan in run_overflow_cases():
        assert plan.font_size_pt >= min(case.steps)
        assert plan.font_size_pt in case.steps


def test_split_produces_pages_that_each_fit() -> None:
    """拆页的判据：切出来的每一页都要按最小字号装得下——不是把内容硬切一半。"""
    case = next(item for item in OVERFLOW_CASES if item.expect is OverflowAction.SPLIT)
    plan = next(plan for item, plan in run_overflow_cases() if item is case)
    assert plan.page_count > 1
    items = [item for page in plan.pages for column in page.columns for item in column]
    assert items == list(case.items)  # 一条不丢、顺序不变
    for page in plan.pages:
        for lines, capacity in zip(page.lines, plan.capacity, strict=True):
            assert lines <= capacity


def test_columns_case_splits_sentences_into_two_columns() -> None:
    case = next(item for item in OVERFLOW_CASES if item.expect is OverflowAction.COLUMNS)
    plan = next(plan for item, plan in run_overflow_cases() if item is case)
    assert plan.page_count == 1
    left, right = plan.pages[0].columns
    assert left and right
    assert "".join(left) in case.items[0]
    assert len(left) + len(right) >= 1


def test_warn_case_keeps_every_line_and_reports_the_numbers() -> None:
    """装不下时**不截断**：条目一条不动，把行数/容量的数字报出来交给人定夺。"""
    case = next(item for item in OVERFLOW_CASES if item.expect is OverflowAction.WARN)
    plan = next(plan for item, plan in run_overflow_cases() if item is case)
    assert plan.font_size_pt == min(case.steps)
    assert "容量" in plan.detail
    assert plan.pages[0].columns[0] == case.items


# ------------------------------------------------------------------- 度量


def test_cjk_advance_is_one_em_times_the_margin() -> None:
    """汉字是全宽：一个汉字 = 一个字号宽，再乘字体替换余量（1.06）。"""
    text = "数据资产盘点结论"
    assert text_width_em(text) == pytest.approx(len(text) * DEFAULT_THEME.measure.cjk_em)
    assert measure_width_em(text) == pytest.approx(
        len(text) * DEFAULT_THEME.measure.font_width_margin
    )


def test_mixed_text_is_narrower_than_pure_cjk() -> None:
    """中英混排比等长的纯中文窄——这是"西文只占半个字"的自然结果，不是特判。"""
    cjk = "营收同比增长百分之十八点六"
    mixed = "营收同比 +18.6%"
    assert measure_width_em(mixed) < measure_width_em(cjk)


def test_measure_lands_on_the_widest_whitelisted_font() -> None:
    """回代验证：预算宽度相对**最宽字体（雅黑）实测宽度**的倍率稳定在 1.0–1.2。

    标定过程（fontTools 读 hmtx，2026-10-05 本机）见 `theme_tokens` 模块头。
    这里的断言不是复算一遍字宽表，而是钉住"预算不低估、也不离谱地高估"这一条性质：
    低估会溢出，高估会把能放下的内容拆成两页。
    """
    for text in (
        "本季度华东区经销渠道的复购率提升了 12 个百分点，主要来自会员体系改版",
        "Revenue growth 18.6%（合并口径）",
        "结论：口径、优先级——止损",
    ):
        budget = measure_width_em(text)
        assert 1.0 <= budget / max(text_width_em(text), 0.1) <= 1.2


def test_chars_per_line_matches_the_named_formula() -> None:
    """具名公式：槽宽(in) × 72 × 断行余量 ÷ (字号pt × 中文字宽系数 × 替换余量)。"""
    measure = DEFAULT_THEME.measure
    expected = 11.583 * 72 * measure.fit_slack / (18 * measure.cjk_em * measure.font_width_margin)
    assert chars_per_line(11.583, 18) == pytest.approx(expected)
    # 字号减半 → 每行能放的字数翻倍；槽宽减半 → 减半
    assert chars_per_line(11.583, 9) > chars_per_line(11.583, 18)
    assert chars_per_line(5.7915, 18) == pytest.approx(chars_per_line(11.583, 18) / 2, rel=1e-3)


def test_wrap_lines_keeps_width_within_budget() -> None:
    text = "华东区经销渠道的复购率提升了 12 个百分点，主要来自会员体系改版与门店导购话术的统一培训"
    budget = 16.0
    lines = wrap_lines(text, budget)
    assert len(lines) >= 2
    for line in lines:
        assert measure_width_em(line) <= budget + DEFAULT_THEME.measure.cjk_em
    assert "".join(lines).replace(" ", "") == text.replace(" ", "")


def test_wrap_lines_hard_breaks_a_single_wide_character() -> None:
    """槽窄到一个字都放不下时也必须前进：硬留一个字符，绝不空转。"""
    lines = wrap_lines("宽", 0.05)
    assert lines == ["宽"]


def test_wrap_lines_respects_explicit_newlines() -> None:
    assert wrap_lines("第一行\n第二行", 100.0) == ["第一行", "第二行"]


def test_hanging_indent_only_applies_to_list_slots() -> None:
    """悬挂缩进只对列表槽生效。

    用**同一个矩形**造两个槽来比（只有 role 不同），否则比的是槽宽而不是缩进规则。
    列表要扣掉一个项目符号的宽度，所以它需要的行数不会少于同宽的正常文字槽。
    """
    from app.services.deck.layouts import Rect, Slot, SlotRole

    rect = Rect(0.5, 1.6, 4.0, 4.0)
    as_list = Slot(name="body", rect=rect, role=SlotRole.LIST, max_lines=9)
    as_text = Slot(name="meta", rect=rect, role=SlotRole.TEXT, max_lines=9)
    long_line = "华东区经销渠道的复购率提升了 12 个百分点，主要来自会员体系改版与话术统一"
    list_plan = fit_block([long_line], [as_list], DEFAULT_THEME)
    text_plan = fit_block([long_line], [as_text], DEFAULT_THEME)
    assert list_plan.pages[0].lines[0] >= text_plan.pages[0].lines[0]
    assert text_plan.pages[0].lines[0] >= 2  # 这条文字本来就放不下一行


def test_fit_block_without_items_is_a_no_op() -> None:
    slot = get_layout(Archetype.BULLETS, Density.LIGHT).slot("body")
    plan = fit_block([], [slot], DEFAULT_THEME)
    assert plan.action is OverflowAction.KEEP
    assert plan.pages[0].columns == ((),)


# ------------------------------------------------------------------- 密度


def _slide(**kwargs: object) -> SlideSpec:
    payload = {
        "archetype": "bullets",
        "title": "一页",
        "bullets": ["一条"],
        **kwargs,
    }
    return SlideSpec.model_validate(payload)


def test_density_promotes_when_light_has_no_slot_for_the_payload() -> None:
    """章节页 light 档没有列表槽：给了要点就**升到 heavy**，不许静默丢掉。"""
    section = SlideSpec.model_validate(
        {"archetype": "section", "title": "一、整体表现", "bullets": ["收入", "渠道", "风险"]}
    )
    assert pick_density(section) is Density.HEAVY
    assert "body" in get_layout(Archetype.SECTION, Density.HEAVY).names
    assert "body" not in get_layout(Archetype.SECTION, Density.LIGHT).names


def test_density_promotes_for_many_bullets() -> None:
    slide = _slide(bullets=[f"第 {index} 条" for index in range(6)])
    assert pick_density(slide) is Density.HEAVY


def test_density_stays_light_for_a_short_page() -> None:
    assert pick_density(_slide()) is Density.LIGHT


def test_density_promotes_for_a_wide_chart() -> None:
    slide = SlideSpec.model_validate(
        {
            "archetype": "data",
            "title": "收入",
            "chart": {
                "kind": "line",
                "categories": [f"月 {index}" for index in range(1, 10)],
                "series": [{"name": "收入", "values": list(range(1, 10))}],
                "takeaway": "t",
            },
        }
    )
    assert pick_density(slide) is Density.HEAVY


def test_explicit_density_wins_over_the_heuristic() -> None:
    deck = load_deck_spec(
        {
            "title": "指定密度",
            "slides": [
                {
                    "archetype": "bullets",
                    "title": "要点",
                    "bullets": [f"第 {index} 条" for index in range(8)],
                    "density": "light",
                }
            ],
        }
    )
    plan = map_deck(deck)
    assert plan.pages[0].density is Density.LIGHT


# ------------------------------------------------------------------- 映射


def test_map_sample_deck_end_to_end() -> None:
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    assert plan.title == "2026 年 Q1 经营复盘"
    assert plan.page_count == 6
    assert [page.archetype for page in plan.pages] == [
        Archetype.COVER,
        Archetype.SECTION,
        Archetype.BULLETS,
        Archetype.DATA,
        Archetype.SPLIT,
        Archetype.CLOSING,
    ]
    assert plan.warnings == ()
    # 品牌色跟着 deck 走
    assert plan.theme.palette.primary == "0F6CBD"


def test_every_fill_lands_in_a_real_slot() -> None:
    """填充的槽名必须真的存在于该版式里——写盘层是照槽名取矩形的。"""
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    for page in plan.pages:
        names = set(page.layout.names)
        for fill in page.fills:
            assert fill.slot in names, f"{page.key} 没有槽位 {fill.slot}"


def test_every_page_has_a_visual_element_or_a_decorated_archetype() -> None:
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    decorated = (Archetype.COVER, Archetype.SECTION, Archetype.CLOSING)
    for page in plan.pages:
        assert page.visual_elements or page.archetype in decorated


def test_section_page_gets_its_number_and_keeps_its_bullets() -> None:
    """章节页：序号是画出来的文字（不是从标题推的），要点必须留下来（升档到 heavy）。"""
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    section = next(page for page in plan.pages if page.archetype is Archetype.SECTION)
    assert section.density is Density.HEAVY
    number = section.fill("number")
    assert number is not None
    assert number.payload == "01"
    body = section.fill("body")
    assert body is not None
    assert body.payload == ("收入与利润", "渠道结构", "风险项")


def test_data_page_gets_chart_takeaway_and_kpis() -> None:
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    data = next(page for page in plan.pages if page.archetype is Archetype.DATA)
    assert data.density is Density.HEAVY
    chart = data.fill("data")
    assert chart is not None and chart.kind == "chart"
    quote = data.fill("quote")
    assert quote is not None and "自营与私域" in str(quote.payload)
    assert all(data.fill(f"kpi_{index}") is not None for index in (1, 2, 3))
    assert set(data.visual_elements) == {"data", "kpi_1", "kpi_2", "kpi_3"}


def test_bullets_page_gets_auto_icons() -> None:
    """要点页的视觉元素是自动补的图标（内容不必自己给图标名）。"""
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    bullets = next(page for page in plan.pages if page.archetype is Archetype.BULLETS)
    icons = bullets.fill("icons")
    assert icons is not None
    assert len(icons.payload) == 3
    assert bullets.visual_elements == ("icons",)


def test_split_page_gets_its_image_and_promotes_to_heavy() -> None:
    """图文页给了图和要点：密度必须升到 heavy（light 档没有图的槽）。"""
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    split = next(page for page in plan.pages if page.archetype is Archetype.SPLIT)
    assert split.density is Density.HEAVY
    image = split.fill("image")
    assert image is not None and image.kind == "image"


def test_cover_keeps_subtitle_and_meta() -> None:
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    cover = plan.pages[0]
    assert cover.density is Density.LIGHT
    assert cover.fill("subtitle") is not None
    assert cover.fill("meta") is not None
    assert cover.fill("accent") is not None  # 装饰：原型保证的视觉元素
    assert cover.fill("footer") is not None


def _page_items(page: PagePlan) -> list[str]:
    """把一页里所有列表槽的条目按槽序摊平（heavy 档落 col_1/col_2，light 落 body）。"""
    collected: list[str] = []
    for name in ("body", "col_1", "col_2"):
        fill = page.fill(name)
        if fill is None:
            continue
        payload = fill.payload
        collected.extend(payload if isinstance(payload, tuple) else [str(payload)])
    return collected


def test_many_bullets_split_into_continuation_pages() -> None:
    """拆页落到**实际页数**上：第二页带「（续）」标题，且两页的要点合起来等于原条目。

    12 条要点在这么宽的槽里也装不下，所以密度先升到 heavy（双栏），再由映射层裁成两页。
    """
    plan = map_deck(load_deck_spec(MANY_BULLETS))
    assert plan.page_count == 2
    first, second = plan.pages
    assert second.continuation and first.continuation is False
    assert second.title.endswith("（续）")
    assert first.density is Density.HEAVY
    assert _page_items(first) + _page_items(second) == list(ONLY_BULLETS)
    assert any("拆成 2 页" in warning for warning in first.warnings)
    assert first.decisions[1].action is OverflowAction.SPLIT


def test_footer_carries_the_page_number() -> None:
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    for index, page in enumerate(plan.pages, start=1):
        footer = page.fill("footer")
        assert footer is not None
        assert footer.payload == {"page_number": index}


def test_chart_category_warning_is_raised() -> None:
    deck = {
        "title": "类别很多",
        "slides": [
            {
                "archetype": "data",
                "title": "月度趋势",
                "chart": {
                    "kind": "column",
                    "categories": [f"第 {index} 月" for index in range(1, 14)],
                    "series": [
                        {"name": "收入", "values": list(range(1, 14))}
                    ],
                    "takeaway": "一路向上",
                },
            }
        ],
    }
    plan = map_deck(load_deck_spec(deck))
    assert any("轴标签" in warning for warning in plan.pages[0].warnings)


def test_speaker_notes_are_passed_through() -> None:
    deck = {
        "title": "带备注",
        "slides": [
            {"archetype": "cover", "title": "封面", "notes": "开场先讲背景"},
        ],
    }
    plan = map_deck(load_deck_spec(deck))
    assert plan.pages[0].speaker_notes == "开场先讲背景"


def test_to_dict_is_the_write_layer_contract() -> None:
    """`to_dict()` 是写盘层的输入：画布、令牌、每槽矩形（英寸与 EMU）都要在里面。"""
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    payload = plan.to_dict()
    assert set(payload) == {"deck", "canvas", "theme", "known_gaps", "warnings", "pages"}
    assert payload["canvas"]["width_in"] == 13.333
    assert payload["canvas"]["height_in"] == 7.5
    assert "静默裁掉" in payload["canvas"]["note"]
    assert payload["theme"]["fonts"]["title"][0] == "Microsoft YaHei"
    assert any("a:ea" in gap or "a:latin" in gap for gap in payload["known_gaps"])

    first_slot = payload["pages"][0]["slots"][0]
    rect_in = first_slot["rect_in"]
    rect_emu = first_slot["rect_emu"]
    assert rect_emu["x"] == round(rect_in["x"] * 914_400)
    assert rect_emu["w"] == round(rect_in["w"] * 914_400)
    assert first_slot["role"]


def test_to_dict_is_json_serializable() -> None:
    """写盘层在 Node 侧，所以计划必须是能过 JSON 的（枚举、模型、元组都要降级）。"""
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    text = plan.to_json(indent=None)
    restored = json.loads(text)
    assert restored["deck"]["page_count"] == 6
    assert json.dumps(restored, ensure_ascii=False)


def test_summary_is_human_readable() -> None:
    plan = map_deck(load_deck_spec(SAMPLE_SPEC))
    summary = plan.summary()
    assert "《2026 年 Q1 经营复盘》 6 页" in summary
    assert "data/heavy" in summary
    assert "chart" in summary


def test_map_deck_accepts_raw_mapping_and_json() -> None:
    from_dict = map_deck(SAMPLE_SPEC)
    from_json = map_deck(json.dumps(SAMPLE_SPEC, ensure_ascii=False))
    assert from_dict.page_count == from_json.page_count == 6


def test_theme_override_applies_to_the_plan() -> None:
    from app.services.deck.theme_tokens import build_theme

    theme = build_theme("客户 A", primary="123456")
    plan = map_deck(SAMPLE_SPEC, theme=theme)
    # deck 里带了品牌色时，以 deck 的为准（它更接近这一份材料的实际要求）
    assert plan.theme.name == "客户 A"
    assert plan.theme.palette.primary == "0F6CBD"
