"""表格解析器：CSV / TSV / Excel（M2 / T2.11）。

**为什么表格不能走纯文本直通**：``PlainTextParser`` 把 CSV 原文当 Markdown 产物，
于是切块得到的是 ``张三,30,北京`` 这种形状——**列名只在第一行出现一次**，
切块之后大部分行的列名就不在同一块里了。结果是一列孤零零的 ``30``
什么也说明不了：检索"年龄"命中不到它。

所以这个解析器把每行渲染成 ``第 1 行：姓名=张三，年龄=30，城市=北京``：
**列名在每一行里都出现**，那一列的值才保留了自己的语义。

**它只产出用于检索的文本**；结构化副本由 ``IngestService`` 在同一个文档上另写
（见 ``services/tabular.py`` 的双写说明）。一份数据两种形态，各自服务一个目的：
检索要能命中，查询要能精确定位。
"""

from __future__ import annotations

from app.parsers.base import ParseResult, ParserProvider, ProbeResult
from app.parsers.probe import suffix_of
from app.parsers.tabular_format import TABULAR_EXTENSIONS, parse_tabular, rows_to_text

__all__ = ["TabularParser"]


class TabularParser(ParserProvider):
    """表格直读：渲染成"每行都带列名"的文本。

    它必须**排在 ``PlainTextParser`` 之前**——两者都认 ``.csv``，
    而纯文本直通会把这个文件接走并丢掉列名（见模块说明）。
    """

    name = "TabularParser"

    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        return suffix_of(filename) in TABULAR_EXTENSIONS

    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        # 借 services/tabular 的**模块级纯函数**，不自己读 CSV：
        # 解码阶梯、列名去重、行数上限那些规则只该有一份实现。
        # （用纯函数而不是 TabularService，是因为解析器不该也不需要拿到 stores。）
        parsed = parse_tabular(filename=filename, content=content)
        text = rows_to_text(parsed, sheet_name=filename)
        return ParseResult(
            # markdown 是 str（不是 bytes）：ParseResult 统一以文本形态流转，
            # 编码只在落对象存储时发生
            markdown=text,
            parser_name=self.name,
            page_count=None,
            image_ids=[],
        )
