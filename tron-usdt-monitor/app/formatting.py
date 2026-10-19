"""Telegram message texts (HTML parse mode). Never includes secrets."""

from __future__ import annotations

from html import escape

from app.config import Settings
from app.database import StoredTransaction
from app.stats import MonitorStats
from app.timeutil import format_duration, format_local, format_utc, now_ms

DIRECTION_ICON = {"INCOMING": "⬇️", "OUTGOING": "⬆️", "SELF": "🔁"}


def _local_line(ms: int, settings: Settings) -> str:
    if settings.timezone_name.upper() in ("UTC", "ETC/UTC"):
        return ""
    return f"\n{escape(format_local(ms, settings.timezone))}"


def tronscan_link(tx_hash: str, settings: Settings) -> str:
    return f"{settings.tronscan_tx_url}{tx_hash}"


def build_alert_message(tx: StoredTransaction, settings: Settings) -> str:
    header = "📜 <b>HISTORICAL USDT TRANSFER (BACKFILL)</b>" if tx.is_backfill else "✅ <b>USDT TRANSFER DONE</b>"
    icon = DIRECTION_ICON.get(tx.direction, "")
    link = tronscan_link(tx.tx_hash, settings)
    latency_s = (tx.detected_at_ms - tx.block_timestamp_ms) / 1000
    lines = [
        header,
        "",
        "🌐 Network: TRON",
        "💎 Token: USDT TRC-20",
        "",
        f"🧭 Direction: <b>{escape(tx.direction)}</b> {icon}",
        "",
        f"💵 Amount: <b>{escape(tx.amount_usdt)} USDT</b>",
        "",
        "👤 From:",
        f"<code>{escape(tx.sender)}</code>",
        "",
        "🎯 To:",
        f"<code>{escape(tx.recipient)}</code>",
        "",
        "⛓️ Blockchain Time:",
        f"{format_utc(tx.block_timestamp_ms)}{_local_line(tx.block_timestamp_ms, settings)}",
        "",
        "🤖 Detected By Bot:",
        format_utc(tx.detected_at_ms),
    ]
    if not tx.is_backfill:
        lines.append(f"⚡ Detection Latency: {latency_s:.3f} s")
    if tx.block_number is not None:
        lines += ["", f"🧱 Block: {tx.block_number}"]
    lines += [
        "",
        "🔑 Transaction Hash:",
        f"<code>{escape(tx.tx_hash)}</code>",
        "",
        "🔍 TRONSCAN:",
        f'<a href="{escape(link)}">{escape(link)}</a>',
    ]
    return "\n".join(lines)


SEP = "━━━━━━━━━━━━━━━━━━"


def _amount_lines(settings: Settings) -> list[str]:
    if len(settings.amount_ranges) == 1:
        return [f"💵 <b>Range:</b> {escape(settings.range_lines[0])}"]
    return ["💵 <b>Amounts:</b>", *(f"   ▫️ {escape(line)}" for line in settings.range_lines)]


def _monitoring_state(settings: Settings, stats: MonitorStats) -> tuple[str, str]:
    """(status, monitoring) labels with their icons."""
    if stats.paused:
        return "⏸️ PAUSED", "⏸️ PAUSED"
    if stats.is_healthy(settings.poll_interval_seconds):
        return "🟢 ONLINE", "🟢 ACTIVE"
    if not stats.initialized or (stats.last_poll_ok_ms is None and not stats.consecutive_errors):
        return "🟡 STARTING", "🟡 CONNECTING"
    return "🟠 DEGRADED", "🔴 TRON API UNREACHABLE (retrying)"


def build_wallet_message(settings: Settings, stats: MonitorStats) -> str:
    _, monitoring = _monitoring_state(settings, stats)
    return "\n".join(
        [
            "💼 <b>MONITORED WALLET</b>",
            SEP,
            f"👛 <code>{escape(settings.wallet_address)}</code>",
            "",
            "🌐 <b>Network:</b> TRON Mainnet",
            "💎 <b>Token:</b> USDT TRC-20",
            *_amount_lines(settings),
            f"🧭 <b>Direction:</b> {settings.directions_label}",
            SEP,
            f"📡 <b>Monitoring:</b> {monitoring}",
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
    status, monitoring = _monitoring_state(settings, stats)

    if stats.last_checked_tx_hash and stats.last_checked_tx_time_ms:
        last_checked = (f"<code>{stats.last_checked_tx_hash[:16]}…</code> · "
                        f"{format_utc(stats.last_checked_tx_time_ms)}")
    else:
        last_checked = "none yet"
    if settings.monitor_mode == "blocks" and stats.last_block_checked is not None:
        head = f" (head {stats.head_block})" if stats.head_block else ""
        last_checked = f"block {stats.last_block_checked}{head} · {last_checked}"

    if last_match:
        link = f"{settings.tronscan_tx_url}{last_match.tx_hash}"
        icon = DIRECTION_ICON.get(last_match.direction, "")
        last_match_lines = [
            f"✅ <b>Last match:</b> {escape(last_match.amount_usdt)} USDT {icon}",
            f"   🕒 {format_utc(last_match.block_timestamp_ms)}",
            f'   🔗 <a href="{escape(link)}">{last_match.tx_hash[:16]}…</a>',
        ]
    else:
        last_match_lines = ["✅ <b>Last match:</b> none yet"]

    api_latency = f"{api_last_ms:.0f} ms (avg {api_avg_ms:.0f} ms)" if api_last_ms is not None else "n/a"
    detection = (
        f"{stats.last_detection_latency_ms / 1000:.3f} s" if stats.last_detection_latency_ms is not None else "n/a"
    )
    last_poll = format_utc(stats.last_poll_ok_ms) if stats.last_poll_ok_ms else "never"
    speed = "confirmed only" if settings.confirmed_only else "fast"

    lines = [
        "📊 <b>BOT STATUS</b>",
        SEP,
        f"🚦 <b>Status:</b> {status}",
        f"📡 <b>Monitoring:</b> {monitoring}",
        SEP,
        f"👛 <b>Wallet:</b>\n<code>{escape(settings.wallet_address)}</code>",
        "🌐 <b>Network:</b> TRON Mainnet",
        "💎 <b>Token:</b> USDT TRC-20",
        f"📜 <b>Contract:</b>\n<code>{escape(settings.usdt_contract)}</code>",
        *_amount_lines(settings),
        f"🧭 <b>Direction:</b> {settings.directions_label}",
        SEP,
        f"📈 <b>Detected:</b> {detected_total} total · {stats.matches_this_session} this session",
    ]
    if settings.tx_limit_threshold and limit_count is not None:
        icon = "🚨" if limit_count >= settings.tx_limit_threshold else "🔢"
        lines.append(f"{icon} <b>Counter:</b> {limit_count}/{settings.tx_limit_threshold}")
    lines += [
        f"🔎 <b>Last checked:</b> {last_checked}",
        *last_match_lines,
        SEP,
        f"⚡ <b>API latency:</b> {api_latency}",
        f"⏱️ <b>Detection latency:</b> {detection}",
        f"🛰️ <b>Mode:</b> {escape(settings.monitor_mode)} · poll {settings.poll_interval_seconds:g}s · {speed}",
        f"🕒 <b>Last poll:</b> {last_poll}",
        f"🔁 <b>Transfers checked:</b> {stats.transfers_checked}",
        f"📮 <b>Pending alerts:</b> {pending_alerts}",
        f"⌛ <b>Uptime:</b> {format_duration((now_ms() - stats.started_at_ms) / 1000)}",
    ]
    if stats.consecutive_errors:
        lines.append(f"⚠️ <b>Consecutive API errors:</b> {stats.consecutive_errors}")
        if stats.last_error:
            lines.append(f"🧾 {escape(stats.last_error[:200])}")
    for warning in stats.warnings:
        lines.append(f"⚠️ {escape(warning)}")
    return "\n".join(lines)


def build_wallet_created_message(tx: StoredTransaction, gpu_type: str = "") -> str:
    """Sent right before the transaction alert. Shows the counterparty wallet
    (the receiver for outgoing transfers, the sender for incoming ones) and the
    configured GPU_TYPE label (a fixed text from .env; empty = line omitted)."""
    wallet = tx.sender if tx.direction == "INCOMING" else tx.recipient
    lines = ["✨ <b>WALLET CREATED</b>", "", f"<code>{escape(wallet)}</code>"]
    if gpu_type:
        lines += ["", f"🖥️ GPU type: <b>{escape(gpu_type)}</b> detected"]
    return "\n".join(lines)


def build_limit_message(count: int, threshold: int, paused: bool = True) -> str:
    footer = (
        f"<i>{count}/{threshold} transactions reached. ⏸️ Monitoring is PAUSED.</i>"
        if paused
        else f"<i>{count}/{threshold} transactions reached. Send /reset after refilling to start counting again.</i>"
    )
    return "\n".join(["Balance negative 🚨", "Fill resources", "Run again", "", footer])


def build_resumed_message(threshold: int, cycle: int, was_paused: bool, still_paused: bool = False) -> str:
    head = "🚀 <b>Monitoring started</b>" if was_paused else "♻️ <b>Counter reset</b>"
    lines = [head, ""]
    if threshold:
        lines += [f"Transaction Counter: 🔢 0/{threshold}", f"Cycle #{cycle} started. Counting from now."]
    else:
        lines.append("Watching from now.")
    if still_paused:
        lines += ["", "⏸️ Still paused."]
    return "\n".join(lines)


def _command_lines(settings: Settings) -> list[str]:
    return [
        "📊 /status – monitoring status and latency",
        "💼 /wallet – monitored wallet and amounts",
        f"♻️ /reset – restart the {settings.tx_limit_threshold}-transaction counter at 0",
        "📘 /help – this message",
    ]


def build_help_message(settings: Settings) -> str:
    return "\n".join(
        [
            "📘 <b>TRON USDT MONITOR</b>",
            SEP,
            f"👛 <code>{escape(settings.wallet_address)}</code>",
            "💎 USDT TRC-20 transfers",
            *_amount_lines(settings),
            f"🧭 <b>Direction:</b> {settings.directions_label}",
            SEP,
            "⌨️ <b>Commands</b>",
            *_command_lines(settings),
        ]
    )


def build_start_message(settings: Settings, paused: bool = False) -> str:
    head = (
        ["⏸️ <b>TRON USDT Monitor is paused.</b>"]
        if paused
        else ["🤝 <b>TRON USDT Monitor is running.</b>", "", "🔔 Alerts are sent here automatically."]
    )
    return "\n".join([*head, SEP, "⌨️ <b>Commands</b>", *_command_lines(settings)])


def build_startup_notice(settings: Settings, warnings: list[str], paused: bool = False) -> str:
    lines = [
        "⏸️ <b>Bot online – paused</b>" if paused else "🚀 <b>Monitor started</b>",
        "",
        f"Wallet: <code>{escape(settings.wallet_address)}</code>",
        f"Range: {settings.range_label}",
        f"Direction: {settings.directions_label}",
        f"Mode: {escape(settings.monitor_mode)}",
    ]
    lines += [f"⚠️ {escape(w)}" for w in warnings]
    return "\n".join(lines)
