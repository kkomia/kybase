"""Webhook 端点（M4 / T4.6）。

**为什么订阅管理是控制台级**（``require_admin``）：一个 webhook 订阅意味着
"这个服务会主动往某个地址发文档内容"。拿到读写 API Key 的集成方不该能
凭空把知识库内容转发到它自己的服务器——那是数据外泄，不是普通写操作。

**密钥只回显掩码**：与设置页那套凭据同一条纪律（架构 §13）。
订阅的 secret 用来给载荷签名，接收端拿它验签；它一旦明文出库，
任何能读到列表的人都能伪造签名。所以列表只回 ``sk-…abcd`` 这种形状，
**明文只在上面的响应里出现一次**，让用户自己存起来。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.auth import require_admin, require_read
from app.api.v1.schemas import (
    WebhookCreateIn,
    WebhookEventListOut,
    WebhookListOut,
    WebhookOut,
    WebhookUpdateIn,
)
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.services import Services, get_services
from app.services.api_key import Caller
from app.services.webhook import EVENTS, MAX_ATTEMPTS, SIGNATURE_HEADER

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _mask(secret: str | None) -> str | None:
    """只回显头尾，长度不足时整体打码——短串既没信息量又会被拼出来。"""
    if not secret:
        return None
    if len(secret) <= 8:
        return "…"
    return f"{secret[:4]}…{secret[-4:]}"


def _out(record, *, reveal_secret: str | None = None) -> WebhookOut:  # type: ignore[no-untyped-def]
    return WebhookOut(
        id=record.id,
        url=record.url,
        events=list(record.events),
        enabled=record.enabled,
        has_secret=bool(record.secret),
        secret_masked=_mask(record.secret),
        # 明文只在**创建**那一次返回，之后永远拿不回来。
        # 这不是小气：能反复读到签名密钥就等于签名没有意义
        secret=reveal_secret,
    )


def _require(record):  # type: ignore[no-untyped-def]
    if record is None:
        raise NotFoundError("订阅不存在")
    return record


@router.get("/events", response_model=WebhookEventListOut, summary="支持的事件清单")
def list_events(
    _: Annotated[Caller, Depends(require_read)],
) -> WebhookEventListOut:
    """把事件名做成接口而不是写在文档里。

    接收端要靠这份清单配置订阅，而拼错一个事件名的表现是
    "订阅成功但永远收不到"——那种失败最难查，所以让它**可以被程序读到**。
    """
    return WebhookEventListOut(
        events=list(EVENTS),
        signature_header=SIGNATURE_HEADER,
        max_attempts=MAX_ATTEMPTS,
        delivery_semantics="at-least-once",
    )


@router.get("", response_model=WebhookListOut, summary="订阅列表（密钥掩码）")
def list_webhooks(
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> WebhookListOut:
    return WebhookListOut(items=[_out(item) for item in services.webhooks.list()])


@router.post(
    "",
    response_model=WebhookOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建订阅（密钥明文只在这里返回一次）",
)
def create_webhook(
    payload: WebhookCreateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> WebhookOut:
    try:
        record = services.webhooks.create(
            url=payload.url,
            events=tuple(payload.events) if payload.events else EVENTS,
            secret=payload.secret,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        # 拼错事件名是调用方错误，不是 500：``invalid_request`` 是这套接口
        # 对"你给的东西不对"的统一答复（《API 接口规范》§1.2）
        raise InvalidRequestError(str(exc)) from exc
    return _out(record, reveal_secret=record.secret)


@router.delete(
    "/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除订阅",
)
def delete_webhook(
    webhook_id: str,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> None:
    _require(services.webhooks.get(webhook_id))
    services.webhooks.delete(webhook_id)


@router.patch("/{webhook_id}", response_model=WebhookOut, summary="启用 / 停用订阅")
def update_webhook(
    webhook_id: str,
    payload: WebhookUpdateIn,
    services: Annotated[Services, Depends(get_services)],
    _: Annotated[Caller, Depends(require_admin)],
) -> WebhookOut:
    """只支持改 ``enabled``。

    **不做"改地址"**：改地址等于把一个已经验证过的投递目标换掉，
    而这中间没有任何确认步骤——想换地址就删了重建，那一步是有意识的。
    """
    _require(services.webhooks.get(webhook_id))
    record = services.webhooks.set_enabled(webhook_id, payload.enabled)
    return _out(record)
