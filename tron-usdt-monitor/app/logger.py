"""Structured logging with secret redaction.

Every log line passes through ``RedactingFormatter`` which replaces any
configured secret (bot token, API key, key-bearing URL parts) with ``***``,
including inside exception tracebacks.

Usage: ``log.info("event_name", extra=kv(field=value, ...))``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

_SECRETS: list[str] = []


def kv(**fields: Any) -> dict[str, Any]:
    return {"ctx": fields}


def register_secrets(values: Iterable[str]) -> None:
    for value in values:
        if value and value not in _SECRETS:
            _SECRETS.append(value)
    _SECRETS.sort(key=len, reverse=True)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def _fmt_value(value: Any) -> str:
    text = str(value)
    return json.dumps(text) if (" " in text or not text) else text


class RedactingFormatter(logging.Formatter):
    def __init__(self, json_mode: bool = False) -> None:
        super().__init__()
        self.json_mode = json_mode

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        ts_text = ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        ctx = getattr(record, "ctx", None) or {}
        message = record.getMessage()
        exc_text = self.formatException(record.exc_info) if record.exc_info else None
        if self.json_mode:
            payload: dict[str, Any] = {
                "ts": ts_text,
                "level": record.levelname,
                "logger": record.name,
                "event": message,
                **{k: v for k, v in ctx.items()},
            }
            if exc_text:
                payload["exception"] = exc_text
            line = json.dumps(payload, default=str, ensure_ascii=False)
        else:
            fields = " ".join(f"{k}={_fmt_value(v)}" for k, v in ctx.items())
            line = f"{ts_text} {record.levelname:<7} {record.name} | {message}"
            if fields:
                line += f" | {fields}"
            if exc_text:
                line += "\n" + exc_text
        return redact(line)


def setup_logging(level: str = "INFO", fmt: str = "text", secrets: Iterable[str] = ()) -> None:
    register_secrets(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(json_mode=(fmt == "json")))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx/httpcore log full request URLs (the Telegram URL contains the bot token).
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
