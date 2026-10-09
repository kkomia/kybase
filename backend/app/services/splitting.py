"""大文件强制切分（M2 / **T2.7**，《架构设计 v0.2》§4.2）。

**要解决的问题**：云端解析渠道都有页数上限——MinerU 单文件 ≤ 200 页、
PaddleOCR API ≤ 1000 页。一份 1500 页的 PDF 直接提交只会拿到
``-60006 文件超过 200 页``，然后任务失败。用户看到的是"这个文件传不上去"，
而实际上它完全可以被解析，只是要先在本地按页范围切开、分别送、再拼回来。

模块里只有三件事，而且**刻意都是纯函数或闭包驱动**：

1. :func:`plan_split` —— 纯决策：多少页、该切成几段、每段的页范围。没有 io。
2. :func:`extract_page_range` —— 用 PyMuPDF 按页范围抽出一个新的 PDF 字节串。
3. :class:`PageRangeSplitter` —— 按计划逐段解析并合并。

**为什么不在解析器里做**：解析器（``parsers/``）是 L3，彼此不许互相引用，
也不碰存储；而切分必须**反复调用解析器**并写子文件记录。那是编排，
属于 ``services/``（工程规范 §3.2 的分层）。

**为什么串行**：架构 §4.2 明确要求"流式、顺序调用解析服务，避免并发打满配额或内存"。
并发提交 5 个 300 页的切片会瞬间吃掉当日额度的很大一块，
而且云端任务队列一拥堵，5 个都变慢——总耗时反而更长。

**页码重映射**：所有解析产物统一是 Markdown，而 Markdown 没有"页"这个概念。
不处理的话，一个 1500 页文档的检索结果里页码信息全丢，引文无法定位到原页。
所以合并时在每段开头插入一页标记（:data:`PAGE_MARKER_TEMPLATE`），
让 chunker 能把页码带进 chunk（见 ``chunking.py`` 的页标记识别）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.page_markers import PAGE_MARKER_TEMPLATE as _PAGE_MARKER_TEMPLATE
from app.core.page_markers import page_marker
from app.parsers.base import ParseError, ParseResult, ParserProvider

__all__ = [
    "DEFAULT_PART_PAGES",
    "MINERU_PAGE_LIMIT",
    "PADDLE_PAGE_LIMIT",
    "PAGE_MARKER_TEMPLATE",
    "PageRange",
    "PartOutcome",
    "SplitPlan",
    "SplitResult",
    "extract_page_range",
    "merge_parts",
    "plan_split",
    "render_page_marker",
]

logger = logging.getLogger(__name__)

#: MinerU 标准 API 的单文件页数上限（``-60006`` 对应的就是它）。
MINERU_PAGE_LIMIT = 200

#: PaddleOCR API 的单文件页数上限。
PADDLE_PAGE_LIMIT = 1000

#: 期望的每段页数。
#:
#: **它只是"愿", 实际段长还要服从渠道上限**（见 :func:`plan_split` 的
#: ``step = min(part_pages, limit)``）。所以这里的 500 意味着"如果渠道允许，
#: 一段可以到 500 页"；对上 MinerU 的 200 页上限时实际仍按 200 切。
#:
#: 为什么不直接写 200：将来按渠道智能路由时（架构 §4.2 的"优先 PaddleOCR"），
#: PaddleOCR 上限是 1000 页，用它时就能一次吃 500 页、少一半往返。
#: 段长大本身是有价值的（少几次上传下载），只是不能越过上限。
DEFAULT_PART_PAGES = 500

#: 每段合并时插入的页标记。**写侧**——读侧是 `chunking._PAGE_MARKER`，一对。
#:
#: **为什么要插标记**：产物是 Markdown，没有页的概念。不插的话，
#: 1500 页文档的检索结果全都定位不到原页，引文就失去可核查性。
#:
#: 形态是一个可以被正则认出的独占行。`chunking` 会认它、把它从正文里摘掉、
#: 把页号写进 `ChunkRecord.page`。
#:
#: 页标记的格式定义在 ``app/core/page_markers.py``（写侧与读侧共用一份，
#: 解析器也要写它——那里解释了为什么不能各写一份）。这里保留同名别名，
#: 老代码与测试继续用 ``splitting.PAGE_MARKER_TEMPLATE`` 即可。
#:
#: 语义上它是**段起始页标记**：MinerU/PaddleOCR 逐页产出时插的是真实页号，
#: 切段合并时插的是该段的第一页。chunker 把它当**硬边界**（一块只能属于一页，
#: 标错了比没有更糟），所以块不会横跨两个标记。
PAGE_MARKER_TEMPLATE = _PAGE_MARKER_TEMPLATE


@dataclass(frozen=True, slots=True)
class PageRange:
    """一段页范围。``start`` / ``end`` 都是**从 1 开始的闭区间**（与 UI 上"第 3–7 页"一致）。"""

    start: int
    end: int

    @property
    def page_count(self) -> int:
        return self.end - self.start + 1

    def __str__(self) -> str:
        return f"P{self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """切分计划。``parts`` 为空表示**不需要切**。"""

    total_pages: int
    parts: tuple[PageRange, ...]
    reason: str

    @property
    def needed(self) -> bool:
        return bool(self.parts)


def plan_split(
    page_count: int | None,
    *,
    part_pages: int = DEFAULT_PART_PAGES,
    limit: int = MINERU_PAGE_LIMIT,
) -> SplitPlan:
    """决定要不要切、怎么切。

    ``limit`` 是**渠道上限**（默认取两个里更严的 MinerU 200 页），
    ``part_pages`` 是**期望的段长**。两者都参与，但分工必须分清：

    - ``limit`` 决定**要不要切**；
    - 实际段长取 ``min(part_pages, limit)``，保证**每一段都不超过渠道上限**。

    ``limit`` 只用来判"要不要切"。早先的写法是"``limit`` 既判要不要切、
    又当段长用"，于是一份 201 页的文档（超 MinerU 上限）会被切成
    ``(1, 500) → 钳到 (1, 201)`` 这样**一段 201 页**——等于没切，
    提交上去仍然拿 ``-60006``。段长必须服从上限，这是这一条的全部意义。

    ``page_count`` 为 ``None``（纯文本、表格，或探测拿不到页数）一律返回"不切"——
    **不知道页数就不许猜**：按猜测切一个 Markdown 文件只会把内容截断。
    """
    if part_pages < 1:
        raise ValueError(f"part_pages 必须为正整数，收到 {part_pages}")
    if limit < 1:
        raise ValueError(f"limit 必须为正整数，收到 {limit}")

    if page_count is None:
        return SplitPlan(total_pages=0, parts=(), reason="无法确定页数，不做切分")
    if page_count <= 0:
        return SplitPlan(total_pages=page_count, parts=(), reason="页数为 0，不做切分")

    if page_count <= limit:
        return SplitPlan(
            total_pages=page_count,
            parts=(),
            reason=f"{page_count} 页未超过 {limit} 页上限",
        )

    # 段长服从上限：`part_pages` 比 limit 大时以 limit 为准。
    # 这一行是"切出来的段一定不会被渠道拒"的唯一保证
    step = min(part_pages, limit)
    ranges: list[PageRange] = []
    start = 1
    while start <= page_count:
        end = min(start + step - 1, page_count)
        ranges.append(PageRange(start=start, end=end))
        start = end + 1

    return SplitPlan(
        total_pages=page_count,
        parts=tuple(ranges),
        reason=(
            f"{page_count} 页超过 {limit} 页上限，切为 {len(ranges)} 段（每段最多 {step} 页）"
        ),
    )


def extract_page_range(content: bytes, pages: PageRange) -> bytes:
    """从 PDF 里抽出 ``pages`` 指定的页，返回一个新的 PDF 字节串。

    **越界钳制而不是报错**：探测到的页数与实际能读出的页数理论上应当一致，
    但探测与切分之间文件是不变的，唯一的可能是 PDF 本身页数信息损坏。
    这种情况下把范围钳到实际页数、继续解析一段总比整个文件失败好——
    能救回来的页就是能救回来的内容。

    页范围是 1-based 闭区间（``pymupdf`` 的 ``select`` 也是这个口径）。
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 里声明
        raise ParseError(
            "缺少 pymupdf，无法按页切分大文件。请先安装依赖", stage="parsing"
        ) from exc

    try:
        source = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ParseError(f"PDF 无法打开，无法按页切分：{exc}", stage="parsing") from exc

    try:
        total = source.page_count
        start = max(1, pages.start)
        end = min(pages.end, total)
        if start > end:
            raise ParseError(
                f"页范围 {pages} 超出实际页数（共 {total} 页）", stage="parsing"
            )

        target = pymupdf.open()
        try:
            target.insert_pdf(source, from_page=start - 1, to_page=end - 1)
            return target.tobytes()
        finally:
            target.close()
    finally:
        source.close()


def render_page_marker(page: int) -> str:
    """给合并用的页标记。"""
    return page_marker(page)


def merge_parts(texts: Sequence[str], parts: Sequence[PageRange]) -> str:
    """按顺序把各段产物拼起来，每段前面插上该段起始页的标记。

    ``texts`` 与 ``parts`` 必须等长：长度不一致说明调用方把某段的产物弄丢了，
    这属于编程错误，直接抛而不是猜。
    """
    if len(texts) != len(parts):
        raise ValueError(f"产物数量 {len(texts)} 与页段数量 {len(parts)} 不一致")

    blocks: list[str] = []
    for text, part in zip(texts, parts, strict=True):
        blocks.append(f"{render_page_marker(part.start)}\n\n{text.strip()}")
    return "\n\n".join(blocks) + "\n"


@dataclass(slots=True)
class PartOutcome:
    """一段的解析结果。失败的段带 ``error``，成功的带 ``markdown``。"""

    part: PageRange
    part_id: str
    markdown: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class SplitResult:
    """切分解析的汇总。"""

    markdown: str
    outcomes: list[PartOutcome]

    @property
    def failed(self) -> list[PartOutcome]:
        return [item for item in self.outcomes if not item.ok]

    @property
    def succeeded(self) -> list[PartOutcome]:
        return [item for item in self.outcomes if item.ok]


class PageRangeSplitter:
    """按页范围逐段解析并合并。

    三个回调把"记录簿记"与"切分编排"隔开，本类因此**不 import 任何存储实现**：

    - ``on_part``：一段刚要开始解析（调用方在这里把子文件置为 ``parsing``）；
    - ``on_part_done``：一段结束，成功给 markdown、失败给 error；
    - ``part_id_of``：由调用方决定子文件的 id（本类不生成 id）。
    """

    def __init__(
        self,
        parser: ParserProvider,
        *,
        on_part: Callable[[PageRange, str], None] | None = None,
        on_part_done: Callable[[PartOutcome], None] | None = None,
        part_id_of: Callable[[int], str] | None = None,
    ) -> None:
        self._parser = parser
        self._on_part = on_part
        self._on_part_done = on_part_done
        self._part_id_of = part_id_of or (lambda index: f"part_{index:03d}")

    def run(
        self,
        *,
        content: bytes,
        filename: str,
        plan: SplitPlan,
        mime_type: str | None = None,
        probe=None,
    ) -> SplitResult:
        """逐段执行。

        **一段失败不放弃其余段**：200 页的那一段可能因为版面复杂而失败，
        而前后两段完全正常。放弃全部会让用户重跑整个 1500 页，
        而按段记录后他只需重跑失败的那一段（架构 §4.2：
        "单个子文件失败只需重跑该子文件"）。
        """
        outcomes: list[PartOutcome] = []
        texts: list[str] = []

        for index, part in enumerate(plan.parts):
            part_id = self._part_id_of(index)
            if self._on_part is not None:
                self._on_part(part, part_id)

            try:
                chunk_bytes = extract_page_range(content, part)
                parsed = self._parser.parse(
                    content=chunk_bytes,
                    filename=_suffixed_name(filename, part),
                    mime_type=mime_type or "application/pdf",
                    probe=probe,
                )
                outcome = PartOutcome(part=part, part_id=part_id, markdown=parsed.markdown)
                texts.append(parsed.markdown)
            except ParseError as exc:
                outcome = PartOutcome(part=part, part_id=part_id, error=str(exc))
                texts.append("")
                logger.warning("第 %s 段解析失败：%s", part, exc)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                outcome = PartOutcome(part=part, part_id=part_id, error=detail)
                texts.append("")
                logger.exception("第 %s 段解析出现意外异常", part)

            outcomes.append(outcome)
            if self._on_part_done is not None:
                self._on_part_done(outcome)

        # 只合并成功的段：把空字符串也标记进去会让页标记指向一片空白
        kept_texts = [text for text, item in zip(texts, outcomes, strict=True) if item.ok]
        kept_parts = [item.part for item in outcomes if item.ok]
        markdown = merge_parts(kept_texts, kept_parts) if kept_texts else ""
        return SplitResult(markdown=markdown, outcomes=outcomes)


def build_result(result: SplitResult, *, parser_name: str, probe=None) -> ParseResult:
    """把切分汇总成**一个** ``ParseResult``。

    对下游（chunker / 存储 / API）来说，切分是内部实现：它拿到的仍是一份文档、
    一份 Markdown。子文件的意义只在 UI（可展开、可单独重跑）。

    ``page_count`` 取**所有段**之和，而不是只有成功的段：页数是原文件的属性，
    不是"解析成功了多少页"。缺了失败的那几页不是因为我们不知道它们有多少页，
    而是解析没成功——把总数改小会让界面显示一个错误的页数，
    而真正的坏消息（哪几段失败）由调用方抛出的错误说清楚。
    """
    total = sum(item.part.page_count for item in result.outcomes)
    return ParseResult(
        markdown=result.markdown,
        parser_name=parser_name,
        page_count=total or None,
        probe=probe,
        image_ids=[],
    )


def _suffixed_name(filename: str, part: PageRange) -> str:
    """给切片的文件名带上页范围。

    不是装饰：云端解析服务会**按文件名**在它那侧登记任务，
    同名提交两次在部分渠道上会被当成同一个任务去重。
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem}_{part}.pdf"

