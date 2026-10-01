"""Live fast path: ONE poller over the USDT contract's ``Transfer`` event stream.

Instead of polling each wallet (N wallets = N requests per interval), this
reads every USDT transfer on TRON once and matches it in memory against the
wallet registry.  API cost is independent of how many wallets are monitored:
roughly one request per POLL_INTERVAL_SECONDS (more only while catching up).

Checkpointing: the cursor (``checkpoints['stream:<contract>']``) is the
newest block timestamp fully processed.  Each poll starts at
``cursor - STREAM_OVERLAP_SECONDS`` because the API does not guarantee perfect
ordering and late/unconfirmed events may show up slightly after newer ones.
Re-reading the overlap is harmless: storage is idempotent on (tx_hash, event_index).
The cursor is only advanced *after* a page has been committed, so a crash or a
DB/API failure never skips events.
"""

from __future__ import annotations

import asyncio
import time

from app.config import Settings
from app.db import repository as repo
from app.domain import dt_to_ms, ms_to_dt, utcnow
from app.engine.processor import SOURCE_STREAM, TransferProcessor
from app.logging_setup import get_logger
from app.monitor.loop import run_forever
from app.tron.client import TronSource
from app.tron.normalizer import parse_events

log = get_logger(__name__)


def stream_key(contract: str) -> str:
    return f"stream:{contract}"


class StreamMonitor:
    def __init__(self, settings: Settings, source: TronSource, processor: TransferProcessor, session_factory) -> None:
        self.s = settings
        self.source = source
        self.proc = processor
        self.sf = session_factory
        self.key = stream_key(settings.usdt_contract)
        self.cursor_ms: int | None = None
        self.last_block: int | None = None
        self.last_poll_at: float | None = None
        self.events_scanned = 0
        self.rejected: dict[str, int] = {}

    async def initialise(self, start_ms: int | None = None) -> None:
        """First run: start at ``start_ms`` (now). Restart: resume from the saved cursor."""
        async with self.sf() as s, s.begin():
            cp = await repo.get_checkpoint(s, self.key)
            if cp is None:
                start = ms_to_dt(start_ms if start_ms is not None else dt_to_ms(utcnow()))
                await repo.init_checkpoint(s, self.key, start)
                log.info("Live stream starting from now", cursor=start.isoformat())
                self.cursor_ms = dt_to_ms(start)
            else:
                self.cursor_ms = dt_to_ms(cp.last_timestamp)
                self.last_block = cp.last_block
                log.info("Live stream resuming from checkpoint", cursor=cp.last_timestamp.isoformat(), block=cp.last_block)

    async def poll_once(self) -> bool:
        """Process everything after the cursor. Returns True if more pages are waiting."""
        if self.cursor_ms is None:
            await self.initialise()
        min_ts = self.cursor_ms - self.s.stream_overlap_seconds * 1000
        fingerprint: str | None = None
        for _ in range(self.s.stream_max_pages_per_poll):
            fetched_at = time.perf_counter()
            raws, fingerprint = await self.source.get_contract_events(
                min_timestamp_ms=min_ts,
                fingerprint=fingerprint,
                only_confirmed=self.s.require_confirmed,
                limit=self.s.stream_page_limit,
            )
            if raws:
                transfers, rejected = parse_events(raws, self.s.usdt_contract)
                self.events_scanned += len(raws)
                for k, v in rejected.items():
                    self.rejected[k] = self.rejected.get(k, 0) + v
                if rejected.get("malformed"):
                    log.warning("Malformed events skipped", count=rejected["malformed"])
                await self.proc.process(transfers, source=SOURCE_STREAM, live=True)  # raises -> cursor stays
                await self._advance(transfers, raws)
                log.debug("Stream page processed", events=len(raws), page_ms=f"{(time.perf_counter() - fetched_at) * 1000:.0f}")
            if not fingerprint or not raws:
                self.last_poll_at = time.time()
                return False
        self.last_poll_at = time.time()
        return True  # page budget exhausted: keep going immediately (catching up)

    async def _advance(self, transfers, raws) -> None:
        stamps = [t.timestamp_ms for t in transfers] or [
            int(r["block_timestamp"]) for r in raws if str(r.get("block_timestamp", "")).isdigit()
        ]
        if not stamps:
            return
        newest = max(stamps)
        blocks = [t.block_number for t in transfers if t.block_number]
        block = max(blocks) if blocks else None
        if newest > (self.cursor_ms or 0):
            async with self.sf() as s, s.begin():
                await repo.advance_checkpoint(s, self.key, ms_to_dt(newest), block)
            self.cursor_ms = newest
        if block and block > (self.last_block or 0):
            self.last_block = block

    @property
    def lag_seconds(self) -> float | None:
        if self.cursor_ms is None:
            return None
        return max(0.0, time.time() - self.cursor_ms / 1000)

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "Live stream monitor started",
            contract=self.s.usdt_contract,
            interval=f"{self.s.poll_interval_seconds}s",
            mode="confirmed-only" if self.s.require_confirmed else "fast (includes unconfirmed)",
        )
        await run_forever("stream", self.poll_once, self.s.poll_interval_seconds, stop)
