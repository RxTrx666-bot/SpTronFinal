"""Time helpers. All internal timestamps are integer milliseconds since epoch (UTC)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ms_to_datetime(ms: int, tz: ZoneInfo | timezone = timezone.utc) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)


def format_utc(ms: int, with_millis: bool = False) -> str:
    """Format as ``YYYY-MM-DD HH:mm:ss UTC`` (optionally with milliseconds)."""
    dt = ms_to_datetime(ms)
    if with_millis:
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{ms % 1000:03d} UTC"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_local(ms: int, tz: ZoneInfo) -> str:
    dt = ms_to_datetime(ms, tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S ") + (dt.tzname() or str(tz))


def iso_utc(ms: int) -> str:
    """ISO-8601 UTC string with milliseconds, used for DB storage."""
    return ms_to_datetime(ms).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
