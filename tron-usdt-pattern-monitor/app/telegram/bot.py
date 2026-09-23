"""Telegram Bot API client and read-only command handler.

Commands (only answered in the configured TELEGRAM_CHAT_ID):
  /start /help               - introduction
  /status                    - collector health, cursor lag, queues
  /watchlist [page]          - the automatic watchlist (paginated)
  /stats                     - counters and measured alert latency
  /pattern <sender> <recipient> - full learned model of one relationship
  /pause <sender> <recipient>   - silence alerts for a relationship
  /resume <sender> <recipient>  - re-enable it

There is intentionally NO command to add wallets: the watchlist is built
automatically from on-chain behaviour.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.collector.address import is_valid_tron_address
from app.config.settings import Settings
from app.database import repository as repo
from app.domain import WatchlistStatus, ms_to_datetime
from app.logging_setup import get_logger
from app.telegram.alerts import SendError
from app.telegram.messages import MessageFormatter, humanize_seconds
from app.watchlist.model import WatchlistSnapshot

log = get_logger(__name__)

PAGE_SIZE = 8


class TelegramClient:
    def __init__(self, token: str, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/", timeout=httpx.Timeout(timeout), transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        try:
            kwargs = {"timeout": timeout} if timeout else {}
            resp = await self._client.post(method, json=payload, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SendError(f"network: {type(exc).__name__}", retryable=True) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("ok"):
            return data.get("result")
        desc = str(data.get("description") or resp.text)[:200]
        if resp.status_code == 429:
            ra = (data.get("parameters") or {}).get("retry_after", 5)
            raise SendError(f"rate limited: {desc}", retryable=True, retry_after=float(ra))
        if resp.status_code >= 500:
            raise SendError(f"telegram {resp.status_code}: {desc}", retryable=True)
        # 400/401/403: configuration problem (bad token / chat id).  Retry slowly so
        # the alert is still delivered once the operator fixes the configuration.
        raise SendError(f"telegram {resp.status_code}: {desc}", retryable=True)

    async def send_message(self, chat_id: str, text: str) -> str:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            result = await self.call("sendMessage", payload)
        except SendError as exc:
            if "can't parse entities" in str(exc):
                payload.pop("parse_mode")
                result = await self.call("sendMessage", payload)
            else:
                raise
        return str(result.get("message_id"))

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload, timeout=timeout + 10) or []

    async def get_me(self) -> dict:
        return await self.call("getMe", {})


class TelegramSink:
    def __init__(self, client: TelegramClient, chat_id: str) -> None:
        self.client = client
        self.chat_id = chat_id

    async def send(self, text: str) -> str | None:
        return await self.client.send_message(self.chat_id, text)


class CommandHandler:
    """Pure command logic (testable without Telegram)."""

    def __init__(self, app: Any) -> None:
        self.app = app  # app.main.Application
        self.fmt: MessageFormatter = app.formatter

    async def handle(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return ""
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        handlers: dict[str, Callable] = {
            "/start": self.help,
            "/help": self.help,
            "/status": self.status,
            "/watchlist": self.watchlist,
            "/stats": self.stats,
            "/pattern": self.pattern,
            "/pause": self.pause,
            "/resume": self.resume,
        }
        fn = handlers.get(cmd)
        if fn is None:
            return "Unknown command. Try /help"
        return await fn(args)

    async def help(self, _args) -> str:
        return (
            "🛰 <b>TRON USDT Test-Transfer Pattern Monitor</b> (read-only)\n\n"
            "I watch every USDT TRC-20 transfer on TRON, learn TEST → LARGE behaviour "
            "for each Sender → Recipient relationship and build the watchlist automatically. "
            "When a watched sender repeats its learned test transfer you get a 🔴 RED alert "
            "<b>before</b> the large transfer.\n\n"
            "/status – monitor health\n"
            "/watchlist [page] – automatic watchlist\n"
            "/stats – statistics &amp; latency\n"
            "/pattern &lt;sender&gt; &lt;recipient&gt; – learned model\n"
            "/pause &lt;sender&gt; &lt;recipient&gt; · /resume … – mute/unmute one relationship\n\n"
            "No wallets need to be added manually."
        )

    async def status(self, _args) -> str:
        a = self.app
        cs = a.collector.stats
        now_ms = int(time.time() * 1000)
        lag = (now_ms - cs.confirmed_cursor_ms) / 1000 if cs.confirmed_cursor_ms else None
        async with a.session_factory() as s:
            counts = await repo.watchlist_counts(s)
        bf = "complete" if cs.backfill_done else (
            f"running – at {ms_to_datetime(cs.backfill_next_ms):%Y-%m-%d %H:%M} UTC" if cs.backfill_next_ms else "running"
        )
        return (
            "📡 <b>STATUS</b>\n"
            f"Contract: <code>{a.settings.usdt_contract_address}</code>\n"
            f"Confirmed stream lag: {humanize_seconds(lag) if lag is not None else 'n/a'}\n"
            f"Unconfirmed stream: {'on' if a.settings.enable_unconfirmed else 'off'}\n"
            f"Historical backfill: {bf}\n"
            f"Transfers seen/stored: {a.stats.events_seen:,}/{a.stats.events_stored:,}\n"
            f"Analysis backlog: {a.analysis.backlog}\n"
            f"Watchlist: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty") + "\n"
            f"Alerts sent/failed (this run): {a.dispatcher.sent}/{a.dispatcher.failed}\n"
            f"API errors: {cs.api_errors} · DB errors: {cs.db_errors}"
        )

    async def watchlist(self, args) -> str:
        page = int(args[0]) if args and args[0].isdigit() else 1
        statuses = [
            WatchlistStatus.ACTIVE.value,
            WatchlistStatus.CANDIDATE.value,
            WatchlistStatus.WEAKENED.value,
            WatchlistStatus.PAUSED.value,
        ]
        async with self.app.session_factory() as s:
            _, total = await repo.list_watchlist(s, statuses, 0, 1)
            pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(max(1, page), pages)
            rows, total = await repo.list_watchlist(s, statuses, (page - 1) * PAGE_SIZE, PAGE_SIZE)
        snaps = [WatchlistSnapshot.from_entry(r) for r in rows]
        return self.fmt.watchlist_page(snaps, page, pages, total, (page - 1) * PAGE_SIZE)

    async def stats(self, _args) -> str:
        async with self.app.session_factory() as s:
            counts = await repo.table_counts(s)
            by_type = await repo.alerts_by_type(s)
            lat = sorted(await repo.recent_latencies(s, "TEST_DETECTED"))
        lines = ["📊 <b>STATS</b>"]
        lines += [f"{k}: {v:,}" for k, v in counts.items()]
        for t, d in sorted(by_type.items()):
            lines.append(f"alerts {t}: " + ", ".join(f"{k} {v}" for k, v in d.items()))
        if lat:
            p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] / 1000  # noqa: E731
            lines.append(
                f"Test-alert latency (block time → Telegram delivered): p50 {p(0.5):.1f}s, p95 {p(0.95):.1f}s, n={len(lat)}"
            )
        return "\n".join(lines)

    def _pair_args(self, args) -> tuple[str, str] | None:
        if len(args) != 2 or not all(is_valid_tron_address(a) for a in args):
            return None
        return args[0], args[1]

    async def pattern(self, args) -> str:
        pair = self._pair_args(args)
        if pair is None:
            return "Usage: /pattern &lt;sender&gt; &lt;recipient&gt; (TRON base58 addresses)"
        async with self.app.session_factory() as s:
            entry = await repo.get_watchlist(s, *pair)
            seqs = await repo.pair_sequences(s, *pair)
            wp = await repo.get_pair(s, *pair)
        if entry is None:
            if wp is None:
                return "No USDT transfers recorded for this relationship."
            return (
                "No TEST → LARGE pattern learned for this relationship yet.\n"
                f"Transfers recorded: {wp.total_transfers}, sequences found: {len(seqs)}"
            )
        extra = json.loads(entry.model_json) if entry.model_json else {}
        return self.fmt.pattern_detail(WatchlistSnapshot.from_entry(entry), extra, seqs)

    async def pause(self, args) -> str:
        pair = self._pair_args(args)
        if pair is None:
            return "Usage: /pause &lt;sender&gt; &lt;recipient&gt;"
        now = self.app.clock.now()
        async with self.app.session_factory() as s, s.begin():
            snap = await self.app.manager.pause(s, *pair, until=None, reason="paused by operator", now=now)
        if snap is None:
            return "That relationship is not on the watchlist."
        self.app.cache.put(snap)
        return "⏸ Paused. Use /resume to re-enable."

    async def resume(self, args) -> str:
        pair = self._pair_args(args)
        if pair is None:
            return "Usage: /resume &lt;sender&gt; &lt;recipient&gt;"
        async with self.app.session_factory() as s, s.begin():
            ok = await self.app.manager.resume(s, *pair, self.app.clock.now())
        if not ok:
            return "That relationship is not on the watchlist."
        snap = await self.app.analysis.analyze_pair(*pair)
        return f"▶️ Resumed – status now {snap.status.value if snap else 'n/a'}."


async def run_command_loop(client: TelegramClient, chat_id: str, handler: CommandHandler, stop: asyncio.Event) -> None:
    offset: int | None = None
    delay = 1.0
    while not stop.is_set():
        try:
            updates = await client.get_updates(offset, timeout=25)
            delay = 1.0
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram getUpdates failed", error=str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat != str(chat_id) or not text.startswith("/"):
                continue  # only the configured chat may query the bot
            try:
                reply = await handler.handle(text)
            except Exception:  # noqa: BLE001
                log.exception("Command failed", command=text[:40])
                reply = "⚠️ Command failed; see logs."
            if reply:
                try:
                    await client.send_message(chat_id, reply)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to send command reply", error=str(exc)[:200])
