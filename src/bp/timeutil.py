from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from chinese_calendar import get_holiday_detail

BEIJING = zoneinfo.ZoneInfo("Asia/Shanghai")

UTC = timezone.utc

# DeepSeek published peak window, expressed in Beijing time:
#   01:00-04:00 UTC and 06:00-10:00 UTC, Mon-Fri, excluding Chinese public holidays.
# All other hours (weekends, holidays, evenings, nights) bill at half price.
PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((9, 12), (14, 18))


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_bj() -> datetime:
    return datetime.now(BEIJING)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING).astimezone(UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 with a trailing Z, second precision (SQLite-comparable)."""
    if dt is None:
        return None
    return to_utc(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return to_utc(dt)


@lru_cache(maxsize=1)
def holiday_data_years() -> tuple[int, int] | None:
    """The span of years chinese_calendar actually has arrangements for.

    Read off the package's own holiday table instead of hardcoding a range, so
    the answer stays correct after a plain dependency bump — which is exactly
    the remediation when a new year's arrangement ships.
    """
    try:
        from chinese_calendar.constants import holidays
    except Exception:
        return None
    years = {day.year for day in holidays}
    return (min(years), max(years)) if years else None


def holiday_data_covers(year: int) -> bool:
    """Whether the official arrangement for ``year`` is known.

    The arrangement is published by the State Council late in the preceding
    year, so the newest release of the package legitimately stops at the
    current year. A gap here is "the data does not exist yet", not a bug, and
    cannot be fixed by upgrading.
    """
    span = holiday_data_years()
    return bool(span) and span[0] <= year <= span[1]


def is_chinese_holiday(d: date) -> bool:
    """True when ``d`` is a Chinese public holiday.

    For years the package does not cover, falls back to weekend-only: a weekday
    is then assumed to be a working day. That is right about 96% of the time
    (11 public holiday days out of roughly 250 weekdays), and the cost of being
    wrong is bounded — the nightly 18:05 batch falls outside DeepSeek's peak
    windows by time of day, so a missed holiday only delays daytime catch-up
    until that evening's run. ``bp doctor`` reports the gap rather than letting
    it degrade silently.
    """
    if not holiday_data_covers(d.year):
        return d.weekday() >= 5
    try:
        is_holiday, name = get_holiday_detail(d)
    except Exception:
        # 只剩这一层作为兜底：不能让调度器因为一个查日期的调用而崩掉。
        return d.weekday() >= 5
    return bool(is_holiday) and name is not None


def is_deepseek_peak(dt: datetime) -> bool:
    """True when DeepSeek bills at double rate for this instant.

    Only Mon-Fri count as peak days; a 调休 Saturday that chinesecalendar marks
    as a working day is still off-peak, matching DeepSeek's own UTC definition.
    """
    bj = bj_dt(dt)
    if bj.weekday() >= 5:
        return False
    if is_chinese_holiday(bj.date()):
        return False
    minutes = bj.hour * 60 + bj.minute
    return any(start * 60 <= minutes < end * 60 for start, end in PEAK_WINDOWS)


def bj_dt(dt: datetime) -> datetime:
    return dt.replace(tzinfo=BEIJING) if dt.tzinfo is None else dt.astimezone(BEIJING)


def next_off_peak(dt: datetime) -> datetime:
    """First instant at or after ``dt`` that is billed off-peak."""
    candidate = dt.astimezone(BEIJING)
    for _ in range(24 * 8):
        if not is_deepseek_peak(candidate):
            return candidate
        candidate += timedelta(minutes=5)
    return candidate


def bj_today(dt: datetime | None = None) -> date:
    return (dt or now_utc()).astimezone(BEIJING).date()
