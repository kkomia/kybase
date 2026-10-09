"""本地 PDF / Office 直提（v17）：没有云端凭据时产品还能不能用。

回归背景：v17 之前文字型 PDF 与 docx/pptx 一律送 MinerU，没配 token 时
`parser_router` 直接抛"暂不支持的文件类型"——一个纯文字的 PDF 打不开。

这里用**真实构造的文件**（docx 用 python-docx 生成、pptx 手工拼 OOXML zip、
PDF 用 pypdf 生成）验证抽取结果，而不是塞假字节。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.parsers.base import ParseError, ProbeKind, ProbeResult

# 这两个库在 `parsers` extra 里。CI 只跑 `uv sync`（不含 extras），缺库时跳过
# ——解析器本身对缺库是优雅降级（`supports()` 返回 False），不是报错。
pytest.importorskip("pypdf")
pytest.importorskip("docx")
from app.parsers.local_office import LocalOfficeParser
from app.parsers.local_pdf import LocalPdfTextParser


def _probe(kind: str = ProbeKind.TEXT) -> ProbeResult:
    return ProbeResult(kind=kind, text_coverage=1.0)


# --------------------------------------------------------------------- PDF


def _pdf_bytes(pages: list[str]) -> bytes:
    """用 pypdf 生成一个真实的多页 PDF（含文本层）。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=300, height=200)
        stream = DecodedStreamObject()
        # 最小可用的文本绘制指令：写死字体资源，让 extract_text 能抽到
        stream.set_data(
            f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("latin-1", errors="replace")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = writer._add_object(
            _font_resources(writer)
        )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _font_resources(writer):  # type: ignore[no-untyped-def]
    from pypdf.generic import DictionaryObject, NameObject

    resources = DictionaryObject()
    fonts = DictionaryObject()
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    fonts[NameObject("/F1")] = writer._add_object(font)
    resources[NameObject("/Font")] = writer._add_object(fonts)
    return resources


def test_pdf_only_claims_text_layer_files() -> None:
    """只接文字型：混合件本地抽会**静默丢页**，比"不支持"更难排查。"""
    parser = LocalPdfTextParser()
    assert parser.supports(filename="a.pdf", mime_type=None, probe=_probe(ProbeKind.TEXT))
    assert not parser.supports(filename="a.pdf", mime_type=None, probe=_probe(ProbeKind.SCANNED))
    assert not parser.supports(filename="a.pdf", mime_type=None, probe=_probe(ProbeKind.MIXED))
    assert not parser.supports(filename="a.md", mime_type=None, probe=_probe())


def test_pdf_extracts_text_with_page_markers() -> None:
    parser = LocalPdfTextParser()
    content = _pdf_bytes(["Hello page one", "Hello page two"])

    result = parser.parse(content=content, filename="a.pdf", probe=_probe())

    assert result.parser_name == "LocalPdfTextParser"
    assert result.page_count == 2
    # 页标记是白拿的收益：chunk 因此带页码，引用能直接跳到 PDF 那一页
    assert "<!-- page:1 -->" in result.markdown
    assert "<!-- page:2 -->" in result.markdown
    assert "Hello page one" in result.markdown
    assert "Hello page two" in result.markdown


def test_empty_page_keeps_the_page_number() -> None:
    """空页也要写标记：第 7 页不能因为第 6 页是空的就变成第 6 页（引用会整体错位）。"""
    parser = LocalPdfTextParser()
    content = _pdf_bytes(["first", "", "third"])

    result = parser.parse(content=content, filename="a.pdf", probe=_probe())

    assert "<!-- page:3 -->" in result.markdown
    assert "third" in result.markdown


# ---------------------------------------------------------------- Office：docx


def _docx_bytes() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("眼轴监测", level=1)
    document.add_paragraph("眼轴长度是近视防控的核心指标。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "年龄"
    table.cell(0, 1).text = "参考值"
    table.cell(1, 0).text = "6 岁"
    table.cell(1, 1).text = "22.5mm"
    document.add_paragraph("表格之后还有一段正文。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_keeps_heading_table_and_order() -> None:
    parser = LocalOfficeParser()

    result = parser.parse(content=_docx_bytes(), filename="报告.docx")

    assert "# 眼轴监测" in result.markdown
    assert "眼轴长度是近视防控的核心指标" in result.markdown
    assert "| 年龄 | 参考值 |" in result.markdown
    assert "| 6 岁 | 22.5mm |" in result.markdown
    # 顺序：表格在第二段之前、最后一段之后——分别遍历 paragraphs/tables 会把表格挤到文末
    assert result.markdown.index("| 年龄 |") < result.markdown.index("表格之后还有一段正文")


def test_docx_only_claims_modern_formats() -> None:
    """旧的二进制 .doc 不假装支持：它是另一种格式，只能走云端。"""
    parser = LocalOfficeParser()
    assert parser.supports(filename="a.docx", mime_type=None, probe=_probe())
    assert not parser.supports(filename="a.doc", mime_type=None, probe=_probe())
    assert not parser.supports(filename="a.xlsx", mime_type=None, probe=_probe())


# ---------------------------------------------------------------- Office：pptx


def _pptx_bytes() -> bytes:
    """手工拼一个最小 pptx：幻灯片编号两位数时验证排序（slide2 必须在 slide10 前）。"""
    def slide(n: int, title: str, body: str) -> tuple[str, bytes]:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
            f'<p:sp><p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>'
            f'<p:sp><p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>'
            "</p:spTree></p:cSld></p:sld>"
        )
        return f"ppt/slides/slide{n}.xml", xml.encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for n, title, body in [
            (1, "第一页", "第一页正文"),
            (2, "第二页", "第二页正文"),
            (10, "第十页", "第十页正文"),
        ]:
            name, data = slide(n, title, body)
            archive.writestr(name, data)
    return buffer.getvalue()


def test_pptx_orders_slides_numerically() -> None:
    """按编号排，不是按 zip 条目名的字典序（否则 slide10 会跑到 slide2 前面）。"""
    parser = LocalOfficeParser()

    result = parser.parse(content=_pptx_bytes(), filename="演示.pptx")

    assert result.markdown.index("第二页") < result.markdown.index("第十页")
    assert "## 第一页" in result.markdown
    assert "第一页正文" in result.markdown


def test_pptx_rejects_dtd() -> None:
    """含 DTD 的 XML 直接拒绝：正常 Office 文件不会有 DTD，而实体攻击都以它为前提。"""
    payload = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
        '<p:sp><p:txBody><a:p><a:r><a:t>&a;</a:t></a:r></a:p></p:txBody></p:sp>'
        "</p:spTree></p:cSld></p:sld>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", payload.encode())

    with pytest.raises(ParseError, match="DTD"):
        LocalOfficeParser().parse(content=buffer.getvalue(), filename="bad.pptx")


def test_broken_office_file_reports_readable_reason() -> None:
    with pytest.raises(ParseError, match="读不出来"):
        LocalOfficeParser().parse(content=b"not a zip at all", filename="x.docx")


def test_empty_content_is_rejected() -> None:
    with pytest.raises(ParseError):
        LocalPdfTextParser().parse(content=b"", filename="a.pdf")
    with pytest.raises(ParseError):
        LocalOfficeParser().parse(content=b"", filename="a.docx")
