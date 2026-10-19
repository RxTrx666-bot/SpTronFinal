"""Telegram Bot API client, alert and status formatting."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from outflow_alert.amounts import format_units
from outflow_alert.storage import MatchedTransfer


class SendError(Exception):
    def __init__(
        self, message: str, *, retry_after: float | None = None, fatal: bool = False, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.fatal = fatal
        self.status = status


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # The token is part of the URL: never log request URLs (httpx logger is silenced in logging setup).
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
            raise SendError(f"network: {type(exc).__name__}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("ok"):
            return data.get("result")
        desc = str(data.get("description") or resp.text)[:200]
        if resp.status_code == 429:
            retry_after = (data.get("parameters") or {}).get("retry_after", 5)
            raise SendError(f"rate limited: {desc}", retry_after=float(retry_after), status=429)
        # 400/401/403/404 are configuration problems (bad token, chat id, bot not in chat).
        raise SendError(
            f"telegram {resp.status_code}: {desc}", fatal=resp.status_code in (401, 404), status=resp.status_code
        )

    async def get_me(self) -> dict:
        return await self.call("getMe", {})

    async def send_message(self, chat_id: str, text: str, *, reply_markup: dict | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.call("sendMessage", payload)

    async def edit_message(self, chat_id: str, message_id: int, text: str, *, reply_markup: dict | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            await self.call("editMessageText", payload)
        except SendError as exc:
            if "message is not modified" not in str(exc):
                raise


def _fmt_time(ts_ms: int | None) -> str:
    if not ts_ms:
        return "unknown"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_alert(
    t: MatchedTransfer,
    *,
    decimals: int,
    confirmation_mode: str,
    tx_url: str,
    address_url: str,
) -> str:
    e = html.escape
    amount = format_units(t.amount_raw, decimals)
    target = format_units(t.target_raw, decimals)
    exact = "exact match" if t.amount_raw == t.target_raw else f"≈ {target} filter"
    status = "solidified" if confirmation_mode == "solidified" else "in block, receipt SUCCESS"
    lines = [
        "🚨 <b>Outgoing USDT transfer</b>",
        "",
        f"💸 <b>{e(amount)} USDT</b>  <i>({e(exact)})</i>",
        f"📤 From: <a href=\"{e(address_url.format(address=t.from_address))}\"><code>{e(t.from_address)}</code></a>",
        f"📥 To: <a href=\"{e(address_url.format(address=t.to_address))}\"><code>{e(t.to_address)}</code></a>",
        f"🧱 Block: <code>{e(str(t.block_number or '?'))}</code> · {e(_fmt_time(t.block_ts))}",
        f"✅ Status: {e(status)}",
        f"🔗 <a href=\"{e(tx_url.format(txid=t.txid))}\">{e(t.txid[:16])}…</a>",
    ]
    return "\n".join(lines)


DIVIDER = "━━━━━━━━━━━━━━━━━━"


@dataclass(frozen=True)
class StatusSnapshot:
    paused: bool
    poll_error: str | None
    wallet: str
    contract: str
    last_latency_ms: float | None
    avg_latency_ms: float | None
    poll_interval_seconds: float
    confirmation_mode: str
    last_poll_ms: int | None
    transfers_checked: int
    now_ms: int


def _fmt_seconds(value: float) -> str:
    return f"{int(value)}s" if float(value).is_integer() else f"{value:g}s"


def _fmt_ago(ts_ms: int | None, now: int) -> str:
    if not ts_ms:
        return "never"
    age = max(0, (now - ts_ms) // 1000)
    if age < 60:
        ago = f"{age}s ago"
    elif age < 3600:
        ago = f"{age // 60}m ago"
    else:
        ago = f"{age // 3600}h {age % 3600 // 60}m ago"
    return f"{_fmt_time(ts_ms)} ({ago})"


def format_status(s: StatusSnapshot) -> str:
    e = html.escape
    if s.paused:
        status = monitoring = "⏸️ PAUSED"
    else:
        status = "▶️ RUNNING"
        if s.poll_error:
            monitoring = "🔴 ERROR"
        elif s.last_poll_ms is None:
            monitoring = "🟡 STARTING"
        else:
            monitoring = "🟢 ACTIVE"
    if s.last_latency_ms is None:
        latency = "n/a"
    else:
        latency = f"{round(s.last_latency_ms)} ms (avg {round(s.avg_latency_ms or s.last_latency_ms)} ms)"
    lines = [
        "📊 BOT STATUS",
        DIVIDER,
        f"🚦 Status: {status}",
        f"📡 Monitoring: {monitoring}",
        DIVIDER,
        "👛 Wallet:",
        e(s.wallet),
        "🌐 Network: TRON Mainnet",
        "💎 Token: USDT TRC-20",
        "📜 Contract:",
        e(s.contract),
        DIVIDER,
        f"⚡ API latency: {latency}",
        f"🛰️ Mode: account · poll {_fmt_seconds(s.poll_interval_seconds)} · {e(s.confirmation_mode)}",
        f"🕒 Last poll: {_fmt_ago(s.last_poll_ms, s.now_ms)}",
        f"🔁 Transfers checked: {s.transfers_checked:,}",
    ]
    if s.poll_error and not s.paused:
        lines.append(f"⚠️ Last error: {e(s.poll_error[:150])}")
    return "\n".join(lines)
