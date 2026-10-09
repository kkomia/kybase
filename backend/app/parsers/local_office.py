"""本地 Office 直提（v17）：docx / pptx。

**为什么需要它**：与 PDF 同理——Office 文件原先全靠 MinerU，没配 token 时
`.docx` 直接"暂不支持"。而 docx 就是一个 zip + XML，本地抽正文不需要任何额外依赖。

**能力边界（刻意划清）**：
- 只接 **.docx / .pptx**（OOXML）。旧的二进制 `.doc / .ppt / .xls` 是另一种格式，
  本地不解析——那类只能走云端，`supports()` 明确返回 False（不假装支持）。
- xlsx/xls/csv 不在这里，它们由 `TabularParser` 处理（有结构化副本）。
- **不做版面还原**：段落与表格按文档顺序取出，样式只映射标题层级。
  表格结构（合并单元格等）会简化成 Markdown 表格，复杂版式请用云端引擎。
- 排路由时它垫在云端引擎**之后**：有凭据先用云端还原得更好的那份。

**为什么 docx 用 python-docx、pptx 用标准库**：`python-docx` 是
`pyproject` 里已声明的解析依赖（parsers extra），拿它读段落/表格/标题样式最稳；
`python-pptx` **没有**声明，为了一个"抽取文本"的用途去加一个新依赖不划算——
pptx 的文本全在 `ppt/slides/slideN.xml` 的 `<a:t>` 里，用 zipfile + ElementTree 就够。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

from app.parsers.base import ParseError, ParseResult, ParserProvider, ProbeResult
from app.parsers.probe import suffix_of

__all__ = ["LOCAL_OFFICE_EXTENSIONS", "LocalOfficeParser"]

LOCAL_OFFICE_EXTENSIONS = frozenset({".docx", ".pptx"})

#: PPTX 里的文本都在这个命名空间下。
_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

_SLIDE_NAME = re.compile(r"ppt/slides/slide(\d+)\.xml$")

#: OOXML 的 XML 里**不该出现 DTD**。出现就说明这份文件被构造过。
#: 用**拒绝**而不是"忽略"：一个正常的 docx/pptx 永远不需要 DTD。
_DTD_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")

#: 单个 XML 部件的解压上限。zip 炸弹的典型形态是"几 KB 压成几 GB"，
#: 读之前先看解压后的大小，比事后补救简单。
_MAX_XML_BYTES = 32 * 1024 * 1024


def _read_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    """安全地读一个 OOXML 部件。

    上传的文件是**不可信输入**，而 stdlib 的 ElementTree 有两条已知风险：
    实体展开（billion laughs）与外部实体。两条都以 DTD 为前提——正常的
    docx/pptx 里没有 DTD。所以：

    1. 解压后超过 `_MAX_XML_BYTES` 直接拒绝（zip 炸弹）；
    2. 出现 `<!DOCTYPE` / `<!ENTITY` 直接拒绝（实体类攻击的入口）。

    残余风险因此只剩"不含 DTD 的畸形 XML"，那最多是解析报错，
    而报错已经被 `parse()` 翻成可读文案。**不引 defusedxml 是有意的**：
    为一个"抽取文本"的用途加一个依赖，不如收窄输入。
    """
    info = archive.getinfo(name)
    if info.file_size > _MAX_XML_BYTES:
        raise ParseError(f"文件内的 XML 部件过大（{info.file_size} 字节），已拒绝解析")
    raw = archive.read(name)
    if any(marker in raw for marker in _DTD_MARKERS):
        raise ParseError("文件内的 XML 含 DTD 声明，已拒绝解析（正常的 Office 文件不会这样）")
    # 上面两步就是收窄输入；剩下的畸形 XML 最多解析报错，由 parse() 翻成可读文案
    return ElementTree.fromstring(raw)  # noqa: S314


class LocalOfficeParser(ParserProvider):
    """docx / pptx 的本地文本抽取。"""

    name = "LocalOfficeParser"

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        suffix = suffix_of(filename)
        if suffix not in LOCAL_OFFICE_EXTENSIONS:
            return False
        if suffix == ".docx":
            try:
                import docx  # noqa: F401
            except ImportError:  # pragma: no cover - parsers extra 未安装
                return False
        return True

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        if not content:
            raise ParseError(f"文件内容为空：{filename or '(未命名)'}")
        suffix = suffix_of(filename)
        try:
            markdown = self._docx(content) if suffix == ".docx" else self._pptx(content)
        except ParseError:
            raise
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            # 这三种都是"文件不是有效的 OOXML"：报可读原因，而不是抛栈
            raise ParseError(
                f"这个 {suffix} 文件读不出来（可能已损坏或只是改过后缀）：{exc}"
            ) from exc

        markdown = markdown.strip()
        if not markdown:
            raise ParseError(f"这个文件里没有可提取的文字：{filename or '(未命名)'}")
        return ParseResult(
            markdown=markdown,
            parser_name=self.name,
            page_count=None,  # Office 文档没有固定页的概念
            probe=probe,
        )

    # ------------------------------------------------------------------ docx

    @staticmethod
    def _docx(content: bytes) -> str:
        """段落 + 表格，按文档顺序。标题样式映射成 Markdown 标题。

        **顺序是重点**：python-docx 的 `paragraphs` 与 `tables` 是两个独立列表，
        分别遍历会把表格全挤到文末——一份"文字-表格-文字"的报告就乱了。
        所以按 `document.element.body` 的子元素顺序走。
        """
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        # python-docx 自己解析 XML，这里只把"含 DTD"的文件挡在门外：
        # 契约是"正常 Office 文件不含 DTD"，含了就不是我们要处理的东西
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                if archive.getinfo(name).file_size > _MAX_XML_BYTES:
                    continue  # 过大的部件交给 python-docx 自己判，这里只查 DTD
                if any(marker in archive.read(name) for marker in _DTD_MARKERS):
                    raise ParseError("文件内含 DTD 声明，已拒绝解析（正常的 Office 文件不会这样）")

        document = docx.Document(io.BytesIO(content))
        out: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                text = Paragraph(child, document).text.strip()
                if text:
                    out.append(f"{_heading_prefix(child)}{text}")
            elif child.tag.endswith("}tbl"):
                out.extend(_table_markdown(Table(child, document)))
        return "\n\n".join(part for part in out if part)

    # ------------------------------------------------------------------ pptx

    @staticmethod
    def _pptx(content: bytes) -> str:
        """一页一张，标题取本页第一个文本框，其余文本作正文。

        页码顺序**按幻灯片编号排**（`slide2` 在 `slide10` 前面），
        不能按 zip 里的条目名字典序——那是 `slide1, slide10, slide2…`。
        """
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = []
            for name in archive.namelist():
                matched = _SLIDE_NAME.search(name)
                if matched:
                    names.append((int(matched.group(1)), name))
            if not names:
                raise ParseError("这个 pptx 里没有幻灯片")
            sections: list[str] = []
            for _, name in sorted(names):
                texts = [
                    node.text.strip()
                    for node in _read_xml(archive, name).iter(f"{_DRAWING_NS}t")
                    if node.text and node.text.strip()
                ]
                if not texts:
                    continue
                title = texts[0]
                body = texts[1:]
                lines = [f"## {title}"]
                lines.extend(body)
                sections.append("\n\n".join(lines))
        return "\n\n".join(sections)


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _heading_prefix(element) -> str:  # type: ignore[no-untyped-def]
    """从段落的 `w:pStyle` 里认出标题级别（Heading1 → `#`）。

    用样式名而不是字号：字号是排版值（同一个字号可能是标题也可能是强调），
    而样式名是作者声明的语义。认不出来就按正文处理——宁可不加标题，
    也不要把正文误标成标题（那会让切块的 heading_path 全是噪声）。

    **`pStyle` 在 `w:pPr` 里面**，不是 `w:p` 的直接子元素：
    第一版写成 `element.find(w:pStyle)` 永远返回 None，于是标题全被当正文
    （测试当场发现）。所以这里先定位 `pPr` 再取 `pStyle`。
    """
    properties = element.find(f"{_W_NS}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{_W_NS}pStyle")
    if style is None:
        return ""
    value = (style.get(f"{_W_NS}val") or "").lower()
    if value.startswith("heading"):
        level = value.removeprefix("heading").strip()
        if level.isdigit():
            return "#" * min(6, max(1, int(level))) + " "
    return ""


def _table_markdown(table) -> list[str]:  # type: ignore[no-untyped-def]
    """表格 → Markdown 表格；合并单元格会被展开成重复值（Markdown 表达不了合并）。"""
    rows = [[cell.text.strip().replace("\n", " ") for cell in row.cells] for row in table.rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows[1:]:
        # 列数不一致时补齐/截断：Markdown 表格要求每行列数一样，否则渲染会错位
        padded = (row + [""] * len(header))[: len(header)]
        lines.append("| " + " | ".join(padded) + " |")
    return ["\n".join(lines)]
