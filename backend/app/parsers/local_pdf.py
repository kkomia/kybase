"""本地 PDF 文本层直提（v17）。

**为什么需要它**：原先文字型 PDF 也一律送云端（MinerU）。没配 token 时
`parser_router` 直接抛「暂不支持的文件类型」——**一个纯文字的 PDF 打不开**，
而这类文件用本地库几毫秒就能抽出来。这是"没有云端凭据时产品还能不能用"的下限。

**能力边界（刻意划清）**：
- 只接**文字型** PDF（探测覆盖率 ≥ 阈值）。扫描件/混合件需要 OCR/版面分析，
  本地做不了——那类文件仍旧返回不支持，让用户去配 MinerU/PaddleOCR，
  而不是给出一份丢了一半页面的正文。
- **不做版面还原**：按页抽文本、逐页标记 `<!-- page:N -->`，表格会退化成一行行的
  文字（云端引擎的 TSR 更强）。所以路由上它排在云端引擎**之后**：
  有凭据就用好的，没凭据至少能用。

页标记是白拿的收益：chunk 因此带上页码，引用能直接跳到 PDF 那一页
（`page_markers` 的写读两侧共用一份定义，与云端路径完全一致）。
"""

from __future__ import annotations

import io

from app.core.page_markers import page_marker
from app.parsers.base import ParseError, ParseResult, ParserProvider, ProbeKind, ProbeResult
from app.parsers.probe import PDF_EXTENSIONS, suffix_of

__all__ = ["LocalPdfTextParser"]


class LocalPdfTextParser(ParserProvider):
    """文字型 PDF 的本地直提（pypdf）。

    名字里带 ``Local`` 是刻意的：``parse_results.parser_name`` 会落库并在界面上显示
    "这个文件是谁解析的"，用户据此判断"我这份 PDF 是本地抽的还是云端还原的"。
    """

    name = "LocalPdfTextParser"

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        if suffix_of(filename) not in PDF_EXTENSIONS:
            return False
        # 只接文字型：MIXED 意味着有些页没有文本层，本地抽会**静默丢掉那些页**
        # （用户看到一份缺页的文档，比看到"不支持"更难排查）
        if probe.kind != ProbeKind.TEXT:
            return False
        try:
            import pypdf  # noqa: F401
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
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            pages = [self._page_text(page) for page in reader.pages]
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"PDF 文本提取失败：{exc}") from exc

        blocks: list[str] = []
        for index, text in enumerate(pages, start=1):
            if not text.strip():
                # 空页也写标记：**页码不能因为跳过而压缩**（第 7 页不能因为
                # 第 6 页是空的就变成第 6 页），否则引用跳页会整体错位
                blocks.append(page_marker(index))
                continue
            blocks.append(f"{page_marker(index)}\n\n{text.strip()}")
        markdown = "\n\n".join(blocks).strip()

        if not markdown.strip():
            raise ParseError(
                f"这份 PDF 没有可提取的文字：{filename or '(未命名)'}；"
                "如果是扫描件，请到「设置 → 服务配置」配置 MinerU 或 PaddleOCR"
            )
        return ParseResult(
            markdown=markdown,
            parser_name=self.name,
            page_count=len(pages),
            probe=probe,
        )

    @staticmethod
    def _page_text(page) -> str:  # type: ignore[no-untyped-def]
        """抽一页的文本，顺带把行内的碎换行收一收。

        pypdf 按版面位置换行，于是同一段话被切成很多短行；不收的话切块时
        这些短行会各自成段，检索命中后给模型的上下文全是断句。
        只合并"不像列表项/标题"的行，避免把真正的结构压平。
        """
        raw = page.extract_text() or ""
        lines: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            # 明显是独立结构（标题、列表项、表格行）就保留换行
            if stripped.startswith(("#", "-", "*", "•", "·")) or stripped[0].isdigit():
                lines.append(stripped)
                continue
            if lines and lines[-1] and not lines[-1].endswith(("。", "！", "？", "：", ";", "；")):
                # 上一行没结束且当前行不像新段落 → 接回去
                lines[-1] = f"{lines[-1]}{stripped}"
            else:
                lines.append(stripped)
        return "\n".join(lines).strip()
