"""Polling loop: scan TronGrid -> queue candidates -> verify on-chain -> Telegram outbox."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from outflow_alert.address import InvalidAddress, normalize_address, short
from outflow_alert.amounts import format_units, match_target, parse_raw_value
from outflow_alert.config import Settings
from outflow_alert.storage import Storage, now_ms
from outflow_alert.telegram import SendError, TelegramClient, format_alert
from outflow_alert.tron_client import TronClient, TronError
from outflow_alert.verifier import Verdict, evaluate_transaction_info

log = logging.getLogger(__name__)

_BINDING_KEYS = ("wallet", "contract")


class StateMismatch(RuntimeError):
    pass


def _backoff_ms(attempts: int, base_ms: int, cap_ms: int) -> int:
    return min(cap_ms, base_ms * (2 ** min(attempts, 16)))


class Monitor:
    def __init__(self, settings: Settings, storage: Storage, tron: TronClient, telegram: TelegramClient) -> None:
        self.s = settings
        self.db = storage
        self.tron = tron
        self.tg = telegram
        self._outbox_event = asyncio.Event()
        self._stop = asyncio.Event()
        self.last_scan_ok_ms: int | None = None

    # ------------------------------------------------------------- lifecycle
    def bind_state(self) -> None:
        """Pin the database to one wallet/contract and initialise the cursor on first start."""
        expected = {"wallet": self.s.wallet, "contract": self.s.usdt_contract}
        for key in _BINDING_KEYS:
            stored = self.db.get(key)
            if stored is None:
                self.db.set(key, expected[key])
            elif stored != expected[key]:
                raise StateMismatch(
                    f"database {self.s.db_path} belongs to {key}={stored}, configured {expected[key]}; "
                    "use a different DB_PATH"
                )
        if self.db.get_cursor() is None:
            start = now_ms() - self.s.initial_backfill_seconds * 1000
            self.db.advance_cursor(start)
            log.info("first start: scanning from %s ms (backfill %ss)", start, self.s.initial_backfill_seconds)

    def stop(self) -> None:
        self._stop.set()
        self._outbox_event.set()

    # ------------------------------------------------------------------ scan
    def _accept_listing_item(self, item: dict) -> tuple[str, int] | None:
        """Cheap pre-filter on TronGrid listing data.  Returns (txid, block_ts) for candidates."""
        try:
            txid = str(item["transaction_id"]).lower()
            token = item.get("token_info") or {}
            if normalize_address(str(token.get("address", ""))) != self.s.usdt_contract:
                return None
            if normalize_address(str(item.get("from", ""))) != self.s.wallet:
                return None
            if item.get("type", "Transfer") != "Transfer":
                return None
            amount = parse_raw_value(item.get("value"))
            block_ts = int(item.get("block_timestamp") or 0)
        except (KeyError, ValueError, TypeError, InvalidAddress) as exc:
            log.warning("skipping malformed listing item %s: %s", str(item)[:200], exc)
            return None
        if len(txid) != 64:
            return None
        if match_target(amount, self.s.amounts_raw, self.s.tolerance_raw) is None:
            return None
        return txid, block_ts

    async def scan(self) -> int:
        """Scan from ``cursor - lookback``; advance the cursor as pages complete.  Returns new candidates."""
        cursor = self.db.get_cursor() or now_ms()
        min_ts = max(0, cursor - self.s.lookback_seconds * 1000)
        fingerprint: str | None = None
        new = 0
        for _ in range(self.s.max_pages_per_poll):
            items, fingerprint = await self.tron.list_outgoing_trc20(
                self.s.wallet, self.s.usdt_contract, min_timestamp_ms=min_ts,
                limit=self.s.page_limit, fingerprint=fingerprint,
            )
            page_max_ts = 0
            for item in items:
                accepted = self._accept_listing_item(item)
                ts = int(item.get("block_timestamp") or 0) if isinstance(item, dict) else 0
                page_max_ts = max(page_max_ts, ts)
                if accepted and self.db.add_candidate(*accepted):
                    new += 1
                    log.info("candidate %s (block_ts=%s) queued for verification", accepted[0], accepted[1])
            # Results are ordered by block_timestamp ascending, so every item up to this page's
            # max timestamp has been seen.  The lookback window re-covers late-indexed entries.
            if page_max_ts:
                self.db.advance_cursor(page_max_ts)
            if not fingerprint:
                break
        else:
            log.warning("scan stopped after %s pages; continuing next poll", self.s.max_pages_per_poll)
        self.last_scan_ok_ms = now_ms()
        return new

    # ---------------------------------------------------------------- verify
    async def verify_due(self) -> int:
        """Verify queued candidates against gettransactioninfobyid.  Returns alerts queued."""
        queued = 0
        solidified = self.s.confirmation_mode == "solidified"
        for cand in self.db.due_candidates():
            try:
                info = await self.tron.get_transaction_info(cand.txid, solidified=solidified)
            except TronError as exc:
                self.db.reschedule_candidate(cand.txid, _backoff_ms(cand.attempts, 1000, 60_000), str(exc))
                log.warning("verify %s failed: %s", cand.txid, exc)
                continue
            result = evaluate_transaction_info(
                cand.txid, info, wallet=self.s.wallet, contract=self.s.usdt_contract,
                targets=self.s.amounts_raw, tolerance_raw=self.s.tolerance_raw,
            )
            if result.verdict is Verdict.NOT_FOUND:
                if now_ms() - cand.first_seen > self.s.verify_max_age_seconds * 1000:
                    self.db.finish_candidate(cand.txid, "expired", result.reason)
                    log.error("candidate %s never confirmed within %ss; dropped", cand.txid, self.s.verify_max_age_seconds)
                else:
                    self.db.reschedule_candidate(cand.txid, _backoff_ms(cand.attempts, 1000, 15_000), result.reason)
                continue
            if result.verdict is Verdict.REJECTED:
                self.db.finish_candidate(cand.txid, "rejected", result.reason)
                log.info("candidate %s rejected: %s", cand.txid, result.reason)
                continue
            alerts = [
                (m, chat, format_alert(m, decimals=self.s.token_decimals, confirmation_mode=self.s.confirmation_mode,
                                       tx_url=self.s.explorer_tx_url, address_url=self.s.explorer_address_url))
                for m in result.matches
                for chat in self.s.telegram_chat_ids
            ]
            queued += self.db.finish_candidate(cand.txid, "alerted", None, result.matches, alerts)
            for m in result.matches:
                log.info("MATCH %s log#%s %s USDT -> %s", m.txid, m.log_index,
                         format_units(m.amount_raw, self.s.token_decimals), short(m.to_address))
        if queued:
            self._outbox_event.set()
        return queued

    # --------------------------------------------------------------- deliver
    async def deliver_due(self) -> int:
        sent = 0
        for item in self.db.due_outbox():
            try:
                await self.tg.send_message(item.chat_id, item.text)
            except SendError as exc:
                if exc.retry_after is not None:
                    delay = int(exc.retry_after * 1000) + 250
                elif exc.fatal:
                    delay = 300_000
                else:
                    delay = _backoff_ms(item.attempts, 2000, 300_000)
                self.db.outbox_retry(item.id, delay, str(exc))
                log.warning("telegram delivery to %s failed (attempt %s): %s", item.chat_id, item.attempts + 1, exc)
                if exc.retry_after is not None:
                    break  # rate limited: back off the whole queue
                continue
            self.db.outbox_sent(item.id)
            sent += 1
            log.info("alert for %s log#%s delivered to %s", item.txid, item.log_index, item.chat_id)
        return sent

    # ----------------------------------------------------------------- loops
    async def poll_once(self) -> None:
        try:
            await self.scan()
        except TronError as exc:
            log.warning("scan failed: %s", exc)
        await self.verify_due()

    def _heartbeat(self) -> None:
        if self.s.heartbeat_file:
            try:
                Path(self.s.heartbeat_file).write_text(str(int(time.time())))
            except OSError as exc:
                log.debug("heartbeat write failed: %s", exc)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def poll_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.poll_once()
                self._heartbeat()
            except Exception:  # noqa: BLE001 - keep the monitor alive on unexpected errors
                log.exception("unexpected error in poll loop")
            await self._sleep(max(0.0, self.s.poll_interval_seconds - (time.monotonic() - started)))

    async def delivery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.deliver_due()
            except Exception:  # noqa: BLE001
                log.exception("unexpected error in delivery loop")
            nxt = self.db.next_outbox_due_ms()
            wait = 30.0 if nxt is None else max(0.2, (nxt - now_ms()) / 1000)
            self._outbox_event.clear()
            try:
                await asyncio.wait_for(self._outbox_event.wait(), timeout=min(wait, 30.0))
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.poll_loop()), asyncio.create_task(self.delivery_loop())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
