"""Delivers the Telegram outbox (``alerts`` table).

* first delivery attempt happens immediately (the worker is woken by the
  detector right after the incident commits);
* failures (Telegram outage, rate limit, network) are retried with
  exponential backoff - alerts are never dropped and survive restarts;
* ``dedup_key`` uniqueness means an incident is announced once per chat;
* while alert delivery is globally paused (/pause) alerts stay queued.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app import repository as repo
from app.database import is_transient_db_error
from app.domain import AlertStatus
from app.models import Alert, PoisoningEvent
from app.services.notifier import CONSOLE_CHAT
from app.services.telegram_service import ConsoleTransport, SendError
from app.utils.clock import Clock, as_utc
from app.utils.logging import get_logger

log = get_logger(__name__)


class AlertWorker:
    def __init__(self, settings, session_factory, clock: Clock, transport, notifier, admin) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.t = transport
        self.notifier = notifier
        self.admin = admin
        self.console = ConsoleTransport(echo=True)
        self.sent = 0
        self.failed_attempts = 0
        self.last_error: str | None = None

    async def deliver_due(self) -> int:
        if self.admin.alerts_paused:
            return 0
        async with self.sf() as s:
            due = await repo.due_alerts(s, self.clock.now())
        n = 0
        for a in due:
            n += await self._deliver(a.id)
        return n

    async def _deliver(self, alert_id: int) -> int:
        async with self.sf() as s:
            a = await s.get(Alert, alert_id)
            if a is None or a.status != AlertStatus.PENDING.value:
                return 0
            payload, chat = dict(a.payload), a.chat_id
        transport = self.console if chat == CONSOLE_CHAT and not isinstance(self.t, ConsoleTransport) else self.t
        try:
            msg_id = await transport.send_message(chat, payload["text"], parse_mode=payload.get("parse_mode", "HTML"), reply_markup=payload.get("reply_markup"))
            error = None
        except SendError as exc:
            msg_id, error = None, exc
        except Exception as exc:  # noqa: BLE001 - unexpected transport failure: retry
            msg_id, error = None, SendError(f"{type(exc).__name__}: {exc}")
        now = self.clock.now()
        async with self.sf() as s, s.begin():
            a = await s.get(Alert, alert_id)
            if error is None:
                a.status = AlertStatus.SENT.value
                a.sent_at = now
                a.telegram_message_id = msg_id
                a.attempts += 1
                if a.event_id and a.alert_type in ("SUCCESS", "UPGRADE", "CANDIDATE", "ATTEMPT"):
                    ev = await s.get(PoisoningEvent, a.event_id)
                    if ev is not None and ev.alert_sent_at is None:
                        ev.alert_sent_at = now
                        ev.alert_latency_ms = max(0, int((now - as_utc(ev.detected_at)).total_seconds() * 1000))
                        log.info("ALERT_SENT", case=ev.case_id, victim=ev.victim_wallet, tx=ev.tx_hash, latency_ms=ev.alert_latency_ms, chat=chat)
                self.sent += 1
                return 1
            a.attempts += 1
            a.last_error = str(error)[:500]
            self.failed_attempts += 1
            self.last_error = str(error)[:200]
            if a.attempts >= self.s.alert_max_attempts:
                a.status = AlertStatus.FAILED.value
                log.error("ALERT_FAILED_PERMANENTLY", alert_id=alert_id, attempts=a.attempts, error=str(error)[:150])
            else:
                delay = error.retry_after or min(self.s.alert_retry_max_seconds, self.s.alert_retry_base_seconds * 2 ** min(a.attempts - 1, 12))
                a.next_attempt_at = now + timedelta(seconds=delay)
                log.warning("ALERT_DELIVERY_RETRY", alert_id=alert_id, attempt=a.attempts, retry_in=f"{delay:.1f}s", error=str(error)[:150])
        return 0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.deliver_due()
            except Exception as exc:  # noqa: BLE001
                if not is_transient_db_error(exc):
                    log.exception("ALERT_WORKER_ERROR", error=type(exc).__name__)
            self.notifier.wakeup.clear()
            try:
                await asyncio.wait_for(self.notifier.wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
