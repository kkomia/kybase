"""cron 表达式：解析与"下一次什么时候跑"（v0.33）。

**为什么只有 5 个字段**（分 时 日 月 周），没有秒、没有 ``@daily`` 这类别名、
也不支持 ``MON``/``JAN`` 这样的名字：定时任务这个面的用法是"每天几点"、
"每周一几点"，而字段越少，**写错时的报错越说得清**。多出来的形态
（``@every 5m``、6 字段带秒）都要各自一套解析与显示规则，收益抵不上。

**按服务器本地时间解释**（不是 UTC）：用户说的"每天 9 点"是他钟表上的 9 点。
部署在容器里时容器默认是 UTC，所以 `deploy/docker-compose.yml` 里显式设了 ``TZ``
（默认 ``Asia/Shanghai``）——不然"早报 9 点"会在北京时间 17 点跑，
而这种现象极难被归因到"容器的时区"上。

**支持哪些写法**：``*``、``5``、``5,10``、``1-5``、``*/15``、``1-30/5``。
``日`` 与 ``周`` 同时被限定时按 **OR** 匹配（与 cron 的通行做法一致：
"每月 1 号**或**每周一"），只有一个被限定时按 AND——这条规则不写下来的话，
"每月 1 号和每周一"会被读成两个都要满足，而那样几乎永远不触发。

**本地时间与夏令时**：中国没有夏令时，这里不做特殊处理；有夏令时的时区下
"缺失的那个小时"会被跳过（``astimezone`` 的既有行为），这是可接受的取舍——
为它写一套转换规则会让这个模块比它服务的功能还复杂。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.exceptions import InvalidRequestError

__all__ = [
    "FIELDS",
    "MAX_SEARCH_DAYS",
    "describe_cron",
    "next_after",
    "validate_cron",
]

#: 五个字段的名字与取值范围。周是 ``0-7``（0 与 7 都是周日）——cron 的传统写法，
#: 而"7 也是周日"这件事不写上会让人以为 ``7`` 是错的。
FIELDS: tuple[tuple[str, int, int], ...] = (
    ("分钟", 0, 59),
    ("小时", 0, 23),
    ("日", 1, 31),
    ("月", 1, 12),
    ("周", 0, 7),
)

#: 往后找多少次日出（天）。到了一个范围还没找到就判"永远不会跑"：
#: ``0 0 30 2 *``（2 月 30 日）这类表达式必须报错，而不是让调用方死循环。
MAX_SEARCH_DAYS = 366 * 8

_WEEK_NAMES = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")


def validate_cron(expr: str) -> str:
    """校验表达式，返回**规范化后的文本**（去多余空格）。

    报错要能指导下一步：说清是第几个字段、字段名是什么、取值范围多少。
    """
    text = " ".join((expr or "").split())
    if not text:
        raise InvalidRequestError("缺少 cron 表达式")
    parts = text.split(" ")
    if len(parts) != len(FIELDS):
        raise InvalidRequestError(
            f"cron 要 {len(FIELDS)} 个字段（分 时 日 月 周），收到 {len(parts)} 个：{text}。"
            "例：`0 9 * * *` = 每天 9:00"
        )
    for part, (name, low, high) in zip(parts, FIELDS, strict=True):
        _parse_field(part, name=name, low=low, high=high)
    return text


def next_after(expr: str, after: datetime) -> datetime:
    """``after`` **之后**（不含它自己）的下一次执行时刻。

    算法是"按天跳、在匹配的那天里找第一个匹配的时刻"，而不是"逐分钟加"：
    逐分钟在 ``0 0 29 2 *``（四年一次的闰日）上要空转几百万次，
    而按天跳最多 366×8 次。

    ``after`` 带不带时区都行：带 tz 的按**该时区的墙上时间**匹配
    （``astimezone`` 之后取 hour/minute），返回的也是同一个 tz。
    """
    text = validate_cron(expr)
    minutes, hours, days, months, weeks = (
        _parse_field(part, name=name, low=low, high=high)
        for part, (name, low, high) in zip(text.split(" "), FIELDS, strict=True)
    )
    dom_restricted = days is not None
    dow_restricted = weeks is not None

    # 从下一分钟开始找：`after` 那一分钟不该再被选中一次（否则重复触发）
    moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(MAX_SEARCH_DAYS):
        if _date_matches(moment, days, months, weeks, dom_restricted, dow_restricted):
            found = _first_time_on(moment, hours=hours, minutes=minutes)
            if found is not None:
                return found
        moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
    raise InvalidRequestError(
        f"这个表达式在 {MAX_SEARCH_DAYS // 366} 年内不会触发（{text}）——检查一下日期与月份的搭配"
    )


def describe_cron(expr: str) -> str:
    """把表达式翻成一句人话（界面用）。**只覆盖能一眼说清的那些**：
    说不清的（``*/7 3 * * *``）回表达式本身，而不是编一句半对的话——
    界面上写错的一句话，比一个原始表达式更容易让人误会。
    """
    text = validate_cron(expr)
    minute, hour, day, month, week = text.split(" ")
    if (hour, day, month, week) == ("*", "*", "*", "*"):
        if minute == "*":
            return "每分钟"
        if minute.startswith("*/") and minute[2:].isdigit():
            return f"每 {int(minute[2:])} 分钟"
    at = _time_text(hour, minute)
    if at is None:
        return text
    if (day, month, week) == ("*", "*", "*"):
        # 小时也是 ``*`` 时那句话本身已经说清了频率（"每小时第 30 分"），
        # 再加"每天"就错了——``30 * * * *`` 是每小时一次，不是每天一次
        return at if hour == "*" else f"每天 {at}"
    if day == "*" and month == "*" and week.isdigit():
        week_text = _WEEK_NAMES[int(week) % 7]
        return f"每{week_text} {at}"
    if day.isdigit() and month == "*" and week == "*":
        return f"每月 {day} 号 {at}"
    return text


def _time_text(hour: str, minute: str) -> str | None:
    """``HH:MM`` 那种能一眼说清的时刻；说不清回 ``None``（由调用方退回原表达式）。"""
    if hour.isdigit() and minute.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    if hour == "*" and minute.isdigit():
        return f"每小时第 {int(minute)} 分"
    return None


def _date_matches(
    moment: datetime,
    days: set[int] | None,
    months: set[int] | None,
    weeks: set[int] | None,
    dom_restricted: bool,
    dow_restricted: bool,
) -> bool:
    if months is not None and moment.month not in months:
        return False
    # ``datetime.weekday()`` 是 0=周一，而 cron 的 0=周日，所以要换算
    weekday = (moment.weekday() + 1) % 7
    in_dom = days is None or moment.day in days
    in_dow = weeks is None or weekday in weeks
    if dom_restricted and dow_restricted:
        return in_dom or in_dow
    return in_dom and in_dow


def _first_time_on(
    moment: datetime, *, hours: set[int] | None, minutes: set[int] | None
) -> datetime | None:
    """这一天里第一个不早于 ``moment`` 的匹配时刻（没有就返回 ``None``）。"""
    candidates_hours = sorted(hours) if hours is not None else range(24)
    candidates_minutes = sorted(minutes) if minutes is not None else range(60)
    for hour in candidates_hours:
        for minute in candidates_minutes:
            found = moment.replace(hour=hour, minute=minute)
            if found >= moment:
                return found
    return None


def _parse_field(text: str, *, name: str, low: int, high: int) -> set[int] | None:
    """解析一个字段。``None`` 表示"不限"（原来是 ``*``）。

    支持 ``5`` / ``5,10`` / ``1-5`` / ``*/15`` / ``1-30/5``；其余一律报错并说明。
    """
    if text == "*":
        return None
    values: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise InvalidRequestError(f"{name} 字段里有空的取值：{text!r}")
        step = 1
        body = chunk
        if "/" in chunk:
            body, _, step_text = chunk.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise InvalidRequestError(f"{name} 字段的步长不合法：{chunk!r}（例：*/15）")
            step = int(step_text)
            if body in ("", "*"):
                body = f"{low}-{high}"
        if body == "*":
            start, end = low, high
        elif "-" in body:
            left, _, right = body.partition("-")
            start, end = (
                _number(left, name=name, low=low, high=high),
                _number(right, name=name, low=low, high=high),
            )
            if start > end:
                raise InvalidRequestError(f"{name} 字段的区间反了：{chunk!r}（小的写前面）")
        else:
            start = end = _number(body, name=name, low=low, high=high)
        values.update(range(start, end + 1, step))
    if not values:
        raise InvalidRequestError(f"{name} 字段没有任何取值：{text!r}")
    # 周字段：cron 里 0 与 7 都是周日，统一折成 0（否则 7 会变成一个"第 7 天"）
    if name == "周" and 7 in values:
        values.discard(7)
        values.add(0)
    return values


def _number(text: str, *, name: str, low: int, high: int) -> int:
    text = text.strip()
    if not text.isdigit():
        raise InvalidRequestError(f"{name} 字段只接受数字：{text!r}（不支持 MON / JAN 这类名字）")
    value = int(text)
    if not low <= value <= high:
        raise InvalidRequestError(f"{name} 字段超出范围（{low}-{high}）：{value}")
    return value
