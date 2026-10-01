"""Core value types shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

WALLET_ROOT = "root"
WALLET_DISCOVERED = "discovered"

ALERT_LARGE_TRANSFER = "large_transfer"
ALERT_DISCOVERY = "discovery"

ALERT_PENDING = "pending"
ALERT_SENDING = "sending"
ALERT_SENT = "sent"
ALERT_SUPPRESSED = "suppressed"  # created while monitoring was /pause'd

KIND_DISCOVERY = "discovery"  # sent by an expanding wallet (Wallet A)
KIND_INCOMING = "incoming"  # received by a monitored (discovered) wallet
KIND_BOTH = "both"


@dataclass(frozen=True, slots=True)
class Transfer:
    """One normalized USDT TRC-20 ``Transfer`` event.

    ``(tx_hash, event_index)`` uniquely identifies it on chain; one transaction
    can contain several USDT transfers, so ``tx_hash`` alone is not enough.
    """

    tx_hash: str
    event_index: int
    block_number: int | None
    timestamp_ms: int
    from_address: str
    to_address: str
    amount_base_units: int
    contract_address: str
    confirmed: bool

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_hash, self.event_index)

    @property
    def timestamp(self) -> datetime:
        return ms_to_dt(self.timestamp_ms)

    def sort_key(self) -> tuple:
        return (self.timestamp_ms, self.block_number or 0, self.tx_hash, self.event_index)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
