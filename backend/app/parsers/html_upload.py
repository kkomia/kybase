"""上传的 HTML 文件 → Markdown（v17 补的本地兜底）。

**为什么需要它**：`.html` 原先会落到纯文本直通（靠"声明的 MIME 是 `text/*`"兜底），
于是 `<script>`、导航、页脚原样进库——用户问什么都能匹配到菜单里的"首页 关于 联系方式"。
正文提取与剥标签的逻辑早就在（连接器路径用的 `html_format`），只是上传路径没有复用。

**与连接器的分工**：连接器负责"抓一个 URL"（网络、重试、编码协商），
这里只负责"手里已经有一份 HTML 字节，把它变成可入库的正文"。
所以本模块是纯函数式的，不认识网络。
"""

from __future__ import annotations

from app.parsers.base import ParseError, ParseResult, ParserProvider, ProbeResult
from app.parsers.html_format import extract_article, html_to_markdown
from app.parsers.probe import suffix_of
from app.parsers.text_decode import decode_bytes

__all__ = ["HTML_EXTENSIONS", "HtmlUploadParser"]

HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml"})


class HtmlUploadParser(ParserProvider):
    """上传的 HTML：先做正文提取，提取不出来再退回整页转换。

    解码复用共享的 `decode_bytes`（UTF-8 → GB18030 → 替换），不自己写一遍：
    中文网页里 GB18030 很常见，两处各写一份迟早只剩一处能修。
    """

    name = "HtmlUploadParser"

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        if suffix_of(filename) in HTML_EXTENSIONS:
            return True
        # 后缀不认识时按 MIME 认一条路：有些导出工具会给 `download` 这种文件名
        return (mime_type or "").lower().startswith("text/html")

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        text = decode_bytes(content, filename)
        markdown = extract_article(text).strip()
        if not markdown:
            # 正文提取挑不出容器时退回整页转换——**宁可带点噪声，也别让文档变空**：
            # 空文档进了库，用户以为收进来了，实际什么也检索不到
            markdown = html_to_markdown(text).strip()
        if not markdown:
            raise ParseError(f"这个 HTML 里没有可提取的正文：{filename or '(未命名)'}")
        return ParseResult(
            markdown=markdown,
            parser_name=self.name,
            page_count=None,  # 网页没有页的概念
            probe=probe,
        )
