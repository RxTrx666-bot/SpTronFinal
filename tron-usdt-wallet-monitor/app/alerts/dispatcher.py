"""Alert engine -> Telegram (transactional outbox).

Alerts are first committed to the ``alerts`` table together with the transfer
that caused them (``engine.processor``).  This dispatcher then delivers
``pending`` rows:

    pending --claim--> sending --Telegram OK--> sent
                          └── Telegram error --> pending (retried with backoff)

* A Telegram outage never loses an alert: the row stays ``pending`` until it
  is delivered, across restarts.
* A row is claimed atomically, so two dispatchers can never both send it.
* The only window for a duplicate message is a crash *between* Telegram
  accepting the message and the ``sent`` commit (milliseconds).  Such a row is
  re-sent once on restart, explicitly marked "re-sent after a restart".
"""

from __future__ import annotations

import asyncio
import time

from app.alerts.formatter import discovery_message, large_transfer_message
from app.config import Settings
from app.db import repository as repo
from app.db.models import Alert
from app.domain import ALERT_DISCOVERY, utcnow
from app.logging_setup import get_logger
from app.monitor.loop import sleep_or_stop
from app.telegram.client import TelegramClient, TelegramError

log = get_logger(__name__)


class AlertDispatcher:
    def __init__(self, settings: Settings, session_factory, telegram: TelegramClient) -> None:
        self.s = settings
        self.sf = session_factory
        self.tg = telegram
        self._wake = asyncio.Event()
        self.sent = 0
        self.failures = 0
        self.last_error: str | None = None

    def wake(self) -> None:
        self._wake.set()

    async def recover(self) -> int:
        async with self.sf() as s, s.begin():
            n = await repo.requeue_interrupted(s)
        if n:
            log.warning("Re-queued alerts interrupted by a crash", count=n)
        return n

    def render(self, a: Alert) -> str:
        resent = a.last_error == "interrupted"
        if a.alert_type == ALERT_DISCOVERY:
            return discovery_message(a, resent=resent)
        return large_transfer_message(a, resent=resent)

    async def deliver_pending(self) -> int:
        """Deliver every pending alert once. Returns how many were sent."""
        async with self.sf() as s:
            alerts = await repo.pending_alerts(s)
        delivered = 0
        for a in alerts:
            async with self.sf() as s, s.begin():
                if not await repo.claim_alert(s, a.id):
                    continue  # someone else took it
            started = time.perf_counter()
            try:
                msg_id = await self.tg.send_message(self.s.telegram_admin_chat_id, self.render(a))
            except TelegramError as exc:
                self.failures += 1
                self.last_error = str(exc)
                async with self.sf() as s, s.begin():
                    await repo.mark_failed(s, a.id, str(exc))
                log.error("Telegram alert failed; will retry", alert_id=a.id, attempt=a.attempts + 1, error=str(exc)[:150])
                if exc.retry_after:
                    await asyncio.sleep(min(exc.retry_after, 60))
                raise
            async with self.sf() as s, s.begin():
                await repo.mark_sent(s, a.id, msg_id)
            delivered += 1
            self.sent += 1
            now = utcnow()
            log.info(
                "Telegram alert sent",
                alert_id=a.id,
                type=a.alert_type,
                tx=a.tx_hash,
                telegram_ms=f"{(time.perf_counter() - started) * 1000:.0f}",
                # chain timestamp -> detected -> sent
                chain_to_detect_ms=int((a.detected_at - a.transfer_timestamp).total_seconds() * 1000),
                detect_to_sent_ms=int((now - a.detected_at).total_seconds() * 1000),
                chain_to_sent_ms=int((now - a.transfer_timestamp).total_seconds() * 1000),
            )
        return delivered

    async def run(self, stop: asyncio.Event) -> None:
        await self.recover()
        backoff = 1.0
        while not stop.is_set():
            self._wake.clear()
            try:
                await self.deliver_pending()
                backoff = 1.0
            except TelegramError:
                await sleep_or_stop(stop, backoff)
                backoff = min(backoff * 2, 120)
                continue
            except Exception:  # noqa: BLE001 - DB down etc.
                log.exception("Alert dispatcher error; retrying")
                await sleep_or_stop(stop, backoff)
                backoff = min(backoff * 2, 60)
                continue
            # Sleep until woken by the processor or stopped (or poll every 5 s as a fallback).
            waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
            _, pending = await asyncio.wait(waiters, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            for w in pending:
                w.cancel()
