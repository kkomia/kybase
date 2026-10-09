"""写盘层·结构校验（幻灯片生成·第五层）。

镜像同构：``app/services/deck/verify.py`` → 本文件。

**这一组的重点是"反向"**：`test_render.py` 那条端到端证明了真实的产物能通过，
但那只说明检查现在没红，不说明它会红——恒真的检查等于没有检查（`test_layouts.py`
吃过同样的亏）。所以这里**手写一个最小但完整的包**，再逐个把它改坏：
少一个图表部件、多一张图、图被拉变形、`a:ea` 是空的、画布写小、文字不见了、
版式被换过、内容类型声明指向不存在的部件——每一处都要求对应的那条检查报出来。

这个包是手写的而不是渲染出来的，理由是它**不需要 Node**：没有 Node 的机器上
（CI 只装 Python 的场合）这些检查照跑，端到端那条才 skip。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.services.deck.verify import DeckCheck, verify_pptx
from tests.unit.services.deck.test_render import png_bytes

pytestmark = pytest.mark.local

_CT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_R = "http://schemas.openxmlformats.org/package/2006/relationships"
_OR = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CHART_TYPE = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
_SLIDE_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
_LAYOUT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"
_MASTER_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"
_PRES_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"

#: 第 2 页的 rels 顺序（`_rels` 按列表位置编号）：布局 / 图表 / 图片 → 图片是第 3 个。
_LAYOUT_RID = 1
_CHART_RID = 2
IMAGE_RID = 3

#: 夹具里那张图的像素尺寸（480×180 → 宽高比 2.667），与 `_picture()` 默认的框一致。
PNG_SIZE = (480, 180)

#: 一份两页的计划：一页要点、一页图文带原生图表。字段形状就是 `DeckPlan.to_dict()`。
PLAN: dict[str, Any] = {
    "deck": {"title": "结构校验夹具", "page_count": 2},
    "canvas": {"width_in": 13.333, "height_in": 7.5},
    "theme": {
        "name": "fixture",
        "palette": {},
        "fonts": {"title": ["Microsoft YaHei"], "body": ["DengXian"], "latin": "Arial"},
        "type_scale": {},
        "spacing": {},
        "measure": {},
    },
    "pages": [
        {
            "index": 1,
            "layout": "bullets/heavy",
            "title": "第一页",
            "speaker_notes": "",
            "slots": [
                {"slot": "title", "kind": "text", "payload": "第一页"},
                {"slot": "body", "kind": "list", "payload": ["甲", "乙"]},
                {"slot": "footer", "kind": "decoration", "payload": {"page_number": 1}},
            ],
        },
        {
            "index": 2,
            "layout": "split/light",
            "title": "第二页",
            "speaker_notes": "",
            "slots": [
                {"slot": "title", "kind": "text", "payload": "第二页"},
                {
                    "slot": "image",
                    "kind": "image",
                    "payload": {"kind": "local", "path": "x.png", "alt": "一张图"},
                },
                {
                    "slot": "data",
                    "kind": "chart",
                    "payload": {
                        "kind": "column",
                        "categories": ["甲", "乙"],
                        "series": [{"name": "系列", "values": [1, 2]}],
                        "takeaway": "结论",
                    },
                },
                {"slot": "footer", "kind": "decoration", "payload": {"page_number": 2}},
            ],
        },
    ],
}


def _run(texts: str, font: str = "DengXian", *, field: bool = False) -> str:
    """一段带字体三元组的文字。``a:latin`` / ``a:ea`` / ``a:cs`` 三条是写盘层的约定。"""
    field_xml = (
        f'<a:fld id="{{4F1B0A3C-0000-0000-0000-000000000001}}" type="slidenum">'
        f'<a:rPr lang="zh-CN"><a:latin typeface="{font}"/><a:ea typeface="{font}"/>'
        f'<a:cs typeface="{font}"/></a:rPr><a:t>1</a:t></a:fld>'
        if field
        else ""
    )
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="文本框"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang=\"zh-CN\">"
        f'<a:latin typeface="{font}"/><a:ea typeface="{font}"/><a:cs typeface="{font}"/>'
        f"</a:rPr><a:t>{texts}</a:t></a:r>{field_xml}</a:p></p:txBody></p:sp>"
    )


def _slide(body_parts: list[str]) -> bytes:
    """一页幻灯片。``xmlns:r`` 用的是 **officeDocument** 那套命名空间。

    这不是随手写的：幻灯片里引用图片写的是 `r:embed`，而 `r:` 在真实 pptx 里绑的是
    `…/officeDocument/2006/relationships`（`_rels` 文件则用 package 那套）。
    一开始这里绑错了，于是"被拉变形"那条检查在夹具上永远跳过——**夹具本身也是被测对象**。
    """
    inner = "".join(body_parts)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}" xmlns:r="{_OR}">'
        f"<p:cSld><p:spTree>{inner}</p:spTree></p:cSld></p:sld>"
    ).encode()


def _picture(width_emu: int = 5400000, height_emu: int = 2025000) -> str:
    """图片形状：``blip`` 指向 rId 里的图片部件，``a:ext`` 是它在页面上的框。

    默认框的宽高比 = 5400000/2025000 ≈ 2.667，与夹具里那张 480×180 的图一致
    （`PNG_SIZE`）；测试要验"被拉变形"时改这两个数即可。
    """
    return (
        '<p:pic><p:nvPicPr><p:cNvPr id="4" name="图片" descr="一张图"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        f'<p:blipFill><a:blip r:embed="rId{IMAGE_RID}"/><a:stretch><a:fillRect/></a:stretch>'
        "</p:blipFill>"
        f'<p:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm></p:spPr></p:pic>'
    )


def _rels(relations: list[tuple[str, str]]) -> bytes:
    inner = "".join(
        f'<Relationship Id="rId{index}" Type="{kind}" Target="{target}"/>'
        for index, (kind, target) in enumerate(relations, start=1)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_R}">{inner}</Relationships>'
    ).encode()


def _layout(name: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldLayout xmlns:p="{_P}" xmlns:a="{_A}">'
        f'<p:cSld name="{name}"><p:spTree/></p:cSld></p:sldLayout>'
    ).encode()


def _chart() -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<c:chartSpace xmlns:c="{_C}" xmlns:a="{_A}"><c:chart><c:plotArea>'
        f"<c:barChart><c:ser><c:tx><c:strRef><c:strCache><c:pt><c:v>系列</c:v>"
        f"</c:pt></c:strCache></c:strRef></c:tx></c:ser></c:barChart>"
        f"</c:plotArea></c:chart>"
        f'<c:txPr><a:p><a:pPr><a:defRPr><a:latin typeface="DengXian"/>'
        f'<a:ea typeface="DengXian"/><a:cs typeface="DengXian"/>'
        f"</a:defRPr></a:pPr></a:p></c:txPr></c:chartSpace>"
    ).encode()


def _content_types(*extra_overrides: str) -> bytes:
    overrides = [
        ("/ppt/presentation.xml", _PRES_TYPE),
        ("/ppt/slideMasters/slideMaster1.xml", _MASTER_TYPE),
        ("/ppt/slideLayouts/slideLayout1.xml", _LAYOUT_TYPE),
        ("/ppt/slideLayouts/slideLayout2.xml", _LAYOUT_TYPE),
        ("/ppt/slides/slide1.xml", _SLIDE_TYPE),
        ("/ppt/slides/slide2.xml", _SLIDE_TYPE),
        ("/ppt/charts/chart1.xml", _CHART_TYPE),
    ]
    body = "".join(
        f'<Default Extension="{ext}" ContentType="{kind}"/>'
        for ext, kind in (
            ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
            ("xml", "application/xml"),
            ("png", "image/png"),
        )
    )
    body += "".join(
        f'<Override PartName="{part}" ContentType="{kind}"/>' for part, kind in overrides
    )
    body += "".join(extra_overrides)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CT_TYPES}">{body}</Types>'
    ).encode()


def baseline() -> dict[str, bytes]:
    """一个**应当全过**的最小包（2 页 / 1 张原生图表 / 1 张图片 / 页码字段）。"""
    presentation = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:p="{_P}" xmlns:r="{_R}"><p:sldMasterIdLst>'
        f'<p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'<p:sldSz cx="12191695" cy="6858000"/></p:presentation>'
    ).encode()
    master = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldMaster xmlns:p="{_P}" xmlns:a="{_A}">'
        f'<p:cSld name="母版"><p:spTree/></p:cSld></p:sldMaster>'
    ).encode()
    return {
        "[Content_Types].xml": _content_types(),
        "ppt/presentation.xml": presentation,
        "ppt/slideMasters/slideMaster1.xml": master,
        "ppt/slideLayouts/slideLayout1.xml": _layout("KYLAB_BULLETS_HEAVY"),
        "ppt/slideLayouts/slideLayout2.xml": _layout("KYLAB_SPLIT_LIGHT"),
        "ppt/slides/slide1.xml": _slide(
            [_run("第一页"), _run("甲"), _run("乙"), _run("", field=True)]
        ),
        "ppt/slides/slide2.xml": _slide([_run("第二页"), _picture(), _run("", field=True)]),
        "ppt/slides/_rels/slide1.xml.rels": _rels(
            [(f"{_OR}/slideLayout", "../slideLayouts/slideLayout1.xml")]
        ),
        "ppt/slides/_rels/slide2.xml.rels": _rels(
            [
                (f"{_OR}/slideLayout", "../slideLayouts/slideLayout2.xml"),
                (f"{_OR}/chart", "../charts/chart1.xml"),
                (f"{_OR}/image", "../media/image1.png"),
            ]
        ),
        "ppt/charts/chart1.xml": _chart(),
        "ppt/media/image1.png": png_bytes(*PNG_SIZE, (0x1B, 0x4F, 0x8A)),
    }


def write_package(tmp_path: Path, parts: dict[str, bytes]) -> Path:
    target = tmp_path / "fixture.pptx"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return target


def check_of(result: DeckCheck, code: str) -> Any:
    found = [item for item in result.checks if item.code == code]
    assert found, f"没有 code={code} 这条检查"
    return found[0]


# ------------------------------------------------------------------ 正向

def test_baseline_package_passes_every_check(tmp_path: Path) -> None:
    """手写的夹具先要全过——否则后面的"改坏一处"就不知道是改坏的还是本来就不行。"""
    result = verify_pptx(write_package(tmp_path, baseline()), PLAN)

    failures = [check.line() for check in result.checks if not check.ok]
    assert not failures, "\n".join(failures)
    assert {check.code for check in result.checks} == {
        "package",
        "canvas",
        "text",
        "page_no",
        "notes",
        "slide_ea",
        "chart",
        "charts",
        "chart_ea",
        "pages",
        "raster",
    }


def test_missing_package_is_a_sentence_not_an_exception(tmp_path: Path) -> None:
    """打不开的产物：报"打不开"，而不是抛 BadZipFile 到调用方。"""
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"not a zip at all")

    result = verify_pptx(broken, PLAN)

    assert not result.ok
    assert check_of(result, "package").ok is False
    assert "打不开" in check_of(result, "package").notes[0]


# ------------------------------------------------------------------ 反向：每个缺陷都要被抓到

def _mutated(tmp_path: Path, **changes: Any) -> DeckCheck:
    parts = baseline()
    for name, payload in changes.items():
        if payload is None:
            parts.pop(name, None)
        else:
            parts[name] = payload
    return verify_pptx(write_package(tmp_path, parts), PLAN)


def test_rasterised_chart_is_caught(tmp_path: Path) -> None:
    """图表被渲成图片（或者图片被多塞了一张）：图片数就会对不上。"""
    result = _mutated(tmp_path, **{"ppt/media/image2.png": b"\x89PNG\r\n\x1a\n extra"})

    assert check_of(result, "raster").ok is False
    assert "栅格化" in check_of(result, "raster").notes[0]


def test_dropped_image_is_caught(tmp_path: Path) -> None:
    """图片被静默丢掉：同样是对不上，方向相反。"""
    result = _mutated(tmp_path, **{"ppt/media/image1.png": None})

    assert check_of(result, "raster").ok is False
    assert "少了" in check_of(result, "raster").notes[0]


def test_squashed_picture_is_caught(tmp_path: Path) -> None:
    """图片被塞进框拉变形：**数量和面积都对，只有长宽比错了**。

    真实踩过一次：PptxGenJS 对本地图片量不出原尺寸，照它的 `sizing: contain` 走会把
    480×180 的图拉成框的比例（写盘层因此自己算等比矩形）。这条检查就是拦住它的。
    """
    squashed = _slide([_run("第二页"), _picture(5400000, 5400000), _run("", field=True)])
    result = _mutated(tmp_path, **{"ppt/slides/slide2.xml": squashed})

    assert check_of(result, "raster").ok is False
    assert "被拉变形" in check_of(result, "raster").notes[0]


def test_chart_that_is_not_native_is_caught(tmp_path: Path) -> None:
    """图表部件不在，或者内容类型声明错了：`chart` / `charts` 两条都要红。"""
    missing = _mutated(tmp_path, **{"ppt/charts/chart1.xml": None})
    assert check_of(missing, "charts").ok is False
    assert check_of(missing, "chart").ok is False

    wrong_type = _mutated(
        tmp_path,
        **{
            "[Content_Types].xml": _content_types(
                f'<Override PartName="/ppt/charts/chart2.xml" ContentType="{_CHART_TYPE}"/>'
            )
        },
    )
    # 多声明了一个不存在的部件 → 包结构先报；图表本身仍然原生
    assert check_of(wrong_type, "package").ok is False
    assert check_of(wrong_type, "chart").ok is True


def test_blank_chart_ea_is_caught(tmp_path: Path) -> None:
    """`a:ea` 被写成空值（就是没有后处理时库的真实行为）：图表中文会拿不到字体。"""
    blank = _chart().replace(b'<a:ea typeface="DengXian"/>', b'<a:ea typeface=""/>')
    result = _mutated(tmp_path, **{"ppt/charts/chart1.xml": blank})

    assert check_of(result, "chart_ea").ok is False
    assert "空的" in check_of(result, "chart_ea").notes[0]


def test_missing_chart_ea_is_caught(tmp_path: Path) -> None:
    """直接没有 `a:ea`（补丁完全没跑）：条数对不上。"""
    result = _mutated(
        tmp_path,
        **{"ppt/charts/chart1.xml": _chart().replace(b'<a:ea typeface="DengXian"/>', b"")},
    )

    assert check_of(result, "chart_ea").ok is False
    assert "补丁没生效" in check_of(result, "chart_ea").notes[0]


def test_slide_font_triple_gap_is_caught(tmp_path: Path) -> None:
    """正文只写了 a:latin 没写 a:ea：中文那一段就吃不到指定字体。"""
    slide = baseline()["ppt/slides/slide1.xml"].replace(b'<a:ea typeface="DengXian"/>', b"")
    result = _mutated(tmp_path, **{"ppt/slides/slide1.xml": slide})

    assert check_of(result, "slide_ea").ok is False
    assert "数量不等" in check_of(result, "slide_ea").notes[0]


def test_wrong_canvas_is_caught(tmp_path: Path) -> None:
    """画布写小（库里默认的 10×5.625）：右边和下边会被静默裁掉。"""
    small = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:p="{_P}" xmlns:r="{_R}">'
        f'<p:sldSz cx="9144000" cy="5143500"/></p:presentation>'
    ).encode()
    result = _mutated(tmp_path, **{"ppt/presentation.xml": small})

    assert check_of(result, "canvas").ok is False
    assert "10.000×5.625" in check_of(result, "canvas").notes[0]


def test_text_drawn_as_picture_is_caught(tmp_path: Path) -> None:
    """文字没落成 <a:t>（被画成了图片）：计划里的文案找不到。"""
    result = _mutated(tmp_path, **{"ppt/slides/slide1.xml": _slide([_picture()])})

    assert check_of(result, "text").ok is False
    assert "少了这些文字" in check_of(result, "text").notes[0]


def test_wrong_page_count_is_caught(tmp_path: Path) -> None:
    """少了一页：页数直接对不上。"""
    result = _mutated(tmp_path, **{"ppt/slides/slide2.xml": None})

    assert check_of(result, "pages").ok is False
    assert "页数对不上" in check_of(result, "pages").notes[0]


def test_swapped_layout_is_caught(tmp_path: Path) -> None:
    """两页换了版式部件：同键不同部件、异键同一部件，两种都算错。"""
    swapped = _rels([(f"{_OR}/slideLayout", "../slideLayouts/slideLayout1.xml")])
    result = _mutated(tmp_path, **{"ppt/slides/_rels/slide2.xml.rels": swapped})

    assert check_of(result, "pages").ok is False
    assert "版式部件" in "；".join(check_of(result, "pages").notes)


def test_missing_page_number_field_is_caught(tmp_path: Path) -> None:
    """页码被画成死文字而不是字段：页码就不会跟着页序变。"""
    slide = baseline()["ppt/slides/slide1.xml"]
    end = slide.find(b"</a:fld>") + len(b"</a:fld>")
    without_field = slide[: slide.find(b"<a:fld")] + slide[end:]
    result = _mutated(tmp_path, **{"ppt/slides/slide1.xml": without_field})

    assert check_of(result, "page_no").ok is False
    assert "页码字段" in check_of(result, "page_no").notes[0]


def test_broken_xml_is_caught(tmp_path: Path) -> None:
    """部件不是良构 XML：包能打开但未必认得出。"""
    result = _mutated(tmp_path, **{"ppt/slides/slide1.xml": b"<p:sld><p:cSld>"})

    assert check_of(result, "package").ok is False
    assert any("良构" in note for note in check_of(result, "package").notes)


def test_declared_but_missing_part_is_caught(tmp_path: Path) -> None:
    """声明了不存在的部件——**这条正是 PptxGenJS 每调一次 defineSlideMaster 就会犯的错**。"""
    dangling = _content_types(
        f'<Override PartName="/ppt/slideMasters/slideMaster2.xml" ContentType="{_MASTER_TYPE}"/>'
    )
    result = _mutated(tmp_path, **{"[Content_Types].xml": dangling})

    assert check_of(result, "package").ok is False
    assert "slideMaster2.xml" in check_of(result, "package").notes[0]


def test_notes_that_did_not_land_are_caught(tmp_path: Path) -> None:
    """计划里写了讲者备注，但备注部件不在。"""
    plan = {**PLAN, "pages": [{**PLAN["pages"][0], "speaker_notes": "这一段要写在备注里"}]}
    result = verify_pptx(write_package(tmp_path, baseline()), plan)

    assert check_of(result, "notes").ok is False
    assert "备注部件" in check_of(result, "notes").notes[0]


def test_summary_prints_every_check(tmp_path: Path) -> None:
    """summary 是给人（也给模型）看的报告：结论 + 逐条。"""
    result = verify_pptx(write_package(tmp_path, baseline()), PLAN)
    text = result.summary()

    assert "结构检查全部通过" in text
    for code in ("canvas", "chart", "raster", "pages"):
        assert code in text


def test_zip_reads_with_a_fresh_handle(tmp_path: Path) -> None:
    """产物就是个普通 zip：用标准库能读回来（没有自造格式）。"""
    target = write_package(tmp_path, baseline())
    with zipfile.ZipFile(io.BytesIO(target.read_bytes())) as archive:
        assert "ppt/charts/chart1.xml" in archive.namelist()
