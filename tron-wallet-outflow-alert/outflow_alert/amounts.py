"""Exact USDT amount arithmetic.

Token amounts are always integers in base units (USDT: 6 decimals).
Configuration strings are converted with ``decimal.Decimal`` and must be
exactly representable in base units; floats are never used.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

USDT_DECIMALS = 6


def to_base_units(text: str, decimals: int = USDT_DECIMALS) -> int:
    """'1.01' -> 1010000.  Rejects negatives, non-numbers and excess precision."""
    if isinstance(text, float):
        raise TypeError("floats are not accepted for token amounts")
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"invalid amount {text!r}")
    scaled = value.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {text!r} has more than {decimals} decimals")
    return int(scaled)


def parse_amount_list(text: str, decimals: int = USDT_DECIMALS) -> tuple[int, ...]:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("AMOUNTS must contain at least one amount")
    values = [to_base_units(p, decimals) for p in parts]
    if any(v == 0 for v in values):
        raise ValueError("AMOUNTS must be greater than zero")
    return tuple(sorted(set(values)))


def parse_raw_value(value: object) -> int:
    """Parse an on-chain integer value (decimal string or int) without floats."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"unexpected amount type: {value!r}")
    if isinstance(value, int):
        raw = value
    elif isinstance(value, str) and value.strip().isdigit():
        raw = int(value.strip())
    else:
        raise ValueError(f"invalid raw amount: {value!r}")
    if raw < 0:
        raise ValueError(f"negative amount: {value!r}")
    return raw


def match_target(amount_raw: int, targets: Sequence[int] | Iterable[int], tolerance_raw: int) -> int | None:
    """Return the closest target within ``tolerance_raw`` (inclusive), else ``None``."""
    best: int | None = None
    for target in targets:
        diff = abs(amount_raw - target)
        if diff <= tolerance_raw and (best is None or diff < abs(amount_raw - best)):
            best = target
    return best


def format_units(raw: int, decimals: int = USDT_DECIMALS) -> str:
    """1010000 -> '1.01', 10000000 -> '10', 1234567890 -> '1,234.56789'."""
    text = f"{Decimal(int(raw)).scaleb(-decimals):,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
