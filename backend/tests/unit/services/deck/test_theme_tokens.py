"""设计令牌（幻灯片生成·令牌层）。

镜像同构：``app/services/deck/theme_tokens.py`` → 本文件。

这里钉的是**三条实测硬约束**，它们都是"不报错的错"（错了要么静默裁切、要么静默换字体），
所以只能靠测试记住：

1. 画布必须显式是 13.333 × 7.5 英寸——库自带的 16:9 布局是 10 × 5.625，
   按 13.333 写坐标会被**静默裁掉**页脚与图表轴；
2. 不能嵌字体，目标机缺字体时**不报错、被替换**，中文宽度按实测再宽约 6%，
   所以"字宽预算"必须带上 `font_width_margin`；
3. 图表里的中文**不吃** ``*FontFace``（chart xml 只有 ``a:latin``）——这条登记在
   令牌里（`chart_font_gap`），等写盘层处理。
"""

from __future__ import annotations

import pytest

from app.services.deck.theme_tokens import (
    CJK_FONT_WHITELIST,
    DEFAULT_THEME,
    EMU_PER_INCH,
    LATIN_FALLBACK_FONT,
    SLIDE_HEIGHT_IN,
    SLIDE_WIDTH_IN,
    FontChoice,
    build_theme,
    inches_to_emu,
    inches_to_points,
    theme_with_brand,
)

pytestmark = pytest.mark.local


def test_canvas_is_16_9_13_333_by_7_5() -> None:
    """画布尺寸是硬约束：整个几何层（`layouts.py`）都以它为准。"""
    assert (SLIDE_WIDTH_IN, SLIDE_HEIGHT_IN) == (13.333, 7.5)
    assert pytest.approx(16 / 9, abs=0.002) == SLIDE_WIDTH_IN / SLIDE_HEIGHT_IN


def test_emu_conversion_matches_ooxml() -> None:
    assert EMU_PER_INCH == 914_400
    assert inches_to_emu(1.0) == 914_400
    assert inches_to_emu(0.0) == 0
    assert inches_to_emu(13.333) == 12_191_695
    assert inches_to_points(1.0) == 72.0


def test_font_whitelist_covers_both_platforms() -> None:
    """白名单必须同时有 Windows 与 macOS 的落点：本机开发在 Windows，
    但 .pptx 会被拿到 Mac 上打开。"""
    families = {item.family for item in CJK_FONT_WHITELIST}
    assert {"Microsoft YaHei", "DengXian", "PingFang SC"} <= families
    platforms = {platform for item in CJK_FONT_WHITELIST for platform in item.platforms}
    assert platforms == {"windows", "macos"}
    assert all(item.cjk for item in CJK_FONT_WHITELIST)


def test_font_whitelist_keeps_chinese_aliases() -> None:
    """别名（中文字体名）要留着：模型可能写「微软雅黑」，而 OOXML 里要写英文名。"""
    aliases = {alias for item in CJK_FONT_WHITELIST for alias in item.aliases}
    assert "微软雅黑" in aliases
    assert "等线" in aliases
    assert "苹方-简" in aliases


def test_latin_fallback_is_not_a_cjk_font() -> None:
    """Arial 只是西文那一段的兜底——它不含汉字，不能拿来充当中文落点。"""
    assert LATIN_FALLBACK_FONT.family == "Arial"
    assert LATIN_FALLBACK_FONT.cjk is False
    assert isinstance(LATIN_FALLBACK_FONT, FontChoice)


def test_fonts_are_bound_in_pairs() -> None:
    """标题/正文成对绑定：度量系数挂在链上，两套链会让溢出判定分叉。"""
    fonts = DEFAULT_THEME.fonts
    assert fonts.title[0] == "Microsoft YaHei"
    assert fonts.body[0] == "DengXian"
    assert fonts.title[-1] == fonts.body[-1] == "Arial"
    assert DEFAULT_THEME.font_chain("title") == fonts.title
    assert DEFAULT_THEME.font_chain("body") == fonts.body


def test_type_scale_starts_at_the_documented_sizes() -> None:
    scale = DEFAULT_THEME.type_scale
    assert scale.cover_title_pt == 40
    assert scale.section_title_pt == 36
    assert scale.slide_title_pt == 32
    assert scale.body_pt == 18
    assert scale.min_body_pt == 14  # 正文下限：再小投影到最后一排就读不了
    assert scale.min_title_pt == 24


def test_size_steps_descend_and_respect_floors() -> None:
    scale = DEFAULT_THEME.type_scale
    for steps in (scale.body_steps, scale.subtitle_steps, scale.title_steps("bullets")):
        assert list(steps) == sorted(steps, reverse=True)
        assert all(size >= scale.min_body_pt for size in steps)
    assert scale.body_steps == (18, 16, 14)
    assert scale.subtitle_steps == (20, 18, 16, 14)
    assert scale.title_steps("cover") == (40, 36, 32, 28, 24)
    assert scale.title_steps("bullets") == (32, 28, 24)
    assert scale.title_steps("section")[0] == 36
    assert scale.caption_steps == (14,)


def test_spacing_baseline_is_half_and_three_tenths() -> None:
    """0.5 / 0.3 是全流程基准，`layouts.py` 的安全区与槽位间隙都由它们推出来。"""
    spacing = DEFAULT_THEME.spacing
    assert spacing.margin_x_in == 0.5
    assert spacing.block_gap_in == 0.3
    assert spacing.safe_left == 0.5
    assert spacing.safe_right == pytest.approx(SLIDE_WIDTH_IN - 0.5)
    assert spacing.safe_bottom == pytest.approx(SLIDE_HEIGHT_IN - 0.45)
    assert spacing.content_width == pytest.approx(12.333)


def test_measure_budget_carries_the_font_substitution_margin() -> None:
    """度量余量：按白名单里最宽的字体 + 替换余量（实测 +6%），中文整 1.0 em。"""
    measure = DEFAULT_THEME.measure
    assert measure.cjk_em == 1.00
    assert measure.font_width_margin == pytest.approx(1.06)
    assert 1.0 < measure.font_width_margin <= 1.15
    # 断行只用到可用宽度的 97%：留下"最后一格挤不下"的余地
    assert 0.9 <= measure.fit_slack < 1.0
    assert measure.line_spacing >= 1.1  # 行距不能压到 1.0，否则行会贴在一起
    assert measure.latin_lower_em < measure.latin_upper_em < measure.cjk_em


def test_shape_style_has_an_icon_cycle_for_bullet_pages() -> None:
    """要点页的视觉元素就是图标：没有它，"每页至少一个视觉元素"这条约束就落不了地。"""
    shapes = DEFAULT_THEME.shapes
    assert len(shapes.icon_cycle) >= 4
    assert len(set(shapes.icon_cycle)) == len(shapes.icon_cycle)
    assert shapes.icon_for(0) == shapes.icon_cycle[0]
    assert shapes.icon_for(len(shapes.icon_cycle)) == shapes.icon_cycle[0]
    assert shapes.accent_bar_in > 0


def test_chart_font_gap_is_registered() -> None:
    """已知缺口要留在令牌里，别让写盘层以为"中文在图表里也吃字体"。"""
    assert "a:latin" in DEFAULT_THEME.chart_font_gap


def test_palette_series_colors_cycle() -> None:
    palette = DEFAULT_THEME.palette
    assert len(palette.chart_series) >= 5
    assert palette.series_color(0) == palette.chart_series[0]
    assert palette.series_color(len(palette.chart_series)) == palette.chart_series[0]


def test_brand_recolor_follows_primary() -> None:
    themed = DEFAULT_THEME.with_brand(primary="#0f6cbd", accent="e07b1e")
    assert themed.palette.primary == "0F6CBD"
    assert themed.palette.accent == "E07B1E"
    assert themed.palette.chart_series[0] == "0F6CBD"
    # 不改色时保持默认（空字符串不该把品牌色清掉）
    assert DEFAULT_THEME.with_brand().palette.primary == DEFAULT_THEME.palette.primary


def test_build_theme_overrides_fonts_and_colors() -> None:
    theme = build_theme(
        "客户 A",
        primary="123456",
        title_fonts=("Source Han Sans", "Arial"),
        body_fonts=("Source Han Serif", "Arial"),
    )
    assert theme.name == "客户 A"
    assert theme.palette.primary == "123456"
    assert theme.font_chain("title") == ("Source Han Sans", "Arial")
    assert theme.font_chain("body") == ("Source Han Serif", "Arial")
    # 阶梯与度量沿用默认：改色不该把字号改了
    assert theme.type_scale == DEFAULT_THEME.type_scale
    assert theme.measure == DEFAULT_THEME.measure


def test_theme_with_brand_accepts_none() -> None:
    assert theme_with_brand(None, primary="ABCDEF").palette.primary == "ABCDEF"
