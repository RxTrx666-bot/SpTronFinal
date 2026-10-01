"""Confirmation tracking, pending-analysis recovery, system-log persistence, heartbeat."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update

from app.database import is_transient_db_error
from app.domain import Confirmation
from app.models import PoisoningEvent, SystemLog, Transaction
from app.services.tron_service import TronApiError
from app.utils.clock import Clock, as_utc, from_ms
from app.utils.logging import add_sink, get_logger, remove_sink
from app.utils.ratelimit import PRIORITY_DETECTION

log = get_logger(__name__)


class ConfirmationWorker:
    """Marks transfers CONFIRMED once solidified (irreversible) and incidents DROPPED if they never confirm."""

    def __init__(self, settings, session_factory, source, clock: Clock, notifier) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.notifier = notifier
        self.solid_block: int | None = None

    async def run_once(self) -> None:
        solid = await self.source.get_solid_block_number()
        self.solid_block = solid
        now = self.clock.now()
        drop_before = now - timedelta(minutes=self.s.drop_unconfirmed_after_minutes)
        async with self.sf() as s, s.begin():
            incident_tx = select(PoisoningEvent.transaction_id)
            await s.execute(
                update(Transaction)
                .where(
                    Transaction.confirmation_status == Confirmation.UNCONFIRMED.value,
                    Transaction.block_number.is_not(None),
                    Transaction.block_number <= solid,
                    Transaction.id.not_in(incident_tx),
                )
                .values(confirmation_status=Confirmation.CONFIRMED.value)
            )
        # Incidents and transfers without a block number: verify individually.
        async with self.sf() as s:
            events = (
                (await s.execute(select(PoisoningEvent).where(PoisoningEvent.confirmation_status == Confirmation.UNCONFIRMED.value).limit(50))).scalars().all()
            )
            loose = (
                (
                    await s.execute(
                        select(Transaction)
                        .where(Transaction.confirmation_status == Confirmation.UNCONFIRMED.value, Transaction.block_number.is_(None))
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
        alerts = False
        for ev in events:
            d = await self.source.get_transaction(ev.tx_hash, priority=PRIORITY_DETECTION)
            async with self.sf() as s, s.begin():
                e = await s.get(PoisoningEvent, ev.id)
                tx = await s.get(Transaction, ev.transaction_id)
                if d.confirmed and d.success is not False:
                    e.confirmation_status = tx.confirmation_status = Confirmation.CONFIRMED.value
                    if d.block_number:
                        e.block_number = tx.block_number = d.block_number
                    e.updated_at = now
                    log.info("INCIDENT_CONFIRMED", case=e.case_id, tx=e.tx_hash, block=e.block_number)
                elif (d.confirmed and d.success is False) or (not d.found and as_utc(e.detected_at) < drop_before):
                    e.confirmation_status = tx.confirmation_status = Confirmation.DROPPED.value
                    e.updated_at = now
                    log.warning("INCIDENT_TX_DROPPED", case=e.case_id, tx=e.tx_hash)
                    if not e.is_historical:
                        alerts |= bool(await self.notifier.event_alert(s, e.id, "DROPPED"))
        for t in loose:
            d = await self.source.get_transaction(t.tx_hash, priority=PRIORITY_DETECTION)
            async with self.sf() as s, s.begin():
                row = await s.get(Transaction, t.id)
                if d.block_number:
                    row.block_number = d.block_number
                if d.initiator and not row.initiator_address:
                    row.initiator_address = d.initiator
                if d.confirmed:
                    row.confirmation_status = Confirmation.CONFIRMED.value
                elif not d.found and as_utc(row.created_at) < drop_before:
                    row.confirmation_status = Confirmation.DROPPED.value
        if alerts:
            self.notifier.wake()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except TronApiError as exc:
                log.warning("TRON_API_UNAVAILABLE", component="confirmations", error=str(exc)[:120])
            except Exception as exc:  # noqa: BLE001
                if not is_transient_db_error(exc):
                    log.exception("CONFIRMATION_WORKER_ERROR", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.confirmation_check_interval_seconds)
            except asyncio.TimeoutError:
                pass


class RecoveryWorker:
    def __init__(self, settings, ingestor) -> None:
        self.s = settings
        self.ingestor = ingestor

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.pending_recovery_interval_seconds)
            except asyncio.TimeoutError:
                pass
            try:
                await self.ingestor.recover_pending(older_than_seconds=self.s.pending_recovery_interval_seconds)
            except Exception as exc:  # noqa: BLE001
                if not is_transient_db_error(exc):
                    log.exception("RECOVERY_ERROR", error=type(exc).__name__)


class SystemLogWriter:
    """Persists WARNING+ log records and explicit audit events to ``system_logs``."""

    def __init__(self, session_factory, clock: Clock, max_queue: int = 5000) -> None:
        self.sf = session_factory
        self.clock = clock
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0

    def install(self) -> None:
        add_sink(self._sink)

    def uninstall(self) -> None:
        remove_sink(self._sink)

    def _put(self, row: dict) -> None:
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1

    def _sink(self, record: logging.LogRecord, message: str) -> None:
        fields = getattr(record, "fields", {}) or {}
        if record.name.startswith("sqlalchemy") or record.name.startswith("app.workers.maintenance"):
            return
        self._put(
            {
                "ts": from_ms(int(record.created * 1000)),
                "level": record.levelname,
                "event_type": message[:64],
                "wallet": str(fields.get("victim") or fields.get("wallet") or "")[:34] or None,
                "tx_hash": str(fields.get("tx") or "")[:64] or None,
                "message": message,
                "data": {k: str(v)[:300] for k, v in fields.items()},
            }
        )

    def audit(self, event_type: str, message: str, *, wallet: str | None = None, tx_hash: str | None = None, data: dict | None = None) -> None:
        self._put(
            {
                "ts": self.clock.now(),
                "level": "AUDIT",
                "event_type": event_type[:64],
                "wallet": wallet,
                "tx_hash": tx_hash,
                "message": message,
                "data": {k: str(v)[:300] for k, v in (data or {}).items()},
            }
        )

    async def flush(self) -> int:
        rows = []
        while not self.queue.empty() and len(rows) < 500:
            rows.append(self.queue.get_nowait())
        if not rows:
            return 0
        try:
            async with self.sf() as s, s.begin():
                s.add_all([SystemLog(**r) for r in rows])
        except Exception:  # noqa: BLE001 - logging must never break the app
            self.dropped += len(rows)
            return 0
        return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            await self.flush()
        await self.flush()


class Heartbeat:
    """Touches HEARTBEAT_FILE while the live monitor is healthy (Docker HEALTHCHECK reads it)."""

    def __init__(self, path: str, monitor) -> None:
        self.path = Path(path)
        self.monitor = monitor

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            ok = self.monitor.last_poll_ok is not None and time.time() - self.monitor.last_poll_ok < 120
            if ok:
                try:
                    self.path.write_text(str(int(time.time())))
                except OSError:
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass


class NetworkPruneWorker:
    """Hourly retention of the network-wide payment memory."""

    def __init__(self, settings, network) -> None:
        self.s = settings
        self.network = network

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.network.prune()
            except Exception as exc:  # noqa: BLE001
                if not is_transient_db_error(exc):
                    log.exception("NETWORK_PRUNE_ERROR", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.s.network_prune_interval_minutes * 60)
            except asyncio.TimeoutError:
                pass
