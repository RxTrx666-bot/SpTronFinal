"""Telegram message texts (HTML parse mode). Never includes secrets."""

from __future__ import annotations

from html import escape

from app.config import Settings
from app.database import StoredTransaction
from app.stats import MonitorStats
from app.timeutil import format_duration, format_local, format_utc, now_ms

DIRECTION_ICON = {"INCOMING": "📥", "OUTGOING": "📤", "SELF": "🔁"}


def _local_line(ms: int, settings: Settings) -> str:
    if settings.timezone_name.upper() in ("UTC", "ETC/UTC"):
        return ""
    return f"\n{escape(format_local(ms, settings.timezone))}"


def tronscan_link(tx_hash: str, settings: Settings) -> str:
    return f"{settings.tronscan_tx_url}{tx_hash}"


def build_alert_message(tx: StoredTransaction, settings: Settings) -> str:
    header = "🕘 <b>HISTORICAL USDT TRANSACTION (BACKFILL)</b>" if tx.is_backfill else "🚨 <b>USDT TRANSACTION DETECTED</b>"
    icon = DIRECTION_ICON.get(tx.direction, "")
    link = tronscan_link(tx.tx_hash, settings)
    latency_s = (tx.detected_at_ms - tx.block_timestamp_ms) / 1000
    lines = [
        header,
        "",
        "Network: TRON",
        "Token: USDT TRC-20",
        "",
        f"Direction: <b>{escape(tx.direction)}</b> {icon}",
        "",
        f"Amount: <b>{escape(tx.amount_usdt)} USDT</b>",
        "",
        "From:",
        f"<code>{escape(tx.sender)}</code>",
        "",
        "To:",
        f"<code>{escape(tx.recipient)}</code>",
        "",
        "Blockchain Time:",
        f"{format_utc(tx.block_timestamp_ms)}{_local_line(tx.block_timestamp_ms, settings)}",
        "",
        "Detected By Bot:",
        format_utc(tx.detected_at_ms),
    ]
    if not tx.is_backfill:
        lines.append(f"Detection Latency: {latency_s:.3f} s")
    if tx.block_number is not None:
        lines += ["", f"Block: {tx.block_number}"]
    lines += [
        "",
        "Transaction Hash:",
        f"<code>{escape(tx.tx_hash)}</code>",
        "",
        "TRONSCAN:",
        f'<a href="{escape(link)}">{escape(link)}</a>',
    ]
    return "\n".join(lines)


def build_wallet_message(settings: Settings, stats: MonitorStats) -> str:
    active = stats.is_healthy(settings.poll_interval_seconds)
    return "\n".join(
        [
            "👛 <b>MONITORED WALLET</b>",
            "",
            f"<code>{escape(settings.wallet_address)}</code>",
            "",
            "Network: TRON",
            "Token: USDT TRC-20",
            "",
            "Amount Range:",
            f"{settings.min_usdt} – {settings.max_usdt} USDT",
            "",
            "Direction:",
            settings.directions_label,
            "",
            "Monitoring:",
            "🟢 ACTIVE" if active else "🔴 NOT ACTIVE (API unreachable, see /status)",
        ]
    )


def build_status_message(
    settings: Settings,
    stats: MonitorStats,
    detected_total: int,
    last_match: StoredTransaction | None,
    api_last_ms: float | None,
    api_avg_ms: float | None,
    pending_alerts: int,
    limit_count: int | None = None,
) -> str:
    healthy = stats.is_healthy(settings.poll_interval_seconds)
    if healthy:
        status, monitoring = "🟢 ONLINE", "🟢 ACTIVE"
    elif not stats.initialized or (stats.last_poll_ok_ms is None and not stats.consecutive_errors):
        status, monitoring = "🟡 STARTING", "🟡 CONNECTING"
    else:
        status, monitoring = "🟠 DEGRADED", "🔴 TRON API UNREACHABLE (retrying)"

    if stats.last_checked_tx_hash and stats.last_checked_tx_time_ms:
        last_checked = f"<code>{stats.last_checked_tx_hash[:16]}…</code>\n{format_utc(stats.last_checked_tx_time_ms)}"
    else:
        last_checked = "none yet"
    if settings.monitor_mode == "blocks" and stats.last_block_checked is not None:
        last_checked = f"Block {stats.last_block_checked}" + (
            f" (head {stats.head_block})" if stats.head_block else ""
        ) + f"\n{last_checked}"

    if last_match:
        link = f"{settings.tronscan_tx_url}{last_match.tx_hash}"
        last_match_text = (
            f"{escape(last_match.direction)} {escape(last_match.amount_usdt)} USDT\n"
            f"{format_utc(last_match.block_timestamp_ms)}\n"
            f'<a href="{escape(link)}">{last_match.tx_hash[:16]}…</a>'
        )
    else:
        last_match_text = "none yet"

    if api_last_ms is not None:
        api_latency = f"{api_last_ms:.0f} ms (avg {api_avg_ms:.0f} ms)"
    else:
        api_latency = "n/a"
    detection = (
        f"{stats.last_detection_latency_ms / 1000:.3f} s" if stats.last_detection_latency_ms is not None else "n/a"
    )
    last_poll = format_utc(stats.last_poll_ok_ms) if stats.last_poll_ok_ms else "never"
    lines = [
        "🤖 <b>BOT STATUS</b>",
        "",
        f"Status: {status}",
        "",
        "Wallet:",
        f"<code>{escape(settings.wallet_address)}</code>",
        "",
        "Network:",
        "TRON Mainnet",
        "",
        "Token:",
        "USDT TRC-20",
        "",
        "Contract:",
        f"<code>{escape(settings.usdt_contract)}</code>",
        "",
        "Range:",
        f"{settings.min_usdt} – {settings.max_usdt} USDT",
        "",
        "Direction:",
        settings.directions_label,
        "",
        "Monitoring:",
        monitoring,
        "",
        "Transactions Detected:",
        f"{detected_total} total ({stats.matches_this_session} this session)",
        "",
        "Last Transaction Checked:",
        last_checked,
        "",
        "Last Matching Transaction:",
        last_match_text,
        "",
        "API Latency:",
        api_latency,
        "",
        "Last Detection Latency:",
        detection,
    ]
    if settings.tx_limit_threshold and limit_count is not None:
        icon = "🚨" if limit_count >= settings.tx_limit_threshold else "🔢"
        lines += ["", "Transaction Counter:", f"{icon} {limit_count}/{settings.tx_limit_threshold}"]
    lines += [
        "",
        f"Mode: {escape(settings.monitor_mode)} · poll {settings.poll_interval_seconds:g}s"
        f" · {'confirmed only' if settings.confirmed_only else 'fast (unconfirmed blocks)'}",
        f"Last successful poll: {last_poll}",
        f"Transfers checked: {stats.transfers_checked}",
        f"Pending alerts: {pending_alerts}",
        f"Uptime: {format_duration((now_ms() - stats.started_at_ms) / 1000)}",
    ]
    if stats.consecutive_errors:
        lines.append(f"⚠️ Consecutive API errors: {stats.consecutive_errors}")
        if stats.last_error:
            lines.append(f"Last error: {escape(stats.last_error[:200])}")
    for warning in stats.warnings:
        lines.append(f"⚠️ {escape(warning)}")
    return "\n".join(lines)


def build_wallet_created_message(tx: StoredTransaction) -> str:
    """Sent right before the transaction alert. Shows the counterparty wallet
    (the receiver for outgoing transfers, the sender for incoming ones)."""
    wallet = tx.sender if tx.direction == "INCOMING" else tx.recipient
    return "\n".join(["🆕 <b>WALLET CREATED</b>", "", f"<code>{escape(wallet)}</code>"])


def build_limit_message(count: int, threshold: int) -> str:
    return "\n".join(
        [
            "Balance negative 🚨",
            "Fill resources",
            "Run again",
            "",
            f"<i>{count}/{threshold} transactions reached. Send /reset after refilling to start counting again.</i>",
        ]
    )


def build_help_message(settings: Settings) -> str:
    return "\n".join(
        [
            "ℹ️ <b>TRON USDT MONITOR</b>",
            "",
            f"Watches <code>{escape(settings.wallet_address)}</code> for USDT TRC-20 transfers "
            f"between {settings.min_usdt} and {settings.max_usdt} USDT (inclusive). Direction: {settings.directions_label}.",
            "",
            "/status – monitoring status and latency",
            "/wallet – monitored wallet and filter",
            f"/reset – restart the {settings.tx_limit_threshold}-transaction counter (after refilling)",
            "/help – this message",
        ]
    )


def build_start_message(settings: Settings) -> str:
    return "👋 <b>TRON USDT Monitor is running.</b>\n\nAlerts are sent here automatically.\n\n" + "\n".join(
        build_help_message(settings).split("\n")[4:]
    )


def build_startup_notice(settings: Settings, warnings: list[str]) -> str:
    lines = [
        "🟢 <b>Monitor started</b>",
        "",
        f"Wallet: <code>{escape(settings.wallet_address)}</code>",
        f"Range: {settings.min_usdt} – {settings.max_usdt} USDT",
        f"Direction: {settings.directions_label}",
        f"Mode: {escape(settings.monitor_mode)}",
    ]
    lines += [f"⚠️ {escape(w)}" for w in warnings]
    return "\n".join(lines)
