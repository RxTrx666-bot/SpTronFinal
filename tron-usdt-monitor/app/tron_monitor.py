"""Transaction detection engine.

Pipeline for every candidate transfer:

    TRON API data -> parser (strict validation) -> TransferFilter (contract, token,
    wallet, amount range) -> duplicate check (DB, UNIQUE tx_hash) -> insert as
    'pending' -> AlertDispatcher (Telegram) -> mark 'sent'

Two monitoring strategies are available (MONITOR_MODE):

* ``account`` (default, TronGrid): poll TronGrid's indexed TRC-20 transfer list of the
  wallet for the USDT contract. Every candidate that passes the filter is then
  verified against the raw on-chain Transfer event log (gettransactioninfobyid),
  which also yields the block number and confirms the tx succeeded.
* ``blocks`` (any java-tron full node): read every new block's transaction receipts
  and decode the USDT Transfer event logs directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from app.config import Settings
from app.database import NewTransaction, Repository
from app.filters import TransferFilter, format_token_amount
from app.logger import kv
from app.stats import MonitorStats
from app.timeutil import format_utc, now_ms
from app.transaction_parser import (
    MalformedTransactionError,
    TokenTransfer,
    parse_transfer_logs,
    parse_trongrid_trc20_record,
)
from app.tron_client import TronApiError, TronClient
from app.tx_limit import TxLimitTracker

log = logging.getLogger(__name__)

STATE_ACCOUNT_CURSOR = "account.cursor_ms"
STATE_ACCOUNT_FLOOR = "account.floor_ms"
STATE_LAST_BLOCK = "blocks.last_block"
STATE_BACKFILL_DONE = "backfill.done"


class AlertSink(Protocol):
    async def enqueue(self, tx_hash: str) -> None: ...


class TransactionProcessor:
    """Final authority: filter + duplicate-safe persistence + alert hand-off."""

    def __init__(
        self,
        settings: Settings,
        transfer_filter: TransferFilter,
        repo: Repository,
        alerts: AlertSink,
        stats: MonitorStats,
        clock: Callable[[], int] = now_ms,
        limit_tracker: "TxLimitTracker | None" = None,
    ) -> None:
        self.limit_tracker = limit_tracker
        self.settings = settings
        self.filter = transfer_filter
        self.repo = repo
        self.alerts = alerts
        self.stats = stats
        self.clock = clock

    async def process(self, transfer: TokenTransfer, *, backfill: bool = False) -> bool:
        """Returns True when a new matching transaction was stored and queued for alert."""
        decision = self.filter.evaluate(transfer)
        if not decision.matched or decision.direction is None:
            log.debug("transfer_ignored", extra=kv(tx_hash=transfer.tx_hash, reason=decision.reason))
            return False
        if self.limit_tracker is not None and self.limit_tracker.paused and not backfill:
            log.info("transfer_ignored_paused", extra=kv(tx_hash=transfer.tx_hash))
            return False
        if await self.repo.exists(transfer.tx_hash):
            log.info("duplicate_transaction_ignored", extra=kv(tx_hash=transfer.tx_hash))
            return False
        detected_ms = self.clock()
        amount = format_token_amount(transfer.amount_raw, self.settings.token_decimals)
        inserted = await self.repo.insert_transaction(
            NewTransaction(
                tx_hash=transfer.tx_hash,
                block_number=transfer.block_number,
                block_timestamp_ms=transfer.block_timestamp_ms,
                direction=decision.direction.value,
                sender=transfer.sender,
                recipient=transfer.recipient,
                amount_raw=transfer.amount_raw,
                amount_usdt=amount,
                contract_address=transfer.contract_address,
                token_symbol=self.settings.token_symbol,
                detected_at_ms=detected_ms,
                is_backfill=backfill,
                source=transfer.source,
            )
        )
        if not inserted:  # lost a race with another insert of the same tx_hash
            log.info("duplicate_transaction_ignored", extra=kv(tx_hash=transfer.tx_hash, stage="insert"))
            return False
        latency_ms = detected_ms - transfer.block_timestamp_ms
        if not backfill:
            self.stats.last_detection_latency_ms = latency_ms
        self.stats.matches_this_session += 1
        log.info(
            "matching_transaction_detected",
            extra=kv(
                tx_hash=transfer.tx_hash,
                direction=decision.direction.value,
                amount_usdt=amount,
                sender=transfer.sender,
                recipient=transfer.recipient,
                block=transfer.block_number,
                block_time=format_utc(transfer.block_timestamp_ms, with_millis=True),
                detected=format_utc(detected_ms, with_millis=True),
                detection_latency_s=f"{latency_ms / 1000:.3f}",
                source=transfer.source,
                backfill=backfill,
            ),
        )
        if latency_ms < 0 and not backfill:
            log.warning("negative_latency_check_server_clock_ntp", extra=kv(latency_ms=latency_ms))
        await self.alerts.enqueue(transfer.tx_hash)
        if self.limit_tracker is not None and not backfill:
            await self.limit_tracker.check()
        return True


class BaseMonitor:
    def __init__(
        self,
        settings: Settings,
        client: TronClient,
        repo: Repository,
        processor: TransactionProcessor,
        stats: MonitorStats,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repo = repo
        self.processor = processor
        self.filter = processor.filter
        self.stats = stats
        self._sleep = sleep
        self._heartbeat = heartbeat
        stats.mode = settings.monitor_mode
        self._resume_generation = 0

    async def reanchor_now(self) -> None:
        """After a pause, continue from the current chain position (skip the paused period)."""
        raise NotImplementedError

    async def initialize(self) -> None:
        raise NotImplementedError

    async def poll_once(self) -> bool:
        """One monitoring iteration. Returns True if more work is immediately pending."""
        raise NotImplementedError

    async def _maybe_backfill(self, before_ms: int) -> None:
        if not self.settings.backfill_enabled or await self.repo.get_state(STATE_BACKFILL_DONE):
            return
        try:
            count = await run_backfill(self.settings, self.client, self.processor, before_ms)
        except TronApiError as exc:
            log.error(
                "backfill_failed_will_retry_next_start",
                extra=kv(error=str(exc), hint="backfill needs the TronGrid /v1 API"),
            )
            return
        await self.repo.set_state(STATE_BACKFILL_DONE, str(now_ms()))
        log.info("backfill_complete", extra=kv(alerts_queued=count))

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                tracker = self.processor.limit_tracker
                if tracker is not None:
                    await tracker.apply_control()  # server-side `python -m app.control start|stop`
                if tracker is not None and tracker.paused:
                    # Paused (at startup or after the limit): no polling, no alerts until started.
                    self.stats.paused = True
                    if self._heartbeat:
                        self._heartbeat()
                    await self._wait(stop, max(1.0, self.settings.poll_interval_seconds))
                    continue
                self.stats.paused = False
                if tracker is not None and tracker.resume_generation != self._resume_generation:
                    generation = tracker.resume_generation
                    await self.reanchor_now()  # may raise -> retried next loop
                    self._resume_generation = generation
                    self.stats.initialized = False
                if not self.stats.initialized:
                    await self.initialize()
                    self.stats.initialized = True
                    log.info("monitoring_started", extra=kv(mode=self.settings.monitor_mode,
                                                            wallet=self.settings.wallet_address))
                started = time.perf_counter()
                more_pending = await self.poll_once()
                self.stats.poll_succeeded()
                if self._heartbeat:
                    self._heartbeat()
                backoff = 1.0
                if more_pending:
                    continue
                elapsed = time.perf_counter() - started
                await self._wait(stop, max(0.0, self.settings.poll_interval_seconds - elapsed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one failure stop monitoring
                self.stats.poll_failed(f"{type(exc).__name__}: {exc}")
                level = logging.WARNING if isinstance(exc, TronApiError) else logging.ERROR
                log.log(
                    level,
                    "monitor_poll_failed",
                    extra=kv(error=str(exc), error_type=type(exc).__name__,
                             consecutive=self.stats.consecutive_errors, retry_in_s=backoff),
                    exc_info=not isinstance(exc, TronApiError),
                )
                await self._wait(stop, backoff)
                backoff = min(backoff * 2, 60.0)
                if self.stats.consecutive_errors and self.stats.consecutive_errors % 5 == 0:
                    log.info("reconnecting_tron_api", extra=kv(consecutive_errors=self.stats.consecutive_errors))

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        if self._sleep is not asyncio.sleep:
            await self._sleep(seconds)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class AccountMonitor(BaseMonitor):
    """Polls TronGrid's indexed TRC-20 history of the wallet, verifies via event logs."""

    PAGE_LIMIT = 200

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cursor_ms = 0
        self._floor_ms = 0
        self._seen: dict[tuple[str, str, str, int], int] = {}  # evaluated (non-pending) records
        self._pending_since: dict[str, int] = {}  # tx_hash -> first time verification was pending

    async def _chain_now_ms(self) -> int:
        try:
            _, ts = await self.client.get_head_block(solidity=self.settings.confirmed_only)
            return ts
        except TronApiError as exc:
            log.warning("head_block_unavailable_using_local_clock", extra=kv(error=str(exc)))
            return now_ms()

    async def reanchor_now(self) -> None:
        start = await self._chain_now_ms()
        await self.repo.set_state(STATE_ACCOUNT_FLOOR, str(start))
        await self.repo.set_state(STATE_ACCOUNT_CURSOR, str(start))
        self._seen.clear()
        self._pending_since.clear()
        log.info("monitoring_point_reanchored_after_resume", extra=kv(from_time=format_utc(start)))

    async def initialize(self) -> None:
        cursor = await self.repo.get_state(STATE_ACCOUNT_CURSOR)
        floor = await self.repo.get_state(STATE_ACCOUNT_FLOOR)
        if cursor is None or floor is None:
            start = await self._chain_now_ms()
            await self.repo.set_state(STATE_ACCOUNT_FLOOR, str(start))
            await self.repo.set_state(STATE_ACCOUNT_CURSOR, str(start))
            self._cursor_ms = self._floor_ms = start
            log.info("monitoring_point_established", extra=kv(from_time=format_utc(start)))
        else:
            self._cursor_ms, self._floor_ms = int(cursor), int(floor)
            log.info(
                "resuming_from_cursor",
                extra=kv(cursor=format_utc(self._cursor_ms), gap_s=round((now_ms() - self._cursor_ms) / 1000)),
            )
        self.stats.cursor_ms = self._cursor_ms
        await self._maybe_backfill(self._floor_ms)

    async def poll_once(self) -> bool:
        lookback_s = self.settings.lookback_seconds
        if self.settings.verify_event_log:
            # re-fetch window must outlive the verification wait, or pending txs could be dropped
            lookback_s = max(lookback_s, self.settings.verify_timeout_seconds + 30)
        lookback_ms = lookback_s * 1000
        min_ts = max(self._floor_ms, self._cursor_ms - lookback_ms)
        fingerprint: str | None = None
        max_seen = self._cursor_ms
        pages = 0
        more_pending = False
        while True:
            records, fingerprint = await self.client.get_trc20_transfers(
                self.settings.wallet_address,
                self.settings.usdt_contract,
                min_timestamp=min_ts,
                order="asc",
                limit=self.PAGE_LIMIT,
                only_confirmed=self.settings.confirmed_only,
                fingerprint=fingerprint,
                only_from=self.settings.outgoing_only,
            )
            pages += 1
            for record in records:
                ts = await self._handle_record(record)
                if ts is not None:
                    max_seen = max(max_seen, ts)
            if not fingerprint or len(records) < self.PAGE_LIMIT:
                break
            if pages >= self.settings.max_pages_per_poll:
                more_pending = True  # large catch-up: continue next iteration immediately
                break
        if max_seen > self._cursor_ms:
            self._cursor_ms = max_seen
            await self.repo.set_state(STATE_ACCOUNT_CURSOR, str(max_seen))
        self.stats.cursor_ms = self._cursor_ms
        self._prune(min_ts)
        log.debug("poll_complete", extra=kv(pages=pages, cursor=format_utc(self._cursor_ms)))
        return more_pending

    def _prune(self, min_ts: int) -> None:
        horizon = min_ts - 60_000
        for key in [k for k, ts in self._seen.items() if ts < horizon]:
            del self._seen[key]

    async def _handle_record(self, record: Any) -> int | None:
        try:
            transfer = parse_trongrid_trc20_record(record)
        except MalformedTransactionError as exc:
            tx = record.get("transaction_id") if isinstance(record, dict) else None
            log.warning("malformed_record_skipped", extra=kv(tx_hash=tx, error=str(exc)))
            return None
        if transfer.block_timestamp_ms < self._floor_ms:
            return transfer.block_timestamp_ms
        key = (transfer.tx_hash, transfer.sender, transfer.recipient, transfer.amount_raw)
        if key in self._seen:
            return transfer.block_timestamp_ms
        self.stats.transfers_checked += 1
        self.stats.last_checked_tx_hash = transfer.tx_hash
        self.stats.last_checked_tx_time_ms = transfer.block_timestamp_ms

        decision = self.filter.evaluate(transfer)
        if not decision.matched:
            log.debug("transfer_ignored", extra=kv(tx_hash=transfer.tx_hash, reason=decision.reason))
            self._seen[key] = transfer.block_timestamp_ms
            return transfer.block_timestamp_ms
        if await self.repo.exists(transfer.tx_hash):
            log.debug("duplicate_transaction_ignored", extra=kv(tx_hash=transfer.tx_hash))
            self._seen[key] = transfer.block_timestamp_ms
            return transfer.block_timestamp_ms

        final = transfer
        if self.settings.verify_event_log:
            verified = await self._verify(transfer)
            if verified is None:  # receipt not yet available -> retried next poll
                return transfer.block_timestamp_ms
            if verified is False:
                self._seen[key] = transfer.block_timestamp_ms
                return transfer.block_timestamp_ms
            final = verified
        self._seen[key] = transfer.block_timestamp_ms
        self._pending_since.pop(transfer.tx_hash, None)
        await self.processor.process(final)
        return transfer.block_timestamp_ms

    async def _verify(self, candidate: TokenTransfer) -> TokenTransfer | None | bool:
        """Confirm the candidate against the on-chain Transfer event log.

        Returns the event-log transfer on success, None if the receipt is not yet
        available (retry later), False if the chain data contradicts the index.
        """
        info = await self.client.get_transaction_info(candidate.tx_hash, solidity=self.settings.confirmed_only)
        if not info or "id" not in info:
            first = self._pending_since.setdefault(candidate.tx_hash, now_ms())
            waited_s = (now_ms() - first) / 1000
            if waited_s < self.settings.verify_timeout_seconds:
                log.debug("verification_pending", extra=kv(tx_hash=candidate.tx_hash, waited_s=waited_s))
                return None
            log.warning(
                "verification_timeout_using_indexed_data",
                extra=kv(tx_hash=candidate.tx_hash, waited_s=round(waited_s, 1)),
            )
            return candidate
        try:
            logs = parse_transfer_logs(info, contract=self.settings.usdt_contract)
        except MalformedTransactionError as exc:
            log.warning("verification_malformed_receipt", extra=kv(tx_hash=candidate.tx_hash, error=str(exc)))
            return False
        for event in logs:
            if (
                event.sender == candidate.sender
                and event.recipient == candidate.recipient
                and event.amount_raw == candidate.amount_raw
            ):
                return event.with_metadata(
                    token_symbol=self.settings.token_symbol, token_decimals=self.settings.token_decimals
                )
        log.warning(
            "verification_failed_no_matching_event",
            extra=kv(tx_hash=candidate.tx_hash, events_found=len(logs)),
        )
        return False


class BlockMonitor(BaseMonitor):
    """Scans every new block's receipts and decodes USDT Transfer event logs."""

    async def reanchor_now(self) -> None:
        head, head_ts = await self.client.get_head_block(solidity=self.settings.confirmed_only)
        await self.repo.set_state(STATE_LAST_BLOCK, str(head))
        log.info("monitoring_point_reanchored_after_resume", extra=kv(block=head, from_time=format_utc(head_ts)))

    async def initialize(self) -> None:
        last = await self.repo.get_state(STATE_LAST_BLOCK)
        if last is None:
            head, head_ts = await self.client.get_head_block(solidity=self.settings.confirmed_only)
            await self.repo.set_state(STATE_LAST_BLOCK, str(head))
            self._last = head
            log.info("monitoring_point_established", extra=kv(block=head, from_time=format_utc(head_ts)))
            await self._maybe_backfill(head_ts)
        else:
            self._last = int(last)
            log.info("resuming_from_block", extra=kv(block=self._last))
            await self._maybe_backfill(now_ms())
        self.stats.last_block_checked = self._last

    async def poll_once(self) -> bool:
        head, _ = await self.client.get_head_block(solidity=self.settings.confirmed_only)
        self.stats.head_block = head
        if head <= self._last:
            return False
        end = min(head, self._last + self.settings.max_blocks_per_poll)
        numbers = list(range(self._last + 1, end + 1))
        step = self.settings.block_fetch_concurrency
        for i in range(0, len(numbers), step):
            batch = numbers[i : i + step]
            results = await asyncio.gather(
                *(self.client.get_transaction_info_by_block(n, solidity=self.settings.confirmed_only) for n in batch)
            )
            for number, infos in zip(batch, results):
                await self._process_block(number, infos)
                self._last = number
                self.stats.last_block_checked = number
                await self.repo.set_state(STATE_LAST_BLOCK, str(number))
        behind = head - self._last
        if behind > 0:
            log.info("catching_up_blocks", extra=kv(last=self._last, head=head, behind=behind))
        return behind > 0

    async def _process_block(self, number: int, infos: list[Any]) -> None:
        relevant = 0
        for info in infos:
            try:
                transfers = parse_transfer_logs(
                    info, contract=self.settings.usdt_contract, wallet=self.settings.wallet_address
                )
            except MalformedTransactionError as exc:
                log.warning("malformed_transaction_skipped", extra=kv(block=number, error=str(exc)))
                continue
            for transfer in transfers:
                relevant += 1
                self.stats.transfers_checked += 1
                self.stats.last_checked_tx_hash = transfer.tx_hash
                self.stats.last_checked_tx_time_ms = transfer.block_timestamp_ms
                await self.processor.process(transfer)
        log.debug("block_checked", extra=kv(block=number, txs=len(infos), wallet_usdt_transfers=relevant))


async def run_backfill(
    settings: Settings, client: TronClient, processor: TransactionProcessor, before_ms: int
) -> int:
    """Scan history (newest first) and alert up to BACKFILL_LIMIT matches, oldest first."""
    log.info("backfill_started", extra=kv(limit=settings.backfill_limit, max_pages=settings.backfill_max_pages))
    matches: list[TokenTransfer] = []
    fingerprint: str | None = None
    pages = scanned = 0
    while len(matches) < settings.backfill_limit and pages < settings.backfill_max_pages:
        records, fingerprint = await client.get_trc20_transfers(
            settings.wallet_address,
            settings.usdt_contract,
            max_timestamp=before_ms,
            order="desc",
            limit=200,
            only_confirmed=True,
            fingerprint=fingerprint,
            only_from=settings.outgoing_only,
        )
        pages += 1
        for record in records:
            scanned += 1
            try:
                transfer = parse_trongrid_trc20_record(record)
            except MalformedTransactionError:
                continue
            if processor.filter.evaluate(transfer).matched:
                matches.append(transfer)
                if len(matches) >= settings.backfill_limit:
                    break
        if not fingerprint or not records:
            break
    log.info("backfill_scan_done", extra=kv(pages=pages, scanned=scanned, matches=len(matches)))
    count = 0
    for transfer in reversed(matches):
        if await processor.process(transfer, backfill=True):
            count += 1
    return count


def build_monitor(
    settings: Settings,
    client: TronClient,
    repo: Repository,
    processor: TransactionProcessor,
    stats: MonitorStats,
    heartbeat: Callable[[], None] | None = None,
) -> BaseMonitor:
    cls = AccountMonitor if settings.monitor_mode == "account" else BlockMonitor
    return cls(settings, client, repo, processor, stats, heartbeat=heartbeat)
