"""用量统计（调研报告 G7）。

成熟产品里的"运营/用量"面板回答三个问题：**花了多少 token、调用了几次、
贵不贵**。本项目原先只有内容统计（多少文档、多少块），完全没有成本视角——
用久了会不知道"这个月是不是烧太快了"。

**一处刻意的克制**：这里**不算钱**。单价随供应商、版本、缓存命中、
时段折扣不断变，内置一张价目表必然过期，而过期的价钱比不给更糟
（用户会照着它做决定）。所以只给 token 与调用量这两组**客观事实**，
把"多少钱"留给用户自己乘。
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.services.llm import LLMUsage
from app.storage.base import StoreBundle, UsageEventRecord

__all__ = ["USAGE_RETENTION_DAYS", "UsageService"]

logger = logging.getLogger(__name__)

#: 用量保留天数。它只用于看趋势，留太久没有价值；
#: 与幂等键同一套思路——由 worker 空闲维护顺手清掉。
USAGE_RETENTION_DAYS = 180

#: 用途的中文名（界面直接用，避免前端再维护一份会漂的映射）
KIND_LABELS: dict[str, str] = {
    "chat": "对话生成",
    "embedding": "向量化",
    "search": "检索",
    "rerank": "重排",
    "parse": "文档解析",
}


class UsageService:
    """记一次调用、按窗口聚合。"""

    def __init__(self, stores: StoreBundle) -> None:
        self._stores = stores

    # ------------------------------------------------------------------ 记

    def record(
        self,
        *,
        kind: str,
        provider: str = "",
        model_id: str = "",
        usage: LLMUsage | None = None,
        items: int = 0,
        duration_ms: int = 0,
    ) -> UsageEventRecord:
        """记一次调用。

        ``reported`` 由 ``usage`` 是否真的带回了数字决定，**不由调用方传**：
        这是"供应商报没报"这个客观事实，让每个调用点各判一次迟早会不一致。
        """
        # 三态由 usage 自己说，**不由调用方传**：这是"数字哪来的"这个客观事实，
        # 让每个调用点各判一次迟早会不一致
        if usage is None:
            source = "none"
        elif usage.estimated:
            source = "estimated"
        elif usage.is_reported:
            source = "reported"
        else:
            source = "none"

        record = UsageEventRecord(
            id=f"use_{uuid.uuid4().hex[:12]}",
            kind=kind,
            provider=provider,
            model_id=model_id,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            items=max(0, items),
            duration_ms=max(0, duration_ms),
            source=source,
        )
        try:
            return self._stores.meta.record_usage(record)
        except Exception:
            # **统计失败绝不能影响业务**：用户已经拿到回答/已经完成入库，
            # 因为写一行统计而报错是本末倒置。记日志即可。
            logger.exception("用量记录失败（不影响本次调用）：%s", kind)
            return record

    # ------------------------------------------------------------------ 汇总

    def summary(self, *, days: int = 30) -> dict[str, object]:
        """按本地日历日聚合最近 ``days`` 天。

        **按本地日分桶**，不是 UTC：用户在 UTC+8 看"今天用了多少"，
        按 UTC 分桶会让每天 08:00 之前的数据算到前一天去（这个坑在
        「今日入库」上踩过一次，见 ``services/stats.py`` 的 ``local_day``）。
        """
        window_start = datetime.now(UTC) - timedelta(days=days)
        events = self._stores.meta.list_usage(since=window_start)

        by_day: dict[str, dict[str, int]] = defaultdict(_empty_bucket)
        by_kind: dict[str, dict[str, int]] = defaultdict(_empty_bucket)
        by_model: dict[str, dict[str, int]] = defaultdict(_empty_bucket)

        for event in events:
            day = local_day(event.created_at)
            model_key = (
                f"{event.provider}/{event.model_id}" if event.provider else event.model_id
            )
            for bucket in (by_day[day], by_kind[event.kind]):
                _accumulate(bucket, event)
            # **没有 model_id 的事件不进"按模型"**（典型是检索）：它不是模型调用，
            # 而检索消耗的嵌入模型已由 embedding 事件单独计过一次，
            # 再算进来会让那张表出现一行"（未记录）"并让调用数虚高
            if model_key:
                _accumulate(by_model[model_key], event)

        estimated_tokens = sum(
            event.total_tokens for event in events if event.source == "estimated"
        )

        return {
            "days": days,
            "total": _totals(events),
            "estimated_tokens": estimated_tokens,
            "by_day": [
                {"day": day, **values} for day, values in sorted(by_day.items())
            ],
            "by_kind": [
                {"kind": kind, "label": KIND_LABELS.get(kind, kind), **values}
                for kind, values in sorted(by_kind.items(), key=lambda item: -item[1]["calls"])
            ],
            "by_model": [
                {"model": model or "（未记录）", **values}
                for model, values in sorted(by_model.items(), key=lambda item: -item[1]["calls"])
            ],
            # 区分三态：不区分的话统计页会把"没报"画成"没用"、
            # 把"估算"画成"实测"，那是在撒谎
            "reported_calls": sum(1 for event in events if event.source == "reported"),
            "estimated_calls": sum(1 for event in events if event.source == "estimated"),
            "unreported_calls": sum(1 for event in events if event.source == "none"),
        }

    # ------------------------------------------------------------------ 维护

    def purge_expired(self, *, days: int = USAGE_RETENTION_DAYS) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = self._stores.meta.purge_usage_before(cutoff)
        if removed:
            logger.info("清理了 %d 条过期用量记录", removed)
        return removed


def local_day(moment: datetime | None) -> str:
    """把时间点归到**本地日历日**（``YYYY-MM-DD``）。

    与 ``services/stats.py`` 同一口径：用户在本地时间看报表。
    """
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%Y-%m-%d")


def _empty_bucket() -> dict[str, int]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "items": 0,
        "unreported": 0,
        "estimated": 0,
    }


def _accumulate(bucket: dict[str, int], event: UsageEventRecord) -> None:
    bucket["calls"] += 1
    bucket["prompt_tokens"] += event.prompt_tokens
    bucket["completion_tokens"] += event.completion_tokens
    bucket["items"] += event.items
    if event.source == "none":
        bucket["unreported"] += 1
    elif event.source == "estimated":
        bucket["estimated"] += 1


def _totals(events: list[UsageEventRecord]) -> dict[str, int]:
    total = _empty_bucket()
    for event in events:
        _accumulate(total, event)
    return total
