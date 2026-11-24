"""US stock market hours, without needing a timezone database."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

OPEN = time(9, 30)
CLOSE = time(16, 0)


def new_york_time(utc_now: datetime | None = None) -> datetime:
    """Current wall-clock time in New York (naive datetime)."""
    utc = (utc_now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    # US daylight time runs from 2:00 on the second Sunday of March (07:00 UTC)
    # to 2:00 on the first Sunday of November (06:00 UTC).
    dst_start = _nth_sunday(utc.year, 3, 2).replace(hour=7)
    dst_end = _nth_sunday(utc.year, 11, 1).replace(hour=6)
    offset = -4 if dst_start <= utc < dst_end else -5
    return utc + timedelta(hours=offset)


def is_scan_time(grace_minutes: int = 0, utc_now: datetime | None = None) -> bool:
    """True on weekdays from the open until `grace_minutes` after the close.

    The grace period lets one last scan see the final closing prices.
    Market holidays are not excluded; a scan on a holiday just finds nothing new.
    """
    now = new_york_time(utc_now)
    if now.weekday() >= 5:
        return False
    opened = datetime.combine(now.date(), OPEN)
    closed = datetime.combine(now.date(), CLOSE) + timedelta(minutes=grace_minutes)
    return opened <= now <= closed


def session_fraction(candle_date: str, utc_now: datetime | None = None) -> float:
    """How much of the regular session has elapsed for a daily candle.

    1.0 for a finished day; between 0 and 1 for today's candle while the
    market is open.
    """
    now = new_york_time(utc_now)
    if candle_date != now.date().isoformat():
        return 1.0
    opened = datetime.combine(now.date(), OPEN)
    closed = datetime.combine(now.date(), CLOSE)
    if now >= closed:
        return 1.0
    return max((now - opened) / (closed - opened), 0.0)


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1)
    days_to_sunday = (6 - first.weekday()) % 7
    return first + timedelta(days=days_to_sunday + 7 * (n - 1))
