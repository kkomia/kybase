"""``PlainTextParser`` 的单元测试。

镜像同构：``app/parsers/plain_text.py`` → ``tests/unit/parsers/test_plain_text.py``。
"""

import pytest

from app.parsers.base import ParseError, ProbeKind, ProbeResult
from app.parsers.plain_text import PlainTextParser
from app.parsers.probe import probe


@pytest.fixture
def parser() -> PlainTextParser:
    return PlainTextParser()


def _text_probe(coverage: float = 1.0) -> ProbeResult:
    return ProbeResult(kind=ProbeKind.TEXT, text_coverage=coverage)


# --------------------------------------------------------------------- supports


def test_supports_markdown_by_extension(parser: PlainTextParser) -> None:
    assert parser.supports(filename="a.md", mime_type=None, probe=_text_probe()) is True


def test_supports_text_by_mime(parser: PlainTextParser) -> None:
    assert parser.supports(filename="a", mime_type="text/plain", probe=_text_probe()) is True


def test_does_not_support_extensionless_binary_container(parser: PlainTextParser) -> None:
    """没有后缀但探测结论是"扫描件"时不能接：二进制容器不该被当文本读。"""
    scanned = ProbeResult(kind=ProbeKind.SCANNED, text_coverage=0.0)
    assert parser.supports(filename="payload", mime_type=None, probe=scanned) is False


def test_never_supports_binary_containers_even_when_text_like(
    parser: PlainTextParser,
) -> None:
    """PDF 的头是 ASCII，文本层覆盖率也可能很高——但它是容器格式，不能直读。

    这正是踩过的坑：PDF 被纯文本直通接走，切出来的块是原始 PDF 字节流。
    """
    probe = ProbeResult(kind=ProbeKind.TEXT, text_coverage=1.0)
    for name in ("doc.pdf", "report.docx", "sheet.xlsx", "scan.png"):
        assert parser.supports(filename=name, mime_type=None, probe=probe) is False


def test_does_not_support_scanned_content(parser: PlainTextParser) -> None:
    scanned = ProbeResult(kind=ProbeKind.SCANNED, text_coverage=0.0)
    assert parser.supports(filename="scan.pdf", mime_type=None, probe=scanned) is False


# --------------------------------------------------------------------- 解析


def test_markdown_passes_through_unchanged(parser: PlainTextParser) -> None:
    source = "# 标题\n\n正文段落。\n"
    result = parser.parse(content=source.encode(), filename="doc.md")

    assert result.markdown == source
    assert result.parser_name == "PlainTextParser"
    assert result.page_count is None


def test_plain_text_is_wrapped_as_markdown(parser: PlainTextParser) -> None:
    """非 Markdown 文本要包一层，否则正文里的 # 会被当成标题。"""
    result = parser.parse(content="第一行\n第二行\n".encode(), filename="notes.txt")

    assert result.markdown.startswith("# notes.txt")
    assert "第一行" in result.markdown
    assert "```text" in result.markdown  # 正文被放进代码块


def test_probe_result_is_carried_through(parser: PlainTextParser) -> None:
    result = parser.parse(content=b"x", filename="a.md", probe=_text_probe(0.8))
    assert result.probe is not None and result.probe.text_coverage == 0.8


def test_end_to_end_with_real_probe(parser: PlainTextParser) -> None:
    """探测 + 解析串起来跑一遍（路由链路的最小闭环）。"""
    content = "# 知识库\n\n向量检索说明。".encode()
    result = parser.parse(content=content, filename="kb.md", probe=probe(content, filename="kb.md"))
    assert "向量检索说明" in result.markdown


# --------------------------------------------------------------------- 编码阶梯


def test_utf8_bom_is_stripped(parser: PlainTextParser) -> None:
    result = parser.parse(content="\ufeff# 标题".encode(), filename="a.md")
    assert result.markdown == "# 标题"


def test_gb18030_is_decoded(parser: PlainTextParser) -> None:
    """中文环境大量存量文件是 GBK/GB18030，不能变成乱码。"""
    result = parser.parse(content="# 中文标题".encode("gb18030"), filename="a.md")
    assert "中文标题" in result.markdown


def test_undecodable_bytes_fall_back_without_losing_document(parser: PlainTextParser) -> None:
    """宁可留几个替换字符，也不该因为一个坏字节丢掉整篇文档。"""
    result = parser.parse(content=b"abc\xff\xfe\x00def", filename="broken.txt")
    assert result.markdown


def test_empty_content_raises_parse_error(parser: PlainTextParser) -> None:
    with pytest.raises(ParseError, match="内容为空"):
        parser.parse(content=b"", filename="empty.txt")


def test_decode_reports_filename_in_error() -> None:
    with pytest.raises(ParseError, match=r"empty\.txt"):
        PlainTextParser.decode(b"", "empty.txt")
