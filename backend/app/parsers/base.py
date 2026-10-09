"""解析插件层的共享契约（M2 T2.3）。

纪律（工程规范 §3.3）：**各解析器实现只依赖本模块的 ``ParseResult``，实现之间互不引用**。
路由决策（该用哪个解析器）属于业务逻辑，放 ``services/``，不放在 ``parsers/``——
否则路由一 import 实现就违反了 L3 规则（`scripts/check_layering.py` 会拦）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ParseError",
    "ParseResult",
    "ParserProvider",
    "ProbeKind",
    "ProbeResult",
]


class ProbeKind:
    """探测结论（架构 §4.1）。"""

    TEXT = "text"
    """有文本层：直提，不经 OCR。"""

    SCANNED = "scanned"
    """扫描件/图片型：需走 OCR。"""

    MIXED = "mixed"
    """混合型：部分页有文本层，需页级路由。"""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """探测结果。

    路由决策与它一起写入任务记录，界面才能回答"这个文件为什么走了 OCR"（架构 §4.1）。
    """

    kind: str
    text_coverage: float
    page_count: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParseResult:
    """解析中间产物。

    所有格式统一产出 Markdown，图片以锚点语法内联（``![img](image_id)``）；
    ``caption`` 是多模态扩展预留位，当前不填（架构 §7）。
    """

    markdown: str
    parser_name: str
    page_count: int | None = None
    probe: ProbeResult | None = None
    image_ids: list[str] = field(default_factory=list)
    caption: str | None = None


class ParseError(Exception):
    """解析失败。

    带上 ``stage`` 便于状态机把失败定位到具体步骤（架构 §4：失败定位到具体步骤）。
    """

    def __init__(self, message: str, *, stage: str = "parsing") -> None:
        super().__init__(message)
        self.stage = stage


class ParserProvider(ABC):
    """解析插件协议。

    实现类命名规则 ``<引擎><节点>Parser``（工程规范 §3.2），例如
    ``PlainTextParser`` / ``MinerUCloudParser`` / ``PaddleOCRApiParser``。
    ``name`` 是**决策与落库用的标识**，必须与类名一致——它会写进
    ``parse_results.parser_name``，界面上显示"这个文件是谁解析的"，也用于问题定位。
    """

    name: str = "unnamed"

    @abstractmethod
    def supports(self, *, filename: str, mime_type: str | None, probe: ProbeResult) -> bool:
        """能否处理该文件。路由决策器据此挑选候选。"""

    @abstractmethod
    def parse(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str | None = None,
        probe: ProbeResult | None = None,
    ) -> ParseResult:
        """解析为 Markdown。失败必须抛 :class:`ParseError`，不要返回半成品。"""
