"""Transaction-limit notice.

Counts live (non-backfill) matching transactions in the current *cycle*. When the
count reaches TX_LIMIT_THRESHOLD, one "Balance negative / Fill resources / Run again"
notice is sent (through the alert queue, so it arrives right after the alert that hit
the limit and is retried until delivered). ``/reset`` starts a new cycle at 0.

State lives in the database, so the count and "already notified" survive restarts.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.database import Repository
from app.logger import kv

log = logging.getLogger(__name__)

STATE_CYCLE = "limit.cycle"
STATE_START_ID = "limit.start_id"
STATE_NOTIFIED_CYCLE = "limit.notified_cycle"


class NoticeSink(Protocol):
    async def enqueue_notice(self, cycle: int) -> None: ...


class TxLimitTracker:
    def __init__(self, repo: Repository, threshold: int, sink: NoticeSink | None = None) -> None:
        self.repo = repo
        self.threshold = threshold
        self.sink = sink

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    async def cycle(self) -> int:
        return int(await self.repo.get_state(STATE_CYCLE) or 1)

    async def count(self) -> int:
        start_id = int(await self.repo.get_state(STATE_START_ID) or 0)
        return await self.repo.count_live_transactions_after(start_id)

    async def already_notified(self, cycle: int) -> bool:
        return await self.repo.get_state(STATE_NOTIFIED_CYCLE) == str(cycle)

    async def mark_notified(self, cycle: int) -> None:
        await self.repo.set_state(STATE_NOTIFIED_CYCLE, str(cycle))

    async def check(self) -> bool:
        """Queue the notice if the threshold is reached and not yet notified this cycle."""
        if not self.enabled or self.sink is None:
            return False
        cycle = await self.cycle()
        count = await self.count()
        if count < self.threshold or await self.already_notified(cycle):
            return False
        log.warning("tx_limit_reached", extra=kv(count=count, threshold=self.threshold, cycle=cycle))
        await self.sink.enqueue_notice(cycle)
        return True

    async def reset(self) -> int:
        """Start a new cycle counting from 0. Returns the new cycle number."""
        cycle = await self.cycle() + 1
        await self.repo.set_state(STATE_START_ID, str(await self.repo.max_transaction_id()))
        await self.repo.set_state(STATE_CYCLE, str(cycle))
        log.info("tx_limit_reset", extra=kv(cycle=cycle, threshold=self.threshold))
        return cycle
