"""Admin-only Telegram commands.

Only messages from TELEGRAM_ADMIN_CHAT_ID are answered; everything else is
ignored silently (and logged without content).  No command can move funds,
add keys or reveal configuration secrets - the bot is read-only.
"""

from __future__ import annotations

import asyncio
import time
from html import escape

from app.alerts.formatter import code, fmt_ts, humanize, tx_link
from app.amounts import fmt_usdt
from app.db import repository as repo
from app.logging_setup import get_logger
from app.monitor.loop import sleep_or_stop
from app.runtime import Runtime
from app.telegram.client import TelegramClient, TelegramError

log = get_logger(__name__)

STATE_TG_OFFSET = "telegram_update_offset"

COMMANDS = [
    ("start", "Introduction"),
    ("status", "System status"),
    ("stats", "Statistics"),
    ("wallets", "Discovered wallets (paginated)"),
    ("recent", "Recent large transfers"),
    ("pause", "Pause alerts"),
    ("resume", "Resume alerts"),
    ("help", "Help"),
]

HELP = (
    "🛰 <b>TRON USDT Wallet Monitor</b> (read-only)\n\n"
    "Watches Wallet A, auto-discovers every wallet it sends USDT to, and alerts when any "
    "discovered wallet receives ≥ <b>{threshold} USDT</b> from anyone.\n\n"
    "/status – system status\n"
    "/stats – statistics\n"
    "/wallets [page] – discovered wallets\n"
    "/recent – recent large transfers\n"
    "/pause – pause alerts (monitoring continues)\n"
    "/resume – resume alerts\n"
    "/help – this message"
)


class CommandHandler:
    def __init__(self, rt: Runtime, tg: TelegramClient) -> None:
        self.rt = rt
        self.tg = tg
        self.admin = str(rt.settings.telegram_admin_chat_id)

    # ------------------------------------------------------------- dispatch
    def is_admin(self, chat_id) -> bool:
        return str(chat_id) == self.admin

    async def handle_update(self, upd: dict) -> None:
        if cb := upd.get("callback_query"):
            chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
            if not self.is_admin(chat_id) or not self.is_admin((cb.get("from") or {}).get("id", chat_id)):
                log.warning("Ignored callback from unauthorized chat", chat_id=chat_id)
                return
            await self.tg.answer_callback(cb.get("id", ""))
            data = str(cb.get("data") or "")
            if data.startswith("wallets:"):
                text, markup = await self.wallets_text(int(data.split(":", 1)[1] or 1))
                try:
                    await self.tg.edit_message(self.admin, cb["message"]["message_id"], text, markup)
                except TelegramError as exc:
                    if "not modified" not in str(exc):
                        raise
            return
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if not self.is_admin(chat_id):
            log.warning("Ignored command from unauthorized chat", chat_id=chat_id)
            return
        cmd, *args = text.split()
        cmd = cmd[1:].split("@", 1)[0].lower()
        reply, markup = await self.execute(cmd, args)
        if reply:
            await self.tg.send_message(self.admin, reply, markup)

    async def execute(self, cmd: str, args: list[str]) -> tuple[str | None, dict | None]:
        if cmd in ("start", "help"):
            return HELP.format(threshold=fmt_usdt(self.rt.settings.alert_min_base_units)), None
        if cmd == "status":
            return await self.status_text(), None
        if cmd == "stats":
            return await self.stats_text(), None
        if cmd == "wallets":
            page = int(args[0]) if args and args[0].isdigit() else 1
            return await self.wallets_text(page)
        if cmd == "recent":
            return await self.recent_text(), None
        if cmd == "pause":
            await self.rt.set_paused(True)
            log.warning("Alerts paused by admin")
            return "⏸ <b>Alerts paused.</b>\nMonitoring and discovery continue; large transfers are recorded but not sent.\nUse /resume to re-enable.", None
        if cmd == "resume":
            await self.rt.set_paused(False)
            log.info("Alerts resumed by admin")
            return "▶️ <b>Alerts resumed.</b>", None
        return "Unknown command. /help", None

    # ------------------------------------------------------------- texts
    async def status_text(self) -> str:
        rt, s = self.rt, self.rt.settings
        async with rt.session_factory() as sess:
            st = await repo.stats(sess)
        last_api = rt.last_api_success
        lag = rt.stream.lag_seconds
        lines = [
            "📊 <b>STATUS</b>",
            "",
            f"State: {'⏸ PAUSED' if rt.paused else '🟢 Running'}",
            f"Root wallet: {code(s.root_wallet)}",
            f"Discovered wallets: <b>{st['discovered']:,}</b>",
            f"Currently monitored: <b>{st['monitored']:,}</b>",
            f"Large transfers detected: <b>{st['alerts']:,}</b>",
            f"Alert threshold: ≥ {fmt_usdt(s.alert_min_base_units)} USDT",
            "",
            f"Last processed block: {rt.stream.last_block or '—'}",
            f"Stream lag: {humanize(lag) if lag is not None else '—'}",
            f"Last successful API request: {humanize(time.time() - last_api) + ' ago' if last_api else '—'}",
            f"Scheduler sweeps: {rt.scheduler.sweeps} (last {humanize(rt.scheduler.last_sweep_seconds or 0)})",
            f"Backfill: {'running' if rt.scheduler.backfill_running else ('enabled' if s.backfill_enabled else 'off')}",
            f"Pending alerts: {st['pending']}",
            f"Uptime: {humanize(time.time() - rt.started_at)}",
        ]
        if not s.is_official_usdt:
            lines += ["", f"⚠️ Contract {code(s.usdt_contract)} is NOT the official Tether USDT contract."]
        return "\n".join(lines)

    async def stats_text(self) -> str:
        async with self.rt.session_factory() as sess:
            st = await repo.stats(sess)
        largest = st["largest"]
        big = (
            f"{fmt_usdt(largest.amount_base_units)} USDT → {code(largest.discovered_wallet)}\n<a href=\"{tx_link(largest.tx_hash)}\">transaction</a>"
            if largest
            else "—"
        )
        return "\n".join(
            [
                "📈 <b>STATISTICS</b>",
                "",
                f"Total discovered wallets: <b>{st['discovered']:,}</b>",
                f"USDT transfers stored (relevant): <b>{st['transfers']:,}</b>",
                f"USDT events scanned since start: <b>{self.rt.stream.events_scanned:,}</b>",
                f"Total alerts: <b>{st['alerts']:,}</b>",
                f"Alerts in last 24h: <b>{st['alerts_24h']:,}</b>",
                f"Missed-by-stream transfers recovered: {self.rt.scheduler.recovered}",
                "",
                "Largest transfer detected:",
                big,
            ]
        )

    async def wallets_text(self, page: int) -> tuple[str, dict | None]:
        size = self.rt.settings.wallets_page_size
        page = max(1, page)
        async with self.rt.session_factory() as sess:
            rows, total = await repo.wallets_page(sess, page, size)
        pages = max(1, -(-total // size))
        if page > pages:
            page = pages
            async with self.rt.session_factory() as sess:
                rows, total = await repo.wallets_page(sess, page, size)
        lines = [f"👛 <b>DISCOVERED WALLETS</b> ({total:,}) — page {page}/{pages}", ""]
        if not rows:
            lines.append("No wallets discovered yet.")
        for i, w in enumerate(rows, start=(page - 1) * size + 1):
            amt = fmt_usdt(w.first_seen_amount_base_units) if w.first_seen_amount_base_units is not None else "?"
            flag = "" if w.active else " (inactive)"
            lines.append(f"{i}. {code(w.address)}{flag}\n    {w.discovered_at:%Y-%m-%d %H:%M} UTC · {escape(amt)} USDT")
        buttons = []
        if page > 1:
            buttons.append({"text": "◀️ Prev", "callback_data": f"wallets:{page - 1}"})
        if page < pages:
            buttons.append({"text": "Next ▶️", "callback_data": f"wallets:{page + 1}"})
        return "\n".join(lines), ({"inline_keyboard": [buttons]} if buttons else None)

    async def recent_text(self, limit: int = 10) -> str:
        async with self.rt.session_factory() as sess:
            alerts = await repo.recent_alerts(sess, limit)
        if not alerts:
            return "🕒 No large transfers detected yet."
        lines = [f"🕒 <b>RECENT LARGE TRANSFERS</b> (last {len(alerts)})", ""]
        for a in alerts:
            lines.append(
                f"• <b>{fmt_usdt(a.amount_base_units)} USDT</b> → {code(a.discovered_wallet)}\n"
                f"  from {code(a.sender)}\n"
                f"  {fmt_ts(a.transfer_timestamp)} · <a href=\"{tx_link(a.tx_hash)}\">tx</a> · {a.status}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------- loop
    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self.tg.set_commands(COMMANDS)
        except TelegramError as exc:
            log.warning("Could not register bot commands", error=str(exc)[:150])
        async with self.rt.session_factory() as s:
            saved = await repo.get_state(s, STATE_TG_OFFSET)
        offset = int(saved) if saved else None
        backoff = 1.0
        while not stop.is_set():
            try:
                updates = await self.tg.get_updates(offset)
                backoff = 1.0
            except TelegramError as exc:
                log.warning("Telegram getUpdates failed", error=str(exc)[:150])
                await sleep_or_stop(stop, exc.retry_after or backoff)
                backoff = min(backoff * 2, 60)
                continue
            for upd in updates:
                offset = int(upd.get("update_id", 0)) + 1
                try:
                    await self.handle_update(upd)
                except Exception:  # noqa: BLE001 - one bad command never stops the bot
                    log.exception("Command handling failed")
            if updates:
                try:
                    async with self.rt.session_factory() as s, s.begin():
                        await repo.set_state(s, STATE_TG_OFFSET, str(offset))
                except Exception:  # noqa: BLE001
                    log.warning("Could not persist Telegram offset")
