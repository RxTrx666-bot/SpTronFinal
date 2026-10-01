"""Central monitoring scheduler (per-wallet safety net) and historical backfill.

The live stream (``monitor.stream``) catches transfers within seconds.  This
scheduler guarantees nothing is missed even if the stream had a gap (API
outage longer than the overlap window, a wallet discovered from an event that
arrived late, ...):

    discovery queue (new wallets, high priority)  ─┐
    periodic sweep over every monitored wallet    ─┴─> work queue
                                                        │  RECONCILE_WORKERS workers
                                                        ▼  (global API semaphore + rate limit)
                                   /v1/accounts/{w}/transactions/trc20  (from checkpoint)
                                                        │  only unknown candidates
                                                        ▼
                                   /v1/transactions/{tx}/events -> TransferProcessor

There is never one loop per wallet: a fixed number of workers drains a queue,
so 10 or 10,000 wallets cost the same concurrency; more wallets only make a
sweep take longer (bounded by MAX_REQUESTS_PER_SECOND).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.config import Settings
from app.db import repository as repo
from app.domain import Transfer, dt_to_ms, ms_to_dt
from app.engine.processor import SOURCE_BACKFILL, SOURCE_RECONCILE, TransferProcessor
from app.engine.registry import WalletRegistry
from app.logging_setup import get_logger
from app.monitor.loop import sleep_or_stop
from app.tron.client import TronApiError, TronSource
from app.tron.normalizer import parse_events, parse_tx_info_logs

log = get_logger(__name__)

IN, OUT = "in", "out"
STATE_BACKFILL_DONE = "backfill_done"
STATE_BACKFILL_NEXT = "backfill_next_ms"
MAX_PAGES_PER_CHECK = 200


def checkpoint_key(address: str, direction: str) -> str:
    return address if direction == IN else f"out:{address}"


@dataclass(frozen=True)
class _Job:
    address: str
    direction: str


class WalletScheduler:
    def __init__(
        self,
        settings: Settings,
        source: TronSource,
        processor: TransferProcessor,
        session_factory,
        registry: WalletRegistry,
    ) -> None:
        self.s = settings
        self.source = source
        self.proc = processor
        self.sf = session_factory
        self.reg = registry
        self._priority: asyncio.Queue[_Job] = asyncio.Queue()
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._queued: set[_Job] = set()
        self.sweeps = 0
        self.last_sweep_at: float | None = None
        self.last_sweep_seconds: float | None = None
        self.recovered = 0  # transfers the stream had missed
        self.backfill_running = False

    # ------------------------------------------------------------- queueing
    def enqueue_discovered(self, addresses: list[str]) -> None:
        """Processor callback: check freshly discovered wallets right away."""
        for a in addresses:
            job = _Job(a, IN)
            if job not in self._queued:
                self._queued.add(job)
                self._priority.put_nowait(job)

    async def _next_job(self) -> tuple[_Job, bool]:
        while True:
            if not self._priority.empty():
                return self._priority.get_nowait(), True
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=0.5), False
            except asyncio.TimeoutError:
                continue

    async def _worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            job, prio = await self._next_job()
            try:
                await self.check_wallet(job.address, job.direction)
            except TronApiError as exc:
                log.warning("Wallet check failed; will retry next sweep", wallet=job.address, error=str(exc)[:150])
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("Wallet check error; will retry next sweep", wallet=job.address)
            finally:
                self._queued.discard(job)
                if not prio:
                    self._queue.task_done()

    def _sweep_jobs(self) -> list[_Job]:
        return [_Job(w.address, OUT) for w in self.reg.expanders()] + [_Job(w.address, IN) for w in self.reg.monitored()]

    async def sweep_once_inline(self) -> int:
        """Sequential sweep without workers (tests / one-off maintenance)."""
        jobs = self._sweep_jobs()
        for job in jobs:
            await self.check_wallet(job.address, job.direction)
        return len(jobs)

    async def sweep_once(self) -> int:
        """Queue every wallet for the worker pool and wait until the sweep is done."""
        jobs = self._sweep_jobs()
        n = 0
        for job in jobs:
            if job not in self._queued:
                self._queued.add(job)
                self._queue.put_nowait(job)
                n += 1
        await self._queue.join()
        return n

    async def run(self, stop: asyncio.Event) -> None:
        workers = [asyncio.create_task(self._worker(stop), name=f"wallet-worker-{i}") for i in range(self.s.reconcile_workers)]
        log.info("Monitoring scheduler started", workers=self.s.reconcile_workers, sweep_every=f"{self.s.reconcile_interval_seconds}s")
        try:
            while not stop.is_set():
                started = time.monotonic()
                n = await self.sweep_once()
                self.sweeps += 1
                self.last_sweep_at = time.time()
                self.last_sweep_seconds = time.monotonic() - started
                log.info("Sweep complete", wallets=n, seconds=f"{self.last_sweep_seconds:.1f}", recovered_total=self.recovered)
                await sleep_or_stop(stop, self.s.reconcile_interval_seconds - self.last_sweep_seconds)
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    # ------------------------------------------------------------- core check
    async def check_wallet(
        self,
        address: str,
        direction: str,
        *,
        min_ms: int | None = None,
        max_ms: int | None = None,
        live: bool = True,
        source_name: str = SOURCE_RECONCILE,
        use_checkpoint: bool = True,
    ) -> int:
        """Fetch ``address``'s USDT transfers (in/out) and process the ones we lack.

        Returns the number of newly stored transfers.
        """
        key = checkpoint_key(address, direction)
        if min_ms is None:
            min_ms = await self._checkpoint_ms(key, address) - self.s.reconcile_overlap_seconds * 1000
        candidates: dict[tuple[str, str, str, int], int] = {}  # (tx, from, to, amount) -> ts
        newest = None
        fingerprint = None
        for _ in range(MAX_PAGES_PER_CHECK):
            items, fingerprint = await self.source.get_account_trc20(
                address, direction=direction, min_timestamp_ms=min_ms, max_timestamp_ms=max_ms, fingerprint=fingerprint, limit=200
            )
            for it in items:
                row = self._candidate(it)
                if row is None:
                    continue
                tx, frm, to, amount, ts = row
                newest = ts if newest is None else max(newest, ts)
                # outgoing from Wallet A: every amount counts (discovery has no minimum)
                # incoming to a monitored wallet: only potential alerts matter
                if direction == OUT or amount >= self.s.alert_min_base_units:
                    candidates[(tx, frm, to, amount)] = ts
            if not fingerprint or not items:
                break

        stored = 0
        unresolved_ts: list[int] = []
        if candidates:
            async with self.sf() as s:
                known = await repo.known_transfers(s, {c[0] for c in candidates})
            missing = {c: ts for c, ts in candidates.items() if c not in known}
            for (tx, frm, to, amount), ts in sorted(missing.items(), key=lambda kv: kv[1]):
                transfers = await self.resolve(tx, frm, to, amount)
                if not transfers:
                    unresolved_ts.append(ts)
                    log.warning("Could not resolve transfer yet; will retry", tx=tx, wallet=address)
                    continue
                res = await self.proc.process(transfers, source=source_name, live=live)
                stored += res.stored
            if source_name == SOURCE_RECONCILE and stored:
                self.recovered += stored
                log.warning("Scheduler recovered transfers missed by the live stream", wallet=address, stored=stored)

        if use_checkpoint and newest is not None:
            safe = min(unresolved_ts) - 1 if unresolved_ts else newest
            async with self.sf() as s, s.begin():
                await repo.advance_checkpoint(s, key, ms_to_dt(safe))
        return stored

    async def _checkpoint_ms(self, key: str, address: str) -> int:
        async with self.sf() as s, s.begin():
            cp = await repo.get_checkpoint(s, key)
            if cp is not None:
                return dt_to_ms(cp.last_timestamp)
            w = self.reg.get(address)
            start = max(self.proc.monitor_started_ms, w.discovered_at_ms if w and w.hop > 0 else 0)
            await repo.init_checkpoint(s, key, ms_to_dt(start))
            return start

    def _candidate(self, it: dict) -> tuple[str, str, str, int, int] | None:
        """Validate one /transactions/trc20 row (USDT only, valid fields)."""
        try:
            token = (it.get("token_info") or {}).get("address")
            if token and token != self.s.usdt_contract:
                return None
            if it.get("type") not in (None, "Transfer"):
                return None
            tx = str(it["transaction_id"]).lower()
            amount = int(str(it["value"]))
            ts = int(it["block_timestamp"])
            frm, to = str(it["from"]), str(it["to"])
            if len(tx) != 64 or amount <= 0:
                return None
            return tx, frm, to, amount, ts
        except (KeyError, TypeError, ValueError):
            return None

    async def resolve(self, tx: str, frm: str, to: str, amount: int) -> list[Transfer]:
        """Get the exact (tx_hash, event_index) form of a transfer seen via the account API."""
        transfers, _ = parse_events(await self.source.get_transaction_events(tx), self.s.usdt_contract)
        if not any(t.from_address == frm and t.to_address == to and t.amount_base_units == amount for t in transfers):
            # Fallback: decode the transaction's logs (log position = event index).
            transfers = parse_tx_info_logs(await self.source.get_transaction_info(tx), self.s.usdt_contract)
        return transfers

    # ------------------------------------------------------------- backfill
    async def run_backfill(self, stop: asyncio.Event) -> None:
        """Discover Wallet A's historical recipients (BACKFILL_LOOKBACK_DAYS); resumable.

        Wallets found this way are monitored for *future* incoming transfers;
        historical transfers never alert unless ALERT_ON_HISTORICAL=true.
        """
        async with self.sf() as s:
            if await repo.get_state(s, STATE_BACKFILL_DONE) == "1":
                return
            nxt = await repo.get_state(s, STATE_BACKFILL_NEXT)
        end = self.proc.monitor_started_ms
        start = int(nxt) if nxt else end - self.s.backfill_lookback_days * 86_400_000
        step = self.s.backfill_window_hours * 3_600_000
        self.backfill_running = True
        log.info("Backfill started", root=self.reg.root, start=ms_to_dt(start).isoformat(), end=ms_to_dt(end).isoformat())
        discovered_before = len(self.reg.monitored())
        try:
            t = start
            while t < end and not stop.is_set():
                w_end = min(t + step, end)
                for root in [w.address for w in self.reg.expanders() if w.hop == 0]:
                    while not stop.is_set():
                        try:
                            await self.check_wallet(
                                root, OUT, min_ms=t, max_ms=w_end - 1, live=False, source_name=SOURCE_BACKFILL, use_checkpoint=False
                            )
                            break
                        except Exception as exc:  # noqa: BLE001 - retry the window
                            log.error("Backfill window failed; retrying", error=str(exc)[:150])
                            await sleep_or_stop(stop, 10)
                if stop.is_set():
                    return
                async with self.sf() as s, s.begin():
                    await repo.set_state(s, STATE_BACKFILL_NEXT, str(w_end))
                log.info("Backfill progress", reached=ms_to_dt(w_end).isoformat(), monitored=len(self.reg.monitored()))
                t = w_end
            async with self.sf() as s, s.begin():
                await repo.set_state(s, STATE_BACKFILL_DONE, "1")
            log.info("Backfill complete", discovered=len(self.reg.monitored()) - discovered_before)
        finally:
            self.backfill_running = False
