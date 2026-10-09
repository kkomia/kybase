"""HTML 正文提取 → Markdown：**再导出的壳**，实现在 `app.core.html_format`。

2026-10-08 剥离阶段 0 把实现**例外上移**到共享底座：这一件不只是解析器——
KB 侧的连接器与上传路径要用它（`services/connectors/`、`parsers/html_upload.py`），
Agent 侧的阅读模式也要用它（`services/web.py`）。`parsers/` 整体仍随 KB 走，
所以这里留一个壳，**KB 侧调用点一行没动**（`parsers/html_upload.py`、
`services/connectors/html.py`、`services/connectors/rss.py`）。

为什么不是"Agent 侧复制一份"：这 400 多行正文提取（文本密度打分、两遍扫描、
代码块与列表的 Markdown 还原）是纯算法，复制一份只会让两边的正文质量慢慢分叉。

纪律上它与 `text_decode` / `tabular_format` 同类——**共享的格式转换器**，
不是"某个解析器实现"（见 `scripts/check_layering.py` 的 `PARSER_SHARED`）。
"""

from __future__ import annotations

from app.core.html_format import extract_article, html_to_markdown, html_to_text

__all__ = ["extract_article", "html_to_markdown", "html_to_text"]
