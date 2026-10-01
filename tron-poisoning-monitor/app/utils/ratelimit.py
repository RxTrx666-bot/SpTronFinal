"""Priority-aware async rate limiter (token bucket).

All TRON API calls share one bucket so the provider's limit is respected
globally.  Lower ``priority`` numbers are served first, so live block / wallet
polling is never starved by a long historical scan or a fund trace.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time

PRIORITY_LIVE = 0
PRIORITY_DETECTION = 1
PRIORITY_INVESTIGATION = 2
PRIORITY_HISTORY = 3


class PriorityRateLimiter:
    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        self.rate = max(rate_per_second, 0.0)
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_second)))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self._wakeup: asyncio.TimerHandle | None = None

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def _dispatch(self) -> None:
        self._wakeup = None
        self._refill()
        while self._waiters and self._tokens >= 1:
            _, _, fut = heapq.heappop(self._waiters)
            if fut.done():
                continue
            self._tokens -= 1
            fut.set_result(None)
        # drop cancelled waiters at the head
        while self._waiters and self._waiters[0][2].done():
            heapq.heappop(self._waiters)
        if self._waiters and self._wakeup is None:
            delay = (1 - self._tokens) / self.rate if self.rate else 0.05
            self._wakeup = asyncio.get_running_loop().call_later(max(delay, 0.001), self._dispatch)

    async def acquire(self, priority: int = PRIORITY_LIVE) -> None:
        if self.rate <= 0:
            return
        self._refill()
        if not self._waiters and self._tokens >= 1:
            self._tokens -= 1
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._seq), fut))
        if self._wakeup is None:
            self._dispatch()
        await fut
