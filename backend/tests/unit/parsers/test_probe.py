"""文件探测的单元测试。

镜像同构：``app/parsers/probe.py`` → ``tests/unit/parsers/test_probe.py``。
"""

import pytest

from app.parsers.base import ProbeKind
from app.parsers.probe import probe, suffix_of


def test_markdown_file_is_text() -> None:
    result = probe("# 标题\n正文".encode(), filename="note.md")
    assert result.kind == ProbeKind.TEXT
    assert result.text_coverage == pytest.approx(1.0)
    assert result.detail["is_markdown"] is True


def test_plain_text_extension_is_text_even_if_short() -> None:
    assert probe(b"abc", filename="a.txt").kind == ProbeKind.TEXT


def test_text_mime_type_is_text() -> None:
    assert probe(b"hello", filename="noext", mime_type="text/plain").kind == ProbeKind.TEXT


def test_utf8_content_without_extension_is_text() -> None:
    """没有扩展名时靠内容判定：UTF-8 可解码即认为有文本层。"""
    result = probe("这是一段中文内容".encode(), filename="payload")
    assert result.kind == ProbeKind.TEXT
    assert result.detail["suffix"] == ""


def test_binary_content_is_scanned() -> None:
    result = probe(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", filename="image.png")
    assert result.kind == ProbeKind.SCANNED
    assert result.text_coverage == 0.0


def test_extension_wins_over_content_heuristic() -> None:
    """扩展名/MIME 声明优先于内容启发式：带 NUL 的 .txt 仍按文本处理。

    否则一个含异常字节的 txt 会被"路由去 OCR"，那显然更错。
    内容为空这类问题由解析器自己报错（见 PlainTextParser.decode）。
    """
    assert probe(b"abc\x00def", filename="fake.txt").kind == ProbeKind.TEXT


def test_binary_without_text_extension_is_scanned() -> None:
    assert probe(b"abc\x00def", filename="blob").kind == ProbeKind.SCANNED


def test_empty_content_without_extension_is_not_text() -> None:
    assert probe(b"", filename="empty").kind == ProbeKind.SCANNED


def test_empty_markdown_still_routes_to_text() -> None:
    """空 .md 仍按文本路由，由解析器给出"内容为空"的明确错误。"""
    assert probe(b"", filename="empty.md").kind == ProbeKind.TEXT


def test_non_utf8_bytes_fall_back_to_printable_heuristic() -> None:
    """无扩展名的 GBK 文本：UTF-8 解码失败后走可打印字符占比判定，仍应是文本。"""
    result = probe("中文内容示例".encode("gb18030"), filename="payload")
    assert result.kind == ProbeKind.TEXT
    assert result.text_coverage == pytest.approx(1.0)


def test_detail_explains_the_decision() -> None:
    """detail 会落进任务记录，必须能解释"为什么这样路由"。"""
    result = probe("# 标题".encode(), filename="a.md")
    assert result.detail["reason"]
    assert "suffix" in result.detail


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.MD", ".md"),
        ("notes/path/file.tar.gz", ".gz"),
        ("noext", ""),
        (".hidden", ""),  # 点开头的是隐藏文件，不是扩展名
    ],
)
def test_suffix_of(filename: str, expected: str) -> None:
    assert suffix_of(filename) == expected
