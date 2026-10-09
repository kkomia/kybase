"""幂等键（《架构设计 v0.2》§3.2：上传类接口要求幂等键，防客户端重试造成重复入库）。

要解决的具体场景：客户端 POST 上传之后网络断了，它不知道服务端收没收到，
于是重试一次。没有幂等键，同一份文件就进两次——第一次可能已经入库并在向量化，
第二次变成一份新文档。**这不是理论问题**：移动网络、代理超时、用户手抖刷新
都会造出这个局面。

三条判定，缺一不可：

| 情况 | 处置 |
|------|------|
| 键没出现过 | 占住它 → 执行业务 → 把响应挂上去 |
| 键出现过、请求内容**相同** | 直接回上次那份响应（重放，不重复干活） |
| 键出现过、请求内容**不同** | 409。**这条最容易被漏掉**—— |
|  | 复用同一个键发不同内容，多半是客户端把键写死了，静默放行会让它"以为成功" |
| 键出现过、但业务还没跑完 | 409（"处理中"）。不能回空，否则客户端以为失败又重试 |

**为什么响应体也存**：只记"这个键来过了"是不够的——重放时客户端仍然需要
``document.id`` 与 ``task_id`` 才能继续。只回一个"已处理"等于让它自己去猜。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.exceptions import ConflictError
from app.storage.base import IdempotencyRecord, StoreBundle

__all__ = ["Claim", "IdempotencyService", "fingerprint"]

logger = logging.getLogger(__name__)

#: 幂等键保留时长。客户端重试是分钟级的事，一天足够宽松；
#: 再长只是让表白白变大（见 purge_expired）。
RETENTION_HOURS = 24


def fingerprint(*parts: object) -> str:
    """把"这次请求是什么"折算成一个稳定摘要。

    用长度的前缀分隔各部分，而不是直接拼接：``("ab", "c")`` 与 ``("a", "bc")``
    直接拼都是 ``"abc"``，会让两个不同的请求被判成同一个（真踩过这类拼接歧义）。
    """
    hasher = hashlib.sha256()
    for part in parts:
        chunk = str(part).encode("utf-8")
        hasher.update(f"{len(chunk)}:".encode("ascii"))
        hasher.update(chunk)
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class Claim:
    """``begin`` 的结果。

    ``replay`` 有值 → 直接回它，不要执行业务；``None`` → 本次是新请求，执行业务，
    完成后必须调 ``complete`` 把响应挂上去。
    """

    replay: dict[str, Any] | None = None

    @property
    def is_replay(self) -> bool:
        return self.replay is not None


class IdempotencyService:
    """占键、判重放、挂响应。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    def begin(self, key: str, request_hash: str) -> Claim:
        """占住一个键并判定该不该重放。

        返回值而不是抛异常：调用方要写的分支是"重放就提前 return，否则继续干活"，
        用异常表达正常控制流会让每个调用点都得包一层 try。
        """
        try:
            self._stores.meta.create_idempotency_key(
                IdempotencyRecord(key=key, request_hash=request_hash)
            )
            return Claim()  # 占住了，本次是新请求
        except ConflictError:
            pass  # 键已存在，落到下面判定

        existing = self._stores.meta.get_idempotency_key(key)
        if existing is None:
            # 抢键失败但立刻又查不到：唯一可能是它刚被清理。重试一次比猜更安全
            raise ConflictError("幂等键状态异常，请重试") from None

        if existing.request_hash != request_hash:
            raise ConflictError(
                "同一个幂等键被用于不同的请求内容。"
                "如果你在重试，请复用与首次完全相同的内容；换内容请换一个键"
            )

        if existing.response is None:
            raise ConflictError("该请求正在处理中，请稍后重试（幂等键已占住，尚未完成）")

        return Claim(replay=dict(existing.response))

    def complete(self, key: str, response: dict[str, Any]) -> None:
        """把首次执行的结果挂到键上，供后续重放。

        写失败**不抛**：业务已经成功了，此时报错会让客户端以为失败并重试，
        而重试又会因为键已存在而回 409"处理中"——把一次成功变成一次困扰。
        记日志即可。
        """
        try:
            self._stores.meta.save_idempotent_response(key, response)
        except Exception:
            logger.warning("保存幂等响应失败（业务已完成，重放将回 409）", exc_info=True)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """清掉过期的键，返回删除条数。

        客户端的重试窗口是分钟级（网络超时、用户手抖刷新），保留一天远远够用。
        没有这一步，``idempotency_keys`` 会随每次上传无限增长——这类表不会报错，
        只会在几个月后变成"某个晚上数据库突然大了一截"。
        """
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=RETENTION_HOURS)
        return self._stores.meta.purge_expired_idempotency_keys(before=cutoff)

    def release(self, key: str) -> None:
        """业务执行失败时放掉键，让客户端能真正重试。

        不放的话：键留着、``response`` 为空，重试永远拿到"正在处理中"——
        而实际上什么都没在处理。这比直接报错更糟，因为它看起来像"再等等就好"。
        """
        try:
            self._stores.meta.release_idempotency_key(key)
        except Exception:
            logger.warning("释放幂等键失败：%s", key, exc_info=True)
