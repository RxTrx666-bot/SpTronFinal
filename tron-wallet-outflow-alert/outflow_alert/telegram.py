"""Telegram Bot API client (send-only) and alert formatting."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

import httpx

from outflow_alert.amounts import format_units
from outflow_alert.storage import MatchedTransfer


class SendError(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.fatal = fatal


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # The token is part of the URL: never log request URLs (httpx logger is silenced in logging setup).
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/", timeout=httpx.Timeout(timeout), transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            resp = await self._client.post(method, json=payload)
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
            raise SendError(f"rate limited: {desc}", retry_after=float(retry_after))
        # 400/401/403/404 are configuration problems (bad token, chat id, bot not in chat).
        raise SendError(f"telegram {resp.status_code}: {desc}", fatal=resp.status_code in (401, 404))

    async def get_me(self) -> dict:
        return await self.call("getMe", {})

    async def send_message(self, chat_id: str, text: str) -> None:
        await self.call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )


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
