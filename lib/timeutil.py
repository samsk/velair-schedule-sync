"""Clock and duration helpers."""

from __future__ import annotations

import re
from typing import Tuple

CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
DURATION_RE = re.compile(r"^(\d+):(\d{2})$")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY = ("mon", "tue", "wed", "thu", "fri")
WEEKEND = ("sat", "sun")


def parse_clock(value: str) -> int:
    """Parse HH:MM to minutes since midnight.

    Args:
        value: Clock string like "22:00".
    Returns:
        Minutes in [0, 1440).
    """
    match = CLOCK_RE.match(value.strip())
    if not match:
        raise ValueError(f"bad clock: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError(f"bad clock: {value!r}")
    return (hour % 24) * 60 + minute


def parse_duration(value: str) -> int:
    """Parse H:MM or HH:MM duration to minutes.

    Args:
        value: Duration like "0:30" or "2:00".
    Returns:
        Total minutes.
    """
    match = DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(f"bad duration: {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def format_clock(minutes: int) -> str:
    """Format minutes since midnight as HH:MM.

    Args:
        minutes: Absolute or wrapped minutes.
    Returns:
        Zero-padded clock string.
    """
    minutes = minutes % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def wrap_add(base: int, delta: int) -> int:
    """Add minutes wrapping at midnight.

    Args:
        base: Minutes since midnight.
        delta: Signed minutes to add.
    Returns:
        Wrapped minutes in [0, 1440).
    """
    return (base + delta) % 1440


def ceil_quarter(minutes: int) -> int:
    """Round up to next 15-minute step.

    Args:
        minutes: Minutes since midnight.
    Returns:
        Ceiled minutes (may wrap to 0).
    """
    rem = minutes % 15
    if rem == 0:
        return minutes % 1440
    return (minutes + (15 - rem)) % 1440


def floor_quarter(minutes: int) -> int:
    """Round down to previous 15-minute step.

    Args:
        minutes: Minutes since midnight.
    Returns:
        Floored minutes.
    """
    return (minutes - (minutes % 15)) % 1440


def day_kind(day: str) -> str:
    """Map a day name to weekday/weekend.

    Args:
        day: mon..sun, weekday, or weekend.
    Returns:
        "weekday" or "weekend".
    """
    if day in ("weekday", "weekend"):
        return day
    if day in WEEKDAY:
        return "weekday"
    if day in WEEKEND:
        return "weekend"
    raise ValueError(f"bad day: {day!r}")


def expand_days(day: str) -> Tuple[str, ...]:
    """Expand day selector to concrete day names.

    Args:
        day: mon..sun, weekday, weekend, or all.
    Returns:
        Tuple of day names.
    """
    if day == "all":
        return DAYS
    if day == "weekday":
        return WEEKDAY
    if day == "weekend":
        return WEEKEND
    if day in DAYS:
        return (day,)
    raise ValueError(f"bad day: {day!r}")
