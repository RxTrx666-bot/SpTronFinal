"""Exact USDT amount arithmetic.

Token amounts are always kept as integers in the token's smallest unit
(``amount_raw``; USDT has 6 decimals).  Conversion to human units uses
``decimal.Decimal`` only.  Floating point is never used for token amounts.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

USDT_DECIMALS = 6


def raw_to_usdt(raw: int, decimals: int = USDT_DECIMALS) -> Decimal:
    return Decimal(int(raw)).scaleb(-decimals)


def usdt_to_raw(amount: Decimal | str | int, decimals: int = USDT_DECIMALS) -> int:
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {amount!r}") from exc
    scaled = value.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {amount!r} has more than {decimals} decimals")
    return int(scaled)


def _strip(d: Decimal) -> str:
    text = f"{d:,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def fmt_usdt(raw: int, decimals: int = USDT_DECIMALS, *, max_places: int | None = None) -> str:
    """Human formatting: 5 -> '5', 20000000000 -> '20,000', 1 -> '0.000001'."""
    value = raw_to_usdt(raw, decimals)
    if max_places is not None and value >= 1:
        value = value.quantize(Decimal(1).scaleb(-max_places), rounding=ROUND_HALF_UP)
    return _strip(value)


def fmt_usdt_approx(raw: int, decimals: int = USDT_DECIMALS) -> str:
    """Rounded display for typical values (≈ 6.67, ≈ 25,000)."""
    value = raw_to_usdt(raw, decimals)
    if value >= 1000:
        value = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    elif value >= 1:
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return _strip(value)


def fmt_compact(raw: int, decimals: int = USDT_DECIMALS) -> str:
    """Compact form used in ranges: 15,000 -> '15K', 1,250,000 -> '1.25M'."""
    value = raw_to_usdt(raw, decimals)
    for unit, scale in (("B", Decimal(10) ** 9), ("M", Decimal(10) ** 6), ("K", Decimal(10) ** 3)):
        if value >= scale:
            return _strip((value / scale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + unit
    return fmt_usdt_approx(raw, decimals)


def fmt_range(low_raw: int, high_raw: int, *, compact: bool = False) -> str:
    f = fmt_compact if compact else fmt_usdt_approx
    lo, hi = f(low_raw), f(high_raw)
    return lo if lo == hi else f"{lo}–{hi}"
