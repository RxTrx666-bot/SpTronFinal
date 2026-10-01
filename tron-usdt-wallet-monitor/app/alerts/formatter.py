"""Telegram message texts (HTML parse mode). Never includes secrets."""

from __future__ import annotations

from datetime import datetime
from html import escape

from app.amounts import fmt_usdt
from app.db.models import Alert

TRONSCAN_TX = "https://tronscan.org/#/transaction/{}"
TRONSCAN_ADDR = "https://tronscan.org/#/address/{}"


def tx_link(tx: str) -> str:
    return TRONSCAN_TX.format(tx)


def addr_link(addr: str) -> str:
    return TRONSCAN_ADDR.format(addr)


def fmt_ts(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "—"


def humanize(seconds: float) -> str:
    """3 hours 17 minutes / 2 days 4 hours / 45 seconds."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    parts = []
    for name, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " ".join(parts[:2])


def code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def large_transfer_message(a: Alert, *, root_label: str = "Wallet A", resent: bool = False) -> str:
    since = humanize((a.transfer_timestamp - a.discovered_at).total_seconds()) if a.discovered_at else "—"
    status = "✅ Confirmed" if a.confirmed else "⏳ Unconfirmed (fast mode)"
    lines = [
        "🚨 <b>LARGE USDT TRANSFER DETECTED</b>",
        "",
        "Network: TRON",
        "Token: USDT TRC-20",
        "",
        f"Amount: <b>{fmt_usdt(a.amount_base_units)} USDT</b>",
        "",
        "Discovered Wallet:",
        code(a.discovered_wallet),
        "",
        "Sender:",
        code(a.sender),
        "",
        "Original Discovery:",
        f"{escape(root_label)} ({code(a.root_wallet)})",
        "",
        "Discovery Transaction:",
        f'<a href="{tx_link(a.discovery_tx)}">{escape(a.discovery_tx)}</a>' if a.discovery_tx else "—",
        "",
        "Large Transfer Transaction:",
        code(a.tx_hash),
        "",
        "Time Since Discovery:",
        since,
        "",
        "Transaction:",
        tx_link(a.tx_hash),
        "",
        "Timestamp:",
        fmt_ts(a.transfer_timestamp),
        "",
        f"Status: {status}" + (f" · Block {a.block_number}" if a.block_number else ""),
    ]
    if resent:
        lines += ["", "♻️ <i>Re-sent after a restart – may duplicate an earlier message.</i>"]
    return "\n".join(lines)


def discovery_message(a: Alert, *, root_label: str = "Wallet A", resent: bool = False) -> str:
    lines = [
        "🔎 <b>NEW WALLET DISCOVERED</b>",
        "",
        "Root Wallet:",
        f"{escape(root_label)} ({code(a.root_wallet)})",
        "",
        "Destination:",
        code(a.discovered_wallet),
        "",
        "Amount:",
        f"{fmt_usdt(a.amount_base_units)} USDT",
        "",
        "Transaction:",
        tx_link(a.tx_hash),
        "",
        "Timestamp:",
        fmt_ts(a.transfer_timestamp),
    ]
    if resent:
        lines += ["", "♻️ <i>Re-sent after a restart – may duplicate an earlier message.</i>"]
    return "\n".join(lines)
