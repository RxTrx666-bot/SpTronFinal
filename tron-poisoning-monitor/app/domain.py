"""Core value types shared by every layer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    POISONING_ATTEMPT = "POISONING_ATTEMPT"  # look-alike activity, victim has NOT sent funds
    POISONING_CANDIDATE = "POISONING_CANDIDATE"  # victim paid a look-alike, evidence below threshold
    SUCCESSFUL_POISONING_EVENT = "SUCCESSFUL_POISONING_EVENT"  # victim paid a look-alike, threshold crossed


class Confirmation(str, Enum):
    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED"
    DROPPED = "DROPPED"


class AnalysisStatus(str, Enum):
    PENDING = "PENDING"
    DONE = "DONE"
    ERROR = "ERROR"


class WalletStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REMOVED = "REMOVED"


class HistoryStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class AlertStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class Tri(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TokenTransfer:
    """One TRC-20 ``Transfer`` event, normalised.  ``amount`` is integer base units."""

    tx_hash: str
    token_contract: str
    from_address: str
    to_address: str
    amount: int
    block_timestamp_ms: int
    block_number: int | None = None
    confirmed: bool = False
    initiator: str | None = None  # owner_address that signed the transaction, when known
    log_index: int | None = None
    seq: int = 0  # occurrence index of identical (from,to,amount) tuples inside one tx

    @property
    def transfer_key(self) -> str:
        """Source-independent idempotency key (block scan and account history agree on it)."""
        raw = f"{self.tx_hash.lower()}|{self.token_contract}|{self.from_address}|{self.to_address}|{self.amount}|{self.seq}"
        return hashlib.sha256(raw.encode()).hexdigest()


def assign_sequences(transfers: list[TokenTransfer]) -> list[TokenTransfer]:
    """Give identical (tx, token, from, to, amount) tuples increasing ``seq`` numbers."""
    from dataclasses import replace

    counts: dict[tuple, int] = {}
    out = []
    for t in transfers:
        k = (t.tx_hash.lower(), t.token_contract, t.from_address, t.to_address, t.amount)
        n = counts.get(k, 0)
        counts[k] = n + 1
        out.append(replace(t, seq=n) if n != t.seq else t)
    return out


@dataclass(frozen=True)
class Contact:
    """An address 'touching' another wallet's history - the planting step of address poisoning.

    kind: USDT_DUST (tiny real USDT), ZERO_VALUE (0-amount USDT transferFrom spoof),
          TOKEN (transfer of another TRC-20 token - typically a fake "USDT"), TRX (tiny TRX).
    """

    toucher: str  # the address that ends up in the victim's history (the possible fake)
    touched: str  # the wallet whose history it appears in (the possible victim)
    kind: str
    tx_hash: str
    timestamp_ms: int
    amount: int = 0
    token_contract: str | None = None


@dataclass
class BlockData:
    number: int
    timestamp_ms: int
    transfers: list[TokenTransfer] = field(default_factory=list)
    fetched_at_ms: int = 0
    contacts: list[Contact] = field(default_factory=list)


@dataclass
class TxDetails:
    tx_hash: str
    found: bool
    block_number: int | None = None
    block_timestamp_ms: int | None = None
    success: bool | None = None
    initiator: str | None = None
    confirmed: bool = False


@dataclass
class AccountInfo:
    address: str
    exists: bool
    create_time_ms: int | None = None
    trx_balance_sun: int | None = None


@dataclass
class AddressLabel:
    address: str
    label: str
    category: str  # exchange / service / scam / unknown
    source: str
