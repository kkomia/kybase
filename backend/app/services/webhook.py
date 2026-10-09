"""Webhook 事件推送（M4 / T4.6）。

**要解决的问题**：文档摄入是异步的，可能跑几分钟。调用方（外部 Agent、自建前端、
脚本）目前只能轮询 `GET /documents/{id}` 看状态——架构 §3.5 的原话是
"轮询 `get_document_status` 之外的推送通道"。轮询在长任务上是纯粹的浪费：
一份 200 页的 PDF 要轮询几百次，而它真正只发生了一次状态变化。

**交付语义：至少一次（at-least-once），不是恰好一次。**
做不到恰好一次——"对方收到了但我没收到确认"这个窗口无法消除。
所以每个事件带一个 `id`，接收端按 `id` 去重；重复投递是**契约的一部分**，
不是 bug。这一点必须说在明处，否则接收端会以为重复是故障。

**签名**（`X-Kylab-Signature`）：
``t=<unix 秒>,v1=<hex(hmac_sha256(secret, "<t>.<body>"))>``

时间戳参与签名是必须的：只签 body 的话，攻击者可以原样重放一个几小时前的
合法请求，接收端无法分辨。接收端应当拒绝时间戳偏差过大的请求
（建议 5 分钟），并用自己的 secret 重算比对。这里用的是与 Stripe 相同的形式，
因为它是被验证过很多年的形状。

**重试**：指数退避，与任务队列同一套节奏（1s / 2s / 4s …）。
**不重试 4xx**：400/401/404 说明请求本身有问题或对方不认，重试只会重复伤害；
只有 5xx 与网络错误值得重试。这一条区分很要紧——把 401 重试五遍
既浪费又会在对方日志里刷出一片错误。

**投递失败不阻断摄入**：webhook 是**旁路**。它挂了不能让用户的文档卡住，
所以投递失败只记日志（并体现在返回值里），不抛给调用方。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.lazy_httpx import httpx  # 惰性代理：不让 click/pygments/rich 进导入闭包（P4-3）
from app.storage.base import StoreBundle, WebhookRecord

__all__ = [
    "DOCUMENT_DELETED",
    "DOCUMENT_FAILED",
    "DOCUMENT_INDEXED",
    "EVENTS",
    "DeliveryResult",
    "WebhookService",
    "sign_payload",
]

logger = logging.getLogger(__name__)

#: 支持的事件名。**做成常量而不是到处散字符串**：接收端要靠这份清单配置订阅，
#: 而拼错一个事件名只会导致"订阅了但永远收不到"，不会报错。
DOCUMENT_INDEXED = "document.indexed"
DOCUMENT_FAILED = "document.failed"
DOCUMENT_DELETED = "document.deleted"

EVENTS: tuple[str, ...] = (DOCUMENT_INDEXED, DOCUMENT_FAILED, DOCUMENT_DELETED)

#: 每次投递的最大尝试次数（首次 + 重试）。
MAX_ATTEMPTS = 4

#: 退避基数：第 n 次重试等 ``BACKOFF_BASE * 2**(n-1)`` 秒。
BACKOFF_BASE_SECONDS = 1.0

#: 单次请求超时。接收端慢不该把摄入线程拖住。
REQUEST_TIMEOUT_SECONDS = 10.0

#: 签名头名。
SIGNATURE_HEADER = "X-Kylab-Signature"
EVENT_HEADER = "X-Kylab-Event"
DELIVERY_HEADER = "X-Kylab-Delivery"


def sign_payload(secret: str, body: bytes, *, timestamp: int | None = None) -> tuple[str, int]:
    """算出签名头的值。返回 ``(header_value, timestamp)``。

    形式与 Stripe 一致：``t=<秒>,v1=<hex>``。``v1`` 允许将来换算法时并存，
    接收端可以按前缀挑自己认识的那个。
    """
    moment = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode("utf-8"), f"{moment}.".encode() + body, hashlib.sha256)
    return f"t={moment},v1={mac.hexdigest()}", moment


def verify_payload(
    secret: str, body: bytes, header: str, *, tolerance_seconds: int = 300
) -> bool:
    """接收端侧的实现（供测试与文档使用）。

    **它是给接收端抄的范例**：本项目自己的测试用它验签，因而也顺便证明了
    "拿这个 secret 按这个算法算能对上"这件事是真的。
    """
    parsed: dict[str, str] = {}
    for piece in header.split(","):
        if "=" in piece:
            key, value = piece.split("=", 1)
            parsed[key.strip()] = value.strip()
    if "t" not in parsed or "v1" not in parsed:
        return False

    # **非法的时间戳是"验签失败"，不是异常**：这个函数挂在接收端的请求处理路径上，
    # 一个畸形的头应当得到 401 而不是 500。测试第一版就撞上了这里——
    # `t=,v1=` 会让 int("") 抛 ValueError，而调用方完全没有准备接它。
    try:
        moment = int(parsed["t"])
    except ValueError:
        return False

    if tolerance_seconds >= 0 and abs(int(time.time()) - moment) > tolerance_seconds:
        # 时间戳超出容忍窗口：可能是重放，也可能是两边的钟差太多
        return False

    expected = hmac.new(secret.encode("utf-8"), f"{moment}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), parsed["v1"])


@dataclass(slots=True)
class DeliveryResult:
    """一次事件的全部投递结果（每个订阅一条）。"""

    event: str
    event_id: str
    results: list[tuple[str, bool, str]] = field(default_factory=list)
    """``(webhook_id, 成功与否, 说明)``。"""

    @property
    def delivered(self) -> int:
        return sum(1 for _, ok, _ in self.results if ok)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.results if not ok)


class WebhookService:
    """订阅管理与事件投递。"""

    def __init__(
        self,
        stores: StoreBundle,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._stores = stores
        self._client = client
        # 注入 sleep 是为了让重试测试不必真的等 1+2+4 秒——
        # 真等的测试没人愿意跑，而没人跑的测试等于没有
        self._sleep = sleep

    # ------------------------------------------------------------------ 订阅

    def create(
        self,
        *,
        url: str,
        events: Sequence[str] = EVENTS,
        secret: str | None = None,
        enabled: bool = True,
        webhook_id: str | None = None,
    ) -> WebhookRecord:
        """新建订阅。

        ``events`` 里的未知事件名**直接拒绝**而不是静默存下：
        拼错一个事件名的表现是"订阅了但永远收不到"，那是最难查的一类问题。
        """
        unknown = [name for name in events if name not in EVENTS]
        if unknown:
            raise ValueError(f"不支持的事件：{'、'.join(unknown)}。可选：{'、'.join(EVENTS)}")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"webhook 地址必须是 http(s)：{url!r}")

        return self._stores.meta.create_webhook(
            WebhookRecord(
                id=webhook_id or f"wh_{uuid.uuid4().hex[:12]}",
                url=url,
                events=tuple(events),
                secret=secret,
                enabled=enabled,
            )
        )

    def list(self) -> list[WebhookRecord]:
        return self._stores.meta.list_webhooks()

    def get(self, webhook_id: str) -> WebhookRecord | None:
        return self._stores.meta.get_webhook(webhook_id)

    def set_enabled(self, webhook_id: str, enabled: bool) -> WebhookRecord:
        """启停订阅。**停用不是删除**：保留它让人随时能再打开，
        而删除会连带丢掉 id 与 secret，接收端那边就得重新配一遍。"""
        record = self._stores.meta.set_webhook_enabled(webhook_id, enabled)
        if record is None:
            raise KeyError(webhook_id)
        return record

    def delete(self, webhook_id: str) -> None:
        self._stores.meta.delete_webhook(webhook_id)

    # ------------------------------------------------------------------ 投递

    def emit(self, event: str, payload: dict) -> DeliveryResult:
        """把事件推给所有订阅了它的**启用中**的 webhook。

        ``payload`` 里不要放密钥。签名用的 secret 来自订阅本身，不来自载荷。
        """
        if event not in EVENTS:
            raise ValueError(f"未知事件 {event!r}，可选：{'、'.join(EVENTS)}")

        event_id = f"evt_{uuid.uuid4().hex}"
        body = json.dumps(
            {
                "id": event_id,
                "event": event,
                "created_at": _now_iso(),
                "data": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        outcome = DeliveryResult(event=event, event_id=event_id)
        for record in self._stores.meta.list_webhooks():
            if not record.enabled or event not in record.events:
                continue
            ok, detail = self._deliver(record, event, event_id, body)
            outcome.results.append((record.id, ok, detail))

        if not outcome.results:
            logger.debug("事件 %s 没有订阅者，已丢弃", event)
        return outcome

    def _deliver(
        self, record: WebhookRecord, event: str, event_id: str, body: bytes
    ) -> tuple[bool, str]:
        """投递一条，按需重试。**永远不抛**——投递失败不能阻断摄入。"""
        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: event,
            # 重复投递是契约的一部分，接收端按这个 id 去重
            DELIVERY_HEADER: event_id,
            "X-Kylab-Attempt": "1",
        }
        if record.secret:
            signature, _ = sign_payload(record.secret, body)
            headers[SIGNATURE_HEADER] = signature

        client = self._client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        owns_client = self._client is None
        last = ""
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                headers["X-Kylab-Attempt"] = str(attempt)
                try:
                    response = client.post(record.url, content=body, headers=headers)
                except httpx.HTTPError as exc:
                    last = f"{type(exc).__name__}: {exc}"
                else:
                    if 200 <= response.status_code < 300:
                        return True, f"HTTP {response.status_code}"
                    last = f"HTTP {response.status_code}"
                    # **4xx 不重试**：请求本身有问题或对方不认，
                    # 重试只会重复伤害，还会在对方日志里刷出一片错误
                    if 400 <= response.status_code < 500:
                        logger.warning("webhook %s 被拒（%s），不重试", record.id, last)
                        return False, last

                if attempt < MAX_ATTEMPTS:
                    self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        finally:
            if owns_client:
                client.close()

        logger.warning(
            "webhook %s 投递 %s 失败，已尝试 %d 次：%s", record.id, event, MAX_ATTEMPTS, last
        )
        return False, last


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
