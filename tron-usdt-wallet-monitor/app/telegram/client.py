"""Minimal Telegram Bot API client (sendMessage, getUpdates, ...).

The bot token is part of the request URL; httpx request logging is disabled
and every log line passes through the secret redactor.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.logging_setup import get_logger

log = get_logger(__name__)


class TelegramError(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.telegram.org",
        min_interval: float = 1.1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=f"{api_url.rstrip('/')}/bot{token}/", timeout=httpx.Timeout(40.0), transport=transport)
        self._min_interval = min_interval
        self._last_send = 0.0
        self._send_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        try:
            resp = await self._client.post(method, json=payload, **({"timeout": timeout} if timeout else {}))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TelegramError(f"network error: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("ok"):
            return data.get("result")
        desc = str(data.get("description") or resp.text)[:200]
        retry_after = (data.get("parameters") or {}).get("retry_after") if resp.status_code == 429 else None
        raise TelegramError(f"telegram {resp.status_code}: {desc}", retry_after=float(retry_after) if retry_after else None)

    async def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> str:
        """Send with per-bot pacing (Telegram allows ~1 msg/s per chat)."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with self._send_lock:
            wait = self._min_interval - (time.monotonic() - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                result = await self.call("sendMessage", payload)
            finally:
                self._last_send = time.monotonic()
        return str((result or {}).get("message_id", ""))

    async def edit_message(self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str) -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id})
        except TelegramError:
            pass

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload, timeout=timeout + 10) or []

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
