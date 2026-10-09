"""cron 表达式（v0.33）。

镜像同构：``app/services/cron.py`` → 本文件。

判定"下一次什么时候跑"这件事有一类**不好发现**的错法：把边界算错一分钟
（同一分钟被选中两次、或者刚跑完又跑一次）。所以这里的用例大多钉的是边界：
"同一分钟不重复触发"、"日与周同时限定时按 OR"、"闰日这种稀疏表达式也能算出来"。

另外两条：**字段越界与语法错要报得出来**（写 `0 25 * * *` 的人需要知道是"小时"错了），
以及**永远不会触发的表达式要报错而不是死循环**（`0 0 30 2 *`：2 月没有 30 号）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.exceptions import InvalidRequestError
from app.services.cron import describe_cron, next_after, validate_cron

#: 2026-09-20 是周日。
SUNDAY = datetime(2026, 9, 20, 10, 30)


# ------------------------------------------------------------------ 解析


def test_normalizes_spacing() -> None:
    assert validate_cron("  0   9  *  *  * ") == "0 9 * * *"


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("0 9 * *", "5 个字段"),
        ("0 9 * * * *", "5 个字段"),
        ("60 9 * * *", "分钟 字段超出范围"),
        ("0 25 * * *", "小时 字段超出范围"),
        ("0 9 0 * *", "日 字段超出范围"),
        ("0 9 * 13 *", "月 字段超出范围"),
        ("0 9 * * 8", "周 字段超出范围"),
        ("0 9 * * MON", "只接受数字"),
        ("*/0 9 * * *", "步长不合法"),
        ("1-0 9 * * *", "区间反了"),
        ("", "缺少 cron 表达式"),
    ],
)
def test_bad_expressions_explain_what_is_wrong(expr: str, message: str) -> None:
    """报错要说清"哪个字段、错在哪"——模型/用户据此才改得动。"""
    with pytest.raises(InvalidRequestError, match=message):
        validate_cron(expr)


# ------------------------------------------------------------------ 下一次


def test_daily_at_nine() -> None:
    assert next_after("0 9 * * *", SUNDAY) == datetime(2026, 9, 21, 9, 0)


def test_the_same_minute_does_not_fire_twice() -> None:
    """**边界**：正好停在 09:00 时，下一次是明天——否则它会连着触发两次。"""
    assert next_after("0 9 * * *", datetime(2026, 9, 20, 9, 0)) == datetime(2026, 9, 21, 9, 0)


def test_every_fifteen_minutes() -> None:
    assert next_after("*/15 * * * *", SUNDAY) == datetime(2026, 9, 20, 10, 45)


def test_weekly_on_monday() -> None:
    assert next_after("30 8 * * 1", SUNDAY) == datetime(2026, 9, 21, 8, 30)


def test_sunday_is_both_zero_and_seven() -> None:
    """cron 的传统写法：0 与 7 都是周日。不折的话 7 会变成"第 7 天"（不存在）。"""
    assert next_after("0 4 * * 0", SUNDAY) == next_after("0 4 * * 7", SUNDAY)


def test_day_and_weekday_together_mean_or() -> None:
    """``0 9 1 * 1`` = 每月 1 号**或**每周一。按 AND 读的话它几乎永远不触发。"""
    # 2026-09-21 是周一（也是"1 号"之后最近的一个匹配）
    assert next_after("0 9 1 * 1", SUNDAY) == datetime(2026, 9, 21, 9, 0)


def test_monthly_on_the_first() -> None:
    assert next_after("0 9 1 * *", SUNDAY) == datetime(2026, 10, 1, 9, 0)


def test_weekday_range_covers_the_coming_monday() -> None:
    assert next_after("0 9 * * 1-5", SUNDAY) == datetime(2026, 9, 21, 9, 0)


def test_sparse_expression_still_terminates() -> None:
    """闰日：逐分钟扫要几百万步，按天跳才快。这条同时钉住"算得出来"。"""
    assert next_after("0 0 29 2 *", SUNDAY) == datetime(2028, 2, 29, 0, 0)


def test_impossible_expression_reports_instead_of_looping() -> None:
    with pytest.raises(InvalidRequestError, match="不会触发"):
        next_after("0 0 30 2 *", SUNDAY)


def test_aware_datetimes_keep_their_timezone() -> None:
    """带时区的输入按它自己的墙上时间匹配，返回的也是同一个时区。"""
    from datetime import UTC, timedelta, timezone

    beijing = timezone(timedelta(hours=8))
    moment = datetime(2026, 9, 20, 10, 30, tzinfo=beijing)
    got = next_after("0 9 * * *", moment)
    assert got.tzinfo is not None
    assert got.astimezone(UTC) == datetime(2026, 9, 21, 9, 0, tzinfo=beijing).astimezone(UTC)


# ------------------------------------------------------------------ 人话


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        ("0 9 * * *", "每天 09:00"),
        ("30 8 * * 1", "每周一 08:30"),
        ("0 9 1 * *", "每月 1 号 09:00"),
        ("*/15 * * * *", "每 15 分钟"),
        ("* * * * *", "每分钟"),
        ("30 * * * *", "每小时第 30 分"),
    ],
)
def test_describe_says_it_in_plain_words(expr: str, text: str) -> None:
    assert describe_cron(expr) == text


def test_describe_falls_back_to_the_expression() -> None:
    """说不清的就原样回表达式，**不编一句半对的话**（界面上一句错的描述更误导）。"""
    assert describe_cron("*/7 3 * * *") == "*/7 3 * * *"
