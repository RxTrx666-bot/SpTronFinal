"""Structured logging with secret redaction.

Text:  ``2026-10-01T14:31:02.184Z INFO POISONING_CANDIDATE victim=T.. tx=.. similarity=94``
JSON:  one object per line (LOG_FORMAT=json).

Usage: ``log = get_logger(__name__)`` then ``log.info("EVENT_TYPE", key=value)``.

Every registered secret (API key, bot token, X credentials) is replaced with
``***`` in every formatted line, including exception tracebacks and messages
from third-party libraries (e.g. httpx logs URLs containing the bot token).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

_SECRETS: list[str] = []
_BOT_URL = re.compile(r"(api\.telegram\.org/bot)[^/\s]+")
_SINKS: list[Callable[[logging.LogRecord, str], None]] = []


def register_secrets(values: list[str]) -> None:
    for v in values:
        if v and v not in _SECRETS:
            _SECRETS.append(v)
    _SECRETS.sort(key=len, reverse=True)


def redact(text: str) -> str:
    for s in _SECRETS:
        if s in text:
            text = text.replace(s, "***")
    return _BOT_URL.sub(r"\1***", text)


def add_sink(fn: Callable[[logging.LogRecord, str], None]) -> None:
    """Register a callback receiving (record, redacted message) - used for the system_logs table."""
    _SINKS.append(fn)
    _attach_sink_handler()


def _attach_sink_handler() -> None:
    root = logging.getLogger()
    if _SINK_HANDLER not in root.handlers:
        root.addHandler(_SINK_HANDLER)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)  # WARNING+ records must reach the system_logs sink


def remove_sink(fn: Callable[[logging.LogRecord, str], None]) -> None:
    if fn in _SINKS:
        _SINKS.remove(fn)


class _Formatter(logging.Formatter):
    def __init__(self, fmt: str) -> None:
        super().__init__()
        self.json = fmt == "json"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        fields: dict[str, Any] = getattr(record, "fields", {}) or {}
        if self.json:
            payload = {"ts": ts, "level": record.levelname, "logger": record.name, "event": record.getMessage()}
            payload.update({k: _jsonable(v) for k, v in fields.items()})
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return redact(json.dumps(payload, ensure_ascii=False))
        extra = " ".join(f"{k}={_text(v)}" for k, v in fields.items())
        line = f"{ts} {record.levelname} {record.getMessage()}" + (f" {extra}" if extra else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return redact(line)


class _SinkHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        if not _SINKS:
            return
        try:
            msg = redact(record.getMessage())
            for sink in list(_SINKS):
                sink(record, msg)
        except Exception:
            pass


_SINK_HANDLER = _SinkHandler(level=logging.WARNING)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _text(v: Any) -> str:
    s = str(v)
    return f'"{s}"' if (" " in s or not s) else s


class StructuredLogger:
    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, exc_info: Any = None, **fields: Any) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(level, event, exc_info=exc_info, extra={"fields": fields}, stacklevel=3)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, exc_info: Any = None, **fields: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=exc_info, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=True, **fields)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(name)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(fmt))
    root.addHandler(handler)
    root.addHandler(_SINK_HANDLER)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "asyncio", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
