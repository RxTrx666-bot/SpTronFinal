"""Transaction-limit notice and pause.

Counts live (non-backfill) matching transactions in the current *cycle*. When the
count reaches TX_LIMIT_THRESHOLD:

1. the monitor is PAUSED immediately (no further detection or alerts), and
2. one "Balance negative 🚨 / Fill resources / Run again" notice with a ▶️ Start
   button is sent (through the alert queue, right after the alert that hit the limit,
   retried until delivered).

Pressing 🚀 Let's go (or sending /letsgo) resumes monitoring *from that moment* and
starts a new cycle at 0. With START_PAUSED=true the bot also starts paused after every
(re)start and waits for /letsgo. Changing TX_LIMIT_THRESHOLD also starts a new cycle ("count from now").

All state lives in the database, so count, pause and "already notified" survive restarts.
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
STATE_PAUSED = "limit.paused"
STATE_THRESHOLD = "limit.threshold"


class NoticeSink(Protocol):
    async def enqueue_notice(self, cycle: int) -> None: ...


class TxLimitTracker:
    def __init__(
        self, repo: Repository, threshold: int, sink: NoticeSink | None = None, pause_on_limit: bool = True
    ) -> None:
        self.repo = repo
        self.threshold = threshold
        self.sink = sink
        self.pause_on_limit = pause_on_limit
        self.paused = False  # in-memory mirror of STATE_PAUSED (checked on every transfer)
        self.resume_generation = 0  # bumped on every resume; the monitor re-anchors "now" when it changes

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    async def load(self) -> None:
        """Call once at startup: restores the pause flag; a new threshold starts a new cycle."""
        self.paused = self.enabled and self.pause_on_limit and await self.repo.get_state(STATE_PAUSED) == "1"
        stored = await self.repo.get_state(STATE_THRESHOLD)
        if self.enabled and stored != str(self.threshold):
            await self.reset()
            await self.repo.set_state(STATE_THRESHOLD, str(self.threshold))
            log.info("tx_limit_threshold_set_counting_from_now",
                     extra=kv(threshold=self.threshold, previous=stored))
        if self.paused:
            log.warning("monitoring_paused_waiting_for_start", extra=kv(threshold=self.threshold))

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
        """Pause + queue the notice if the threshold is reached (once per cycle)."""
        if not self.enabled or self.sink is None:
            return False
        cycle = await self.cycle()
        count = await self.count()
        if count < self.threshold or await self.already_notified(cycle):
            return False
        if self.pause_on_limit and not self.paused:
            self.paused = True
            await self.repo.set_state(STATE_PAUSED, "1")
        log.warning("tx_limit_reached", extra=kv(count=count, threshold=self.threshold, cycle=cycle,
                                                 paused=self.paused))
        await self.sink.enqueue_notice(cycle)
        return True

    async def reset(self) -> int:
        """Start a new cycle counting from 0. Returns the new cycle number."""
        cycle = await self.cycle() + 1
        await self.repo.set_state(STATE_START_ID, str(await self.repo.max_transaction_id()))
        await self.repo.set_state(STATE_CYCLE, str(cycle))
        log.info("tx_limit_reset", extra=kv(cycle=cycle, threshold=self.threshold))
        return cycle

    async def pause(self, reason: str) -> None:
        """Pause monitoring until /letsgo (used at startup when START_PAUSED=true)."""
        if not self.paused:
            self.paused = True
            await self.repo.set_state(STATE_PAUSED, "1")
        log.warning("monitoring_paused_waiting_for_letsgo", extra=kv(reason=reason))

    async def resume(self) -> tuple[bool, int]:
        """▶️ Start: un-pause (monitoring restarts from *now*) and begin a new cycle.

        Returns (was_paused, new_cycle).
        """
        was_paused = self.paused
        if was_paused:
            self.paused = False
            await self.repo.set_state(STATE_PAUSED, "0")
            self.resume_generation += 1
            log.info("monitoring_resumed")
        return was_paused, await self.reset()
