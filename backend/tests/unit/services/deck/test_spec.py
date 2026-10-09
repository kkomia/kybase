"""deck-spec 契约（幻灯片生成·契约层）。

镜像同构：``app/services/deck/spec.py`` → 本文件。

**这是 LLM 与渲染之间唯一的接口**，所以这里钉两件事：

1. **合法 spec 一定过**，且过完之后字段是"能直接喂给映射层"的形状；
2. **非法 spec 报的是人话**——不是 ``Input should be 'cover', 'section', …``，
   而是 ``未知页型「magic」：可用的是 cover（封面）、…``。第二条比第一条重要：
   这份消息会被回给模型让它重写，说不到点上就是白跑一轮。

四类典型非法（用户点名的那些）各自一条用例：未知页型、空标题、图表缺数据、
某页没有任何视觉元素。
"""

from __future__ import annotations

import json

import pytest

from app.services.deck.layouts import Archetype, Density
from app.services.deck.spec import (
    MAX_BULLETS_PER_SLIDE,
    MAX_SLIDES,
    MAX_TITLE_CHARS,
    ChartKind,
    ChartSpec,
    DeckSpec,
    DeckSpecError,
    ImageIntent,
    ImageIntentKind,
    Kpi,
    contract_hint,
    load_deck_spec,
)

pytestmark = pytest.mark.local

LEGAL_SPEC: dict = {
    "title": "2026 年 Q1 经营复盘",
    "author": "经营分析组",
    "org": "示例公司",
    "brand": {"primary": "0F6CBD", "accent": "E07B1E"},
    "slides": [
        {
            "archetype": "cover",
            "title": "2026 年 Q1 经营复盘",
            "subtitle": "渠道结构与复购质量",
            "meta": "经营分析组 · 2026-04-08",
        },
        {
            "archetype": "bullets",
            "title": "三件已经确认的事",
            "bullets": ["营收同比增长 18%", "毛利率回升到 42%"],
        },
        {
            "archetype": "data",
            "title": "收入结构在变",
            "chart": {
                "kind": "column",
                "categories": ["线下", "自营", "私域"],
                "series": [{"name": "Q4 占比", "values": [51, 18, 16]}],
                "takeaway": "线下让出的份额被自营与私域接住了",
            },
            "kpis": [{"label": "自营占比", "value": "18%", "delta": "+6pp"}],
        },
        {
            "archetype": "closing",
            "title": "下一步：把转化率做成能力",
            "subtitle": "Q2 重点动作见附页",
        },
    ],
}


def _spec(**overrides: object) -> dict:
    spec = json.loads(json.dumps(LEGAL_SPEC))
    spec.update(overrides)
    return spec


def _one_slide(slide: dict) -> dict:
    return {"title": "一份 deck", "slides": [slide]}


def _error(payload: dict) -> str:
    with pytest.raises(DeckSpecError) as caught:
        load_deck_spec(payload)
    return caught.value.message


def test_legal_spec_passes() -> None:
    deck = load_deck_spec(LEGAL_SPEC)
    assert isinstance(deck, DeckSpec)
    assert deck.title == "2026 年 Q1 经营复盘"
    assert [slide.archetype for slide in deck.slides] == [
        Archetype.COVER,
        Archetype.BULLETS,
        Archetype.DATA,
        Archetype.CLOSING,
    ]
    assert deck.slides[2].chart is not None
    assert deck.slides[2].chart.kind is ChartKind.COLUMN
    assert deck.brand is not None
    assert deck.brand.primary == "0F6CBD"
    assert deck.slides[1].density is Density.AUTO
    # 从第一页到最后一页都合规的结构，不该有任何提醒
    assert deck.structure_notes() == []


def test_load_accepts_a_json_string() -> None:
    deck = load_deck_spec(json.dumps(LEGAL_SPEC, ensure_ascii=False))
    assert deck.slides[0].title == "2026 年 Q1 经营复盘"


def test_broken_json_says_where() -> None:
    message = _error("{不是 JSON")
    assert "不是合法的 JSON" in message
    assert "行" in message and "列" in message


def test_unknown_archetype_lists_the_choices() -> None:
    """非法 ①：未知页型 —— 必须把可用的页型列出来，否则模型只能瞎试。"""
    message = _error(_one_slide({"archetype": "magic", "title": "封面"}))
    assert "未知页型「magic」" in message
    for archetype in Archetype:
        assert archetype.value in message
    assert "封面" in message and "收尾页" in message


def test_empty_title_is_rejected() -> None:
    """非法 ②：空标题。"""
    message = _error(_one_slide({"archetype": "cover", "title": "   "}))
    assert "标题不能为空" in message
    assert "第 1 页" in message


def test_title_too_long_is_rejected() -> None:
    message = _error(_one_slide({"archetype": "cover", "title": "标" * (MAX_TITLE_CHARS + 1)}))
    assert f"超过上限 {MAX_TITLE_CHARS} 字" in message
    assert "放进 body 或 bullets" in message


def test_chart_without_data_names_the_missing_fields() -> None:
    """非法 ③：图表缺数据 —— 要说清缺的是"类别"和"数据系列"，而不是"字段校验失败"。"""
    payload = {
        "title": "一份 deck",
        "slides": [
            {"archetype": "cover", "title": "封面"},
            {"archetype": "data", "title": "收入结构", "chart": {"kind": "column"}},
        ],
    }
    message = _error(payload)
    assert "缺了必填的「类别」" in message
    assert "缺了必填的「数据系列」" in message
    assert "第 2 页「收入结构」" in message


def test_page_without_any_visual_element_is_rejected() -> None:
    """非法 ④：某页没有任何视觉元素 —— 纯标题页在投影上等于没有信息层次。"""
    payload = {
        "title": "一份 deck",
        "slides": [
            {"archetype": "cover", "title": "封面"},
            {"archetype": "bullets", "title": "三点结论"},
        ],
    }
    message = _error(payload)
    assert "没有任何视觉元素" in message
    assert "第 2 页「三点结论」" in message
    assert "图标" in message and "图表" in message


def test_decor_archetypes_are_exempt_from_the_visual_rule() -> None:
    """封面/章节页/收尾页的视觉元素由原型的装饰形状保证，不该被这条规则挡下。"""
    deck = load_deck_spec(
        {
            "title": "一份 deck",
            "slides": [
                {"archetype": "cover", "title": "封面"},
                {"archetype": "section", "title": "一、整体表现"},
                {"archetype": "closing", "title": "谢谢"},
            ],
        }
    )
    assert len(deck.slides) == 3


def test_chart_series_length_must_match_categories() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "data",
                "title": "收入",
                "chart": {
                    "kind": "column",
                    "categories": ["a", "b"],
                    "series": [{"name": "x", "values": [1]}],
                    "takeaway": "一句话",
                },
            }
        )
    )
    assert "系列「x」有 1 个数值，但类别有 2 个" in message


def test_pie_chart_rejects_multiple_series() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "data",
                "title": "份额",
                "chart": {
                    "kind": "pie",
                    "categories": ["a", "b"],
                    "series": [
                        {"name": "x", "values": [1, 2]},
                        {"name": "y", "values": [3, 4]},
                    ],
                    "takeaway": "一句话",
                },
            }
        )
    )
    assert "只能有一个系列" in message


def test_chart_requires_a_takeaway() -> None:
    """图不自己说话：一句结论是必给的。"""
    message = _error(
        _one_slide(
            {
                "archetype": "data",
                "title": "收入",
                "chart": {
                    "kind": "line",
                    "categories": ["a"],
                    "series": [{"name": "x", "values": [1]}],
                },
            }
        )
    )
    assert "一句结论" in message


def test_chart_given_but_not_an_object() -> None:
    message = _error(_one_slide({"archetype": "data", "title": "收入", "chart": "column"}))
    assert "图表要写成一个对象" in message


def test_unknown_chart_kind_lists_choices() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "data",
                "title": "收入",
                "chart": {
                    "kind": "radar",
                    "categories": ["a"],
                    "series": [{"name": "x", "values": [1]}],
                    "takeaway": "t",
                },
            }
        )
    )
    assert "未知的图表类型「radar」" in message
    assert "column" in message


def test_payload_on_the_wrong_archetype_is_rejected() -> None:
    """契约跟着版式走：封面没有图表槽，就不该收图表。"""
    message = _error(
        _one_slide(
            {
                "archetype": "cover",
                "title": "封面",
                "chart": {
                    "kind": "pie",
                    "categories": ["a"],
                    "series": [{"name": "x", "values": [1]}],
                    "takeaway": "t",
                },
            }
        )
    )
    assert "收不了图表" in message
    assert "数据页" in message


def test_bullets_on_cover_are_rejected() -> None:
    message = _error(_one_slide({"archetype": "cover", "title": "封面", "bullets": ["一条"]}))
    assert "收不了要点" in message


def test_image_intent_needs_a_prompt_for_generate() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "split",
                "title": "图文",
                "bullets": ["一条"],
                "image": {"kind": "generate", "alt": "门店照片"},
            }
        )
    )
    assert "必须给 prompt" in message


def test_image_intent_needs_a_path_for_local() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "split",
                "title": "图文",
                "bullets": ["一条"],
                "image": {"kind": "local", "alt": "门店照片"},
            }
        )
    )
    assert "必须给 path" in message


def test_image_intent_needs_alt() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "split",
                "title": "图文",
                "bullets": ["一条"],
                "image": {"kind": "search", "prompt": "门店"},
            }
        )
    )
    assert "缺了必填的「图片说明」" in message


def test_blank_bullet_is_rejected() -> None:
    message = _error(
        _one_slide({"archetype": "bullets", "title": "要点", "bullets": ["好的", "  "]} )
    )
    assert "第 2 条要点是空的" in message


def test_too_many_bullets_is_rejected() -> None:
    bullets = [f"第 {index} 条" for index in range(MAX_BULLETS_PER_SLIDE + 1)]
    message = _error(_one_slide({"archetype": "bullets", "title": "要点", "bullets": bullets}))
    assert f"上限 {MAX_BULLETS_PER_SLIDE} 条" in message


def test_too_many_kpis_is_rejected() -> None:
    message = _error(
        _one_slide(
            {
                "archetype": "data",
                "title": "指标",
                "kpis": [{"label": f"指标{index}", "value": "1"} for index in range(4)],
                "chart": {
                    "kind": "bar",
                    "categories": ["a"],
                    "series": [{"name": "x", "values": [1]}],
                    "takeaway": "t",
                },
            }
        )
    )
    assert "最多 3 个" in message


def test_unknown_field_is_rejected_not_ignored() -> None:
    """默认 pydantic 会忽略多余字段——那会让模型写错字段名自己却不知道。"""
    message = _error(_one_slide({"archetype": "bullets", "title": "要点", "bullet": ["一条"]}))
    assert "多了不认识的字段「bullet」" in message


def test_brand_color_must_be_hex() -> None:
    message = _error(_spec(brand={"primary": "深蓝"}))
    assert "不是 6 位十六进制" in message
    assert "1B4F8A" in message


def test_brand_color_accepts_hash_prefix_and_normalizes() -> None:
    deck = load_deck_spec(_spec(brand={"primary": "#0f6cbd"}))
    assert deck.brand is not None
    assert deck.brand.primary == "0F6CBD"


def test_empty_slides_is_rejected() -> None:
    message = _error({"title": "空 deck", "slides": []})
    assert "页面" in message


def test_too_many_slides_is_rejected() -> None:
    slides = [{"archetype": "cover", "title": f"第 {index} 页"} for index in range(MAX_SLIDES + 1)]
    message = _error({"title": "过长", "slides": slides})
    assert str(MAX_SLIDES) in message


def test_deck_title_is_required() -> None:
    message = _error({"title": "", "slides": [{"archetype": "cover", "title": "封面"}]})
    assert "这份 deck 要有标题" in message


def test_structure_notes_flag_unusual_decks() -> None:
    """不常见的结构只提醒、不拒绝——那才是"该说的话"该待的地方。"""
    deck = load_deck_spec(
        {
            "title": "一份 deck",
            "slides": [
                {"archetype": "bullets", "title": "开门见山", "bullets": ["一条"]},
                {"archetype": "closing", "title": "收尾"},
            ],
        }
    )
    notes = deck.structure_notes()
    assert any("第一页不是封面" in note for note in notes)
    assert any("没有数据页" in note for note in notes)
    assert not any("最后一页不是收尾页" in note for note in notes)


def test_models_can_be_built_directly() -> None:
    """契约同时是**可编程**的：别的调用方（测试、脚本、未来的工具层）能直接造模型。"""
    slide = {
        "archetype": "split",
        "title": "图文页",
        "bullets": ["一条要点"],
        "image": ImageIntent(kind=ImageIntentKind.GENERATE, prompt="一张示意图", alt="示意图"),
    }
    deck = DeckSpec(title="直接构造", slides=[slide])
    assert isinstance(deck.slides[0].image, ImageIntent)
    assert isinstance(deck.slides[0].image.kind, ImageIntentKind)
    assert isinstance(ChartSpec, type)
    assert isinstance(Kpi(label="指标", value="1"), Kpi)


def test_contract_hint_covers_every_archetype_and_the_visual_rule() -> None:
    hint = contract_hint()
    for archetype in Archetype:
        assert f"{archetype.value}（" in hint
    assert "每页必须至少有一个非文字元素" in hint
    assert "takeaway" in hint  # 数据页必给的字段要写在说明里
    assert "image" in hint
