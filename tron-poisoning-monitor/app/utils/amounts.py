"""Integer token-amount helpers.

All amounts are integer base units (USDT: 1 USDT = 1_000_000 units).  Floating
point is never used for money: parsing goes through :class:`decimal.Decimal`
and formatting through integer division.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_token_amount(text: str | int, decimals: int) -> int:
    """'25000' / '0.000001' / '1,234.5' -> integer base units.  Rejects excess precision."""
    if isinstance(text, bool):
        raise ValueError("bool is not an amount")
    if isinstance(text, int):
        return text * 10**decimals
    s = str(text).strip().replace(",", "").replace("_", "")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {text!r}") from exc
    if not d.is_finite() or d < 0:
        raise ValueError(f"invalid amount: {text!r}")
    scaled = d.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {text!r} has more than {decimals} decimals")
    return int(scaled)


def format_amount(units: int, decimals: int = 6, *, grouping: bool = True, trim: bool = True) -> str:
    """Integer base units -> human string, e.g. 25000000000 -> '25,000'."""
    if not isinstance(units, int):
        units = int(units)
    neg = units < 0
    whole, frac = divmod(abs(units), 10**decimals)
    whole_s = f"{whole:,}" if grouping else str(whole)
    frac_s = str(frac).rjust(decimals, "0") if decimals else ""
    if trim:
        frac_s = frac_s.rstrip("0")
    out = whole_s + (f".{frac_s}" if frac_s else "")
    return ("-" if neg else "") + out


def format_token(units: int, symbol: str = "USDT", decimals: int = 6) -> str:
    return f"{format_amount(units, decimals)} {symbol}"


def ratio_pct(part: int, whole: int) -> int:
    """Integer percentage (floor) without floats."""
    if whole <= 0:
        return 0
    return (part * 100) // whole
