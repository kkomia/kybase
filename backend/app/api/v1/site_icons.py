"""站点图标端点（D11-②）：给"网页 logo"一个**我们自己的源**。

前端只请求这一条，浏览器不再直连第三方站点（隐私与稳定，理由见
``services/site_icons.py`` 的模块说明）。端点本身很小——判断与缓存都在服务层。

**为什么是"取 blob"而不是让 `<img src>` 直接指过来**：这一组端点要凭据（v0.11 起
`/api/v1` 一律要），而 `<img>` 带不了 ``Authorization`` 头；换签名又要在客户端按图标
逐个签发。前端用 ``fetch`` + ``Blob`` + ``ObjectURL``：既有凭据，又不会因为 404
在控制台留下一条"加载图片失败"（取不到就走站点字牌 / 通用地球）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.auth import require_read
from app.core.caller import Caller
from app.core.exceptions import NotFoundError
from app.core.services import Services, get_services
from app.services.site_icons import SiteIconService

router = APIRouter(prefix="/site-icons", tags=["site-icons"])


@router.get(
    "",
    response_class=Response,
    summary="站点图标（本机缓存；任意合法域名，取不到回 404 由前端退字牌 / 地球）",
)
def site_icon(
    services: Annotated[Services, Depends(get_services)],
    caller: Annotated[Caller, Depends(require_read)],
    domain: str = Query(description="站点域名（任意合法域名，SSRF 由 check_public_url 兜底）"),
) -> Response:
    """按域名发一枚图标（png / jpeg / gif / ico / webp，按魔数嗅探后才发）。

    - **任何形态合法的域名都代为抓取**：收什么域名不是这条接口该管的事，白名单已撤
      （2026-10-01 用户："这个为啥抓不到真实的图标呢"）。**是不是公网地址**由服务层
      每一跳的 `web.check_public_url` 兜底——它才是"这个域名该不该抓"的唯一判断；
    - **畸形域名 → 422**（写错了 / 在探），**抓不到 → 404**（正常的降级路径，
      前端退回站点字牌或通用地球，不报错、不留空位）；
    - 缓存命中时不发任何外部请求；响应带一天浏览器缓存。
    """
    icon = SiteIconService(services.runtime.data_dir).icon(domain)
    if icon is None:
        raise NotFoundError(f"这个站点暂时取不到图标：{domain}")
    return Response(
        content=icon.content,
        media_type=icon.media_type,
        headers={
            # 一天：图标是低频变化的东西，而我们自己那边还有 30 天的磁盘缓存
            "Cache-Control": "public, max-age=86400",
            # 位图仍然按嗅探结果发，明确禁止浏览器再猜一次类型
            "X-Content-Type-Options": "nosniff",
        },
    )
