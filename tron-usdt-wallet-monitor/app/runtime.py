"""Container for the running components (used by /status, /health, commands)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config import Settings
from app.db import repository as repo
from app.engine.processor import TransferProcessor
from app.engine.registry import WalletRegistry
from app.monitor.scheduler import WalletScheduler
from app.monitor.stream import StreamMonitor

if TYPE_CHECKING:
    from app.alerts.dispatcher import AlertDispatcher
    from app.tron.client import TronGridClient

STATE_PAUSED = "paused"
STATE_MONITOR_STARTED = "monitor_started_ms"


@dataclass
class Runtime:
    settings: Settings
    session_factory: object
    registry: WalletRegistry
    processor: TransferProcessor
    stream: StreamMonitor
    scheduler: WalletScheduler
    dispatcher: "AlertDispatcher | None" = None
    tron: "TronGridClient | None" = None
    started_at: float = field(default_factory=time.time)
    token_info: dict = field(default_factory=dict)
    db_ok: bool = True

    async def set_paused(self, paused: bool) -> None:
        async with self.session_factory() as s, s.begin():
            await repo.set_state(s, STATE_PAUSED, "1" if paused else "0")
        self.processor.paused = paused

    @property
    def paused(self) -> bool:
        return self.processor.paused

    @property
    def last_api_success(self) -> float | None:
        return self.tron.last_success_at if self.tron else None

    def health(self) -> tuple[bool, dict]:
        now = time.time()
        last_poll = self.stream.last_poll_at
        stalled = last_poll is None and now - self.started_at > self.settings.health_max_stall_seconds
        if last_poll is not None and now - last_poll > self.settings.health_max_stall_seconds:
            stalled = True
        lag = self.stream.lag_seconds
        behind = lag is not None and last_poll is not None and lag > self.settings.health_max_stall_seconds
        ok = not stalled and not behind and self.db_ok
        body = {
            "status": "ok" if ok else "degraded",
            "uptime_seconds": int(now - self.started_at),
            "db_ok": self.db_ok,
            "paused": self.paused,
            "stream_last_poll_age_seconds": None if last_poll is None else round(now - last_poll, 1),
            "stream_lag_seconds": None if self.stream.lag_seconds is None else round(self.stream.lag_seconds, 1),
            "last_block": self.stream.last_block,
            "monitored_wallets": len(self.registry.monitored()),
            "last_api_success_age_seconds": None if self.last_api_success is None else round(now - self.last_api_success, 1),
            "alerts_sent": self.dispatcher.sent if self.dispatcher else 0,
            "api_rate_limited": self.tron.rate_limited if self.tron else 0,
            "late_events_recovered": self.stream.late_events,
        }
        return ok, body
