"""Exact-precision amount handling and the transfer acceptance filter.

No floating point is ever used: amounts are integer base units (1 USDT =
1_000_000 units) and user-facing decimals are parsed with ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.transaction_parser import TokenTransfer


class Direction(str, Enum):
    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"
    SELF = "SELF"  # monitored wallet sent to itself


def parse_token_amount(value: str | int | Decimal, decimals: int = 6) -> int:
    """Convert a human decimal amount (e.g. "1.0001") to integer base units, exactly.

    Raises ValueError for negative, non-finite, malformed values or values with
    more precision than the token supports.
    """
    if isinstance(value, float):
        raise ValueError("floats are not accepted; pass a string or Decimal")
    try:
        dec = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {value!r}") from exc
    if not dec.is_finite() or dec < 0:
        raise ValueError(f"invalid amount {value!r}")
    scaled = dec.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {value!r} has more than {decimals} decimal places")
    return int(scaled)


def format_token_amount(raw: int, decimals: int = 6) -> str:
    """Format integer base units with exactly ``decimals`` places, e.g. 1000087 -> "1.000087"."""
    sign = "-" if raw < 0 else ""
    whole, frac = divmod(abs(int(raw)), 10**decimals)
    if decimals == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:0{decimals}d}"


@dataclass(frozen=True)
class FilterDecision:
    matched: bool
    reason: str
    direction: Direction | None = None


class TransferFilter:
    """Accepts a transfer only when every rule in the spec holds.

    1. It is a decoded TRC-20 ``Transfer`` event/record
    2. Emitted by exactly the configured USDT contract
    3. Token metadata (when present) is USDT with 6 decimals
    4. The monitored wallet is sender or recipient, in a monitored direction
       (ALERT_DIRECTIONS, default OUTGOING only)
    5. min_raw <= amount <= max_raw (inclusive, integer compare)
    """

    def __init__(
        self,
        wallet: str,
        contract: str,
        min_raw: int,
        max_raw: int,
        symbol: str = "USDT",
        decimals: int = 6,
        directions: frozenset[Direction] | None = None,
    ) -> None:
        if min_raw < 0 or max_raw < min_raw:
            raise ValueError("invalid amount range")
        self.wallet = wallet
        self.contract = contract
        self.min_raw = min_raw
        self.max_raw = max_raw
        self.symbol = symbol
        self.decimals = decimals
        # Which directions alert. SELF (wallet -> itself) is sent by the wallet, so it
        # follows OUTGOING unless listed explicitly.
        allowed = set(directions) if directions else set(Direction)
        if Direction.OUTGOING in allowed:
            allowed.add(Direction.SELF)
        self.directions = frozenset(allowed)

    def amount_in_range(self, raw: int) -> bool:
        return self.min_raw <= raw <= self.max_raw

    def direction_of(self, sender: str, recipient: str) -> Direction | None:
        if sender == self.wallet and recipient == self.wallet:
            return Direction.SELF
        if sender == self.wallet:
            return Direction.OUTGOING
        if recipient == self.wallet:
            return Direction.INCOMING
        return None

    def evaluate(self, transfer: "TokenTransfer") -> FilterDecision:
        if transfer.event_type != "Transfer":
            return FilterDecision(False, f"not a Transfer event ({transfer.event_type})")
        if transfer.contract_address != self.contract:
            return FilterDecision(False, f"wrong contract {transfer.contract_address}")
        if transfer.token_symbol is not None and transfer.token_symbol != self.symbol:
            return FilterDecision(False, f"wrong token symbol {transfer.token_symbol}")
        if transfer.token_decimals is not None and transfer.token_decimals != self.decimals:
            return FilterDecision(False, f"wrong token decimals {transfer.token_decimals}")
        direction = self.direction_of(transfer.sender, transfer.recipient)
        if direction is None:
            return FilterDecision(False, "monitored wallet not involved")
        if direction not in self.directions:
            return FilterDecision(False, f"direction {direction.value} not monitored", direction)
        if not self.amount_in_range(transfer.amount_raw):
            return FilterDecision(
                False,
                f"amount {format_token_amount(transfer.amount_raw, self.decimals)} out of range",
                direction,
            )
        return FilterDecision(True, "match", direction)
