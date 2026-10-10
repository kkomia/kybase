"""按页取原文：页范围切片（文字）与按页出图（图片）。

用户要的是"给我第 3 到第 5 页的原文"，两半拼起来才算答完：
**这些页的解析文字**（检索真正依据的东西）与**这些页的原文件图**（扫描件与版式的样子）。

两半的实现路子完全不同，这是本模块的主要事实：

- 文字**已经有了**。解析产物在对象存储里是一整段 Markdown，逐页写着
  ``<!-- page:N -->`` 标记（格式见 ``core/page_markers.py``），按标记切段就行——
  不新增表、不重跑解析。面向用户的 ``/preview`` 之所以看不到标记，是因为
  它在**输出时**剥掉（``services/documents.py``），落盘的那份里标记一直都在。
- 图片**压根没有**。解析器拿到过图片字节，但页码没有可靠地保留下来
  （PaddleOCR 逐页产出却不带页码、MinerU 给的是 bbox），所以"第 N 页的图"只能
  **从原始 PDF 现场栅格化**。原件本身就是图片时，第 1 页就是它本身。
  栅格化结果按 :func:`page_image_key` 缓存：翻页是高频动作，同一页被翻第二次
  不该再画一遍。

**没有页码信息时如实说"没有"，不报错**：纯文本类文档（.md / .txt）本来就只有一页，
却也不带标记；一个 500 会把"界面该说明的事"变成"像是后端坏了"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.downloads import media_type_of, suffix_of
from app.core.exceptions import InvalidRequestError, NotFoundError, UnsupportedContentError
from app.core.page_markers import PAGE_MARKER_RE, strip_page_markers
from app.core.services import Services
from app.core.signing import DEFAULT_TTL_SECONDS, sign_resource
from app.services.documents import content_kind
from app.storage.base import StoreBundle

__all__ = [
    "MAX_PAGES_PER_REQUEST",
    "PAGE_IMAGE_DPI",
    "PAGE_IMAGE_PREFIX",
    "PageSlice",
    "check_page_range",
    "page_image",
    "page_image_key",
    "page_image_kind",
    "page_image_resource",
    "page_image_url",
    "page_text",
    "slice_pages",
]

logger = logging.getLogger(__name__)

MAX_PAGES_PER_REQUEST = 50
"""单次最多取多少页。

**必须有上限**：页图那一半会把每一页都渲染成 PNG（一页百毫秒级、几百 KB），
而"取整份 500 页文档的图"是一个很容易被误触的请求（前端一次拉 50 页就是一屏的
量级了）。超限当场拒，而不是让一次点击把解析机与对象存储按住几十秒。
"""

PAGE_IMAGE_DPI = 150
"""页图的渲染密度。

**150 是清晰度与体积的取舍点**：A4 在这个密度下约 1240×1754，PNG 通常 200–600KB，
屏幕上看正文与小字都够清楚；再往上（300dpi）体积大约翻三倍，而屏幕上几乎看不出
差别（打印才看得出来）；往下（96dpi）扫描件的小字会糊成一团。
换这个值会让旧缓存自然失效——key 里有它（见 :func:`page_image_key`）。
"""

PAGE_IMAGE_PREFIX = "images"
"""页图在对象存储里的前缀。与 ``originals/``（原件）、``markdown/``（解析产物）分开。"""

_PNG_MEDIA_TYPE = "image/png"

#: 能按页出图的原件种类。**只认 PDF 与位图**：其余格式（Office、压缩包、纯文本）
#: 没有"页"这个东西可切，硬给一张图只会是错的。
_IMAGE_SOURCE_KINDS = frozenset({"pdf", "image"})

#: 出图时**排除**的后缀。SVG 是文本格式、能带 ``<script>``，内联渲染等于在
#: 我们自己的 origin 下执行上传者的脚本（同 ``api/v1/documents.py`` 的
#: ``INLINE_BLOCKED_SUFFIXES`` 那条安全边界）。它也不是"一页"，退出去正好。
_PAGE_IMAGE_BLOCKED_SUFFIXES = (".svg", ".svgz")


@dataclass(frozen=True, slots=True)
class PageSlice:
    """一页的解析文字。``text`` 为空串表示这一页没有正文（标记之间是空的）。"""

    page: int
    text: str


# --------------------------------------------------------------------- 取文字


def slice_pages(
    markdown: str, *, from_page: int, to_page: int
) -> tuple[list[PageSlice], bool]:
    """把整份解析产物按页标记切成 ``[from_page, to_page]`` 的各页文字。

    返回 ``(各页文字, 这份产物有没有页码信息)``。**没有页标记时返回空列表 + False**，
    调用方据此告诉用户"这份文档没有页码信息"，而不是回一个空结果让人以为文档是空的。

    切法就是逐行扫标记（标记是独占一行的 HTML 注释）：遇到 ``<!-- page:N -->``
    就翻页，之后的行归这一页。三个刻意的取舍：

    - **同一页号出现多次就按出现顺序拼起来**（``dict`` 累加）。产物里出现重复
      页号说明上游插标记时出了问题，但"把两段都给他"总好过丢掉其中一段。
    - **页号乱序（… 2, 1 …）不去纠正**：那同样是上游的毛病，纠正只会掩盖它。
      本函数只忠实回答"标着第 N 页的那些段落是什么"。
    - **第一个标记之前的文本只在它属于第 1 页时才收**。标记是插在"第一个定位到的块"
      上的，它前面的文本（标题、前言）正常属于第 1 页；而万一第一个标记不是第 1 页
      （前面几页没能定位到），那段文本的页码就是未知的——宁可少一段，也不把它
      安到某一页头上（``core/page_markers.py`` 那条"宁缺勿错"是同一个口径）。

    请求范围里**没有标记的页也出一条空文字**：页图那一半照样有图，
    "这一页没有正文"与"这一页不存在"是两件事。
    """
    sections: dict[int, list[str]] = {}
    head: list[str] = []
    current: int | None = None
    for line in markdown.splitlines():
        match = PAGE_MARKER_RE.match(line.strip())
        if match is not None:
            current = int(match.group(1))
            sections.setdefault(current, []).append(line)
            continue
        if current is None:
            head.append(line)
        else:
            sections[current].append(line)

    if not sections:
        return [], False

    if 1 in sections and any(item.strip() for item in head):
        # 第一个标记之前的内容归第 1 页（标题、前言），见上面第三条取舍
        sections[1] = head + sections[1]

    pages: list[PageSlice] = []
    for page in range(from_page, to_page + 1):
        # 标记行也在片段里，交给 strip_page_markers 整行剥掉（顺带把连续空行压一压，
        # 与阅读视角给的是同一套清洗，两处看起来才一致）
        text = strip_page_markers("\n".join(sections.get(page, ()))).strip()
        pages.append(PageSlice(page=page, text=text))
    return pages, True


def page_text(
    services: Services, document_id: str, from_page: int, to_page: int
) -> tuple[list[PageSlice], bool]:
    """取某份文档在 ``[from_page, to_page]`` 里的各页文字。

    没有解析产物（还没解析完 / 已失败）与产物里没有页标记，都回 ``([], False)``：
    对调用方来说这是同一件事——**现在没有页码信息可用**，而那说法对两者都成立。
    """
    check_page_range(from_page, to_page)
    services.documents.get(document_id)  # 文档不存在 → 404，与范围判定同一个顺序
    stores = _stores(services)
    parsed = stores.meta.get_parse_result(document_id)
    if parsed is None:
        return [], False
    markdown = stores.objects.read(parsed.markdown_path).decode("utf-8", errors="replace")
    return slice_pages(markdown, from_page=from_page, to_page=to_page)


# --------------------------------------------------------------------- 取图片


def page_image_kind(document_name: str) -> str | None:
    """这份原件能不能按页出图，能的话是哪种（``"pdf"`` / ``"image"``）；不能则 ``None``。

    按**文件名**判种类（``content_kind`` 是同一套判定，见那里的说明：上传时声明的
    mime 是客户端给的，不可靠）。协议层据此决定要不要为这些页签发图片链接——
    对一份 .txt 签一堆必然 409 的链接，界面只会平白多出一片加载失败的图。
    """
    if suffix_of(document_name) in _PAGE_IMAGE_BLOCKED_SUFFIXES:
        return None
    kind = content_kind(document_name, has_markdown=False)
    return kind if kind in _IMAGE_SOURCE_KINDS else None


def page_image(
    services: Services, document_id: str, page: int
) -> tuple[bytes, str, str]:
    """取第 ``page`` 页的原图，返回 ``(字节, media_type, 文件名)``。

    - 原件是 **PDF**：用 pymupdf 现场栅格化这一页（结果进缓存）。
    - 原件是 **图片**：第 1 页就是原件本身，直接给它的字节。
    - 其余种类：``UnsupportedContentError``（409，"现在拿不到"——不是 404，
      文件在，只是这份格式没有页图这回事）。
    """
    if page < 1:
        raise InvalidRequestError(f"页码从 1 开始（收到 {page}）")
    record = services.documents.get(document_id)
    kind = page_image_kind(record.name)
    if kind is None:
        raise UnsupportedContentError(
            f"这份文档的原件不能按页出图（只支持 PDF 与图片）：{record.name}"
        )

    stores = _stores(services)
    if kind == "image":
        if page != 1:
            raise NotFoundError(f"{record.name} 只有第 1 页")
        # media_type 走**后缀**那一支（第二个参数刻意传 None）：声明类型是上传方可控的
        # 输入，一个"声明成 text/html 的 .png"会让我们在自己的 origin 下把 HTML 渲染出来。
        # 后缀兜底认不出来时它就是 octet-stream——那只会退化成下载，不会执行。
        return (
            _read_original(stores, document_id),
            media_type_of(record.name, None),
            record.name,
        )

    return _render_pdf_page(stores, document_id, record.name, page)


def page_image_key(document_id: str, page: int) -> str:
    """页图在对象存储里的 key：``images/{文档}/page-{页}@{dpi}.png``。

    **dpi 写进 key**：改渲染密度就等于换一批 key，旧图自然失效，
    不需要"记得去清一遍缓存"这种动作。

    这里**不带内容 hash**（头像那套是带的）：页图是解析产物的伴生物，
    与原件同一个生命周期（回收站会连原件一起清）。代价是"原件被就地换掉"这种
    今天不存在的动作会让缓存短暂偏旧——真要做那件事时得回来补 hash。
    """
    return f"{PAGE_IMAGE_PREFIX}/{document_id}/page-{page}@{PAGE_IMAGE_DPI}.png"


# --------------------------------------------------------------------- 签名链接


def page_image_resource(document_id: str, page: int) -> str:
    """页图的签名资源标识：``document:<id>:page:<页>:image``。

    **文档 id 与页号都必须绑进签名**——这是它存在的全部意义：只绑文档的话，
    一条"第 1 页"的链接改个页号就能换到任意一页，而"哪一页"正是要控制的东西。
    被签内容的拼接交给 ``core/signing.py`` 的长度前缀写法（不在这里自己拼 HMAC：
    直接字符串相加有歧义，``doc1 + page:23`` 与 ``doc12 + page:3`` 会撞成同一个）。
    """
    return f"document:{document_id}:page:{page}:image"


def page_image_url(
    document_id: str,
    page: int,
    *,
    secret: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[str, int]:
    """签一条页图链接，返回 ``(相对 URL, 到期时间戳)``。

    相对路径而不是绝对地址：对外域名只有部署时才知道（同
    ``DocumentService.download_url``）。有效期取默认的那一档——它是"点开就看"的链接，
    不该在聊天记录或日志里长期有效。
    """
    signature, expires_at = sign_resource(
        page_image_resource(document_id, page), secret, ttl_seconds=ttl_seconds
    )
    return (
        f"/api/v1/documents/{document_id}/pages/{page}/image"
        f"?expires={expires_at}&signature={signature}",
        expires_at,
    )


# --------------------------------------------------------------------- 内部


def check_page_range(from_page: int, to_page: int) -> None:
    """范围校验（三条规则都只看请求的形状，不看具体文档）。

    **业务判断住服务层**：协议层只声明参数，不在那里写 if。三条都回中文 422
    （``InvalidRequestError``）：调用方要做的动作是"把页号改对再来"。
    """
    if from_page < 1:
        raise InvalidRequestError(f"起始页从 1 开始（收到 {from_page}）")
    if to_page < from_page:
        raise InvalidRequestError(f"结束页不能小于起始页（{from_page} 到 {to_page}）")
    span = to_page - from_page + 1
    if span > MAX_PAGES_PER_REQUEST:
        raise InvalidRequestError(
            f"一次最多取 {MAX_PAGES_PER_REQUEST} 页（这次要了 {span} 页）"
        )


def _stores(services: Services) -> StoreBundle:
    """从组合根取存储。

    ``Services`` 没有公开的 ``stores`` 字段（bundle 由各服务各自持有），这里沿
    "每个服务都把 bundle 存在 ``_stores``"这条全仓一致的约定取一次。
    **不用 ``core.storage.get_stores()``**：那是另一个进程级单例，与本次请求真正在用的
    那份不是同一个对象（测试里 reset 之后就更不是），两处各拿一份就是
    "读到的和写的不是同一个地方"那类最难查的分叉。
    """
    return services.documents._stores


def _read_original(stores: StoreBundle, document_id: str) -> bytes:
    """原件字节。路径在设置表里（``document.<id>.original_path``），与摄入那边同源。"""
    path = stores.meta.get_setting(f"document.{document_id}.original_path")
    if not path or not stores.objects.exists(path):
        raise NotFoundError("原文不在对象存储里（可能已被回收站清理）")
    return stores.objects.read(path)


def _render_pdf_page(
    stores: StoreBundle, document_id: str, name: str, page: int
) -> tuple[bytes, str, str]:
    """PDF 的第 ``page`` 页 → PNG。先看缓存，未命中才渲染并写回。"""
    key = page_image_key(document_id, page)
    try:
        if stores.objects.exists(key):
            return stores.objects.read(key), _PNG_MEDIA_TYPE, _page_image_filename(name, page)
    except Exception:
        # 读缓存失败**不等于**取不到图：渲染才是权威来源，继续往下走
        logger.warning("页图缓存读取失败，改为重新渲染：%s", key, exc_info=True)

    png = _rasterize(_read_original(stores, document_id), page, name)
    try:
        stores.objects.write(key, png)
    except Exception:
        # 写缓存失败只影响"下次快一点"，不该让这次请求失败
        logger.warning("页图缓存写入失败：%s", key, exc_info=True)
    return png, _PNG_MEDIA_TYPE, _page_image_filename(name, page)


def _rasterize(pdf: bytes, page: int, name: str) -> bytes:
    """把 PDF 的第 ``page`` 页（1 起）画成 PNG 字节。"""
    # **惰性 import**：pymupdf 在 `parsers` extra 里（Dockerfile 用 --all-extras 构建，
    # 而开发机上可能只装了基础依赖）。放在模块级的话，这台机器上连"按页取文字"
    # 都会一起 import 失败——两半能力不该共命运。用 `import pymupdf` 而不是
    # `import fitz`：后者在新版本里会打一条改名告警。
    try:
        import pymupdf
    except ImportError as exc:
        raise UnsupportedContentError(
            "这台服务器没有装 PDF 渲染库（pymupdf）：补上 parsers 依赖后即可按页出图"
        ) from exc

    try:
        document = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:
        # 打不开就说打不开：能落到这里的原件是"后缀叫 .pdf 但内容不是 PDF"那一类
        raise UnsupportedContentError(f"这份 PDF 打不开，无法按页出图：{exc}") from exc
    try:
        if page > document.page_count:
            raise NotFoundError(f"{name} 一共 {document.page_count} 页（要的是第 {page} 页）")
        pixmap = document.load_page(page - 1).get_pixmap(dpi=PAGE_IMAGE_DPI)
        return pixmap.tobytes("png")
    finally:
        # 一次性打开的文档要关掉：pymupdf 的对象不参与 Python 的 GC 生命周期，
        # 漏掉 close 会让每次请求留一份解析树在内存里（翻几十页就看得出来）
        document.close()


def _page_image_filename(name: str, page: int) -> str:
    """页图的下载名：``报告.pdf`` 的第 3 页 → ``报告-第3页.png``。

    取名与原件的名字（而不是 ``page-3.png``）：用户存下来之后要靠它认出这是哪一份文档。
    """
    suffix = suffix_of(name)
    stem = name[: -len(suffix)] if suffix else name
    return f"{stem}-第{page}页.png"
