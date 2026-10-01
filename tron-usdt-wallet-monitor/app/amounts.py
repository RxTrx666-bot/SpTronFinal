"""Exact USDT amount arithmetic.

Amounts are always kept as integers in base units (USDT has 6 decimals, so
1 USDT = 1_000_000 base units).  Conversion to/from human units uses
``decimal.Decimal`` only - floating point is never used for token amounts.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

USDT_DECIMALS = 6


def base_to_usdt(base_units: int, decimals: int = USDT_DECIMALS) -> Decimal:
    return Decimal(int(base_units)).scaleb(-decimals)


def usdt_to_base(amount: Decimal | str | int, decimals: int = USDT_DECIMALS) -> int:
    """'500' -> 500_000_000.  Rejects more decimals than the token supports."""
    if isinstance(amount, float):
        raise TypeError("floats are not accepted for token amounts; pass a str or Decimal")
    try:
        value = Decimal(str(amount).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {amount!r}") from exc
    if not value.is_finite():
        raise ValueError(f"invalid amount {amount!r}")
    scaled = value.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {amount!r} has more than {decimals} decimals")
    return int(scaled)


def fmt_usdt(base_units: int, decimals: int = USDT_DECIMALS) -> str:
    """Exact human formatting: 2_500_000_000 -> '2,500', 1 -> '0.000001', 500_000_001 -> '500.000001'."""
    text = f"{base_to_usdt(base_units, decimals):,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
