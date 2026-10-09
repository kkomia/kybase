"""文件探测（M2 T2.4）。

职责：给出"文字型 / 扫描件 / 混合型"的判定与**文本层覆盖率**，供路由决策器使用
（架构 §4.1：逐文件探测、逐文件路由）。

关键点：**先按格式分支，再谈文本覆盖率**。PDF 的头是 ASCII（``%PDF-1.7``）、
DOCX 是 zip（前几个字节恰好可打印），只靠"抽样能不能解成 UTF-8"会把它们判成文字型——
真踩过这个坑：PDF 被交给纯文本直通，切出来的块是原始的 PDF 字节流。
所以二进制格式一律走**格式自己的文本层探测**，只有真·文本文件才用编码启发式。

探测本身不依赖网络：离线也能把路由链路跑通。
"""

from __future__ import annotations

from app.parsers.base import ProbeKind, ProbeResult

TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".text", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml"}
)
MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown"})

PDF_EXTENSIONS = frozenset({".pdf"})
OFFICE_EXTENSIONS = frozenset(
    {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".ods", ".odp"}
)
IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jp2"}
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"})
"""视频容器后缀。

今天它唯一的去处是**媒体向量**（`parsers/media_direct.py`）：本地没有解码器，
云端两家 OCR 也不接视频；而服务端的 ffmpeg 能解（接入文档 §4）。所以探测给出的结论
是"需要外部能力"，具体的路在解析路由那里按配置决定。"""

#: 二进制容器格式：无论抽样看起来多像文本，都不许当纯文本处理
BINARY_EXTENSIONS = (
    PDF_EXTENSIONS
    | OFFICE_EXTENSIONS
    | IMAGE_EXTENSIONS
    | VIDEO_EXTENSIONS
    | frozenset(
        {".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".bin", ".db", ".sqlite"}
    )
)

_SAMPLE_BYTES = 64 * 1024
"""抽样窗口：只看开头一段就够判断编码与可读性，避免大文件全量解码。"""

TEXT_COVERAGE_THRESHOLD = 0.9
"""文本文件的可打印字符占比阈值。"""

#: PDF 抽样页数：架构 §4.1 就是"抽样若干页"，取 5 页足够区分三类文档
PDF_SAMPLE_PAGES = 5
#: 文本层覆盖率判定（架构 §4.1）：高于 0.8 文字型、低于 0.2 扫描件、中间是混合型
PDF_TEXT_COVERAGE_HIGH = 0.8
PDF_TEXT_COVERAGE_LOW = 0.2
#: 单页最少多少个字符才算"这页有文本层"（避免把零星页眉当成文字型）
PDF_MIN_CHARS_PER_PAGE = 20


def _printable_ratio(sample: bytes) -> float:
    """可打印字符占比。

    以 UTF-8 能否解码为第一判据：能解码且不含 NUL 就基本是文本；
    否则退化为"ASCII 可打印 + 常见多字节高位字节"的比例估算。
    """
    if not sample:
        return 0.0
    if b"\x00" in sample:
        return 0.0
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return 1.0

    printable = sum(1 for byte in sample if 32 <= byte < 127 or byte >= 0x80)
    return printable / len(sample)


def _probe_pdf(content: bytes, *, suffix: str) -> ProbeResult:
    """PDF：抽样若干页，统计"有文本层的页"占比（架构 §4.1 的覆盖率口径）。

    没有文本层不等于空文件——扫描件的正确结论是 ``SCANNED``，要送去 OCR，
    而不是当成解析失败。pypdf 缺失时退化为"按后缀判为待 OCR"，不阻断链路。
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - parsers extra 未安装时
        return ProbeResult(
            kind=ProbeKind.SCANNED,
            text_coverage=0.0,
            detail={"reason": "未安装 pypdf，无法探测文本层，按扫描件处理", "suffix": suffix},
        )

    import io

    try:
        reader = PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
        if page_count == 0:
            return ProbeResult(
                kind=ProbeKind.SCANNED,
                text_coverage=0.0,
                page_count=0,
                detail={"reason": "PDF 没有页面", "suffix": suffix},
            )

        sampled = list(range(min(PDF_SAMPLE_PAGES, page_count)))
        with_text = 0
        for index in sampled:
            try:
                text = reader.pages[index].extract_text() or ""
            except Exception:
                text = ""
            if len(text.strip()) >= PDF_MIN_CHARS_PER_PAGE:
                with_text += 1
        coverage = with_text / len(sampled)
    except Exception as exc:
        return ProbeResult(
            kind=ProbeKind.SCANNED,
            text_coverage=0.0,
            detail={"reason": f"PDF 探测失败（{exc}），按扫描件处理", "suffix": suffix},
        )

    if coverage >= PDF_TEXT_COVERAGE_HIGH:
        kind = ProbeKind.TEXT
    elif coverage <= PDF_TEXT_COVERAGE_LOW:
        kind = ProbeKind.SCANNED
    else:
        kind = ProbeKind.MIXED

    return ProbeResult(
        kind=kind,
        text_coverage=coverage,
        page_count=page_count,
        detail={
            "reason": f"抽样 {len(sampled)} 页，{with_text} 页有文本层",
            "suffix": suffix,
            "sampled_pages": len(sampled),
            "pages_with_text": with_text,
        },
    )


def probe(
    content: bytes, *, filename: str = "", mime_type: str | None = None
) -> ProbeResult:
    """探测文件形态。

    返回的 ``detail`` 会落进任务记录，用于在控制台解释路由决策。
    """
    suffix = suffix_of(filename)
    mime = (mime_type or "").lower()

    if suffix in PDF_EXTENSIONS or mime == "application/pdf":
        return _probe_pdf(content, suffix=suffix or ".pdf")

    if suffix in IMAGE_EXTENSIONS or mime.startswith("image/"):
        # 图片天生没有文本层，直接是扫描件语义
        return ProbeResult(
            kind=ProbeKind.SCANNED,
            text_coverage=0.0,
            detail={"reason": "图片文件，需走 OCR", "suffix": suffix},
        )

    if suffix in VIDEO_EXTENSIONS or mime.startswith("video/"):
        # 视频没有"文本层"这回事（探测能说的只有"它不是文本"）。**不要**在这里下
        # "不支持"的结论：配了多模态嵌入之后它有一条真正的路（媒体直通解析器），
        # 走不走那条路由解析路由按配置决定（见 `services/parser_router.py`）。
        return ProbeResult(
            kind=ProbeKind.SCANNED,
            text_coverage=0.0,
            detail={"reason": "视频文件，需走媒体向量（服务端 ffmpeg 解码）", "suffix": suffix},
        )

    if suffix in OFFICE_EXTENSIONS:
        # Office 是 zip 容器，抽样字节没有意义；交给解析器去读文档结构
        return ProbeResult(
            kind=ProbeKind.MIXED,
            text_coverage=0.0,
            detail={"reason": "Office 容器格式，交由解析器读取文档结构", "suffix": suffix},
        )

    sample = content[:_SAMPLE_BYTES]
    ratio = _printable_ratio(sample)

    is_known_text = suffix in TEXT_EXTENSIONS or mime.startswith("text/")
    # 二进制容器即使抽样可打印也不许当文本（PDF 的头就是 ASCII）
    looks_like_text = suffix not in BINARY_EXTENSIONS and ratio >= TEXT_COVERAGE_THRESHOLD
    is_text = is_known_text or looks_like_text

    if not is_text:
        detail = {"reason": "二进制内容且扩展名未声明为文本", "suffix": suffix}
        return ProbeResult(kind=ProbeKind.SCANNED, text_coverage=ratio, detail=detail)

    detail = {
        "reason": "扩展名或 MIME 声明为文本" if is_known_text else "抽样内容可解码为 UTF-8",
        "suffix": suffix,
        "is_markdown": suffix in MARKDOWN_EXTENSIONS,
    }
    return ProbeResult(kind=ProbeKind.TEXT, text_coverage=ratio, detail=detail)


def suffix_of(filename: str) -> str:
    lowered = filename.lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > 0 else ""
