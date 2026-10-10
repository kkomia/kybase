"""按页取原文的端点：页范围列表（文字 + 页图链接）与页图本体。

**两条刻意分开，因为它们的授权方式不可能相同**：

- ``GET /documents/{id}/pages`` 要**鉴权**（``require_read`` + 知识库范围闸）：它回的
  是这一页的正文内容，与读文档本身是同一件事。
- ``GET /documents/{id}/pages/{页}/image`` **只认签名，刻意不挂 ``require_read``**：
  这个 URL 要能直接塞进 ``<img src>``，而图片请求带不了 Authorization 头。
  凭据是 URL 里的签名，签名绑死了"哪个文档的哪一页、什么时候过期"——比一个长期令牌
  更窄（换一页、换一份文档都过不了，见 ``services/document_pages.py::page_image_resource``）。
  与 ``/documents/{id}/content``、``/avatars/{user_id}`` 是同一条理由、同一个形状。

业务判断一律在服务层：页范围的三条规则、切页、栅格化、缓存、签名资源串都在
``services/document_pages.py``，这里只做"取参数 → 转发 → 拼响应"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.auth import require_read, signing_secret, signing_secret_or_raise
from app.api.v1.documents import _guard_document
from app.api.v1.schemas import DocumentPageOut, DocumentPagesOut
from app.core.config import Settings, get_settings
from app.core.downloads import content_disposition
from app.core.exceptions import UnauthorizedError
from app.core.services import Services, get_services
from app.core.signing import SigningError, verify_resource
from app.services.api_key import Caller
from app.services.document_pages import (
    page_image,
    page_image_kind,
    page_image_resource,
    page_image_url,
    page_text,
)

router = APIRouter(tags=["documents"])


@router.get(
    "/documents/{document_id}/pages",
    response_model=DocumentPagesOut,
    summary="按页范围取原文（解析文字 + 页图签名链接）",
)
def document_pages(
    document_id: str,
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
    from_page: int = Query(default=1, description="起始页（含）。从 1 开始"),
    to_page: int = Query(description="结束页（含）。与起始页之间最多 50 页"),
    images: bool = Query(default=False, description="是否一并签发这些页的原图链接"),
) -> DocumentPagesOut:
    """一次给出一段页范围的**文字**，以及（可选）这些页**原图的签名链接**。

    ``images=false`` 时不签发任何链接（三个 image 字段都是 null）：页图要现场渲染，
    不需要图的人不该为它买单。**超出文档总页数的页也不签**——签出来的链接必然 404。

    **没有页码信息时不报错**：``page_text_available=false``，``pages`` 里每页的
    ``text`` 是空串。界面据此如实显示"这份文档没有页码信息"，而不是显示一个错误。

    **产物里有页标记也不等于页码是对的**：``pages_reliable=false`` 说的是"产物里的
    页码与文档自称的总页数对不上"（存量数据里有一批切分文档就是这样，见
    ``services/document_pages.py`` 模块头）。这时 ``pages`` 里的文字照样给出去——
    它是这份文档的真实内容，只是**不能当成"第 N 页"引用**；界面据此提示"页码不可信、
    可重新解析"，而 ``pages_reliable`` 是 ``null``（没有页码信息）时不该这么说。
    """
    record = _guard_document(services, caller, document_id)
    total_pages = record.page_count

    reading = page_text(services, document_id, from_page, to_page)
    available_text = {item.page: item.text for item in reading.pages}
    # 能出图的种类才签链接：对一份 .txt 签一堆必然 409 的链接，界面只会多一片加载失败的图
    kind = page_image_kind(record.name) if images else None
    # 没有签名密钥时**发不了链接但不报错**（同 preview 那条）：文字仍然给出去，
    # 界面退化成"只能看文字"。整个端点为一个可选的一半报红字是不划算的。
    secret = signing_secret(get_settings(), services) if kind else None

    pages: list[DocumentPageOut] = []
    for page in range(from_page, to_page + 1):
        url: str | None = None
        expires_at: int | None = None
        # 超出总页数的页**不签**：签出来的链接必然 404，界面上就是一个加载失败的图。
        # 那种情况下 null 才是对的（界面配 truncated 那条一起收住）。总页数未知时不拦。
        within = total_pages is None or page <= total_pages
        if secret is not None and within:
            url, expires_at = page_image_url(document_id, page, secret=secret)
        pages.append(
            DocumentPageOut(
                page=page,
                text=available_text.get(page, ""),
                image_url=url,
                image_expires_at=expires_at,
                image_kind=kind if url else None,
            )
        )

    return DocumentPagesOut(
        document_id=document_id,
        from_page=from_page,
        to_page=to_page,
        total_pages=total_pages,
        page_text_available=reading.text_available,
        # 三值直接透传（``null`` = 没有页码信息，``false`` = 有页码但对不上）。
        # 服务层判完就完了，协议层不再叠一层自己的判据
        pages_reliable=reading.reliable,
        pages_located=reading.pages_located,
        # 只有"要的页超出了文档总页数"才算截断：那些页注定是空的，界面据此收住
        truncated=total_pages is not None and to_page > total_pages,
        pages=pages,
    )


@router.get(
    "/documents/{document_id}/pages/{page}/image",
    summary="按签名取某一页的原图（PNG）",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}, "description": "该页的图片"}},
)
def document_page_image(
    document_id: str,
    page: int,
    expires: int = Query(description="签发时给出的到期时间戳"),
    signature: str = Query(description="签发时给出的签名"),
    services: Services = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> Response:
    """某一页的图。**刻意不挂 ``require_read``**：这个 URL 要能塞进 ``<img src>``，
    而图片请求带不了 Authorization 头。授权凭据是 URL 里的签名，签名绑死了
    "哪个文档的哪一页"（见 ``page_image_resource``）——这比"任何人都能按 id 取图"
    窄得多，而后者就是"不签名"的代价。

    用 ``Response`` 而不是 ``FileResponse``：内容是渲染进内存的字节，没有磁盘路径
    可给（同 ``/content`` 那条：``FileResponse`` 只接受路径）。
    """
    secret = signing_secret_or_raise(settings, services)
    try:
        verify_resource(page_image_resource(document_id, page), signature, expires, secret)
    except SigningError as exc:
        # 401：链接无效或过期，正确的动作是回到列表端点重新要一条
        raise UnauthorizedError(f"页图链接无效：{exc}") from exc

    data, media_type, filename = page_image(services, document_id, page)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            # inline：这张图是**在页面里直接显示**的；attachment 会让浏览器下载它，
            # 那就不是"看原图"了
            "Content-Disposition": content_disposition(filename, disposition="inline"),
            # 内联渲染时禁止浏览器再嗅探一遍类型：原件类型与声明类型都是上传方那侧的东西
            "X-Content-Type-Options": "nosniff",
            # 页图的 key 绑的是"文档 + 页号"（不是内容 hash），所以只给一个短缓存：
            # 万一原件被换掉，浏览器最多多用一分钟旧的
            "Cache-Control": "private, max-age=600",
            "Content-Length": str(len(data)),
        },
    )
