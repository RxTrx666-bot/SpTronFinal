"""In-memory runtime statistics shown by /status."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.timeutil import now_ms


@dataclass
class MonitorStats:
    mode: str = ""
    started_at_ms: int = field(default_factory=now_ms)
    initialized: bool = False
    polls: int = 0
    poll_errors: int = 0
    consecutive_errors: int = 0
    last_poll_ok_ms: int | None = None
    last_error: str | None = None
    last_error_ms: int | None = None
    transfers_checked: int = 0
    matches_this_session: int = 0
    last_checked_tx_hash: str | None = None
    last_checked_tx_time_ms: int | None = None
    last_block_checked: int | None = None
    head_block: int | None = None
    cursor_ms: int | None = None
    last_detection_latency_ms: int | None = None
    last_alert_latency_ms: int | None = None
    alerts_sent: int = 0
    alert_failures: int = 0
    alert_queue_size: int = 0
    warnings: list[str] = field(default_factory=list)

    def poll_succeeded(self) -> None:
        self.polls += 1
        self.consecutive_errors = 0
        self.last_poll_ok_ms = now_ms()

    def poll_failed(self, error: str) -> None:
        self.poll_errors += 1
        self.consecutive_errors += 1
        self.last_error = error
        self.last_error_ms = now_ms()

    def is_healthy(self, poll_interval: float) -> bool:
        if self.last_poll_ok_ms is None:
            return False
        max_age_ms = max(30_000, int(poll_interval * 10_000))
        return now_ms() - self.last_poll_ok_ms <= max_age_ms
