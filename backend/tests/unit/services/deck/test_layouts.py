"""版式原型（幻灯片生成·原型层）。

镜像同构：``app/services/deck/layouts.py`` → 本文件。

这里的**主角是那条机械检查**（`validate_layouts`）：12 个版式的全部槽位逐对算面积交集、
逐块比对画布与安全区。为什么要做成机械的——因为"差 0.001 英寸"这种错肉眼在代码里看不出来，
在投影上就是文字压线或贴边。实测抓到过一次：半幅宽 5.9165 四舍五入成 5.917 之后，
第二栏右边界变成 12.834，**顶出安全区 0.001 英寸**。

因此本文件有两条同样重要的断言：

1. 12 个版式**零违规**（正向）；
2. 人为塞一个重叠/越界的槽进去，检查**必须报出来**（反向）。

只做第 1 条的检查是危险的：它可能恒真，而恒真的检查等于没有检查。
"""

from __future__ import annotations

import pytest

from app.services.deck.layouts import (
    ARCHETYPE_LABELS,
    ARCHETYPES,
    DENSITIES,
    LAYOUTS,
    Archetype,
    Density,
    Layout,
    Rect,
    Slot,
    SlotRole,
    get_layout,
    iter_layouts,
    safe_rect,
    validate_layouts,
)
from app.services.deck.theme_tokens import (
    DEFAULT_THEME,
    SLIDE_HEIGHT_IN,
    SLIDE_WIDTH_IN,
    ThemeTokens,
)

pytestmark = pytest.mark.local

ALL_ARCHETYPES = (Archetype.COVER, Archetype.SECTION, Archetype.BULLETS)


def test_rect_rounds_to_three_decimals() -> None:
    rect = Rect(0.5004, 1.6004, 5.9165, 5.0004)
    assert (rect.x, rect.y, rect.w, rect.h) == (0.5, 1.6, 5.917, 5.0)
    assert rect.x2 == 6.417
    assert rect.y2 == 6.6
    assert rect.area == pytest.approx(5.917 * 5.0)


def test_rect_intersection_and_containment() -> None:
    left = Rect(0.5, 1.6, 5.0, 5.0)
    right = Rect(5.5, 1.6, 5.0, 5.0)
    assert left.intersection_area(right) == 0.0
    assert not left.overlaps(right)  # 边贴边不算重叠
    assert left.intersection_area(Rect(5.0, 1.6, 5.0, 5.0)) == pytest.approx(0.5 * 5.0)
    assert Rect(0.0, 0.0, 13.333, 7.5).contains(left)
    assert not left.contains(Rect(0.0, 0.0, 1.0, 1.0))


def test_rect_to_emu_is_consistent_with_inches() -> None:
    rect = Rect(0.5, 1.6, 5.9165, 5.0)
    emu = rect.to_emu()
    assert emu["x"] == round(0.5 * 914_400)
    assert emu["y"] == round(1.6 * 914_400)
    assert set(emu) == {"x", "y", "w", "h"}


def test_twelve_layouts_one_per_archetype_and_density() -> None:
    assert len(LAYOUTS) == 12
    assert len(ARCHETYPES) == 6
    assert DENSITIES == (Density.LIGHT, Density.HEAVY)
    for archetype in ARCHETYPES:
        for density in DENSITIES:
            assert LAYOUTS[(archetype, density)].key == f"{archetype.value}/{density.value}"
    assert tuple(iter_layouts()) == tuple(LAYOUTS[(a, d)] for a in ARCHETYPES for d in DENSITIES)


def test_every_layout_has_a_title_and_a_footer() -> None:
    for layout in iter_layouts():
        assert layout.title_slot in layout.names
        assert layout.footer_slot in layout.names
        assert layout.slot(layout.title_slot).required
        assert layout.slot(layout.title_slot).role is SlotRole.TITLE
        assert layout.slot(layout.footer_slot).role is SlotRole.DECORATION


def test_every_layout_has_at_least_one_visual_slot() -> None:
    """原型层保证：要么有装图表/图片/图标的槽，要么有必绘的装饰形状。

    这条是"每页至少一个视觉元素"在版式这一侧的对应面——两者都不成立时，
    那一页无论内容多好都只能输出纯文字。
    """
    for layout in iter_layouts():
        visuals = layout.slots_with_role(
            SlotRole.ICONS, SlotRole.CHART, SlotRole.IMAGE, SlotRole.KPI
        )
        decorations = layout.slots_with_role(SlotRole.DECORATION)
        assert visuals or decorations, f"{layout.key} 既没有视觉元素槽也没有装饰形状"


def test_text_slots_declare_font_role_and_line_caps() -> None:
    for layout in iter_layouts():
        for slot in layout.slots:
            if slot.is_text:
                assert slot.font_role, f"{layout.key}/{slot.name} 没有 font_role"
                assert slot.max_lines >= 1


def test_declared_columns_exist_and_are_lists() -> None:
    for layout in iter_layouts():
        for name in layout.columns:
            assert name in layout.names
            assert layout.slot(name).role is SlotRole.LIST


def test_mechanical_check_passes_for_all_twelve_layouts() -> None:
    """**核心断言**：12 个版式的全部槽位互不重叠、不越界、不出安全区。"""
    assert validate_layouts() == []


def test_slot_names_are_unique_per_layout() -> None:
    for layout in iter_layouts():
        assert len(set(layout.names)) == len(layout.names)


def test_check_detects_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向验证：塞一对重叠的槽进去，检查必须报出来（否则它是恒真的假检查）。"""
    base = get_layout(Archetype.BULLETS, Density.LIGHT)
    broken = Layout(
        archetype=base.archetype,
        density=base.density,
        slots=(
            base.slot("title"),
            Slot(name="icons", rect=Rect(0.5, 1.6, 4.0, 5.0), role=SlotRole.ICONS),
            Slot(name="body", rect=Rect(2.0, 1.6, 4.0, 5.0), role=SlotRole.LIST, max_lines=6),
            base.slot("footer"),
        ),
    )
    monkeypatch.setitem(LAYOUTS, (Archetype.BULLETS, Density.LIGHT), broken)
    problems = validate_layouts()
    assert any("槽位重叠" in item for item in problems)
    assert any("icons" in item and "body" in item for item in problems)


def test_check_detects_out_of_safe_area(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向验证之二：越出安全区与越出画布都要能被抓到。"""
    base = get_layout(Archetype.CLOSING, Density.LIGHT)
    broken = Layout(
        archetype=base.archetype,
        density=base.density,
        slots=(
            base.slot("title"),
            Slot(name="body", rect=Rect(12.5, 1.6, 3.0, 2.0), role=SlotRole.LIST, max_lines=2),
            base.slot("footer"),
        ),
    )
    monkeypatch.setitem(LAYOUTS, (Archetype.CLOSING, Density.LIGHT), broken)
    problems = validate_layouts()
    assert any("越出画布" in item for item in problems)
    assert any("越出安全区" in item for item in problems)


def test_check_follows_the_theme_spacing() -> None:
    """几何是从间距推出来的：把边距放大之后，原来的槽位就该被判越界。

    这条同时说明 `validate_layouts(theme)` 的语义：它核对的是"给定间距下的安全区"，
    所以换主题（改间距）时必须重新核对一次，而不是默认沿用默认主题的结论。
    """
    wide = ThemeTokens(spacing=DEFAULT_THEME.spacing.__class__(margin_x_in=1.5))
    problems = validate_layouts(wide)
    assert problems
    assert all("越出安全区" in item for item in problems)


def test_safe_rect_matches_spacing() -> None:
    safe = safe_rect()
    assert (safe.x, safe.y) == (0.5, 0.4)
    assert safe.x2 == pytest.approx(SLIDE_WIDTH_IN - 0.5)
    assert safe.y2 == pytest.approx(SLIDE_HEIGHT_IN - 0.45)


def test_all_slots_stay_inside_canvas() -> None:
    for layout in iter_layouts():
        for slot in layout.slots:
            assert slot.rect.x >= 0 and slot.rect.x2 <= SLIDE_WIDTH_IN
            assert slot.rect.y >= 0 and slot.rect.y2 <= SLIDE_HEIGHT_IN


def test_density_variants_differ_in_structure() -> None:
    """两档不是"同一个版式换字号"：heavy 档要有更多槽位或分栏，否则它没有存在意义。"""
    for archetype in ARCHETYPES:
        light = get_layout(archetype, Density.LIGHT)
        heavy = get_layout(archetype, Density.HEAVY)
        assert len(heavy.slots) >= len(light.slots)
        assert (len(heavy.slots), bool(heavy.columns)) != (len(light.slots), bool(light.columns))


def test_get_layout_rejects_auto_density() -> None:
    with pytest.raises(ValueError, match="AUTO"):
        get_layout(Archetype.COVER, Density.AUTO)


def test_archetype_labels_are_chinese() -> None:
    assert set(ARCHETYPE_LABELS) == set(ARCHETYPES)
    assert ARCHETYPE_LABELS[Archetype.BULLETS] == "要点页"
    assert all(label for label in ARCHETYPE_LABELS.values())


def test_unknown_slot_reports_clearly() -> None:
    layout = get_layout(Archetype.DATA, Density.LIGHT)
    with pytest.raises(KeyError, match="没有槽位"):
        layout.slot("nope")


def test_slot_visual_flag_matches_roles() -> None:
    for layout in iter_layouts():
        for slot in layout.slots:
            expected = slot.role in (SlotRole.ICONS, SlotRole.CHART, SlotRole.IMAGE, SlotRole.KPI)
            assert slot.is_visual is expected


def test_subset_helpers_agree() -> None:
    """`ALL_ARCHETYPES` 只是本文件的一个便捷常量，别让它与真正的枚举走散。"""
    assert set(ALL_ARCHETYPES) <= set(ARCHETYPES)
