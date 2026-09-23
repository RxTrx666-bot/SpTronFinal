"""Injectable clocks so time-dependent logic (decay, expiry) is testable."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SystemClock(Clock):
    pass


class ManualClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
