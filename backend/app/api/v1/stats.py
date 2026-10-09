"""统计端点：驾驶舱的数据源。

一个接口给全量数字，而不是拆成七八个——驾驶舱要同时渲染五六块图，
分开请求既慢又容易"上半页是新的、下半页是旧的"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.auth import require_read
from app.api.v1.schemas import DashboardOut, UsageOut
from app.core.services import Services, get_services
from app.services.api_key import Caller
from app.services.stats import DEFAULT_WINDOW_DAYS

router = APIRouter(tags=["stats"])

MAX_WINDOW_DAYS = 365


@router.get("/stats/dashboard", response_model=DashboardOut, summary="驾驶舱统计")
def dashboard(
    window_days: int = Query(
        default=DEFAULT_WINDOW_DAYS,
        ge=7,
        le=MAX_WINDOW_DAYS,
        description="活跃度观察窗口（天）",
    ),
    services: Services = Depends(get_services),
    caller: Caller = Depends(require_read),
) -> DashboardOut:
    # 成员（v10）的驾驶舱只统计自己可见的库：库名、文档数都是私有数据
    kb_ids = services.api_keys.visible_kb_ids(caller) if caller.user is not None else None
    stats = services.stats.dashboard(window_days=window_days, kb_ids=kb_ids)
    return DashboardOut.model_validate(stats)


@router.get("/stats/usage", response_model=UsageOut, summary="用量（token 与调用量，含检索）")
def usage_summary(
    days: int = Query(default=30, ge=1, le=MAX_WINDOW_DAYS, description="观察窗口（天）"),
    services: Services = Depends(get_services),
    _: Caller = Depends(require_read),
) -> UsageOut:
    """最近 N 天的用量（对话 / 向量化 / 检索 / 重排）。

    **只给 token 与调用量，不给钱**：单价随供应商、版本、缓存命中、时段折扣
    不断变，内置一张价目表必然过期——而过期的价钱比不给更糟，
    用户会照着它做决定。

    ``unreported_calls`` 说清"有几次调用供应商没报用量"：不区分的话，
    统计页会把"没报"画成"没用"，那是在撒谎。
    """
    return UsageOut.model_validate(services.usage.summary(days=days))
