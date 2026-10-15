"""Periodic housekeeping and crash recovery."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.database import repository as repo
from app.database.session import is_transient_db_error
from app.domain import WatchlistStatus
from app.logging_setup import get_logger

log = get_logger(__name__)


class Maintenance:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.s = app.settings
        self._last_reeval = 0.0
        self._prune_idle_until = 0.0
        self._pruned_total = 0
        self._last_prune_log = 0.0
        self._last_heartbeat_log = 0.0

    async def run_once(self) -> None:
        a = self.app
        now = a.clock.now()
        self.heartbeat()

        # 1. Pending test events whose follow-up window elapsed -> EXPIRED (lowers success rate).
        async with a.session_factory() as s, s.begin():
            expired_pairs = await repo.expire_test_events(s, now)
        for p in expired_pairs:
            a.analysis.enqueue(p)

        # 2. Transfers stored but never matched (crash between store and match).
        async with a.session_factory() as s:
            todo = await repo.unprocessed_transactions(s, now - timedelta(seconds=5))
        for tx in todo:
            await a.matcher.match(tx, now)
        if todo:
            log.info("Recovered unmatched transfers", count=len(todo))

        # 3. Durable analysis flags (crash recovery of the analysis queue).
        if a.analysis.backlog < 1000 and a.collector.stats.backfill_done:
            await a.analysis.sweep_flagged(limit=1000)

        # 4. Unconfirmed transfers that never confirmed.
        cursor = a.collector.stats.confirmed_cursor_ms
        if cursor:
            from app.domain import ms_to_datetime

            cutoff = ms_to_datetime(cursor) - timedelta(minutes=self.s.unconfirmed_drop_after_minutes)
            async with a.session_factory() as s, s.begin():
                dropped = await repo.mark_dropped_unconfirmed(s, cutoff)
                await repo.cancel_test_events_for_txs(s, dropped)
            if dropped:
                log.warning("Unconfirmed transfers never confirmed; marked DROPPED", count=len(dropped))

        # 5. Timed pauses that have ended, and periodic re-evaluation (recency decay).
        mono = time.monotonic()
        reeval_all = mono - self._last_reeval > self.s.watchlist_reevaluate_minutes * 60
        for snap in a.cache.values():
            if snap.status == WatchlistStatus.PAUSED and not snap.manual_pause and snap.pause_until and snap.pause_until <= now:
                a.analysis.enqueue(snap.pair)
            elif reeval_all and snap.status != WatchlistStatus.EXPIRED:
                a.analysis.enqueue(snap.pair)
        if reeval_all:
            self._last_reeval = mono

        # 6. Retention: delete old transfers in small slices, and only while the live
        #    stream is up to date, so a large clean-up never starves live processing.
        await self.prune_step(now, mono)

        self.heartbeat()

    async def prune_step(self, now, mono: float) -> int:
        a = self.app
        if self.s.retention_days <= 0 or mono < self._prune_idle_until:
            return 0
        cursor = a.collector.stats.confirmed_cursor_ms
        if cursor is None or time.time() * 1000 - cursor > self.s.prune_max_lag_seconds * 1000:
            return 0  # behind: give the database to the live stream first
        cutoff = now - timedelta(days=self.s.retention_days)
        deleted = 0
        for _ in range(self.s.prune_batches_per_cycle):
            async with a.session_factory() as s, s.begin():
                n = await repo.prune_transactions(s, cutoff, batch=self.s.prune_batch_size)
            deleted += n
            if n < self.s.prune_batch_size:
                self._prune_idle_until = mono + 3600  # nothing (more) to delete for now
                break
            await asyncio.sleep(0.2)
        self._pruned_total += deleted
        if self._pruned_total and (deleted == 0 or mono - self._last_prune_log > 300):
            log.info("Pruned old transfers", count=self._pruned_total, older_than=cutoff.isoformat())
            self._pruned_total = 0
            self._last_prune_log = mono
        return deleted

    def heartbeat(self) -> None:
        try:
            Path(self.s.heartbeat_file).write_text(str(int(time.time())))
        except OSError:
            pass
        mono = time.monotonic()
        if mono - self._last_heartbeat_log >= self.s.heartbeat_log_seconds:
            self._last_heartbeat_log = mono
            a = self.app
            cs = a.collector.stats
            lag = (time.time() * 1000 - cs.confirmed_cursor_ms) / 1000 if cs.confirmed_cursor_ms else None
            log.info(
                "Heartbeat",
                confirmed_lag_s=f"{lag:.0f}" if lag is not None else "n/a",
                seen=a.stats.events_seen,
                stored=a.stats.events_stored,
                watchlist=len(a.cache),
                analysis_backlog=a.analysis.backlog,
                alerts_sent=a.dispatcher.sent,
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                if is_transient_db_error(exc):
                    log.error("Maintenance skipped: database unavailable", error=type(exc).__name__)
                else:
                    log.exception("Maintenance error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.maintenance_interval_seconds)
            except asyncio.TimeoutError:
                pass
