"""头像图片的读取端点（v0.29）。

**单独一组，而且刻意不挂鉴权依赖**：这个 URL 要能直接塞进 ``<img src>``，
而图片请求带不了 Authorization 头。它的授权凭据是 URL 里的签名，签名绑定了
"哪个用户的哪一份头像、什么时候过期"——比一个长期令牌更窄：换一个 user id
去请求同一份签名会被拒（见 ``services/avatars.py::resource``）。

与文档下载那条路同一个形状（``/conversations/{id}/files/content``），
差别只在**内容寻址**：头像的 key 里带着内容 hash，所以这里可以放心地
让浏览器长期缓存——换头像就是换 key，不会拿到旧图。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.auth import signing_secret_or_raise
from app.core.config import Settings, get_settings
from app.core.exceptions import UnauthorizedError
from app.core.services import KbServices, get_kb_services
from app.core.signing import SigningError, verify_resource
from app.services.avatars import resource

router = APIRouter(prefix="/avatars", tags=["avatars"])


@router.get("/{user_id}", summary="取一张头像（签名链接）")
def get_avatar(
    user_id: str,
    services: Annotated[KbServices, Depends(get_kb_services)],
    settings: Annotated[Settings, Depends(get_settings)],
    expires: Annotated[int, Query(description="到期时间戳；由签发方给出")] = 0,
    signature: Annotated[str, Query(description="签名；见 core/signing.py")] = "",
) -> Response:
    secret = signing_secret_or_raise(settings, services)
    record = services.users.get(user_id)
    try:
        verify_resource(resource(user_id, record.avatar_key), signature, expires, secret)
    except SigningError as exc:
        raise UnauthorizedError(f"头像链接无效：{exc}") from exc

    blob, media_type = services.avatars.read(user_id)
    return Response(
        content=blob,
        media_type=media_type,
        headers={
            # **可以放心长期缓存**：key 是内容寻址的（换头像 = 换 URL），
            # 而链接本身有签名与到期时间——缓存的是这张图，不是这份权限。
            "Cache-Control": "private, max-age=86400",
            # 图片类型是按魔数认的，不让浏览器再嗅探一遍
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(blob)),
        },
    )
