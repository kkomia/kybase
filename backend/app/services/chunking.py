"""切分器（M2 T2.8）。

三种模式（架构 §5.1）：**固定长度 / 语义切块 / 父子切块**。本模块实现固定长度，
另外两种按计划在 M2 后段补齐。

两个与架构承诺直接相关的细节：

- **标题路径注入**：chunk 记录 ``第3章 > 3.2节`` 形式的 ``heading_path``，检索结果里能看出
  命中的是哪一节；
- **稳定 ID + 内容 hash**：``chunk_id`` 由文档与序号推出（可复现），``content_hash`` 用于
  增量更新时对齐新旧序列（架构 §6.1）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.core.page_markers import PAGE_MARKER_RE
from app.storage.base import ChunkRecord

__all__ = [
    "CHUNK_OVERLAP_MAX",
    "CHUNK_SIZE_MAX",
    "CHUNK_SIZE_MIN",
    "ChunkingConfig",
    "chunk_markdown",
    "config_from_record",
    "content_hash_of",
]


#: 页标记的读取侧。格式定义在 ``app/core/page_markers.py``（写侧：解析器与切分器），
#: 两处共用一份，不可能分家。
_PAGE_MARKER = PAGE_MARKER_RE

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
"""ATX 标题最多 6 级：7 个 ``#`` 在 Markdown 里就不是标题，而是普通段落。"""
DEFAULT_CHUNK_SIZE = 512
DEFAULT_OVERLAP = 64

#: 块长的可用区间。**上下限都是"产品判断"而不是技术限制**：
#: - 128 以下：一个块装不满两句完整的话，检索命中后给模型的上下文没有意义；
#: - 2048 以上：嵌入模型普遍有 token 上限（按中文约 1 字 1.5 token 估，2048 字 ≈ 3072 token），
#:   再大就有被上游静默截断的风险——那会变成"向量只对前一半文字负责"的隐蔽错误。
CHUNK_SIZE_MIN = 128
CHUNK_SIZE_MAX = 2048

#: 重叠的上限比例。重叠是为了让跨块的句子不被切断，但重叠=块长会让切分原地打转
#: （每块都是上一块的尾巴），所以最多到一半。
CHUNK_OVERLAP_RATIO_MAX = 0.5
CHUNK_OVERLAP_MAX = int(CHUNK_SIZE_MAX * CHUNK_OVERLAP_RATIO_MAX)


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """切分参数。默认值来自架构 §5.1 的"固定长度"模式。"""

    size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_OVERLAP

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("块长必须为正整数")
        if self.overlap < 0:
            raise ValueError("重叠长度不能为负")
        if self.overlap >= self.size:
            raise ValueError("重叠长度必须小于块长，否则切分会原地打转")


def config_from_record(record) -> ChunkingConfig:  # type: ignore[no-untyped-def]
    """从知识库记录里取切分参数，并**兜底钳位**。

    为什么要钳而不是直接信库里的值：早期版本的建库接口允许
    ``chunk_size=512 & chunk_overlap=4000`` 这种组合入库，``ChunkingConfig``
    会直接抛 ValueError——那会让**一份历史配置把后续所有文档的摄入打挂**，
    而且报错发生在切分阶段，看起来像文件坏了。
    钳位只影响越界的历史值，正常值原样通过。
    """
    size = int(getattr(record, "chunk_size", DEFAULT_CHUNK_SIZE) or DEFAULT_CHUNK_SIZE)
    size = min(max(size, CHUNK_SIZE_MIN), CHUNK_SIZE_MAX)
    # 字段缺失时退回默认重叠；存的是 0（显式"不重叠"）或 None（历史空值）都按 0 处理
    overlap = int(getattr(record, "chunk_overlap", DEFAULT_OVERLAP) or 0)
    overlap = min(max(overlap, 0), max(0, int(size * CHUNK_OVERLAP_RATIO_MAX)))
    return ChunkingConfig(size=size, overlap=overlap)


def content_hash_of(text: str) -> str:
    """内容 hash：增量更新时用它判断某个位置的 chunk 是否变化（架构 §6.1）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def chunk_markdown(
    markdown: str,
    *,
    document_id: str,
    knowledge_base_id: str,
    config: ChunkingConfig | None = None,
    part_id: str | None = None,
) -> list[ChunkRecord]:
    """把 Markdown 切成 chunk 序列。

    先按标题与空行切成"块"，再把块贪心装进不超过 ``size`` 的桶；单块超长时按
    ``size`` 硬切并保留 ``overlap`` 字符重叠。块的标题路径取所属标题栈。

    ``size`` 是**硬上限**，重叠也算在里面：新块开头若加上重叠尾巴就放不下当前块，
    宁可放弃这一段重叠也不让任何 chunk 超限（超限会破坏下游 embedding 的批量假设）。
    """
    settings = config or ChunkingConfig()
    blocks = _split_blocks(markdown)
    chunks: list[ChunkRecord] = []

    buffer = ""
    buffer_heading: str | None = None
    buffer_page: int | None = None
    last_text = ""

    def flush() -> str:
        """把当前缓冲落成一个 chunk，返回其文本（供下一块取重叠尾巴）。"""
        nonlocal buffer, buffer_heading, buffer_page
        text = buffer.strip()
        if text:
            ordinal = len(chunks)
            chunks.append(
                ChunkRecord(
                    chunk_id=_chunk_id(document_id, part_id, ordinal),
                    document_id=document_id,
                    knowledge_base_id=knowledge_base_id,
                    part_id=part_id,
                    ordinal=ordinal,
                    text=text,
                    content_hash=content_hash_of(text),
                    heading_path=buffer_heading,
                    page=buffer_page,
                )
            )
        buffer = ""
        buffer_heading = None
        buffer_page = None
        return text

    for text, heading, page in blocks:
        for piece in _split_oversized(text, settings):
            if buffer and len(buffer) + 1 + len(piece) > settings.size:
                last_text = flush()

            if not buffer:
                tail = _tail(last_text, settings.overlap)
                fits_with_tail = bool(tail) and len(tail) + 1 + len(piece) <= settings.size
                buffer = f"{tail}\n{piece}" if fits_with_tail else piece
                buffer_heading = heading
                buffer_page = page
            else:
                buffer = f"{buffer}\n{piece}"

    flush()
    return chunks


def _split_blocks(markdown: str) -> list[tuple[str, str | None, int | None]]:
    """按标题与空行切成块，并跟踪标题栈与**当前页码**。

    页码来自大文件切分器插入的页标记（``splitting.PAGE_MARKER_TEMPLATE``）。
    一份 1500 页的文档被切成 8 段分别解析，它们的产物是拼起来的，
    没有标记的话「这条命中在第几页」这个问题就永远答不出来——
    而引文页码是"答案可核查"的前提（架构 §5）。

    **页号是"当前页"，会一直沿用到下一个标记**，而不是只用一次。
    这一点踩过两次：

    1. 第一版遇到空行就把页码清掉——而页标记与正文之间必然隔着空行
       （`merge_parts` 就是这么拼的），于是标记后面那段正文拿到 None；
    2. 第二版改成"待用页码用掉即清"，只修了第一段：**同一页的第二段又成了 None**。
       实测一份 2 页的 PDF 拿到的是 `[1, null, null, null, 2, null, null, null]`——
       八块里六块没有页码，引用照样写不出"第几页"。页码是页的属性，
       不是"紧跟标记那一段"的属性。

    所以现在是：标记设置当前页，之后所有段落都带上它，直到下一个标记。
    空行、标题都不影响（它们只是分块信号，不是分页信号）。

    **标记本身不进 chunk 正文**：它是给这里读的元数据，留在正文里会污染检索文本，
    也会让嵌入向量被一串 HTML 注释干扰。
    """
    blocks: list[tuple[str, str | None, int | None]] = []
    stack: list[tuple[int, str]] = []
    pending: list[str] = []
    current_page: int | None = None

    def heading_path() -> str | None:
        return " > ".join(title for _, title in stack) if stack else None

    def flush_pending() -> None:
        text = "\n".join(pending).strip()
        pending.clear()
        if text:
            blocks.append((text, heading_path(), current_page))

    for line in markdown.splitlines():
        stripped = line.strip()
        marker = _PAGE_MARKER.match(stripped)
        if marker:
            # 页标记是**块边界**（前面积攒的内容属于上一页）兼**页码来源**：
            # 先把前面的落成块，再更新"当前页"，之后的内容直到下一个标记都属于这一页
            flush_pending()
            current_page = int(marker.group(1))
            continue
        match = _HEADING.match(stripped)
        if match:
            flush_pending()
            level = len(match.group(1))
            title = match.group(2).strip()
            stack[:] = [(lvl, name) for lvl, name in stack if lvl < level]
            stack.append((level, title))
            continue
        if not stripped:
            flush_pending()
            continue
        pending.append(line)

    flush_pending()
    return blocks


def _split_oversized(text: str, config: ChunkingConfig) -> list[str]:
    """单块超过块长时硬切，保留重叠；否则原样返回。"""
    if len(text) <= config.size:
        return [text]

    pieces: list[str] = []
    step = config.size - config.overlap
    for start in range(0, len(text), step):
        piece = text[start : start + config.size]
        if not piece.strip():
            continue
        pieces.append(piece)
        if start + config.size >= len(text):
            break
    return pieces


def _tail(text: str, length: int) -> str:
    return text[-length:] if length > 0 else ""


def _chunk_id(document_id: str, part_id: str | None, ordinal: int) -> str:
    """稳定 ID：同一文档同一序号的 chunk 在重跑后 ID 不变，便于对齐与覆盖写。"""
    scope = f"{document_id}/{part_id}" if part_id else document_id
    return f"{scope}#{ordinal:05d}"
