"""Pattern-learning service: (re-)analyses one Sender -> Recipient pair at a time.

Pairs are queued (coalesced) whenever a new confirmed transfer makes a TEST ->
LARGE sequence possible, when a follow-up completes, when a pending test
expires, and periodically for recency decay.  A pair is never analysed by two
workers at once.  The durable ``wallet_pairs.needs_analysis`` flag guarantees
that queued work survives a crash.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.amounts import fmt_range
from app.clock import Clock
from app.config.settings import Settings
from app.database import repository as repo
from app.database.session import is_transient_db_error
from app.detector.pattern_engine import analyze_history, median_int
from app.logging_setup import get_logger
from app.watchlist.manager import WatchlistManager
from app.watchlist.model import WatchlistCache, WatchlistSnapshot

log = get_logger(__name__)

Pair = tuple[str, str]


class AnalysisService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker,
        clock: Clock,
        cache: WatchlistCache,
        manager: WatchlistManager,
        notify: Callable[[], None],
    ) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.cache = cache
        self.manager = manager
        self.notify = notify
        self.queue: asyncio.Queue[Pair] = asyncio.Queue()
        self._queued: set[Pair] = set()
        self._inflight: set[Pair] = set()
        self._dirty: set[Pair] = set()
        self.analysed = 0

    # ---------------------------------------------------------------- queue
    def enqueue(self, pair: Pair) -> None:
        if pair in self._inflight:
            self._dirty.add(pair)
            return
        if pair not in self._queued:
            self._queued.add(pair)
            self.queue.put_nowait(pair)

    @property
    def backlog(self) -> int:
        return self.queue.qsize()

    async def _process(self, pair: Pair, *, silent: bool = False) -> None:
        self._queued.discard(pair)
        if pair in self._inflight:
            self._dirty.add(pair)
            return
        self._inflight.add(pair)
        try:
            await self.analyze_pair(*pair, silent=silent)
        finally:
            self._inflight.discard(pair)
            if pair in self._dirty:
                self._dirty.discard(pair)
                self.enqueue(pair)

    async def run_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                pair = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(pair)
            except Exception as exc:  # noqa: BLE001
                if is_transient_db_error(exc):
                    log.warning("Analysis deferred: database unavailable", error=type(exc).__name__)
                    await asyncio.sleep(2)
                    self.enqueue(pair)  # the needs_analysis flag also persists it
                else:
                    log.exception("Pattern analysis failed", sender=pair[0], recipient=pair[1])

    async def drain(self, *, silent: bool = False) -> int:
        """Process everything queued (tests / simulation / backfill)."""
        n = 0
        while not self.queue.empty():
            pair = self.queue.get_nowait()
            await self._process(pair, silent=silent)
            n += 1
        return n

    # ---------------------------------------------------------------- analysis
    async def analyze_pair(self, sender: str, recipient: str, *, silent: bool = False) -> WatchlistSnapshot | None:
        now = self.clock.now()
        since = now - timedelta(days=self.s.pattern_lookback_days)
        before = self.cache.get(sender, recipient)
        async with self.sf() as s, s.begin():
            history = await repo.pair_history(s, sender, recipient, since, self.s.pair_history_limit)
            model = analyze_history(sender, recipient, history, now, self.s)
            await repo.replace_sequences(s, sender, recipient, since, model, now)
            await repo.update_pair_after_analysis(
                s,
                sender,
                recipient,
                median_raw=median_int([h.amount_raw for h in history]) if history else None,
                sequence_count=len(model.sequences) if model else 0,
                now=now,
            )
            snap = await self.manager.apply_model(s, sender, recipient, model, now, silent=silent)
        self.analysed += 1
        if snap is not None:
            self.cache.put(snap)
            if before is None or before.successful_sequences != snap.successful_sequences or before.status != snap.status:
                log.info(
                    "Pattern model updated",
                    sender=sender,
                    recipient=recipient,
                    status=snap.status.value,
                    sequences=snap.successful_sequences,
                    test_range=fmt_range(snap.test_min_raw, snap.test_max_raw),
                    large_range=fmt_range(snap.large_min_raw, snap.large_max_raw),
                    confidence=f"{snap.confidence}({snap.confidence_score:.2f})",
                )
            self.notify()
        elif model is not None and model.sequences:
            log.debug("Candidate test pattern detected", sender=sender, recipient=recipient, sequences=len(model.sequences))
        return snap

    async def sweep_flagged(self, limit: int = 1000) -> int:
        """Re-queue pairs whose durable needs_analysis flag is set (crash recovery)."""
        async with self.sf() as s:
            rows = await repo.flagged_pairs(s, limit=limit)
        for _, snd, rcp in rows:
            self.enqueue((snd, rcp))
        return len(rows)

    async def run_backfill_analysis(self, stop: asyncio.Event | None = None) -> int:
        """Analyse every relationship that could contain a pattern after a backfill.
        Runs silently (a single summary message is sent instead of per-pair alerts)."""
        async with self.sf() as s, s.begin():
            n = await repo.flag_backfill_candidates(
                s,
                min_transfers=2 * self.s.candidate_min_sequences,
                ratio=self.s.ratio_fraction,
                min_large_raw=self.s.min_large_raw,
            )
        log.info("Historical analysis started", candidate_pairs=n)
        done = 0
        last_id = 0
        silent = not self.s.backfill_notify_individual
        while stop is None or not stop.is_set():
            async with self.sf() as s:
                rows = await repo.flagged_pairs(s, limit=500, after_id=last_id)
            if not rows:
                break
            for pid, snd, rcp in rows:
                last_id = pid
                await self._process((snd, rcp), silent=silent)
                done += 1
                if done % 1000 == 0:
                    log.info("Historical analysis progress", analysed=done, of=n)
        log.info("Historical analysis finished", analysed=done, watchlist=len(self.cache))
        return done
