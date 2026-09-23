"""Telegram message formatting (HTML parse mode).

Telegram cannot colour text, so the design uses colour-coded emoji, bold
headings, quote boxes (<blockquote>) for grouped details, a confidence bar,
tap-to-copy addresses (<code>) and clickable Tronscan links.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.amounts import fmt_compact, fmt_range, fmt_usdt, fmt_usdt_approx
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
DIVIDER = "━━━━━━━━━━━━━━━━━━"
ADDRESS_URL = "https://tronscan.org/#/address/"


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


def confidence_bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, round(score * width)))
    return "▰" * filled + "▱" * (width - filled)


def _code(addr: str) -> str:
    return f"<code>{html.escape(addr)}</code>"


def _addr(addr: str) -> str:
    """Tap-to-copy address plus a small Tronscan link."""
    return f"{_code(addr)} <a href=\"{ADDRESS_URL}{html.escape(addr)}\">↗</a>"


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
        label = "UTC" if hours == 0 else f"UTC{'+' if hours > 0 else '-'}{abs(hours):g}"
        return f"{local:%Y-%m-%d %H:%M:%S} {label}"

    def tx_link(self, tx_hash: str) -> str:
        return f"{self.tx_url}{tx_hash}"

    def _tx_anchor(self, tx_hash: str, label: str = "View on Tronscan") -> str:
        return f"🔗 <a href=\"{self.tx_link(tx_hash)}\">{label}</a>"

    def _confidence_line(self, snap: WatchlistSnapshot) -> str:
        return (
            f"{CONF_ICON.get(snap.confidence, '⚪')} <b>{snap.confidence}</b>  "
            f"{confidence_bar(snap.confidence_score)}  <code>{snap.confidence_score:.2f}</code>"
        )

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
        larges = " · ".join(f"${fmt_usdt_approx(a)}" for a in snap.recent_large_amounts) or "—"
        latency = max(0.0, (detected_at - tx_time).total_seconds())
        status = "✅ CONFIRMED" if confirmed else "⏳ UNCONFIRMED <i>(not yet final on-chain)</i>"
        return (
            "🚨🔴 <b>WATCHLIST TEST TRANSFER DETECTED</b> 🔴🚨\n"
            "<i>⚠️ Known test-transfer pattern repeated — a large transfer may follow</i>\n"
            f"{DIVIDER}\n"
            f"💵 <b>Test amount:</b>  <code>{fmt_usdt(amount_raw)} USDT</code>\n\n"
            f"📤 <b>Sender</b>\n{_addr(snap.sender)}\n"
            f"📥 <b>Recipient</b>\n{_addr(snap.recipient)}\n\n"
            "<blockquote>📊 <b>Learned pattern</b>\n"
            f"🧪 Test range: <b>{fmt_range(snap.test_min_raw, snap.test_max_raw)} USDT</b>\n"
            f"💰 Past large transfers: {larges}\n"
            f"🎯 Typical follow-up: <b>≈ ${fmt_usdt_approx(snap.typical_large_raw)}</b>\n"
            f"⏱ Usually within {humanize_seconds(snap.followup_p80_seconds)}"
            f" · watching {humanize_seconds(snap.followup_window_seconds)}\n"
            f"🔁 Successful sequences: <b>{snap.successful_sequences}</b></blockquote>\n"
            f"🛡 <b>Confidence</b>  {self._confidence_line(snap)}\n"
            f"📌 <b>Status:</b> {status}\n"
            f"{DIVIDER}\n"
            f"🕒 Block: {self.fmt_time(tx_time)}\n"
            f"📡 Detected: {self.fmt_time(detected_at)} · ⚡ {latency:.1f} s\n"
            f"{self._tx_anchor(tx_hash)}"
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
            "🚨💰 <b>LARGE FOLLOW-UP DETECTED</b> 💰🚨\n"
            "<i>The large transfer after the test has arrived</i>\n"
            f"{DIVIDER}\n"
            f"🧪 <b>Test:</b>   <code>{fmt_usdt(test_amount_raw)} USDT</code>\n"
            f"💰 <b>Large:</b>  <code>{fmt_usdt(amount_raw)} USDT</code>  <i>(≈{ratio:,}x)</i>\n"
            f"⏱ <b>Time Difference:</b> {humanize_seconds(dt_seconds)}\n\n"
            f"📤 <b>Sender</b>\n{_addr(sender)}\n"
            f"📥 <b>Recipient</b>\n{_addr(recipient)}\n\n"
            "<blockquote>📈 Historical Pattern: <b>TEST → LARGE</b> — confirmed again, model updated</blockquote>\n"
            f"📌 <b>Status:</b> {'✅ CONFIRMED' if confirmed else '⏳ UNCONFIRMED'}\n"
            f"🕒 Detected: {self.fmt_time(detected_at)}\n"
            f"{self._tx_anchor(tx_hash, 'Large transaction')}  ·  "
            f"<a href=\"{self.tx_link(test_tx_hash)}\">Test transaction</a>"
        )

    def _pattern_block(self, snap: WatchlistSnapshot) -> str:
        return (
            f"📤 <b>Sender</b>\n{_addr(snap.sender)}\n"
            f"📥 <b>Recipient</b>\n{_addr(snap.recipient)}\n\n"
            "<blockquote>🔬 <b>Pattern: TEST → LARGE</b>\n"
            f"🧪 Test amounts: <b>{fmt_range(snap.test_min_raw, snap.test_max_raw)} USDT</b>"
            f" (typ. ≈ {fmt_usdt_approx(snap.typical_test_raw)})\n"
            f"💰 Large amounts: <b>{fmt_range(snap.large_min_raw, snap.large_max_raw)} USDT</b>"
            f" (typ. ≈ {fmt_usdt_approx(snap.typical_large_raw)})\n"
            f"📐 Test → large ratio: ≈ {float(snap.ratio):,.0f}x\n"
            f"🔁 Successful sequences: <b>{snap.successful_sequences}</b>\n"
            f"🎯 Success rate: {snap.success_rate:.0%}\n"
            f"⏱ Typical follow-up: {humanize_seconds(snap.typical_followup_seconds)}</blockquote>\n"
            f"🛡 <b>Confidence</b>  {self._confidence_line(snap)}\n"
            f"📌 <b>Status:</b> {STATUS_ICON.get(snap.status.value, '')} {snap.status.value}"
        )

    def new_pattern_alert(self, snap: WatchlistSnapshot) -> str:
        return (
            "🟡 <b>NEW USDT TEST → LARGE PATTERN DISCOVERED</b>\n"
            "<i>Candidate — more evidence needed before it goes on the active watchlist</i>\n"
            f"{DIVIDER}\n" + self._pattern_block(snap)
        )

    def activated_alert(self, snap: WatchlistSnapshot) -> str:
        return (
            "🟠 <b>AUTOMATIC WATCHLIST ACTIVATED</b>\n"
            "<i>Its next test-like transfer will trigger a 🔴 RED alert</i>\n"
            f"{DIVIDER}\n" + self._pattern_block(snap)
        )

    def test_confirmed_alert(self, *, sender: str, recipient: str, amount_raw: int, tx_hash: str) -> str:
        return (
            "✅ <b>Watchlist test transfer confirmed on-chain</b>\n"
            f"💵 <code>{fmt_usdt(amount_raw)} USDT</code>\n"
            f"📤 {_code(sender)}\n📥 {_code(recipient)}\n"
            f"{self._tx_anchor(tx_hash)}"
        )

    def startup_message(self, *, entries: int, min_seq: int, ratio: str, min_conf: str) -> str:
        return (
            "🟢 <b>TRON USDT pattern monitor started</b>\n"
            f"{DIVIDER}\n"
            f"👁 Watching every USDT TRC-20 transfer, live\n"
            f"📋 Watchlist entries: <b>{entries}</b>\n"
            "<blockquote>⚙️ <b>Rules</b>\n"
            f"🔁 Min sequences: {min_seq}\n"
            f"📐 Min large/test ratio: {ratio}x\n"
            f"🛡 Min confidence: {min_conf}</blockquote>\n"
            "Send /help for commands"
        )

    def backfill_summary(self, *, days: int, analysed: int, snaps: list[WatchlistSnapshot]) -> str:
        active = [s for s in snaps if s.status == WatchlistStatus.ACTIVE]
        lines = [
            "📚 <b>HISTORICAL BACKFILL COMPLETE</b>",
            DIVIDER,
            f"🗓 History analysed: <b>{days} days</b>",
            f"🔎 Relationships checked: <b>{analysed:,}</b>",
            f"🟢 Active watchlist: <b>{len(active)}</b> · 📋 other entries: {len(snaps) - len(active)}",
        ]
        if active:
            lines.append("")
            lines.append("🏆 <b>Top patterns</b>")
            for i, s in enumerate(sorted(active, key=lambda x: -x.confidence_score)[:10], 1):
                lines.append(
                    f"{i}. {CONF_ICON.get(s.confidence, '')} <code>{s.sender[:6]}…{s.sender[-4:]}</code> → "
                    f"<code>{s.recipient[:6]}…{s.recipient[-4:]}</code>\n"
                    f"    🧪 {fmt_range(s.test_min_raw, s.test_max_raw, compact=True)} → "
                    f"💰 {fmt_range(s.large_min_raw, s.large_max_raw, compact=True)} USDT · 🔁 {s.successful_sequences}"
                )
            if len(active) > 10:
                lines.append(f"… /watchlist shows all {len(active)}")
        return "\n".join(lines)

    # ------------------------------------------------------------ command replies
    def watchlist_page(self, snaps: list[WatchlistSnapshot], page: int, pages: int, total: int, offset: int) -> str:
        if not snaps:
            return (
                "🔴 <b>AUTOMATIC WATCHLIST</b>\n"
                f"{DIVIDER}\n"
                "📭 No relationships discovered yet.\n"
                "<i>Entries appear automatically once a sender repeats TEST → LARGE with the same recipient.</i>"
            )
        parts = [f"🔴 <b>AUTOMATIC WATCHLIST</b>  ·  page {page}/{pages}  ·  {total} entries", DIVIDER]
        for i, s in enumerate(snaps, offset + 1):
            parts.append(
                f"<b>{i}.</b> {STATUS_ICON.get(s.status.value, '')} <b>{s.status.value}</b>"
                f"  ·  {CONF_ICON.get(s.confidence, '')} {s.confidence}\n"
                f"📤 {_code(s.sender)}\n📥 {_code(s.recipient)}\n"
                f"🧪 {fmt_range(s.test_min_raw, s.test_max_raw, compact=True)} USDT → "
                f"💰 {fmt_range(s.large_min_raw, s.large_max_raw, compact=True)} USDT"
                f"  ·  🔁 {s.successful_sequences}\n"
            )
        if page < pages:
            parts.append(f"➡️ Next page: /watchlist {page + 1}")
        return "\n".join(parts)

    def pattern_detail(self, snap: WatchlistSnapshot, extra: dict, sequences: list) -> str:
        comps = extra.get("components", {})
        reasons = extra.get("reasons", [])
        seq_lines = [
            f"{'✅' if s.is_inlier else '▫️'} {fmt_usdt(int(s.test_amount_raw))} → {fmt_usdt(int(s.large_amount_raw))} USDT"
            f"  <i>in {humanize_seconds(s.time_difference_seconds)}</i>"
            for s in sequences[:10]
        ]
        comp_lines = [
            f"{name:<12} {confidence_bar(v, 8)} {v:.2f}" for name, v in comps.items() if name != "score"
        ]
        out = (
            "🔎 <b>PATTERN DETAIL</b>\n"
            f"{DIVIDER}\n"
            + self._pattern_block(snap)
            + f"\n🎚 <b>Match band:</b> <code>{fmt_usdt(snap.match_low_raw)} – {fmt_usdt(snap.match_high_raw)} USDT</code>\n"
            + f"👁 <b>Follow-up watch window:</b> {humanize_seconds(snap.followup_window_seconds)}\n"
            + f"💪 <b>Pattern strength:</b> {snap.pattern_strength:.2f}\n"
        )
        if comp_lines:
            out += "\n📊 <b>Signals</b>\n<pre>" + "\n".join(comp_lines) + "</pre>\n"
        if reasons:
            out += f"\n⚠️ <b>Not active because:</b> {html.escape('; '.join(reasons))}\n"
        if seq_lines:
            out += "\n🔁 <b>Recent sequences</b>\n" + "\n".join(seq_lines)
        return out


def compact_amount(raw: int) -> str:
    return fmt_compact(raw)
