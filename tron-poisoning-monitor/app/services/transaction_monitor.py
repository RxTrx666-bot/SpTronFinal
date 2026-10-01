"""Live transaction monitoring.

Two strategies (``MONITOR_MODE``):

``block`` (default, recommended at scale)
    Polls the chain head and reads every new block with two requests
    (``getblockbynum`` + ``gettransactioninfobyblocknum``).  Transfers of the
    configured tokens are filtered in memory against the monitored-wallet set,
    so the cost is constant whether 10 or 100,000 wallets are watched, and a
    victim's outgoing transfer is seen ~1 block (≈3 s) after it is produced.
    The last processed block is persisted; after a restart processing resumes
    from the next block.  Gaps longer than ``MAX_BLOCK_CATCHUP`` blocks (e.g.
    after long downtime) are filled through per-wallet history queries.

``account``
    Polls each wallet's TRC-20 history endpoint.  Simpler, but the cost grows
    with the number of wallets; suitable for small deployments or providers
    without block endpoints.

All paths funnel into :class:`Ingestor`, which stores transfers idempotently
and runs the detector on each new one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from sqlalchemy import select

from app import repository as repo
from app.database import is_transient_db_error, with_db_retry
from app.domain import AnalysisStatus, BlockData, TokenTransfer
from app.models import Transaction, WatchedWallet
from app.services.poisoning_detector import PoisoningDetector, WalletRegistry
from app.services.tron_service import TronApiError
from app.utils.clock import Clock, as_utc, to_ms
from app.utils.logging import get_logger
from app.utils.ratelimit import PRIORITY_LIVE

log = get_logger(__name__)

STATE_BLOCK_CURSOR = "block_cursor"
STATE_BLOCK_CURSOR_TS = "block_cursor_ts"


class Ingestor:
    def __init__(self, settings, session_factory, source, clock: Clock, registry: WalletRegistry, detector: PoisoningDetector) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.registry = registry
        self.detector = detector
        self.stats = {"transfers_seen": 0, "relevant": 0, "new": 0, "analysis_errors": 0}

    def relevant(self, transfers: list[TokenTransfer]) -> list[TokenTransfer]:
        tokens = self.s.tokens_by_contract
        reg = self.registry
        return [
            t for t in transfers
            if t.token_contract in tokens and (reg.is_monitored(t.from_address) or reg.is_monitored(t.to_address))
        ]  # fmt: skip

    async def ingest(self, transfers: list[TokenTransfer], *, source: str, detected_at=None) -> list[int]:
        """Store relevant transfers and analyse the new ones in order.  Returns created incident ids."""
        self.stats["transfers_seen"] += len(transfers)
        rel = self.relevant(transfers)
        if not rel:
            return []
        self.stats["relevant"] += len(rel)
        detected = detected_at or self.clock.now()

        async def store():
            async with self.sf() as s, s.begin():
                return await repo.insert_transfers(s, rel, source=source, detected_at=detected, now=self.clock.now())

        new = await with_db_retry(store, what="insert_transfers")
        self.stats["new"] += len(new)
        events: list[int] = []
        for tx_id, t in new:
            events += await self.safe_analyze(tx_id, t.from_address, t.to_address)
        return events

    async def safe_analyze(self, tx_id: int, from_address: str | None = None, to_address: str | None = None) -> list[int]:
        try:
            return await with_db_retry(lambda: self.detector.analyze(tx_id, from_address=from_address, to_address=to_address), what="analyze")
        except Exception as exc:  # noqa: BLE001 - a bug must not stop monitoring; recovery retries
            self.stats["analysis_errors"] += 1
            log.exception("ANALYSIS_FAILED", tx_id=tx_id, error=type(exc).__name__)
            try:
                async with self.sf() as s, s.begin():
                    tx = await s.get(Transaction, tx_id)
                    if tx is not None and tx.analysis_status == AnalysisStatus.PENDING.value:
                        tx.analysis_attempts += 1
                        tx.analysis_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                        if tx.analysis_attempts >= 5:
                            tx.analysis_status = AnalysisStatus.ERROR.value
            except Exception:  # noqa: BLE001
                pass
            return []

    async def recover_pending(self, older_than_seconds: float = 0) -> int:
        """Analyse transfers that were stored but not analysed (crash / restart)."""
        cutoff = self.clock.now() - timedelta(seconds=older_than_seconds)
        async with self.sf() as s:
            ids = await repo.pending_transaction_ids(s, cutoff)
        for tx_id in ids:
            await self.safe_analyze(tx_id)
        if ids:
            log.info("PENDING_ANALYSIS_RECOVERED", count=len(ids))
        return len(ids)

    async def poll_wallet(self, address: str, since_ms: int, *, source: str, priority: int = PRIORITY_LIVE, max_pages: int = 10) -> int:
        """Fetch a wallet's transfers since ``since_ms`` (account mode / gap fill).  Returns max timestamp seen."""
        token = self.s.primary_token
        fp = None
        latest = since_ms
        for _ in range(max_pages):
            page, fp = await self.source.get_trc20_transfers(address, token.contract, min_timestamp_ms=since_ms, order="asc", fingerprint=fp, priority=priority)
            if page:
                await self.ingest(page, source=source)
                latest = max(latest, max(t.block_timestamp_ms for t in page))
            if not fp or not page:
                break
        return latest


class BlockMonitor:
    def __init__(self, settings, session_factory, source, clock: Clock, ingestor: Ingestor) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.ingestor = ingestor
        self.contracts = {t.contract: t.decimals for t in settings.token_list}
        self.cursor: int | None = None
        self.head: int | None = None
        self.last_block_ts_ms: int | None = None
        self.last_poll_ok: float | None = None
        self.blocks_processed = 0
        self.api_errors = 0
        self.last_block_latency_ms: int | None = None

    async def _save_cursor(self, number: int, ts_ms: int) -> None:
        async def save():
            async with self.sf() as s, s.begin():
                now = self.clock.now()
                await repo.set_state(s, STATE_BLOCK_CURSOR, str(number), now)
                await repo.set_state(s, STATE_BLOCK_CURSOR_TS, str(ts_ms), now)

        await with_db_retry(save, what="save_block_cursor")
        self.cursor = number
        self.last_block_ts_ms = ts_ms

    async def init_cursor(self) -> None:
        async with self.sf() as s:
            cur = await repo.get_state(s, STATE_BLOCK_CURSOR)
            ts = await repo.get_state(s, STATE_BLOCK_CURSOR_TS)
        if cur is not None:
            self.cursor = int(cur)
            self.last_block_ts_ms = int(ts) if ts else None
            log.info("BLOCK_MONITOR_RESUME", cursor=self.cursor)
            return
        head = await self.source.get_now_block_number()
        start = head - 1 - self.s.start_block_lag
        await self._save_cursor(start, self.clock.now_ms())
        log.info("BLOCK_MONITOR_INIT", head=head, cursor=start)

    async def process_block(self, blk: BlockData) -> list[int]:
        detected = self.clock.now()
        events = await self.ingestor.ingest(blk.transfers, source="LIVE", detected_at=detected)
        await self._save_cursor(blk.number, blk.timestamp_ms)
        self.blocks_processed += 1
        self.last_block_latency_ms = max(0, to_ms(detected) - blk.timestamp_ms)
        return events

    async def gap_fill(self, head: int) -> None:
        since = (self.last_block_ts_ms or self.clock.now_ms()) - self.s.account_poll_overlap_seconds * 1000
        wallets = list(self.ingestor.registry.wallets)
        log.warning("BLOCK_GAP_FILL", cursor=self.cursor, head=head, wallets=len(wallets))
        sem = asyncio.Semaphore(self.s.account_poll_concurrency)

        async def one(addr):
            async with sem:
                await self.ingestor.poll_wallet(addr, since, source="GAPFILL")

        await asyncio.gather(*(one(a) for a in wallets))
        await self._save_cursor(head - 1, self.clock.now_ms())

    async def step(self) -> int:
        """One polling iteration.  Returns number of blocks processed."""
        head = await self.source.get_now_block_number()
        self.head = head
        self.last_poll_ok = time.time()
        if self.cursor is None:
            await self.init_cursor()
        if head <= self.cursor:
            return 0
        if head - self.cursor > self.s.max_block_catchup:
            await self.gap_fill(head)
        processed = 0
        n = self.cursor + 1
        while n <= head:
            batch = list(range(n, min(head, n + self.s.block_prefetch - 1) + 1))
            blocks = await asyncio.gather(*(self.source.get_block(b, self.contracts) for b in batch))
            for blk in blocks:
                await self.process_block(blk)
                processed += 1
            n = batch[-1] + 1
        return processed

    def next_poll_delay(self) -> float:
        """Sleep until the next block is due instead of polling blindly.

        TRON produces a block every 3 s.  Polling right when the next block should be
        readable keeps detection latency the same while using ~1-2 head requests per
        block instead of ~3.  If the block is late, fall back to BLOCK_POLL_INTERVAL_SECONDS.
        """
        minimum = self.s.block_poll_interval_seconds
        if not self.last_block_ts_ms:
            return minimum
        due_ms = self.last_block_ts_ms + 3000 + self.s.block_arrival_margin_ms
        wait = (due_ms - self.clock.now_ms()) / 1000
        return min(3.0, wait) if wait > minimum else minimum

    async def run(self, stop: asyncio.Event) -> None:
        delay = self.s.block_poll_interval_seconds
        while not stop.is_set():
            try:
                processed = await self.step()
                if processed and self.head is not None and self.cursor is not None and self.cursor < self.head:
                    continue  # still catching up
                delay = self.next_poll_delay()
            except TronApiError as exc:
                self.api_errors += 1
                log.warning("TRON_API_UNAVAILABLE", component="block_monitor", api_status=exc.status, error=str(exc)[:150], retry_in=f"{delay:.1f}s")
                delay = min(delay * 2, 30)
            except Exception as exc:  # noqa: BLE001
                if is_transient_db_error(exc):
                    log.warning("DB_UNAVAILABLE", component="block_monitor", error=type(exc).__name__)
                else:
                    log.exception("BLOCK_MONITOR_ERROR", error=type(exc).__name__)
                delay = min(delay * 2, 30)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


class AccountMonitor:
    """Per-wallet polling (MONITOR_MODE=account)."""

    def __init__(self, settings, session_factory, source, clock: Clock, ingestor: Ingestor) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.ingestor = ingestor
        self.last_poll_ok: float | None = None
        self.rounds = 0
        self.api_errors = 0

    async def poll_all(self) -> None:
        async with self.sf() as s:
            rows = (await s.execute(select(WatchedWallet).where(WatchedWallet.status != "REMOVED"))).scalars().all()
            wallets = [(w.address, w.poll_cursor_ms or to_ms(as_utc(w.added_at))) for w in rows]
        sem = asyncio.Semaphore(self.s.account_poll_concurrency)
        overlap = self.s.account_poll_overlap_seconds * 1000

        async def one(addr: str, cursor: int) -> None:
            async with sem:
                try:
                    latest = await self.ingestor.poll_wallet(addr, cursor - overlap, source="LIVE", max_pages=3)
                except TronApiError as exc:
                    self.api_errors += 1
                    log.warning("TRON_API_UNAVAILABLE", component="account_monitor", wallet=addr, error=str(exc)[:120])
                    return
                if latest > cursor:

                    async def save():
                        async with self.sf() as s, s.begin():
                            w = await repo.get_wallet(s, addr)
                            if w:
                                w.poll_cursor_ms = latest

                    await with_db_retry(save, what="poll_cursor")

        await asyncio.gather(*(one(a, c) for a, c in wallets))
        self.rounds += 1
        self.last_poll_ok = time.time()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll_all()
            except Exception as exc:  # noqa: BLE001
                log.exception("ACCOUNT_MONITOR_ERROR", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.account_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
