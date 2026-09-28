"""Telegram Bot API integration (raw HTTPS, long polling).

* ``TelegramClient``  – thin API wrapper with retry/429 handling; never logs the token.
* ``AlertDispatcher`` – single consumer queue: sends alerts, marks them 'sent' in the DB.
  Undelivered alerts stay 'pending' and are re-queued on restart, so an alert is never
  lost when Telegram is down and never sent twice by concurrent senders.
* ``TelegramBot``     – admin-only command handling (/start /status /wallet /reset /help, plus the
  hidden /letsgo, which is never shown in any message, menu or button).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

import httpx

from app import formatting
from app.config import Settings
from app.database import Repository
from app.logger import kv
from app.stats import MonitorStats
from app.timeutil import format_utc, now_ms
from app.tron_client import TronClient
from app.tx_limit import TxLimitTracker

NOTICE_PREFIX = "notice:"

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Intro"),
    ("status", "Monitoring status and latency"),
    ("wallet", "Monitored wallet and amount range"),
    ("reset", "Restart the transaction counter at 0"),
    ("help", "Help"),
]


class TelegramApiError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None,
                 description: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.description = description


class TelegramClient:
    def __init__(
        self,
        token: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        # The token is part of the URL path: never log URLs (httpx logging is silenced).
        self._http = httpx.AsyncClient(
            base_url=f"{api_base}/bot{token}", timeout=httpx.Timeout(timeout), transport=transport
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        try:
            response = await self._http.post(
                f"/{method}", json=payload or {}, timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT
            )
        except httpx.TimeoutException as exc:
            raise TelegramApiError(f"{method}: timeout") from exc
        except httpx.TransportError as exc:
            raise TelegramApiError(f"{method}: network error {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramApiError(f"{method}: invalid JSON (HTTP {response.status_code})",
                                   retryable=response.status_code >= 500) from exc
        if data.get("ok"):
            return data.get("result")
        description = str(data.get("description", ""))
        code = int(data.get("error_code") or response.status_code)
        params = data.get("parameters") or {}
        if code == 429:
            raise TelegramApiError(f"{method}: rate limited", retry_after=float(params.get("retry_after", 5)),
                                   description=description)
        raise TelegramApiError(f"{method}: HTTP {code} {description}", retryable=code >= 500 or code == 409,
                               description=description)

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = "HTML",
                           reply_markup: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)


RESUME_CALLBACK = "resume"
# Buttons are no longer sent (the start command is kept hidden); RESUME_CALLBACK is still
# handled so buttons on messages sent by older versions keep working.


class AlertDispatcher:
    def __init__(
        self,
        settings: Settings,
        client: TelegramClient,
        repo: Repository,
        stats: MonitorStats,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_backoff: float = 60.0,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repo = repo
        self.stats = stats
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._sleep = sleep
        self.max_backoff = max_backoff
        self.limit_tracker: TxLimitTracker | None = None

    async def enqueue_notice(self, cycle: int) -> None:
        await self.enqueue(f"{NOTICE_PREFIX}{cycle}")

    async def enqueue(self, tx_hash: str) -> None:
        if tx_hash in self._queued:
            return
        self._queued.add(tx_hash)
        await self._queue.put(tx_hash)
        self.stats.alert_queue_size = self._queue.qsize()

    async def load_pending(self) -> int:
        pending = await self.repo.pending_alerts()
        for tx in pending:
            await self.enqueue(tx.tx_hash)
        if pending:
            log.info("pending_alerts_requeued", extra=kv(count=len(pending)))
        return len(pending)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            tx_hash = await self._queue.get()
            try:
                await self._deliver_item(tx_hash, stop)
            finally:
                self._queued.discard(tx_hash)
                self.stats.alert_queue_size = self._queue.qsize()

    async def drain(self) -> None:
        """Deliver everything currently queued (used by tests)."""
        stop = asyncio.Event()
        while not self._queue.empty():
            tx_hash = self._queue.get_nowait()
            try:
                await self._deliver_item(tx_hash, stop)
            finally:
                self._queued.discard(tx_hash)

    async def _deliver_item(self, item: str, stop: asyncio.Event) -> bool:
        if item.startswith(NOTICE_PREFIX):
            return await self._deliver_notice(int(item[len(NOTICE_PREFIX):]), stop)
        return await self._deliver(item, stop)

    async def _deliver_notice(self, cycle: int, stop: asyncio.Event) -> bool:
        tracker = self.limit_tracker
        if tracker is None or await tracker.already_notified(cycle) or await tracker.cycle() != cycle:
            return False
        text = formatting.build_limit_message(await tracker.count(), tracker.threshold, tracker.paused)
        if await self._broadcast(text, stop, label=f"limit_notice_cycle_{cycle}"):
            await tracker.mark_notified(cycle)
            log.info("tx_limit_notice_sent", extra=kv(cycle=cycle))
            return True
        return False

    async def _broadcast(self, text: str, stop: asyncio.Event, label: str,
                         reply_markup: dict[str, Any] | None = None) -> bool:
        """Send ``text`` to every admin chat, retrying until delivered.

        A chat that fails permanently (e.g. the person never pressed Start, or blocked
        the bot) is skipped once at least one other chat has received the message, so
        one unreachable chat never holds up alerts for the others. If *no* chat can be
        reached, keep retrying: the message is never silently dropped.
        Returns False only when shutting down before delivery.
        """
        pending = list(self.settings.telegram_admin_chat_ids)
        delivered = 0
        parse_mode: str | None = "HTML"
        backoff = 1.0
        while pending and not stop.is_set():
            retry: list[int] = []
            permanent: list[tuple[int, TelegramApiError]] = []
            delay = 0.0
            for chat_id in pending:
                try:
                    await self.client.send_message(chat_id, text, parse_mode=parse_mode,
                                                   reply_markup=reply_markup)
                    delivered += 1
                except TelegramApiError as exc:
                    self.stats.alert_failures += 1
                    if "can't parse entities" in exc.description and parse_mode:
                        log.warning("telegram_html_rejected_retrying_plain", extra=kv(item=label))
                        parse_mode = None
                        retry.append(chat_id)
                    elif exc.retryable:
                        retry.append(chat_id)
                        delay = max(delay, exc.retry_after if exc.retry_after is not None else backoff)
                    else:
                        permanent.append((chat_id, exc))
            for chat_id, exc in permanent:
                if delivered:
                    log.error("telegram_chat_unreachable_skipped",
                              extra=kv(item=label, chat_id=chat_id, error=str(exc),
                                       hint="that person must open the bot and press Telegram's Start"))
                else:
                    retry.append(chat_id)
                    delay = max(delay, backoff)
            pending = retry
            if pending:
                log.warning("telegram_send_failed",
                            extra=kv(item=label, chats=len(pending), retry_in_s=round(delay, 1)))
                await self._sleep(delay + random.uniform(0, 0.25))
                backoff = min(backoff * 2, self.max_backoff)
        return not pending

    async def _deliver(self, tx_hash: str, stop: asyncio.Event) -> bool:
        tx = await self.repo.get_transaction(tx_hash)
        if tx is None or tx.alert_status == "sent":
            return False
        if self.settings.wallet_created_notice:
            # "Wallet created" goes first, then the transaction alert.
            if not await self._broadcast(formatting.build_wallet_created_message(tx, self.settings.gpu_type), stop,
                                         label=f"wallet_created_{tx_hash[:12]}"):
                return False
            log.info("wallet_created_notice_sent", extra=kv(tx_hash=tx_hash))
        text = formatting.build_alert_message(tx, self.settings)
        if not await self._broadcast(text, stop, label=f"alert_{tx_hash[:12]}"):
            return False
        sent_ms = now_ms()
        await self.repo.mark_alert_sent(tx_hash, sent_ms)
        self.stats.alerts_sent += 1
        alert_latency = sent_ms - tx.block_timestamp_ms
        if not tx.is_backfill:
            self.stats.last_alert_latency_ms = alert_latency
        log.info(
            "telegram_alert_sent",
            extra=kv(
                tx_hash=tx_hash,
                chats=len(self.settings.telegram_admin_chat_ids),
                block_time=format_utc(tx.block_timestamp_ms, with_millis=True),
                detected=format_utc(tx.detected_at_ms, with_millis=True),
                alerted=format_utc(sent_ms, with_millis=True),
                detection_latency_s=f"{(tx.detected_at_ms - tx.block_timestamp_ms) / 1000:.3f}",
                alert_latency_s=f"{alert_latency / 1000:.3f}",
                telegram_send_s=f"{(sent_ms - tx.detected_at_ms) / 1000:.3f}",
            ),
        )
        return True


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        client: TelegramClient,
        repo: Repository,
        stats: MonitorStats,
        tron: TronClient,
        limit_tracker: TxLimitTracker | None = None,
    ) -> None:
        self.limit_tracker = limit_tracker
        self.settings = settings
        self.client = client
        self.repo = repo
        self.stats = stats
        self.tron = tron
        self._offset: int | None = None

    def is_authorized(self, chat_id: Any) -> bool:
        try:
            return int(chat_id) in self.settings.telegram_admin_chat_ids
        except (TypeError, ValueError):
            return False

    async def setup(self) -> None:
        for method, payload in (
            ("deleteWebhook", {"drop_pending_updates": False}),
            ("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}),
        ):
            try:
                await self.client.call(method, payload)
            except TelegramApiError as exc:
                log.warning("telegram_setup_call_failed", extra=kv(method=method, error=str(exc)))

    async def notify_admin(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        """Best-effort message to every admin chat (startup notices)."""
        for chat_id in self.settings.telegram_admin_chat_ids:
            try:
                await self.client.send_message(chat_id, text, reply_markup=reply_markup)
            except TelegramApiError as exc:
                log.warning("telegram_notify_failed", extra=kv(chat_id=chat_id, error=str(exc)))

    async def run(self, stop: asyncio.Event) -> None:
        await self.setup()
        log.info("telegram_polling_started")
        backoff = 1.0
        while not stop.is_set():
            try:
                payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if self._offset is not None:
                    payload["offset"] = self._offset
                updates = await self.client.call("getUpdates", payload, timeout=35)
                backoff = 1.0
                for update in updates or []:
                    self._offset = int(update.get("update_id", 0)) + 1
                    try:
                        await self.handle_update(update)
                    except Exception:
                        log.exception("telegram_update_handler_failed")
            except asyncio.CancelledError:
                raise
            except TelegramApiError as exc:
                delay = exc.retry_after if exc.retry_after is not None else backoff
                log.warning("telegram_polling_error", extra=kv(error=str(exc), retry_in_s=round(delay, 1)))
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, 60.0)

    async def handle_callback(self, query: dict[str, Any]) -> str | None:
        """Inline button presses (only on messages sent by older versions)."""
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        user_id = (query.get("from") or {}).get("id")
        authorized = self.is_authorized(chat_id) or self.is_authorized(user_id)
        # An old 🚀 Let's go button pressed while already running must not reset the count.
        running = self.limit_tracker is None or not self.limit_tracker.paused
        answer = "⛔ Unauthorized" if not authorized else ("✅ Already running" if running else "")
        try:
            await self.client.call("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": answer})
        except TelegramApiError:
            pass
        if not authorized:
            log.warning("unauthorized_callback_rejected", extra=kv(chat_id=chat_id, user_id=user_id))
            return None
        if query.get("data") != RESUME_CALLBACK or running:
            return None
        reply = await self.resume_monitoring()
        log.info("command_handled", extra=kv(command="letsgo_button", user_id=user_id))
        return reply

    async def resume_monitoring(self) -> str:
        """Hidden /letsgo: start monitoring from now with a new count. Tells every admin."""
        tracker = self.limit_tracker
        if tracker is None:
            return formatting.build_start_message(self.settings)
        was_paused, cycle = await tracker.resume()
        self.stats.paused = False
        text = formatting.build_resumed_message(tracker.threshold, cycle, was_paused)
        await self.notify_admin(text)
        return text

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        if isinstance(update.get("callback_query"), dict):
            return await self.handle_callback(update["callback_query"])
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = (message.get("chat") or {}).get("id")
        if not isinstance(text, str) or chat_id is None or not text.startswith("/"):
            return None
        command = text.split()[0].split("@")[0].lower()
        if not self.is_authorized(chat_id):
            user_id = (message.get("from") or {}).get("id")
            log.warning("unauthorized_command_rejected", extra=kv(chat_id=chat_id, user_id=user_id, command=command))
            try:
                await self.client.send_message(chat_id, "⛔ Unauthorized. This bot is private.", parse_mode=None)
            except Exception:
                pass
            return None
        if command == "/letsgo":
            if self.limit_tracker is not None and self.limit_tracker.paused:
                # resume_monitoring() already notifies every admin chat (including this one)
                reply = await self.resume_monitoring()
                log.info("command_handled", extra=kv(command=command))
                return reply
            reply = "✅ Already running – monitoring is active."
            await self.client.send_message(chat_id, reply)
            return reply
        reply = await self.render_command(command)
        if reply is None:
            reply = "Unknown command. Use /help."
        await self.client.send_message(chat_id, reply)
        log.info("command_handled", extra=kv(command=command))
        return reply

    async def render_command(self, command: str) -> str | None:
        if command == "/start":
            paused = self.limit_tracker is not None and self.limit_tracker.paused
            return formatting.build_start_message(self.settings, paused)
        if command == "/help":
            return formatting.build_help_message(self.settings)
        if command == "/wallet":
            return formatting.build_wallet_message(self.settings, self.stats)
        if command == "/reset":
            if self.limit_tracker is None or not self.limit_tracker.enabled:
                return "Transaction counter is disabled (TX_LIMIT_THRESHOLD=0)."
            cycle = await self.limit_tracker.reset()  # counter only; starting is /letsgo's job
            return formatting.build_resumed_message(self.limit_tracker.threshold, cycle, False,
                                                    self.limit_tracker.paused)
        if command == "/status":
            return formatting.build_status_message(
                self.settings,
                self.stats,
                detected_total=await self.repo.count_transactions(),
                last_match=await self.repo.latest_transaction(),
                api_last_ms=self.tron.stats.last_latency_ms,
                api_avg_ms=self.tron.stats.avg_latency_ms,
                pending_alerts=len(await self.repo.pending_alerts()),
                limit_count=await self.limit_tracker.count() if self.limit_tracker else None,
            )
        return None
