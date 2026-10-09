"""写盘层·Python 桥与端到端（幻灯片生成·写盘通路）。

镜像同构：``app/services/deck/render.py`` → 本文件。

**这一组里只有一条是"真写盘"**（``test_end_to_end_...``）：用前四层造一份中文计划、
调 `node scripts/deck/render.mjs` 写出 .pptx、再用 `verify_pptx` 解包逐条核对。
其余几条都不需要 Node：

- 计划 → JSON 的形状（`DeckPlan.to_dict()` 就是写盘层的输入契约）；
- **没有 Node 时降级成一句人话**——这条尤其要能离线跑，因为它讲的正是"没有 Node 的机器"；
- 计划不合法时，错误话里要指明第几页哪个槽（渲染器那条人话经桥原样带回来）。

需要 Node 的用例在缺 Node 时**明确 skip 并说清原因**：不说原因的 skip 会变成
"以为验过了，其实没跑"。
"""

from __future__ import annotations

import json
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from app.services.deck import (
    DeckRenderError,
    find_node,
    load_deck_spec,
    map_deck,
    node_requirement,
    plan_json,
    render_deck,
    render_script,
    verify_pptx,
)
from app.services.deck import render as render_module

pytestmark = pytest.mark.local

#: 缺 Node 时的 skip 说明。**必须写清"什么没被验证"**，而不只是"没有 node"。
_NO_NODE = (
    "本机没有 Node.js：端到端渲染没跑（`scripts/deck/render.mjs` 是 Node 脚本）。"
    "装一个 Node 20 以上进 PATH，或用 KYLAB_NODE 指向可执行文件，这条就会跑"
)

#: 一份 5 页中文计划：封面 / 要点（heavy，带图标列）/ 数据（原生柱状图 + 指标卡）/
#: 图文（本地图片）/ 收尾。四类载荷各出现一次，正好把写盘层的四条分支都走到。
DECK: dict[str, Any] = {
    "title": "写盘层端到端",
    "slides": [
        {
            "archetype": "cover",
            "title": "给 agent 的 PPT 补上版式",
            "subtitle": "从标题加要点到能直接投到会议室",
            "meta": "2026-10-05 · 自动化测试",
            "notes": "封面备注",
        },
        {
            "archetype": "bullets",
            "density": "heavy",
            "title": "现状与差距",
            "bullets": [
                "只有标题加要点，没有任何版式",
                "图表、图片、母版、页码一律没有",
                "模型直接摆坐标不可控：差半英寸就压线",
            ],
            "notes": "要点页备注",
        },
        {
            "archetype": "data",
            "density": "heavy",
            "title": "能力覆盖对比",
            "kpis": [{"label": "版式槽位", "value": "12", "delta": "6 页型两档密度"}],
            "chart": {
                "kind": "column",
                "categories": ["版式", "图表", "页码"],
                "series": [
                    {"name": "现役实现", "values": [0, 0, 0]},
                    {"name": "本轮写盘层", "values": [1, 1, 1]},
                ],
                "takeaway": "三项能力从无到有，中文那两条还在打补丁",
                "unit": "覆盖（1 = 有）",
            },
            "notes": "数据页备注",
        },
        {
            "archetype": "split",
            "density": "heavy",
            "title": "两层分工",
            "bullets": ["Python 决定画什么、画在哪", "Node 只把矩形落成 OOXML"],
            "image": {"kind": "local", "path": "brand.png", "alt": "品牌色块"},
            "notes": "图文页备注",
        },
        {
            "archetype": "closing",
            "title": "下一步：切换现役路径",
            "subtitle": "现役路径先并存，切换单独一轮",
            "notes": "收尾页备注",
        },
    ],
}


def png_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """不引依赖造一张**真 PNG**（写盘层要读它的头部尺寸，图必须是合法的）。

    `test_verify.py` 也用它——那边手写的合成包里那张图要能被解出真实尺寸，
    才能验"被拉变形"那条检查（假字节读不出宽高，检查会合理地跳过）。
    """
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(payload)) + body + crc

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _plan(tmp_path: Path) -> Any:
    """前三层 → 计划。图片造在 ``tmp_path`` 下（相对路径按 plan 所在目录解析）。"""
    (tmp_path / "brand.png").write_bytes(png_bytes(48, 18, (0x1B, 0x4F, 0x8A)))
    return map_deck(load_deck_spec(json.dumps(DECK, ensure_ascii=False)))


# ------------------------------------------------------------------ 计划 → JSON

def test_deck_plan_serialises_to_the_json_the_write_layer_eats() -> None:
    """`DeckPlan.to_json()` 就是写盘层的输入契约：画布、令牌、每页每槽的矩形与载荷。"""
    plan = map_deck(load_deck_spec(json.dumps(DECK, ensure_ascii=False)))

    payload = json.loads(plan_json(plan))

    assert payload["canvas"]["width_in"] == 13.333
    assert payload["canvas"]["height_in"] == 7.5
    assert "静默裁掉" in payload["canvas"]["note"]
    assert payload["theme"]["fonts"]["title"][0] == "Microsoft YaHei"
    assert [page["layout"] for page in payload["pages"]] == [
        "cover/light",
        "bullets/heavy",
        "data/heavy",
        "split/heavy",
        "closing/light",
    ]
    for page in payload["pages"]:
        for slot in page["slots"]:
            # 每个槽都要有英尺与 EMU 两套矩形——写盘层照第一套画，第二套用来核对
            assert set(slot["rect_in"]) == {"x", "y", "w", "h"}
            assert set(slot["rect_emu"]) == {"x", "y", "w", "h"}


def test_plan_json_keeps_chinese_unreadable_only_to_the_eye_of_json() -> None:
    """中文不转义（``ensure_ascii=False``）：计划是给人看的，也是给排查看的。"""
    plan = map_deck(load_deck_spec(json.dumps(DECK, ensure_ascii=False)))

    assert "给 agent 的 PPT 补上版式" in plan_json(plan)


def test_broken_plan_json_is_a_sentence_not_a_crash() -> None:
    with pytest.raises(DeckRenderError, match="不是合法 JSON"):
        plan_json("{这不是 json")


def test_unknown_plan_type_is_named_back() -> None:
    with pytest.raises(DeckRenderError, match="收到 int"):
        plan_json(42)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 没有 Node 时的降级

def test_without_node_the_requirement_is_a_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 Node 时报**"装什么"**，而不是抛 FileNotFoundError。

    这条要在没装 Node 的机器上也成立，所以用的是**打桩**而不是改 PATH：
    改 PATH 会影响同进程里的别的东西，而这里要验的只是"查找失败时怎么说话"。
    """
    monkeypatch.setattr(render_module, "find_node", lambda: None)

    problem = node_requirement()

    assert "Node.js" in problem
    assert "KYLAB_NODE" in problem or "PATH" in problem


def test_without_node_rendering_degrades_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(render_module, "find_node", lambda: None)
    target = tmp_path / "deck.pptx"

    with pytest.raises(DeckRenderError) as excinfo:
        render_deck(_plan(tmp_path), target)

    assert "Node.js" in excinfo.value.message
    assert not target.exists(), "渲染没做成就不该留下半个文件"


def test_missing_render_script_is_reported_not_guessed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """脚本不在（不是完整检出）：说的是"找不到写盘层脚本"，不是"没装 Node"。"""
    monkeypatch.setattr(render_module, "render_script", lambda: tmp_path / "nope.mjs")

    problem = node_requirement()

    assert "scripts/deck/render.mjs" in problem
    assert "Node.js" not in problem


def test_node_and_script_are_where_we_say_they_are() -> None:
    """这台机器上能找到的东西，就该找得到（找不到时由上面的降级用例负责说清楚）。"""
    if find_node() is None:
        pytest.skip(_NO_NODE)
    assert render_script().is_file()


# ------------------------------------------------------------------ 真写盘

@pytest.mark.skipif(find_node() is None, reason=_NO_NODE)
def test_end_to_end_chinese_deck_renders_and_verifies(tmp_path: Path) -> None:
    """端到端：计划 → .pptx → 解包核对。**这一条是"写盘层真的通了"的证明。**

    断言分两截：先是桥带回来的摘要（页数、版式序列、图表数、图片数、图表字体补丁），
    再是解包核对（`verify_pptx` 的 11 条）——摘要能对上是"命令跑通了"，
    解包能对上才是"文件是计划说的那样"。
    """
    plan = _plan(tmp_path)
    target = tmp_path / "out" / "deck.pptx"

    result = render_deck(plan, target, base_dir=tmp_path)

    assert result.path == target
    assert target.is_file() and zipfile.is_zipfile(target)
    assert result.pages == plan.page_count == 5
    assert result.layouts == tuple(page.key for page in plan.pages)
    assert result.charts == 1
    assert result.media == 1
    # 图表字体补丁：库写出来的 a:ea 是 0 条，补完要和 a:latin 一样多
    assert result.patch, "渲染摘要里没有图表字体补丁的记录"
    for item in result.patch:
        assert item["ea_before"] == 0
        assert item["ea_after"] == item["latin"] > 0

    check = verify_pptx(target, plan)
    failures = [one.line() for one in check.checks if not one.ok]
    assert not failures, "\n".join(failures)
    # 长宽比那条要**真的跑过**：图不合法时它会合理地跳过，而"跳过"与"通过"看起来一样
    raster = next(one for one in check.checks if one.code == "raster")
    assert any("长宽比一致" in note for note in raster.notes), raster.notes


@pytest.mark.skipif(find_node() is None, reason=_NO_NODE)
def test_invalid_plan_comes_back_as_a_sentence_from_the_renderer(tmp_path: Path) -> None:
    """计划不合法时，错误话要能指到"第几页哪个槽"，而不是一句"渲染失败"。"""
    plan = _plan(tmp_path)
    broken = json.loads(plan_json(plan))
    del broken["pages"][0]["slots"][0]["rect_in"]  # 标题槽没有矩形

    with pytest.raises(DeckRenderError) as excinfo:
        render_deck(broken, tmp_path / "broken.pptx", base_dir=tmp_path)

    assert "计划不合法" in excinfo.value.message
    assert "rect_in" in excinfo.value.message
    # 渲染器自己那句以"计划不合法："开头，桥不能再补一遍同义词
    assert excinfo.value.message.count("计划不合法") == 1
    assert excinfo.value.message.startswith("渲染 PPT 失败（计划不合法）：第 1 页")


@pytest.mark.skipif(find_node() is None, reason=_NO_NODE)
def test_plan_without_canvas_is_refused(tmp_path: Path) -> None:
    """画布缺失是这个库里最容易踩的坑（默认 10×5.625 会静默裁掉内容），所以单独一条。"""
    plan = json.loads(plan_json(_plan(tmp_path)))
    del plan["canvas"]

    with pytest.raises(DeckRenderError) as excinfo:
        render_deck(plan, tmp_path / "no-canvas.pptx", base_dir=tmp_path)

    assert "画布" in excinfo.value.message


@pytest.mark.skipif(find_node() is None, reason=_NO_NODE)
def test_missing_local_image_is_reported_and_drawn_as_a_placeholder(tmp_path: Path) -> None:
    """本地图片文件不在：**画一个说明框并把原因说出来**，不静默丢图。

    静默丢了的话，"图片数吻合"那条结构检查就永远为真——恒真的检查等于没有检查。
    """
    plan = json.loads(plan_json(_plan(tmp_path)))
    for page in plan["pages"]:
        for slot in page["slots"]:
            if slot["kind"] == "image":
                slot["payload"]["path"] = "不存在的图.png"

    result = render_deck(plan, tmp_path / "no-image.pptx", base_dir=tmp_path)

    assert result.media == 0
    assert any("图片槽的本地文件" in note for note in result.warnings)

    # 结构校验这时应当直接判失败：计划里有 1 张本地图片，包里 0 张
    check = verify_pptx(result.path, plan)
    assert not check.ok
    assert any(one.code == "raster" for one in check.checks if not one.ok)


@pytest.mark.skipif(find_node() is None, reason=_NO_NODE)
@pytest.mark.parametrize(
    ("kind", "marker", "direction"),
    [
        ("column", "<c:barChart>", 'barDir val="col"'),
        ("bar", "<c:barChart>", 'barDir val="bar"'),
        ("line", "<c:lineChart>", None),
        ("area", "<c:areaChart>", None),
        ("pie", "<c:pieChart>", None),
        ("doughnut", "<c:doughnutChart>", None),
    ],
)
def test_every_chart_kind_reaches_the_ooxml(
    tmp_path: Path, kind: str, marker: str, direction: str | None
) -> None:
    """六个图表类型逐个落地，并**在产物里核到对应的绘图元素**。

    只断言"渲染没报错"是不够的：写盘层的类型映射有个兜底分支（不认识的类型退回柱状图），
    映射写错一个字母时表现就是"悄悄画成柱状图"，而文件照样生成、检查照样通过。
    所以要打开 chart xml 看它到底是哪种图。
    """
    plan = json.loads(plan_json(_plan(tmp_path)))
    for page in plan["pages"]:
        for slot in page["slots"]:
            if slot["kind"] == "chart":
                slot["payload"]["kind"] = kind
                slot["payload"]["series"] = slot["payload"]["series"][:1]  # 饼/环只许一个系列

    result = render_deck(plan, tmp_path / f"{kind}.pptx", base_dir=tmp_path)

    with zipfile.ZipFile(result.path) as archive:
        xml = archive.read("ppt/charts/chart1.xml").decode()
    assert marker in xml, f"{kind} 没有渲染成 {marker}"
    if direction:
        assert direction in xml, f"{kind} 的柱条方向不对（缺 {direction}）"
    assert verify_pptx(result.path, plan).ok
