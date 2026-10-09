"""幻灯片写盘层·结构校验：把"这份 .pptx 真是计划说的那样"变成机械断言。

**为什么必须解包验，而不是"渲染没报错就算过"。** 写盘层的失败方式恰好是**静默**的那几种：
库的默认画布是 10×5.625，照 13.333 写坐标会被裁掉右边和下边（不报错）；图表里的中文
不吃 `*FontFace`，被替换成宋体（不报错）；图丢了、图被栅格化成图片、页码没写进去——
都不会让 `write()` 抛异常。所以"文件生成了"证明不了任何事，得把包拆开逐条对。

十一条检查，每条都要能**反过来失败**（见 `tests/unit/services/deck/test_verify.py` 的合成包）：

============  ==========================================================================
``package``   包能打开、``[Content_Types].xml`` 良构、每个部件都有内容类型声明
``canvas``    画布尺寸 == 计划的 ``canvas``（13.333×7.5 英寸；钉不住就会被静默裁掉）
``pages``     页数 == 计划页数，且**版式序列**与计划一致（同键同版式、异键异版式、顺序不变）
``text``      每页的标题/正文/要点/指标都是真 ``<a:t>``（不是图片），图片的 alt 在 ``descr``
``chart``     计划里每张图都有一个原生图表部件：被该页 rels 引用、含 ``c:plotArea``、
              ``[Content_Types].xml`` 声明的是 drawingml.chart 而不是图片
``charts``    图表部件数 == 计划里的图表数（多一个少一个都算）
``raster``    ``ppt/media/`` 的图片数 == 计划里的本地图片数（没栅格化、没丢图），
              且每张图的框与原图**长宽比一致**（被"塞进框"拉变形时数量是看不出来的）
``chart_ea``  图表 xml 里每条 ``a:latin`` 都有配套 ``a:ea``（补丁确实生效），字体非空且在白名单链上
``slide_ea``  每页 ``a:latin`` / ``a:ea`` / ``a:cs`` 三条齐全（中文那一段才拿得到指定字体）
``page_no``   页脚槽编号与页序一致时，页码是**真字段**（``a:fld type="slidenum"``）
``notes``     计划里有讲者备注的页，备注真的在 ``ppt/notesSlides/`` 里
============  ==========================================================================

命令行（排查时直接跑，输出每条检查的证据）：

    python -m app.services.deck.verify deck.pptx plan.json
"""

from __future__ import annotations

import re
import struct
import sys
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from app.services.deck.render import PlanInput, plan_dict

__all__ = [
    "Check",
    "DeckCheck",
    "verify_pptx",
]

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: 原生图表的部件类型。图片类型是 image/png 之类——**这一条就是"没被栅格化"的一半证据**。
_CHART_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_SLIDE_RELS_RE = re.compile(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$")
_LAYOUT_RE = re.compile(r"^ppt/slideLayouts/slideLayout(\d+)\.xml$")
_CHART_RE = re.compile(r"^ppt/charts/chart\d+\.xml$")
_NOTES_RE = re.compile(r"^ppt/notesSlides/notesSlide(\d+)\.xml$")
_MEDIA_RE = re.compile(r"^ppt/media/[^/]+$")

#: JPEG 的 SOF 标记（帧头，里面才是宽高）。0xC4/0xC8/0xCC 不是 SOF，别混进来。
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)

#: 画布比对容差（EMU）。1 EMU = 1/914400 英寸，所以 1000 EMU ≈ 0.00003 英寸：
#: 只吸收浮点换算的末位噪声，不会掩盖"真的写小了半英寸"。
_EMU_TOLERANCE = 1000


@dataclass(frozen=True, slots=True)
class Check:
    """一条检查。通过时 `notes` 是证据，失败时 `notes` 是原因——两种都要能读。"""

    code: str
    title: str
    ok: bool
    notes: tuple[str, ...] = ()

    def line(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        head = f"[{mark}] {self.code} · {self.title}"
        if not self.notes:
            return head
        body = "\n".join(f"        {'-' if not self.ok else '·'} {note}" for note in self.notes)
        return f"{head}\n{body}"


@dataclass(frozen=True, slots=True)
class DeckCheck:
    """一份产物的全部检查结果。``ok`` 是它们的与。"""

    path: Path
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"{check.code}：{'; '.join(check.notes) or check.title}"
                     for check in self.checks if not check.ok)

    def summary(self) -> str:
        """给人和模型看的一份报告：先一句话结论，再逐条（失败的排前面）。"""
        head = f"{self.path.name}：{'结构检查全部通过' if self.ok else '结构检查未通过'}"
        lines = [head, f"  {len(self.checks)} 条检查，{len(self.failures)} 条不过"]
        for check in sorted(self.checks, key=lambda item: item.ok):
            lines.append("  " + check.line().replace("\n", "\n  "))
        return "\n".join(lines)


def _parse(raw: bytes) -> ElementTree.Element:
    """解析一个部件。

    ``# noqa: S314`` 的理由与 `parsers/local_office.py` 同：OOXML 本来就是个 zip+xml，
    要读它就得解它。这里解的是**我们自己刚渲染出来的包**（或本地文件），
    没有网络输入；而且"解不开"本身就是要报出来的检查项（良构性）。
    """
    return ElementTree.fromstring(raw)  # noqa: S314


def _root(parts: Mapping[str, bytes], name: str) -> ElementTree.Element:
    """某个部件的解析结果；**部件不在或不是良构 XML 时返回一个空根**。

    为什么不在这里抛：这个校验器的承诺是"失败也走返回值，不抛异常"。
    一个坏部件该由 `package` 那条检查说清"哪里坏了"，别的检查不该被它带走——
    否则一个坏的 slide1.xml 会让"画布对不对""图片数吻合吗"这些无关结论一起消失。
    """
    raw = parts.get(name)
    if raw is None:
        return ElementTree.Element("missing")
    try:
        return _parse(raw)
    except ElementTree.ParseError:
        return ElementTree.Element("broken")


def _part_target(base: str, target: str) -> str:
    """把 rels 里的相对 Target 归一成包内路径。

    ``../slideLayouts/x.xml`` → ``ppt/slideLayouts/x.xml``。
    """
    if target.startswith("/"):
        return target.lstrip("/")
    parts = base.split("/")[:-1]
    for piece in target.split("/"):
        if piece == "..":
            parts = parts[:-1]
        elif piece not in ("", "."):
            parts.append(piece)
    return "/".join(parts)


def verify_pptx(path: str | Path, plan: PlanInput) -> DeckCheck:
    """解包核对：这份产物是不是计划说的那样。**不抛异常**——失败也走返回值。

    只读文件、不写任何东西；不依赖 Node（校验本身是纯 Python）。
    """
    target = Path(path)
    spec = plan_dict(plan)
    pages: list[Mapping[str, Any]] = list(spec.get("pages") or [])
    theme = spec.get("theme") or {}
    theme_fonts = theme.get("fonts") or {}
    fonts = {str(item) for chain in ("title", "body") for item in (theme_fonts.get(chain) or [])}

    checks: list[Check] = []
    try:
        with zipfile.ZipFile(target) as archive:
            parts = {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile) as exc:
        note = f"打不开 {target}：{exc}"
        return DeckCheck(target, (Check("package", "包能打开", False, (note,)),))

    checks.append(_check_package(parts))
    checks.append(_check_canvas(parts, spec))
    checks.append(_check_text(parts, pages))
    checks.append(_check_page_numbers(parts, pages))
    checks.append(_check_notes(parts, pages))
    if fonts:
        checks.append(_check_slide_fonts(parts, fonts))
    else:
        checks.append(
            Check("slide_ea", "每页字体三元组齐全", False, ("计划的主题里没有字体链，无法核对",))
        )
    checks.extend(_check_charts(parts, pages, fonts))
    checks.append(_check_page_layouts(parts, pages))
    checks.append(_check_media(parts, pages))
    return DeckCheck(target, tuple(checks))


# ------------------------------------------------------------------ 各条检查


def _check_package(parts: Mapping[str, bytes]) -> Check:
    """包结构与 ``[Content_Types].xml``。

    两个真正的"打开时弹修复"的来源都在这里：**声明了但包里没有的部件**，
    以及**包里存在却没有内容类型声明的部件**。
    """
    notes: list[str] = []
    failures: list[str] = []
    raw = parts.get("[Content_Types].xml")
    if raw is None:
        return Check("package", "包结构与内容类型声明", False, ("包里没有 [Content_Types].xml",))
    try:
        root = _parse(raw)
    except ElementTree.ParseError as exc:
        note = f"[Content_Types].xml 不是良构 XML：{exc}"
        return Check("package", "包结构与内容类型声明", False, (note,))

    defaults = {
        (element.get("Extension") or "").lower(): (element.get("ContentType") or "")
        for element in root.findall(f"{_CT}Default")
    }
    overrides = {
        (element.get("PartName") or "").lstrip("/"): (element.get("ContentType") or "")
        for element in root.findall(f"{_CT}Override")
    }
    missing = sorted(name for name in overrides if name not in parts)
    if missing:
        failures.append("声明了但包里没有的部件：" + "、".join(missing))
    for name in sorted(parts):
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if name not in overrides and extension not in defaults:
            failures.append(f"{name} 没有任何内容类型声明（打开时可能弹修复）")
    for extension in ("rels", "xml"):
        if extension not in defaults:
            failures.append(f"[Content_Types].xml 缺 {extension} 的 Default 声明")

    # 顺带把每个部件都解一遍：良构性是"真能打开"的前置条件，而它很便宜。
    broken: list[str] = []
    for name in sorted(parts):
        if name.endswith(".xml") or name.endswith(".rels") or name.endswith(".vml"):
            try:
                _parse(parts[name])
            except ElementTree.ParseError as exc:
                broken.append(f"{name}（{exc}）")
    if broken:
        failures.append("这些部件不是良构 XML：" + "、".join(broken[:5]))
    if not failures:
        notes.append(
            f"{len(parts)} 个部件，Default {len(defaults)} 条 / "
            f"Override {len(overrides)} 条，全部良构且都有内容类型"
        )
    return Check("package", "包结构与内容类型声明", not failures, tuple(failures or notes))


def _check_canvas(parts: Mapping[str, bytes], spec: Mapping[str, Any]) -> Check:
    """画布尺寸必须与计划一致（结论 1：钉不住就会被静默裁掉右边和下边）。"""
    canvas = spec.get("canvas") or {}
    want_w = float(canvas.get("width_in") or 0)
    want_h = float(canvas.get("height_in") or 0)
    if "ppt/presentation.xml" not in parts:
        return Check("canvas", "画布尺寸", False, ("包里没有 ppt/presentation.xml",))
    size = _root(parts, "ppt/presentation.xml").find(f"{_P}sldSz")
    if size is None:
        return Check("canvas", "画布尺寸", False, ("presentation.xml 里没有 <p:sldSz>",))
    got_w = int(size.get("cx") or 0)
    got_h = int(size.get("cy") or 0)
    want_emu_w = round(want_w * 914400)
    want_emu_h = round(want_h * 914400)
    ok = abs(got_w - want_emu_w) <= _EMU_TOLERANCE and abs(got_h - want_emu_h) <= _EMU_TOLERANCE
    note = (
        f"画布 {got_w}×{got_h} EMU = {got_w / 914400:.3f}×{got_h / 914400:.3f} 英寸"
        f"（计划 {want_w}×{want_h} 英寸）"
    )
    return Check("canvas", "画布尺寸", ok, (note,))


def _expected_texts(page: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """一页要出现的东西：``(必须出现在 <a:t> 里的文案, 图片 alt)``。

    图表的数据**不在**这里：它写在图表部件里（原生的代价与好处都是这一条），
    由 `_check_charts` 单独核。
    """
    texts: list[str] = []
    alts: list[str] = []
    for slot in page.get("slots") or []:
        kind = slot.get("kind")
        payload = slot.get("payload")
        if kind in ("text", "list"):
            for item in payload if isinstance(payload, list) else [payload]:
                if isinstance(item, str) and item.strip():
                    texts.append(item)
        elif kind == "kpi" and isinstance(payload, Mapping):
            for key in ("value", "label", "delta"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
        elif kind == "image" and isinstance(payload, Mapping):
            alt = payload.get("alt")
            if isinstance(alt, str) and alt.strip():
                alts.append(alt)
    return texts, alts


def _slide_parts(parts: Mapping[str, bytes]) -> list[tuple[int, str]]:
    """``[(页号, 部件名)]``，按页号排。页号从部件名里取（``slide3.xml`` → 3）。"""
    found: list[tuple[int, str]] = []
    for name in parts:
        matched = _SLIDE_RE.match(name)
        if matched is not None:
            found.append((int(matched.group(1)), name))
    return sorted(found, key=lambda item: item[0])


def _check_text(parts: Mapping[str, bytes], pages: list[Mapping[str, Any]]) -> Check:
    """文字是真 ``<a:t>``：页数对不对得上，每页该有的话在不在。"""
    slides = _slide_parts(parts)
    failures: list[str] = []
    notes: list[str] = []
    if len(slides) != len(pages):
        return Check(
            "text",
            "文字是真 <a:t>",
            False,
            (f"页数对不上：包里有 {len(slides)} 页，计划 {len(pages)} 页",),
        )
    for (_, name), page in zip(slides, pages, strict=True):
        root = _root(parts, name)
        rendered = [(element.text or "") for element in root.iter(f"{_A}t")]
        wanted, alts = _expected_texts(page)
        missing = [item for item in wanted if item not in rendered]
        if missing:
            failures.append(f"第 {page.get('index')} 页少了这些文字：{missing[:3]}")
        if not rendered:
            failures.append(f"第 {page.get('index')} 页一个 <a:t> 都没有（文字被画成图片了？）")
        descr = [element.get("descr") or "" for element in root.iter(f"{_P}cNvPr")]
        for alt in alts:
            if alt not in descr:
                failures.append(f"第 {page.get('index')} 页的图片少了 alt 说明：{alt[:20]}")
        notes.append(f"{name}：<a:t> {len(rendered)} 个，计划要的文案都在")
    if failures:
        return Check("text", "文字是真 <a:t>", False, tuple(failures[:6]))
    evidence = (f"共 {len(slides)} 页逐页核对：计划里的文案都在 <a:t> 里", *notes[:2])
    return Check("text", "文字是真 <a:t>", True, evidence)


def _check_page_numbers(parts: Mapping[str, bytes], pages: list[Mapping[str, Any]]) -> Check:
    """页码：计划里的页脚编号与页序一致时，产物里应当是真页码字段。"""
    failures: list[str] = []
    checked = 0
    for index, name in _slide_parts(parts):
        page = next((item for item in pages if int(item.get("index") or 0) == index), None)
        if page is None:
            continue
        footer = next(
            (slot for slot in page.get("slots") or [] if slot.get("slot") == "footer"),
            None,
        )
        if footer is None:
            continue
        payload = footer.get("payload")
        number = payload.get("page_number") if isinstance(payload, Mapping) else None
        if number is not None and int(number) != index:
            continue  # 计划自己不自洽：写盘层按计划的数字画死文字，这里不要求字段
        checked += 1
        fields = [
            element
            for element in _root(parts, name).iter(f"{_A}fld")
            if element.get("type") == "slidenum"
        ]
        if not fields:
            failures.append(f"第 {index} 页没有页码字段（a:fld type=slidenum）")
    if failures:
        return Check("page_no", "页码是真字段", False, tuple(failures))
    return Check("page_no", "页码是真字段", True, (f"{checked} 页都有 a:fld type=slidenum",))


def _check_notes(parts: Mapping[str, bytes], pages: list[Mapping[str, Any]]) -> Check:
    """讲者备注：计划里写了就应当落在 ``ppt/notesSlides/`` 里。"""
    notes_parts: dict[int, str] = {}
    for name in parts:
        matched = _NOTES_RE.match(name)
        if matched is not None:
            notes_parts[int(matched.group(1))] = name
    missing: list[str] = []
    wanted = 0
    for page in pages:
        text = str(page.get("speaker_notes") or "").strip()
        if not text:
            continue
        wanted += 1
        index = int(page.get("index") or 0)
        name = notes_parts.get(index)
        if name is None:
            missing.append(f"第 {index} 页没有备注部件")
            continue
        rendered = [(element.text or "") for element in _root(parts, name).iter(f"{_A}t")]
        if text not in rendered:
            missing.append(f"第 {index} 页的备注不在 notesSlide{index}.xml 里")
    if missing:
        return Check("notes", "讲者备注", False, tuple(missing))
    return Check("notes", "讲者备注", True, (f"{wanted} 页有备注，都写进去了",))


def _font_faces(parts: Mapping[str, bytes], name: str, tag: str) -> list[str]:
    """某个部件里某类字体声明的 typeface 列表。

    用解析而不是正则：属性顺序、自闭合写法变了都不会误判。
    """
    return [(element.get("typeface") or "") for element in _root(parts, name).iter(f"{_A}{tag}")]


def _check_slide_fonts(parts: Mapping[str, bytes], fonts: set[str]) -> Check:
    """每页 ``a:latin`` / ``a:ea`` / ``a:cs`` 三条齐全。

    PptxGenJS 传 `fontFace` 时会写全三条——这是"中文那一段也吃到指定字体"的前提。
    ``a:ea`` 与 ``a:latin`` 数量不等，就说明有文字只拿到了西文字体声明。
    """
    failures: list[str] = []
    notes: list[str] = []
    for index, name in _slide_parts(parts):
        latin = _font_faces(parts, name, "latin")
        ea = _font_faces(parts, name, "ea")
        cs = _font_faces(parts, name, "cs")
        if len(ea) != len(latin) or len(cs) != len(latin):
            failures.append(
                f"第 {index} 页 a:latin {len(latin)} / a:ea {len(ea)} / "
                f"a:cs {len(cs)}，三条数量不等"
            )
        stray = sorted({face for face in ea + latin if face and face not in fonts})
        if stray:
            failures.append(f"第 {index} 页用了白名单之外的字体：{stray}")
        notes.append(f"第 {index} 页 latin/ea/cs = {len(latin)}/{len(ea)}/{len(cs)}")
    if failures:
        return Check("slide_ea", "每页字体三元组齐全", False, tuple(failures[:5]))
    evidence = (f"共 {len(notes)} 页，字体都在白名单链上", *notes[:2])
    return Check("slide_ea", "每页字体三元组齐全", True, evidence)


def _chart_parts(parts: Mapping[str, bytes]) -> list[str]:
    return sorted(name for name in parts if _CHART_RE.match(name))


def _check_charts(
    parts: Mapping[str, bytes], pages: list[Mapping[str, Any]], fonts: set[str]
) -> tuple[Check, Check, Check]:
    """三件事一起查（图表原生 / 数量一致 / 字体补丁）：它们读的是同一份部件。"""
    planned = sum(
        1
        for page in pages
        for slot in page.get("slots") or []
        if slot.get("kind") == "chart"
    )
    names = _chart_parts(parts)
    native_failures: list[str] = []
    count_failures: list[str] = []
    ea_failures: list[str] = []
    ea_notes: list[str] = []

    declared = parts.get("[Content_Types].xml")
    overrides: dict[str, str] = {}
    if declared is not None:
        overrides = {
            str(item.get("PartName") or "").lstrip("/"): str(item.get("ContentType") or "")
            for item in _root(parts, "[Content_Types].xml").findall(f"{_CT}Override")
        }

    for name in names:
        if overrides.get(name) != _CHART_CONTENT_TYPE:
            native_failures.append(
                f"{name} 的内容类型不是原生图表（声明为 {overrides.get(name)!r}）"
            )
        if _root(parts, name).find(f".//{_C}plotArea") is None:
            native_failures.append(f"{name} 里没有 <c:plotArea>：不像一张原生图表")

    # 每个图表部件都要被某一页引用（引用了才算"放上了页面"，而不是包里的孤儿部件）
    referenced: set[str] = set()
    for name in parts:
        if not _SLIDE_RELS_RE.match(name):
            continue
        base = name.replace("_rels/", "").removesuffix(".rels")
        for relation in _root(parts, name).findall(f"{_REL}Relationship"):
            if str(relation.get("Type") or "").endswith("/chart"):
                referenced.add(_part_target(base, str(relation.get("Target") or "")))
    orphan = sorted(set(names) - referenced)
    if orphan:
        native_failures.append("这些图表部件没有任何页面引用：" + "、".join(orphan))

    if len(names) == 0 and planned > 0:
        # "0 个部件都是原生图表"读起来像通过——**那是恒真的检查**，这里必须说破。
        native_failures.append(f"计划里有 {planned} 张图，但包里一个原生图表部件都没有")
    if len(names) != planned:
        count_failures.append(f"包里有 {len(names)} 个图表部件，计划里有 {planned} 张图")

    for name in names:
        latin = _font_faces(parts, name, "latin")
        ea = _font_faces(parts, name, "ea")
        cs = _font_faces(parts, name, "cs")
        ea_notes.append(f"{name}：a:latin {len(latin)} → a:ea {len(ea)} / a:cs {len(cs)}")
        if len(ea) < len(latin):
            ea_failures.append(
                f"{name} 有 {len(latin)} 条 a:latin 但只有 {len(ea)} 条 a:ea（补丁没生效）"
            )
        blank = [index + 1 for index, face in enumerate(ea) if not face.strip()]
        if blank:
            ea_failures.append(
                f"{name} 的第 {blank[:5]} 条 a:ea 是空的：图表中文还是拿不到指定字体"
            )
        stray = sorted({face for face in ea if face and face not in fonts})
        if stray:
            ea_failures.append(f"{name} 的 a:ea 用了白名单之外的字体：{stray}")

    native_ok = tuple(native_failures or [f"{len(names)} 个图表部件都是原生图表并被页面引用"])
    count_ok = tuple(count_failures or [f"计划 {planned} 张 = 包内 {len(names)} 个部件"])
    ea_ok = tuple(ea_failures or ea_notes or ["计划里没有图表"])
    return (
        Check("chart", "图表是原生部件", not native_failures, native_ok),
        Check("charts", "图表数量与计划一致", not count_failures, count_ok),
        Check("chart_ea", "图表字体补丁生效", not ea_failures, ea_ok),
    )


def _check_page_layouts(parts: Mapping[str, bytes], pages: list[Mapping[str, Any]]) -> Check:
    """版式序列：页数要对、同版式的页挂同一个版式部件、不同版式的页不能混用一个。

    这里**不写死版式部件的命名**（那是写盘层的实现细节）：只要求"计划里的版式键
    与产物里的版式部件一一对应、顺序一致"。写盘层将来换命名，这条检查照样有效。
    """
    slides = _slide_parts(parts)
    if len(slides) != len(pages):
        return Check(
            "pages",
            "页数与版式序列",
            False,
            (f"页数对不上：包里有 {len(slides)} 页，计划 {len(pages)} 页",),
        )
    key_to_part: dict[str, str] = {}
    part_to_key: dict[str, str] = {}
    order: list[str] = []
    failures: list[str] = []
    for (index, name), page in zip(slides, pages, strict=True):
        key = str(page.get("layout") or "")
        rels = f"ppt/slides/_rels/{Path(name).name}.rels"
        if rels not in parts:
            failures.append(f"第 {index} 页没有 rels，读不出它挂的版式")
            continue
        targets = [
            _part_target(name, str(relation.get("Target") or ""))
            for relation in _root(parts, rels).findall(f"{_REL}Relationship")
            if str(relation.get("Type") or "").endswith("/slideLayout")
        ]
        if not targets:
            failures.append(f"第 {index} 页没有挂任何版式")
            continue
        part = targets[0]
        if key_to_part.setdefault(key, part) != part:
            failures.append(f"第 {index} 页的版式 {key} 与同键的其他页不是同一个版式部件")
        if part_to_key.setdefault(part, key) != key:
            failures.append(f"第 {index} 页与「{part_to_key[part]}」共用了同一个版式部件")
        order.append(part)
    missing = sorted({str(page.get("layout")) for page in pages} - set(key_to_part))
    if missing:
        failures.append("这些版式没有对应的页面：" + "、".join(missing))
    if failures:
        return Check("pages", "页数与版式序列", False, tuple(failures[:6]))
    labels = []
    for part in sorted(set(order)):
        node = _root(parts, part).find(f".//{_P}cSld")
        label = node.get("name") if node is not None else part
        labels.append(f"{part}（{label}）")
    return Check(
        "pages",
        "页数与版式序列",
        True,
        (
            f"{len(slides)} 页，计划里的版式序列 "
            + " → ".join(str(page.get("layout")) for page in pages),
            "版式部件：" + "、".join(labels),
            "顺序：" + " → ".join(order),
        ),
    )


def _raster_size(raw: bytes) -> tuple[int, int] | None:
    """栅格图的像素尺寸（PNG / GIF / BMP / JPEG）。**只依赖标准库**：认不出的格式返回 None。

    要这个数是为了核对"图片有没有被拉变形"——那是一条**只看图片数看不到的**错：
    图放进去了、数量也对，但被拉成框的比例，肉眼一看就是歪的。
    """
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            width, height = struct.unpack(">II", raw[16:24])
            return width, height
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", raw[6:10])
            return width, height
        if raw[:2] == b"BM":
            width, height = struct.unpack("<ii", raw[18:26])
            return abs(width), abs(height)
        if raw[:2] == b"\xff\xd8":
            index = 2
            while index + 9 < len(raw):
                if raw[index] != 0xFF:
                    index += 1
                    continue
                marker = raw[index + 1]
                length = struct.unpack(">H", raw[index + 2 : index + 4])[0]
                if marker in _JPEG_SOF_MARKERS:
                    height, width = struct.unpack(">HH", raw[index + 5 : index + 9])
                    return width, height
                index += 2 + length
    except (struct.error, IndexError):
        return None
    return None


def _picture_ratios(parts: Mapping[str, bytes]) -> list[tuple[str, float, float]]:
    """每张页面图片：``(部件名, 图片框的宽高比, 原图的宽高比)``。

    原图宽高比取不到（矢量图之类）就不列入——那部分用"认不出来"说清楚，
    而不是当 1.0 去比，那样会凭空报错。
    """
    found: list[tuple[str, float, float]] = []
    for name in sorted(parts):
        matched = _SLIDE_RE.match(name)
        if matched is None:
            continue
        rels = f"ppt/slides/_rels/{Path(name).name}.rels"
        related = {
            str(relation.get("Id") or ""): _part_target(name, str(relation.get("Target") or ""))
            for relation in _root(parts, rels).findall(f"{_REL}Relationship")
        }
        for picture in _root(parts, name).iter(f"{_P}pic"):
            blip = picture.find(f".//{_A}blip")
            if blip is None:
                continue
            target = related.get(str(blip.get(f"{_OR}embed") or ""), "")
            size = _raster_size(parts.get(target, b""))
            ext = picture.find(f".//{_A}ext")
            if size is None or ext is None:
                continue
            box_w = float(ext.get("cx") or 0)
            box_h = float(ext.get("cy") or 0)
            if box_w <= 0 or box_h <= 0 or not size[0] or not size[1]:
                continue
            found.append((target, box_w / box_h, size[0] / size[1]))
    return found


def _check_media(parts: Mapping[str, bytes], pages: list[Mapping[str, Any]]) -> Check:
    """图片数 + 每张图的宽高比。

    数这条同时证明两件事：**图表没被栅格化成图片**（否则会多出图片），
    以及**没有图片被静默丢掉**（少了就说明丢了一张）。

    再加一条长宽比：图片被"塞进框"拉变形时数量完全正常，只有比例会露馅——
    而 PptxGenJS 对本地图片**量不出原尺寸**（见 `scripts/deck/render.mjs` 的 `writeImage`），
    照它的 `sizing` 走就会踩到，所以这条必须有。
    """
    expected = sum(
        1
        for page in pages
        for slot in page.get("slots") or []
        if slot.get("kind") == "image" and (slot.get("payload") or {}).get("kind") == "local"
    )
    media = sorted(name for name in parts if _MEDIA_RE.match(name))
    embedded = sum(
        1
        for name in parts
        if _SLIDE_RELS_RE.match(name)
        for relation in _root(parts, name).findall(f"{_REL}Relationship")
        if str(relation.get("Type") or "").endswith("/image")
    )
    failures: list[str] = []
    if len(media) != expected:
        failures.append(
            f"ppt/media/ 里有 {len(media)} 张图（{ '、'.join(media) }），"
            f"计划里的本地图片有 {expected} 张：多了说明有东西被栅格化，少了说明图被丢了"
        )
    if embedded != expected:
        failures.append(f"页面里引用到的图片有 {embedded} 处，计划 {expected} 张")
    ratios = _picture_ratios(parts)
    for target, box_ratio, image_ratio in ratios:
        if abs(box_ratio - image_ratio) > 0.02 * image_ratio:
            failures.append(
                f"{target} 被拉变形了：框的宽高比 {box_ratio:.3f}，原图 {image_ratio:.3f}"
                "（等比缩放要在写盘层算出来，别交给库）"
            )
    if failures:
        return Check("raster", "图片数吻合、长宽比没被拉", False, tuple(failures))
    evidence = [
        f"ppt/media/ {len(media)} 张 = 计划的本地图片 {expected} 张，页面引用 {embedded} 处"
    ]
    if ratios:
        evidence.append(
            "长宽比一致：" + "、".join(f"{target} {ratio:.3f}" for target, ratio, _ in ratios)
        )
    return Check("raster", "图片数吻合、长宽比没被拉", True, tuple(evidence))


def _main(argv: list[str]) -> int:  # pragma: no cover - 排查入口
    if len(argv) < 3:
        print("用法：python -m app.services.deck.verify <deck.pptx> <plan.json>", file=sys.stderr)
        return 2
    check = verify_pptx(argv[1], Path(argv[2]).read_text(encoding="utf-8"))
    print(check.summary())
    return 0 if check.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv))
