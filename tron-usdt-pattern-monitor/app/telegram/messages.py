"""Telegram message formatting (HTML parse mode)."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.amounts import fmt_compact, fmt_range, fmt_usdt, fmt_usdt_approx
from app.collector.address import short
from app.domain import WatchlistStatus
from app.watchlist.model import WatchlistSnapshot

STATUS_ICON = {
    WatchlistStatus.ACTIVE.value: "🟢",
    WatchlistStatus.CANDIDATE.value: "🟡",
    WatchlistStatus.PAUSED.value: "⏸",
    WatchlistStatus.WEAKENED.value: "🟠",
    WatchlistStatus.EXPIRED.value: "⚪",
}
CONF_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}


def humanize_seconds(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} seconds"
    m = s // 60
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} hour{'s' if h != 1 else ''}"
    d, h = divmod(h, 24)
    return f"{d} days {h} h" if h else f"{d} days"


def _code(addr: str) -> str:
    return f"<code>{html.escape(addr)}</code>"


class MessageFormatter:
    def __init__(self, display_timezone: str = "UTC", tx_url: str = "https://tronscan.org/#/transaction/") -> None:
        try:
            self.tz = ZoneInfo(display_timezone)
        except Exception:  # noqa: BLE001
            self.tz = timezone.utc
        self.tx_url = tx_url

    # ------------------------------------------------------------ helpers
    def fmt_time(self, dt: datetime) -> str:
        local = dt.astimezone(self.tz)
        off = local.utcoffset()
        hours = off.total_seconds() / 3600 if off else 0
        if hours == 0:
            label = "UTC"
        else:
            label = f"UTC{'+' if hours > 0 else '-'}{abs(hours):g}"
        return f"{local:%Y-%m-%d %H:%M:%S} {label}"

    def tx_link(self, tx_hash: str) -> str:
        return f"{self.tx_url}{tx_hash}"

    # ------------------------------------------------------------ alerts
    def test_alert(
        self,
        snap: WatchlistSnapshot,
        *,
        tx_hash: str,
        amount_raw: int,
        tx_time: datetime,
        detected_at: datetime,
        confirmed: bool,
    ) -> str:
        larges = "\n".join(f"${fmt_usdt_approx(a)}" for a in snap.recent_large_amounts) or "—"
        latency = max(0.0, (detected_at - tx_time).total_seconds())
        return (
            "🔴🔴 🚨🚨 <b>WATCHLIST TEST TRANSFER DETECTED</b> 🚨🚨 🔴🔴\n"
            "⚠️ <b>KNOWN TEST-TRANSFER PATTERN REPEATED</b>\n\n"
            f"<b>Sender:</b>\n{_code(snap.sender)}\n"
            f"<b>Recipient:</b>\n{_code(snap.recipient)}\n\n"
            f"<b>New Test Transfer:</b>\n{fmt_usdt(amount_raw)} USDT\n"
            f"<b>Historical Test Pattern:</b>\n{fmt_range(snap.test_min_raw, snap.test_max_raw)} USDT\n"
            f"<b>Historical Large Transfers:</b>\n{larges}\n"
            f"<b>Successful Sequences:</b>\n{snap.successful_sequences}\n"
            f"<b>Typical Follow-Up:</b>\n≈ ${fmt_usdt_approx(snap.typical_large_raw)}\n"
            f"<b>Historical Follow-Up Window:</b>\nUsually within {humanize_seconds(snap.followup_p80_seconds)}"
            f" (watching for {humanize_seconds(snap.followup_window_seconds)})\n"
            f"<b>Confidence:</b>\n{CONF_ICON.get(snap.confidence, '')} {snap.confidence}"
            f" ({snap.confidence_score:.2f})\n"
            f"<b>Status:</b> {'CONFIRMED' if confirmed else 'UNCONFIRMED (not yet solidified)'}\n\n"
            f"<b>Transaction:</b>\n{self.tx_link(tx_hash)}\n"
            f"<b>Block time:</b> {self.fmt_time(tx_time)}\n"
            f"<b>Detected:</b> {self.fmt_time(detected_at)}\n"
            f"<b>Detection latency:</b> {latency:.1f} s after block time"
        )

    def followup_alert(
        self,
        *,
        sender: str,
        recipient: str,
        test_amount_raw: int,
        test_tx_hash: str,
        amount_raw: int,
        tx_hash: str,
        dt_seconds: int,
        tx_time: datetime,
        detected_at: datetime,
        confirmed: bool,
    ) -> str:
        ratio = amount_raw // test_amount_raw if test_amount_raw else 0
        return (
            "🚨🚨 <b>LARGE FOLLOW-UP DETECTED</b> 🚨🚨\n\n"
            f"The previously detected test transfer was:\n<b>{fmt_usdt(test_amount_raw)} USDT</b>\n"
            f"A follow-up transfer has now occurred:\n<b>{fmt_usdt(amount_raw)} USDT</b> (≈{ratio:,}x)\n\n"
            f"<b>Sender:</b>\n{_code(sender)}\n"
            f"<b>Recipient:</b>\n{_code(recipient)}\n\n"
            f"<b>Transaction:</b>\n{self.tx_link(tx_hash)}\n"
            f"<b>Test transaction:</b>\n{self.tx_link(test_tx_hash)}\n"
            f"<b>Time Difference:</b>\n{humanize_seconds(dt_seconds)}\n"
            f"<b>Historical Pattern:</b>\nTEST → LARGE\n"
            f"<b>Status:</b> {'CONFIRMED' if confirmed else 'UNCONFIRMED'}\n"
            f"<b>Detected:</b> {self.fmt_time(detected_at)}"
        )

    def _pattern_block(self, snap: WatchlistSnapshot) -> str:
        return (
            f"<b>Sender:</b>\n{_code(snap.sender)}\n"
            f"<b>Recipient:</b>\n{_code(snap.recipient)}\n"
            f"<b>Pattern:</b>\nTEST → LARGE\n"
            f"<b>Historical Test Amounts:</b>\n{fmt_range(snap.test_min_raw, snap.test_max_raw)} USDT\n"
            f"<b>Historical Large Amounts:</b>\n{fmt_range(snap.large_min_raw, snap.large_max_raw)} USDT\n"
            f"<b>Typical Test Amount:</b>\n≈ {fmt_usdt_approx(snap.typical_test_raw)} USDT\n"
            f"<b>Typical Large Amount:</b>\n≈ {fmt_usdt_approx(snap.typical_large_raw)} USDT\n"
            f"<b>Test → Large Ratio:</b>\n≈ {float(snap.ratio):,.0f}x\n"
            f"<b>Successful Sequences:</b>\n{snap.successful_sequences}\n"
            f"<b>Success Rate:</b>\n{snap.success_rate:.0%}\n"
            f"<b>Typical Follow-Up Window:</b>\n{humanize_seconds(snap.typical_followup_seconds)}\n"
            f"<b>Confidence:</b>\n{snap.confidence} ({snap.confidence_score:.2f})\n"
            f"<b>Status:</b>\n{STATUS_ICON.get(snap.status.value, '')} {snap.status.value}"
        )

    def new_pattern_alert(self, snap: WatchlistSnapshot) -> str:
        return "🟡 <b>NEW USDT TEST → LARGE PATTERN DISCOVERED</b>\n\n" + self._pattern_block(snap)

    def activated_alert(self, snap: WatchlistSnapshot) -> str:
        return (
            "🟠 <b>AUTOMATIC WATCHLIST ACTIVATED</b>\n"
            "Future test-like transfers of this relationship will trigger a 🔴 RED alert.\n\n"
            + self._pattern_block(snap)
        )

    def test_confirmed_alert(self, *, sender: str, recipient: str, amount_raw: int, tx_hash: str) -> str:
        return (
            "✅ <b>Watchlist test transfer confirmed on-chain</b>\n"
            f"{short(sender)} → {short(recipient)}: {fmt_usdt(amount_raw)} USDT\n"
            f"{self.tx_link(tx_hash)}"
        )

    def backfill_summary(self, *, days: int, analysed: int, snaps: list[WatchlistSnapshot]) -> str:
        active = [s for s in snaps if s.status == WatchlistStatus.ACTIVE]
        lines = [
            "📚 <b>HISTORICAL BACKFILL COMPLETE</b>",
            f"History analysed: {days} days, {analysed:,} candidate relationships",
            f"Automatic watchlist: {len(active)} ACTIVE, {len(snaps) - len(active)} other",
        ]
        for i, s in enumerate(sorted(active, key=lambda x: -x.confidence_score)[:10], 1):
            lines.append(
                f"{i}. {short(s.sender)} → {short(s.recipient)}: "
                f"{fmt_range(s.test_min_raw, s.test_max_raw, compact=True)} → "
                f"{fmt_range(s.large_min_raw, s.large_max_raw, compact=True)} USDT "
                f"({s.successful_sequences} seq, {s.confidence})"
            )
        if len(active) > 10:
            lines.append(f"… use /watchlist to see all {len(active)}")
        return "\n".join(lines)

    # ------------------------------------------------------------ command replies
    def watchlist_page(self, snaps: list[WatchlistSnapshot], page: int, pages: int, total: int, offset: int) -> str:
        if not snaps:
            return "🔴 <b>AUTOMATIC WATCHLIST</b>\n\nNo relationships discovered yet."
        parts = [f"🔴 <b>AUTOMATIC WATCHLIST</b> (page {page}/{pages}, {total} entries)\n"]
        for i, s in enumerate(snaps, offset + 1):
            parts.append(
                f"<b>{i}.</b>\nSender:\n{_code(s.sender)}\nRecipient:\n{_code(s.recipient)}\n"
                f"Pattern:\n{fmt_range(s.test_min_raw, s.test_max_raw, compact=True)} USDT → "
                f"{fmt_range(s.large_min_raw, s.large_max_raw, compact=True)} USDT\n"
                f"Successful Sequences:\n{s.successful_sequences}\n"
                f"Confidence:\n{s.confidence}\n"
                f"Status:\n{STATUS_ICON.get(s.status.value, '')} {s.status.value}\n"
            )
        if page < pages:
            parts.append(f"Next page: /watchlist {page + 1}")
        return "\n".join(parts)

    def pattern_detail(self, snap: WatchlistSnapshot, extra: dict, sequences: list) -> str:
        comps = extra.get("components", {})
        reasons = extra.get("reasons", [])
        seq_lines = [
            f"• {fmt_usdt(int(s.test_amount_raw))} → {fmt_usdt(int(s.large_amount_raw))} USDT "
            f"in {humanize_seconds(s.time_difference_seconds)}{'' if s.is_inlier else ' (outlier)'}"
            for s in sequences[:10]
        ]
        comp_line = ", ".join(f"{k} {v:.2f}" for k, v in comps.items() if k != "score")
        return (
            "🔎 <b>PATTERN DETAIL</b>\n\n"
            + self._pattern_block(snap)
            + f"\n<b>Learned match band:</b>\n{fmt_usdt(snap.match_low_raw)} – {fmt_usdt(snap.match_high_raw)} USDT\n"
            + f"<b>Follow-up watch window:</b>\n{humanize_seconds(snap.followup_window_seconds)}\n"
            + f"<b>Pattern strength:</b> {snap.pattern_strength:.2f}\n"
            + (f"<b>Signals:</b> {comp_line}\n" if comp_line else "")
            + (f"<b>Not active because:</b> {'; '.join(reasons)}\n" if reasons else "")
            + ("\n<b>Recent sequences:</b>\n" + "\n".join(seq_lines) if seq_lines else "")
        )


def compact_amount(raw: int) -> str:
    return fmt_compact(raw)
