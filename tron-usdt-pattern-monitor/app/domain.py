"""Shared domain types (enums and value objects)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class TxStatus(str, Enum):
    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED"
    DROPPED = "DROPPED"


class WatchlistStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    WEAKENED = "WEAKENED"
    EXPIRED = "EXPIRED"


class ConfidenceLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[self.value]


class TestEventStatus(str, Enum):
    __test__ = False  # not a pytest test class

    PENDING = "PENDING"
    FOLLOWED_UP = "FOLLOWED_UP"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class AlertType(str, Enum):
    NEW_PATTERN = "NEW_PATTERN"
    WATCHLIST_ACTIVATED = "WATCHLIST_ACTIVATED"
    TEST_DETECTED = "TEST_DETECTED"
    LARGE_FOLLOWUP = "LARGE_FOLLOWUP"
    TEST_CONFIRMED = "TEST_CONFIRMED"
    SYSTEM = "SYSTEM"


# Lower number = sent first.  The known-test alert is the critical early warning.
ALERT_PRIORITY = {
    AlertType.TEST_DETECTED: 0,
    AlertType.LARGE_FOLLOWUP: 1,
    AlertType.TEST_CONFIRMED: 2,
    AlertType.WATCHLIST_ACTIVATED: 3,
    AlertType.NEW_PATTERN: 4,
    AlertType.SYSTEM: 5,
}


class AlertStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def datetime_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class TransferEvent:
    """A decoded, validated USDT TRC-20 ``Transfer`` event."""

    transaction_hash: str
    event_index: int
    block_number: int | None
    block_timestamp_ms: int
    sender: str
    recipient: str
    amount_raw: int
    token_contract: str
    confirmed: bool

    @property
    def timestamp(self) -> datetime:
        return ms_to_datetime(self.block_timestamp_ms)

    @property
    def pair(self) -> tuple[str, str]:
        return (self.sender, self.recipient)

    @property
    def key(self) -> tuple[str, int]:
        return (self.transaction_hash, self.event_index)
