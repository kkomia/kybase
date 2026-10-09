"""用量统计（G7）。

镜像同构：``app/services/usage.py`` → 本文件。

三条要紧的性质：
1. **``reported`` 必须区分"没报"与"用了 0"**——否则统计页会把"没报"画成"没用"，那是在撒谎；
2. **按本地日历日分桶**，与「今日入库」同一口径（UTC 分桶会让 UTC+8 的用户
   每天 08:00 之前的数据算到前一天）；
3. **统计失败绝不影响业务**——用户已经拿到回答，因为写一行统计而报错是本末倒置。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.llm import LLMUsage
from app.services.usage import KIND_LABELS, UsageService, local_day


@pytest.fixture
def usage(bundle) -> UsageService:  # type: ignore[no-untyped-def]
    return UsageService(bundle)


# --------------------------------------------------------------------- 记录


def test_record_reported_usage(usage: UsageService) -> None:
    event = usage.record(
        kind="chat",
        provider="https://api.deepseek.com",
        model_id="deepseek-chat",
        usage=LLMUsage(prompt_tokens=120, completion_tokens=80),
        items=1,
        duration_ms=1500,
    )

    assert event.total_tokens == 200
    assert event.reported is True
    assert event.duration_ms == 1500


def test_unreported_usage_is_flagged(usage: UsageService) -> None:
    """**"没报"与"用了 0"必须能区分开。**

    不区分的话统计页会把"供应商没回 usage"画成"没消耗"——那是撒谎。
    """
    event = usage.record(kind="embedding", provider="", model_id="bge-m3", usage=None)

    assert event.source == "none"
    assert event.reported is False
    assert event.prompt_tokens == 0


def test_estimated_usage_is_not_counted_as_reported(usage: UsageService) -> None:
    """**"估算"必须与"实测"分开。**

    向量化接口通常不返回 usage，我们只能按字符数估。把它标成实测，
    统计页就会显示一个假精度——用户拿它做成本判断会偏好几倍。
    假精度比没数字更糟。
    """
    event = usage.record(
        kind="embedding",
        model_id="bge-m3",
        usage=LLMUsage(prompt_tokens=120, estimated=True),
        items=3,
    )

    assert event.source == "estimated"
    assert event.reported is False, "估算不能算作实测"
    assert event.prompt_tokens == 120, "数字本身仍要留着——它是有参考价值的"


def test_summary_separates_the_three_sources(usage: UsageService) -> None:
    usage.record(kind="chat", usage=LLMUsage(100, 50))  # 实测
    usage.record(kind="embedding", usage=LLMUsage(30, estimated=True), items=2)  # 估算
    usage.record(kind="rerank", usage=None)  # 未上报

    summary = usage.summary()

    assert summary["reported_calls"] == 1
    assert summary["estimated_calls"] == 1
    assert summary["unreported_calls"] == 1
    assert summary["estimated_tokens"] == 30
    # token 总数仍含估算那块，但界面上有单独字段能把它剥出来
    assert summary["total"]["prompt_tokens"] == 130


def test_zero_usage_counts_as_unreported(usage: UsageService) -> None:
    """全 0 的 usage 视为没报：真用 0 token 是不可能的。"""
    event = usage.record(kind="chat", usage=LLMUsage(0, 0))
    assert event.reported is False


def test_items_are_clamped_to_non_negative(usage: UsageService) -> None:
    assert usage.record(kind="embedding", items=-5).items == 0


def test_record_never_raises_even_if_the_store_fails(bundle) -> None:  # type: ignore[no-untyped-def]
    """**统计失败绝不能影响业务**：用户已经拿到回答/文档已经入库，
    因为写一行统计而报错是本末倒置。"""

    class BrokenMeta:
        def record_usage(self, record):  # type: ignore[no-untyped-def]
            raise RuntimeError("磁盘满了")

    service = UsageService(type("S", (), {"meta": BrokenMeta()})())  # type: ignore[arg-type]

    event = service.record(kind="chat", usage=LLMUsage(10, 5))  # 不该抛

    assert event.total_tokens == 15


# --------------------------------------------------------------------- 汇总


def test_summary_totals(usage: UsageService) -> None:
    usage.record(kind="chat", usage=LLMUsage(100, 50), items=1)
    usage.record(kind="chat", usage=LLMUsage(200, 60), items=1)
    usage.record(kind="embedding", usage=LLMUsage(300, 0), items=4)

    summary = usage.summary(days=30)
    total = summary["total"]

    assert total["calls"] == 3
    assert total["prompt_tokens"] == 600
    assert total["completion_tokens"] == 110
    assert total["items"] == 6


def test_summary_groups_by_kind_with_labels(usage: UsageService) -> None:
    usage.record(kind="chat", usage=LLMUsage(10, 5))
    usage.record(kind="embedding", usage=LLMUsage(20, 0), items=2)

    by_kind = {item["kind"]: item for item in usage.summary()["by_kind"]}

    assert set(by_kind) == {"chat", "embedding"}
    assert by_kind["chat"]["label"] == KIND_LABELS["chat"]
    assert by_kind["embedding"]["items"] == 2


def test_summary_groups_by_model(usage: UsageService) -> None:
    usage.record(
        kind="chat",
        provider="https://a.example.com",
        model_id="model-a",
        usage=LLMUsage(10, 5),
    )
    usage.record(
        kind="chat",
        provider="https://b.example.com",
        model_id="model-b",
        usage=LLMUsage(20, 5),
    )

    by_model = {item["model"]: item for item in usage.summary()["by_model"]}

    assert "https://a.example.com/model-a" in by_model
    assert "https://b.example.com/model-b" in by_model


def test_summary_marks_unreported_calls(usage: UsageService) -> None:
    usage.record(kind="chat", usage=LLMUsage(10, 5))
    usage.record(kind="embedding", usage=None)

    summary = usage.summary()

    assert summary["unreported_calls"] == 1


def test_summary_of_empty_history_is_all_zeros(usage: UsageService) -> None:
    """一次都没用过时不能炸，界面要能正常渲染空态度。"""
    summary = usage.summary()

    assert summary["total"]["calls"] == 0
    assert summary["by_day"] == []
    assert summary["by_kind"] == []
    assert summary["unreported_calls"] == 0


def test_summary_respects_the_window(bundle) -> None:  # type: ignore[no-untyped-def]
    """窗口外的数据不能算进来，否则"最近 7 天"会显示全部历史。"""
    service = UsageService(bundle)
    old = datetime.now(UTC) - timedelta(days=40)
    bundle.meta.record_usage(
        __import__("app.storage.base", fromlist=["UsageEventRecord"]).UsageEventRecord(
            id="use_old", kind="chat", prompt_tokens=9999, created_at=old
        )
    )
    service.record(kind="chat", usage=LLMUsage(10, 5))

    assert service.summary(days=7)["total"]["prompt_tokens"] == 10
    assert service.summary(days=90)["total"]["prompt_tokens"] == 10009


def test_by_day_uses_local_calendar_days(usage: UsageService) -> None:
    """**按本地日分桶**，不是 UTC。

    按 UTC 分桶会让 UTC+8 的用户在每天 08:00 之前看"今天"，数据其实算在前一天
    ——这个坑在「今日入库」上已经踩过一次。
    """
    usage.record(kind="chat", usage=LLMUsage(10, 5))

    days = usage.summary()["by_day"]
    assert len(days) == 1
    assert days[0]["day"] == local_day(datetime.now(UTC))


def test_local_day_handles_naive_and_missing() -> None:
    assert local_day(None) == ""
    naive = datetime(2026, 9, 11, 3, 0, 0)
    assert local_day(naive).startswith("2026-09-1")


# --------------------------------------------------------------------- 清理


def test_purge_removes_only_old_events(bundle) -> None:  # type: ignore[no-untyped-def]
    service = UsageService(bundle)
    from app.storage.base import UsageEventRecord

    bundle.meta.record_usage(
        UsageEventRecord(
            id="use_old",
            kind="chat",
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    service.record(kind="chat", usage=LLMUsage(10, 5))

    removed = service.purge_expired(days=180)

    assert removed == 1
    assert service.summary(days=400)["total"]["calls"] == 1


def test_model_less_events_do_not_pollute_by_model(usage: UsageService) -> None:
    """检索这类"没有 model_id"的事件只进按类目与按日，不进按模型。

    否则"按模型"里会出现一行"（未记录）"，而它的嵌入消耗其实已由 embedding
    事件计过一次，两处都算会让调用数虚高。
    """
    usage.record(kind="search", items=3, duration_ms=12)
    usage.record(kind="chat", provider="https://api.example.com", model_id="m1", items=1)

    summary = usage.summary(days=1)

    assert {item["kind"] for item in summary["by_kind"]} == {"search", "chat"}
    assert [item["model"] for item in summary["by_model"]] == ["https://api.example.com/m1"]
