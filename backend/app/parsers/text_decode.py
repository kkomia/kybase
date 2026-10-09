"""文本字节的解码阶梯（多个解析器共用）。

**为什么单独成模块**：`.txt` 与 `.html` 都要"把字节变成文字"，而中文环境里
GB18030 非常常见——两处各写一份阶梯，迟早只剩一处能修（另一处会在某份 GB18030
文件上报乱码）。它与 `tabular_format` / `html_format` 同类：**共享的格式处理**，
不是某个解析器实现，所以允许被多个解析器引用（L3 的共享清单）。

全部解码都失败时用 UTF-8 替换错误字符，而不是抛错：宁可留几个乱码字符，
也不要因为编码问题丢掉整篇文档。
"""

from __future__ import annotations

from app.parsers.base import ParseError

__all__ = ["ENCODINGS", "decode_bytes"]

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
"""解码阶梯：先 UTF-8（含 BOM），再中文环境最常见的 GB18030，最后替换兜底。"""


def decode_bytes(content: bytes, filename: str = "") -> str:
    """按阶梯解码；空内容直接报错（空文件没有可解析的东西）。"""
    if not content:
        raise ParseError(f"文件内容为空：{filename or '(未命名)'}")
    for encoding in ENCODINGS:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")
