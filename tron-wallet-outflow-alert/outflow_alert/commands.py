"""Telegram commands via long polling.

Only chats listed in TELEGRAM_CHAT_ID are answered; everything else is ignored.

  /start, /status  - status card with Pause / Resume / Refresh buttons
  /pause           - stop polling and alerting
  /resume          - resume from now (transfers made while paused are not alerted)
  /help            - command list
"""

from __future__ import annotations

import asyncio
import logging

from outflow_alert.monitor import Monitor
from outflow_alert.storage import now_ms
from outflow_alert.telegram import SendError, StatusSnapshot, TelegramClient, format_status

log = logging.getLogger(__name__)

LONG_POLL_SECONDS = 25
BOT_COMMANDS = [
    {"command": "status", "description": "Bot status"},
    {"command": "pause", "description": "Pause monitoring"},
    {"command": "resume", "description": "Resume monitoring"},
    {"command": "help", "description": "Show commands"},
]
HELP_TEXT = (
    "🤖 <b>Commands</b>\n"
    "/status – bot status\n"
    "/pause – pause monitoring and alerts\n"
    "/resume – resume monitoring (from now)\n"
    "/help – this list"
)


def status_keyboard(paused: bool) -> dict:
    toggle = {"text": "▶️ Resume", "callback_data": "resume"} if paused else {"text": "⏸️ Pause", "callback_data": "pause"}
    return {"inline_keyboard": [[toggle, {"text": "🔄 Refresh", "callback_data": "status"}]]}


class CommandHandler:
    def __init__(self, monitor: Monitor, telegram: TelegramClient) -> None:
        self.m = monitor
        self.tg = telegram
        self.allowed = set(monitor.s.telegram_chat_ids)

    def status_text(self) -> str:
        tron = self.m.tron
        return format_status(
            StatusSnapshot(
                paused=self.m.paused,
                poll_error=self.m.last_poll_error,
                wallet=self.m.s.wallet,
                contract=self.m.s.usdt_contract,
                last_latency_ms=tron.last_latency_ms,
                avg_latency_ms=tron.avg_latency_ms,
                poll_interval_seconds=self.m.s.poll_interval_seconds,
                confirmation_mode=self.m.s.confirmation_mode,
                last_poll_ms=self.m.last_poll_ms,
                transfers_checked=self.m.db.transfers_checked(),
                now_ms=now_ms(),
            )
        )

    async def register_commands(self) -> None:
        try:
            await self.tg.call("setMyCommands", {"commands": BOT_COMMANDS})
        except SendError as exc:
            log.warning("setMyCommands failed: %s", exc)

    # ---------------------------------------------------------------- dispatch
    def _apply(self, action: str) -> str | None:
        """Run a state-changing action; returns a short notice for the user."""
        if action == "pause":
            return "⏸️ Monitoring paused." if self.m.pause() else "Already paused."
        if action == "resume":
            return "▶️ Monitoring resumed." if self.m.resume() else "Already running."
        return None

    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or update.get("channel_post") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text.startswith("/") or not chat_id:
            return
        if chat_id not in self.allowed:
            log.warning("ignoring command from unauthorised chat %s", chat_id)
            return
        command = text.split()[0][1:].split("@", 1)[0].lower()
        if command in ("start", "status"):
            await self.tg.send_message(chat_id, self.status_text(), reply_markup=status_keyboard(self.m.paused))
        elif command in ("pause", "resume"):
            notice = self._apply(command)
            await self.tg.send_message(
                chat_id, f"{notice}\n\n{self.status_text()}", reply_markup=status_keyboard(self.m.paused)
            )
        elif command == "help":
            await self.tg.send_message(chat_id, HELP_TEXT)

    async def _handle_callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        action = query.get("data", "")
        if chat_id not in self.allowed:
            await self.tg.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Not authorised"})
            return
        notice = self._apply(action) if action in ("pause", "resume") else "🔄 Refreshed"
        await self.tg.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": notice or ""})
        if message.get("message_id"):
            await self.tg.edit_message(
                chat_id, message["message_id"], self.status_text(), reply_markup=status_keyboard(self.m.paused)
            )

    # -------------------------------------------------------------------- loop
    async def poll_updates_once(self, timeout: int = LONG_POLL_SECONDS) -> int:
        offset = int(self.m.db.get("telegram_offset") or 0)
        updates = await self.tg.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "channel_post", "callback_query"]},
            timeout=timeout + 10,
        )
        for update in updates or []:
            # Persist the offset first so a bad update is never retried forever.
            self.m.db.set("telegram_offset", str(int(update["update_id"]) + 1))
            try:
                await self.handle_update(update)
            except SendError as exc:
                log.warning("command reply failed: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("error handling update %s", update.get("update_id"))
        return len(updates or [])

    async def run(self) -> None:
        await self.register_commands()
        delay = 1.0
        while True:
            try:
                await self.poll_updates_once()
                delay = 1.0
            except SendError as exc:
                if exc.status == 409:
                    log.warning("getUpdates conflict (a webhook or another instance is active): %s", exc)
                    try:
                        await self.tg.call("deleteWebhook", {"drop_pending_updates": False})
                    except SendError:
                        pass
                else:
                    log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(exc.retry_after or delay)
                delay = min(delay * 2, 60.0)
