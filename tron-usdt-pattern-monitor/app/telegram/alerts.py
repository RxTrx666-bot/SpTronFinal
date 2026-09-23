"""Alert delivery (outbox pattern).

Alerts are first written to the ``alerts`` table inside the same database
transaction that detected them (unique ``dedup_key`` => never duplicated).
``AlertDispatcher`` runs as an independent task and delivers pending alerts,
highest priority first (the known-test alert always jumps the queue), with
retries + exponential backoff and Telegram ``retry_after`` handling.

Delivery is at-least-once: if the process dies in the milliseconds between
Telegram accepting a message and the row being marked SENT, the message is
re-sent after restart.  An alert *record* is never created twice.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import Settings
from app.database import repository as repo
from app.database.session import is_transient_db_error
from app.domain import AlertStatus
from app.logging_setup import get_logger

log = get_logger(__name__)


class SendError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class AlertSink(Protocol):
    async def send(self, text: str) -> str | None: ...


class ConsoleSink:
    """Used when Telegram is not configured (and in simulation)."""

    def __init__(self, printer=print) -> None:
        self.printer = printer

    async def send(self, text: str) -> str | None:
        import re

        plain = re.sub(r"<[^>]+>", "", text)
        self.printer("\n" + "═" * 64 + "\n" + plain + "\n" + "═" * 64)
        return None


class RecordingSink:
    """Collects messages in memory (tests); can be told to fail N times."""

    def __init__(self, fail_times: int = 0) -> None:
        self.messages: list[str] = []
        self.fail_times = fail_times
        self.calls = 0

    async def send(self, text: str) -> str | None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise SendError("simulated telegram outage", retryable=True)
        self.messages.append(text)
        return str(len(self.messages))


class CompositeSink:
    def __init__(self, *sinks: AlertSink) -> None:
        self.sinks = sinks

    async def send(self, text: str) -> str | None:
        mid = None
        for s in self.sinks:
            r = await s.send(text)
            mid = mid or r
        return mid


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlertDispatcher:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker, sink: AlertSink) -> None:
        self.s = settings
        self.sf = session_factory
        self.sink = sink
        self._wake = asyncio.Event()
        self.sent = 0
        self.failed = 0

    def wake(self) -> None:
        self._wake.set()

    async def recover(self) -> None:
        async with self.sf() as s, s.begin():
            n = await repo.reset_stuck_alerts(s)
        if n:
            log.warning("Re-queued alerts interrupted by a previous shutdown", count=n)

    async def send_one(self, *, ignore_schedule: bool = False) -> bool:
        """Deliver the highest-priority due alert. Returns False when none is due."""
        async with self.sf() as s, s.begin():
            alert = await repo.next_due_alert(s, _utcnow(), ignore_schedule=ignore_schedule)
            if alert is None:
                return False
            alert.status = AlertStatus.SENDING.value
            alert.attempts += 1
            alert_id, text, attempts = alert.id, alert.message_text, alert.attempts
        start = _utcnow()
        try:
            message_id = await self.sink.send(text)
        except Exception as exc:  # noqa: BLE001
            retryable = getattr(exc, "retryable", True)
            retry_after = getattr(exc, "retry_after", None)
            delay = retry_after or min(
                self.s.alert_retry_max_seconds, self.s.alert_retry_base_seconds * 2 ** (attempts - 1)
            ) * (0.75 + random.random() / 2)
            give_up = (not retryable) or attempts >= self.s.alert_max_attempts
            async with self.sf() as s, s.begin():
                a = await s.get(repo.Alert, alert_id)
                a.status = AlertStatus.FAILED.value if give_up else AlertStatus.PENDING.value
                a.last_error = f"{type(exc).__name__}: {exc}"[:500]
                a.next_attempt_at = _utcnow() + timedelta(seconds=delay)
            if give_up:
                self.failed += 1
                log.error("Telegram alert permanently failed", alert_id=alert_id, attempts=attempts, error=str(exc)[:200])
            else:
                log.warning("Telegram send failed; will retry", alert_id=alert_id, attempt=attempts, retry_in=f"{delay:.1f}s", error=str(exc)[:200])
            return True
        end = _utcnow()
        async with self.sf() as s, s.begin():
            a = await s.get(repo.Alert, alert_id)
            a.status = AlertStatus.SENT.value
            a.sent_at = end
            a.telegram_message_id = message_id
            a.telegram_send_start_time = start
            a.telegram_send_end_time = end
            a.last_error = None
            if a.blockchain_event_time is not None:
                a.total_detection_latency_ms = int((end - a.blockchain_event_time).total_seconds() * 1000)
            atype, latency = a.alert_type, a.total_detection_latency_ms
            det = a.blockchain_detection_time
            ev = a.blockchain_event_time
            pstart, pend = a.processing_start_time, a.processing_end_time
        self.sent += 1
        fields = {"alert_id": alert_id, "type": atype, "telegram_ms": int((end - start).total_seconds() * 1000)}
        if latency is not None:
            fields["total_detection_latency_ms"] = latency
            if det and ev:
                fields["chain_to_detect_ms"] = int((det - ev).total_seconds() * 1000)
            if pstart and pend:
                fields["processing_ms"] = int((pend - pstart).total_seconds() * 1000)
        log.info("Telegram alert sent", **fields)
        return True

    async def flush(self, *, ignore_schedule: bool = True, max_messages: int = 10_000) -> int:
        n = 0
        while n < max_messages and await self.send_one(ignore_schedule=ignore_schedule):
            n += 1
        return n

    async def run(self, stop: asyncio.Event) -> None:
        await self.recover()
        while not stop.is_set():
            try:
                worked = await self.send_one()
            except Exception as exc:  # noqa: BLE001
                if is_transient_db_error(exc):
                    log.error("Database unavailable for alert dispatch; retrying", error=type(exc).__name__)
                else:
                    log.exception("Alert dispatcher error")
                worked = False
                await asyncio.sleep(2)
            if worked:
                await asyncio.sleep(0.05)  # stay under Telegram's per-chat rate limit
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
