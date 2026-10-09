"""解析路由决策器的单元测试。

镜像同构：``app/services/parser_router.py`` → ``tests/unit/services/test_parser_router.py``。
"""

import pytest

from app.parsers.base import ParseError, ParseResult, ParserProvider, ProbeKind, ProbeResult
from app.parsers.plain_text import PlainTextParser
from app.services.parser_router import ParserRouter


def _text_probe() -> ProbeResult:
    return ProbeResult(kind=ProbeKind.TEXT, text_coverage=1.0)


def _scanned_probe() -> ProbeResult:
    return ProbeResult(kind=ProbeKind.SCANNED, text_coverage=0.0)


class _AlwaysParser(ParserProvider):
    """测试替身：无条件支持，用来验证注册顺序即优先级。"""

    name = "AlwaysParser"

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        return True

    def parse(self, *, content: bytes, filename: str, mime_type: str | None = None,
              probe: ProbeResult | None = None) -> ParseResult:
        return ParseResult(markdown="", parser_name=self.name)


def test_router_requires_at_least_one_parser() -> None:
    with pytest.raises(ValueError):
        ParserRouter([])


def test_routes_markdown_to_plain_text_parser() -> None:
    router = ParserRouter([PlainTextParser()])
    decision = router.decide(filename="a.md", mime_type=None, probe=_text_probe())

    assert decision.parser_name == "PlainTextParser"
    assert "PlainTextParser" in decision.reason
    assert decision.probe.kind == ProbeKind.TEXT


def test_decision_reason_mentions_probe_kind() -> None:
    """理由要能落进任务记录，供控制台解释路由。"""
    router = ParserRouter([PlainTextParser()])
    decision = router.decide(filename="a.md", mime_type=None, probe=_text_probe())
    assert ProbeKind.TEXT in decision.reason


def test_unsupported_file_raises_with_stage() -> None:
    router = ParserRouter([PlainTextParser()])
    with pytest.raises(ParseError) as excinfo:
        router.decide(filename="scan.pdf", mime_type="application/pdf", probe=_scanned_probe())

    assert "暂不支持" in str(excinfo.value)
    assert excinfo.value.stage == "probing"  # 失败定位到探测阶段


def test_registration_order_is_priority() -> None:
    router = ParserRouter([_AlwaysParser(), PlainTextParser()])
    decision = router.decide(filename="a.md", mime_type=None, probe=_text_probe())
    assert decision.parser_name == "AlwaysParser"


def test_exposes_registered_parser_names() -> None:
    router = ParserRouter([PlainTextParser(), _AlwaysParser()])
    assert router.parser_names == ("PlainTextParser", "AlwaysParser")


def test_html_upload_goes_to_the_html_parser_not_plain_text() -> None:
    """`.html` 必须走正文提取。顺序反了就会把 <script> 与导航原样收进库
    ——纯文本直通在 MIME 是 text/* 时会收下任何东西。"""
    from app.parsers.html_upload import HtmlUploadParser
    from app.parsers.probe import probe

    router = ParserRouter([HtmlUploadParser(), PlainTextParser()])
    html = "<html><body><script>x</script><p>正文</p></body></html>".encode()

    decision = router.decide(
        filename="page.html", mime_type="text/html", probe=probe(html, filename="page.html")
    )

    assert decision.parser_name == "HtmlUploadParser"


def test_built_in_order_prefers_cloud_and_keeps_local_as_fallback(bundle) -> None:  # type: ignore[no-untyped-def]
    """真实注册顺序：云端在前、本地兜底在后（v17）。

    刻意验"顺序"而不是"有没有"：顺序反了会让配了 MinerU 的用户也吃本地直提，
    而那正是版面还原更差的路径。
    """
    from app.services.parser_router import build_parsers
    from app.services.runtime_config import RuntimeConfigService

    names = [
        parser.name for parser in build_parsers(RuntimeConfigService(bundle))
    ]

    assert names.index("MinerUCloudParser") < names.index("LocalPdfTextParser")
    assert names.index("PaddleOCRApiParser") < names.index("LocalPdfTextParser")
    assert names.index("LocalPdfTextParser") < names.index("LocalOfficeParser")
    # 表格与 HTML 必须在纯文本直通之前：它们都争同一个后缀
    assert names.index("TabularParser") < names.index("PlainTextParser")
    assert names.index("HtmlUploadParser") < names.index("PlainTextParser")


def test_text_pdf_routes_to_local_when_no_cloud_credentials(bundle) -> None:  # type: ignore[no-untyped-def]
    """没配任何云端凭据时，文字型 PDF 也要能解析（v17 之前它直接"暂不支持"）。"""
    pytest.importorskip("pypdf")  # parsers extra 未装时 supports() 恒为 False（见解析器注释）
    from app.services.parser_router import build_parsers
    from app.services.runtime_config import RuntimeConfigService

    router = ParserRouter(build_parsers(RuntimeConfigService(bundle)))

    decision = router.decide(
        filename="文字型.pdf",
        mime_type="application/pdf",
        probe=_text_probe(),
    )

    assert decision.parser_name == "LocalPdfTextParser"


def test_scanned_pdf_still_unsupported_without_credentials(bundle) -> None:  # type: ignore[no-untyped-def]
    """扫描件没有云端凭据时**仍然明确不支持**：本地没有 OCR，
    给一份空白正文比说"不支持"更糟（用户以为收进来了）。"""
    from app.services.parser_router import build_parsers
    from app.services.runtime_config import RuntimeConfigService

    router = ParserRouter(build_parsers(RuntimeConfigService(bundle)))

    with pytest.raises(ParseError, match="暂不支持"):
        router.decide(filename="扫描件.pdf", mime_type="application/pdf", probe=_scanned_probe())


def test_scanned_pdf_hint_points_to_the_settings_page(bundle) -> None:  # type: ignore[no-untyped-def]
    """说不支持之后要给出路：扫描件 + 没配云端引擎时，用户手上只有个打不开的 PDF。"""
    from app.services.parser_router import build_parsers
    from app.services.runtime_config import RuntimeConfigService

    router = ParserRouter(build_parsers(RuntimeConfigService(bundle)))

    with pytest.raises(ParseError) as excinfo:
        router.decide(filename="扫描件.pdf", mime_type="application/pdf", probe=_scanned_probe())
    assert "MinerU" in str(excinfo.value)


def test_legacy_doc_hint_says_save_as_docx(bundle) -> None:  # type: ignore[no-untyped-def]
    """旧的二进制 .doc 本地不解析（另一种格式），但要说清怎么办。"""
    from app.services.parser_router import build_parsers
    from app.services.runtime_config import RuntimeConfigService

    router = ParserRouter(build_parsers(RuntimeConfigService(bundle)))

    with pytest.raises(ParseError) as excinfo:
        router.decide(filename="旧文档.doc", mime_type=None, probe=_text_probe())
    assert ".docx" in str(excinfo.value)


def test_candidates_returns_all_supporting_parsers_in_priority_order() -> None:
    """降级的依据：谁都能解时要把它们**按优先级**都列出来。

    只给一个的话，首选失败就等于整份文档失败——扫描件的 MinerU/PaddleOCR
    两条通道、文字型 PDF 的本地直提都白放着。
    """
    router = ParserRouter([_AlwaysParser(), PlainTextParser()])

    decisions = router.candidates(
        filename="a.md", mime_type="text/markdown", probe=_text_probe()
    )

    assert [item.parser_name for item in decisions] == ["AlwaysParser", "PlainTextParser"]
    # decide 仍然只给第一个（单次决策语义不变）
    assert (
        router.decide(filename="a.md", mime_type="text/markdown", probe=_text_probe()).parser_name
        == "AlwaysParser"
    )


def test_candidates_is_empty_for_unsupported_types() -> None:
    """一个都不支持时给空列表（由调用方决定怎么报错），而不是抛异常。"""
    router = ParserRouter([PlainTextParser()])

    empty = router.candidates(
        filename="a.pdf", mime_type="application/pdf", probe=_scanned_probe()
    )

    assert empty == []
